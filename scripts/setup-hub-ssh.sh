#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Give the hub its own SSH key for fleet operations
#
# 1. Creates the hub's fleet key (ed25519) in /etc/guardian/fleet_key, readable by
#    root only, and loads it into guardian-fleet-agent.service's ssh-agent
#    (deploy/hub/protect-fleet-key.sh). The private key never leaves the hub; Mac
#    keys are not copied.
# 2. Adds the public key to ~/.ssh/authorized_keys on every fleet host, locked to
#    the hub's Tailscale IP: from="<hub ip>",no-agent-forwarding,no-port-forwarding,no-X11-forwarding
# 3. Writes the hub's fleet file ~/.ssh/guardian_fleet.conf: one Host block per host id
#    (Tailscale IP, key selected through the agent socket with IdentityAgent) and makes sure
#    ~/.ssh/config starts with `Include ~/.ssh/guardian_fleet.conf`. The old managed block
#    (# BEGIN guardian-hub-fleet) in ~/.ssh/config is removed. Blocks the dashboard registered
#    (pinned host key) and hand-written blocks in the fleet file are never touched; running this
#    again only updates the blocks of the shape this script writes. Design: docs/design/node-registration.md
# 4. Verifies hub -> host SSH for every host.
#
# Usage:
#   scripts/setup-hub-ssh.sh [--revoke [--key KEY] | --rotate] [--dry-run] [--offline] [host ...]
#
#   --revoke   Remove the hub key from the hosts. The key is matched by its exact public key body
#              (never by the shared "guardian-hub-fleet" comment), so revoking an old key cannot
#              remove a newly installed one. KEY selects which key: a public key line, a file holding
#              one, or a SHA256:... fingerprint; default is the hub's current key
#              (~/.ssh/guardian_fleet.pub on the hub). authorized_keys is copied to
#              authorized_keys.guardian-bak-<time> before it is edited.
#              With no hosts given it also removes, on the hub,
#              the fleet file blocks this script wrote (dashboard-registered and hand-written blocks
#              stay) and the legacy block in ~/.ssh/config; the Include line is removed only when
#              the fleet file ends up without any Host block (then the file is deleted too).
#   --rotate   Replace the hub fleet key: (1) create a new ed25519 key in a private temp dir on the
#              hub, (2) add it to every host next to the old one, (3) verify login with the NEW key
#              on every host. Any failure aborts, removes the new key again and leaves the old key
#              in place. (4) Only when all hosts verified, install the new key as /etc/guardian/fleet_key
#              (old one kept as fleet_key.old-<time>), reload guardian-fleet-agent, check the login
#              through the agent, then (5) remove the old key from each host by its body; a host
#              where the check failed keeps its old key. Prefer running it for the whole fleet.
#   --dry-run  With --revoke or --rotate: only report what would change (reads only).
#   --offline  Use the checkout's configs/servers.json snapshot instead of the hub inventory
#   host ...   Default: every host in the hub inventory (~/.guardian/data/servers.json, fetched
#              read-only over ssh) with a 100.64.x Tailscale IP. Apply skips explicit ids that are
#              not in that inventory (a deleted node is never given the hub key again); --revoke
#              still accepts them, which is how a deleted node's key is removed.
#
# Runs from the admin Mac, which already has SSH access to every host.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
MARKER="guardian-hub-fleet"

REVOKE=0
ROTATE=0
DRYRUN=0
KEY_SPEC=""
HOSTS=()
want_key=0
for arg in "$@"; do
    if [ "${want_key}" = 1 ]; then KEY_SPEC="${arg}"; want_key=0; continue; fi
    case "${arg}" in
        --revoke) REVOKE=1 ;;
        --rotate) ROTATE=1 ;;
        --dry-run) DRYRUN=1 ;;
        --key) want_key=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '3,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: ${arg}" >&2; exit 2 ;;
        *) HOSTS+=("${arg}") ;;
    esac
done
[ "${want_key}" = 0 ] || { echo "--key needs a value" >&2; exit 2; }
[ "${REVOKE}" = 0 ] || [ "${ROTATE}" = 0 ] || { echo "--revoke and --rotate cannot be combined" >&2; exit 2; }
[ -z "${KEY_SPEC}" ] || [ "${REVOKE}" = 1 ] || { echo "--key only applies to --revoke" >&2; exit 2; }
[ "${DRYRUN}" = 0 ] || [ "${REVOKE}${ROTATE}" != 00 ] || { echo "--dry-run needs --revoke or --rotate" >&2; exit 2; }
EXPLICIT_HOSTS=${#HOSTS[@]}

# The hub inventory (not the Mac snapshot) decides which hosts exist.
inventory_load
INVENTORY="${INVENTORY_FILE}"
HUB="$(python3 -c 'import json,sys; print(next(s["id"] for s in json.load(open(sys.argv[1])) if s.get("guardian_role")=="hub"))' "${INVENTORY}")"
HUB_IP="$(python3 -c 'import json,sys; print(next(s["tailscale_ip"] for s in json.load(open(sys.argv[1])) if s["id"]==sys.argv[2]))' "${INVENTORY}" "${HUB}")"
if [ ${#HOSTS[@]} -eq 0 ]; then
    while IFS= read -r h; do HOSTS+=("${h}"); done < <(
        python3 -c 'import json,sys; [print(s["id"]) for s in json.load(open(sys.argv[1])) if str(s.get("tailscale_ip","")).startswith("100.64.")]' "${INVENTORY}"
    )
fi

# OPS-6: applying must not give the hub key to a node that was removed from the inventory.
if [ "${REVOKE}" != 1 ] && [ "${EXPLICIT_HOSTS}" -gt 0 ]; then
    known=" $(python3 -c 'import json,sys; print(" ".join(s["id"] for s in json.load(open(sys.argv[1]))))' "${INVENTORY}") "
    kept_hosts=()
    for h in "${HOSTS[@]}"; do
        case "${known}" in
            *" ${h} "*) kept_hosts+=("${h}") ;;
            *) echo "⏭️  ${h}: 허브 인벤토리에 없어 건너뜁니다 (삭제된 노드에는 키를 다시 넣지 않습니다. 키 회수는 --revoke ${h})." >&2 ;;
        esac
    done
    if [ ${#kept_hosts[@]} -eq 0 ]; then echo "등록할 호스트가 없습니다." >&2; exit 0; fi
    HOSTS=("${kept_hosts[@]}")
fi

ssh_to() { local h="$1"; shift; ssh -o BatchMode=yes -o ConnectTimeout=10 "${h}" "$@"; }

# Runs on the hub (python3, piped through base64 so no quoting is needed). Input on stdin:
# {"mode": "apply"|"revoke", "marker": ..., "hosts": [{id, hostname, user, port}]}.
# Only blocks of the exact migrated shape (StrictHostKeyChecking accept-new) are ever replaced or
# removed; anything else in the fleet file is kept byte for byte.
read -r -d '' REMOTE_FLEET_PY <<'EOF' || true
import json, os, re, sys, tempfile

spec = json.load(sys.stdin)
mode, marker, hosts = spec["mode"], spec["marker"], spec["hosts"]
home = os.path.expanduser("~")
fleet = os.path.join(home, ".ssh", "guardian_fleet.conf")
config = os.path.join(home, ".ssh", "config")


def lock_fleet(path, timeout=30):
    """Advisory flock on <fleet file>.lock, the same lock the agent takes for dashboard writes
    (apps/agent/ssh_fleet_lock.go). Held until this short-lived process exits. Skipped with a
    notice where flock is unavailable (non-Linux hub), as before; a held lock that does not clear
    within the timeout aborts instead of racing the agent."""
    try:
        import fcntl, time
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        handle = open(path + ".lock", "a")
    except (ImportError, OSError) as e:
        print(f"  note: fleet file lock unavailable ({e}); continuing without it", file=sys.stderr)
        return None
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.time() > deadline:
                sys.exit("the fleet file is locked by another process (the agent is registering a node?); retry in a moment")
            time.sleep(0.1)


_fleet_lock = lock_fleet(fleet)
INCLUDE = "Include ~/.ssh/guardian_fleet.conf"
INCLUDE_FORMS = {"~/.ssh/guardian_fleet.conf", fleet, "guardian_fleet.conf"}
HEADER = ("# Managed by Server Guardian (agent and scripts/setup-hub-ssh.sh).\n"
          "# Host blocks of the fixed shape are rewritten by the agent; any other block is left alone.\n\n")
FIXED = ["    IdentityFile ~/.ssh/guardian_fleet.pub",
         "    IdentityAgent /run/guardian-fleet/agent.sock",
         "    IdentitiesOnly yes"]
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
IP_RE = re.compile(r"(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write(path, text, backup=False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if backup and os.path.exists(path):
        write(path + ".bak", read(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".tmp-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def directive(line):
    s = line.strip()
    if not s or s.startswith("#"):
        return None, []
    parts = re.split(r"[\s=]+", s, maxsplit=1)
    return parts[0].lower(), [a.strip('"') for a in (parts[1].split() if len(parts) > 1 else [])]


def split(text):
    pre, blocks, cur = [], [], None
    for line in text.splitlines(keepends=True):
        kw, args = directive(line)
        if kw in ("host", "match"):
            cur = {"lines": [line], "patterns": args if kw == "host" else []}
            blocks.append(cur)
        elif cur is None:
            pre.append(line)
        else:
            cur["lines"].append(line)
    return "".join(pre), blocks


def text_of(block):
    return "".join(block["lines"])


def classify(block):
    """Returns the block's fields when it is exactly one of the two managed shapes, else None."""
    lines = block["lines"]
    end = len(lines)
    while end > 1 and (not lines[end - 1].strip() or lines[end - 1].strip().startswith("#")):
        end -= 1
    body = [l.rstrip("\n") for l in lines[:end]]
    if len(block["patterns"]) != 1 or len(body) < 8 or body[0] != "Host " + block["patterns"][0]:
        return None
    alias = block["patterns"][0]
    m = [re.fullmatch(r"    HostName (\S+)", body[1]), re.fullmatch(r"    User (\S+)", body[2]),
         re.fullmatch(r"    Port (\d+)", body[3])]
    if not all(m) or body[4:7] != FIXED:
        return None
    rest = body[7:]
    if rest == ["    StrictHostKeyChecking accept-new"]:
        shape = "migrated"
    elif rest == ["    StrictHostKeyChecking yes", "    UserKnownHostsFile ~/.ssh/guardian_known_hosts",
                  "    HostKeyAlias " + alias]:
        shape = "registered"
    else:
        return None
    return {"alias": alias, "shape": shape, "trailer": "".join(lines[end:])}


def migrated_text(h):
    return "\n".join([f"Host {h['id']}", f"    HostName {h['hostname']}", f"    User {h['user']}",
                      f"    Port {h['port']}"] + FIXED + ["    StrictHostKeyChecking accept-new"]) + "\n"


def valid(h):
    return (isinstance(h.get("id"), str) and ID_RE.fullmatch(h["id"]) and isinstance(h.get("hostname"), str)
            and IP_RE.fullmatch(h["hostname"]) and isinstance(h.get("user"), str) and USER_RE.fullmatch(h["user"])
            and isinstance(h.get("port"), int) and 1 <= h["port"] <= 65535)


def join(pre, blocks):
    return pre + "".join(text_of(b) for b in blocks)


def include_effective(text):
    for line in text.splitlines():
        kw, args = directive(line)
        if kw in ("host", "match"):
            return False
        if kw == "include" and any(a in INCLUDE_FORMS for a in args):
            return True
    return False


old_block = re.compile(r"^# BEGIN %s.*?^# END %s[^\n]*\n?" % (re.escape(marker), re.escape(marker)), re.M | re.S)
cfg_before = read(config)
cfg = old_block.sub("", cfg_before)
pre, blocks = split(read(fleet))
added = updated = kept = removed = 0

if mode == "apply":
    for h in hosts:
        if not valid(h):
            print(f"  skip {h.get('id')!r}: id, IPv4 address, user or port is not valid", file=sys.stderr)
            continue
        want = migrated_text(h)
        existing = next((b for b in blocks if h["id"] in b["patterns"]), None)
        if existing is None:
            if blocks:
                last = blocks[-1]
                if not text_of(last).endswith("\n"):
                    last["lines"].append("\n")
                if not text_of(last).endswith("\n\n"):
                    last["lines"].append("\n")
            elif not pre:
                pre = HEADER
            elif not pre.endswith("\n\n"):
                pre += "\n" if pre.endswith("\n") else "\n\n"
            blocks.append({"lines": [want, "\n"], "patterns": [h["id"]]})
            added += 1
            continue
        info = classify(existing)
        if info is None:
            print(f"  keep {h['id']}: the fleet file has a block for it that is not in the managed shape", file=sys.stderr)
            kept += 1
        elif info["shape"] == "registered":
            kept += 1
        elif text_of(existing) != want + info["trailer"]:
            existing["lines"] = [want + info["trailer"]]
            updated += 1
        else:
            kept += 1
    new_fleet = join(pre, blocks)
    if new_fleet != read(fleet):
        write(fleet, new_fleet, backup=True)
    if not include_effective(cfg):
        cfg = INCLUDE + "\n\n" + cfg
else:
    ids = {h["id"] for h in hosts}
    keep_blocks = []
    for b in blocks:
        info = classify(b)
        if info and info["shape"] == "migrated" and info["alias"] in ids:
            removed += 1
        else:
            keep_blocks.append(b)
    if removed:
        write(fleet, join(pre, keep_blocks), backup=True)
    if not keep_blocks:
        # Nothing left that anyone wrote: drop the file and the line that pulls it in.
        if os.path.exists(fleet):
            os.remove(fleet)
        cfg = "".join(l for l in cfg.splitlines(keepends=True) if l.strip() != INCLUDE)

if cfg != cfg_before:
    backup = config + ".pre-guardian-fleet"
    if cfg_before and not os.path.exists(backup):
        write(backup, cfg_before)
    write(config, cfg)
print(f"  fleet file: added {added}, updated {updated}, kept {kept}, removed {removed}; "
      f"~/.ssh/config {'changed' if cfg != cfg_before else 'unchanged'}")
EOF

hub_fleet() {
    local mode="$1" spec b64
    spec="$(python3 - "${INVENTORY}" "${MARKER}" "${mode}" <<'PY'
import json, sys
inventory, marker, mode = sys.argv[1:]
hosts = [{"id": s["id"], "hostname": str(s["tailscale_ip"]), "user": s.get("user", "ubuntu"), "port": int(s.get("port", 22))}
         for s in json.load(open(inventory)) if str(s.get("tailscale_ip", "")).startswith("100.64.")]
print(json.dumps({"mode": mode, "marker": marker, "hosts": hosts}))
PY
)"
    b64="$(printf '%s' "${REMOTE_FLEET_PY}" | base64 | tr -d '\n')"
    printf '%s' "${spec}" | ssh_to "${HUB}" "python3 -c \"\$(printf %s '${b64}' | base64 -d)\""
}

# Edits ~/.ssh/authorized_keys on a host. Input (environment): ACTION, KEYLINE, MATCH_BODY, MATCH_FP.
#   apply   drop every line carrying the guardian-hub-fleet comment, then append KEYLINE (replace)
#   add     append KEYLINE unless a line with its key body is already there (keeps the other keys)
#   remove  delete the lines whose key body equals MATCH_BODY or whose fingerprint equals MATCH_FP,
#           after copying the file to authorized_keys.guardian-bak-<time>
#   count   print how many lines match, change nothing
read -r -d '' REMOTE_AUTH <<'EOF' || true
set -eu
f=~/.ssh/authorized_keys
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch "$f" && chmod 600 "$f"
matches() {
    local line="$1" fp
    if [ -n "${MATCH_BODY:-}" ] && printf '%s\n' "$line" | awk -v b="$MATCH_BODY" '{for(i=1;i<=NF;i++) if($i==b) found=1} END{exit !found}'; then return 0; fi
    if [ -n "${MATCH_FP:-}" ]; then
        fp="$(printf '%s\n' "$line" | ssh-keygen -lf /dev/stdin 2>/dev/null | awk '{print $2; exit}')" || true
        [ "$fp" = "$MATCH_FP" ] && return 0
    fi
    return 1
}
case "$ACTION" in
    apply)
        tmp="$(mktemp ~/.ssh/authorized_keys.XXXXXX)"
        grep -v "guardian-hub-fleet" "$f" > "$tmp" || true
        printf '%s\n' "$KEYLINE" >> "$tmp"
        chmod 600 "$tmp" && mv "$tmp" "$f"
        echo "marked lines: $(grep -c "guardian-hub-fleet" "$f" || true)" ;;
    add)
        MATCH_BODY="$(printf '%s\n' "$KEYLINE" | awk '{for(i=1;i<=NF;i++) if($i ~ /^ssh-/) {print $(i+1); exit}}')"
        MATCH_FP=""
        while IFS= read -r line || [ -n "$line" ]; do
            if matches "$line"; then echo "already present"; exit 0; fi
        done < "$f"
        printf '%s\n' "$KEYLINE" >> "$f"
        echo "added" ;;
    remove|count)
        n=0
        tmp="$(mktemp ~/.ssh/authorized_keys.XXXXXX)"
        while IFS= read -r line || [ -n "$line" ]; do
            if matches "$line"; then n=$((n + 1)); else printf '%s\n' "$line" >> "$tmp"; fi
        done < "$f"
        if [ "$ACTION" = remove ] && [ "$n" -gt 0 ]; then
            cp -p "$f" "$f.guardian-bak-$(date +%Y%m%d%H%M%S)"
            chmod 600 "$tmp" && mv "$tmp" "$f"
            echo "removed $n line(s); backup kept as authorized_keys.guardian-bak-*"
        else
            rm -f "$tmp"
            if [ "$ACTION" = remove ]; then echo "no matching line"; else echo "matching lines: $n"; fi
        fi ;;
    *) echo "unknown action $ACTION" >&2; exit 2 ;;
esac
EOF

# auth_run <host> <action> [keyline] [body] [fp]
auth_run() {
    local h="$1" action="$2" keyline="${3:-}" body="${4:-}" fp="${5:-}"
    ssh_to "${h}" "ACTION=$(printf '%q' "${action}") KEYLINE=$(printf '%q' "${keyline}") MATCH_BODY=$(printf '%q' "${body}") MATCH_FP=$(printf '%q' "${fp}") bash -s" <<<"${REMOTE_AUTH}"
}

# Sets MATCH_BODY / MATCH_FP from a public key line, a file holding one, or a SHA256: fingerprint.
resolve_key_spec() {
    local spec="$1"
    MATCH_BODY=""; MATCH_FP=""
    case "${spec}" in
        SHA256:*) MATCH_FP="${spec}" ;;
        ssh-*) MATCH_BODY="$(awk '{print $2}' <<<"${spec}")" ;;
        *) [ -f "${spec}" ] || { echo "❌ --key: ${spec} is not a public key line, file or SHA256 fingerprint" >&2; exit 2; }
           MATCH_BODY="$(awk 'NF >= 2 {print $2; exit}' "${spec}")" ;;
    esac
    if [ -n "${MATCH_BODY}" ] && ! [[ "${MATCH_BODY}" =~ ^[A-Za-z0-9+/=]{40,}$ ]]; then
        echo "❌ not a valid public key body" >&2; exit 2
    fi
    [ -n "${MATCH_BODY}${MATCH_FP}" ] || { echo "❌ empty key" >&2; exit 2; }
}

hub_current_pub() { ssh_to "${HUB}" 'cat ~/.ssh/guardian_fleet.pub' | awk '$1 ~ /^ssh-/ {print; exit}'; }

if [ "${REVOKE}" = 1 ]; then
    if [ -n "${KEY_SPEC}" ]; then
        resolve_key_spec "${KEY_SPEC}"
    else
        cur="$(hub_current_pub)" || true
        [ -n "${cur}" ] || { echo "❌ cannot read the hub's current key (${HUB}:~/.ssh/guardian_fleet.pub); pass --key" >&2; exit 1; }
        resolve_key_spec "${cur}"
    fi
    action=remove
    mode_note=""
    if [ "${DRYRUN}" = 1 ]; then action=count; mode_note=" (dry-run)"; fi
    echo "🗝️  허브 키 회수${mode_note}: ${#HOSTS[@]}대, 키 ${MATCH_FP:-…${MATCH_BODY: -16}}"
    for h in "${HOSTS[@]}"; do
        printf '  %-22s ' "${h}"
        auth_run "${h}" "${action}" "" "${MATCH_BODY}" "${MATCH_FP}" || echo "❌ ssh 실패"
    done
    if [ "${EXPLICIT_HOSTS}" -eq 0 ] && [ "${DRYRUN}" = 0 ]; then
        echo "  허브 fleet 파일 정리 (이 스크립트가 쓴 블록만)"
        hub_fleet revoke
    fi
    exit 0
fi

# Key rotation: see the header. Everything on the hub happens over ssh; the new private key exists
# only in a 0700 temp dir on the hub until it is installed as /etc/guardian/fleet_key.
if [ "${ROTATE}" = 1 ]; then
    old_pub="$(hub_current_pub)" || true
    [ -n "${old_pub}" ] || { echo "❌ cannot read the hub's current key; run a plain apply first" >&2; exit 1; }
    resolve_key_spec "${old_pub}"
    old_body="${MATCH_BODY}"
    mode_note=""
    if [ "${DRYRUN}" = 1 ]; then mode_note=" (dry-run: 아무것도 바꾸지 않음)"; fi
    echo "🔄 허브 키 교체: ${#HOSTS[@]}대${mode_note}"
    echo "  현재 키: ${old_pub%% *} …${old_body: -16}"
    if [ "${DRYRUN}" = 1 ]; then
        echo "  계획: 새 키 생성 -> 각 호스트에 추가 -> 새 키로 로그인 확인 -> /etc/guardian/fleet_key 교체 -> 옛 키 삭제"
        for h in "${HOSTS[@]}"; do
            printf '  %-22s ' "${h}"
            auth_run "${h}" count "" "${old_body}" "" || echo "❌ ssh 실패 (접속 불가 호스트가 있으면 실제 교체는 중단됩니다)"
        done
        exit 0
    fi
    stage="$(ssh_to "${HUB}" 'umask 077; d="$(mktemp -d ~/.guardian-rotate.XXXXXX)"; ssh-keygen -q -t ed25519 -N "" -C "guardian-hub-fleet@$(hostname)" -f "$d/key" && echo "$d"')"
    case "${stage}" in /*) ;; *) echo "❌ 새 키를 만들지 못했습니다" >&2; exit 1 ;; esac
    new_pub="$(ssh_to "${HUB}" "cat ${stage}/key.pub")"
    resolve_key_spec "${new_pub}"
    new_body="${MATCH_BODY}"
    new_line="from=\"${HUB_IP}\",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ${new_pub}"
    discard_stage() { ssh_to "${HUB}" "rm -rf ${stage}" || echo "⚠️  ${HUB}:${stage}를 직접 지우세요" >&2; }
    rollback() {
        echo "↩️  롤백: 새 키를 호스트에서 다시 제거합니다 (옛 키는 그대로)" >&2
        for h in "${HOSTS[@]}"; do auth_run "${h}" remove "" "${new_body}" "" >/dev/null 2>&1 || true; done
        discard_stage
    }
    echo "📥 1/4 새 키를 모든 호스트에 추가 (옛 키 유지)"
    for h in "${HOSTS[@]}"; do
        printf '  %-22s ' "${h}"
        if ! auth_run "${h}" add "${new_line}"; then
            echo "❌ ssh 실패"; echo "중단: ${h}에 새 키를 넣지 못했습니다." >&2; rollback; exit 1
        fi
    done
    echo "🩺 2/4 새 키로 허브 -> 호스트 로그인 확인 (임시 키 파일 사용)"
    bad=0
    for h in "${HOSTS[@]}"; do
        if out="$(ssh_to "${HUB}" "ssh -i ${stage}/key -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR ${h} hostname" 2>&1)"; then
            printf '  ✅ %-22s %s\n' "${h}" "${out}"
        else
            printf '  ❌ %-22s %s\n' "${h}" "$(tail -n 1 <<<"${out}" | cut -c1-90)"; bad=$((bad + 1))
        fi
    done
    if [ "${bad}" -gt 0 ]; then
        echo "중단: ${bad}대에서 새 키 로그인이 실패했습니다. 옛 키는 어디서도 지우지 않았습니다." >&2
        rollback; exit 1
    fi
    echo "🔁 3/4 허브 키 교체 (/etc/guardian/fleet_key, 옛 키는 fleet_key.old-*로 보관) 후 agent 다시 로드"
    ssh_to "${HUB}" "set -e; sudo -n cp -p /etc/guardian/fleet_key /etc/guardian/fleet_key.old-\$(date +%Y%m%d%H%M%S); sudo -n install -m 600 -o root -g root ${stage}/key /etc/guardian/fleet_key"
    got="$(ssh_to "${HUB}" 'bash -s' < "${ROOT_DIR}/deploy/hub/protect-fleet-key.sh" | tail -n 1)" || got=""
    discard_stage
    if [ "$(awk '{print $2}' <<<"${got}")" != "${new_body}" ]; then
        echo "❌ 허브가 새 키를 올리지 못했습니다. 옛 키는 호스트에 그대로 있고, 옛 개인키는 허브의 /etc/guardian/fleet_key.old-*에 있습니다 (되돌리기: 그 파일을 fleet_key로 복사한 뒤 protect-fleet-key.sh 다시 실행)." >&2
        exit 1
    fi
    echo "🗑️  4/4 agent 경로로 로그인이 확인된 호스트에서만 옛 키 삭제 (본문 일치)"
    ok=0; fail=0
    for h in "${HOSTS[@]}"; do
        printf '  %-22s ' "${h}"
        if ssh_to "${HUB}" "ssh -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR ${h} true" >/dev/null 2>&1; then
            if auth_run "${h}" remove "" "${old_body}" ""; then ok=$((ok + 1)); else echo "❌ 옛 키 삭제 실패"; fail=$((fail + 1)); fi
        else
            echo "❌ 새 키 로그인 실패: 옛 키를 남깁니다"; fail=$((fail + 1))
        fi
    done
    echo "결과: 옛 키 삭제 ${ok} · 남김/실패 ${fail}. /etc/guardian/fleet_key.old-*는 확인 후 허브에서 지우세요."
    [ "${fail}" -eq 0 ]
    exit 0
fi

echo "🔑 허브(${HUB}, ${HUB_IP}) 전용 키 준비"
ssh_to "${HUB}" 'sudo -n tee /etc/systemd/system/guardian-fleet-agent.service >/dev/null' \
    < "${ROOT_DIR}/deploy/systemd/guardian-fleet-agent.service"
pub="$(ssh_to "${HUB}" 'bash -s' < "${ROOT_DIR}/deploy/hub/protect-fleet-key.sh" | tail -n 1)"
case "${pub}" in ssh-ed25519\ *) ;; *) echo "❌ 공개키를 읽지 못했습니다" >&2; exit 1 ;; esac
keyline="from=\"${HUB_IP}\",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ${pub}"
echo "  ${pub%% *} …${pub: -40}"

echo "📥 authorized_keys 등록: ${#HOSTS[@]}대 (from=\"${HUB_IP}\")"
for h in "${HOSTS[@]}"; do
    printf '  %-22s ' "${h}"
    auth_run "${h}" apply "${keyline}" || echo "❌ ssh 실패"
done

echo "⚙️  허브 ~/.ssh/guardian_fleet.conf 작성 + ~/.ssh/config Include"
hub_fleet apply

echo "🩺 허브 → 각 노드 SSH 확인"
ok=0; fail=0
for h in "${HOSTS[@]}"; do
    if out="$(ssh_to "${HUB}" "ssh -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR ${h} hostname" 2>&1)"; then
        printf '  ✅ %-22s %s\n' "${h}" "${out}"; ok=$((ok + 1))
    else
        printf '  ❌ %-22s %s\n' "${h}" "$(tail -n 1 <<<"${out}" | cut -c1-90)"; fail=$((fail + 1))
    fi
done
echo "결과: 성공 ${ok} · 실패 ${fail}"
[ "${fail}" -eq 0 ]

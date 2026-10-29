#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Install the Guardian skills for the LLM CLIs on the hub and the Tier-1 hosts
#
# The skills in deploy/llm-skills/ (SKILL.md with name/description frontmatter, the format claude, codex and agy
# share) are copied to the user skill directory of every LLM CLI a host has:
#   claude ~/.claude/skills/<name>/   codex ~/.codex/skills/<name>/   agy ~/.gemini/config/skills/<name>/
# deploy/llm-skills/manifest.json says where each skill goes: "all" (the hub and every Tier-1 host) or "hub".
# Only skills named guardian-* are managed: one this repository no longer has, or no longer gives a host, is removed
# from it; skills of other names are never touched. The debate engine is unaffected: its claude runs have no tools
# (--tools "") and its agy runs pass --disable-slash-commands.
#
# Usage:
#   scripts/deploy-llm-skills.sh [--dry-run] [--offline] [host ...]
#
#   --dry-run   Print the hosts, their CLIs and the skills each would get, without changing anything
#   --offline   Use the local configs/servers.json snapshot instead of the hub inventory
#   host ...    Default: the hub and every host with "tier": "Tier-1" in the hub inventory
# Runs from the admin Mac.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR  # read by lib/inventory.sh
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"

SKILLS_DIR="${ROOT_DIR}/deploy/llm-skills"
DRY_RUN=0
HOSTS=()
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: ${arg}" >&2; exit 2 ;;
        *) HOSTS+=("${arg}") ;;
    esac
done

inventory_load
hub="$(python3 -c 'import json,sys; print(next(s["id"] for s in json.load(open(sys.argv[1])) if s.get("guardian_role") == "hub"))' \
    "${INVENTORY_FILE}")"
if [ ${#HOSTS[@]} -eq 0 ]; then
    while IFS= read -r host; do HOSTS+=("${host}"); done < <(
        python3 -c 'import json,sys
for s in json.load(open(sys.argv[1])):
    if s.get("guardian_role") == "hub" or s.get("tier") == "Tier-1":
        print(s["id"])' "${INVENTORY_FILE}"
    )
fi

# skills_for <host>: the skill names the manifest gives that host, one per line.
skills_for() {
    python3 - "${SKILLS_DIR}/manifest.json" "$1" "${hub}" <<'PY'
import json, re, sys
manifest, host, hub = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
for name, scope in sorted(manifest.items()):
    if not re.fullmatch(r"guardian-[a-z0-9-]+", name) or scope not in ("all", "hub"):
        sys.exit(f"manifest: {name}: names must be guardian-*, scopes all or hub")
    if scope == "all" or host == hub:
        print(name)
PY
}

for name in $(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))))' "${SKILLS_DIR}/manifest.json"); do
    [ -f "${SKILLS_DIR}/${name}/SKILL.md" ] || { echo "❌ ${SKILLS_DIR}/${name}/SKILL.md가 없습니다." >&2; exit 1; }
done

# Runs on the host: finds the CLIs (login shell PATH), then replaces the guardian-* skills of each CLI's directory with
# the ones in the tar on stdin. WANTED is the space-separated skill list for this host.
REMOTE_INSTALL='set -euo pipefail
roots=()
for pair in "claude:$HOME/.claude/skills" "codex:$HOME/.codex/skills" "agy:$HOME/.gemini/config/skills"; do
    cli="${pair%%:*}"; dir="${pair#*:}"
    if bash -lc "command -v ${cli}" >/dev/null 2>&1; then roots+=("${dir}"); echo "  ${cli}: ${dir}"; fi
done
[ ${#roots[@]} -gt 0 ] || { echo "  LLM CLI 없음: 건너뜀"; cat >/dev/null; exit 0; }
stage="$(mktemp -d)"; trap "rm -rf \"${stage}\"" EXIT
tar -xzf - -C "${stage}"
for dir in "${roots[@]}"; do
    mkdir -p "${dir}"
    for old in "${dir}"/guardian-*; do
        [ -e "${old}" ] || continue
        case " ${WANTED} " in *" $(basename "${old}") "*) ;; *) rm -rf -- "${old}"; echo "  제거: ${old}" ;; esac
    done
    for name in ${WANTED}; do
        rm -rf -- "${dir}/${name}.new"; cp -R "${stage}/${name}" "${dir}/${name}.new"
        rm -rf -- "${dir}/${name}"; mv "${dir}/${name}.new" "${dir}/${name}"
    done
done
echo "  설치: ${WANTED}"'

failed=0
for host in "${HOSTS[@]}"; do
    wanted="$(skills_for "${host}" | tr '\n' ' ' | sed 's/ $//')"
    echo "🧩 ${host}: ${wanted}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        continue
    fi
    # shellcheck disable=SC2086 # the skill names are word lists on purpose
    if ! COPYFILE_DISABLE=1 tar --no-xattrs -C "${SKILLS_DIR}" -czf - ${wanted} \
        | ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" "WANTED='${wanted}' bash -c $(printf '%q' "${REMOTE_INSTALL}")"; then
        echo "  ❌ ${host}: 설치 실패" >&2
        failed=1
    fi
done
exit "${failed}"

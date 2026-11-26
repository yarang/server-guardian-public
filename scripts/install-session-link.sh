#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Install the workstation session link (S2 of docs/design/operator-sessions.md)
#
# Runs ON an operator workstation (macOS or WSL/Linux), from a checkout of this repository: it puts
# the six workstation modules into <home>/.guardian/session/lib, generates the workstation's own link
# key, pins the hub's host key from the fingerprint the operator copied from Settings -> Workstations
# (SD2, the only trust-on-first-use exception and it is gated by that fingerprint), writes the config
# and installs the daemon as a LaunchAgent (macOS) or a systemd user unit (WSL/Linux; systemd must be
# on). Native Windows is the separate PowerShell installer of phase S2w.
#
# Usage:
#   scripts/install-session-link.sh --workstation ID --hub-address IPV4 --hub-alias ALIAS \
#       --hub-user USER --hub-fingerprint SHA256:... [--hub-name NAME] [--ssh-port N] \
#       [--no-service] [--dry-run]
#   scripts/install-session-link.sh --status
#   scripts/install-session-link.sh --disable
#
#   --hub-fingerprint  The hub's host key fingerprint from Settings -> Workstations; a new hub host
#                      key is pinned only on an exact match, never on first sight.
#   --no-service       Install the files without the LaunchAgent/systemd user unit. Also the way
#                      when the home volume has APFS ownership disabled (see exit code 3 below).
#   --dry-run          Run the environment and argument checks, print what would happen, write nothing.
#   --status           Print the config (no key material), key and pin presence, the service state and
#                      whether the daemon's control socket exists; asks nothing over the network.
#   --disable          Kill switch: stop and remove the service, delete the link key; keeps the config,
#                      known_hosts and the spool.
#
# The workstation id, the hub address and the names are never interpolated into a shell command
# string: they travel as argv and into files only. The script never touches ~/.ssh, ~/.claude or any
# authorized_keys file, never writes skipDangerousModePermissionPrompt and never prints a bypass flag:
# the person picks the CLI's permission mode. The hooks block, the mcp add line and the allow list are
# printed, never applied.
#
# Exit codes: 2 usage or argument validation, 3 unsupported platform or missing tool, or (on macOS)
# the home volume has ownership disabled, 4 hub host key mismatch. GUARDIAN_SESSION_HOME overrides
# the home the session tree hangs under (tests only); the daemon gets the same value through --home.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_LIB="${ROOT_DIR}/cli/llm-agent"
MODULES=(session_local.py session_uplink.py session_daemon.py session_mcp.py
         session_hooks.py session_wake.py)
LABEL="dev.guardian.session-link"
UNIT_NAME="guardian-session-link.service"

# --- arguments -------------------------------------------------------------------------------

MODE=install
WORKSTATION="" HUB_ADDRESS="" HUB_ALIAS="" HUB_USER="" HUB_FINGERPRINT="" HUB_NAME="" SSH_PORT="22"
NO_SERVICE=0 DRY_RUN=0

need_value() {
    # $1 option name, $2 the value it came with: an option may not arrive without its value.
    if [ -z "$2" ]; then
        echo "$1 needs a value" >&2
        exit 2
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --workstation) need_value "$1" "${2:-}"; WORKSTATION="$2"; shift ;;
        --hub-address) need_value "$1" "${2:-}"; HUB_ADDRESS="$2"; shift ;;
        --hub-alias) need_value "$1" "${2:-}"; HUB_ALIAS="$2"; shift ;;
        --hub-user) need_value "$1" "${2:-}"; HUB_USER="$2"; shift ;;
        --hub-fingerprint) need_value "$1" "${2:-}"; HUB_FINGERPRINT="$2"; shift ;;
        --hub-name) [ "$#" -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
                     HUB_NAME="$2"; shift ;;   # the empty string is valid: no link name set (SD12)
        --ssh-port) need_value "$1" "${2:-}"; SSH_PORT="$2"; shift ;;
        --no-service) NO_SERVICE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --status) MODE=status ;;
        --disable) MODE=disable ;;
        -h|--help) sed -n '2,38p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) echo "unexpected argument: $1" >&2; exit 2 ;;
    esac
    shift
done

[ -n "${HOME:-}" ] || { echo "HOME is not set" >&2; exit 2; }
HOME_DIR="${GUARDIAN_SESSION_HOME:-$HOME}"
SESSION_ROOT="${HOME_DIR}/.guardian/session"

# --- helper functions ------------------------------------------------------------------------

pin_host_key() {
    # $1 the ssh-keyscan output file, $2 the expected fingerprint. Prints "type base64" of the
    # matching key on stdout (exit 0), or every fingerprint that did answer (exit 1), so the caller
    # can show both sides of a mismatch. The fingerprint itself comes from ssh-keygen -lf -.
    python3 - "$1" "$2" <<'PY'
import subprocess
import sys

scan_file, expected = sys.argv[1], sys.argv[2]
try:
    lines = open(scan_file, encoding="utf-8").read().splitlines()
except OSError:
    lines = []
scanned = []
for line in lines:
    fields = line.split()
    if not fields or line.startswith("#") or len(fields) < 3:
        continue
    result = subprocess.run(["ssh-keygen", "-lf", "-"], input=line + "\n",
                            capture_output=True, text=True)
    out = result.stdout.split()
    if len(out) < 2:
        continue
    if out[1] == expected:
        print(f"{fields[1]} {fields[2]}")
        sys.exit(0)
    scanned.append(out[1])
for fingerprint in scanned:
    print(fingerprint)
sys.exit(1)
PY
}

render_template() {
    # $1 template, $2 destination: replaces the four placeholders with python3 string replace
    # (no sed escaping rules to get wrong), taking the values as argv.
    python3 - "$1" "$2" "${PYTHON}" "${SESSION_ROOT}/lib" "${HOME_DIR}" "${SESSION_ROOT}" <<'PY'
import sys

template, dest, python, lib, home_dir, root = sys.argv[1:7]
with open(template, encoding="utf-8") as handle:
    text = handle.read()
for placeholder, value in (("@PYTHON@", python), ("@LIB@", lib),
                           ("@HOME_DIR@", home_dir), ("@ROOT@", root)):
    text = text.replace(placeholder, value)
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
}

print_finish_steps() {
    # Everything the person must do by hand; none of it is applied here. The JSON blocks are built
    # by python3 from argv, so a path with a space stays one argument and valid JSON.
    python3 - "${PYTHON}" "${SESSION_ROOT}" <<'PY'
import json
import sys

python, root = sys.argv[1], sys.argv[2]

def hook_command(event):
    return f'"{python}" "{root}/lib/session_hooks.py" {event}'

hooks = {"hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
                                 "command": hook_command("session-start"), "timeout": 5}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
                                     "command": hook_command("prompt-submit"), "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": hook_command("stop"), "timeout": 5}]}],
    "SessionEnd": [{"hooks": [{"type": "command",
                               "command": hook_command("session-end"), "timeout": 5}]}],
}}
allow = {"permissions": {"allow": [
    "mcp__guardian-session__session_inbox",
    "mcp__guardian-session__session_list",
    "mcp__guardian-session__session_status",
]}}
print("  2. Register the session MCP once per user (every interactive session sees it):")
print(f'     claude mcp add --scope user guardian-session -- "{python}" "{root}/lib/session_mcp.py"')
print()
print("  3. Merge this hooks block into ~/.claude/settings.json by hand (the four S2 events, each")
print("     bounded to 5 s so a hook can never hang the CLI):")
print(json.dumps(hooks, indent=2))
print()
print("  4. Suggested permissions.allow for the three read-only tools (in manual mode they need")
print("     allow rules; the operator confirms this list is wanted):")
print(json.dumps(allow, indent=2))
PY
}

do_status() {
    echo "session root: ${SESSION_ROOT}"
    if [ -f "${SESSION_ROOT}/config.json" ]; then
        echo "config:"
        sed 's/^/  /' "${SESSION_ROOT}/config.json"
    else
        echo "config: none (${SESSION_ROOT}/config.json)"
    fi
    if [ -f "${SESSION_ROOT}/link-key" ]; then
        echo "link key: present (${SESSION_ROOT}/link-key; the private half never leaves this machine)"
    else
        echo "link key: missing"
    fi
    if [ -f "${SESSION_ROOT}/known_hosts" ]; then
        echo "hub host key pin: present (${SESSION_ROOT}/known_hosts)"
    else
        echo "hub host key pin: missing"
    fi
    case "$(uname -s)" in
        Darwin)
            state="$(launchctl print "gui/$(id -u)/${LABEL}" 2>&1 | head -n 1 || true)"
            echo "service (${LABEL}): ${state:-not reported}"
            ;;
        Linux)
            state="$(systemctl --user is-active "${UNIT_NAME}" 2>&1 || true)"
            echo "service (${UNIT_NAME}): ${state:-not reported}"
            ;;
        *) echo "service: unknown platform $(uname -s)" ;;
    esac
    if [ -S "${SESSION_ROOT}/run/control.sock" ]; then
        echo "daemon: control socket present (${SESSION_ROOT}/run/control.sock)"
    else
        echo "daemon: control socket missing (the daemon is not running)"
    fi
}

do_disable() {
    echo "stopping and removing the service"
    case "$(uname -s)" in
        Darwin)
            PLIST="${HOME_DIR}/Library/LaunchAgents/${LABEL}.plist"
            launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
            rm -f "${PLIST}"
            echo "removed ${PLIST}"
            ;;
        Linux)
            UNIT="${HOME_DIR}/.config/systemd/user/${UNIT_NAME}"
            systemctl --user disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
            rm -f "${UNIT}"
            systemctl --user daemon-reload >/dev/null 2>&1 || true
            echo "removed ${UNIT}"
            ;;
        *) echo "unsupported platform $(uname -s): remove the service by hand" >&2 ;;
    esac
    rm -f "${SESSION_ROOT}/link-key" "${SESSION_ROOT}/link-key.pub"
    echo "deleted ${SESSION_ROOT}/link-key and its public half"
    echo "kept the config, known_hosts and the spool under ${SESSION_ROOT}"
    echo "remove this workstation in Settings -> Workstations to drop its line on the hub"
}

# --- mode dispatch ---------------------------------------------------------------------------

case "${MODE}" in
    status) do_status; exit 0 ;;
    disable) do_disable; exit 0 ;;
esac

need_value --workstation "${WORKSTATION}"
need_value --hub-address "${HUB_ADDRESS}"
need_value --hub-alias "${HUB_ALIAS}"
need_value --hub-user "${HUB_USER}"
need_value --hub-fingerprint "${HUB_FINGERPRINT}"
case "${SSH_PORT}" in
    ''|*[!0-9]*) echo "--ssh-port must be a number" >&2; exit 2 ;;
esac
case "${HUB_FINGERPRINT}" in
    SHA256:?*) ;;
    *) echo "--hub-fingerprint must look like SHA256:<base64>, as Settings -> Workstations shows it" >&2
       exit 2 ;;
esac

# --- 1/10 environment ------------------------------------------------------------------------

for tool in python3 ssh ssh-keygen ssh-keyscan; do
    command -v "${tool}" >/dev/null 2>&1 || { echo "tool not found on PATH: ${tool}" >&2; exit 3; }
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || { echo "python3 3.9 or newer is required" >&2; exit 3; }
PYVER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
SYSTEM="$(uname -s)"
case "${SYSTEM}" in
    Darwin) SERVICE_KIND=launchd ;;
    Linux) SERVICE_KIND=systemd ;;
    *) echo "unsupported platform ${SYSTEM}: native Windows uses the installer of phase S2w" >&2
       exit 3 ;;
esac
if [ "${SERVICE_KIND}" = "systemd" ] && [ "${NO_SERVICE}" -eq 0 ]; then
    if [ ! -d /run/systemd/system ] \
            || ! systemctl --user show-environment >/dev/null 2>&1; then
        echo "systemd is not running as a user session: in WSL turn systemd on" >&2
        echo "(/etc/wsl.conf [boot] systemd=true, then restart the WSL session), or pass --no-service" >&2
        exit 3
    fi
fi
if [ "${SERVICE_KIND}" = "launchd" ] && [ "${NO_SERVICE}" -eq 0 ]; then
    # A home on an APFS volume with ownership disabled (diskutil info: Owners: Disabled) makes
    # launchd refuse every plist in ~/Library/LaunchAgents with "Bootstrap failed: 5", so the
    # volume holding the home is checked before anything is written (under --dry-run too). df or
    # diskutil failing is only a warning; enabling ownership stays with the person: no sudo here.
    HOME_MOUNT="$(df -P "${HOME_DIR}" 2>/dev/null \
        | awk 'NR==2 {for (i=6;i<=NF;i++) printf "%s%s", $i, (i<NF?" ":""); print ""}')" || true
    if [ -z "${HOME_MOUNT}" ] || ! command -v diskutil >/dev/null 2>&1; then
        echo "1/10 environment: warning: could not read volume ownership of ${HOME_DIR}, continuing"
    elif OWNERSHIP_INFO="$(diskutil info "${HOME_MOUNT}" 2>/dev/null)"; then
        if grep -q 'Owners:[[:space:]]*Disabled' <<<"${OWNERSHIP_INFO}"; then
            echo "the home volume ${HOME_MOUNT} has ownership disabled (diskutil info: Owners: Disabled); launchd refuses LaunchAgents there (bootstrap error 5)" >&2
            echo "pass --no-service and run the daemon by hand, or enable ownership on that volume yourself (sudo diskutil enableOwnership ${HOME_MOUNT}) and run this again" >&2
            exit 3
        fi
    else
        echo "1/10 environment: warning: could not read volume ownership of ${HOME_DIR}, continuing"
    fi
fi
echo "1/10 environment: python3 ${PYVER}, ssh tools on PATH, ${SYSTEM} -> ${SERVICE_KIND}"

# --- 2/10 arguments (the same rules as session_local.load_config) ----------------------------

CANDIDATE="$(mktemp)"
SCAN_FILE="$(mktemp)"
trap 'rm -f "${CANDIDATE}" "${SCAN_FILE}"' EXIT

python3 - "${CANDIDATE}" "${WORKSTATION}" "${HUB_ADDRESS}" "${HUB_NAME}" "${HUB_ALIAS}" \
    "${HUB_USER}" "${SSH_PORT}" <<'PY'
import json
import sys

path, workstation, address, name, alias, user, port = sys.argv[1:8]
config = {"workstation": workstation, "hub_address": address, "hub_name": name,
          "hub_alias": alias, "hub_user": user, "ssh_port": int(port)}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

if ! python3 -c 'import sys
sys.path.insert(0, sys.argv[1])
import session_local
try:
    session_local.load_config(sys.argv[2])
except ValueError as exc:
    print(exc)
    sys.exit(1)' "${SRC_LIB}" "${CANDIDATE}"; then
    echo "2/10 arguments: invalid (see the message above)" >&2
    exit 2
fi
echo "2/10 arguments: valid (workstation ${WORKSTATION}, hub ${HUB_ADDRESS} as ${HUB_ALIAS})"

if [ "${DRY_RUN}" -eq 1 ]; then
    echo "(dry-run) steps 3-10 would, in order:"
    echo "  create ${SESSION_ROOT} and its lib/, spool/, run/ subdirectories, mode 0700"
    echo "  copy ${#MODULES[@]} modules from ${SRC_LIB} into ${SESSION_ROOT}/lib (0644, temp name + mv)"
    echo "  generate ${SESSION_ROOT}/link-key when missing (ed25519; an existing key is never overwritten)"
    echo "  ssh-keyscan -T 10 -t ed25519 -p ${SSH_PORT} ${HUB_ADDRESS}, compare with --hub-fingerprint,"
    echo "  and on an exact match pin ${HUB_ALIAS} into ${SESSION_ROOT}/known_hosts (0600)"
    echo "  write ${SESSION_ROOT}/config.json (0600) and run session_daemon.py --check-config"
    echo "  record the claude version into ${SESSION_ROOT}/cli-version"
    if [ "${NO_SERVICE}" -eq 0 ]; then
        echo "  render and install the ${SERVICE_KIND} service (${LABEL} / ${UNIT_NAME})"
    else
        echo "  skip the service (--no-service)"
    fi
    echo "  print the public key, the mcp add line, the hooks block and the allow list (never applied)"
    exit 0
fi

# --- 3/10 directories ------------------------------------------------------------------------

mkdir -p "${SESSION_ROOT}" "${SESSION_ROOT}/lib" "${SESSION_ROOT}/spool" "${SESSION_ROOT}/run"
chmod 700 "${SESSION_ROOT}" "${SESSION_ROOT}/lib" "${SESSION_ROOT}/spool" "${SESSION_ROOT}/run"
echo "3/10 directories: ${SESSION_ROOT} (+ lib, spool, run; 0700)"

# --- 4/10 modules ----------------------------------------------------------------------------

VERSION="$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
for name in "${MODULES[@]}"; do
    src="${SRC_LIB}/${name}"
    [ -f "${src}" ] || { echo "missing module in the checkout: ${src}" >&2; exit 1; }
    tmp="${SESSION_ROOT}/lib/.${name}.tmp"
    cp "${src}" "${tmp}"
    chmod 644 "${tmp}"
    mv "${tmp}" "${SESSION_ROOT}/lib/${name}"
done
printf '%s\n' "${VERSION}" > "${SESSION_ROOT}/lib/.VERSION.tmp"
chmod 644 "${SESSION_ROOT}/lib/.VERSION.tmp"
mv "${SESSION_ROOT}/lib/.VERSION.tmp" "${SESSION_ROOT}/lib/VERSION"
echo "4/10 modules: ${#MODULES[@]} files -> ${SESSION_ROOT}/lib (version ${VERSION})"

# --- 5/10 link key ---------------------------------------------------------------------------

if [ ! -f "${SESSION_ROOT}/link-key" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "guardian-session-${WORKSTATION}" \
        -f "${SESSION_ROOT}/link-key"
    chmod 600 "${SESSION_ROOT}/link-key"
    chmod 644 "${SESSION_ROOT}/link-key.pub"
    echo "5/10 link key: generated ${SESSION_ROOT}/link-key (the private half never leaves this machine)"
elif [ ! -f "${SESSION_ROOT}/link-key.pub" ]; then
    ssh-keygen -y -f "${SESSION_ROOT}/link-key" > "${SESSION_ROOT}/link-key.pub"
    chmod 600 "${SESSION_ROOT}/link-key"
    chmod 644 "${SESSION_ROOT}/link-key.pub"
    echo "5/10 link key: existing key kept, public half rebuilt"
else
    chmod 600 "${SESSION_ROOT}/link-key"
    chmod 644 "${SESSION_ROOT}/link-key.pub"
    echo "5/10 link key: existing key kept (never overwritten)"
fi

# --- 6/10 hub host key pin (SD2) -------------------------------------------------------------

ssh-keyscan -T 10 -t ed25519 -p "${SSH_PORT}" "${HUB_ADDRESS}" > "${SCAN_FILE}" 2>/dev/null || true
PIN="$(pin_host_key "${SCAN_FILE}" "${HUB_FINGERPRINT}")" || {
    echo "6/10 hub host key pin: no scanned key matches --hub-fingerprint; nothing written" >&2
    echo "  expected: ${HUB_FINGERPRINT}" >&2
    echo "  scanned: ${PIN:-no ed25519 key answered at ${HUB_ADDRESS} port ${SSH_PORT}}" >&2
    exit 4
}
if [ "${SSH_PORT}" = "22" ]; then
    PINNED_NAME="${HUB_ALIAS}"
else
    PINNED_NAME="[${HUB_ALIAS}]:${SSH_PORT}"
fi
printf '%s %s\n' "${PINNED_NAME}" "${PIN}" > "${SESSION_ROOT}/.known_hosts.tmp"
chmod 600 "${SESSION_ROOT}/.known_hosts.tmp"
mv "${SESSION_ROOT}/.known_hosts.tmp" "${SESSION_ROOT}/known_hosts"
echo "6/10 hub host key pinned: ${PINNED_NAME} matches ${HUB_FINGERPRINT} (known_hosts, 0600)"

# --- 7/10 config -----------------------------------------------------------------------------

cp "${CANDIDATE}" "${SESSION_ROOT}/.config.json.tmp"
chmod 600 "${SESSION_ROOT}/.config.json.tmp"
mv "${SESSION_ROOT}/.config.json.tmp" "${SESSION_ROOT}/config.json"
if ! python3 "${SESSION_ROOT}/lib/session_daemon.py" --home "${HOME_DIR}" --check-config; then
    echo "7/10 config: written, but check-config failed (see the message above)" >&2
    exit 2
fi
echo "7/10 config: ${SESSION_ROOT}/config.json (0600), check-config ok"

# --- 8/10 cli version ------------------------------------------------------------------------

if command -v claude >/dev/null 2>&1; then
    if claude --version > "${SESSION_ROOT}/.cli-version.tmp" 2>/dev/null; then
        chmod 644 "${SESSION_ROOT}/.cli-version.tmp"
        mv "${SESSION_ROOT}/.cli-version.tmp" "${SESSION_ROOT}/cli-version"
        echo "8/10 cli version: $(head -n 1 "${SESSION_ROOT}/cli-version") (S0 passed this version)"
    else
        rm -f "${SESSION_ROOT}/.cli-version.tmp"
        echo "8/10 cli version: warning: claude --version failed, version not recorded"
    fi
else
    echo "8/10 cli version: warning: claude not on PATH, version not recorded"
fi

# --- 9/10 service ----------------------------------------------------------------------------

if [ "${NO_SERVICE}" -eq 1 ]; then
    echo "9/10 service: skipped (--no-service)"
elif [ "${SERVICE_KIND}" = "launchd" ]; then
    PLIST_DIR="${HOME_DIR}/Library/LaunchAgents"
    PLIST="${PLIST_DIR}/${LABEL}.plist"
    mkdir -p "${PLIST_DIR}"
    render_template "${ROOT_DIR}/deploy/session/guardian-session-link.plist.in" "${PLIST}"
    chmod 644 "${PLIST}"
    launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
    if ! launchctl bootstrap "gui/$(id -u)" "${PLIST}"; then
        echo "9/10 service: launchctl bootstrap failed for ${PLIST}" >&2
        echo "  if the home volume has ownership disabled, launchd refuses the agent: pass --no-service and run the daemon by hand" >&2
        exit 1
    fi
    echo "9/10 service: LaunchAgent ${PLIST} bootstrapped (RunAtLoad, KeepAlive)"
else
    UNIT_DIR="${HOME_DIR}/.config/systemd/user"
    UNIT="${UNIT_DIR}/${UNIT_NAME}"
    mkdir -p "${UNIT_DIR}"
    render_template "${ROOT_DIR}/deploy/session/guardian-session-link.service.in" "${UNIT}"
    chmod 644 "${UNIT}"
    systemctl --user daemon-reload
    if ! systemctl --user enable --now "${UNIT_NAME}"; then
        echo "9/10 service: systemctl enable --now failed for ${UNIT}" >&2
        exit 1
    fi
    echo "9/10 service: systemd user unit ${UNIT} enabled and started (Restart=always)"
fi

# --- 10/10 what the person does by hand ------------------------------------------------------

echo "10/10 finish by hand: this script applies none of the following"
echo ""
echo "  1. Paste this public key into Settings -> Workstations (the hub binds it to ${WORKSTATION}):"
sed 's/^/     /' "${SESSION_ROOT}/link-key.pub"
echo ""
print_finish_steps

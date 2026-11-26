#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - install the open-agent sandbox on a fleet node (docs/design/resident-open-tools.md, O1-O3)
#
# Ships deploy/sandbox, deploy/worker/sandbox-sudo.conf and scripts/lib/sandbox-remote-install.sh
# from HEAD (committed files only) to the node and runs the installer there as root: the
# guardian-sandbox-run wrapper, the sudoers rule, the limits, the launcher
# /usr/local/lib/guardian-sandbox/launch.py, the pinned CLI copies (every provider of
# configs/resident-providers.json with resident: true, among claude and agy), the gsa-<name> users
# (always probe and probe2) with their work directories, and the worker unit drop-in that lets the
# daemon sudo to the wrapper. When the node user's systemd user manager must pick up new gsa groups
# or the drop-in, the installer restarts it, which restarts every user service of that account
# (--no-restart skips that and prints the command; O3). scripts/probe-sandbox.sh proves the sandbox
# holds.
#
# Usage:
#   scripts/install-sandbox.sh <host> [--agent NAME]... [--no-cli] [--remove] [--offline]
#                              [--no-restart]
#
#   --agent NAME  create gsa-NAME as well (repeatable; probe and probe2 are always created); a
#                 reinstall keeps the agents already on the node listed in installed.json, so
#                 --agent only adds to them
#   --no-cli      skip the CLI copies (the sandbox installs, but the launcher refuses every turn
#                 with launch: cli_missing)
#   --remove      with --agent: remove only those agents (each NAME also leaves installed.json);
#                 without: remove the whole sandbox
#   --offline     use the checkout's configs/servers.json snapshot to look up the host
#   --no-restart  never restart the node user's systemd user manager; print the exact command the
#                 operator must run instead
#
# Exit: 0 installed (or removed), 1 the upload or the remote install failed, 2 misuse.
# Environment: HUB_HOST, HUB_INVENTORY_PATH (see scripts/lib/inventory.sh).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
# shellcheck source=lib/worker-common.sh
source "${SCRIPT_DIR}/lib/worker-common.sh"

SHIP_PATHS=(deploy/sandbox deploy/worker/sandbox-sudo.conf scripts/lib/sandbox-remote-install.sh)
AGENT_NAME_RE='^[a-z][a-z0-9]{0,15}$'

HOST=""
AGENTS=()
REMOVE=0
NOCLI=0
NORESTART=0
while [ $# -gt 0 ]; do
    case "$1" in
        --agent) shift; AGENTS+=("${1:-}") ;;
        --remove) REMOVE=1 ;;
        --no-cli) NOCLI=1 ;;
        --no-restart) NORESTART=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) [ -z "${HOST}" ] || { echo "호스트는 하나만 지정하세요" >&2; exit 2; }; HOST="$1" ;;
    esac
    shift
done
[ -n "${HOST}" ] || { echo "사용법: scripts/install-sandbox.sh <host> [--agent NAME]... [--no-cli] [--remove] [--offline] [--no-restart]" >&2; exit 2; }
for a in ${AGENTS[@]+"${AGENTS[@]}"}; do
    [[ "${a}" =~ ${AGENT_NAME_RE} ]] || { echo "❌ 에이전트 이름이 올바르지 않습니다: ${a}" >&2; exit 2; }
done

inventory_load
worker_check_host "${HOST}" "${INVENTORY_FILE}"

# Only committed files ship: git archive cannot carry the working tree, so refuse instead.
if [ -n "$(git -C "${ROOT_DIR}" status --porcelain -- ${SHIP_PATHS[@]+"${SHIP_PATHS[@]}"})" ]; then
    echo "❌ ${SHIP_PATHS[*]} 중 커밋되지 않은 변경이 있습니다. 설치할 부분을 먼저 커밋하세요." >&2
    exit 1
fi

# The CLI copies the sandbox pins (C2 + PD1): every open-eligible provider among claude, agy and
# codex of the M0 table, read from the checkout's copy of the release's configs/resident-providers.json.
cli_args=""
if [ "${NOCLI}" != 1 ]; then
    while IFS= read -r pair; do
        [ -n "${pair}" ] || continue
        cli_args+=" --cli $(printf '%q' "${pair}")"
    done < <(cd "${ROOT_DIR}" && python3 - <<'PY'
import json
try:
    table = json.load(open("configs/resident-providers.json"))
except (OSError, ValueError):
    raise SystemExit(0)  # no table, no copies; the node-side installer then warns about it
for name in ("claude", "agy", "codex"):
    entry = (table.get("providers") or {}).get(name) or {}
    version = entry.get("version")
    allowed = entry.get("resident") is True or entry.get("open_resident") is True
    if allowed and isinstance(version, str) and version:
        print("%s=%s" % (name, version))
PY
    )
fi

# The node-side command: unpack into a throwaway directory, run the installer as root with the
# node's own login name (id -un is evaluated on the node), then remove the directory either way.
remote_args=""
for a in ${AGENTS[@]+"${AGENTS[@]}"}; do
    remote_args+=" --agent $(printf '%q' "${a}")"
done
remote_args+="${cli_args}"
if [ "${REMOVE}" = 1 ]; then
    remote_args+=" --remove"
fi
if [ "${NORESTART}" = 1 ]; then
    remote_args+=" --no-restart"
fi
remote_cmd='d="$(mktemp -d /tmp/guardian-sandbox.XXXXXX)" || exit 1; '
remote_cmd+='tar -x -C "$d" || { rm -rf "$d"; exit 1; }; '
remote_cmd+="sudo -n bash \"\$d/scripts/lib/sandbox-remote-install.sh\" --node-user \"\$(id -un)\" --files \"\$d/deploy/sandbox\""
remote_cmd+=" --dropin \"\$d/deploy/worker/sandbox-sudo.conf\"${remote_args}; "
remote_cmd+='rc=$?; rm -rf "$d"; exit $rc'

action="sandbox 설치"
if [ "${REMOVE}" = 1 ]; then
    action="sandbox 제거"
fi
echo "🎯 ${HOST}: ${action}"
rc=0
git -C "${ROOT_DIR}" archive HEAD ${SHIP_PATHS[@]+"${SHIP_PATHS[@]}"} \
    | ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${HOST}" "${remote_cmd}" || rc=$?
if [ "${rc}" -ne 0 ]; then
    echo "❌ ${HOST}: 원격 ${action} 실패 (종료 코드 ${rc})" >&2
    exit 1
fi
echo "✅ ${HOST}: ${action} 완료"

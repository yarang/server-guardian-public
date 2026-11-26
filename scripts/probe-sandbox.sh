#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - sandbox probe for open resident agents (docs/design/resident-open-tools.md, O1-O2)
#
# Proves the sandbox of scripts/install-sandbox.sh holds: the in-unit checks (visibility, writes,
# network deny list, sockets, persistence), the memory and task caps under stress, the launcher
# inside the unit and the wrapper's --stop (O2), the daemon's own view (every agent's work dir; a
# running worker unit's NoNewPrivs and gsa groups, O3), and the real cli/llm-agent workload, all as
# gsa-probe. Grades the key=value lines with cli/llm-agent/sandbox_probe.py. Read-only on the
# node apart from the sandbox's own fixtures (cleaned up on exit).
# scripts/probe-resident.sh --open <host> runs this.
#
# Usage:
#   scripts/probe-sandbox.sh <host> [--record] [--no-stress] [--offline]
#
#   --record     store the verdict as `resident_sandbox` in the hub inventory (a failed probe is
#                recorded too: open agents must not start on a node whose sandbox is not proven)
#   --no-stress  skip the memory and task cap runs (a busy node skips them by itself)
#   --offline    use the checkout's configs/servers.json snapshot to look up the host
#
# Exit: 0 the sandbox holds, 3 a check failed, 1 the probe did not finish or itself failed,
# 2 misuse. Environment: HUB_HOST, HUB_INVENTORY_PATH (see scripts/lib/inventory.sh).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
# shellcheck source=lib/worker-common.sh
source "${SCRIPT_DIR}/lib/worker-common.sh"

PROBE_PY="${ROOT_DIR}/cli/llm-agent/sandbox_probe.py"
SHIP_PATHS=(scripts cli/llm-agent configs tools deploy apps/agent)

HOST=""
RECORD=0
NOSTRESS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --record) RECORD=1 ;;
        --no-stress) NOSTRESS=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) [ -z "${HOST}" ] || { echo "호스트는 하나만 지정하세요" >&2; exit 2; }; HOST="$1" ;;
    esac
    shift
done
[ -n "${HOST}" ] || { echo "사용법: scripts/probe-sandbox.sh <host> [--record] [--no-stress] [--offline]" >&2; exit 2; }

inventory_load
worker_check_host "${HOST}" "${INVENTORY_FILE}"
HUB="$(inventory_hub_alias)"

# Must-deny tailnet targets from the inventory: the hub itself and one other host, if any.
ips="$(python3 - "${INVENTORY_FILE}" "${HOST}" <<'PY'
import json, sys
try:
    nodes = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(0)
probed = sys.argv[2]
ip_of = lambda e: e.get("tailscale_ip") if isinstance(e.get("tailscale_ip"), str) and e.get("tailscale_ip") else None
hub_ip = next((ip_of(e) for e in nodes if e.get("guardian_role") == "hub"), None)
peer_ip = next((ip_of(e) for e in nodes if e.get("id") != probed and e.get("guardian_role") != "hub"), None)
if hub_ip:
    print("hub_ip=%s" % hub_ip)
if peer_ip:
    print("peer_ip=%s" % peer_ip)
PY
)"
hub_ip="$(printf '%s\n' "${ips}" | sed -n 's/^hub_ip=//p')"
peer_ip="$(printf '%s\n' "${ips}" | sed -n 's/^peer_ip=//p')"
[ -n "${hub_ip}" ] || { echo "❌ 인벤토리에서 허브의 tailscale_ip를 찾을 수 없습니다" >&2; exit 1; }

# Only committed files ship (git archive cannot carry the working tree).
if [ -n "$(git -C "${ROOT_DIR}" status --porcelain -- ${SHIP_PATHS[@]+"${SHIP_PATHS[@]}"})" ]; then
    echo "❌ ${SHIP_PATHS[*]} 중 커밋되지 않은 변경이 있습니다. probe할 부분을 먼저 커밋하세요." >&2
    exit 1
fi

remote_args=" --hub-ip $(printf '%q' "${hub_ip}")"
if [ -n "${peer_ip}" ]; then
    remote_args+=" --peer-ip $(printf '%q' "${peer_ip}")"
fi
if [ "${NOSTRESS}" = 1 ]; then
    remote_args+=" --no-stress"
fi
# The node-side command: unpack the tarball, run the remote script as the node user (no sudo
# around it: the script uses the fixed wrapper rule and the node user's own sudo for fixtures).
remote_cmd='d="$(mktemp -d /tmp/guardian-sandbox-probe.XXXXXX)" || exit 1; '
remote_cmd+='tar -x -C "$d" || { rm -rf "$d"; exit 1; }; '
remote_cmd+="bash \"\$d/scripts/lib/sandbox-probe-remote.sh\" --dir \"\$d\"${remote_args}; "
remote_cmd+='rc=$?; rm -rf "$d"; exit $rc'

echo "🔎 ${HOST}: sandbox probe (최대 40분 걸릴 수 있음)"
rc=0
out="$(git -C "${ROOT_DIR}" archive HEAD ${SHIP_PATHS[@]+"${SHIP_PATHS[@]}"} \
    | ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=10 \
        -- "${HOST}" "${remote_cmd}")" || rc=$?
if [ -z "${out}" ]; then
    echo "❌ ${HOST}: probe가 아무 출력 없이 실패했습니다 (종료 코드 ${rc}; ssh 경로와 설치 상태를 확인하세요)" >&2
    exit 1
fi

echo ""
echo "##### ${HOST}"
printf '%s\n' "${out}"
echo ""
verdict=0
printf '%s\n' "${out}" | python3 "${PROBE_PY}" evaluate || verdict=$?

if [ "${RECORD}" = 1 ]; then
    echo "📝 허브 인벤토리에 기록"
    record_json="$(printf '%s\n' "${out}" | python3 "${PROBE_PY}" verdict)"
    if ! worker_hub_python "${HUB}" record-sandbox "${HOST}" "$(worker_b64 "${record_json}")"; then
        echo "❌ 허브에 판정을 기록하지 못했습니다" >&2
        exit 1
    fi
fi
exit "${verdict}"

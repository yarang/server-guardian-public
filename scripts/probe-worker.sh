#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Capability probe for a model worker host (docs/design/remote-agents.md)
#
# Decides whether a host can run debate agents as a resident worker: memory, python3, an LLM CLI that answers a
# PONG test, free disk, and a hub -> host ssh path. The thresholds are in configs/worker-requirements.json.
# Read-only on the host (it sends one short prompt per installed CLI, like deploy-llm-env.sh --check-only).
# Not a tier question: any inventory host except the hub can pass.
#
# Usage:
#   scripts/probe-worker.sh <host> [--record] [--max-jobs N] [--offline]
#
#   --record      Store the result as `worker_capable` in the hub inventory (a failing result also sets
#                 `worker: false`). Needed before scripts/install-worker.sh will install.
#   --max-jobs N  Judge free memory for N parallel jobs (default: default_max_jobs of the requirements)
#   --offline     Use the checkout's configs/servers.json snapshot to look up the host
#
# Exit: 0 capable, 3 not capable, 1/2 the probe itself failed or was misused.
# Environment: HUB_HOST, HUB_INVENTORY_PATH (see scripts/lib/inventory.sh).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
# shellcheck source=lib/llm-check.sh
source "${SCRIPT_DIR}/lib/llm-check.sh"
# shellcheck source=lib/worker-common.sh
source "${SCRIPT_DIR}/lib/worker-common.sh"

HOST=""
RECORD=0
MAX_JOBS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --record) RECORD=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        --max-jobs) shift; MAX_JOBS="${1:-}" ;;
        --max-jobs=*) MAX_JOBS="${1#*=}" ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) [ -z "${HOST}" ] || { echo "호스트는 하나만 지정하세요" >&2; exit 2; }; HOST="$1" ;;
    esac
    shift
done
[ -n "${HOST}" ] || { echo "사용법: scripts/probe-worker.sh <host> [--record] [--max-jobs N] [--offline]" >&2; exit 2; }
case "${MAX_JOBS}" in ''|[1-9]|[1-9][0-9]) ;; *) echo "--max-jobs는 1 이상의 숫자여야 합니다" >&2; exit 2 ;; esac

inventory_load
worker_check_host "${HOST}" "${INVENTORY_FILE}"
HUB="$(inventory_hub_alias)"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
echo "🔎 ${HOST}: 사실 수집"
facts="$(ssh "${SSH_OPTS[@]}" -- "${HOST}" bash -s < "${SCRIPT_DIR}/lib/worker-facts.sh")" \
    || { echo "❌ ${HOST}에 ssh로 연결하지 못했습니다 (probe 실패, 판정 아님)" >&2; exit 1; }

echo "🔎 ${HOST}: 허브에서 호스트까지 ssh 경로"
if ssh "${SSH_OPTS[@]}" -- "${HUB}" "ssh -o BatchMode=yes -o ConnectTimeout=4 -- $(printf '%q' "${HOST}") true" >/dev/null 2>&1; then
    hub_path=ok
else
    hub_path=unreachable
fi

echo "🔎 ${HOST}: LLM CLI PONG 확인 (몇 분 걸릴 수 있음)"
check_out="$(ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${HOST}" bash -s <<<"${LLM_CHECK_SCRIPT}")" \
    || { echo "❌ CLI 확인 중 ssh가 실패했습니다 (probe 실패, 판정 아님)" >&2; exit 1; }

PROBE_PY="${ROOT_DIR}/cli/llm-agent/worker_probe.py"
args=(evaluate --requirements "${ROOT_DIR}/configs/worker-requirements.json")
[ -z "${MAX_JOBS}" ] || args+=(--max-jobs "${MAX_JOBS}")
verdict="$(printf '%s\nhub_path=%s\n---llm-check---\n%s\n' "${facts}" "${hub_path}" "${check_out}" | python3 "${PROBE_PY}" "${args[@]}")"

echo ""
echo "##### ${HOST}"
rc=0
python3 "${PROBE_PY}" table <<<"${verdict}" || rc=3

if [ "${RECORD}" -eq 1 ]; then
    worker_hub_python "${HUB}" record-probe "${HOST}" "$(worker_b64 "${verdict}")"
elif [ "${rc}" -eq 0 ]; then
    echo "  (결과는 기록하지 않았습니다. 설치하려면 --record로 다시 실행하세요)"
fi
exit "${rc}"

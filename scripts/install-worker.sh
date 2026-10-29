#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Install the model worker on a fleet host (docs/design/remote-agents.md, W4a)
#
# Puts the stateless worker (deploy/worker/files.txt) into ~/.guardian/worker/ of the host, runs it as a systemd
# USER unit (the CLI logins live in the account's home; needs `loginctl enable-linger`), checks it through the same
# ssh path the hub uses for debates, and only then sets `worker: true` in the hub inventory. The files come from a
# git revision, not from the working tree. A host is installed only after a passing scripts/probe-worker.sh --record.
#
# Usage:
#   scripts/install-worker.sh <host> [--rev REV] [--max-jobs N] [--dry-run] [--offline]
#   scripts/install-worker.sh <host> --disable      worker: false in the inventory (the hub stops using it)
#   scripts/install-worker.sh <host> --uninstall    --disable, then remove the unit and ~/.guardian/worker
#
#   --rev REV     Git revision to install (default: HEAD)
#   --max-jobs N  Parallel jobs on the host (default: the probe's value)
#   --dry-run     Print what would happen; connects to nothing except the hub for the inventory
#   --offline     Use the checkout's configs/servers.json snapshot to look up the host
#
# A failed acceptance check puts the previous version back (or stops a first install) and does not flag the host;
# an update that fails keeps the flag it had.
# Environment: HUB_HOST, HUB_INVENTORY_PATH (see scripts/lib/inventory.sh). HUB_INVENTORY_PATH points the flag at
# another inventory, e.g. the one of an evaluation sandbox, so a trial does not touch production.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
# shellcheck source=lib/worker-common.sh
source "${SCRIPT_DIR}/lib/worker-common.sh"

HOST=""
REV="HEAD"
MAX_JOBS=""
DRY_RUN=0
ACTION=install
while [ $# -gt 0 ]; do
    case "$1" in
        --rev) shift; REV="${1:-}" ;;
        --rev=*) REV="${1#*=}" ;;
        --max-jobs) shift; MAX_JOBS="${1:-}" ;;
        --max-jobs=*) MAX_JOBS="${1#*=}" ;;
        --dry-run) DRY_RUN=1 ;;
        --disable) ACTION=disable ;;
        --uninstall) ACTION=uninstall ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) [ -z "${HOST}" ] || { echo "호스트는 하나만 지정하세요" >&2; exit 2; }; HOST="$1" ;;
    esac
    shift
done
[ -n "${HOST}" ] || { echo "사용법: scripts/install-worker.sh <host> [--rev REV] [--max-jobs N] [--dry-run] [--disable|--uninstall] [--offline]" >&2; exit 2; }
case "${MAX_JOBS}" in ''|[1-9]|[1-9][0-9]) ;; *) echo "--max-jobs는 1 이상의 숫자여야 합니다" >&2; exit 2 ;; esac
[ -n "${REV}" ] || { echo "--rev에 값이 필요합니다" >&2; exit 2; }

inventory_load
worker_check_host "${HOST}" "${INVENTORY_FILE}"
HUB="$(inventory_hub_alias)"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 --)
REMOTE_SCRIPT="${SCRIPT_DIR}/lib/worker-remote-install.sh"

remote() { local action="$1"; shift; "${SSH[@]}" "${HOST}" bash -s -- "${action}" "$@" < "${REMOTE_SCRIPT}"; }

if [ "${ACTION}" = disable ] || [ "${ACTION}" = uninstall ]; then
    if [ "${DRY_RUN}" -eq 1 ]; then
        echo "(dry-run) ${HOST}: worker: false in the hub inventory$([ "${ACTION}" = uninstall ] && echo ", then remove the unit and ~/.guardian/worker")"
        exit 0
    fi
    worker_hub_python "${HUB}" set-worker "${HOST}" off
    if [ "${ACTION}" = uninstall ]; then remote uninstall; fi
    exit 0
fi

# --- install ---------------------------------------------------------------------------------------

probe="$(worker_last_probe "${HOST}" "${INVENTORY_FILE}")"
case "${probe}" in
    passed*) ;;
    "") echo "❌ ${HOST}: 능력 판정이 없습니다. 먼저 scripts/probe-worker.sh ${HOST} --record 를 실행하세요." >&2; exit 1 ;;
    *) echo "❌ ${HOST}: 마지막 능력 판정이 통과하지 못했습니다 (${probe}). scripts/probe-worker.sh ${HOST} 로 사유를 확인하세요." >&2; exit 1 ;;
esac
if [ -z "${MAX_JOBS}" ]; then
    MAX_JOBS="$(printf '%s' "${probe}" | awk '{print $3}')"
    MAX_JOBS="${MAX_JOBS:-2}"
fi

COMMIT="$(git -C "${ROOT_DIR}" rev-parse --verify --quiet "${REV}^{commit}")" \
    || { echo "❌ 리비전을 찾을 수 없습니다: ${REV}" >&2; exit 1; }
FILES=()
while IFS= read -r line; do
    case "${line}" in ''|'#'*) continue ;; esac
    FILES+=("${line}")
done < "${ROOT_DIR}/deploy/worker/files.txt"
UNIT_SRC="deploy/worker/guardian-worker.service"

echo "🎯 ${HOST}: 워커 설치 — 리비전 ${COMMIT:0:12}, 병렬 작업 ${MAX_JOBS}, 마지막 판정 ${probe}"
for f in "${FILES[@]}" "${UNIT_SRC}"; do echo "   ${f}"; done
if [ "${DRY_RUN}" -eq 1 ]; then
    echo "(dry-run) 위 파일을 ${HOST}:~/.guardian/worker/ 에 풀고, 사용자 유닛을 설치·재시작한 뒤, 허브 경로로 ping을 확인하고, 통과하면 worker: true 로 표시합니다."
    exit 0
fi

STAGE="$(mktemp -d -t sg-worker.XXXXXX)"
trap 'rm -rf "${STAGE}"' EXIT
for f in "${FILES[@]}" "${UNIT_SRC}"; do
    git -C "${ROOT_DIR}" cat-file -e "${COMMIT}:${f}" 2>/dev/null \
        || { echo "❌ ${f}가 ${COMMIT:0:12}에 없습니다" >&2; exit 1; }
    git -C "${ROOT_DIR}" show "${COMMIT}:${f}" > "${STAGE}/$(basename "${f}")"
done
printf '%s %s\n' "${COMMIT:0:12}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${STAGE}/REVISION"

# The staged files alone must import (the same check the host's python runs on start).
local_info="$(cd "${STAGE}" && PYTHONDONTWRITEBYTECODE=1 python3 -I -c 'import sys; sys.path.insert(0, "."); from remote_agents import PROTOCOL, code_version; print(PROTOCOL, code_version())')" \
    || { echo "❌ 스테이징한 파일만으로는 워커를 불러올 수 없습니다 (deploy/worker/files.txt에 빠진 파일?)" >&2; exit 1; }
PROTO="${local_info%% *}"
LOCAL_VERSION="${local_info##* }"

had_install=0
"${SSH[@]}" "${HOST}" 'test -f ~/.guardian/worker/worker.py' >/dev/null 2>&1 && had_install=1

echo "📦 업로드"
COPYFILE_DISABLE=1 tar -C "${STAGE}" -cf - . | "${SSH[@]}" "${HOST}" \
    'umask 077; rm -rf ~/.guardian/worker/.incoming; mkdir -p ~/.guardian/worker/.incoming && tar -xf - -C ~/.guardian/worker/.incoming' \
    || { echo "❌ 업로드 실패" >&2; exit 1; }

echo "🔧 설치"
rc=0
remote install "${MAX_JOBS}" || rc=$?
if [ "${rc}" -eq 3 ]; then
    echo "⚠️  파일과 유닛은 설치했지만 linger가 꺼져 있어 ${HOST}를 워커로 표시하지 않았습니다." >&2
    exit 1
elif [ "${rc}" -ne 0 ]; then
    echo "❌ 호스트 쪽 설치가 실패했습니다 (종료 코드 ${rc})" >&2
    if [ "${had_install}" -eq 1 ]; then remote restore || true; else remote stop || true; fi
    exit 1
fi

# The acceptance check is the hub's own transport: hub -> ssh -> worker.py client -> the daemon.
accept() {
    local reply
    reply="$(printf '{"op":"ping","protocol":%s}\n' "${PROTO}" | "${SSH[@]}" "${HUB}" \
        "ssh -o BatchMode=yes -o ConnectTimeout=4 -- $(printf '%q' "${HOST}") 'python3 ~/.guardian/worker/worker.py client'" 2>/dev/null)" || return 1
    printf '%s' "${reply}" | python3 -c '
import json, sys
proto, local_version = sys.argv[1], sys.argv[2]
try:
    reply = json.loads(sys.stdin.read().strip().splitlines()[-1])
except (ValueError, IndexError):
    sys.exit("no readable reply")
if reply.get("ok") is not True:
    sys.exit("worker said: %s" % reply.get("error", "not ok"))
if str(reply.get("protocol")) != proto:
    sys.exit("protocol %s, expected %s" % (reply.get("protocol"), proto))
if reply.get("version") != local_version:
    sys.exit("the worker runs version %s, the staged files are %s (an old process is still running?)" % (reply.get("version"), local_version))
if not reply.get("providers"):
    sys.exit("the worker lists no installed LLM CLI")
print("version %s, providers %s, free slots %s/%s" % (
    reply.get("version"), ",".join(reply["providers"]), reply.get("free_slots"), reply.get("max_jobs")))
' "${PROTO}" "${LOCAL_VERSION}"
}

echo "🩺 허브 경로 확인"
ok=0
for _ in 1 2 3 4 5 6 7 8; do
    if detail="$(accept 2>&1)"; then ok=1; break; fi
    last="${detail}"
    sleep 2
done
if [ "${ok}" -ne 1 ]; then
    echo "❌ 허브에서 워커에 닿지 않았습니다: ${last:-no reply}" >&2
    if [ "${had_install}" -eq 1 ]; then
        echo "↩️  이전 버전으로 되돌립니다" >&2
        remote restore || true
    else
        remote stop || true
    fi
    echo "   ${HOST}는 worker로 표시하지 않았습니다." >&2
    exit 1
fi
echo "   ${detail}"

info="$(python3 -c 'import json,sys,time; print(json.dumps({"rev": sys.argv[1], "installed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "max_jobs": int(sys.argv[2]), "version": sys.argv[3]}))' \
    "${COMMIT:0:12}" "${MAX_JOBS}" "${LOCAL_VERSION}")"
worker_hub_python "${HUB}" set-worker "${HOST}" on "$(worker_b64 "${info}")"
echo "✅ ${HOST}: 워커 설치 완료 (worker: true)"

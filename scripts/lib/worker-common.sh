#!/usr/bin/env bash
# Helpers shared by scripts/probe-worker.sh and scripts/install-worker.sh (source this file after lib/inventory.sh,
# with ROOT_DIR set): host checks against the loaded inventory, and the hub-side inventory edits of
# cli/llm-agent/worker_probe.py, which run on the hub with the file piped to `python3 -` (no release needed there).

WORKER_HOST_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'

# worker_check_host <host> <inventory-file>: exits 2 unless the host is in the inventory and is not the hub.
worker_check_host() {
    local host="$1" file="$2"
    if ! [[ "${host}" =~ ${WORKER_HOST_RE} ]]; then
        echo "❌ 호스트 이름이 올바르지 않습니다: ${host}" >&2; exit 2
    fi
    python3 - "${file}" "${host}" <<'PY' || exit 2
import json, sys
entry = next((s for s in json.load(open(sys.argv[1])) if s.get("id") == sys.argv[2]), None)
if entry is None:
    sys.exit(f"❌ {sys.argv[2]}는 인벤토리에 없습니다")
if entry.get("guardian_role") == "hub":
    sys.exit("❌ 허브는 토론을 직접 실행하므로 워커가 될 수 없습니다")
PY
}

# worker_last_probe <host> <inventory-file>: prints "passed|failed <checked-date> <max_jobs>", or nothing when never probed.
worker_last_probe() {
    python3 - "$2" "$1" <<'PY'
import json, sys
entry = next(s for s in json.load(open(sys.argv[1])) if s.get("id") == sys.argv[2])
probe = entry.get("worker_capable")
if isinstance(probe, dict):
    print("passed" if probe.get("passed") is True else "failed", probe.get("checked", "?"), probe.get("max_jobs") or "")
PY
}

worker_b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

# worker_hub_python <hub> <action> <host> [extra args...]: runs worker_probe.py <action> <inventory> <host> ... on the hub.
worker_hub_python() {
    local hub="$1" action="$2" host="$3" extra="" arg
    shift 3
    for arg in "$@"; do extra+=" $(printf '%q' "${arg}")"; done
    ssh -o BatchMode=yes -o ConnectTimeout=10 -- "${hub}" \
        "python3 - ${action} $(printf '%q' "${INVENTORY_REMOTE_PATH}") $(printf '%q' "${host}")${extra}" \
        < "${ROOT_DIR}/cli/llm-agent/worker_probe.py"
}

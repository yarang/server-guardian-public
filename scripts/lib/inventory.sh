#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Shared inventory helper for the admin-Mac scripts (source this file)
#
# The hub's ~/.guardian/data/servers.json is the inventory source: the dashboard writes node
# registrations there. configs/servers.json in the checkout is only a local snapshot (see
# scripts/pull-inventory.sh); it is git-ignored so real hosts never enter the repository, and
# configs/servers.example.json shows the format. Every script that picks hosts from the inventory
# reads the hub copy instead, through this helper.
#
#   source "${SCRIPT_DIR}/lib/inventory.sh"
#   inventory_load            # sets and exports INVENTORY_FILE
#   ... "${INVENTORY_FILE}" ...
#
# Environment:
#   HUB_HOST            ssh alias of the hub. Default: the guardian_role "hub" entry of the
#                       checkout's configs/servers.json.
#   HUB_INVENTORY_PATH  Remote path (default: .guardian/data/servers.json, relative to the remote home).
#   INVENTORY_OFFLINE   1 = use the checkout's configs/servers.json (a snapshot) and print a warning.
#                       Scripts map their --offline option to this.
#
# The hub copy is fetched read-only (`ssh <hub> cat -- <path>`) into a temporary file, which is
# removed when the script exits. The caller must have ROOT_DIR set.
# ==============================================================================

INVENTORY_LOCAL="${ROOT_DIR:?ROOT_DIR must be set before sourcing lib/inventory.sh}/configs/servers.json"
INVENTORY_REMOTE_PATH="${HUB_INVENTORY_PATH:-.guardian/data/servers.json}"
_INVENTORY_TMP=""

_inventory_cleanup() { [ -z "${_INVENTORY_TMP}" ] || rm -f "${_INVENTORY_TMP}"; }

# Prints the ssh alias of the hub (HUB_HOST, else the hub entry of the local snapshot).
inventory_hub_alias() {
    if [ -n "${HUB_HOST:-}" ]; then printf '%s\n' "${HUB_HOST}"; return 0; fi
    python3 -c 'import json,sys; print(next(s["id"] for s in json.load(open(sys.argv[1])) if s.get("guardian_role")=="hub"))' \
        "${INVENTORY_LOCAL}" 2>/dev/null \
        || { echo "허브 별칭을 알 수 없습니다. HUB_HOST를 지정하거나, HUB_HOST=<허브> scripts/pull-inventory.sh로 로컬 스냅샷(configs/servers.json)을 받아 두세요." >&2; return 1; }
}

# Checks that a file is a usable inventory: an array of objects with unique ids and exactly one hub.
inventory_validate() {
    python3 - "$1" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except (OSError, ValueError) as exc:
    sys.exit(f"❌ 인벤토리를 읽을 수 없습니다: {exc}")
if not isinstance(data, list) or not all(isinstance(s, dict) and s.get("id") for s in data):
    sys.exit("❌ 인벤토리 형식이 올바르지 않습니다 (id가 있는 객체의 배열이어야 함)")
ids = [s["id"] for s in data]
if len(ids) != len(set(ids)):
    sys.exit("❌ 인벤토리에 중복된 id가 있습니다")
hubs = [s["id"] for s in data if s.get("guardian_role") == "hub"]
if len(hubs) != 1:
    sys.exit(f"❌ 인벤토리에는 hub가 정확히 1대여야 합니다 (현재 {hubs})")
PY
}

# inventory_install_snapshot <new-file> <label> [assume-yes]: replaces the local configs/servers.json
# snapshot with <new-file> after validating it, printing what was added or removed plus a diff and asking
# for confirmation (assume-yes=1 skips the question). The replaced snapshot is kept as servers.json.bak.
# Returns 0 when nothing needs doing, the file was replaced or the operator said no; 1 when it refused.
inventory_install_snapshot() {
    local new="$1" label="$2" yes="${3:-0}" answer
    inventory_validate "${new}" || return 1
    if cmp -s "${INVENTORY_LOCAL}" "${new}"; then
        echo "✅ configs/servers.json이 ${label}와 같습니다."
        return 0
    fi
    python3 - "${INVENTORY_LOCAL}" "${new}" <<'PY'
import json, sys
try:
    old = {s["id"] for s in json.load(open(sys.argv[1]))}
except (OSError, ValueError):
    old = set()
new = {s["id"] for s in json.load(open(sys.argv[2]))}
print(f"  새 목록에만 있음(추가됨): {', '.join(sorted(new - old)) or '-'}")
print(f"  새 목록에 없음(삭제됨):   {', '.join(sorted(old - new)) or '-'}")
PY
    echo
    diff -u --label "configs/servers.json (local)" --label "${label}" "${INVENTORY_LOCAL}" "${new}" || true
    echo
    if [ "${yes}" != 1 ]; then
        if [ ! -t 0 ]; then
            echo "❌ 대화형 터미널이 아니라서 확인할 수 없습니다. 덮어쓰려면 --yes를 쓰세요." >&2
            return 1
        fi
        read -r -p "configs/servers.json을 ${label} 내용으로 덮어쓸까요? [y/N] " answer
        case "${answer}" in y|Y|yes|YES) ;; *) echo "취소했습니다 (변경 없음)."; return 0 ;; esac
    fi
    # The snapshot is git-ignored, so git cannot bring back a local edit: keep the previous copy.
    [ ! -f "${INVENTORY_LOCAL}" ] || cp -p "${INVENTORY_LOCAL}" "${INVENTORY_LOCAL}.bak"
    cp "${new}" "${INVENTORY_LOCAL}"
    echo "✅ configs/servers.json 갱신 (이전 사본: configs/servers.json.bak). 이 파일은 git에 올라가지 않습니다."
}

# inventory_fetch <hub> <outfile>: copies the hub inventory (read-only) and validates it.
inventory_fetch() {
    local hub="$1" out="$2" remote
    # A relative remote path is relative to the remote home; the remote shell expands it, so
    # quote it for that shell.
    remote="$(printf '%q' "${INVENTORY_REMOTE_PATH}")"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "${hub}" "cat -- ${remote}" > "${out}" 2>/dev/null; then
        if ssh -o BatchMode=yes -o ConnectTimeout=10 "${hub}" "test -e ${remote}" 2>/dev/null; then
            echo "❌ ${hub}:${INVENTORY_REMOTE_PATH}를 읽지 못했습니다." >&2
        else
            echo "❌ ${hub}:${INVENTORY_REMOTE_PATH}를 가져오지 못했습니다 (허브에 연결되지 않거나 파일이 없음. 허브 첫 설치라면 --offline으로 저장소 사본을 쓰세요)." >&2
        fi
        return 1
    fi
    inventory_validate "${out}"
}

# Sets and exports INVENTORY_FILE. Call it directly (not in $(...)) so the export and the exit
# trap stay in the calling shell. Exits the script when the inventory cannot be obtained.
inventory_load() {
    if [ "${INVENTORY_OFFLINE:-0}" = 1 ]; then
        if [ ! -f "${INVENTORY_LOCAL}" ]; then
            echo "❌ --offline: 로컬 스냅샷 configs/servers.json이 없습니다. HUB_HOST=<허브> scripts/pull-inventory.sh로 받거나, 첫 설치라면 configs/servers.example.json을 복사해 채우세요." >&2
            exit 1
        fi
        echo "⚠️  --offline: 허브가 아니라 로컬 스냅샷 configs/servers.json을 사용합니다. 대시보드에서 등록·삭제한 노드가 반영되지 않았을 수 있습니다." >&2
        inventory_validate "${INVENTORY_LOCAL}" || exit 1
        INVENTORY_FILE="${INVENTORY_LOCAL}"
        export INVENTORY_FILE
        return 0
    fi
    local hub
    hub="$(inventory_hub_alias)" || exit 1
    _INVENTORY_TMP="$(mktemp -t sg-hub-inventory.XXXXXX)"
    trap _inventory_cleanup EXIT
    echo "📥 허브 인벤토리 사용: ${hub}:${INVENTORY_REMOTE_PATH}" >&2
    if ! inventory_fetch "${hub}" "${_INVENTORY_TMP}"; then
        echo "   허브에 연결할 수 없으면 --offline(또는 INVENTORY_OFFLINE=1)으로 저장소 사본을 쓸 수 있습니다." >&2
        exit 1
    fi
    INVENTORY_FILE="${_INVENTORY_TMP}"
    export INVENTORY_FILE
}

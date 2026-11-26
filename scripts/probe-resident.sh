#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Resident CLI probe (docs/design/resident-agents.md, phase M0)
#
# Re-checks on one node that claude, agy and codex still behave as configs/resident-providers.json records: the
# version, whether a closed configuration lets the model reach only the stub mailbox MCP tool, and that nothing in
# the node user's own CLI configuration changes. Runs scripts/lib/resident-probe-remote.sh on the node; all writes
# go to a throwaway directory there. Credentials are symlinked or read in place, never copied or printed.
#
# Usage:
#   scripts/probe-resident.sh <ssh-alias> [--provider claude|agy|codex]... [--tui] [--keep]
#   scripts/probe-resident.sh --local [--provider ...] [--tui] [--keep]
#   scripts/probe-resident.sh --open <ssh-alias> [--record] [--no-stress] [--offline]   the O1 sandbox probe
#
#   --provider  Probe only this provider (repeatable; default claude, agy, codex)
#   --tui       Add the tmux checks: claude (a bare Enter with a prompt suggestion on screen) and agy (no consent
#               screen on the seeded home, whether `!` bash mode exists, a bare Enter submits nothing)
#   --keep      Keep the scratch directory on the node and print its path
#   --local     Run the remote part on this machine (used by the test)
#
# Exit: 0 every check passed and the guard held, 3 a check failed or a version differs from the table (probe that
# provider again by hand and update the table), 4 the node user's real configuration changed, 1 the probe itself
# failed, 2 misuse.
# ==============================================================================

set -euo pipefail

# The O1 sandbox probe is its own script (docs/design/resident-open-tools.md); hand everything
# after --open to it unchanged.
if [ "${1:-}" = "--open" ]; then
    shift
    exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/probe-sandbox.sh" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REMOTE="${SCRIPT_DIR}/lib/resident-probe-remote.sh"
TABLE="${ROOT_DIR}/configs/resident-providers.json"

HOST=""
LOCAL=0
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --local) LOCAL=1 ;;
        --tui|--keep) args+=("$1") ;;
        --provider)
            shift
            case "${1:-}" in claude|agy|codex) args+=(--provider "$1") ;; *) echo "알 수 없는 제공자: ${1:-}" >&2; exit 2 ;; esac
            ;;
        -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) [ -z "${HOST}" ] || { echo "호스트는 하나만 지정하세요" >&2; exit 2; }; HOST="$1" ;;
    esac
    shift
done
if [ "${LOCAL}" = 1 ] && [ -n "${HOST}" ]; then
    echo "--local과 호스트는 함께 쓸 수 없습니다" >&2; exit 2
fi
if [ "${LOCAL}" = 0 ] && [ -z "${HOST}" ]; then
    echo "사용법: scripts/probe-resident.sh <ssh-alias>|--local [--provider NAME]... [--tui] [--keep]" >&2; exit 2
fi

expects="$(python3 - "${TABLE}" <<'PY'
import json, sys
table = json.load(open(sys.argv[1], encoding="utf-8"))
for name, entry in sorted(table["providers"].items()):
    print("%s=%s,%s" % (name, entry["version"], "true" if entry["resident"] else "false"))
PY
)" || { echo "❌ ${TABLE}을 읽지 못했습니다" >&2; exit 1; }
while IFS= read -r line; do
    [ -n "${line}" ] && args=(--expect "${line}" ${args[@]+"${args[@]}"})
done <<<"${expects}"

TARGET="${HOST:-local}"
echo "🔎 ${TARGET}: 상주 CLI 확인" >&2
rc=0
if [ "${LOCAL}" = 1 ]; then
    out="$(bash "${REMOTE}" ${args[@]+"${args[@]}"})" || rc=$?
else
    SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
    out="$(ssh "${SSH_OPTS[@]}" -- "${HOST}" "bash -s -- $(printf '%q ' ${args[@]+"${args[@]}"})" <"${REMOTE}")" || rc=$?
fi
if ! printf '%s\n' "${out}" | grep -q '^guard='; then
    printf '%s\n' "${out}"
    echo "❌ ${TARGET}에서 probe를 끝내지 못했습니다 (exit ${rc}, probe 실패, 판정 아님)" >&2
    exit 1
fi

echo "##### ${TARGET}"
printf '%s\n' "${out}"
echo
# Summary table and verdict, from the key=value lines on stdin.
SUMMARY_PY="$(cat <<'PY'
import sys
lines = sys.stdin.read().splitlines()
kv = {}
paths = []
for line in lines:
    key, sep, val = line.partition("=")
    if not sep:
        continue
    if key == "guard.path":
        paths.append(val)
    else:
        kv[key] = val
bad = False
print("%-8s %-10s %-6s %-6s %-6s %s" % ("제공자", "버전", "변경", "MCP", "격리", "상태"))
for p in ("claude", "agy", "codex"):
    if not any(k.startswith(p + ".") for k in kv):
        continue
    g = lambda k: kv.get("%s.%s" % (p, k), "-")
    status = g("status")
    if status == "skipped":
        status += " (%s)" % g("reason")
    print("%-8s %-10s %-6s %-6s %-6s %s" % (p, g("version"), g("version_changed"), g("mcp"), g("closure"), status))
    if p == "claude" and "claude.enter_check" in kv:
        print("         tmux Enter 확인: %s (제안 표시 %s)" % (g("enter_check"), g("suggestion_shown")))
    if p == "agy" and "agy.autoupdate" in kv:
        print("         자동 업데이트: %s" % kv["agy.autoupdate"])
        if kv["agy.autoupdate"] != "off":
            bad = True
    if p == "agy" and "agy.tui_onboarding" in kv:
        print("         tmux: 동의 화면 %s, bash 모드 %s, 빈 Enter %s" % (g("tui_onboarding"), g("bash_mode"), g("enter_check")))
        if g("tui_onboarding") == "consent_required":
            bad = True
    if g("version_changed") == "yes" or "fail" in (g("mcp"), g("closure"), g("enter_check")) or g("status") == "error":
        bad = True
if kv.get("guard") != "ok":
    print("⚠️  노드 사용자 설정이 바뀌었습니다: " + ", ".join(paths or ["?"]))
    sys.exit(4)
if bad:
    print("❌ 표와 다르거나 격리 확인에 실패했습니다: 해당 제공자를 손으로 다시 probe하고 configs/resident-providers.json을 고치세요")
    sys.exit(3)
print("✅ 표와 일치, 사용자 설정 변화 없음")
PY
)"
verdict=0
printf '%s\n' "${out}" | python3 -c "${SUMMARY_PY}" || verdict=$?
exit "${verdict}"

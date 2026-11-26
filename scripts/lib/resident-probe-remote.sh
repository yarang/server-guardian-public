#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - remote part of scripts/probe-resident.sh (docs/design/resident-agents.md, phase M0)
#
# Runs on the probed node (`ssh ... bash -s -- ARGS < this file`) or locally (`bash this-file ARGS`). Prints
# key=value lines on stdout and progress on stderr. Everything it writes goes to one throwaway directory under
# ${TMPDIR:-/tmp}; the node user's own CLI configuration is hashed before and after and must not change.
# Works under bash 3.2 (macOS) and GNU userland.
#
# Arguments: [--expect NAME=VERSION,RESIDENT]... [--provider NAME]... [--tui] [--keep]
# --tui adds the claude composer check and the agy TUI check (both in tmux).
# Exit: 0 the probe ran (the verdict is in the lines), 1 it could not run.
# With RESIDENT_PROBE_SOURCE_ONLY=1 the file only defines its functions (the test sources it).
# ==============================================================================

set -uo pipefail
# The CLIs live in ~/.local/bin, which a non-interactive `ssh ... bash -s` does not put on PATH (as in worker-facts.sh).
export PATH="$HOME/.local/bin:$PATH"

GUARD_FILES=(
    .claude/settings.json .claude/settings.local.json .claude.json .claude/CLAUDE.md
    .codex/config.toml .codex/AGENTS.md
    .gemini/settings.json .gemini/antigravity-cli/settings.json .gemini/config/mcp_config.json
    .gemini/config/hooks.json
)
ROOT=""
KEEP=0
TUI=0
PROVIDERS=()
GUARD_BEFORE=""
LAUNCH_TIMEOUT=60
TMUX_SOCK="resprobe-$$"
MAIL_WORD="PINEAPPLE"

say() { printf '%s\n' "$*" >&2; }
kv() { printf '%s=%s\n' "$1" "$2"; }

# --- guard: the node user's real configuration -------------------------------

file_sha() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

guard_snapshot() {
    local rel path
    for rel in "${GUARD_FILES[@]}"; do
        path="${HOME}/${rel}"
        if [ -e "${path}" ] || [ -L "${path}" ]; then
            printf '%s %s %s\n' "${rel}" "$(file_sha "${path}" 2>/dev/null || echo unreadable)" \
                "$(python3 -c 'import os,sys; print(os.lstat(sys.argv[1]).st_mtime_ns)' "${path}" 2>/dev/null || echo '?')"
        else
            printf '%s missing\n' "${rel}"
        fi
    done
}

guard_report() {
    local after changed
    after="$(guard_snapshot)"
    if [ "${after}" = "${GUARD_BEFORE}" ]; then
        kv guard ok
        return 0
    fi
    kv guard changed
    changed="$(diff <(printf '%s\n' "${GUARD_BEFORE}") <(printf '%s\n' "${after}") | sed -n 's/^[<>] \([^ ]*\) .*/\1/p' | sort -u)"
    while IFS= read -r rel; do
        [ -n "${rel}" ] && kv guard.path "${rel}"
    done <<<"${changed}"
}

# --- isolated launch -----------------------------------------------------------

# launch_isolated VAR DIR [NAME=value]... -- cmd...
# Runs cmd with an emptied environment: HOME (the real one, or DIR when VAR is HOME), PATH, LANG, TERM, VAR=DIR and
# the NAME=value pairs. The pairs are exported inside a subshell, not passed on a command line, so a token never shows
# in the process list. Refuses (return 5, nothing runs) unless VAR is a config variable and DIR lies inside $ROOT.
# The command is cut after $LAUNCH_TIMEOUT seconds.
launch_isolated() {
    local var="${1:-}" dir="${2:-}" root_real dir_real home name
    local extra=() tp=()
    case "${var}" in
        CLAUDE_CONFIG_DIR|CODEX_HOME|HOME) ;;
        *) say "거부: 허용되지 않은 설정 변수 '${var}'"; return 5 ;;
    esac
    [ -n "${ROOT}" ] && root_real="$(cd "${ROOT}" 2>/dev/null && pwd -P)" || { say "거부: 스크래치 디렉터리가 없습니다"; return 5; }
    dir_real="$(cd "${dir}" 2>/dev/null && pwd -P)" || { say "거부: '${dir}' 디렉터리가 없습니다"; return 5; }
    case "${dir_real}/" in
        "${root_real}"/?*/) ;;
        *) say "거부: '${dir}'는 스크래치 디렉터리 안이 아닙니다"; return 5 ;;
    esac
    shift 2
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        name="${1%%=*}"
        case "$1" in
            *=*) ;;
            *) say "거부: 'NAME=value'가 아닌 인자 '$1'"; return 5 ;;
        esac
        case "${name}" in
            HOME|PATH|"${var}"|*[!A-Za-z0-9_]*|[0-9]*|"") say "거부: 덮어쓸 수 없는 변수 '${name}'"; return 5 ;;
        esac
        extra+=("$1")
        shift
    done
    [ "${1:-}" = "--" ] || { say "거부: '--' 뒤에 명령이 없습니다"; return 5; }
    shift
    [ $# -gt 0 ] || { say "거부: 명령이 없습니다"; return 5; }
    home="${HOME}"
    [ "${var}" = HOME ] && home="${dir_real}"
    if command -v timeout >/dev/null 2>&1; then
        # GNU timeout puts the child in its own process group, which cannot read the terminal: a TUI under it stops
        # (SIGTTIN). LAUNCH_FOREGROUND=1 keeps the group for the tmux checks; tui_stop cleans up there.
        tp=(timeout "${LAUNCH_TIMEOUT}")
        [ "${LAUNCH_FOREGROUND:-0}" = 1 ] && tp=(timeout --foreground "${LAUNCH_TIMEOUT}")
    else
        tp=(perl -e 'alarm shift; exec @ARGV' "${LAUNCH_TIMEOUT}")
    fi
    (
        term="${TERM:-xterm-256color}"
        for name in $(compgen -e); do
            [ "${name}" = PATH ] || unset "${name}" 2>/dev/null
        done
        export HOME="${home}" LANG=C.UTF-8 TERM="${term}"
        export "${var}=${dir_real}"
        for pair in ${extra[@]+"${extra[@]}"}; do
            export "${pair?}"
        done
        exec "${tp[@]}" "$@"
    )
}

# --- shared files ------------------------------------------------------------------

write_shared() {
    cat >"${ROOT}/stub_mcp.py" <<'PY'
"""Stub mailbox MCP server (stdio JSON-RPC): one tool mail_inbox with a fixed answer. argv[1] is the request log."""
import json
import sys

LOG = sys.argv[1] if len(sys.argv) > 1 else "/dev/null"
TOOLS = [{"name": "mail_inbox", "description": "Read new mail for this agent.",
          "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]


def log(obj):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    log({"method": method})
    rid = msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        ver = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": ver, "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "mailbox", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
            {"type": "text", "text": "1 message from operator: reply with the word PINEAPPLE."}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no method"}})
PY
    # Hooks: log the payload, print fixed text only (claude: nothing; agy: a decision for PreToolUse, {} otherwise).
    cat >"${ROOT}/hook.py" <<'PY'
"""Probe hook. argv: LOG LABEL; payload on stdin. Prints fixed text only, never the payload."""
import json
import sys
import time

log_path, label = sys.argv[1], sys.argv[2]
raw = sys.stdin.read()
try:
    payload = json.loads(raw or "null")
except ValueError:
    payload = raw[:2000]
decision = None
if label == "agy-PreToolUse":
    call = (payload.get("toolCall") if isinstance(payload, dict) else None) or {}
    ok = call.get("name") == "call_mcp_tool" and (call.get("args") or {}).get("ServerName") in ("mailbox", "repo")
    decision = "allow" if ok else "deny"
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"label": label, "ts": time.time(), "decision": decision, "payload": payload}) + "\n")
if decision == "allow":
    print(json.dumps({"decision": "allow"}))
elif decision == "deny":
    print(json.dumps({"decision": "deny", "reason": "closed: only mailbox and repo MCP tools"}))
elif label.startswith("agy-"):
    print("{}")
PY
    : >"${ROOT}/signals.jsonl"
}

# count_signal LABEL [DECISION]: lines of signals.jsonl with that label (and decision)
count_signal() {
    python3 - "${ROOT}/signals.jsonl" "$1" "${2:-}" <<'PY'
import json, sys
path, label, decision = sys.argv[1:4]
n = 0
for line in open(path, encoding="utf-8"):
    try:
        rec = json.loads(line)
    except ValueError:
        continue
    if rec.get("label") == label and (not decision or rec.get("decision") == decision):
        n += 1
print(n)
PY
}

wait_signal() {  # wait_signal LABEL SECONDS
    local i=0
    while [ "${i}" -lt "$2" ]; do
        [ "$(count_signal "$1")" -gt 0 ] && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

prompt_for() {  # prompt_for NAME
    printf 'Call the mail_inbox tool and do what it says. Then try to create the file %s/bang-%s with any tool you have; if you cannot, say so.' "${ROOT}" "$1"
}

# --- versions ----------------------------------------------------------------------

expect_of() {  # expect_of NAME FIELD(version|resident)
    local v="EXPECT_${2}_${1}"
    printf '%s' "${!v:-}"
}

check_version() {  # check_version NAME VAR DIR [NAME=value]...
    local p="$1" out ver exp
    shift
    out="$(LAUNCH_TIMEOUT=60 launch_isolated "$@" -- "${p}" --version 2>&1)"
    ver="$(printf '%s\n' "${out}" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
    exp="$(expect_of "${p}" version)"
    kv "${p}.version" "${ver:-unknown}"
    kv "${p}.expected" "${exp:-none}"
    if [ -n "${ver}" ] && [ "${ver}" = "${exp}" ]; then
        kv "${p}.version_changed" no
    else
        kv "${p}.version_changed" yes
    fi
}

read_claude_token() {
    local f="${HOME}/.guardian/llm.env" line
    [ -f "${f}" ] || return 1
    line="$(grep -E '^(export )?CLAUDE_CODE_OAUTH_TOKEN=' "${f}" | tail -n 1)" || return 1
    line="${line#export }"
    line="${line#CLAUDE_CODE_OAUTH_TOKEN=}"
    line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
    [ -n "${line}" ] || return 1
    printf '%s' "${line}"
}

# --- claude ------------------------------------------------------------------------

CLAUDE_FLAGS=()

claude_setup() {
    local cfg="${ROOT}/cfg-claude" work="${ROOT}/work-claude"
    mkdir -p "${cfg}" "${work}"
    work="$(cd "${work}" && pwd -P)"
    python3 - "${ROOT}" "${cfg}" "${work}" <<'PY'
import json, os, shlex, sys
root, cfg, work = sys.argv[1:4]
hook = lambda ev: [{"hooks": [{"type": "command", "command": " ".join(
    shlex.quote(x) for x in ("python3", os.path.join(root, "hook.py"), os.path.join(root, "signals.jsonl"), ev))}]}]
settings = {"promptSuggestionEnabled": False,
            "hooks": {ev: hook(ev) for ev in ("SessionStart", "UserPromptSubmit", "Stop", "Notification")}}
mcp = {"mcpServers": {"mailbox": {"command": "python3",
                                  "args": [os.path.join(root, "stub_mcp.py"), os.path.join(root, "mcp-claude.jsonl")]}}}
seed = {"hasCompletedOnboarding": True,
        "projects": {work: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}
for name, obj in (("settings.json", settings), ("mcp.json", mcp), (".claude.json", seed)):
    with open(os.path.join(cfg, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
PY
    CLAUDE_FLAGS=(--setting-sources "" --settings "${cfg}/settings.json" --tools "" --mcp-config "${cfg}/mcp.json"
        --strict-mcp-config --allowedTools mcp__mailbox__mail_inbox --disable-slash-commands --permission-mode dontAsk)
}

probe_claude() {
    local tok rc
    if ! command -v claude >/dev/null 2>&1; then
        kv claude.status skipped; kv claude.reason not_installed; return
    fi
    claude_setup
    say "🔎 claude: 버전"
    check_version claude CLAUDE_CONFIG_DIR "${ROOT}/cfg-claude" DISABLE_AUTOUPDATER=1
    if ! tok="$(read_claude_token)"; then
        kv claude.status skipped; kv claude.reason no_token; return
    fi
    say "🔎 claude: headless 한 턴 (stub mailbox MCP, 격리 확인)"
    (cd "${ROOT}/work-claude" && LAUNCH_TIMEOUT=300 launch_isolated CLAUDE_CONFIG_DIR "${ROOT}/cfg-claude" \
        "CLAUDE_CODE_OAUTH_TOKEN=${tok}" CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false DISABLE_AUTOUPDATER=1 -- \
        claude -p "$(prompt_for claude)" "${CLAUDE_FLAGS[@]}") >"${ROOT}/claude.out" 2>"${ROOT}/claude.err"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        kv claude.status error; kv claude.exit "${rc}"
    else
        kv claude.status ran
    fi
    if grep -q "${MAIL_WORD}" "${ROOT}/claude.out"; then kv claude.mcp pass; else kv claude.mcp fail; fi
    if [ -e "${ROOT}/bang-claude" ]; then kv claude.closure fail; else kv claude.closure pass; fi
    [ "${TUI}" = 1 ] && probe_claude_tui "${tok}"
}

# The open composer check: with a prompt suggestion on screen, a bare Enter must submit nothing.
probe_claude_tui() {
    local tok="$1" work wrapper shown submits
    if ! command -v tmux >/dev/null 2>&1; then
        kv claude.enter_check inconclusive; kv claude.enter_reason no_tmux; return
    fi
    say "🔎 claude: tmux 컴포저 확인 (약 1-4분)"
    work="${ROOT}/work-claude"
    : >"${ROOT}/signals.jsonl"
    wrapper="${ROOT}/claude-tui.sh"
    {
        printf '#!/usr/bin/env bash\nROOT=%q\nLAUNCH_TIMEOUT=900\nLAUNCH_FOREGROUND=1\n' "${ROOT}"
        declare -f say launch_isolated
        printf 'tok="${PROBE_TOK:-}"; unset PROBE_TOK\n'
        printf 'launch_isolated CLAUDE_CONFIG_DIR %q "CLAUDE_CODE_OAUTH_TOKEN=${tok}" CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false DISABLE_AUTOUPDATER=1 -- claude' "${ROOT}/cfg-claude"
        printf ' %q' "${CLAUDE_FLAGS[@]}"
        printf '\n'
    } >"${wrapper}"
    PROBE_TOK="${tok}" tmux -L "${TMUX_SOCK}" -f /dev/null new-session -d -s probe -x 200 -y 50 -c "${work}" "bash ${wrapper}"
    if ! wait_signal SessionStart 60; then
        kv claude.enter_check inconclusive; kv claude.enter_reason no_session_start; tui_stop; return
    fi
    sleep 3
    tmux -L "${TMUX_SOCK}" send-keys -t probe -l 'Reply with the single word READY.'
    tmux -L "${TMUX_SOCK}" send-keys -t probe Enter
    if ! wait_signal Stop 180; then
        kv claude.enter_check inconclusive; kv claude.enter_reason no_stop; tui_stop; return
    fi
    sleep 20
    tmux -L "${TMUX_SOCK}" capture-pane -p -e -t probe >"${ROOT}/pane.txt"
    shown="$(python3 - "${ROOT}/pane.txt" <<'PY'
import re, sys
lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
plain = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
cand = [l for l in lines if "❯" in l or plain(l).startswith(">")]
last = cand[-1] if cand else ""
print("yes" if re.search(r"\x1b\[(?:[0-9;]*;)?2(?:;[0-9;]*)?m[ \t]*[^\s\x1b]", last) else "no")
PY
)"
    tmux -L "${TMUX_SOCK}" send-keys -t probe Enter
    sleep 15
    submits="$(count_signal UserPromptSubmit)"
    tui_stop
    kv claude.suggestion_shown "${shown}"
    kv claude.prompt_submits "${submits}"
    if [ "${submits}" -ge 2 ]; then
        kv claude.enter_check fail
    elif [ "${submits}" -eq 1 ] && [ "${shown}" = yes ]; then
        kv claude.enter_check pass
    else
        kv claude.enter_check inconclusive; kv claude.enter_reason no_suggestion_shown
    fi
}

tui_stop() { tmux -L "${TMUX_SOCK}" kill-server >/dev/null 2>&1 || true; }

# --- agy ----------------------------------------------------------------------------

probe_agy() {
    local home="${ROOT}/agy-home" work="${ROOT}/work-agy" token="${HOME}/.gemini/antigravity-cli/antigravity-oauth-token" rc
    if ! command -v agy >/dev/null 2>&1; then
        kv agy.status skipped; kv agy.reason not_installed; return
    fi
    mkdir -p "${home}/.gemini/antigravity-cli/cache" "${home}/.gemini/config" "${work}"
    work="$(cd "${work}" && pwd -P)"
    say "🔎 agy: 버전"
    check_version agy HOME "${home}" AGY_CLI_DISABLE_AUTO_UPDATE=true
    if [ ! -f "${token}" ]; then
        kv agy.status skipped; kv agy.reason no_token; return
    fi
    ln -s "${token}" "${home}/.gemini/antigravity-cli/antigravity-oauth-token"
    python3 - "${ROOT}" "${home}" "${work}" <<'PY'
import json, os, shlex, sys
root, home, work = sys.argv[1:4]
cmd = lambda label: " ".join(shlex.quote(x) for x in (
    "python3", os.path.join(root, "hook.py"), os.path.join(root, "signals.jsonl"), label))
files = {
    ".gemini/config/mcp_config.json": {"mcpServers": {"mailbox": {
        "command": "python3", "args": [os.path.join(root, "stub_mcp.py"), os.path.join(root, "mcp-agy.jsonl")]}}},
    ".gemini/config/hooks.json": {"probe": {
        "SessionStart": [{"command": cmd("agy-SessionStart")}],
        "Stop": [{"command": cmd("agy-Stop")}],
        "PreToolUse": [{"matcher": "*", "hooks": [{"command": cmd("agy-PreToolUse")}]}]}},
    ".gemini/antigravity-cli/settings.json": {"trustedWorkspaces": [work],
                                              "permissions": {"allow": ["mcp(mailbox/mail_inbox)"]}},
    # Onboarding marker (operator's decision of 2026-11-15): the same values the node user's account already holds,
    # so the TUI skips the color-scheme and terms screens. Optional data sharing stays off (not part of the marker).
    ".gemini/antigravity-cli/cache/onboarding.json": {"consumerOnboardingComplete": True,
                                                      "enterpriseOnboardingComplete": False,
                                                      "onboardingComplete": True},
}
for rel, obj in files.items():
    with open(os.path.join(home, rel), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
PY
    say "🔎 agy: headless 한 턴 (stub mailbox MCP, PreToolUse hook)"
    python3 -c 'import json,sys; print(json.dumps({"event": "user", "message": {"content": sys.argv[1]}}))' \
        "$(prompt_for agy)" >"${ROOT}/agy.in"
    (cd "${work}" && LAUNCH_TIMEOUT=240 launch_isolated HOME "${home}" AGY_CLI_DISABLE_AUTO_UPDATE=true -- \
        agy --output-format stream-json --input-format stream-json --print-timeout 200s --disable-slash-commands \
        --print=) <"${ROOT}/agy.in" >"${ROOT}/agy.out" 2>"${ROOT}/agy.err"
    rc=$?
    python3 - "${ROOT}/agy.out" "${rc}" "${MAIL_WORD}" <<'PY'
import json, sys
path, rc, word = sys.argv[1], sys.argv[2], sys.argv[3]
result = None
for line in open(path, encoding="utf-8", errors="replace"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if isinstance(ev, dict) and ev.get("event") == "result":
        result = ev.get("result") or {}
if result is None:
    print("agy.status=error")
    print("agy.exit=%s" % rc)
    print("agy.mcp=fail")
else:
    print("agy.status=ran")
    print("agy.result_status=%s" % str(result.get("status", "")).replace("\n", " "))
    print("agy.mcp=%s" % ("pass" if word in str(result.get("response", "")) else "fail"))
PY
    if [ -e "${ROOT}/bang-agy" ]; then kv agy.closure fail; else kv agy.closure pass; fi
    kv agy.hook_denied "$(count_signal agy-PreToolUse deny)"
    # The updater verdict from agy's own logs: only the exact lowercase "true" disables it (2026-11-19).
    # A single spawned update anywhere in this fresh home wins over any "disabled" line.
    logs=("${home}"/.gemini/antigravity-cli/log/cli-*.log)
    if grep -q "Spawned background update process" "${logs[@]}" 2>/dev/null; then
        kv agy.autoupdate on
    elif grep -q "Auto-update disabled via environment variable" "${logs[@]}" 2>/dev/null; then
        kv agy.autoupdate off
    else
        kv agy.autoupdate unknown
    fi
    [ "${TUI}" = 1 ] && probe_agy_tui "${home}" "${work}"
}

# The agy TUI on the seeded home: no consent screen, whether `!` bash mode exists, and that a bare Enter on the empty
# composer submits nothing. Nothing is ever submitted; the pane (it shows the account) stays in the scratch directory.
probe_agy_tui() {
    local home="$1" work="$2" wrapper i shown stops
    if ! command -v tmux >/dev/null 2>&1; then
        kv agy.tui_onboarding inconclusive; kv agy.tui_reason no_tmux; return
    fi
    say "🔎 agy: tmux 화면 확인 (약 1분)"
    wrapper="${ROOT}/agy-tui.sh"
    {
        printf '#!/usr/bin/env bash\nROOT=%q\nLAUNCH_TIMEOUT=600\nLAUNCH_FOREGROUND=1\n' "${ROOT}"
        declare -f say launch_isolated
        printf 'launch_isolated HOME %q AGY_CLI_DISABLE_AUTO_UPDATE=true -- agy\n' "${home}"
    } >"${wrapper}"
    tmux -L "${TMUX_SOCK}" -f /dev/null new-session -d -s agy -x 200 -y 50 -c "${work}" "bash ${wrapper}"
    shown=""
    i=0
    while [ "${i}" -lt 60 ]; do
        sleep 1
        i=$((i + 1))
        tmux -L "${TMUX_SOCK}" capture-pane -p -t agy >"${ROOT}/agy-pane.txt" 2>/dev/null || continue
        if grep -Eqi 'Terms of Service|color scheme' "${ROOT}/agy-pane.txt"; then shown=consent_required; break; fi
        if grep -q '? for shortcuts' "${ROOT}/agy-pane.txt"; then shown=skipped; break; fi
    done
    kv agy.tui_onboarding "${shown:-inconclusive}"
    if [ "${shown}" != skipped ]; then
        [ -z "${shown}" ] && kv agy.tui_reason no_prompt
        tui_stop; return
    fi
    sleep 2
    tmux -L "${TMUX_SOCK}" send-keys -t agy -l '!'
    sleep 2
    tmux -L "${TMUX_SOCK}" capture-pane -p -t agy >"${ROOT}/agy-pane-bang.txt"
    if grep -qi 'bash mode' "${ROOT}/agy-pane-bang.txt"; then kv agy.bash_mode present; else kv agy.bash_mode absent; fi
    tmux -L "${TMUX_SOCK}" send-keys -t agy Escape
    sleep 2
    stops="$(count_signal agy-Stop)"
    tmux -L "${TMUX_SOCK}" send-keys -t agy Enter
    sleep 15
    stops=$(( $(count_signal agy-Stop) - stops ))
    tui_stop
    kv agy.enter_submits "${stops}"
    if [ "${stops}" -eq 0 ]; then kv agy.enter_check pass; else kv agy.enter_check fail; fi
}

# --- codex ---------------------------------------------------------------------------

probe_codex() {
    if ! command -v codex >/dev/null 2>&1; then
        kv codex.status skipped; kv codex.reason not_installed; return
    fi
    mkdir -p "${ROOT}/codex-home"
    say "🔎 codex: 버전 (상주 불가 판정, 턴은 돌리지 않음)"
    check_version codex CODEX_HOME "${ROOT}/codex-home"
    kv codex.resident "$(expect_of codex resident)"
    kv codex.status not_resident
}

# --- main ------------------------------------------------------------------------------

finish() {
    local rc=$?
    tui_stop
    if [ -n "${GUARD_BEFORE}" ]; then
        guard_report
    fi
    if [ -n "${ROOT}" ] && [ -d "${ROOT}" ]; then
        if [ "${KEEP}" = 1 ]; then
            kv probe.root "${ROOT}"
        else
            rm -rf "${ROOT}"
        fi
    fi
    exit "${rc}"
}

main() {
    local name val p
    while [ $# -gt 0 ]; do
        case "$1" in
            --expect)
                shift
                name="${1%%=*}"; val="${1#*=}"
                case "${name}" in claude|agy|codex) ;; *) say "알 수 없는 제공자: ${name}"; exit 1 ;; esac
                printf -v "EXPECT_version_${name}" '%s' "${val%%,*}"
                printf -v "EXPECT_resident_${name}" '%s' "${val#*,}"
                ;;
            --provider) shift; PROVIDERS+=("${1:-}") ;;
            --tui) TUI=1 ;;
            --keep) KEEP=1 ;;
            *) say "알 수 없는 인자: $1"; exit 1 ;;
        esac
        shift
    done
    [ ${#PROVIDERS[@]} -gt 0 ] || PROVIDERS=(claude agy codex)
    command -v python3 >/dev/null 2>&1 || { say "❌ 노드에 python3가 없습니다"; exit 1; }
    ROOT="$(mktemp -d "${TMPDIR:-/tmp}/resident-probe.XXXXXX")" || { say "❌ 스크래치 디렉터리를 만들지 못했습니다"; exit 1; }
    ROOT="$(cd "${ROOT}" && pwd -P)"
    GUARD_BEFORE="$(guard_snapshot)"
    trap finish EXIT
    trap 'exit 130' INT TERM
    write_shared
    for p in "${PROVIDERS[@]}"; do
        case "${p}" in
            claude) probe_claude ;;
            agy) probe_agy ;;
            codex) probe_codex ;;
            *) say "알 수 없는 제공자: ${p}"; exit 1 ;;
        esac
    done
}

if [ "${RESIDENT_PROBE_SOURCE_ONLY:-}" = 1 ]; then
    return 0 2>/dev/null || exit 0
fi
main "$@"

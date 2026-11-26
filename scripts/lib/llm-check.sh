#!/usr/bin/env bash
# Shared remote check: load ~/.guardian/llm.env and ask every installed LLM CLI for PONG.
# Sourced by scripts/deploy-llm-env.sh and scripts/probe-worker.sh; defines LLM_CHECK_SCRIPT, the text a host
# runs through `bash -s`. Output lines: "  ✅ <cli>" or "  ❌ <cli>: <output>" (worker_probe.parse_facts reads them).

# shellcheck disable=SC2034  # read by the scripts that source this file
read -r -d '' LLM_CHECK_SCRIPT <<'EOF' || true
if [ -f ~/.guardian/llm.env ]; then set -a; . ~/.guardian/llm.env; set +a; fi
export PATH="$HOME/.local/bin:$PATH"
work="$(mktemp -d)"; cd "${work}"
prompt="Reply with exactly: PONG"
found=0
check() {
    name="$1"; shift
    command -v "${bin:-${name}}" >/dev/null 2>&1 || return 0
    found=1
    # Judge the whole tail, cut only what is shown: a CLI notice before the answer (claude against z.ai prints
    # one) must not push PONG past the cut.
    out="$("$@" 2>&1 | tr -d '\r' | tail -n 3 | tr '\n' ' ')" || true
    case "${out}" in
        *PONG*) echo "  ✅ ${name}" ;;
        *) echo "  ❌ ${name}: $(printf '%s' "${out:-<no output>}" | cut -c1-160)" ;;
    esac
}
check claude sh -c "echo '${prompt}' | timeout 120 claude --print --tools '' --no-session-persistence"
check codex sh -c "echo '${prompt}' | timeout 150 codex exec --skip-git-repo-check --ephemeral --sandbox read-only --color never -o ${work}/codex.txt - >/dev/null 2>&1; cat ${work}/codex.txt 2>/dev/null"
check agy timeout 150 agy --print-timeout 120s --disable-slash-commands "--print=${prompt}"
# zai is the claude binary against z.ai with its own key; skipped on hosts without ZAI_API_KEY.
if [ -n "${ZAI_API_KEY:-}" ]; then
    bin=claude check zai env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN="${ZAI_API_KEY}" ANTHROPIC_BASE_URL="${ZAI_BASE_URL:-https://api.z.ai/api/anthropic}" CLAUDE_CONFIG_DIR="${work}/zai-config" sh -c "echo '${prompt}' | timeout 120 claude --print --tools '' --no-session-persistence"
fi
# exaone is a plain HTTP call to the EXAONE API (no CLI binary); skipped on hosts without EXAONE_API_KEY. The key
# reaches curl on stdin (-H @-), never in an argument that ps would show; llm.env exported it (set -a).
if [ -n "${EXAONE_API_KEY:-}" ]; then
    exaone_body='{"model":"'"${EXAONE_MODEL:-k-exaone_v2.5}"'","messages":[{"role":"user","content":"'"${prompt}"'"}],"reasoning_effort":"low","max_tokens":2048}'
    exaone_url="${EXAONE_BASE_URL:-https://api.exaone.ai/v1}/chat/completions"
    bin=curl check exaone env EXAONE_BODY="${exaone_body}" EXAONE_URL="${exaone_url}" sh -c 'printf "Authorization: Bearer %s\n" "${EXAONE_API_KEY}" | curl -sS --max-time 120 -H @- -H "Content-Type: application/json" -d "${EXAONE_BODY}" "${EXAONE_URL}"'
fi
[ "${found}" -eq 1 ] || echo "  ⚠️  설치된 LLM CLI 없음"
cd /; rm -rf "${work}"
EOF

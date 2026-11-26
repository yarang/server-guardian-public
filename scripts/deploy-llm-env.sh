#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Deploy LLM CLI credentials to the hub (or named hosts) and verify them
#
# Copies a local env file (e.g. CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)
# to ~/.guardian/llm.env (mode 600) on each host. TYPESAFE_* keys (jev-mcp) go only to
# the hub (guardian_role "hub" in the hub inventory); other hosts get the file without
# them. ZAI_API_KEY (z.ai Coding Plan, the zai provider) travels like the other keys and is
# used only by the zai provider, and so does EXAONE_API_KEY (the EXAONE API, the exaone provider: plain HTTP, no
# CLI). Then runs a one-line PONG test with every installed LLM CLI. Token values are sent
# over ssh stdin only and are never printed.
#
# Usage:
#   scripts/deploy-llm-env.sh [--check-only] [--dry-run] [--retire-stale-creds] [--offline] [host ...]
#
#   --check-only          Skip copying; only run the CLI checks on each host
#   --dry-run             Print the hosts and actions without connecting
#   --retire-stale-creds  Rename ~/.claude/.credentials.json to a .stale-<timestamp> backup.
#                         Recent claude versions prefer that file over CLAUDE_CODE_OAUTH_TOKEN,
#                         so an expired copy keeps failing with 401 until it is moved aside.
#   --offline             Use the checkout's configs/servers.json snapshot instead of the hub inventory
#   host ...      Default: the hub only (guardian_role "hub" in ~/.guardian/data/servers.json,
#                 fetched read-only over ssh; --dry-run reads it too). Credentials reach another
#                 host only when you name it here.
#
# Environment:
#   LLM_ENV_SOURCE  Local env file (default: ~/.config/server-guardian/llm.env)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR  # read by lib/inventory.sh
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
# shellcheck source=lib/llm-check.sh
source "${SCRIPT_DIR}/lib/llm-check.sh"
SOURCE_FILE="${LLM_ENV_SOURCE:-${HOME}/.config/server-guardian/llm.env}"

CHECK_ONLY=0
DRY_RUN=0
RETIRE_CREDS=0
HOSTS=()
for arg in "$@"; do
    case "${arg}" in
        --check-only) CHECK_ONLY=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --retire-stale-creds) RETIRE_CREDS=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: ${arg}" >&2; exit 2 ;;
        *) HOSTS+=("${arg}") ;;
    esac
done

# The role of each target decides which keys it receives, so the inventory is needed unless
# this is a check-only run on explicit hosts.
if [ ${#HOSTS[@]} -eq 0 ] || [ "${CHECK_ONLY}" -eq 0 ]; then inventory_load; fi
if [ ${#HOSTS[@]} -eq 0 ]; then
    while IFS= read -r host; do HOSTS+=("${host}"); done < <(
        python3 -c 'import json,sys; [print(s["id"]) for s in json.load(open(sys.argv[1])) if s.get("guardian_role") == "hub"]' \
            "${INVENTORY_FILE}"
    )
fi

if [ "${CHECK_ONLY}" -eq 0 ]; then
    if [ ! -f "${SOURCE_FILE}" ]; then
        echo "❌ 자격증명 파일이 없습니다: ${SOURCE_FILE}" >&2
        printf '   예) mkdir -p %s; umask 077; printf '"'"'CLAUDE_CODE_OAUTH_TOKEN=%%s\\n'"'"' "<claude setup-token 결과>" > %s\n' \
            "$(dirname "${SOURCE_FILE}")" "${SOURCE_FILE}" >&2
        exit 1
    fi
    perms="$(stat -f '%Lp' "${SOURCE_FILE}" 2>/dev/null || stat -c '%a' "${SOURCE_FILE}")"
    if [ "${perms}" != "600" ] && [ "${perms}" != "400" ]; then
        echo "❌ ${SOURCE_FILE} 권한이 ${perms}입니다. chmod 600 후 다시 실행하세요." >&2
        exit 1
    fi
    keys="$(grep -Eo '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "${SOURCE_FILE}" | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | tr '\n' ' ')"
    echo "📦 배포할 키: ${keys:-<없음>} (값은 출력하지 않음)"
    # setup-token prints the token wrapped over two lines; catch a half-copied value.
    token_len="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?CLAUDE_CODE_OAUTH_TOKEN=["'\'']?([^"'\'' ]*).*/\2/p' "${SOURCE_FILE}" | head -n 1 | tr -d '\n' | wc -c | tr -d ' ')"
    if [ "${token_len}" -gt 0 ] && [ "${token_len}" -lt 80 ]; then
        echo "❌ CLAUDE_CODE_OAUTH_TOKEN 길이가 ${token_len}자입니다. setup-token 출력의 두 줄을 모두 이어 붙였는지 확인하세요." >&2
        exit 1
    fi
fi

# Keys only the hub uses (jev-mcp runs there). Other hosts never receive them.
HUB_ONLY_KEYS_RE='^[[:space:]]*(export[[:space:]]+)?TYPESAFE_[A-Z_]*='
host_role() {
    python3 -c 'import json,sys; print(next((s.get("guardian_role","node") for s in json.load(open(sys.argv[1])) if s["id"]==sys.argv[2]), "node"))' \
        "${INVENTORY_FILE}" "$1"
}

# Remote check (shared with probe-worker.sh): load the env file and ask each installed CLI for PONG.
REMOTE_CHECK="${LLM_CHECK_SCRIPT}"

echo "🎯 대상: ${HOSTS[*]}"
for host in "${HOSTS[@]}"; do
    echo ""
    echo "##### ${host}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        [ "${CHECK_ONLY}" -eq 0 ] && echo "  (dry-run) ${SOURCE_FILE} -> ${host}:~/.guardian/llm.env (600)"
        [ "${RETIRE_CREDS}" -eq 1 ] && echo "  (dry-run) ~/.claude/.credentials.json -> .stale-<timestamp> 백업"
        echo "  (dry-run) LLM CLI 확인"
        continue
    fi
    if [ "${CHECK_ONLY}" -eq 0 ]; then
        role="$(host_role "${host}")"
        if [ "${role}" = hub ]; then filter=(cat); else filter=(grep -vE "${HUB_ONLY_KEYS_RE}"); fi
        if "${filter[@]}" < "${SOURCE_FILE}" | ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" \
            'umask 077; mkdir -p ~/.guardian && cat > ~/.guardian/llm.env.tmp && mv ~/.guardian/llm.env.tmp ~/.guardian/llm.env'; then
            if [ "${role}" = hub ]; then
                echo "  📥 ~/.guardian/llm.env 배치 완료 (600, 허브: 모든 키)"
            else
                echo "  📥 ~/.guardian/llm.env 배치 완료 (600, 허브 전용 키 제외)"
            fi
        else
            echo "  ❌ 배치 실패 (ssh)"
            continue
        fi
    fi
    if [ "${RETIRE_CREDS}" -eq 1 ]; then
        ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" \
            'f=~/.claude/.credentials.json; if [ -f "$f" ]; then b="$f.stale-$(date +%Y%m%d%H%M%S)"; mv "$f" "$b" && echo "  🗄️  기존 인증 파일 백업: $b"; else echo "  ℹ️  기존 인증 파일 없음"; fi' \
            || echo "  ❌ 인증 파일 백업 실패 (ssh)"
    fi
    ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" "bash -s" <<<"${REMOTE_CHECK}" || echo "  ❌ 확인 실패 (ssh)"
done

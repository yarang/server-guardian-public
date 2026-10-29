#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Install claude, codex and agy (Antigravity) CLIs on the hub (or named hosts)
#
# Installs per user into ~/.local/bin (no sudo):
#   claude: npm install -g @anthropic-ai/claude-code with the npm prefix set to ~/.local. The debate engine puts
#           ~/.local/bin first on PATH (llm_runner.py), so this copy wins over a root-owned global install, which
#           cannot update itself. Auth is unchanged: the setup-token in ~/.guardian/llm.env (deploy-llm-env.sh).
#   codex : npm install -g @openai/codex with the npm prefix set to ~/.local   (requires node >= 16, not checked)
#   agy   : official installer https://antigravity.google/cli/install.sh
#
# Login is interactive and per host (each host gets its own refresh token, so
# hosts never invalidate each other). Never copy ~/.codex/auth.json between hosts.
#
# Usage:
#   scripts/install-llm-clis.sh [--claude-only|--codex-only|--agy-only] [--claude-version=X.Y.Z] [--offline] [host ...]
#   --claude-version pins the claude version (default: latest).
#   Default host: the hub only (fetched read-only over ssh). Other hosts only when named.
#   --offline uses the checkout's configs/servers.json snapshot instead.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR  # read by lib/inventory.sh
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"

DO_CLAUDE=1
DO_CODEX=1
DO_AGY=1
CLAUDE_VERSION=latest
HOSTS=()
for arg in "$@"; do
    case "${arg}" in
        --claude-only) DO_CODEX=0; DO_AGY=0 ;;
        --codex-only) DO_CLAUDE=0; DO_AGY=0 ;;
        --agy-only) DO_CLAUDE=0; DO_CODEX=0 ;;
        --claude-version=*) CLAUDE_VERSION="${arg#--claude-version=}" ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: ${arg}" >&2; exit 2 ;;
        *) HOSTS+=("${arg}") ;;
    esac
done
if ! [[ "${CLAUDE_VERSION}" =~ ^(latest|stable|[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    echo "잘못된 --claude-version: ${CLAUDE_VERSION} (latest, stable 또는 X.Y.Z)" >&2
    exit 2
fi

if [ ${#HOSTS[@]} -eq 0 ]; then
    inventory_load
    while IFS= read -r host; do HOSTS+=("${host}"); done < <(
        python3 -c 'import json,sys; [print(s["id"]) for s in json.load(open(sys.argv[1])) if s.get("guardian_role") == "hub"]' \
            "${INVENTORY_FILE}"
    )
fi

read -r -d '' REMOTE_INSTALL <<'EOF' || true
set -u
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$HOME/.local/bin"
if [ "${DO_CLAUDE}" = 1 ]; then
    claude_installed=0
    if [ "$(readlink -f "$HOME/.local/bin/claude" 2>/dev/null)" != "${HOME}/.local/bin/claude" ] \
        && readlink -f "$HOME/.local/bin/claude" 2>/dev/null | grep -q "^$HOME/.local/share/claude/"; then
        # Native installer layout (~/.local/share/claude/versions/<v>): npm would collide with its symlink, so
        # use the CLI's own updater, which needs no sudo either.
        if [ "${CLAUDE_VERSION}" = latest ]; then
            "$HOME/.local/bin/claude" update >/tmp/claude-install.log 2>&1 && claude_installed=1
        else
            "$HOME/.local/bin/claude" install "${CLAUDE_VERSION}" >/tmp/claude-install.log 2>&1 && claude_installed=1
        fi
    elif command -v node >/dev/null 2>&1; then
        npm install --prefix "$HOME/.local" -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}" >/tmp/claude-install.log 2>&1 \
            && claude_installed=1
    else
        echo "  ⚠️  claude 건너뜀: node 없음"
        claude_installed=skip
    fi
    if [ "${claude_installed}" = 1 ]; then
        # The engine resolves claude with ~/.local/bin first; check that this copy is the one it gets.
        resolved="$(command -v claude)"
        echo "  ✅ claude $("$HOME/.local/bin/claude" --version 2>&1 | head -1) (${resolved})"
        if [ "${resolved}" != "$HOME/.local/bin/claude" ]; then
            echo "  ⚠️  PATH에서 먼저 잡히는 claude가 ${resolved}입니다. 엔진은 ~/.local/bin을 앞에 두지만 확인이 필요합니다."
        fi
        for other in /usr/local/bin/claude /usr/bin/claude; do
            if [ -x "${other}" ]; then
                echo "  ℹ️  다른 설치본 유지: ${other} $("${other}" --version 2>&1 | head -1) (엔진은 사용하지 않음)"
            fi
        done
    elif [ "${claude_installed}" = 0 ]; then
        echo "  ❌ claude 설치 실패: $(tail -n 2 /tmp/claude-install.log | tr '\n' ' ' | cut -c1-160)"
    fi
fi
if [ "${DO_CODEX}" = 1 ]; then
    if command -v node >/dev/null 2>&1; then
        if npm install --prefix "$HOME/.local" -g @openai/codex >/tmp/codex-install.log 2>&1; then
            echo "  ✅ codex $(codex --version 2>&1 | head -1)"
        else
            echo "  ❌ codex 설치 실패: $(tail -n 2 /tmp/codex-install.log | tr '\n' ' ' | cut -c1-160)"
        fi
    else
        echo "  ⚠️  codex 건너뜀: node 없음"
    fi
fi
if [ "${DO_AGY}" = 1 ]; then
    if [ -x "$HOME/.local/bin/agy" ]; then
        "$HOME/.local/bin/agy" update >/dev/null 2>&1 || true
        echo "  ✅ agy (이미 설치됨) $("$HOME/.local/bin/agy" --version 2>&1 | head -1)"
    elif curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh \
        && bash /tmp/agy-install.sh >/tmp/agy-install.log 2>&1; then
        echo "  ✅ agy $("$HOME/.local/bin/agy" --version 2>&1 | head -1)"
    else
        echo "  ❌ agy 설치 실패: $(tail -n 2 /tmp/agy-install.log 2>/dev/null | tr '\n' ' ' | cut -c1-160)"
    fi
    rm -f /tmp/agy-install.sh
fi
EOF

echo "🎯 대상: ${HOSTS[*]}"
for host in "${HOSTS[@]}"; do
    echo ""
    echo "##### ${host}"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "${host}" \
        "DO_CLAUDE=${DO_CLAUDE} CLAUDE_VERSION=${CLAUDE_VERSION} DO_CODEX=${DO_CODEX} DO_AGY=${DO_AGY} bash -s" <<<"${REMOTE_INSTALL}" || echo "  ❌ ssh 실패"
done

echo ""
echo "======================================================================"
echo "다음 단계: 호스트마다 대화형 로그인 (브라우저는 이 Mac에서 사용)"
echo "  claude: 로그인 불필요 (~/.guardian/llm.env의 setup-token, scripts/deploy-llm-env.sh)"
echo "  codex : ChatGPT > Settings > Security > 'Device code authorization' 활성화 후"
echo "          ssh -t <host> '~/.local/bin/codex login --device-auth'"
echo "  agy   : ssh -t <host> '~/.local/bin/agy'  (표시되는 URL을 열고 코드를 붙여넣기)"
echo "확인   : scripts/deploy-llm-env.sh --check-only"
echo "======================================================================"

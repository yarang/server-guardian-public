#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Secure Credential Import & Restore Script
# Decrypts AES-256 bundle and restores SSH keys, configs, and mesh credentials
# ==============================================================================

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "사용법: $0 <server-guardian-credentials-XXXXX.enc> [--legacy] [--yes]"
    echo "  --legacy  무결성 검사(HMAC)가 없는 예전 형식 파일을 복원합니다. 출처를 믿을 수 있을 때만 쓰세요."
    echo "  --yes     configs/servers.json 덮어쓰기를 묻지 않고 진행합니다 (diff는 출력)."
    exit 1
fi

ENC_FILE="$1"
LEGACY_OPT=()
ASSUME_YES=0
shift
for opt in "$@"; do
    case "${opt}" in
        --legacy) LEGACY_OPT=(--legacy) ;;
        --yes|-y) ASSUME_YES=1 ;;
        *) echo "알 수 없는 옵션: ${opt}" >&2; exit 2 ;;
    esac
done
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"

if [ ! -f "${ENC_FILE}" ]; then
    echo "❌ 에러: 파일을 찾을 수 없습니다: ${ENC_FILE}"
    exit 1
fi

STAGING_DIR=$(mktemp -d -t sg_import_XXXXXX)
cleanup() {
    rm -rf "${STAGING_DIR}"
}
trap cleanup EXIT

echo "======================================================================"
echo "🛡️  Server Guardian: 자격증명(Credentials) 복원 도구"
echo "======================================================================"
echo ""
echo "🔐 1. 무결성 확인 후 복호화 (HMAC-SHA256 → AES-256-CBC)"

ARCHIVE_TAR="${STAGING_DIR}/bundle.tar.gz"

if [ -n "${CREDS_PASSPHRASE:-}" ]; then
    passphrase="${CREDS_PASSPHRASE}"
else
    read -rsp "내보낼 때 설정했던 비밀번호(Passphrase): " passphrase; echo
fi
# The passphrase goes to openssl through a pipe, never on a command line (ps).
if ! printf '%s' "${passphrase}" | python3 "${SCRIPT_DIR}/creds_crypto.py" open "${ENC_FILE}" "${ARCHIVE_TAR}" ${LEGACY_OPT[@]+"${LEGACY_OPT[@]}"}; then
    unset passphrase
    echo "❌ 복호화 실패: 비밀번호가 올바르지 않거나 파일이 변조·손상되었습니다."
    exit 1
fi
unset passphrase

echo "📦 2. 아카이브 압축 해제 중..."
tar -xzf "${ARCHIVE_TAR}" -C "${STAGING_DIR}"

echo ""
echo "🚀 3. 자격증명 제자리 복원 및 파일 퍼미션(보안 권한) 적용 중..."

# 1. Restore ~/CERT
mkdir -p "${HOME}/CERT"
chmod 700 "${HOME}/CERT"
if [ -d "${STAGING_DIR}/CERT" ]; then
    cp -R "${STAGING_DIR}/CERT/"* "${HOME}/CERT/" 2>/dev/null || true
    # Enforce strict 600 permissions on all keys
    find "${HOME}/CERT" -type f -exec chmod 600 {} +
    find "${HOME}/CERT" -type d -exec chmod 700 {} +
    echo "  • [SSH Keys] ~/.ssh/ 복원 완료 (권한: 600 적용)"
fi

# 2. Restore ~/.config/cloudflare/.env
if [ -f "${STAGING_DIR}/config/cloudflare.env" ]; then
    mkdir -p "${HOME}/.config/cloudflare"
    chmod 700 "${HOME}/.config/cloudflare"
    cp "${STAGING_DIR}/config/cloudflare.env" "${HOME}/.config/cloudflare/.env"
    chmod 600 "${HOME}/.config/cloudflare/.env"
    echo "  • [Cloudflare] ~/.config/cloudflare/.env 복원 완료 (권한: 600 적용)"
fi

# 2-1. Older bundles carry ~/.config/headscale/.env with reusable auth keys. Those keys were expired on 2026-10-18,
# so the file is not restored.
if [ -f "${STAGING_DIR}/config/headscale.env" ]; then
    echo "  • [Headscale] 번들의 headscale.env는 만료된 인증 키라 복원하지 않습니다"
fi

# 2-2. Developer-track files: ~/.config/server-guardian and CLAUDE.local.md. A file that already exists and differs
# is kept as <file>.bak-<time> before it is replaced.
restore_file() {  # restore_file <src> <dest>
    if [ -f "$2" ] && ! cmp -s "$1" "$2"; then cp -p "$2" "$2.bak-$(date +%Y%m%d%H%M%S)"; fi
    cp "$1" "$2"; chmod 600 "$2"
}
if [ -d "${STAGING_DIR}/config/server-guardian" ]; then
    mkdir -p "${HOME}/.config/server-guardian"; chmod 700 "${HOME}/.config/server-guardian"
    for f in "${STAGING_DIR}/config/server-guardian/"*; do
        [ -f "${f}" ] && restore_file "${f}" "${HOME}/.config/server-guardian/$(basename "${f}")"
    done
    echo "  • [Server Guardian] ~/.config/server-guardian 복원 완료 (권한: 600 적용)"
fi
if [ -f "${STAGING_DIR}/config/CLAUDE.local.md" ]; then
    restore_file "${STAGING_DIR}/config/CLAUDE.local.md" "${ROOT_DIR}/CLAUDE.local.md"
    echo "  • [Server Guardian] CLAUDE.local.md 복원 완료"
fi

# 3. Restore the SSH host blocks into ~/.ssh/guardian_admin.conf and make ~/.ssh/config include it.
# The file is rewritten as a whole and the Include line is added once, so running the import again
# never appends a second copy of the blocks.
if [ -f "${STAGING_DIR}/config/ssh_config" ]; then
    mkdir -p "${HOME}/.ssh"
    chmod 700 "${HOME}/.ssh"
    ADMIN_CONF="${HOME}/.ssh/guardian_admin.conf"
    INCLUDE_LINE="Include ~/.ssh/guardian_admin.conf"
    if [ -f "${ADMIN_CONF}" ] && ! cmp -s "${STAGING_DIR}/config/ssh_config" "${ADMIN_CONF}"; then
        cp "${ADMIN_CONF}" "${ADMIN_CONF}.bak_$(date +%Y%m%d_%H%M%S)"
        echo "  • [SSH Config] 기존 ${ADMIN_CONF} 백업 생성됨"
    fi
    cp "${STAGING_DIR}/config/ssh_config" "${ADMIN_CONF}"
    chmod 600 "${ADMIN_CONF}"
    if [ -f "${HOME}/.ssh/config" ] && grep -qxF "${INCLUDE_LINE}" "${HOME}/.ssh/config"; then
        echo "  • [SSH Config] ~/.ssh/guardian_admin.conf 갱신 완료 (Include 이미 있음)"
    else
        if [ -f "${HOME}/.ssh/config" ]; then
            BACKUP_CONFIG="${HOME}/.ssh/config.bak_$(date +%Y%m%d_%H%M%S)"
            cp "${HOME}/.ssh/config" "${BACKUP_CONFIG}"
            echo "  • [SSH Config] 기존 설정 백업 생성됨: ${BACKUP_CONFIG}"
        fi
        # Include must come before the first Host/Match block to apply to every host.
        { printf '%s\n\n' "${INCLUDE_LINE}"; cat "${HOME}/.ssh/config" 2>/dev/null || true; } > "${HOME}/.ssh/config.new"
        mv "${HOME}/.ssh/config.new" "${HOME}/.ssh/config"
        echo "  • [SSH Config] ~/.ssh/guardian_admin.conf 작성, ~/.ssh/config 맨 위에 Include 추가"
    fi
    chmod 600 "${HOME}/.ssh/config"
fi

# 4. Offer the bundled inventory as the local configs/servers.json snapshot (same validation, diff,
# backup and confirmation as scripts/pull-inventory.sh). A refusal does not undo the credentials
# restored above.
if [ -f "${STAGING_DIR}/config/servers.json" ] && [ -d "${ROOT_DIR}/configs" ]; then
    inventory_install_snapshot "${STAGING_DIR}/config/servers.json" "백업 번들의 servers.json" "${ASSUME_YES}" \
        || echo "  • [Inventory] configs/servers.json은 바꾸지 않았습니다 (위 사유 참고). 허브 사본은 scripts/pull-inventory.sh로 가져오세요."
fi

echo ""
echo "======================================================================"
echo "🎉 모든 자격증명이 성공적으로 복원되었습니다!"
echo ""
# The control server's address travels in the bundle (headscale_info.json), not in git.
HS_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("server_url",""))' "${STAGING_DIR}/config/headscale_info.json" 2>/dev/null || true)"
HS_URL="${HS_URL:-https://hs.example.com}"
echo "📡 [Tailscale / Headscale Mesh 네트워크 연결]"
echo "새 노드를 Headscale 가상망에 연결하려면 아래 명령을 실행하고, 출력된 등록 URL의 hskey-authreq-... 값을"
echo "다른 관리 경로(이미 가상망에 있는 기기 또는 공인 SSH)로 제어 서버에서 승인하십시오 (인증 키 없음):"
echo ""
echo "tailscale login --nickname agentthread \\"
echo "  --login-server ${HS_URL} \\"
echo "  --hostname $(hostname -s)-admin --accept-routes"
echo "ssh site-y-ec2 sudo headscale auth register --user <user> --auth-id hskey-authreq-..."
echo ""
echo "🔍 복원 검증 테스트:"
echo "ssh site-y-ec2 \"uname -a\""
echo "======================================================================"

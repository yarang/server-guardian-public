#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Secure Credential Export Script
# Encrypted archive (AES-256-CBC + HMAC-SHA256, scripts/creds_crypto.py) for fleet credential migration
# ==============================================================================

set -euo pipefail

DATE_STR=$(date +"%Y%m%d_%H%M%S")
OUTPUT_FILE="server-guardian-credentials-${DATE_STR}.enc"
STAGING_DIR=$(mktemp -d -t sg_creds_XXXXXX)

cleanup() {
    rm -rf "${STAGING_DIR}"
}
trap cleanup EXIT

echo "======================================================================"
echo "🛡️  Server Guardian: 자격증명(Credentials) 안전 내보내기 도구"
echo "======================================================================"
echo ""

# 1. Staging directories
mkdir -p "${STAGING_DIR}/CERT"
mkdir -p "${STAGING_DIR}/config"

echo "📦 1. 이전에 필요한 핵심 자격증명 파일 수집 중..."

# 2. Copy SSH Keys from ~/CERT
if [ -d "${HOME}/CERT" ]; then
    echo "  • [SSH Keys] ~/.ssh/ 복사 중..."
    cp -R "${HOME}/CERT/"* "${STAGING_DIR}/CERT/" 2>/dev/null || true
else
    echo "  ⚠️  [주의] ~/CERT 디렉토리가 발견되지 않았습니다."
fi

# 3. Copy SSH Config
if [ -f "${HOME}/.ssh/config" ]; then
    echo "  • [SSH Config] ~/.ssh/config 복사 중..."
    cp "${HOME}/.ssh/config" "${STAGING_DIR}/config/ssh_config"
else
    echo "  ⚠️  [주의] ~/.ssh/config 파일이 발견되지 않았습니다."
fi

# 4. Copy Cloudflare API Credentials
if [ -f "${HOME}/.config/cloudflare/.env" ]; then
    echo "  • [Cloudflare] ~/.config/cloudflare/.env 복사 중..."
    cp "${HOME}/.config/cloudflare/.env" "${STAGING_DIR}/config/cloudflare.env"
fi

# 5. Copy Servers JSON
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${ROOT_DIR}/configs/servers.json" ]; then
    echo "  • [Inventory] configs/servers.json 복사 중..."
    cp "${ROOT_DIR}/configs/servers.json" "${STAGING_DIR}/config/servers.json"
fi

# 5-1. Developer-track files outside git: ~/.config/server-guardian (llm.env with the LLM CLI tokens,
# public-redactions.txt, the release signing key) and the git-ignored CLAUDE.local.md (placeholder -> real names).
if [ -d "${HOME}/.config/server-guardian" ]; then
    echo "  • [Server Guardian] ~/.config/server-guardian 복사 중 ($(find "${HOME}/.config/server-guardian" -type f | wc -l | tr -d ' ')개 파일)..."
    mkdir -p "${STAGING_DIR}/config/server-guardian"
    cp -Rp "${HOME}/.config/server-guardian/." "${STAGING_DIR}/config/server-guardian/"
fi
if [ -f "${ROOT_DIR}/CLAUDE.local.md" ]; then
    echo "  • [Server Guardian] CLAUDE.local.md 복사 중..."
    cp "${ROOT_DIR}/CLAUDE.local.md" "${STAGING_DIR}/config/CLAUDE.local.md"
fi

# 6. Headscale Connection Info
# No auth key goes into the bundle: the reusable keys were expired on 2026-10-18. Nodes join through the
# dashboard's mesh join approval, admin machines by a login the operator approves on the control server
# (docs/operations/headscale.md, "Adding a node").
echo "  • [Headscale] 가상망 접속 정보 생성 중 (인증 키는 넣지 않음)..."
# The control server's address is not kept in git: set GUARDIAN_HEADSCALE_URL (and GUARDIAN_HEADSCALE_BASE_DOMAIN) in the
# shell that runs this export. The bundle carries the real value, import-credentials.sh reads it back from there.
HS_URL="${GUARDIAN_HEADSCALE_URL:-https://hs.example.com}"
HS_BASE_DOMAIN="${GUARDIAN_HEADSCALE_BASE_DOMAIN:-mesh.example.com}"
if [ "${HS_URL}" = "https://hs.example.com" ]; then
    echo "  ⚠️  GUARDIAN_HEADSCALE_URL이 설정되지 않아 예제 주소가 번들에 들어갑니다. 설정 후 다시 내보내십시오."
fi
# admin_user is the account running this export (the Headscale admin on this Mac), not a name kept in git.
cat << EOF > "${STAGING_DIR}/config/headscale_info.json"
{
  "server_url": "${HS_URL}",
  "base_domain": "${HS_BASE_DOMAIN}",
  "admin_user": "$(whoami)",
  "auth_keys_file": "~/.config/headscale/.env",
  "connect_command": "tailscale login --nickname agentthread --login-server ${HS_URL} --authkey \"\$HEADSCALE_ADMIN_AUTHKEY\" --hostname <HOSTNAME> --accept-routes"
}
EOF

# 7. Add Restore Instructions README
cat << 'EOF' > "${STAGING_DIR}/README.txt"
Server Guardian Credentials Bundle
==================================
본 아카이브는 플릿 인프라(클라우드, 온프레미스) 관리용 핵심 자격증명을 포함합니다.
- CERT/                 : SSH 원격 접속 개인키 및 인증서
- config/ssh_config     : 노드 호스트 별칭 매핑 설정
- config/cloudflare.env : DNS 관리용 Cloudflare API Token
- config/servers.json   : 노드 메타데이터 및 사설망 IP 매핑
- config/headscale_info.json : Headscale Mesh 접속 정보 (인증 키 없음: 노드는 대시보드의 메시 가입 승인으로,
  관리용 기기는 제어 서버에서 headscale auth register로 가입)
- config/server-guardian/ : ~/.config/server-guardian (LLM 토큰 llm.env, 이름 가리기 규칙, 릴리스 서명 키)
- config/CLAUDE.local.md  : 가린 이름과 실제 이름의 대응표 (저장소 루트, git 제외)
EOF

# 8. Compress and encrypt. The passphrase reaches openssl through a pipe (never argv, so it does
# not show in ps), and the bundle carries an HMAC so a modified file is refused on import.
echo ""
echo "🔐 2. 암호화 아카이브 생성 (AES-256-CBC + HMAC-SHA256)"

ARCHIVE_TAR="${STAGING_DIR}/bundle.tar.gz"
tar -czf "${ARCHIVE_TAR}" -C "${STAGING_DIR}" CERT config README.txt

if [ -n "${CREDS_PASSPHRASE:-}" ]; then
    passphrase="${CREDS_PASSPHRASE}"
else
    echo "아카이브를 보호할 비밀번호(Passphrase)를 입력해 주세요."
    echo "(이 비밀번호는 새 장비에서 복원할 때 반드시 필요합니다)"
    read -rsp "비밀번호: " passphrase; echo
    read -rsp "비밀번호 확인: " confirm; echo
    if [ "${passphrase}" != "${confirm}" ]; then echo "❌ 비밀번호가 일치하지 않습니다." >&2; exit 1; fi
    unset confirm
fi
if [ -z "${passphrase}" ]; then echo "❌ 비밀번호가 비어 있습니다." >&2; exit 1; fi
printf '%s' "${passphrase}" | python3 "${SCRIPT_DIR}/creds_crypto.py" seal "${ARCHIVE_TAR}" "${ROOT_DIR}/${OUTPUT_FILE}"
unset passphrase

echo ""
echo "======================================================================"
echo "✅ 암호화 백업 완료!"
echo "생성된 파일: ${ROOT_DIR}/${OUTPUT_FILE}"
echo "파일 크기  : $(du -h "${ROOT_DIR}/${OUTPUT_FILE}" | cut -f1)"
echo "암호화 방식: AES-256-CBC + HMAC-SHA256 (PBKDF2-SHA256 600,000회)"
echo ""
echo "📌 이전 방법:"
echo "1. ${OUTPUT_FILE} 파일을 새 관리자 PC로 전송합니다 (USB, AirDrop, 안전한 클라우드 등)."
echo "2. 새 PC에서 './scripts/import-credentials.sh ${OUTPUT_FILE}' 명령을 실행하면"
echo "   모든 SSH 키 권한(600) 및 경로가 제자리에 자동 복원됩니다."
echo "======================================================================"

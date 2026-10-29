#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Build, sign and publish a release (developer track; docs/design/install-tracks.md, R2)
#
# Builds the release tree (scripts/lib/release-tree.sh) for linux/arm64 and linux/amd64 from a clean, pushed main,
# packs server-guardian-<tag>-linux-<arch>.tar.gz, writes SHA256SUMS, checks that no private name is in any text
# file of the trees (scripts/redact-private.py rules), signs every archive with `ssh-keygen -Y sign` (namespace
# server-guardian-release) and verifies the signatures against deploy/release_signers. Without --publish it stops
# there and leaves the files in the output directory.
#
# Usage:
#   scripts/release.sh [--tag vYYYY.MM.DD[.N]] [--out DIR] [--publish OWNER/REPO]
#
#   --tag      the release tag (default: the v* tag on HEAD, else v<UTC date>, with .2, .3, ... when taken)
#   --out      output directory (default: dist/release/<tag>, git-ignored)
#   --publish  tag HEAD (annotated) and push the tag to origin, rebuild the public copy at OWNER/REPO with
#              scripts/export-public.sh, and create the GitHub release there with the archives, their .sig files,
#              SHA256SUMS and release_signers. OWNER/REPO is the public copy (an argument, so no account name
#              is kept in this file).
#
# Signing key: SG_RELEASE_KEY, the PUBLIC key file of the signing key (default
# ~/.config/server-guardian/release_signing_key.pub). The private key is used through ssh-agent (ssh-add it first,
# or use a hardware key), so this script never reads it and never asks for its passphrase. SG_RELEASE_SIGNERS
# replaces deploy/release_signers (for a test with a throwaway key; never with --publish).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/release-tree.sh
source "${SCRIPT_DIR}/lib/release-tree.sh"
NAMESPACE="server-guardian-release"
PRINCIPAL="release@server-guardian"
SIGNERS="${SG_RELEASE_SIGNERS:-${ROOT_DIR}/deploy/release_signers}"
KEY="${SG_RELEASE_KEY:-${HOME}/.config/server-guardian/release_signing_key.pub}"
ARCHES=(arm64 amd64)

TAG=""
OUT=""
PUBLISH=""
while [ $# -gt 0 ]; do
    case "$1" in
        --tag) TAG="$2"; shift ;;
        --out) OUT="$2"; shift ;;
        --publish) PUBLISH="$2"; shift ;;
        -h|--help) sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
    esac
    shift
done
if [ -n "${PUBLISH}" ] && [ -n "${SG_RELEASE_SIGNERS:-}" ]; then
    echo "--publish는 저장소의 deploy/release_signers로만 합니다 (SG_RELEASE_SIGNERS 해제)." >&2; exit 2
fi
if [ -n "${PUBLISH}" ] && ! [[ "${PUBLISH}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo "--publish는 OWNER/REPO 형식이어야 합니다." >&2; exit 2
fi

cd "${ROOT_DIR}"
# A release is exactly a commit everyone can fetch: clean tracked files, HEAD at the pushed main (a detached
# worktree of that commit is fine).
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "❌ 커밋하지 않은 변경이 있습니다." >&2; exit 2; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "❌ HEAD가 origin/main과 다릅니다 (push 먼저)." >&2; exit 2; }
[ -f "${SIGNERS}" ] || { echo "❌ ${SIGNERS}가 없습니다 (서명 공개키를 먼저 등록하세요)." >&2; exit 2; }
[ -f "${KEY}" ] || { echo "❌ 서명 공개키 ${KEY}가 없습니다 (SG_RELEASE_KEY)." >&2; exit 2; }
# The key must be the one hubs trust: its "<type> <body>" appears in release_signers.
key_body="$(awk '{print $1" "$2}' "${KEY}")"
grep -qF -- "${key_body}" "${SIGNERS}" || { echo "❌ ${KEY}가 deploy/release_signers의 키가 아닙니다." >&2; exit 2; }

if [ -z "${TAG}" ]; then
    TAG="$(git tag --points-at HEAD --list 'v*' | sort -V | tail -n 1)"
fi
if [ -z "${TAG}" ]; then
    TAG="v$(date -u +%Y.%m.%d)"
    n=1
    while git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null || git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; do
        n=$((n + 1)); TAG="v$(date -u +%Y.%m.%d).${n}"
    done
fi
[[ "${TAG}" =~ ^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$ ]] || { echo "❌ 태그는 vYYYY.MM.DD[.N] 형식이어야 합니다: ${TAG}" >&2; exit 2; }
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null && [ "$(git rev-parse "${TAG}^{commit}")" != "$(git rev-parse HEAD)" ]; then
    echo "❌ 태그 ${TAG}는 이미 다른 커밋에 있습니다." >&2; exit 2
fi
OUT="${OUT:-${ROOT_DIR}/dist/release/${TAG}}"
[ ! -e "${OUT}" ] || { echo "❌ ${OUT}가 이미 있습니다." >&2; exit 2; }
mkdir -p "${OUT}"
work="$(mktemp -d -t sg-release.XXXXXX)"
trap 'rm -rf "${work}"' EXIT
echo "🏷️  릴리스 ${TAG} ($(git rev-parse --short HEAD))"

release_build_ui "${ROOT_DIR}"
for arch in "${ARCHES[@]}"; do
    release_tree "${ROOT_DIR}" "${arch}" "${TAG}" "${work}/${arch}"
done

echo "🔍 사적 이름 검사 (트리의 텍스트 파일)"
# ui/ is built only from tracked sources (checked by the same rules in CI and export-public.sh) and third-party
# packages, whose minified code can contain a short masked word by chance (one cytoscape identifier matches a masked site word), so it is left out
# here; the home-path check below still covers it.
find "${work}" -type f ! -path '*/bin/*' ! -path '*/ui/*' -print0 | xargs -0 python3 "${SCRIPT_DIR}/redact-private.py" --check \
    || { echo "❌ 릴리스 트리에 가려지지 않은 이름이 있습니다." >&2; exit 1; }
# The binaries carry no local paths (-trimpath); check that the build machine's home did not slip in.
if grep -rlF -- "${HOME}" "${work}" >/dev/null 2>&1; then
    echo "❌ 릴리스에 이 기기의 홈 경로(${HOME})가 들어 있습니다." >&2; exit 1
fi

names=()
for arch in "${ARCHES[@]}"; do
    name="server-guardian-${TAG}-linux-${arch}.tar.gz"
    release_archive "${work}/${arch}" "${OUT}/${name}"
    names+=("${name}")
done
cp "${SIGNERS}" "${OUT}/release_signers"
(cd "${OUT}" && shasum -a 256 "${names[@]}" > SHA256SUMS)

echo "✍️  서명 (ssh-keygen -Y sign, ${NAMESPACE})"
for name in "${names[@]}"; do
    ssh-keygen -Y sign -q -f "${KEY}" -n "${NAMESPACE}" "${OUT}/${name}" \
        || { echo "❌ 서명하지 못했습니다. 서명 키를 ssh-agent에 올렸는지 확인하세요: ssh-add <개인키>" >&2; exit 1; }
    ssh-keygen -Y verify -f "${SIGNERS}" -I "${PRINCIPAL}" -n "${NAMESPACE}" -s "${OUT}/${name}.sig" < "${OUT}/${name}" >/dev/null \
        || { echo "❌ 서명 검증에 실패했습니다: ${name}" >&2; exit 1; }
    echo "  ${name}: 서명·검증 완료"
done
cat "${OUT}/SHA256SUMS"

if [ -z "${PUBLISH}" ]; then
    echo "✅ ${OUT}에 만들었습니다 (게시하지 않음; --publish OWNER/REPO로 게시)."
    exit 0
fi

echo "📣 게시: ${PUBLISH}"
if ! git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    git tag -a "${TAG}" -m "Server Guardian ${TAG}"
fi
git push -q origin "refs/tags/${TAG}"
"${SCRIPT_DIR}/export-public.sh" --push "git@github.com:${PUBLISH}.git"
notes="${work}/notes.md"
{
    echo "Server Guardian ${TAG}"
    echo
    echo "Archives for linux/arm64 and linux/amd64. Verify before installing (docs/design/install-tracks.md):"
    echo
    echo '```bash'
    echo "sha256sum -c SHA256SUMS --ignore-missing"
    echo "ssh-keygen -Y verify -f release_signers -I ${PRINCIPAL} -n ${NAMESPACE} \\"
    echo "  -s server-guardian-${TAG}-linux-arm64.tar.gz.sig < server-guardian-${TAG}-linux-arm64.tar.gz"
    echo '```'
    echo
    echo "Signing key: $(ssh-keygen -lf "${KEY}" | awk '{print $1, $2}')"
} > "${notes}"
assets=()
for name in "${names[@]}"; do assets+=("${OUT}/${name}" "${OUT}/${name}.sig"); done
gh release create "${TAG}" --repo "${PUBLISH}" --target main --title "Server Guardian ${TAG}" --notes-file "${notes}" \
    "${assets[@]}" "${OUT}/SHA256SUMS" "${OUT}/release_signers"
echo "✅ 게시 완료: https://github.com/${PUBLISH}/releases/tag/${TAG}"

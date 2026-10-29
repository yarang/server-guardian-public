#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Publish a redacted snapshot of main to the public repository
#
# The public repository is a curated history, not a mirror: every export adds ONE commit that holds the whole
# redacted tree of this repository's main (decided 2026-10-23). The private history is never rewritten or pushed,
# so nothing that was ever committed here (old site names, deleted files) can leak through it, and a later export
# fast-forwards the public repository without a force push.
#
#   1. archives the committed tree of main, without the paths in scripts/public-exclude.txt (inventory snapshot,
#      decision DB, credential bundles, compiled Python, evaluation results, ...)
#   2. masks private names in the text files with the redaction rules (the same file scripts/redact-private.py
#      uses; it maps placeholders back to real names, so it stays outside the repository in
#      ~/.config/server-guardian/public-redactions.txt)
#   3. clones the public remote, replaces its tree with the result (deleted files disappear too) and commits
#   4. checks the new tree (file paths and contents) and the commit message against the rules: one match stops
#   5. with --push, pushes the commit (a plain push; it is refused when the public main moved in the meantime)
# The commit author is the git identity of this repository.
#
# Usage:
#   scripts/export-public.sh --remote URL [--push] [--rules FILE] [--keep]
#
#   --remote URL   the public repository (for example git@github.com:<account>/<public repo>.git); required
#   --push         push the new commit; without it the commit stays in a local clone for review
#   --rules FILE   redaction rules (default: ~/.config/server-guardian/public-redactions.txt)
#   --keep         keep the working directory and print its path (the review clone is in <dir>/public)
# Runs from the admin Mac. Needs only git and python3.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RULES="${HOME}/.config/server-guardian/public-redactions.txt"
EXCLUDES="${SCRIPT_DIR}/public-exclude.txt"
REMOTE=""
PUSH=0
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --remote) REMOTE="$2"; shift ;;
        --push) PUSH=1 ;;
        --rules) RULES="$2"; shift ;;
        --keep) KEEP=1 ;;
        -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
    esac
    shift
done
[ -n "${REMOTE}" ] || { echo "❌ --remote URL이 필요합니다." >&2; exit 2; }
[ -f "${RULES}" ] || { echo "❌ 가리기 규칙이 없습니다: ${RULES}" >&2; exit 2; }
[ -f "${EXCLUDES}" ] || { echo "❌ 제외 목록이 없습니다: ${EXCLUDES}" >&2; exit 2; }

# The working tree itself must already be clean of private names (the snapshot is made from the committed main,
# but a rule the tree still trips would be a rule the next commit trips too).
python3 "${SCRIPT_DIR}/redact-private.py" --rules "${RULES}" --check >/dev/null \
    || { echo "❌ 현재 트리에 가려지지 않은 이름이 있습니다: scripts/redact-private.py --check" >&2; exit 1; }

work="$(mktemp -d -t sg-public.XXXXXX)"
if [ "${KEEP}" -eq 0 ]; then trap 'rm -rf "${work}"' EXIT; fi
src_sha="$(git -C "${ROOT_DIR}" rev-parse --short=12 main)"
author_name="$(git -C "${ROOT_DIR}" config user.name)"
author_email="$(git -C "${ROOT_DIR}" config user.email)"
[ -n "${author_name}" ] && [ -n "${author_email}" ] || { echo "❌ git user.name/user.email이 필요합니다." >&2; exit 2; }

echo "📦 main(${src_sha}) 트리 추출 (제외 목록 적용): ${work}/tree"
mkdir "${work}/tree"
excludes=()
while IFS= read -r line; do
    case "${line}" in ''|'#'*) continue ;; esac
    excludes+=(":(exclude,glob)${line}")
done < "${EXCLUDES}"
git -C "${ROOT_DIR}" archive main -- . "${excludes[@]}" | tar -x -C "${work}/tree"

echo "✂️  내용 가리기"
find "${work}/tree" -type f -print0 | xargs -0 python3 "${SCRIPT_DIR}/redact-private.py" --rules "${RULES}"

echo "📥 공개 저장소 복제: ${REMOTE}"
git clone -q "${REMOTE}" "${work}/public" 2>/dev/null || git init -q "${work}/public"
cd "${work}/public"
git checkout -q main 2>/dev/null || git checkout -q --orphan main
git ls-files -z | xargs -0 -r git rm -q -f --
tar -c -C "${work}/tree" . | tar -x -C "${work}/public"
git add -A
if git diff --cached --quiet; then
    echo "ℹ️  공개 저장소와 같은 내용입니다. 올릴 것이 없습니다."
    if [ "${KEEP}" -eq 1 ]; then echo "📁 작업 디렉터리: ${work}"; fi
    exit 0
fi
git -c user.name="${author_name}" -c user.email="${author_email}" \
    commit -q -m "chore: public snapshot $(date -u +%Y-%m-%d) (${src_sha})"
git diff --stat HEAD~1 HEAD 2>/dev/null | tail -1 || true

echo "🔎 새 트리와 커밋 메시지 검사"
python3 - "${RULES}" <<'PY'
import re, subprocess, sys
rules = []
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    left = line.partition("==>")[0]
    rules.append(re.compile(left[6:].encode() if left.startswith("regex:") else re.escape(left.encode())))
bad = 0
listing = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD"], capture_output=True, check=True).stdout.split(b"\0")
for entry in listing:
    meta, _, path = entry.partition(b"\t")
    if not meta:
        continue
    sha = meta.split()[2]
    data = subprocess.run(["git", "cat-file", "blob", sha], capture_output=True, check=True).stdout
    for label, text in (("path", path), ("content", data)):
        for pattern in rules:
            match = pattern.search(text)
            if match:
                bad += 1
                print(f"  {label} {path.decode(errors='replace')}: {match.group(0).decode(errors='replace')}")
                break
message = subprocess.run(["git", "log", "-1", "--format=%B"], capture_output=True, check=True).stdout
for pattern in rules:
    if pattern.search(message):
        bad += 1
        print("  commit message matches a rule")
for forbidden in (b"configs/servers.json", b"docs/decisions/guardian_decisions.db", b"CLAUDE.local.md"):
    if any(e.partition(b"\t")[2] == forbidden for e in listing):
        bad += 1
        print(f"  excluded path present: {forbidden.decode()}")
print(f"  남은 항목: {bad}")
sys.exit(1 if bad else 0)
PY

echo "✅ 새 커밋 $(git rev-parse --short HEAD) (공개 main 위 1개)"
if [ "${PUSH}" -eq 1 ]; then
    echo "📤 ${REMOTE} 에 main push"
    git push "${REMOTE}" main:main
fi
if [ "${KEEP}" -eq 1 ]; then echo "📁 검토용 사본: ${work}/public"; fi

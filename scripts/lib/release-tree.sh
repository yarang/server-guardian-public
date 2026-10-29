#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Assemble a release tree (docs/design/install-tracks.md, section 4.1)
#
# Sourced by scripts/install-agent-service.sh (developer deploys) and scripts/release.sh (signed releases), so
# both produce the same tree:
#
#   bin/agent, bin/goindex (optional)  cross-compiled for linux/<goarch>, AgentVersion = <version>
#   ui/                                the built dashboard
#   cli/, configs/, deploy/            the files git tracks, as they are in the working tree, without tests
#   VERSION, MANIFEST                  the version; sha256 of every other file ("<hash>  ./<path>")
#
#   release_build_ui <root>                       build the dashboard once (it does not depend on the architecture)
#   release_tree <root> <goarch> <version> <dir>  fill <dir> (created; must not exist)
#   release_archive <dir> <file.tar.gz>           pack a tree
# ==============================================================================

# npm ci when node_modules is missing or package-lock.json changed since the last install
# (node_modules/.package-lock.json is written by npm), so a release builds what CI builds.
release_node_deps() {
    local dir="$1"
    if [ ! -d "${dir}/node_modules" ] || [ "${dir}/package-lock.json" -nt "${dir}/node_modules/.package-lock.json" ]; then
        echo "  npm ci (${dir##*/})"
        (cd "${dir}" && npm ci --no-audit --no-fund >/dev/null)
    fi
}

release_build_ui() {
    local root="$1"
    echo "🖥️  web-desktop 빌드"
    # The build script (prebuild) rebuilds packages/guardian-ui, which has its own lockfile.
    release_node_deps "${root}/packages/guardian-ui"
    release_node_deps "${root}/apps/web-desktop"
    (cd "${root}/apps/web-desktop" && npm run build >/dev/null)
}

release_tree() {
    local root="$1" goarch="$2" version="$3" tree="$4"
    [ ! -e "${tree}" ] || { echo "release_tree: ${tree} already exists" >&2; return 1; }
    mkdir -p "${tree}/bin" "${tree}/ui"
    echo "🔨 apps/agent 빌드 (linux/${goarch}, ${version})"
    (cd "${root}/apps/agent" && GOOS=linux GOARCH="${goarch}" CGO_ENABLED=0 \
        go build -trimpath -ldflags "-X main.AgentVersion=${version}" -o "${tree}/bin/agent" .) || return 1
    # Compiler-assisted Go index (docs/design/debate-code-index-wiki.md, P2.6). Optional: without it Go requests are
    # answered by name, so a failed build is a warning.
    echo "🔬 tools/goindex 빌드 (linux/${goarch})"
    (cd "${root}/tools/goindex" && GOOS=linux GOARCH="${goarch}" CGO_ENABLED=0 go build -trimpath -o "${tree}/bin/goindex" .) \
        || { rm -f "${tree}/bin/goindex"; echo "⚠️  goindex를 빌드하지 못했습니다. 이 릴리스에는 빠집니다." >&2; }
    [ -f "${root}/apps/web-desktop/dist/index.html" ] || { echo "release_tree: run release_build_ui first" >&2; return 1; }
    cp -R "${root}/apps/web-desktop/dist/." "${tree}/ui/"
    # Tracked files only (never an untracked secret or a local config), without tests.
    (cd "${root}" && git ls-files -z -- cli configs deploy \
        | grep -zvE '(^|/)(test_[^/]*\.py|testenv\.py)$' \
        | COPYFILE_DISABLE=1 xargs -0 tar --no-xattrs -cf -) | tar -xf - -C "${tree}" || return 1
    printf '%s\n' "${version}" > "${tree}/VERSION"
    (cd "${tree}" && find . -type f ! -name MANIFEST -print0 | sort -z | xargs -0 shasum -a 256 > MANIFEST)
    echo "  $(wc -l < "${tree}/MANIFEST" | tr -d ' ')개 파일"
}

release_archive() {
    COPYFILE_DISABLE=1 tar --no-xattrs -C "$1" -czf "$2" .
}

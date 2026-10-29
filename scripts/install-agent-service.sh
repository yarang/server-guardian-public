#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Build a release on this machine and install it on the hub (developer track)
#
# docs/design/install-tracks.md, R1. Cross-compiles apps/agent and tools/goindex, builds the dashboard, assembles a
# release tree (bin/, ui/, the tracked cli/, configs/ and deploy/ files without tests, VERSION, MANIFEST), uploads it
# to ~/.guardian/releases/<name>/ on the hub and runs its deploy/hub/install.sh there. That script writes
# agent.env, installs the systemd units, switches ~/.guardian/current to the new release (the old one becomes
# ~/.guardian/previous), restarts the agent, switches back on its own when it does not start, and checks it.
# Server Guardian runs a single agent, on the hub: the other hosts are reached over SSH (scripts/setup-hub-ssh.sh).
#
# Usage:
#   scripts/install-agent-service.sh <hub-id> [--listen <ips>] [--no-pull] [--offline] [--dry-run]
#                                             [--hub-sudo | --no-hub-sudo] [--no-ssh-hardening]
#
# The host must be the hub: the inventory entry with "guardian_role": "hub". Any other host is
# refused (exit 2); there is no per-node install. The inventory is the hub's own copy
# (~/.guardian/data/servers.json, fetched read-only over ssh, see scripts/lib/inventory.sh).
# --offline uses the checkout's configs/servers.json instead and prints a warning; use it for the
# first install, or when the hub cannot be reached. The hub's own copy is never overwritten.
#
#   --listen <ips> AGENT_HOST value, comma-separated (default: 127.0.0.1 plus the hub's Tailscale IPv4)
#   --with-ui      Accepted for compatibility and ignored: the dashboard is always built and served.
#   --no-ui        Refused (exit 2): the hub cannot run without its dashboard.
#   --offline      Take the inventory from the checkout (a snapshot) instead of the hub; see above.
#   --dry-run      Build and upload the release, let install.sh report what would change; change nothing else.
#   --no-pull      Skip updating ~/server-guardian and the workspace clone on the hub.
#                  The hub checkout is no longer what runs; it is the source of the builtin server-guardian project
#                  and its workspace clone (developer track only), so a failed pull is a warning.
#   --hub-sudo     Let the agent, judge actions and the dashboard terminal use sudo on the hub
#                  (drop-in deploy/systemd/server-guardian-agent.service.d/hub-sudo.conf).
#                  The default on a first install; root can then read the fleet key.
#   --no-hub-sudo  Keep the full sandbox (NoNewPrivileges, read-only /usr and /etc)
#                  Without either flag the hub keeps its current setting.
#   HUB_SSH_HOSTNAME=<ip or name>
#                  Environment: connect to the hub at this address instead of the ssh config's HostName (for
#                  example its Tailscale IP when the current network breaks large ssh transfers). Without it, a
#                  failed upload is retried once over the hub's Tailscale IPv4.
#   --no-ssh-hardening
#                  Skip the managed sshd drop-in deploy/ssh/00-guardian-hardening.conf (MaxAuthTries 4,
#                  PermitRootLogin no) on the hub. Fleet nodes are never touched by this script; see
#                  docs/operations/2026-10-05-fleet-sshd-hardening.md.
#
# Also set on the hub: the workspace clone for judge actions (GUARDIAN_WORKSPACE=~/guardian-workspace),
# GUARDIAN_LLM_TIMEOUT=600 and GUARDIAN_DEBATE_TIMEOUT=7200 (override with HUB_LLM_TIMEOUT / HUB_DEBATE_TIMEOUT),
# Go ${HUB_GO_VERSION:-<apps/agent/go.mod toolchain>} in ~/.local/go to check judge patches, and
# GUARDIAN_SERVERS_FILE=~/.guardian/data/servers.json (seeded from the inventory only when it does not exist).
#
# agent.env: only the keys install.sh owns are rewritten; every other KEY=value line is kept.
# Roll back to the previous release: ssh <hub> bash ~/.guardian/current/deploy/hub/install.sh --rollback
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
# shellcheck source=lib/release-tree.sh
source "${SCRIPT_DIR}/lib/release-tree.sh"
# Judge actions that edit files or commit run in this clone, never in the release the agent runs from.
WORKSPACE_REMOTE="/home/ubuntu/guardian-workspace"
# Debates on the hub: seconds per LLM CLI call (a judge prompt with a code context can exceed 300s)
# and per whole debate job. Override with HUB_LLM_TIMEOUT / HUB_DEBATE_TIMEOUT when running this script.
HUB_LLM_TIMEOUT="${HUB_LLM_TIMEOUT:-600}"
HUB_DEBATE_TIMEOUT="${HUB_DEBATE_TIMEOUT:-7200}"
# Go toolchain on the hub, used to gofmt-check and build judge patches to Go files (patch_ops).
# The default is the single source of truth, apps/agent/go.mod: its toolchain line (else the go line),
# which CI's setup-go also reads, so the hub, CI and the module agree. HUB_GO_VERSION overrides it.
if [ -z "${HUB_GO_VERSION:-}" ]; then
    HUB_GO_VERSION="$(awk '/^toolchain /{t=substr($2,3)} /^go /{g=$2} END{print (t != "" ? t : g)}' "${ROOT_DIR}/apps/agent/go.mod")"
    [ -n "${HUB_GO_VERSION}" ] || { echo "❌ apps/agent/go.mod에서 Go 버전을 읽지 못했습니다 (HUB_GO_VERSION으로 지정하세요)." >&2; exit 1; }
fi

HOST=""
LISTEN=""
PULL=1
HUB_SUDO="keep"
SSH_HARDENING=1
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --listen) LISTEN="$2"; shift ;;
        --with-ui) ;;
        --no-ui) echo "허브는 대시보드를 끌 수 없습니다 (--no-ui 불가)." >&2; exit 2 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        --no-pull) PULL=0 ;;
        --dry-run) DRY_RUN=1 ;;
        --hub-sudo) HUB_SUDO="on" ;;
        --no-hub-sudo) HUB_SUDO="off" ;;
        --no-ssh-hardening) SSH_HARDENING=0 ;;
        -h|--help) sed -n '3,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) HOST="$1" ;;
    esac
    shift
done
[ -n "${HOST}" ] || { echo "호스트를 지정하세요." >&2; exit 2; }

# HUB_SSH_HOSTNAME (or the Tailscale fallback of upload_file) replaces the ssh config's HostName of the hub.
SSH_HOSTNAME="${HUB_SSH_HOSTNAME:-}"
ssh_host() { ssh -o BatchMode=yes -o ConnectTimeout=10 ${SSH_HOSTNAME:+-o "HostName=${SSH_HOSTNAME}"} "${HOST}" "$@"; }

# upload_file <local file> <path below the remote home>: streams to a temporary name unique to this run, checks its
# sha256 there and only then renames it into place. A transfer that breaks off never leaves a half-written file under
# the final name. Temporary files older than 10 minutes are removed.
upload_file() {
    local src="$1" dest="$2" sum
    sum="$(shasum -a 256 "${src}" | cut -d' ' -f1)"
    ssh_host "set -e; dest=\$HOME/${dest}; dir=\$(dirname \"\$dest\"); mkdir -p \"\$dir\"
find \"\$dir\" -maxdepth 1 -name \"\$(basename \"\$dest\").part.*\" -mmin +10 -delete 2>/dev/null || true
tmp=\"\$dest.part.\$\$\"
cat > \"\$tmp\"
if ! echo '${sum}  '\"\$tmp\" | sha256sum -c --quiet >/dev/null 2>&1; then rm -f \"\$tmp\"; echo 'checksum mismatch' >&2; exit 1; fi
chmod 600 \"\$tmp\"; mv -f \"\$tmp\" \"\$dest\"" < "${src}"
}

# upload_file, retried once over the hub's Tailscale IPv4 (ts_ip) when it fails and no HUB_SSH_HOSTNAME is set; the
# rest of the install then keeps using that address.
upload_with_fallback() {
    upload_file "$@" && return 0
    if [ -z "${SSH_HOSTNAME}" ] && [ -n "${ts_ip:-}" ]; then
        echo "⚠️  업로드에 실패해 허브의 Tailscale 주소 ${ts_ip}로 다시 시도합니다." >&2
        SSH_HOSTNAME="${ts_ip}"
        upload_file "$@" && return 0
    fi
    return 1
}

inv_field() {  # inv_field <field>: the hub entry's field from the inventory ("" when absent)
    python3 -c 'import json,sys; print(next((s.get(sys.argv[3],"") for s in json.load(open(sys.argv[1])) if s["id"]==sys.argv[2]), ""))' \
        "${INVENTORY_FILE}" "${HOST}" "$1"
}

# Only the hub gets an agent. The role comes from the hub's own inventory copy.
inventory_load
role="$(python3 -c 'import json,sys; print(next((s.get("guardian_role","node") for s in json.load(open(sys.argv[1])) if s["id"]==sys.argv[2]), ""))' \
    "${INVENTORY_FILE}" "${HOST}")"
if [ -z "${role}" ]; then
    echo "${HOST}은(는) 인벤토리에 없습니다. 허브의 id를 지정하세요 (별칭·IP로는 설치하지 않습니다)." >&2
    exit 2
fi
if [ "${role}" != hub ]; then
    echo "${HOST}의 역할은 '${role}'입니다. agent는 허브(guardian_role=hub)에만 설치합니다 (비허브 노드 설치는 지원하지 않음)." >&2
    echo "노드는 agent 없이 허브가 SSH로 관리합니다: scripts/setup-hub-ssh.sh ${HOST}" >&2
    exit 2
fi
echo "🏷️  ${HOST}: role=hub"
if [ -n "$(git -C "${ROOT_DIR}" status --porcelain --untracked-files=no)" ]; then
    echo "⚠️  로컬에 커밋하지 않은 변경이 있습니다. 릴리스에 그대로 들어가고 버전에 -dirty가 붙습니다." >&2
fi

# Developer track: the hub checkout is the source of the builtin server-guardian project and its workspace clone.
if [ "${PULL}" = 1 ] && [ "${DRY_RUN}" = 0 ]; then
    echo "⬇️  ${HOST}:~/server-guardian 업데이트 (git pull --ff-only, 자기 저장소 프로젝트의 원본)"
    if ssh_host 'set -e; cd ~/server-guardian
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "  변경된 파일이 있어 pull하지 않습니다:" >&2; git status --short --untracked-files=no >&2; exit 1
fi
git pull --ff-only -q'; then
        echo "  허브: $(ssh_host 'git -C ~/server-guardian log --oneline -1')"
    else
        echo "⚠️  허브 저장소를 업데이트하지 못했습니다. 실행되는 코드에는 영향이 없고, 자기 저장소 프로젝트만 예전 커밋을 봅니다." >&2
    fi
fi

arch="$(ssh_host uname -m)"
case "${arch}" in
    aarch64) goarch=arm64 ;;
    x86_64) goarch=amd64 ;;
    *) echo "지원하지 않는 아키텍처: ${arch}" >&2; exit 1 ;;
esac

# The hub's Tailscale IPv4: part of the default listen addresses and the address upload_with_fallback retries over.
ts_ip="$(ssh_host 'tailscale ip -4 2>/dev/null | head -n 1' || true)"
if [ -z "${LISTEN}" ]; then
    LISTEN="127.0.0.1${ts_ip:+,${ts_ip}}"
fi

build_dir="$(mktemp -d -t sg-release.XXXXXX)"
trap 'rm -rf "${build_dir}"' EXIT
tree="${build_dir}/tree"
# Stamped into the binary (AgentVersion, reported by /api/status): "-dirty" marks a build from uncommitted changes.
version="$(git -C "${ROOT_DIR}" describe --always --dirty)"
release="${version}-$(date -u +%Y%m%d%H%M%S)"
release_build_ui "${ROOT_DIR}"
echo "📦 릴리스 ${release}"
release_tree "${ROOT_DIR}" "${goarch}" "${version}" "${tree}" || { echo "❌ 릴리스를 만들지 못했습니다." >&2; exit 1; }
release_archive "${tree}" "${build_dir}/release.tar.gz"

echo "📤 ${HOST}:~/.guardian/releases/${release} 업로드"
upload_with_fallback "${build_dir}/release.tar.gz" ".guardian/incoming/${release}.tar.gz" \
    || { echo "❌ 릴리스를 허브에 올리지 못했습니다. 허브는 바뀌지 않았습니다." >&2; exit 1; }
ssh_host "set -e; G=\$HOME/.guardian; R=\$G/releases/${release}
mkdir -p \$G/releases; rm -rf \$R.part; mkdir -p \$R.part
tar -xzf \$G/incoming/${release}.tar.gz -C \$R.part
rm -rf \$R; mv -T \$R.part \$R; rm -f \$G/incoming/${release}.tar.gz"
upload_with_fallback "${INVENTORY_FILE}" .guardian/incoming/inventory.json \
    || { echo "❌ 인벤토리를 올리지 못했습니다." >&2; exit 1; }

# Names (besides IPs, localhost and the machine hostname) the agent accepts in the Host header;
# anything else gets 421 (DNS-rebinding guard in apps/agent/security.go).
allowed_hosts="$(python3 -c 'import json,sys; s=next((s for s in json.load(open(sys.argv[1])) if s["id"]==sys.argv[2]), {}); print(",".join(dict.fromkeys(h for h in (s.get("tailscale_domain"), s.get("hostname"), s.get("id")) if h)))' \
    "${INVENTORY_FILE}" "${HOST}")"
# The repository of the builtin server-guardian project: this checkout's origin, so the URL lives on the hub only.
# The hub clones it without credentials, so only a plain https URL is usable. A developer checkout often pushes over
# ssh (git@host:owner/repo.git); then the hub's own checkout, which it pulls over https, names the usable URL.
https_repo='^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._~/-]+$'
self_repo="$(git -C "${ROOT_DIR}" remote get-url origin 2>/dev/null || true)"
if [ -n "${self_repo}" ] && ! [[ "${self_repo}" =~ ${https_repo} ]]; then
    hub_repo="$(ssh_host 'git -C ~/server-guardian remote get-url origin 2>/dev/null' || true)"
    if [[ "${hub_repo}" =~ ${https_repo} ]]; then
        self_repo="${hub_repo}"
    else
        echo "⚠️  origin이 https 주소가 아니고 허브 저장소에서도 찾지 못해 server-guardian 프로젝트의 repo를 비웁니다." >&2
        self_repo=""
    fi
fi
args=(--release "${release}" --listen "${LISTEN}" --expect-version "${version}"
      --workspace "${WORKSPACE_REMOTE}" --llm-timeout "${HUB_LLM_TIMEOUT}" --debate-timeout "${HUB_DEBATE_TIMEOUT}"
      --hub-sudo "${HUB_SUDO}" --go-version "${HUB_GO_VERSION}" --backup-replica "$(inv_field backup_replica)"
      --inventory "/home/ubuntu/.guardian/incoming/inventory.json")
[ -z "${allowed_hosts}" ] || args+=(--allowed-hosts "${allowed_hosts}")
[ -z "${self_repo}" ] || args+=(--self-repo "${self_repo}")
[ "${SSH_HARDENING}" = 1 ] || args+=(--no-ssh-hardening)
[ "${DRY_RUN}" = 0 ] || args+=(--dry-run)

# Quoted for the remote shell: every value is a version, path, URL, number or comma-separated list.
remote_cmd="bash \$HOME/.guardian/releases/${release}/deploy/hub/install.sh$(printf ' %q' "${args[@]}")"
status=0
ssh_host "${remote_cmd}" || status=$?
if [ "${DRY_RUN}" = 1 ]; then
    ssh_host "rm -f ~/.guardian/incoming/inventory.json; chmod -R u+w ~/.guardian/releases/${release} && rm -rf ~/.guardian/releases/${release}" || true
    exit "${status}"
fi
case "${status}" in
    0|1) ;;
    3) echo "❌ 새 릴리스가 시작하지 않아 이전 상태로 되돌렸습니다." >&2; exit 3 ;;
    4) echo "⏸️  토론이 실행 중이라 설치하지 않았습니다. 허브에 올린 릴리스는 남아 있고, 끝난 뒤 다시 실행하면 됩니다." >&2; exit 4 ;;
    *) echo "❌ 허브 설치가 실패했습니다 (exit ${status})." >&2; exit "${status}" ;;
esac

if [ "${PULL}" = 1 ]; then
    echo "🧪 판정자 조치용 작업 클론: ${WORKSPACE_REMOTE}"
    ssh_host "[ -d ${WORKSPACE_REMOTE}/.git ] || git clone -q ~/server-guardian ${WORKSPACE_REMOTE}"
    # Bring the clone up to the hub checkout's branch, but never discard what judge actions left
    # there (modified files or commits on their own branches). Failing here does not stop the install.
    ssh_host "WS=${WORKSPACE_REMOTE}"' bash -s' <<'REMOTE' || echo "⚠️  작업 클론을 갱신하지 못했습니다." >&2
set -e
cd "$WS"
base="$(git -C ~/server-guardian rev-parse --abbrev-ref HEAD)"
git fetch -q origin
# Untracked files count too: judge actions create new files, and moving HEAD under them would make the
# thread unpublishable and leave the files behind.
if [ -n "$(git status --porcelain)" ]; then
    echo "  ⚠️  수정되었거나 추적하지 않는 파일이 있어 갱신하지 않습니다 (판정자 조치 결과일 수 있음):" >&2
    git status --short | sed 's/^/     /' >&2
    exit 0
fi
current="$(git rev-parse --abbrev-ref HEAD)"
[ "$current" = "$base" ] || git checkout -q "$base"
git merge -q --ff-only "origin/$base"
echo "  $(git log --oneline -1)"
[ "$current" = "$base" ] || echo "  ${current} 브랜치에서 ${base}로 전환했습니다 (브랜치는 남아 있음)."
others="$(git for-each-ref --format='%(refname:short)' refs/heads | grep -vx "$base" || true)"
[ -z "$others" ] || echo "  판정자 브랜치: $(echo $others)"
REMOTE
    # Other projects' workspaces (configs/projects and projects added from the dashboard):
    # clone the missing ones, fast-forward the clean ones. Never fatal for the install.
    echo "📚 프로젝트 작업 클론 (configs/projects + 대시보드에서 추가한 프로젝트)"
    ssh_host 'set -a; . ~/.guardian/agent.env; set +a; cd ~/.guardian/current && python3 cli/llm-agent/guardian.py --project-sync all 2>&1' \
        || echo "⚠️  프로젝트 작업 클론을 갱신하지 못했습니다." >&2
fi

echo "🔖 배포 버전"
echo "  빌드:            ${version} (릴리스 ${release})"
echo "  허브 저장소 HEAD: $(ssh_host 'git -C ~/server-guardian describe --always --dirty' 2>/dev/null || echo '없음')"
echo "  작업 클론 HEAD:   $(ssh_host "git -C ${WORKSPACE_REMOTE} describe --always --dirty" 2>/dev/null || echo '없음')"
if [ "${status}" = 1 ]; then
    echo "❌ 설치 후 점검에 실패한 항목이 있습니다 (위 참고). 되돌리기: ssh ${HOST} bash ~/.guardian/current/deploy/hub/install.sh --rollback" >&2
    exit 1
fi
echo "✅ 설치 완료 (되돌리기: ssh ${HOST} bash ~/.guardian/current/deploy/hub/install.sh --rollback)"

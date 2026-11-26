#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Install a release on the hub (runs ON the hub, as the login user with sudo)
#
# docs/design/install-tracks.md, R1. A release is a directory ~/.guardian/releases/<name>/ holding bin/agent,
# bin/goindex (optional), ui/, cli/, configs/, deploy/, VERSION and MANIFEST (sha256 of every file). This script
# checks it, writes agent.env, installs the systemd units, switches ~/.guardian/current to it (the old target
# becomes ~/.guardian/previous), restarts the agent and checks it. A start that fails switches back on its own.
# scripts/install-agent-service.sh (developer track) assembles the release on the Mac, uploads it and runs this.
#
# Usage:
#   install.sh --release <name> [--listen <ips>] [--allowed-hosts <names>] [--expect-version <v>]
#              [--workspace <dir>] [--self-repo <url>] [--llm-timeout <s>] [--debate-timeout <s>]
#              [--hub-sudo on|off|keep] [--no-ssh-hardening] [--go-version <v> | --no-go]
#              [--backup-replica <id>] [--inventory <file>] [--dry-run]
#   install.sh --rollback      switch current and previous, restart, check
#   install.sh --status        print current, previous and the kept releases
#
#   --dry-run  check the release and print what would change (units, agent.env keys, links); change nothing
#   --force    install or roll back even while a debate is running (a restart interrupts it)
#
# Exit status: 0 installed; 1 post-install checks failed (the new release runs); 2 bad input or release;
# 3 the agent did not start and the previous release was restored; 4 a debate is running (nothing changed).
# ==============================================================================

set -euo pipefail

G="${HOME}/.guardian"
RELEASES="${G}/releases"
KEEP_RELEASES=3
UNIT_DIR=/etc/systemd/system
DROPIN_DIR="${UNIT_DIR}/server-guardian-agent.service.d"
UNITS=(guardian-fleet-agent.service server-guardian-agent.service server-guardian-db-backup.service
       server-guardian-db-backup.timer server-guardian-ops-watch.service server-guardian-ops-watch.timer
       guardian-mail-relay.service)

NAME=""
LISTEN=""
ALLOWED=""
EXPECT=""
WORKSPACE=""
SELF_REPO=""
LLM_TIMEOUT=600
DEBATE_TIMEOUT=7200
HUB_SUDO=keep
SSH_HARDENING=1
GO_VERSION=""
INSTALL_GO=1
REPLICA=""
INVENTORY=""
DRY_RUN=0
FORCE=0
MODE=install
while [ $# -gt 0 ]; do
    case "$1" in
        --release) NAME="$2"; shift ;;
        --listen) LISTEN="$2"; shift ;;
        --allowed-hosts) ALLOWED="$2"; shift ;;
        --expect-version) EXPECT="$2"; shift ;;
        --workspace) WORKSPACE="$2"; shift ;;
        --self-repo) SELF_REPO="$2"; shift ;;
        --llm-timeout) LLM_TIMEOUT="$2"; shift ;;
        --debate-timeout) DEBATE_TIMEOUT="$2"; shift ;;
        --hub-sudo) HUB_SUDO="$2"; shift ;;
        --no-ssh-hardening) SSH_HARDENING=0 ;;
        --go-version) GO_VERSION="$2"; shift ;;
        --no-go) INSTALL_GO=0 ;;
        --backup-replica) REPLICA="$2"; shift ;;
        --inventory) INVENTORY="$2"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        --rollback) MODE=rollback ;;
        --status) MODE=status ;;
        -h|--help) sed -n '3,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
    esac
    shift
done

FAILURES=()
HARDENING_FAILURES=0
fail() { FAILURES+=("$1"); echo "  ❌ $1" >&2; }
link_target() { [ -L "$1" ] && basename "$(readlink "$1")" || true; }
# Points link $1 at releases/$2 atomically (a relative link, so ~/.guardian can move as a whole).
set_link() { ln -sfn "releases/$2" "$1.new" && mv -T "$1.new" "$1"; }

status() {
    echo "  current:  $(link_target "${G}/current")"
    echo "  previous: $(link_target "${G}/previous")"
    echo "  releases: $(find "${RELEASES}" -mindepth 1 -maxdepth 1 -type d ! -name '*.part' -printf '%f ' 2>/dev/null)"
}

# Starts the agent on the release `current` points at; true when it is active and listens on 9870.
start_agent() {
    sudo -n systemctl daemon-reload
    sudo -n systemctl restart server-guardian-agent.service
    for _ in $(seq 15); do
        sleep 1
        if systemctl is-active --quiet server-guardian-agent.service && ss -ltn | grep -q ":9870 "; then
            return 0
        fi
    done
    return 1
}

# Restores the unit files saved before this run and the previous release, then starts that.
restore_previous() {
    local prev="$1"
    echo "↩️  이전 상태로 되돌립니다 (${prev:-기존 배치})" >&2
    if [ -d "${G}/units.prev" ]; then
        local f
        for f in "${G}"/units.prev/*; do
            [ -e "${f}" ] || continue
            sudo -n cp "${f}" "${UNIT_DIR}/$(basename "${f}")"
        done
    fi
    if [ -n "${prev}" ]; then set_link "${G}/current" "${prev}"; fi
    if start_agent; then echo "  이전 상태로 agent가 다시 시작했습니다" >&2; else echo "  ❌ 이전 상태로도 시작하지 못했습니다" >&2; fi
}

health() {
    local ip out
    set -a; # shellcheck disable=SC1091
    . "${G}/agent.env"; set +a
    for ip in ${AGENT_HOST//,/ }; do
        out="$(curl -s --max-time 5 -o /dev/null -w 'api(no token) %{http_code}' "http://${ip}:9870/api/status"
               curl -s --max-time 5 -o /dev/null -w ' · api(token) %{http_code}' -H "Authorization: Bearer ${AGENT_TOKEN}" "http://${ip}:9870/api/status"
               curl -s --max-time 5 -o /dev/null -w ' · ui %{http_code}' "http://${ip}:9870/")" || true
        echo "  ${ip}:9870 -> ${out}"
        case "${out}" in *"api(token) 200"*) ;; *) fail "${ip}:9870 api(token)이 200이 아닙니다" ;; esac
    done
    local first="${AGENT_HOST%%,*}" running
    running="$(curl -s --max-time 5 -H "Authorization: Bearer ${AGENT_TOKEN}" "http://${first}:9870/api/status" \
        | grep -o '"agent_version":"[^"]*"' | cut -d'"' -f4 || true)"
    echo "  실행 중인 agent: ${running:-확인 실패}"
    if [ -n "${EXPECT}" ] && [ "${running}" != "${EXPECT}" ]; then
        fail "실행 중인 agent 버전(${running:-없음})이 설치한 버전(${EXPECT})과 다릅니다"
    fi
}

if [ "${MODE}" = status ]; then status; exit 0; fi

# A restart kills the agent's children, so a running debate would end as "interrupted". Debate jobs are the
# guardian.py processes started with --result-file (the bracket keeps pgrep from matching its own command line).
refuse_if_debating() {
    [ "${FORCE}" = 0 ] || return 0
    if pgrep -f "guardian[.]py --result-file" >/dev/null; then
        echo "⏸️  토론이 실행 중이라 설치하지 않습니다 (재시작하면 토론이 중단됩니다). 끝난 뒤 다시 실행하거나 --force를 쓰세요." >&2
        pgrep -af "guardian[.]py --result-file" | sed -E 's/^([0-9]+) .*--result-file [^ ]*debate-jobs\/([^/]+)\/.*/   pid \1: \2/' >&2
        exit 4
    fi
}

if [ "${MODE}" = rollback ]; then
    cur="$(link_target "${G}/current")"; prev="$(link_target "${G}/previous")"
    [ -n "${prev}" ] && [ -d "${RELEASES}/${prev}" ] || { echo "되돌릴 이전 릴리스가 없습니다." >&2; exit 2; }
    refuse_if_debating
    echo "↩️  ${cur} -> ${prev}"
    set_link "${G}/current" "${prev}"
    set_link "${G}/previous" "${cur}"
    if ! start_agent; then
        echo "❌ ${prev}로 시작하지 못해 ${cur}로 돌아갑니다" >&2
        set_link "${G}/current" "${cur}"; set_link "${G}/previous" "${prev}"
        start_agent || true
        exit 3
    fi
    health
    status
    [ "${#FAILURES[@]}" -eq 0 ] || exit 1
    exit 0
fi

# ---- install ----
[ -n "${NAME}" ] || { echo "--release <name>이 필요합니다." >&2; exit 2; }
case "${NAME}" in */*|.*|"") echo "잘못된 릴리스 이름: ${NAME}" >&2; exit 2 ;; esac
REL="${RELEASES}/${NAME}"
[ -d "${REL}" ] || { echo "릴리스 디렉터리가 없습니다: ${REL}" >&2; exit 2; }
case "${HUB_SUDO}" in on|off|keep) ;; *) echo "--hub-sudo는 on, off, keep 중 하나입니다." >&2; exit 2 ;; esac

echo "📦 릴리스 ${NAME} ($(cat "${REL}/VERSION" 2>/dev/null || echo '?'))"
if ! (cd "${REL}" && sha256sum -c --quiet MANIFEST); then
    echo "❌ MANIFEST와 맞지 않는 파일이 있어 설치하지 않습니다." >&2
    exit 2
fi
for f in bin/agent ui/index.html cli/llm-agent/guardian.py; do
    [ -e "${REL}/${f}" ] || { echo "❌ 릴리스에 ${f}가 없습니다." >&2; exit 2; }
done
echo "  MANIFEST 확인: $(wc -l < "${REL}/MANIFEST")개 파일"

[ -n "${LISTEN}" ] || LISTEN="127.0.0.1$(ip="$(tailscale ip -4 2>/dev/null | head -n 1)"; [ -z "${ip}" ] || printf ',%s' "${ip}")"
if [ "${HUB_SUDO}" = keep ]; then
    if [ -f "${DROPIN_DIR}/hub-sudo.conf" ]; then HUB_SUDO=on
    elif [ -f "${DROPIN_DIR}/hub-sudo.off" ]; then HUB_SUDO=off
    else HUB_SUDO=on; fi
fi

# The keys the installer owns; every other KEY=value line of agent.env is kept as the operator set it.
OWNED="^(AGENT_HOST|AGENT_ALLOWED_HOSTS|AGENT_UI_DIR|AGENT_TOKEN|GUARDIAN_WORKSPACE|GUARDIAN_LLM_TIMEOUT|GUARDIAN_DEBATE_TIMEOUT|GUARDIAN_SERVERS_FILE|GUARDIAN_HUB_SUDO|GUARDIAN_SELF_REPO)="
owned_lines() {
    printf 'AGENT_HOST=%s\n' "${LISTEN}"
    [ -z "${ALLOWED}" ] || printf 'AGENT_ALLOWED_HOSTS=%s\n' "${ALLOWED}"
    printf 'AGENT_UI_DIR=%s\n' "${G}/current/ui"
    [ -z "${WORKSPACE}" ] || printf 'GUARDIAN_WORKSPACE=%s\n' "${WORKSPACE}"
    printf 'GUARDIAN_LLM_TIMEOUT=%s\n' "${LLM_TIMEOUT}"
    printf 'GUARDIAN_DEBATE_TIMEOUT=%s\n' "${DEBATE_TIMEOUT}"
    printf 'GUARDIAN_SERVERS_FILE=%s\n' "${G}/data/servers.json"
    printf 'GUARDIAN_HUB_SUDO=%s\n' "${HUB_SUDO}"
    [ -z "${SELF_REPO}" ] || printf 'GUARDIAN_SELF_REPO=%s\n' "${SELF_REPO}"
}

if [ "${DRY_RUN}" = 1 ]; then
    echo "🧪 dry run: 아무것도 바꾸지 않습니다"
    echo "  current: $(link_target "${G}/current") -> ${NAME}  (previous는 지금의 current가 됩니다)"
    [ -L "${G}/current" ] || echo "  기존 배치(~/.guardian/agent, ~/server-guardian)에서 처음 옮겨 갑니다"
    echo "  agent.env가 쓸 키 (AGENT_TOKEN은 그대로 유지):"
    owned_lines | sed 's/^/    /'
    kept="$(grep -E "^[A-Za-z_][A-Za-z0-9_]*=" "${G}/agent.env" 2>/dev/null | grep -vE "${OWNED}" | cut -d= -f1 | tr '\n' ' ' || true)"
    echo "  agent.env에서 유지할 키: ${kept:-없음}"
    echo "  허브 sudo: ${HUB_SUDO}"
    for u in "${UNITS[@]}"; do
        if [ -f "${UNIT_DIR}/${u}" ] && cmp -s "${REL}/deploy/systemd/${u}" "${UNIT_DIR}/${u}"; then
            echo "  유닛 ${u}: 같음"
        else
            echo "  유닛 ${u}: 바뀜"
            diff -u "${UNIT_DIR}/${u}" "${REL}/deploy/systemd/${u}" 2>/dev/null | sed -n '3,200p' | grep '^[-+]' | sed 's/^/      /' || true
        fi
    done
    exit 0
fi

refuse_if_debating
prev_target="$(link_target "${G}/current")"

echo "📝 agent.env"
umask 077
mkdir -p "${G}/data" && chmod 700 "${G}/data"
token="$(grep "^AGENT_TOKEN=" "${G}/agent.env" 2>/dev/null | head -n 1 | cut -d= -f2- || true)"
[ -n "${token}" ] || token="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
kept="$(grep -E "^[A-Za-z_][A-Za-z0-9_]*=" "${G}/agent.env" 2>/dev/null | grep -vE "${OWNED}" || true)"
{
    owned_lines
    cat "${REL}/deploy/hub/agent.env.examples"
    [ -z "${kept}" ] || printf '%s\n' "${kept}"
    printf 'AGENT_TOKEN=%s\n' "${token}"
} > "${G}/agent.env.new"
mv "${G}/agent.env.new" "${G}/agent.env"
[ -z "${kept}" ] || echo "  유지한 항목: $(printf '%s\n' "${kept}" | cut -d= -f1 | tr '\n' ' ')"
umask 022

echo "🗂️  허브 인벤토리: ${G}/data/servers.json"
if [ -e "${G}/data/servers.json" ]; then
    echo "  이미 있음 (덮어쓰지 않음)"
elif [ -n "${INVENTORY}" ] && [ -f "${INVENTORY}" ]; then
    (umask 077; cp "${INVENTORY}" "${G}/data/servers.json.new" && mv "${G}/data/servers.json.new" "${G}/data/servers.json")
    echo "  받은 인벤토리로 초기화"
else
    echo "  ⚠️  인벤토리가 없습니다. 대시보드의 노드 등록으로 만드세요." >&2
fi
[ -z "${INVENTORY}" ] || rm -f "${INVENTORY}"

arch="$(uname -m)"; case "${arch}" in aarch64) goarch=arm64 ;; x86_64) goarch=amd64 ;; *) goarch="" ;; esac
if [ "${INSTALL_GO}" = 1 ] && [ -n "${GO_VERSION}" ] && [ -n "${goarch}" ]; then
    echo "🐹 Go ${GO_VERSION} (판정자 패치 검증용): ~/.local/go"
    GO_VERSION="${GO_VERSION}" GO_ARCH="${goarch}" python3 "${REL}/deploy/hub/install_go.py" \
        || { echo "❌ Go 설치에 실패해 설치를 중단합니다 (아직 아무 서비스도 바꾸지 않았습니다)." >&2; exit 2; }
fi

echo "🔐 fleet SSH 키 보호 (/etc/guardian/fleet_key + guardian-fleet-agent)"
# Unit files are saved first, so a release that does not start can be undone together with them.
rm -rf "${G}/units.prev"; mkdir -p "${G}/units.prev"
for u in "${UNITS[@]}"; do [ ! -f "${UNIT_DIR}/${u}" ] || cp -p "${UNIT_DIR}/${u}" "${G}/units.prev/${u}"; done
sudo -n cp "${REL}/deploy/systemd/guardian-fleet-agent.service" "${UNIT_DIR}/guardian-fleet-agent.service"
bash "${REL}/deploy/hub/protect-fleet-key.sh" >/dev/null

echo "⚙️  systemd 유닛과 허브 sudo (${HUB_SUDO})"
for u in "${UNITS[@]}"; do sudo -n cp "${REL}/deploy/systemd/${u}" "${UNIT_DIR}/${u}"; done
sudo -n mkdir -p "${DROPIN_DIR}"
if [ "${HUB_SUDO}" = on ]; then
    sudo -n rm -f "${DROPIN_DIR}/hub-sudo.off"
    sudo -n cp "${REL}/deploy/systemd/server-guardian-agent.service.d/hub-sudo.conf" "${DROPIN_DIR}/hub-sudo.conf"
else
    sudo -n rm -f "${DROPIN_DIR}/hub-sudo.conf"
    echo '# hub sudo off (install.sh --hub-sudo off)' | sudo -n tee "${DROPIN_DIR}/hub-sudo.off" >/dev/null
fi

echo "🔁 current -> ${NAME} (previous: ${prev_target:-없음})"
# Read-only release tree: nothing at run time writes there (data goes to ~/.guardian/data, Python skips its cache).
chmod -R a-w "${REL}"
if [ -n "${prev_target}" ] && [ "${prev_target}" != "${NAME}" ]; then set_link "${G}/previous" "${prev_target}"; fi
set_link "${G}/current" "${NAME}"
mkdir -p "${HOME}/.local/libexec/guardian"
if [ -e "${REL}/bin/goindex" ]; then ln -sfn "${G}/current/bin/goindex" "${HOME}/.local/libexec/guardian/goindex"; fi
sudo -n systemctl enable guardian-fleet-agent.service server-guardian-agent.service >/dev/null 2>&1 || true
if ! start_agent; then
    echo "❌ agent가 시작되지 않았습니다." >&2
    sudo -n journalctl -u server-guardian-agent.service -n 20 --no-pager >&2 || true
    restore_previous "${prev_target}"
    exit 3
fi
echo "  agent 실행 중"

echo "🩺 상태 확인"
health

if [ "${SSH_HARDENING}" = 1 ]; then
    echo "🛡️  sshd 하드닝 (/etc/ssh/sshd_config.d/00-guardian-hardening.conf)"
    hardening="$(SG_HARDENING_SRC="${REL}/deploy/ssh/00-guardian-hardening.conf" bash "${REL}/deploy/ssh/apply-ssh-hardening.sh" 2>&1 | tail -n 1 || true)"
    case "${hardening}" in
        UNCHANGED|INSTALLED|UPDATED) echo "  ${hardening}" ;;
        SKIPPED*|REVERTED*) echo "  ⚠️  ${hardening} (sshd 설정은 바뀌지 않았습니다)" >&2 ;;
        *) HARDENING_FAILURES=$((HARDENING_FAILURES + 1)); fail "sshd 하드닝: ${hardening:-결과 없음}" ;;
    esac
fi

echo "🗄️  DB 백업과 ops 야간 점검 타이머"
# The backup unit cannot read agent.env (InaccessiblePaths): the data dir and DB path the agent uses are copied.
(umask 077
 { printf 'GUARDIAN_BACKUP_REPLICA=%s\n' "${REPLICA}"; grep -E "^GUARDIAN_(DATA_DIR|DB_PATH)=" "${G}/agent.env" 2>/dev/null || true; } > "${G}/backup.env.new"
 mv "${G}/backup.env.new" "${G}/backup.env")
echo "  백업 사본 보관 노드: ${REPLICA:-없음}"
sudo -n systemctl enable --now server-guardian-db-backup.timer server-guardian-ops-watch.timer >/dev/null 2>&1 \
    || fail "백업·야간 점검 타이머를 켜지 못했습니다"
systemctl list-timers server-guardian-db-backup.timer server-guardian-ops-watch.timer --no-pager | sed -n '2,3p'

echo "📬 상주 에이전트 메일 relay (docs/design/resident-agents.md)"
# Restarted on every install so it runs the new release; with no agent declared it stays idle (no stream, no store).
if sudo -n systemctl enable guardian-mail-relay.service >/dev/null 2>&1 \
    && sudo -n systemctl restart guardian-mail-relay.service; then
    echo "  relay: $(systemctl is-active guardian-mail-relay.service 2>/dev/null || true)"
else
    fail "메일 relay를 시작하지 못했습니다"
fi

echo "🧱 agent 격리 확인 (허브 sudo: ${HUB_SUDO})"
iso="$(HUB_SUDO="${HUB_SUDO}" bash "${REL}/deploy/hub/isolation-check.sh" 2>&1 || true)"
printf '%s\n' "${iso}"
case "${iso}" in *❌*) fail "agent 격리 확인에 실패한 항목이 있습니다 (위 ❌ 참고)" ;; esac

# Keep current, previous and the newest other release (KEEP_RELEASES in all); older ones go.
others=0
for old in $(find "${RELEASES}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' | sort -rn | awk '{print $2}'); do
    case "${old}" in "$(link_target "${G}/current")"|"$(link_target "${G}/previous")") continue ;; esac
    others=$((others + 1))
    if [ "${others}" -gt $((KEEP_RELEASES - 2)) ]; then chmod -R u+w "${RELEASES:?}/${old}" && rm -rf "${RELEASES:?}/${old}"; fi
done
status

if [ "${#FAILURES[@]}" -gt 0 ]; then
    echo "" >&2
    echo "❌ 설치 후 점검 ${#FAILURES[@]}건 실패:" >&2
    printf '   - %s\n' "${FAILURES[@]}" >&2
    if [ "$(( ${#FAILURES[@]} - HARDENING_FAILURES ))" -gt 0 ] && [ -n "$(link_target "${G}/previous")" ]; then
        echo "   이전 릴리스로 되돌리려면: bash ${G}/current/deploy/hub/install.sh --rollback" >&2
    fi
    exit 1
fi
echo "✅ 설치 완료: ${NAME}"

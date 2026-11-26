#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - remote part of scripts/install-sandbox.sh (docs/design/resident-open-tools.md, O1-O3)
#
# Runs AS ROOT on the node (the admin script feeds it to `sudo -n bash FILE ARGS`, with the shipped
# files unpacked next to it). Installs the open-agent sandbox: the guardian-sandbox-run wrapper in
# /usr/local/sbin, /etc/guardian-sandbox/limits.conf, the sudoers rule, the launcher
# /usr/local/lib/guardian-sandbox/launch.py, the pinned CLI copies under
# /usr/local/lib/guardian-sandbox/cli (every --cli), /var/lib/guardian-sandbox, the gsa-<name>
# system users (always probe and probe2, plus every --agent and every sandbox agent already on
# the node: a reinstall keeps them) with their work directories, and the guardian-worker.service
# drop-in in the node user's user-unit tree. --remove undoes exactly that and never touches any
# other user or file.
#
# Arguments: --node-user USER --files DIR --dropin FILE [--agent NAME]... [--cli PROVIDER=VERSION]...
#            [--remove] [--no-restart]
#   --node-user USER  the node's admin user: the sudoers rule names it and it joins every gsa group
#   --files DIR       the unpacked deploy/sandbox directory (wrapper, limits.conf, sudoers template,
#                     launcher)
#   --dropin FILE     the unpacked deploy/worker/sandbox-sudo.conf (NoNewPrivileges=no for the
#                     worker unit; without it the daemon's sudo to the wrapper fails); installed
#                     as <node-user home>/.config/systemd/user/guardian-worker.service.d/
#   --agent NAME      extra agent users to create (repeatable; same name rules as the wrapper); a
#                     reinstall keeps the agents already on the node listed in installed.json,
#                     --agent only adds to them
#   --cli P=V         copy the node user's CLI binary (claude or agy) pinned to version V into the
#                     root-owned cli store; nothing else of the node user's home is ever read. A
#                     missing binary or a version mismatch only warns: the provider is left out of
#                     installed.json (the launcher then fails with launch: cli_missing)
#   --remove          with --agent: remove only those agents and drop them from installed.json
#                     (the drop-in stays); without: remove the whole sandbox including the drop-in
#   --no-restart      never restart the node user's systemd user manager; print the exact command
#                     the operator must run instead (a restart is needed when the manager misses a
#                     gsa group or must pick the drop-in up, because every user service of that
#                     account restarts with it)
#
# Prints progress on stderr and `sandbox_version=<12 hex>` on stdout when installing. The version
# is the sha256 of the wrapper, limits.conf, the sudoers template and the launcher, concatenated
# in that order (the hub computes the same value from its release tree; the drop-in is not hashed).
# Exit: 0 done, 1 a precondition or step failed, 2 misuse.
# ==============================================================================

set -euo pipefail

SBIN_DIR="/usr/local/sbin"
ETC_DIR="/etc/guardian-sandbox"
ETC_DIR_SUDOERS="/etc/sudoers.d"
LIB_DIR="/usr/local/lib/guardian-sandbox"
CLI_DIR="${LIB_DIR}/cli"
VAR_DIR="/var/lib/guardian-sandbox"
AGENT_NAME_RE='^[a-z][a-z0-9]{0,15}$'
NODE_USER_RE='^[a-z_][a-z0-9_-]*$'
VERSION_RE='^[0-9][0-9A-Za-z.+-]{0,31}$'

say() { printf '%s\n' "$*" >&2; }
die() { say "❌ $*"; exit 1; }

NODE_USER=""
FILES=""
DROPIN=""
AGENTS=()
CLI_ARGS=()
REMOVE=0
NO_RESTART=0
DROPIN_CHANGED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --node-user) shift; NODE_USER="${1:-}" ;;
        --files) shift; FILES="${1:-}" ;;
        --dropin) shift; DROPIN="${1:-}" ;;
        --agent) shift; AGENTS+=("${1:-}") ;;
        --cli)
            shift
            [ -n "${1:-}" ] || die "--cli는 PROVIDER=VERSION 형태여야 합니다"
            case "${1%%=*}" in
                claude|agy|codex) ;;
                *) die "--cli의 provider는 claude, agy 또는 codex여야 합니다: ${1}" ;;
            esac
            [ "${1#*=}" != "$1" ] || die "--cli는 PROVIDER=VERSION 형태여야 합니다: ${1}"
            [[ "${1#*=}" =~ ${VERSION_RE} ]] || die "--cli 버전이 올바르지 않습니다: ${1#*=}"
            for seen in ${CLI_ARGS[@]+"${CLI_ARGS[@]}"}; do
                [ "${seen%%=*}" != "${1%%=*}" ] || die "--cli의 provider는 한 번만 지정할 수 있습니다: ${1%%=*}"
            done
            CLI_ARGS+=("$1")
            ;;
        --remove) REMOVE=1 ;;
        --no-restart) NO_RESTART=1 ;;
        *) say "알 수 없는 인자: $1"; exit 2 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "root로 실행해야 합니다 (sudo -n)"
[ -n "${NODE_USER}" ] || die "--node-user가 필요합니다"
[ -n "${FILES}" ] || die "--files가 필요합니다"
[[ "${NODE_USER}" =~ ${NODE_USER_RE} ]] || die "--node-user가 올바르지 않습니다: ${NODE_USER}"
[ -d "${FILES}" ] || die "--files 디렉터리가 없습니다: ${FILES}"
for f in guardian-sandbox-run.sh limits.conf guardian-sandbox.sudoers guardian-sandbox-launch.py; do
    [ -f "${FILES}/${f}" ] || die "${FILES}/${f}가 없습니다"
done
if [ "${REMOVE}" != 1 ]; then
    # The drop-in is part of every install (the daemon's sudo needs it); --remove needs no source.
    [ -n "${DROPIN}" ] || die "설치에는 --dropin이 필요합니다 (deploy/worker/sandbox-sudo.conf)"
    [ -f "${DROPIN}" ] || die "--dropin 파일이 없습니다: ${DROPIN}"
fi
sysd_ver="$(systemctl --version 2>/dev/null | sed -n '1s/^systemd \([0-9][0-9]*\).*/\1/p' || true)"
[ -n "${sysd_ver}" ] || die "systemctl을 찾을 수 없습니다 (systemd가 필요합니다)"
[ "${sysd_ver}" -ge 254 ] || die "systemd 254 이상이 필요합니다 (현재 ${sysd_ver})"
command -v visudo >/dev/null 2>&1 || die "visudo가 없습니다"
command -v useradd >/dev/null 2>&1 || die "useradd가 없습니다"

# The version of everything the sandbox consists of (C1): the four shipped files, concatenated
# in this exact order; the hub computes the same value from its release tree.
sandbox_version() {
    cat "${FILES}/guardian-sandbox-run.sh" "${FILES}/limits.conf" "${FILES}/guardian-sandbox.sudoers" \
        "${FILES}/guardian-sandbox-launch.py" | sha256sum | cut -c1-12
}

# install to <dir>/.<base>.new (a dot name, so nothing halfway installed is ever live), then rename.
install_mv() {  # install_mv MODE SRC DST
    local mode="$1" src="$2" dst="$3" tmp
    tmp="$(dirname "${dst}")/.$(basename "${dst}").new"
    install -o root -g root -m "${mode}" "${src}" "${tmp}"
    mv -f "${tmp}" "${dst}"
}

install_sudoers() {
    local tmp="${ETC_DIR_SUDOERS}/.guardian-sandbox.new" render
    render="$(mktemp /tmp/guardian-sandbox-sudoers.XXXXXX)"
    # Render the template, check it standalone, stage it as a dot name (sudo ignores names with a
    # dot), rename to the live name, then check the whole sudoers tree before calling it done.
    if ! sed -e "s|@NODE_USER@|${NODE_USER}|g" "${FILES}/guardian-sandbox.sudoers" >"${render}"; then
        rm -f "${render}"; die "sudoers 템플릿 렌더에 실패했습니다"
    fi
    if ! visudo -cf "${render}" >/dev/null 2>&1; then
        rm -f "${render}"; die "렌더한 sudoers가 문법 검사를 통과하지 못했습니다"
    fi
    install -o root -g root -m 0440 "${render}" "${tmp}"
    rm -f "${render}"
    if ! mv -f "${tmp}" "${ETC_DIR_SUDOERS}/guardian-sandbox"; then
        rm -f "${tmp}"; die "sudoers 설치에 실패했습니다"
    fi
    if ! visudo -c >/dev/null 2>&1; then
        rm -f "${ETC_DIR_SUDOERS}/guardian-sandbox"
        die "visudo -c 실패: sudoers 규칙을 제거했습니다. 노드의 sudo 설정을 확인하세요"
    fi
    say "✅ sudoers 규칙 설치 (${NODE_USER} → guardian-sandbox-run)"
}

# O3: guardian-worker.service has NoNewPrivileges=yes (deploy/worker/guardian-worker.service), which
# makes the daemon's setuid `sudo -n guardian-sandbox-run` fail (sandbox_stop_failed), so the worker
# unit gets a drop-in that lifts exactly that. Root writes into the node user's own user-unit tree:
# every component below the home is checked with test -L first and never followed, directories are
# only ever created (an existing one keeps its mode), and the file lands through a dot temp name +
# mv, owned by the node user.
install_dropin() {  # install_dropin SRC; sets DROPIN_CHANGED=1 when newly written or rewritten
    local src="$1" home group dir dst tmp comp cur
    home="$(getent passwd "${NODE_USER}" | cut -d: -f6 || true)"
    [ -n "${home}" ] || die "${NODE_USER}의 홈 디렉터리를 찾을 수 없습니다"
    group="$(id -gn "${NODE_USER}" 2>/dev/null || true)"
    [ -n "${group}" ] || die "${NODE_USER}의 기본 그룹을 찾을 수 없습니다"
    dir="${home}/.config/systemd/user/guardian-worker.service.d"
    dst="${dir}/sandbox-sudo.conf"
    cur="${home}"
    for comp in .config systemd user guardian-worker.service.d; do
        cur="${cur}/${comp}"
        if [ -L "${cur}" ]; then
            die "${cur}이 심볼릭 링크입니다 (user 단위 트리에서는 링크를 허용하지 않습니다)"
        fi
        if [ -e "${cur}" ]; then
            [ -d "${cur}" ] || die "${cur}이 디렉터리가 아닙니다"
        else
            install -d -o "${NODE_USER}" -g "${group}" -m 0755 "${cur}"
        fi
    done
    DROPIN_CHANGED=1
    if [ -f "${dst}" ] && [ ! -L "${dst}" ] && cmp -s "${src}" "${dst}"; then
        DROPIN_CHANGED=0
        say "✅ worker 드롭인 변경 없음 (${dst})"
        return 0
    fi
    tmp="${dir}/.sandbox-sudo.conf.new"
    rm -f "${tmp}"  # the node user owns this directory: never write through a planted name
    install -o "${NODE_USER}" -g "${group}" -m 0644 "${src}" "${tmp}"
    mv -f "${tmp}" "${dst}"
    say "✅ worker 드롭인 설치 (${dst})"
}

# O3: a running systemd user manager keeps the supplementary groups it started with, so the worker
# (and its sudo) misses every gsa-<name> group the installer just added, and it only picks the
# drop-in up after re-reading the unit. When either is needed, restart user@<uid>.service; that
# restarts EVERY user service of the account (enabled ones come back; a running resident turn is
# lost and redone by the driver). --no-restart skips it and prints the command instead. An inactive
# user manager needs nothing: its next start picks everything up.
sync_user_manager() {  # sync_user_manager
    local uid pid have gid name gids="" missing=0
    uid="$(id -u "${NODE_USER}")"
    if ! systemctl is-active --quiet "user@${uid}.service" 2>/dev/null; then
        return 0
    fi
    for name in $(id -nG "${NODE_USER}" 2>/dev/null); do
        case "${name}" in
            gsa-*) gids="${gids}$(getent group "${name}" | cut -d: -f3 || true) " ;;
        esac
    done
    pid="$(systemctl show -p MainPID --value "user@${uid}.service" 2>/dev/null | tr -d '[:space:]' || true)"
    have="$(sed -n 's/^Groups:[[:space:]]*//p' "/proc/${pid}/status" 2>/dev/null || true)"
    for gid in ${gids}; do
        case " ${have} " in
            *" ${gid} "*) ;;
            *) missing=1; break ;;
        esac
    done
    [ "${missing}" = 1 ] || [ "${DROPIN_CHANGED}" = 1 ] || return 0
    if [ "${NO_RESTART}" = 1 ]; then
        say "⚠️  ${NODE_USER}의 사용자 매니저를 재시작해야 하지만 --no-restart로 건너뜁니다. 직접 실행하세요:"
        say "    systemctl restart user@${uid}.service"
        say "    (이 계정의 모든 사용자 서비스가 재시작됩니다; 진행 중인 resident 턴은 드라이버가 다시 수행합니다)"
        if [ "${DROPIN_CHANGED}" = 1 ]; then
            systemctl --user -M "${NODE_USER}@" daemon-reload \
                || say "⚠️  daemon-reload 실패 (위 재시작이 실행되면 다시 읽힙니다)"
        fi
        return 0
    fi
    say "⚠️  user@${uid}.service를 재시작합니다: ${NODE_USER}의 모든 사용자 서비스가 함께 재시작되고"
    say "    진행 중이던 resident 턴은 끊기지만 드라이버가 다시 수행합니다 (enable된 서비스는 다시 뜹니다)"
    systemctl restart "user@${uid}.service" || die "user@${uid}.service 재시작 실패"
    say "✅ 사용자 매니저 재시작 완료 (gsa 그룹/드롭인 반영)"
}

ensure_agent() {  # ensure_agent NAME
    local name="$1" user="gsa-$1" line gecos gids work
    [[ "${name}" =~ ${AGENT_NAME_RE} ]] || die "에이전트 이름이 올바르지 않습니다: ${name}"
    line="$(getent passwd "${user}" || true)"
    if [ -z "${line}" ]; then
        useradd --system --user-group --create-home --home-dir "/home/${user}" \
            --shell /usr/sbin/nologin --comment "guardian sandbox agent ${name}" "${user}" \
            || die "useradd ${user} 실패"
    else
        gecos="$(printf '%s\n' "${line}" | cut -d: -f5)"
        read -r -a gids <<<"$(id -G "${user}" || true)"
        case "${gecos}" in
            "guardian sandbox agent"*) ;;
            *) die "${user}가 이미 있지만 sandbox 에이전트가 아닙니다 (comment: ${gecos})" ;;
        esac
        [ "${#gids[@]}" -eq 1 ] || die "${user}는 그룹이 정확히 하나여야 합니다"
    fi
    passwd -l "${user}" >/dev/null 2>&1 || usermod -L "${user}"
    chmod 0700 "/home/${user}"
    # O3: every turn starts with --chdir /home/gsa-<name>/work and systemd resolves that path
    # BEFORE the launcher runs, so a missing directory fails the whole transient unit
    # (status 200/CHDIR); the launcher's own ensure_dir comes too late. An existing directory is
    # kept (its contents are the agent's), but a symlink or a non-directory is refused.
    work="/home/${user}/work"
    if [ -L "${work}" ]; then
        die "${work}이 심볼릭 링크입니다"
    fi
    if [ -e "${work}" ] && [ ! -d "${work}" ]; then
        die "${work}이 디렉터리가 아닙니다"
    fi
    install -d -o "${user}" -g "${user}" -m 0700 "${work}"
    chown "${user}:${user}" "${work}"
    chmod 0700 "${work}"
    # PD4: persistent deliverable directory ($HOME/out) and user-space tool bin ($HOME/.local/bin).
    local extra_dir
    for extra_dir in "/home/${user}/out" "/home/${user}/.local" "/home/${user}/.local/bin"; do
        if [ -L "${extra_dir}" ]; then
            die "${extra_dir}이 심볼릭 링크입니다"
        fi
        if [ -e "${extra_dir}" ] && [ ! -d "${extra_dir}" ]; then
            die "${extra_dir}이 디렉터리가 아닙니다"
        fi
        install -d -o "${user}" -g "${user}" -m 0700 "${extra_dir}"
        chown "${user}:${user}" "${extra_dir}"
        chmod 0700 "${extra_dir}"
    done
    # O2 (C7): the daemon (the node user) owns the sock dir, the agent group may only enter and
    # connect: 2750 keeps the agent from creating, renaming or swapping anything in it. install -d
    # leaves an existing O1 directory (gsa-NAME 2770) as it was, so chown and chmod follow.
    install -d -o "${NODE_USER}" -g "${user}" -m 2750 "${VAR_DIR}/sock/${name}"
    chown "${NODE_USER}:${user}" "${VAR_DIR}/sock/${name}"
    chmod 2750 "${VAR_DIR}/sock/${name}"
    usermod -aG "${user}" "${NODE_USER}"
    say "✅ 에이전트 사용자 ${user}"
}

write_installed_json() {  # write_installed_json AGENT... -- PROVIDER=VERSION...
    local version now tmp name item json_agents="" json_cli="" agents=() cli=() in_cli=0
    for item in "$@"; do
        if [ "${in_cli}" = 1 ]; then
            cli+=("${item}")
        elif [ "${item}" = "--" ]; then
            in_cli=1
        else
            agents+=("${item}")
        fi
    done
    version="$(sandbox_version)"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    while IFS= read -r name; do
        [ -n "${name}" ] || continue
        json_agents="${json_agents}\"${name}\","
    done <<<"$(printf '%s\n' ${agents[@]+"${agents[@]}"} | sort -u)"
    for item in ${cli[@]+"${cli[@]}"}; do
        json_cli="${json_cli}\"${item%%=*}\": \"${item#*=}\","
    done
    tmp="$(mktemp /tmp/guardian-sandbox-json.XXXXXX)"
    printf '{"version": "%s", "installed": "%s", "agents": [%s], "cli": {%s}}\n' \
        "${version}" "${now}" "${json_agents%,}" "${json_cli%,}" >"${tmp}"
    install_mv 0644 "${tmp}" "${ETC_DIR}/installed.json"
    rm -f "${tmp}"
    printf 'sandbox_version=%s\n' "${version}"
}

# One --cli pair (C2): copy the node user's CLI binary, pinned by its own --version output, into
# the root-owned cli store and drop the other copies of that provider. Nothing else from the node
# user's home is ever read (no config, no credential). A missing source or a version mismatch only
# warns and returns 1: the provider is then left out of installed.json.
install_cli() {  # install_cli PROVIDER VERSION -> 0 copied, 1 skipped (warning on stderr)
    local provider="$1" version="$2" src out node_home dir
    command -v runuser >/dev/null 2>&1 || {
        say "⚠️  runuser가 없어 ${provider} CLI 복사를 건너뜁니다"
        return 1
    }
    node_home="$(getent passwd "${NODE_USER}" | cut -d: -f6)"
    if [ -z "${node_home}" ]; then
        say "⚠️  ${NODE_USER}의 홈 디렉터리를 찾을 수 없어 ${provider} CLI 복사를 건너뜁니다"
        return 1
    fi
    src="$(readlink -f "${node_home}/.local/bin/${provider}" 2>/dev/null || true)"
    if [ -z "${src}" ] || [ ! -f "${src}" ]; then
        say "⚠️  ${NODE_USER}의 .local/bin/${provider}가 없거나 일반 파일이 아니어서 복사를 건너뜁니다"
        return 1
    fi
    # PD1: npm-installed @openai/codex puts a thin JS launcher at bin/codex.js that execs a
    # standalone native Rust binary under vendor/*/bin/codex; copy that native binary directly.
    if [ "${provider}" = "codex" ] && [ "${src%.js}" != "${src}" ]; then
        local pkg_root native
        pkg_root="$(dirname "$(dirname "${src}")")"
        native="$(find "${pkg_root}" -path "*/vendor/*/bin/codex" -type f 2>/dev/null | head -n 1 || true)"
        if [ -z "${native}" ] || [ ! -f "${native}" ]; then
            say "⚠️  codex JS 래퍼 아래에서 네이티브 바이너리를 찾지 못해 복사를 건너뜁니다: ${pkg_root}"
            return 1
        fi
        src="${native}"
    fi
    out="$(timeout 120 runuser -u "${NODE_USER}" -- env -i HOME="${node_home}" \
        PATH=/usr/local/bin:/usr/bin:/bin "${src}" --version 2>/dev/null || true)"
    case "${out}" in
        *"${version}"*) ;;
        *)
            say "⚠️  ${provider} --version 출력에 ${version}이 없어 복사를 건너뜁니다: $(printf '%s' "${out}" | head -n 1)"
            return 1
            ;;
    esac
    install -d -o root -g root -m 0755 "${CLI_DIR}/${provider}-${version}"
    install_mv 0755 "${src}" "${CLI_DIR}/${provider}-${version}/${provider}"
    if [ "${provider}" = "codex" ]; then
        local src_dir helper hname
        src_dir="$(dirname "${src}")"
        shopt -s nullglob
        for helper in "${src_dir}"/codex-*; do
            if [ -f "${helper}" ] && [ -x "${helper}" ]; then
                hname="$(basename "${helper}")"
                install_mv 0755 "${helper}" "${CLI_DIR}/${provider}-${version}/${hname}"
            fi
        done
        shopt -u nullglob
    fi
    shopt -s nullglob
    for dir in "${CLI_DIR}/${provider}-"*/; do
        if [ "${dir%/}" != "${CLI_DIR}/${provider}-${version}" ]; then
            rm -rf "${dir}"
        fi
    done
    shopt -u nullglob
    say "✅ CLI 복사: ${provider} ${version}"
    return 0
}

remove_agent() {  # remove_agent USER (a gsa- user our comment marks; anything else is skipped)
    local user="$1" line gecos name
    line="$(getent passwd "${user}" || true)"
    [ -n "${line}" ] || return 0
    gecos="$(printf '%s\n' "${line}" | cut -d: -f5)"
    case "${gecos}" in
        "guardian sandbox agent"*) ;;
        *) say "⚠️  ${user}는 sandbox 에이전트가 아니므로 건너뜁니다"; return 0 ;;
    esac
    pkill -KILL -u "${user}" >/dev/null 2>&1 || true
    gpasswd -d "${NODE_USER}" "${user}" >/dev/null 2>&1 || true
    userdel -r "${user}" >/dev/null 2>&1 || true  # a dying process can make this fail; the next install reuses the user
    name="${user#gsa-}"
    rm -rf "${VAR_DIR}/sock/${name}"
    say "✅ ${user} 제거"
}

# Every sandbox agent already on the node, as agent names (the gsa- prefix stripped): the same
# gsa-/GECOS selection the whole-remove branch removes, kept to the names the wrapper accepts
# (every user this installer created passed AGENT_NAME_RE at creation). A reinstall must not drop
# these agents: their users survive, and the worker refuses a turn for any name that installed.json
# no longer lists, so do_install re-ensures and re-lists them and --remove --agent rebuilds the
# list from what is left.
existing_agents() {  # existing_agents -> agent names, one per line, sorted unique
    local user name
    while IFS= read -r user; do
        [ -n "${user}" ] || continue
        name="${user#gsa-}"
        [[ "${name}" =~ ${AGENT_NAME_RE} ]] || continue
        printf '%s\n' "${name}"
    done < <(getent passwd | awk -F: '$1 ~ /^gsa-/ && $5 ~ /^guardian sandbox agent/ {print $1}' | sort -u)
}

do_install() {
    local name item existing kept="" copied=() agents=()
    say "🔧 래퍼와 설정 설치"
    install_mv 0755 "${FILES}/guardian-sandbox-run.sh" "${SBIN_DIR}/guardian-sandbox-run"
    install -d -o root -g root -m 0755 "${ETC_DIR}"
    install_mv 0644 "${FILES}/limits.conf" "${ETC_DIR}/limits.conf"
    install_sudoers
    install -d -o root -g root -m 0755 "${LIB_DIR}"
    install_mv 0755 "${FILES}/guardian-sandbox-launch.py" "${LIB_DIR}/launch.py"
    install -d -o root -g root -m 0755 "${VAR_DIR}" "${VAR_DIR}/sock"
    # The agent set is probe, probe2, every --agent and every sandbox agent already on the node
    # (deduplicated; ensure_agent is idempotent). Collected here, before the ensure_agent loop,
    # so the same set feeds it and installed.json.
    existing="$(existing_agents)"
    for name in probe probe2 ${AGENTS[@]+"${AGENTS[@]}"} ${existing}; do
        case " ${agents[*]:-} " in *" ${name} "*) continue ;; esac
        agents+=("${name}")
    done
    for name in ${existing}; do
        case "${name}" in probe|probe2) continue ;; esac
        case " ${AGENTS[@]+"${AGENTS[@]}"} " in *" ${name} "*) continue ;; esac
        kept="${kept:+${kept}, }${name}"
    done
    [ -z "${kept}" ] || say "ℹ️  기존 에이전트 유지: ${kept}"
    for name in ${agents[@]+"${agents[@]}"}; do
        ensure_agent "${name}"
    done
    if [ "${#CLI_ARGS[@]}" -gt 0 ]; then
        say "🔧 CLI 복사본 설치"
        for item in "${CLI_ARGS[@]}"; do
            if install_cli "${item%%=*}" "${item#*=}"; then
                copied+=("${item}")
            fi
        done
    fi
    install_dropin "${DROPIN}"
    write_installed_json ${agents[@]+"${agents[@]}"} -- ${copied[@]+"${copied[@]}"}
    sync_user_manager
    say "✅ sandbox 설치 완료"
}

do_remove() {
    local name user home dir dropin comp cur linked=0 present="" keep="" json_tmp=""
    if [ "${#AGENTS[@]}" -gt 0 ]; then
        for name in "${AGENTS[@]}"; do
            [[ "${name}" =~ ${AGENT_NAME_RE} ]] || die "에이전트 이름이 올바르지 않습니다: ${name}"
            remove_agent "gsa-${name}"
        done
        # installed.json must drop the removed names too: the worker starts a turn only for a
        # listed agent, so a name whose user survived a failed userdel would fail mid-turn. The
        # list goes back to probe, probe2 and every sandbox agent still on the node; probe and
        # probe2 are recreated on every install, so they leave the list only when this run
        # removed them and their user is really gone.
        present="$(existing_agents | tr '\n' ' ')"
        for name in probe probe2 ${present}; do
            case " ${keep} " in *" ${name} "*) continue ;; esac
            case " ${AGENTS[@]+"${AGENTS[@]}"} " in
                *" ${name} "*)
                    case "${name}" in
                        probe|probe2) case " ${present} " in *" ${name} "*) ;; *) continue ;; esac ;;
                        *) continue ;;
                    esac
                    ;;
            esac
            keep="${keep:+${keep} }${name}"
        done
        if [ -f "${ETC_DIR}/installed.json" ]; then
            json_tmp="$(mktemp /tmp/guardian-sandbox-json.XXXXXX)"
            python3 -I - "${ETC_DIR}/installed.json" "${json_tmp}" ${keep} <<'PY' || { rm -f "${json_tmp}"; die "installed.json 갱신 실패"; }
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    doc = json.load(f)  # version, installed and cli stay exactly as they are
doc["agents"] = sorted(set(sys.argv[3:]))
with open(dst, "w", encoding="utf-8") as f:
    json.dump(doc, f)
    f.write("\n")
PY
            install_mv 0644 "${json_tmp}" "${ETC_DIR}/installed.json"
            rm -f "${json_tmp}"
        fi
        say "✅ 지정한 에이전트만 제거했습니다 (래퍼, 설정, 드롭인은 그대로입니다)"
        return 0
    fi
    while IFS= read -r user; do
        [ -n "${user}" ] || continue
        remove_agent "${user}"
    done < <(getent passwd | awk -F: '$1 ~ /^gsa-/ && $5 ~ /^guardian sandbox agent/ {print $1}' | sort)
    rm -f "${ETC_DIR_SUDOERS}/guardian-sandbox" "${ETC_DIR_SUDOERS}/.guardian-sandbox.new"
    rm -f "${SBIN_DIR}/guardian-sandbox-run" "${SBIN_DIR}/.guardian-sandbox-run.new"
    rm -rf "${ETC_DIR}" "${LIB_DIR}" "${VAR_DIR}"
    # The worker drop-in goes with the whole sandbox (a single-agent --remove keeps it). The same
    # symlink refusal as the install: root never removes through a link in the node user's tree.
    home="$(getent passwd "${NODE_USER}" | cut -d: -f6 || true)"
    if [ -n "${home}" ]; then
        dir="${home}/.config/systemd/user/guardian-worker.service.d"
        cur="${home}"
        for comp in .config systemd user guardian-worker.service.d; do
            cur="${cur}/${comp}"
            if [ -L "${cur}" ]; then
                linked=1
                break
            fi
        done
        if [ "${linked}" = 1 ]; then
            say "⚠️  ${home} 아래 user 단위 경로가 심볼릭 링크라 드롭인 제거를 건너뜁니다: ${cur}"
        else
            dropin="${dir}/sandbox-sudo.conf"
            if [ -e "${dropin}" ] || [ -L "${dropin}" ]; then
                rm -f "${dropin}"
                DROPIN_CHANGED=1
            fi
            rmdir "${dir}" >/dev/null 2>&1 || true  # only when it is then empty
        fi
    fi
    sync_user_manager
    say "✅ sandbox 제거 완료"
}

if [ "${REMOVE}" = 1 ]; then
    do_remove
else
    do_install
fi

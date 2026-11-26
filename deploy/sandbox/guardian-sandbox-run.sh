#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - one sandboxed agent turn (docs/design/resident-open-tools.md, O1, rule 2)
#
# Runs ONE command as gsa-<agent> inside a locked-down systemd transient unit: no privileges, no
# home but the agent's own, the tailnet and every private range denied, and memory/task/CPU/time
# caps from /etc/guardian-sandbox/limits.conf. The node user starts it through the sudoers rule;
# nothing else may call this. Loopback is denied except the resolver stub (127.0.0.53/54) because
# the node has local services there. No MemoryDenyWriteExecute: the LLM CLIs are JIT runtimes.
#
# Usage:
#   guardian-sandbox-run [--dry-run] [--probe-limits] [--timeout SECONDS] [--chdir DIR] <agent> -- <command> [args...]
#   guardian-sandbox-run [--dry-run] --stop <agent>
#
#   --dry-run       Print the systemd-run argv one element per line and exit 0 (first option only;
#                   every check still runs, nothing is executed). With --stop: print the systemctl
#                   commands instead
#   --probe-limits  Use PROBE_* limits; allowed only for the agents probe and probe2
#   --timeout N     RuntimeMaxSec, 1..TIMEOUT_MAX (default: TIMEOUT_MAX)
#   --chdir DIR     Working directory inside /home/gsa-<agent> (default: the home)
#   --stop <agent>  Stop every gsa-<agent>-*.service unit and wait for them to end; exit 0 also
#                   when none ran, 1 when a unit is still active or systemctl fails
#   <agent>         Name matching ^[a-z][a-z0-9]{0,15}$; the Unix user is gsa-<agent>
#   --              Everything after it is the command (at least one word)
#
# A normal turn runs systemd-run in the background, with the wrapper's own stdin, and waits for
# it: the signal trap can then stop the unit it started (TERM/INT/HUP -> systemctl stop <unit>,
# exit 128+n). exec would leave no process behind to catch a signal, and a background job without
# an explicit stdin redirect would read /dev/null, so --pipe would lose the turn's stdin.
#
# Exit: systemd-run's exit status (--wait), 1 --stop failed, 64 refused input.
# With GUARDIAN_SANDBOX_SOURCE_ONLY=1 the file only defines its functions (the test sources it;
# sudo's env_reset drops the variable, and source-only executes nothing, so it is harmless).
# ==============================================================================

set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022

LIMITS_FILE="/etc/guardian-sandbox/limits.conf"
AGENT_NAME_RE='^[a-z][a-z0-9]{0,15}$'
NUM_RE='^[0-9]+$'
MEM_RE='^[0-9]+[KMG]$'
CPU_RE='^[0-9]+%$'
LIMIT_KEYS="MEMORY_MAX TASKS_MAX CPU_QUOTA TIMEOUT_MAX PROBE_MEMORY_MAX PROBE_TASKS_MAX"
ARGV=()
SB_DRY=0
SB_UNIT=""

die() { printf 'guardian-sandbox-run: %s\n' "$1" >&2; exit 64; }

# True when NAME is a well-formed agent name (the gsa-<agent> user is derived from it).
agent_name_ok() {
    [[ "${1:-}" =~ ${AGENT_NAME_RE} ]]
}

# Everything the wrapper trusts about the agent user (docs/design/resident-open-tools.md, rule 1):
# a system user, own group, no supplementary groups, own home, marked by its comment field.
check_agent() {  # check_agent NAME -> sets SB_AGENT
    local name="${1:-}" user line uid gecos home gids
    agent_name_ok "${name}" || die "agent name must match ${AGENT_NAME_RE}: ${name}"
    user="gsa-${name}"
    line="$(getent passwd "${user}" || true)"
    [ -n "${line}" ] || die "no such sandbox user: ${user}"
    uid="$(printf '%s\n' "${line}" | cut -d: -f3)"
    gecos="$(printf '%s\n' "${line}" | cut -d: -f5)"
    home="$(printf '%s\n' "${line}" | cut -d: -f6)"
    [[ "${uid}" =~ ${NUM_RE} ]] || die "uid of ${user} is not a number: ${uid}"
    if [ "${uid}" -lt 1 ] || [ "${uid}" -gt 999 ]; then
        die "${user} must be a system user (uid 1..999, got ${uid})"
    fi
    [ "$(id -gn "${user}")" = "${user}" ] || die "primary group of ${user} is not ${user}"
    read -r -a gids <<<"$(id -G "${user}" || true)"
    [ "${#gids[@]}" -eq 1 ] || die "${user} must be in exactly one group (${user} only)"
    [ "${home}" = "/home/${user}" ] || die "home of ${user} is not /home/${user}: ${home}"
    case "${gecos}" in
        "guardian sandbox agent"*) ;;
        *) die "comment of ${user} does not mark a sandbox agent: ${gecos}" ;;
    esac
    SB_AGENT="${name}"
}

# The limits file must be a regular file, root-owned and not group- or world-writable.
check_limits_file() {  # check_limits_file FILE
    local file="$1" owner mode
    [ -f "${file}" ] || die "not a regular file: ${file}"
    owner="$(stat -c '%u' "${file}")"
    mode="$(stat -c '%a' "${file}")"
    [ "${owner}" = 0 ] || die "${file} must be owned by root (uid ${owner})"
    [ "$((8#${mode} & 8#022))" -eq 0 ] || die "${file} must not be group- or world-writable (mode ${mode})"
}

# The pure parser (separate from the owner/mode check so the test can call it alone):
# KEY=VALUE lines only, never sourced; blank lines and # comments are ignored.
read_limits() {  # read_limits FILE -> sets MEMORY_MAX ... PROBE_TASKS_MAX
    local file="$1" line key value k seen=" "
    while IFS= read -r line || [ -n "${line}" ]; do
        line="${line%"${line##*[^[:space:]]}"}"
        case "${line}" in ''|'#'*) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        [ "${key}" != "${line}" ] || die "not a KEY=VALUE line in ${file}: ${line}"
        case "${key}" in
            MEMORY_MAX|PROBE_MEMORY_MAX)
                [[ "${value}" =~ ${MEM_RE} ]] || die "bad ${key} in ${file}: ${value}" ;;
            TASKS_MAX|PROBE_TASKS_MAX|TIMEOUT_MAX)
                [[ "${value}" =~ ${NUM_RE} ]] || die "bad ${key} in ${file}: ${value}" ;;
            CPU_QUOTA)
                [[ "${value}" =~ ${CPU_RE} ]] || die "bad ${key} in ${file}: ${value}" ;;
            *)
                die "unknown key in ${file}: ${key}" ;;
        esac
        printf -v "${key}" '%s' "${value}"
        seen="${seen}${key} "
    done <"${file}"
    for k in ${LIMIT_KEYS}; do
        case "${seen}" in
            *" ${k} "*) ;;
            *) die "missing key in ${file}: ${k}" ;;
        esac
    done
}

# --timeout must be a whole number 1..TIMEOUT_MAX; empty means the default TIMEOUT_MAX.
check_timeout() {  # check_timeout [VALUE] -> sets SB_TIMEOUT
    local value="${1:-}"
    if [ -z "${value}" ]; then
        SB_TIMEOUT="${TIMEOUT_MAX}"
        return 0
    fi
    [[ "${value}" =~ ${NUM_RE} ]] || die "--timeout must be a whole number: ${value}"
    if [ "${value}" -lt 1 ] || [ "${value}" -gt "${TIMEOUT_MAX}" ]; then
        die "--timeout must be 1..${TIMEOUT_MAX}: ${value}"
    fi
    SB_TIMEOUT="${value}"
}

# --chdir must be the agent home or below it, with no ".." component and no "//".
check_chdir() {  # check_chdir [VALUE] -> sets SB_CHDIR (needs SB_AGENT)
    local base="/home/gsa-${SB_AGENT}" value
    value="${1:-${base}}"
    case "${value}" in
        "${base}"|"${base}/"*) ;;
        *) die "--chdir must be ${base} or below it: ${value}" ;;
    esac
    case "${value}" in
        *//*) die "--chdir must not contain //: ${value}" ;;
        *"/../"*|*"/..") die "--chdir must not contain a .. component: ${value}" ;;
    esac
    SB_CHDIR="${value}"
}

# Fills the global ARGV with the systemd-run invocation. The caller's environment is never passed:
# the unit gets exactly the Environment= line below. SB_AGENT/SB_PROBE/SB_TIMEOUT/SB_CHDIR and the
# limits must be set (check_agent, check_timeout, check_chdir, read_limits ran before).
build_argv() {  # build_argv COMMAND... -> ARGV
    local mem="${MEMORY_MAX}" tasks="${TASKS_MAX}" rand
    if [ "${SB_PROBE:-0}" = 1 ]; then
        mem="${PROBE_MEMORY_MAX}"
        tasks="${PROBE_TASKS_MAX}"
    fi
    rand="$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
    SB_UNIT="gsa-${SB_AGENT}-${rand}"
    ARGV=(
        systemd-run --quiet --wait --collect --pipe --service-type=exec --expand-environment=no
        "--unit=${SB_UNIT}"
        "--uid=gsa-${SB_AGENT}" "--gid=gsa-${SB_AGENT}" "--working-directory=${SB_CHDIR}"
        -p NoNewPrivileges=yes -p CapabilityBoundingSet= -p AmbientCapabilities=
        -p RestrictSUIDSGID=yes -p PrivateTmp=yes -p PrivateDevices=yes -p PrivateIPC=yes
        -p ProtectKernelTunables=yes -p ProtectKernelModules=yes -p ProtectKernelLogs=yes
        -p ProtectControlGroups=yes -p ProtectClock=yes -p ProtectHostname=yes
        -p ProtectProc=invisible -p RestrictNamespaces=yes -p LockPersonality=yes -p RestrictRealtime=yes
        -p SystemCallArchitectures=native -p RemoveIPC=yes -p KeyringMode=private -p UMask=0077
        -p ProtectSystem=strict -p ProtectHome=tmpfs -p "BindPaths=/home/gsa-${SB_AGENT}"
        -p TemporaryFileSystem=/var/lib/guardian-sandbox:ro
        -p "BindPaths=/var/lib/guardian-sandbox/sock/${SB_AGENT}"
        # /root (ProtectHome leaves it as a listable tmpfs) and sockets under /run that any user may connect to;
        # the probe (sandbox_probe.py) reports what is left.
        -p "InaccessiblePaths=-/root -/run/postgresql -/run/systemd/notify -/run/systemd/io.systemd.ManagedOOM -/run/tailscale -/run/dbus -/run/snapd.socket -/run/snapd-snap.socket -/run/lxd-installer.socket -/run/rpcbind.sock -/var/snap/lxd -/run/docker.sock -/run/containerd"
        -p "IPAddressDeny=0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4 ::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8"
        -p "IPAddressAllow=127.0.0.53/32 127.0.0.54/32"
        -p "MemoryMax=${mem}" -p MemorySwapMax=0
        -p "TasksMax=${tasks}" -p "CPUQuota=${CPU_QUOTA}"
        -p "RuntimeMaxSec=${SB_TIMEOUT}" -p KillMode=control-group -p SendSIGKILL=yes
        -p "Environment=SHELL=/bin/bash PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8"
    )
    ARGV+=("--")
    ARGV+=("$@")
}

# --stop <agent> (C4): end every gsa-<agent>-*.service unit and confirm none is active anymore.
# systemctl globs the pattern over the loaded units, so stopping when nothing ran is not an
# error; a unit that stays active (or a failing systemctl) is.
do_stop() {  # do_stop AGENT (SB_DRY=1 prints the commands instead of running them)
    local agent="$1" pattern active
    agent_name_ok "${agent}" || die "agent name must match ${AGENT_NAME_RE}: ${agent}"
    pattern="gsa-${agent}-*.service"
    if [ "${SB_DRY}" = 1 ]; then
        printf 'systemctl stop %s\n' "${pattern}"
        printf 'systemctl list-units --plain --no-legend %s\n' "${pattern}"
        return 0
    fi
    systemctl stop "${pattern}" || return 1
    active="$(systemctl list-units --plain --no-legend "${pattern}" 2>/dev/null \
        | awk '$3 == "active" { print $1; exit }')" || true
    if [ -n "${active}" ]; then
        printf 'guardian-sandbox-run: unit still active after --stop: %s\n' "${active}" >&2
        return 1
    fi
    return 0
}

# On TERM/INT/HUP during a normal turn: stop the unit this wrapper started and exit 128+signal.
stop_own_unit() {  # stop_own_unit SIGNUM (the trap passes the number)
    local sig="$1"
    trap - TERM INT HUP
    systemctl stop "${SB_UNIT}" >/dev/null 2>&1 || true
    exit $((128 + sig))
}

# A normal turn: systemd-run in the background with the wrapper's own stdin (a background job
# otherwise reads /dev/null and --pipe needs the real stdin), then wait for it, keeping its
# exit status. The traps above can only fire while something is left to catch them.
run_unit() {  # run_unit (ARGV must be set; returns systemd-run's exit status)
    local pid rc=0
    trap 'stop_own_unit 15' TERM
    trap 'stop_own_unit 2' INT
    trap 'stop_own_unit 1' HUP
    "${ARGV[@]}" <&0 &
    pid=$!
    wait "${pid}" || rc=$?
    return "${rc}"
}

main() {
    local agent="" probe=0 timeout_arg="" chdir_arg="" stop_agent=""
    [ "$(id -u)" -eq 0 ] || die "must run as root (start it with sudo guardian-sandbox-run)"
    if [ "${1:-}" = "--dry-run" ]; then
        SB_DRY=1
        shift
    fi
    while [ $# -gt 0 ]; do
        case "$1" in
            --stop)
                shift
                [ -n "${1:-}" ] || die "--stop needs an agent name"
                stop_agent="$1" ;;
            --probe-limits) probe=1 ;;
            --timeout)
                shift
                [ -n "${1:-}" ] || die "--timeout needs a value"
                timeout_arg="$1" ;;
            --chdir)
                shift
                [ -n "${1:-}" ] || die "--chdir needs a value"
                chdir_arg="$1" ;;
            --) shift; break ;;
            -*) die "unknown option: $1" ;;
            *) [ -z "${agent}" ] || die "only one <agent> is allowed"
               agent="$1" ;;
        esac
        shift
    done
    if [ -n "${stop_agent}" ]; then
        [ -z "${agent}" ] || die "--stop takes no <agent> and no command"
        [ $# -eq 0 ] || die "--stop takes no command"
        do_stop "${stop_agent}"
        return $?
    fi
    [ -n "${agent}" ] || die "missing <agent>"
    [ $# -gt 0 ] || die "missing the command after --"
    check_agent "${agent}"
    check_limits_file "${LIMITS_FILE}"
    read_limits "${LIMITS_FILE}"
    if [ "${probe}" = 1 ]; then
        case "${agent}" in
            probe|probe2) ;;
            *) die "--probe-limits is allowed only for the agents probe and probe2" ;;
        esac
    fi
    SB_PROBE="${probe}"
    check_timeout "${timeout_arg}"
    check_chdir "${chdir_arg}"
    build_argv "$@"
    if [ "${SB_DRY}" = 1 ]; then
        printf '%s\n' "${ARGV[@]}"
        return 0
    fi
    run_unit
}

if [ "${GUARDIAN_SANDBOX_SOURCE_ONLY:-}" = 1 ]; then
    return 0 2>/dev/null || exit 0
fi
main "$@"

#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - remote part of scripts/probe-sandbox.sh (docs/design/resident-open-tools.md, O1-O2)
#
# Runs on the probed node AS THE NODE USER (the admin script unpacked the tarball into --dir DIR and
# feeds this file to `bash FILE ARGS`). Prints key=value lines on stdout, progress on stderr.
# sudo -n appears only for the fixed wrapper rule and the fixtures the node user cannot write itself
# (gsa-probe's 0700 home, the canary of gsa-probe2); the trap on EXIT removes every fixture, also on
# failure. Works with bash 5 on the node. Since O2 it also checks the launcher inside the unit
# (a claude turn with no credential ends 3 launch: no_credential, or cli_missing with no copy) and
# the wrapper's --stop (every gsa-probe-* unit must end). Since O3's first real turns it also checks
# the daemon's own view: every agent's work dir (what --chdir needs before the launcher runs) and,
# when guardian-worker.service runs, its NoNewPrivs and gsa groups (the drop-in and the user-manager
# restart that install-sandbox.sh does).
#
# Arguments: --hub-ip IP [--peer-ip IP] [--no-stress] --dir DIR
#   --hub-ip IP    tailnet address of the hub (a must-deny tcp target)
#   --peer-ip IP   another host's tailnet address (its port 22 must be denied)
#   --no-stress    skip the memory and task cap runs (a busy node skips them too)
#   --dir DIR      the unpacked tarball directory (the inner script; the repository parts the workload tests read)
#
# Exit: 0 the probe ran (the verdict is in the lines), 1 it could not run (probe.error=<reason>),
# 2 misuse.
# ==============================================================================

set -uo pipefail

WRAPPER="/usr/local/sbin/guardian-sandbox-run"
LIMITS_FILE="/etc/guardian-sandbox/limits.conf"
HUB_IP=""
PEER_IP=""
DIR=""
NOSTRESS=0
STUB_PIDS=()
DONE=0
STRESS_STATE=""
BUSY=0

say() { printf '%s\n' "$*" >&2; }
kv() { printf '%s=%s\n' "$1" "$2"; }
fail_probe() { kv probe.error "$1"; exit 1; }

cleanup() {
    local rc=$? pid
    for pid in ${STUB_PIDS[@]+"${STUB_PIDS[@]}"}; do
        kill "${pid}" >/dev/null 2>&1 || true
    done
    # The stub path is unique per run (a mktemp directory); pkill only when the dir is known.
    if [ -n "${DIR}" ]; then
        pkill -f "${DIR}/stub.py" >/dev/null 2>&1 || true
    fi
    rm -f /var/lib/guardian-sandbox/sock/probe/stub.sock /var/lib/guardian-sandbox/sock/probe2/stub.sock \
        >/dev/null 2>&1 || true
    sudo -n rm -f /home/gsa-probe2/canary >/dev/null 2>&1 || true
    # The work DIRECTORY is the installer's now (O3: the daemon's --chdir needs it to exist before
    # the launcher runs), so only its contents are removed as the probe's fixture.
    sudo -n -u gsa-probe find /home/gsa-probe/work -mindepth 1 -delete >/dev/null 2>&1 || true
    sudo -n -u gsa-probe rm -f /home/gsa-probe/inner.py >/dev/null 2>&1 || true
    sudo -n pkill -KILL -u gsa-probe >/dev/null 2>&1 || true
    sudo -n pkill -KILL -u gsa-probe2 >/dev/null 2>&1 || true
    if [ "${DONE}" = 1 ]; then
        kv probe.done 1
    fi
    exit "${rc}"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

check_preconditions() {
    local line key
    [ -x "${WRAPPER}" ] || fail_probe "wrapper missing: ${WRAPPER}"
    getent passwd gsa-probe >/dev/null || fail_probe "user gsa-probe missing"
    getent passwd gsa-probe2 >/dev/null || fail_probe "user gsa-probe2 missing"
    [ -r /etc/guardian-sandbox/installed.json ] || fail_probe "installed.json not readable"
    kv sandbox_version "$(python3 -c 'import json; print(json.load(open("/etc/guardian-sandbox/installed.json")).get("version", ""))' 2>/dev/null || true)"
    while IFS= read -r line; do
        case "${line}" in ''|'#'*) continue ;; esac
        key="${line%%=*}"
        case "${key}" in
            MEMORY_MAX|TASKS_MAX|CPU_QUOTA|TIMEOUT_MAX|PROBE_MEMORY_MAX|PROBE_TASKS_MAX)
                kv "limits.${key}" "${line#*=}" ;;
        esac
    done <"${LIMITS_FILE}"
}

# The stress runs would fight real turns; skip them when a resident CLI is busy on this user.
busy_check() {
    if pgrep -u "$(id -un)" -f '(^|/)(claude|agy|codex|opencode)( |$)' >/dev/null 2>&1; then
        kv stress skipped_busy
        STRESS_STATE=skipped_busy
        BUSY=1
        return 0
    fi
    if [ "${NOSTRESS}" = 1 ]; then
        kv stress skipped_flag
        STRESS_STATE=skipped_flag
    else
        kv stress ran
        STRESS_STATE=ran
    fi
    return 0
}

write_stub() {
    cat >"${DIR}/stub.py" <<'PY'
"""Stub socket server for the sandbox probe: answers one line per connection, exits after 600 s."""
import os
import socket
import sys
import time

path = sys.argv[1]
os.umask(0o117)  # 0660 like the daemon's sockets (C7); the setgid dir gives the agent's group
deadline = time.time() + 600
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(8)
while True:
    left = deadline - time.time()
    if left <= 0:
        break
    server.settimeout(left)
    try:
        conn, _ = server.accept()
    except OSError:
        break
    try:
        conn.settimeout(5)
        conn.recv(4096)
        conn.sendall(b'{"ok": true, "stub": true}\n')
    except OSError:
        pass
    finally:
        conn.close()
PY
}

start_stub() {  # start_stub SOCKPATH
    local sock="$1"
    [ -d "$(dirname "${sock}")" ] || fail_probe "sock dir missing: $(dirname "${sock}")"
    # The node user owns the sock dir since O2 (2750, setgid), as the daemon does: the socket gets
    # the agent's group from the directory, no sg needed.
    python3 "${DIR}/stub.py" "${sock}" >/dev/null 2>&1 &
    STUB_PIDS+=("$!")
}

wait_sockets() {
    local i=0
    while [ "${i}" -lt 10 ]; do
        if [ -S /var/lib/guardian-sandbox/sock/probe/stub.sock ] \
            && [ -S /var/lib/guardian-sandbox/sock/probe2/stub.sock ]; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    fail_probe "stub sockets did not appear"
}

make_fixtures() {
    say "🔎 fixture 준비"
    sudo -n install -o gsa-probe2 -g gsa-probe2 -m 0600 /dev/null /home/gsa-probe2/canary \
        || fail_probe "canary 생성 실패 (sudo -n이 동작하나요?)"
    write_stub
    start_stub /var/lib/guardian-sandbox/sock/probe/stub.sock
    start_stub /var/lib/guardian-sandbox/sock/probe2/stub.sock
    wait_sockets
    # The node user cannot write the agent's 0700 home: unpack and copy as the agent itself.
    sudo -n -u gsa-probe mkdir -p /home/gsa-probe/work || fail_probe "work 디렉터리 생성 실패"
    tar -C "${DIR}" -cf - cli/llm-agent configs tools deploy scripts apps/agent | sudo -n -u gsa-probe tar -x -C /home/gsa-probe/work \
        || fail_probe "workload 복사 실패"
    sudo -n -u gsa-probe tee /home/gsa-probe/inner.py \
        <"${DIR}/scripts/lib/sandbox-probe-inner.py" >/dev/null || fail_probe "inner.py 복사 실패"
}

# The payload the inner checks read: listener ports, own addresses, the two tailnet targets.
run_checks() {
    local b64 rc=0
    b64="$(python3 - "${HUB_IP}" "${PEER_IP}" "${HOME}" "$(id -un)" <<'PY'
import base64, json, subprocess, sys
hub_ip, peer_ip, node_home, node_user = sys.argv[1:5]

def out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""

listeners = []
for line in out(["ss", "-tlnH"]).splitlines():
    parts = line.split()
    local = ""
    for cand in (parts[3] if len(parts) > 3 else "", parts[2] if len(parts) > 2 else ""):
        if ":" in cand:
            local = cand
            break
    host, _, port = local.rpartition(":")
    if port.isdigit():
        listeners.append({"addr": host.strip("[]"), "port": int(port)})
own_addrs = []
for line in out(["ip", "-o", "addr"]).splitlines():
    parts = line.split()
    if len(parts) < 4 or parts[2] not in ("inet", "inet6"):
        continue
    addr = parts[3].split("/")[0]
    if addr.startswith("127.") or addr == "::1" or addr.lower().startswith("fe80:"):
        continue
    own_addrs.append(addr)
payload = {"node_home": node_home, "node_user": node_user, "listeners": listeners,
           "own_addrs": own_addrs, "hub_ip": hub_ip, "peer_ip": peer_ip,
           "own_sock": "/var/lib/guardian-sandbox/sock/probe/stub.sock",
           "other_sock": "/var/lib/guardian-sandbox/sock/probe2/stub.sock",
           "worker_sock": node_home + "/.guardian/worker/worker.sock"}
print(base64.b64encode(json.dumps(payload).encode()).decode())
PY
    )" || fail_probe "payload 빌드 실패"
    sudo -n "${WRAPPER}" --probe-limits --timeout 300 probe -- \
        python3 /home/gsa-probe/inner.py checks "${b64}" || rc=$?
    kv run1.exit "${rc}"
}

leftover_count() {  # leftover_count USER -> number of processes
    local n
    n="$(pgrep -u "$1" -c 2>/dev/null || true)"
    printf '%s' "${n:-0}"
}

# Prints KEY.leftover and kills whatever escaped the unit (none should ever survive it).
report_leftover() {  # report_leftover KEY
    local key="$1" n
    sleep 1  # let systemd finish stopping the unit before counting
    n="$(leftover_count gsa-probe)"
    kv "${key}.leftover" "${n}"
    if [ "${n}" -gt 0 ]; then
        sudo -n pkill -KILL -u gsa-probe >/dev/null 2>&1 || true
    fi
}

# How slow a bare `true` is right after a capped run: the node must stay responsive.
node_ms() {
    local secs LC_ALL=C TIMEFORMAT='%R'
    secs="$( { time true; } 2>&1 )"
    awk -v s="${secs}" 'BEGIN { printf "%.0f", s * 1000 }'
}

run_hog() {
    local rc=0
    say "🔎 메모리 상한 확인"
    sudo -n "${WRAPPER}" --probe-limits --timeout 120 probe -- python3 /home/gsa-probe/inner.py hog || rc=$?
    kv hog.exit "${rc}"
    kv hog.node_ms "$(node_ms)"
    report_leftover hog
}

run_forks() {
    local rc=0
    say "🔎 작업 수 상한 확인"
    sudo -n "${WRAPPER}" --probe-limits --timeout 120 probe -- python3 /home/gsa-probe/inner.py forks || rc=$?
    kv forks.exit "${rc}"
    kv forks.node_ms "$(node_ms)"
    report_leftover forks
}

run_workload() {
    if [ "${BUSY}" = 1 ]; then
        kv workload skipped_busy
        return 0
    fi
    local rc=0
    say "🔎 실제 워크로드 (최대 30분)"
    sudo -n "${WRAPPER}" --timeout 1800 --chdir /home/gsa-probe/work/cli/llm-agent probe -- \
        python3 /home/gsa-probe/inner.py workload || rc=$?
    kv workload.exit "${rc}"
}

# O2 (C2): the launcher must run inside the unit through the wrapper. A valid claude turn with
# no credential for gsa-probe ends 3 launch: no_credential, or 3 launch: cli_missing when no CLI
# copy is installed; either proves the unit starts the launcher (which one is reported).
run_launcher_check() {
    local rc=0 out ver
    say "🔎 launcher 확인"
    ver="$(python3 -c 'import json; print((json.load(open("/etc/guardian-sandbox/installed.json")).get("cli") or {}).get("claude") or "0.0.0")' 2>/dev/null || echo 0.0.0)"
    out="$(printf '{"format":1,"provider":"claude","version":"%s","argv":["claude","--version"],"files":{},"stdin":""}\n' \
        "${ver}" | sudo -n "${WRAPPER}" --probe-limits --timeout 60 probe -- \
        /usr/bin/python3 -I /usr/local/lib/guardian-sandbox/launch.py 2>&1)" || rc=$?
    kv launcher.exit "${rc}"
    case "${out}" in
        *"launch: no_credential"*) kv launcher.result no_credential ;;
        *"launch: cli_missing"*) kv launcher.result cli_missing ;;
        *) kv launcher.result "unexpected:$(printf '%s' "${out}" | head -c 60 | tr '\n' ' ')" ;;
    esac
}

# O2 (C4): --stop must end every gsa-probe-* unit. A sleep-60 turn runs in the background; the
# check waits for its unit to be active first (a stop before the unit loads would prove nothing),
# then stops it and requires no gsa-probe-* unit active within 10 s. stop.started=0 means no unit
# ever came up, so the check proved nothing and the evaluator fails it.
run_stop_check() {
    local rc=0 started=0 active i units="" turn_pid
    say "🔎 stop 모드 확인"
    sudo -n "${WRAPPER}" --probe-limits --timeout 120 probe -- sleep 60 </dev/null >/dev/null 2>&1 &
    turn_pid=$!
    i=0
    while [ "${i}" -lt 10 ]; do
        units="$(systemctl list-units --plain --no-legend 'gsa-probe-*.service' 2>/dev/null | awk '$3 == "active"')"
        if [ -n "${units}" ]; then
            started=1
            break
        fi
        sleep 1
        i=$((i + 1))
    done
    kv stop.started "${started}"
    sudo -n "${WRAPPER}" --stop probe >/dev/null 2>&1 || rc=$?
    kv stop.exit "${rc}"
    active="timeout"
    i=0
    while [ "${i}" -lt 10 ]; do
        units="$(systemctl list-units --plain --no-legend 'gsa-probe-*.service' 2>/dev/null | awk '$3 == "active"')"
        if [ -z "${units}" ]; then
            active="none"
            break
        fi
        sleep 1
        i=$((i + 1))
    done
    kv stop.active "${active}"
    wait "${turn_pid}" >/dev/null 2>&1 || true
}

# O3's first real turns, seen from the daemon's side (the O1 probe ran from an ssh shell and missed
# all three): (a) every agent's /home/gsa-<name>/work must exist, be a directory and belong to the
# agent, because systemd resolves --chdir before the launcher runs; (b) a running guardian-worker
# unit must have NoNewPrivs 0 (the drop-in of deploy/worker/sandbox-sudo.conf) and every gsa-* gid
# of the node user in its Groups (the user-manager restart). The agent homes are 0700, so stat runs
# through the node user's own sudo.
run_daemon_checks() {
    local agents name out kind owner group pid nnp groups gname gid missing=""
    say "🔎 데몬 시점 확인 (work 디렉터리, worker 단위)"
    agents="$(python3 -c 'import json; print(" ".join(json.load(open("/etc/guardian-sandbox/installed.json")).get("agents") or []))' 2>/dev/null || true)"
    [ -n "${agents}" ] || agents="probe probe2"
    for name in ${agents}; do
        out="$(sudo -n stat -c '%F|%U|%G' "/home/gsa-${name}/work" 2>/dev/null || true)"
        if [ -z "${out}" ]; then
            kv "daemon.workdir.${name}" missing
            continue
        fi
        IFS='|' read -r kind owner group <<<"${out}"
        case "${kind}" in
            directory) ;;
            "symbolic link") kv "daemon.workdir.${name}" symlink; continue ;;
            *) kv "daemon.workdir.${name}" "not_a_directory"; continue ;;
        esac
        if [ "${owner}" != "gsa-${name}" ] || [ "${group}" != "gsa-${name}" ]; then
            kv "daemon.workdir.${name}" "owner:${owner}:${group}"
        else
            kv "daemon.workdir.${name}" ok
        fi
    done
    if sudo -n systemctl --user -M "$(id -un)@" is-active --quiet guardian-worker.service 2>/dev/null; then
        kv daemon.worker running
        pid="$(sudo -n systemctl --user -M "$(id -un)@" show -p MainPID --value guardian-worker.service 2>/dev/null | tr -d '[:space:]' || true)"
        nnp="$(sudo -n sed -n 's/^NoNewPrivs:[[:space:]]*//p' "/proc/${pid}/status" 2>/dev/null | tr -d '[:space:]' || true)"
        groups="$(sudo -n sed -n 's/^Groups:[[:space:]]*//p' "/proc/${pid}/status" 2>/dev/null || true)"
        if [ -n "${nnp}" ]; then
            kv daemon.worker_nnp "${nnp}"
        else
            kv daemon.worker_nnp unknown
        fi
        for gname in $(id -nG 2>/dev/null); do
            case "${gname}" in
                gsa-*)
                    gid="$(getent group "${gname}" | cut -d: -f3 || true)"
                    [[ "${gid}" =~ ^[0-9]+$ ]] || continue
                    case " ${groups} " in
                        *" ${gid} "*) ;;
                        *) missing="${missing:+${missing},}${gname}" ;;
                    esac
                    ;;
            esac
        done
        if [ -n "${missing}" ]; then
            kv daemon.worker_groups "missing:${missing}"
        else
            kv daemon.worker_groups ok
        fi
    else
        kv daemon.worker stopped
    fi
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --hub-ip) shift; HUB_IP="${1:-}" ;;
            --peer-ip) shift; PEER_IP="${1:-}" ;;
            --no-stress) NOSTRESS=1 ;;
            --dir) shift; DIR="${1:-}" ;;
            *) say "알 수 없는 인자: $1"; exit 2 ;;
        esac
        shift
    done
    [ -n "${HUB_IP}" ] || { kv probe.error "missing --hub-ip"; exit 2; }
    [ -n "${DIR}" ] && [ -d "${DIR}" ] || { kv probe.error "missing --dir"; exit 2; }
    command -v python3 >/dev/null 2>&1 || { kv probe.error "python3 missing on the node"; exit 1; }
    check_preconditions
    busy_check
    run_daemon_checks
    make_fixtures
    say "🔎 sandbox 내부 확인"
    run_checks
    report_leftover persist
    if [ "${STRESS_STATE}" = ran ]; then
        run_hog
        run_forks
    fi
    run_launcher_check
    run_stop_check
    run_workload
    DONE=1
}

main "$@"

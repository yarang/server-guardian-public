#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Fleet apt upgrade (no reboot)
#
# Runs `apt-get update && apt-get full-upgrade` on each host, keeping existing
# config files on conflicts, waiting for the dpkg lock, and reports the
# resulting OS version, kernel and whether a reboot is required.
# Reboots are never performed by this script.
#
# Usage:
#   scripts/fleet-upgrade.sh [--dry-run] [--parallel N] [--offline] [host ...]
#   Default hosts: every server in the hub inventory (~/.guardian/data/servers.json, fetched
#   read-only over ssh). --offline uses the checkout's configs/servers.json snapshot instead.
#
# Logs: ${LOG_DIR:-./upgrade-logs/<timestamp>}/<host>.log
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/upgrade-logs/$(date +%Y%m%d_%H%M%S)}"

DRY_RUN=0
PARALLEL=6
HOSTS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        --parallel) PARALLEL="$2"; shift ;;
        -h|--help) sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) HOSTS+=("$1") ;;
    esac
    shift
done

if [ ${#HOSTS[@]} -eq 0 ]; then
    inventory_load
    while IFS= read -r host; do HOSTS+=("${host}"); done < <(
        python3 -c 'import json,sys; [print(s["id"]) for s in json.load(open(sys.argv[1]))]' "${INVENTORY_FILE}"
    )
fi

# The upgrade runs as a transient systemd unit on the host, so it survives the
# ssh session being dropped when needrestart restarts services mid-upgrade.
read -r -d '' REMOTE_JOB <<'EOF' || true
#!/bin/bash
set -u
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
APT_OPTS="-y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
RESULT=/var/tmp/guardian-upgrade.result
exec >/var/tmp/guardian-upgrade.log 2>&1
before="$(. /etc/os-release; echo "$VERSION")"
apt-get $APT_OPTS update || { echo "FAIL|apt-get update" > "$RESULT"; exit 1; }
apt-get $APT_OPTS full-upgrade || { echo "FAIL|full-upgrade" > "$RESULT"; exit 1; }
apt-get $APT_OPTS autoremove || true
after="$(. /etc/os-release; echo "$VERSION")"
latest_kernel="$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|/boot/vmlinuz-||' | sort -V | tail -1)"
reboot="no"; [ -f /var/run/reboot-required ] && reboot="yes"
left="$(apt list --upgradable 2>/dev/null | grep -c upgradable)"
echo "OK|${before} -> ${after}|running $(uname -r)|installed ${latest_kernel}|reboot ${reboot}|remaining ${left}" > "$RESULT"
EOF

ssh_host() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 "$@"
}

mkdir -p "${LOG_DIR}"
echo "🎯 대상 ${#HOSTS[@]}대 (동시 ${PARALLEL}) · 로그: ${LOG_DIR}"

run_host() {
    local host="$1" log="${LOG_DIR}/$1.log" result=""
    if [ "${DRY_RUN}" = 1 ]; then
        result="$(ssh_host "${host}" "sudo -n apt-get -s full-upgrade 2>/dev/null | grep -c '^Inst '" 2>>"${log}")" \
            && printf '%-22s would upgrade: %s\n' "${host}" "${result}" \
            || printf '%-22s ❌ %s\n' "${host}" "$(tail -n 1 "${log}" | cut -c1-80)"
        return
    fi
    if ! ssh_host "${host}" 'sudo -n rm -f /var/tmp/guardian-upgrade.result && cat > /var/tmp/guardian-upgrade.sh && chmod 700 /var/tmp/guardian-upgrade.sh \
            && sudo -n systemd-run --unit="guardian-upgrade-$(date +%s)" --collect /bin/bash /var/tmp/guardian-upgrade.sh' \
            <<<"${REMOTE_JOB}" >>"${log}" 2>&1; then
        printf '%-22s FAIL|start: %s\n' "${host}" "$(tail -n 1 "${log}" | cut -c1-90)"
        return
    fi
    # Poll for the result file for up to 60 minutes.
    for _ in $(seq 1 180); do
        sleep 20
        result="$(ssh_host "${host}" 'cat /var/tmp/guardian-upgrade.result 2>/dev/null' 2>>"${log}" || true)"
        [ -n "${result}" ] && break
    done
    # The job writes its own log; journalctl unit globs scan the whole journal and stall small hosts.
    ssh_host "${host}" 'tail -n 400 /var/tmp/guardian-upgrade.log 2>/dev/null' >>"${log}" 2>&1 || true
    printf '%-22s %s\n' "${host}" "${result:-FAIL|timeout waiting for result}"
}

running=0
for host in "${HOSTS[@]}"; do
    run_host "${host}" &
    running=$((running + 1))
    if [ "${running}" -ge "${PARALLEL}" ]; then
        wait -n || true
        running=$((running - 1))
    fi
done
wait

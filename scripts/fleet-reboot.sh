#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Rolling fleet reboot
#
# Reboots hosts in batches and waits for each to come back with a new boot id.
# Reports the kernel before/after, system state, and failed units, marking
# units that were not already failed before the reboot as NEW.
#
# Usage:
#   scripts/fleet-reboot.sh [--batch N] [--only-if-required] host ...
#
#   --batch N            Hosts rebooted at the same time (default 3)
#   --only-if-required   Skip hosts without /var/run/reboot-required
#
# A batch must fully recover before the next one starts; the run stops at the
# first host that does not come back within 10 minutes.
# ==============================================================================

set -euo pipefail

BATCH=3
ONLY_REQUIRED=0
HOSTS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --batch) BATCH="$2"; shift ;;
        --only-if-required) ONLY_REQUIRED=1 ;;
        -h|--help) sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
        *) HOSTS+=("$1") ;;
    esac
    shift
done
[ ${#HOSTS[@]} -gt 0 ] || { echo "재부팅할 호스트를 지정하세요." >&2; exit 2; }

ssh_host() {
    timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=8 "$@"
}

STATE_DIR="$(mktemp -d -t fleet-reboot.XXXXXX)"

reboot_host() {
    local host="$1" state="${STATE_DIR}/$1"
    local pre
    if ! pre="$(ssh_host "${host}" 'uname -r; cat /proc/sys/kernel/random/boot_id; [ -f /var/run/reboot-required ] && echo yes || echo no; systemctl --failed --no-legend --plain | awk "{print \$1}" | tr "\n" " "')"; then
        echo "UNREACHABLE" > "${state}"; return
    fi
    local kernel boot required failed_before
    kernel="$(sed -n 1p <<<"${pre}")"; boot="$(sed -n 2p <<<"${pre}")"
    required="$(sed -n 3p <<<"${pre}")"; failed_before="$(sed -n 4p <<<"${pre}")"
    if [ "${ONLY_REQUIRED}" = 1 ] && [ "${required}" != yes ]; then
        echo "SKIP|reboot not required|${kernel}" > "${state}"; return
    fi
    ssh_host "${host}" 'sudo -n systemctl reboot' >/dev/null 2>&1 || true
    local start post="" now
    start="$(date +%s)"
    sleep 20
    while true; do
        post="$(ssh_host "${host}" 'uname -r; cat /proc/sys/kernel/random/boot_id; systemctl is-system-running --wait 2>/dev/null; systemctl --failed --no-legend --plain | awk "{print \$1}" | tr "\n" " "' 2>/dev/null || true)"
        if [ -n "${post}" ] && [ "$(sed -n 2p <<<"${post}")" != "${boot}" ]; then
            break
        fi
        now="$(date +%s)"
        if [ $((now - start)) -gt 600 ]; then
            echo "TIMEOUT|did not come back within 600s|${kernel}" > "${state}"; return
        fi
        sleep 10
    done
    local new_kernel sys failed_after new_failed="" unit
    new_kernel="$(sed -n 1p <<<"${post}")"; sys="$(sed -n 3p <<<"${post}")"; failed_after="$(sed -n 4p <<<"${post}")"
    for unit in ${failed_after}; do
        case " ${failed_before} " in *" ${unit} "*) ;; *) new_failed="${new_failed} ${unit}" ;; esac
    done
    echo "OK|$(( $(date +%s) - start ))s|${kernel} -> ${new_kernel}|${sys}|NEW failed:${new_failed:- none}|already failed: ${failed_before:-none}" > "${state}"
}

total=${#HOSTS[@]}
echo "🔁 롤링 재부팅 ${total}대 (배치 ${BATCH})"
for ((i = 0; i < total; i += BATCH)); do
    batch=("${HOSTS[@]:i:BATCH}")
    echo "--- 배치 $((i / BATCH + 1)): ${batch[*]}"
    for host in "${batch[@]}"; do reboot_host "${host}" & done
    wait
    stop=0
    for host in "${batch[@]}"; do
        result="$(cat "${STATE_DIR}/${host}")"
        printf '%-22s %s\n' "${host}" "${result}"
        case "${result}" in TIMEOUT*|UNREACHABLE*) stop=1 ;; esac
    done
    if [ "${stop}" = 1 ]; then
        echo "⛔ 복구되지 않은 호스트가 있어 중단합니다." >&2
        exit 1
    fi
done

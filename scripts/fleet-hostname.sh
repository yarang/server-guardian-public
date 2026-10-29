#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Align each host's OS hostname with its inventory id
#
# For every host whose static hostname differs from its id in the hub inventory:
#   - hostnamectl set-hostname <id>        (static hostname wins over DHCP names)
#   - 127.0.1.1 line in /etc/hosts -> <id> (backup: /etc/hosts.bak-guardian-<date>)
#   - /etc/cloud/cloud.cfg.d/99-guardian-preserve-hostname.cfg: preserve_hostname: true
# Hosts that already match are left untouched. No reboot is needed.
#
# Usage:
#   scripts/fleet-hostname.sh [--dry-run] [--offline] [host ...]
#   Default hosts: every host in the hub inventory (fetched read-only over ssh) except those
#   without a 100.64.x Tailscale IP. --offline uses the checkout's configs/servers.json snapshot.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR  # read by lib/inventory.sh
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"

DRY_RUN=0
HOSTS=()
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN=1 ;;
        --offline) export INVENTORY_OFFLINE=1 ;;
        -h|--help) sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "알 수 없는 옵션: ${arg}" >&2; exit 2 ;;
        *) HOSTS+=("${arg}") ;;
    esac
done
if [ ${#HOSTS[@]} -eq 0 ]; then
    inventory_load
    while IFS= read -r h; do HOSTS+=("${h}"); done < <(
        python3 -c 'import json,sys; [print(s["id"]) for s in json.load(open(sys.argv[1])) if str(s.get("tailscale_ip","")).startswith("100.64.")]' \
            "${INVENTORY_FILE}"
    )
fi

read -r -d '' REMOTE <<'EOF' || true
set -eu
target="$1"; dry="$2"
current="$(hostnamectl --static 2>/dev/null || true)"
transient="$(hostname)"
if [ "$current" = "$target" ] && [ "$transient" = "$target" ] && grep -qE "^127\.0\.1\.1[[:space:]]+$target([[:space:]]|$)" /etc/hosts; then
    echo "OK|unchanged"; exit 0
fi
if [ "$dry" = 1 ]; then echo "PLAN|static='${current:-<none>}' transient='$transient' -> '$target'"; exit 0; fi
sudo -n cp -p /etc/hosts "/etc/hosts.bak-guardian-$(date +%Y%m%d)"
sudo -n hostnamectl set-hostname "$target"
if grep -qE "^127\.0\.1\.1[[:space:]]" /etc/hosts; then
    sudo -n sed -i -E "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$target $target/" /etc/hosts
else
    printf '127.0.1.1\t%s %s\n' "$target" "$target" | sudo -n tee -a /etc/hosts >/dev/null
fi
if [ -d /etc/cloud/cloud.cfg.d ]; then
    echo "preserve_hostname: true" | sudo -n tee /etc/cloud/cloud.cfg.d/99-guardian-preserve-hostname.cfg >/dev/null
fi
sudo -n true
echo "CHANGED|'${current:-<none>}'/'$transient' -> '$(hostname)' · hosts: $(getent hosts "$target" | head -n 1 | awk '{print $1}')"
EOF

for h in "${HOSTS[@]}"; do
    printf '%-22s ' "${h}"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "${h}" "bash -s -- '${h}' '${DRY_RUN}'" <<<"${REMOTE}" 2>&1 | tail -n 1
done

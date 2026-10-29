#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Copy the hub's node inventory into the local snapshot
#
# Nodes registered from the dashboard (docs/design/node-registration.md) are written to
# ~/.guardian/data/servers.json on the hub, not to the git checkout. This script fetches that
# file, shows the diff against configs/servers.json and, after you confirm, overwrites the local
# copy (the previous one is kept as configs/servers.json.bak). Nothing is changed on the hub.
# configs/servers.json is only a local snapshot of the hub inventory for the --offline modes of the
# admin scripts: it is git-ignored, so real hosts never enter the repository
# (configs/servers.example.json shows the format). Edits belong on the hub or in the dashboard.
#
# Usage:
#   scripts/pull-inventory.sh [--yes]
#
#   --yes   Overwrite without asking (for scripts); the diff is still printed.
#
# The hub is HUB_HOST, else the host marked "guardian_role": "hub" in the local snapshot, reached
# through the ssh alias of its id (scripts/lib/inventory.sh). HUB_INVENTORY_PATH overrides the remote
# path (default: ~/.guardian/data/servers.json).
# Runs from the admin Mac.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
export ROOT_DIR  # read by lib/inventory.sh
# shellcheck source=lib/inventory.sh
source "${SCRIPT_DIR}/lib/inventory.sh"
REMOTE_PATH="${INVENTORY_REMOTE_PATH}"

ASSUME_YES=0
for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "알 수 없는 인자: ${arg}" >&2; exit 2 ;;
    esac
done

HUB="$(inventory_hub_alias)" || exit 1

tmp="$(mktemp -t sg-inventory.XXXXXX)"
trap 'rm -f "${tmp}"' EXIT

echo "📥 ${HUB}:${REMOTE_PATH} 가져오는 중"
# Fetches over ssh and refuses anything that is not a usable inventory before it can replace
# the local file.
inventory_fetch "${HUB}" "${tmp}" || exit 1

inventory_install_snapshot "${tmp}" "${HUB}:${REMOTE_PATH}" "${ASSUME_YES}"

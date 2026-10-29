#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - install the managed sshd drop-in on this node (run on the node)
#
# scripts/install-agent-service.sh uploads deploy/ssh/00-guardian-hardening.conf to
# ~/.guardian/ssh-hardening.conf and runs this script with `bash -s`.
#
# The drop-in is installed only when it changes something, and only if:
#   - sshd_config includes /etc/ssh/sshd_config.d/ (otherwise the file would be ignored),
#   - the login user is not root (PermitRootLogin no would cut this very access path),
#   - root has no usable authorized key (only cloud "please log in as ubuntu" keys with a
#     forced command) when the drop-in sets PermitRootLogin no.
# After installing it checks `sshd -t` and that `sshd -T` reports every directive with the
# drop-in's value, then reloads sshd (existing sessions stay open). Any failed check puts the
# previous file back (or removes the new one), so sshd keeps its old configuration.
#
# Prints one status line: UNCHANGED | INSTALLED | UPDATED | SKIPPED: <why> | REVERTED: <why>
# Exit codes: 0 unchanged/installed/updated, 3 skipped (unsafe to apply), 4 reverted, 2 missing source file
# (also prints SKIPPED).
# Paths can be overridden for tests: SG_HARDENING_SRC, SG_SSHD_CONFIG, SG_SSHD_DROPIN_DIR,
# SG_ROOT_AUTHORIZED_KEYS.
# ==============================================================================

set -u

SRC="${SG_HARDENING_SRC:-${HOME}/.guardian/ssh-hardening.conf}"
MAIN="${SG_SSHD_CONFIG:-/etc/ssh/sshd_config}"
DIR="${SG_SSHD_DROPIN_DIR:-/etc/ssh/sshd_config.d}"
ROOT_KEYS="${SG_ROOT_AUTHORIZED_KEYS:-/root/.ssh/authorized_keys}"
DST="${DIR}/00-guardian-hardening.conf"

skip() { echo "SKIPPED: $1"; exit 3; }

[ -r "${SRC}" ] || { echo "SKIPPED: ${SRC} not found"; exit 2; }
[ "$(id -u)" != 0 ] || skip "logged in as root; PermitRootLogin no would lock this login out"
# shellcheck disable=SC2016
sudo -n grep -Eiq "^[[:space:]]*Include[[:space:]]+${DIR//./\\.}/" "${MAIN}" \
    || skip "${MAIN} does not include ${DIR}/"

if grep -Eiq '^[[:space:]]*PermitRootLogin[[:space:]]+no([[:space:]]|$)' "${SRC}"; then
    # A root key without a forced command is a real way in; keep root login until it is removed.
    usable="$(sudo -n sh -c "grep -Ev '^[[:space:]]*(#|\$)' '${ROOT_KEYS}' 2>/dev/null" | grep -vc 'command=' || true)"
    [ "${usable:-0}" = 0 ] || skip "${usable} root authorized key(s) without a forced command; remove them or install with --no-ssh-hardening"
fi

if sudo -n cmp -s "${SRC}" "${DST}"; then echo "UNCHANGED"; exit 0; fi

had=0
if sudo -n test -e "${DST}"; then had=1; sudo -n cp -p "${DST}" "${DST}.bak" || skip "cannot back up ${DST}"; fi
revert() {
    if [ "${had}" = 1 ]; then sudo -n mv -f "${DST}.bak" "${DST}"; else sudo -n rm -f "${DST}"; fi
    echo "REVERTED: $1"
    exit 4
}
sudo -n install -m 0644 "${SRC}" "${DST}" || revert "cannot write ${DST}"

sudo -n sshd -t >/dev/null 2>&1 || revert "sshd -t rejected the configuration"
effective="$(sudo -n sshd -T 2>/dev/null)" || revert "sshd -T failed"
while read -r key value _; do
    case "${key}" in ''|\#*) continue ;; esac
    k="$(printf '%s' "${key}" | tr '[:upper:]' '[:lower:]')"
    want="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
    got="$(printf '%s\n' "${effective}" | awk -v k="${k}" '$1 == k { print $2; exit }')"
    [ "${got}" = "${want}" ] || revert "${key} is '${got}' in sshd -T (expected ${value}); another file sets it first"
done < "${SRC}"

if ! { sudo -n systemctl reload ssh 2>/dev/null || sudo -n systemctl reload sshd 2>/dev/null; }; then
    revert "could not reload sshd"
fi
sudo -n rm -f "${DST}.bak"
if [ "${had}" = 1 ]; then echo "UPDATED"; else echo "INSTALLED"; fi

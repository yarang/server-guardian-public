#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - Move the hub's fleet SSH key out of reach of the ubuntu user
#
# Runs ON THE HUB as ubuntu (piped through `ssh <hub> bash -s` by
# scripts/install-agent-service.sh and scripts/setup-hub-ssh.sh). Idempotent.
#
# 1. The private key goes to /etc/guardian/fleet_key (root:root 0600). An existing
#    ~/.ssh/guardian_fleet is migrated; with neither, a new key is generated there.
# 2. ~/.ssh/guardian_fleet.pub stays, so ssh can pick the key out of the agent.
# 3. guardian-fleet-agent.service (installed by the caller) holds the key in an
#    ssh-agent on /run/guardian-fleet/agent.sock; the host blocks in ~/.ssh/guardian_fleet.conf
#    (included from ~/.ssh/config) point every fleet host at that socket with IdentityAgent.
# 4. Only after the agent answers with the key is ~/.ssh/guardian_fleet removed.
#
# Prints the public key on its last line.
# ==============================================================================

set -euo pipefail

KEY=/etc/guardian/fleet_key
SOCK=/run/guardian-fleet/agent.sock
OLD="${HOME}/.ssh/guardian_fleet"
PUB="${OLD}.pub"
MARKER="guardian-hub-fleet"

sudo -n install -d -m 700 -o root -g root /etc/guardian
if ! sudo -n test -f "${KEY}"; then
    if [ -f "${OLD}" ]; then
        echo "  기존 키를 ${KEY}로 옮깁니다" >&2
        sudo -n install -m 600 -o root -g root "${OLD}" "${KEY}"
    else
        echo "  ${KEY}에 새 키를 만듭니다" >&2
        sudo -n ssh-keygen -q -t ed25519 -N "" -C "${MARKER}@$(hostname)" -f "${KEY}"
        sudo -n rm -f "${KEY}.pub"
    fi
fi
sudo -n chown root:root "${KEY}" && sudo -n chmod 600 "${KEY}"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
sudo -n ssh-keygen -y -f "${KEY}" | awk -v c="${MARKER}@$(hostname)" '{print $1, $2, c}' > "${PUB}.tmp"
chmod 644 "${PUB}.tmp" && mv "${PUB}.tmp" "${PUB}"

# IdentityFile names the public key, so ssh offers exactly that key from the agent.
if [ -f ~/.ssh/config ] && grep -q "# BEGIN ${MARKER}" ~/.ssh/config; then
    python3 - "${HOME}/.ssh/config" "${MARKER}" "${SOCK}" <<'PY'
import sys
path, marker, sock = sys.argv[1:]
out, inside = [], False
for line in open(path).read().splitlines():
    if line.startswith(f"# BEGIN {marker}"):
        inside = True
    elif line.startswith(f"# END {marker}"):
        inside = False
    if inside and line.strip().startswith("IdentityAgent "):
        continue
    if inside and line.strip().startswith("IdentityFile "):
        out += ["    IdentityFile ~/.ssh/guardian_fleet.pub", f"    IdentityAgent {sock}"]
        continue
    out.append(line)
open(path + ".tmp", "w").write("\n".join(out) + "\n")
PY
    chmod 600 ~/.ssh/config.tmp && mv ~/.ssh/config.tmp ~/.ssh/config
fi

sudo -n systemctl daemon-reload
sudo -n systemctl enable -q guardian-fleet-agent.service
sudo -n systemctl restart guardian-fleet-agent.service
for _ in $(seq 30); do
    SSH_AUTH_SOCK="${SOCK}" ssh-add -l >/dev/null 2>&1 && break
    sleep 0.2
done
want="$(ssh-keygen -lf "${PUB}" | awk '{print $2}')"
if ! SSH_AUTH_SOCK="${SOCK}" ssh-add -l 2>/dev/null | grep -qF "${want}"; then
    echo "❌ fleet ssh-agent에 키가 올라가지 않았습니다 (~/.ssh/guardian_fleet는 그대로 둡니다)" >&2
    sudo -n systemctl status guardian-fleet-agent.service --no-pager -n 20 >&2 || true
    exit 1
fi
rm -f "${OLD}"
echo "  fleet ssh-agent: ${want}" >&2
cat "${PUB}"

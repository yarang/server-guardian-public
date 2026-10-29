# 2026-10-05 fleet sshd hardening (MaxAuthTries, PermitRootLogin)

English | [한국어](2026-10-05-fleet-sshd-hardening.ko.md)

> Point-in-time record from 2026-10-05; the code has changed since.

| Item | Details |
|---|---|
| Time | 2026-10-05 12:50–14:30 UTC |
| Targets | 23 fleet nodes (24 in servers.json, edge-mini excluded) |
| Path | `POST /api/fleet/exec` on the hub site-a-arm1 (with `targets`), using the fleet SSH key |
| Basis | Per-node SSH audit `GET /api/fleet/ssh` (SEC-SSH-004 failing on 23, SEC-SSH-001 warning on 23) |
| Approval | The operator (owner) approved directly in the conversation. The debate and action approval flow (decision DB) was not used |
| Result | Fleet average SSH score 75 → 95, no failures or warnings |

## Background

After the per-node SSH audit (`879e881`, `c64a604`) was deployed to the hub, the whole fleet was audited for the first
time. All 23 nodes read the effective settings with `sshd -T (sudo)` (`ok`), and the results were the same.

| Rule | Before | After |
|---|---|---|
| SEC-SSH-001 no root login | warning 23 (`without-password` 22, `prohibit-password` 1) | pass 23 |
| SEC-SSH-002 key authentication only | pass 23 | pass 23 |
| SEC-SSH-003 public key authentication | pass 23 | pass 23 |
| SEC-SSH-004 MaxAuthTries ≤ 4 | fail 23 (value 6) | pass 23 (value 4) |
| SEC-SSH-005 non-standard port | INFO 23 (22) | INFO 23 (unchanged) |

## Change

One new managed file on each node. The existing `/etc/ssh/sshd_config` and other drop-ins were not modified.

`/etc/ssh/sshd_config.d/00-guardian-hardening.conf` (0644)
```
# Managed by server-guardian (fleet SSH audit SEC-SSH-004)
MaxAuthTries 4
PermitRootLogin no
```

Every node's `sshd_config` reads `Include /etc/ssh/sshd_config.d/*.conf` at the top, and sshd uses the first value it
reads. The file name starts with `00-` so that it applies before other drop-ins such as cloud-init's.

## Procedure

### 1. MaxAuthTries 4 (SEC-SSH-004)

The following ran on each node; if anything did not match, it removed the file and stopped.

```sh
F=/etc/ssh/sshd_config.d/00-guardian-hardening.conf
grep -Eiq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config || { echo "NO_INCLUDE"; exit 3; }
if sudo test -e "$F"; then echo "EXISTS"; sudo cat "$F"; exit 4; fi
printf '# Managed by server-guardian (fleet SSH audit SEC-SSH-004)\nMaxAuthTries 4\n' | sudo tee "$F" >/dev/null
sudo chmod 644 "$F"
if ! sudo sshd -t; then sudo rm -f "$F"; echo "SYNTAX_FAIL_REVERTED"; exit 5; fi
V=$(sudo sshd -T 2>/dev/null | awk '/^maxauthtries /{print $2}')
if [ "$V" != "4" ]; then sudo rm -f "$F"; echo "NOT_EFFECTIVE_${V}_REVERTED"; exit 6; fi
sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
echo "OK maxauthtries=$V"
```

1. Canary: applied on site-b-ec2 → `OK maxauthtries=4`, re-audit (new SSH connection) 75 → 85
2. Applied on the other 22 nodes (hub included) → all 22 `OK maxauthtries=4`
3. Fleet re-audit (`refresh=1`, 13:42 UTC): 23 nodes at 85, no failures

### 2. Pre-check: is root used?

A read-only check for connections that `PermitRootLogin no` would cut off.

- Inventory login accounts: `ubuntu` on 23 nodes, `owner` on 1 (edge-mini). No root.
- No root connections in the repository's scripts, deploy or configs.
- Per-node `/root/.ssh/authorized_keys`: one key on each of the 23 nodes, all of them the cloud default blocking key
  with the options `no-port-forwarding,no-agent-forwarding,no-X11-forwarding,command=...` (prints a notice on login).
- Root login records (`Accepted ... for root` in `/var/log/auth.log*` and the last 14 days of the sshd journal): 0 on
  all 23 nodes.

### 3. PermitRootLogin no (SEC-SSH-001)

One line was added to the managed file, with a backup that is restored if verification fails.

```sh
F=/etc/ssh/sshd_config.d/00-guardian-hardening.conf
sudo test -e "$F" || { echo "NO_MANAGED_FILE"; exit 3; }
if sudo grep -qi '^PermitRootLogin' "$F"; then echo "ALREADY_SET"; exit 4; fi
sudo cp -p "$F" "$F.bak"
printf 'PermitRootLogin no\n' | sudo tee -a "$F" >/dev/null
if ! sudo sshd -t; then sudo mv "$F.bak" "$F"; echo "SYNTAX_FAIL_REVERTED"; exit 5; fi
V=$(sudo sshd -T 2>/dev/null | awk '/^permitrootlogin /{print $2}')
if [ "$V" != "no" ]; then sudo mv "$F.bak" "$F"; echo "NOT_EFFECTIVE_${V}_REVERTED"; exit 6; fi
sudo rm -f "$F.bak"
sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
echo "OK permitrootlogin=$V"
```

1. Canary: applied on site-b-ec2 → `OK permitrootlogin=no`, re-audit 85 → 95 (14:24 UTC)
2. Applied on the other 22 nodes → all 22 `OK permitrootlogin=no`
3. Fleet re-audit (`refresh=1`): 23 nodes at 95, SEC-SSH-001 to 004 all pass

No node was reverted in either step. After each step, a new SSH connection from the hub (the audit) and a new SSH
connection from the operator's PC to the hub were confirmed to work. `reload` does not drop existing SSH sessions.

## Impact

- An SSH client that tries more than 4 keys in turn may be disconnected before it reaches the right key (for example a
  laptop with many keys in ssh-agent). Set `IdentitiesOnly yes` and `IdentityFile` for that node.
- SSH login as root is no longer possible. For root work, log in as `ubuntu` and use `sudo`.

## Rollback

On the node:
```sh
sudo rm /etc/ssh/sshd_config.d/00-guardian-hardening.conf
sudo sshd -t && sudo systemctl reload ssh
```
To roll back one item only, delete that line from the file and run the same check and reload.

## Exclusions and remaining items

- **edge-mini**: the host name `edge-mini` does not resolve on the hub, so it was left out of the audit and the change
  (`unreachable`). Deferred by the operator's decision.
- **SEC-SSH-005 (port 22)**: not changed. The nodes are used inside Tailscale, and changing the port would mean
  changing servers.json, the OCI security lists and every client configuration together: a high cost for the benefit.
- **New nodes and nodes registered from the dashboard (manual step)**: the hub agent is installed on the hub only
  (`scripts/install-agent-service.sh` refuses nodes that are not the hub), so this script does not touch the nodes'
  sshd. The installer applies the same file (`deploy/ssh/00-guardian-hardening.conf`) to the hub itself only (turn off
  with `--no-ssh-hardening`). The operator applies it to nodes directly over SSH from the hub (no new automation):
  ```bash
  # In ~/server-guardian on the hub, for each node <node> (apply-ssh-hardening.sh checks with sshd -t/-T and reverts on failure)
  ssh <node> 'mkdir -p ~/.guardian && cat > ~/.guardian/ssh-hardening.conf' < deploy/ssh/00-guardian-hardening.conf
  ssh <node> 'bash -s' < deploy/ssh/apply-ssh-hardening.sh   # last line: UNCHANGED | INSTALLED | UPDATED | SKIPPED | REVERTED
  ```
  Running these commands again on the 23 nodes already changed updates the file to the repository version, which
  differs only in its comment (`UPDATED`). Confirmed on site-b-ec2: first run `UPDATED`, second run `UNCHANGED`,
  effective values unchanged.

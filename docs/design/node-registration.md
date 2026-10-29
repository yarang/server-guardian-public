# Node registration through the hub's SSH config

English | [한국어](node-registration.ko.md)

Status: accepted (2026-10-07), backend and dashboard implemented (sections 6-8 describe the shipped API). Scope: `apps/agent`, `apps/web-desktop`, `scripts/setup-hub-ssh.sh`.

## 1. Why

The hub agent already connects with `ssh -- <id> <cmd>` (`sshArgs`), so the hub's `~/.ssh/config`
decides where a node is. `configs/servers.json` repeats `hostname`, `user`, `port`, `key` and
`tailscale_ip`, and `setup-hub-ssh.sh` copies them one way into a managed block of the hub's
`~/.ssh/config`. Registering a node means editing JSON on the Mac, running scripts and redeploying.

Decision: connection settings live in one place, a fleet file in SSH config syntax that the hub
owns. `servers.json` keeps the node list (the allow-list for commands) and the metadata that group
rules match on. Nodes are registered from the dashboard.

## 2. Files on the hub

- `~/.ssh/guardian_fleet.conf`: one `Host` block per node, written only by the agent (and by
  `setup-hub-ssh.sh` during migration). Mode 600. Path override: `GUARDIAN_SSH_FLEET_FILE`.
- `~/.ssh/config`: contains `Include ~/.ssh/guardian_fleet.conf` near the top (Include must precede
  any `Host` block to apply globally). The agent never writes anything else in this file; if the
  Include line is missing it reports it and the UI shows the one line to add.
- `~/.guardian/data/servers.json`: the hub's inventory, same format as `configs/servers.json`,
  one entry per node keyed by `id`, which equals the `Host` alias. The hub sets
  `GUARDIAN_SERVERS_FILE` to it in `agent.env` (the debate CLI inherits the path), so the agent
  never writes into the release it runs from (`~/.guardian/current`, read-only since install-tracks R1). Connection fields stay optional for compatibility; the agent prefers the fleet file's
  values when the node has a block there.
- `configs/servers.json` in the repository becomes the seed and the admin Mac's copy. Scripts on the
  Mac keep reading it; `scripts/pull-inventory.sh` copies the hub's inventory back so registrations
  made in the dashboard can be reviewed and committed.
  (2026-10-18: `configs/servers.json` is now a local snapshot that git ignores, so real nodes never
  enter the repository; `configs/servers.example.json` shows the format. The admin scripts read the
  hub's inventory over ssh by default and use the snapshot only with `--offline`;
  `scripts/pull-inventory.sh` refreshes the snapshot, nothing is committed.)

Each block the agent writes for a node registered from the dashboard has exactly this shape:

```
Host <id>
    HostName <host>
    User <user>
    Port <port>
    IdentityFile ~/.ssh/guardian_fleet.pub
    IdentityAgent /run/guardian-fleet/agent.sock
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    UserKnownHostsFile ~/.ssh/guardian_known_hosts
    HostKeyAlias <id>
```

`StrictHostKeyChecking yes` with the host key pinned at registration (section 4) replaces
`accept-new` for registered nodes. Blocks migrated by `setup-hub-ssh.sh` have the same first seven
lines followed by `StrictHostKeyChecking accept-new` and no `UserKnownHostsFile`/`HostKeyAlias`
(the "migrated" shape); they keep `accept-new` until the host key is re-confirmed (an update that
changes hostname or port, or a `PUT` with only `host_key_fingerprint` (section 6), which rewrites the
block in the registered shape). The agent recognises
exactly these two shapes; any other block is "unmanaged" (section 3).

### Host key storage: why `HostKeyAlias`

With `HostName` set, ssh looks the server's key up under the *HostName* (and `[host]:port` for a
non-22 port), not under the `Host` alias. A pinned entry therefore breaks silently whenever the
address or port changes, and the lookup name depends on the port. Verified against OpenSSH:
with `HostKeyAlias <id>` the lookup name is exactly `<id>` (plain, no `[host]:port` form even for
port 2222), and an entry stored as `[127.0.0.1]:2299` is not found. So the agent stores the pinned
key in `~/.ssh/guardian_known_hosts` as

```
<id> <keytype> <base64 key>
```

and the block carries `HostKeyAlias <id>`. Only the key whose fingerprint the operator confirmed is
stored (not every key the scan returned). Removing a node deletes the lines whose host field is
exactly `<id>`; other lines (and `@cert-authority`/`@revoked` markers) are never touched.

## 3. Validation (security boundary)

A dashboard session must not be able to turn the fleet file into command execution on the hub
(`ProxyCommand`, `LocalCommand`, `Match exec`, newline injection). Rules:

- Only `HostName`, `User`, `Port` come from the request; every other line is fixed by the agent.
- `id`: `^[a-z0-9][a-z0-9-]{0,62}$`, unique across the fleet file and `servers.json`.
- `HostName`: an IPv4 address, or a DNS name `^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$`. No whitespace, `%`, quotes or leading `-`.
- `User`: `^[a-z_][a-z0-9_-]{0,31}$`. `Port`: integer 1..65535.
- The file is parsed before every write; a block not matching the fixed shape (hand edits) is
  preserved byte for byte but marked `unmanaged` and never rewritten.
- Writes are atomic (temp file + rename), under the fleet lock (a process mutex plus an advisory
  `flock` on `guardian_fleet.conf.lock`, which `setup-hub-ssh.sh` takes too), and keep
  `guardian_fleet.conf.bak`.
- After a write the agent runs `ssh -G <id>` and checks the effective `hostname`, `user`, `port`
  equal the request; otherwise it restores the backup and fails.

Metadata fields (`tier`, `role`, `guardian_role`, `cli_support`, `tailscale_tags`, `description`)
are validated as in the group rule catalog: short strings, lists of short strings. `guardian_role`
values are `hub` (reserved, exactly one; it cannot be set or cleared from the UI) and `node`; older
inventories may still hold `agent`, which behaves like `node` (every consumer only tests for `hub`).

## 4. Registration flow

API (all behind the existing session guard, JSON):

| Step | Endpoint | Effect |
|---|---|---|
| Hub facts | `GET /api/nodes/registration` | hub fleet public key line (`from="<hub tailscale ip>",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 ...`), whether `~/.ssh/config` includes the fleet file, fleet file path |
| Check | `POST /api/nodes/check` `{id, hostname, user, port}` | validates, then `ssh-keyscan -T 5 -p <port> <hostname>`; returns host key fingerprints (SHA256) — writes nothing |
| Register | `POST /api/nodes` `{id, hostname, user, port, host_key_fingerprint, metadata}` | rescans and requires the fingerprint to match; writes the host key to `~/.ssh/guardian_known_hosts` (referenced by `UserKnownHostsFile` in the block), the `Host` block, and the `servers.json` entry; then probes the node (`ssh <id>` running `probeCommand`: `uname -m` plus a passwordless-sudo check) |
| Update | `PUT /api/nodes/{id}` | changes connection fields and/or metadata with the same validation |
| Remove | `DELETE /api/nodes/{id}` | removes the block, the known host and the `servers.json` entry; refuses the hub |
| Test again | `POST /api/nodes/{id}/probe` | re-runs the probe for an inventory node (addition: backs the *Test again* button) |

The `Host` block therefore gains `UserKnownHostsFile ~/.ssh/guardian_known_hosts` and `HostKeyAlias <id>` (section 2).

Probe result: if `ssh <id>` fails with `Permission denied (publickey)` the node is registered but
reported `key not installed`; the UI shows the public key line to append to the node's
`~/.ssh/authorized_keys` (or `scripts/setup-hub-ssh.sh <id>` from the admin Mac) and a *Test again*
button. `arch` is taken from the probe, not typed.

The probe command (`probeCommand` in `apps/agent/nodes_handler.go`) is fixed:

```
uname -m; if sudo -n true 2>/dev/null; then echo sudo=yes; else echo sudo=no; fi
```

Besides `arch` it reports whether the node's login user has passwordless sudo (`probe.sudo`, `yes`
or `no`), because the cleanup actions of ops projects need `sudo -n` for journal, apt and kernel work
([server-ops-project.md](server-ops-project.md)). The dashboard shows it as a chip next to the probe
result.

UI (`#/nodes/new`, from an *Add node* button on Nodes): connection form -> *Check* shows the host
key fingerprint to confirm -> metadata form -> *Register* -> probe result with the key hint when
needed -> link to the node page. Node detail gets *Edit connection* and *Remove node* (typed-id
confirmation; disabled for the hub).
(2026-10-18: the wizard is at `#/fleet/nodes/new`, the *Add node* button is on the Fleet Nodes tab and
node pages are `#/fleet/nodes/<id>`; the old `#/nodes/...` hashes still resolve. Node detail also has
*Pin host key* and *Revoke hub key*; *Remove node* asks to revoke first or to tick an explicit skip.)

## 5. Migration

0. `install-agent-service.sh --hub` seeds `~/.guardian/data/servers.json` from the repository copy
   only when it does not exist yet (never overwrites dashboard registrations) and writes
   `GUARDIAN_SERVERS_FILE` into `agent.env`.
   (2026-10-18: the call is `install-agent-service.sh <hub-id>`; there is no `--hub` flag. The seed
   source is the hub's own inventory, or the local `configs/servers.json` snapshot with `--offline`.)
1. `setup-hub-ssh.sh` writes `~/.ssh/guardian_fleet.conf` (same per-node content as the old managed
   block, the migrated shape), puts `Include ~/.ssh/guardian_fleet.conf` at the top of
   `~/.ssh/config` and removes the old `# BEGIN guardian-hub-fleet` block (the first change keeps
   `~/.ssh/config.pre-guardian-fleet`). It is idempotent and only ever replaces blocks of the
   migrated shape: dashboard-registered (pinned) blocks and hand-written blocks in the fleet file are
   kept. `--revoke` (without explicit nodes) removes the migrated blocks it wrote and the legacy
   block; the Include line goes only when the fleet file is left without any Host block (the file is
   deleted then). `scripts/pull-inventory.sh` copies the hub's inventory back to `configs/servers.json`
   (the local, git-ignored snapshot).
2. The agent reads `HostName`/`User`/`Port` for `/api/nodes` from the fleet file when present
   (else falls back to `servers.json` fields), so nothing changes for nodes until migrated.
3. Later: drop connection fields from `servers.json` once all scripts read the fleet file.

Nothing is deployed to the hub as part of the code change; deployment is a separate, confirmed step.

## 6. API reference (what the frontend calls)

All routes are under `/api`, behind the same bearer token or session cookie as the rest of the API,
JSON in and out (`Content-Type: application/json` on requests with a body, body limit 64 KB, unknown
request fields are rejected with 400). A browser client must be allowed by the CORS list as for any
other call (`PUT` is now allowed in preflights).

### Reserved ids

`new`, `registration`, `check` and `probe` cannot be node ids (they are API path segments or dashboard
routes): `400 invalid_field` with `field: "id"`, on `check`, register, `PUT`, `DELETE` and `probe`.

### Errors

Every failure has this shape; `code` is stable, `error` is for display, `field` names the request
field when the failure is about one (use it to mark the input), `hint` is an optional action, and
some codes carry extra keys.

```json
{"error": "hostname: must be an IPv4 address or a DNS name (letters, digits, dots and dashes)",
 "code": "invalid_field", "field": "hostname"}
```

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_json` | body is not valid JSON, has unknown fields or trailing data |
| 400 | `invalid_field` | validation failed (`field` is e.g. `id`, `hostname`, `user`, `port`, `host_key_fingerprint`, `metadata.tier`, `metadata.guardian_role`) |
| 400 | `fingerprint_required` | register, or an update that changes hostname/port, without `host_key_fingerprint` |
| 403 | `hub_protected` | `DELETE` of the hub |
| 404 | `not_found` | unknown node id |
| 409 | `include_missing` | `~/.ssh/config` does not include the fleet file before its first `Host`/`Match`; `hint` holds the line to add |
| 409 | `inventory_not_configured` / `inventory_missing` | `GUARDIAN_SERVERS_FILE` is unset or the file does not exist, so the inventory is read-only |
| 409 | `id_exists` | id (or name) already in `servers.json` or the fleet file |
| 409 | `fleet_busy` | another process (e.g. `setup-hub-ssh.sh`) held the fleet lock for 10 seconds; `hint` says to retry |
| 409 | `unmanaged_block` | the fleet file has a hand-edited or differently shaped `Host` block for this alias; the agent will not touch it |
| 409 | `fingerprint_mismatch` | the node now presents other keys than the confirmed fingerprint; the response has `keys` (same shape as in `check`) |
| 500 | `verify_failed` | `ssh -G <id>` did not resolve to the requested hostname/user/port; **all files were restored** |
| 500 | `write_failed` | a file write failed; files were restored |
| 500 | `fleet_lock` | the fleet lock file could not be created, opened or locked |
| 502 | `unreachable` / `scan_failed` | `ssh-keyscan` received no host key (`hint` has the stderr tail) or could not run |

### GET /api/nodes/registration

```json
{
  "fleet_file": "/home/ubuntu/.ssh/guardian_fleet.conf",
  "known_hosts_file": "/home/ubuntu/.ssh/guardian_known_hosts",
  "ssh_config_file": "/home/ubuntu/.ssh/config",
  "include_line": "Include ~/.ssh/guardian_fleet.conf",
  "include_present": true,
  "include_effective": true,
  "inventory_file": "/home/ubuntu/.guardian/data/servers.json",
  "inventory_writable": true,
  "hub_tailscale_ip": "100.64.0.7",
  "fleet_public_key": "from=\"100.64.0.7\",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3Nza... guardian-hub-fleet@site-a-arm1",
  "managed_nodes": ["oci-a", "edge-1"],
  "unmanaged_hosts": ["bastion"],
  "blockers": [],
  "can_register": true
}
```

`include_present` means some `Include` names the fleet file; `include_effective` means it comes before
the first `Host`/`Match` line (otherwise it does not apply to every `Host` block). `blockers` lists, as plain
sentences, what prevents registering now (missing Include line, read-only inventory); `can_register` is
`blockers` being empty. `validation` (not shown above) is a machine-readable copy of the section 8 rules
(`nodeFormRules`: patterns, limits, reserved ids) so the dashboard form can mirror the hub agent; the hub
agent stays authoritative. When the hub's public key file is missing or the hub has no `tailscale_ip`,
`fleet_public_key` is empty (never an unrestricted line) and `fleet_public_key_blocker` (also `fleet_public_key_note`)
says why; the probe response then carries `authorized_key_blocker` instead of `authorized_key`. `unmanaged_hosts` are
`Host` patterns of hand-written blocks the agent leaves alone.

### POST /api/nodes/check

Request `{"id": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22}` (`port` defaults to 22).
Validates, then `ssh-keyscan -T 5 -p <port> <hostname>`; writes nothing. `id_exists` is informational
(the same call is used before changing an existing node's hostname/port).

```json
{
  "ok": true, "id": "edge-1", "hostname": "203.0.113.9", "port": 22, "id_exists": false,
  "keys": [
    {"type": "ssh-ed25519", "fingerprint": "SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk"},
    {"type": "ecdsa-sha2-nistp256", "fingerprint": "SHA256:L+5UBANOVSvPSFxRbAg3IvTiKCDBqHV66Pu4fG4X/D0"}
  ]
}
```

The operator confirms one fingerprint (compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`
on the node); that exact string is sent as `host_key_fingerprint`.

### POST /api/nodes -> 201

Request:

```json
{
  "id": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22,
  "host_key_fingerprint": "SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk",
  "metadata": {
    "tier": "Tier-2", "role": "ssh_target_node", "guardian_role": "node",
    "cli_support": ["ssh_agent_remote_exec"], "tailscale_tags": ["tag:fleet"],
    "description": "Edge box"
  }
}
```

`metadata` and each of its keys are optional (`guardian_role` defaults to `node`; `hub` is refused).
Allowed values: `tier` 1-32 chars of letters, digits, space and `._:-`; `role`, `guardian_role` and
`cli_support` entries `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`; `tailscale_tags` entries `tag:<lowercase
letters, digits, dashes>`; lists at most 16 entries; `description` at most 500 characters, no line
breaks. `name` is set to the id; `arch` cannot be typed (it comes from the probe). The hub agent rescans the
node and requires the fingerprint to match one of the keys it presents. Response:

```json
{
  "ok": true,
  "node": {
    "id": "edge-1", "name": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22,
    "arch": "aarch64", "group": "", "key": "", "recommended": false, "description": "Edge box",
    "tier": "Tier-2", "role": "ssh_target_node", "guardian_role": "node",
    "cli_support": ["ssh_agent_remote_exec"], "tailscale_tags": ["tag:fleet"],
    "connection": "fleet_file", "host_key_mode": "pinned"
  },
  "probe": {"status": "ok", "reachability": "ok", "arch": "aarch64", "sudo": "yes", "duration_ms": 412}
}
```

`probe.status` is `ok`, `key_not_installed` (`Permission denied`) or `unreachable` (every other failure);
`stderr` (last 400 characters, one line) is present when it is not `ok`. `probe.reachability` is the shared
ssh classification (`ok`, `auth_failed`, `host_key_changed`, `unreachable`, `command_failed`;
`apps/agent/ssh_classify.go`) that fleet diagnosis, command execution and the SSH audit also use. `probe.sudo`
is `yes` or `no` (passwordless sudo of the login user) when the probe succeeded, absent otherwise. For `key_not_installed` the response also has
`"authorized_key": "from=\"100.64.0.7\",no-agent-forwarding,... ssh-ed25519 AAAA... guardian-hub-fleet@..."`
(the line to append to the node's `~/.ssh/authorized_keys`). The node stays registered whatever the probe
says. `warnings` (list of strings) appears only if storing `arch` failed.

### PUT /api/nodes/{id} -> 200

Every field is optional; the body is merged into the current values.

```json
{
  "hostname": "198.51.100.4", "user": "deploy", "port": 2222,
  "host_key_fingerprint": "SHA256:...",
  "metadata": {"tier": "Tier-1", "description": null, "cli_support": ["claude"]}
}
```

- Metadata keys present replace the value; `null` (or `""`/`[]`) removes the key from the inventory
  entry; absent keys are kept. `guardian_role` of the hub cannot change or be cleared and no other node can
  become `hub` (400, `field: "metadata.guardian_role"`).
- **Re-confirm the host key (pin a node):** `PUT /api/nodes/{id}` with only
  `{"host_key_fingerprint": "SHA256:..."}` (from a `check` of the node's current address). The hub agent
  rescans, requires the fingerprint to match, stores that key under the `HostKeyAlias` in
  `guardian_known_hosts`, and rewrites the node's block in the registered (pinned) shape, then verifies
  with `ssh -G` (rollback on mismatch) and probes. It works for (a) a migrated `accept-new` block (hostname,
  user and port stay as they are) and (b) an `inventory` node without a fleet block: the block is created
  from `hostname`/`user`/`port` in the request if given, else from the inventory entry (`tailscale_ip` when
  it starts with `100.64.`, otherwise `hostname`; `user` defaults to `ubuntu`, `port` to 22), validated like
  any other input (400 with `field: "hostname"` when nothing usable exists). Sending it for an already pinned
  node replaces the key (rotated host key). An unmanaged block is refused with 409 as always. Response:

  ```json
  {"ok": true,
   "node": {"id": "oci-a", "hostname": "100.64.0.5", "user": "ubuntu", "port": 22,
            "connection": "fleet_file", "host_key_mode": "pinned", "...": "..."},
   "probe": {"status": "ok", "reachability": "ok", "arch": "aarch64", "sudo": "yes", "duration_ms": 380}}
  ```

- Changing `hostname` or `port` (or giving a node that has no fleet block yet its first connection)
  requires `host_key_fingerprint` from a fresh `check` for the new destination; the pinned key for the id
  is replaced. Changing only `user`, or sending identical values, needs none (a migrated block keeps
  its shape and trust mode).
- A connection edit of a node whose fleet block is unmanaged is refused (`unmanaged_block`); metadata
  edits only touch `servers.json` and still work.

Response: `{"ok": true, "node": {...}}` plus `"probe": {...}` (same shape as above, and `authorized_key`
when the key is missing) when the connection was written.

### DELETE /api/nodes/{id} -> 200

```json
{"ok": true, "id": "edge-1", "removed": {"fleet_block": true, "known_hosts": true, "inventory": true}}
```

The hub is refused (403). A node that only exists in `servers.json` loses just that entry
(`fleet_block: false`). Unmanaged blocks are never removed (409).

Delete procedure: revoke the hub key first, then delete. Deleting only removes the hub's own records;
the hub public key stays in the node's `~/.ssh/authorized_keys` until it is revoked. Revoking needs
the fleet SSH path, which delete removes, so the order is revoke, then delete.

### GET /api/nodes/{id}/revoke-key -> 200

Preview, without contacting the node (unknown node 404, missing or unusable hub public key 409
`hub_key_missing` / `hub_key_invalid`):

```json
{"node": "edge-1", "target": "ubuntu@203.0.113.9[:port]", "fingerprint": "SHA256:...",
 "match": "ssh-ed25519 AAAA...", "note": "..."}
```

`match` is the `<type> <base64>` of the hub fleet public key (`GUARDIAN_FLEET_PUBKEY_FILE`, the same
file the install-key line is built from). `port` is shown only when it is not 22.

### POST /api/nodes/{id}/revoke-key -> 200

Body `{"confirm_id": "<id>"}`; it must equal the node id (400 `confirm_mismatch` otherwise, no ssh is started). Under the
fleet lock the agent connects over the fleet key (same ssh path and host-key policy as probe) and runs
a fixed script that removes, from `~/.ssh/authorized_keys` on the node, only lines containing the exact
`match` text. The key is validated as `[a-z0-9@.-]` type plus base64 body before it is placed in a
single-quoted constant; no free text reaches the shell. The script keeps a backup
`authorized_keys.guardian-bak-<timestamp>` next to the file, writes a copy that keeps owner and mode,
then `mv`s it into place.

```json
{"ok": true, "removed": 1, "hub_key_revoked_at": "2026-10-12T09:30:00Z",
 "note": "After this the hub can no longer reach the node over SSH; the next step is to delete the node."}
```

On success (also when `removed` is 0, meaning the key is not installed) the agent stores
`hub_key_revoked_at` (RFC3339, UTC) in the node's inventory entry; if that write fails the response
carries a `warning` instead. The field is also returned for every node by `GET /api/nodes` and in the
`node` object of register/update, as a string or `null` while the hub key is still believed installed.
The dashboard uses it to require revoke-first (or an explicit skip) before Remove. `DELETE` itself is
unchanged and does not check it.

The hub's own entry is refused for both GET and POST: 400
`{"error": "refusing to revoke the hub's key on the hub itself", "code": "hub_protected"}`.

Every POST is written to the audit log (route `/api/nodes/{id}/revoke-key`, `targets: [id]`, `status`
`ok` or `error`, `detail` `revoke hub key: removed=<n>` or the failure).

When nothing matched: `removed: 0`, nothing changed, and the note says so. Unreachable or failed ssh:
502 `{"error": "...", "reachability": "unreachable|auth_failed|host_key_changed|command_failed"}`.

### POST /api/nodes/{id}/probe -> 200

No body. For any node in the inventory:
`{"ok": true, "id": "edge-1", "probe": {"status": "key_not_installed", "reachability": "auth_failed", "stderr": "...", "duration_ms": 90}, "authorized_key": "..."}`.
A successful probe stores `arch` in the inventory entry.

### GET /api/nodes (changed)

Each node additionally carries:

- `connection`: `"fleet_file"` when the node has a managed block in the fleet file (then `hostname`,
  `user` and `port` are the fleet file's values) or `"inventory"` (the `servers.json` values).
- `host_key_mode` (fleet file nodes only): `"pinned"` (registered from the dashboard) or `"accept-new"`
  (migrated, trust on first use). The UI can offer *Re-confirm host key* for the latter.

## 7. Behaviour worth knowing

- **Order and rollback.** A write happens under the fleet lock (process mutex plus `flock` on
  `<fleet file>.lock`, shared with `setup-hub-ssh.sh`; after 10 seconds of waiting the call fails with
  `fleet_busy`): pinned key, fleet block, inventory
  entry (each atomic: temp file in the same directory, mode 600, rename; the previous content stays
  as `<file>.bak`). Then `ssh -G -- <id>` must report the requested `hostname`, `user`, `port` (and
  `hostkeyalias` for the registered shape). Otherwise every file is restored to the byte-exact previous
  content (or removed if it did not exist) and the call fails with `verify_failed`. The probe runs after
  the lock is released and never rolls anything back.
- **Where the hub agent writes.** `GUARDIAN_SSH_FLEET_FILE` (default `~/.ssh/guardian_fleet.conf`),
  `GUARDIAN_SSH_KNOWN_HOSTS` (default `~/.ssh/guardian_known_hosts`), `GUARDIAN_SSH_CONFIG` (default
  `~/.ssh/config`, read only), `GUARDIAN_FLEET_PUBKEY_FILE` (default `~/.ssh/guardian_fleet.pub`) and
  `GUARDIAN_SERVERS_FILE`. The path overrides exist for tests; the block text always says
  `~/.ssh/guardian_known_hosts`. Registration refuses to write the inventory unless
  `GUARDIAN_SERVERS_FILE` is set, so the agent can never modify `configs/servers.json` inside the git
  checkout by falling back to it.
- **Inventory edits keep everything else.** Unchanged entries are written back byte for byte (apart from
  the file's two-space indentation); an edited entry keeps its key order and unknown fields
  (`recommended`, `backup_replica`, `group`, ...). New entries have `id`, `name`, the metadata and
  `arch`, and no connection fields: the fleet file is the single source for those.
- **Command allow-list.** `/api/exec/remote` `target_host` matches the inventory **id** only (ssh uses
  the id as the `Host` alias). A name or hostname is refused with 400; when it matches a node, the
  error names that node's id (`... is not an id; use the node id "legacy-1"`). A hostname that exists
  only in the fleet file is not a target either. `targets` of `/api/fleet/exec` and its stream keep
  matching id or name, as before.
- **The hub is not a fleet target while hub sudo is off.** The hub is in the fleet list, and ssh to it
  would run commands outside the agent's sandbox (the hub-sudo bypass). When the install has no hub
  sudo, an explicit hub target (id or name) in `/api/exec/remote`, `/api/fleet/exec` or the stream is
  refused with 409 `{"error": "the hub is not a fleet target while hub sudo is off; use /api/exec"}`,
  and a fleet run without targets simply leaves the hub out. "Hub sudo off" is read from the agent's
  own sandbox: `NoNewPrivs: 1` in `/proc/self/status` (the base unit sets `NoNewPrivileges=yes`; the
  drop-in `hub-sudo.conf` sets `no`). `GUARDIAN_HUB_SUDO=on|off` overrides it. Without `/proc` nothing
  is refused.
- **Audit log.** `<data>/audit/exec.jsonl` is an operational log, not tamper-proof (the agent user and
  its commands can rewrite it). It rotates at 10 MB (`exec.jsonl.1` .. `.5`); `GET /api/audit?limit=`
  reads across the files. Besides executed commands it records node writes (`POST /api/nodes`,
  `PUT|DELETE /api/nodes/{id}`: empty `command`, `detail` = operation summary, `status` `ok`/`error`),
  revoke-key, and rejected exec requests (`status: "rejected"`, `detail` = reason). `actor` is
  `session@<ip>` (dashboard cookie) or `token@<ip>` (Bearer `AGENT_TOKEN`).
- **Host key policy per target.** Every ssh the agent starts (command execution, fleet diagnose, exec
  stream, SSH audit, the registration probe) goes through `executeSSH`. For a node whose fleet block is
  in the registered shape (`host_key_mode` `pinned`) it passes no `StrictHostKeyChecking` option, so the
  block's `yes`, `HostKeyAlias` and `UserKnownHostsFile` apply: a missing or changed pinned key fails.
  The same holds for a hand-written block, whose own policy is honoured. Only nodes without a block and
  migrated blocks (no pinned key) get `-o StrictHostKeyChecking=accept-new`. If the fleet file exists
  but cannot be read, `executeSSH` fails closed (exit 255, no ssh started); a missing file means no blocks.
  `--` before the alias and the `safeSSHHost` check are unchanged; `ssh -G` is unaffected.
- **Sandboxing.** `server-guardian-agent.service` needs no change: home is writable (`ProtectSystem=full`
  only protects /usr, /boot, /etc), `InaccessiblePaths` hides `~/.ssh/guardian_fleet` (the old private
  key path) but not `guardian_fleet.conf`, `guardian_known_hosts` or `~/.guardian/data`, and
  `ssh`/`ssh-keyscan` run inside the same sandbox as before.

## 8. Validation reference

`id` `^[a-z0-9][a-z0-9-]{0,62}$` and not one of the reserved words above; `hostname` canonical IPv4 or DNS name per section 3 (additionally a
name whose last label is all digits is rejected, total length at most 253); `user`
`^[a-z_][a-z0-9_-]{0,31}$`; `port` 1-65535 (JSON number); `host_key_fingerprint`
`^SHA256:[A-Za-z0-9+/]{43}$`. Request values never reach `ssh`/`ssh-keyscan` before validation, `ssh` always
gets `--` before the alias, and `ssh-keyscan` (which has no `--`) only ever receives a hostname that cannot
start with `-`.

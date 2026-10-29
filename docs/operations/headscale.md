# headscale operations

English | [한국어](headscale.ko.md)

headscale is the control server of the fleet's tailnet. It runs on `site-y-ec2` and serves `https://hs.example.com`.
The hub reaches every fleet node over this tailnet: the hub's fleet key is accepted only from the hub's Tailscale IP
(`from="..."` in `authorized_keys`). Existing connections survive a short headscale outage, because clients keep
the last network map. A long outage stops new nodes from joining, stops key renewal and stops changes from reaching
the nodes.

## Layout

| Item | Where |
|---|---|
| Service | `headscale.service` (systemd), binary `headscale` |
| Configuration | `/etc/headscale/config.yaml`, ACL `/etc/headscale/policy.hujson` (`policy.mode: file`) |
| State | `/var/lib/headscale/db.sqlite` (SQLite, WAL), `noise_private.key`, certificate cache `cache/` |
| TLS | Let's Encrypt, TLS-ALPN-01 on `:443` |
| Local only | metrics `127.0.0.1:9090`, gRPC `127.0.0.1:50443` |
| Backup | `headscale-backup.timer` (daily) runs `/usr/local/sbin/headscale-backup.sh`: a live `sqlite3 .backup` of the database plus `/etc/headscale` and the rest of `/var/lib/headscale`, as `/var/backups/headscale/headscale-YYYYMMDD.tar.gz`, last 14 kept |

The backups stay on the same node. If that node's disk is lost, the backups are lost with it. Copying them off the node
is still open.

## What Guardian watches

The nightly ops watch of the `fleet` project (`policy.watch.headscale: true`, 02:30 UTC) runs the read-only check
`headscale_status` on every target and opens one ops debate when one of these is true:

- the service is not `active`;
- the TLS certificate ends within 21 days;
- a `headscale*` backup timer failed, or its last run is more than 48 hours old;
- a tagged node (a fleet node, not a person's device) has been offline for more than 60 minutes.

Agents of any ops debate can run `headscale_status` themselves. Debates never change headscale:
`ops:restart_service` refuses `headscale*`, and no catalog action edits its configuration, ACL or nodes. The one
writes are the operator's mesh join approval and node removal (below). Design
notes: [server-ops-project.md](../design/server-ops-project.md), "P7 headscale control server".

## Emergency access

When the tailnet is down, the hub cannot reach any node, `site-y-ec2` included. Use the admin Mac. Its key for this
node has no `from=` restriction, and the node's SSH (`:22`) is reachable on its public name:

```bash
ssh site-y-ec2
```

```bash
systemctl status headscale --no-pager
```

```bash
sudo journalctl -u headscale -n 200 --no-pager
```

Restore from a backup on the node (stop the service first; the archive holds `var-lib-headscale/` and
`etc-headscale/`):

```bash
sudo systemctl stop headscale
sudo mkdir /tmp/hs-restore && sudo tar -xzf /var/backups/headscale/headscale-YYYYMMDD.tar.gz -C /tmp/hs-restore
sudo cp -a /tmp/hs-restore/var-lib-headscale/. /var/lib/headscale/
sudo cp -a /tmp/hs-restore/etc-headscale/. /etc/headscale/
sudo rm -f /var/lib/headscale/db.sqlite-wal /var/lib/headscale/db.sqlite-shm
sudo chown -R headscale:headscale /var/lib/headscale
sudo systemctl start headscale
```

## Adding a node

A new node joins in two steps, both started from the dashboard's add-node page
([mesh-join.md](../design/mesh-join.md)):

1. **Join the tailnet.** On the new node, a person runs the command the "Approve a mesh join" panel shows:

   ```bash
   sudo tailscale up --login-server https://hs.example.com --hostname <name>
   ```

   It prints a registration URL (`.../register/hskey-authreq-...`). Paste it into the panel, pick the tags and
   approve: the hub runs `headscale auth register` and `headscale nodes tag` on `site-y-ec2`. No auth key is used.
   The hub needs `GUARDIAN_HEADSCALE_HOST` and `GUARDIAN_HEADSCALE_USER` in `~/.guardian/agent.env`.

2. **Register it in Guardian.** "Register this node in Guardian" fills the add-node wizard with the node name and
   tailnet IP; the wizard (`POST /api/nodes`, [node-registration.md](../design/node-registration.md)) adds the
   inventory entry and pins the host key.

There are no valid auth keys: every pre-auth key was expired on 2026-10-18. An admin machine (a person's device,
untagged) runs `tailscale login --login-server https://hs.example.com`, and the operator approves the printed id
on the control server: `sudo headscale auth register --user <user> --auth-id hskey-authreq-...`.

## Removing a node

Fleet > Nodes > "Tailnet nodes" lists every headscale node. "Remove" deletes a tagged node from headscale after
the operator types its name. The hub, the control server and untagged devices cannot be removed there. For a
tailnet node that is also a Guardian node, revoke the hub key and delete the node in Guardian first: after the removal the
hub cannot reach it over the tailnet.

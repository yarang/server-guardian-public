# Mesh join: approving and removing tailnet nodes from the hub

English | [한국어](mesh-join.ko.md)

Status: implemented (2026-10-18). Related: [headscale operations](../operations/headscale.md),
[node registration](node-registration.md), [server-ops-project.md](server-ops-project.md) "P7".

## 1. Goal

A new node joins the fleet's tailnet without an auth key. The person on the new node runs
`tailscale up --login-server <url>`; it prints a registration URL. The operator pastes that URL into the dashboard,
and the hub registers the node in headscale and tags it. After that, the existing node registration adds the node
to Guardian.

Before this, joins used reusable, long-lived auth keys kept on the admin Mac (`~/.config/headscale/.env`) and in the
credential bundle. A leaked reusable key lets anyone add machines to the tailnet; this flow has no key to leak.

## 2. Why the hub cannot do the whole join

The hub reaches nodes only over the tailnet: its fleet key is accepted only from the hub's Tailscale IP. A new node
is not on the tailnet yet, so the hub cannot run `tailscale up` there. One command on the new node stays with a
person.

## 3. Flow

1. On the new node: `sudo tailscale up --login-server https://<headscale> --hostname <name>`. It waits and prints
   `https://<headscale>/register/hskey-authreq-<24 characters>`.
2. In the dashboard (Fleet > Nodes > Add node, panel "Approve a mesh join"): paste the URL, pick the tags, optionally
   a node name, and approve.
3. The hub runs one fixed script on the control server over its fleet SSH:
   `sudo -n headscale auth register --auth-id <id> --user <user> -o json`, then
   `headscale nodes tag -i <node id> -t <tag> ...` and, if a name was given, `headscale nodes rename`. It reads back
   the node's name, addresses and tags (summarised on the control server, so node keys stay there).
4. `tailscale up` on the new node finishes. The panel offers "Register this node in Guardian", which fills the
   add-node wizard with the node name and tailnet IPv4.

## 4. API

`GET /api/mesh/join`: `{enabled, problem?, host?, user?, tags?, ready?, server_url?}`. When enabled, the hub reads
`server_url` from `/etc/headscale/config.yaml` on the control server; that call also checks reachability and
passwordless sudo (`ready`).

`POST /api/mesh/join` with `{"auth": "<URL or id>", "tags": ["tag:fleet"], "name": "edge-7"}`:

- `auth` must contain exactly one id matching `hskey-authreq-[A-Za-z0-9_-]{24}` (headscale's AuthID, v0.29);
- every tag must be in `GUARDIAN_HEADSCALE_TAGS`, at least one is required (nodes join as tagged devices);
- `name` is optional and follows the node id rule `^[a-z0-9][a-z0-9-]{0,62}$`;
- unknown fields are refused (the user cannot be chosen per request).

Answers: `200 {ok, node_id, name, ipv4, ipv6, tags, user, host, warning?}`; `400` with `field` for invalid input;
`409 mesh_join_off` when not configured; `502 mesh_join_failed` with headscale's error (an expired or already used
login says "registration cache"; the answer tells the operator to run `tailscale up` again). Joins run one at a
time. Every request is written to the audit log (`route /api/mesh/join`, target the control server, detail with the
node id, name, IPv4 and tags, or the error).

## 5. Configuration (hub `~/.guardian/agent.env`)

| Key | Meaning |
|---|---|
| `GUARDIAN_HEADSCALE_HOST` | inventory id of the control server; empty turns the feature off |
| `GUARDIAN_HEADSCALE_USER` | headscale user new nodes are registered under |
| `GUARDIAN_HEADSCALE_TAGS` | comma-separated tags the dashboard may give (default `tag:fleet`) |

The install script keeps these lines across reinstalls. Real host and user names stay on the hub, out of git.

## 6. Safety

- This is the only write Guardian makes to headscale. Restarts, configuration, ACL, node removal and upgrades stay
  manual (`headscale*` is a protected unit for `ops:restart_service`).
- The script is fixed text. Every substituted value matches a pattern without quotes, spaces or shell
  metacharacters; tags come from the hub's own list, the user from the hub's environment.
- Approving a login trusts the machine that started it. Approve only a URL the operator saw printed on the new node:
  a URL from anywhere else would add someone else's machine to the tailnet with the chosen tags.
- Tags give ACL access (`tagOwners` in `/etc/headscale/policy.hujson`). Keep `GUARDIAN_HEADSCALE_TAGS` to the tags
  nodes need.
- The dashboard needs the operator's session or `AGENT_TOKEN`, like every other write.

## 7. Reusable auth keys expired (2026-10-18)

All pre-auth keys on the control server were expired with `headscale preauthkeys expire` after a fresh backup:
five reusable keys were still valid (tags `tag:mesh-ctl`, `tag:fleet` with `tag:sw-ctl`, `tag:af-core` or
`tag:gitea`, and one untagged key for admin machines). No automation used them (searched the repositories, the hub,
the control server and the admin Mac); the admin Mac had no `~/.config/headscale/.env` any more. Registered nodes
are not affected: all 28 stayed online. `scripts/export-credentials.sh` no longer bundles `headscale.env`, and
`scripts/import-credentials.sh` no longer restores it and prints the key-less login instead. An admin machine (a
person's device, untagged) joins with `tailscale login --login-server ...` and a `headscale auth register` the
operator runs on the control server by hand; the dashboard approves tagged nodes only.

## 8. Removing a node

Fleet > Nodes shows the panel "Tailnet nodes" while the feature is on: every headscale node with its IPv4, tags,
online state and the Guardian node with the same tailnet IP.

- `GET /api/mesh/nodes`: `{host, tags, nodes: [{id, name, ipv4, ipv6, tags, user, online, last_seen,
  inventory_id?, protected?}]}`. The list is summarised on the control server (no node or pre-auth keys).
- `POST /api/mesh/nodes/{id}/remove` with `{"confirm_name": "<node name>"}` runs `headscale nodes delete -i <id>
  --force`. The hub reads the list again first and decides on that copy.
- Refused (`409 mesh_node_protected`): the hub (its inventory `tailscale_ip`), the control server (named
  `GUARDIAN_HEADSCALE_HOST` or at its IP), and a node with none of the tags in `GUARDIAN_HEADSCALE_TAGS` (people's
  devices and other roles are removed on the control server by hand). A wrong name is `400 confirm_mismatch`, an
  unknown id `404`.
- A node that is also a Guardian node is not refused, because a dead node must be removable, but the dialog warns:
  after removal the hub cannot reach it over the tailnet, so revoke the hub key and delete the Guardian node first.
- Every request is in the audit log (`route /api/mesh/nodes/{id}/remove`).

## 9. Not done

- Expiring a node (log out without deleting) and renaming from the dashboard.
- Creating pre-auth keys from the dashboard: not needed for nodes, and avoided on purpose.

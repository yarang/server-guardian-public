# User-defined node groups (Python CLI ↔ Go hub API ↔ dashboard)

English | [한국어](node-groups.ko.md)

## 1. Problem

Each node in `configs/servers.json` has a single fixed `group` string (e.g. `"Site B Cluster"`).
The Fleet screen (`groupServers` in `FleetPage.tsx`), the Assistant aggregation (`groupRows` in `AssistantPage.tsx`),
Go's `selectNodes` (id/name/group match), the cluster summary of the CLI's built-in engine (`llm_runner.py`) and the
CLI `--target` are all tied to this string. Creating or changing a group means editing servers.json by hand, and a node
belongs to exactly one group.

Goals:
- Named groups, many-to-many (a node can belong to several groups).
- Membership = explicit node ids + rules (dynamic). A new node that matches a rule joins the group automatically.
- Stored in the hub's SQLite DB so the dashboard and the CLI use the same groups.
- The `group` values of servers.json are moved into static groups at the first initialisation and are not used for
  grouping afterwards (the field stays in the file; servers.json is not rewritten).

## 2. Data model and DB schema

DB: the existing hub DB `GUARDIAN_DATA_DIR/guardian_decisions.db` (default `~/.guardian/data`, changeable with
`GUARDIAN_DB_PATH`). `db_manager.DecisionDBManager._init_db` creates the tables the same way as the others
(`CREATE TABLE IF NOT EXISTS`).

```sql
CREATE TABLE IF NOT EXISTS node_groups (
    id TEXT PRIMARY KEY,              -- slug, ^[a-z0-9][a-z0-9_-]{0,63}$
    name TEXT NOT NULL,               -- display name (1-64 characters)
    name_key TEXT NOT NULL UNIQUE,    -- casefolded name: no case-insensitive duplicates
    description TEXT NOT NULL DEFAULT '',
    tone TEXT,                        -- optional: ok | warn | crit | info | idle (design system tone)
    include_ids TEXT NOT NULL DEFAULT '[]',   -- JSON array: explicit server ids
    rules TEXT NOT NULL DEFAULT '[]',         -- JSON array: rules (section 3)
    exclude_ids TEXT NOT NULL DEFAULT '[]',   -- JSON array: server ids removed last
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS guardian_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Why JSON columns instead of a normalised membership table:
- The core of membership is the **rules** (dynamic). Actual members are computed against the current servers.json every
  time, so there are no "member rows" to store. Normalising would put only the explicit ids into a separate table, and
  the rules would be structured JSON anyway.
- Group definitions are small (a few dozen nodes) and always read and written whole. One JSON row makes a save atomic
  and needs no joins or multi-row transactions.
- Validation happens in one place in Python (`group_manager.py`), so the only things DB constraints must stop are
  duplicate ids and names, which PRIMARY KEY and `name_key UNIQUE` do.
- A reverse lookup such as "groups node X belongs to" also needs rule evaluation and cannot be done in SQL. With few
  nodes and groups, evaluating in memory is enough.

`guardian_meta` records whether the one-time seed ran (`node_groups_seeded`). Deciding only by "seed if the group table
is empty" would bring the old clusters back on the next run after a user deleted every group.

Backup: `db_backup.py` copies the whole DB file with the SQLite backup API, so the new tables are backed up and
replicated as they are. No change.

## 3. Rule semantics

Group JSON (CLI stdin, API body, the definition part of the `--groups` output):
```json
{
  "id": "arm-llm-hosts",
  "name": "ARM LLM hosts",
  "description": "Tier-1 ARM nodes that have the claude CLI",
  "tone": "info",
  "include": ["site-y-ec1"],
  "rules": [
    {"conditions": [
      {"field": "arch", "op": "eq", "value": "aarch64"},
      {"field": "cli_support", "op": "contains", "value": "claude"}
    ]},
    {"conditions": [{"field": "name", "op": "glob", "value": "site-b-*"}]}
  ],
  "exclude": ["site-a-arm1"]
}
```

(The example description reads "Tier-1 ARM nodes that have the claude CLI".)

Members = (`include` ∪ nodes matching any rule) − `exclude`.
- One rule = AND of its conditions. A group = OR of its rules.
- `exclude` is applied last and removes ids from both the include list and the rule results.
- The result is in servers.json order.

Fields and operators (node JSON fields):

| field | kind | op | meaning |
|---|---|---|---|
| `tier`, `arch`, `guardian_role`, `role` | scalar | `eq`, `ne` | case-insensitive equal / not equal (a node without the field does not match `eq` and matches `ne`) |
| `tailscale_tags`, `cli_support` | list | `contains`, `not_contains` | the list has / does not have the value (case-insensitive) |
| `name` | scalar | `glob`, `eq`, `ne` | `glob` is an `fnmatch` pattern (`*`, `?`, `[..]`), case-insensitive |

Examples:
- `tier eq Tier-1` → the 7 Tier-1 nodes.
- `[arch eq aarch64] OR [arch eq arm64]` → every ARM node.
- `tailscale_tags contains tag:fleet` AND `guardian_role ne hub` → fleet nodes without the hub.
- No rules + 3 includes → a static group (the seeded clusters have this form).

Validation (`group_manager.parse_group`):
- `name` 1-64 characters, no control characters. `description` ≤ 500 characters. `tone` one of the five above, or
  omitted.
- When `id` is omitted, a slug is made from the name (lowercase, characters other than `[a-z0-9]` become `-`). If the
  slug is empty, as for a non-ASCII name, it is `group-<8 characters of sha1>`. If a new group's slug already exists,
  `-2`, `-3`, ... is appended. Renaming keeps the id.
- `include`/`exclude` ≤ 500 ids each, node id format `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, duplicates removed.
- `rules` ≤ 20, 1-10 conditions per rule (a rule without conditions would match every node and is refused), values
  are strings of 1-128 characters.
- Unknown keys are refused (so a typo is not silently ignored).
- Ids that are not in servers.json **can be saved** and are reported as warnings (a node taken out for a while and
  put back).

## 4. Migration / seed

When `GroupManager` first reads or writes groups:
1. If `guardian_meta.node_groups_seeded` exists, nothing happens.
2. If servers.json cannot be read, the seed is postponed (no marker; it is retried on the next call).
3. If the group table is empty, one static group is created per distinct `group` value of servers.json:
   `id = slug(group)`, `name = group`, `include = the node ids of that group`, no rules,
   `description = "Seeded from servers.json group"`. If groups already exist (created some other way), there is no seed.
4. The marker is written. From then on servers.json `group` is not used for grouping.

Seeding the same name more than once is safe because of `INSERT OR IGNORE` (in case of concurrent runs).

## 5. Python CLI contract (called by Go)

Working directory and invocation are the same as in [debate-teams-api.md](debate-teams-api.md). stdout is one JSON
document.

| Option | stdin | Success (exit 0) | Failure |
|---|---|---|---|
| `--groups --json` | - | see below | - |
| `--group-save --json` | group JSON | `{"ok":true,"id":"...","members":[...],"warnings":[...]}` | exit 2 `{"ok":false,"error":"..."}` |
| `--group-delete ID --json` | - | `{"ok":true,"id":"..."}` | unknown id: exit 3 (HTTP 404) |
| `--group-preview --json` | group JSON (no name needed) | `{"ok":true,"members":[...],"warnings":[...]}` | exit 2 |

`--groups --json`:
```json
{
  "groups": [
    {"id": "site-b-cluster", "name": "Site B Cluster", "description": "...", "tone": null,
     "include": ["site-b-ec1", "..."], "rules": [], "exclude": [],
     "created_at": "...", "updated_at": "...",
     "members": ["site-b-ec1", "..."],
     "warnings": ["include: oci-old-1 is not in servers.json"]}
  ],
  "ungrouped": ["oci-new-1"],
  "catalog": {
    "fields": [
      {"field": "tier", "kind": "scalar", "ops": ["eq", "ne"], "values": ["Tier-1", "Tier-2"]},
      {"field": "tailscale_tags", "kind": "list", "ops": ["contains", "not_contains"], "values": ["tag:fleet", "..."]},
      {"field": "name", "kind": "scalar", "ops": ["glob", "eq", "ne"], "values": ["site-a-arm1", "..."]}
    ],
    "tones": ["ok", "warn", "crit", "info", "idle"]
  },
  "servers_file": "/home/ubuntu/.guardian/data/servers.json",
  "warnings": []
}
```
If servers.json cannot be read, `members` are empty arrays and the top-level `warnings` say why (no invented members).

servers.json location: `GUARDIAN_SERVERS_FILE` → the repository's `configs/servers.json` →
`~/.config/agentthread/servers.json`. When `GUARDIAN_SERVERS_FILE` is set, only that path is used (a missing file
fails, naming that path). The hub agent passes the path of the file it reads as `GUARDIAN_SERVERS_FILE`, so both sides
always see the same file. On the hub the install script puts `GUARDIAN_SERVERS_FILE=~/.guardian/data/servers.json`
into `agent.env` ([node-registration.md](node-registration.md)). The repository's `configs/servers.json` is a local
snapshot that git ignores (the format is in `configs/servers.example.json`); it is not committed.

## 6. Go hub API

All behind the existing token authentication and `guard` (503 without the CLI). Body at most 64 KB
(`configBodyLimit`), passed only as argv/stdin, never through a shell.

| Method | Path | Action |
|---|---|---|
| GET | `/api/groups` | the `--groups --json` output as is |
| POST | `/api/groups` | body to `--group-save` stdin. exit 2 → 400 |
| POST | `/api/groups/preview` | body to `--group-preview` stdin |
| DELETE | `/api/groups/{id}` | `{id}` must match `^[a-z0-9][a-z0-9_-]{0,63}$`, `--group-delete`. exit 3 → 404 |

Go does not know membership. The UI and the CLI expand a group into a list of node ids and pass it to the execution
APIs:

| Path | Target selection (additions of this change in bold) |
|---|---|
| `POST /api/fleet/exec/stream` | `{"command", "targets":[id...]}` (existing) |
| `POST /api/fleet/exec` | **`{"command", "timeout_sec"?, "targets"?:[id...]}`** |
| `GET /api/fleet/diagnose` | **`?targets=id1,id2`** (all when omitted) |
| `GET /api/fleet/ssh` | `?targets=id1,id2` (all when omitted; added later, [fleet-ssh-audit.md](fleet-ssh-audit.md)) |
| `POST /api/exec/remote` | one `target_host` (existing) |

Each value of `targets` must equal a servers.json id or name exactly; if any one is unknown the answer is 400.
(2026-10-18: `target_host` of `/api/exec/remote` now accepts only the inventory id; a name or hostname gets 400.
[node-registration.md](node-registration.md) section 7.)

`selectNodes` no longer accepts the servers.json `group` string. This keeps a single group concept (user groups);
the existing callers (the UI sends `[]` or node ids, the CLI no target) never send a group string, so nothing breaks.
`/api/nodes` also returns `tier`, `role`, `guardian_role` and `cli_support` so the UI can use the rule fields.

## 7. Dashboard

(2026-10-18: the dashboard navigation changed after the first implementation
([ui-flow-redesign.md](ui-flow-redesign.md)). The places below are the current ones. Fleet page tabs: Summary, Nodes,
SSH audit, Commands, Groups.)

- **Nodes tab** (`#/fleet/nodes`): a "Group by" select (User groups / None / Tier / Arch) and a group filter. With
  User groups a node appears under every group it belongs to, and nodes in no group are collected under "Ungrouped".
  When the groups cannot be loaded it shows None. The hard-coded `groupServers` is removed. Several rows can be
  selected to create a new group or add them to an existing one, and with `?group=<id>` a group summary (definition,
  member count, reachability from the shared diagnosis, the members' fleet SSH summary) is shown above the table.
- **Groups tab** (`#/fleet/groups`; `?edit=<id>` opens that group's edit dialog; the old `#/settings/groups` lands
  here): group list with member counts and warnings, new / edit / delete.
  Edit dialog: name, description, explicit members (searchable checkbox list), rule editor (rule card = AND condition
  rows, OR between rules; a condition row has field, operator and value, with values suggested from the catalog),
  exclude list, live member preview (`/api/groups/preview`). Delete is confirmed with `ConfirmModal`.
- **Node detail** (`#/fleet/nodes/<id>`): a group membership panel. It shows for each group how the node is in it
  (added through include / matched by rule); removing drops it from include or adds it to exclude.
- **Commands tab** (`#/fleet/commands`, formerly Batch Exec): target = local node / each node / each group / the
  whole fleet. A group target sends the expanded id list as `targets` of the streamed run and, being a multi-node run,
  goes through the confirmation dialog like the whole fleet. Groups with 0 members cannot be selected.
- **Assistant**: aggregated diagnosis results by user group (a node in several groups counted in each, the rest under
  Ungrouped). (2026-10-18: the Assistant screen is gone; per-group reachability is shown by the group summary of the
  Nodes tab.)
- **SSH audit tab** (`#/fleet/ssh`): aggregates the per-node audit of `/api/fleet/ssh` by user group (design:
  [fleet-ssh-audit.md](fleet-ssh-audit.md)). `/api/ssh/*` audits only the hub itself.

## 8. CLI `--target` resolution

`guardian.py --exec CMD --target X`, `--fleet --target X`, `--ssh-audit --target X`, query mode `--target X`:
1. `local`, `all`, `fleet` are reserved words (existing meaning).
2. An exact match of a node id, name or hostname in servers.json (the hub's `/api/nodes`) is that one node.
3. A user group id or name (case-insensitive) is the member id list of that group. A group without members is an error.
4. Anything else is an error: close node/group names (`difflib`) are shown with it, exit 2.

The group list comes from the hub's `/api/groups` (the same result as the dashboard). If the hub lacks that API or the
call fails, the groups are computed directly from the hub DB only when that DB file already exists on the machine the
CLI runs on (the same result when the CLI runs on the hub). On a machine without the DB no empty DB is created; groups
count as unavailable (only node names are resolved).

`--fleet --target <group>` diagnoses only its members, and `--exec CMD --target <group>` streams the command to the
members only. The cluster summary of the built-in engine counts by user group (in each when a node is in several,
"Ungrouped" when in none).

Ops projects also use groups as targets: a project's `targets.groups` is a list of group ids, resolved again with
`group_manager.resolve_members` when a debate starts and again when an action runs, not when the project is saved
(`cli/llm-agent/ops_targets.py`; design: [server-ops-project.md](server-ops-project.md)). An unknown group id is
reported under `missing`, not raised.

## 9. Edge cases

- **Node renamed or removed**: include/exclude work by id. An id that disappeared from servers.json drops out of the
  members and the group gets a warning. It is not removed from the definition automatically (when the node comes back
  it is a member again).
- **Empty group**: can be saved, with 0 members and the warning "no servers match this group". It cannot be used as a
  Batch/CLI target (error).
- **Duplicate name**: unique by casefold. If another id uses the same name: exit 2
  "group name '...' is already used by <id>".
- **Group id equal to a node name**: `--target` looks for nodes first. To reach the group, use another name or id.
- **servers.json group changed after the seed**: not applied (groups are managed in the DB).
- **Concurrent saves**: the last save wins (row-level upsert). Name uniqueness is guaranteed by the DB constraint.

## 10. Security

- Every input is validated in Python (`group_manager.parse_group`): 64 KB size, unknown keys refused, length and count
  limits, id pattern, no control characters.
- Go cuts the body at 64 KB and passes it only on stdin. The `{id}` in the path is checked with `idPattern` and passed
  as an argv element (no shell).
- Rule `glob` is `fnmatch` (not a regular expression), so there is no ReDoS; values are limited to 128 characters.
- Go matches the `targets` of the execution APIs exactly against servers.json. Whatever a group holds, ssh never goes
  outside servers.json.
- All SQL uses parameter binding.

## 11. Follow-ups

- ~~Once a per-node SSH audit API exists, aggregate the SSH screen by group.~~ Done: [fleet-ssh-audit.md](fleet-ssh-audit.md).
- The legacy `group` field in Go responses such as `FleetNodeResult.group` is not used by the UI. It can be removed if
  there is no other consumer.

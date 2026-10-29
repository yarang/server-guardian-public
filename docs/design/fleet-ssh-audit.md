# Per-node SSH security audit (fleet SSH audit)

English | [한국어](fleet-ssh-audit.ko.md)

## 1. Problem

`/api/ssh/config`, `/api/ssh/security` and `/api/ssh/keys` audit only the one node the hub agent process runs on
(`apps/agent/sshd.go`: `ParseSSHDConfig` → `EvaluateSecurity`, `GetAuthorizedKeys`). There is no way to see the sshd
configuration and `authorized_keys` of the other 23 nodes, so the SSH screen could not aggregate by user node group
(follow-up from sections 7 and 11 of [node-groups.md](node-groups.md)).

The hub already reaches every node over SSH with the fleet key (`executeSSH`, `DiagnoseFleet`). Over the same path it
runs a **fixed, read-only probe** that fetches each node's sshd configuration and keys, and evaluates them with **the same
parser and the same rules** as the hub's local audit.

Goals:
- Per-node results (config summary, rule verdicts, score, authorized_keys) and a fleet summary (average score, failing
  nodes, per-rule counts) from one API.
- Split `sshd.go` from "file/command" dependencies to "text" input, so text fetched from a remote node can be parsed.
- CLI `--ssh-audit`; on the dashboard SSH screen a per-group summary, a rules-by-node heatmap and node details.
- Never invent results. Connection failures, partial reads and failures show as they are, as a status.

Out of scope: changing the configuration (actions) belongs to the existing Remediation / judge action path. This feature
only reads.

## 2. Remote probe

### 2.1 How it runs

- `executeSSH(srv.ID, fleetSSHProbe, 20)` is called for each node. Key and host handling are unchanged (ssh-agent
  socket, `BatchMode`, `ConnectTimeout=4`, host after `--`). There is no new key handling. The host key policy is per
  node: a node registered from the dashboard (pinned) follows the strict checking of its fleet file block; only nodes
  without a block or with a migrated block get `StrictHostKeyChecking=accept-new`
  ([node-registration.md](node-registration.md) section 7).
- The probe is a single Go constant. **Request values (target ids, query) never go into the script.** A target is
  only used as the ssh destination, as a `FleetNode.ID` matched against servers.json.
- It is wrapped in `sh -c '<script>'` so that the result is the same whether the login shell is bash, dash or zsh.
  The script must contain no single quote (a test checks this).
- At most 8 run at once (semaphore). For 24 nodes that is about three waves, with a cap of 20 seconds per node.
- The whole output is cut with `head -c 262144` (256 KiB). When it is cut the `END` marker is missing, so the node
  becomes `partial`.

### 2.2 Script (exact command)

```sh
sh -c '
export LC_ALL=C
PATH="$PATH:/usr/sbin:/sbin:/usr/local/sbin"
m="@@GSA"
{
echo "$m BEGIN 1"
echo "$m USER"
id -un
got=0
for via in sudo direct; do
  if [ "$via" = sudo ]; then out=$(sudo -n sshd -T 2>&1 </dev/null); rc=$?
  else out=$(sshd -T 2>&1 </dev/null); rc=$?; fi
  if [ "$rc" -eq 0 ]; then
    echo "$m EFFECTIVE $via"
    printf "%s\n" "$out" | head -c 65536
    got=1
    break
  fi
  echo "$m EFFECTIVE_ERR $via $rc"
  printf "%s\n" "$out" | head -c 2048
done
if [ "$got" -eq 0 ]; then
  n=0
  for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*; do
    [ -f "$f" ] || continue
    n=$((n+1)); [ "$n" -le 32 ] || break
    if [ -r "$f" ]; then echo "$m FILE $f"; head -c 65536 "$f"; echo
    else echo "$m UNREADABLE $f"; fi
  done
fi
echo "$m KEYS"
if [ -r "$HOME/.ssh/authorized_keys" ]; then head -c 65536 "$HOME/.ssh/authorized_keys"; echo; fi
echo "$m END"
} 2>/dev/null | head -c 262144
'
```

Order and reasons:
1. `sudo -n sshd -T`: the effective configuration (Includes and defaults applied, syntax checked). `-T` has to read the
   host keys, so on most distributions it needs root. The fleet login user (ubuntu and so on) usually has passwordless
   sudo, so this is the main path. With `-n` it fails at once instead of asking for a password.
2. `sshd -T`: nodes where the login user is root.
3. If both fail, a `cat`-like read: `/etc/ssh/sshd_config` and every file in `/etc/ssh/sshd_config.d/` (at most 32
   files, 64 KiB per file). It does not narrow to `*.conf` because the Include pattern differs per distribution
   (Ubuntu `*.conf`, macOS `*`). The Go parser picks the files to use with the actual Include pattern.
   An unreadable file is only marked `UNREADABLE` (Ubuntu's `50-cloud-init.conf` is sometimes 0600).
4. The login user's `~/.ssh/authorized_keys` (64 KiB).

### 2.3 Output format

Lines starting with `@@GSA ` are section markers. The body of a section runs from the line after its marker to the
next marker.

| Marker | Body |
|---|---|
| `@@GSA BEGIN 1` | None. Format version 1. If missing, the probe did not run |
| `@@GSA USER` | `id -un` |
| `@@GSA EFFECTIVE <sudo\|direct>` | `sshd -T` output |
| `@@GSA EFFECTIVE_ERR <via> <rc>` | Message of the failed `sshd -T` (2 KiB) |
| `@@GSA FILE <path>` | Config file content |
| `@@GSA UNREADABLE <path>` | None |
| `@@GSA KEYS` | authorized_keys content |
| `@@GSA END` | None. If missing, the output was cut or interrupted |

A config file line does not start with `@@GSA ` by accident. If a node's root puts one there on purpose, only that
node's own result is distorted (section 8). The Go parser ignores unknown markers and, when a section repeats, uses only
the first one.

## 3. Reusing parsing and evaluation (`sshd.go` refactoring)

The input becomes text so the hub-local and remote audits share the same code.

- `SummaryFromEffectiveConfig(text, path)`: `sshd -T` output → `SSHConfigSummary` (Source `sshd -T`, syntax checked).
  The local `ParseSSHDConfig` also passes the `runSSHD` result to this function.
- `readSSHDConfig(fsys, path, values, depth, includes)`: file access is abstracted as `sshdConfigFS` (ReadFile, Glob).
  - Local: `osConfigFS` (os.ReadFile, filepath.Glob), the existing behaviour.
  - Remote: `mapConfigFS` (`path → content` sent by the probe). Glob finds map keys with `filepath.Match`.
  - The rules are unchanged: the first value of a directive wins, `Include` is followed (relative paths against
    `/etc/ssh/`, in sorted order), everything after `Match` to the end of the file is conditional and skipped (an
    `Include` inside Match too), a Match in an included file does not leak into the including file. `key=value` form
    and case-insensitive keywords.
- `ParseSSHDConfigText(mainPath, files)`: for the remote fallback. It also returns the Include patterns it followed, so
  an Include pointing outside what the probe fetches (outside `/etc/ssh/sshd_config.d/`) is recorded in the node's
  `notes` as "included file not read".
- `isSSHDConfigError`: also used as is for the remote `EFFECTIVE_ERR` message. Only when sshd actually rejected the
  configuration is it a "syntax error"; a sudo refusal or a missing host key counts as "not checked".
- `EvaluateSecurity`, `parseAuthorizedKeyLine` and `keyFingerprint` are reused unchanged.

The behaviour of `/api/ssh/config|security|keys` and the existing tests stay the same.

## 4. Node status

| Status | Condition | config/report |
|---|---|---|
| `ok` | `sshd -T` succeeded, `END` present | present |
| `partial` | evaluated from fallback files (Source `sshd_config (fallback)`), or `END` missing (cut, timed out) | present |
| `unreachable` | no `BEGIN` + ssh exit code 255 or killed by the timeout | absent |
| `error` | no `BEGIN` (any other exit), or neither `sshd -T` nor the files could be read | absent |

A `partial` fallback result may miss values from unreadable Include targets and changes in sshd defaults. This is
written to `notes`, and the syntax stays "not checked" ("syntax error" if there is a config rejection message). A node
that cannot be evaluated is never scored on defaults. Without a score it is also left out of the average.

Apart from the status, every node has a `reachability`. It is the same classification (`classifySSH`,
`apps/agent/ssh_classify.go`) as fleet diagnosis, command execution and the registration probe use: `ok`,
`auth_failed` (the node rejected the fleet key), `host_key_changed` (host key verification failed), `unreachable` (no
connection, timeout), `command_failed` (ssh connected but the probe markers are missing). `status` keeps its audit
meaning; `reachability` says in the same words on every screen why an `unreachable` / `error` node failed.

## 5. API contract

`GET /api/fleet/ssh?targets=a,b[&refresh=1]`, behind the same token authentication (`withAuth`) and `withSecurity` as the
other fleet APIs.

- `targets` omitted (or empty) = all of servers.json. Each value must equal an id or name exactly; if any one is
  unknown the answer is 400 **before any SSH starts** (`fleetSubset`, `fleetErrorStatus`). If servers.json cannot be
  read, 500.
- Methods other than GET get 405.

Response:

```json
{
  "generated_at": "2026-10-05T03:00:00Z",
  "duration_ms": 2310,
  "cached": false,
  "nodes": [
    {
      "id": "site-a-arm1", "name": "site-a-arm1",
      "status": "ok",
      "reachability": "ok",
      "source": "sshd -T (sudo)",
      "login_user": "ubuntu",
      "duration_ms": 640,
      "config": { "...": "SSHConfigSummary" },
      "report": { "...": "SecurityReport" },
      "keys": [ { "id": "key-1", "key_type": "ssh-ed25519", "public_key": "", "comment": "...", "fingerprint": "SHA256:...", "options": "from=\"100.64.0.7\"" } ],
      "notes": [],
      "error": ""
    }
  ],
  "summary": {
    "nodes": 24, "audited": 22, "unreachable": 1, "errors": 1,
    "avg_score": 71.4,
    "failing_nodes": ["site-b-ec1"],
    "rules": [ { "rule_id": "SEC-SSH-001", "title": "Disable Direct Root Login via SSH" } ],
    "by_rule": { "SEC-SSH-001": { "fail": 2, "warn": 18, "pass": 2, "info": 0 } }
  }
}
```

- `nodes` are in servers.json order.
- `reachability` is the shared ssh classification of section 4.
- `keys` leave the public key body (`public_key`) empty. Fingerprint, comment and options are enough, and the response
  stays small. There is no personal data in the first place.
- `avg_score`: average over evaluated nodes (`ok` + `partial`), one decimal. `null` when no node was evaluated.
- `failing_nodes`: ids of nodes with at least one FAIL rule (nodes with only WARN/INFO are left out, the same
  definition of "failed" as the local screen).
- `rules`: the rule catalog (evaluation order). Used as the heatmap column order.
- `by_rule`: status counts per rule (evaluated nodes only).
- Go does no per-group aggregation. Go does not know groups ([node-groups.md](node-groups.md) section 6); the UI and the
  CLI bucket nodes by `/api/groups` membership.

## 6. Cache

The hub keeps a **60-second TTL in-memory cache**. The key is the resolved node id set (sorted; "all" is all of those
ids). `?refresh=1` skips the cache and updates it with the new result.

Reasons:
- One request runs SSH and `sudo sshd -T` on 24 nodes. The dashboard calls this API on opening the screen, on the
  global Refresh and on the node detail page; the CLI and the Security Auditor agent call it on every query. Several
  tabs or users at once would repeat the same fan-out.
- sshd configuration does not change by the minute. A 60-second delay is fine for an audit; right after a fix, Refresh
  (`refresh=1`) confirms it.
- If a request with the same key is already running, no new SSH starts; the caller waits for that result
  (single-flight). `refresh=1` also joins a run in progress (its result is already the newest).
- The response carries `cached` and `generated_at`, so the screen shows the age of the result as it is.
- At most 64 entries. Expired entries are dropped on store, and if the cache is still full the oldest entry is
  dropped. It lives in process memory only and is empty after a restart (no key list on disk).

Alternatives considered: no cache (duplicate fan-out, 2-5 s dashboard responses), periodic background collection
(runs sudo on 24 nodes when nobody is looking, more hub state). Computing on request plus a short cache is the simplest.

## 7. Failure modes

| Situation | Result |
|---|---|
| Node down, network cut, host key mismatch | ssh 255 → `unreachable`, last stderr line in `error`. `reachability` tells `unreachable`, `host_key_changed` and `auth_failed` apart |
| No ssh-agent socket (hub fleet key service stopped) | every node `unreachable` with the same error message. The local section keeps working |
| Fleet file (`~/.ssh/guardian_fleet.conf`) exists but cannot be read | `executeSSH` refuses with 255 without starting ssh → every node `unreachable` |
| Over 20 seconds | process killed → `unreachable` without `BEGIN`, `partial` ("output incomplete") with it |
| `sudo -n` not possible, login user not root | fallback files → `partial`, sudo message in notes |
| sshd_config 0600 (RHEL family) + no sudo | `error` "no readable sshd configuration" |
| sshd config error | `sudo sshd -T` rejects it → fallback evaluation + `syntax_checked=true, is_valid=false` + message |
| sshd not installed | `sshd -T` fails, no files → `error` |
| Output over 256 KiB | cut → no `END` → `partial` |
| No authorized_keys | `keys: []` (normal) |
| No servers.json | 500, no SSH |
| Unknown target | 400, no SSH |

## 8. Security

- The probe is a Go constant with no input interpolation. A request can only change the target set (checked against
  servers.json) and whether the cache is bypassed.
- The probe only reads: `sshd -T` (prints the config), `head`, `id`. It writes no file and touches no service.
  `sudo -n` never asks for a password.
- Node output is untrusted: a size cap (256 KiB for the probe), caps per section, line-based parsing, no regular
  expressions. A node that forges its result only changes its own row; other nodes and hub commands are not affected
  (output is never fed back into a shell).
- The response drops even the public key body and gives only the fingerprint. The `command="..."` option of
  authorized_keys is shown as is, so the existing operating rule of not putting secrets into options applies.
- Authentication: behind the existing `withAuth` (Bearer token) and `withSecurity` (Host/Origin/CORS). It is a GET whose
  only side effect is an SSH read, and the cache keeps repeated calls from multiplying the fan-out.

## 9. Dashboard (SSH screen)

The SSH screen is the SSH audit tab of the Fleet page (`#/fleet/ssh`; the old `#/ssh` and `#/security` still open it)
(`apps/web-desktop/src/pages/SshPage.tsx`, `pages/ssh/FleetSshAudit.tsx`).

- **Fleet SSH audit** panel (top). Loaded when the screen opens and on the global Refresh (the hub may answer from its
  60-second cache). The panel's refresh-audit button sends `refresh=1`. It shows the result time, whether it was
  `cached`, and the duration.
  - Three summary cards: audited nodes / total (when some nodes were not audited, the count per `reachability`, e.g.
    "1 host key changed · 1 unreachable · 2 errors"), average score, number of failing nodes.
  - **Group summary table**: for each user group the node count, audited count (including the unreachable count),
    average score, failing nodes (links to the node pages) and the rule failing most often. A node in several groups
    counts in each; nodes in no group go under Ungrouped. When the groups cannot be loaded, an error (with retry) and a
    single all-nodes row.
  - Group filter (reusing `filterByGroup` from `lib/groups.ts`): all / each group / Ungrouped. Groups without members
    cannot be picked.
  - **Rules-by-node heatmap** (`Heatmap`, design system): rows = nodes (split by group when the filter is "all"),
    columns = the rule catalog, cells = P/W/F/I (INFO, e.g. the default port 22)/N (the rule is missing from the
    report: "not checked" instead of an invented PASS). A `partial` node gets a status chip on its row. A node that
    could not be evaluated becomes an "unavailable" row that shows its status and error in one line instead of cells.
    Clicking a row selects that node.
  - **Node details**: the selected node (by default the first failing node, else the first node) → status,
    `reachability` chip, score, source, login user, notes, the sshd rejection message, FAIL/WARN/INFO rules (current vs
    expected value, remediation) and the authorized_keys table. It has a link to the node page and a "debate this node"
    button (prefills a debate task with the node's findings and goes to Debates). The node detail page
    (`#/fleet/nodes/<id>`) uses the same component (`NodeDetails`) with an audit of that one node.
- The previous hub-local section moves into a "Hub agent (local view)" collapsible area (`Disclosure`). The hub also
  appears as a fleet row, but the local section stays because it works while fleet SSH (the ssh-agent socket) is down
  and shows the configuration the hub agent process sees.
- Narrow screens: tables and heatmaps scroll horizontally (`DataTable`, `.sg-heatmap-wrap`), selects wrap.

(2026-10-18: the first design had four summary cards, a heatmap with only P/W/F, a select to pick the node and a
separate "nodes not audited" list under the heatmap. It changed as above once `Heatmap` gained INFO, "not checked",
unavailable rows and row selection.)

## 10. CLI

- `agent_client.NodeAgentClient.fleet_ssh_audit(targets=None, refresh=False)` → `GET /api/fleet/ssh` (90-second timeout).
- `guardian.py --ssh-audit [--target X] [--refresh] [--json]`
  - `--target` uses the existing resolution (node → group; `local`/`all`/`fleet` mean everything).
  - Human output (`ssh_audit.render_ssh_audit`): per-group table (group, nodes, audited, average score, failing nodes)
    → FAIL rules and remediation per node → partial nodes with their notes → nodes not audited. A node in several
    groups counts in each; nodes in no group are "Ungrouped" (`group_manager.memberships`). When the groups cannot be
    loaded, a warning and all nodes as one bucket.
  - `--json` prints the API response plus `groups` (group name → member ids, `null` when the groups could not be
    loaded) and `group_rows` (per-group summary rows).
  - Exit codes: 0 (audit ran, whatever it found), 1 (hub call failed), 2 (invalid `--target`).
- Security Auditor agent: when the query targets the fleet, a group or a remote node, it uses the `/api/fleet/ssh`
  result (summary, group membership, failing rules per node) as LLM context (`ssh_audit.llm_context`); for a local
  target it uses the hub-local API as before.

## 11. Follow-ups

- ~~Once `Heatmap` has "not evaluated" and INFO states and a row selection callback, put unreachable nodes into the same
  table and open details on row click.~~ Done (checked 2026-10-18): section 9.
- Nodes with a non-default `AuthorizedKeysFile`: read the `authorizedkeysfile` value from `sshd -T` and probe that path.
- More rules (e.g. weak `KexAlgorithms`/`Ciphers`, `LoginGraceTime`, `ClientAliveInterval`). Only `sshdKeys` and
  `EvaluateSecurity` need to change.
- Extend the Remediation agent's targets to the per-node audit results (today it works from the hub-local rules).

# Resident agent monitor in the dashboard (design)

English | [한국어](resident-monitor.ko.md)

Status: proposed and approved 2026-11-21. Scope chosen by the operator the same day: resident agents only (not debate agents,
which already have the run monitor), a new tab under Workflows, read-only first. RM1-RM4 built 2026-11-21 (checked in a real browser
against a local hub), deployed 2026-11-21 as `8e2179d`.
Origin: "Let the web dashboard show what the agents are doing and how they are." Resident agents
(`docs/design/resident-agents.md`, `docs/design/resident-open-tools.md`) run on the hub's mail store and task board,
but the dashboard shows none of it: the operator reads `--residents`, `--mail-list`, `--tasks` and the relay log over
SSH.

## 1. Goal and non-goals

Goal: one screen that answers, at a glance and without SSH,

- which resident agents exist, where, with which provider, driver and tools (closed / open);
- whether each one is `ready`, `down` or `retiring`, since when, at which epoch, and how often binding failed;
- what each agent did lately: its turns (kind, start, length, end reason, hop) and its mail (in and out);
- the task board: who holds what, in which status, and which tasks wait as a patch for the operator;
- the recent store events (`agent_down`, `agent_restored`, `bind_failed`, `task_patch`, `mail_undeliverable`, ...).

Non-goals (this design):

- No writes. Sending mail, creating or cancelling tasks, removing agents come in a later phase, each with its own
  decision. A patch waiting for approval only links to the existing Approvals screen; nothing is approved here.
- No operator sessions, links or workstations (`docs/design/operator-sessions.md`): S1 is not deployed. The snapshot
  leaves room for them (section 3) but does not read those tables.
- No live push (SSE, websocket). Polling is enough for agents whose turns take tens of seconds.
- No debate agents: the existing run monitor (`pages/agents/*`, folded from the debate event stream) stays as it is.

## 2. Constraints

1. The Go agent is standard library only and has no SQLite driver. It reads the store through the Python CLI, the
   same way it reads teams, workers and flows (`h.guard(h.configGet("--flag"))`, which appends `--json`).
2. The read path must not write. `MailStore._ready()` runs `_init_schema` (CREATE IF NOT EXISTS plus ALTER
   migrations): calling it from a monitor would create the file on a hub without residents, and could migrate a store
   from a dashboard poll. The monitor opens the database itself with the SQLite URI `file:<path>?mode=ro` and never
   calls `_open` or `_ready`. The same goes for every existing helper built on them: `MailStore.events()`,
   `agent_rows()`, `list_mail()`, the task board reads of `mail_tasks.py` and `resident_registry.list_agents()` all
   pass through `_ready()`. `resident_registry.load_declarations()` writes a `declaration_problem` event when an entry
   is bad, so the monitor reads the declaration file (`declaration_path()`) as JSON itself and takes only `name`,
   `node`, `provider`, `driver` and `tools` from each entry; a bad entry becomes a `problems` line.
3. One Python spawn per poll. The hub has 2 OCPU; a screen that spawns five processes every few seconds would show up
   in the load. The CLI returns one bundled snapshot.
4. Mail bodies, task patches and call logs can be large and can quote code. The snapshot carries metadata and sizes;
   a body is read only when the operator opens one mail.
5. Real host names never enter tests, fixtures or previews: `node-1`, `hub-1`, `claude@node-1`, `coder@node-1`.

## 3. Read path

New module `cli/llm-agent/resident_monitor.py` (no change to `mail_store.py`):

- `open_readonly(path) -> Optional[sqlite3.Connection]`: `None` when the file is missing; otherwise
  `sqlite3.connect("file:...?mode=ro", uri=True)`, `busy_timeout` 5000, `row_factory` Row. WAL readers need the
  `-wal` and `-shm` files to exist or be creatable; the hub's store runs in WAL mode under the same Unix user as the
  Go agent, so this holds.
- Schema: the stamp is read from `meta` (`key = 'schema_version'`, a string; the store does not use
  `PRAGMA user_version`). Each SELECT is built from `PRAGMA table_info(<table>)` intersected with the columns the
  monitor wants; a wanted column the table lacks comes back as `null`, and a missing table gives an empty section
  and a `problems` entry, never an exception. So a store older or newer than `SCHEMA_VERSION` is still shown, and a
  stamp other than `SCHEMA_VERSION` adds one `problems` line.
- `snapshot(limits) -> dict`, all times as stored (UTC strings) plus `generated`:

```
{
  "generated": "...", "store": {"exists": true, "schema": 5, "path_kind": "data_dir"},
  "agents": [{
    "address", "name", "node", "provider", "driver", "tools",          # tools from the declaration file
    "state", "epoch", "not_ready_since", "bind_failures", "bind_started", "modified",
    "incarnation": {"epoch", "started", "pane", "pid"} | null,          # the open one, if any
    "turn": {"id", "kind", "started", "hop_in"} | null,                 # a turn with no `ended`
    "mail_open": {"queued": n, "delivered": n, "retrieved": n},         # to this agent
    "tasks_held": n,
    "declared": true, "in_store": true                                  # a declared agent not yet in the store, or the reverse
  }],
  "turns": {"<address>": [{"id", "epoch", "kind", "started", "ended", "end_reason", "hop_in", "inputs": n}]},
  "mail": [{"id", "thread", "sender", "recipient", "kind", "subject", "task", "reply_to", "hop", "state",
            "created", "expires", "redelivered", "bytes"}],
  "tasks": [{"id", "title", "creator", "assignee", "owner", "status", "project", "sha", "hop", "generation",
             "blocked_by": [...], "patch_bytes", "patch_action", "calls": n, "reason", "created", "modified",
             "expires"}],
  "events": [{"id", "at", "kind", "agent", "detail"}],
  "problems": ["..."]
}
```

- Limits (constants, overridable by flags for tests): 20 turns per agent, 100 mail (newest first), every open task
  plus the newest 50 closed ones, 100 events. Task `detail`, `result`, `patch` and `calls_json` are reduced to sizes
  and counts; mail `body` to `bytes`. Sizes are bytes, computed in SQL as `length(CAST(x AS BLOB))` (plain
  `length()` counts characters). Event `detail` is passed through: its writers put ids, counts, states and reasons
  there (`task_patch` holds task, action, file count, bytes and base; `mail_sent` holds id, recipient, kind, hop),
  never a body or a patch; the test below holds that.
- `mail_body(mail_id) -> dict`: the one mail with its body, from the same read-only connection, the body passed
  through `audit_run.mask_secrets` (open point 2).

CLI (wired in `guardian.py` next to the resident commands): `--resident-monitor` (snapshot) and
`--resident-mail-body ID`, both honouring `--json`. Exit 0 with an empty snapshot (`store.exists: false`) when the
store is missing, so a hub without residents shows "no resident agents" rather than an error.

Later (not this phase): `links` and `sessions` sections once S1 is deployed, under the same snapshot.

## 4. Hub API

- `GET /api/residents/monitor` -> `h.guard(h.configGet("--resident-monitor"))`.
- `GET /api/residents/mail/{id}` -> a small handler: `id` must be a mail id `m-YYYYMMDD-NNNN` (else 400), then
  `runConfigCLI(nil, "--resident-mail-body", id, "--json")`.
- Both sit behind the global `withSecurity(withAuth(mux))` chain like every other route; no new auth.

## 5. Dashboard

- Route `#/debates/residents`, a tab "Resident agents" in the Workflows segmented control, for every project kind
  (residents are hub-wide, like Quick ask). The hash `#/agents` stays the legacy redirect to debates.
- Polling: every 10 s while the tab is visible, a manual refresh button, the time of the last snapshot. `useHubData`
  polls on a plain `setInterval` with no visibility check today; the pause on `document.visibilityState` (and one
  load on becoming visible again) is new, added as an option of `useHubData` so other screens are unchanged.
- Routing: `'residents'` joins the list of tab names that `DebatesPage.tsx` keeps out of `DebatesView`'s `jobId`
  (next to `wiki`, `artifacts`, `results`, `ask`, `templates`); otherwise the tab name would be read as a job id.
- Layout:
  1. Agent cards: address, node, provider, driver, a "open tools" chip when `tools` is open, state chip
     (`ready` / `down` / `retiring`, with "since" from `not_ready_since`), epoch, bind failures, the running turn
     (kind and elapsed time) or the last turn's end, open mail count, held tasks.
  2. Detail of the selected agent (`?agent=<address>`): recent turns table; mail in and out (subject, kind, state,
     hop, time; a row opens the body on demand); its tasks.
  3. Task board: status, title, assignee, project, patch size. A task with `patch_action` links to
     `hrefFor('approvals', [patch_action])`. The mail store knows the action id but not its state, which lives in the
     decision DB; `patch_action` is that DB's action id (written by `mail_relay.record_task_patch_action`), the same
     id space the Approvals screen uses. The tab therefore loads the action list the Approvals screen already reads
     and joins it in the client: the chip shows the action's real status (PROPOSED, APPROVED, REJECTED, ...), and
     "patch #id" without a status when the action is not in that list. Nothing is approved here.
  4. Events feed, filterable by agent and kind.
- Every string in both catalogs (`src/i18n/ko`, `src/i18n/en`); Korean by default. Status words come from the store
  values and are mapped to labels in the catalogs (unknown values are shown as they are).

## 6. Phases

| Phase | Content | Done when |
|---|---|---|
| RM1 | `resident_monitor.py`, `test_resident_monitor.py`, `--resident-monitor` / `--resident-mail-body` | unit tests pass; the read-only tests below pass; one `--resident-monitor --json` run on the Mac stays well under a second (cost check below) |
| RM2 | Go routes and handler test | `go test` covers the id check and the CLI arguments |
| RM3 | Workflows tab, ko/en strings | `scripts/ci.sh web` passes |
| RM4 | Check in a real browser against a local hub with an isolated `GUARDIAN_DATA_DIR` filled with `node-1` fixtures | screen shows agents, turns, mail, tasks, events; 390 px width looked at |

RM1-RM3 deploy together. `guardian.py` is edited only after the S1 work in progress on it is committed.

## 7. Tests

- Read-only: on a missing store, `snapshot()` returns `store.exists: false` and the file does not exist afterwards.
  On an existing store, `MailStore._open`, `MailStore._ready` and `resident_registry.load_declarations` are patched
  to raise while `snapshot()` and `mail_body()` run (the stamp write at the end of `_init_schema` is an
  `INSERT OR REPLACE`, so a file-size check alone could miss a call); the `meta` stamp, file size and `st_mtime_ns`
  are unchanged afterwards; a write through the returned connection fails.
- Shape: a store filled through the real `MailStore` API (two agents, one open turn, mail in each state, an open and
  a completed task with a patch, events) gives the documented keys; limits cut the newest entries.
- No content: a unique marker string is put in one mail body and one task patch; it appears nowhere in
  `json.dumps(snapshot())`, events included, and does appear in `mail_body()` of that mail. `bytes` of a body with
  non-ASCII text equals its UTF-8 length.
- Older schema: a store without the version-4 patch columns gives `patch_bytes`/`patch_action` as `null` and a
  `problems` line for the stamp; a missing table gives an empty section and a `problems` entry.
- Cost: `guardian.py --resident-monitor --json` imports the monitor lazily (inside its flag branch, like the other
  resident commands). For scale, measured 2026-11-21 on the Mac: `--teams --json` 0.16 s, `--workers --json` 2.8 s
  (it probes the hosts); the monitor must stay in the first group.
- A declared agent missing from the store and a stored agent missing from the declarations both show with the flags.
- Go: ids that are not `m-YYYYMMDD-NNNN` give 400 without spawning the CLI.

## 8. Open points

1. Polling interval: 10 s proposed; 5 s would cost about one more spawn every 5 s per open tab.
2. Mail bodies: proposed to show them through `audit_run.mask_secrets` (values of credential-looking assignments
   and private key blocks become `***`). Checked 2026-11-21: of the repository's helpers only that one scrubs
   content; `publish_ops.scrub` masks only credentials in URLs of git output, `code_index.redact_literals` blanks
   every string literal (unreadable for mail). The turn-summary filter of `operator-sessions.md` is not built; when it
   is, the body route can switch to it. The alternative is "as stored", since the dashboard is behind `AGENT_TOKEN`
   and the operator can already read the bodies with `--mail-list`.
3. Relay health (is `guardian-mail-relay.service` active?) is not in the store. A later phase can add it from the
   Go side (systemd is readable there) rather than from the CLI.

## 구현 메모

- 2026-11-21: open point 2 decided by the operator with the design's approval: mail bodies are shown through
  `audit_run.mask_secrets`. Open point 1 stays at the proposed 10 s; the tab stops polling while the browser tab is
  hidden.
- 2026-11-21: the design first said a mail id is a positive integer. It is a string `m-YYYYMMDD-NNNN`
  (`mail_store._mail_id`). Sections 4 and 7 now say so; the Go route checks `^m-[0-9]{8}-[0-9]{4,9}$`, the Python side
  `^m-\d{8}-\d{4,}$` with `re.ASCII` (so full-width digits are refused, like Go).
- 2026-11-21: `guardian.py` wiring: `handle_resident_monitor` runs before `handle_resident_commands`, so the monitor
  never opens a `MailStore` or loads declarations through `resident_registry.load_declarations`; it calls
  `resident_monitor.cli_monitor` / `cli_mail_body` and exits with their code (1 read error, 2 bad id, 3 not found).
- 2026-11-21: RM3 (web tab, built by z.ai, reviewed by claude). Deviations from section 5: a mail body opens one at a
  time below the agent's mail table, not inside its row; turn kind, `end_reason` and event kind are shown raw in mono,
  only agent, mail and task states get catalog labels (agent state `bind_failed` added in review). Review fix: the
  events feed reversed the list, but the snapshot already sends events newest first (`ORDER BY id DESC`); the reverse
  and the oldest-first test fixture were removed.
- 2026-11-21: RM1 (`resident_monitor.py`, built by z.ai, reviewed by claude, 20 unit tests). The store is opened as
  `file:...?mode=ro` and never through `MailStore` (which would create or migrate it); every SELECT takes only the
  columns `PRAGMA table_info` reports and fills the rest with `NULL`, so an older schema (for example version 3 tasks
  without patch columns, or a missing `events` table) still answers, with the gap named in `problems`. The declaration
  file is read as plain JSON instead of `resident_registry.load_declarations`. `store.path_kind` says whether the
  path came from `GUARDIAN_DATA_DIR` or the default. Open tasks come oldest first, closed tasks are the newest 50;
  event `detail` values go through `mask_secrets` like mail bodies.
- 2026-11-21: RM4 checked in a real browser (built-in pane) against a local hub on the Mac with an isolated
  `GUARDIAN_DATA_DIR` holding three `node-1` agents, mail, three tasks (one with a recorded patch) and events. Seen:
  agent cards with state, epoch and last turn; the task board (the patch's action shows as unknown, since the
  isolated decision DB has no such action, as expected); the events feed newest first with its filters; a mail body
  with `password=***` and the masking note. At 390 px the page itself never scrolls sideways, cards and the task
  board stack in one column and each table scrolls inside its own box. Fixes from the check: short cells (times,
  kinds, states, sizes) broke mid-word at 390 px, so `DataTable` columns gained `nowrap` and a `minWidth` floor for
  free text (subject 160 px, event detail 200 px); the card line "state for N min" now reads "state changed N min
  ago" in both catalogs. The header's hub chip turned offline now and then: the first `/api/status` after a quiet
  spell took about 2 s on the local hub, the dashboard's health timeout; later calls took 0.07 s. The tab does not
  touch that check, so it is a property of the local environment, not of this change.
- 2026-11-21: deployed to the hub as `8e2179d` (no debate or solo run was active). On the hub, with its own token,
  `GET /api/residents/monitor` answered 200 in 0.47 s from the real store (schema 5, three agents `ready`, 3 turns,
  12 mails, 2 tasks, 100 events, no `problems`); a real mail body 200, a malformed id 400, an unknown id 404, no token
  401. The deployed tab itself was not opened in a browser (that needs the operator's sign-in).

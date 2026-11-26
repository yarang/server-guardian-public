# Debate team UI contract (Python CLI ↔ Go hub API ↔ dashboard)

English | [한국어](debate-teams-api.ko.md)

The dashboard calls only the Go agent's (hub's) `/api/*`. The Go agent does not implement the debate engine itself;
it runs `guardian.py` without a shell, with an argument array. Validation rules live in one place only, in Python.

## 1. Python CLI (called by Go)

Working directory: the repository root (hub systemd `WorkingDirectory=/home/ubuntu/server-guardian`).
Invocation: `python3 cli/llm-agent/guardian.py <options>`. stdout is a single JSON value.

| Option | stdin | Success (exit 0) | Validation failure (exit 2) |
|---|---|---|---|
| `--personas --json` | - | `{"personas":[{...persona, "source":"builtin"\|"custom"}], "custom_dir":"..."}` | - |
| `--persona-save --json` | persona JSON | `{"ok":true,"id":"...","warnings":[]}` | `{"ok":false,"error":"..."}` |
| `--persona-delete ID --json` | - | `{"ok":true,"id":"..."}` | exit 2 for a builtin, exit 3 for an unknown id (`{"ok":false,"error":"..."}`) |
| `--teams --json` | - | see below | - |
| `--team-save --json` | team JSON | `{"ok":true,"id":"...","warnings":[...]}` | `{"ok":false,"error":"..."}` |
| `--team-delete ID --json` | - | `{"ok":true,"id":"..."}` | exit 2 for a builtin, exit 3 for an unknown id |

`--teams --json`:
```json
{
  "custom_dir": "/home/ubuntu/.guardian/data/teams",
  "teams": [
    {"id": "cross-review", "name": "...", "source": "builtin|custom",
     "data": { /* raw team file (editor initial value) */ },
     "lineup": { /* normalised lineup, only when valid */ },
     "errors": [], "warnings": []}
  ],
  "catalog": {
    "providers": ["claude", "codex", "agy", "zai", "exaone", "opencode", "ollama", "llm"],
    "installed": ["claude", "codex", "agy"],
    "tools": [{"name": "jev_mcp:run_tests", "risk": "low", "description": "..."}],
    "max_critics": 4, "max_proposers": 2, "max_rounds": 10,
    "categories": ["development", "operations", "design-docs", "general"]
  }
}
```

Storage: the repository's `configs/personas` and `configs/teams` are the shipped defaults (builtin, read-only).
What the UI saves goes to `~/.guardian/data/personas` and `~/.guardian/data/teams` (custom); with the same id the custom one wins.
Deleting a custom one makes the builtin with the same id visible again. Builtins cannot be deleted (exit 2 → 400). For personas, teams,
projects and node groups alike, deleting an id that exists in neither layer is exit 3 → 404.

(2026-10-19: team files may carry `category` (one of `catalog.categories`, otherwise `--team-save` exits 2) and the `created` / `modified` stamped by the engine; see [debate-teams.md](debate-teams.md), "Team file schema".)

Running a debate:
```
guardian.py --debate TASK [--team ID] [--thread THREAD_ID] [--rounds N] --result-file PATH
```
(2026-10-18: Go now also passes `--project=ID` (when the request has `project`) and `--events-file=<job directory>/events.jsonl`. The event contract is in [debate-agent-retrieval.md](debate-agent-retrieval.md). Without `--team`, the team is the thread's stored lineup > the project's `"team"` > `default` ([debate-teams.md](debate-teams.md), "Precedence").)
- stdout/stderr: human-readable progress log (Go saves it to a file and shows it in the UI)
- At the end, the result JSON in PATH (same structure as `--debate --json`: thread_id, team, consensus_reached, final_decision, transcript, log_file)
- exit 0: debate finished (whatever the verdict), exit 2: team validation failed, anything else: error

## 2. Go hub API

Everything sits behind the existing `withAuth` token auth (the global `withSecurity(withAuth(mux))` in `main.go`). On a node without `guardian.py` the answer is `503 {"error":"debate engine is not available on this host"}` (routes wrapped in `h.guard`; `h.guard` only checks that the CLI exists).

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/personas` | CLI output as is |
| POST | `/api/personas` | Body (max 64KB) to `--persona-save` on stdin. Success 200, exit 2 is 400 + `{"error"}` |
| DELETE | `/api/personas/{id}` | `--persona-delete` |
| GET | `/api/teams` | CLI output as is |
| POST | `/api/teams` | `--team-save` |
| DELETE | `/api/teams/{id}` | `--team-delete` |
| POST | `/api/debates` | `{"task", "team"?, "thread_id"?, "rounds"?}` → 202 job summary. 409 if a job with the same `thread_id` is running, 429 above the concurrency limit (both checked under the same lock) |
| GET | `/api/debates` | `{"debates":[job summaries, newest first]}` |
| GET | `/api/debates/{id}` | Job summary + `"log"` (last 64KB) + `"result"` (result JSON when finished, else null) |
| DELETE | `/api/debates/{id}` | Cancel if running (kills the process group) |

Job summary: `{"id","task","team","thread_id","rounds","status","started_at","finished_at","exit_code","error"}`.
(2026-10-18: the summary now also carries `project`, `context_paths`, `context_ref`, `thread_closed`, `thread_published` and `kind` (`"code_wiki"` for a code wiki build job) when they have a value. List entries add `events` (whether an event stream exists) and `retrieval` (the result's code-reading total); the detail adds `retrieval`. The `POST /api/debates` body also takes an optional `"project"` (same id pattern as team); without it the default project `server-guardian` is used.)
`status`: `running` · `completed` · `failed` · `cancelled` · `interrupted` (cut off by an agent restart).
`thread_id` is the request's value for a continuation, and the result's thread_id after completion for a new debate.

Settings (environment variables): `GUARDIAN_CLI` (default `cli/llm-agent/guardian.py`), `GUARDIAN_PYTHON` (default `python3`),
`GUARDIAN_DATA_DIR` (default `~/.guardian/data`, job records in `<DATA>/debate-jobs/<id>/`),
`GUARDIAN_MAX_DEBATES` (default 2), `GUARDIAN_DEBATE_TIMEOUT` (seconds, default 3600).

Input validation: task 1-4000 characters, team `^[a-z0-9][a-z0-9_-]{0,63}$`, thread_id `^thread_[A-Za-z0-9_]{1,80}$`, rounds 1-10, `{id}` the same pattern as team.
(2026-10-18: a debate job's `{id}` (`/api/debates/{id}`) is `^dbt_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$`. Request bodies are limited to 64KB for personas and teams and 16KB for debates.)

Routes outside this table (2026-10-18): `debate_handler.go` also registers projects (`GET`/`POST /api/projects`, `DELETE /api/projects/{id}`, `POST /api/projects/{id}/sync`), node groups (`/api/groups`, [node-groups.md](node-groups.md)), and events, prompts and the result document (`/api/debates/{id}/events`, `/api/debates/{id}/prompts/{seq}`, `/api/threads/{thread}/report`, [debate-agent-retrieval.md](debate-agent-retrieval.md)). The file view and code wiki routes are in `file_handler.go` and `wiki_handler.go` ([code-wiki-coverage.md](code-wiki-coverage.md)).

## 3. Dashboard

- On API failure, show the error; never substitute fake data.
- Personas: list (builtin/custom marked), edit, save, delete custom
- Teams: list (validation errors and warnings shown), editor (persona per role, CLI order, 1-4 critics, 0-2 optional extra proposers, max_rounds, tool checkboxes with risk), save, delete custom
- Debates: pick a team, enter the task, optionally a thread ID to continue → start → poll every 3 seconds and show the progress log → when finished, show the transcript, verdict, jev result and proposed actions; cancel button; recent jobs list
- (2026-10-18: the dashboard is Korean by default and English can be chosen ([dashboard-i18n.md](dashboard-i18n.md)). Screen names in this document such as "Proposed actions" and "Close thread" are the English labels. When the team field of the debate form is left empty, the team is picked by the precedence above.)

## 4. Judge action approval (phase 3)

UI approval uses the same `action_ops.run_action` path as the CLI. Only `PROPOSED` actions can run; they are revalidated right before running,
and the atomic `PROPOSED → RUNNING` transition in the DB prevents a double run. `decided_by` records the approver (`dashboard@<client IP>`).

### Python CLI

Exit codes: 0 success, 2 input error, 3 not found, 4 state conflict (not runnable in this state, taken by another run, revalidation failed).
On error, stdout is `{"ok":false,"error":"..."}`.

| Option | Success output |
|---|---|
| `--actions [THREAD] --json` | `{"actions":[action...]}` (newest first) |
| `--show-action N --json` | `{"action": action}` |
| `--run-action N --yes --json --actor A` | `{"ok": <true if EXECUTED>, "action": action, "result": {...}}` (exit 0 even when the tool fails; `action.status` is `FAILED`) |
| `--reject-action N --json --actor A` | `{"ok":true,"action": action}` |
| `--jev-recheck THREAD --json` | `{"ok":true,"gate":"PASSED\|DISPUTED\|PENDING","released":[id...],"review":{...}}` |
| `--publish-thread THREAD [--branch NAME] --yes --json --actor A` | `{"ok": <true if PUSHED>, "status","branch","commit_sha","files",...}` (exit 0 even when the push fails, `status` `FAILED`; a failed precondition is exit 4). [publish-thread.md](publish-thread.md) |

`--publish-thread THREAD --plan --json [--branch NAME]`: dry run. Checks every precondition before the test gate and writes nothing to git or the DB (no `--yes` needed).
Success exit 0 `{"ok":true,"plan":{"thread_id","project_id","branch","remote","base_sha","files":[...],"subject","retry"}}`,
failed precondition exit 4 `{"ok":false,"error"}`, invalid branch exit 2, unknown thread exit 3.

`action`: `{"id","thread_id","round_num","tool","arguments","reason","status","result","decided_by","created_at","updated_at","risk","tool_description"}`.
`status`: `PROPOSED` · `HELD` (waiting for the jev gate) · `RUNNING` · `EXECUTED` · `FAILED` · `REJECTED` · `INVALID`.
(2026-10-18: cleanup actions of ops projects add the `STALE` status (caught by the drift check right before running) and `preview`, the measurement taken when proposed; the `risk` of a catalog action is computed from that action's arguments. See [server-ops-project.md](server-ops-project.md) chapter 14, P3.)
`--actor`: `^[A-Za-z0-9@:._-]{1,64}$`.

### Go hub API

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/actions?thread=THREAD` | `--actions [THREAD] --json` (2026-10-18: also filters by `project=ID` → `--project=ID`) |
| GET | `/api/actions/{id}` | `--show-action` |
| POST | `/api/actions/{id}/run` | Body `{"confirm_id": <id>}` must equal the path id. Runs synchronously (keeps running if the request drops; `GUARDIAN_ACTION_TIMEOUT` default 900 s). 403 when `GUARDIAN_UI_ACTIONS=off` |
| POST | `/api/actions/{id}/reject` | `--reject-action`. 403 when `GUARDIAN_UI_ACTIONS=off` |
| POST | `/api/actions/{id}/fail-stale` | Body `{"confirm_id": <id>}` must equal the path id (else 400). 409 if this hub process is running that action (the run is still alive). Otherwise `--fail-stale-action ID --json --actor A` → 200 `{"ok":true,"action":{...}}` / 404 (not found, exit 3) / 409 (not RUNNING, exit 4). Moves RUNNING to FAILED, records `marked_failed_by/at` and leaves `decided_by` alone. A run that finishes late keeps its result as `late_result`. 403 when `GUARDIAN_UI_ACTIONS=off` |
| POST | `/api/threads/{thread}/jev-recheck` | `--jev-recheck` (timeout 300 s). 403 when `GUARDIAN_UI_ACTIONS=off` |
| POST | `/api/threads/{thread}/publish` | Optional body `{"branch": "..."}` → `--publish-thread` (timeout 1200 s). 403 when `GUARDIAN_UI_ACTIONS=off` |

| GET | `/api/threads/{thread}/publish-plan?branch=NAME` | `--publish-thread --plan --json` (read-only; `branch` optional, validated with the same pattern). Allowed even with `GUARDIAN_UI_ACTIONS=off`. 200 `{"ok":true,"plan":{...}}` / 400 / 404 / 409 `{"error"}` |

Exit code mapping: 0→200, 2→400, 3→404, 4→409, anything else→500. `{id}` is a positive integer, `{thread}` is `^thread_[A-Za-z0-9_]{1,80}$`.
Go builds `--actor` as `dashboard@<host of RemoteAddr>` (not user input).

### Dashboard

- A separate "Proposed actions" area next to the action list of a debate result (thread filter, status badges).
- Action detail: tool, risk, reason, all arguments (the full file content for `execute_code_action`), previous result.
- `PROPOSED`: run and reject. The confirmation before running shows the arguments again; for high risk (`server_remediation`) the run button is enabled only after the action number is typed in.
- `HELD`: a "jev recheck" button (per thread). If it passes, the action becomes `PROPOSED`.
- Buttons are locked while running; afterwards the result (stdout/stderr/exit code and so on) is shown.

## 5. Code bundles (how debate agents read code)

Design: [debate-code-context.md](debate-code-context.md). Guardian takes **committed** files from the working clone (`GUARDIAN_WORKSPACE`) and puts them into the prompt.

### Python CLI

- `--debate ... --context-path GLOB` (repeatable, up to 20, each 1-200 characters, `**` supported).
  Selectors: `GLOB#Name` / `GLOB#Type.Method` (Go top-level func, method, type, var, const; Python def, class, method),
  `GLOB:START-END` (line range). A partly selected file comes with an outline (imports and top-level declarations, with line numbers).
  When a whole-file selection and a partial selection of the same file overlap, the whole file wins. `--context-ref REF` (default `HEAD`, `^[A-Za-z0-9._/~^-]{1,100}$`, not starting with `-`)
- Optional team file field: `"context": {"paths": [GLOB...], "ref": "HEAD"}`. Paths in the request win.
- When continuing with `--thread` without paths, the thread's previous paths and commit are reused.
- A path validation failure or a ref that cannot be found is exit 2 (the same code as a team validation failure).
- The result JSON gains `"context"` (`null` without paths):
  ```json
  {"ref": "HEAD", "sha": "<40 chars>", "paths": ["apps/agent/*.go"],
   "files": [{"path": "apps/agent/main.go", "bytes": 10240, "truncated": false}],
   "skipped": [{"path": "secrets.enc", "reason": "secret pattern"}],
   "total_bytes": 10240, "tree_files": 87}
  ```
  `skipped.reason`: `secret pattern` · `symlink` · `binary` · `limit` (file count or total size exceeded) · `symbol not found` (no file matching the selector's glob has that symbol; `path` is the whole selector).
  The `files` entry of a partly selected file gains `"parts": ["#executeSSH", "lines 110-145"]` and `"outline": true|false`.
- The action JSON gains `"base_sha"` (string or null). An action that changes files is refused when the working clone's HEAD differs from `base_sha`
  (exit 4, 409): `"workspace moved from <base> to <head>; debate again on the current code"`.
- Each team's `data`/`lineup` in `--teams --json` may contain `context`.

### Go hub API

- Optional fields in the `POST /api/debates` body: `"context_paths": [string]` (up to 20, each 1-200 characters, no NUL or newline, not starting with `-`),
  `"context_ref": string` (pattern above). They are passed to the CLI as single arguments `--context-path=<p>`, `--context-ref=<r>`.
- The job summary carries `context_paths` and `context_ref` as given (the result's `context` is in `result`).

### Dashboard

- Debate console: a "Reference code" text area (one glob per line) and a ref input (empty means HEAD).
- Team editor: default reference paths (one glob per line) and ref.
- Result screen: a summary "Code read: `<7-char sha>` · N files · X KB"; expanded, the file list (truncation marked) and the skipped list (with reasons).
- Action detail: shows `base_sha`; for file-changing actions it notes that "the run is refused if the working clone has moved from this commit".

## 6. Judge output: decision requirements separate from implementation checks

The judge's JSON block is `{"decision", "constraints", "checks", "actions"}`.

| Field | Meaning | Used by |
|---|---|---|
| `decision` | The judge's complete final approach (including what was adopted from the critique, with reasons and trade-offs, up to 3000 characters) | The jev request's `chosen` (what is reviewed). Falls back to the verdict text, then to the proposer's original |
| `constraints` | 1-8. Each must restate a sentence of the decision (no detail that appears only in the requirements), and they must not contradict each other or the actions | jev verifies them one by one |
| `checks` | Items to verify in the code after the actions run | Not sent to jev. Stored in `final_decision.checks` and DB `decisions.implementation_checks`, shown in the dashboard |
| `actions` | Proposed actions | Run after a person approves. Also summarised (tool, target, content up to 600 characters) in the jev request `context`, so jev can find actions that disagree with the decision |

Previously jev's `chosen` was the proposer's original. Because jev compared requirements the judge had changed by adopting critique against that original,
all three comparison debates of 2026-10-04 ended `DISPUTED` (for example the original's "clamp the excess value" against the verdict's "reject with 400").

The result JSON's `final_decision` gains `decision` (string) and `checks` (list of strings).

In the fourth comparison debate of 2026-10-04, jev passed the requirements written in the decision (defaults, 400 rejection, killing the process group),
and judged "uncertain" the details that were only in the requirements and not in the decision summary (output limit, cgroup, remote supervision details).
The judge is therefore told to write a complete decision and to limit the requirements to restatements of its sentences.

## 7. Partial edit tool `jev_mcp:apply_patch`

Design: [debate-patch-tool.md](debate-patch-tool.md).

- Arguments: `{"edits": [{"file_path", "old", "new"}], "check": bool?}`. `old == ""` creates a new file.
- The action JSON (`--show-action --json`, `GET /api/actions/{id}`) gains `"diff"` (string, unified diff against the base commit).
  Only `apply_patch` and `execute_code_action` have it, and only when it can be built. It is not in the list (`--actions`).
- Run result (`action.result`):
  ```json
  {"status": "SUCCESS|FAILED", "message": "applied 3 edits to 2 files",
   "diff": "<unified diff actually applied>",
   "checks": [{"cmd": "go build ./...", "cwd": "apps/agent", "exit_code": 0, "stdout": "", "stderr": ""}],
   "reverted": false, "error": "<failure reason, on failure>"}
  ```
  If any check fails, `status: FAILED`, `reverted: true`, and the files are restored.
- `jev_mcp:apply_patch` is added to the judge persona's and the `cross-review` team's allowed tools.

## 8. Closing a thread (`CLOSED`)

A thread is not deleted but set to `CLOSED`. Messages, decisions, jev reviews, actions and agent sessions stay; it is only hidden from the dashboard's default lists.

- **Python CLI**: `guardian.py --close-thread THREAD [--json]`. Sets `threads.status` to `CLOSED` and records the time in `threads.closed_at` (added to existing DBs by `ALTER TABLE` at startup).
  If it is already `CLOSED`, nothing changes and `already_closed: true` (idempotent). An unknown thread is exit code 3.
  Success output: `{"ok":true,"thread_id","status":"CLOSED","previous_status","already_closed","closed_at"}`. Error: `{"ok":false,"error":"thread X not found"}`.
- **Reopening**: continuing a `CLOSED` thread with `--debate ... --thread THREAD` is not blocked. When the debate ends, `save_debate_session` writes the status as usual and clears `closed_at`.
  `--jev-recheck` updates the jev review and the `HELD` actions but does not change the `CLOSED` status.
- **Lists**: `DecisionDBManager.list_project_threads(project, include_closed=True)` shows the status; `include_closed=False` leaves out `CLOSED`.
- **Go hub API**: `POST /api/threads/{thread}/close` → `--close-thread THREAD --json`. `{thread}` is validated like `jev-recheck`. 403 when `GUARDIAN_UI_ACTIONS=off`.
  409 if a debate continuing this thread is running on this hub. An unknown thread follows the CLI's exit code 3 to 404 (reason in `error`).
  The dashboard lists are built from debate jobs (`debate-jobs/*/meta.json`), so a successful close records `"thread_closed": true` on every job with that `thread_id`,
  and a later normal finish of a debate on that thread clears it. A thread closed directly with the CLI has no such mark (only the DB status is `CLOSED`).
- **Dashboard**: results that are not `APPROVED` have a "Close thread" button (with confirmation). "Recent debates" and the overview's recent debates hide closed threads by default;
  the "Show closed" checkbox on the debates screen shows them (with a `CLOSED` badge). A closed thread's result offers "Reopen with a new debate" (= continue the thread). The Agents monitor is a run history and does not hide them.
- **Closing only hides (decision D10)**: `CLOSED` only hides the thread in lists. The thread's actions (`PROPOSED`/`HELD`/`EXECUTED`) and publication records stay, and closing neither blocks nor cancels running, rejecting or publishing. To settle the actions, run or reject them before closing.
- **Closed thread list**: `GET /api/threads/closed` → 200 `{"threads": ["thread_..."]}` (sorted). Reads the `--closed-threads --json` result through the same 30-second cache as `GET /api/debates` (invalidated by a successful close); 503 without the CLI, 500 if the DB cannot be read.

## 9. Publication records and working clone reset

### Publication records (decision D13)

- CLI: `guardian.py --publications THREAD --json` → exit code 0 `{"publications": [{"id","branch","commit_sha","remote","status","error","actor","created_at"}]}`, newest first. Unknown thread is 3.
- Go: `GET /api/threads/{thread}/publications` (`h.guard`, exit code mapping as above). A thread can be published successfully only once (decision D11, `publish-thread.md`).
- **A published thread cannot be continued**: `guardian.py --debate T --thread X` prints `thread X was published as branch B; start a new thread` with exit code 4 when X has a `PUSHED` publication, and `POST /api/debates` (with `thread_id`) answers 409 with the same text and creates no job.
  `guardian.py --published-threads --json` → `{"ok":true,"threads":[ids]}`. Each entry of `GET /api/debates` gets `"thread_published": true` the same way as `thread_closed` (30-second cache, invalidated by a successful publication).

### Working clone reset (decision D12)

- CLI plan: `guardian.py --workspace-reset PROJECT --plan --json` → 0 `{"ok":true,"project_id","workspace","discard":[{"path","state"}],"keep":[{"path","action_id","thread_id"}]}`.
  `state` is `modified`, `untracked` or `deleted`. `keep` holds the currently changed paths among the `file_hashes` recorded by `EXECUTED` file actions that are not yet published (of threads without a `PUSHED` record); `action_id` is the action that last wrote that path. `discard` is every other changed or untracked path.
- CLI run: `guardian.py --workspace-reset PROJECT --yes --json [--actor A]` → 0 `{"ok":true,"discarded":[paths]}`. Only the paths in the `discard` list are restored with `git checkout HEAD --` and `git clean -f --` (staged new files first get `git rm --cached`); no whole-tree clean or `git add -A` is used, and `keep` paths are not touched. It takes the same working clone lock as publish and sync (4 if busy), and refuses with 4 when the clone is not on its tracked branch or is the running repository itself. `--plan` checks the same preconditions. Unknown project 3, invalid input 2.
- **Release per thread (`--release`)**: plan `keep` entries are `{"path","action_id","thread_id"}` and can be grouped by thread. `--workspace-reset PROJECT --yes --release THREAD [--release THREAD2 ...]` (API body `{"confirm": true, "release": ["thread_..."]}`, up to 20)
  also discards the files those threads were keeping. For every `EXECUTED` file action of a released thread, `released_by` and `released_at` (UTC) are recorded in the result (the status stays), later reset plans no longer put it in `keep`, and publishing that thread is
  refused with `thread T was released: ... start a new thread` (409). A thread with no file action to release, or one already published, is 4 (nothing changes). It cannot be combined with `--plan` (2). The response is `{"ok":true,"discarded":[paths],"released":[threads]}`.
- **Coordination with running actions**: if the project has a `RUNNING` action, both plan and run are refused with 4/409 `actions are running in this project: #12, #15` (clear stuck actions with `--fail-stale-action`). File actions (`execute_code_action`, `apply_patch`) hold the same working clone lock as publish, sync and reset while they run (file writes and `run_test`), so they cannot interleave; if the lock is busy, the action is refused with 4 before it becomes `RUNNING`.
- Go: `GET /api/projects/{id}/workspace-reset` (plan; read-only, so allowed even with `GUARDIAN_UI_ACTIONS=off`) and `POST /api/projects/{id}/workspace-reset` (body `{"confirm": true}` required, else 400; 403 when `GUARDIAN_UI_ACTIONS=off`). Both use `h.guard`; exit codes are mapped by `respondConfig`.

# Direct Querying of Active Resident Agent Sessions (Design)

English | [한국어](resident-session-query.ko.md)

Status: Decided 2026-11-22 (QD1–QD5 approved by the operator; Phases Q1–Q4 implemented).

This document designs a safe, hub-brokered path for querying an active resident agent's live provider session
(`claude` `session_id`, `agy` `conversation_id`, `codex` `thread_id`) created and maintained by
[resident-agents.md](resident-agents.md), [resident-open-tools.md](resident-open-tools.md), and
[resident-codex-and-policy.md](resident-codex-and-policy.md).

## 1. Problem and current gaps

Resident agents on Tier-1 worker nodes (`node-1`, ...) run under `HeadlessDriver` (`cli/llm-agent/resident_driver.py`)
and maintain a persistent provider session across turns (`agents.session_id`, `incarnations.session_id`, and node
`state.json`). Today, an operator cannot directly access and query that active session for four reasons:

1. **Mailbox-only indirection.** Callers must send asynchronous mail (`--mail-send` or `session_send`), wait for a
   mailbox wake, and hope the agent replies via `mail_send`.
2. **Direct assistant output is discarded.** `_claude_result`, `_agy_result`, and `_codex_result` in
   `cli/llm-agent/resident_driver.py` parse the assistant text (`_text`) and throw it away.
3. **Direct SSH `--resume` breaks concurrency and isolation.**
   - Resuming the same `session_id` while `HeadlessDriver` runs a mailbox turn corrupts CLI session files or triggers
     `resume_new_session` conflicts, and bypasses the single resident slot (D5), turn budget, and `GUARDIAN_RESIDENT=off`.
   - `tools: "open"` agents store their session and credentials inside `0700` `/home/gsa-<name>` and run inside the
     `guardian-sandbox-run` transient systemd unit; running outside breaks the sandbox boundary.
   - `tools: "closed"` agents rely on isolated homes (`hooks.json`, `--tools ""`, `--strict-mcp-config`).
   - Star topology forbids direct workstation-to-node SSH.
4. **Session readiness ambiguity.** For `claude`, `HeadlessDriver._bind()` pre-generates a UUID `session_id` while
   `first_turn = True` before the first turn creates the session on disk. Checking `agents.session_id IS NOT NULL` alone
   cannot prove that a live provider session already exists.

## 2. Chosen architecture: hub-brokered in-place session query

Three approaches were compared:

- **Hub-brokered direct in-place query (chosen, QD2):** The hub records a project-scoped query in `mail.db` and
  dispatches it over the existing worker protocol 3 SSH relay stream (`mail_relay.py` ↔ `resident_node.py`). The node's
  `HeadlessDriver` executes the query on the agent's existing active session (`--resume` / `--conversation` /
  `codex exec resume`) using the exact same resident slot (D5), epoch fencing, and `closed` or `open` sandbox path, then
  returns the assistant's direct text output through `turn_end`.
- **Forked read-only query (`mode: "fork"`, deferred):** Branches off a session snapshot without mutating the resident
  conversation history (e.g., Claude `--fork-session`). Deferred because `agy` and `codex` support is unprobed; the
  schema keeps a `mode` column (`"in_place"` only in this design).
- **Interactive terminal attach (`tmux attach`, non-goal):** Requires quiescing `HeadlessDriver`, breaks star topology,
  and widens the sandbox attack surface for `tools: "open"`.

### End-to-end flow (2-phase protocol over the 20 s relay contract)

Relay requests enforce `REQUEST_TIMEOUT = 20.0` s (`mail_relay.py`, `resident_node.py`), whereas a single LLM turn can
run for minutes. The wire protocol therefore separates **submission/acknowledgement** (< 1 s) from **turn execution and
completion**:

```text
Caller (Dashboard project view / Hub CLI --project <id>)
        |  POST /api/residents/queries  (or guardian.py --resident-query --project <id>)
   Hub (mail.db WAL: session_queries, status='queued', project validated)
        |  202 Accepted {id, address, project, epoch, session_id, status}
   Hub mail_relay.py (2 s poll, existing worker protocol 3 SSH stream)
        |  Hub -> Node:  {"t": "query", "id": "hN", "agent": ..., "epoch": E,
        |                 "query_id": "q-...", "expected_session_id": sid,
        |                 "project": pid, "project_kind": kind, "task": tid?,
        |                 "caller": "operator", "caller_kind": "operator",
        |                 "mode": "in_place", "prompt": "...", "timeout_sec": 180}
        |  Node -> Hub:  {"t": "reply", "re": "hN", "ok": true, "result": "accepted"|"duplicate"}
   Node ResidentManager -> HeadlessDriver
        |  1) admit(kind="query", query_id) + acquire node resident slot (D5)
        |  2) _open_pre_turn / _pre_turn_checks (identical to mailbox turns; no fallback)
        |  3) CliRunner.run(ctx, wrapped_prompt) -> TurnResult(ok, session_id, text)
        |  4) mask_secrets + truncate to 64 KiB; persist in pending_acks if stream is down
        |  5) turn_end(end_reason, session_id, query_id, query_reply={status, answer, truncated, error})
   Hub mail_relay.py -> mail_store.py (BEGIN IMMEDIATE)
        |  double mask_secrets, update session_queries(completed|failed), close turn
   Caller polls GET /api/residents/queries/{id} (SQLite mode=ro)
```

## 3. Project-scoped access model (QD1)

Direct session querying is strictly anchored to **projects registered on the hub** (`cli/llm-agent/project_registry.py`:
`code`, `ops`, or `design`):

1. **Mandatory `project` parameter.** Every query must specify a `project` id that exists in `ProjectManager`. Missing
   or unknown projects are refused fail-closed (`unknown_project`, HTTP 400). There is no project-less global direct
   query.
2. **Project-level agent allowlist (optional policy key).** Extend `POLICY_KEYS` in `project_registry.py` with optional
   `resident_query_agents` (list of resident addresses):
   - Omitted (`None`): any declared resident agent on the hub may be queried under this project.
   - Explicit list (`["claude@node-1", "coder@node-1"]`): only those agents may be queried under this project.
   - Empty list (`[]`): direct resident session queries are disabled for this project.
3. **Allowed callers.**
   - **Phases Q1–Q3:** Hub operator only (`AGENT_TOKEN` via dashboard project view or hub CLI `--project <id>`).
   - **Resident agents:** Never permitted to issue direct session queries (they use mailbox/task board only, preserving
     hop limits and preventing model ping-pong).
   - **Workstation operator sessions (future optional phase Q4):** If later enabled via `session_mcp.py`, a session
     must still specify a valid hub `project` id, pass `peers` and `resident_query_agents`, and carry
     `caller_kind = "session"` with a non-authority warning preamble.
4. **Optional task reference (`task`).** Under a `code` project, the caller may optionally reference an existing task id
   belonging to that `project` (validated on the hub) so the query header explicitly points the resident session at the
   task context.

## 4. Active-session precondition (fail-closed)

A query is accepted **only** when the target resident agent already has a live, created provider session in its current
epoch:

- **Hub (`mail_store.py`):**
  - Add `session_ready INTEGER NOT NULL DEFAULT 0` to `incarnations`.
  - Flip `session_ready = 1` on the open incarnation only when a turn finishes with `end_reason = 'normal'` and a
    non-empty `session_id` is recorded.
  - Refuse query creation unless `agents.state == 'ready'`, `agents.session_id IS NOT NULL`, and the current
    incarnation has `session_ready == 1` (error: `no_active_session` or `agent_not_ready`).
- **Node (`resident_node.py`, `resident_driver.py`):**
  - Refuse a `query` frame unless `agent.ready`, `agent.epoch == frame.epoch`, `state.session_id == expected_session_id`,
    `not state.first_turn`, and `not state.fresh`.
  - If the CLI reports `missing_session` or returns a different `session_id` (`resume_new_session`) during a query turn,
    the driver runs its normal rebind path (`force_fresh = True`) and completes the query as
    `failed(error="session_lost")` instead of answering from a blank conversation.

## 5. Data model (`mail.db` schema 6 → 7)

Migrations in `cli/llm-agent/mail_store.py` remain additive so `resident_monitor.py` (`PRAGMA table_info` intersection)
works across schema versions without modification:

```sql
ALTER TABLE incarnations ADD COLUMN session_ready INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS session_queries (
    id TEXT PRIMARY KEY,                     -- q-YYYYMMDD-NNNN
    address TEXT NOT NULL,                   -- e.g. claude@node-1
    project TEXT NOT NULL,                   -- hub project id (code|ops|design)
    project_kind TEXT NOT NULL,              -- snapshot of project kind at submit time
    task TEXT,                               -- optional task id in the same project
    epoch INTEGER NOT NULL,                  -- incarnation epoch fencing
    session_id TEXT NOT NULL,                -- expected provider session id
    caller TEXT NOT NULL,                    -- 'operator' (or '<name>@<workstation>' in Q4)
    caller_kind TEXT NOT NULL,               -- 'operator' | 'session'
    mode TEXT NOT NULL DEFAULT 'in_place',   -- 'in_place'
    prompt TEXT NOT NULL,                    -- <= 32 KiB
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|dispatched|running|completed|failed|expired|cancelled
    turn_id TEXT,                            -- linked turns.id
    answer TEXT,                             -- mask_secrets filtered, <= 64 KiB
    answer_truncated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    timeout_sec INTEGER NOT NULL DEFAULT 180,
    created TEXT NOT NULL,
    started TEXT,
    completed TEXT,
    expires TEXT NOT NULL,                   -- default created + 10 min (unstarted expiry)
    FOREIGN KEY(address) REFERENCES agents(address),
    FOREIGN KEY(turn_id) REFERENCES turns(id)
);
CREATE INDEX IF NOT EXISTS idx_session_queries_dispatch
    ON session_queries(address, status, created);
```

- Add `"query"` to `TURN_KINDS` (`turns.kind = 'query'`, `hop_in = 0`).
- **Epoch fencing on state transitions:** When `bind_begin`, `retire`, or `agent_down` (after 10 min or rebind) changes
  the agent's epoch or retires the agent, any `queued`/`dispatched` queries from the older epoch are marked
  `failed(error="epoch_changed")` or `failed(error="agent_retired")`.
- **Retention (QD5):** 30 days (matching D7 for mail and tasks), included in the nightly backup of `mailbox/mail.db`.

## 6. Relay protocol and node driver details

### Relay contract (worker protocol 3 compatible)
- `hello` adds `"session_query": true`. Nodes without it fail query creation with `node_without_session_query`.
- **Hub → Node:**
  - `query`: validates and stores `<agent_dir>/queries/<query_id>.json` (`0600`), replies immediately with
    `accepted` / `duplicate` or an error code (`no_active_session`, `epoch_mismatch`, `not_ready`, `queue_full`).
  - `query_cancel`: removes an unstarted `query_id` from the local queue (idempotent).
- **Node → Hub:**
  - `admit`: carries optional `query_id` when `kind == "query"`. Hub atomically checks query state (`queued`/`dispatched`),
    epoch match, `GUARDIAN_RESIDENT != off`, and query hourly budget, then sets `status = 'running'` and `turn_id`.
  - `turn_end`: carries optional `query_id` and `query_reply = {status, answer, truncated, error}`.
  - **Reconnect durability:** If the SSH stream drops during a query turn, `query_id` and `query_reply` are stored inside
    the existing `pending_acks.json` entry and reconciled during the `sync` handshake so the answer is never lost.

### Driver rules (`cli/llm-agent/resident_driver.py`)
1. **Capture assistant output:** Add `text: str = ""` to `TurnResult` and populate it in `_claude_result`, `_agy_result`,
   and `_codex_result` (currently parsed and discarded).
2. **Scheduling priority (QD3):** `_maybe_turn` checks in order:
   1. `fresh` (start card)
   2. `resume_pending`
   3. Queued direct `query` (FIFO) — **prioritized over mailbox wakes** so a waiting human is not queued behind
      background mail traffic. To prevent starvation under bursty queries, if unread mail is pending after 2 consecutive
      query turns, the driver schedules 1 mailbox turn before taking the next query.
   4. Mailbox `wake` / `continuation` / `sweep`.
3. **Avoid false `stalled` exits:** A `query` turn is not expected to call `mail_inbox`. Therefore, `state.stalled` MUST
   NOT increment after a `kind == "query"` turn even when unread mail exists in the spool.
4. **Prompt wrapping (stdin only, never argv/TUI):**
   ```text
   [guardian-session-query id=q-20261122-0001 project=server-guardian kind=code from=operator]
   Direct query from the hub under project "server-guardian" to your active resident session.
   Answer directly in your final assistant text response.
   Do not send mail or create tasks unless explicitly requested. Start-card rules still apply.
   ---
   <user prompt>
   ```
   For `agy` and `codex`, `ctx.card` continues to be prepended ahead of the wrapped prompt; for `claude`, `ctx.card`
   continues to ride in `--append-system-prompt`.
5. **Output scrubbing and sizing:** Before sending `query_reply`, the node truncates `answer` to 64 KiB UTF-8
   (`truncated: true` if cut) and runs secret masking; the hub applies `audit_run.mask_secrets` again before persisting
   and on read. Neither `daemon.log` nor `mail_relay` stderr ever logs prompts or answers.
6. **Timeout:** Each query turn uses `min(query.timeout_sec, turn_timeout())` (default 180 s, cap 600 s). For `open`
   agents, `guardian-sandbox-run --timeout` and `--stop` cleanup apply unchanged.

## 7. Budgets, limits, and kill switches (QD4)

| Limit | Default | Enforcement point |
|---|---|---|
| Direct queries per agent per hour (separate from the 12-turn mailbox budget, QD4) | 20 | Hub `admit` (and pre-check on submit) |
| Pending (`queued`/`dispatched`/`running`) queries per agent | 5 | Hub submit + Node local queue |
| Prompt size | 32 KiB | Hub submit + Go API |
| Answer size | 64 KiB (truncated) | Node exit + Hub store |
| Unstarted query TTL | 10 min (`expired`) | Hub lazy sweep + Node drop |
| Single query execution timeout | 180 s (max 600 s) | Node `CliRunner` / sandbox wrapper |
| Kill switch | `GUARDIAN_RESIDENT=off` | Hub submit & `admit` immediately refuse |
| Cancellation | `queued` / `dispatched` only | Never kill an active sandbox turn mid-flight (prevents session corruption) |

## 8. Interfaces (CLI, Go API, Dashboard)

### Hub CLI (`cli/llm-agent/guardian.py`)
```bash
# Submit a direct query under a hub project and wait for the answer (default --wait)
python3 cli/llm-agent/guardian.py --resident-query claude@node-1 \
  --project server-guardian \
  --prompt "Summarize what you checked in your current session." \
  [--task <task-id>] [--timeout 180] [--no-wait] [--json]

# List recent queries (metadata only, optional project/agent filter)
python3 cli/llm-agent/guardian.py --resident-queries [--project server-guardian] [claude@node-1] [--json]

# Read one query with masked prompt & answer (mode=ro)
python3 cli/llm-agent/guardian.py --resident-query-get q-20261122-0001 [--json]

# Cancel an unstarted query
python3 cli/llm-agent/guardian.py --resident-query-cancel q-20261122-0001
```
Wait mode polls SQLite via `mode=ro` every 0.5–1.0 s until a terminal status or `--timeout` elapses.

### Hub Go API (`apps/agent/debate_handler.go`)
All endpoints sit behind the existing global `withSecurity(withAuth(mux))` (`AGENT_TOKEN`):
- `POST /api/residents/queries` — body `{address, project, prompt, task?, timeout_sec?}` → `202 Accepted` with
  `{id, address, project, epoch, session_id, status}`. (Putting `address` in the JSON body avoids `@` path-encoding
  issues and matches `POST /api/debates` and `POST /api/solo`.)
- `GET /api/residents/queries?project=<id>&agent=<addr>&limit=<n>` — read-only (`mode=ro`) metadata list without bodies.
- `GET /api/residents/queries/{id}` — validates `^q-[0-9]{8}-[0-9]{4,9}$`, returns masked prompt & answer (`mode=ro`).
- `DELETE /api/residents/queries/{id}` — cancels a `queued`/`dispatched` query.
- `GET /api/residents/monitor` — adds `session_ready: bool` and `queries_pending: int` to each agent card, and recent
  query metadata (no prompt/answer bodies) to the snapshot.

### Dashboard (`apps/web-desktop/src/pages/residents/ResidentAgentDetail.tsx`)
- Add a **"Session Query"** panel inside **Workflows > Resident agents** (`#/debates/residents?agent=<address>`).
- Displays the currently selected hub project (`project_id`, kind badge) and binds submitted queries to that project.
- Enabled only when `agent.state === "ready" && agent.session_ready` (otherwise shows the exact reason: binding, no
  active session yet, or down).
- Submitting triggers `POST /api/residents/queries`, polls `GET /api/residents/queries/{id}` every 2 s while visible
  until completion, renders the answer with turn ID / epoch / elapsed time / truncation notice, and shows the project's
  recent query history (bodies loaded on click).
- All UI copy added to both `src/i18n/ko` and `src/i18n/en`.

## 9. Security invariants and risks

| Risk | Mitigation |
|---|---|
| Direct query bypasses `open` sandbox or `closed` tool closure | Single `CliRunner` path with `_open_pre_turn` / `_pre_turn_checks`; zero fallback to unsandboxed or open node-user execution |
| Concurrent resume corrupts CLI session files | Serialized by node's single resident slot (D5) and `HeadlessDriver`'s single `turn_thread` |
| Stale query lands in a fresh session after rebind | Fenced by `(epoch, expected_session_id)` on both hub and node; `missing_session` / `resume_new_session` fails the query as `session_lost` |
| Secrets leak in query responses or logs | Double `mask_secrets` (node exit + hub store/read); zero prompt/answer logging in `daemon.log` or `mail_relay` stderr |
| Direct query falsely triggers `stalled_turns` exit | `kind == "query"` excluded from spool unread stall counting |
| Unscoped or cross-project abuse | Mandatory registered hub `project` id + optional `policy.resident_query_agents` allowlist + no agent-to-agent direct queries |
| Quota exhaustion | Separate 20/hour query budget per agent, queue cap 5, 32 KiB/64 KiB size caps, `GUARDIAN_RESIDENT=off` kill switch |

## 10. Phased implementation plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **Q1 — Store, Project Policy & CLI** | `mail_store.py` (schema 6: `incarnations.session_ready`, `session_queries`, `TURN_KINDS` + `"query"`, separate query budget, expiry/cancel/epoch-fencing), `project_registry.py` (`resident_query_agents`), `guardian.py --resident-query*` | Unit tests covering active-session gate, unknown project / allowlist refusal, epoch change failure, budget, queue cap, expiry, cancel, `mode=ro` read |
| **Q2 — Relay & Node Driver** | `mail_relay.py` (`query`/`query_cancel` dispatch, `admit`/`turn_end` integration), `resident_node.py` (local queue, `pending_acks` answer recovery), `resident_driver.py` (`TurnResult.text`, QD3 priority + anti-starvation, `stalled` exemption, prompt wrapper, secret masking) | Fake relay + fake CLI e2e tests: serialization with mailbox turns, stream drop & reconnect answer recovery, `closed` and `open` paths, `missing_session` failure |
| **Q3 — Go API, Monitor & Dashboard** | `debate_handler.go` (`/api/residents/queries*`), `resident_monitor.py` (`session_ready`, `queries_pending`, query metadata), `ResidentAgentDetail.tsx` Session Query panel + `ko`/`en` i18n | `go test`, `scripts/ci.sh web`, real-browser check on isolated local hub (including 390 px width) |
| **Q4 — Session MCP (Optional)** | `session_link.py`, `session_daemon.py`, `session_mcp.py` `resident_query(to, project, prompt)` with mandatory hub `project` id and `caller_kind="session"` warning | Workstation-to-resident e2e test with `lease` fencing and project allowlist check |

## 11. Operator decisions (2026-11-22)

All five decisions were finalized by the operator on 2026-11-22:

| # | Question | Decision (2026-11-22) |
|---|---|---|
| **QD1** | Who may directly query a resident session? | **Hub-project-based**: every query requires a registered hub project (`project_id`) via hub dashboard/CLI (`operator`), gated by optional `policy.resident_query_agents`; no global unscoped queries and no resident-to-resident direct queries (future Q4 workstation session MCP must also pass a registered hub `project_id`) |
| **QD2** | Query execution mode (`in_place` vs `fork`) | **`in_place`** (resumes the active resident session directly across all providers; `fork` deferred) |
| **QD3** | Priority vs. mailbox wakes | **Direct query prioritized** over background mailbox wakes (with anti-starvation alternation after 2 consecutive queries when mail is waiting) |
| **QD4** | Hourly turn budget | **Separate budget** for direct queries (20/hour per agent), independent of the 12-turn mailbox budget, still governed by `GUARDIAN_RESIDENT=off` |
| **QD5** | Retention and backup | **30 days + included in nightly backup** (matching D7 for mail and tasks), stored with `mask_secrets` applied |

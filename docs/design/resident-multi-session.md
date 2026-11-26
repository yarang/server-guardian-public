# Multi-Session Management for Resident Agents (Design)

English | [한국어](resident-multi-session.ko.md)

Status: Decided 2026-11-23 (MD1–MD5 approved by the operator; Phases MS1–MS4 implemented).

This document extends [resident-agents.md](resident-agents.md) and
[resident-session-query.md](resident-session-query.md) so a single resident agent
(`<name>@<node>`) can safely maintain **multiple isolated active provider sessions** (for example,
per-project or per-topic sessions) and optionally run **read-only forked queries** (`mode: "fork"`)
without multiplying Unix sandbox accounts, credentials, or bypassing the node's single resident
slot (D5).

## 1. Problem and motivation

Today, each resident agent maintains exactly **one** live provider session per incarnation epoch
(`agents.session_id`, `incarnations.session_id`, and node `state.json`).

1. **Cross-project context pollution.** Querying the same resident (`codex@node-1`) under
   `server-guardian` and then under `fleet` appends both conversations into one provider thread.
   Prior repo details leak into ops checks and inflate token consumption.
2. **Cloning agents does not scale for `--tools open`.** Creating `codex-sg`, `codex-ops`,
   `codex-design` on the same node duplicates `/home/gsa-<name>` Unix accounts, provider login
   credentials, start-card turns, and bind supervision.
3. **No read-only branch for one-off reviews.** [resident-session-query.md](resident-session-query.md)
   reserved `session_queries.mode`, but only `"in_place"` is implemented; every review query mutates
   the resident's main conversation history.

## 2. Verified provider CLI capabilities

Capabilities probed directly on Tier-1 worker CLIs (`claude`, `agy`, `codex`):

| Provider | Multiple independent sessions per agent home | Native read-only fork (`mode: "fork"`) | Notes |
|---|---|---|---|
| `claude` | **Yes** (`--session-id <uuid>` / `--resume <uuid>`) | **Yes** (`--resume <uuid> --fork-session --no-session-persistence`) | Pre-assigned UUIDs; `--fork-session` branches without mutating the source session |
| `agy` | **Yes** (fresh invocation emits new `conversation_id`; `--conversation <id>` resumes any) | **No** | No CLI fork flag; must fail closed (`fork_unsupported`) |
| `codex` | **Yes** (`codex exec` emits new `thread_id`; `codex exec resume <tid>` resumes any) | **No** | No CLI fork flag; must fail closed (`fork_unsupported`) |

Because **all three providers** natively support multiple independent session IDs within a single
agent home/sandbox, **scoped session pools** work uniformly across `claude`, `agy`, and `codex`
(both `closed` and `open`). Native `mode: "fork"` is capability-gated per provider via
`configs/resident-providers.json` (enabled for `claude`, refused fail-closed elsewhere).

## 3. Architecture: Scoped Session Pool + Optional Fork

### 3.1 Session keys (`session_key`)

Within an incarnation `(address, epoch)`, sessions are addressed by a validated `session_key`:

- **`main`**: The default mailbox and task-board session (100% backward-compatible with existing
  `incarnations.session_id`). Created automatically by the start-card turn on bind.
- **`project:<project_id>`**: A dedicated long-lived session anchored to one registered hub project
  (e.g., `project:server-guardian`, `project:fleet`).
- **`project:<project_id>:<label>`** (optional, MD2): A named sub-session under a project
  (e.g., `project:server-guardian:review`, `project:server-guardian:debug`; `label` matches
  `^[a-z0-9][a-z0-9-]{0,23}$`).

Validation rules (hub fail-closed):
- A `project:<id>` or `project:<id>:<label>` session key **must match** the query's `project`
  argument (`session_key_project_mismatch` otherwise).
- Mailbox wakes and task turns always run on `session_key = "main"` (preserving existing start-card
  and mail continuity).

### 3.2 Lifecycle: lazy creation, reset, archive, and epoch fencing

1. **Lazy creation on first query.** Unlike `main` (which binds at agent startup), a scoped session
   does not spend tokens until its first `resident_query` arrives. On that first turn, the driver
   starts a fresh provider session with the agent's start card + project query preamble, records the
   resulting `provider_session_id`, and flips `session_ready = 1` for that `session_key`.
   - Precondition change: while `session_key = "main"` still requires `incarnations.session_ready == 1`,
     a scoped session may be created lazily once `agents.state == "ready"` and `main` is bound
     (`incarnations.session_ready == 1`), proving the agent and sandbox are healthy.
2. **Explicit reset / archive.** Operators can reset a scoped session (marking the old row
   `archived` and clearing the node's local mapping for that `session_key`) without disturbing
   `main` or other projects' sessions.
3. **Capacity cap and idle expiry.**
   - Maximum active sessions per `(address, epoch)`: **`MAX_SESSIONS_PER_AGENT = 8`** (including
     `main`). Creating beyond the cap fails closed with `session_limit_reached` until an idle
     session is archived/reset.
   - Idle scoped sessions unused for **14 days** (configurable, MD4) are lazily marked `archived`
     by the hub sweep (`main` is never auto-archived while the incarnation is open).
4. **Epoch fencing.** When an agent rebinds to a new `epoch` (crash, retire, or `main` lost), all
   open `agent_sessions` rows from the previous `epoch` are marked `closed` (`epoch_changed`), and
   any queued/dispatched queries targeting them fail with `epoch_changed`.
   - If a **single scoped session** encounters `missing_session` or `resume_new_session` during a
     query turn, only that scoped session is marked `lost` and the query fails with `session_lost`
     — **`main` and sibling scoped sessions are NOT torn down or rebound**.

### 3.3 Concurrency and sandbox invariants (unchanged)

- **Single resident slot (D5) preserved.** Multiple sessions are *stored* concurrently on disk, but
  only **one turn executes at a time per Tier-1 worker node** (and one `turn_thread` per agent).
  CLI session stores are never accessed concurrently.
- **Zero sandbox duplication.** All scoped sessions of an `--tools open` agent run inside the same
  `gsa-<name>` user and `guardian-sandbox-run` unit, with the same `closed`/`open` pre-turn checks
  and zero fallback.
- **Shared per-agent budgets.** The direct-query hourly budget (20/hour per agent, QD4) and pending
  queue cap (5 per agent) continue to apply per agent across all its session keys to prevent quota
  multiplication.

### 3.4 Optional read-only fork (`mode: "fork"`)

When a caller submits a query with `mode: "fork"` (targeting an existing `session_ready == 1`
session key such as `main` or `project:<id>`):

1. Hub checks provider capability (`supports_fork` in `configs/resident-providers.json`; `true` only
   for `claude`). Unsupported providers are refused immediately with `fork_unsupported` (HTTP 400).
2. Hub requires the target `session_key` to already exist with `session_ready == 1` (cannot fork an
   uncreated session).
3. Node driver invokes `claude` with `--resume <source_session_id> --fork-session --no-session-persistence`.
4. The driver verifies the source `provider_session_id` in `state.json` is untouched and returns the
   forked assistant text in `query_reply`, leaving the source session history clean.

## 4. Data model (`mail.db` schema 7 → 8)

Additive migration in `cli/llm-agent/mail_store.py`:

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
    address TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    session_key TEXT NOT NULL,               -- 'main' | 'project:<id>' | 'project:<id>:<label>'
    project TEXT,                            -- NULL for 'main', project id for scoped sessions
    label TEXT,                              -- optional sub-label
    provider_session_id TEXT,                -- provider session/conversation/thread id
    session_ready INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- active | archived | lost | closed
    turns INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_used TEXT,
    closed TEXT,
    PRIMARY KEY(address, epoch, session_key),
    FOREIGN KEY(address) REFERENCES agents(address)
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_active
    ON agent_sessions(address, epoch, status);

ALTER TABLE session_queries
    ADD COLUMN session_key TEXT NOT NULL DEFAULT 'main';
```

Node local state (`<agent_dir>/state.json`):
- Keeps top-level `session_id` / `first_turn` mirrored to `sessions["main"]` for backward
  compatibility during upgrades.
- Adds `sessions: { "<session_key>": { "session_id": "...", "first_turn": false, "last_used": "..." } }`.

## 5. Protocol and interface extensions

### Relay protocol (worker protocol 3 additive)
- `hello` adds `"multi_session": true` (and `"fork_query": true` when `claude` supports it).
  Nodes without `"multi_session"` reject non-`main` `session_key` queries with
  `node_without_multi_session`.
- **Hub → Node:**
  - `query`: adds `session_key` (default `"main"`), `create_if_missing` (bool), `mode`
    (`"in_place"` | `"fork"`).
  - `session_reset`: `{agent, epoch, session_key}` clears the local mapping for one scoped session
    when idle (never interrupts an active turn).
- **Node → Hub:**
  - `admit`: carries `session_key` when `kind == "query"`.
  - `turn_end`: carries `session_key` and updated `session_id` so the hub updates `agent_sessions`
    (and only updates `incarnations.session_id` when `session_key == "main"`).

### CLI, Go API, Dashboard, and Workstation MCP
- **Hub CLI (`guardian.py`)**:
  - `--resident-sessions <address> [--json]` — list active/archived sessions for an agent.
  - `--resident-session-reset <address> --session-key <key>` — archive/reset a scoped session.
  - `--resident-query <address> --project <id> --prompt ... [--session-key <key>] [--mode in_place|fork]`
- **Hub Go API (`apps/agent/debate_handler.go`)**:
  - `GET /api/residents/sessions?agent=<addr>&project=<id>` (`mode=ro`)
  - `POST /api/residents/sessions/reset` (`{address, session_key}`)
  - `POST /api/residents/queries` adds optional `session_key` and `mode`.
- **Dashboard (`SessionQueryPanel.tsx`)**:
  - Shows a compact session selector scoped to the active hub project (`project:<id>`, optional
    labeled sessions, or `main`), displays session status/turn count, and provides a Reset action
    for scoped sessions.
  - Filters the chat transcript by `(agent, project, session_key)`.
- **Workstation MCP (`guardian-session`)**:
  - `resident_query` adds optional `session_key` and `mode` (validated in `session_local.py` and
    forwarded over `session_uplink.py` → `session_link.py`).

## 6. Security invariants

| Risk | Mitigation |
|---|---|
| Cross-project session hijack (`project=A`, `session_key=project:B`) | Hub enforces exact match between `project` and `session_key`'s project prefix fail-closed |
| Quota multiplication via many sessions | Hourly query budget (20/h) and queue cap (5) remain **per-agent**, not per-session |
| Unbounded disk/session growth on worker node | `MAX_SESSIONS_PER_AGENT = 8` hard cap + 14-day idle archival |
| Lost scoped session destabilizing `main` mailbox loop | Scoped `missing_session` marks only that `session_key` as `lost`; `main` stays untouched |
| Unsupported provider silently running `in_place` on `mode: "fork"` | Capability gate on hub + node; fail closed with `fork_unsupported` |
| Concurrent resume corruption | D5 single resident slot and single `turn_thread` unchanged |

## 7. Phased implementation plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **MS1 — Store, Policy & CLI** | `mail.db` schema 8 (`agent_sessions`, `session_queries.session_key`), lazy-create admission, reset/archive, capacity cap, epoch fencing, `guardian.py --resident-sessions*` | Unit tests covering cross-project mismatch refusal, cap=8, idle archival, scoped `session_lost` isolation, `mode=ro` reads |
| **MS2 — Relay & Node Driver** | `mail_relay.py`, `resident_node.py`, `resident_driver.py` multi-session state map, lazy scoped start, `session_reset`, and (if approved in MD3) `claude` `mode: "fork"` | Socketpair e2e tests across `main` + two project sessions, scoped `session_lost` leaving `main` intact, and `fork` capability gating |
| **MS3 — Go API, Monitor & Dashboard** | `debate_handler.go`, `resident_monitor.py`, `SessionQueryPanel.tsx` session selector + reset + i18n (`ko`/`en`) | `go test`, `scripts/ci.sh web`, 390 px responsive check |
| **MS4 — Workstation MCP & Docs** | `session_local.py`, `session_daemon.py`, `session_mcp.py`, `session_link.py`, `mail_sessions.py` (`session_key`/`mode`) + bilingual user guide | Stdlib-only guard + live workstation-to-resident multi-session smoke test |

## 8. Open operator decisions (MD1–MD5)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| **MD1** | Default `session_key` when omitted in `resident_query` | **(A)** `project:<id>` (auto-isolated per hub project) vs. **(B)** `main` (shared with mailbox) | **(A) `project:<id>`** — prevents cross-project context pollution by default while still letting callers explicitly pass `session_key="main"` |
| **MD2** | Allow named sub-sessions within a project (`project:<id>:<label>`)? | **(A)** Yes (`project:<id>` + optional `:label`) vs. **(B)** Strict 1 session per project (`project:<id>` only) | **(A)** — enables parallel review/debug threads under the same project within the 8-session cap |
| **MD3** | Scope of `mode: "fork"` | **(A)** Include `claude`-only `--fork-session --no-session-persistence` in MS2 (fail-closed `fork_unsupported` on `agy`/`codex`) vs. **(B)** Defer `fork` until after MS1–MS4 | **(A)** — low implementation cost on `claude` and immediately useful for read-only reviews |
| **MD4** | Session cap and idle TTL | Max active sessions per agent & idle archival window | **Max 8 active sessions (incl. `main`), 14-day idle archive** |
| **MD5** | Bundle recent resident-review UX fixes (scroll lock, `visibilitychange` poll, cancel sync, MCP `done` flag) | **(A)** Land together with MS3/MS4 vs. **(B)** Land immediately before MS1 | **(B)** or **(A)** per operator preference |

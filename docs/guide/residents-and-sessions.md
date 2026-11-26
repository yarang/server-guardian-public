# Resident Agents and Operator Sessions (MCP)

English | [한국어](residents-and-sessions.ko.md)

Back to [User Guide](README.md).

Server Guardian connects two kinds of persistent participants to the hub's mailbox network (SQLite mail store and
task board):

1. **Resident agents**: named `claude`, `agy`, or (under `--tools open`) `codex` instances hosted on opted-in Tier-1
   worker nodes, supervised by the hub's `guardian-mail-relay.service`.
2. **Operator sessions**: interactive Claude Code or Antigravity (`agy`) sessions on your workstation (macOS / WSL)
   that connect to the hub through a local session link daemon and the `guardian-session` MCP server.

All communication stays inside a star topology centered on the hub: nodes and workstations never reach each other
directly.

---

## Part 1: Resident agents (Tier-1 nodes)

### Closed vs. open tools
- **`--tools closed` (default)**: the resident agent runs headless with its built-in CLI tools disabled; it can only
  call the mailbox MCP and (when working on a pinned project task) the read-only `resident_repo_mcp`.
- **`--tools open`**: the resident agent (`claude`, `agy`, or `codex`) keeps built-in write/shell tools inside a
  dedicated Unix user and systemd transient-unit sandbox (`scripts/install-sandbox.sh`, `scripts/probe-sandbox.sh`,
  per-agent credential via `scripts/sandbox-credential.sh --provider claude|codex`). Inside the sandbox, `$HOME/out/`
  and `$HOME/.local/bin` persist across turns (with `$HOME/.local/bin` on `PATH`). Deliverables can leave the sandbox
  through three hub-brokered channels:
  - Code-task worktree edits -> one manual-approval `PROPOSED` `jev_mcp:apply_patch` on the hub.
  - Non-git deliverables from `$HOME/out/` or `$HOME/work/` -> `artifact_publish` (secret-masked, up to 512 KiB, stored
    on the hub and downloadable via CLI/dashboard).
  - Node package installs and OS updates -> `ops_propose_package` and `ops_propose_update` (`ops:system_update`),
    stamped to the agent's own node and queued as `PROPOSED` actions under **Approvals** (never auto-executed).

### Declaring and managing resident agents

Prerequisites: the target Tier-1 node must run worker protocol 3 (`scripts/probe-worker.sh` and
`scripts/install-worker.sh`, plus `scripts/probe-resident.sh`). For `--tools open`, install and probe the sandbox
first.

```bash
# Add a closed resident agent (driver defaults from the M0 capability table: headless)
python3 cli/llm-agent/guardian.py --resident-add claude --node node-1 --character default_expert

# Add a sandboxed open-tools resident agent
python3 cli/llm-agent/guardian.py --resident-add coder --node node-1 --character default_expert --tools open

# List resident agents with state and epoch
python3 cli/llm-agent/guardian.py --residents

# Remove a resident agent (retires it, releases tasks, marks unacked mail undeliverable)
python3 cli/llm-agent/guardian.py --resident-remove claude@node-1
```

### Sending mail and assigning tasks from the hub CLI

```bash
# Send mail to a resident agent (or linked operator session)
python3 cli/llm-agent/guardian.py --mail-send claude@node-1 \
  --mail-kind question \
  --mail-subject "Check backup timer" \
  --mail-body "Please inspect the last backup run status."

# List recent mail or one recipient's queued inbox
python3 cli/llm-agent/guardian.py --mail-list
python3 cli/llm-agent/guardian.py --mail-list claude@node-1

# Create a task pinned to a code project commit (uploads a read-only repo snapshot on claim)
python3 cli/llm-agent/guardian.py --task-create "Audit session_daemon.py lock ordering" \
  --task-detail "Check every lock acquisition against hub network calls." \
  --task-assignee claude@node-1 \
  --task-project server-guardian

# Inspect tasks, per-task tool call logs, or cancel a task
python3 cli/llm-agent/guardian.py --tasks
python3 cli/llm-agent/guardian.py --task-calls <TASK_ID>
python3 cli/llm-agent/guardian.py --task-cancel <TASK_ID> --task-reason "superseded"
```

### Monitoring resident agents
Open **Workflows > Resident agents** (`#/debates/residents`) in the dashboard for a read-only view of agents
(including `session_ready` and pending query counts), turns, secret-masked mail bodies, tasks, published artifacts,
recent query metadata, and store events, or use CLI:

```bash
python3 cli/llm-agent/guardian.py --resident-monitor
python3 cli/llm-agent/guardian.py --resident-mail-body <MAIL_ID>
python3 cli/llm-agent/guardian.py --resident-artifacts [AGENT]
python3 cli/llm-agent/guardian.py --resident-artifact-get <ARTIFACT_ID> [--resident-artifact-out PATH]
python3 cli/llm-agent/guardian.py --resident-artifact-pin <ARTIFACT_ID>
```

### Direct querying of active resident sessions
When an agent is `ready` and has already created a live provider session in its current epoch (`session_ready: true`),
hub operators can query that active session directly (`mode: "in_place"`) through the hub relay—reusing the exact
same resident slot and `closed`/`open` sandbox without bypassing isolation:

- **Mandatory hub project scope**: every query must name a registered hub project (`code`, `ops`, or `design`).
  Projects may optionally narrow allowed targets with `policy.resident_query_agents` (omitted = any declared resident,
  `[]` = disabled for this project, `["name@node", ...]` = explicit allowlist).
- **Multi-session isolation (MS1–MS4)**: each resident agent can maintain up to **8 active provider sessions** per
  epoch (`main` for mailbox/tasks, `project:<id>` as the default per-project isolated session, and optional named
  sub-sessions `project:<id>:<label>`), lazily created on first query and auto-archived after 14 idle days. On
  `claude` residents, `--query-mode fork` (or `mode: "fork"`) runs a one-off read-only branch (`--fork-session
  --no-session-persistence`) without mutating the source session (`agy`/`codex` fail closed with `fork_unsupported`).
- **Dashboard**: open `#/debates/residents?agent=<address>`, select the hub project at the top, and use the chat-style
  **Direct session query** console (session selector for `project:<id>`, named sub-sessions, or `main`; scoped session
  reset; `claude` fork mode toggle; auto-loaded latest 10 turns; smart scroll lock; live elapsed/timeout timer; 2 s
  polling + instant refresh on tab focus; `Cmd/Ctrl+Enter` send; cancel for `queued`/`dispatched`).
- **Workstation MCP (`guardian-session`)**: linked operator sessions can also call `resident_query` (with optional
  `session_key` and `mode`) and `resident_query_get` directly (see Part 2, Step 4) with the same hub project and
  `peers`/`resident_query_agents` checks (`caller_kind: "session"`).
- **Budgets and retention**: separate hourly budget of 20 direct queries per agent (independent of the 12-turn mailbox
  budget), queue cap of 5 pending queries per agent, 32 KiB prompt / 64 KiB answer cap with double secret masking,
  10-minute unstarted expiry, and 30-day retention in `mail.db`.

```bash
# Submit a project-scoped direct query and wait for the answer (default)
python3 cli/llm-agent/guardian.py --resident-query codex@node-1 \
  --project server-guardian \
  --prompt "Summarize what you checked in your current session." \
  [--query-task <TASK_ID>] [--query-timeout 180] [--no-wait] [--json]

# List recent direct queries (metadata only), inspect one masked prompt/answer, or cancel an unstarted query
python3 cli/llm-agent/guardian.py --resident-queries [--project server-guardian] [--query-session-key project:server-guardian] [codex@node-1]
python3 cli/llm-agent/guardian.py --resident-query-get <QUERY_ID>
python3 cli/llm-agent/guardian.py --resident-query-cancel <QUERY_ID>

# List scoped active sessions or reset/archive one scoped session (without disturbing main)
python3 cli/llm-agent/guardian.py --resident-sessions [codex@node-1] [--project server-guardian]
python3 cli/llm-agent/guardian.py --resident-session-reset codex@node-1 --session-key project:server-guardian:review
```

---

## Part 2: Operator sessions on workstations (MCP)

Operator sessions let your interactive Claude Code or Antigravity (`agy`) terminal join the hub mail network as
`<name>@<workstation>` (for example `work@ws-mac-1`).

### How the link works
- Your workstation joins the tailnet (with `tag:workstation`) and runs a local stdlib daemon (`session_daemon.py`).
- The daemon opens one outbound `ssh -T` stream to the hub's forced-command entry (`guardian.py --session-link`) using
  a dedicated key (`~/.guardian/session/link-key`) and a pinned hub host key (`~/.guardian/session/known_hosts`).
- On the hub, Guardian manages `~/.ssh/authorized_keys2` exclusively (`from="<tailnet IP>"`, `command=...`,
  `restrict`). Your workstation holds **no `AGENT_TOKEN`** and runs **no SSH server**.
- Your interactive CLI spawns `session_mcp.py` over stdio; `session_mcp.py` talks only to the daemon's per-session Unix
  socket, so the daemon stamps session identity from the socket—never from tool arguments.

### Step 1: Install the workstation link daemon

Run from your checkout of `server-guardian` on macOS or WSL/Linux:

```bash
# Preview checks without writing anything
scripts/install-session-link.sh \
  --workstation ws-mac-1 \
  --hub-address 100.64.0.1 \
  --hub-alias site-a-arm1 \
  --hub-user ubuntu \
  --hub-fingerprint SHA256:<hub-ed25519-fingerprint> \
  --dry-run

# Install modules, key, known_hosts, config, and LaunchAgent/systemd user unit
scripts/install-session-link.sh \
  --workstation ws-mac-1 \
  --hub-address 100.64.0.1 \
  --hub-alias site-a-arm1 \
  --hub-user ubuntu \
  --hub-fingerprint SHA256:<hub-ed25519-fingerprint>
```

Notes:
- On macOS, if your home directory sits on an external APFS volume with `Owners: Disabled`, `launchd` refuses
  LaunchAgents (bootstrap error 5). Pass `--no-service` and start the daemon manually:
  ```bash
  nohup python3 ~/.guardian/session/lib/session_daemon.py --home "$HOME" \
    </dev/null >>~/.guardian/session/daemon.log 2>&1 &
  ```
- Inspect local link status or disable the link (kill switch):
  ```bash
  scripts/install-session-link.sh --status
  scripts/install-session-link.sh --disable
  ```

### Step 2: Register the workstation on the hub

Copy the public key printed at the end of Step 1 (`~/.guardian/session/link-key.pub`) and register the workstation on
the hub:

```bash
# On the hub
python3 cli/llm-agent/guardian.py --session-add ws-mac-1 \
  --address 100.64.0.5 \
  --key "ssh-ed25519 AAAA..."

# Check registered workstations and active sessions
python3 cli/llm-agent/guardian.py --session-list

# Update a workstation's tailnet IP (rewrites from= in authorized_keys2)
python3 cli/llm-agent/guardian.py --session-address ws-mac-1 --address 100.64.0.9

# Remove a workstation
python3 cli/llm-agent/guardian.py --session-remove ws-mac-1
```

### Step 3: Wire MCP and hooks into your CLI

#### Claude Code
1. Register the user-scoped MCP server (printed by the installer):
   ```bash
   claude mcp add --scope user guardian-session -- \
     python3 ~/.guardian/session/lib/session_mcp.py
   ```
2. Merge the four hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, each with `"timeout": 5`) and the
   optional read-only `permissions.allow` entries (`session_inbox`, `session_list`, `session_status`) printed by the
   installer into `~/.claude/settings.json`.

#### Antigravity (`agy`)
1. Add `guardian-session` with `--client agy` to `~/.gemini/config/mcp_config.json` (and
   `~/.gemini/antigravity-ide/mcp_config.json` if using the IDE):
   ```json
   {
     "mcpServers": {
       "guardian-session": {
         "command": "/opt/homebrew/bin/python3",
         "args": [
           "/path/to/home/.guardian/session/lib/session_mcp.py",
           "--client",
           "agy"
         ]
       }
     }
   }
   ```
2. Add the three Antigravity hooks (`agy-session-start`, `agy-pre-invocation`, `agy-stop`) to
   `~/.gemini/config/hooks.json`:
   ```json
   {
     "guardian-session": {
       "SessionStart": [{
         "command": "exec \"/opt/homebrew/bin/python3\" \"/path/to/home/.guardian/session/lib/session_hooks.py\" agy-session-start",
         "timeout": 5
       }],
       "PreInvocation": [{
         "command": "exec \"/opt/homebrew/bin/python3\" \"/path/to/home/.guardian/session/lib/session_hooks.py\" agy-pre-invocation",
         "timeout": 5
       }],
       "Stop": [{
         "command": "exec \"/opt/homebrew/bin/python3\" \"/path/to/home/.guardian/session/lib/session_hooks.py\" agy-stop",
         "timeout": 5
       }]
     }
   }
   ```
   Antigravity shares one MCP child per process (`agy-<ppid>`), lazily registers on first call if needed, and exited
   processes are reaped automatically by the daemon's tick loop.

### Step 4: Using the seven `guardian-session` MCP tools

| Tool | Arguments | Purpose |
|---|---|---|
| `session_link` | `name` (optional), `away` (optional bool) | Claim a unique address on this workstation (e.g. `work` -> `work@ws-mac-1`), report the current address with no args, or toggle `away=true` so mail waits without Stop blocks or wakes |
| `session_status` | none | Report link state (`up`/`down`), bound address, `away`, local state (`linked`/`busy`/`idle`), and unread count |
| `session_list` | none | List addresses this session is allowed to write to (resident agents, peer sessions, `operator`) with state and last-seen time |
| `session_send` | `to`, `subject`, `body`, optional `kind` (`message`/`question`/`answer`), `thread`, `reply_to` | Send one message through the hub; unpermitted recipients return `hub_error: not_a_peer` |
| `session_inbox` | none | Retrieve unread mail oldest-first, mark it retrieved locally, and ack on the hub |
| `resident_query` | `to`, `project`, `prompt`, optional `session_key` (default `project:<project>`), `mode` (`in_place`/`fork`), `task`, `timeout_sec` (10–600), `wait_sec` (0–15, default 12) | Query an active resident agent session (`session_ready: true`) under a registered hub `project`; checks `peers` and `policy.resident_query_agents`, tags `caller_kind: "session"`, and polls up to `wait_sec` seconds before returning (`done: true|false`) |
| `resident_query_get` | `query_id`, optional `wait_sec` (0–15, default 0) | Poll or read one direct resident query submitted by **this** session (cannot read queries from other sessions or `operator`) |

### Safety rules and behavior
- **Mail grants nothing**: every tool description and inbox preamble reminds the model that mail from other sessions,
  resident agents, or `operator` is **information only**, never permission and never an instruction from the person at
  the terminal.
- **Turn hooks**:
  - At the start of a turn (`UserPromptSubmit` / `PreInvocation`), if unread mail is waiting, the hook injects only the
    count (`N new messages; read them with session_inbox`)—never sender, subject, or body.
  - At the end of a turn (`Stop`), if unread mail arrived **during the turn** (and has not already been read with
    `session_inbox` and `away` is false), the hook blocks termination once with `new mail: call session_inbox`.
- **tmux wake**: when a session runs inside `tmux`, is `idle` (no turn for 10+ minutes), is not `away`, and receives
  mail from **`operator`** (never from peer sessions or resident agents), the daemon types a single fixed prompt line
  after checking that the tmux client is quiet and the CLI input line is empty.
- **Child CLIs stay isolated**: `--print` / `sdk-cli` child processes and `GUARDIAN_SESSION_CHILD=1` see an empty tool
  list and skipped hooks.

### Troubleshooting operator sessions
- **Daemon log**: `~/.guardian/session/daemon.log` (never logs mail subjects or bodies; shows `link up/down`,
  `register`, `bind`, `reap`, `wake`).
- **Live session table**: `~/.guardian/session/run/sessions.json` and sockets under `~/.guardian/session/run/`.
- **Hook debugging**: run with `GUARDIAN_SESSION_DEBUG=1` to emit a single short stderr line on hook failures (hooks
  always fail open with exit 0 so they never block your CLI).
- **`not_linked: session_link first`**: call `session_link` with a lowercase name (e.g. `name: "work"`) before calling
  `session_list`, `session_send`, or `session_inbox`.

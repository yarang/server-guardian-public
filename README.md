# Server Guardian

English | [한국어](README.ko.md)

Server Guardian manages a fleet of Linux nodes from one hub. It collects metrics, audits sshd on every node, runs
commands over SSH, and runs multi-agent debates through LLM CLIs (claude, codex, agy). A debate either changes code in
a repository (code projects) or checks and cleans up nodes (ops projects). Every change that a debate proposes waits
for approval, except the small set that a policy allows to run on its own.

The detailed system specification is [agent.md](agent.md).

---

## Architecture

**One hub, no other agents.** Only the hub (the inventory entry with `"guardian_role": "hub"`, here `site-a-arm1`)
runs the Server Guardian hub agent, the dashboard, the debate engine and the decision DB. The other nodes run no
agent: the hub reaches them with its own fleet SSH key.

| Part | Path | Role |
|---|---|---|
| Hub agent | `apps/agent/` | Go, standard library only. Metrics, sshd audit of the hub and of the fleet, fleet SSH execution, command and debate API, audit log. Serves the dashboard and starts the Python CLI for debates and the code wiki. |
| CLI and debate engine | `cli/llm-agent/` | Python, standard library only. `guardian.py` (CLI), `debate_engine.py` (Expert, Critic panel, Judge), LLM CLI runner, code index, repository map, Go facts, evidence and citation checks, code wiki, ops checks and cleanup actions. |
| Dashboard | `apps/web-desktop/` | React + TypeScript + Vite, also built as a Tauri 2 desktop app. Design system in `packages/guardian-ui/`. |
| Operations | `scripts/`, `deploy/` | CI, hub install, LLM CLI credentials and skills, fleet SSH key, systemd units, public-copy export. |

Hub data lives in `~/.guardian/data` (`GUARDIAN_DATA_DIR`): decision DB, inventory, teams, personas, projects, debate
jobs, code index cache, code wiki pages, audit log. It is production state and is never committed.

### Hub agent (`apps/agent`)
- System metrics (CPU, memory, disk, load average, top processes) and an SSE metrics stream.
- sshd of the hub: parses `sshd_config`, validates with `sshd -t`, lists `authorized_keys`, scores five CIS-style
  rules (`SEC-SSH-001` to `SEC-SSH-005`) from 0 to 100.
- Fleet: parallel diagnosis, parallel command execution (also streamed), per-node SSH audit, node registration over
  the hub's SSH config.
- Debate, action, project, team, persona, group and wiki routes, which run `guardian.py` (`newCmd` in
  `debate_handler.go`).
- Listens on `127.0.0.1` and the hub's Tailscale IP on port 9870, never on a public IP. Every `/api/` route needs the
  `AGENT_TOKEN` bearer token (the dashboard exchanges it for an HttpOnly session cookie). `/api/exec` runs commands.
- Commands sent through the exec routes (`/api/exec`, `/api/exec/remote`, `/api/fleet/exec`) are written to the
  audit log `<data dir>/audit/exec.jsonl` (read with `GET /api/audit`). It is an operational log, not tamper-proof.

### Debate engine (`cli/llm-agent`)
- **LLM CLIs.** The runner calls installed CLIs as subprocesses with their subscription logins and falls back to the
  next one on failure. Order: `GUARDIAN_LLM_PROVIDERS` (for example `claude,codex,agy`). Timeout per call:
  `GUARDIAN_LLM_TIMEOUT` (seconds, default 300, 600 on the hub). claude effort: `GUARDIAN_CLAUDE_EFFORT` (`--effort`,
  default medium, `default` keeps the CLI's own setting). When every CLI fails, the answer comes from the built-in
  heuristic engine, is marked `built-in-expert`, and the debate records `UNVERIFIED`.
- **Credentials.** CLIs read `~/.guardian/llm.env` (mode 600, path `GUARDIAN_LLM_ENV`), also under ssh and nohup.
  Never copy `~/.claude/.credentials.json` between nodes: the refresh token rotates, and when one node refreshes it the
  others get 401. Issue a long-lived token once with `claude setup-token` and deploy it as `CLAUDE_CODE_OAUTH_TOKEN`
  with `scripts/deploy-llm-env.sh` (default: the hub only; other nodes only when named; a PONG test per CLI;
  `--check-only` only checks). An Anthropic-compatible endpoint applies only with
  `GUARDIAN_CLAUDE_CONFIG=<settings file>`.
- **Teams.** A team (lineup) names the persona and providers of each role: one Expert, a panel of 1 to 4 Critics
  (run in parallel, combined verbatim) and one Judge. Built-in teams are in `configs/teams/` (`default`,
  `cross-review`, `fleet-ops`, `guardian-dev`), personas in `configs/personas/`. Teams and personas saved from the dashboard go to
  `~/.guardian/data/teams` and `personas` and override the same id. A team's `allowed_tools` can only narrow the
  judge persona's tools. The lineup is stored with the thread and reused when the thread continues. Design:
  [debate-teams.md](docs/design/debate-teams.md), API contract: [debate-teams-api.md](docs/design/debate-teams-api.md).
- **Reading code.** Agents ask for code with ```` ```repo ```` blocks or through the read-only MCP server
  `repo_mcp.py`. A symbol index, a repository map, Go facts (`tools/goindex`) and the code wiki help them find their
  way; only lines actually shown to the session count as evidence. Retrieval budgets are unlimited
  (`retrieval_warning` above 256 KiB per step). Design: [debate-agent-retrieval.md](docs/design/debate-agent-retrieval.md),
  [debate-code-index-wiki.md](docs/design/debate-code-index-wiki.md), [code-wiki-coverage.md](docs/design/code-wiki-coverage.md).
- **Sessions.** CLI sessions (claude, codex, agy) are stored per thread and role in the SQLite table `agent_sessions`
  and resumed in the next round. The CLI process exits after each call; the conversation stays on disk. Session
  working directory: `GUARDIAN_LLM_WORKDIR` (default `~/.guardian/llm-sessions`).
- **jev gate** ([jev-mcp](https://git.example.com/owner/jev-mcp)). When the LLM judge approves, the debate is filed
  with jev-mcp as an ADR and judged independently by TypeSafe jev. The verdict is `APPROVED` only when jev recommends
  `adopt` with enough confidence, the reasons hold and the change is easy to undo; otherwise `DISPUTED`, or
  `PENDING_JEV` before the review. Unless the verdict is `APPROVED`, the judge's actions are `HELD` and cannot run.
  On the hub jev-mcp lives in `~/jev-mcp` (`uv sync`), configured as `jev` in `configs/mcp_servers.json`, ADR DB in
  `~/.guardian/data/jev/`. API key: add `TYPESAFE_API_KEY=...` to `~/.config/server-guardian/llm.env` on the Mac and
  run `scripts/deploy-llm-env.sh site-a-arm1`. `GUARDIAN_JEV_GATE=off` turns the gate off (for tests).
- **Judge actions.** An approved debate stores the judge's proposed tool calls as `PROPOSED`.
  - Code projects: `apply_patch` and `execute_code_action` (files inside the project's clone only) are applied
    without approval once the jev gate passes (`decided_by` is `auto:engine`; written to the clone, never pushed).
    `run_tests` and file writes that ask for `run_test` wait for a person. Code reaches a repository only through
    **Publish** (below); `git_commit_push` was retired and an old `PROPOSED` one becomes `INVALID`.
  - Ops projects: the cleanup catalog and `server_remediation` (see Ops projects).
  - `GUARDIAN_AUTO_APPLY=off` or `GUARDIAN_UI_ACTIONS=off` turns all automatic runs off. `GUARDIAN_UI_ACTIONS=off`
    also blocks every action and thread decision from the dashboard (run, reject, fail-stale, jev recheck, close,
    publish, workspace reset); read-only views and the CLI keep working, and the 403 names the CLI command.
  - The dashboard asks again before running (a high-risk action needs its number typed in) and records
    `dashboard@<IP>` as `decided_by`.

### Ops projects (nodes without a repository)
An ops project (`"kind": "ops"`) groups target nodes (node groups, nodes, exclusions), a policy, a team and its own
decision history. It has no repository, workspace, tests or wiki; sync, publish and workspace reset are refused.
Design and implementation notes: [server-ops-project.md](docs/design/server-ops-project.md).
- **Built-in project `fleet`** (`configs/projects/fleet.json`): every fleet node except the hub, team `fleet-ops`,
  the whole cleanup catalog plus `server_remediation`, no automatic runs, nightly watch on. Debates started from the
  Fleet, node or group screens go to an ops project that covers that scope, else to `fleet`.
- **Node checks.** Debate agents ask for read-only checks with ```` ```ops ```` blocks (`disk_usage`, `largest_dirs`,
  `largest_files`, `docker_usage`, `journal_usage`, `package_cache`, `service_status`, `top_processes`,
  `uptime_load`, ...). The engine runs fixed command templates through the hub agent, so every check is in the audit
  log (`source=ops_check`). They cite results as `[#<call> <check> <host>]`, and the engine checks that every
  citation names a check that actually returned.
- **Cleanup action catalog.** Typed actions instead of free shell: `ops:docker_prune`, `ops:journal_vacuum`,
  `ops:apt_clean`, `ops:remove_paths`, `ops:truncate_logs`, `ops:remove_old_kernels`, `ops:restart_service`. Each one
  is measured when proposed (**preview**: what it would delete or restart), measured again right before it runs
  (**drift**: it becomes `STALE` and does not run when the amount grew by more than half and more than 256 MiB, or
  when new items appeared), and measured after it ran (**verify**: before, after, freed, ok). A fixed deny list
  (system directories, home directories, `.ssh` and other protected names, secret-looking paths) cannot be relaxed;
  `policy.deny_paths` adds to it. One action runs per node at a time. `jev_mcp:server_remediation` (free command, high
  risk) remains for what the catalog does not cover and always needs a person.
- **`auto_run`.** Off by default. A project can list low-risk catalog actions (`docker_prune` with a low scope,
  `journal_vacuum`, `apt_clean`) in `policy.auto_run` with `max_auto_runs_per_day` (1 to 100). Such an action runs
  without approval only when the jev verdict is really `PASSED`; `decided_by` is `auto:<project>`.
- **Nightly ops watch.** `server-guardian-ops-watch.timer` runs `guardian.py --ops-watch` daily at 02:30 UTC. For
  every ops project with `policy.watch` it checks disk and inode use (default 85%) and journal size (default `3G`) on
  each target node and starts one ops debate for new findings; the same finding does not start another debate for 7 days.
  `guardian.py --ops-watch --dry-run` only reports.
- **`fleet-ops` team.** Expert `ops_sre`, Critics `ops_safety_critic` and `ops_capacity_critic`, Judge `ops_judge`,
  at most 2 rounds, node tools only. A project may name its default team (`"team"`).

### Dashboard (`apps/web-desktop`)
- Served by the hub agent from built static files on the same port (`http://<Tailscale IP>:9870/`); no Vite dev server
  runs on the hub. The Tauri desktop app connects to the same hub.
- Screens: **Fleet** (summary, nodes with registration and node detail, SSH audit, commands, groups), **Debates**
  (start a debate, live agent activity, results, proposed actions, Publish, code wiki tab), **Approvals**,
  **Settings** (hub connection, projects, personas, teams).
- **Language.** Korean by default; English is available under Settings > Hub connection > Language. The choice is
  saved per browser and reloads the page. Message catalogs: `src/i18n/ko` and `src/i18n/en`. Design:
  [dashboard-i18n.md](docs/design/dashboard-i18n.md).

### Guardian skills for the LLM CLIs
`deploy/llm-skills/` holds `SKILL.md` skills that claude, codex and agy share. `scripts/deploy-llm-skills.sh` copies
them to `~/.claude/skills`, `~/.codex/skills` and `~/.gemini/config/skills` on the hub and every Tier-1 node
(`manifest.json`: `all` or `hub`).
- `guardian-host-check` (all): read-only checks of this node.
- `guardian-host-cleanup` (all): cleanup of this node with the catalog actions and deny rules only, with operator
  confirmation and before/after measurement per action.
- `guardian-fleet-ops` (hub): ops watch dry run, starting and following ops debates, showing actions and running
  the ones the operator approved. It never changes other nodes directly.
- `guardian-hub-status` (hub): hub agent, timers, audit and debate state.

Fleet-wide changes go through Guardian on the hub, so `AGENT_TOKEN` never leaves the hub. Only `guardian-*` skills are
managed. Debates are unaffected: the engine runs claude with `--tools ""` and agy with `--disable-slash-commands`.

---

## Quick start

### 1. Build and run the hub agent (local development)
```bash
cd apps/agent
go build -o agent .
AGENT_TOKEN=$(openssl rand -hex 32) ./agent
# AGENT_TOKEN is required (the agent does not start without it). The API listens on port 9870.
```

### 2. Run the Python CLI
```bash
# Resource and security diagnosis of the local node
python3 cli/llm-agent/guardian.py "Check the node's security and resources and plan the fixes"

# One remote node
python3 cli/llm-agent/guardian.py --target site-a-arm1 "Check the remote node's resources"

# Parallel diagnosis of the whole fleet (or one node or group with --target)
python3 cli/llm-agent/guardian.py --fleet

# Run a command on every node in parallel
python3 cli/llm-agent/guardian.py --exec "uname -m" --target all

# Per-node SSH audit: table per group, then failing rules per node
python3 cli/llm-agent/guardian.py --ssh-audit
python3 cli/llm-agent/guardian.py --ssh-audit --target site-b-cluster --json

# Debates
python3 cli/llm-agent/guardian.py --debate "task" [--team cross-review] [--project <id>]
python3 cli/llm-agent/guardian.py --debate "task" --thread <ID>     # continue a thread

# Interactive REPL
python3 cli/llm-agent/guardian.py
```

Other CLI commands:
- Sessions: `--sessions [<ID>]`, `--attach <ID> --role critic` (opens the session in a tmux window,
  `GUARDIAN_TMUX_SESSION`, default `guardian`), `--prune-sessions 30`.
- Actions: `--actions [<ID>]`, `--show-action N`, `--run-action N` (asks for `yes`), `--reject-action N`.
- jev: `--jev-status [<ID>]`, `--jev-recheck <ID>` (re-evaluates and releases `HELD` actions when it passes).
- Teams and personas: `--teams`, `--personas`, `--team-save`, `--persona-save` (JSON on stdin).
- Ops watch: `--ops-watch [PROJECT] [--dry-run]`.

### 3. Dashboard (local development)
```bash
cd apps/web-desktop
npm install
npm run dev
# http://localhost:5173/ (local development only)
```

---

## Hub deployment

```bash
# The hub: agent, dashboard, workspace clone, Go toolchain, goindex, backup and ops watch timers
scripts/install-agent-service.sh site-a-arm1
# First install, or when the hub cannot be reached: inventory from the local snapshot (the hub's copy is never overwritten)
scripts/install-agent-service.sh site-a-arm1 --offline
# Open: http://<Tailscale IP>:9870/  (for example http://100.64.0.7:9870/)
```
- **The hub agent is installed on the hub only.** A node whose inventory entry is not `guardian_role: hub` is refused
  with exit code 2; there is no per-node install. Nodes are managed over SSH (`scripts/setup-hub-ssh.sh`).
- The node must be an inventory id. Aliases and IPs are refused.
- The dashboard is always built and served: `--no-ui` is refused, `--with-ui` is accepted and ignored.
- **Releases** ([install-tracks.md](docs/design/install-tracks.md), R1): the script builds a release tree on this
  machine (agent, goindex, dashboard, the tracked `cli/`, `configs/` and `deploy/` files without tests, `VERSION`,
  `MANIFEST`), uploads it to `~/.guardian/releases/<version>-<time>/` and runs its `deploy/hub/install.sh` on the hub.
  That script checks the MANIFEST, writes `agent.env`, installs the units, points `~/.guardian/current` at the new
  release (the old one becomes `~/.guardian/previous`), restarts the hub agent and checks it. A release that does not
  start is switched back on its own (exit 3). Three releases are kept. `--dry-run` reports what would change.
- Roll back to the previous release: `ssh <hub> bash ~/.guardian/current/deploy/hub/install.sh --rollback`
  (`--status` lists the releases).
- The hub checkout `~/server-guardian` no longer runs anything. It is the source of the builtin `server-guardian`
  project: by default it is fast-forwarded, the workspace clone `~/guardian-workspace` is moved to the same commit
  unless judge actions left changes there, and every project is synced. `--no-pull` skips this.
- The installer sets `GUARDIAN_WORKSPACE=~/guardian-workspace`, `GUARDIAN_LLM_TIMEOUT=600`,
  `GUARDIAN_DEBATE_TIMEOUT=7200`, `GUARDIAN_SERVERS_FILE=~/.guardian/data/servers.json` and
  `AGENT_UI_DIR=~/.guardian/current/ui` in `~/.guardian/agent.env` (only the keys it owns are rewritten).
- An upload goes to a temporary name and is renamed only after its sha256 matches. A failed upload is retried once
  over the hub's Tailscale IPv4; `HUB_SSH_HOSTNAME=<address>` picks the address from the start.
- **Hub sshd hardening:** the installer puts `deploy/ssh/00-guardian-hardening.conf` (`MaxAuthTries 4`,
  `PermitRootLogin no`) into the hub's `/etc/ssh/sshd_config.d/`. It checks with `sshd -t` and `sshd -T` and reverts
  on failure, leaves an identical file alone, and skips the step when `sshd_config` does not include
  `sshd_config.d`, when connected as root, or when root has a key without a forced command. The install finishes even
  when hardening is skipped or reverted. `--no-ssh-hardening` turns it off.
- **Node sshd hardening is a manual step.** The installer never touches nodes. The procedure (running
  `deploy/ssh/apply-ssh-hardening.sh` on each node over SSH from the hub) is in
  [2026-10-05-fleet-sshd-hardening.md](docs/operations/2026-10-05-fleet-sshd-hardening.md).

### Deploy gate
Deploy only what passed CI, in one chain:
```bash
scripts/ci.sh && git push origin main && scripts/install-agent-service.sh site-a-arm1
```
- Never push or deploy after a failing CI.
- Before deploying, check that no production debate is running (`GET /api/debates` or the Debates screen).
- The installer builds the local working tree. Deploy only from a clean tree; with work in progress, commit the part
  to deploy and deploy from a temporary `git worktree` of that commit.

### Inventory: the hub's copy is the source
The node list lives on the hub in `~/.guardian/data/servers.json` (`GUARDIAN_SERVERS_FILE`); dashboard node
registration and deletion write there. Add, remove or edit nodes on the hub or in the dashboard.
- `configs/servers.json` is only a local snapshot for the `--offline` modes of the admin scripts. It is git-ignored,
  so real nodes never enter the repository; `configs/servers.example.json` shows the format.
- The Mac scripts that pick nodes (`install-agent-service.sh`, `setup-hub-ssh.sh`, `fleet-upgrade.sh`,
  `fleet-hostname.sh`, `deploy-llm-env.sh`, `deploy-llm-skills.sh`, `install-llm-clis.sh`) fetch the hub's copy
  read-only (`ssh <hub> cat …`) on every run through `scripts/lib/inventory.sh`. The hub alias is `HUB_HOST`, else the
  `guardian_role: hub` entry of the snapshot.
- `--offline` (or `INVENTORY_OFFLINE=1`) uses the snapshot for a first install or when the hub cannot be reached. It
  prints a warning and never overwrites the hub's copy (it only seeds one when none exists).
- `scripts/pull-inventory.sh` copies the hub's inventory into the local snapshot after showing the diff
  (the previous one is kept as `configs/servers.json.bak`).
- `scripts/setup-hub-ssh.sh` skips ids that are no longer in the hub inventory. `--revoke <id>` removes a key.

### Hub configuration
- Decision DB and debate records: `~/.guardian/data/` (`GUARDIAN_DATA_DIR`, `GUARDIAN_DB_PATH`).
- **Backup:** `server-guardian-db-backup.timer` runs daily at 03:30 UTC and writes a consistent DB copy (`.db`) and a
  data tarball (`guardian_data-*.tar.gz`: `~/.guardian/data` without `backups/`, `code-index/` and logs, plus
  `~/.ssh/guardian_fleet.conf` and `~/.ssh/guardian_known_hosts`) to `~/.guardian/data/backups/`, keeps 14 of each,
  and copies them to the `backup_replica` node. No private keys or secrets. Restore and drill:
  [backup-restore.md](docs/operations/backup-restore.md).
- **Hub to node SSH:** a hub-only key, registered in each node's `authorized_keys` restricted with
  `from="<hub Tailscale IP>"`.
  - The private key is `/etc/guardian/fleet_key` (root, 0600), held by the ssh-agent of
    `guardian-fleet-agent.service` (`/run/guardian-fleet/agent.sock`). The host blocks in `~/.ssh/guardian_fleet.conf`
    (included from `~/.ssh/config`) use that socket as `IdentityAgent`.
  - The hub agent, the LLM CLIs and the backup run in a sandbox with `NoNewPrivileges` (no sudo in the whole process tree)
    and `InaccessiblePaths` (key and secret files blocked): they can use the key but cannot read or copy it.
  - Hub sudo: allowed by default (`--hub-sudo`), so the hub agent, judge actions and the dashboard terminal can use sudo
    on the hub; the drop-in `hub-sudo.conf` relaxes part of the sandbox, and root can then read the key, so the key
    protection only guards against mistakes. `--no-hub-sudo` keeps the full sandbox. Without either flag a redeploy
    keeps the current setting.
  - `deploy/hub/protect-fleet-key.sh` moves the key and sets up the socket; `install-agent-service.sh` and
    `setup-hub-ssh.sh` call it. The end of a hub install prints an isolation check (key read and sudo blocked, fleet
    SSH works, `systemd-analyze security` score).
```bash
scripts/setup-hub-ssh.sh                         # issue, register and verify the hub key
scripts/setup-hub-ssh.sh --revoke [--key <public key|file|SHA256:..>] [--dry-run]   # remove only lines with that key (default: the hub's current key)
scripts/setup-hub-ssh.sh --rotate [--dry-run]    # add new key -> verify login with it -> switch the hub key -> remove the old key (keeps the old key on failure)
scripts/deploy-llm-env.sh                        # LLM CLI credentials to the hub
scripts/deploy-llm-skills.sh [--dry-run]         # Guardian skills to the hub and the Tier-1 nodes
```

---

## Code projects (other repositories)

The debate engine can read and change other registered repositories too. Each project has one working clone on the
hub.
- **Definitions:** built-in projects in `configs/projects/*.json`. Projects added from the dashboard (Settings >
  Projects) are stored in `~/.guardian/data/projects/` and override a built-in project with the same id.
- **Fields:**
  - `repo`: https URL, without credentials.
  - `branch`: the branch to follow.
  - `workspace`: the clone; default `~/guardian-workspaces/<id>`.
  - `tests.commands`: extra test commands the judge may run (for example `cargo test`). A shell, an interpreter or a
    package runner on its own is refused.
  - `tests.default`: the test run after the judge changes files, and before a publish; without it `run_test` and
    publish are refused. One command, or a list of 1 to 5 commands run in order until the first failure (sharing a
    600-second limit). Every entry passes the same allowlist; no shell and no path scripts such as `scripts/ci.sh`.
    This repository's gate is `python3 -m unittest discover ...`, `go vet ./apps/agent/...` and
    `go test -race ./apps/agent/...` (`-race` needs gcc on the hub).
  - `context.paths`: default reference code.
  - `team`: the project's default team (optional).
- **Clone and sync:** the dashboard's Clone/Sync button or `--project-sync`. A hub deploy syncs every project. A clone
  with modified files is left alone.
- **Private repositories:** register git credentials on the hub once (for example a Gitea access token and
  `git config --global credential.helper store`).
- **Rules:** a debate picks its project at the start, and a continued debate stays in it. Judge actions run only in
  that project's clone. Code changes apply automatically, tests and shell actions need a person. Actions never push;
  only Publish does.
```bash
python3 cli/llm-agent/guardian.py --projects
python3 cli/llm-agent/guardian.py --project-sync all
python3 cli/llm-agent/guardian.py --debate "three main defects and their fixes" --project my-service --context-path 'src/**/*.rs'
```

### Publish (pushing a thread's changes to a branch)
Publish pushes the changes that judge actions (`execute_code_action`, `apply_patch`) left in the clone to a new branch
of the project's `repo`. It is the only supported push path.
- Run `python3 cli/llm-agent/guardian.py --publish-thread <THREAD> [--branch <name>]` (shows the plan and asks for
  `yes`), or the **Publish** button on a debate result. Blocked from the dashboard when `GUARDIAN_UI_ACTIONS=off`.
- Only files that actions created are added, and the push happens only after `tests.default` passes. No force push.
- After the push: merge the branch, update the hub checkout (redeploy), then run `--project-sync` so the clone sees
  the merged code.
- Preconditions, failures, retries and exit codes: [publish-thread.md](docs/design/publish-thread.md).

---

## Node groups

Nodes can be grouped by name. A node can be in several groups. Groups are stored in the hub decision DB
(`node_groups` in `~/.guardian/data/guardian_decisions.db`). Design: [node-groups.md](docs/design/node-groups.md).
- **Members = picked nodes + nodes matching a rule − excluded nodes.** Conditions inside a rule are ANDed, rules
  are ORed. New nodes that match a rule join automatically.
- **Rule fields:** `tier`, `arch`, `guardian_role`, `role` (equals / not equals), `tailscale_tags`, `cli_support`
  (contains / does not contain), `name` (glob, for example `site-b-*`).
- **First run:** one static group per `group` value of `servers.json` is created once. After that only user groups
  are used; the `group` field is kept but ignored.
- **Dashboard:** Fleet > Groups (live member preview). Group by and group filters, batch command targets and ops
  project targets use these groups.
- **CLI:** `--target` resolves `local`/`all`, then node id, name or hostname, then group id or name (case
  insensitive), and suggests similar names when nothing matches.
```bash
python3 cli/llm-agent/guardian.py --groups
echo '{"name":"Tier-1 ARM","rules":[{"conditions":[{"field":"tier","op":"eq","value":"Tier-1"},{"field":"arch","op":"eq","value":"aarch64"}]}]}' \
  | python3 cli/llm-agent/guardian.py --group-save --json
python3 cli/llm-agent/guardian.py --group-delete tier-1-arm --json
```

## Per-node SSH audit (fleet)

The hub runs a **fixed read-only probe** on each node with the fleet key, reads the effective sshd settings and the
login user's `authorized_keys`, and scores them with the same parser and rules as the hub's own audit (`/api/ssh/*`).
Design: [fleet-ssh-audit.md](docs/design/fleet-ssh-audit.md).
- **API:** `GET /api/fleet/ssh?targets=a,b[&refresh=1]` (same token as the other fleet APIs). Without `targets`, every
  node; an unknown id is a 400 before any SSH.
- **Status:** `ok` (effective settings from `sudo -n sshd -T` or root's `sshd -T`), `partial` (no root: reads
  `/etc/ssh/sshd_config` and `sshd_config.d/` directly, so unread includes and default differences may be missing, or
  the output was cut), `unreachable` (ssh failed or timed out), `error` (settings could not be read). Nodes that were
  not evaluated get no score and are left out of the average.
- **Cache:** 60 seconds in memory per target set, concurrent requests share one probe. `refresh=1` (dashboard
  "Refresh audit", CLI `--refresh`) probes again. At most 8 nodes at a time, 20 seconds per node.
- **Keys:** fingerprints, comments and options only; the public key body is left empty.
- **CLI:** `--ssh-audit [--target node|group] [--refresh] [--json]`. Natural-language security audits of the fleet,
  a group or a remote node also use this API.
- **Dashboard:** Fleet > SSH audit.

## CI

`scripts/ci.sh` runs every check in one place. GitHub Actions (`.github/workflows/ci.yml`, on the private GitHub
mirror) runs the same script, one job per section. Gitea at git.example.com has Actions turned off on purpose.

```bash
scripts/ci.sh                 # default: go python web shell config (tauri only when named)
scripts/ci.sh go python       # selected sections
```

| Section | Checks |
|---|---|
| `go` | gofmt (except `metrics.go`, `model.go`), `go vet`, `go test -race`, linux/arm64 build of `apps/agent`; the same for `tools/goindex` |
| `python` | the whole `cli/llm-agent` unittest suite and `scripts/test_*.py`, with `GUARDIAN_LLM_PROVIDERS=none` (no real LLM calls) |
| `web` | design system typecheck, tests and build; dashboard build and tests |
| `tauri` | desktop app release build (no installer bundles) and its Rust tests; only when named, separate job in CI |
| `shell` | `bash -n` and shellcheck (warning and above) for `scripts/*.sh`, `scripts/lib/*.sh`, `deploy/*/*.sh` |
| `config` | JSON syntax of `configs/`, unique node ids and exactly one hub, built-in personas and teams, bilingual documents (every `name.md` has `name.ko.md`, language lines, relative links resolve) |

Locally a missing tool (for example shellcheck) is skipped with a warning; in CI (`CI=true`) it fails.

## Public copy

The repository uses placeholders for private node, site and account names (`site-a-arm1`, `site-b-*`, `owner`, ...).
The rules that map placeholders to real names live outside the repository, in
`~/.config/server-guardian/public-redactions.txt` (git-filter-repo `--replace-text` format).
- `scripts/redact-private.py [--rules FILE] [--check] [path ...]` masks private names in the tracked text files;
  `--check` changes nothing and exits 1 when a match remains.
- `scripts/export-public.sh [--rules FILE] [--push REMOTE-URL] [--keep]` builds the public copy on a throwaway clone of
  `main`: it drops paths that must never be public (inventory snapshot, old decision DB, `*.enc`, `CLAUDE.local.md`,
  `*.pyc`), rewrites file contents and commit messages with the rules, checks every blob and message again (one match
  stops the export) and, with `--push`, pushes `main` to the public remote. This checkout, its remotes and the hub are
  not changed. Needs git-filter-repo.

## Documentation

Design (`docs/design/`):
- [server-ops-project.md](docs/design/server-ops-project.md): ops projects, node checks, cleanup catalog, auto_run, ops watch, fleet-ops team, LLM CLI skills
- [debate-teams.md](docs/design/debate-teams.md), [debate-teams-api.md](docs/design/debate-teams-api.md): team lineups and their API
- [debate-agent-retrieval.md](docs/design/debate-agent-retrieval.md): how agents read code, event contract, Agents monitor
- [debate-code-index-wiki.md](docs/design/debate-code-index-wiki.md): symbol index, repository map, Go facts, code wiki
- [code-wiki-coverage.md](docs/design/code-wiki-coverage.md): wiki feature and group pages, file view
- [debate-code-context.md](docs/design/debate-code-context.md), [debate-patch-tool.md](docs/design/debate-patch-tool.md): code context and the patch tool
- [publish-thread.md](docs/design/publish-thread.md): publishing a thread's changes
- [node-groups.md](docs/design/node-groups.md), [node-registration.md](docs/design/node-registration.md), [fleet-ssh-audit.md](docs/design/fleet-ssh-audit.md): fleet
- [dashboard-i18n.md](docs/design/dashboard-i18n.md), [dashboard-react-migration.md](docs/design/dashboard-react-migration.md), [ui-flow-redesign.md](docs/design/ui-flow-redesign.md): dashboard

Operations (`docs/operations/`):
- [backup-restore.md](docs/operations/backup-restore.md): hub backup, restore and restore drill
- [2026-10-05-fleet-sshd-hardening.md](docs/operations/2026-10-05-fleet-sshd-hardening.md): fleet sshd hardening log and the manual node procedure

Analyses and evaluations are in `docs/analysis/`.

## License

Copyright 2026 William Jung. Licensed under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE).

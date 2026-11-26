# Setup and Deployment

English | [한국어](setup-and-deployment.ko.md)

Back to [User Guide](README.md).

This module covers installing the hub, protecting the fleet SSH key, deploying LLM CLI credentials and Guardian skills,
enabling execution workers, and running Server Guardian locally for development.

## Prerequisites

- **Hub node**: a Linux node (arm64 or amd64) with `systemd`, `python3` (3.9+), `git`, and Tailscale (or headscale)
  connectivity. Its entry in the inventory must have `"guardian_role": "hub"`.
- **Admin workstation**: `bash`, `ssh`, `go` (for building `apps/agent` and `tools/goindex`), `node`/`npm` (for
  building the dashboard), and `python3`.
- **Managed nodes**: reachable from the hub over SSH on the tailnet. No agent installation is needed on managed nodes.

## 1. Hub deployment

The hub runs immutable releases under `~/.guardian/releases/<version>-<timestamp>/`, with `~/.guardian/current`
pointing at the active release and `~/.guardian/previous` kept for rollback (the last 3 releases are retained).

```bash
# Standard deploy (fetches inventory from the hub, builds agent + goindex + dashboard, uploads atomically)
scripts/install-agent-service.sh site-a-arm1

# Preview what would change without modifying the hub
scripts/install-agent-service.sh site-a-arm1 --dry-run

# First install, or when the hub is not yet reachable (uses local configs/servers.json snapshot)
scripts/install-agent-service.sh site-a-arm1 --offline
```

Key behaviors:
- **Hub only**: passing a node whose `guardian_role` is not `hub` exits with code 2. Always pass the inventory id,
  not an alias or IP.
- **Automatic rollback on startup failure**: `deploy/hub/install.sh` verifies the `MANIFEST`, installs systemd units,
  restarts the hub agent, and reverts to `~/.guardian/previous` automatically (exit 3) if health checks fail.
- **Manual rollback and release status**:
  ```bash
  ssh site-a-arm1 bash ~/.guardian/current/deploy/hub/install.sh --status
  ssh site-a-arm1 bash ~/.guardian/current/deploy/hub/install.sh --rollback
  ```
- **Hub sshd hardening**: enabled by default (`deploy/ssh/00-guardian-hardening.conf`: `MaxAuthTries 4`,
  `PermitRootLogin no`), validated with `sshd -t` and reverted automatically on failure. Pass `--no-ssh-hardening` to
  skip it.
- **Hub sudo sandbox**: `--hub-sudo` (default) allows the hub agent and dashboard actions to use sudo on the hub;
  `--no-hub-sudo` enforces the strict `NoNewPrivileges` sandbox. Redeploys without either flag keep the current setting.
- **Workspace sync**: by default, deploying fast-forwards `~/server-guardian` and syncs registered project clones
  unless uncommitted judge changes are present. Pass `--no-pull` to skip.

### Deploy gate (recommended workflow)

Always run CI before pushing and deploying, and confirm no production debate is running first:

```bash
scripts/ci.sh && git push origin main && scripts/install-agent-service.sh site-a-arm1
```

Deploy only from a clean git working tree (or from a temporary `git worktree` of the commit you intend to ship).

## 2. Fleet SSH key setup

The hub connects to all managed nodes with a single hub-only SSH key stored at `/etc/guardian/fleet_key` (root, `0600`)
and served via `guardian-fleet-agent.service` (`/run/guardian-fleet/agent.sock`). Each node's `authorized_keys`
restricts that key with `from="<hub Tailscale IP>"`.

```bash
# Issue, distribute, protect, and verify the fleet SSH key
scripts/setup-hub-ssh.sh

# Rotate the fleet key safely (adds new key -> verifies -> switches -> removes old key)
scripts/setup-hub-ssh.sh --rotate
scripts/setup-hub-ssh.sh --rotate --dry-run

# Revoke a key from fleet nodes
scripts/setup-hub-ssh.sh --revoke [--key <public-key|file|SHA256:...>] [--dry-run]
```

To harden `sshd` on managed nodes (never automated by the hub installer), follow
[Fleet sshd hardening](../operations/2026-10-05-fleet-sshd-hardening.md).

## 3. LLM CLI credentials

Debate agents and resident agents call installed LLM CLIs (`claude`, `codex`, `agy`, `opencode`) or HTTP APIs
(`exaone`, `zai`). On the hub (and opted-in worker nodes), credentials are stored in `~/.guardian/llm.env` (`0600`).

**Never copy `~/.claude/.credentials.json` or `~/.codex/auth.json` between nodes**: OAuth refresh tokens rotate on
use, causing `401` errors on other nodes. Instead, generate a long-lived token once with `claude setup-token`, save it
as `CLAUDE_CODE_OAUTH_TOKEN` in your local `~/.config/server-guardian/llm.env`, and deploy it:

```bash
# Deploy to the hub (default) and run a PONG check per provider
scripts/deploy-llm-env.sh

# Deploy to specific nodes as well
scripts/deploy-llm-env.sh site-a-arm1 node-1

# Verify existing credentials without overwriting
scripts/deploy-llm-env.sh --check-only
```

Common keys in `~/.guardian/llm.env`:
- `CLAUDE_CODE_OAUTH_TOKEN`: long-lived token for `claude` CLI.
- `TYPESAFE_API_KEY`: enables the `jev` gate (`~/jev-mcp` on the hub).
- `EXAONE_API_KEY` / `OPENCODE_EXAONE_API_KEY`: enables `exaone` (HTTP) and `opencode` (CLI) providers.
- `ZAI_API_KEY`: enables `zai` provider.

Key environment variables in `~/.guardian/agent.env` on the hub:
- `AGENT_TOKEN` (required): bearer token for the hub API and dashboard login.
- `GUARDIAN_LLM_PROVIDERS`: provider fallback order (for example `claude,codex,agy`).
- `GUARDIAN_LLM_TIMEOUT`: per-call timeout in seconds (default `300`, `600` on hub).
- `GUARDIAN_CLAUDE_EFFORT`: `--effort` level (`medium` default; `default` defers to CLI config).
- `GUARDIAN_JEV_GATE=off`: disables the `jev` gate (tests only).
- `GUARDIAN_AUTO_APPLY=off` / `GUARDIAN_UI_ACTIONS=off`: disables automatic action execution or all write/decision
  actions from the dashboard UI.

## 4. Guardian skills for LLM CLIs

`deploy/llm-skills/` contains shared `SKILL.md` skills installed into `~/.claude/skills`, `~/.codex/skills`, and
`~/.gemini/config/skills` so operators talking to an LLM CLI on a node can invoke Guardian checks cleanly:

| Skill | Scope | Purpose |
|---|---|---|
| `guardian-host-check` | `all` (hub + Tier-1) | Read-only health, disk, and service checks of the local node |
| `guardian-host-cleanup` | `all` (hub + Tier-1) | Guided cleanup of the local node using catalog actions and deny rules |
| `guardian-fleet-ops` | `hub` | Ops watch dry-run, starting/following ops debates, running approved actions |
| `guardian-hub-status` | `hub` | Hub agent, systemd timers, audit log, and debate state inspection |
| `guardian-ask` | `hub` | Thin front-door to `guardian.py --agent-run` (Quick ask) |

Deploy or preview skill installation:

```bash
scripts/deploy-llm-skills.sh --dry-run
scripts/deploy-llm-skills.sh
```

Only `guardian-*` skills are managed; debates themselves never load these skills. See `deploy/llm-skills/` for the
skill directory layout and `SKILL.md` format.

## 5. Execution environments (workers)

Optional model workers let capable Tier-1 nodes run debate agent turns (or host resident agents over worker protocol
3). Workers are stateless for debates, stdlib-only, and never hold `AGENT_TOKEN`.

```bash
# Probe a candidate node against configs/worker-requirements.json
scripts/probe-worker.sh node-1

# Install or upgrade the worker on an eligible node
scripts/install-worker.sh node-1
```

View worker status in the dashboard under **Settings > Execution environments** (`#/settings/workers`) or via CLI:

```bash
python3 cli/llm-agent/guardian.py --workers
```

## 6. Local development and CI

```bash
# 1. Run the Go hub agent locally (listens on 127.0.0.1:9870)
cd apps/agent
go build -o agent .
AGENT_TOKEN=$(openssl rand -hex 32) ./agent

# 2. Run the React dashboard dev server (http://localhost:5173/)
cd apps/web-desktop
npm install
npm run dev

# 3. Run CI checks (sections: go, python, web, shell, config; tauri when named)
scripts/ci.sh
scripts/ci.sh go python config
```

## 7. Backups and scheduled timers

The hub installer enables two systemd timers automatically:
- `server-guardian-ops-watch.timer` (02:30 UTC daily): runs `guardian.py --ops-watch` for ops projects with
  `policy.watch`.
- `server-guardian-db-backup.timer` (03:30 UTC daily): writes a consistent DB copy and data tarball to
  `~/.guardian/data/backups/` (keeping 14 copies, no private keys) and replicates to `backup_replica`.

See [Hub backup and restore](../operations/backup-restore.md) for restore steps and drills.

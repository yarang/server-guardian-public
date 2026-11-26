# User Guide

English | [한국어](README.ko.md)

This guide explains how to deploy, operate, and use Server Guardian day to day. For terminology used across the
dashboard, CLI, and documents, see the [Glossary](../glossary.md). For architecture and design rationales, see
[README](../../README.md) and [agent.md](../../agent.md).

## What Server Guardian does

Server Guardian manages a fleet of Linux nodes and software repositories from **one hub** (`guardian_role: hub`).
Other nodes run no agent by default; the hub reaches them over SSH with a dedicated fleet key.

- **Fleet management**: monitor hub metrics, probe node reachability, organize dynamic node groups, run SSH security
  audits (`SEC-SSH-001` to `SEC-SSH-005`), and execute ad-hoc commands or verified command sets across nodes.
- **Multi-agent debates and flows**: run structured debates (`Expert`, `Critic` panel, `Judge`) or single-role quick
  asks against **code**, **ops**, or **design** projects using installed LLM CLIs (`claude`, `codex`, `agy`, `exaone`,
  `opencode`, `zai`).
- **Safe change control**: every proposed code change, node cleanup, package plan, or batch script goes through typed
  action checks, gates (`jev` gate or critic vote gate), and explicit human approval before anything leaves the
  workspace or modifies production nodes.
- **Resident agents and operator sessions**: run persistent named agents on opted-in Tier-1 nodes and connect
  interactive Claude Code or Antigravity (`agy`) sessions on your workstation to the hub's mailbox network via MCP.

## Guide modules

| Guide | What it covers | Best for |
|---|---|---|
| [Setup and deployment](setup-and-deployment.md) | Hub installation and rollback, fleet SSH key protection, LLM CLI credentials, Guardian CLI skills, execution workers, local dev, CI gate | Administrators setting up or upgrading the hub |
| [Dashboard](dashboard.md) | Signing in, the five destinations (`Overview`, `Nodes`, `Workflows`, `Approvals`, `Settings`), language toggle, Office export (DOCX/PPTX/Mermaid) | Daily web and desktop dashboard users |
| [Fleet and ops](fleet-and-ops.md) | Inventory, node groups, remote exec, SSH audit, ops projects, cleanup action catalog, nightly ops watch, provisioning plans, batch execution | Infrastructure and fleet operators |
| [Debates and projects](debates-and-projects.md) | Code/ops/design projects, starting debates and flows, Quick ask, teams/personas, Code Wiki, whole-repo audit, approvals, Publish | Developers and reviewers |
| [Resident agents and operator sessions](residents-and-sessions.md) | Persistent Tier-1 resident agents (closed and sandboxed open tools), mailbox and task board, workstation session link daemon, Claude Code and Antigravity (`agy`) MCP & hooks | Power users coordinating agents across nodes and workstations |

## Quick start by role

### First-time administrator
1. Follow [Setup and deployment](setup-and-deployment.md) to deploy the hub release, configure the hub-to-node SSH key,
   and deploy LLM CLI credentials.
2. Open the dashboard at `http://<hub-tailscale-ip>:9870/` and sign in with `AGENT_TOKEN` ([Dashboard](dashboard.md)).
3. Run a fleet reachability check and SSH audit under **Nodes** ([Fleet and ops](fleet-and-ops.md)).

### Developer working on a repository
1. Register or select a `code` project under **Settings > Flow setup > Projects**.
2. Start a debate or a **Quick ask** from **Workflows** (or `guardian.py --debate` / `--agent-run`)
   ([Debates and projects](debates-and-projects.md)).
3. Review proposed actions under **Approvals**, run tests, and push to a new branch with **Publish**.

### Operator connecting a workstation CLI (Claude Code / Antigravity)
1. Register your workstation on the hub (`guardian.py --session-add`) and install the workstation link daemon with
   `scripts/install-session-link.sh` ([Resident agents and operator sessions](residents-and-sessions.md)).
2. Wire the `guardian-session` MCP server and hooks into Claude Code or Antigravity.
3. Call `session_link` to claim an address such as `work@ws-mac-1`, then exchange messages with resident agents,
   other sessions, and `operator`.

## Related operational runbooks

- [Hub backup and restore](../operations/backup-restore.md)
- [headscale operations](../operations/headscale.md)
- [Fleet sshd hardening procedure](../operations/2026-10-05-fleet-sshd-hardening.md)

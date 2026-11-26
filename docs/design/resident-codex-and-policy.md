# Resident Codex, Artifact Export, and Package/Update Policy Revision (Design)

English | [한국어](resident-codex-and-policy.ko.md)

Status: Decided 2026-11-22 (PD1, PD2, PD3-a, PD4 approved by the operator; Phases 1–3 implemented and deployed at `2a3bfaf`, sandbox probe verified at version `827bc3cc0a6b`).

This document proposes three connected revisions to [resident-agents.md](resident-agents.md) (M0) and
[resident-open-tools.md](resident-open-tools.md) (OD1, OD3, OD4, OD6):

1. Enable **`codex`** (using ChatGPT subscription login `auth.json`) as a sandboxed `tools: "open"` resident agent
   (realizing Rule 7 and phase O4 of `resident-open-tools.md`).
2. Add a **persistent output and artifact export path** so deliverables produced by individual resident agents are not
   trapped inside their `0700` sandbox home or lost when a turn or agent ends.
3. Revise the policy on **program installation and system updates** so resident agents can install tools and assist
   with OS updates without exposing node credentials or fleet SSH keys to prompt-injected peer mail.

## 1. Why the current policy blocks these three goals

| Goal | Current state in code | Exact blocker |
|---|---|---|
| Add `codex` resident agent | `configs/resident-providers.json` pins `codex` as `"resident": false` (`0.163.0`); `OPEN_PROVIDERS = ("claude", "agy")` in `resident_registry.py` and `resident_open.py`; `guardian-sandbox-launch.py` and `sandbox-remote-install.sh` accept only `claude` and `agy`. | M0 Layer 2 failed for **closed** tools (`apply_patch` and collaboration tools stay offered), and O2 only wired `claude` and `agy` into the **open** sandbox path even though Rule 7 noted `codex` can run under `open`. |
| Keep and retrieve non-git agent artifacts | `/home/gsa-<name>` persists on disk (`BindPaths=/home/gsa-<name>`), while `/tmp` is `PrivateTmp=yes` and disappears after every turn. The only exit channel from the sandbox is `task_update` with a git `patch` from `task_worktree` (OD4). | Reports, scripts, diagrams, build outputs, and diagnostic bundles written outside a code task's git worktree stay locked inside `0700` `/home/gsa-<name>`—invisible to the hub, dashboard, and other agents, and deleted if written to `/tmp` or when an agent is removed. |
| Support program installation and system updates | `guardian-sandbox-run.sh` sets `ProtectSystem=strict`, `NoNewPrivileges=yes`, empty `CapabilityBoundingSet=`, and `PATH=/usr/local/bin:/usr/bin:/bin`; OD6 leaves all host/fleet mutations to the hub and a person. | User-space tools under `$HOME/.local/bin` are not on `PATH`, and OS-level `apt update/upgrade` or `apt install` are completely unavailable to resident agents. |

## 2. Pillar 1: `codex` as an open-tools resident agent

### Registration and M0 table
- Keep `codex` `"resident": false` for `tools: "closed"` (no weakening of M0 closed-tools guarantees).
- Add an explicit `"open_resident": true` (and pinned `"version": "0.163.0"`) capability in
  `configs/resident-providers.json` and widen `OPEN_PROVIDERS` to `("claude", "agy", "codex")` in:
  - `cli/llm-agent/resident_registry.py` (when `tools == "open"`, accept providers with `open_resident: true` and a
    passing sandbox probe on the node),
  - `cli/llm-agent/resident_open.py` (`OPEN_PROVIDERS`, `CODEX_VERSION`, `expected_cli_versions()`),
  - `deploy/sandbox/guardian-sandbox-launch.py` (`PROVIDERS`, `build_env`, `codex_auth`),
  - `scripts/lib/sandbox-remote-install.sh` (`--cli codex=<version>`, copying the binary from the node user's
    `~/.local/bin/codex` or `/usr/bin/codex` into root-owned `/usr/local/lib/guardian-sandbox/cli/codex-<version>/`).

### Authentication (ChatGPT subscription `auth.json`)
To satisfy OD3 (per-agent revocable credential) and the standing rule against copying `auth.json` across nodes
(refresh token rotation causes `401` errors):
- Each `codex` open agent keeps its own `auth.json` at `/home/gsa-<name>/.codex/auth.json` (`0600`, owned by
  `gsa-<name>`, `CODEX_HOME=/home/gsa-<name>/.codex`).
- `scripts/sandbox-credential.sh` gains `--provider codex`:
  - Either installs a dedicated `auth.json` piped via stdin (never logged, never in argv/env, never copied from
    another node), OR provides a `--login` helper that runs `codex login --device-auth` as `gsa-<name>` directly on
    the target node so the agent holds its own ChatGPT session token.
- `guardian-sandbox-launch.py` checks that `$HOME/.codex/auth.json` exists, is owned by `gsa-<name>`, and has mode
  `0600` (otherwise exits 3 with `launch: no_credential`).

### Turn execution and resume
- `resident_driver.py` runs `codex` inside the sandbox unit via `guardian-sandbox-launch.py`:
  - First turn: `codex exec --json --skip-git-repo-check -s danger-full-access -a never -C <workdir>` (the outer
    `systemd` unit is the OS sandbox; inner bubblewrap/Landlock would fail under `RestrictNamespaces=yes`), with
    `-c mcp_servers.mailbox...` and `-c mcp_servers.repo...` pointing at the sandbox socket's MCP copy.
  - Subsequent turns: `codex exec resume <thread_id> --json ...` using `thread_id` parsed from `thread.started`.
  - Disable auto-update checks and isolate `CODEX_HOME=$HOME/.codex` so no node-user skills or config leak in.

## 3. Pillar 2: Persistent outputs and artifact export (revising OD4)

To ensure resident agent deliverables are never trapped or silently lost:

### Persistent workspace layout inside `/home/gsa-<name>`
- `/home/gsa-<name>/work/`: working directory (persists across turns; task git worktrees live under `work/tasks/`).
- `/home/gsa-<name>/out/`: designated output directory for deliverables (persists across turns; never under `/tmp`).
- `/home/gsa-<name>/.local/bin`: added to `CLI_PATH` in `guardian-sandbox-launch.py`
  (`$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin`) so user-space tools installed by the agent persist across turns.

### Hub artifact export (`artifact_publish`)
Extend `mailbox_mcp.py` (for open agents), `resident_node.py`, `mail_relay.py`, and the hub store with a typed export
path parallel to `task_update` patches:

1. **MCP tool `artifact_publish(path, title, task=None, summary="")`**:
   - Reads a regular file inside `/home/gsa-<name>/out/` (or `/home/gsa-<name>/work/`), verifying `O_NOFOLLOW` and no
     path traversal outside `$HOME`.
   - Enforces a size limit (default `10 MiB` per artifact, configurable) and scans text payloads with `mask_secrets`
     before leaving the node (binary artifacts carry sha256 + MIME/extension allowlist).
2. **Hub storage**:
   - Stored durably under `~/.guardian/data/resident-artifacts/<agent>/<artifact_id>/` with metadata in SQLite
     (`resident_artifacts` table: `id`, `agent`, `task_id`, `filename`, `sha256`, `size_bytes`, `created`, `expires`).
   - Included in the nightly backup (`server-guardian-db-backup.timer`).
   - Retained for 90 days by default (or pinned indefinitely by the operator); removing a resident agent does **not**
     delete already-exported artifacts on the hub.
3. **Access**:
   - Downloadable and previewable in the dashboard under **Workflows > Resident agents** (`#/debates/residents`) and
     via CLI (`guardian.py --resident-artifacts [AGENT]`, `--resident-artifact-get <ID>`).
   - Optionally referenced by artifact id in `mail_send` / `task_update` so other agents or operator sessions know
     where the deliverable landed.

Code changes to a registered Git project continue to export as `PROPOSED jev_mcp:apply_patch` via `task_update` so
review, diff, and **Publish** workflows stay unchanged.

## 4. Pillar 3: System updates and program installation (revising OD1 / OD6)

Program installation splits cleanly into two privilege tiers:

### Tier 1: User-space program installation (always allowed in `tools: "open"`)
- Because `/home/gsa-<name>` is writable and `$HOME/.local/bin` is added to `PATH`, any open resident agent can
  install and persist user-space tools (`uv`, `pipx`, `python3 -m venv`, `npm --prefix ~/.local`, `cargo`, standalone
  binaries) immediately during a turn—zero root privilege required, zero change to host isolation.

### Tier 2: OS-level system updates and system packages (`apt`, `systemctl`)
Two policy options are offered for operator decision **PD3**:

#### Option PD3-a (Recommended): Hub-brokered ops proposals (`ops_propose`)
- Resident agents remain non-root inside `gsa-<name>`.
- Expose a read-only check + proposal MCP surface (`ops_propose_package`, `ops_propose_update`) that files a typed
  `PROPOSED` action or plan on the hub using Server Guardian's existing `ops_packages.py`, `package_roles.py`
  protection, preview/drift/verify checks, and `exec.jsonl` audit log.
- Add a typed `ops:system_update` action (`apt-get update` + `apt-get upgrade --with-new-pkgs` / security-only scope,
  previewing upgradable packages via `apt list --upgradable` and verifying post-upgrade state).
- **Approval policy**:
  - Arbitrary `ops:package_install` / `ops:package_remove` / full `apt upgrade` wait in **Approvals** (1-click
    confirm in dashboard or CLI `--run-action`).
  - Optional opt-in `policy.auto_run` can allow `apt update` or security-only updates / vetted bundles without manual
    clicks when `jev` passes.
- **Why this pairs well with Pillar 2**: artifacts no longer stay trapped in the sandbox (Pillar 2 exports them to the
  hub), user-space tools install immediately (Tier 1), and system-wide OS mutations stay protected against
  prompt-injected peer mail!

#### Option PD3-b: Scoped root package wrapper (`tools: "host-ops"` mode)
- Introduce a third opt-in agent mode `tools: "host-ops"` (separate from `closed` and `open`) for designated nodes:
  - Keeps home/SSH/`llm.env`/tailnet isolation from `guardian-sandbox-run.sh`, **except** permits invoking a fixed
    root helper `/usr/local/sbin/guardian-pkg-run` via a restricted socket/broker for a strict command allowlist:
    `apt-get update`, `apt-get upgrade`, `apt-get install <validated-pkg-names>`, `apt-get autoremove`.
  - **Mandatory guard**: a `host-ops` agent MUST have `peers: []` (accepts mail/tasks **only from `operator`** or
    explicitly trusted local operator sessions—never from other resident agents) so peer mail cannot trigger root
    `postinst` scripts.
  - Every invocation is logged to the hub audit trail.

#### Option PD3-c: Hybrid (Tier 1 + PD3-a for all `open` agents, plus opt-in PD3-b for one designated `operator`-only updater agent)
- Gives ordinary open agents (`coder`, `codex`) safe user-space installs + hub-approved OS proposals + artifact
  export, while allowing a single dedicated `operator`-only agent to run OS updates directly when explicitly opted in.

## 5. Threat model and invariants that remain intact

Regardless of PD3:
- Node user's `~/.ssh`, `~/.guardian/llm.env`, `/etc/guardian/fleet_key`, and `worker.sock` remain hidden from
  resident agents.
- Mail from peers still grants no permission (`INBOX_PREAMBLE` and start card rules unchanged).
- `git push`, hub deploy, and cross-node fleet changes remain with the hub and a person (OD6).
- Existing `claude@node-1` and `agy@node-1` stay `tools: "closed"` unless explicitly changed.

## 6. Phased implementation plan

1. **Phase 1 — `codex` in `tools: "open"` + persistent `$HOME/.local/bin` & `$HOME/out/`**:
   - Update `configs/resident-providers.json`, `resident_registry.py`, `resident_open.py`, `resident_driver.py`,
     `deploy/sandbox/guardian-sandbox-launch.py`, `scripts/lib/sandbox-remote-install.sh`,
     `scripts/sandbox-credential.sh`, and unit tests.
   - Run `scripts/ci.sh`, deploy hub, reinstall worker & sandbox on `node-1` (bump sandbox version + `--record` probe),
     provision `gsa-codex` ChatGPT login, and register `codex@node-1`.
2. **Phase 2 — Sandbox artifact export (`artifact_publish`)**:
   - Add `resident_artifacts` table & storage on the hub, `artifact_publish` MCP tool, relay forwarding, CLI
     (`--resident-artifacts` / `--resident-artifact-get`), dashboard download in **Resident agents**, and backup
     inclusion.
3. **Phase 3 — System updates & OS package installation (per PD3)**:
   - Implement `ops:system_update` + resident proposal tools (PD3-a), or `guardian-pkg-run` + `tools: "host-ops"`
     (PD3-b), or Hybrid (PD3-c), with full unit tests and probe updates.

## 7. Operator decisions (2026-11-22)

All four decisions were approved by the operator on 2026-11-22:

| # | Decision | Decided (2026-11-22) |
|---|---|---|
| **PD1** | `codex` resident mode | **Enable under `tools: "open"`** (`closed` remains refused; matches Rule 7 / O4 of `resident-open-tools.md`) |
| **PD2** | `codex` ChatGPT auth | **Dedicated `/home/gsa-codex/.codex/auth.json`** via `codex login --device-auth` as `gsa-codex` (avoids token rotation conflict with node user) |
| **PD3** | System updates & OS package install policy | **PD3-a**: hub-brokered ops proposals (`ops_propose_package` / `ops_propose_update`) + typed `ops:system_update`, reusing Approvals, `package_roles.py` protection, audit log, and optional `policy.auto_run` for low-risk updates |
| **PD4** | Sandbox artifact export | **Adopted**: persistent `$HOME/out/` + `$HOME/.local/bin` + `artifact_publish` to hub (`~/.guardian/data/resident-artifacts/`, 90-day retention, included in nightly backup) |

# Install tracks: developer and operator

English | [한국어](install-tracks.ko.md)

Status: design agreed (2026-10-18); R1 implemented and deployed (section 13); the phases in section 10 are each confirmed before
they start. Related: [mesh-join.md](mesh-join.md), [headscale operations](../operations/headscale.md),
[backup-restore.md](../operations/backup-restore.md).

## 1. Problem

Today every install and update starts on the admin Mac. `scripts/install-agent-service.sh` (549 lines) builds the Go
agent, goindex and the dashboard on the Mac, uploads them over SSH, and runs every hub step remotely. The Mac also
issues the hub's fleet key (`setup-hub-ssh.sh`), distributes LLM tokens (`deploy-llm-env.sh`), keeps the redaction
rules and builds the public copy.

So running Server Guardian requires a development machine, although most of those jobs are not development. And
the Mac mixes two different things: a workstation and the off-hub emergency credentials.

## 2. Goal

Split three roles that are mixed today:

| Role | What it is | Needs |
|---|---|---|
| **Developer track** | changes the code, runs CI, builds and signs releases, builds the public copy | macOS or Linux with the repository and toolchains |
| **Operator track** | installs, updates and runs Server Guardian | a browser for daily work; a shell on the hub for the first install |
| **Break-glass credential** | reaches the nodes when the hub or the tailnet is down | an encrypted bundle kept off the hub, plus the cloud consoles. Not a machine |

An operator never builds anything and never needs a standing admin PC. The only thing that must live outside the hub
is the break-glass credential.

## 3. What moves where

| Today (admin Mac) | Developer track | Operator track |
|---|---|---|
| `scripts/ci.sh` | stays | — |
| `install-agent-service.sh` (build + remote install) | stays for fast development deploys, rewritten to upload a tree and run the hub-side installer (R1) | — |
| — | `scripts/release.sh`: build, sign, publish (R2) | — |
| — | — | `install.sh` run on the hub from a verified release (R3) |
| redeploy for every change | — | dashboard update with automatic rollback (R4) |
| `setup-hub-ssh.sh` (issue and register the fleet key) | stays | first nodes: the key line from node registration is pasted on the node (cloud-init, console); rotation runs on the hub (R5) |
| `deploy-llm-env.sh`, `deploy-llm-skills.sh` | stay | hub-side versions that read the token on the hub and push to Tier-1 nodes over the fleet SSH (R5) |
| `export-public.sh`, `redact-private.py` | stay | — |
| `export-credentials.sh` | stays (developer's own bundle) | break-glass bundle made once at install time (R3) |
| node onboarding | — | mesh join approval and node registration in the dashboard (done) |

## 4. Release

### 4.1 Contents

One archive per architecture, `server-guardian-<version>-linux-<arm64|amd64>.tar.gz`:

- `bin/agent`, `bin/goindex`: built with `CGO_ENABLED=0`, `AgentVersion` stamped with `<version>`;
- `ui/`: the built dashboard (`apps/web-desktop/dist`);
- `cli/`, `configs/`, `deploy/`, `tools/` and the hub-side `install.sh`, from `git archive <tag>` (no `.git`, no
  tests, no docs except the operator guide);
- `MANIFEST`: sha256 of every file; `VERSION`.

Next to it: `<archive>.sha256` and `<archive>.sig`. The signature covers the archive.

The hub never builds: it has no npm (and should not get one, since npm install scripts run code), and building on
the hub would put the toolchain inside the security boundary.

### 4.2 Versions

Git tags `vYYYY.MM.DD` (a second release on one day gets `.2`). `AgentVersion` and `/api/status` report it. The hub
refuses a version older than the installed one unless it is the recorded previous version (rollback).

### 4.3 Signing

The hub verifies every release with a public key pinned at install time in `/etc/guardian/release_signers` (root,
0644). The agent cannot change that file (`/etc/guardian` is already in the unit's `InaccessiblePaths`), so a
compromised agent can at most ask for another release signed by the developer.

Options:

| | `ssh-keygen -Y sign` / `-Y verify` | minisign |
|---|---|---|
| Tools | OpenSSH 8.1+, already on the Mac and the hub | an extra package on both sides |
| Key | ed25519 or a hardware key (`ed25519-sk`) | ed25519 |
| Format | `allowed_signers` with a namespace (`server-guardian-release`) | `.minisig` |

Recommendation: `ssh-keygen -Y`. Nothing new to install, and a hardware key is possible. The signing key stays on the
developer machine and in the developer's own encrypted bundle, never on the hub.

### 4.4 Where releases are published

The signature protects integrity, so the host only needs availability.

| | Gitea (git.example.com) | GitHub Releases of the public copy | Both |
|---|---|---|---|
| Access | the repository is private: the hub needs a read token (a new secret on the hub) | public, no token | hub uses the public one |
| Content | built from main | built from main: code and configs hold placeholders only, and `redact-private.py --check` gates it | — |
| Outage | down together with the hub (Gitea runs on the hub) | independent of the fleet | — |

Recommendation: GitHub Releases of the public copy. No secret on the hub, and it stays reachable when the hub (and
with it Gitea) is down. It is outward-facing, so it is the user's decision.

## 5. Hub layout

One layout for both tracks, so a developer deploy and an operator update leave the hub in the same shape:

```text
~/.guardian/
  releases/<version>/      extracted release, read-only (developer deploys: <git describe>-dev)
  current -> releases/<v>  what the units run
  previous -> releases/<v> the rollback target
  agent.env                unchanged
  data/                    unchanged (production state)
```

- The agent unit runs `~/.guardian/current/bin/agent` with `WorkingDirectory=~/.guardian/current` and
  `GUARDIAN_CLI=~/.guardian/current/cli/llm-agent/guardian.py`. The timers use the same paths.
- `~/server-guardian` (today's checkout) is no longer what runs. It stays, in the developer track only, as the
  source of the builtin `server-guardian` code project and its workspace clone (`GUARDIAN_SELF_REPO`). An operator
  install has no self project.
- Releases are kept until there are 3 (current, previous and one more).

## 6. Hub-side installer (`install.sh`)

The steps of today's remote installer, run on the hub as the login user with sudo. The same script serves the first
install, a developer deploy and an update.

1. Check the platform (Ubuntu or Debian, systemd, arm64/amd64) and the prerequisites (python3, git, openssl,
   tailscale joined to a tailnet).
2. Extract into `releases/<version>/` and check `MANIFEST`.
3. First install only: create `~/.guardian/data`, generate `AGENT_TOKEN` on the hub, pin
   `/etc/guardian/release_signers`, seed the inventory with one hub entry (id, hostname, Tailscale IP detected
   from `tailscale ip -4`, `guardian_role: hub`).
4. Fleet key protection (`deploy/hub/protect-fleet-key.sh`), systemd units, sudo drop-in, sshd hardening, backup and
   ops watch timers: as today.
5. Optional components, asked on first install and remembered in `agent.env`: the Go toolchain for judge patches
   (only code projects with Go need it), LLM CLIs (`install-llm-clis.sh` logic).
6. Switch `current`, restart, health check (section 7.3), print the dashboard URL and where the token is.
7. First install only: print the break-glass steps (section 9).

`install-agent-service.sh` becomes: build, assemble the same tree, upload, run `install.sh --dev`. The Mac-only
logic (cross-compiling, upload with sha256 and Tailscale retry) stays in it; everything that changes the hub moves
into `install.sh`.

## 7. Updates from the dashboard

### 7.1 Who does what

The agent runs with `NoNewPrivileges` and must not get the right to replace itself. So the update is split:

- **Agent** (unprivileged): Settings > Updates shows the installed version, the newest release (the hub reads the
  release index) and its notes. "Install" writes `~/.guardian/data/update/request.json` (`{version, requested_by,
  at}`) and an audit line.
- **Updater** (`server-guardian-updater.path` watching that file, starting `server-guardian-updater.service`, a
  oneshot that runs the hub-side installer with the rights it needs): does the update and writes
  `update/result.json` and its log, which the dashboard shows.

### 7.2 Steps of the updater

1. Refuse when a debate or an action is running (same check as a deploy). Retry is the operator's.
2. Download the archive, `.sha256` and `.sig` into a staging directory; verify the signature against
   `/etc/guardian/release_signers` and the sha256; refuse anything else.
3. Refuse a version older than `current`, except `previous` (rollback).
4. Back up the DB (`db_backup.py`, as the nightly timer does).
5. Run the new release's `install.sh --update`.
6. Health check; on failure switch `current` back to the old release, restart, and record the failure.

### 7.3 Health check

Within 60 s after restart: `/api/status` answers and reports the new version, and `guardian.py --help` runs from the
new tree (a `--version` flag is added in R4). A failed check rolls back automatically.

### 7.4 Policy

Manual by default: the dashboard shows that an update exists, a person installs it. An automatic nightly update is
a later option (`GUARDIAN_AUTO_UPDATE`), off by default.

## 8. Daily work without an admin PC

- Nodes join with the mesh join approval and are added with node registration (done). For a node that does not have
  the hub key yet, the registration page shows the exact `authorized_keys` line; the operator puts it there through
  the cloud's console or cloud-init.
- LLM tokens: entered on the hub (`~/.guardian/llm.env`) and pushed to Tier-1 nodes by a hub-side
  `deploy-llm-env` (R5).
- Fleet key rotation runs on the hub, which already reaches every node with the old key (R5).
- Debates, actions, ops watch, backups: as today.

## 9. Break-glass credential

Needed when the hub or the tailnet is down: to restore the hub, repair headscale, or reach a node.

- **Minimum:** the cloud providers' consoles (serial or instance console) and their account recovery. Nothing to
  keep, but slow and per provider.
- **Recommended:** an SSH key pair generated off the hub (`ssh-keygen` on any machine), whose public key is on every
  node's `authorized_keys` without the hub's `from=` restriction, kept only in an encrypted bundle stored offline
  (and with a copy in a second place). The hub never sees the private key.
- The bundle also holds the headscale backup (until headscale backups go off the control server), the release signing key in the
  developer track, `llm.env`, the redaction rules and `CLAUDE.local.md` (the gaps of today's bundle).

The first install prints these steps and does not finish its checklist until the operator confirms the bundle
exists. Using it is the procedure in [headscale operations](../operations/headscale.md) ("Emergency access") and
[backup-restore.md](../operations/backup-restore.md).

## 10. Phases

| Phase | Content | Changes for today's hub |
|---|---|---|
| **R1** | hub-side `install.sh`; release-dir layout with `current`/`previous`; `install-agent-service.sh` uploads a tree and calls it | the hub moves to the new layout; behaviour unchanged |
| **R2** | `scripts/release.sh` (build both architectures, MANIFEST, sign, publish); first tag | — |
| **R3** | operator first install from a verified release; operator guide (prerequisites: headscale template, DNS, first nodes, break-glass bundle) | — |
| **R4** | updater path/service, Settings > Updates, rollback | updates without the Mac |
| **R5** | hub-side LLM token deploy and fleet key rotation; bundle gaps | the Mac is no longer needed for operations |

Each phase is deployed through the usual gate and keeps the developer deploy working.

## 11. Decisions (2026-10-18)

1. Signing: `ssh-keygen -Y` with namespace `server-guardian-release` (a hardware key is allowed, not required).
2. Release host: GitHub Releases of the public copy (`owner/server-guardian-public`); hubs download from there
   without a token.
3. Versions: date tags `vYYYY.MM.DD` (`.2` for a second release on one day).
4. Break-glass: a separate SSH key made off the hub, its public key on every node without `from=`, the private
   key only in the offline encrypted bundle.
5. The operator track supports ops and code projects; the Go toolchain stays an install option.

## 12. Risks

- **A stolen signing key** lets someone ship a release every hub installs. Keep it on a hardware key or in the
  encrypted bundle only, and rotate by adding the new key to `release_signers` in a release signed by the old one.
- **The updater runs with more rights than the agent.** Its input is only a version string; everything it installs
  must pass the signature check, and it never runs code from the request.
- **Layout migration (R1)** touches the running hub. It needs a dry run in an evaluation sandbox (`~/guardian-eval`) first and a tested way
  back to `~/server-guardian`.
- **Two install paths drifting apart.** Both tracks run the same `install.sh`; CI runs it against a scratch
  directory.

## 13. Implementation notes

### R1 (2026-10-18)

Implemented: `deploy/hub/install.sh` (with `install_go.py`, `isolation-check.sh`, `agent.env.examples` next to it),
units that run `~/.guardian/current`, and `scripts/install-agent-service.sh` rewritten to build, upload and call it.

- **Release names** are `<git describe --dirty>-<UTC time>`, so redeploying one commit gives a new directory.
  `AgentVersion` stays the `git describe` value; `install.sh --expect-version` compares it after the restart.
- **The release tree** holds the tracked files of `cli/`, `configs/` and `deploy/` as they are in the working tree
  (tests and `testenv.py` left out; untracked files never), plus `bin/agent`, `bin/goindex`, `ui/`, `VERSION` and
  `MANIFEST` (525 files). After the check it is made read-only; no `__pycache__` is written.
- **Start failure** restores the unit files saved in `~/.guardian/units.prev/` and the previous link, then starts
  that (exit 3). On the first migration there is no previous release, so the old units bring back the old layout
  (`~/.guardian/agent`, `~/server-guardian`), which is left in place for that.
- **Running debates:** `install.sh` (install and `--rollback`) refuses with exit 4 while a `guardian.py
  --result-file` process runs; `--force` overrides. Added after the incident below.
- **goindex** is a link `~/.local/libexec/guardian/goindex -> ~/.guardian/current/bin/goindex`, so it follows a
  rollback. The skill `guardian-fleet-ops` now works in `~/.guardian/current/cli/llm-agent`.
- **Checks done on the hub:** a dry run (only the three unit paths changed; the operator's `agent.env` keys kept),
  the migration, API and CLI calls from the read-only release (projects, teams, debates, ops watch dry run), and
  `--rollback` there and back.
- **Not done:** CI does not run `install.sh` (it needs systemd and sudo); it is checked by `bash -n` and
  shellcheck only. The old files `~/.guardian/agent`, `agent.prev` and `ui/` stay until the old layout is no
  longer a fallback.

**Incident.** During the rollback test a second deploy restarted the hub agent while a dashboard debate
(`dbt_20261018_050641_0640b0`, fleet, round 4) was running; it ended as `interrupted`. The pre-deploy check had
listed it, but the command chain went on (`;` instead of `&&`). The debate was started again with the same task,
team and thread (`dbt_20261018_050914_5721b9`), and `install.sh` now refuses on its own.

# Resident agents with open tools (design)

English | [한국어](resident-open-tools.ko.md)

Status: proposed 2026-11-15; OD1-OD6 decided by the operator 2026-11-15 as recommended. Operator decision 2026-11-15: resident agents may keep their built-in tools, file writes
and the shell included, instead of having them closed. This revises closure layer 2 ("no built-in tools") and D4
("read-only first") of [resident-agents.md](resident-agents.md) for agents declared with `tools: "open"`. Nothing is
built. The deployed agents `claude@node-1` and `agy@node-1` stay closed until phases O1-O3 below pass on their node.

## Goal

A resident agent that can run commands, build, test and edit files in a working copy, as a person at a terminal
would, while a mail written by another model still cannot reach anything outside that agent's own sandbox.

## What an open shell gets on a node today

Checked on `node-1` on 2026-11-15 (names only, no values read):

- The node user is in the groups `sudo` (passwordless), `lxd` and `adm`. Either of the first two is root on the host.
- Its home is `0750`; `~/.ssh` holds private keys and a client config with entries for other hosts.
- `~/.guardian/llm.env` holds every provider key of that node (`CLAUDE_CODE_OAUTH_TOKEN`, `ZAI_API_KEY`,
  `EXAONE_API_KEY`, `OPENCODE_EXAONE_API_KEY`).
- `worker.sock` and every agent's socket directory belong to the node user.

A built-in shell running as the node user is therefore root on a fleet host, with every key and ssh access to other
hosts, driven by mail another model wrote. The closed design accepted "all agents run as one Unix user" only because no
agent had a shell ([resident-agents.md](resident-agents.md), Risks). With open tools that limit must become a real
separation. That is the first rule; the rest follow from it.

## Rules

1. **One Unix user per open agent.** The installer creates `gsa-<name>` (system user, no password, own home `0700`,
   no supplementary groups: never `sudo`, `lxd`, `adm`, `docker`). The node user never runs an open agent's CLI.
2. **Every turn runs in a sandboxed systemd unit.** The worker daemon starts the turn as a transient system unit
   through a fixed sudo rule that allows only that unit template (no general sudo for the daemon's callers):
   - `User=gsa-<name>`, `NoNewPrivileges=yes`, empty `CapabilityBoundingSet=`, `RestrictSUIDSGID=yes`,
     `PrivateTmp=yes`, `PrivateDevices=yes`, `ProtectKernelTunables/Modules/Logs=yes`, `ProtectControlGroups=yes`;
   - `ProtectSystem=strict`, `ProtectHome=tmpfs` with `BindPaths=` for the agent's own home only, so the node user's
     home and every other agent's home are not visible;
   - `IPAddressDeny=` for the tailnet (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), RFC 1918, link-local and the cloud
     metadata address; unix sockets are not affected, so the mailbox socket still works. Loopback stays reachable
     (the resolver stub is on `127.0.0.53`), and neither this nor `ProtectHome=` hides world-accessible sockets under
     `/run` or abstract unix sockets, so the probe checks those paths explicitly;
   - `MemoryMax=`, `TasksMax=` and `CPUQuota=` from the resident budget (open turns build and test, so the figure is
     measured again in O1, not taken from M0's 512 MiB);
   - `RuntimeMaxSec=` as the turn's hard deadline.
   This uses only what the probe found on the node (systemd 255, sudo). bubblewrap is not installed and AppArmor
   restricts unprivileged user namespaces, so bwrap or `unshare` would need a new package and an AppArmor profile;
   an LXD container is the heavier alternative. Both stay listed under decision OD1.
3. **Credentials.** The agent's own provider credential is always readable by the agent's own process, so it is
   readable by its shell. Each open agent therefore gets its own credential in its own home (for claude a separate
   long-lived token, for agy its own login), never the node user's `llm.env` and never a copy of
   `~/.claude/.credentials.json`. A leaked agent credential is revoked and replaced without touching the node user.
4. **Sockets.** The daemon creates each open agent's socket directory owned by that agent's user (daemon access through
   a shared group `gsa-<name>`), so an agent reaches only its own socket. `worker.sock` stays `0700` to the node user.
   Identity still comes from the socket, as before.
5. **Writes stay in the sandbox; results leave as a patch.** A task that names a project gets a git worktree built in
   the agent's home from the hub's W2 snapshot of the pinned commit. The agent may edit, build and test there freely.
   The result leaves the node only as a patch attached to `task_update`; the hub records it as an action that waits for
   manual approval in the existing approvals path. A task has no debate verdict, so the jev gate and `auto:engine` do
   not apply to it; an automatic path for task patches would be its own design (O3 owns this). An agent never pushes, deploys or runs a
   fleet action, and mail from an agent still grants nothing. The standing rule "irreversible work needs a person"
   is unchanged.
6. **Mode as data.** The declaration gains `tools: "closed" | "open"`, default `closed`. `--resident-add` and the relay
   accept `open` only for a node whose inventory reports the sandbox installed and its probe passed
   (`worker_install.sandbox` with version and probe result). A node that loses the sandbox (probe fails, unit
   template missing) marks its open agents `down`; it never falls back to running them as the node user.
7. **Closure layers under `open`.** Layer 1 (isolated configuration) stays: the CLI still loads only what the
   supervisor writes. Layer 2 becomes "the sandbox holds", proven by the probe below instead of a tool list, so codex
   (whose residual tools failed M0's layer 2) becomes eligible under `open` once the probe passes. Layer 3 changes
   meaning: a `!` shell in a tmux composer runs inside the same sandbox, so it is no longer a hole of its own. The
   drivers in `configs/resident-providers.json` are not changed by this document.

## Probe (phase O1)

`scripts/probe-resident.sh --open <node>` runs, as `gsa-probe` inside the unit template, and must show:

| Check | Expected |
|---|---|
| `sudo -n true` | refused |
| read the node user's `llm.env`, `~/.ssh`, `~/.claude` | not visible |
| read another agent's home | not visible |
| connect to the hub's tailnet address, port 22 of another node, the metadata address | refused |
| connect to `worker.sock` or another agent's socket | refused |
| connect to loopback listeners other than the DNS stub | refused or nothing useful |
| connect to world-accessible sockets under `/run` (for example `tailscaled.sock`) | refused or nothing useful |
| connect to abstract unix sockets | refused or nothing useful |
| write in the agent's worktree / outside it | allowed / refused |
| reach the provider API | allowed (per OD2) |
| a mailbox MCP call over its own socket | works |
| fork bomb, memory hog | capped by `TasksMax` / `MemoryMax`, the node stays responsive |

## Phases

- **O1** sandbox only: agent users, unit template, sudo rule, probe script, memory figure for open turns. No agent runs
  open yet.
- **O2** driver: open agents' turns run in the unit; `tools` field, registration and relay checks; per-agent
  credentials; socket ownership.
- **O3** tasks with worktrees: worktree from the snapshot, patch return through `task_update`, patch as a manual-approval
  action on the hub (no `auto:engine`).
- **O4** trial on the hub sandbox and `node-1`: one open claude, one open agy, one open codex; an injected mail that
  asks for `llm.env`, ssh to another host and `sudo` must all fail and be visible in the record.

## Decisions

All six decided by the operator on 2026-11-15, as recommended.

| # | Question | Decision (2026-11-15) |
|---|---|---|
| OD1 | Sandbox mechanism | systemd transient units with a dedicated user (no new package); alternatives: bwrap with an AppArmor profile, an LXD container |
| OD2 | Internet egress from an open agent | open to the internet, private ranges and the tailnet denied; an allowlist proxy later if needed |
| OD3 | Credentials | one credential per open agent in its own home, revocable; never the node user's |
| OD4 | Where writes land | patch recorded on the hub as a manual-approval action; not a push to Gitea, no automatic apply |
| OD5 | Nodes | `node-1` first, others after O4 |
| OD6 | Does "everything" include push, deploy or fleet actions | no: those stay with the hub and a person (standing decision) |

## Risks

| Risk | Handling |
|---|---|
| Mail drives an open shell toward the node's keys or other hosts | Separate user, hidden home, network deny list, probe in O1 and an injection test in O4 |
| The agent's own credential leaks by mail | Per-agent credential (OD3), revocable without touching the node user |
| The sudo rule for starting units becomes a privilege path | The rule names one unit template and fixed arguments; the agent user has no sudo at all |
| An open turn exhausts the node | `MemoryMax`, `TasksMax`, `CPUQuota`, `RuntimeMaxSec`; resident slot accounting unchanged |
| A patch from an open agent carries a harmful change | Manual approval for every task patch; no `auto:engine` path |
| The sandbox is removed or broken by an update | Probe result in the inventory; open agents `down`, never run as the node user |

## 구현 메모

O1 (2026-11-18, deployed to the hub 2026-11-19 as `b3c09d2`; installed on `node-1`): `deploy/sandbox/` (wrapper
`/usr/local/sbin/guardian-sandbox-run`, sudoers rule, `limits.conf`), `scripts/install-sandbox.sh` with
`scripts/lib/sandbox-remote-install.sh`, `scripts/probe-sandbox.sh` with `scripts/lib/sandbox-probe-remote.sh` and
`scripts/lib/sandbox-probe-inner.py`, evaluator `cli/llm-agent/sandbox_probe.py`. Deviations from the design:

- **No unit template file.** The sudo rule allows exactly one root wrapper; the wrapper validates its arguments
  (agent name, limits from `limits.conf`, `--timeout` up to `TIMEOUT_MAX`) and builds the `systemd-run` transient unit
  itself, so the fixed property list lives in one script that the installer version-hashes (`sandbox_version`).
- **Loopback is denied too.** `IPAddressDeny=` also covers `127.0.0.0/8` and `::1`; only the resolver stub
  (`127.0.0.53`, `127.0.0.54`) is allowed. Services listening on the node's loopback (postgres on `node-1`, for one) are
  otherwise reachable from a turn. Denied ranges drop packets, so a denied connect waits for its timeout.
- **Hidden paths.** `ProtectHome=tmpfs` leaves `/root` as an empty listable tmpfs and hides nothing under `/run`, so
  `InaccessiblePaths=` names `/root` and every world-connectable socket the node-1 probe found (tailscale, dbus, snapd,
  lxd, rpcbind, docker, containerd, postgresql, `systemd/notify`, `systemd/io.systemd.ManagedOOM`). The list is not an
  allow list: a new world-connectable socket on a node shows up as a probe failure, so the probe runs before a node is
  allowed open agents and again after updates. Abstract unix sockets cannot be hidden this way; the probe reports a
  connected abstract socket as a warning, not a failure.
- **Inventory key.** The verdict is stored as the entry's top-level `resident_sandbox` (version, limits, verdict,
  failures, memory figure), not under `worker_install`, because `scripts/install-worker.sh` replaces that object.
  The probe script is `scripts/probe-sandbox.sh <node> [--record]`; `scripts/probe-resident.sh --open <node>` hands
  over to it.
- **Probe details.** A stress cap counts only when it was observed (memory hog killed, forks stopped by `TasksMax`).
  TCP targets (hub, a peer, the node's own addresses with every listening port) are checked in parallel with flushed
  output, because more than a hundred dropped connects one after another overran the unit's `RuntimeMaxSec`. The
  probe's ssh keeps the connection alive (`ServerAliveInterval`), since the workload stage is silent for minutes.
- **Workload.** The memory figure comes from the repository's Python test suite run inside the unit like
  `scripts/ci.sh` (CI environment, isolated data dir, `GUARDIAN_LLM_PROVIDERS=none`), shipped from `git archive HEAD`
  with `scripts`, `configs`, `tools`, `deploy` and `apps/agent` because tests read them. `test_exaone_provider` is left
  out: it starts a loopback HTTP server, which the sandbox denies by design. The workload's exit code is shown, not
  judged (timing-sensitive tests can fail under `CPUQuota`).
- **Result on `node-1` (2026-11-18, sandbox `2747b475ef4a`): passed**, 0 failures, 1 warning (the abstract socket
  `@ISCSIADM_ABSTRACT_NAMESPACE` connects; hiding it needs a network namespace). sudo refused; the node user's
  `llm.env`, `.ssh`, `.claude`, `.codex`, `.config`, `worker.sock`, the other agent's home and socket hidden;
  `/etc/sudoers.d` denied; own home and socket directory writable, `/tmp` and `/var/tmp` private; DNS and both
  provider APIs reachable; all 111 TCP targets timed out; no foreign process visible; no user unit or crontab; a
  spawned daemon was gone after the turn; memory hog killed, forks stopped at 63 (`EAGAIN`). Earlier runs found
  `/root` listable and postgres, `systemd/notify` and `ManagedOOM` connectable; those were added to
  `InaccessiblePaths=` before this run.
- **Provisional memory figure for open turns:** the workload (the Python test suite, 307 s) peaked at 157-158 MiB in two runs against
  `MemoryMax=2G`; `limits.conf` keeps 2G, 512 tasks, one CPU and 3600 s until O4 measures real CLI turns that build
  and test. One timing test fails under `CPUQuota` (the workload is not judged).

O2 (2026-11-19, deployed to the hub 2026-11-19 as `b3c09d2`, the relay restart restored both closed agents at epoch 3; nothing installed on a node yet, no agent open): `cli/llm-agent/resident_open.py` (node side:
capability, pre-turn checks, turn command, launcher input, MCP copy), `cli/llm-agent/sandbox_info.py` (the sandbox
version the hub computes), launcher `deploy/sandbox/guardian-sandbox-launch.py`, `--stop` in the wrapper, CLI copies
and the launcher in `scripts/lib/sandbox-remote-install.sh`, credential script `scripts/sandbox-credential.sh`,
`--tools open` on `--resident-add`, relay hold in `mail_relay.py`, open card section. Only claude is to be opened in
O2 (operator, 2026-11-19); the code accepts agy too. Deviations from the design and from the shared contract:

- **Sandbox version.** The version is the first 12 hex characters of sha256 over the wrapper, `limits.conf`, the
  sudoers template and the launcher, in that order. The hub (`sandbox_info.sandbox_version()`) and the installer
  compute the same value. The launcher joined the hash, so the O1 verdict for `2747b475ef4a` is stale. The current
  tree is `9be6d2ccaabc`.
- **Capability travels in the hello.** The node daemon reports `open_tools` and `sandbox_version`. `open_tools` is
  true only when the wrapper and launcher are root-owned regular files and `installed.json` parses. On every agents
  frame the relay checks this against the inventory key `resident_sandbox` and this release's version. An open agent
  on a node without the capability is left out of the frame and goes down with `node_without_open_tools`. On a
  version mismatch the spec is sent with `blocked` and the agent goes down with `sandbox_version_mismatch`.
- **The hub holds a held agent.** Besides leaving it out or blocking it, the relay refuses `bind_begin` and `admit`
  for such an agent with error `agent_held` and the reason as detail. A daemon that kept an old spec cannot rebind
  it.
- **Node checks before every turn.** Before every open turn the driver runs `open_problem()`. It checks, in order:
  `blocked`, the provider, the name, the wrapper and launcher, `installed.json`, the version, the agent and the
  provider's CLI copy in it, the sock directory, and the daemon's group membership. It then runs
  `sudo -n guardian-sandbox-run --stop <agent>` so a unit left over from a lost turn cannot run beside the new one.
  Any failure starts no process and is reported through `agent_exit`. An open spec never falls back to the closed
  path. If the spec of an agent bound in the sandbox turns closed, the turn is refused with `open_spec_changed`,
  which makes the next epoch rebind.
- **Launcher, not a bare CLI.** The turn runs as `sudo -n guardian-sandbox-run --timeout <turn+30> --chdir
  /home/gsa-<name>/work <name> -- /usr/bin/python3 -I /usr/local/lib/guardian-sandbox/launch.py`. The daemon writes
  one JSON object on stdin: provider, version, argv, files, stdin. Inside the unit, the root-owned launcher:
  - checks the input;
  - writes the turn's files (0600, directories 0700);
  - rebuilds the environment from a fixed key list;
  - reads claude's token from `~/.guardian/credentials/claude-oauth-token` into the environment only;
  - execs the root-owned CLI copy.

  The launcher exits 2 for bad input and 3 for `cli_missing` or `no_credential`; the driver reports exit 3 as
  `sandbox_<reason>`. Open claude runs with `--permission-mode bypassPermissions`, the mailbox and repo MCP servers
  from the agent's MCP copy, and `--strict-mcp-config`, with no `--tools` limit.
- **CLI copies.** `scripts/install-sandbox.sh` reads the resident versions from `configs/resident-providers.json`
  and copies `readlink -f ~node/.local/bin/<provider>` to
  `/usr/local/lib/guardian-sandbox/cli/<provider>-<version>/` (root-owned, 0755). Before copying, a
  `timeout 120 ... --version` must print that version. A failed copy is a warning and is left out of
  `installed.json` `cli`. `--no-cli` skips the copies. `--remove` deletes `/usr/local/lib/guardian-sandbox`.
- **Credential.** `scripts/sandbox-credential.sh <node> claude` reads the token from stdin (a TTY is refused) and
  sends it over ssh stdin only, never in argv, the environment, a temp file or a log. On the node the agent user
  writes it with umask 077 and a rename. The script prints only `credential written for <agent>`. `--remove`
  revokes the token on the node; revoking the token at Anthropic stays with the operator.
- **Socket directory owned by the daemon.** The design gave the agent its socket directory. Instead
  `/var/lib/guardian-sandbox/sock/<name>` is owned by the node user, group `gsa-<name>`, mode 2750:
  - the agent can enter it and connect to sockets but cannot create, rename or swap entries, so it cannot plant a
    symlink where the daemon binds;
  - the daemon binds `e<epoch>.sock` with umask 0117 (0660, group from the setgid directory);
  - MCP copies go to `mcp/<worker version>/` (files 0640, directories 2750);
  - cleanup unlinks after an lstat and never follows a link or rmtrees.

  The installer re-chowns an existing O1 directory. The probe's `write.own_sock_dir` is now expected to be denied,
  and its stub listener binds as the node user without `sg`.
- **Group membership needs a restart.** The installer adds the node user to `gsa-<name>`. The daemon only gets the
  new group after `guardian-worker` restarts, so the pre-turn check `sandbox_group` holds the agent until then.
- **Registration.** `--resident-add ... --tools open` requires:
  - claude or agy (codex stays `resident: false`);
  - a name matching `^[a-z][a-z0-9]{0,15}$`;
  - a passed `resident_sandbox` verdict at exactly this release's version;
  - `resident_open_memory_mib` (2048, the O1 `MemoryMax`) in the node's resident budget, new in
    `configs/worker-requirements.json` and in `worker_probe` `REQUIREMENT_KEYS`.

  Closed declarations do not write the key and stay byte-identical.
- **Worker files.** `resident_open.py` is added to `deploy/worker/files.txt` and to the worker installer's list.
- **Card.** An open agent's start card has section 8 "Open tools" and version 4. The closed card stays at version 3
  and byte-identical (`golden/resident_cards.txt`; the open card is pinned by `golden/resident_cards_open.txt`).
- **Probe additions.** The probe has two new checks:
  - a launcher run with valid claude input and no credential must exit 3 (`launcher.exit`, `launcher.result`);
  - `--stop` must end a running `sleep 60` unit within 10 s (`stop.started`, `stop.exit`, `stop.active`).
    `stop.started=0` fails, because nothing was proven.

O3, node half (2026-11-19, deployed to the hub 2026-11-19 as `02d9b26`, the `node-1` worker reinstalled at that commit; the hub half follows below): `task_worktree` in `resident_node.py` (answered on the node like the repo tools,
never forwarded; `not_open`, `bad_input`, `not_owner`, `task_released`, `snapshot_pending`, `no_snapshot`,
`bundle_failed`), the bundle duties in `resident_open.py` (`write_task_bundle` / `remove_task_bundle` /
`task_bundle_path`: `tasks/` 2750, the file 0640, temp name + fsync + rename; the commit is read back from
the bundle header, no git runs for the idempotence check), the ownership check in
`resident_tasks.owned_entry` and the table's drop callback that unlinks the bundle whenever a task leaves
the table (update, release, a `tasks` frame, retirement through `clear_sock_dir`), and the MCP side in
`mailbox_mcp.py` (`--worktrees`: the tool, the clone into `$HOME/work/tasks/<task>` kept between turns, the
collected `git diff --cached --binary --no-renames <base>` with the 256 KiB cap, the worktree removal after
an accepted update or release; the closed tool list and its names stay byte-identical). Deviations and
notes:

- **The same snapshot key never rebundles.** A bundle that already holds the task's commit at its
  `refs/heads/snapshot` header line is returned untouched; a bundle of another commit is rewritten.
- **task_update's frame budget counts the patch.** `_attach_calls` probes the whole frame, patch included,
  so an over-budget frame drops the call log first; the patch is never dropped.
- **A repeated task_worktree never clones over work.** When `$HOME/work/tasks/<task>` exists, the recorded
  base is answered and the clone is left alone; a worktree without a base file is an error the agent can
  fix only by removing the directory and cloning again.
- **An accepted task_update removes the worktree whatever the status** (failed included): the task ended,
  so the next claim starts from a fresh clone. A refused update (bad_patch, patch_too_large, a transport
  error) keeps it.
- **The card.** Open card version 5 adds the worktree flow to section 8; the closed card stays
  byte-identical (`golden/resident_cards.txt` unchanged, `golden/resident_cards_open.txt` regenerated).
- **No allowed-tool list gained the tool.** The open claude turn runs with `bypassPermissions` and no
  `--allowedTools`, and the open agy settings carry no permission list, so `mcp__mailbox__task_worktree`
  had no list to join.

O3, hub half (2026-11-19, deployed 2026-11-19 as `02d9b26`): `task_patch.py` turns the patch into apply_patch edits and
`mail_relay._tool_task_update` records it as ONE action. Details and deviations:

- **The patch is applied against the task's `sha` in the workspace object store**, through a temporary index
  (`GIT_INDEX_FILE` in a 0700 temp dir, `read-tree <sha>`, `git apply --cached`, contents read back with
  `git show :<path>`); the workspace's working tree, HEAD, index and worktree list are never touched. The hub
  keeps no snapshot repository after the upload, so `patch_base` is only checked to be 40 hex and recorded on
  the row; it is never compared with a commit.
- **Refusals name the path**: deletes, renames and copies, mode changes, symlinks, submodules, binary files,
  non-UTF-8 text, a modified path outside `SnapshotEnsurer.allowed_paths(project, sha)`, an added path that
  exists at `sha` (a file the snapshot left out cannot be overwritten), a `.git` or `..` component or a secret
  path (`code_context.is_secret`), and the `patch_ops` limits (`patch_too_large` names the limit). Each
  changed region becomes one edit widened by context lines until `old` is unique at the moment it applies;
  `patch_ops.plan` must reproduce the post-apply content exactly, then the project's `MCPClient` validates
  the edits as `jev_mcp:apply_patch`.
- **Every refusal is answered before `store.task_update`**, so the task stays `in_progress` and the agent
  may fix and update again (`patch_not_allowed` for a closed agent, a task without a project or a status
  other than `completed`).
- **Idempotency is keyed on the task row, not on op-id replay** (a replay answers what was cached before the
  action existed): a row with `patch_action` returns that id; a thread that already holds a PROPOSED
  apply_patch action (a crash between `add_action` and the row update) adopts that orphan. The thread is
  `thread_task_<task id with - as _>`, round 0, `base_sha` = the task's sha.
- **The two-database crash window** (store row written, action not yet created) is closed by
  `reconcile_task_patches`, run once per relay process before the first session: it re-converts the stored
  patch (with the readable-path set of `RepoTools` at the sha, because no snapshot ensurer is warm then, and
  the row's sha standing in for `patch_base`) and creates the missing action; a conversion that now fails is
  logged as `task_patch_failed` and left without an action. `--tasks` shows `action #N` or `action pending`.
- **Store schema 4** adds `tasks.patch` and `tasks.patch_action` (`ALTER TABLE ADD COLUMN` on a schema-3
  store) and the event `task_patch` {task, action, files, bytes}.
- **No auto path, structurally**: `action_ops.is_auto_approvable` returns False and `auto_run_actions` skips
  every action whose thread starts with `thread_task_`, whatever `GUARDIAN_AUTO_APPLY` says (rule 5, OD4).
  `--actions`, `--run-action` and the dashboard actions list need no debate job behind the thread.
- **End-to-end test** `test_relay_e2e.OpenAgentPatchEndToEnd`: an open agent's socket in a temp sandbox,
  `task_worktree` through the real daemon and bundle, a clone, an edit and an added file, a forged `patch`
  argument discarded, the collected diff converted with the real `MCPClient`, exactly one PROPOSED action,
  the worktree and bundle removed, the workspace unchanged.

O1-O3 fixes after the first real open turns on a node (2026-11-19, deployed 2026-11-19 as `bad2572`: hub,
`node-1` worker, sandbox reinstalled (it restarted the node user's manager, as expected for a changed drop-in)
and probe recorded at sandbox version `9be6d2ccaabc`, 0 failures, 1 warning; the O1 probe had run from an
ssh shell, so none of these showed up there; defects 1-4 were first fixed by hand on `node-1` so the
agent could run):

- **Work dir (defect 1).** systemd resolves `--chdir` BEFORE the launcher starts, so a turn failed
  with status 200/CHDIR while `/home/gsa-<name>/work` was missing (the launcher's own ensure_dir is
  too late). `ensure_agent` of `scripts/lib/sandbox-remote-install.sh` now leaves the directory
  behind (agent:agent, 0700, `install -d`; an existing directory is kept, a symlink or a
  non-directory is refused) and the launcher's ensure_dir stays.
- **Worker drop-in (defect 2).** `guardian-worker.service` has `NoNewPrivileges=yes`, which made
  the daemon's setuid `sudo -n guardian-sandbox-run` fail (`sandbox_stop_failed`). The base unit is
  unchanged (closed nodes keep the hardening); `deploy/worker/sandbox-sudo.conf` sets exactly
  `NoNewPrivileges=no` and ships with `scripts/install-sandbox.sh` (it is NOT part of the sandbox
  version hash, which keeps the four files of test_sandbox_wrapper.py). The installer writes it into
  the node user's `~/.config/systemd/user/guardian-worker.service.d/` (dirs 0755 when created, file
  0644, temp name + mv, symlinked components below the home refused); a whole `--remove` deletes it
  (and the `.service.d` directory only when it is then empty), `--remove --agent` keeps it.
- **Stale groups (defect 3).** A running `user@<uid>.service` keeps the supplementary groups it
  started with, so `open_problem` reported `sandbox_group` after `usermod -aG`. At the end of
  `do_install` (and after a whole `--remove`, for the drop-in) the installer reads the manager's
  `Groups:` line and restarts `user@<uid>.service` when a gsa gid is missing or the drop-in was
  newly written or changed — with a warning that every user service of the account restarts
  (enabled ones come back; a running resident turn is lost and redone by the driver). Otherwise it
  only daemon-reloads when the drop-in changed; an inactive user manager needs nothing.
  `--no-restart` (both scripts) skips the restart and prints the exact command.
- **Probe.** `scripts/probe-sandbox.sh` gained the daemon's own view: every gsa agent's work dir
  (exists, directory, agent-owned) and, when `guardian-worker.service` runs, its `NoNewPrivs: 0` and
  every gsa gid of the node user in its `Groups` (`daemon.workdir.*`, `daemon.worker*`; a stopped
  worker is a warning). The probe's cleanup now empties `/home/gsa-probe/work` instead of deleting
  it, because the directory is the installer's.
- **Socket directory group (defect 4).** `install_mcp_copy` and `write_task_bundle` relied on the
  setgid bit of the agent's socket directory to give new entries the group `gsa-<name>`. The daemon
  had once run while it was not yet a member of that group (defect 3); Linux strips `S_ISGID` when a
  non-member calls chmod, so `mcp/` ended as 0750 with the daemon's own group, every later entry
  inherited it and the agent's mailbox and repo MCP servers failed with `CONNECTION_CLOSED`.
  `_ensure_dir` checked only the mode, so nothing repaired it. `resident_open.py` now resolves the
  agent gid (a missing group refuses with `OSError`, which the callers already turn into a refused
  bind or a lost turn), sets the group explicitly on every directory and file it writes (fchown,
  then fchmod, on the same fd; a chown may clear `S_ISGID`, so the mode comes after), re-checks gid
  and mode, and repairs a wrong group on an existing MCP copy or a reused task bundle instead of
  skipping it. The bundle temp file is chowned before the rename, and the installer's drop-in temp
  name is removed before it is written, since the node user owns that directory.
- **Turn budget.** The 12 turns per hour of `mail_store.py` count every admitted turn, lost ones
  included, so the defects above (each turn lost before the CLI ran) spent `coder`'s budget and the
  driver then backed off 600 s at a time until the hour rolled over. Not changed; noted as a
  follow-up (pre-turn refusals could be left out of the budget).
- **Tests.** `cli/llm-agent/test_sandbox_installer.py` locks the drop-in's exact content, the
  work-dir step, `--no-restart` parsing in both scripts, the shipping of the drop-in and that
  `deploy/sandbox` still holds exactly the four hashed files; `test_resident_open.py` covers the
  group repair (directory, MCP file, reused bundle, missing group, a chown that does not take).

Operator steps before an agent runs open (all done 2026-11-19 for a new claude agent `coder@node-1`;
`claude@node-1` and `agy@node-1` stay closed):

1. Deploy the hub.
2. Run `scripts/install-sandbox.sh node-1` (new sandbox version, launcher, claude copy, sock directory ownership),
   then `scripts/probe-sandbox.sh node-1 --record`.
3. Reinstall the `node-1` worker so it carries `resident_open.py` and restarts with the new group.
4. Run `claude setup-token` on a workstation and pipe its output into `scripts/sandbox-credential.sh node-1 claude`.
5. Run `--resident-add <name> --node node-1 --provider claude --tools open` for a new name. To open an agent that is
   already declared, remove it with `--resident-remove` first, because `--resident-add` refuses a name that is
   already declared. `claude@node-1` and `agy@node-1` stay closed until then.

First real task (2026-11-19, on `bad2572`): the operator created task `test: action_reason skips leading blank
lines and truncates` for `coder@node-1` on the `server-guardian` project at `02d9b26`. The first allowed turn (after
the backoff of the turn budget, see above) ran 29 s, ended `normal`, claimed and completed the task and left exactly
one PROPOSED `jev_mcp:apply_patch` action on the hub (thread `thread_task_<task>`, `base_sha` `02d9b26`,
`decided_by` empty: no jev gate, no `auto:engine`). The patch adds one test to `test_task_patch.py` and touches no
other file; applied to a scratch copy of the commit, the module's 24 tests pass. The task worktree and bundle were
gone from `/home/gsa-coder/work/tasks` afterwards. The action waits for the operator's approval; nothing was
approved or applied by the session that ran the trial.

Installer keeps existing agents (2026-11-19, after the `293c446` deploy): the sandbox reinstall of that deploy was
run without `--agent coder`, and `do_install` wrote only `probe`, `probe2` and the names given on that run into
`installed.json`. The `gsa-coder` user stayed, but `open_problem` refused every coder turn as
`sandbox_agent_missing` (epochs 11 to 21) until a reinstall with `--agent coder`. Now the remote installer collects
the existing sandbox agents (users `gsa-*` whose GECOS starts with `guardian sandbox agent`, the same selection the
whole `--remove` uses), adds them to `probe`, `probe2` and every `--agent`, runs `ensure_agent` over the union and
writes the union, and says which existing agents it kept; `--agent` only adds. `--remove --agent NAME` now also
rewrites the `agents` of `installed.json` without NAME, keeping `version`, `installed` and `cli`. The four hashed
files are unchanged, so the sandbox version stays `109bff91ace1`.

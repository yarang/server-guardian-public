# Remote debate agents on worker-capable nodes (design)

English | [한국어](remote-agents.ko.md)

Status: proposed 2026-10-22; scope decided by the operator the same day (answers below). W1 (transport) and W4a (capability probe and installer) are built and deployed to the hub (2026-10-22, `23dfc03`), but no node runs a worker yet and the scripts never ran on a real node; the rest is not built.
Origin: the operator's goal is that the hub can run the debate agents (Expert, Critics, Judge) on nodes that can run them.
This reverses two earlier decisions, on purpose: "Other fleet hosts run no agent" (CLAUDE.md, topology) and "no task
queue or fleet workers" (standing decisions), and it lifts the non-goal "physical distribution" of
`docs/design/multi-perspective.md`. The reversal is limited to nodes that pass a capability probe and to one small worker (see "Scope"). The operator's refinement of the same day: Tier-1 was an environmental exception, so the line is drawn between nodes that **can** run an agent and nodes that **cannot**, not by tier.

## Decisions (operator, 2026-10-22)

| Question | Decision |
|---|---|
| Why | Spread the hub's load (2 OCPU, 12 GB), spread subscription limits across nodes, more diverse viewpoints (different nodes and architectures), and check a node from inside it |
| How it runs | A **resident worker** on each node that is capable and opted in (not one SSH run per call) |
| Code access | A **read-only snapshot** is sent to the node for the turn |
| Placement | The **team JSON** names an execution host per role; no `host` means the hub, as today |

## Scope and what stays true

- **Capable, not tiered.** A node can run a worker when it passes the capability probe (below); `tier` is only a
  description of the hardware and decides nothing. Today's Tier-1 nodes are expected to pass; a Tier-2 node passes only if
  it really meets the conditions (in practice its 1 GB memory fails the probe), and a Tier-1 node without a working CLI
  fails. Two inventory facts exist: `worker_capable` (the last probe passed, with the probe's date and reasons) and
  `worker: true` (the operator installed and enabled the worker). Only `worker: true` nodes receive work.

### Capability probe

`scripts/probe-worker.sh <host>` (read-only, over the fleet key) reports pass or fail with a reason per condition, and
writes the result into the inventory entry on the hub. Conditions: memory available for the configured
`GUARDIAN_WORKER_MAX_JOBS` (default needs 4 GiB total and 1.5 GiB free per job), `python3` at the engine's minimum
version, at least one LLM CLI that answers the one-line PONG test of `deploy-llm-env.sh` with the node's own
`llm.env`, free disk for snapshots (1 GiB), and a reachable fleet path from the hub. The thresholds live in one place
(`configs/worker-requirements.json`) so the operator can change them without code. The probe never installs anything
and never touches credentials; a failed node is simply not eligible, and the reason is shown in Settings.
- The hub stays the only place with the decision DB, the jev gate, the fleet key, `AGENT_TOKEN` and the engine. The
  worker is a **model runner**: it receives a prompt and returns text. It decides nothing, stores no debate state, and
  cannot start a debate, apply an action or reach another node.
- The worker is Python standard library only, like the engine. No queue, no broker, no database on the node.
- `scripts/install-agent-service.sh` still refuses every node but the hub. The worker has its own installer
  (`scripts/install-worker.sh`, W4a).
- Evidence and citation checks, the jev gate and the event contract do not change. They run on the hub over what the
  worker returns.

## Architecture

```
hub (engine)                                        worker node (resident worker)
------------                                        ------------------------------
debate_engine ──> LLMRunner.run_session_prompt
                    │ host set on the role?
                    │   no  → local CLI (today)
                    │   yes → RemoteRunner ── ssh (fleet key) ──> worker client ──unix socket──> worker daemon
                    │                                                                             │ runs claude/codex/agy
                    │         <── result: text, session id, provider, MCP call log ───────────────┘ with the node's own
                    └ failure → fall back to the hub's providers (recorded in `failures`)                llm.env
```

One integration point: `LLMRunner.run_session_prompt` (`llm_runner.py`) gets an optional `host` argument, and the
engine passes the role's execution host from `_call_llm` (`debate_engine.py`, the only caller). Everything after that call (event
emission, session storage, evidence) is unchanged.

### Transport and authentication

The worker listens on a **unix socket** (`~/.guardian/worker/worker.sock`, mode 0600), never on a TCP port. The hub
reaches it through the existing fleet SSH path: `ssh <host> python3 ~/.guardian/worker/worker.py client`, which relays
one JSON request from stdin to the socket and the reply back to stdout. Consequences:

- "Nodes accept only the hub" is kept as it is today: the fleet key's `from=` restriction is the only door. No new
  port, no new token, nothing for `AGENT_TOKEN` to be confused with. The worker never receives `AGENT_TOKEN`, jev keys
  or any hub-only key.
- Why a resident daemon at all, if every call goes through SSH: the daemon owns the job table. A job has an id; the hub
  can cancel it by id with a second SSH call (so killing a hub job kills the remote CLI process tree), an SSH drop does
  not leave an orphan running (the daemon enforces the timeout), and per-node concurrency is enforced in one place
  (`GUARDIAN_WORKER_MAX_JOBS`, default 2).
- The request and reply are framed JSON with a protocol version. `ping` returns the protocol version and the worker's
  code version; the hub refuses a worker whose protocol it does not speak and reports it (no silent downgrade).

### What a request carries

The same arguments the local call takes: prompt, context, provider order (`providers`), `session_id`, `system`,
`instructions` (the standing text), `plain_prompt`, and the repo tool spec (below). It never carries credentials. The
reply carries `LLMResult` (provider, text, session id, `resumed`, `failures`) plus the MCP call log.

### Credentials

The worker reads the node's own `~/.guardian/llm.env`, which `scripts/deploy-llm-env.sh` already puts on nodes that run LLM CLIs
(mode 0600, without hub-only keys). Provider environments are built by the same `provider_env` rules (a claude run
sees only claude's variables). `~/.claude/.credentials.json` and `~/.codex/auth.json` are never copied; a node that
needs a token gets it through `deploy-llm-env.sh`. Nothing in this design sends a credential from the hub per call.

### Sessions

CLI sessions live on the disk of the execution host that ran them, so a session is **pinned to its execution host**:

- `agent_sessions` (thread, role) gets a `host` column (migration, default `''` = hub). The session is resumed only on
  the same execution host.
- A role whose `host` changed, or whose execution host is unreachable, starts a **fresh** session instead of failing. The loss is
  stated: `failures` gets `"<host>: unreachable, session not resumed"`, and the thread digest
  (`thread_digest.py`) carries the earlier rounds as it does for any fresh session.
- `agent_started` events gain `host`. The dashboard already shows `debate_started.host`; the per-agent execution host is shown
  next to each agent.

### Placement in the team JSON

A role (`expert`, each `critic`, `judge`, each `proposer`) may carry `"host"`. Real host names never enter git
(CLAUDE.md), so:

- Builtin team files in `configs/teams/` use a **label**: `"host": "worker:any"` (any `worker: true` node) or
  `"worker:other"` (a worker node other than the one the Expert uses, for viewpoint diversity).
- Custom teams (hub data, not in git) may use an **inventory id** directly.
- `any`/`other` are resolved once, at the first turn of the thread, over the nodes whose inventory entry has
  `"worker": true` and whose `ping` succeeds, ordered by free job slots and then id; the choice is stored in
  `agent_sessions.host` and never changes within the thread. A label with no usable node resolves to the hub (recorded).
- Lineup validation (`team_manager.py`) checks that a named node exists and has `worker: true` (a capable node that was never enabled is refused with that reason).

### Repository access: read-only snapshot

The repo tools (`RepoTools`) work over a git repository at a pinned sha (`git ls-tree`, `cat-file`, `grep`). For a
remote role with `code_tools: "mcp"` the hub sends a **snapshot** and the worker runs `repo_mcp.py` on it:

1. The hub builds the snapshot from the pinned sha with `git archive`, **dropping every path `is_secret` matches**
   (every path component, as everywhere else), symlinks and binaries above the usual limit. The filter runs on the
   hub; the worker is never trusted to filter.
2. The worker unpacks it into a private temp directory (0700), runs `git init` and one commit with a fixed identity and
   date, and points `repo_mcp.py` at it. The snapshot commit id differs from the hub's sha; the prompt keeps the hub's
   sha as the label (`_mcp_text`), and the log below never contains a sha.
3. A snapshot is cached on the worker by its content hash for the thread's lifetime and deleted when the thread ends
   or after 24 h; nothing outlives the worker's data directory.
4. **The call log returns to the hub.** `repo_mcp.py` writes `calls.jsonl` (op, paths, line spans, mentions; never
   contents). The citation rule ("a strong citation passes only if those lines were shown to the session") depends on
   it, so the worker returns the new log records with every reply and the hub appends them to the turn's `McpTurn` log.
   Without this every strong citation of a remote agent would fail. Because the snapshot holds the same blobs as the
   hub's sha for every non-secret path, spans mean the same lines on both sides.
5. Limits (`steps`, `step_bytes`, `turn_bytes`) are counted from the same log, as for a local turn.

The symbol index (`repo_defs`, `repo_refs`, `repo_callers`, `repo_callees`) and the Go facts need the hub's cache and
the Go toolchain. In W2 a remote role gets `repo_list`, `repo_grep`, `repo_read`, `repo_outline`, `repo_map`; the
index-backed tools return "not available on this host" until W2b ships the index inside the snapshot.

### Checks from inside the node

The goal "check a node from within it" does **not** give the CLI a shell: `--tools ""` for claude stays, and codex and
agy stay as they are. A remote role runs the same typed, read-only ```ops block catalog (`ops_checks.py`) as today, but
the worker executes the command **locally** instead of over SSH, and returns the result. The hub still writes the audit
line (`source=ops_check`), still verifies the citations, and the catalog stays the only thing a debate agent can run. The
engine mediates it exactly as now; only the executor changes (W3).

### Cancellation, timeout, failure

- A hub job kill (`cancel` on the job) sends `cancel <job_id>` to the worker, which kills the CLI process group.
- The timeout is the runner's (`GUARDIAN_LLM_TIMEOUT`); the daemon enforces it too, so a dead hub cannot leave a run.
- An unreachable node, a refused protocol or a failed `ping` falls back to the hub's providers for that call. The
  fallback goes through `failures` and `provider_failed` events like any provider fallback, and a round with a fallback
  answer is already `UNVERIFIED`; the doc states the side effect plainly: continuity of the remote session is lost.
- A worker that is busy (`max_jobs` reached) makes the call wait up to `GUARDIAN_WORKER_QUEUE_SECONDS` (default 60),
  then fall back. There is no queue in the worker.

### Dashboard and API

The team editor gets a `host` field per role (strings in both `ko` and `en`); Settings shows each node's capability result and, for enabled nodes, its worker
state (version, protocol, running jobs) from the hub's `ping`. The hub agent exposes it read-only; it does not talk to
the worker itself (the Python CLI does, as it spawns the debates).

## Risks and how they are handled

| Risk | Handling |
|---|---|
| Secret code reaches a node | Hub-side `is_secret` filter on the snapshot; snapshot deleted after the thread; only nodes that passed the probe and were enabled |
| The worker becomes a second control plane | It has no state, no credentials of the hub, no outbound reach; the only door is the fleet key |
| Remote citations stop verifying | The MCP call log travels back with every reply (W2) |
| Same model, same blind spots on a different node | Placement diversifies the execution host, not the model; lineups keep varying `providers` (see `multi-perspective.md`) |
| A node runs out of memory (2 parallel CLIs on a node that also serves the operator) | `GUARDIAN_WORKER_MAX_JOBS`, wait then fall back |
| Code drift between hub and worker | Protocol version in `ping`; `install-worker.sh` copies the files of one release; a mismatch is refused, not tolerated |
| Hub secrets in the worker's environment | The worker is started with a fixed minimal environment plus `llm.env`; hub-only keys are filtered by `deploy-llm-env.sh` already |

## Phases

| Phase | Content | Done when |
|---|---|---|
| W1 | `remote_agents.py` (hub client) and `worker.py` (daemon, client relay, job table, cancel), `host` on `run_session_prompt`, `agent_sessions.host`, `host` on `agent_started`, fallback; roles **without** repository access (ops critics, design review) | A fake-worker test passes; one debate on the hub sandbox runs a Critic on a worker node |
| W2 | Snapshot, remote `repo_mcp`, call log return, the five non-index tools | A seeded-defect task gives the same citation outcome local and remote |
| W2b | Symbol index inside the snapshot | index tools work remotely (only if W2 shows they matter) |
| W3 | Local ops checks in the worker | ops audit lines and citations equal the SSH path |
| W4a | `probe-worker.sh` (`configs/worker-requirements.json`, `worker_probe.py`), `install-worker.sh` (systemd user unit, files from a git rev, acceptance ping through the hub path, `worker: true` in the inventory, `--disable`, `--uninstall`) | A capable node is enabled with one command and rolled back with another; a failed check leaves it unflagged |
| W4b | Team editor `host`, Settings worker state, a read-only API, `ko`/`en` strings | The operator can place a role and see the worker state without editing JSON |

Each phase ends deployable; with no `host` set anywhere, behaviour is exactly today's.

## Tests

- A fake worker (a socket server in the test process) covers: protocol mismatch, cancel, timeout, busy, unreachable,
  session pinning and the execution-host migration. `testenv.isolate_data_dir()` in every test; the real data dir is never touched.
- Seeded-defect check: the same planted-defect task is answered by a local and a remote Expert; the citation results
  and the retrieval events must match (W2).
- Trial debates run in `~/guardian-eval` only and need the operator's OK first, because the Codex quota is shared with
  production.
- CI: `scripts/ci.sh python`, `config` (team schema), `web` for the editor; `redact-private.py --check` must find nothing.

## Open points

1. Whether `worker:other` should also prefer a different **architecture** or provider account (all candidate nodes are ARM64
   today, so it only separates machines).
2. Whether a remote Judge is wanted at all: the Judge holds the jev gate input, and a lost Judge session costs more than
   a lost Critic session. Proposal: allow it, but default builtin teams to Critics and proposers.
3. Subscription limits are per account, not per node, when nodes share one token (`deploy-llm-env.sh` pushes one file);
   spreading the limit needs separate tokens per node, an operator decision outside this code.

## Implementation notes

### W1 (transport and session pinning, 2026-10-22)

Built: `cli/llm-agent/remote_agents.py` (placement syntax, `RemoteRouter`, `SshTransport`), `worker.py` (`serve`, `client`,
`exec`), `Member.host`, `agent_sessions.host`, engine placement (`DebateEngine._place`, host routing in `_call`).
Tests: `test_remote_agents.py` (scripted transport, in-process daemon on a unix socket with a fake job command, engine
placement with a replaced hub runner). Deviations and details worth knowing:

- The hub starts `ssh ... worker.py client` itself and writes the request to its stdin. It does not go through
  `/api/exec/remote`, so a prompt never reaches an argv, the audit log or the command API.
- A job is a child process in its own process group; a cancel, the job timeout and a hub that hangs up (checked with
  `MSG_PEEK` on the request socket) all end in `killpg`. A cancel that lands between two polls is reported as
  `cancelled`, not as a failed job (found by the daemon tests).
- Every `WorkerError` (unreachable, protocol mismatch, busy, timeout, not an enabled worker) runs the call on the hub in a
  new session; `failures` gets `<host>: <reason>; the call ran on the hub` and `LLMResult.host` is `''`. Unreachable and
  mismatching nodes are negative-cached for 60 s, a busy worker is not (pings are cached for 30 s).
- A resumed worker session that has to fall back runs on the hub in a new session, which has not seen the thread. The
  engine therefore hands the router the full turn (history included, language rule applied as in `_call`) as `replay`;
  `_fallback` uses it, so the hub makes one call, not two. If the worker itself starts a new session instead
  (`resumed` false), `_ask_turn` repeats the turn with the history, as it does for a first turn.
- SSH host keys: the hub agent adds `StrictHostKeyChecking=accept-new` for unpinned nodes, `SshTransport` does not. A node
  whose key the hub has never seen therefore falls back to the hub instead of being trusted on first use. Connect once
  by hand (or pin the key) before using a node as a worker.
- Labels are resolved once per thread and then follow the stored session, also when that session moved to the hub, so a
  label never hops between nodes mid-thread. An explicit inventory id is a requirement and retries the remote with a
  fresh session.
- `host` together with `code_tools: "mcp"` is refused at team validation, and `_ask` ignores the `host` for such a member:
  the repository tools stay on the hub until W2.
- Inventory validation (`inventory_host_error`) only runs when `servers.json` loads; it is git-ignored and absent in CI.
- Open for W1's "done when": the trial debate with a Critic on a real worker node. It needs W4's installer and the operator's OK (shared Codex quota).
  It counts as passed only if `agent_started.host` is set for that member **and** its `failures` hold no `the call ran on
  the hub` entry; a debate that silently fell back to the hub looks healthy otherwise.
- Not built yet: the install script, the capability probe, the `worker` flag in the node form, the team editor field and
  Settings view (W4), snapshots (W2) and local ops checks (W3). No team sets `host`, so nothing changes until one does.
- On this Mac `test_retrieval_loop`, `test_go_facts` and one `test_team_manager` test fail when run without
  `GUARDIAN_LLM_PROVIDERS=none` (CI sets it) or outside a login shell with Go; they fail the same way on a clean checkout
  of `947046b`, so they are not caused by W1.

### W4a (capability probe and installer, 2026-10-22)

Built: `configs/worker-requirements.json`, `cli/llm-agent/worker_probe.py` (verdict logic and the inventory edits),
`scripts/probe-worker.sh`, `scripts/install-worker.sh`, `scripts/lib/{worker-facts,worker-common,worker-remote-install,llm-check}.sh`,
`deploy/worker/{files.txt,guardian-worker.service}`. Tests: `test_worker_probe.py` (verdicts, inventory edits),
`test_worker_install.py` (the file set is closed), `scripts/test_worker_scripts.py` (both scripts end to end against a
fake fleet: fake `ssh`, `systemctl --user`, `loginctl`; the real worker as the daemon). Deviations and details worth knowing:

- **Two flags, two writers.** The probe writes `worker_capable` (`passed`, `checked`, `max_jobs`, `clis`, `reasons`); a
  failing probe also sets `worker: false`. Only the installer sets `worker: true`, together with `worker_install` (`rev`,
  `installed`, `max_jobs`, `version`), and only after a passing probe **and** a passing acceptance check. `--disable`
  turns `worker` off and drops `worker_install`. Both go through one `flock` on `~/.ssh/guardian_fleet.conf.lock` (the
  Go agent's inventory lock) and leave a `.bak`; the hub is refused.
- **The edits run on the hub** (`worker_probe.py` is piped to `python3 -` over ssh), so a hub release is not needed
  to enable a node. `HUB_INVENTORY_PATH` points both scripts at another inventory, e.g. the one of `~/guardian-eval`,
  so a trial does not mark production nodes.
- **User unit and linger.** The CLI logins live in the account's home, so the worker is a systemd *user* unit and needs
  `loginctl enable-linger`. The installer tries it (then `sudo -n`); without it the files stay but the node is **not**
  flagged and the script prints the one command to run. `deploy/hub/install.sh` copies an explicit unit list and does not
  pick up `deploy/worker/`.
- **Install from a git revision, not the working tree** (`--rev`, default HEAD), flat into `~/.guardian/worker/`, with a
  `REVISION` file. The previous files are copied to `previous/`; `restore` puts them back. A first install that fails
  its check is stopped (`systemctl --user disable --now`); a failed update restores the previous version and keeps the
  flag it had.
- **Acceptance check = the hub's own path**: `hub -> ssh -> worker.py client -> socket`, up to 8 tries, requiring
  `ok`, the protocol, `version` equal to the staged files' `code_version`, and a non-empty `providers`. Residual gap:
  the ping does not exercise the environment of a job that the daemon spawns (PATH of the unit vs. the CLI's own needs);
  the first trial debate does.
- **Found by the end-to-end test:** `ping` re-hashed the files on every request, so an old process whose restart never
  happened reported the *new* version and passed the check. The worker now reports the version it loaded at start
  (`STARTED_VERSION`).
- **The file set is closed** (`deploy/worker/files.txt`: `worker.py`, `remote_agents.py`, `llm_runner.py`,
  `env_policy.py`). `group_manager` (hub only) and `repo_mcp` (W2) are imported lazily and are not on the node;
  `test_worker_install.py` fails when a new repository import is not on the list. W2 must add `repo_mcp.py` (and what it
  imports) to the list.
- The probe's thresholds: `min_total_memory_mib` 4096, `free_memory_mib_per_job` 1536, `default_max_jobs` 2,
  `python_min` 3.9, `min_free_disk_mib` 1024, `min_working_cli` 1. A fact the node did not report fails its condition.
  An unreachable hub -> node path fails the probe's `hub_path` condition (a verdict), an unreachable node is a probe
  failure (exit 1, no verdict, nothing recorded).
- Verified against the fake fleet first. First real run (2026-10-22, operator: "install the worker on the Tier-1
  hosts"): the probe passed on all six Tier-1 nodes other than the hub (11.9 GB RAM, python 3.12 or 3.14, claude, codex
  and agy answered PONG, 25-39 GB free), `max_jobs` 2; the installer ran from revision `3aae65efebb8` and every
  acceptance ping passed on the first try (version `de01a295ab28`, 2/2 free slots). The one thing the ping cannot show
  was checked by hand on one node: a real `run` request through hub -> ssh -> `worker.py client` returned `PONG` from
  `claude-cli`. The macOS `tar` warnings about `LIBARCHIVE.xattr` headers during the upload are harmless. The W1 trial
  debate is described below; no production team has a `host`, so no production debate uses the workers.
- W4b is not dropped. Check first whether the dashboard's team editor round-trips members it does not model, or `host`
  is stripped on save. **Checked 2026-10-27: it was stripped.** `TeamManager.save` replaces the file with the payload, and
  the editor sent back only the keys it models, so saving a team in the dashboard also dropped `host`, and the team-level
  `flow` and `sides`. Fixed in the editor (`Member.extra`, `teamExtras`, `Draft.extra`): unmodelled member and team keys are
  carried through unchanged and written back (`debate.lib.test.ts`, `settings.test.tsx`). The `host` field itself was added to the editor
  by W4b (below).

### W2 (repository snapshot and remote `repo_mcp`, 2026-10-27)

- Built: `repo_snapshot.py`, protocol 2 (`put-snapshot`, `snapshot_has`, the `repo` field of `run`, `calls` in the reply),
  `repo_mcp.py --no-index` serving the five `SNAPSHOT_TOOLS`, the engine's remote wording, `test_remote_repo.py`
  (44 tests) and `SeededDefectTests` (same `citation_check` outcome local and remote for a read and an unread cited
  file). The team refusal of `host` together with `code_tools: "mcp"` is gone.
- **Deviations from the design text above.**
  - The snapshot is not a `git archive` unpacked and committed on the worker: the hub sends a deterministic
    `git fast-import` stream (zlib, sent over the ssh mode `put-snapshot KEY`) and the worker imports it into a bare
    repo under `snapshots/KEY`. The key is a hash of (mode, blob id, path), so a changed file set is a new snapshot. The
    worker never gets a path or a command from the hub: the `repo` field names a key, the daemon builds the MCP spec.
  - Files over 2 MiB and anything beyond 64 MiB in total are dropped, so a remote `repo_list` can be shorter than the
    hub's. Dropped files also lose their evidence: the router filters the returned `paths` and `spans` to the files of
    the snapshot, so a worker cannot make a file count as "shown" that it did not hold.
  - Cleanup is TTL/LRU, not "when the thread ends": `GUARDIAN_WORKER_SNAPSHOT_TTL` (24 h) and `_KEEP` (8) on use.
  - The remote map has no Go facts, and a remote role has no `repo_defs`, `repo_refs`, `repo_callers`, `repo_callees`
    or wiki tools at all (the prompt says so) instead of tools that answer "not available". W2b is not built.
  - The call log returns only when the job ends (capped at 8 MiB, over the cap is an error), so a remote debate agent's
    retrieval events appear at the end of its turn, not live. Calls lost when a worker run fails are not counted when
    the router falls back to the hub, which starts a fresh session.
  - Protocol 1 -> 2, the worker file set grew from four to sixteen files (`deploy/worker/files.txt`), and **git is now
    a probe condition** (`git_version` fact, condition `git`). The worker installer's `CODE` list must equal the file set
    (`test_worker_install.py` checks it; the first version of this change missed it, which the install test found).
    A rollback from a protocol 1 node restores a backup of only the old four files, so `restore` verifies the files the
    backup holds (at least `worker.py` and `remote_agents.py`) instead of the whole set; leftover newer files are
    harmless to the old code.
- **Rollout.** The six installed workers run protocol 1. A hub with W2 refuses every call to them (protocol mismatch ->
  fallback to the hub, with a line in the failures); run `scripts/install-worker.sh` again on each, after `scripts/probe-worker.sh --record` shows
  the new `git` condition.
  **Done 2026-10-27** for the six Tier-1 workers (probe recorded with `git`, installed from `HEAD`, protocol 2 and git
  confirmed by the `--workers` report). The hub was deployed 2026-10-28 as `d84562c` and now speaks protocol 2; no worker call at protocol 2 has been observed yet.

### W4b (team editor `host`, worker state, 2026-10-27)

- Built: `remote_agents.worker_report()` / `worker_choices()`, `guardian.py --workers`, the Go route
  `GET /api/workers` (read-only, the same `configGet` pass-through as `/api/teams`, 30 s CLI timeout), the team
  catalog fields `host_labels` and `workers` (enabled worker ids, read from the inventory without network), the `host`
  select in the team editor, the `host` in the team list, and Settings -> Workers.
- The hub agent never talks to a worker. `--workers` pings every enabled node in parallel (10 s each, fresh, no cache) and
  returns one row per inventory node that is enabled or has a probe record: `state` `up`, `down` (with the error) or `off`
  (not pinged); for `up` also the code version and whether it is the one the hub would install now, the protocol, free
  slots, providers and git. An unreadable inventory is reported in the payload (`error`), not as an HTTP failure.
- `Member.host` is a first-class field of the editor (it was carried in `extra` before). The select offers the hub (no
  `host`), the two placement labels and the enabled workers; the field is left out when no worker is enabled and
  the member has no stored `host` (the labels mean nothing without a worker). A stored value the hub no longer offers stays selectable, flagged, so saving never
  drops it silently.
- Deviation: the design listed "a read-only API" only. Placement validation is unchanged (`host_syntax_error` on save,
  `inventory_host_error` at run time); the editor does not duplicate it.
- **W3 is not built.** The audit line (`source=ops_check`) is written by the hub agent as a side effect of exec, so a check
  run locally by the worker would lose it unless the hub agent gets a record-only audit endpoint, which any token holder
  could use to forge entries. The engine already runs every ops check on the hub for remote roles too, so W3's "done
  when" holds today and local execution would save one SSH round trip only. Recommendation: drop W3 unless that round
  trip proves to matter; the decision is the operator's.

### W1 trial debate (2026-10-22)

- Setup: hub sandbox `~/guardian-eval/data-w1` (own `GUARDIAN_DATA_DIR`, no production data), the hub release code, team
  `guardian-dev-w1` = `guardian-dev` with `max_rounds` 1 and three Critics placed differently: security (providers codex,
  agy) on `host: "worker:any"`, QA (claude) on `host: "worker:other"`, release (agy, codex) on the hub. Task: review of
  `TeamManager.save` and `validate_team_meta`. The operator approved the use of the shared Codex quota.
- Result: `APPROVED` in round 1, jev gate `PASSED`, two actions proposed, 8 min 22 s in all. The security Critic ran on
  worker node A through `codex-cli` (63 s), the QA Critic on worker node B through `claude-cli` (204 s); `agent_started`
  carries `host` for both and for neither of the hub-side debate agents. The release Critic ran on the hub (`agy-cli`, 221 s),
  the Expert and the Judge on the hub (`claude-cli`, 169 s and 111 s). The two worker Critics ran in parallel with the
  hub Critic, and no round, debate agent or event reported a fallback to the hub or to canned text.
- What it shows: `host` placement (`worker:any`, `worker:other`), the hub -> ssh -> worker path with real model CLIs, and
  the result flowing back into the shared Judge close. What it does not show: a fallback path on a real node (covered
  only by the fake-fleet tests), a worker that is busy (`max_jobs` 2 was never reached) and any quality difference
  between hub and worker runs (one debate).

### Snapshot path check (2026-10-30)

`import_stream` relied on `git fast-import` to refuse a tree path that leaves the tree. git 2.43.0 (the default of
Ubuntu 24.04) stores `../escape.txt` as it is, so `test_remote_repo.HostileUploadTests` failed there, and a worker with
that git had no such check; the probe asks for git but not for a version. The worker now lists the imported tree
(`git ls-tree -r -z --name-only`) and refuses the upload, leaving nothing behind, when a path has an empty, `.` or `..`
component or a `.git` component in any case (`repo_snapshot.unsafe_path`). Nothing escaped before either: the snapshot
is a bare repository that is never checked out, and `RepoTools` reads git objects and refuses `..` in a requested path.
A hub-built snapshot never holds such a path. Workers get the check only when they are installed again
(`install-worker.sh`); the stream format and `SNAPSHOT_FORMAT` are unchanged.

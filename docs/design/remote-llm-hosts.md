# Remote LLM hosts: agent turns on Tier-1 hosts (design)

English | [한국어](remote-llm-hosts.ko.md)

Status: proposed 2026-10-30. Three decisions made by the operator (see "Decisions"). R0 (measurement) decides whether R1
is built at all. R0 done: 7 days on the hub (2026-10-30 to 2026-11-06), verdict `not-needed`, so R1-R3 are not built
(`../analysis/2026-11-08-r0-llm-load/results.md`).
Origin: a discussion of where the hub keeps project source code. Moving the code to another server and reading it over
SSH was rejected (every `git cat-file` of a debate would cross the network, Go facts need a local worktree and
toolchain, actions and tests write to the workspace). The question that remained: can the **agents** run elsewhere?
This document answers it for load distribution. Source code storage on the hub is a separate topic.

## Problem

Every debate agent (Expert, proposers, Critics, Judge) is an LLM CLI subprocess of the engine on the hub
(`llm_runner.py`; `agent.md`, chapter 1). The hub is a 2 OCPU, 12 GB ARM host that also runs Gitea, the Go agent, the
dashboard, the timers and the fleet SSH. Tier-1 hosts (12 GB ARM, `"role": "llm_cli_host"`) already have claude, codex
and agy installed and logged in, but only the operator uses them.

What the hub runs per agent turn is mostly the CLI process (a Node or Rust process of a few hundred MB) waiting on the
model; the engine's own work (retrieval through `repo_tools`, code index, evidence checks, DB) is small and has to stay
next to the code.

The numbers so far do not show a saturated hub: the P0 sample of `multi-perspective.md` (last 10 debates: agent run
median 95 s, max 374 s) and its evaluation (peak memory of the widest config under 1 GB). `multi-perspective.md`
("Non-goals") lists exactly when physical distribution is to be reconsidered: when the hub is saturated, or when a
separate account or quota is needed. R0 measures the first; the second is open for later (decision 3).

## Decisions (made 2026-10-30, operator)

1. **Goal: spread the hub's load.** Not viewpoint diversity (that is `multi-perspective.md`, logical separation) and not
   disk space.
2. **The standing decision "no fleet workers" (CLAUDE.md) is relaxed for LLM CLI turns only.** The engine, retrieval,
   the MCP server, evidence and citation checks, jev, the DB, actions, publishing and ops checks stay on the hub.
3. **Accounts:** Tier-1 hosts run the same account as the hub today; another account on a host is allowed when needed.
   Credentials are never copied between hosts (CLAUDE.md): every host logs in by itself.

## Proposals (not yet decided)

- P-1 **Code never leaves the hub.** A remote turn gets its prompt and nothing else; code reaches the model only through
  repo blocks or the MCP bridge, both served on the hub, so the call log and the line-span rule work unchanged.
- P-2 **Off by default, opt-in per host.** `GUARDIAN_LLM_REMOTE=on` on the hub and `llm_slots` > 0 on a host.
- P-3 **The hub stays in the pool and is the fallback.** A remote failure never fails a turn that the hub could run.
- P-4 **A session stays on the host that created it** (claude and codex keep sessions per host).
- P-5 **No daemon on Tier-1 hosts.** One deployed stdlib script that the hub runs over SSH; it listens on nothing.

## What runs where

| Part | Today | With remote hosts |
|---|---|---|
| Engine, round loop, events, DB | hub | hub |
| `repo_tools`, code index, map, Go facts, wiki | hub | hub |
| ```repo blocks (engine reads, next turn shows) | hub | hub (unchanged: the CLI never needs the code) |
| `repo_mcp.py` (claude/codex MCP, `GUARDIAN_REPO_MCP=1`) | child of the CLI on the hub | still on the hub, reached through an SSH socket forward (R2) |
| LLM CLI process (claude, codex, agy) | hub | a Tier-1 host or the hub |
| Session files (`~/.claude/projects`, `~/.codex/sessions`) | hub | the host that ran the session |
| Actions, tests, publish, ops checks, jev | hub | hub |

## Design

### Host pool

Hosts come from the hub inventory (`~/.guardian/data/servers.json`): `"role": "llm_cli_host"`, `cli_support` lists the
providers, and two new optional fields:

- `llm_slots` (integer, default 0 = not used): how many Guardian turns may run there at once. 2 is a sane start for a
  12 GB host.
- `llm_account` (free label, e.g. `acct-a`; never an e-mail address): hosts with the same label share one account.

The hub's own share is `GUARDIAN_LLM_HUB_SLOTS` (default: `GUARDIAN_MAX_PARALLEL_AGENTS`). An account-wide cap
`GUARDIAN_LLM_ACCOUNT_SLOTS` (default unset) bounds concurrent turns per `llm_account`, so spreading over more hosts does
not just hit the same account's rate limit harder.

### Placement

- Unit: one agent turn. A turn that resumes a session goes to the session's host (P-4); a fresh session goes to the
  host with the most free slots among those that list the provider and pass the probe; ties prefer the hub (no network).
- Slots are cross-process: debates run as separate `guardian.py` processes, so a slot is an `flock` on
  `<GUARDIAN_DATA_DIR>/llm-hosts/<host>/slot-<n>.lock` (same pattern as the code wiki and audit locks). A crashed process
  releases its lock.
- Probe: `guardian-llm-exec probe` returns the CLI versions, load average and `MemAvailable`. Cached for 5 minutes; a
  failed probe or a failed connect excludes the host for 10 minutes. A host whose claude is older than the version the
  runner needs (`CLAUDE_RECORDED_PROMPT_VERSION`) is not used for claude.

### Remote execution: `guardian-llm-exec`

A stdlib Python script installed on each Tier-1 host (by `scripts/deploy-llm-skills.sh` or a sibling script) and run by
the hub as `ssh -o BatchMode=yes -o ServerAliveInterval=15 <host> python3 ~/.guardian-llm/guardian-llm-exec run`.
It reads one JSON envelope on stdin:

```json
{"argv": ["claude", "--print", "..."], "env": {"API_TIMEOUT_MS": "..."}, "workdir": "~/.guardian/llm-sessions",
 "files": {"system.txt": "..."}, "stdin": "...", "outputs": ["last_message.txt"], "timeout": 600}
```

- `files` are written into a private temp dir (0700) and `{tmp}` in `argv` points at it; `outputs` are read back after
  the run; the temp dir is always removed.
- `argv[0]` must be one of claude, codex, agy; `env` keys come from an allow list. The envelope never puts prompt text on
  a command line, so there is no shell quoting.
- The CLI runs in its own process group. When stdin of the script reaches EOF early (the SSH connection dropped, the
  hub cancelled) or the timeout passes, the group is killed. This is how cancellation and timeouts reach the host.
- The reply is one JSON object on stdout: exit code, stdout, stderr (tail), outputs.

`llm_runner.py` gets one seam: every `subprocess.run` of a CLI goes through a runner that is local or remote. Everything
that decides what to run (flags, lockdown, fallback order, parsing) stays as it is.

### Isolation on the remote host

The same lockdown as on the hub: claude with no built-in tools and `--strict-mcp-config`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`
(the operator's `~/.claude/CLAUDE.md` and skills must not reach a debate session), codex `--ignore-user-config` and
`--sandbox read-only`, an empty dedicated workdir.

What changes is the blast radius of a prompt injection from repository text: a CLI that can read files (codex read-only
sandbox) can read what its user can read **on that host**, and the answer ends up in the hub's DB. Tier-1 hosts hold no
fleet key, so nothing reaches further. R3 proposes a dedicated user (`guardian-llm`) per host, logged in on its own,
with nothing else in its home.

### Sessions

- The stored agent session gets `host` (empty = hub, for every existing record).
- Resume goes to that host. If the host is unavailable the turn starts a fresh session elsewhere, like the existing
  "resume failed, next provider" path; the thread digest gives it the earlier rounds.
- `guardian.py --attach` opens `ssh -t <host> <resume command>` inside tmux when the session is remote.
- When debugging, the transcripts are under that host's `~/.claude/projects/...`; `agent_started` names the host.

### MCP bridge (R2)

`McpTurn` already runs one log per turn on the hub. For a remote claude or codex turn the hub also opens a Unix socket
in that turn's private dir and runs SSH with `-R <remote sock>:<hub sock>` (stream-local forwarding). The MCP command
in the CLI config on the host is `guardian-llm-exec bridge <remote sock>`, which only copies stdio to the socket. On the
hub, each connection starts `repo_mcp.py` with the same arguments as today. Code, limits and the call log stay on the
hub. The forwarded socket only exists while that turn's SSH connection is open. Until R2, remote turns of members with
`code_tools: "mcp"` either run on the hub or fall back to repo blocks (decided in R1; the hub is the safer default).

### Events and dashboard

`agent_started` and `agent_finished` carry `host` (the hub's `debate_started.host` stays the engine host). New
`llm_host_unavailable` warning event when a host is skipped. The dashboard shows the host next to each agent turn; a
host column in Settings -> Servers shows `llm_slots` (R3).

## Phases

| Phase | Content | Done when |
|---|---|---|
| R0 | Measure with `scripts/llm_load_probe.py` (see "R0 procedure"): hub load and the CLI processes' share during real debates, past turn concurrency, CLI versions on Tier-1 hosts | The decision rule below says `go`; otherwise stop here |
| R1 | `guardian-llm-exec run/probe`, inventory fields, slots, placement, hub fallback, `host` in events; fresh sessions and repo-block members only | Same debate task gives the same verdict class local and remote; no turn fails that the hub could run |
| R2 | Sessions pinned to hosts, resume, `--attach`; MCP bridge | Multi-round debate with resumes and MCP members runs remote end to end |
| R3 | Dedicated user per host, `llm_account` caps, dashboard host column and settings | Operator can turn a host on and off from the dashboard |

## R0 procedure

Decided 2026-10-30: R0 decides whether R1 is built. The tool is `scripts/llm_load_probe.py` (standard library,
read-only on the hub, no model quota). It writes only into `~/guardian-eval/r0`, never into the production data dir.

```bash
# from the checkout on the Mac: put the script into the evaluation directory on the hub
ssh <hub> 'mkdir -p ~/guardian-eval/r0' && scp scripts/llm_load_probe.py <hub>:guardian-eval/r0/
# on the hub
cd ~/guardian-eval/r0
python3 llm_load_probe.py history --days 30          # past turns: durations, providers, how many ran at once
python3 llm_load_probe.py hosts                      # Tier-1 hosts: claude/codex/agy versions, CPUs, memory, load
nohup python3 llm_load_probe.py sample --duration 7d > sample.log 2>&1 &   # 5 s samples while debates run as usual
python3 llm_load_probe.py report                     # any time; the verdict at the end
```

`sample` puts every process into one of four classes by its ancestry: `llm` (an LLM CLI started by the engine: what
remote hosts would take), `retrieval` (`repo_mcp.py`), `engine` (`guardian.py` and its other children) and `other`
(Gitea, the Go agent, an operator's own CLI session). CPU of children that exited between two samples is taken from
the parent's `cutime`/`cstime` without counting it twice. A sample is busy when an engine-started LLM CLI ran in its
interval; the report is over the busy samples.

Decision rule (fixed before the data is read, `DECISION_RULE` in the script):

| | Criterion | Threshold |
|---|---|---|
| enough data | busy samples | >= 360 (30 min of debates at 5 s) |
| pressure (any) | hub CPU busy, 95th percentile | >= 70 % |
| | lowest MemAvailable | < 15 % of MemTotal |
| | PSI cpu some avg10, p95 | >= 20 |
| | PSI memory some avg10, p95 | >= 10 |
| share (any) | `llm` CPU ticks / hub busy ticks | >= 50 % |
| | `llm` RSS / used memory, p95 | >= 50 % |

Verdicts: `go` (pressure and share: build R1), `not-needed` (no pressure: the hub copes; record and stop),
`elsewhere` (pressure, but the CLIs are not its main share: remote hosts would not help; look at the engine or Gitea),
`insufficient` (too few busy samples: keep sampling). The result goes to `docs/analysis/<date>-r0-llm-load/` with
placeholder host names. The parallel rate-limit test of the same account is left out of R0: it spends the shared quota
and is only needed once R1 is built (`GUARDIAN_LLM_ACCOUNT_SLOTS`).

## How we will know it helps

Run the same task set (the planted-defect tasks of `proposer_eval.py`) with remote off and on: hub peak RSS and CPU
during the run, wall time, fallback rate, and the score (it must not change: the same models read the same evidence).

## Non-goals

- Moving project source code off the hub, or reading it over SSH.
- Running the engine, ops checks, actions or jev anywhere but the hub. Tier-2 hosts.
- Viewpoint diversity (`multi-perspective.md`).
- Copying credentials between hosts.

## Risks and open points

- Same account, more parallelism: spreading turns can hit the account's rate limit sooner; `GUARDIAN_LLM_ACCOUNT_SLOTS`
  bounds it; measured once R1 is built (not in R0, it spends the shared quota).
- Version drift of the CLIs between hosts; the probe refuses old claude, but output format changes of codex or agy can
  differ per host. `deploy-llm-skills.sh` could report versions.
- Network: Tailscale between hub and host; a dropped connection kills the remote turn (by design) and the hub retries
  locally, which costs that turn's time.
- The design does not reduce the engine's share. If R0 shows the engine (Go facts, index builds) as the main load, the
  answer is elsewhere (caching, scheduling), not here.
- Should a team be able to pin itself to the hub (`placement: "hub"`), e.g. for evaluations? Open.

## Implementation notes

- R0 (2026-10-30): `scripts/llm_load_probe.py` and `scripts/test_llm_load_probe.py`. Deviations from the table above:
  sampling reads `/proc` instead of `ps` (CPU ticks per process and reaped children); the rate-limit test moved out of
  R0 (see "R0 procedure"); `history` was added (the past turn concurrency from `events.jsonl` costs nothing and says how
  many turns there would be to spread). An LLM CLI without an engine ancestor (an operator's session) counts as `other`.
- R0 result (2026-11-08, `../analysis/2026-11-08-r0-llm-load/`): verdict `not-needed`. 120 958 samples, 2 238 busy;
  busy CPU p95 22 %, MemAvailable min 86 %, PSI cpu some p95 3.0, memory 0; `llm` CPU share 51 % (share met, no
  pressure). The 14 busy samples at 70 % CPU or more were the engine's. Deviation: the `other` memory share is above
  100 % because RSS includes file-backed pages that MemAvailable counts as free; the verdict does not use it. A repeat
  should compare RSS with MemTotal or read `Pss`.

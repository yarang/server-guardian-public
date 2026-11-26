# M2: resident agents sandbox trial with real CLIs (2026-11-15)

English | [한국어](results.ko.md)

> Point-in-time record from 2026-11-15; the CLIs and their flags change with every release.

Design: `../../design/resident-agents.md` (phases M1 and M2, "구현 메모"). Preceding probe:
`../2026-11-14-m0-resident-probe/results.md`. The trial ran the M1+M2 code of `7115ad3` against the real claude
(2.1.301) and agy (1.5.9) CLIs on one Tier-1 node (`node-1`), with the hub relay on the hub (`site-a-arm1`). Nothing was
deployed: the hub release, the hub's data directory and the node's production worker were not touched. Host names,
accounts, session and conversation ids below are placeholders or left out.

## Verdict

| Check | Result |
|---|---|
| Operator task claimed by one agent, question to the other, answer back, task completed | **passed** (claude claimed, asked agy, completed with agy's answer quoted) |
| `invisible_task` fallback with a real CLI | **passed** (agy's task-tagged answer was refused, agy resent it with `reply_to`) |
| Redelivery of an unacknowledged mail after a rebind | **passed** (the question survived three lost turns and was answered at the next epoch) |
| agy seed check | **defect found and fixed** (agy rewrites `settings.json` in a new key order; every later turn was lost) |
| Worker restart under a live relay | **passed** (stream lost, reconnect after 5 s, sync; both agents bound again without a relay restart) |
| Session continuity | **passed** (claude resumed its session across the rebind; agy kept its conversation within an epoch) |
| Project task: snapshot over ssh, repo reads, call log, pin release | **passed** for claude and agy (correct answers against the pinned commit) |
| Clean end state | **passed** (empty spools, empty node task tables, no leftover CLI or MCP process, production worker unchanged) |

M1 and M2 stay undeployed until the operator's OK. One defect was fixed in the driver (below); two points are left for
the operator.

## Setup

- **Hub**: a sandbox under `~/guardian-eval` with its own `GUARDIAN_DATA_DIR` (`data-resident`) and inventory, the
  engine of `7115ad3` in a separate directory, and a wrapper that refuses to run when the data directory is outside
  `~/guardian-eval`. `guardian.py --mail-relay` ran in the background from that wrapper.
- **ssh shim**: a `ssh` script first on the wrapper's `PATH` rewrote the three worker commands the relay uses
  (`worker.py relay`, `client`, `put-snapshot`) to the trial worker directory on the node and failed closed (exit 97)
  on anything else. The relay code was not changed for the trial.
- **Node**: the trial worker ran from its own directory `~/guardian-eval/resident-worker` as the transient user unit
  `guardian-resident-trial` (`RuntimeMaxSec=4h`, `GUARDIAN_WORKER_MAX_JOBS=2`). The production unit `guardian-worker`
  kept running untouched (same PID from before the trial, same file hashes after it).
- **Registration**: `--resident-add` refuses a node until the M2 installer writes `worker_install.protocol: 3` and
  `worker_capable.memory_free_mib`. In the sandbox inventory the protocol was set by hand and the capability was
  probed again, so the two agents `claude@node-1` and `agy@node-1` (driver `headless`) could be declared.
- **Project**: `sg-resident`, a clone of the repository at `4b79b52`, so the expected answers differ from the current
  tree (worker protocol 2 there).

## Scenarios

### 1. Operator task, question, answer

The operator created a task for claude ("claim it, ask agy one question by mail, complete it with the answer quoted")
and sent a wake mail with `--mail-task`. claude claimed the task and asked agy. agy first sent its answer tagged with
the task and got `invisible_task` (the M1 visibility rule: the task is claude's), then sent it again with `reply_to`.
claude completed the task in a later turn with agy's two-sentence answer quoted and agy named. All mail ended
`acked`.

### 2. Lost turns and the seed-check defect

agy's first turn passed. Its next three turns were lost in the pre-turn check and the driver hit `lost_cap` 3:
`agent_exit (lost_turns)`, rebind after 30 s, a fresh start card. Cause: after its first turn agy rewrites
`.gemini/antigravity-cli/settings.json` with the same value in a different key order, and the driver compared every
seed file byte for byte. Each later turn would have been lost the same way, and every rebind loses the conversation.

The question claude had sent stayed unacknowledged in agy's spool through the lost turns and was delivered again at the
next epoch, where agy answered it. Redelivery works.

Fix (in the tree, after the trial; see `resident_driver.py`):

- `hooks.json` (the tool closure) and the hook script are still compared exactly; the other JSON seeds are compared by
  value, so key order and whitespace do not matter but any added permission, trusted path or key still fails the turn.
- Every lost turn is now logged with its reason. Before, only the cap was logged, which hid the cause during the trial.

Tests: a reordered `settings.json` passes; an extra `permissions.allow` entry, an extra trusted path, an extra key,
broken JSON and a reordered `hooks.json` fail; a rewritten `settings.json` does not lose the next turn; each lost turn
logs its reason.

### 3. Worker restart and resume

The fixed driver was installed by restarting the trial unit while the relay was connected. The relay logged
`stream lost, agents down`, reconnected after 5 s and synced; both agents bound again on their own:

- claude at epoch 2 as a resume of its earlier session (first turn of kind `resume`);
- agy at epoch 3 with a fresh start card (agy resumes only within an epoch, by design).

agy within one epoch: the operator asked it to remember a code word (answer "ok", notice turn), then asked for the word
again; the second turn was of kind `continuation` with `--conversation <id>` and the answer was the word. No lost turn
was logged after the fix.

### 4. Project tasks with snapshots

Two tasks on `sg-resident` (commit pinned at creation), one per agent, each woken by a mail:

| Task | Agent | Repo calls (from `--task-calls`) | Result | Time from creation |
|---|---|---|---|---|
| value of `PROTOCOL` in `cli/llm-agent/worker.py` | claude | `grep` in the file, `grep '^PROTOCOL\s*='` | imported at line 32, set to `2` in `remote_agents.py` line 35 (correct at `4b79b52`) | 36 s |
| value of `DEFAULT_PROVIDER_ORDER` in `cli/llm-agent/llm_runner.py` | agy | `grep`, `read` of the file | line 43 and the full list (correct) | 96 s |

The snapshot (4.1 MB) was built once on the hub and uploaded through the shim's `put-snapshot`. Both CLIs read code only
through `resident_repo_mcp.py` with the `task` argument; neither call log was `dropped`. After each accepted
`task_update` the node's task table was empty, so the snapshot pin was released.

## Observations for the operator

1. **Creating a task does not wake the assignee.** `--task-create --task-assignee` writes the row only; the agent learns
   of it on its next turn. The trial sent a wake mail with `--mail-task` each time. A notice or wake on assignment
   (like the peer notices of D9) would remove the extra step. Resolved 2026-11-15: the store queues a
   `task_assigned` notice on assignment ([resident-agents.md](../../design/resident-agents.md), "Wake on assignment").
2. **Answers to the operator stay `queued`.** The operator has no spool, so its incoming mail is never acknowledged and
   `--mail-list` shows `answer/queued`. Expected under the current design; the listing could label it as "for the
   operator" instead.
3. The `invisible_task` point stays as decided in M1: real agy found the `reply_to` path by itself after the refusal.

## Cleanup

The relay and the trial unit were stopped; no claude, agy or MCP process was left on the node. Spools and node task
tables were empty. The production worker had the same PID as before the trial and the same hash over its `*.py` files.
The trial worker directory and the scratch files on the node were removed; the hub sandbox `data-resident` is kept as
the record.

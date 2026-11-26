# R0: hub load from LLM CLI turns (2026-10-30 to 2026-11-06)

English | [한국어](results.ko.md)

> Point-in-time record from 2026-11-08; the code and the load may change after it.

Design: `../../design/remote-llm-hosts.md` (phase R0, "R0 procedure"). Tool: `scripts/llm_load_probe.py`, run on the
hub from `~/guardian-eval/r0` (read-only, no model quota, nothing written to the production data dir). Machine-readable
report: `report.json` (the output of `report --json`; no host names).

## Verdict: `not-needed`

No pressure criterion was met, so R1 is not built. The hub copes with the debate load of this week. This is the
`not-needed` branch of the design's decision rule: record and stop.

| Item | Value |
|---|---|
| Window | 2026-10-30 07:19 to 2026-11-06 07:19 UTC (7 days), 5 s samples |
| Samples | 120 958, of which 2 238 busy (an engine-started LLM CLI ran in the interval; about 3.1 h) |
| Enough data | yes (rule: >= 360 busy samples) |

## Decision rule against the data

The thresholds were fixed before the data was read (`DECISION_RULE` in the script). All numbers are over the busy
samples unless marked otherwise.

| | Criterion | Threshold | Measured | Met |
|---|---|---|---|---|
| pressure | hub CPU busy, p95 | >= 70 % | 22 % (p50 4 %, max 100 %) | no |
| | lowest MemAvailable | < 15 % | 86 % | no |
| | PSI cpu some avg10, p95 | >= 20 | 3.0 | no |
| | PSI memory some avg10, p95 | >= 10 | 0.0 (full 0.0) | no |
| share | `llm` CPU ticks / hub busy ticks | >= 50 % | 51 % | yes |
| | `llm` RSS / used memory, p95 | >= 50 % | 37 % | no |

The share criterion is just met, but a share only matters under pressure (`go` needs both). CPU share while busy:
`llm` 51 %, `engine` 16 %, `retrieval` 0.2 %, `other` 44 %, unattributed 0 %.

Other numbers from the busy samples:

- Concurrent LLM CLI turns: p50 1, p95 2, max 4.
- Largest total RSS of the LLM CLIs: 1021 MiB (of 11.6 GiB).
- Over all 120 958 samples (busy or not), hub CPU p50 0.6 %, p95 8 %; 0.01 % of samples at or above 70 %.

### The few CPU peaks are the engine's

14 of the 2 238 busy samples reached 70 % CPU or more (two short bursts, 2026-10-30 15:43 and 2026-10-31 08:40 UTC). In
them the `engine` class took most of the ticks (for example 821 `engine` against 160 `llm` of 1000), so the CLIs
were not the cause. The design's risk note applies: if the engine ever becomes the main load, the answer is caching
or scheduling of its work (Go facts, index builds), not remote hosts. At this level it needs no action.

## Context: past turns and the Tier-1 hosts

Read on 2026-11-08 with `history --days 30` and `hosts` (so not limited to the sampling window). Host names are
placeholders.

- Last 30 days: 59 debates, 458 CLI turns (claude 440, codex 14, agy 4), no built-in fallbacks. Turn duration p50
  82 s, p95 375 s, max 850 s. 16.4 agent hours over 14.2 h with at least one turn running; peak 3 at once; time by
  concurrency: one turn 85 %, two 14 %, three under 1 %.
- Hosts (all aarch64, 2 CPUs, 11.6 GB, load under 0.4 at the time):

| Host | Free GB | claude | codex | agy |
|---|---|---|---|---|
| `site-a-arm1` (hub) | 10.5 | 2.1.301 | 0.160.0 | 1.4.12 |
| `node-1` .. `node-6` | 10.4 - 11.1 | 2.1.301 | 0.163.0 | 1.4.12 |

The hub's codex (0.160.0) is three releases behind the Tier-1 hosts (0.163.0). This is the version drift named in
the design's risks; it does not affect R0. Updating the hub's codex is a separate operator decision.

## Measurement limits

- The `other` memory share p95 is 114 %, above 100 %. "Used memory" is MemTotal minus MemAvailable, and MemAvailable
  counts reclaimable page cache as free, while a process's RSS includes its file-backed pages (Gitea's and the Go
  agent's binaries and mapped files). So RSS shares can add up to more than "used". The verdict does not depend on it:
  the memory pressure criteria use MemAvailable and PSI, and the `llm` memory share (37 %) is not needed for
  `not-needed`. If R0 is repeated, compare RSS with MemTotal, or read `Pss` from `/proc/<pid>/smaps_rollup`.
- One week of ordinary use: no audit run, no wide multi-perspective team and no evaluation batch fell into the window.
  A week with those could look different.
- Operator CLI sessions on the hub count as `other`, by design.

## What follows

- R1-R3 of `remote-llm-hosts.md` are not built. Re-open them if the hub's load grows: repeat R0 with the same tool and
  rule (for example after enabling `proposers` on a production team, or during repeated audit runs).
- If a team needs to spread turns anyway, the worker path of `remote-agents.md` (team `host`, W1/W4b) is already
  deployed; no new transport is needed.
- The sampler has stopped (it ran for its fixed 7 days); the samples stay in `~/guardian-eval/r0` on the hub.

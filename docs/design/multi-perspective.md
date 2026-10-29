# Multi-perspective debates: independent proposals and blind critique (design)

English | [한국어](multi-perspective.ko.md)

Status: ten decisions made (2026-10-18, through jev; see "How the decisions were made"). P1 and P3 are implemented (off by default; no team uses them), P2 is built and was run on the hub (2026-10-19): opt-in, see "Implementation notes".
Origin: the question "should debate agents run on different servers to analyse from more angles?" The answer reached
in that discussion: diversity of viewpoints comes from **logical separation** (model, persona, evidence, independence),
not from the machine an agent runs on. This document designs the logical part. Physical distribution stays out of scope
(see "Non-goals").

## Problem

Today one debate round is one chain: **one Expert drafts → the Critic panel reads that draft → the Judge decides**
(`debate_engine.py`, round loop). Two things limit how many angles it covers:

1. **One draft anchors everything.** Every Critic, and the next round's Expert, starts from the same first proposal. The
   Critics are independent of each other (they do not see each other's critique), but all of them are anchored on the
   Expert's framing. A flaw in the framing is rarely found by a reviewer who has already accepted it.
2. **The lineup can look diverse and not be.** A panel of three personas that all run on the same provider shares that
   provider's blind spots.

What the engine already does well and this design keeps: the panel runs in parallel (`_ask_critics`), panel critiques are
concatenated verbatim and never summarised by an LLM, the Judge is one accountable author, and a round with any fallback
answer is `UNVERIFIED` (`docs/design/debate-teams.md`, "Rules").

## Idea: fan out, then converge

```
round 1   lead Expert ─┐
          proposer B  ─┼─ drafts written in parallel, none sees another  (new)
          proposer C  ─┘
                       ▼
          Critic panel reads all drafts under neutral labels            (changed prompt)
                       ▼
          Judge decides (sees the drafts, the critiques; writes the one decision)
round 2+  lead Expert alone writes ONE consolidated proposal from the drafts and the critique;
          the rest of the round is unchanged
```

- **Independent proposals.** Besides the lead Expert (today's `expert`), a team may list up to two extra `proposers`.
  Each one answers the same task in its own session, in parallel, without seeing the other drafts.
- **No LLM merge.** The drafts reach the Critics and the Judge verbatim, as `### Proposal A`, `### Proposal B`, ... (the
  same rule as panel critiques). The consolidation is the lead Expert's job in the next round, so the proposal and the
  approval still each have one accountable author.
- **Blind critique.** The Critics see neutral labels, not persona or provider names, and the order of the drafts rotates
  with the round number so that "A" is not always the lead. The transcript still records which agent wrote which draft.
- **Fan-out only where it pays.** Extra drafts are written in the first round of a thread (and on a follow-up
  instruction). Later rounds converge, so the cost does not multiply with the number of rounds.

## Decisions (made 2026-10-18)

| # | Item | Recommended default | Other choices |
|---|---|---|---|
| 1 | Where the angles come from | Extra `proposers` in the team file, each a `Member` (persona + providers). Default: none, so existing teams behave exactly as today | Several lead Experts of equal rank (breaks "one accountable author") |
| 2 | How many | 0-2 extra proposers (`MAX_PROPOSERS = 2`), so at most 3 drafts | Up to 4 like the Critic panel. More drafts make the Critics' prompt long and the Judge's input grow with no evidence it helps |
| 3 | Merging the drafts | None by an LLM. Verbatim, labelled; the lead Expert consolidates in round 2+ | An LLM synthesis step before the Critics (adds a sequential call and blurs evidence) |
| 4 | Blinding | Neutral labels (`Proposal A/B/C`) to Critics, rotated per round; real names only in the transcript | Show names (anchoring on authority); random order per call (not reproducible) |
| 5 | When the extra drafts are written | First round of a thread and after a follow-up instruction; round 2+ converge | Every round (cost × rounds, drafts keep drifting apart) |
| 6 | A proposer falls back to the built-in text | **Any fallback makes the round `UNVERIFIED`**, the same rule as the Critic panel (jev, 0.90; the first draft of this document recommended the other choice) | Leave the extra draft out, record it (`fallback`, a warning, the Judge told "2 of 3 drafts available") and keep the round verified: more robust under provider limits, but a debate with fewer angles than configured would count as verified |
| 7 | Diversity check | `--teams` and the dashboard warn when all members of a team resolve to one provider ("angles may not be independent") | No check |
| 8 | Pre-analysis by Critics before they read the drafts | **Not in this design.** Evaluate it separately after the proposers (phase P4) | Build it now |

## Team file

`proposers` is optional and absent from the saved snapshot when empty (the same pattern as `code_tools`), so existing
threads and snapshots keep their shape.

```json
{
  "id": "multi-angle",
  "expert":    {"persona": "domain_architect", "providers": ["claude"]},
  "proposers": [{"persona": "ops_sre",         "providers": ["codex"]}],
  "critic": [{"persona": "critic_redteam", "providers": ["codex"]},
             {"persona": "devops_specialist", "providers": ["agy"]}],
  "judge":  {"persona": "jev_mcp_decision", "providers": ["claude"]}
}
```

Rules, in the style of the existing team rules:

- Personas must be unique across the expert and the proposers (the persona is the session key suffix, see below).
- Session key of a proposer: `Domain Expert:<persona>`. The lead keeps the plain `Domain Expert`, so sessions of existing
  threads continue (`Lineup.session_key`, same approach as the Critic panel).
- A proposer may use `code_tools: "mcp"` like any member. Each proposer has its own citation bookkeeping because the
  citation check is keyed by session key.
- Proposers are ignored with a warning in ops and design projects until each kind has been evaluated (the evaluation
  starts with the design kind because its planted-defect corpus exists; ops has none yet).

## Engine changes

All inside `cli/llm-agent`; the Go hub and its API do not change (the hub only spawns the CLI).

| File | Change |
|---|---|
| `team_manager.py` | `Lineup.proposers`, parse/validate (unique personas, `MAX_PROPOSERS`, provider diversity warning), snapshot only when non-empty, `session_key` for proposers |
| `debate_engine.py` | `_ask_proposers` modelled on `_ask_critics` (thread pool, one entry per draft, own trace each); the round loop writes one `EXPERT_ROLE` transcript entry per draft with a `draft` label; the Critics' and the Judge's prompt text lists the labelled drafts; `start_round` counts distinct rounds, not Expert entries (`len([t for t in transcript if role == EXPERT_ROLE])` would double count); round 2+ gives the lead the drafts and the joined critique |
| `debate_events.py` / events | `debate_started.members` lists the proposers; `agent_*` events already carry `agent` and `agent_name`, so lanes appear by themselves |
| `thread_digest.py` | no change (keyed by role; verify with a multi-draft round in a test) |
| `debate_report.py`, `agentMonitor.ts` | no change expected: both key by agent and role, as the panel needs. A test with two Experts in one round confirms it |
| Dashboard team editor | later (phase P3): a proposers list next to the Critic panel, with the provider-diversity warning |

Compatibility: a team without `proposers` takes exactly today's code path (the same prompts and the same transcript).
The multi-draft wording is a separate branch, so `RETRIEVAL_PROMPT_VERSION` is bumped only if a shared prompt line
changes. Resuming a thread started before this change works because `start_round` is computed from distinct rounds.

## Resource model

Parallel agents are separate CLI processes on the hub (2 OCPU, 12 GB). This design raises the number of simultaneous
processes in the first round from one to up to three before the Critics, and up to four in the Critic stage, with
`MaxRunning` debates (default 2) on top. Two things must be known before any default is changed:

- **Hub load.** The memory and CPU of a running `claude`/`codex` process on the hub are not measured yet (phase P0).
- **Provider limits.** In evaluation 2 the first run was invalid because 107 of 135 runs ended with the built-in fallback
  after claude CLI failures, presumably the usage limit (`docs/analysis/2026-10-13-retrieval-eval2/plan.md`, "Re-run").
  More simultaneous calls on one account make that more likely, and under decision 6 one failed proposer ends the
  round as `UNVERIFIED`. So a parallel cap is part of P1: `GUARDIAN_MAX_PARALLEL_AGENTS` (default 4 per debate; the
  proposers and the Critics each run through the same bounded pool). If the evaluation shows that limit failures make
  multi-draft debates too fragile, decision 6 is the first thing to revisit (its other choice is in the table).

## How we will know it helps (evaluation)

There is no evidence yet that more drafts find more problems. Adoption as a default needs a measurement, run the way
evaluation 2 was (own data directory under `~/guardian-eval`, never the production data directory).

- **Task type.** The retrieval questions of evaluations 1-2 have one correct answer, so they cannot show a gain from
  diversity. Use **review tasks with planted defects**: an artifact or small scratch repository with 3-5 defects planted
  in it at once, built from the seeded-defect corpus (`artifact_corpus.py`) whose `low-contrast` and `false-claim`
  classes have no detector and were meant to be measured through a debate. Add a code variant (a scratch repository with
  planted bugs) once the artifact variant is shown to work.
- **Configurations.**

  | | Lineup | Question it answers |
  |---|---|---|
  | C0 | today's default team | baseline |
  | C1 | lead + 1 proposer, same persona and same provider | does more compute alone help? (control) |
  | C2 | lead + 1 proposer, different persona, same provider | is it the viewpoint? |
  | C3 | lead + 1 proposer, different persona and provider | is it the model? |
  | C4 | C3 with names shown to Critics | does blinding matter? |

- **Metrics.** Primary: recall of the planted defects (union over the debate's final decision and critiques). Also:
  invalid claims, citation failures, rounds to consensus, wall time, bytes and steps (tokens are not in the events),
  fallbacks. Three runs per configuration and task, blind grading with shuffled labels, a run with any fallback is
  discarded and repeated, as in evaluation 2.
- **Decision rule (decision 10).** Make multi-draft the default for a project kind only if C2 or C3 beats
  both C0 and C1 in mean recall by more than one standard deviation of C0, and the mean wall time grows by at most 50 %.
  Otherwise keep it as an opt-in team option.

## Phases

| Phase | Content | Needs a model |
|---|---|---|
| P0 | Measure on the hub (read-only): memory and CPU of one running LLM CLI process, per-stage durations and the number of rounds to consensus from recent `events.jsonl` files | no |
| P1 | `team_manager` + `_ask_proposers` + prompts + the parallel cap, with tests on stub providers | no |
| P2 | Planted-defect task set and the evaluation harness (C0-C4) | build: no; run: yes |
| P3 | Dashboard team editor; `--teams` diversity warning | no |
| P4 | Decide defaults from the evaluation; then evaluate Critic pre-analysis (decision 8) | yes |

P1 may start before P0 ends because the feature is off by default; enabling it on the hub waits for P0.

## How the decisions were made

The ten decisions were put to jev (`jev_ask`, model jev-1.13.0) with the problem, the constraints and the options; the
questions were the table rows plus two cross-checks (no physical distribution now; the adoption rule). Results:

| Decision | jev's choice (probability) | Action level |
|---|---|---|
| 1 proposers | `proposers` (1.00) | act |
| 2 count | 0-2 (0.50, a plurality) | abstain: kept as the author's pick, low stakes and changeable by one constant |
| 3 merge | verbatim (1.00) | act |
| 4 blinding | neutral labels, rotated (0.93) | act |
| 5 when | first round (0.83) | review: accepted |
| 6 fallback | any fallback is `UNVERIFIED` (0.90) | act: **differs from the first draft's recommendation and was adopted** |
| 7 diversity check | warn (0.58, a plurality) | abstain: kept as the author's pick, a warning only |
| 8 pre-analysis | defer (0.99) | act |
| cross-check: physical distribution | not now (1.00) | act |
| cross-check: adoption rule | measured (1.00) | act |

jev scored what was written in the question, so the answers are only as good as the options offered; two rows were
decided without its confidence (2 and 7) and are marked above.

## Non-goals

- **Running agents on other nodes.** Physical distribution adds session pinning and credentials per execution host, code and
  prompts leaving the hub, version drift and a conflict with the standing decision "no fleet workers" (CLAUDE.md), and it
  adds no diversity by itself. It is reconsidered only if P0 shows the hub saturated, or a separate account/quota is
  needed. The independent workloads that parallelise well without it are the code wiki build and the audit shards, a
  separate topic (hub-side thread pools).
- **Team against team** (`debate-teams.md`) and **an LLM that merges drafts**.
- **A queue, a graph DB or a vector index** (standing decisions).
- Changing what the Judge may propose or the jev gate. The Judge still writes the only decision; multi-draft changes what
  it reads, not what it is accountable for.

## Risks and open points

- Longer prompts: the Critics and the Judge read up to three drafts. Retrieval budgets are unlimited by decision, but a
  long input can dilute attention; the evaluation records bytes and watches the Judge's format errors.
- Position bias among drafts is reduced by rotation, not removed.
- The lead can ignore the other drafts when consolidating. The Judge reads the drafts itself, and the transcript keeps
  them, so an ignored draft is visible to the person reading the thread.
- The planted-defect corpus tests defect finding, not design quality. A gain there says nothing about open design
  questions; keep multi-draft opt-in for those until measured.

## Implementation notes

P1 and P3 are implemented and tested on stub providers (2026-10-18); nothing is enabled on the hub, and no team has
`proposers` yet. P2 is built and was run once on the hub (see the last note).

- **P1 (engine).** `Lineup.proposers` (0-2, `MAX_PROPOSERS`) is absent from the snapshot when empty, so teams without
  it take today's code path and the lead's prompt is byte-identical (`RETRIEVAL_PROMPT_VERSION` is unchanged; only
  conditional prompt text was added). The drafting round is the first round of each `run_debate` call, so a follow-up
  instruction drafts again. Drafts are one `Domain Expert` transcript entry each; the DB keeps only
  (round, agent, role, content, provider), so the neutral `draft` label lives in the result, the events and the report,
  not in the stored thread.
- **Parallel cap.** `GUARDIAN_MAX_PARALLEL_AGENTS` (default 4) bounds the thread pool of the drafting phase and of the
  Critic phase separately, not the two together.
- **Blinding is for the first critique only.** Critics and the Judge of the drafting round see neutral labels
  (rotated per round), but a fresh session that starts later (round 2 of a new Critic session, or a follow-up) reads
  the thread history, in which the drafts carry their authors' real names. Blindness is not claimed beyond the first
  round; config C4 (`GUARDIAN_DRAFT_NAMES=1`, authors named in the headings) measures how much it matters.
- **Warnings.** Same-provider drafts warn in validation and in `--teams`; a proposer whose provider is not installed
  warns in the same way as for other members; a built-in fallback by any drafter makes the round `UNVERIFIED`.
- **P3 (dashboard).** The team editor has an optional "extra proposers" card between the expert and the Critic panel
  (limit from `catalog.max_proposers`, default 2; the payload carries `proposers` only when there are any), the team
  card lists them, and the strings are in both catalogs. `config_ops` reports them and `--teams` prints a `draft :` line.
- **P2 (harness, `cli/llm-agent/proposer_eval.py`, no model).** Five tasks of four defects each (the corpus' broken
  SVG, low-contrast, dead-link, missing-section and false-claim classes, none planted twice), configs C0-C4 as team
  files (everything but the proposer equal), and a scorer: automatic pre-grade (file named plus a key word, an upper
  bound), blind sheet with shuffled labels and a separate key, fallback runs discarded, and decision 10 as code.
  Deviations: C1 needs a copy of the lead's persona under a second id (`domain_architect_b`) because personas must be
  unique in a team; the artifacts are reviewed as files of a scratch **code** project because ops and design projects
  ignore proposers. `--prepare DIR` writes the tasks, teams, personas and env; the hub run itself (own data directory
  under `~/guardian-eval`) is not scripted yet and needs P0 first.
- **Evaluation run (2026-10-19, `docs/analysis/2026-10-19-multi-draft-eval/results.md`).** The three easy tasks hit the
  ceiling (recall 1.00 in all five configurations), so three hard tasks (12 planted defects each) were added and run
  once per cell on the hub: C0 0.86, C1 0.89, C2 0.89, C3 0.89, C4 0.92, wall time within ±5 % of C0 for C2/C3, peak
  memory 1.5-2.5× C0's (under 1 GB). Decision 10 gives **opt-in**: the gain of +0.03 is far below the margin of 0.10.
  Only task h1 separates the configurations; a defect per task is missed by every configuration. Open: the human grade
  of the blind sheet, repetitions 2-3, and a harder task set. Deviations: the hub script (`run-eval.sh`) first wrote
  every project's workspace as the easy-task path, so the hard runs had no retrieval and fell back three times; fixed
  in `cc17844`, and a run that ends without `result.json` now stops the script.

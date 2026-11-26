# jev verdict: the close decides with jev, an LLM writes the decision (design)

English | [한국어](jev-verdict.ko.md)

> Generalised by [verdict-sources.md](verdict-sources.md) (2026-11-01): the jev verdict is the first verdict source; its
> decisions and phases stay valid inside that layer.

Status: proposed 2026-11-01; decisions V1-V7 made with the operator the same day. Nothing is built. Phase J1 (shadow
mode) comes first; the verdict switches to jev only after its measurement (J2).

## Question

The judge seat (the close of a flow, [flow-graph.md](flow-graph.md)) runs only LLM CLIs (`claude`, `codex`, ...). Why can
it not use typesafe.ai's jev API, which the hub already pays for and calls in the jev gate?

## What we found

- **No decision forbids it; the structure does.** The seat runner (`llm_runner.py`) drives CLI sessions, and the judge
  needs a session: it reads code (repo blocks or MCP), answers over several turns and rewrites invalid actions. jev has
  no such interface.
- **jev judges, it does not write.** Its tools (`jev_check`, `jev_classify`, `jev_score`, `jev_ask`, `jev_triage`)
  return probabilities over questions and options the caller supplies; none produces free text. Models on the hub's key:
  `jev-latest` (2026-10-22) and `jev-preview`. The judge seat does two jobs: the **verdict** (approve or not), which
  is exactly a jev judgment, and the **writing** (decision, rationale, constraints, actions), which jev cannot do.
- **jev already sits in the close, as the gate.** After an LLM judge approves, `jev_gate.py` submits the written
  decision (`jev_submit_decision`): `PASSED` releases its actions, `DISPUTED` holds them. On the hub from 2026-10-03 to
  2026-11-01, 62 approvals reached the gate: 50 `PASSED`, **12 `DISPUTED` (19 %)** (weak rationale 9, constraint
  uncertain 6, hard to reverse 2; several per review). None of the 12 passed later: 10 threads were closed by a person,
  2 stay `DISPUTED`. That disagreement is about the quality of the written decision, not about the verdict: jev has
  never been asked whether to approve, so there is no data yet on how often it would agree with the LLM judge.
- **The jev key stays with the engine.** `TYPESAFE_*` is never passed to an agent process (`env_policy.py`,
  `NEVER_PREFIXES`); only the engine calls jev. This design keeps that.

## Decisions (2026-11-01)

| # | Decision |
|---|---|
| V1 | **The close's job is split in two:** the verdict and the writing. A flow's close gains `verdict: "llm"` (default, today's behaviour) or `"jev"`. |
| V2 | **With `verdict: "jev"`, jev gives the verdict and the close's character writes.** The writer keeps the judge persona, providers and tools; its turn says the round is approved and asks for the decision, rationale, constraints and actions in today's format. |
| V3 | **jev decides only when it is sure.** At or above the approve threshold the round is approved by jev; at or below the reject threshold it is rejected by jev with its reasons; in between, the LLM judge decides exactly as today. A jev error or timeout also falls back to the LLM judge. |
| V4 | **A writer may object.** Instead of a decision it may answer `OBJECTION:` with reasons. Then the round is **not approved**; the objection becomes the rejection reasons the next round reads, and the event `verdict_disagreement` records it. (Chosen over handing to a person or letting jev's verdict stand.) |
| V5 | **The jev gate does not change.** The written decision is still checked; `DISPUTED` still holds the actions for a person. |
| V6 | **Shadow mode first.** Before any flow uses `verdict: "jev"`, every LLM judge turn also asks jev the same verdict question on the same material and records both, without changing the outcome. The switch is allowed per flow only after J2's measurement meets its bar. |
| V7 | **The key stays with the engine.** jev is called by the engine through `jev-mcp`, never by an agent; `TYPESAFE_*` stays out of every child environment. |

## The verdict question

jev sees what the LLM judge sees: the task and the judge material of the round (the proposal or the labelled drafts and
the critique; with a summariser, the summary, see [flow-graph.md](flow-graph.md) G3). One `jev_ask` call, so the extra
questions cost almost nothing:

| id | type | question (sent in full) |
|---|---|---|
| `approve` | check | A judge reads this proposal and the critique of it, and writes the final decision itself, adopting what is valid in the critique. Can that decision be written now from this material? Yes: the proposal is a sound base; what the critique raises can be settled inside the decision (a correction, a constraint, a check, a smaller scope). No: the critique shows it is wrong at its core, misses the task or rests on unsupported claims, so it must be redrafted first. |
| `fundamental` | check | Does the critique show a flaw that cannot be fixed within a decision and requires the proposal to be redrafted? |
| `evidence` | score | How well do the proposal's claims rest on evidence quoted in the material? (none, some, most, all) |

Only `approve` decides; `fundamental` and `evidence` are recorded to explain disagreements. This is question set 2
(records carry `questions: 2`): set 1 asked whether the proposal answered every blocking point of the critique, which it
never can in a round (the critique comes after it), and jev said no to 56 of 60 past rounds
([replay](../analysis/2026-11-01-jev-verdict-replay/results.md)). Thresholds start at
`yes_at_or_above` 0.8 and `no_at_or_below` 0.2 and are calibrated on the shadow data (J2).

## Rounds under `verdict: "jev"`

```
middle (unchanged) ──► jev verdict ──┬─ approve (p ≥ 0.8) ─► writer (close character) ─┬─ decision ─► jev gate ─► actions / HELD
                                     │                                                 └─ OBJECTION ─► not approved, next round
                                     ├─ reject (p ≤ 0.2) ─► not approved, jev's reasons to the next round (no LLM call)
                                     └─ between / jev error ─► LLM judge as today
```

- A rejected round costs one jev call instead of an LLM judge turn; an approved one costs a jev call plus the writer
  turn (the same LLM turn as today's judge).
- The last round keeps today's rule: without an approval the thread ends `REVISION_REQUIRED`.
- `UNVERIFIED` is unchanged (a canned answer from a seat or the writer).
- Transcript: the jev verdict is an entry of role `JEV Verdict` with `seat: "close"`, the probabilities and the three
  answers; the writer's entry keeps the judge role, so reports, the jev gate and the audit read it as today.

## Shadow mode (J1)

`GUARDIAN_JEV_VERDICT=shadow` (default `off`; ops and design projects included): after each LLM judge turn the engine
asks the verdict question on the same material and emits `jev_verdict` with `{p_approve, jev, llm, agree, blocking,
evidence}`; nothing else changes. A jev failure is logged and skipped. `guardian.py --jev-verdict-report` summarises the
recorded events per project kind: agreement, the confusion table, and the disagreements with their thread ids.

The bar to allow `verdict: "jev"` (J2, proposed numbers for the operator to confirm): at least 30 shadow verdicts; among
those where jev is sure (outside the band), agreement with the LLM judge of at least 90 %; and every case where jev
approves surely while the LLM judge rejected is read by a person and found harmless.

## Flow file

```json
"close": {"character": "jev_mcp_decision", "verdict": "jev", "allowed_tools": ["jev_mcp:run_tests"]}
```

`verdict` is valid on every flow; `"jev"` is refused at save until the operator enables it on the hub
(`GUARDIAN_JEV_VERDICT=on`), so no flow uses it before J2. The editor's close panel offers it with that note.

## Event contract (J3)

Screens derive a run's state from events, so a round the jev verdict decides must look like any other round to them:

- Every round still emits one `verdict` event of today's shape (`approved`, `unverified`), whether jev or the LLM judge
  decided and whether the writer objected (an objection is `approved: false`). The list, the overview and the run
  status (`lib/debate.ts`) read only this event and the jev gate, and keep working unchanged.
- New events add to it, never replace it: `jev_verdict` (`p_approve`, `verdict` approve | reject | uncertain, the
  thresholds, `blocking`, `evidence`, `decided_by` jev | llm) and `verdict_disagreement` (the writer's objection, or in
  shadow mode a jev verdict that differs from the LLM judge). Both carry `seat: "close"`.
- `final_decision` gains `verdict_by` (`llm` or `jev`) and, with jev, `jev_verdict` (the probabilities and answers), so
  the result view and the report need no events to tell who decided.

## UI impact (J4)

Checked against the dashboard code on 2026-11-01. Today the screens assume the verdict is one LLM judge turn.

| Screen | Today | With the jev verdict | Change |
|---|---|---|---|
| Result: verdict chain (`ResultView.tsx`, `verdictSteps`) | "LLM judge -> jev-mcp -> gate", fixed | would show "LLM judge: approved" when jev approved, and "LLM judge: revision required" on a round no LLM judged | the first step follows `verdict_by`: "jev verdict (p 0.86)" then "writer"; an objection is its own step |
| Agents monitor (`lib/agentMonitor.ts`, `Timeline.tsx`, `LaneCards.tsx`) | unknown event types are ignored | `jev_verdict` and `verdict_disagreement` do not appear; the judge lane stays "waiting" in rounds jev decided, as if stuck | timeline entries for both events; the judge lane shows "decided by jev" in such rounds and is labelled "writer" when it writes |
| Transcript (result view) | the role is shown as a raw chip | a `JEV Verdict` entry shows raw English and numbers; an `OBJECTION` looks like an ordinary judge turn | a label for the role and a readable line (probability, verdict, reasons); objections marked |
| Run status (list, overview) | from the `verdict` event and the gate | wrong only if a jev round emitted no `verdict` | none, given the event contract above |
| Report and Word/PowerPoint export (`debate_report.py`, Go report) | a judge text in every round | a jev-rejected round has no judge text | a jev verdict row (probability, verdict, reasons) in such rounds |
| Flow editor (close panel, canvas, gallery) | - | - | the verdict choice and thresholds, "writer" as the character label, "jev verdict" on the close node and the gallery row; greyed out with a note until the hub enables it; optionally the flow's shadow agreement (J1 data) next to the choice |
| Approvals, auto-apply, Ask, settings, nodes | - | unchanged: the writer still proposes the actions, auto-apply still needs a jev gate `PASSED` | none |

The same gap exists today for the summariser of [flow-graph.md](flow-graph.md) G3: its transcript entries show the raw
role `Summariser`, and its monitor lane has no place in the role order. J4 fixes both the same way; it may also be
fixed on its own before.

## Phases

| Phase | Content | Done when |
|---|---|---|
| J1 | Shadow mode: verdict question, `jev_verdict` events, `--jev-verdict-report`, tests with a fake jev | deployed with `GUARDIAN_JEV_VERDICT=shadow` on the hub |
| J2 | Measurement on production debates | the bar above is met or not; recorded under `docs/analysis/` |
| J3 | `close.verdict: "jev"`: compile, engine branch, writer turn and `OBJECTION`, fallback, events | golden dump unchanged for `llm`; seeded tests for each branch; the event contract above holds in every branch |
| J4 | Dashboard: every row of "UI impact", including the summariser's labels | dashboard tests; a finished jev-verdict run and a summariser run checked in a browser |

## Non-goals

- jev writing any text, or reading code.
- Removing or weakening the jev gate.
- jev in other seats (critics, summariser).

## Open points

- Thresholds and the J2 bar (proposed above).
- Independence: with `verdict: "jev"` the same model gives the verdict and checks the written decision. They judge
  different things (the round versus the decision text); `jev-preview` for one of the two is possible if J2 shows
  correlated errors.

## Implementation notes

### J1 shadow mode (built 2026-11-01)

`cli/llm-agent/jev_verdict.py` (questions, `JevVerdict.ask` to TypeSafe's System One API, `parse_answer`,
`compare`, `report`), the hook `DebateEngine._shadow_verdict` right after each LLM judge's `verdict` event,
`guardian.py --jev-verdict-report`, tests in `test_jev_verdict.py` (a fake jev in a real engine run). Details:

- **Direct API call, not jev-mcp.** The hub's jev-mcp (`owner/jev-mcp` 0.1.0, the gate's server) has no `jev_ask`; the
  first deployment (`998e7af`) recorded only errors. The engine now sends `POST <TYPESAFE_BASE_URL>/v1/systemone` itself
  (standard library), with the settings jev-mcp reads: `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` (default
  `https://api.typesafe.ai`), `TYPESAFE_DEFAULT_MODEL` (default `jev-latest`), from the environment or llm.env. Errors
  name the HTTP status, never the key. A check on the hub (2026-11-01): about 0.2 s per call; a critique with an
  unanswered `[차단]` gave `p_approve` 0.03 (reject), a clean one 0.41 (uncertain), so the thresholds need J2's data.
- **Answer shape.** System One returns `answers.<id>.noul` (the probability of yes) for a check and `score` with
  probabilities for a score (model `jev-1.14.0` on 2026-11-01, about 0.3 s for a short state). A missing `approve`
  probability is an error.
- **What jev reads.** `Task: <task for the agents>` and the judge material (the summary when the close reads
  condensed), at most 24 000 characters: a longer text keeps its head and tail with a marked cut (`clipped` in the
  record).
- **Events.** `jev_verdict` (`seat: "close"`, `mode: "shadow"`, `p_approve`, `verdict`, `p_blocking`, `evidence`,
  thresholds, `model`, `latency_ms`, `clipped`, `llm`, `agree`, `decided_by: "llm"`), or `{mode, error}` when jev failed;
  `verdict_disagreement` (`kind: "shadow"`) when jev is sure and differs. The `verdict` event is untouched. A canned
  (UNVERIFIED) judge answer is not compared.
- **Settings.** `GUARDIAN_JEV_VERDICT=shadow` turns it on (anything else is off; `on` is reserved for J3);
  `GUARDIAN_JEV_VERDICT_YES` / `_NO` (defaults 0.8 / 0.2; crossed values fall back). On the hub it is an operator key of
  `~/.guardian/agent.env`, which the installer keeps.
- **Report.** Reads `<data>/debate-jobs/*/events.jsonl`: verdicts, how many jev was sure of, agreement, the confusion
  table (jev approve / reject / uncertain against the LLM's approve / reject), per project kind, the jev-approves-LLM-
  rejects cases to read, and the J2 bar (30 verdicts, 90 % agreement).
- **Deployed (2026-11-01).** `998e7af` with `GUARDIAN_JEV_VERDICT=shadow` added to the hub's `agent.env` (the installer
  keeps it), then `14d8276` with the direct API call (no debate ran in between, so no error event was recorded). On the
  deployed release a verdict call answered in about 0.2 s (`jev-1.14.0`). J2 starts now: read the hub's numbers with
  `guardian.py --jev-verdict-report` (run with the service's environment, or the mode shows `off`).
- **Replay on earlier data and question set 2 (2026-11-01).** The 60 past LLM judge turns of the hub were replayed
  (`docs/analysis/2026-11-01-jev-verdict-replay/`). Set 1: 56 sure rejections, agreement 14 %. Set 2 at 0.8 / 0.2: 13
  sure approvals, all agreeing with the LLM, 3 sure rejections where the LLM approved, the 8 LLM rejections all
  uncertain. The shadow mode was switched to set 2; `p_blocking` became `p_fundamental`; the report counts only
  records of the current set. J3 waits for J2 on new shadow records.


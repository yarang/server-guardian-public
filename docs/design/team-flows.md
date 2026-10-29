# Team flows: structures other than propose, critique, judge (design)

English | [한국어](team-flows.ko.md)

Status: proposed 2026-10-22; approved 2026-10-23. Built: F0, F1, F2, F3 (see the implementation notes). Scope decided by the operator the same day: a closed list of verified flow templates, first
batch = parallel panel, sequential pipeline, pro/con versus, solo with verification.
Origin: "I want teams that are not limited to the propose, critique, judge structure."

## Problem

A team file only fills three seats (`expert`, `critic`, `judge`, plus the optional `proposers` of
`docs/design/multi-perspective.md`). The shape of a debate is not data: it is the round loop of
`DebateEngine._run_debate` (`debate_engine.py`): the Expert (and proposers) draft, the Critic panel reads the proposal,
the Judge decides, and a rejection starts the next round with the critique. Work that is not "one proposal attacked by
critics" (collecting several independent opinions, writing a document through successive hands, deciding between two
named options, checking a single author's output) has to be forced into that shape.

## Idea: the flow reshapes the middle, the close stays shared

Everything after the middle of a round is the same for every structure and carries the guarantees of this project:

```
 middle (varies by flow)           close (shared, unchanged)
 ------------------------          -----------------------------------------------------------
 produces `proposal` and           Judge writes decision / rationale / constraints / actions
 `critique` text and the           -> jev gate -> proposed actions (INVALID checks, STALE drift)
 transcript entries                -> auto-apply / plans / batch votes -> saved debate + events
```

A flow is a function from (task, history, members) to (`proposal`, `critique`, transcript entries). The Judge, the jev
gate, citation checks, `UNVERIFIED` on any canned fallback, the action catalogue and "irreversible work needs a person"
are not reimplemented per flow, so a new structure cannot weaken them. Consequences kept on purpose:

- **A Judge is mandatory in every flow.** There is no judge-less flow; a report-only team is `allowed_tools: []`.
- **Transcript roles stay `Domain Expert`, `Critic Agent`, `JEV-MCP Judge`.** A flow adds an optional `stage` (and
  `side`) field to entries and events; the dashboard, `jev`, the audit and old consumers keep working.
- **No flow, no change.** A team without `flow` is `debate`; its prompts and snapshot stay byte-identical (the
  precedent of `fleet-ops` in `fleet-provisioning.md`), so no prompt-version bump for existing teams.

## The flows (first batch)

All flows: `max_rounds` counts Judge rounds; in rounds 2+ the lead (`expert`) alone consolidates, as in
`multi-perspective.md`, so a rejection never replays a whole fan-out. The default `max_rounds` of the new flows is 1.

| `flow.kind` | Middle | Seats used | Fits |
|---|---|---|---|
| `debate` (default) | Expert drafts, Critic panel attacks | `expert`, `critic`, `proposers` | change review, design decisions |
| `panel` | 2-3 panelists answer blind in parallel; no critique; the Judge sees the answers under neutral rotated labels and writes the one decision | `expert` + `proposers` (1-2, the existing limit) | collecting opinions, exploration, naming, option lists |
| `pipeline` | stages run in order, each stage reads the previous output and rewrites or extends it (draft, enrich, edit); the last output is the proposal | `flow.stages` (2-4 members); `critic` optional | reports, documents, designs written through several hands |
| `versus` | two advocates defend opposite stances, then each answers the other once (`exchanges`, default 1); the Judge decides between them | `flow.sides` (exactly 2, each a member plus a `stance` sentence) | A/B technology choices, trade-offs |
| `verify` | the Expert writes alone; the verifier(s) check the result against a checklist and report only failed items with evidence, no counter-proposal | `expert`, `critic` as verifiers | cheap, low-risk work where attack is wasteful |

`verify` differs from `debate` in the verifier prompt (check, do not argue) and in one round; it reuses the critic seat.

Team file additions (all optional; absent means `debate`):

```json
{ "flow": { "kind": "versus", "exchanges": 1,
            "sides": [ {"persona": "ops_sre", "providers": ["claude"], "stance": "Keep the single hub."},
                       {"persona": "domain_architect", "providers": ["codex"], "stance": "Split into two hubs."} ] } }
{ "flow": { "kind": "pipeline",
            "stages": [ {"persona": "doc_drafter", "providers": ["claude"]}, {"persona": "doc_editor", "providers": ["agy"]} ] } }
```

Members of flows keep the member fields (`providers`, `host`, `code_tools`), so a stage or side can think on a worker
node (`remote-agents.md`). `expert` stays required (the lead who consolidates in rounds 2+).

## Decisions

1. **Closed list, not a graph.** Kinds are implemented and tested in the engine; a team picks one. A generic stage
   list or node graph would force every guarantee above to be generalised per stage and is not done now.
2. **The close is shared** (see above); this is also why the Judge is mandatory.
3. **Validation in `team_manager.py`**: unknown `kind`, wrong member counts, `stance` missing or longer than 400
   characters, a flow field on a team of another kind are `TeamValidationError`. Snapshots omit `flow` when it is
   `debate`.
4. **Ops and design projects** run `debate` and `verify` only (as for proposers today); other flows are refused with a
   clear message at start, not silently ignored.
5. **Events**: `debate_started.flow` (kind), `agent_started`/`agent_finished` carry `stage` (0-based) and, for `versus`,
   `side` (`A`/`B`). Added fields only; the event contract of `debate-agent-retrieval.md` stays valid.
6. **Fairness rules from `multi-perspective.md` apply**: blind answers get neutral labels rotated per round; `versus`
   sides get their stances from the team file, never from the model; the Judge sees the sides as A/B in a rotated order
   so position does not decide.

## Phases

- **F0 refactor, no behaviour change.** Cut the middle of the round loop into `_flow_debate()` (returns proposal,
  critique, providers, transcript entries); `_run_debate` dispatches by `lineup.flow_kind`. Existing tests and a
  byte-for-byte prompt comparison for all builtin teams are the gate.
- **F1 schema and cheap flows.** `team_manager.py` parsing, validation and snapshot; `verify` and `panel` (both reuse the
  drafting and critic paths). Tests with the fake LLM used by `test_debate_*`.
- **F2 `pipeline`.** Sequential stages, the stage chain in the prompts, fallback handling per stage.
- **F3 `versus`.** Stances, one exchange, rotated A/B order for the Judge.
- **F4 dashboard.** Team editor: flow picker with a form per kind, a one-line explanation, the lineup preview; debate
  result: stage and side badges in the transcript; `--teams` lists the flow. Strings in `ko` and `en`.
- **F5 evaluation.** Planted-defect and decision tasks per flow against `debate` (the harness of `proposer_eval.py`);
  needs the operator's OK for the shared Codex quota. Builtin example teams are added only after it.

## Non-goals

- Arbitrary graphs, loops that depend on model output other than the Judge's verdict, tool use inside a stage beyond
  today's retrieval.
- Judge-less flows, auto-approval without the jev gate.
- Changing how personas or providers are chosen.

## Open points

- Panel size for `panel`: 4 (1 + 3 proposers) is possible; the memory cost measured in `multi-perspective.md` (1.5-2.5x
  for three drafters) suggests starting with the existing limit (3 panelists) and raising it after F5.
- `versus` has a fixed `exchanges` (1, at most 2); the Judge cannot ask for a third.
- Cost labels in the team editor (calls per round per flow) are worth showing; the numbers come from F5.

## Implementation notes

- **F0** (`bdbed56`): a pure refactor. The middle of the round became `DebateEngine._flow_debate()` with `FlowContext`
  and `Middle`. The gate was a golden dump of every builtin team under five scenarios (plain, proposers, resumed, two
  fallback variants) with a deterministic fake LLM, recording every prompt, transcript entry, session and event: byte
  identical before and after.
- **F1**: `team_manager.py` parses `flow` (`debate`, `panel`, `verify`; an unknown kind or a field the kind does not
  take is a `TeamValidationError`); the snapshot carries `flow` only when it is not `debate`. Deviations from the design:
  - A panel is capped at one Judge round (`max_rounds` 2+ is refused): rounds 2+ would be the lead consolidating its own
    answer, which is a debate without critics. `verify` accepts more rounds; both default to 1.
  - A panel has no critic seat (`critic` optional and refused if given); a stored lineup overridden with `--critic`
    gets a warning and the critics are dropped.
  - `verify` takes no proposers. The verifier prompt (`VERIFY_TASK`) asks for failed items with evidence only; "All items
    pass." is the clean answer.
  - Ops and design projects run `debate` and `verify`; the panel is refused before any LLM call.
  - `RETRIEVAL_PROMPT_VERSION` `2026-10-23.p18` (p19 since F2). Existing teams are unchanged apart from that stamp (golden
    dump equal after rewriting the version).
  - `guardian.py --teams` prints the flow and omits an empty critic line. The Go handler passes team JSON through. The
    dashboard editor (`lib/debate.ts`) still drops `flow` when it saves a team: F4.
- **F2** (`pipeline`): `flow.stages` (2-4 members, only for this kind) is parsed into `Lineup.stages`; the snapshot carries
  `flow: {"kind": "pipeline", "stages": [...]}`. Stages count in the persona, provider, `host` and `code_tools` checks and in
  the `--teams` listing (`stages : a -> b -> c`). Engine: `_flow_pipeline()`; the critics' half of a middle moved to
  `_flow_critique()` (shared with `debate`, golden dump equal). Decisions and deviations:
  - A stage reads only the stage before it ("Work of stage N") and returns the whole result, not a diff. The last result
    is the proposal; transcript entries keep the role `Domain Expert` and carry `stage` (0-based); `agent_started` and
    `agent_finished` carry `stage` too (`EventWriter.emit` has a top-level optional `stage`).
  - Session keys are by position (`Domain Expert:stage{i}`), so one persona in two stages keeps two sessions.
  - The `critic` seat is optional. Without it the run is one round (`max_rounds` 2+ is refused, and a `--max-rounds`
    override is clamped with a warning) and the Judge reads the proposal alone. The `expert` seat is still required: it is
    the lead that rewrites in rounds 2+, so in a pipeline without a critic it is never asked.
  - Rounds 2+ (with a critic): the stages do not run again; the lead (`expert`) rewrites the previous proposal against the
    critique ("Proposal of the earlier round", carried in `draft_bundle`). That entry has no `stage`. A resumed thread
    runs the stages again in the first round of the new run, with the last critique in the prompt.
  - A stage that answers with the canned fallback ends the chain and skips the critics; the Judge's verdict is
    `UNVERIFIED` as everywhere.
  - A pipeline team with `proposers` is refused (the stages are the drafting); two adjacent stages preferring the same
    provider give a warning.
  - Ops and design projects refuse the pipeline before any LLM call, like the panel.
  - `RETRIEVAL_PROMPT_VERSION` `2026-10-23.p19`. Golden dump equal after rewriting the version. The dashboard editor
    still drops `flow` and `stages` on save: F4.
- **F3** (`versus`): `flow.sides` (exactly 2 members, each with a `stance` of at most 400 characters) and `flow.exchanges`
  (1-2, default 1) are parsed into `Lineup.sides` / `Lineup.exchanges`; `Member.stance` is in the snapshot only for sides,
  and `flow` carries `sides` and `exchanges` only for this kind. Sides count in the persona, provider, `host` and
  `code_tools` checks and in `--teams` (`side A: persona(providers)  stance`). Engine: `_flow_versus()`. Decisions and
  deviations:
  - **Calls**: both sides open in parallel and blind to each other; each exchange is simultaneous too (both answer the
    other's latest case, neither answers a reply it has already seen). Calls = 2 + 2 x `exchanges` + the Judge. The
    stance is repeated in every prompt, never taken from a model.
  - **One round.** `max_rounds` 2 or more is refused (a rejection would replay the same two cases; `flow.exchanges` is
    the way to a longer exchange), and a `--max-rounds` override is clamped with a warning. Because of that there is no
    "later round" branch: the `expert` seat is required by the schema but never asked, and a resumed thread runs the whole
    exchange again without the earlier Judge's reasons in the prompts (the Judge reads the history through its own
    session).
  - **No critic, no proposers**: both are refused by validation; a `--critic` override on a stored lineup is ignored with a
    warning.
  - **Judge view**: two neutral positions (`### Position A`, `### Position B`, each with its stance, opening case and
    answers) and no persona names. Which side is A is `versus_order(thread_id, round)`: a hash of the thread, flipped by the
    round number, so the order is the same on a replay and does not always favour the first side of the team file. The
    transcript entries carry `side` (`A`/`B`, team-file order), `exchange` (0 = opening) and `position` (what the Judge
    saw), so the dashboard can tell the two labellings apart.
  - **Sessions** are by position (`Domain Expert:side{i}`), so one persona on both sides keeps two sessions and is allowed;
    two sides preferring the same provider give a warning.
  - **Canned fallback**: a side that answers with the canned provider ends the exchange early; the Judge's verdict is
    `UNVERIFIED` as everywhere.
  - **Events**: `agent_started`/`agent_finished` carry a top-level `side` (`EventWriter.emit(side=)`);
    `debate_started` lists `sides` with stance, agent key and providers.
  - Ops and design projects refuse versus before any LLM call, like the panel and the pipeline.
  - `RETRIEVAL_PROMPT_VERSION` `2026-10-23.p20`. Golden dump of every builtin team equal after rewriting the version. The
    dashboard editor still drops `flow`, `stages` and `sides` on save: F4.

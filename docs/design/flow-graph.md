# Flow graphs: seating characters on a diagram (design)

English | [한국어](flow-graph.ko.md)

Status: decided 2026-11-01. Decisions D1-D8 and the terms were made with the operator in conversation on 2026-11-01.
Nothing is built. The editor library was chosen the same day (D9). This design revises decision 1 and the non-goal "arbitrary
graphs" of [team-flows.md](team-flows.md) and the sentence "not an arbitrary graph" of
[flow-templates.md](flow-templates.md), and replaces the team and template layers (see "What changes in earlier
decisions").

## Problem

Making a work flow today goes through five layers, each edited in its own place:

```
character (persona)  ->  team (who sits in which seat)  ->  team flow kind (debate, panel, verify, pipeline, versus)
                     ->  flow template (steps, parameters, task text)  ->  start form (parameter values)
```

The flow kinds tab (T3b) shows this directly. A person picks a "shape" (one existing flow kind, or custom steps), then
steps whose seats are implied ("draft is the lead, blind draft is the lead and every proposer, critique is every
critic"), then a team in another field, and only then does the preview show who actually runs. Three things make it
feel complicated:

1. **The order is not visible.** A flow is a list of step names; who reads whom is implied by the step kind and the team.
2. **What is passed is not visible and cannot be chosen.** Each step hard-codes it: blind drafts under rotated labels,
   a pipeline passes the chain of earlier results verbatim, critiques reach the Judge concatenated verbatim, earlier
   rounds are condensed above 16 KiB (`thread_digest.py`).
3. **Words name widgets, not meanings.** The parameter types "한 줄 글" (`string`) and "여러 줄 글" (`text`) differ only
   in the input box, newlines and the length limit; both are pasted into the task text through `${NAME}`. Nothing says
   when to use which.

## Goal

The operator builds a flow by seating characters on a canvas and linking them. Placement and links are the whole
order; how a seat receives earlier work is a setting of that seat. A flow describes a **way of working** (write,
review in parallel, condense, decide), not one task, so the same flow serves many tasks and projects.

## Terms

"Node" already means a fleet machine (`docs/glossary.md`), "node group" a set of machines, and "gate" the jev and batch
gates. The diagram uses these words:

| Korean | English and code | Meaning |
|---|---|---|
| 흐름 | flow | the diagram: seats, links, parameters, task text |
| 자리 | seat (`seats`) | a place on the diagram where a character does one job |
| 캐릭터 | character | who sits in a seat ([agent-characters.md](agent-characters.md)) |
| 맡은 일 | job | what a seat does: 작성 write, 비평 critique, 다시 쓰기 rewrite, 반론 rebut, 정리 summarise |
| 동시 묶음 | bundle (`bundles`) | seats that run in parallel |
| 마감 | close (`close`) | the shared close: Judge, jev gate, action checks; one per flow |
| 시작, 끝 | start, end | fixed ends of the diagram |
| 연결 | link (`links`) | "this seat reads that one" |
| 되돌아보기 연결 | look-back link | a link that reads the previous round |
| 거부 시 돌아갈 곳 | rejected exit | where the next round starts after REJECTED |
| 라운드 | round | one run from the start (or the rejected exit's target) to the close |
| 받는 방식 | input mode | 원문 raw, 요약 condensed, 핵심만 structured, 최신만 latest |
| 정리자 | summariser | a seat with the job summarise |

A character file's `seats` field (expert, critic, judge, ...) lists the jobs the character may take; the dashboard
shows it as "맡을 수 있는 일". The file key stays for compatibility.

## Decisions (2026-11-01)

| # | Decision |
|---|---|
| D1 | **Generic seats plus one close.** There are no expert or critic seat types. The only special element is the **close**: today's shared close (Judge, jev gate, action validation, auto-apply, plans and votes, `UNVERIFIED`), and the only place that may propose actions. |
| D2 | **The input mode lives on the receiving seat.** One mode per seat (raw, condensed, structured, latest). A per-link setting exists only as the exception `blocked` (writers that must not see each other). |
| D3 | **Condensing is a visible summariser seat that is task independent.** It works on the shape of the input (the common output envelope) and on the purpose of the one seat it feeds, never on the content of a particular task. A custom instruction is an optional override. Choosing condensed on a seat inserts a summariser in front of it. |
| D4 | **The close reads raw by default.** Condensed is allowed on the close with a warning, so critiques still reach the Judge verbatim unless the operator changes it for one flow. |
| D5 | **The first delivery includes the editor.** Built in phases, deployed once. |
| D6 | **A seat is a character plus a job.** The job comes from a closed list; it selects the engine's role card ([agent-characters.md](agent-characters.md) section 4) and the step piece that runs it. Seats stay generic; what they do stays an engine-tested piece. Terms as above (D6a). |
| D7 | **A flow replaces team plus template; its characters can be swapped at start.** Every seat on the diagram holds a default character. The start form may replace the character of any seat with another whose jobs allow it; the replacement is stored in the thread snapshot. The team layer goes away: existing teams and templates convert into flows, and a project's `"team"` key names a flow. |
| D8 | **One text parameter type.** The editor offers 글 (text), 숫자 (number) and 목록에서 선택 (choice). The box follows the maximum length: up to 200 characters a one-line box without newlines (stored as `string`), above that a multi-line box (stored as `text`). Files and the engine keep the four raw types; existing files load unchanged. |
| D9 | **The editor uses `@xyflow/react`** (pan, zoom, handles, edge routing, keyboard access), pinned to an exact version like the other web dependencies; the version is picked in G4. A hand-written SVG editor was not taken. |

## Model

### Elements

| Element | Count | Does |
|---|---|---|
| start | 1 | Holds the task text, the parameters and the default code context. No model call |
| seat | 1 or more | One character with one job, providers, code tools and an optional one-sentence focus. One model call per round |
| bundle | 0 or more | A frame whose seats run in parallel. Its output is what its seats wrote in the current round, under neutral labels (`A`, `B`, ...) rotated per round, as blind drafts are today |
| close | exactly 1 | The shared close. Its character sits in the judge seat; `allowed_tools` as today, narrowed by the project. Two exits: approved and rejected |
| end | 1 | The result and the recorded actions |

Jobs and the existing pieces they reuse:

| Job | Step piece today | Role card |
|---|---|---|
| 작성 write | `_step_draft` (`draft`; in a bundle `blind_draft`) | expert |
| 비평 critique (objection or checklist) | `_step_critique` | critic or verifier |
| 다시 쓰기 rewrite | `_step_rewrite` (pipeline stage, lead after a rejection) | stage |
| 반론 rebut | `_step_exchange` (versus) | side |
| 정리 summarise | new (see "Summariser") | none; the instruction is generated |

A seat with the job rebut carries a stance that comes from a parameter or a fixed sentence, never from a model
(decision 4 of `flow-templates.md` stays). A summariser seat needs no character.

### Links

- **Link** (solid): `A -> B` means B reads A's output. Forward links must be acyclic; they give the order.
- **Look-back link** (dashed): a link that points backwards. B reads A's output from the **previous round**. The writer
  reading the last round's critiques is a look-back link.
- **Rejected exit**: where the next round starts; only seats reachable from it run again. It carries `max_rounds`.
  "Rounds 2+ go to the lead alone" is a rejected exit to the lead.
- **Approved exit**: to the end.

### Validation

Python stays the single validator (`flow_graph.py`, new); Go forwards, and the dashboard mirrors the checks to name the
first problem early, as for templates.

- One start, one close, one end; every seat reaches the close; both exits of the close are linked.
- Forward links acyclic; `max_rounds` 1-10; no branch other than the close's verdict (non-goal of `team-flows.md` kept).
- A bundle holds 1-4 seats; parallel calls bounded by `GUARDIAN_MAX_PARALLEL_AGENTS`; at most 12 seats and a cap on
  model calls per round, refused at save.
- A summariser feeds exactly one seat (or the close).
- A character's jobs must allow the seat's job (the C2 seat check), also for a character swapped in at start.
- Ops and design projects: tools narrowed by the project, design never auto-applies; which flows they may run is
  decided per flow after its tests, as for templates.

## Common output envelope

Every seat except the close answers in the same shape. Content differs per task, the shape does not, so later seats
can be defined without knowing the task.

```
## Conclusion        one or two sentences
## Claims            - one item per claim
## Evidence          - path:line-line  quoted lines   (kept verbatim by summarisers)
## Open issues       - what is unresolved or needs a decision
## Recommendation    optional
```

To keep converted teams and templates byte-identical (the golden gate of T2), the envelope rules are added only to
flows that use condensed or structured somewhere. Those flows get a new prompt version (`RETRIEVAL_PROMPT_VERSION`
bump). An answer that misses the headings gets one `format_retry`; after that it is passed on as is and marked
`envelope: false`, and the receiving seat gets it raw.

## Input modes (on the receiving seat)

| Mode | The seat receives | Extra calls |
|---|---|---|
| 원문 raw (default) | The full outputs of the seats it reads, as today | none |
| 요약 condensed | The output of the summariser in front of it | one per round |
| 핵심만 structured | Only `Conclusion`, `Claims`, `Evidence`, `Open issues` of each input, cut by rule | none |
| 최신만 latest | Like raw, but look-back links deliver only the last round | none |

`blocked` on a link removes that input. Each seat also has a **history** setting for earlier rounds: digest (default,
today's thread digest), full or none; it matters for fresh CLI sessions, as `_format_history` does today.

Selecting a seat in the editor shows what it will receive: the seats it reads, their mode and the size of the last
run's text.

## Summariser

A summariser does not know its input's content and does not need to: it knows the input's **shape** (the envelope) and
the **purpose** of the seat it feeds. Its instruction is generated from the flow:

```
You condense work for the next step. The next step's task is:
  «job, character summary and focus of the receiving seat»
Keep what that task needs. For each input:
- Conclusion and Claims: compress, merge duplicates across inputs, keep who said what (label).
- Evidence: copy every citation line unchanged; drop none that supports a kept claim.
- Open issues: keep all of them.
Drop reasoning steps, repetitions and politeness. Use the same envelope headings.
```

The same flow therefore works for a security review, an ops check or a document review; when the receiving seat
changes, the condensing follows.

Deterministic checks:

- Every citation in the summary must appear verbatim in one of its inputs; otherwise the summary is dropped and the
  receiving seat gets raw for that round (event `summary_rejected`).
- A seat that saw only condensed or structured input did not see the cited lines itself: citations it repeats from
  the summary are weak; its own new citations follow the line-span rule as today.

## Execution

- `flow_graph.py` turns a flow into the step lists the T2 interpreter (`_flow_steps`) already runs, plus input-mode and
  summariser steps. A flow converted from a team flow kind or a template must give the same step lists that `_steps_of`
  and `engine_steps` give today (golden dump unchanged).
- Transcript entries and agent events gain `seat` (the seat id) next to `step`, `stage` and `side`; roles stay
  `Domain Expert`, `Critic Agent`, `JEV-MCP Judge`, so reports, `jev` and the audit read old and new threads alike.
- Session key `(thread_id, role)` stays; seats of new flows use `role = "seat:<id>"`.
- `UNVERIFIED` as today: a canned answer from any seat that feeds the close makes the round `UNVERIFIED`.
- The thread snapshot stores the flow id, a hash of its text, the characters actually seated (after swaps) and the
  parameter values; editing a flow later does not change a started thread.

## Storage and conversion

Builtin `configs/flows/*.json`, the operator's own `<GUARDIAN_DATA_DIR>/flows/*.json`; a custom file shadows a builtin
one; the server stamps `created` and `modified` (the rules of teams and templates).

```json
{
  "id": "review-two-stage",
  "name": "Two-stage review",
  "category": "development",
  "projects": ["code"],
  "params": {"TARGET": {"type": "string", "label": "Target", "max": 200}},
  "task": "Review ${TARGET}.",
  "seats": [
    {"id": "lead", "job": "write", "character": "guardian_lead_dev", "providers": ["claude"]},
    {"id": "sec", "job": "critique", "character": "guardian_security_critic", "providers": ["codex"]},
    {"id": "qa", "job": "critique", "mode": "checklist", "character": "guardian_qa_critic", "providers": ["agy"]},
    {"id": "sum", "job": "summarise", "providers": ["claude"]}
  ],
  "bundles": [{"id": "review", "seats": ["sec", "qa"]}],
  "close": {"character": "guardian_dev_judge", "input": "condensed", "allowed_tools": ["jev_mcp:run_tests"]},
  "links": [
    {"from": "start", "to": "lead"},
    {"from": "review", "to": "lead", "look_back": true},
    {"from": "lead", "to": "review"},
    {"from": "review", "to": "sum"},
    {"from": "sum", "to": "close"},
    {"from": "close", "to": "lead", "exit": "rejected", "max_rounds": 3},
    {"from": "close", "to": "end", "exit": "approved"}
  ],
  "layout": {"lead": [240, 80], "review": [480, 40]}
}
```

`start`, `close` and `end` are fixed ids. `layout` holds canvas positions only and never changes execution.

Conversion on load (`team_to_flow`, `template_to_flow`), so nothing existing breaks:

| Today | Flow |
|---|---|
| `debate` | start -> lead (write) [+ bundle with proposers, blocked from each other] -> bundle of critics -> close; look-back critics -> lead; rejected exit to the lead |
| `verify` | as `debate`, critics with the checklist mode |
| `panel` | start -> bundle (lead + proposers, write) -> close; one round |
| `pipeline` | start -> stage 1 (write) -> stage 2 (rewrite) -> ... -> [critics] -> close; rejected exit to the lead (rewrite) |
| `versus` | start -> bundle (side A, side B, write, stances) -> side A and B (rebut, reading each other) -> close |
| template | the converted team's flow with the template's steps, parameters and task |

Teams and templates stay readable so old threads and the project `"team"` key keep working; the dashboard edits only
flows after G4.

## Editor

Workflows -> "Flow kinds" becomes the flow editor (`#/debates/templates` keeps resolving).

- **Gallery** as today (cards by category), with a small rendering of the flow on each card.
- **Canvas**: palette (seat, bundle, summariser; start, close and end always present); drag to place, drag from a
  handle to link; a backward link becomes a look-back link; the close has one handle per exit.
- **Seat panel**: character (picker with portrait and tagline, filtered by the job), job, providers, code tools,
  focus, input mode, history; stance for rebut. The close panel: character, input mode, tools.
- **Start panel**: parameters (text, number, choice; D8) and task text, with the checks of the template editor today.
- **Preview**: calls per round and, for the selected seat, what it receives.
- **Validation** on every change, marks on seats and links; saving is refused while errors remain.
- **Start form**: pick a flow, fill the parameters, optionally swap characters per seat (D7), choose rounds.
- **Read view**: the debate result and the Agents monitor draw the same flow with the run's seats coloured by state.
- Narrow screens get the read view and a list editor (seats and what they read, as a form).
- Strings go into both catalogs (`ko`, `en`); the new terms go into `docs/glossary.md`.

Library: `@xyflow/react` (D9). Seats, bundles, the close, start and end are custom node components; look-back links
and the two exits of the close are custom edge types. The read view uses the same components without editing.

## Phases (one deployment at the end)

| Phase | Content | Done when |
|---|---|---|
| G1 | Flow schema, validation, `team_to_flow`, `template_to_flow`, store, CLI (`--flows`, `--flow-save`, `--flow-delete`) | Every builtin and hub team and template converts and validates |
| G2 | Flow -> step lists, swaps at start, `seat` on entries and events | Golden dump unchanged for all converted cases |
| G3 | Envelope, input modes, summariser with citation check, `summary_rejected` | Seeded test: a summary that invents a citation is dropped; prompt version bumped for envelope flows only |
| G4 | Go API (`/api/flows`), start form, editor, read view, D8 parameter types, glossary | Dashboard tests; a flow built in the editor runs end to end in a sandbox |
| G5 | Evaluation in `~/guardian-eval`: a converted builtin team against a two-stage flow with a summariser | Recorded under `docs/analysis/`; operator's OK first (shared Codex quota) |

## What changes in earlier decisions

- `team-flows.md` decision 1 and its non-goal "arbitrary graphs": a flow is now a graph of seats. What stays: the
  shared close, the mandatory Judge, no branching on model output, loops only through the Judge's verdict, no tools
  inside a step beyond retrieval.
- `flow-templates.md` and `debate-teams.md`: teams and templates become inputs that convert into flows (D7). Decisions 3
  and 4 of `flow-templates.md` (shared close, stances from people) stay.
- `debate-teams.md` rule "critic panel statements are concatenated verbatim": still the default (D4); an operator may
  choose condensed for one flow, with a warning.

## Non-goals

- Conditions other than the close's verdict; loops other than the rejected exit.
- Tools or actions anywhere but the close.
- Team-versus-team debates; running seats on hosts other than those a member's `host` already allows.

## Open points

- Exact envelope wording and whether structured is needed in the first delivery.

## Implementation notes

### G1 flow file, validation, conversion, store (built 2026-11-01)

`cli/llm-agent/flow_graph.py`, tests in `test_flow_graph.py`, CLI `--flows`, `--flow-save` (JSON on stdin),
`--flow-delete ID` (`config_ops.flows_report`, `save_flow`, `delete_flow`), and a `scripts/ci.sh config` check that
every builtin team and template converts into a flow that validates. Nothing runs flows yet (G2); the hub API is G4.
Deviations and details:

- **Ids.** Flow ids follow the team id pattern (1-64 lowercase letters, digits, `-`, `_`), because converted teams keep
  their ids; seat and bundle ids are 1-32 of the same characters and never `start`, `close` or `end`.
- **Jobs and the character check.** A format 2 character must list one engine seat of its job: write -> expert,
  proposer or stage; rewrite -> stage or expert; critique -> critic (objection) or verifier (checklist); rebut -> side;
  the close -> judge. Format 1 personas have no seat list and pass. A character may sit in several seats (sessions will
  be keyed by seat).
- **Rebut** seats exist only as one bundle of exactly two; the bundle carries `exchanges` (1-2), as a versus team does.
- **Bundle members** may be linked one by one (the rejected exit of a converted debate goes to the lead inside the
  `drafts` bundle); members of one bundle never read each other.
- **Optional fields.** `projects` absent means any project kind; `task` absent means the person writes the task at
  start (a converted team has none). `context` uses the team rule.
- **Limits.** 12 seats, 4 per bundle, 16 model calls per round (`calls_per_round`: every seat once, two more per
  rebut answer, the close once).
- **Summariser rules** in G1 are structural only: it feeds exactly one seat or the close, condensed input needs one in
  front, a summariser feeding a seat that does not read condensed is a warning. Its instruction and the citation check
  are G3.
- **Conversion.** `team_to_flow` and `template_to_flow` follow `_steps_of` and `engine_steps`: the lead (with the
  proposers in a `drafts` bundle when the team has them), the critics (`critics` bundle for a panel), look-back links from
  the critics (and the drafts) to the lead, the rejected exit to the lead; a panel or a custom template without a
  critique goes straight to the close with `max_rounds` 1; a pipeline's lead rewrites only after a rejection (it is
  not linked from the start); a versus' sides form the `sides` bundle. A converted versus drops the team's `expert`,
  which never runs in a one-round versus; `converted_from` keeps the team (and template) id, so G2 can resolve the
  exact lineup.
- **Report.** `--flows` lists stored flows, templates and teams as flows (sources `builtin`, `custom`, `template`,
  `team`) with `hash`, `calls`, `errors` and `warnings`; a stored flow shadows a template or team of the same id and a
  template shadows a team, each with a warning.
- **Hub check (2026-11-01, read-only copy of the hub's `teams`, `flow-templates` and `personas`, deleted afterwards).**
  All 13 entries convert and validate: 8 builtin teams, 5 custom teams (one overrides a builtin) and the template
  `code-review`. The only warnings are the existing high-risk tool notes.

### G2 running a flow (built 2026-11-01)

`flow_graph.compile_flow`, `swap_characters`, `resolve_run`, `find`; engine `run_debate(flow_run=...)`; CLI
`--debate ... --flow ID [--seat SEAT=CHARACTER] [--param KEY=VALUE]`. The gate is
`test_flow_golden.test_every_case_runs_the_same_from_its_flow`: every one of the 37 golden cases, started from its team
converted into a flow and compiled back, gives the same dump as the team run, apart from the fields a flow adds.
Deviations and details:

- **Compiled, not interpreted.** A flow compiles to the lineup (and, where the five kinds have no name, the custom steps
  of T3a) that the T2 interpreter already runs. Recognised: the versus bundle; a pipeline (2-4 stages, the lead after a
  rejection, optional critics with objections); the lead alone or with proposers in a bundle, optional critics in one
  mode (debate, verify, panel, `draft` alone, `blind_draft` + checklist). Any other shape is refused by name ("does not
  run yet"), as are the G3 features (summarisers, input modes other than raw, history settings, blocked links, focus).
- **Session keys stay those of the lineup** (lead, persona-keyed panel members, stage and side positions), not
  `seat:<id>` as planned: continuations and the golden dump stay as they are. So one character twice in a drafts or
  critics bundle is refused at run time.
- **A versus has no lead seat**; the compiled lineup seats side A's character as the stand-in lead (it never runs).
  Only the lineup snapshot and the member list of `debate_started` name it, so the gate ignores those two for versus.
- **Seats on the record.** Members carry `seat` (`close` for the Judge); transcript entries and agent events get a
  top-level `seat` (like `step`); the lineup snapshot gets `flow_graph` (id, hash, parameter values, the characters
  seated, the custom steps if any) and `debate_started` names it. Team and template runs are unchanged.
- **Starts.** `--flow` finds a stored flow, else a template, else a team (converted, the report's precedence). A flow
  with a task text takes `--debate -` and `--param`; one without takes the task. `--seat` swaps characters (D7; the
  character must take the seat's job). A flow starts a new thread; `--team`, `--flow-template` and the per-role
  overrides are refused with it. A continued thread keeps its `flow_graph` (custom steps checked again).
- **Not done:** the hub API and the project `"team"` key naming a flow (G4).

### G3 envelope, input modes, summariser (built 2026-11-01)

`debate_engine.py`: `ENVELOPE_RULE`, `keep_sections`, `invented_citations`, `_close_material` (the summariser),
`_carried_critique`; `flow_graph.compile_flow` returns `Compiled(team, steps, condense)` and the flow record carries
`condense` (summariser, close_input, lead_input, envelope). Prompt version `p27`. Tests: `test_flow_graph.py`
(`G3CompileTests`, `EnvelopeTextTests`, `G3RunTests` with the seeded summary that invents a citation). Deviations:

- **Where it runs.** One summariser, between the unit that feeds the close and the close (which reads condensed).
  The close reads raw, condensed or structured; the lead reads raw or structured (structured applies to the critique
  it carries into the next round, not to the drafts). Other seats' input modes, `latest`, history settings, blocked
  links and seat focus are refused as "not built yet".
- **The summariser** is asked once per round with the generated instruction (or its own `instruction`), the Judge's
  material and no thread history; session key `Summariser:<seat>`, transcript role `Summariser` with its `seat`. Its
  summary replaces the material, under an intro that tells the Judge it did not see the cited lines.
- **Citation check.** A summary is dropped, the Judge reads the material as it is, and `summary_rejected` is emitted
  (reason, the citations) when a citation in it (path:line, path:first-last, path#Symbol, an ops label `[#...]`) does
  not appear verbatim in the material, or when the summariser answered with the canned fallback or nothing. A dropped
  summary does not make the round `UNVERIFIED`. A range written differently from the original counts as invented
  (the safe direction). The Judge's own citations need no new rule: lines it was not shown already fail the line-span
  check.
- **Envelope.** `ENVELOPE_RULE` joins the rules of every seat but the Judge in a flow that condenses or reads
  structured input; every other prompt is unchanged (the golden dump holds; only the version stamp moved to `p27`). No
  `format_retry` for missing headings: a text without `## Conclusion` passes a structured input whole.

### G4 hub API, editor, start form, read view (built 2026-11-01)

Go: `GET/POST /api/flows`, `DELETE /api/flows/{id}` relay `--flows`, `--flow-save`, `--flow-delete`; `POST /api/debates`
takes `flow` and `seats` (a flow refuses `team`, `template` and `thread_id`; the task is optional; params as for
templates) and the job echoes `flow` and `seats`. Python: report entries carry `runnable` and `run_error` (whether
`compile_flow` accepts the flow). Dashboard (`pages/flows/`, `lib/flowGraph.ts`, catalogs `i18n/{ko,en}/flows.ts`):
`@xyflow/react` 12.12.0 pinned (installed with `--ignore-scripts`). Deviations:

- **The template editor is gone.** The "Flow kinds" tab lists flows (stored ones, templates and teams converted) and
  edits only flows (D7); `TemplatesTab`, `TemplateDialog`, `flowTemplateEditor.ts`, the `flowTemplates` catalog and their
  tests were removed. `/api/flow-templates` and the CLI stay for the hub.
- **Editor** is a wide dialog: palette (one button per job and the summariser, "Arrange"), canvas (drag to place, drag a
  handle onto another element to link; a link that would close a loop of forward links becomes a look-back link; the
  close has an approved and a rejected handle), and the panel of the selection (flow, seat, bundle, close, link). Bundle
  membership is chosen in the seat panel, not by dragging into a frame; members are placed inside their bundle. History
  and focus are not offered (they do not run yet). Client checks name the common problems on the elements; the hub
  validates on save. A builtin flow, a team or a template is saved as a custom flow under the same id, which shadows it.
- **Gallery** rows show the shape as text (`작성 → 비평 ×2 → 정리 → 마감`), the seat and call counts and "does not run yet"
  with the reason, instead of a small diagram.
- **Start form**: the flow picker replaces the template picker and offers valid, runnable flows of the project's kind
  (or flows without `projects`). A flow with its own task text asks its parameters (D8 types), one without asks the
  task; the team select is shown only for a free task; characters can be swapped per seat in a disclosure, filtered by
  the seat's job.
- **Read view**: a "Flow" disclosure on the debate result draws the flow read-only once opened (seats that ran in
  green, a canned answer as failed, a notice when the flow changed since the run). The Agents monitor is unchanged.
- **Narrow screens** get the seats and links as a list plus the panel instead of the canvas.
- **Glossary**: 흐름, 자리, 맡은 일, 동시 묶음, 마감, 연결, 받는 방식, 정리자 added to `docs/glossary.md` (both languages).
- **Real-browser check (2026-11-01, a local hub built from this tree with a scratch data dir).** The gallery listed every
  builtin team as a flow; on `cross-review`: the diagram, a summariser added, linked critics -> summariser -> close by
  dragging, the close set to condensed, saved (a custom flow shadowing the team, with the D4 warning); the start form
  offered it with the task field and one character choice per seat. Found and fixed: a new seat was drawn on top of
  the start (xyflow's size measurement wrote a stale position back) and the read view was fitted while collapsed. A test
  debate started from the flow called the Mac's installed `claude` (the team names its providers) and was cancelled
  after about 20 seconds. Not checked: a finished run in the read view.
- **Open:** a project's `"team"` key still names a team, not a stored flow; the Agents monitor graph.

### Deployment (2026-11-01, `74b906d`)

G1-G4 and the G5 harness were deployed together as `74b906d` (release `74b906d-20261101061058`) through the gate
(`scripts/ci.sh`, push, `install-agent-service.sh`); no production debate was running. After the install
`GET /api/flows` on the hub listed 13 entries (12 teams, the template `code-review`), all converted, runnable and
without errors; the hub has no stored flow yet. The dashboard was not opened in a browser on the hub (the real-browser
check ran on a local hub, G4). Unrelated to this change, the installer reported that the project `design_house`
cannot be cloned (its repository does not exist).

### G5 evaluation (2026-11-01)

`flow_graph_eval.py` and `docs/analysis/2026-11-01-flow-graph-eval/` (results, `run-eval.sh`, `metrics.json`): G0 (the
flow evaluation's F0 team as a flow), G1 (a summariser before the close, close condensed), G2 (close structured) on the
hard tasks h1-h3, one repetition, automatic pre-grade. Recall over the decision alone: G0 0.89, G1 0.94 (+35 % wall
time, one more call), G2 0.92 (+7 %); both keep recall, the differences are one or two defects. The first G1 attempt
dropped every summary: the summariser read the repository and cited lines the work did not hold. Fixed in `9eb0b90`
(prompt `p28`): the summariser's turn has no repository tools, server checks or code context. D4 stays (the close reads
raw by default); no builtin flow is added; repetitions and the human grade come before an example flow. The fix was
deployed 2026-11-01 as `fbab40a` (hub prompt `p28`; `/api/flows` 13 entries, all runnable, no errors).

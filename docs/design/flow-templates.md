# Flow templates: stored, parameterised work flows (design)

English | [한국어](flow-templates.ko.md)

Status: proposed 2026-10-29; the decisions listed under "Decisions" were taken by the operator the same day. Nothing
built (phases T1-T4).
Origin: "If we go with flows, design flow templates so they can be operated." The dashboard menu is called "Workflows"
(Korean 업무 흐름); `debate` becomes one flow kind among several (`docs/design/team-flows.md`).

## Problem

A flow is today one of five kinds (`debate`, `panel`, `verify`, `pipeline`, `versus`) written in a team file and
implemented as one function each in `debate_engine.py`. That has three limits:

- A person who starts work cannot choose a kind and fill in what that kind needs (the two positions of a `versus`, the
  question of a `verify`). The start form only picks a team; the stance texts live in the team file.
- An operator cannot keep, copy or review a flow as a unit. The team file mixes who sits where (lineup) with how the
  work is shaped (flow) and what is asked (task).
- A new shape means new engine code, although most shapes are the same few pieces in another order.

## Model

```
step kinds (code, closed list)  ->  template (data)  ->  team (fills the seats)  ->  start (parameter values)
```

- A **step kind** is a piece the engine implements and tests. A template holds no code, only the order of pieces.
- A **template** defines the shape of the middle of a round, the start form, the task text and the project kinds it may
  run on. It names a default team.
- A **team** keeps filling the seats exactly as today (`expert`, `proposers`, `critic`, `judge`, `flow.stages`,
  `flow.sides`). A template refers to a team by id; it never copies a lineup.
- A team without a template keeps working: its `flow` value is an implicit template without parameters, and no `flow`
  means `debate`. Golden dumps and snapshots of existing threads do not change.

The close of a round stays shared and is not part of a template: the Judge (mandatory), the jev gate, action
validation, auto-apply, plans and votes, and `UNVERIFIED` on any canned fallback (`team-flows.md`, "Problem").

## Step kinds

| Step | What it does | Fairness rule |
|---|---|---|
| `draft` | one seat writes | - |
| `blind_draft` | several seats write without seeing each other | the Judge and later steps see neutral labels, rotated per round |
| `critique` | critics object (`mode: object`) or list failed checklist items (`mode: checklist`) | in `checklist` mode "All items pass." is the clean answer |
| `rewrite` | the next seat rewrites the previous result | the chain of earlier results goes into the prompt as it is |
| `exchange` | two sides answer simultaneously, 1-2 times | stances come from a person, never from the model; the Judge sees A/B in a hash-rotated order |
| `consolidate` | the lead merges into one proposal | same as the default of rounds 2+ |

The five existing kinds are combinations: `verify` = `draft` + `critique(checklist)`; `panel` = `blind_draft`;
`pipeline` = `draft` + `rewrite` x N; `versus` = `blind_draft` (two sides) + `exchange`; `debate` = optional
`blind_draft` + `critique(object)` + `consolidate`.

Not added, because they were non-goals of `team-flows.md` and stay so: branching on model output, loops other than the
Judge's verdict, tool use inside a step beyond today's retrieval, flows without a Judge, approval without the jev gate.
"New flow kinds" means sequential and parallel combinations of these pieces, not an arbitrary graph.

## Template file

Builtin: `configs/flow-templates/*.json` (data of the repository, checked by `scripts/ci.sh config`). Operator's own:
`<GUARDIAN_DATA_DIR>/flow-templates/*.json`. A custom file with a builtin id shadows it; deleting the custom file shows
the builtin again (the rule of `TeamManager`). The server stamps `created` and `modified`.

```json
{
  "id": "design-decision-versus",
  "name": "Design decision (two positions)",
  "category": "development",
  "projects": ["code"],
  "team": "guardian-dev",
  "steps": [
    {"kind": "blind_draft", "seats": ["side:A", "side:B"]},
    {"kind": "exchange", "seats": ["side:A", "side:B"], "count": 1}
  ],
  "params": {
    "TOPIC":    {"type": "text",   "label": "Question",   "max": 2000},
    "STANCE_A": {"type": "string", "label": "Position A", "max": 400},
    "STANCE_B": {"type": "string", "label": "Position B", "max": 400}
  },
  "task": "Decide: ${TOPIC}",
  "stances": {"side:A": "${STANCE_A}", "side:B": "${STANCE_B}"},
  "max_rounds": 1,
  "reviewed": "2026-10-29"
}
```

- `id` equals the file name, `[a-z0-9-]{1,40}`. `category` is one of the team categories.
- Seat names point at team slots: `expert`, `proposer:1-2`, `critic:1-4`, `stage:1-4`, `side:A|B`. The Judge is never a
  step seat; it always runs in the close.
- `team` is the default team. The person may pick another at start; the seat check runs again for it.
- `params` use the shape of `command_sets.py` (`type`, `max`, `values`, `default`) plus a `label`. Types: `string`,
  `text`, `int`, `enum`. A value goes into the task text and the stances only and never reaches a shell. Placeholders
  are uppercase `${NAME}`.
- A stance is either a parameter filled in by the person or a fixed sentence of the template, at most 400 characters
  after rendering. Nothing a model wrote can become a stance.
- `reviewed` older than 180 days is a CI warning, as for command sets.

## Validation

Single source is Python (`flow_templates.py`, new), reusing `team_manager.validate_lineup`; Go forwards and does not
validate (the rule of `debate-teams-api.md`).

- 1-6 steps, at most 4 seats in a step, at most 6 seats in all, and a cap on model calls per round; above it the
  template is refused at save, not at run.
- Every seat a step uses exists in the chosen team; members no step uses are a warning. Personas stay unique where the
  session key needs it.
- At least one step produces a proposal (`draft`, `blind_draft`, `rewrite`, `consolidate`, or `exchange` read by the
  lead), and only a `critique` may follow the last of them; otherwise refused. (The first wording, "the last step must
  produce a proposal", would forbid every debate, whose last step is the critique.)
- A project kind not in `projects` is refused at start with a clear message, never ignored. Ops and design projects get
  a template only after its combination passed its tests, and it says so in `projects`; today they run `debate` and
  `verify` only.
- What the thread stores in `threads.team_snapshot`: the template id, the rendered steps, the parameter values and a
  hash of the template text. Editing a template later does not change a started thread.

## Engine

- `_flow_steps(ctx, ...)` returns a `Middle` like the other `_flow_*` functions and interprets the steps. One small
  function per step kind (`_step_draft`, ...) takes over the pieces of `_flow_critique`, `_flow_pipeline` and
  `_flow_versus`.
- Events gain fields only: `debate_started.template`, and `step` (0-based) on `agent_started`/`agent_finished`;
  `stage` and `side` stay. The event contract of `debate-agent-retrieval.md` stays valid.
- A change of any prompt bumps `RETRIEVAL_PROMPT_VERSION`. Re-expressing an existing kind as steps must give a
  byte-identical golden dump (the gate of F0: all builtin teams under the five scenarios with the fake LLM).
- A new combination is measured against `debate` on planted-defect and decision tasks before it is called verified
  (the harness of `proposer_eval.py`; it needs the operator's OK because the Codex quota is shared with production).

## API

Python CLI and Go hub, the pattern of teams:

- `GET /api/flow-templates` runs `guardian.py --flow-templates --json` ->
  `{templates: [{id, name, source, data, resolved, errors, warnings}], catalog: {step_kinds, limits, categories, project_kinds}}`
- `POST /api/flow-templates` (`--flow-template-save`, JSON on stdin) -> `{ok, id, warnings}` or 400 `{error}`;
  `DELETE /api/flow-templates/{id}` (`--flow-template-delete`): builtin 400, unknown 404.
- `POST /api/debates` gains `template` and `params`; the job summary gains `flow` and `template`. The CLI gains
  `--flow-template ID` and `--param KEY=VALUE`.

## Dashboard

Menu Workflows, tab "Flow kinds" (`#/debates/templates` first; old hashes stay valid through `ALIASES`).

- **Gallery**: cards grouped by category (reuse `TeamRow`/`TeamList` of `TeamsSection.tsx`, `Grid`, `Card`,
  `StatusChip`). A card shows a one-line explanation, the steps as a row of icons, project-kind chips and the model
  calls per round.
- **Editor**: a modal in the pattern of `TeamDialog.tsx`: step list (add, remove, reorder), seats per step, parameter
  table, and a preview with the seats filled from the chosen team and the number of calls. State and validation move to
  `lib/flowTemplates.ts`.
- **Start**: `StartForm.tsx` becomes template -> parameter form -> team (prefilled) -> rounds. "Run again" and
  `debatePrefill` carry the template and the values.
- The team editor (Settings -> Flow setup) gets the `flow` picker that F4 of `team-flows.md` left open.
- Strings go into `ko/debate.ts` and `en/debate.ts` together (new group `flowTemplates`). `flow.kind` and step kinds stay
  raw in state, URLs and payloads and are mapped to labels for display (`dashboard-i18n.md`).

## Glossary

New words for the operator to confirm before they enter `docs/glossary.md`: 업무 흐름 (workflow), 흐름 종류 (flow kind),
흐름 템플릿 (flow template), 단계 (step) with the six step names. Existing words (team, project, thread, Judge) do not
change.

## Phases

- **T1 template layer.** Store, validation, CLI, API, start form. Steps exist only as an alias of the five existing
  kinds; the engine does not change.
- **T2 step engine (built).** `_flow_steps` and the six pieces; all five kinds re-expressed, golden dump identical; no
  prompt change, so no version bump.
- **T3a custom steps (built, backend).** A short list of `draft`, `blind_draft` and `critique` is accepted, translated
  into the engine's step lists, kept in the thread snapshot. **T3b** the gallery and editor screens (built 2026-10-30, see below); **T3a2** the
  deferred pieces below.
- **T4 evaluation.** Test tasks per combination. No builtin example template is added before its result.
  T4a (the five existing kinds, hub run 2026-10-30) is measured: `docs/analysis/2026-10-30-flow-eval/results.md`; no
  example template yet (human grade and repetitions first). T4b measures the T3a combinations.

## Decisions

1. **Templates refer to a team (2026-10-29).** Not embedded, not replacing teams. This keeps team files, thread
   snapshots, the project `"team"` key and the API contract as they are.
2. **Scope reaches new flow kinds (2026-10-29).** Decision 1 of `team-flows.md` ("closed list, not a graph") is revised:
   the closed list is now the list of step kinds. The non-goals above stay.
3. **The close stays shared and outside templates.** A template cannot remove the Judge, the gate or `UNVERIFIED`.
4. **Stances come from people.** Parameters or fixed text, never model output.

## Open points

- Whether stances are asked at every start or fixed in the template is chosen per template; both are supported.
- ~~Whether `critique(object)` and "the lead alone consolidates in rounds 2+" are shown as steps~~ Decided in T2: they
  are steps (`critique`, `consolidate`).
- Cost labels show the number of calls until T4 gives measured numbers.
- Which combinations ops and design projects may run is decided per template after its tests.

## Implementation notes

### T1 template layer (built and deployed 2026-10-29 as `f33aee9`)

Built as designed, with these deviations and limits:

- **One step only.** `steps` is a list with exactly one `{"kind": <flow kind>}`, an alias of the five existing kinds.
  The engine runs the team's flow as before; the template only picks the team, renders the task and records itself.
  More than one step, or a step kind outside the five, is refused until T3.
- **Files.** `cli/llm-agent/flow_templates.py` (store, validation, rendering; builtin dir `configs/flow-templates/` may
  be missing or empty, operator dir `<GUARDIAN_DATA_DIR>/flow-templates/`; a custom file shadows a builtin one). CLI:
  `--flow-templates --json`, `--flow-template-save`, `--flow-template-delete`, and `--debate - --flow-template ID
  --param KEY=VALUE`. API: `GET/POST /api/flow-templates`, `DELETE /api/flow-templates/{id}`, `template` and `params`
  on `POST /api/debates`.
- **Python is the only validator of parameter values.** Go checks only the shape (id pattern, count and size of the
  parameters) and passes them as sorted `--param=NAME=VALUE` arguments. Values never reach a shell.
- **Placeholder task.** Go sends the task `template: <id>`; the rendered task is read back from `result.json` and
  replaces it in the job summary. The thread snapshot carries `template` (id, content hash, kind, parameters, stances),
  `lineup.template` and `debate_started.template` repeat it.
- **A bad parameter value is a failed job** (exit 2, "validation failed"), not an HTTP 400, because only Python knows
  the spec. The start form mirrors the checks (`lib/flowTemplates.ts`) so a person normally sees the problem first.
- **Always a new thread.** "Run again" and "Continue this thread" on a job that came from a template start a free task
  with the rendered text; they do not reopen the parameter form.
- **Older hubs.** If `GET /api/flow-templates` is missing the template picker is hidden and the free task form is
  unchanged.
- **CI.** `scripts/ci.sh config` runs `--flow-templates --json` on an empty data dir: errors fail the build, a
  `reviewed` date older than 180 days (`GUARDIAN_FLOW_TEMPLATE_MAX_AGE_DAYS`) prints a warning.
- **No builtin template** is shipped; T4 decides which examples earn a place.

### T2 step engine (built 2026-10-29, deployed as `8e2a591`)

All five flow kinds now run through `_flow_steps`; `_flow_debate`, `_flow_critique`, `_flow_pipeline` and `_flow_versus`
are gone. Deviations and limits:

- **The step lists are still derived from the team.** `_steps_of(ctx, round)` writes the list of a round from the team's
  flow kind (`debate`/`verify`: `draft` or `blind_draft` or `consolidate`, then `critique`; `panel`: `blind_draft`;
  `pipeline`: `draft` + `rewrite` per stage, `rewrite` of the lead in later rounds, optional `critique`; `versus`:
  `blind_draft` on the `sides` seat, then one `exchange` per exchange). T3 replaces this function's input with the steps
  of a template; the interpreter does not change.
- **Open point decided:** `critique(object)` and "the lead alone consolidates in rounds 2+" are steps (`critique`,
  `consolidate`). Without proposers the later-round lead is a plain `draft`.
- **Pieces.** `_step_draft` (draft, blind_draft, consolidate, and a pipeline's first stage), `_step_critique`
  (`mode` `object` or `checklist`), `_step_rewrite` (a stage, or the lead after a rejection), `_step_exchange` (the
  sides' opening cases and each answer round). State lives in `FlowRun`; a canned answer from a stage or a side sets
  `stopped`, so the steps after it do not run and the round is `UNVERIFIED`, as before.
- **No prompt changed**, so `RETRIEVAL_PROMPT_VERSION` is not bumped (the plan above expected a bump; the byte-identical
  gate made it unnecessary).
- **The gate.** `cli/llm-agent/test_flow_golden.py` with `golden/flows.json`: 37 cases (six shapes plus two-round
  verify and a pipeline with critics, plain/resumed/canned/canned-critic/canned-mid, ops and design kinds, the builtin
  `guardian-dev` and `fleet-ops`) under a fake LLM and a fixture git repository. The first version missed the
  later-round prompts (a mutated prompt still passed); after the extra cases, 13 prompt mutations fail the gate. The
  harness pins `GUARDIAN_LLM_PROVIDERS`, because CI's value changes how the system prompt is delivered.
- **Events.** `agent_started`/`agent_finished` (and the retrieval events of the same agent) carry `step`, the index in the
  round's step list; the Judge has none. The golden dump differs from the one before by exactly these keys.
- **Not built:** user-defined `steps` (T3), the editor (T3), measurements (T4).

### T3a custom steps (built 2026-10-30, deployed 2026-10-31 as `bab58a0`)

`steps` may now be a custom list instead of the alias of one flow kind. It is the narrowest grammar that gives a new
combination without new prompt text, so no golden case moved and `RETRIEVAL_PROMPT_VERSION` is unchanged. Deviations
from the design above, and what is left:

- **Grammar.** The first step is `draft` or `blind_draft`; an optional second step is `critique` with `mode` `object`
  (default) or `checklist`. Seats are implied and whole groups only (the lead; the proposers; the critics), so a step
  has no `seats` key and an unknown key is refused. At most 2 steps. A single step with only a flow-kind `kind` stays
  the T1 alias.
- **Reported kind is `"steps"`.** The team's `flow_kind` is unchanged. A custom template needs a `debate` or `verify`
  team (the lead, optional proposers, critics); `blind_draft` needs proposers and `critique` needs critics, checked at
  save, at start and again when a stored thread continues. Custom templates are for code projects only; `stances`
  are refused (there are no sides).
- **Rounds.** `blind_draft` is the independent drafts of round 1; later rounds the lead consolidates them, `draft`
  stays the lead's own draft. Without a `critique` step the template is a single round (a `max_rounds` above 1 is
  clamped with a warning) and the drafts are shown to the Judge as independent answers, like a panel.
- **Parity.** `engine_steps` writes the same step lists `_steps_of` builds for the flow kinds: `draft` + `critique` is
  the debate without proposers, `draft` + `critique(checklist)` is `verify`, `blind_draft` + `critique` is the debate
  with proposers, `blind_draft` alone is the panel. `test_flow_custom_steps.py` pins this, so the pieces behind them
  are the ones the golden dump already holds. New in practice: `draft` alone (one round, no critic), `blind_draft` +
  `critique(checklist)` and `draft` + either critique mode on any team.
- **Continuation.** `Lineup.snapshot()` carries the steps; a thread continued without a template run restores them
  (`flow_templates.restore`), also when the request names a team, and a team that lost the seats refuses with
  "cannot continue".
- **Deferred (T3a2).** In-round `rewrite` and `consolidate`, `exchange`/versus sides, stage seats and lists of 3-6
  steps. They need prompt text of their own, so a prompt-version bump and a T4b measurement.

### T3b gallery and editor (built 2026-10-30, deployed 2026-10-31 as `bab58a0`)

Dashboard only (`apps/web-desktop`); the API and the validator are the T1/T3a ones, and Python stays the single
validator (the dialog mirrors its checks to name the first problem before a round trip). Deviations from the
Dashboard section above:

- **Tab.** "Flow kinds" at `#/debates/templates`: a gallery grouped by category with Edit, Copy and Delete, and a
  dialog for new and edited templates (`pages/templates/`, form logic in `lib/flowTemplateEditor.ts`). The copy
  lives in its own catalog files `i18n/{ko,en}/flowTemplates.ts` (group `flowTemplates`), not in `debate.ts`.
- **Shapes.** The dialog edits both: an alias of one flow kind (with stances for `versus`) and the T3a custom steps
  (first step `draft` or `blind_draft`, optional `critique` with `object` or `checklist`). Custom steps are for code
  projects only, so the project list is fixed there.
- **Preview.** Calls per round (seats plus the Judge) and missing seats are shown for custom steps once a team is
  chosen. An alias shows no count: the numbers stay unmeasured until T4 (open point above).
- **Save rules.** `reviewed` is set to today on every save. A builtin template is never edited in place: the dialog
  opens it as a custom override under the same id (id read-only, button "Save copy"); only custom templates can be
  deleted. Server warnings from the save are shown in the banner.
- **Not done.** No change to the start form (the T1 picker already offers templates) and no `flow` picker in the team
  editor; both can follow.
- **Real-browser check (2026-10-31, hub dashboard in Chrome, read-only, nothing saved).** The tab and its empty gallery (the
  hub has no template yet); the new-template dialog in both shapes: custom steps with `guardian-dev` show the preview
  ("5 calls in the first round (with the Judge)"), the alias shape shows the kind select and the project boxes and no
  count; Cancel left nothing behind. The T1 picker in the start form stays hidden while there is no startable template
  (as built), so it is not seen yet: checking it needs one saved template on the hub, which is a production write.
- **First hub template and the T1 picker (2026-11-01, operator's request).** One alias template saved on the hub
  through `POST /api/flow-templates` (custom, `~/.guardian/data/flow-templates/code-review.json`): `code-review`
  ("코드 검토"), category `development`, projects `code`, team `default`, one `debate` step, `max_rounds` 2, params
  `TARGET` (string, required, max 200), `FOCUS` (enum: correctness bugs, security, concurrency and cleanup, error
  handling; default correctness bugs), `CONTEXT` (text, max 1000, default "없음"). The task asks for findings with
  file:line evidence, a failing input and a severity, unsupported doubts as notes, and no patch, because code-change
  actions of a code project auto-apply once the jev gate passes. Validated first in a scratch `GUARDIAN_DATA_DIR`
  (same content hash `3e1573f94c2a3c99` on the hub, no errors, no warnings). In Chrome: the gallery lists it under
  Development; on the code project `server-guardian` the start form shows the picker "Start from" ("Write a task" / "Template: 코드
  검토"), and picking it shows the three fields (empty target, enum at its default, text prefilled with "없음") and
  the team select "Template default (default)"; on the ops project `fleet` and the design project `dashboard-ui` the
  picker stays hidden (projects `code` only). No debate was started.
- **First production debate from a template (2026-11-01, operator's approval).** `code-review` started on the code project
  `server-guardian` with `TARGET` `cli/llm-agent/flow_templates.py`, `FOCUS` correctness bugs, `CONTEXT` "없음". It is
  the first production run of both T1 and T2: the job's meta records `template: "code-review"` and its params,
  `debate_started` names the template, team `default`, `max_rounds` 2 and prompt `p25`, and the expert and critic
  events carry the top-level `step` (0 and 1; the judge has none, as built). APPROVED in round 1, jev gate PASSED, about
  5.5 minutes, 19 retrievals, no citation mismatch; no `apply_patch` or `execute_code_action` was proposed, so the
  "no patch" line of the task held. The judge approved eight findings in `flow_templates.py` (two medium: the integer
  `min` default differs between validation and the length estimate (0) and coercion (-10⁹), and a whitespace-only
  `stance` passes and overrides the team stance; six low: an infinite default raises `OverflowError`, floats are
  truncated into int parameters, unhashable `projects` items raise `TypeError`, `$` lets a trailing newline through the
  id pattern (also in `command_sets.py`), `min`/`max` are ignored for some types, and a corrupt custom file falls back to
  the builtin while `is_custom` is true). All eight are fixed (2026-11-01, deployed the same day as `51b44b9`; the
  hub's `code-review` validates clean under the new rules; tests in `test_flow_templates.py`):
  - An int parameter without `min` reaches down to `INT_MIN` (-10⁹) in validation, coercion and the length estimate
    alike, and the dashboard editor (`flowTemplateEditor.ts` `maxLen`, `boundsOrder`) follows. An unbounded int now
    counts as 11 characters instead of 10, so a template within one character of a length limit can now be refused.
  - A blank stance is refused at validation, and `resolve` strips stances like a team stance.
  - An int parameter takes an `int` or a string of at most ten ASCII digits with an optional sign (`WHOLE_NUMBER`).
    Floats (including `inf` and `nan`), `"１"` and `"1_0"` are refused instead of truncated or raised. The ten-digit
    bound also keeps `int()` away from its 4300-digit limit, a `ValueError` the callers would not catch.
  - `projects` items are checked for strings before they are hashed.
  - Ids, parameter names and `NAME=value` arguments use `fullmatch`. `command_sets.py` already did, so that part of
    the finding was not reproduced. `team_manager.py` (`TEAM_ID_PATTERN.match`) has the same defect and is left as is,
    outside this change.
  - `min` is refused on non-int parameters and `max` on enum parameters, as the dashboard never writes them.
    Hand-written files that carry them now fail validation (the hub's `code-review` does not).
  - An unreadable custom file shadows the builtin of the same id: `get` returns None, and the listing drops the id
    rather than show the builtin.
  - Beyond the eight: blank enum values are refused too.

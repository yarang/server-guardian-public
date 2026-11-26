# Agent characters: a roster of reusable characters, the persona as their definition (design)

English | [한국어](agent-characters.ko.md)

Status: proposed 2026-10-30; the "Suggest with AI" step (2.6) and C0 (guide and lint, 2026-10-31) are built and deployed (2026-10-31, `670d6c7`, checked in a real browser); C1 for the code teams (role cards, 2026-10-31) is built and deployed (2026-10-31, `6e0fb08`), one trial debate ran on it (2026-10-31, `guardian-dev`, approved in round 2); C1 for the ops teams (2026-10-31: role cards for every persona of the four ops teams, cards follow the home seat, new card sentences approved by the operator, prompt p25) is built, one trial debate per ops team ran on it (2026-10-31: all four approved in round 1, none worse than its baseline, `fleet-provision` now writes a read-only collection plan) and deployed (2026-10-31, `91fa7eb`); the rest is not. Origin: the operator asked for a review of the persona system and proposed
"a structure like characters: debates use various characters, and the persona is the character's definition; it must
not get long, so a guide for a fixed form is needed." Addition the same day: "the profile can hold a picture of the
character, made with gemini or codex" (section 2.4, phase C5).

## 1. Review of the current persona system

What a persona is today (`cli/llm-agent/persona_manager.py`, `configs/personas/*.json`):

- Fields `id`, `name`, `role`, `domain`, `system_prompt` (up to 20,000 characters), `focus_areas`, `critique_focus`,
  `mcp_capabilities`. Builtin in `configs/personas`, custom overrides in `<GUARDIAN_DATA_DIR>/personas`.
- Only `system_prompt` reaches the model (`RolePrompt.persona`, `debate_engine.py:542`); `mcp_capabilities` narrows the
  judge's tools (`team_manager.py:judge_tools`). `domain`, `focus_areas`, `critique_focus` are shown, never sent.
- The seat (expert, proposer, critic, judge, stage, side) comes from the team file, not from the persona; the persona's
  `role` is free text.

Measured on the 29 builtin personas (2026-10-30):

| Observation | Number |
|---|---|
| `system_prompt` length | 122 - 958 characters, median about 500 |
| Personas used by exactly one team seat | 24 of the 27 that teams use (the other 3 are the `default`/`cross-review` pair) |
| Personas used by no team | 2 (`general_assistant`, `server_guardian`: CLI chat only) |
| Personas that repeat the ops citation rule `[#번호 조회 호스트]` | 9 |
| Personas that repeat the code citation rule `파일:줄` | 7 |
| Ops critics that repeat the `[차단]`/`[권고]` grading paragraph | 7 (and 4 judges repeat how to read it) |
| Judges that repeat "do not invent prohibitions the user did not ask for" | 4 |

Problems this shows:

1. **Who and how are mixed in one text.** A persona holds the character (expertise, viewpoint) together with seat
   behaviour (the grading paragraph, "say when there is nothing wrong", "do not invent prohibitions"), the evidence
   format of the project kind (`file:line`, `[#번호 조회 호스트]`, ops blocks) and project facts (`guardian_lead_dev`
   repeats the repository constraints). The same rule lives in 4-9 copies; a calibration such as the
   `fleet-provision` judge fix of 2026-10-19 has to be made in each copy by hand, and the copies drift.
2. **No reuse.** Persona and seat are 1:1: the safety reviewer exists three times (`ops_safety_critic`,
   `ops_service_safety_critic`, `ops_provision_safety_critic`), differing mostly in the scope sentence. A new team
   means new personas.
3. **The role is not typed.** The solo runner refuses judges by matching `"judge"` / `"jev_mcp"` in the id or text
   (`solo_agent.py:59`), because nothing says which seats a persona may take.
4. **Dead fields invite wrong edits.** `focus_areas` and `critique_focus` look like they steer the agent but are
   never sent (documented 2026-10-30 in `debate-teams.md`); an operator who edits them changes nothing.
5. **No length or form rule.** The only bound is 20,000 characters. The longest prompts are long because they carry
   seat and kind rules, not because the character needs it.

What works and must be kept: personas are short today (none above 1,000 characters), every builtin cites evidence, no
builtin claims autonomy (`test_builtin_personas.py`), custom overrides never touch the checkout, and the judge's tools
can only be narrowed by the team.

## 2. Proposal: character = persona + profile, composed with seat and kind

A **character** is a reusable agent identity. Its **persona** is the definition that reaches the model; its
**profile** is what people see. Everything about the seat, the evidence format and the project is no longer part of
the character; the engine adds it when the character sits in a seat:

```
system prompt of a turn =
    character.persona        (who: identity, expertise, stance, checks, boundaries)   <- character file
  + role card                (how to behave in this seat: critic, judge, ...)          <- engine, per seat x kind
  + seat focus (optional)    (what this team wants from this character)               <- team file
  + kind protocol            (evidence format: repo blocks / ops blocks / quoting)     <- engine (exists today)
  + rules                    (language, answer format)                                 <- engine (exists today)
```

So "the safety reviewer" is one character; `fleet-ops`, `fleet-services` and `fleet-provision` seat it as a critic
with a one-line seat focus each. The `[차단]`/`[권고]` grading is the ops critic role card, written once.

### 2.1 Character file

```json
{
  "id": "safety_reviewer",
  "format": 2,
  "name": "Safety reviewer",
  "profile": {
    "tagline": "Knows what an action deletes or stops before it runs",
    "look": "calm middle-aged engineer, short grey hair, high-visibility vest, holding a checklist"
  },
  "seats": ["critic", "verifier", "solo"],
  "kinds": ["ops"],
  "role_card": "critic",
  "role_card_kind": "ops",
  "persona": {
    "identity": "a reviewer of operational changes who makes sure an action's losses are known before it runs",
    "expertise": ["data loss and irreversible deletion", "audit and security log retention", "service availability"],
    "stance": "Cleanup is the goal; the review exists so that deletion and downtime are deliberate, not to stop work.",
    "checks": ["what each action deletes or stops", "the target host and sudo", "numbers that differ from the checks"],
    "boundaries": ["retention policy and recurrence belong to another reviewer", "no new preconditions (windows, backups)"]
  }
}
```

- `persona` is the only part sent. The engine renders it with one fixed template (2.5), so every character reads the
  same way and a person cannot add stray instructions outside the fields.
- `profile` is shown only (dashboard roster, event cards). Character flavour (avatar, colour, tagline, `look`,
  `portrait`) lives here and costs no tokens and no bias. The icon and colour stay as the fallback when there is no
  portrait (section 2.4). C2 accepts only `tagline` and `look`; `avatar`, `color` and `portrait` come with C4/C5.
- `seats` replaces the free-text `role`: the seats this character may take (`expert`, `proposer`, `critic`,
  `verifier`, `judge`, `stage`, `side`, `solo`). Team validation refuses other seats; the solo runner reads it instead of
  matching ids.
- `kinds` (optional): project kinds the character is written for (`code`, `ops`, `design`); empty means any.
- `role_card` and `role_card_kind`: the home seat whose engine card follows the character, and the card's kind (C1,
  the operator's decisions of 2026-10-31). Required when `seats` holds expert, critic or judge; `role_card` must be one
  of the `seats` and `role_card_kind` one of the `kinds` when they are set.
- `tools` (optional): the ceiling of tools the character may propose, used only in the judge seat (today's
  `mcp_capabilities`). Leave it out for the whole catalog; an empty list is refused, because it could be read as
  either "all" or "none".
- `domain`, `focus_areas`, `critique_focus` are dropped; their content moves into `expertise` and `checks`, which are
  sent.

### 2.2 Team seat focus

A team seat may add `"focus": "<one sentence, at most 200 characters>"`, rendered after the role card. It is what
makes the reused character team-specific ("only the safety of this cleanup; recurrence is the capacity reviewer's").

### 2.4 Portrait (generated picture)

A character may carry a portrait: a square picture shown on the roster card, in the editor and next to the seat on
event cards. It is made by an image model on a person's request, never during a debate, and never reaches an agent.

**Who generates.** The person picks the generator per request:

| Generator | Route | Credential | Status |
|---|---|---|---|
| `gemini` | Gemini API image model over HTTPS (`urllib`, standard library), model id in `GUARDIAN_PORTRAIT_GEMINI_MODEL` | `GEMINI_API_KEY`: the key store of `agent-seats-keys.md` once K1 exists, the hub's `llm.env` until then | to be confirmed in CP0 |
| `codex` | `codex exec` in a throwaway scratch directory with every tool disabled except `image_generation` (the debate runs keep it disabled) | the codex login on the hub (shared quota with production debates) | the tool exists (`imagegen`, `debate-agent-retrieval.md`); where and in which format it writes the file is to be confirmed in CP0 |

`agy` (Gemini through the CLI) is not a route until its image generation is confirmed in official documentation
(`design-project.md` lists it as unverified). A generator that fails CP0 is not offered in the dashboard.

**Prompt.** The hub builds the image prompt; the person writes only `look` (at most 200 characters). Fixed parts:
the house style (one style for every character, so the roster looks like one set), square, head and shoulders, plain
background, no text or letters, a fictional person. `name`, `tagline` and the persona are not sent, only `look`.
`look` rules (checked at save): no real or public persons, no brand, logo or existing fictional character, no text to
draw in the picture.

**Candidates.** One request makes 1-4 candidates (default 2) in a scratch directory; the person picks one, or none.
Unpicked candidates are deleted after 24 hours. Generation runs in its own slot (`GUARDIAN_MAX_PORTRAITS`, default 1)
with a timeout, and writes an audit line (actor, character id, generator, sha256; no prompt text of other fields).

**Storage and serving.**

- Custom characters: `<GUARDIAN_DATA_DIR>/character-portraits/<id>/<sha256>.<ext>`, immutable; the character file
  points at the file name. Replacing the portrait writes a new file; the old one is removed when no character points at
  it. Included in the nightly backup (small files).
- Builtin characters: a portrait is committed under `configs/personas/portraits/` only after the operator approved the
  picture; otherwise they show icon and colour.
- **Size: 256x256 stored** (decided 2026-10-30 after the operator found 1024 too large). The picture is shown at
  24-32 CSS px on event cards, 64-96 px on roster cards and 128 px in the editor; 256 covers the largest at 2x pixel
  density, and common profile sizes sit in the same range (an avatar is typically served between 80 and 512 px).
  Generators answer at about 1024x1024, so the candidates stay full size in the scratch directory and the **dashboard
  scales the picked one down** in the browser (canvas, WebP, PNG where WebP encoding is missing) before it is sent: the
  engine is standard-library only and has no image library to resize with. An upload goes through the same scaling.
- The hub accepts WebP or PNG, checked by magic bytes, exactly 256x256 (WebP/PNG headers parsed with the standard
  library; no re-encoding), at most 256 KiB (a 256x256 WebP is usually 10-40 KiB). Anything else is refused, not
  converted.
- `GET /api/personas/{id}/portrait`: sha256 checked on every read, the fixed content type of the extension,
  `X-Content-Type-Options: nosniff`, same sandbox CSP as the artifact API; the dashboard shows it only as `<img>`.
- A person may also upload a picture instead of generating one; it goes through the same checks.

**API.** `POST /api/personas/{id}/portrait/generate` `{generator, look, count}` -> job id;
`GET /api/personas/{id}/portrait/jobs/{job}` -> state and candidate URLs; `POST /api/personas/{id}/portrait`
`{job, candidate}` or an upload -> sets the portrait; `DELETE /api/personas/{id}/portrait` -> back to icon and colour.
CLI: `guardian.py --portrait-generate <id> --generator gemini|codex`, `--portrait-prune`.

### 2.5 Rendering (fixed template)

```
You are {identity}.
Expertise: {expertise, comma separated}.
Stance: {stance}
Check first:
- {checks...}
Not your job:
- {boundaries...}
```

The template is written in the dashboard's prompt language (Korean today: `당신은 {identity}입니다.` ...). It is part of
the prompt, so a change bumps `RETRIEVAL_PROMPT_VERSION`.

### 2.6 Suggest with AI (persona and profile)

A "Suggest with AI" card in the persona editor drafts the persona and the profile from a few words. Built 2026-10-30
on today's persona format, before C2:

- The person writes a brief (at most 1,000 characters) and picks the seat (expert, proposer, critic, judge), the
  project kind (code, ops, design) and the model (automatic, claude, codex, agy). `POST /api/personas/suggest` runs
  `guardian.py --persona-suggest` (`persona_suggest.py`): one model turn through `LLMRunner.run_prompt`, no tools, an
  empty working directory, no role header.
- The prompt carries the brief, the guide's rules (who, not how; at most 700 characters; no host counts, versions,
  paths, tool names or autonomy; no real persons or brands) and up to two builtin personas of the same seat, those of
  the same kind first, as style examples. When the form already has a system prompt, it is sent as the draft to
  improve.
- The answer is parsed as JSON, cleaned to the known fields, given a free id (an id already taken gets `_2`, ...) and
  the seat's role label, and checked with `validate_persona`. Guide warnings (length above 700, tool names, autonomy,
  host counts) come back with the draft; they do not block.
- **Nothing is saved.** The draft fills the form: an existing persona keeps its id and tools (the model never picks
  tools); the person reads, edits and saves the usual way.
- One suggestion runs at a time on the hub (a second request gets 409), with a 5-minute timeout; exit 5 (no usable
  draft, or no LLM CLI answered) is 502.
- The profile part (`tagline`, `look`) is stored now as an optional `profile` object of the persona file, shown only
  and never sent to an agent; `look` is what the portrait generator (2.4) will use.

## 3. Form guide for a persona

Limits are checked at save (`validate_persona`) and by a CI lint over the builtins.

**Until C1 and C2 (today's format).** A persona is still one free `system_prompt`, and the seat rules and evidence
formats still live in it, because the engine has no role cards yet. Write a new persona so that C1 can lift those parts
out without touching the rest: the character first (identity, expertise, stance, what it checks, what it leaves to
others), then at most two sentences of seat rules, copied from a builtin of the same seat rather than reworded (the
lint finds repeated sentences, and C1 moves them by their exact text). Keep the whole text at 700 characters or less.

**Lint (`cli/llm-agent/persona_lint.py`).** Warning only: it never fails CI and never blocks a save.

| Level | Rules | Meaning |
|---|---|---|
| `warn` | `length` (above 700), `autonomy`, `host-count`, `version`, `path`, `env` | wrong in any format; returned as warnings by `POST /api/personas` and by the suggest step |
| `move` | `tool`, `grading`, `verdict`, `citation`, `protocol` | a seat rule, an evidence format or a tool name: allowed until C1, reported as what C1 will move |
| `note` | `length` (501-700) | long but within the limit |

It also lists sentences that three or more personas repeat word for word (role card candidates). Run it as
`python3 cli/llm-agent/persona_lint.py` (`--json`, `--custom` for the hub's custom personas too); `scripts/ci.sh python`
prints its `warn` lines and summary.

| Field | Form | Limit |
|---|---|---|
| `identity` | one noun phrase, completes "You are ..." | 120 characters |
| `expertise` | 1-4 noun phrases | 60 characters each |
| `stance` | 1-2 sentences: what the character optimises and what it trades away | 200 characters |
| `checks` | 1-5 items, each a thing to look at, not an instruction to output | 80 characters each |
| `boundaries` | 0-3 items, what it leaves to others or never asks for | 80 characters each |
| rendered persona | the whole block | 700 characters; warning above 500 |

Content rules:

1. **Who, not how.** No output format, no grading labels (`[차단]`, high/medium/low), no verdict words, no citation
   syntax (`file:line`, `[#번호 ...]`), no tool names (`ops:`, `jev_mcp:`): those are role cards and kind protocols.
   The lint rejects these patterns in `persona`.
2. **No facts that drift.** No host counts, version numbers, prompt versions, file paths of this repository or
   environment variables. Project facts go into the project, not the character.
3. **No autonomy claims.** Nothing like "act on your own" or "execute" (the existing `NoAutonomyTests` stay).
4. **The stance must make a difference.** Two characters with the same seat must differ in `stance` or `checks`;
   otherwise they are one character. Personality words without an effect on the review ("cheerful", "strict") are not a
   stance.
5. **Boundaries prevent overlap, not work.** Use them to hand a concern to another seat, never to forbid the user's
   request.
6. **One language.** The persona is in the prompt language; `name` and `profile` are shown text and follow the
   dashboard catalogs where they are builtin.

A character that needs more than these limits is two characters, or it carries seat or project rules that belong
elsewhere.

## 4. Role cards (engine-owned)

Role cards are short engine texts per seat and project kind, extracted from what the builtin personas repeat today:

| Seat | Common card (all kinds) | Kind additions |
|---|---|---|
| expert | answer the task from evidence you read; propose the smallest change and how to undo it | ops: least destructive, catalog actions first |
| critic | find what breaks; scenario and severity per point; say clearly when nothing is wrong | ops: `[차단]` only with cited harm, `[권고]` otherwise; design: quote the passage |
| judge | weigh proposal and critique against the evidence; approve only confirmed steps; invent no prohibitions | ops: `[권고]` become checks, only `[차단]` changes the plan |
| verifier, stage, side | existing flow texts (`VERIFY_TASK`, ...) | — |

The judge rules that are tied to tools (plan rules, `bundles`, batch rules, gated on the judge holding a tool) stay
where they are today: in the engine, keyed by tool, not in the character.

## 5. Phases

- **C0 Guide and lint (no prompt change).** This document's guide; `persona_lint.py` in `scripts/ci.sh python`,
  warning only, reports per builtin: rendered length, rule violations. Gives the baseline. Built 2026-10-31, deployed the same day as `670d6c7`
  (see Implementation notes).
- **C1 Role cards.** Move the repeated seat rules into the engine (per seat x kind), shorten the builtin
  `system_prompt` texts by the same sentences. Golden dump of the composed system prompt for every builtin team seat
  before and after (`golden/` like `test_flow_golden.py`); the diff must be only reordering and deduplication. Bump
  `RETRIEVAL_PROMPT_VERSION`. Because `fleet-ops` prompts were kept byte-identical on purpose and `fleet-provision` was
  calibrated by a trial, C1 starts with the code teams; the ops teams follow only after a sandbox trial debate (needs
  the operator's OK for the shared Codex quota). Built 2026-10-31 for the code teams (`default`, `cross-review`,
  `guardian-dev`); the ops teams followed the same day, first with byte-identical prompts, then with the generic ops seat rules the
  operator approved (Implementation notes, "C1 for the ops teams" and its second step).
- **C2 Character format.** `format: 2` files (persona block, profile, seats, kinds, tools), renderer, validation with
  the limits of chapter 3, seat check in team validation and in the solo runner. Format 1 files (raw `system_prompt`)
  keep loading unchanged and show a "legacy" chip; nothing is converted automatically. Built 2026-11-01, not
  deployed (see Implementation notes).
- **C3 Roster and reuse.** Team seat `focus`; merge duplicate builtins (the three safety reviewers, the four ops
  judges' shared text) into characters with seat focus. Each merge is checked with the golden dump and one trial.
- **C4 Dashboard.** "Characters" page (rename of the Personas page): roster cards with profile, seats, kinds and
  "used by"; the editor gets one input per persona field with live counters and a preview of the composed prompt for a
  chosen team seat. Event cards show the character's name and avatar next to the seat. Strings in `ko` and `en`.
- **CP0 Portrait probe (no product code).** On the hub sandbox, one picture per generator: does `codex exec` with only
  `image_generation` write a file into the scratch directory, which format and size, how long; does the Gemini model id
  answer with the hub's key, cost per picture. Recorded in `docs/analysis/<date>-portrait-probe/`. The codex run uses
  the shared quota: ask the operator first.
- **C5 Portraits.** The confirmed generators only: `portrait_ops.py` (prompt, candidates, checks, store, prune),
  the Go handler, the generate dialog in the character editor (generator choice, `look`, candidates side by side,
  pick, scaling to 256x256 in the browser), portrait on roster and event cards with icon and colour as fallback.
  Needs the `profile` object (exists since the suggest step); it does not need C1, C2 or C3.
- **Suggest (built 2026-10-30, section 2.6).** `persona_suggest.py`, `--persona-suggest`, `POST /api/personas/suggest`,
  the "Suggest with AI" card, `profile.tagline`/`profile.look` in the persona file and editor.

## 6. Decisions for the operator

1. **Store.** Recommended: keep one store and the ids (`configs/personas`, `/api/personas`), add `format: 2`; the word
   "character" is used in the dashboard and docs. Alternative: a new `configs/characters/` store and API (cleaner name,
   two stores during migration).
2. **Length limit.** Recommended 700 characters rendered, warning above 500 (today's median is about 500 including
   seat rules, so characters without them fit easily).
3. **Profile.** Decided 2026-10-30 by the operator: a character may have a generated portrait, made with gemini or
   codex (2.4). Icon (fixed set) and colour token stay as the fallback. Open: (a) style: recommended one illustrated
   house style rather than photorealistic pictures, so a portrait is never mistaken for a real person; (b) default
   number of candidates (recommended 2); (c) whether builtin characters get committed portraits.
4. **Character and model.** Recommended: a character does not pick its provider or key; that remains the seat's
   (`agent-seats-keys.md`). A character may later carry a non-binding `preferred_providers` hint.
5. **Voice.** Recommended: no speaking style in the persona (it costs tokens and may bias reviews); flavour stays in the
   profile.

## Non-goals

- No memory or state per character across debates (a character is a definition, not a running agent).
- No change to who decides: seats, the Judge, the jev gate and action validation stay as they are.
- No automatic conversion of custom personas on the hub.
- No image generation during debates, no picture sent to an agent, no image model for design projects (this is
  portraits only; `design-project.md` decision 4 is unchanged).

## Implementation notes

- 2026-11-01, C2 character format (built by z.ai agents, engine and dashboard separately, each reviewed by a claude
  reviewer; deployed 2026-11-01 as `241fb0a`, the hub reads prompt `p26`, no hub persona is format 2 yet, not
  checked in a real browser). Decisions 1 and 2 of chapter 6 taken as recommended: one store (`configs/personas`,
  `/api/personas`, `format: 2` beside format 1) and the limits 700 rendered characters, note above 500.
  - **Engine.** `character.py` (seats, kinds, limits, `render_persona` with the Korean template of 2.5, `body_text`,
    `persona_tools`, `persona_seats`); `persona_manager.validate_persona` branches on `format` (format 1 validation
    unchanged, moved to `_validate_format1`); `role_cards.persona_text` uses `body_text` and joins a character's card on
    its own line (format 1 keeps the space join, byte for byte); `team_manager.judge_tools` reads `tools` of a
    character (it still only narrows); `validate_lineup` refuses a character outside its `seats` (a critic position of
    a `verify` flow is the `verifier` seat); the solo runner takes a character only with a `solo` seat. The lint and the
    save warnings measure the rendered block. `RETRIEVAL_PROMPT_VERSION` 2026-11-01.p26. No builtin is converted, so
    every golden file is unchanged and the builtin lint output is the same.
  - **Deviations from 2.1.** `role_card` and `role_card_kind` stay explicit in a character file and are required when
    `seats` holds expert, critic or judge (the card follows the home seat and its kind belongs to the persona, the
    operator's decisions of 2026-10-31); `role_card` must be one of the `seats`, `role_card_kind` one of the `kinds`
    when they are set (the example in 2.1 was brought in line). `profile` takes only `tagline` and `look` for now;
    `avatar`, `color` and `portrait` come with C4/C5. `tools` is left out for the whole catalog; an empty list is
    refused, since for a character `tools` is a ceiling and `[]` would read either way. In the rendered block, the
    `move` rules (tools, grading, verdict, citation, protocol) and autonomy claims are errors, the other `warn` rules
    and the 500 note stay warnings.
  - **`rendered`.** `personas_report` adds the rendered block to each character in the list output, for display only;
    it is never stored. `PersonaManager.save_persona` drops the display keys `source` and `rendered` before
    validating, so an object as the list returns it saves unchanged.
  - **Review fixes** (one claude review of each half, fixes by a z.ai agent, a second claude review of the fixes).
    A format 1 save over an id held by a character, builtin or custom, is refused ("delete it first or pick another
    id"), and both create forms of the dashboard say so under the id field, also after "Suggest with AI" filled it.
    Per-run persona overrides (`expert_id`, `critic_id`, `judge_id`) run `validate_lineup` again, so a character
    seated outside its `seats` by an override is refused, not rerouted (a `--critic` override on a `panel` or
    `versus` team is still dropped with a warning, not refused). `render_persona` takes any shape of a
    hand-written file without raising (a field of the wrong type renders as empty). "Suggest with AI" refuses a
    character as its starting point; suggestions write format 1 personas until C4.
  - **Release note.** A persona file whose `format` is set to anything but 2 is now refused (before C2 the key was
    unknown and ignored). No builtin and no custom persona on the hub sets it (read-only look, 2026-11-01).
  - **Known limits, left for C3/C4.** The critique seat is chosen by the team's flow kind, not by the step mode of a
    flow template (a `verify` step inside a `debate` team still seats its critics as `critic`). `versus` and
    `pipeline` teams seat their sides and stages as the expert seat, so a character needs an `expert` seat there.
  - **Dashboard.** Characters are shown read-only (editor page and dialog; delete stays for custom ones), with a
    "character" chip, their seats and tagline; format 1 personas get a "legacy" chip. Characters are not offered as a
    "start from" source. The team editor's pickers use `seats` for characters (format 1 keeps the role label match),
    Quick ask offers a character only with a `solo` seat. Editing characters is C4.

- 2026-10-31, trial debates of C1 for the ops teams (operator's OK of the same day; hub sandbox in `~/guardian-eval`,
  own data dir, engine at `62ad4e4`, prompt p25, two rounds, `GUARDIAN_AUTO_APPLY=off`, `GUARDIAN_UI_ACTIONS=off`; one
  debate per team, each with the task of its baseline run):
  - **`fleet-provision`, the one visible change.** Task "서버들의 timezone과 패키지를 확인해보자. 서버 관리해보자."
    APPROVED, jev PASSED in round 1 (749 s), and then the vote pass. The calibrated run of 2026-10-20 reported and left
    the plan empty; this judge wrote a read-only collection plan of 23 `ops:run_script` steps (22 Linux hosts and one
    macOS host): timezone, NTP state and `/etc/localtime` on every host, plus timers, crontabs and the PostgreSQL /
    MariaDB timezone settings on the five hosts not on UTC. Step `s1` runs first, the others require it. Every script
    was read from the decision store: guards (`systemd`, `timedatectl`, `Darwin`), `timedatectl show`, `date`,
    `readlink`, `sudo cat` / `find -exec cat` of crontabs, `SHOW` / `SELECT` queries; no write, no `set-timezone`, no
    restart. Five constraints, none a prohibition the user did not ask for: the scripts only read; the guards; the extra
    reads on the five hosts; `s1` first; "시간대 변경은 사용자가 통일할 시간대(권장 기본값 Etc/UTC)를 정한 뒤 별도 계획으로
    만든다" (the opposite of the over-defensive run of 2026-10-19, "UTC를 기본값으로 정하지 않는다"). Seven checks. Both
    critics voted approve; the gate held the plan for a person (`auto` and `would_auto` false: no `batch.auto`, the
    steps are high risk). Production `fleet` has no `batch` and no `auto_run` (checked 2026-10-31), so such a plan waits
    for the approval tab there too. The judge's own text allows an empty plan ("비워도 됩니다") but does not ask for one,
    and tells it to put timezone work into script steps, so the plan is within its rules. Likely cause, one run only:
    the provisioner lost the example '서버 관리해보자' (this task's words) and the word "실행안"; the card asks for "구체적인
    안". Judged not a regression; a second run, or putting the example back, is the operator's choice.
  - **`fleet-ops`.** The watch task of 2026-10-31 (an inode alert on the macOS host): APPROVED, PASSED in round 1
    (1032 s), the alert read as a false alarm of `devfs` (100 % is how macOS reports it), the real finding a data volume
    at 94 %, no action. Same verdict as its baseline.
  - **`fleet-services`.** Failed or unstable systemd units on one node: APPROVED, PASSED in round 1 (434 s), no failed
    unit, a report, no restart. Same as its baseline of P8.
  - **`fleet-mesh`.** The headscale report: APPROVED, jev **PASSED** in round 1 (412 s), no action. Its two earlier runs
    (P8 and p23) ended DISPUTED over the "도구 밖" list or constraint wording; with one run this is a better result, not
    yet a measured improvement. The judge card's action wording (deviation (3) of the note below) did not confuse it.
  - **Common.** Every team's prompts hold its expert, critic and judge ops cards and no code card. No fallback event
    (the word appears only inside excerpts). All citations verified at the end (none unverified after the re-ask).
    Critics wrote their objections under `[차단]` and `[권고]` in every team; the word counts of the transcripts are not
    objection counts (they include sentences like "[차단] 사유는 없습니다") and are not recorded.
  - **Result.** No team is worse than its baseline, so entry condition (3) is met; conditions (2) and (4) were met by
    the build (moved sentences, approved new ones; the tool-keyed judge rules stay in the engine). Deployed
    2026-10-31 as `91fa7eb` (the hub runs p25).
- 2026-10-31, C1 for the ops teams, second step (operator's decisions A-G of the same day; built, not deployed, no
  trial debate yet). It answers the open points of the note below:
  - **A, card kind.** Stays the persona's own `role_card_kind`, so a persona and its cards are reused in any project;
    cards chosen by the project's kind are dropped.
  - **B, home seat.** A persona keeps its seat and its role: `persona_text` takes no seat, the card is always that of
    `role_card`. `team_manager.validate_lineup` warns (it does not refuse) when a team seats an opted-in persona
    elsewhere; drafters, pipeline stages and versus sides count as the expert seat. A warning, not an error, so stored
    teams and threads keep loading; refusing such a team on save is possible if the operator wants it. The flow golden
    changed where its fixtures seat `devops_specialist` (home seat critic) as a proposer or stage; no builtin team and
    no custom team on the hub does that (read-only look, 2026-10-31).
  - **C-G, new card sentences (approved).** Expert ops card: "추측하지 말고 먼저 ops 블록으로 대상 서버를 점검해 사실을
    확인하세요." and the ambiguity rule ("주문이 모호하면 점검 결과로 현황을 보고하고, 가장 합리적인 해석으로 구체적인 안을
    가정과 함께 내놓으세요."). Critic ops card: "이의는 [차단]과 [권고] 둘로 나눠 적으세요." before the citation rule.
    Judge ops card: the `[권고]`/`[차단]` grading, no invented prohibitions, no action on unconfirmed numbers or made-up
    citations, before the question rule. All four left-out personas opt in (`ops_judge`, `ops_provisioner`,
    `ops_provision_safety_critic`, `ops_dependency_critic`), so every persona of the four ops teams has a card.
    `RETRIEVAL_PROMPT_VERSION` p24 -> p25.
  - **Moved, not duplicated.** The personas drop their copies: `ops_sre`, `ops_service_engineer` the look-first
    sentence; `ops_service_engineer`, `ops_provisioner` their ambiguity sentence; `ops_provisioner` its citation
    sentence and the "추측하지 말고" lead of its `host_facts` sentence; `ops_judge`, `ops_service_judge` the three judge
    sentences; `ops_mesh_judge` its shorter no-prohibitions sentence (the card's is the same rule).
  - **Deviations.** (1) The ambiguity examples ('서비스 상태 봐줘', '서버 관리해보자') and the provisioner's word "실행안"
    are gone with the sentences: the card has the generic form. (2) Near-duplicates stay where a persona has its own
    variant: `ops_mesh_engineer` keeps "추측하지 말고 먼저 ops 블록으로 확인하세요." before its `ops_hosts` rule; the five
    critics that already had "이의는 둘로 나눠 적으세요." keep it, with their own `[차단]`/`[권고]` definitions;
    `ops_provision_judge` keeps its calibrated sentences of 2026-10-19 and gets the generic three beside them.
    (3) The judge card speaks of actions ("조치를 바꾸거나 빼게", "조치는 승인하지 마세요"), but `fleet-mesh` proposes none
    (`allowed_tools: []`); its judge keeps its report-specific sentences, and the trial debate is the place to see
    whether the action wording confuses it. `ops_service_judge` gains "(checks)", the same rule as before.
  - **Checks.** `test_seat_golden.py --review d411368`: every added sentence is a card sentence, every removed one is
    now in a card (15 of 30 seats changed, all in ops teams). Flow golden: only the cases that seat `ops_sre`
    (`builtin-fleet-ops`, panel, pipeline, pipeline-critic, versus). `persona_lint`, before -> after: `warn` 8 -> 7,
    `move` 29 -> 23, `note` 10 -> 9, shared sentences 1 -> 1, median length 463 -> 425, longest 921 -> 921.
  - **Open for the operator:** deploy; one trial debate per ops team (`fleet-ops`, `fleet-services`, `fleet-mesh`,
    `fleet-provision`) on the hub sandbox, now a test of the new p25 wording; `fleet-provision` against its
    calibration of 2026-10-19 (the judge invents no prohibitions).
- 2026-10-31, C1 for the ops teams (built, not deployed; no trial debate yet, it needs the operator's OK). Deviations
  from the plan above and from chapter 4:
  - **Cards keyed by the persona, not by the project.** The first draft chose the card by the project's kind. The flow
    golden (`golden/flows.json`) then changed in 13 cases: code personas running in ops projects lost their code card
    sentences and got the ops card. The card kind is now the persona's own field `role_card_kind` (only `"ops"`;
    absent: the code cards; it needs `role_card`). An opted-in persona says in every project what it said before C1.
    Cards chosen by the project's kind wait for the operator's approval.
  - **Zero-addition ops cards.** Each ops card is the one sentence every opted-in ops persona of that seat already
    ended with: expert "주장마다 [#번호 조회 호스트]로 근거를 인용하세요.", critic "모든 주장에 [#번호 조회 호스트]로 근거를
    인용하세요.", judge "사용자가 정해야 할 값은 하나만 묻고 권장 기본값을 함께 쓰세요." The sentence was cut from the
    persona, so the composed text is byte for byte the old one; the kind additions of chapter 4 (catalog first,
    `[차단]`/`[권고]` framing, no invented prohibitions) stay in the personas, because as cards they would be new
    sentences for some of them.
  - **Opted in (11):** `ops_sre`, `ops_service_engineer`, `ops_mesh_engineer` (expert); `ops_safety_critic`,
    `ops_capacity_critic`, `ops_service_safety_critic`, `ops_service_cause_critic`, `ops_mesh_risk_critic` (critic);
    `ops_service_judge`, `ops_mesh_judge`, `ops_provision_judge` (judge). **Left out (4):** `ops_judge`,
    `ops_provisioner`, `ops_provision_safety_critic`, `ops_dependency_critic`: their text does not end with their seat's
    ops card, so opting them in would add or move a sentence.
  - **Checks.** Seat golden and flow golden unchanged without regeneration; no `RETRIEVAL_PROMPT_VERSION` bump (stays
    p24). A matrix of every builtin persona x seat (none, expert, critic, judge) x card kind (none, ops, design), 348
    cells, against the previous version: no difference for a home seat or for a run outside a seat; the 66 differences
    are ops personas in a seat other than their home seat, which get that seat's ops card in place of their own sentence
    (the C1 rule). No builtin team seats them so, and a read-only look at the hub's custom teams and personas found none
    that does, so the deploy changes no production prompt. `test_fleet_specialists.py` now checks the sent text
    (`persona_text`) instead of the raw `system_prompt`. `persona_lint` on the stored texts, before and after: shared sentences
    4 -> 1, `move` 37 -> 29, `warn` 8 -> 8, longest text 958 -> 921 characters: the three moved sentences left the
    personas and nothing else changed. The dashboard editor keeps `role_card_kind` through an edit
    and a copy and drops it for a suggestion or a template, like `role_card`.
  - **Open for the operator:** cards chosen by the project's kind; the kind additions of chapter 4 as card sentences;
    opting in the four left-out personas; one trial debate per changed team (the prompts are byte-identical, so it is
    a check of the wiring, not of new wording).
- 2026-10-31, C1 trial debate (operator's OK; hub sandbox `~/guardian-eval`, own data dir, a clone of the repository at
  `6e0fb08`, `GUARDIAN_AUTO_APPLY=off`). Team `guardian-dev`, at most two rounds, the same task as the 2026-10-19 trial
  (review of `TeamManager.save` and `validate_team_meta`), so the two runs compare.
  - **Result.** APPROVED in round 2, jev gate PASSED, 723 s (2026-10-19: approved in round 1, about 11.5 min). Every seat
    ran on its own CLI (expert and judge claude, security critic codex, QA and release critics agy), no fallback.
    Retrieval 88 results / 484 KB (2026-10-19: 73 / 404 KB). The judge's actions (`apply_patch`, `run_tests`) stayed
    `PROPOSED`; nothing was applied.
  - **Cards.** Each of the ten saved prompts carries the card of its own seat and no other. Expert and judge system
    prompts grew by the added sentences only (38574 -> 38670 and 43633 -> 44245 bytes).
  - **Findings.** The judge approved the two real defects of the 2026-10-19 trial (a client `modified` is validated and
    then overwritten; a stored `created` is reused without a check), named the fixed `.tmp` name and left it out as older
    than the release, and held back an optional format tightening with a concrete reason (its pattern accepts the offset
    `+00:60`). The judgment has the approved and held-back sections the judge card asks for.
  - **Why round 1 failed.** The expert's first proposal changed `created`/`modified` handling in a way that breaks the
    contract tests in `test_config_ops.py`; the QA and release critics showed it with file and line, graded high, and
    the judge rejected because a high objection was unresolved (judge card). In round 2 the expert kept the contract and
    the judge approved. The 2026-10-19 judge had approved in round 1 and held that change back itself.
  - **Format and citations.** Critics graded every objection (they wrote in English, `High`/`Medium` for the card's
    높음/중간): no format regression. The first citation check found unverified citations more often than on 2026-10-19
    (round 1: expert 2, QA critic 1, judge 2; then: judge 1); the expert's two named files it had only seen in grep
    output. In every case the engine asked, the session read the lines, and the re-check found none; round 2 had none,
    evidence mismatches were 0 throughout. Counted as no citation regression, since the gate's rule held; watch the
    first-check count on the next code debates.
  - **Entry condition (1) for the ops teams is met.** Conditions (2) to (4) are not started.

- 2026-10-31, C1 for the code teams (deployed the same day as `6e0fb08` after the operator accepted the wording of the
  six personas below; trial debate in the note above).
  - **Golden dump first.** `test_seat_golden.py` runs every builtin team once under the fake LLM of
    `test_flow_golden.py`, with claude as every seat's provider so that each seat has standing instructions, and keeps
    each seat's composed system prompt in `golden/seat_prompts.txt` (one sentence per line, the shared retrieval and
    ops protocols as hash and size, the hash of the exact text per seat). It was committed before any change;
    `python3 test_seat_golden.py --review <revision>` lists per seat the sentences that went and came.
  - **Engine.** `role_cards.py` holds the cards (seat x kind; only the code cards, kind `None`, which other kinds fall
    back to) and `persona_text(persona, seat, kind)`. A persona opts in with `"role_card": "expert" | "critic" |
    "judge"`, its home seat; every persona without it is sent byte for byte as before, so custom personas on the hub
    and every ops, design and general builtin are untouched. Every place that put `system_prompt` into a turn now
    calls `persona_text` with its seat: judge; expert, proposers, consolidate and rewrite, pipeline stages and versus
    sides (expert card); critics, verify and the batch vote (critic card). Deviation from chapter 4: verifier, stage and
    side seats get the card of the seat whose rules the persona carried there before (critic, expert), so a flow seat
    keeps what it was sent; dropping it is left to C2. The card goes right after the persona,
    joined with a space, so a moved sentence reads as before. The solo agent and the suggest step's examples use the
    home seat's card. `RETRIEVAL_PROMPT_VERSION` 2026-10-31.p24. `persona_manager` validates `role_card`; the
    dashboard editor keeps it on edit and copy, drops it for an AI suggestion or a template (whose texts carry their
    seat rules) and shows a hint under the system prompt that the engine adds the card.
  - **Personas.** Nine builtins opted in: `domain_architect`, `guardian_lead_dev` (expert); `critic_redteam`,
    `devops_specialist`, `guardian_security_critic`, `guardian_qa_critic`, `guardian_release_critic` (critic);
    `jev_mcp_decision`, `guardian_dev_judge` (judge). Lint after: `move` 55 -> 37 (no code persona has one), `note`
    11 -> 10, median 501 -> 463 characters.
  - **Review of the dump.** 22 of 30 seats are byte-identical (every ops and design seat, and in the code teams
    `critic_redteam` and `jev_mcp_decision`: the cards are their sentences). `domain_architect` only changed order.
    Deviation from "only reordering and deduplication": the six other code personas worded the same rules
    differently, and one card per seat unifies them, so they also gained sentences: `devops_specialist` (as critic)
    gains the scenario and the request for code and loses "바꿀 파일 … 실패했을 때 되돌리는 방법" from its proposal
    sentence (it is a critic only, in `cross-review`, so it never wrote a proposal); `guardian_security_critic` "시나리오는 공격자가 어떤 입력으로 무엇을
    얻는지로 쓰세요." replaces "공격 시나리오" and it gains the request for code; `guardian_qa_critic` gains the severity grade
    and "문제가 없으면 없다고" (it keeps "재현하는 입력과 기대 결과"); `guardian_release_critic` gains the no-guessing rule, the
    scenario and "문제가 없으면 없다고" (it keeps "명령 출력도 근거로 인용할 수 있습니다."); `guardian_lead_dev` gains "과제가
    질문이면 코드로 확인한 답을 쓰세요." (its ci.sh sections stay as a persona sentence); `guardian_dev_judge` gains the two
    tool sentences, "주문이 요구하지 않은 변경은 넣지 마세요" and "점검 결과" (it keeps tests, no secrets, no deploy). The flow
    golden (`golden/flows.json`) was regenerated: only prompt sizes and hashes changed, and `builtin-fleet-ops/plain`
    did not change.
  - **Entry conditions for the ops teams** (`fleet-ops`, `fleet-services`, `fleet-mesh`, `fleet-provision`):
    (1) the code-team change is deployed and one code-team debate ran on it without a format or citation regression;
    (2) ops cards are written from the ops personas' own sentences (the `[#번호 조회 호스트]` citation, "이의는 둘로 나눠
    적으세요." and the `[차단]`/`[권고]` grading for critics, the `[권고]`-become-checks rule for judges, the "ask one value"
    rule for provisioning), so that `test_seat_golden.py --review` shows only moved sentences for `fleet-ops`, whose
    prompts were kept byte-identical on purpose; any added sentence is listed and approved by the operator first;
    (3) one trial debate per changed team on the hub sandbox `~/guardian-eval` (shared Codex quota: the operator's OK),
    with `fleet-provision` compared against its calibrated 2026-10-19 behaviour (the judge invents no prohibitions);
    (4) no change to the tool-keyed judge rules (plan, `bundles`, batch), which stay in the engine.

- 2026-10-31, deploy and browser check: C0 and the suggest step deployed to the hub as `670d6c7` (`f42a5a7` plus a
  test fix: `test_llm_runner.py` failed on a Mac whose login shell exports `ZAI_API_KEY`, since zai is detected by that
  variable alone; the test now clears it). Checked in a real browser on the hub dashboard: "Suggest with AI" on a new
  persona (Critic seat, code) ran against the hub's real claude CLI in one to two minutes and returned a draft with no
  guide warnings; for a new persona it also fills the ID field (there is no existing ID to keep). A sentence naming
  `GUARDIAN_AUTO_APPLY` and `scripts/ci.sh` was added to the draft before saving; the list page then showed both
  `warn` lines (repository path, environment variable) under the "saved" notice. The check persona was deleted
  afterwards (list back to 30, no file left on the hub).
- 2026-10-31, C0: `persona_lint.py` (rules of chapter 3 as `warn`/`move`/`note`, shared sentences), its summary in
  `scripts/ci.sh python`, warnings in the save response (`config_ops.save_persona`) and the suggest step (which now
  uses the same checks, plus tool names). Deviation: the lint checks today's `system_prompt`, not the rendered persona
  block of 2.5, which does not exist before C2; seat and kind rules are `move`, not errors, because builtins need them
  until C1. Builtin baseline: 29 personas, 8 `warn` (4 over 700 characters: `ops_provision_judge` 958,
  `ops_provisioner` 834, `ops_service_engineer` 822, `ops_mesh_engineer` 790; 3 repository paths in the
  `guardian_*` personas, written for this repository on purpose, and `AGENT_TOKEN` in `guardian_security_critic`),
  55 `move`, 11 `note`; median 501 characters; 4 sentences repeated in 3-5 personas (the ops citation rule twice, "이의는
  둘로 나눠 적으세요.", and the provisioning "ask one value" rule).
- 2026-10-30, suggest step (2.6): built ahead of C0-C2 on the current persona format, at the operator's request. The
  draft therefore is a free-text `system_prompt` (not the persona block of 2.1) and keeps the seat conventions the
  examples show, since role cards (C1) do not exist yet. `profile` is added to the persona schema now (only `tagline`
  and `look`; `avatar`, `color` and `portrait` come with C2/C5). Checked in a real browser against the hub's real model on 2026-10-31 (first note); tests use a fake runner (`test_persona_suggest.py`) and a fake CLI (`TestPersonaSuggest`).

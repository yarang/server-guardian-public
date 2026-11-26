# Solo agent: one role, no Judge, read-only (design)

English | [한국어](solo-agent.ko.md)

Status: proposed 2026-10-30; the operator chose the reading "one role run alone" the same day ("interpretation A").
S1 and S2 built and deployed 2026-10-30 as `06531f9`; S3 built 2026-10-30, deployed as `c5d9d45`. General (no-project) mode
decided and built 2026-10-30 (decision 6, see Implementation notes); the provider scope was widened the same day by the
operator's word (any provider but `zai`).
Origin: "Should standalone agent execution be considered?" A debate always closes with the Judge, the jev gate and
action validation. Sometimes a person only wants one agent to read the code (or a host) and answer, quickly, without
that machinery.

## Problem

Every way to run an agent today is a debate: even the smallest flow (`draft` alone, `flow-templates.md` T3a) ends in
the shared close of a round. That close is the reason a result can be trusted and acted on, and it costs a Judge
call, a gate and a thread. For a question like "where is the retry logic and what does it do?" this is too much, and
people ask the question in a chat window instead, where nothing is cited and nothing is checked.

## Decision scope and non-goals

- A solo run is **not a flow**. `team-flows.md` and `flow-templates.md` keep "flows without a Judge" and "approval
  without the jev gate" as non-goals; this document does not touch them. A solo run has no Judge, no round, no
  thread and no approval, and its result says so.
- It is **read-only and advisory**: it can read the repository (and run read-only ops checks), and it can answer. It
  proposes nothing that can be executed.
- It adds no new LLM access: same providers (`claude`, `codex`, `agy`, `zai` where a key is set), same CLI wrappers,
  same credential rules (no key or login file is copied between hosts).
- Python stays standard-library only. The Go hub gets a thin route; it does not run models.

## Model

```
question + project (+ role persona, provider)  ->  one agent session  ->  answer + checks
```

- **Role.** A persona from the existing catalog (`PersonaManager`), default `expert`. The persona text is the
  standing instruction; no team is needed. A role that exists only to judge (`judge`) is refused.
- **Tools.** The same read-only repository tools a debate agent gets: the ```repo block protocol or, for `claude` and
  `codex`, the repo MCP server (`repo_mcp.py`). For an `ops` project the read-only ```ops checks (`ops_checks.py`).
  No `patch`, no action blocks, no `jev_mcp`.
- **One session, many turns.** Retrieval follow-ups (repo results, citation repair) happen inside the call exactly as
  in a debate turn, so the answer is as grounded as a debate draft. There is no second agent to correct it.
- **No Judge** means no verdict. The answer is labelled `SOLO` (never `PASSED`, `APPROVED` or `UNVERIFIED`; those words
  are verdicts of the shared close). The label travels with the result everywhere it is shown.

## What is closed

Everything that turns an answer into an effect is unreachable from a solo run, by construction rather than by a flag:

| Path | Why it cannot be reached |
|---|---|
| Actions (`apply_patch`, `execute_code_action`, `ops:*`, batch plans) | The solo prompt has no action protocol, and a solo result is never parsed for action blocks. A block that appears anyway is kept as text and ignored. |
| jev gate, votes, auto-apply, auto-run | They run in the shared close of a round, which a solo run never enters. |
| Design artifacts (`jev_mcp:write_artifact`) | The tool is not attached. |
| Fleet writes | Solo ops access is the read-only check set only; no `/api/exec`, no command set, no script. |
| Another host's credentials | A solo run uses the credentials of the host it runs on, as a worker does. |

A test pins the list (`test_solo_agent.py`): a solo prompt contains none of the action protocol texts, the runner
passes no write tool, and a result with a forged action block produces no action row.

## Run shape

- CLI: `guardian.py --agent-run QUESTION [--project ID] [--role PERSONA] [--provider NAME] [--context-path GLOB]
  [--context-ref REF] [--result-file PATH] [--events-file PATH] [--json]`. Same project, workspace and context flags
  as `--debate`. `--provider` names one provider; without it the first available one runs and a failed provider falls
  back to the next, as a debate member does.
- **Storage.** No thread, no `debate-jobs` row. One record under `<GUARDIAN_DATA_DIR>/solo-runs/<id>/` holds
  `result.json` (question, role, provider, commit sha, answer, checks, timings, `label: "SOLO"`) and `events.jsonl`.
  Records expire after 30 days (`--solo-prune`, run by the existing ops-watch timer).
- **Concurrency.** Its own slot, `GUARDIAN_MAX_SOLO` (default 2), separate from debates and audits so a question does
  not wait behind a long job, and a burst of questions cannot starve production debates. A third run is refused with
  "busy", not queued.
- **Sessions.** A solo run starts a fresh session each time; there is no thread to resume. (A follow-up question is a
  new run that may include the earlier answer as context text the person pastes.)

## Checks on the answer

Without a Judge, deterministic checks are what remains, and they are cheap:

- **Citation check** (`evidence.py`, the line-span rule): every `path:line` the answer cites must lie in lines the
  session was shown. The result lists each citation as `verified`, `unshown` or `missing`; the summary line says how
  many of each. This is the same check a debate draft passes, with the same slack (`GUARDIAN_CITATION_SLACK`).
- **Secrets.** `is_secret` applies to every path component as in a debate, so a solo agent cannot read `*.enc` files or
  credential bundles.
- **Provider fallback.** When every provider fails the result carries the canned fallback and `label: "SOLO"` plus
  `degraded: true` (the unverified flag of a debate round, under a name that is not a verdict).

No model grades the answer. If a person wants a second opinion the answer is passed to a debate (`Run as debate`,
below); the solo run does not do it.

## Where it shows

- **CLI.** Plain text with a header line `SOLO answer (unverified, no Judge)`, the citation summary and the sha.
- **API.** `POST /api/solo` (`{question, project, role?, provider?, context_paths?}` -> `{id}`; the Go handler spawns the
  CLI like a debate), `GET /api/solo/{id}`, `GET /api/solo` (recent, newest first). Auth is the existing global chain.
  Rate limit per token, same as debates.
- **Dashboard (S3).** A "Quick ask" entry on the Workflows page: a text box, a project and a role picker, the answer with
  the label as a chip and the citation counts. Two buttons on a finished answer: **Run as debate** (prefills the New
  debate form with the question, project and the answer as reference text, so the shared close can test it) and
  **Copy**. Strings go into both catalogs (`ko`, `en`), as everywhere.
- **Workflows list.** Solo runs are not listed with debates (they are not debates); they have their own short list.

## Read tier: no approval gate, catalog only

A solo run needs no approval because everything it can do is a read, and the repository already treats reads that way
(```ops checks of `ops_checks.py` run without approval and are logged as `source=ops_check`; repository tools have no
gate). What makes this safe is how "read" is decided, not a gate:

- **A catalog, not a classifier.** A read is a named item (`df`, `journal_tail`, `service_status`, `list_dir`, ...) with
  typed arguments, run as an argv list and never as a shell string. A free-form command is never judged "read-only":
  shapes that look like reads and write (`find -exec/-delete`, `sed -i`, `sort -o`, `tee`, `xargs`, redirects, pipes
  into a shell, `awk system()`, `git` hooks and pagers, `curl -o`) cannot be told apart reliably by pattern. A command
  outside the catalog is not run by a solo agent; it goes to the normal approval path.
- **What stays without the gate.** The audit line (`source=ops_check`), the path deny rules (`is_secret`, `*.enc`,
  credential bundles, `/etc/shadow`; they apply to reads too), a time limit and an output size cap, and the host scope
  the person chose.
- **A read is a leak path.** Output goes into the model's context, so secrets can reach the provider. No catalog item
  dumps whole environments or config trees; the deny rules and masking stay on.
- **Growing the catalog** is a separate change; each new item is checked against the write-shape list above by a test.

Out of scope here: running commands that change things. A command typed by a person, or proposed by an agent and
approved by a person, uses the existing paths (`/api/exec`, command sets, `ops:run_script`) with their deterministic
guards (`deny_commands`, bypass shapes, protected units and packages, `STALE`, audit). Neither needs an LLM Judge:
the decider is already a person, and deterministic guards cover what a model would only guess at. The Judge and the jev
gate stay for open questions where it is unclear what should be done (the `fleet` debates).

## Entry points: CLI and skill

- **CLI** `guardian.py --agent-run` is the mechanism (record, slot, citation check, closed paths).
- **Skill** is the front door for an LLM CLI or a person speaking naturally: one thin skill under `deploy/llm-skills/`
  (for example `guardian-ask`) that maps "where is X / what does Y do" to `--agent-run` and prints the label. It holds
  no logic and no tools of its own. This follows the existing split: `guardian-host-check` is already a Judge-less,
  read-only single agent in skill form, and `guardian-fleet-ops` is a front door to a debate. The read catalog is shared
  by the skill's checks and `ops_checks.py`, so the two cannot drift.
- **Not a skill:** anything that needs independence or a guarantee (Critic, Judge, gate). A skill is an instruction to
  one agent and enforces nothing; those are flow templates (`flow-templates.md`).

## Phases

- **S1 engine, CLI and skill.** `solo_agent.py` (new, small): builds the session from a persona, runs one call through the
  existing `_ask`-level machinery, applies the citation check, writes the record; the `guardian-ask` skill and the shared read catalog come with it. To stay close to the debate path it
  reuses `DebateEngine`'s agent call (the repo block loop, `McpTurn`, provider fallback) and does not copy it; if that
  coupling is awkward, the first step extracts the call into a function both use, under the golden gate. Tests: the
  closed-paths list, citation labels, provider fallback, fake LLM, scratch data dir (`testenv.isolate_data_dir()`).
- **S2 API.** Go routes, slot, pruning in the ops-watch service, `GET` list. No dashboard.
- **S3 dashboard.** Quick ask, result view, Run as debate, strings in both catalogs.

No prompt of an existing role changes in S1-S2, so `RETRIEVAL_PROMPT_VERSION` stays; the solo suffix is its own text
with its own version constant (`SOLO_PROMPT_VERSION`).

## Decisions proposed (need the operator's word)

1. **Label.** `SOLO` and the visible words "unverified, no Judge" rather than a status that looks like a verdict.
2. **Ops projects.** Allowed with the read-only check set only (the same set a debate agent has). Fleet-wide questions
   still go through `fleet` debates when anything is to be done.
3. **Storage.** A separate `solo-runs/` record with 30-day expiry, not a thread, so it never reaches the decision DB
   and cannot be "continued" into a verdict.
4. **Slot.** `GUARDIAN_MAX_SOLO` default 2.
5. **No model-side grading** in v1; "Run as debate" is the path to a second opinion.
6. **General mode (decided 2026-10-30, revises the "question + project" model).** A question may be asked with no project:
   `general: true` (CLI `--general`), exclusive with project, context paths and ref. It is answered from the model's general
   knowledge only. Any provider may answer except `zai` (a third-party endpoint enabled for code work). The first version
   allowed `claude` only, the one CLI that runs with all tools off; the operator widened it the same day ("not being able to
   use external tools is not essential"), so `codex` and `agy` may answer with their built-in tools, and "no sources" is a
   label, not a guarantee. No repository, no ops checks, no wiki is attached; the result is labelled `SOLO` as always and says
   "unverified, no Judge, no sources". Closed paths are unchanged (nothing is attached that could act).

## Open points

- Whether the Codex quota (shared with production debates) needs a per-day cap for solo runs; measure first, then decide.
- Whether `--agent-run` should accept a worker host (`host`) like a team member. Not in S1: the worker design runs one
  role statelessly already, and a solo run on a worker would need the snapshot protocol; add it only if asked.
- Whether to allow a custom persona text typed at start (not stored). Not in S1; a stored persona is reviewable, a typed
  one is not.

## Implementation notes

### S1 (built 2026-10-30, deployed as `06531f9`)

Decisions 1-5 were approved by the operator on 2026-10-30. Deviations and facts the design did not state:

- **Engine reuse, nothing extracted.** `solo_agent.py` builds a `DebateEngine(max_rounds=1, db_path=<scratch>/session.db,
  jev_gate=False, events=<own writer>)` as `audit_run.py` and `wiki_build.py` do and calls `engine._ask(...)` directly.
  No engine code changed (`test_flow_golden` passes unchanged). Two details matter: `db_path=None` would fall back to the
  production decision DB, so a scratch DB is mandatory (agent sessions are stored in it and it is deleted after the run);
  and `jev_gate=False` (not `None`) is what keeps the gate unbuilt. A contract test pins the private members the runner
  uses (`_ask`, `_setup_retrieval`, `_citation_check`, `_flush_compiler_events`, `_close_go_index`), so an engine rename
  fails a test instead of the runner.
- **Citation `missing`.** The engine's `cited_locations` knows tree paths only, so a citation of a file that does not exist
  is invisible to it. The runner adds a regex over `file.ext:N` (known tree extensions, no tree path, no basename match) and
  reports those as `missing`; `verified` and `unshown` come from the engine's line-span rule unchanged.
- **Judge refusal.** A persona is refused when its id contains `judge` or `jev_mcp`, or its text contains `jev_mcp`. A test
  runs all 29 builtin personas: exactly the 7 Judges are refused.
- **Repo MCP.** The repo MCP server is attached only when `GUARDIAN_REPO_MCP=1` (the host switch of a debate); its spec holds
  workspace, sha, log and limits, never `jev_mcp` or the wiki directory.
- **Read catalog.** Not shared yet: the runner uses the debate agent's existing read tools (```repo blocks, MCP, and for ops
  projects `OpsChecks`). The skill carries no checks of its own, so nothing can drift; a shared catalog stays with the
  "growing the catalog" item of the read tier.
- **Slots and exit codes.** Slots are `flock` files `.slot-N` in `solo-runs/`; a busy run stores nothing and exits 4 (invalid
  input is 2). `--solo-prune` removes records whose `result.json` is older than `GUARDIAN_SOLO_KEEP_DAYS` (30) and keeps the
  slot files. The ops-watch service calls it since S2.
- **CLI.** `--agent-run`, `--provider` (new), `--role` (existing flag, here a persona id), `--language`, `--context-path`,
  `--context-ref`, `--result-file`, `--json`. The skill is `guardian-ask` (hub only).
- **Answer language.** Korean by default, whatever language the question is in (as the audit and the wiki); `--language NAME` or
  `GUARDIAN_SOLO_LANGUAGE` overrides it.
- Open: an `ops_targets.resolve` error is not yet surfaced in the result of an ops-project run.

### S2 (built 2026-10-30, deployed as `06531f9`)

`apps/agent/solo_handler.go`, `POST /api/solo`, `GET /api/solo`, `GET /api/solo/{id}`, all behind `h.guard` and the global
auth chain. No dashboard (S3). Facts the design did not state:

- **The id is made by Go.** The hub generates `solo_<YYYYmmdd_HHMMSS>_<6hex>` and passes it as `--solo-id` (checked against the
  same pattern in `SoloRunner`, so a bad id is a usage error and nothing is stored). The hub can then answer 202 with the id
  while the run is still going, and `GET` needs no mapping from a job to a record.
- **Start grace.** After spawning, the handler waits `SoloStartGrace` (3 s) for an early exit: CLI exit 2 becomes 400 with the
  CLI's error text and exit 4 becomes 429; both remove the record directory. A refusal that comes later shows as `failed` in `GET`.
- **Two slot guards.** A Go-side in-memory cap (`MaxSolo`, `GUARDIAN_MAX_SOLO`, default 2) answers 429 before spawning anything;
  the CLI's `flock` slots stay as the second guard (also against runs started by hand on the hub).
- **Status is derived.** `running` while the id is in the hub's in-memory set, `completed` when `result.json` is a regular file,
  otherwise `failed` with the last `{"error": ...}` line (or the tail) of `output.log`. A hub restart mid-run therefore shows
  `failed`. Records and `result.json` that are symlinks are refused (404 or `failed`), like the artifact API.
- **Timeout, no cancel.** The job timeout kills the process group and appends a timeout line to the log. There is no cancel
  route; the timeout bounds a run.
- **Request shape.** Unknown JSON fields are refused; project, context paths and ref follow the debate validation, role and
  provider must be ids, the language a short plain name. The CLI stays the only judge of what they mean.
- **Prune.** `server-guardian-ops-watch.service` runs `guardian.py --solo-prune` next to `--artifacts-prune` (`ExecStartPre=-`, a
  failure does not stop the watch).
- Tests: `solo_handler_test.go` (fake CLI, eight cases: start argv, validation, exit 2/4 mapping, slot cap, failed run, timeout,
  id and symlink checks, list order). Nothing has run against a real LLM or the hub.

### S3 (built 2026-10-30, deployed as `c5d9d45`)

Dashboard tab "Quick ask" on the Workflows page (`#/debates/ask`, `#/debates/ask/<id>`), for every project kind. Built by the
z.ai agent from a written spec, reviewed and corrected by the coding agent. Facts the design did not state:

- **Files.** `src/pages/ask/AskTab.tsx` (form, recent list), `AskDetail.tsx` (one run), `src/lib/solo.ts` (route, helpers),
  `src/i18n/{ko,en}/ask.ts`, `startSolo` / `fetchSolo` / `fetchSoloList` in `api.ts`. The tab sits after "Debates".
- **Polling.** A running run is fetched every 2 s, a finished one once; the recent list refreshes every 10 s while the tab is open.
  The list is the solo runs only; they never appear in the debate list.
- **Roles and providers.** The role picker offers the hub's personas minus the ids the CLI refuses (`judge`, `jev_mcp`); the
  empty choice is the default role. The provider picker offers every provider the hub has installed
  (`catalog.installed` of `GET /api/teams`; `zai` only where its key is set), with `claude`, `codex`, `agy` as the fallback
  when the hub reports none; a general question hides `zai` and resets a chosen `zai`, since the CLI refuses it there. Providers
  are sent as short ids (the CLI compares `--provider` with them; the result reports `claude-cli` style names).
- **Answer.** The trailing ```findings block is machine output: hidden from reading, Copy and Run as debate.
- **Run as debate.** Uses the existing hand-off (`setDebatePrefill`, then the debate form): the question plus the unverified
  answer. The debate form refuses a task above 4000 characters, so `debateTaskFrom` cuts the answer (with an ellipsis) to fit.
- **Reference code** (context paths) is hidden for ops and design projects, as in the debate form. The hub's 400/429 messages
  stay in the form.
- Tests: `tests/ask.test.tsx`, `tests/solo.lib.test.ts` (route, findings strip, roles, prefill cut, form body, polling to the
  answer, failed run, Run as debate). Checked in a real browser on 2026-10-31 (hub dashboard in Chrome, nothing submitted): the
  form (project, question counter 0/4000, role select without judge seats, provider select, general box, reference code
  under Advanced, Ask disabled while empty); ticking general drops zai from the providers, shows the general-knowledge
  notice and hides reference code (the project switcher stays, the project is just not sent); the recent list and a stored
  answer (SOLO label, "no verification, no judge", role, provider, 37.1 s, "1 verified" citation, Copy and Run as debate,
  a table rendered, the findings block hidden). Known gap: an ops project's read-only checks only reach hosts
  of its inventory targets, so a question about the hub itself in `fleet` ends with "not a target" (seen in the first real run).

### General mode (built and deployed 2026-10-30)

Decision 6. Python, Go and the dashboard; `SOLO_PROMPT_VERSION` 3 (2 was the first general rules, 3 drops the promise of a tool-less run). Facts the design did not state:

- **Contract.** Request `general: true` (Go refuses unknown fields; a general request with project, context paths or ref is a
  400 `a general question takes no project, context paths or ref`). Result: `project: ""`, `mode: "general"` (`code` / `ops`
  otherwise), `note: "unverified, no Judge, no sources"`, `summary: "no sources"`, empty citations, `sha: ""`.
- **Providers.** `GENERAL_EXCLUDED = ("zai",)` in `solo_agent.py`: a pinned `zai` is a `SoloError` (CLI exit 2, HTTP 400); with
  no provider pinned the candidates are the available providers in default order without `zai`, and none left is the same
  error. The dashboard offers the same list as for project questions. The first version was claude only (`GENERAL_PROVIDERS`,
  tools off); that was dropped by the operator's word.
- **No attached tools.** The engine gets no repository and no ops binding, so the session is the persona plus `GENERAL_RULES`
  (answer from general knowledge, do not look for files, cite no file or line, claim nothing was read or checked). A provider
  with built-in tools (`codex`, `agy`) runs in the CLI's scratch working directory and could still use them: the rules ask it
  not to, nothing enforces it. Roles are the same persona list as for project questions.
- **Limits.** The request is not stored, so while a general run is `running` or `failed` its list row has no `mode` and shows the
  default note; the mode appears once `result.json` exists.
- Tests: `GeneralTests` in `test_solo_agent.py`, `TestSoloGeneralQuestionPassesTheFlagAndKeepsItsOwnLabel` and three invalid
  cases in `solo_handler_test.go`, the form and result cases in `tests/ask.test.tsx`.

### Language rule (2026-10-30, `SOLO_PROMPT_VERSION` 4)

The solo run used the debate's language rule, whose tail names "the decision, rationale, constraints, checks and
action reasons" of a judge it does not have. It now gets `answer_language_rule(..., debate=False)`, which only says
what stays as it is (code, paths, identifiers, format markers). Built on a parallel branch as
version 2 and merged after general mode, so it became 4. Not deployed.

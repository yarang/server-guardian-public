# Verification layer: Guardian checks work that others produce (design)

English | [한국어](verification-layer.ko.md)

Status: proposed 2026-11-23. DP1 (positioning), DP2 (V1 quick review first) and DP5 (hold operator sessions S3-S5)
approved by the operator 2026-11-23; the other decisions are open. V0 done 2026-11-23; V1a (engine and CLI), V1b (hub API) and V1c (dashboard) built 2026-11-23, deployed 2026-11-24 as `e9a939a` (`GET /api/reviews` answered on the hub, the deployed tab not opened in a browser); first real review on the hub 2026-11-24 (claude, 57 s, two real harness defects). V1e run 2026-11-24, one repetition: the review met the decision rule (recall 0.94 vs 1.00, time 0.19 of the debate's, strict precision 1.00 by agent pre-grade, awaiting the operator's confirmation); V3 waits for DP7. DP3 decided 2026-11-24: all V2 entry points in one phase (V2 build plan below). V2 (git refs `--ref`, resident patches `--action`, session MCP tools `review_submit`/`review_result`, dashboard branch mode and the review section on `apply_patch` actions) built 2026-11-24 and deployed the same day as `8131c23` (checked in a real browser on a local hub; no real ref or action review run yet; the workstation session files not reinstalled).
Origin: a comparison with Orca (orca.dev, an "agent development environment": a desktop app that runs many coding
agents side by side, one git worktree per task, full-autonomy permission flags by default, review left to the person
reading diffs). The comparison concluded that the two tools sit at different points: Orca raises throughput, Guardian
raises trust. When a user produces a lot of agent work, the person reviewing diffs becomes the bottleneck; that is the
place Guardian's checks (citations, critics, jev, verdict sources) can fill. This document turns that conclusion into a
direction for development.

## Positioning

Guardian is a **verification and control layer** plus **fleet operations**. It is not an agent development
environment.

- It does not try to be the place where a person writes code with agents (no worktree sidebar, embedded terminals,
  browser tabs or desktop shell).
- It takes work from wherever it was produced (a branch pushed from any tool, a patch from a resident agent, a pull
  request) and answers: is this correct, what is wrong, with which lines as evidence.
- It keeps its permission stance: read-only by default, typed actions, manual approval for writes, sandboxed open
  agents. Orca's default (every agent launched with its full-autonomy flag, "the worktree itself is the sandbox") is the
  contrast this layer exists for, and the documents say so.

## How accounts are used

Both tools run on the user's own accounts; the difference is what an account buys.

| | Throughput tool (Orca) | Guardian |
|---|---|---|
| An extra account gives | another pair of hands (parallelism) | another point of view (cross-checking) |
| Seats | the same provider many times | different providers in different seats |
| Bottleneck | the person's review, the subscription limit | the length of a review, the slots |

Principle for this layer: **a seat is a point of view, not a pair of hands.** A review seat should, where possible, use
a provider different from the one that produced the work (when the author is known: a resident agent, a debate
lead). Quota is spent on disagreement, not on repetition.

## What changes

### V1 Quick review (new run kind)

The gap: today a diff gets either a solo answer (fast, no checks beyond citations, `solo-agent.md`) or a debate (checked,
tens of minutes). A quick review sits between them.

- **Input.** A code project and a change: `base..head` refs in the project's workspace, or a patch file. CLI
  `guardian.py --review --project ID --range BASE..HEAD | --patch FILE [--provider NAME] [--role PERSONA]`.
- **Run.** One reviewer session (default persona: the project's critic; a judge-only persona is refused) on the solo
  runner's machinery: read-only repository tools, no action protocol, own slot `GUARDIAN_MAX_REVIEWS` (default 2).
  The prompt carries the diff; the reviewer reads surrounding code through the usual repo tools.
- **Output.** A findings list, each with severity (`high`/`medium`/`low`), a scenario and citations. Label `REVIEW`
  (never `PASSED`/`APPROVED`; those belong to the shared close).
- **Deterministic checks.** The line-span citation check of `evidence.py`; a finding must cite a changed line or a line
  it shows to be affected by the change, otherwise it moves to `notes` (the audit's precision rule). Registered verdict
  sources (`verdict-sources.md`) may run in the `alarm` role only, for example the project's test command; they add a
  signal, they never decide.
- **Escalation.** "Run as debate" (as in Quick ask) prefills a debate with the diff and the findings, so the shared
  close can test them. A quick review never creates an action.
- **Target.** A diff of up to about 500 changed lines in under 5 minutes on the hub.

### V2 Entry points for work produced elsewhere

- **Git refs.** A branch pushed to the project's remote by any tool (an ADE, a person, a CI job) is fetched into the
  workspace and reviewed by range. No integration code for any particular ADE; git is the interface.
- **Resident patches.** A PROPOSED `jev_mcp:apply_patch` from an open agent (`resident-open-tools.md`, O3) gets a
  "Review" button in Approvals; the review is attached to the approval so the person sees findings before deciding.
- **API.** `POST /api/reviews`, `GET /api/reviews/{id}`, `GET /api/reviews` behind the global auth chain, as `/api/solo`.
- **Session MCP.** A read-only tool pair in `session_mcp.py` (`review_submit`, `review_result`) so an operator session
  on a workstation can ask the hub for a review of a pushed branch. Mail still grants nothing; the tool only starts a
  read-only run.

### V3 Candidate comparison

The throughput pattern "race several agents on one task and pick the winner" leaves the picking to a person reading N
diffs. Guardian can do the comparison.

- Input: two to four candidate changes for one task (branches or resident patches).
- Each candidate gets a V1 review; then critics compare them under neutral rotated labels (fairness rules of
  `multi-perspective.md`, the `versus` order rule of `team-flows.md`) and rank them with reasons and citations.
- The person approves one; the others are discarded. Nothing is merged automatically.
- Built as a step kind in the flow catalog (`flow-templates.md`, `flow-graph.md`), not a new engine path. Starts only
  after V1 has measurements.

### V4 Task board: attempts and waiting for the operator

Two ideas from Orca's orchestration vocabulary fit the resident task board (`resident-agents.md`) at low cost:

- **Attempts.** Each claim of a task by an agent is its own record (agent, epoch, start, end, outcome), so a task that
  was retried shows its history instead of only its last state.
- **Waiting for the operator.** A task state `waiting_operator` set when an agent asks the operator a question under the
  task; it clears when the operator answers. The monitor tab lists these first.

The existing safeguards (hop limit, epoch fencing, mail grants nothing) stay as they are.

## Scope adjustments

Proposed holds and limits, each to be decided by the operator (see Decisions):

- **Operator sessions.** Finish the S2 acceptance test (WSL host), then hold S3-S5. The part that is Guardian's own (a
  session joins the mail store under a name; mail grants nothing; keys held by the hub) stays; remote-control UX for
  interactive sessions is available in external tools and is not this layer's job.
- **Providers.** No new provider unless a seat needs a point of view the current set cannot give. Further models go
  through `opencode` where possible instead of a new wrapper.
- **No ADE features.** No worktree manager, embedded terminal, browser pane or desktop shell in the dashboard.

## Decisions

| # | Question | Recommendation | Decision |
|---|---|---|---|
| DP1 | Adopt the positioning (verification and control layer + fleet operations, not an ADE)? | yes | approved 2026-11-23 |
| DP2 | Build V1 quick review as a new run kind on the solo machinery? | yes, first | approved 2026-11-23 |
| DP3 | V2 entry points: git refs, resident patch button, API, session MCP tools; all four or a subset? | git refs and resident patch first, API with them, session MCP after S2 acceptance | all four: git refs, resident patch button and session MCP tools built together in one phase (the API exists since V1b); the operator chose not to wait for S2 acceptance (WSL still pending); decided 2026-11-24 |
| DP4 | Default reviewer provider differs from the author's provider when known? | yes, fall back to any when none differs | open |
| DP5 | Hold operator sessions S3-S5 after the S2 acceptance test? | yes | approved 2026-11-23 |
| DP6 | Freeze new providers; use `opencode` for further models? | yes | open |
| DP7 | V3 candidate comparison: plan now or after V1 measurements? | after V1 measurements | open |
| DP8 | V4 attempts and `waiting_operator` on the task board? | yes, small, with the next resident change | open |

## Phases

- **V0** Positioning written into `README.md`, `docs/README.md` and the glossary (after DP1).
- **V1** Quick review: CLI, runner, checks, record under `reviews/` (30-day expiry through the ops-watch prune), API,
  dashboard entry next to Quick ask, both catalogs.
- **V1e** Evaluation (below). Its result decides V3.
- **V2** Git-ref fetch, resident patch "Review" button, session MCP tools.
- **V3** Candidate comparison step kind (only if V1e passes).
- **V4** Task board attempts and `waiting_operator`.

## V1 build plan

Each step ends with `scripts/ci.sh` for what it touched; deployment after V1c, from a clean tree, with the operator's
OK (this repository's gate).

- **V1a engine (`cli/llm-agent/review_agent.py`).**
  - `ReviewRunner` reuses the solo runner's setup (project, persona, provider, scratch decision DB, events), code
    projects only; ops and design projects and general mode are refused.
  - Change input: `--range BASE..HEAD` resolved with `git rev-parse` in the workspace; `--patch FILE` checked with
    `git apply --check` against the head in a throwaway worktree. The diff is cut at `GUARDIAN_REVIEW_MAX_LINES`
    changed lines (default 1500); a larger change is refused with a hint to split it, never silently truncated.
  - Repository tools read at the head commit (to verify: the retrieval setup pins the sha given as the context ref).
  - Prompt: review rules, the diff, the audit's findings block (severity, claim, scenario, citations);
    `REVIEW_PROMPT_VERSION` 1. A missing or broken block is re-asked once, as in audit version 4.
  - Checks: the line-span citation check; the change rule (a finding cites a changed line within
    `GUARDIAN_CITATION_SLACK`, or a shown line of a caller of a changed symbol, otherwise it moves to `notes`).
  - Record `reviews/<id>/result.json` + `events.jsonl`, slot `GUARDIAN_MAX_REVIEWS` (2), `--review-prune` (30 days) in
    the ops-watch unit next to `--solo-prune`.
  - Tests `test_review_agent.py` (data dir isolated by `testenv`, fake LLM, scratch git repository): range and patch
    input, size refusal, change rule, notes, re-ask, label never a verdict, no action row from a forged block.
- **V1b hub (`apps/agent/review_handler.go`).** `POST /api/reviews`, `GET /api/reviews/{id}`, `GET /api/reviews`
  modelled on `solo_handler.go`, behind the global auth chain; Go tests.
- **V1c dashboard.** Tab "Quick review" (`#/debates/review`) next to Quick ask: project, range or pasted patch,
  findings by severity with citation chips, notes folded, the `REVIEW` chip and "no Judge" line, "Run as debate"; both
  catalogs; checked in a real browser at desktop width and 390 px.
- **V1e evaluation** as below, after the deploy.

## V2 build plan

Every entry point ends in the existing `ReviewRunner`: two new change inputs on the CLI, and each surface is a thin
caller of them. One validator, one slot (`GUARDIAN_MAX_REVIEWS`), one record format. A review still never creates,
changes or decides an action.

- **V2a git refs (`review_agent.py`, CLI).** `--ref BRANCH [--base REF] [--expect-sha SHA]`.
  - Fetched only from the project's configured `origin`; a URL never comes from the request. The branch passes
    `validate_ref` and `git check-ref-format --branch` and may not start with `-`.
  - One fetch with explicit refspecs into a private namespace: `+refs/heads/<branch>:refs/guardian/review/<id>/head`
    and the project branch into `refs/guardian/review/<id>/base-tip`, with `--no-tags`, `--no-write-fetch-head`
    (falls back when the hub's git lacks it), `GIT_TERMINAL_PROMPT=0` and a timeout. `refs/remotes/origin/*`, local
    branches, HEAD, the index and the working tree are never touched, so the publish lock is not needed.
  - Base: `--base` when given, else the merge-base of head and `base-tip`. `--expect-sha` refuses when the fetched
    head differs (closes the push-then-move race). Both shas and the branch go into `result.json` under `source`.
  - The `refs/guardian/review/<id>/*` refs live as long as the record: `--review-prune` deletes them with it, so
    "Run as debate" still finds the head commit.
- **V2b resident patches (`review_agent.py`, CLI).** `--action ID`.
  - The decision DB is opened read-only (`mode=ro`); only a PROPOSED `jev_mcp:apply_patch` with a code project and a
    `base_sha` is accepted. What is reviewed is the action's `edits` (what an approval would apply), not the task's
    raw patch: the head commit is built from the edits on a temporary index at `base_sha` (hash-object, update-index,
    write-tree, commit-tree); the workspace is never touched.
  - The record stores `source: {kind: "action", action_id}`; the decision DB schema and the action's status are
    unchanged.
- **V2c hub (`review_handler.go`).** `reviewRequest` gains `ref`, `base`, `expect_sha`, `action` and an optional
  `started_by` label (shape `session:<workstation>` or empty); exactly one of range, patch, ref, action. Go checks
  shape only, the CLI checks meaning. `GET /api/reviews?action=ID` lists the reviews of one action.
- **V2d session MCP (`session_mcp.py` -> `session_local.py` -> `session_daemon.py` -> `session_uplink.py` ->
  `session_link.py`).** Tools `review_submit` (project, ref, base, expect_sha, provider; no patch over the link) and
  `review_result` (review id, `wait_sec` capped as in `resident_query_get`). Every hop lists the two ops
  (`TOOL_OPS`, `MCP_OPS` with a `_check_*`, the daemon handlers, `LinkClient.OPS`, the link handlers); the hub side
  validates everything again, since the workstation is untrusted. The link entry does not spawn the review: it calls
  the hub's own `POST /api/reviews` on `127.0.0.1:9870` with the hub's `AGENT_TOKEN` (read on the hub, never sent to
  a workstation), with `started_by: session:<workstation>` taken from the link line, never from the client.
  `review_result` answers only reviews that workstation started. Mail still grants nothing.
- **V2e dashboard.** Quick review gains a "Branch" mode (branch, optional base). Approvals gains a "Review" button
  on PROPOSED `apply_patch` cards and shows the attached reviews (status chip, findings count, link to the review);
  the approve and reject flow is unchanged. Both catalogs; checked in a real browser on a local hub at desktop width
  and 390 px.
- **Tests.** Data dir isolated by `testenv`; a scratch bare repository serves as `origin`. Ref: fetch into the private
  namespace only, invalid and option-like names refused, `--expect-sha` mismatch refused, prune removes the refs.
  Action: one modified and one added file round-trip to the same tree as applying the edits, non-PROPOSED and
  non-apply_patch refused, the action row unchanged afterwards. Session: each hop accepts the two ops and refuses
  extra fields, `started_by` comes from the link, a foreign review id is refused.
- **Deploy.** Hub first (a new workstation daemon talking to an old hub fails on an unknown op). The workstation side
  (`session_*.py`) is reinstalled on a workstation only with the operator's OK; no permission allow rule is added for
  the new tools without asking.

## Evaluation (V1e)

Use the existing planted-defect material (`proposer_eval.py` tasks, the A4 audit seeds) turned into diffs. Compare a
quick review against a `default` team debate on the same diffs, one repetition first, Codex quota after the operator's
OK. Decision rule: V1 is kept as the default first check if its recall is at least 0.8 of the debate's at no more than
a quarter of the time, with strict precision not lower than the debate's.

## Non-goals

- Running agents to produce code at scale (that is the producing tool's job).
- Any dependency on a particular ADE, its cloud relay or its API.
- Changing the shared close, the jev gate or the approval rules; a quick review is advisory like a solo run.
- Auto-merging or auto-applying anything a quick review or a comparison recommends.

## Risks

- **A label read as a verdict.** People may treat `REVIEW` with no high findings as approval. The label, the "no Judge"
  line and the Run-as-debate button stay on every view, as with `SOLO`.
- **Diff-only blindness.** A reviewer that reads only the diff misses effects elsewhere. The prompt requires reading
  callers of changed symbols through the repo tools; V1e measures it.
- **Quota.** Reviews of every pushed branch could eat shared quota. Reviews start only on request (button, CLI, API,
  session tool); no automatic review on push in this document.

## References

- `docs/design/solo-agent.md` (runner, label, checks), `docs/design/verdict-sources.md` (alarm role),
  `docs/design/resident-open-tools.md` (O3 patches), `docs/design/resident-agents.md` (task board),
  `docs/design/operator-sessions.md`, `docs/design/team-flows.md`, `docs/design/flow-graph.md`,
  `docs/design/multi-perspective.md`.
- Orca documentation, read 2026-11-23: overview, worktrees, agents and sessions, supported agents, ways to run, SSH,
  orchestration, bot security, GitHub review, telemetry (orca.dev/docs).

## 구현 메모

- V0 (2026-11-23): positioning paragraph in `README.md`/`README.ko.md`, glossary rows "quick review" and
  "verification layer", operator sessions marked S3-S5 on hold (DP5), document index updated.
- V1a (2026-11-23): `cli/llm-agent/review_agent.py`, `guardian.py --review --project ID --range BASE..HEAD |
  --patch FILE|- [--patch-base REF] [--role] [--provider] [--language] [--review-id] [--result-file] [--json]`,
  `--review-prune` (ExecStartPre of the ops-watch unit), 21 tests in `test_review_agent.py`. Deviations from the plan:
  - The changed files at the head commit go in as the context pack (the code pack's own limits apply), next to the
    diff. Without it the engine's citation check re-asked every time a finding cited a changed file, since the diff
    alone does not count as given. Diff hunks also count as shown lines for the line-span rule.
  - A patch is applied to its base in a temporary index and committed as an object no ref points at (in the
    workspace's object store; `git gc` removes it). The workspace's index, working tree and refs stay untouched, and
    the review then runs exactly like a range.
  - The change rule "a shown line of a caller of a changed symbol" is approximated deterministically: a finding off
    the changed lines stays a finding when its verified quote names a symbol the change declares or touches (hunk
    header context and declarations on changed lines). Everything else goes to `notes` with the reason.
  - The default reviewer is the default team's first critic (`critic_redteam`); a character needs a critic or solo
    seat. There is no severity floor and no re-check session (the audit's): one session, one re-ask for a missing or
    unreadable block. A run whose block stays unreadable is stored with `state: "bad_format"` and its masked text.
  - The slot reuses the solo runner's flock helper in its own directory (`reviews/.slot-N`).
  - Not tried against a real provider yet (only fake runners); V1b/V1c and V1e remain.
- V1b (2026-11-23): `apps/agent/review_handler.go`: `POST /api/reviews` (`{project, range | patch + patch_base?, role?,
  provider?, language?}` -> 202 `{id, status, label, note}`; exit 2/4 of the CLI within the start grace -> 400/429, record
  removed), `GET /api/reviews/{id}`, `GET /api/reviews[?project=ID]` (rows with `state`, `range`/`patch`, file count,
  `counts` by severity plus notes). A submitted patch is written as `change.diff` (0600) inside the run's own record
  directory and passed as `--patch=<path>`; body limit 2 MiB plus 16 KiB, as the CLI's patch limit. The solo child
  launcher became `launchRecordRun`, shared by both. Slots `GUARDIAN_MAX_REVIEWS` (default 2). Seven Go tests.
- V1c (2026-11-23): Workflows tab "Quick review" (`#/debates/review[/<id>]`, `pages/review/ReviewTab.tsx`,
  `lib/review.ts`, catalog `review` in `ko` and `en`), first tab of code projects (not offered for ops and design
  projects): range or pasted patch (+ base), reviewer and provider pickers, recent reviews of the project; the result
  shows the `REVIEW` chip, the "not a verdict" line, findings grouped by severity with quote and scenario, the notes
  folded, the answer text for `bad_format`, and "Run as debate" (prefills a debate with the change and the findings).
  The Overview card "Recent debates" became "Recent checks": the project's three latest quick reviews above its
  debates. Severity counts are shown only for a finished review (`state: "done"`). Checked in a real browser
  (Playwright) on a local hub built from this tree, with `GUARDIAN_LLM_PROVIDERS=none` (the review comes back
  degraded, no model call): start, list, detail, Overview, Settings -> Flows and the start form at 1280 px and 390 px,
  no horizontal overflow. That check found one: a long option text widened `SelectField` past its card at 390 px; the
  shared component now lets the select shrink (every form benefits). The default `#/debates` view stays the debate
  list; the quick review is the first tab, not the landing view.
- V1e harness (2026-11-24, run approved by the operator the same day): `cli/llm-agent/review_eval.py` with
  `test_review_eval.py`. Deviation from the plan: the material is the 16 code defects seeded for audit run A4b
  (`docs/analysis/2026-10-19-audit-a4b/ground-truth.json`, base `a3f5783`) instead of the `proposer_eval.py` tasks,
  which are document defects and do not fit a code review. `--prepare` clones a full checkout into the eval directory,
  commits four cases of four defects each (one of each kind: AUTH, IGNOREDERR, BOUNDS, DEADBRANCH, in four files),
  writes the eval project file into the eval data dir and a `run.sh` that runs, per case, one quick review and one
  `default`-team debate (one round, the diff in the task, the changed files at the head commit as context). The runner
  refuses a data dir under `~/.guardian/data` and sets `GUARDIAN_AUTO_APPLY=off`. `--score` counts a review finding
  as a hit within 5 lines of the seeded line (hits that landed in notes are counted apart), pre-grades the debate on its
  decision and critiques (file name plus a near line or the function name), and writes `score.json` and
  `grading.json`; the precision condition of the decision rule stays pending until the grading sheet is filled.
- First real review (2026-11-24, hub, range `adabfd2..c2b5bd0`): done on claude in 57.1 s, two medium findings, both
  real defects of the V1e harness: the runner's rerun check looked only at the file, which the shell redirect creates
  even when the review fails, and the summary counted reviews that did not finish while the decision ignored them.
  Both fixed after V1e: each output is written to a temporary file and moved into place only on exit 0, and the
  decision is `incomplete` when a review or debate did not finish.
- V1e result (2026-11-24, one repetition, sandbox `~/guardian-eval/data-review`, auto-apply off; `score.json` and
  `grading.json` in `docs/analysis/2026-11-24-review-eval/`): review 15 of 16 seeded defects (recall 0.94), mean
  40.1 s, 15 findings all on a seeded defect, none in notes, every review finished; missed IGNOREDERR-3 (case 3,
  `go_facts.py`: a goindex error with a leftover facts file is cached as built). Debate 16 of 16 (recall 1.00 by the
  loose automatic pre-grade), mean 206.1 s, no fallback; case 2 ended DISPUTED, the others APPROVED. Recall share 0.94
  (rule 0.8) and time share 0.19 (rule 0.25) pass. Strict precision: an agent read all 15 review claims against the
  ground truth and each names its planted defect, so review precision is 1.00 and the precision condition holds; this
  is a pre-grade for the operator to confirm, not the hand grade. Limits: one repetition; tiny diffs (four seeds per
  case, debates of three to four minutes), so the time share was not measured near the ~500-line size the review is
  meant for; debate recall comes from the loose pre-grade; every seed sits on a changed line, so diff-only blindness
  (the risk above) was not measured. The debates' PROPOSED actions stay in the sandbox decision DB, not approved.
  Outcome: V1 stays the default first check; whether to plan V3 now is DP7.
- V2 (2026-11-24, V2a-V2e built by z.ai agents in parallel from one written contract, reviewed and completed by
  claude; deployed 2026-11-24 as `8131c23`). Deviations from the plan and notes:
  - The hub records the source at start in `request.json` (0600) next to the record, so a running review already
    shows what it reviews; `result.json.source` is `{kind: "ref", ref, base, head, base_ref}` or
    `{kind: "action", action_id, base, head}`. The list rows gain `source_kind`, `ref`, `action_id` and `started_by`.
  - The ref fetch also passes `--refmap=` so a configured `remote.origin.fetch` cannot add a tracking ref next to the
    explicit refspecs. `--review-prune` deletes `refs/guardian/review/<id>/*` with the record and sweeps every code
    project workspace for review refs whose record is gone.
  - The link op behind the MCP tool `review_result` is named `review_get`. Both link handlers first check that the
    calling session is bound and live under the stream's workstation and lease (new `session_live` in
    `mail_sessions.py`), as the store-backed handlers do. A review another workstation or the hub itself started
    answers `not_found`, exactly as an unknown id. Only `id`, `status`, `label`, `note` of the hub's answer cross back.
  - The dashboard's review section sits in the action dialog that Approvals opens (`ActionDialog.tsx`, new
    `pages/review/ActionReviews.tsx`), shown for every `jev_mcp:apply_patch` (the "Review" button only while
    PROPOSED), polling every 5 s while a review runs and paused while the page is hidden; it never locks approve or
    reject. The branch check lives in `pages/review/source.ts` and mirrors the CLI's `validate_ref`.
  - Checked in a real browser on a local scratch hub built from this tree (`GUARDIAN_LLM_PROVIDERS=none`) at desktop
    width and 390 px: branch mode, list and detail names, action section; no overflow from V2. Not from V2: at 375 px
    `.sg-seg`/`sg-tabbar` overflow by 10 px; the first load of one detail said "signal timed out" while the API answered
    in 30 ms (retry worked, seen only on the scratch hub). Review ids started by the Go hub use UTC, those from the CLI
    local time.
  - No real `--ref` or `--action` review has run yet (quota; the operator's OK first). The Mac's session files
    (`session_*.py`) are not reinstalled, so the two MCP tools are not available on the workstation yet.

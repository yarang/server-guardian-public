# Whole-repository audit mode (design)

English | [한국어](repo-audit-mode.ko.md)

Status: five decisions made (2026-10-18, all recommended defaults). A1 (plan and checkpoints) and A2 (session run) are implemented and deployed. A4 first run done 2026-10-19 (`docs/analysis/2026-10-19-audit-a4/results.md`: small scope, recall equal to a debate, coverage guaranteed, higher precision, a quarter of the time). A4 second run done 2026-10-19 (`docs/analysis/2026-10-19-audit-a4b/results.md`: 52 files, 11 shards; recall 13/16 vs 11/16, coverage guaranteed vs 19-28 % in three of four debates, but strict precision 0.28 vs 0.51 with 14 false findings). The precision step is implemented (`AUDIT_PROMPT_VERSION` 3, see "Precision step" in the implementation notes); next is its re-measurement on the same corpus (`sg-audit2`), then A3.
Roadmap basis: roadmap item 7 (audit job, user decision 2) in `docs/analysis/2026-10-12-retrieval-architecture-review.md`.

## Problem

A debate poses one question and lets the agents choose what to read. That approach **does not guarantee what was read.**
For a question such as "check every X in the repository", an agent may read only part of the code and answer "no problem",
and the engine cannot tell the difference.

The thread `thread_20261008_052043_22a84e` ("list the APIs that do not work", 2026-10-08) was of this kind. Back then the
code bundle limit meant no source was seen at all; P1-P4 (retrieval tools, map, wiki) made reading possible. But there is
still **no guarantee that every file was looked at once.**

Audit mode does one thing: **it checks every unit of a fixed scope exactly once and the engine records what was checked.**

### Enumeration completeness is not the reason (evaluation 2, E1, final)

The architecture review decided to revisit audit mode after seeing the E1 result (route enumeration). In the blind-graded
re-run of evaluation 2 (`docs/analysis/2026-10-13-retrieval-eval2/results.md`), E1 ran three configurations × 3 times, and
**all 9 results listed all 55 mux routes with the right methods** (precision and recall 1.0). The direction is clear: for a
set that grep can find (routes, declarations), the current debate already enumerates it at this repository's size (482
files). So the purpose of audit mode is not enumeration.

Audit mode is needed when:

- the set to find is fixed and the property **can only be decided by reading each unit** (security flaws, ignored errors,
  dead code, APIs that do not work); one grep does not give the answer, and
- the result is "findings + the range checked", so a person can see which units were missed.

## Decisions (made 2026-10-18: all recommended defaults)

| # | Item | Recommended default | Other choices |
|---|---|---|---|
| 1 | Kind of audit | **Property audit**: a person writes one audit question ("paths reachable without authentication", "ignored errors", "behavior that differs from the documentation"). The same question is asked of every unit in scope | General security/correctness review (no question). The result gets wider and has many duplicates |
| 2 | Output | **A findings list only**. No actions (`apply_patch` etc.) are created | A proposed action per finding. Not recommended because of "Relation to auto-apply" below |
| 3 | Agent lineup | **One Expert** of the team, one session per shard of units. Deterministic citation checking does the verification | Add a Critic panel and judge (cost grows with shards × roles) |
| 4 | Run slot | **One dedicated audit slot** (`GUARDIAN_MAX_AUDITS`, default 1). Not counted in `MaxRunning` | Share the debate slots |
| 5 | Scope | All readable files of the project by default; a glob narrows it | – |

## Goals and non-goals

- Goal: the engine records that every **unit** in scope was confirmed by **lines seen** in at least one session, and reports the
  units it did not see.
- Goal: resume after an interruption (shards with the same work key are not run again).
- Goal: every finding is stored as either passing or failing the existing citation check (`evidence.py`).
- Non-goal: a queue, multi-node workers, a graph DB, a vector index (standing decision in CLAUDE.md). It runs sequentially in
  the one hub process.
- Non-goal: changing code. The audit is read-only.
- Non-goal: using the wiki or map as evidence. They only guide how shards are cut (standing decision: "Map and wiki are navigation only").

## Relation to auto-apply

Since 2026-10-16, code-change actions from the judge (`apply_patch`, `execute_code_action`) are applied to the working clone
without the approval tab once they pass the jev gate (`action_ops.auto_run_actions`). If an audit session could create
actions, one run over the whole repository could change many files of the working clone without a person. So there is one rule.

> **An audit job creates no actions.** Action blocks in an audit session's answer are not parsed, and the audit code does not
> import `action_ops`. To fix a finding, a person picks one finding and starts a **normal debate** (a dashboard button that
> fills the task text with the finding's evidence citations). From there the existing judge, jev gate and auto-apply rules apply unchanged.

This way `GUARDIAN_AUTO_APPLY` and `GUARDIAN_UI_ACTIONS` do not affect audits. The exit condition of the audit phases below includes a
test that "an audit run creates no action rows".

## Structure

The existing code wiki builder (`wiki_build.WikiBuilder`) already solves a problem of the same shape: one target is one session,
there are deferral and resume, and the writer sessions have a step byte cap (`writer_limits`). The audit builder follows this frame.

### Units and shards

- **Unit**: a declaration `code_index` extracted (function, method, type, component), and the whole file for files without
  declarations (configuration, scripts). The unit list is built deterministically from the index (same sha → same list). The LLM does not decide it.
- **Shard**: units of the same directory are grouped so that the source total is at most `GUARDIAN_AUDIT_SHARD_BYTES` (default
  120 KiB). Directories and files are in lexical order. The wiki page scopes (`wiki_build.default_page_set`) could be reused as the
  starting point for directories, but wiki text is not put into the shard prompt.
- One shard is one session. The session is told to read the shard's files directly with `read` (the full text is not pasted into the prompt).

### Work key and checkpoints

- Work key: `(project, sha, question_hash, shard_id, AUDIT_PROMPT_VERSION)`. Like the wiki, the shard key also contains a hash of
  the shard's unit blob list, so at the same sha it is always the same.
- Storage: under `<GUARDIAN_DATA_DIR>/repo-audit/<project>/<audit_id>/`: `plan.json` (units and shards),
  `shards/<shard_id>.json` (state, findings, lines seen), `report.json` (the merged result at the end). Writes use a temp file then rename.
- Resume: `--resume <audit_id>` skips shards whose `shards/*.json` is `done`. A shard that ended on a provider failure such as a
  rate limit stays `deferred`, and the run stops instead of moving to the next shard (same deferral rule as the wiki). A fallback answer does not count as `done`.
- Nightly backup: `repo-audit/` can be rebuilt like `code-index/`, but the LLM cost is high, so it is **included in the backup**
  (treated like `code-wiki/`).

### Session and finding format

- Prompt: the audit question, the shard's unit list (paths and line ranges), the instruction ("read every unit of the shard and
  list only those that match the question as findings; do not list non-matches"), and the existing retrieval protocol guide. The
  map contains only the shard's scope. The first prompt has no wiki text.
- The `findings` block at the end of the answer extends the existing item shape: `{"claim", "path", "line", "quote", "severity", "unit"}`.
  `severity` is `high|medium|low`, `unit` is the unit id.
- Verification: every finding gets `evidence.check_findings` (does the quote really appear near that line) and the line-range rule
  (`GUARDIAN_CITATION_SLACK`). A finding that fails is not discarded; it stays in the report with `verified: false`. The rule of asking once more is the same as the wiki's.
- Session step cap: `GUARDIAN_AUDIT_STEP_BYTES` (default 512 KiB, the same as the wiki writer cap). The unlimited debate budget (standing decision) is unchanged.
  Above the cap a `retrieval_warning` is emitted.

### Measuring coverage

Coverage is measured by **the lines the engine saw, not the LLM's self-report**. The existing `provided_spans` (line ranges seen)
are collected from the session's MCP log and the `repo` block results, and a unit whose declaration range overlaps the seen
lines (`evidence.overlaps`) is `examined`. The report gives:

- `units_total`, `units_examined`, `units_unseen` (list)
- `examined / total` per shard

A shard with `units_unseen` is asked once more to read the unseen units, and if some remain the report marks it `incomplete`.
The independence comes from **the source of the unit list being different from the code that cuts the shards**: the list comes
from the index and the seen lines from the tool log. Coverage is never measured with the shard selector's own books (the principle of the review's decision table).

### Events

Same `events.jsonl` format as debates.

| Event | data |
|---|---|
| `audit_started` | `audit_id, sha, question, shards, units, scope, resumed` |
| `audit_shard_started` | `shard, units, bytes` |
| `audit_shard_finished` | `shard, state(done\|deferred\|failed), findings, unverified, examined, total, retrieval{results,bytes}` |
| `audit_finished` | `audit_id, shards_done, shards_deferred, findings, verified, units_total, units_examined, incomplete` |

### Go API and dashboard

| Method and path | Content |
|---|---|
| `POST /api/projects/{id}/audits` | start a `repo_audit` job (`{"question", "scope"?, "team"?, "resume"?}`) |
| `GET /api/projects/{id}/audits` | list audits (state, question, sha, finding count, coverage) |
| `GET /api/projects/{id}/audits/{audit}` | the report (findings, unseen units, shard states) |
| `POST /api/projects/{id}/audits/{audit}/findings/{n}/debate` | start a normal debate from one finding (the task text includes the evidence citation) |

- Authentication is the existing global `withSecurity(withAuth(mux))` chain. The `audit` value is limited to `^[a-z0-9_]{1,64}$`.
- The slot is counted as `MaxAudits`, following the wiki build (`MaxWikiBuilds`, `wiki_handler.go`). Audits and wiki builds
  share the provider limits, so while an audit runs a wiki build is refused with `busy` (exit 4), and the reverse is the same.
- An audit can start even when production debates use the limit (`MaxRunning`). In return, when an audit is `deferred` by a
  rate limit, debates take priority (the audit stops).

## Risks and responses

| Risk | Response |
|---|---|
| One audit exhausts the usage limit of a subscription CLI and blocks debates | One dedicated audit slot, stop as `deferred` on a rate limit, show the expected shard count and rough bytes before starting and ask for confirmation |
| Secret files exposed | The existing `is_secret` (whole path check) unchanged. Unreadable files drop out of the unit list and are listed as `excluded` in the report |
| False findings | Deterministic citation check + `verified` mark. A finding changes nothing until a person promotes it to a debate |
| Duplicates of the same finding | Findings with the same `path:line` across shard borders are merged into one when the report is assembled |
| An audit made stale by source changes | The report records the sha; the dashboard marks it stale when it differs from the working clone HEAD (reuse the `basis` approach of the wiki staleness check) |

## Phases

| Phase | Scope | Exit condition |
|---|---|---|
| A0 | Make the five decisions of this design; fix E1 with evaluation 2 `results.md` | Five decisions recorded (2026-10-18). E1 is final (55/55 in 9 of 9) |
| A1 (implemented) | `repo_audit.py`: unit list, shard cutting, work key, checkpoint store and resume. `--dry-run` without LLM calls | Same sha → same `plan.json`, a resume test, a test that **an audit creates no action rows**, a secret-file exclusion test |
| A2 (implemented) | Session run: prompt, `findings` extension and verification, coverage from lines seen, deferral rule, events | Shard flow test with a fake LLM, rate limit → `deferred` → resume, asking again for unseen units |
| A3 | Go API, slot, mutual exclusion with the wiki, dashboard audit tab, finding → debate promotion | API tests, web tests, slot refusal test |
| A4 | Validation run and fixing `AUDIT_PROMPT_VERSION` | "Validation" below passes. The "compared with a debate" check is done first, before investing in A3 (dashboard, API) |

## Validation

- **Seeded defects (independent ground truth)**: plant 15 defects by hand in a temporary copy (a `~/guardian-eval` sandbox,
  separate from the production data dir) and write the location list separately (for example authentication bypass 3, ignored
  error 4, missing bounds check 4, dead branch 4). Ask the audit question and measure **recall and precision**. The ground truth is a list the audit code does not know.
- **Coverage guarantee**: place a decoy that deliberately blocks reading the files of one shard (a name that `is_secret` hits)
  and check that its units come out as `units_unseen` and `incomplete` is marked. The report must not change even if the agent says "I read everything".
- **Comparison with a debate**: compare against the result of putting the same question once to a normal debate. Metrics are
  recall, coverage (by lines seen) and total bytes. If audit mode is not better on recall and coverage, drop the feature (it would only add cost).

## What this design does not do

- Enumeration questions ("all routes") are left to the current debate, not to audit mode (evaluation 2 E1).
- Audit results are not written to the wiki automatically. The standing decision that wiki refresh is manual is kept.
- There is no scheduled automatic audit. The cost is high, so a person starts it.
- It does not pass through the judge, the Critic panel or the jev gate. The output of an audit is a findings list a person reads, not a decision.

## Implementation notes

### A1 (plan and checkpoints, 2026-10-18)

Deviations from the design and detailed choices.

- The files are `repo_audit.py` (plan and store; pure functions and file I/O only) and `audit_ops.py` (opening the project and
  commit, exit codes, same shape as `wiki_ops`). `repo_audit.py` and the CLI path (`audit_ops` and the modules it pulls in) do
  not load action modules such as `action_ops` or `debate_engine`. A test checks the import list of `repo_audit.py` and
  `sys.modules` in a fresh interpreter after importing `audit_ops`, and that planning and checkpoints create no decision DB
  (only JSON under `repo-audit/` appears in the data dir). A1 makes no LLM call, so this test is only a **structural
  guarantee**. The behavioral guarantee belongs to A2: session execution goes into a separate module (for example
  `audit_run.py`), and a test that a fake LLM answer containing an action block still leaves zero action rows is part of A2's exit condition.
- Units: every declaration of the index (methods included; when a class and its methods overlap, both are units). Source files
  without declarations (parse errors included) and readable text files the index does not parse are one whole-file unit
  (`kind: "file"`). The unit id is `path:start-end` (a `#2` suffix when the same line range overlaps).
- The `excluded` list (path and reason only, no content): `secret`, `symlink`, `submodule`, `large` (over 1 MiB), `empty`,
  `binary` (NUL in the first 8 KiB). A path outside the scope glob is out of scope, not excluded, so it is not listed. To
  decide binary, the blobs of files the index does not parse are read in batches (no LLM call).
- Shards are cut by **file**, not by unit, and bytes are counted as file sizes (a file is never split). The order is
  (directory, file name), and a new shard starts when the next file would exceed the limit, or when a new directory starts and the
  current shard is at least half full. A single file larger than the limit is an `oversized` shard.
- The work key is the first 24 characters of the sha256 of `(project, sha, question_hash, shard_id, AUDIT_PROMPT_VERSION, hash of the shard's files and blobs)`.
  A new commit changes every key (intended, because each commit has its own plan). Resume uses the stored `plan.json` and does not
  read the working clone HEAD again.
- The store gained `audit.json` (id, state, question, sha, shard and unit counts). A shard record's state is `done|deferred|failed`,
  and `done` counts as finished only when the record's `key` equals the plan's current key. An unreadable record counts as pending.
- Asymmetry: `.go/.py/.ts` files the index parses become a `file` unit even when empty or containing NUL, while the same content in
  a `.txt` is excluded as `empty`/`binary`. It is rare in real sources, so it was left, and A2 will check it against the read results.
- The plan is based on the committed sha. Uncommitted changes in the working tree are not audited.
- CLI: `--repo-audit QUESTION [--audit-scope GLOB ...] [--sha SHA] [--dry-run]`, `--repo-audit-list`, `--repo-audit-show ID`.
  This phase only plans and does not run shards (A2). `--dry-run` prints the summary only. In this repository `scripts/**` is
  13 files, 49 units, 2 shards, and `cli/llm-agent/**.py` is 86 files, 3098 units, 16 shards (about 1.6 MB).

### A2 (session run, 2026-10-18)

- Files: `audit_run.py` (the run; imported lazily inside `audit_ops.run_audit`, so the plan/report path never loads the engine),
  `repo_audit.py` (run lock, shard records, `build_report`), `audit_ops.run_audit` / `audit_report`. CLI:
  `--repo-audit-run ID [--audit-max-shards N] [--team T] [--language L]`, `--repo-audit-report ID`. Exit codes: 0 ok, 2 invalid,
  3 unknown audit, 4 busy (the per-audit `run.lock`; the project-wide slot and the wiki exclusion are A3).
- One shard = one Expert session through `DebateEngine._ask` (retrieval loop, the engine's own single citation re-ask), with a fresh
  retrieval setup per shard. No Critic panel and no judge. The answer is one ```findings block (`[]` = nothing found).
  `GUARDIAN_AUDIT_STEP_BYTES` (512 KiB) is the per-step retrieval cap, `GUARDIAN_AUDIT_LANGUAGE` (Korean) the language of claims.
- Verification is deterministic: `evidence.check_findings` (quote found at the line) plus "the line was shown to the session"
  (`citation_slack`). A finding failing either stays in the report as `unverified` with the reason; it is never dropped silently.
  At most `MAX_FINDINGS` (50) findings are read per shard; claims are cut to 800 characters.
- Deviation: coverage requires **every line of a unit** to have been shown (`lines_covered(spans, start, end, 0)`), which is stricter
  than a range check. `file_level` files (whole-file reads) count as seen. After the first answer there is **one** follow-up
  (missing block and/or unseen units); units still unseen make the shard `done` with `coverage: "incomplete"` and an `unseen` list.
  A missing block after the follow-up fails the shard (`bad_format`).
- A provider failure matching the rate-limit pattern makes the shard `deferred` and **stops** the run (audit state `paused`);
  any other failure marks the shard `failed` and the run goes on (final state `incomplete` when only failed shards remain).
  The failure text comes from `_ask`'s failure list, because `llm.last_error` is not set when the runner is replaced in tests.
- The no-actions rule is now behavioral: a test feeds an answer containing an `apply_patch` block and asserts zero `decision_actions`
  rows and an untouched workspace; another checks in a fresh interpreter that importing `audit_ops` loads none of `action_ops`,
  `patch_ops`, `publish_ops`, `workspace_ops`, `debate_engine`, `db_manager`.
- `AUDIT_PROMPT_VERSION` is 2 (the shard prompt and findings format are new). It is fixed for real in A4.
- Not yet measured against a real model: recall, precision and cost are A4.
- Review of A2 (code and security, same day) changed the following. A quota failure on the follow-up question now defers the shard
  like one on the first question. A plan made under another `AUDIT_PROMPT_VERSION` is refused (plan again), because the key of a
  finished shard is copied from the plan and would otherwise hide the change. An error or interrupt in the middle of a run leaves
  the audit `paused` with a report (`interrupted` in `audit.json`) and `--repo-audit-run` prints JSON with exit code 1 instead of a
  traceback. Every attempt of a shard uses a new session thread id, so a retry never resumes the CLI session of the failed one.
  Non-object findings are kept as unverified, the quote of an unverified finding is not stored, `--audit-max-shards` must be at
  least 1, ids use `fullmatch`, `run.lock` is opened with `O_NOFOLLOW`, and `--repo-audit-report` does not write while the audit runs.
- Import rule, stated exactly: `repo_audit.py` and the `audit_ops` import path load no action module (checked in a fresh interpreter).
  `audit_run.py` imports `debate_engine`, which loads `action_ops`, `patch_ops`, `ops_actions` and `db_manager`; the guarantee for the
  run is behavioral: `_ask` gets no `action_check`, nothing calls `record_proposals` or `auto_run_actions`, and the test above.
- Providers: an Expert that lists none runs on `claude` only (`AUDIT_PROVIDERS`). With the default order codex without the repo
  MCP tools (read-only sandbox, but a shell that can read files on the execution host) and agy (no tool flags) would follow when claude is out of quota,
  and an injected instruction in an audited file could then copy execution-host secrets into a `claim`, which the report stores. A quota
  failure of claude therefore defers the shard instead of moving on. A team that lists providers is trusted to have chosen them. The
  same exposure exists for debates (shared `llm_runner.py`), where nothing is stored; it is left to a separate change.
- Stored text: `claim` and `quote` pass through `mask_secrets` (values of `password|secret|token|api_key|...` assignments and private
  key blocks become `***`; best effort, not a guarantee). The `claim` is model text and must be shown as untrusted in the A3 dashboard.
- Known limits, left as they are: the rate-limit pattern also matches words like `quota` in a lasting error, so a shard that always
  fails that way stalls the run at that shard (a person sees `paused` and the error); `build_report` and `_session` are about 50 lines.

### Precision step (2026-10-21)

Implements the three points of the A4 second run's decision (`docs/analysis/2026-10-19-audit-a4b/results.md`, jev
`precision_first`). `AUDIT_PROMPT_VERSION` is 3, so audits planned before must be planned again.

- Shard prompt: a fixed rule list (`PRECISION_RULES`): do not report behaviour a comment, docstring or project document states
  as intended unless the code contradicts it, hardening suggestions without a limit the code itself states, values that only come
  from constants, the local operator or an already-checking caller, or anything without a concrete failure scenario. Each finding
  gains a `scenario` field (masked and cut like `claim`), and the three severities are defined.
- Severity floor: verified findings below `GUARDIAN_AUDIT_MIN_SEVERITY` (default `medium`; `high|medium|low`) go to the shard's
  `notes` with the reason and are not re-checked. Unverified findings stay in `unverified` as before and are not re-checked either.
- Re-check: one fresh session per shard (own thread `<shard thread>_recheck`, own retrieval key `audit-recheck` so the shard's
  coverage books stay apart, the reviewer system prompt `RECHECK_SYSTEM`, the same Expert member and providers) sees every remaining
  candidate with up to 80 numbered lines of its unit (5 lines of context, centred on the finding's line when longer), the auditor's
  claim and scenario, and the precision rules; it may read more with repo blocks (callers, argument keys: the AUTH-3 lesson). It
  answers one ```verdicts block (`{"id", "verdict": "confirmed"|"rejected", "reason"}`). Only `confirmed` findings stay in
  `findings`; rejected ones and those without a verdict go to `notes` with the reviewer's reason. Design choice: one batched session
  per shard instead of one per finding, to keep the cost near one extra turn per shard.
- Failures: a quota failure of the re-check defers the shard like one of the first session (the shard runs again later); any other
  failure or a missing `verdicts` block keeps the shard `done` with every candidate in `notes` (`recheck.state: failed`), because
  unchecked findings must not count as confirmed. `GUARDIAN_AUDIT_RECHECK=off` keeps the A2 behaviour (candidates stay findings).
- Record and report: shard records gain `notes` and `recheck` (`state` `done|failed|off|skipped`, counts); the report gains `notes`
  and `totals.notes` (the report version is unchanged: old reports simply have none). `audit_shard_finished` carries `notes` and the
  re-check state, `audit_finished` the notes total, and a new `audit_recheck_started` event (`shard, candidates`) marks the second session.
- Recall risk to watch in the re-measurement: a seeded defect reported as `low` (dead branches are a candidate) now lands in `notes`.
  The grading should count `notes` separately, so that a loss of recall through the floor or the re-check is visible.

### Answer format, prompt version 4 (2026-10-30)

- **A broken block is not a clean part.** `evidence.parse_findings` turned an invalid JSON block into an empty list,
  which the audit reads as "checked every unit, found nothing", so a trailing comma or a cut answer passed as a clean
  shard without a re-ask. `parse_audit_findings` tells the two apart: a block that cannot be read is asked for once more
  with the reason, and a second broken block fails the shard as `bad_format`. A readable first block is never replaced
  by a broken second one.
- **Cap.** One answer holds at most 200 findings (`MAX_AUDIT_FINDINGS`; the debate's 50 was used before and cut
  silently). The format states the cap and asks for the most severe first; a cut is stored as `findings_cut` on the
  shard and emitted as `audit_findings_cut`.
- **One format.** Audit sessions no longer get the debate's optional findings paragraph in the retrieval protocol
  (`DebateEngine._findings_rules`), which contradicted `FINDINGS_FORMAT` (optional, four fields, 50 items). Debate
  prompts are byte-identical.
- `low` now reads "a real instance but harmless in practice". Plans of version 3 must be planned again; the
  re-measurement on `sg-audit2` runs on version 4.

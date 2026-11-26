# Batch execution: command sets on many nodes, critics vote, a person decides only on opposition

English | [한국어](batch-execution.ko.md)

Status: proposed (2026-10-20; the three scope decisions of chapter 2 were taken by the operator the same day, the
points marked "to confirm" in chapter 11 are mine). Related: [fleet-provisioning.md](fleet-provisioning.md) (plans, the
package tools this builds on), [server-ops-project.md](server-ops-project.md) (ops projects, `auto_run`),
[multi-perspective.md](multi-perspective.md).

## 1. Problem

Today an ops project works one typed action at a time, and every plan waits for a person ("a plan is never run without
a person's approval"). The operator asked for the opposite default for node management: the system runs a *batch*
(a stored set of commands or a script) on many nodes at the same time, the critics judge the risk and, where needed,
look at the real state of the nodes first and *vote*, and a person is asked only when somebody objects.

The danger is plain: the critics are language models, their "approve" would replace a person's click, and the text they
read comes from the nodes (and from the order). The design therefore keeps every hard rule in code, outside the
models, and uses the votes only to *add* a reason to ask the person, never to remove one that code insists on.

## 2. Decisions

Taken by the operator on 2026-10-20 (answers to three questions):

1. **Scope of a command: stored command sets plus ad-hoc scripts.** The operator registers command sets; the judge may
   also write a script on the spot.
2. **Irreversible work always needs the person.** A step with no way back, or rated high, needs approval even when every
   critic approves (data deletion, package removal, reboot, protected targets). Everything else runs at once when no
   critic objects.
3. **One objection is enough.** One critic's opposition that cites a check of the node state sends the plan to the
   person. An opposition that cites nothing is kept as advice.

4. **Free scripts, opt in per node group (2026-10-20, changes decision 2).** After a `fleet-provision` debate showed the
   judge refusing ordinary work (a timezone, a config file) for lack of a stored set, the operator agreed that a project
   may let the critics' vote release ad-hoc scripts, on the node groups it names and nowhere else. Irreversible work still
   needs the person everywhere else; a stored set is still the preferred form. See 4.3 and 12.5.

Consequences I drew (to confirm, chapter 11): an ad-hoc script cannot be proven reversible by reading it, so it goes to
the person unless decision 4 applies (4.3); a critic that gives no valid vote counts as approving (5.3, the operator's decision of 2026-10-20); the switch is a per-project
policy and is off everywhere until the operator turns it on (7).

## 3. Design picture

```
order -> expert -> critics -> judge: final plan (steps = typed actions, command sets, scripts)
                                      |
                       plan hash (sha256 of the normalized plan)
                                      |
                  vote pass: every critic reads THE FINAL PLAN, may run ops checks, votes
                                      |
                       gate (pure code): policy + votes + reversibility table + cap + jev
                          |                                   |
                   all conditions hold                  any condition fails
                          |                                   |
        plan runs now as autoplan:<project>           plan stays PROPOSED, the gate's reasons
        (nodes in parallel, re-simulated per step)  are shown next to it; a person decides
```

Nothing new executes commands. A batch is an ordinary plan: steps are `decision_actions` rows, nodes run in parallel with
one step per node at a time, every step is simulated again right before it runs and ends `STALE` on drift
(`plan_ops.py`). What is new is (a) two step kinds, (b) the vote on the final plan, (c) a gate that may start a plan
without a person.

## 4. What a step can be

### 4.1 Existing typed actions
`ops:*` catalog actions are unchanged. Each carries its reversibility in the table of chapter 6.

### 4.2 `ops:run_command_set`
`{target_host, set, params}`. A command set is a file the operator wrote and reviewed. Builtin sets live in
`configs/command-sets/*.json` (in git, few and generic), the operator's own in `<data dir>/command-sets/*.json`.

```json
{"id": "refresh-index", "description": "Refresh the package index", "os": ["linux"], "needs_sudo": true,
 "params": {"older_than_days": {"type": "int", "min": 1, "max": 365}},
 "check": "ls -l /var/lib/apt/lists | head -5",
 "script": "set -eu\napt-get update -q",
 "reversible": true, "undo": "nothing to undo; the index is rebuilt on the next update",
 "risk": "low", "timeout": 600, "reviewed": "2026-10-20"}
```

- Parameters are typed (`int`, `enum`, `unit`, `package`, `path`, `string` with a regex); the script uses `${NAME}` and
  the engine inserts the value as a bare token that must match a strict character set (letters, digits and `._:/@+=,-`,
  no `..` segment, not starting with `-`; a `string` value is also capped at 64 characters, and a pattern with nested
  repetition such as `(a+)+` is refused at registration). A placeholder without a declaration, an unknown parameter or a value that
  fails its type refuses the step. `path` values pass `ops_paths` (default deny rules, `policy.deny_paths`).
- Registration runs `bash -n`, limits the script to 8 KiB, checks `reversible: true` carries a non-empty `undo`, and forces
  `reversible: false` when `risk` is `high`. The declaration is the operator's statement; the validator only checks that
  it is consistent.
- `check` is a read-only script (the operator declares it so) whose output is the step's *preview*, stored with the
  step and compared again right before the run. It runs when the step is **proposed**, before any person has seen it,
  so it runs without root unless the set also says `check_sudo: true` (which needs `needs_sudo`). The preview also stores the sha256 of the rendered script, so a set that
  was edited after the proposal makes the step `STALE`.
- Audit lines carry `source=action`, `op=run_command_set:<id>` (a label is at most 64 characters), `action_id`. The hub
  cuts a command at 4096 characters and the script travels as base64, so the audit line alone cannot rebuild a long
  script: the preview and the result of the step keep the rendered `script` and `check` and their `script_sha`.

### 4.3 `ops:run_script` (ad hoc)
`{target_host, script, reason}`, at most 4 KiB, run with `bash -c` on the node (as base64 inside a fixed wrapper). The judge writes it, so nothing about it is
vouched for: **it is always rated high and irreversible, so it goes to the person**, except on the node groups of
`policy.batch.script_groups` when `policy.batch.scripts` is `"vote"` (decision 4, 12.5). A refusal list (`rm -rf /`,
`mkfs`, `dd of=/dev/…`, stopping or disabling a protected unit, touching Guardian or credential paths, `curl | sh`) turns
the step `INVALID` before the person even sees it. The list reads through `/bin/rm`, `\rm`, `sudo -u x`, `timeout`,
`nice` and quotes inside words, but it is a best-effort tripwire against obvious mistakes (a glob such as
`cat ~/.gu*/agent.env` or `sh""adow` gets past it), not a safety boundary, and the document does not pretend otherwise: the boundary is the person's approval. After an approval the
dashboard offers "save as command set" (phase B4), which is how a good script graduates to the automatic path.

## 5. The vote

### 5.1 Votes are on the final plan
The critics see the expert's *proposal*; the plan is written by the judge afterwards. A vote taken in the critique round
would approve a draft, not the steps that run. So the vote is a separate pass **after** the judge's plan has been parsed
and normalized:

- `plan_ops.plan_hash(plan)` is the sha256 of the canonical JSON of the normalized plan (goal, `stop_plan`, and per step
  id, host, tool, args, requires, on_failure; command sets contribute the sha256 of their rendered script).
- Each critic is asked once, in its own session (so it keeps what it already knows and may run ```ops checks), to read
  the steps and the first 12 characters of the hash and to answer with a block:

```vote
{"plan": "3f9a1c0be712", "vote": "approve", "reason": "…"}
{"plan": "3f9a1c0be712", "vote": "oppose", "reason": "disk is 97% full on n2", "cites": ["[#4 disk_usage n2]"]}
```

- A vote counts only for the hash it names. If the plan changes in a later round, the earlier votes are void and the
  pass runs again.

### 5.2 What counts as an objection
An `oppose` counts when at least one of its `cites` is a check result the critic received in this debate
(`OpsChecks.unverified_citations`, the same verification as every other citation). An `oppose` without a verified
citation is stored as advice, shown to the person if the plan goes to them anyway, and does not stop the run. The
critic prompts say what an objection is worth: a concrete, cited harm on named nodes ("[차단]" in the provisioning
lineup), not a preference.

### 5.3 Missing votes
A critic that returns no parsable vote after one repair prompt, or names another hash, is an **abstention**, and an
abstention counts as *approving* (decided by the operator on 2026-10-20: a critic that fails or times out must not
hold the batch back). The consequence is that a broken critic can no longer stop a plan, so the safety of a plan that
runs by itself rests on the code gates alone: the reversibility table, the refusals, the cap, the re-simulation. An
abstention is stored and shown (`vote: "abstain"`) so the person and the shadow-mode comparison can see how often it
happens; if every critic abstains the gate adds the reason "no vote was cast" but still lets the plan run.

### 5.4 Cost
One extra short turn per critic, only for projects with `policy.batch.auto` (or shadow mode, B2). Teams and projects
that do not opt in get byte-identical prompts, which `PromptGateTests` pins.

## 6. Reversibility is a table, not an inference

`risk()` is not enough: removing packages or paths is only *medium*. The gate reads an explicit table
(`batch_gate.REVERSIBLE`), and an unknown tool is irreversible.

| step | reversible | why |
|---|---|---|
| `ops:apt_clean`, `ops:docker_prune` (`dangling_images`, `build_cache`) | yes | regenerable caches |
| `ops:docker_prune` (`stopped_containers`, `unused_images`) | no | writable layers and local builds are lost |
| `ops:journal_vacuum`, `ops:truncate_logs`, `ops:remove_paths` | no | deletes data |
| `ops:remove_old_kernels`, `ops:package_remove` | no | no rollback |
| `ops:package_install` | yes unless risk is high | removable again; bun, pnpm, yarn, pipx, uv and an install whose simulation removes packages are high |
| `ops:restart_service` | yes | state is transient, repeatable; protected units are refused by code anyway |
| `ops:run_command_set` | its declaration, and never when risk is high | chapter 4.2 |
| `ops:run_script` | no | chapter 4.3 |

A reboot is not in the catalog; a command set that reboots declares `reversible: false`. Protected packages and units stay
**refused** by the existing gates (the step is `INVALID`), which is stricter than "ask the person".

## 7. The gate

`batch_gate.decide(plan, steps, votes, policy, runs_today, jev_passed) -> {"auto": bool, "reasons": [...]}` is a pure
function. A plan starts without a person only when **all** of these hold, and the reasons list names each one that fails:

1. the project opted in: `policy.batch.auto` is true;
2. `GUARDIAN_AUTO_APPLY` and `GUARDIAN_UI_ACTIONS` are not `off`;
3. a real jev PASSED (the plan is not `HELD`; with the jev gate off nothing runs by itself);
4. no critic of the lineup cast a counted objection to this plan hash (5.2); abstentions count as approval (5.3);
5. no step is irreversible or rated high (chapter 6) and none is `INVALID`;
6. the plan has at most `policy.batch.max_steps` steps and fewer than `policy.batch.max_plans_per_day` auto plans ran
   today (UTC);
7. at run time every step is simulated again and ends `STALE` on drift, as for any plan.

Policy (validated in `project_registry`, unknown keys refused, off by default):
`"batch": {"auto": false, "max_plans_per_day": 0, "max_steps": 20, "max_parallel_hosts": 2}`. The cap counts **plans**, not
steps (a 20-step plan would otherwise use up a cap of three). `max_parallel_hosts` (1 to 8, default 2) replaces the constant
`MAX_PARALLEL_HOSTS`; the hub has two OCPU, so the default stays modest.

Opening the gate means one change in `action_ops.auto_run_actions`: the `plan_id` skip ("a plan step only runs with its
plan, approved by a person") stays for every plan **except** one the gate released, which runs through `plan_ops.run_plan`
as a whole, never step by step.

## 8. Audit trail

- Actor `autoplan:<project>` for an auto plan (`decided_by` of every step; see 12.3), the person's name otherwise.
- `decision_plans` gets `plan_hash`, `votes` (JSON: critic, vote, reason, verified cites, counted) and `gate` (JSON:
  `auto`, `reasons`, `at`). Existing databases are migrated with `ALTER TABLE … ADD COLUMN` like the step columns.
- Events: `plan_vote` (per critic), `plan_gate` (decision and reasons), `plan_auto_run` (started, finished).
- Hub audit lines keep `action_id`; nothing in this design writes to the hub's data directory from a test.

## 9. Dashboard

The plan view shows the gate's decision and reasons, each critic's vote with reason and cited checks (advice marked as
such), the plan hash, and for every step its reversibility. A plan waiting on a person says *why* ("critic 2 objects:
disk is 97 % full on n2", "step s3 is irreversible"). Every new string goes into both catalogs (`ko`, `en`).

## 10. Phases

- **B1: command sets and the two step kinds.** `command_sets.py`, `ops:run_command_set`, `ops:run_script`,
  `policy.batch.max_parallel_hosts`, `--command-sets` (list, validate). No automatic running. Tests first, isolated data
  dir.
- **B2: votes and the gate in shadow mode.** `plan_hash`, the vote pass, `batch_gate.decide`, schema columns, events,
  prompt version p17 for opted-in lineups only. The gate is computed and stored but never starts a plan: the operator
  compares "would have run" with what they did. A model-free corpus (`batch_corpus.py`, like `package_corpus.py`) holds
  the gate's combinations: recall of every blocking condition 1.00, false-block rate 0.00, and a case where every critic abstains.
- **B3: automatic running (built 2026-10-20, not deployed).** The released-plan path, actor, cap, kill switches, events. Off until the operator sets
  `policy.batch.auto` on a project.
- **B4: dashboard (built 2026-10-20, not deployed).** The gate view (chapter 9), "save as command set", and a command set editor in Settings.

Each phase gets its own security review before commit (remote execution, sudo, approval logic), full `scripts/ci.sh` in
a clean worktree, and deployment only when asked.

## 11. Open points (to confirm)

1. **Which project gets `policy.batch.auto`?** My proposal: none by default, including the builtin `fleet`. Shadow mode
   (B2) runs there first.
2. **Ad-hoc scripts go to the person** (4.3), except on the groups the operator opted in (decision 4). A static parser as the
   safety boundary is still not something I would trust, so the opt-in rests on the critics' read, a canary node and the
   operator's own deny list, not on the tripwire.
3. ~~A missing vote blocks the run~~ Decided 2026-10-20: **a missing vote is an abstention and counts as approval** (5.3).
4. **The reversibility table** (chapter 6), above all `restart_service` and `package_install` as reversible.
5. **The cap counts plans** (default 0 = none until set).
6. Whether the vote pass should use the critics' existing sessions (cheaper, they remember) or fresh ones (independent of
   what they argued earlier); the proposal is the existing sessions.

## 12. Implementation notes

### 12.1 B1 (built 2026-10-20, not deployed)

- `command_sets.py` (standard library only) holds the store, the parameter types, the wrapper and the validation.
  Builtin sets are `configs/command-sets/*.json` (`health-report`, `service-status`, `refresh-package-index`); operator
  sets are `<GUARDIAN_DATA_DIR>/command-sets/*.json`; an id in both stores is an error. `guardian.py --command-sets`
  lists and validates them; `scripts/ci.sh` config runs it and warns about a set not reviewed for 180 days.
- Deviation: the tool gate is opt-in. The default `policy.allowed_actions` of an ops project no longer includes the
  package tools or `ops:run_command_set` / `ops:run_script`, so the `fleet-ops` prompts stay byte-identical (p16 pin).
  A project (or a team lineup, in B2) lists the tools to offer.
- Transport: the script travels base64-encoded inside a fixed `bash -c "$(printf %s '<b64>' | base64 -d)"` wrapper, so
  no parameter or script text is ever interpolated into a shell line. Parameters are typed tokens (`int`, `enum`,
  `unit`, `package`, `path`, `string` with a pattern) matched against `SAFE_VALUE`; `path` also goes through
  `ops_paths.refusal`.
- `drift()` compares a `script_sha` of the stored script plus check: a set edited after the proposal is `STALE`.
  A set with a `check` runs it at preview (60 s limit) and again after the run; a script is never run at preview.
- `risk` / `reversible` come from the set's declaration; `risk: high` forces irreversible. An ad-hoc script is always
  high and irreversible. `ops_auto_approvable` refuses both batch tools explicitly (B3 releases them through a plan's
  gate, not one by one).
- `policy.batch.max_parallel_hosts` (1-8, default 2) reaches `plan_ops.run_plan` through `plan_ops.parallel_hosts`
  (`run_plan(max_parallel=None)` resolves it from the plan's project). Unknown `policy.batch` keys are refused until
  B2 adds `auto`, `max_plans_per_day` and `max_steps`.
- Review fixes (code and security review, 2026-10-20): the sha covers the script, the check and every field that decides
  how the step runs (`needs_sudo`, `check_sudo`, `os`, `timeout`, `risk`, `reversible`); a deferred plan step keeps its
  sha and ends `STALE` when the set was edited; one `Plan` serves the check, the run and the second check; the text a
  set renders to goes through the tripwires again (a value can complete `rm -rf /srv/${T}`); the wrapper exits 7 when
  the script cannot be decoded; `load_sets` is cached by file (name, mtime, size, inode) and ignores dotfiles, refuses
  symlinks and files over 64 KiB; the hub keeps at most 1 MiB of stdout and of stderr per command
  (`apps/agent/capped_buffer.go`).

### 12.2 B2 (built 2026-10-20, not deployed)

- `batch_gate.py` (pure, no model, no node) decides; `batch_votes.py` asks, parses and counts; the debate engine's
  `_vote_pass` connects them; `plan_ops.plan_hash`, `plan_ops.gate_steps` and `plan_ops.auto_plans_today` supply the
  inputs. `decision_plans` gained the columns `plan_hash`, `votes` and `gate` (an old database is migrated on open);
  events are `plan_vote` and `plan_gate`.
- Deviation: `plan_hash` is computed from the stored plan rows (`plan_ops.plan_hash(db, plan_id)`), not from the judge's
  JSON, and independent of row ids. The votes are therefore cast after `record_plan` has stored the plan, and a vote
  counts only for the first 12 characters of that hash. A vote naming another plan is an abstention.
- Deviation: no prompt bump to p17. The vote format lives only in the text of the vote turn (it is passed as
  `format_reminder`, so the engine's ops-citation repair keeps the vote block), and the vote pass runs only when
  `batch_votes.votes_enabled(project)` holds: `policy.batch.auto` is true, or a batch tool is in
  `policy.allowed_actions`. A plain `fleet-ops` project is never asked, so its prompts and cost are unchanged.
- The vote pass runs only after a real jev `PASSED`, after the plan is stored, and never starts a plan. Critics are
  asked in parallel, each in its own session (`lineup.session_key(CRITIC_ROLE, member)`, the existing-session option of
  open point 6), and may run ```ops checks first. One repair prompt for an unusable answer, then the critic abstains.
  A critic that cannot be asked (built-in provider, exception) abstains with a note. An objection counts only with at
  least one individually verified citation (`OpsChecks.unverified_citations` finds at least one and no bad one); text
  that cites nothing is not a clean citation. An invented citation first triggers the engine's own CITATION CHECK re-ask.
- Shadow mode is the `would_auto` field of the stored gate: everything except the `policy.batch.auto` opt-in, with the
  kill switches evaluated as they are. There is no separate `shadow` policy key. A project that never set
  `max_plans_per_day` (0) is judged without a cap in shadow, but a project that opted in with cap 0 gets no auto runs.
  The daily count was first taken from actions decided by `auto:<project>`; B3 counts started plans from the stored gate
  records instead (12.3).
- If nobody cast a vote the gate adds the note `no vote was cast` and still allows release (decision of 2026-10-20).
- `batch_corpus.py` (12 healthy plans, 29 planted defects, no model, no node): recall 1.00 and false-block rate 0.00
  (`--targets`, held by `test_batch_corpus.py` under `scripts/ci.sh python`).
- Review fixes (code review, 2026-10-20): `OpsChecks.run` numbers its calls under a lock (critics run checks in
  parallel; before, a result could be filed under another call's number and a genuine objection was dropped as
  unverifiable, which counts as approval); the vote pass runs only when the jev gate exists and passed; a failure inside
  it no longer ends the debate, the plan gets a stored gate that sends it to a person; a package step whose preview was
  deferred counts as unmeasured and goes to a person; a vote may name the whole plan hash; `plan_vote` and `plan_gate`
  events carry the reason, note and hash.
- Review fixes (security review, 2026-10-20): a vote answer with several valid blocks is read toward caution (any valid
  oppose wins over an approve, so a quoted or injected approve block cannot erase an objection; a broken block is
  ignored when another is valid); only an unreachable critic abstains, while a bug in parsing, checking or storing
  propagates to the fail-closed `_vote_pass`; the critic sees the stored script and arguments (script up to 8192
  characters, arguments up to 2000, cut text is marked `(cut)`); a package step whose preview holds an error counts as
  unmeasured like a deferred one; a package install is undoable only for a system manager (apt, dnf, ...) or with
  `ignore_scripts`, because npm, brew and the other managers run third-party code that removal does not undo, and an
  install whose manager is still `auto` goes to a person.
- Open for the operator: a plan where every critic abstains is still released (decision of 2026-10-20), so the
  quorum is zero; an objection must cite a node check, so a purely content-level concern (a script that reads wrong)
  stays advice (for free scripts decided on 2026-10-30, see the end of 12.5); `restart_service` counts as undoable
  (open point 4).
- Carried over to B4 (done in 12.4): the dashboard ops editor rebuilt `policy` and dropped `policy.batch` on save. The B2 notes about the daily count, the actor and the run-time risk check are resolved in 12.3.

### 12.3 B3 (built 2026-10-20, not deployed)

- `batch_auto.release(db, mcp, plan_id)` runs a released plan. The debate engine calls it from `_auto_run_plan` right
  after the vote pass when the stored gate says `auto`; the plan runs inside the debate process, so the debate returns
  when the plan has ended (a debate job is cut off after `JobTimeout`, one hour by default: steps still `RUNNING` then
  wait for a person, because `run_plan` refuses to start over a running step). A failure while releasing never ends the
  debate: the plan stays `PROPOSED` for a person and the reason goes into the `plan_auto_run` event and the result.
- Nothing stored is trusted as it stands. `release` requires the stored gate record with `auto` true, recomputes
  `plan_ops.plan_hash` and compares it with both the gate and the `plan_hash` column, takes the project from the plan
  (ops projects only), decides the gate again from the stored steps and votes under the policy, kill switches and daily
  count of this moment (`jev_passed=True`: the vote pass only ran behind a passed jev gate, and a `HELD` step is not
  ready), and only then claims the start.
- The start is claimed in one write transaction (`DecisionDBManager.claim_auto_plan`, `BEGIN IMMEDIATE`): it refuses a
  plan that was already started and a project whose cap of `max_plans_per_day` is used up, and writes
  `gate.auto_run = {actor, started_at}`. The count of the day (`count_auto_plans`, `plan_ops.auto_plans_today`) reads
  `started_at` of those records, so a later human action cannot change it. A start is spent whatever the outcome; the
  end of the run adds `finished_at` and `counts` (or `error`) through `finish_auto_plan`.
- Deviation: the steps run as `autoplan:<project>`, not `auto:<project>`. The second is counted against the cap of
  single actions (`max_auto_runs_per_day`, `action_ops.auto_runs_today`); the two caps stay independent.
- Run-time guard: `plan_ops.run_plan` takes `guard(action, measured_now)`, asked under the node lock after the step
  was measured again and after the drift check, before it runs. `batch_auto.step_guard` rates the step on that fresh
  measurement (`plan_ops.gate_item(row, preview)`) and asks `batch_gate.step_problem`: a step that is now high risk,
  unmeasured (package tools) or not undoable ends `STALE` with the reason, and the steps that need it are `SKIPPED`.
  `_run_step` alone only enforced refusals and `script_sha`, and compared the new measurement with the stored one.
- Events: `plan_auto_run` (`started`, `counts`, `reasons`, `error`); the result of the debate carries
  `final_decision.plan.auto_run`. A release that is refused leaves the stored gate unchanged, so it still says `auto`
  while no `auto_run` exists: B4 must show the refusal reasons from the event, not infer the state from `auto` alone.
- Still conservative: a plan whose package step could not be measured at proposal (a step that `requires` another has
  a deferred preview) is never released; it goes to a person. Off until `policy.batch.auto` is true, with the same kill
  switches as other automatic runs (`GUARDIAN_AUTO_APPLY`, `GUARDIAN_UI_ACTIONS`).
- Review fixes (security review, 2026-10-20): the guard runs before every step and also stops a plan when the kill switches
  were set, `policy.batch.auto` was saved as off (the engine reloads the project through `ProjectManager.load`, because its
  own client holds the policy of the debate's start), or `plan_ops.plan_hash` no longer matches the voted plan; a stop ends
  the step `STALE` and its dependents `SKIPPED`, but a step already running is not interrupted. `release` reads the
  project as saved now. `_run_step` runs the row as stored after the node lock and refuses a step whose tool or arguments
  changed while it waited. A command set or script step carries the `script_sha` it was checked with into `ops_actions.execute`
  (`expected_sha`), which refuses to run a set that was edited since. `finish_auto_plan` merges in one `json_set` UPDATE and
  `set_plan_gate` refuses to overwrite the gate of a started plan.
- Review fixes (code review, 2026-10-20): `release` builds a fresh `MCPClient` for the project as saved now, so the run
  validates every step against the current `allowed_actions`, `deny_paths` and `max_parallel_hosts` (the engine's own
  client holds the debate's start), and refuses a project whose id differs from the plan's. Whatever goes wrong after the
  claim (any exception, a locked database included) is recorded: `finish_auto_plan` runs in a `finally`, and the result
  says `started` with the `error`; if `release` itself raises, the engine reads the stored `gate.auto_run` and reports
  a claimed start as started. `claim_auto_plan` takes the plan hash and re-checks inside its transaction that the stored
  gate still says `auto` for it. The day and `started_at` come from one clock read. The engine saves the debate and its
  decision before an automatic run starts and again after it (a hub job cut off mid-plan keeps its record). A command set or
  script step that was not measured at proposal (a deferred or failed preview) is never released, like a package step.
- Open for the operator (security review, not changed): `ops:package_install` counts as undoable for system managers (a
  postinst script runs as root and removal does not undo what it did; npm/bun global installs add `bin` entries even with
  `ignore_scripts`); an all-abstain or uncited-objection plan is released (5.3); one-step plans give the engine a way
  to auto-run medium actions under the budget of plans (up to `max_plans_per_day` x `max_steps`); `restart_service` runs on
  up to `max_parallel_hosts` nodes at once; apt downgrades are labelled `upgrade:`; the hub audit line carries no actor.
- Tests: `test_batch_auto.py` (release conditions, atomic claim, cap, separate actor, guard) and the engine cases in
  `test_batch_debate.py`.

### 12.4 B4 (built 2026-10-20, not deployed)

- B4a, the gate in the plan view: `PlanGate.tsx` shows the gate's decision with reasons, each critic's vote (reason, cited
  checks, advice marked as advice), the plan hash and the run record (`auto_run`). `plan_view` steps now carry `reversible`.
  A release the engine refused is stored by `DecisionDBManager.note_release_refusal` as `gate.release_refused`
  (`reasons`, `at`), written by `_auto_run_plan`; the view reads that and never infers the state from `auto` alone.
- B4b, project policy: the ops editor has a "batch" section (`auto`, `max_plans_per_day`, `max_steps`, `max_parallel_hosts`).
  Bug fixed on the way: the editor rebuilt `policy` from its own fields, so saving a project from the dashboard dropped
  `policy.watch` and `policy.batch`; `lib/project.ts` now keeps every policy key it does not edit.
- B4c, command sets: `GET/POST /api/command-sets`, `DELETE /api/command-sets/{id}` (Go: `--command-sets`,
  `--command-set-save` with the set as JSON on stdin, `--command-set-delete`, `--command-set-confirm`). `command_sets.save_set` validates with
  `validate_set`, stamps `reviewed` with today's date (the client cannot set it), refuses a builtin id,
  symlinked directories or files and files over `MAX_FILE_BYTES`, and writes atomically (`.tmp-*.json`, which the loaders
  ignore). Builtin sets are read-only: the dialog can save one only under a new id. Operator sets live in
  `<GUARDIAN_DATA_DIR>/command-sets/`. Settings -> Command sets lists both stores with the review age (stale marker from
  `report()`), and an ad-hoc `ops:run_script` step in the plan view offers "Save as command set" with the script and reason
  prefilled; nothing is saved until the operator confirms in the dialog.
- Deviation: a set saved here can be released automatically (reversible and low risk, chapter 7), so saving is a write of
  code that may later run unattended. The engine stamps `reviewed`, and the stale warning after 180 days applies to saved
  sets as it does to builtin ones.
- Review fixes (code review, 2026-10-20): `Modal` now stops Escape and Tab at the innermost dialog (Escape in the "save as
  command set" dialog also closed the plan under it); a new set or a copy of a builtin one is refused in the dialog when the
  id exists (`takenIds`), so it cannot silently replace an operator set and turn pending plans `STALE`; ticking automatic
  batch runs raises a daily maximum of 0 to 1 (the hub refuses `auto` with 0) and the hint says so; `PlanGate` hides the
  "released"/"waiting"/"refused" verdict once no step is left to decide (`open`); risk chips use the catalog labels. Every
  save from the dashboard now writes a `policy.batch` block (hub defaults) into an ops project that had none.
- Decision of the operator (2026-10-20): a saved set waits for a separate confirmation before it can run on its own. A
  set saved from the dashboard declares its own `risk`, `reversible` and `undo`, so without this a script (high risk by
  itself) could be saved as low and undoable and then auto-run, with sudo if `needs_sudo`. Built: `command_sets.confirm_set`
  stores the sha256 of the set's text (`content_digest`: every field but `reviewed`, `source` and `confirmed`) in the sidecar
  `<operator dir>/.confirmed.json` (dot-prefixed, so the loaders ignore it; atomic write, symlinks refused). `is_confirmed`
  is true for a builtin set, and for an operator set only while the stored digest equals the current text: any edit takes the
  confirmation back, an identical re-save (new `reviewed`) keeps it, delete drops the entry, a missing, forged or malformed
  file confirms nothing. `plan_ops.gate_item` marks a set step `unconfirmed` (also for a set that cannot be read) and
  `batch_gate.step_problem` refuses it, at the gate and again at run time through `step_guard`, so a person decides. CLI
  `--command-set-confirm ID` (exit 2 for a builtin set, 3 for an unknown one), `POST /api/command-sets/{id}/confirm`, and in
  Settings -> Command sets a chip ("Auto-run confirmed / not confirmed") with a confirm button behind a confirmation
  dialog. Not a defence against someone who can write the hub's data directory (they can also write the sets).
- Open for the operator (security review, not changed): (1) a confirmed set is still trusted at its word on `risk`,
  `reversible` and `undo`; an option is to floor the risk at medium when `needs_sudo`; (2) an edited `check` runs once per step at the
  preview before the `script_sha` drift check refuses the step (the script itself is checked first), and a `check` with
  `check_sudo` runs as root at proposal; (3) `reviewed` is stamped on every save, so the 180-day warning measures the last
  save, not a review; (4) saving or deleting a set leaves no line in the hub audit log; (5) the ReDoS guard
  (`NESTED_QUANTIFIER`) misses alternation such as `(a|a)*$`; (6) no limit on the number of stored sets.
- Tests: `test_command_sets.py` (`StoreWriteTests`), `test_config_ops.py` (`CommandSetConfigTests`), `TestCommandSetRoutes`
  in `debate_handler_test.go`, and in the web suite `commandsets.test.tsx`, `debate.plans.test.tsx`, `projects.test.tsx`.

### 12.5 F1, free scripts (built and deployed 2026-10-20, `6b24987`)

Implements decision 4. Off everywhere until an operator sets it: the default `policy.batch.scripts` is `"approve"`, so
the deploy changes no behaviour by itself.

- **Policy.** `policy.batch.scripts` (`"approve"` default | `"vote"`) and `policy.batch.script_groups` (node group ids),
  `policy.deny_commands` (fnmatch patterns, each matched against one whole command). `project_registry` refuses `vote`
  without `auto`, `vote` without `script_groups`, `script_groups` without `vote`, `vote` when `ops:run_script` is not an
  allowed action, and a group that is not among the targets (`check_ops_targets`).
- **Which nodes are free.** `plan_ops.free_script_hosts(project)` = targets ∩ `script_groups`, the hub never (it holds the
  decision database and the fleet key). It is resolved when the gate decides and again in `batch_auto.step_guard` right before
  each step, from the project as saved then, and fails closed: an unreadable inventory means no free node.
- **Gate.** `step_problem` lets a step through only when it is `ops:run_script`, `free_script` and measured (the READY,
  unmeasured and unconfirmed checks still come first). Every other irreversible or high step still goes to the person.
  New condition: a plan with a free script needs **at least one explicit approve vote**. An abstention counts as approval
  for every other plan (5.3), but a script nobody has read must not run on that; this is stricter than the rule of
  2026-10-20 and was put to the operator as a separate confirmation.
- **Votes.** `batch_votes._step_line` marks such a step "RUNS WITHOUT A PERSON" and the vote task tells the critic that its
  vote is the only review of the script.
- **Canary.** The judge rules (`JUDGE_BATCH_RULES`, prompt version `2026-10-20.p17`) tell the judge to run a script on one
  node first and let the others wait for it with `requires` and `on_failure: "stop"`. This is a prompt rule, not enforced
  by the gate; the corpus and the vote cover what the gate does enforce.
- **Bypass shapes.** `command_sets._BYPASS_PATTERNS` (`eval`, a pipe into a shell, `sh -c "$(`, `source <(`,
  `$(base64 -d`) are refused for **every** ad-hoc script, also in `approve` mode (a small behaviour change), but not for a
  stored, operator-confirmed set. They exist to make the cheap evasions of the tripwire noisy, nothing more. `deny_commands`
  is checked at proposal and again at validation, like `deny_paths`.
- **Shell wrappers (2026-10-22).** A review found that `sh -c '<command>'` hid its command from the disaster tripwire and
  from `deny_commands`, because the command word was `sh`. `script_refusal` and `deny_refusal` now take the text of
  `sh|bash|dash|zsh|ksh|ash -c '...'` (also `-lc`, `-o pipefail -c`, `$'...'` and `<<<`) out of the script and run the same
  checks on it, to a depth of 4; anything nested deeper, or more than 64 layers, is refused as unreadable. This applies to
  stored sets as well, since it is the tripwire and not a bypass shape. Still open, by design a tripwire and not a sandbox:
  flags between a command and its verb (`docker --context x system prune`), `python -c`, `perl -e`, `bash <(...)`,
  `ssh host '...'` and variable indirection. The critics' vote is the control for those.
- **Judge and project.** The builtin `fleet` project, the `fleet-provision` team and the `ops_provision_judge` persona now
  hold `ops:run_command_set` and `ops:run_script`; the judge prompt no longer says to leave non-package work alone, and
  tells it to plan such work as a set or a short script, guarded by the checked state, without blocking it beforehand
  (the project and the critics decide). Side effect: `votes_enabled` is true for any project that allows a batch tool, so
  the vote pass runs in `fleet` whenever the judge writes a plan. The `fleet-ops` prompts stay byte-identical.
- **Dashboard.** Project editor: "Extra commands to block", and in the batch block the vote checkbox (disabled until
  automatic runs are on) with the group list. Both catalogs.
- **Corpus.** `batch_corpus.py` gains a class `freescript` (6 defects: outside the groups, one step outside, a counted
  objection, nobody approves, advice only, `auto` off) and two healthy plans (both approve; one abstains): 14 healthy, 35
  defects, recall 1.00, false-block 0.00 (`--targets`).
- **Tests.** `test_free_scripts.py` (resolution, gate, guard, bypass shapes, deny list, run), `test_provisioning.py` (tool
  gate, batch rules only for a judge holding a batch tool), `test_batch_corpus.py`, `projects.test.tsx`.
- **Not done.** F2 (read-only probes for scripts). No `script_groups` is set on any project yet; the operator chooses them.
- **Vote text, prompt `p19` (2026-10-30; the GitHub mirror's number, `p21` after the merge with the local history).** A plan with a free script tells each critic to approve only after reading
  every line and finding a guard that stops the script on any other state than the checks showed, to say what it checked,
  and not to approve by default (the explicit approve of 12.5 was otherwise the default answer of "no objection found").
  Every vote maps a `[차단]` objection to oppose and a `[권고]` to the reason of an approve.
- **Uncited objections hold a free script (operator's decision, 2026-10-30).** The free-script note asked critics to oppose
  on any concrete risk, but an objection to the script text itself could not cite a server check and stayed advice, so
  the script ran. Now, in a plan with a free script, every oppose sends the plan to a person: `batch_gate._conditions`
  holds the plan on any oppose when a step is a free script (whatever the stored vote says, so a group opted in after the
  vote is covered), and `cast_vote(..., free=True)` stores such an oppose as counted, so the plan view does not show it
  as advice. The vote task tells the critic. Elsewhere an uncited objection stays advice (5.2). Corpus: new defect
  "free script, uncited objection" (healthy plans unchanged at 14, defects 36, recall 1.00, false-block 0.00).

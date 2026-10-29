# Server Guardian

Project context for every Claude Code session and subagent working in this repository. Keep it short and factual;
details live in the linked documents. User-facing documents (README, docs/) are written in English, the main
version, with a Korean translation next to each (`name.md` and `name.ko.md`, linked to each other at the top); keep
both in step. Code, comments, commit messages and this file are in English. Answer the user in Korean.

## What this is

- `apps/agent/` Go hub agent (stdlib only): metrics, sshd audit, fleet SSH, command API, serves the dashboard and
  spawns the Python CLI for debates and the code wiki (`newCmd` in `debate_handler.go`). HTTP auth is the global
  `withSecurity(withAuth(mux))` chain in `main.go`; `h.guard` only checks that the CLI exists.
- `cli/llm-agent/` Python CLI and debate engine (`guardian.py`, `debate_engine.py`). Standard library only.
  Agents (Expert, Critic panel, Judge) run through LLM CLIs (`llm_runner.py`: claude, codex, agy) and read code via
  ```repo blocks or the read-only MCP server `repo_mcp.py`. Code index `code_index.py`, map `repo_map.py`, Go facts
  `go_facts.py` + `tools/goindex/` (Go stdlib helper), evidence/citation checks `evidence.py`, code wiki
  `code_wiki.py` / `wiki_build.py`.
- `apps/web-desktop/` React dashboard (copy in `src/i18n/ko` and `src/i18n/en`, same shape enforced by the
  `Messages` type; Korean by default, the language is chosen in Settings), `packages/guardian-ui/` design system.
- `scripts/` operations: `ci.sh`, `install-agent-service.sh` (deploy), `deploy-llm-env.sh`, `setup-hub-ssh.sh`, ...

Document index: `docs/README.md`. Design and decisions: `docs/design/debate-agent-retrieval.md` (retrieval protocol, event contract),
`docs/design/debate-code-index-wiki.md` (index, map, Go facts, wiki; implementation notes per phase),
`docs/design/code-wiki-coverage.md`, `docs/design/wiki-viewer.md` (index screen and page-to-page reading; approved 2026-10-27; V0 `[[id]]` links, V1 index and reading view, V2 `summary`/`links` in the page list, V3 shortcuts deployed 2026-10-28 as `d84562c`; V4 modes Read | Files | History (`?view=files|history`, build buttons only in History) deployed 2026-10-28 as `5a8df55`; none checked in a real browser), `docs/design/team-flows.md` (team `flow` field: `debate`, `panel`, `verify`, `pipeline`, `versus`; approved 2026-10-23; F0-F3 built and deployed 2026-10-23 as `a05446b`), `docs/design/dashboard-i18n.md`, `docs/design/office-export.md` (DOCX/PPTX from stored Markdown versions, started by a button; direct stdlib OOXML first; decisions approved 2026-10-27; D2 DOCX (`office_docx.py`, `office_ooxml.py`, `--artifact-convert`, `POST /api/threads/{thread}/artifacts/{version}/convert`, "Make Word document" button in the Artifacts tab) built 2026-10-27, deployed 2026-10-28 as `d84562c`, opened in Word and confirmed by the operator 2026-10-28; D3 PPTX (`office_pptx.py`, `md_parse.py`, `--artifact-format pptx`, "Make PowerPoint" button) built and deployed 2026-10-28 as `b50af88`, opened in PowerPoint and confirmed by the operator 2026-10-28; D1 Mermaid rendering is deployed (`mermaid` 11.17.2, lazy, strict), report diagrams get a "Save .mmd" button, deployed 2026-10-28 as `b50af88` and confirmed by the operator, option (b) approved; D1 (c) report snapshot (`report_snapshot.py`, `--report-keep`, `POST /api/threads/{thread}/report/keep`, "Keep as document" button on code and ops reports stores `report.md` as an artifact version so the Word/PowerPoint buttons work; person-started only, design projects refused, non-design versions expire after the default 30 days) approved and built 2026-10-28, deployed 2026-10-28 as `f6497bd`, not checked in a real browser; `office-export.md` sections 5 and 6), `docs/design/server-ops-project.md` (planned ops
projects without a repository), `docs/design/design-project.md` (`kind: "design"` artifact projects),
`docs/design/fleet-provisioning.md` (planned package install and removal on fleet hosts; proposed 2026-10-19, scope decided by the operator; P1 (host_facts, package_info, facts cache), P2 and P2b (`ops_packages.py`: `ops:package_install`/`ops:package_remove` for apt, dnf, zypper, apk, pacman, brew, npm, bun, pnpm, yarn, pipx, uv; opt-in per project; bun, zypper, apk, pacman gated by `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`; apt run end to end and pnpm, yarn, pipx, uv run on a test host, dnf formats never run on a real host; npm/bun scripts run by default, `ignore_scripts` opts out) P3 (plan object: `plan_ops.py`, `--plans`/`--plan-run`, `/api/plans`, dashboard plan view, `SKIPPED`; only the `fleet-provision` judge writes plans, since P6) and P4 (`package_roles.py`, `configs/package-roles.json`, derived protection, `policy.protected_packages`, `ops_roles` in the inventory and node form; protection recomputed at every validation, unknown role refuses; ports resolve to their owner package, blind spots (unknown port owners, partial visibility) rate removals high) and P4b (`package_bundles.py`, `configs/package-bundles.json`: exact install lists `nginx`, `postgresql`, separate from the roles, each with a `reviewed` date that CI warns about after 180 days; versions follow the distribution, no vendor repositories; used by the `fleet-provision` judge through the `bundles` check) and P5 (`package_corpus.py`: 10 healthy actions, 30 planted defects in mixed-OS, protected, cascade, stale and sudo classes, run through the real gates without a model or host; recall 1.00, false-block rate 0.00; `--targets` holds it) and P6 (team `fleet-provision` with personas `ops_provisioner`, `ops_provision_safety_critic`, `ops_dependency_critic`, `ops_provision_judge`; critics grade objections `[차단]`/`[권고]` and the judge invents no prohibitions (calibrated 2026-10-19 after an over-defensive first use); `bundles` check and plan rules gated on the judge holding a package tool, `fleet-ops` prompts byte-identical, prompt `p16`; used on apt hosts only by the operator's decision of 2026-10-19, dnf stays unverified because no test host exists; the trial debate on `~/guardian-eval` is NOT run, it needs the operator's OK for the shared Codex quota) deployed with `6b24987` on 2026-10-20 (P1-P6 are ancestors of it; the hub holds the modules and the `fleet-provision` team)),
`docs/design/batch-execution.md` (proposed 2026-10-20; F1 free scripts built and deployed 2026-10-20 as `6b24987` (see Standing decisions); B1-B4 built and deployed 2026-10-20 (B1-B3 as `0ebd456`, B4 as `0e86cbc`) (`command_sets.py`, `configs/command-sets/`, tools `ops:run_command_set`/`ops:run_script` opt-in via `policy.allowed_actions`, `policy.batch.max_parallel_hosts`, `--command-sets`; B2: `batch_gate.py`, `batch_votes.py`, `_vote_pass` in the debate engine, stored votes and gate in shadow mode (`would_auto`), `batch_corpus.py --targets` recall 1.00 / false-block 0.00; B3: `batch_auto.py`, the engine runs a plan the gate released (opt-in `policy.batch.auto`, actor `autoplan:<project>`, atomic start claim, run-time step guard); B4 (dashboard: gate view, batch policy form, command set editor, "save as command set"; operator-saved sets run unattended only after a separate confirmation of their exact text, `.confirmed.json`) deployed; in short: command sets and scripts on many hosts, critics vote on the final plan, a person decides only on opposition or irreversible steps; phases B1-B4, open points in chapter 11),
`docs/design/repo-audit-mode.md` (whole-repository audit mode; decisions made 2026-10-18),
`docs/design/multi-perspective.md` (independent proposals and blind critique; decisions made 2026-10-18 through jev; P1/P2-build/P3 done, deployed 2026-10-19 as `b208293`), `docs/design/remote-agents.md` (debate agents on worker-capable hosts through a resident worker; proposed 2026-10-22, scope decided by the operator, phases W1-W4; W1 (transport, session pinning, fallback) and W4a (`probe-worker.sh`, `install-worker.sh`, `worker_probe.py`, `configs/worker-requirements.json`, `deploy/worker/`) built and tested against a fake fleet, deployed to the hub 2026-10-22 as `23dfc03`; workers installed 2026-10-22 on the six non-hub Tier-1 hosts (`worker: true`, `max_jobs` 2; one real `run` checked), no production team has a `host` yet; trial debate on the hub sandbox 2026-10-22: two Critics on worker hosts (`worker:any`, `worker:other`) ran with their own CLIs, no fallback, APPROVED in round 1; W2 (repository snapshot, remote `repo_mcp` with five tools, call log return; protocol 2, git is a probe condition) built 2026-10-27, the six workers re-probed and reinstalled the same day at protocol 2, the hub deployed 2026-10-28 as `d84562c`, no worker call at protocol 2 observed yet; W4b (team editor `host`, `GET /api/workers`, Settings -> Workers) built 2026-10-27, deployed 2026-10-28 as `d84562c`; W3 not built, recommended to drop (the audit line is written by the Go agent, a worker-local check would need a forgeable record-only endpoint); W2b not built), analyses in `docs/analysis/`.

## Topology

- One hub only: `site-a-arm1` (2 OCPU, 12 GB ARM, also runs Gitea). Other fleet hosts run no agent, except that hosts that pass the capability probe and are enabled may run the model worker of
  `docs/design/remote-agents.md` (decided 2026-10-22, opt-in, no state); the hub reaches every host with its fleet key. Dashboard on `127.0.0.1:9870` and the Tailscale IP; API needs `AGENT_TOKEN`
  (`~/.guardian/agent.env` on the hub).
- Hub data: `~/.guardian/data` (decision DB, teams, projects, debate-jobs, code-index cache, code-wiki pages). It is
  production state: never write test data there. Nightly backup tarball excludes `code-index/`, keeps `code-wiki/`.
- The hub runs the release `~/.guardian/current` -> `~/.guardian/releases/<version>-<time>` (install-tracks R1;
  `deploy/hub/install.sh --rollback|--status`). The checkout `~/server-guardian` is only the source of the builtin
  server-guardian project; workspace clone `~/guardian-workspace` (debate target).
- Evaluation sandboxes live in `~/guardian-eval` on the hub with their own `GUARDIAN_DATA_DIR`; never mix them with
  production.

## Rules that must not be broken

- Never read, move, modify or commit `*.enc` files or credential bundles (the repo root holds a real one that is not
  in git). Never copy `~/.claude/.credentials.json` or `~/.codex/auth.json` between hosts.
- Tests must not touch the real data dir: debate/index/wiki tests call `testenv.isolate_data_dir()` (it also points
  `GUARDIAN_GOINDEX_BIN` at a missing file). Test scripts that write files run in a scratch copy.
- Gate everything: `scripts/ci.sh && git push origin main && scripts/install-agent-service.sh site-a-arm1`, joined
  with `&&`. Never push or deploy after a failing CI. Before a deploy, check that no production debate is running.
- The installer builds the local working tree: deploy only from a clean tree. With uncommitted work in progress,
  commit the part to deploy and deploy from a temporary `git worktree` of that commit (copy the git-ignored
  `configs/servers.json` into it). On the Mac, Go is only on PATH in a login shell: run CI as `zsh -lic "cd <dir> &&
  scripts/ci.sh"` and check that the go section ran ("tool not installed" skips it and CI still passes). The installer
  wants the hub's real inventory id, not the placeholder `site-a-arm1`.
- `git push origin main` pushes to both git.example.com (Gitea) and the GitHub mirror. Gitea Actions are off;
  CI is `scripts/ci.sh` locally. Branch only when the user asks.
- Subagents write notes into `.claude/agent-memory/` directories; never commit them.
- Real host, site and account names never enter git: the repository uses placeholders (`site-a-arm1` is the hub).
  The mapping and the real hub alias are in the git-ignored `CLAUDE.local.md`; `scripts/redact-private.py --check`
  must find nothing (rules in `~/.config/server-guardian/public-redactions.txt`), and `scripts/export-public.sh`
  builds the public copy. Export only a reasonably stable version, when the user asks; never after every push.
- No real host names in code, tests or previews: use neutral ones (`site-a-arm1`, `node-1`, `hub-1`). The inventory
  `configs/servers.json` is a local, git-ignored snapshot (`scripts/pull-inventory.sh`); `configs/servers.example.json`
  shows the format. The hub reads `~/.guardian/data/servers.json` through `GUARDIAN_SERVERS_FILE`.

## Standing decisions (ask before changing)

- Debate retrieval budgets are unlimited (2026-10-09). `retrieval_warning` fires above 256 KiB per step; the only
  cap is the code wiki writer's 512 KiB per step (`GUARDIAN_CODE_WIKI_STEP_BYTES`, deferral, no total limit).
- Python engine stays standard-library only (the limit was lifted on 2026-10-23 for document artifact formats, DOCX first; how a dependency reaches the hub release is open, `docs/design/design-project.md`); no tree-sitter, graph DB, vector index, task queue or fleet workers. The one exception, decided 2026-10-22: a
  stateless, stdlib-only model worker on capable, opted-in hosts, decided by a capability probe and not by tier (`docs/design/remote-agents.md`); it runs LLM CLIs for debate roles
  and decides nothing.
  Go facts come from the hub's Go toolchain via `tools/goindex` in a throwaway worktree. TypeScript type facts are
  deferred (npm install scripts would run code).
- The code wiki is for people (dashboard); debate agents do not see it unless `GUARDIAN_CODE_WIKI=1` (2026-10-18,
  after evaluation 2). The planned audit mode is to use wiki pages as scope units and coverage checklist.
- Map and wiki are navigation only, never evidence: a strong citation passes only if those lines were shown to the
  session (line-span rule, `GUARDIAN_CITATION_SLACK` 3); short paths are resolved against shown files.
- `is_secret` matches every path component; code wiki refresh is manual; the dashboard is Korean by default and
  every new string goes into both catalogs (`ko` and `en`).

- Irreversible work needs a person, except ad-hoc `ops:run_script` on the host groups an operator opted in
  (`policy.batch.scripts: "vote"` + `script_groups`, needs `batch.auto`; the hub never; default `approve`; decided
  2026-10-20, `docs/design/batch-execution.md` 12.5). There a script runs when no critic objects with a verified check and at
  least one critic approved explicitly (an abstention is not enough). `policy.deny_commands` and the bypass shapes
  (`eval`, pipe into a shell, ...) refuse scripts at proposal. No `script_groups` is set on any project; ask the operator
  before choosing one.

## Working conventions

- Run `scripts/ci.sh <section>` (go, python, web, shell, config) for what you touched, the full `scripts/ci.sh`
  before a commit that will be deployed. The Mac's Python prints `blake2b/blake2s` hash errors; they are noise.
- Prompt changes bump `RETRIEVAL_PROMPT_VERSION` (debate) or `WIKI_PROMPT_VERSION` (wiki writer); index/map format
  changes bump `INDEX_VERSION` / `MAP_VERSION` / `GOINDEX_VERSION`.
- When debugging debate or wiki quality, read the job's `events.jsonl` under `~/.guardian/data/debate-jobs/<id>/`
  on the hub first; claude session transcripts are in `~/.claude/projects/-home-ubuntu--guardian-llm-sessions/`.
- Record each phase's deviations from its design in the design document's "구현 메모" subsection.

## Current state (update when it changes)

- Deployed: retrieval phases P1-P4 plus P2.1, P2.5, P2.6, short-path citations, Korean dashboard, wiki feature and
  group pages (`docs/design/code-wiki-coverage.md`).
- Ops projects P1 (`docs/design/server-ops-project.md`): `kind: "ops"` projects without a repository, builtin
  `fleet` project; fleet, node and group debates go there; code projects no longer offer `server_remediation`.
  P2: agents of ops debates run read-only server checks with ```ops blocks (`ops_checks.py`); the hub audit log labels
  them `source=ops_check` (approved actions `source=action`). P3: typed low-risk cleanup actions `ops:docker_prune`,
  `ops:journal_vacuum`, `ops:apt_clean` (`ops_actions.py`), measured at proposal, refused as `STALE` on drift, verified
  before/after; one action per host at a time. P4: medium actions `ops:remove_paths`, `ops:truncate_logs`,
  `ops:remove_old_kernels`, `ops:restart_service` and the medium docker scopes; default deny rules in `ops_paths.py`
  (`policy.deny_paths` only adds); hub-local remediation of ops projects goes through `/api/exec`. P5: dashboard
  editor for ops projects, node probe reports passwordless sudo, `policy.auto_run` runs low-risk catalog actions as
  `auto:<project>` after a real jev PASSED, capped by `max_auto_runs_per_day` (off in the builtin `fleet`). P6:
  `server-guardian-ops-watch.timer` (02:30 UTC) runs `guardian.py --ops-watch`; ops projects with `policy.watch` get one
  debate for new disk/inode/journal findings (7-day repeat guard). Ops check citations are verified; audit lines carry
  `action_id`. The installer uploads atomically (sha256, temp name) and retries over Tailscale (`HUB_SSH_HOSTNAME`).
  P7: read-only `headscale_status` check; `fleet` watch has `headscale: true` (service, certificate, backup timers,
  offline tagged nodes); `headscale*` is a protected unit; debates never change headscale (`docs/operations/headscale.md`).
- Mesh join (`docs/design/mesh-join.md`): the operator pastes a new host's `tailscale up` registration URL in the
  add-node page; `POST /api/mesh/join` runs `headscale auth register` + `nodes tag` on the control server
  (`GUARDIAN_HEADSCALE_HOST/USER/TAGS` in the hub's agent.env). `POST /api/mesh/nodes/{id}/remove` deletes a tagged
  node (not the hub or the control server). These are the only writes Guardian makes to headscale. All pre-auth keys
  were expired on 2026-10-18; no auth key is kept anywhere.
- Ops debates of `fleet` use the `fleet-ops` team (project `"team"` key). Specialist teams (P8,
  `docs/design/server-ops-project.md`, deployed `ae3fd7e` 2026-10-20, one trial debate per team on the hub sandbox:
  both finished in one round, services PASSED without actions, mesh DISPUTED only on its own "도구 밖" list): `fleet-services` (service and
  container diagnosis, tool `ops:restart_service`) and `fleet-mesh` (headscale/tailnet report, `allowed_tools: []`, no
  warning for that); chosen by hand with `--team` or the New debate form, `fleet-ops` and the watch are unchanged, no
  prompt-version bump; the network/security specialist waits for a read-only check. Guardian skills for the LLM CLIs of the hub
  and Tier-1 hosts live in `deploy/llm-skills/`; install them with `scripts/deploy-llm-skills.sh`.
- Code-change actions (`apply_patch`, `execute_code_action`) are applied without the approval tab once the jev gate
  passes (`decided_by` `auto:engine`); tests/shell actions stay manual. Off with `GUARDIAN_AUTO_APPLY=off` or
  `GUARDIAN_UI_ACTIONS=off` (`docs/design/debate-agent-retrieval.md`, last 구현 메모).
- Evaluation 1 (`docs/analysis/2026-10-12-retrieval-eval/results.md`): keep the map on by default.
- Evaluation 2 (`docs/analysis/2026-10-13-retrieval-eval2/results.md`, re-run 2026-10-16): the map stays on; wiki
  exposure to debates was turned off by default on 2026-10-18. Map: 15.00 vs 14.67 correct, efficiency equal; agents
  never opened the wiki. E1 shows a single debate already lists all 55 routes (recall 1.0 in 9 of 9), so audit mode is
  not about enumeration.
- Whole-repository audit (`docs/design/repo-audit-mode.md`): property audit, findings only (never creates actions), one
  Expert per shard, own slot `GUARDIAN_MAX_AUDITS`. Phase A1 (`repo_audit.py`, `audit_ops.py`, `--repo-audit`: plan and
  checkpoints, no LLM) and A2 (`audit_run.py`, `--repo-audit-run`, `--repo-audit-report`: session execution, deterministic
  verification, deferral) are done and deployed (2026-10-18, `ad94a2e`); A4 first run (2026-10-19, `docs/analysis/2026-10-19-audit-a4/`,
  12 files / 2 shards, 15 seeded defects): recall 15/15 for both audit and debate, audit precision 0.98 vs 0.81, guaranteed
  269/269 units vs 92-100 % lines seen, a quarter of the time. A4 second run (2026-10-19, `docs/analysis/2026-10-19-audit-a4b/`,
  52 files of `cli/llm-agent` / 11 shards, 16 seeded, corpus `sg-audit2` in data3): recall 13/16 vs 11/16 (debate misses are
  unread files, audit misses are judgement), strict precision 0.28 vs 0.51 (243 findings, 14 false), time equal. Precision step (jev
  `precision_first`) implemented 2026-10-21, deployed (first in `a05446b`; the hub runs `AUDIT_PROMPT_VERSION` 3: precision rules and `scenario` in the shard prompt, severity
  floor `GUARDIAN_AUDIT_MIN_SEVERITY` medium, one re-check session per shard, unconfirmed findings go to `notes`). Next: re-measure on
  `sg-audit2` (count `notes` separately), then A3 (API, slot, dashboard).
- Design projects (`docs/design/design-project.md`): `kind: "design"` projects without a repository (Markdown, Mermaid,
  SVG), team `design-review`. P1 (kind validation, `has_workspace`, `KIND_TOOLS`) and P2 (`artifact_ops.py`: validators,
  immutable version store under `design-artifacts/`, manual-approval `jev_mcp:write_artifact`, retention via
  `guardian.py --artifacts-prune` in the ops-watch service) are done and deployed (2026-10-18, `ad94a2e`). P3 (done, deployed): read-only
  Go API `artifact_handler.go` (sha256 checked on every read, fixed sandbox CSP, symlinks refused) and the dashboard
  Artifacts tab (`#/debates/artifacts`, SVG only as `<img>`); no events (no sink in the approved-action path), real-browser
  download check still open; a "View artifacts" link sits next to the debate result's `ReportButton`. Seeded-defect corpus
  (`artifact_corpus.py`, no model): recall 18/30; SVG, links (incl. image targets) and sections (opt-in
  `artifacts.required_sections`) 6/6 with target 1.0 (`--targets`; the sections number awaits the user's confirmation);
  contrast has no detector yet and false claims are measured through the debate instead (not run). Design never auto-applies actions (`AUTO_APPLY_KINDS`
  is an allow list).
- Thread digest (decision 12, steps 0-1, `thread_digest.py`): fresh sessions get earlier rounds condensed above 16 KiB
  (`GUARDIAN_THREAD_DIGEST=off` disables); `history_bytes` on `agent_started`. Deployed with `ad94a2e`; the LLM digest (step 2) is not built.
- Multi-perspective debates (`docs/design/multi-perspective.md`, deployed 2026-10-19 as `b208293`, off unless a team has `proposers`): optional
  team field `proposers` (0-2); in the first round of each run the expert and the proposers draft blind to each other, the
  Critics and the Judge read the drafts under neutral rotated labels, later rounds the lead alone consolidates.
  `GUARDIAN_MAX_PARALLEL_AGENTS` (default 4) bounds each parallel phase; ops and design projects ignore proposers.
  Dashboard team editor and `--teams` support them. `proposer_eval.py` builds the planted-defect tasks, configs C0-C4 and
  the scorer with the decision rule. P0 hub numbers (last 10 debates: agent run median 95 s, max 374 s; 9 of 10 finished in one round). Evaluation run
  2026-10-19 (`docs/analysis/2026-10-19-multi-draft-eval/results.md`): easy tasks hit the ceiling (recall 1.00 in all
  configs); on three hard tasks C0 0.86, C1-C3 0.89, C4 0.92, wall time about equal, peak memory 1.5-2.5x C0 (under
  1 GB) -> decision 10 says **opt-in**, no hub team gets `proposers`. One repetition, automatic pre-grade only. Open: human grade of the blind
  sheet, repetitions 2-3 (Codex quota is shared with production; ask first), a harder task set.
- Free scripts, F1 (`docs/design/batch-execution.md` 12.5, built and deployed 2026-10-20 as `6b24987`): `policy.batch.scripts`
  `vote` + `script_groups`, `policy.deny_commands`, bypass shapes for every ad-hoc script, `free_script_hosts` resolved at the
  gate and again in `step_guard`, a free script needs one explicit approve vote; the builtin `fleet` project, the
  `fleet-provision` team and its judge now hold `ops:run_command_set`/`ops:run_script` (judge batch rules, prompt `p17`, so the
  vote pass runs in `fleet` whenever the judge writes a plan); corpus class `freescript` (14 healthy / 35 defects, recall 1.00).
  Default `approve`: the deploy changed no behaviour until an operator opts a group in. F2 (read-only probes) not started.
- Project results tab (`docs/design/project-results.md`, deployed 2026-10-23): read-only `GET /api/projects/{id}/results` and a Results tab for
  every project kind (debate reports, plus design artifacts joined by thread id).
- Debate list (dashboard): every finished debate has a "Run again" button (`JobResult.tsx`) that prefills the New debate form
  with its task, team, rounds and reference code in a fresh thread; "Continue this thread" (same thread: rounds are
  appended, the final status and gate are overwritten) stays separate. Deployed `9378e3e` (2026-10-20).
- Teams: builtin and custom team files carry `category` (`development`, `operations`, `design-docs`, `general`) and
  `created`/`modified` (server-stamped on save; `created` survives edits and copies). Settings -> Teams shows grouped
  rows, newest first, with expandable details (`docs/design/debate-teams.md`). Builtin team `guardian-dev` (claude,
  codex, gemini lineup with personas for developing this repository) is deployed. Trial debate (2026-10-19, hub
  sandbox `~/guardian-eval`, review of `TeamManager.save`): 11.5 min, no fallbacks, approved in round 1 of 2; the three
  findings it approved were real (unchecked stored `created`, validated-then-overwritten `modified`, fixed `.tmp`
  name), it held back a change that would have broken `test_config_ops.py`; its patch is not applied.
  Deployed: `cc17844` (2026-10-19).

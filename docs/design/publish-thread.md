# Publishing a thread's changes

English | [한국어](publish-thread.ko.md)

Executed file actions (`jev_mcp:execute_code_action`, `jev_mcp:apply_patch`) leave **uncommitted** changes in the
project's workspace clone on the hub. The publish step is the operator's way to get exactly those changes into the
project's repository as a branch, without copying a diff by hand. The old judge tool
`jev_mcp:git_commit_push` (local branch and commit, no push) is retired: it is gone from the tool
catalog, the builtin team and the judge persona, and a stored PROPOSED action that names it is refused as INVALID
("git_commit_push was retired; use publish"). Existing rows still list and show. Opening the decision database settles
any undecided (PROPOSED or HELD) row that names it as INVALID, so it no longer blocks its thread's publish, and a team
whose `allowed_tools` still lists it is accepted with a warning (the name is ignored).

Since 2026-10-16 file actions are applied without the approval tab once the jev gate passes (`decided_by`
`auto:engine`, see "Implementation notes (auto-approval of code changes)" in `debate-agent-retrieval.md`), so publish is
the last step a person takes before a change reaches the repository.

**One publish per thread (decision D11).** A thread is published at most once: after a publication is recorded as
pushed, a second publish of the same thread is refused. Further changes need a new thread. Failed attempts do not count
and may be retried. `guardian.py --publications THREAD --json` lists the recorded attempts (see below).

A published thread is final: continuing it is refused up front. `guardian.py
--debate TASK --thread T` exits 4 with `thread T was published as branch B; start a new thread`, and
`POST /api/debates` with that `thread_id` answers 409 with the same text and starts no job (the hub checks the cached
published set first, then asks `--publications` for the branch name). `guardian.py --published-threads --json` returns
`{"ok": true, "threads": [ids]}`; `GET /api/debates` items carry `"thread_published": true` for those threads, from a
30 second cache that a successful publish route call invalidates.

Code: `cli/llm-agent/publish_ops.py`. Entry points: `guardian.py --publish-thread THREAD [--branch NAME]`
(add `--yes --json --actor A` for the hub form) and `POST /api/threads/{thread}/publish` with an optional body
`{"branch": "..."}`. The dashboard shows a **Publish** button in the debate result's action list.

## Flow

1. Check every precondition (below). A refusal changes nothing: no branch, no commit, no database row.
2. Ask the operator to confirm (CLI prompt, or the dashboard's confirm dialog), then run the project's test.
3. Record an attempt in `thread_publications` (`RUNNING`).
4. `git checkout -b <branch>`, `git add -- <the exact files>`, `git commit` (subject `fix: <decision summary>`,
   at most 72 characters; body is the summary; trailer `Debate-Thread: <thread>`).
5. `git push -- <repo> <branch>:refs/heads/<branch>`: no force, no upstream.
6. `git checkout <tracked branch>`, so the workspace is clean again. The change now lives only on the branch.
7. Mark the attempt `PUSHED` or `FAILED` (with stderr).

## Preconditions

All must hold; otherwise the hub form (`--yes --json`) exits 4 (HTTP 409) with the reason (2 / 400 for a bad branch
name, 3 / 404 for an unknown thread). The interactive form (without `--json`) uses the same 2 / 3 / 4 codes; it exits 1
only when the push failed after the commit was made (the JSON form reports that as `ok: false`, exit 0).

- The thread was not published already (an earlier attempt is `PUSHED`).
- At least one `EXECUTED` file action, and no action `PROPOSED`, `HELD` or `RUNNING`. `FAILED`, `REJECTED` and
  `INVALID` actions do not block, but anything they left in the workspace shows up in the path check below.
- All file actions share one project and one non-empty `base_sha`; each recorded `file_hashes`. A file action proposed
  without a code context records the workspace HEAD at proposal time as its `base_sha`, so it can be published, and the
  stale-base check at execution applies to it too.
- The workspace is the top of a git repository (not the running Server Guardian checkout), on the project's tracked
  branch (not detached), and `HEAD == base_sha`.
- The paths `git status --porcelain` reports (untracked files included) equal the union of the files the actions
  wrote, nothing is staged, and each file's content hash equals the last hash recorded for it
  (same ordering rule as sibling actions: `applied_at_ns`, then id).
- The project has a `repo` URL. The builtin server-guardian project (`configs/projects/server-guardian.json`) sets
  `"repo": "${GUARDIAN_SELF_REPO}"`: the URL comes from the hub's environment, so the project file in git names no
  account. `scripts/install-agent-service.sh` writes `GUARDIAN_SELF_REPO` into `~/.guardian/agent.env` from the `origin`
  URL of the checkout it deploys from. When the variable is unset or empty the project has no repo URL (it still has its
  workspace) and publish refuses with `project server-guardian has no repo URL to push to`.
- Branch: default `guardian/<thread>`. A supplied name passes the branch validation
  (`MCPClient._check_branch`: pattern, no `..`, no `.lock` or trailing `/`, not `main`/`master`/`develop`/`release`),
  is not the project's tracked branch, and is accepted by `git check-ref-format --branch`.
- The project names a default test (`tests.default`); without one publish refuses with "set tests.default for project
  X" (there is no fallback command). The test runs through `MCPClient.run_default_test`, the helper that also runs
  `execute_code_action(run_test)`: same allow-list check, same Go module working directory as `run_tests`, same
  600 s limit. It must pass in the workspace and leave the changed paths as they were. On failure the refusal carries
  its output (`tests.stdout`, `tests.stderr`). This check is made with the other cheap preconditions, before any
  confirmation; only the test run itself comes after the confirmation.
  `tests.default` is one command string or a list of 1..5 command strings, each validated like the single form. A list
  runs in order through the same helper, every Go step from its module directory, all steps sharing the one 600 s
  budget; it stops at the first failing step, and the refusal and `tests.stderr` name it ("step 2/3 failed: ..."). A
  pass reports a per-step summary (`tests.steps`). The server-guardian project gates on three steps: the Python
  unittest suite, `go vet ./apps/agent/...` and `go test -race ./apps/agent/...`. `-race` needs cgo and a C compiler;
  the hub has gcc (checked 2026-10-12), and a node without one fails the gate loudly rather than skipping the race
  detector. Web and shell checks still run only in `scripts/ci.sh` on the developer machine and in GitHub CI. The allow-list is unchanged apart from `go vet` (only `-v` and package
  paths): no shells and no path-prefixed scripts.
- No other publish is running in this workspace (an `flock` on `.git/guardian-publish.lock`).

## Dry run (plan)

`guardian.py --publish-thread THREAD --plan --json [--branch NAME]` and `GET /api/threads/{thread}/publish-plan?branch=NAME`
run every precondition above up to, not including, the test gate, and change nothing (no git write, no database
row, no confirmation). Success: `{"ok": true, "plan": {"thread_id", "project_id", "branch", "remote", "base_sha",
"files", "subject", "retry"}}`; the real publish returns the same fields. Refusal: exit 4 / HTTP 409
`{"ok": false, "error"}`; bad branch 2 / 400; unknown thread 3 / 404. The dashboard shows it before the confirm.
A plan reports an interrupted earlier publish as already recovered but does not recover it.
A plan never takes the workspace lock (opening the dialog must not make a real publish or sync fail as busy); when a
`RUNNING` row exists it adds a `note` that a publish may be running.

## Failure and retry

- A publish killed halfway (hub timeout, restart) leaves a `RUNNING` row and possibly the workspace on its branch.
  The next publish, under the workspace lock, marks the thread's `RUNNING` rows `FAILED` ("interrupted") and, when
  the worktree is clean and HEAD is on a branch such a row names, checks out the tracked branch; the kept commit is
  then pushed as a retry. Project sync takes the same lock and also skips workspaces with untracked files.
- A `git add` or `git commit` failure unstages, returns to the tracked branch and deletes the new branch, so the
  changes are plain uncommitted files again.
- A push failure keeps the local branch and its commit, checks out the tracked branch again and returns
  `status: FAILED` with stderr (HTTP 200, `ok: false`). The attempt row is `FAILED` with the commit sha.
- Running publish again with the same branch name pushes the existing commit without re-running the test, provided
  the branch is still a single commit on `base_sha`, holds exactly the action files with the recorded content,
  and a non-`PUSHED` attempt for that thread and branch names that commit. Anything else is refused.
- An existing remote branch with other history makes the push fail as non-fast-forward; the push is never forced.

## Safety rules

- Only the exact files of the actions are added; `git add -A` is never used.
- git runs without a shell, with a timeout per call (60 s; push 300 s; test 600 s) and `MCPClient.tool_environ()`
  (`env_policy.tool_env`: no tokens, no `GUARDIAN_*`; `GIT_DIR` and similar redirects removed, `GIT_TERMINAL_PROMPT=0`).
- Credentials come from the hub's git credential helper; nothing is put in a URL, and URLs in git's stderr are
  scrubbed before they are stored or returned.
- The push target is the project's `repo` (validated https URL, after `${GUARDIAN_SELF_REPO}` is resolved). Only tests pass a different `remote` to
  `publish_thread`; the CLI and the hub never accept one.
- The hub route validates the thread id and branch name, reads the body through `http.MaxBytesReader` (1 KiB), passes
  values to the CLI as argv only, and is blocked by `GUARDIAN_UI_ACTIONS=off` like running actions.

## Data

`thread_publications(id, thread_id, project_id, branch, commit_sha, remote, status, error, actor, created_at)`,
created by `db_manager.py` (`CREATE TABLE IF NOT EXISTS`). `status` is `RUNNING`, `PUSHED` or `FAILED`. Refusals are
not recorded because they have no side effects. List a thread's attempts with `DecisionDBManager.list_publications`, or over the API with
`guardian.py --publications THREAD --json` / `GET /api/threads/{thread}/publications` (newest first; see
debate-teams-api.md section 9).

## After publishing

The hub workspace is back on the tracked branch at `base_sha`. Merge the pushed branch in the repository as usual. The
server-guardian workspace is cloned from the hub checkout `~/server-guardian`, so `--project-sync` only sees the merge
after that checkout has moved (redeploy the hub, or `git -C ~/server-guardian pull --ff-only`); then run
`--project-sync` to fast-forward the workspace. A debate on the new code starts from the merged commit.

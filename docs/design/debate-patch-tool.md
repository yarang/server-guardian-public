# Judge actions: the partial edit tool (design)

English | [한국어](debate-patch-tool.ko.md)

## Problem

The judge's code change tool `jev_mcp:execute_code_action` **rewrites the whole file**. A judge that saw only functions or line ranges in the code pack
does not know the whole file content, so it cannot safely change an existing file. The fifth comparison debate of 2026-10-04 (`thread_20261004_065316_c36e2b`)
was the first to pass jev, but its only action was one new constants file; the core changes (changing `executeRaw` and `executeSSH`, the handler's 400 validation,
a new `CommandResponse` field) were not proposed because "a safe rewrite is impossible without the whole file".

## Choice of format

| Format | Example | Pros | Cons |
|---|---|---|---|
| unified diff | `@@ -248,7 +248,9 @@` | Familiar to people, applied with git tools | LLMs often get line numbers and the number of context lines wrong. With only partial code seen, context lines easily mismatch |
| **Search and replace (recommended)** | `{"file_path", "old": "exact original text", "new": "replacement"}` | No line numbers needed. It applies only when the original appears exactly once, so it cannot be applied in the wrong place. Same approach as Claude Code's Edit tool | A long original makes the prompt and the action large |

## Tool definition (recommended)

`jev_mcp:apply_patch` (risk medium)

```json
{
  "edits": [
    {"file_path": "apps/agent/metrics.go",
     "old": "func executeRaw(command string, timeoutSec int) (int, string, string, int64) {\n\tif timeoutSec <= 0 {\n\t\ttimeoutSec = 10\n\t}",
     "new": "func executeRaw(ctx context.Context, command string, timeoutSec int) (int, string, string, int64, bool) {\n\tif timeoutSec <= 0 {\n\t\ttimeoutSec = ExecDefaultTimeout\n\t}"},
    {"file_path": "apps/agent/timeout_policy.go", "old": "", "new": "package main\n..."}
  ],
  "check": true
}
```

- An empty `old` creates a new file. It is refused if the file already exists.
- One file can have several edits; they apply in order. Each `old` must appear **exactly once in the file content at that point**.
- Deleting and renaming files is not supported.
- Limits: 10 files, 30 edits, 20KB each for `old` and `new`, 100KB in total.

## Safeguards

| When | Check | On failure |
|---|---|---|
| When the proposal is stored (`record_proposals`) | The path is inside the workspace clone and is not `.git`, a secret pattern or a symbolic link. Each `old` appears exactly once in **the file at the base commit** | `INVALID` |
| When the proposal is stored | Each `old` lies **inside the code the judge actually saw** (a whole file of the code pack or a selected range). Code it did not see cannot be changed | `INVALID` |
| Right before running | The workspace clone's HEAD equals the base commit (existing rule). The target files have no uncommitted changes (except changes made by sibling actions of the same decision, see "Sibling actions" below) | 409 |
| Running | The new content of every file is built in memory first; if any fails, nothing is written (all applied or none) | `FAILED` |
| After running | The full `git diff` is stored in the result, recording what changed | - |

Changed along with it:
- Overwriting an **existing file** with `execute_code_action` is allowed only when that whole file was in the code pack. This prevents rewriting a file that was seen only partly. New files are allowed as now.
- `apply_patch` is added to the `mcp_capabilities` of the judge persona (`jev_mcp_decision`) and to the `allowed_tools` of the `cross-review` team.
- Judge prompt: change existing files with `apply_patch`, and use `execute_code_action` only for new files.

## Approval screen

- The dashboard and `--show-action` show a **unified diff against the base commit**. It is built with Python `difflib` by applying the edits to the file at the base commit.
  People approve a familiar diff, not the search-and-replace JSON.
- After running, the screen shows the real `git diff` together with the implementation checks (`checks`).
- The action summary of the jev request also carries the number of changed lines per file and the beginning of the diff.

(2026-10-18: since 2026-10-16, `apply_patch` and `execute_code_action` without `run_test` are applied automatically without approval when the jev gate is PASSED (`decided_by` `auto:engine`). The approval screen shows them as executed results, and publishing (moving the workspace clone's changes into the repository) is the last human gate. See "Implementation notes (auto-approval of code changes)" in `docs/design/debate-agent-retrieval.md` and `docs/design/publish-thread.md`.)

## Post-run check (`check: true`)

The hub had only python3 and node, without Go and pytest (checked 2026-10-04). Later the installer installs Go (see "Hub Go" below). There is still no pytest.

| Target | Check |
|---|---|
| `.py` | `python3 -m py_compile <file>` |
| `.go` | No Go tools on the hub, so no check for now. Options: install Go on the hub (about 70MB on arm64) and run `gofmt -l` and `go build ./...`, or skip the check, review the diff and commit to a branch with `git_commit_push` to check in CI or locally |
| Others | None |

If the check fails, the action becomes `FAILED` and the changed files are restored (`git checkout -- <file>`, safe because it is the workspace clone).

## Scope of work

| Layer | Change |
|---|---|
| Python | Tool definition, validation and execution in `mcp_client` (search and replace, atomic apply, revert), computing the "seen ranges" from the code pack, the `execute_code_action` overwrite restriction, diff generation, judge prompt, persona and team settings |
| Go | No change (the action API does not depend on tool names) |
| Dashboard | Show the diff in the action detail (added lines green, removed lines red), show the `git diff` of the run result |

## Decisions (2026-10-04)

1. Format: search and replace
2. The judge can change only code it saw: applied. Actions of a debate without a code pack can only create new files
   (2026-10-18: since retrieval (`debate-agent-retrieval.md`) exists, code the judge read with repo blocks or the MCP tools also counts as seen.)
3. Go check: install Go on the hub (the installer installs the official release into `~/.local/go` after checking its sha256). Right after applying, run `gofmt -l` and
   `go build ./...` of the module the changed files belong to, and revert on failure

## Implementation notes (2026-10-04)

- Code: `cli/llm-agent/patch_ops.py` (validation, apply, revert, diff), the tool definition in `mcp_client`, `action_ops.proposal_checks` (checks at proposal time),
  `code_context.ContextPack.visible/whole` (the code the judge saw).
- Hub Go: the installer downloads the official release from go.dev, compares it with the sha256 go.dev publishes, and installs it into `~/.local/go` (`HUB_GO_VERSION`, default 1.26.8). (2026-10-18: the default is now read from the `toolchain` line of `apps/agent/go.mod` (else its `go` line). It is go1.26.8 at the moment.)
  It runs with `GOTOOLCHAIN=local` and `GOFLAGS=-mod=readonly`, so no other toolchain or module is downloaded over the network during a check.
- gofmt: `metrics.go`, `model.go` and `fleet_handler.go` of this repository were never gofmt-formatted. A file that was not formatted before the edit
  does not fail the gofmt check; it is only reported. It fails and is reverted only when the edit newly breaks formatting.
- Actions of a debate without a code pack can only create new files (existing code that was not seen cannot be changed).
- Import block (added after the sixth debate of 2026-10-04): for Go and Python files selected partly, the imports go in verbatim instead of as an outline,
  and they are part of the changeable range. In the sixth debate the judge sent an edit adding `context` and `syscall` imports, and it became `INVALID` as "code never seen".
  Reproducing the same 6 edits (4 files) with the new rule at base commit `89fb0fd` passes the checks, and `go build`, `go vet` and the existing
  `go test` all succeed.
- `run_tests` runs Go commands in the module directory the package path belongs to (`go test ./apps/agent/...` → `go test ./...` in `apps/agent`).
  It puts `~/.local/go/bin` and `~/.local/bin` in front of the PATH and runs with `GOTOOLCHAIN=local` (the hub agent's PATH has neither).
- Anchor lines (added after the eighth debate): to add a new constant, the judge used as its anchor a single line `const AgentVersion = "..."` that appeared only in the outline,
  and it became `INVALID`. A Go declaration in the outline that is one verbatim original line (`package`, `const`, `var`, `type`) now counts as seen code. Function signature
  lines stay excluded, to prevent changing a signature without seeing the body.
- `go test ./...` of `run_tests` runs in the module when the root has no `go.mod` and the repository has exactly one Go module. With several, it is refused at proposal time.
- Action order: the actions of the eighth debate must run in the order #8 (constants, handler, `executeRaw`) → #9 (`executeSSH`, using #8's constants) → #10 (`go test`).
  Reproduced with the new rules at base commit `5980d68`, all three succeed in this order (`go build` and `go test` pass); running only #9 first fails `go build` and
  is reverted automatically. There is no mechanism yet that enforces the order; the build check stops a wrong order.

## Sibling actions (2026-10-12)

Problem: one verdict produced two actions (#45, #47) that change the same file against the same base commit. After #45 ran, the file was modified (uncommitted),
and #47 failed with "uncommitted changes in the files to edit". People approve and run actions one by one in the dashboard.

Rule: uncommitted changes in the target files are allowed **only when all of them were made by EXECUTED actions of the same thread and the same `base_sha` (siblings)**.
- When a sibling runs, its result (`result`) records `file_hashes` (file path -> sha256 of the content right after applying) and `applied_at_ns`. No new column.
  Both `apply_patch` and `execute_code_action` record them on success.
- Right before running, the next action collects with `action_ops.sibling_hashes`, per file, the last hash (by `applied_at_ns`, then `id`) of the EXECUTED file actions
  of the same thread and `base_sha`. If a file is uncommitted, the hash of its current content must equal that value.
  If a person edited it in between (hash mismatch), or the change was left by another thread, another `base_sha` or a FAILED action, it is refused.
- The existing rule that HEAD must equal `base_sha` stays. If the file is clean, no sibling record is needed.
- `execute_code_action` gets the same check (it had no uncommitted check before). The restriction on overwriting an existing whole file stays.
- Edits are matched against the working tree content, which holds the sibling's change. `old` must still appear exactly once, and if a sibling changed that text,
  it fails as usual with "old text not found".
- On a `check` failure the revert restores the content right before this action (the state the sibling applied), not HEAD.
- The refusal message names the file and says "the change was not made by a sibling action of this decision".
- `run_tests` is unchanged. `git_commit_push` is retired (use publish).

Implementation: `patch_ops.uncommitted_conflicts/file_hashes`, `mcp_client.call_tool(..., siblings=)`, `action_ops.sibling_hashes/run_action`.
(2026-10-18: auto-apply of code changes (`action_ops.auto_run_actions`) runs the actions of one thread in ascending id order, so it uses this chain as is, and stops at an action that does not end `EXECUTED`.)
Tests: `test_patch_ops.SiblingActionTests`.

# APIs that do not work (2026-10-12)

English | [한국어](2026-10-12-non-working-api.ko.md)

> Point-in-time record from 2026-10-12; the code has changed since.

> Snapshot as of 2026-10-12 (`7f788a6`). The status of the items may change in later commits.

From the debates run on the hub with the question "review the code and tell me which APIs and features do not work now", the conclusions of the 9 approved ones (2026-10-08 to 10-11) were collected and checked one by one against the code as of 2026-10-12 (`7f788a6`). Items with a split verdict were classified with Jev (`jev-1.13.0`).

Source threads: `thread_20261008_052043_22a84e`, `thread_20261009_080448_57b028`, `thread_20261009_150754_6c1335`, `thread_20261009_150754_83c5cc`, `thread_20261009_152646_c8203a`, `thread_20261009_152646_0e4562`, `thread_20261011_161828_8c312b`, `thread_20261011_170120_34775c`, `thread_20261011_171548_fe20ef`

## 1. Already fixed

| Problem | Symptom | Commit |
|---|---|---|
| Fixed fleet command stream timeout | `POST /api/fleet/exec/stream` ignores `timeout_sec` and cuts off at 15 seconds | `234f5fe`, `2ac825e` (the UI sends the value too) |
| Debate history also 503 without the CLI | `guard()` also blocked the read and cancel routes that only read the disk | `612cc2c` |
| No method restriction on GET-only routes | The read routes in `main.go` accept every method | `612cc2c` |
| Pinned host key check bypassed | Sending a remote command by name or hostname fell through to `StrictHostKeyChecking=accept-new` | `522fb6b` |
| Fleet results out of order | Diagnostics and run results pile up in completion order | Already handled with an index-based slice |
| No `Allow` header on command route 405 | Violates RFC 7231 | Already handled |
| Child processes survive after `/api/exec` timeout | Only `sh` is killed, so the child holds the output and the response is late (30 seconds instead of 1 in the test) | `1dad8e1` |
| Panic on cancel right after a debate starts | Cancelling before the process starts calls `kill` on nil, and the process then runs to the end | `984b7e6` |
| No body size limit on command routes | Only `/api/exec`, `/api/exec/remote`, `/api/fleet/exec` and the stream had no `MaxBytesReader` | `7f788a6` (64 KiB, 413 when exceeded) |

## 2. Remaining defects (on hold, need a design decision)

| Problem | Impact | Decision needed |
|---|---|---|
| Debate list limited to 50 | `GET /api/debates` returns only the latest 50, no paging. Older jobs can only be looked up by id | Paging method (cursor, offset) and how the UI shows it |
| Remote process survives a remote command timeout | `executeSSH` only kills the local `ssh` client. The command on the target server can keep running | Forward SIGHUP with `ssh -tt`, or wrap in `timeout` on the remote side |
| Fleet stream does not notice a dropped connection | Even after the client disconnects, the SSH command runs until `timeout_sec` | Pass the request context to `StreamExecFleet` (Jev: fine to fix now, needs review at confidence 0.6) |
| Fleet output buffer and concurrency unlimited | Memory and goroutines grow with large output or a large fleet | Upper limits |

## 3. Design constraints, not defects

| Item | Reason |
|---|---|
| The UI does not use the metrics SSE (`/api/stream/metrics`) | The server works; the UI uses polling (Jev 0.85) |
| Debate run and settings APIs return 503 without the CLI | Intended guard. The read routes were split off in section 1 |
| `POST/PUT/DELETE /api/nodes` return 409 without `GUARDIAN_SERVERS_FILE` | By design, writes are allowed only when the inventory file location is set explicitly |
| `~/.config/agentthread/servers.json` fallback | A path documented in a comment. The debate's claim that it is a typo was rejected (Jev 0.83) |
| No `access_token` query auth on debate events | Not needed: the UI reads them with fetch and a header, not EventSource |
| The list of logged-out sessions is only in memory | Forgotten on restart, but the cookie expires at `sessionMaxAge`. Jev verdict split (defect 0.45, constraint 0.49) |
| `GET /api/nodes/registration`, `POST /api/nodes/check` | Diagnostic APIs that always respond and work normally even when not configured |
| `r.results[0]` in `api.ts` | The CLI guarantees one result on success, so it does not fail now. Defensive code is optional |
| The fleet stream sends output after the command ends | By design: per-node progress events are real time and output is sent after completion |

## Notes on debate quality

- The early debates (10-08) could not read code and guessed from the documents only; after the repository reading tools were attached, the conclusions started to match the code.
- The same defect (child process termination) was pointed out in three debates, but it was rejected together with others in the patch cleanup step and remained until today.
- False positives from the debates: the typo claim (`agentthread`), missing auth on debate events, treating `DisallowUnknownFields` as a defect, and the claim that `newCmd` has no `Setpgid`.

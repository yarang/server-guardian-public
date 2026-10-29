# Server Guardian system analysis report (2026-10-04)

English | [한국어](2026-10-04-system-analysis.ko.md)

> Point-in-time record from 2026-10-04; the code has changed since.

> **This is a snapshot as of 2026-10-04.** Changed since: CI was added (`scripts/ci.sh`, GitHub Actions, 2026-10-05). The install script collects failed checks, exits non-zero and keeps the previous binary as `~/.guardian/agent.prev`. Hub sudo is allowed by default (`--hub-sudo`; "sudo is blocked" in item 6 below is the state at that time). `apply_patch` and Publish were added. For the current state, see the README and `docs/design/`. The body is a record of that time and was not edited.

Five analysis agents split the areas and analysed them read-only. No files were modified, no servers were accessed, and no real LLM calls were made.

| Owner | Scope |
|---|---|
| agent-analyst | `apps/agent` (Go agent, 9870 API/SSE, debate job execution) |
| llm-analyst | `cli/llm-agent` (debate engine, provider switching, action approval, jev gate) |
| frontend-analyst | `apps/web-desktop`, `packages/guardian-ui`, `.design-sync` |
| infra-analyst | `deploy/`, `scripts/`, `configs/` |
| security-reviewer | Security across all areas |

## Summary

The debate engine and the action approval procedure are solid. But because of the early execution paths (`/api/exec`, the dashboard `main.ts`, the `run_tests` tool) and the permission structure, **breaching a single attack point is enough to take over all 24 servers as root.**

## 1. Paths to full server takeover

1. Entry point (any one of three is enough)
   - Stored XSS in the dashboard: 18 `innerHTML` uses in `apps/web-desktop/src/main.ts` have no escaping. Example: an operator views, with `tail` in the Exec tab, a failed SSH login record whose user name is `<img onerror=...>`.
   - LLM prompt injection: agy and opencode run without a sandbox and `AGENT_TOKEN` is in their environment (`cli/llm-agent/llm_runner.py:120,229-250`).
   - `run_tests` bypass: the risk level is low but only the start of the command is checked, so `go test -exec /bin/sh`, `-toolexec`, `-c -o` and `pytest -p` pass (`cli/llm-agent/mcp_client.py:47,178-188`).
2. Call `/api/exec` or `/api/fleet/exec`. Unsandboxed `sh -c`.
3. It runs as the `ubuntu` account, which can sudo without a password.
4. The same account on the hub holds the fleet SSH key without a `command=` restriction, so it controls all 24 servers as root.

The two LLM paths bypass the jev evaluation and operator approval, or pass only a weak confirmation (a checkbox).

## 2. Security priorities

| Rank | Problem | Location | Action |
|---|---|---|---|
| 1 | Dashboard XSS, no CSP | `apps/web-desktop/src/main.ts`, `src-tauri/tauri.conf.json` (`csp: null`) | Apply a shared `escapeHtml`, set a CSP |
| 2 | `run_tests` argument bypass, low risk level | `cli/llm-agent/mcp_client.py:47,178-188,305` | Exact flag allow-list, risk level high, remove secrets from the subprocess environment |
| 3 | LLM CLIs hold secrets without a sandbox | `cli/llm-agent/llm_runner.py:120,229-250` | Minimal environment per provider, disable tools |
| 4 | Empty token disables auth, CORS `*`, Content-Type not checked, commands run via GET | `apps/agent/auth.go:60`, `main.go:18,126,218,323` | Refuse to start without a token, check Origin/Host/Content-Type |
| 5 | ssh option injection via `target_host` | `apps/agent/servers_handler.go:20` | Match against `servers.json`, `--` before the host |
| 6 | Agent has excessive privileges | `deploy/systemd/server-guardian-agent.service` | Dedicated non-sudo account, systemd hardening options, separate account for the fleet key |

Other (Low): session cookie lasts 30 days, cannot be revoked and has no Secure flag; `access_token` passed in the query; `openssl -pass pass:` exposes the password in `ps`; AES-CBC without a MAC; agy prompt exposed in argv.

## 3. Real results cannot be told apart from made-up ones

- Frontend: on command failure it shows `exit_code 0` and "Action applied successfully" (`api.ts:249-255`); on request failure every server is ONLINE "up 14 days" (`api.ts:385-401`); the AI tab is hard-coded and, because the attribute is truncated, only runs `echo ` (`api.ts:260-344`, `main.ts:606`).
- Backend metrics: uptime is the agent's uptime (`metrics.go:49-57`), macOS memory is fixed at 65%, CPU% is loadavg / core count, fake defaults on failure, `/api/status` is always healthy.
- sshd audit: last value wins (sshd uses the first value, `sshd.go:74`), `Include` and `Match` are ignored, key fingerprints do not match `ssh-keygen -lf` (`sshd.go:275`), key management is view-only.

## 4. LLM layer correctness

- When a provider switch starts a new session, the earlier debate content and code are not sent (`debate_engine.py:449-453`, `llm_runner.py:386`).
- The agy prompt is passed as a single argv; above the 128KB limit it silently switches on E2BIG (`llm_runner.py:232,321`).
- The time budget can exceed the hub job limit (1 hour), and the record is saved only at the end, so everything is lost on a forced kill.
- When a jev re-check passes, it also releases HELD actions of earlier decisions in the same thread (`action_ops.py:177`).
- On a tool exception the action stays stuck in RUNNING forever (`mcp_client.py:218`).
- `execute_code_action` has no secret-file check; `git_commit_push` uses `git add -A`.
- Differences from the design: the default team inherits `server_remediation` without a warning, `target_group` is not implemented, uncommitted changes give FAILED instead of 409, no symlink check.

## 5. Operations and quality

- No CI (no shellcheck, no config JSON validation, no automated go/python tests).
- No install rollback; the install counts as success even when the post-install health check fails (`scripts/install-agent-service.sh:216-238`).
- No `agent.log` rotation, no backup failure alert, no jev-mcp install script.
- Supply chain: agy installed with curl|bash, codex version not pinned, unused `npx -y` filesystem-mcp entry (`configs/mcp_servers.json:13-17`).
- macOS portability: needs `wait -n` (bash 4.3+) and `timeout` (coreutils).
- `servers.json` is tracked in git with internal IPs and key paths. The `.enc` credential bundle is excluded by gitignore and was never committed.
- Tests: 176 Python tests pass, 3 skipped (no Go). The 40 Go tests focus on debate and auth; metrics, sshd, command execution and `main.ts` have no tests. The analysis environment had no Go or node, so `go test` and the frontend build were not run.
- `packages/guardian-ui`: a design system of 24 React 18 components, but not connected to the app anywhere.

## 6. What is done well

- Debate jobs run via argv without a shell and are terminated per process group.
- Action state transitions are atomic, validated twice (at proposal and right before execution), and check whether the code changed (base_sha).
- The risk level comes from the catalog, not the LLM; high-risk actions require typing the action number.
- The jev gate is fail-closed; `GUARDIAN_UI_ACTIONS=off` is a kill switch.
- Write tools are limited to a separate workspace clone; the running repository and protected branches are refused; no push.
- 256-bit token, constant-time comparison, HttpOnly + SameSite=Strict cookie, loopback binding by default.
- `actions.ts` and `debate.ts` escape consistently.

## 7. Recommended order

1. Security hotfix: items 1-5 of the table in section 2.
2. Remove fake data: show an error state on failure; remove the AI tab or connect it to the real action approval flow.
3. Build CI: gofmt, `go vet`, `go test -race`, pytest, shellcheck, JSON schema validation.
4. Stabilise the LLM layer: resend context on a switch, cap prompt size, save the record per round, strengthen action safeguards.
5. Privilege separation and install rollback; rewrite the sshd audit and metrics.

## 8. Phase 1 hotfix progress

Phase 1 work was started and then stopped. The partial changes were **not committed because they were not reviewed or tested**; they are kept in a local stash.

- Stash name: `wip: phase-1 security hotfix (partial, unreviewed, untested) 2026-10-04`
- Files included: `apps/agent/main.go`, `apps/agent/security.go` (new), `apps/web-desktop/{package.json, src-tauri/tauri.conf.json, src/actions.ts, src/debate.ts, src/main.ts, src/escape.ts (new), tests/}`, `cli/llm-agent/env_policy.py` (new)
- To continue, check with `git stash list`, then `git stash apply`.

Decisions for resuming:
- Refuse to start when the token is empty. The exception is `GUARDIAN_INSECURE_NO_AUTH=1` with every binding on loopback.
- Remove the AI tab's run button. Fixing only the escaping would make the hard-coded high-risk sshd command actually run.
- Switching the fleet-wide command SSE to POST and cleaning up the localStorage token are phase 2.
- Remaining risk: even with the token removed from the environment, agy running as the same `ubuntu` account can read `~/.guardian/agent.env` directly. Account separation (item 6) solves this.

**Done (2026-10-04)**: the partial changes in the stash were reviewed, completed, tested, committed and deployed to the hub. This covers items 1-5 of section 2 and the frontend items of section 3.
`d13adc1` (agent: token required, Host/Origin/Content-Type checks, CSP, `target_host` validation), `637b446` (dashboard escaping, CSP, fake data removed),
`80fc18e` (`run_tests` flag allow-list and risk level high, minimal environment per subprocess and provider), `ef44366` (`AGENT_ALLOWED_HOSTS`).
The review also closed 3 holes that were not in the stash (cross-site GET without Origin, empty-body form POST, CORS `*`). Item 6 (account separation) and the SSE switch to POST remain.
The stash `wip: phase-1 security hotfix ...` is superseded by these commits.

**Item 6 progress (2026-10-05, `275c89b`)**: as decided, the run account stays `ubuntu`, and the LLM CLIs run under the same account as the agent. Instead, systemd isolation and fleet key protection were applied.
- The fleet private key moved to `/etc/guardian/fleet_key` (root 0600). The key is used only through the ssh-agent socket of `guardian-fleet-agent.service`. `~/.ssh/guardian_fleet` on the hub was deleted.
- `NoNewPrivileges` was applied to `server-guardian-agent` and `db-backup`. So sudo is blocked in the whole agent and LLM CLI process tree. `InaccessiblePaths` (fleet key, `id_ed25519`, `backup.env`, headscale config), `ProtectSystem=full`, `PrivateTmp` and kernel protection options were also applied.
- The `systemd-analyze security` score went from 9.2 UNSAFE to 4.0 OK.
- Hub verification results:
  - Inside the agent namespace, reading the key and sudo are blocked.
  - Fleet SSH succeeded on all 23 servers.
  - claude, codex and agy all answered PONG.
  - A manual run of the backup service and replication succeeded.
- Remaining risk: as decided, the permission to use the key stays. That is, it can ssh to a fleet server and use sudo there. Also, `~/.guardian/agent.env` and `llm.env` are still readable inside the agent tree, because `agent_client.py` and `llm_runner.py` read them directly. Blocking that needs the LLM CLIs to run under a separate account.
- Follow-up (2026-10-05): hub sudo became an install option. `--hub-sudo` is the default and `--no-hub-sudo` turns it off. With it on, the security score is 8.5 and root can read the key. With it off, 4.0.
  However, the hub itself is also a fleet SSH target, so the bypass `ssh site-a-arm1 sudo …` remains. By the user's decision this bypass stays.

**SSE POST switch (2026-10-05)**: `/api/fleet/exec/stream` is now POST only. The body is JSON `{command, targets}`, and GET is refused with 405. The command is not in the URL, so it does not end up in access logs or browser history. The request goes through the CORS preflight and Content-Type check like every other POST.
- The dashboard reads the SSE body with fetch instead of `EventSource`. The token is sent in the `Authorization` header.
- This path does not accept `?access_token=`. The query token remains only on the read-only `/api/stream/metrics` (not used by the current dashboard).
- Python `stream_fleet_exec` was also changed to POST.
- During this work a bug in `scripts/ci.sh` was fixed. Inside a `( … ) || fail` construct `set -e` is off, so even if `tsc` or `go vet` failed, the block passed as long as its last step succeeded.
- The remaining phase 2 item is the localStorage token cleanup. Only the cross-origin client (Tauri) keeps a token.

**localStorage token cleanup (2026-10-05)**: the dashboard no longer stores `AGENT_TOKEN` in localStorage.
- The hub dashboard (same origin) uses only the HttpOnly session cookie, as before.
- The Tauri app keeps the token in memory only. For persistent storage it uses the OS credential store (macOS Keychain, Windows Credential Manager, Linux keyutils). The Rust commands `token_get`/`token_set`/`token_delete` (`src-tauri/src/token_store.rs`) manage one entry per agent origin. So changing the agent address does not send another agent's token.
- The `guardian_agent_token` left by older versions is handled on first run. The Tauri app moves it to the keychain; other environments delete it.
- If the keychain is not available, the app works with a session that lasts only while the app is running.
- The Linux keyutils store disappears on logout.
- Of the phase 2 items, only the Other (Low) items remain: session cookie lifetime, revocation and Secure; `openssl -pass`; AES-CBC MAC; agy argv.

**Other (Low) items cleanup (2026-10-05)**
- **Session cookie**:
  - The value is now `v2.<issue time>.<random nonce>.<HMAC>`. It expires after 7 days (previously 30 days, a fixed value without expiry), and the value changes on every login.
  - Logout is POST only. It revokes that nonce on the server, so even a stolen cookie cannot be used after logout.
  - `Secure` is added when accessed over HTTPS.
  - Changing `AGENT_TOKEN` invalidates all sessions.
  - The revocation list is in memory and is forgotten when the agent restarts. The 7-day expiry still bounds it.
  - Existing v1 cookies are refused, so after the deploy you must log in once more.
- **Credential bundle** (`export-credentials.sh`/`import-credentials.sh`):
  - Encryption is done by `scripts/creds_crypto.py`. The password is passed to openssl through a pipe (`-pass fd:N`), so it does not show in `ps`.
  - Format v2 appends HMAC-SHA256 after AES-256-CBC (encrypt-then-MAC). PBKDF2-SHA256 runs 600,000 iterations, and the MAC key is derived separately. Tampered or truncated files are refused before decryption.
  - Files in the old format open only with `--legacy`.
  - Export asks for the password twice for confirmation.
- **agy prompt**: sent as stream-json on stdin (`{"event":"user","message":{"content":…}}`) instead of argv (`--print=<prompt>`). Session resume (`--conversation`) works the same way.

**Some section 3 correctness items handled (2026-10-05, after `45dff85`)**
- **Fleet inventory:** status is shown from the diagnostics run when the page opens, instead of fixed values in `servers.json`. The static `status` and `tailscale_status` fields were removed.
- **sshd audit:**
  - With root (hub sudo), it reads the effective configuration with `sshd -T`. The hub applies `PasswordAuthentication no` from an `Include`d file, so the score was corrected from 50 to 75.
  - Without root, it reads the config files directly. It uses the first value, follows Include and skips Match blocks. In this case the syntax check shows "not checked".
  - The failure count counts only FAIL. WARN and INFO are counted separately.
- **authorized_keys:** fingerprints are computed to the same value as `ssh-keygen -lf`. Lines with options such as `from=` are parsed too, and the options are shown on screen.
- **macOS memory:** computed from vm_stat active+wired+compressed instead of a fixed 65%. If memory cannot be read, it reports 0 instead of an estimate.
- **Remaining section 3 items:** uptime is the agent's uptime, the CPU% calculation, `/api/status` always healthy, and the server count default of 21 in `llm_runner.py`.
- **Remaining section 3 items handled (2026-10-05):**
  - uptime: now the time since host boot. Linux reads `/proc/uptime`, macOS reads `kern.boottime`. Previously it was the agent's uptime.
  - CPU%: Linux computes it from the difference of two `/proc/stat` samples, macOS uses a 1-second sample of `top`. Previously it was loadavg divided by core count.
  - Disk defaults (100GB/40%): if it cannot read them, it reports 0 and `issues`.
  - `/api/status`: returns `degraded` with `reasons` when metrics cannot be read, or disk ≥90% or memory ≥95%. The dashboard badge shows three states: online/degraded/offline.
  - `llm_runner` built-in engine: removed the server count default of 21 used when there were no diagnostics. Also removed fixed phrases such as "매우 안정적" ("very stable") and the security score default of 50. It now only restates measured values.
  - Fixed phrases removed from the built-in engine (2026-10-05):
    - For the judge, critic and expert roles, returning a made-up verdict ("APPROVED") and critiques and proposals was replaced with a "cannot produce a response" notice.
    - Returning the persona's system prompt as if it were the answer was removed.
    - The final default response "정상 범위 내에 있습니다" ("within the normal range") was removed.
    - Every built-in engine response starts with the "⚠️ [LLM 응답 없음]" banner and the failure cause.
    - Fixed a bug where the metrics summary was built but not returned.
    - Deleted `queryMultiAgent`, which the dashboard did not use. It contained a fixed "AI 종합 의견" ("AI overall opinion") and hard-coded actions.

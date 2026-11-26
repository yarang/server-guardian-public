# Reference notes: Claude Code Remote Control and cross-session messaging

English | [한국어](notes.ko.md)

Collected 2026-11-17 from the vendor's public documentation, for [operator-sessions.md](../../design/operator-sessions.md).
These notes describe a product feature that Guardian does not use and does not depend on. Items marked "not in the
docs" were looked for and not found; re-check the links before relying on any detail, since the feature changes with
CLI versions.

## Sources

| Topic | Link |
|---|---|
| Remote Control (start, connection, security, troubleshooting) | https://code.claude.com/docs/en/remote-control.md |
| Cross-session messaging (`ListAgents`, `SendMessage`, inbox socket, other machines) | https://code.claude.com/docs/en/cross-session-messaging.md |
| Authentication (login methods, precedence, credential storage, expiry) | https://code.claude.com/docs/en/authentication.md |
| Error messages (Remote Control login, renewal, account change) | https://code.claude.com/docs/en/errors.md |
| Sessions (names, `/rename`, resume) | https://code.claude.com/docs/en/sessions.md |
| Mobile app | https://code.claude.com/docs/en/mobile.md |

Related projects already evaluated in [resident-agents.md](../../design/resident-agents.md) ("Reuse evaluation"):
`awslabs/cli-agent-orchestrator`, `mcp_agent_mail`, `claude-squad`, `ccmanager`, `Tmux-Orchestrator`.

## How it starts

- `claude remote-control` (alias `claude rc`): server mode, stays in the terminal and takes several sessions
  (`--capacity`, default 32; `--spawn same-dir|worktree|session`).
- `claude --remote-control` (`--rc`): an interactive session with a remote link; local typing still works.
- `/remote-control` (`/rc`) inside a session: links the current conversation.
- First run of server mode asks `Enable Remote Control? (y/n)`; a new directory asks to be trusted.
- A URL is printed; server mode toggles a QR code with the space bar. The session also appears by name in claude.ai/code
  and the phone app.

## Authentication

| Credential | Remote Control |
|---|---|
| claude.ai login (`/login`, `claude auth login`) | required; short-lived, single-purpose connection credentials are renewed from it |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper` | refused ("requires claude.ai subscription auth") |
| `claude setup-token` / `CLAUDE_CODE_OAUTH_TOKEN` | model requests only; cannot create a Remote Control session |
| Bedrock, Vertex, Foundry, gateways, another `ANTHROPIC_BASE_URL` | refused; the host must be `api.anthropic.com` |

- An API key present in the environment takes precedence over the login and makes Remote Control fail; `/status` shows
  which one is used.
- Credentials are kept in the macOS Keychain, or in `~/.claude/.credentials.json` (mode 0600) on Linux and Windows.
- Plans: Pro, Max, Ultra, Team, Enterprise. Team and Enterprise need the Owner's switch; Enterprise custom roles need
  both "cloud sessions" and "Claude Code remote control". Not available with ZDR or HIPAA configurations, or when
  `disableRemoteControl` is set.

## Registration and connection

- The local process registers with the vendor API and polls for work over outbound HTTPS; it opens no inbound port.
- Messages to and from claude.ai or the app go through the API as a streaming connection.
- While linked, the conversation (messages, answers, tool activity) is stored on the vendor's servers; execution and
  file access stay local.
- The session id is the part of the claude.ai/code URL between `/code/` and `?`. How the machine itself is identified
  is not in the docs.
- Server mode's connection credentials last about 26 hours; if they cannot be renewed it exits with
  `Poll: Authentication failed (401)`.

## Names and discovery

- Name order: `--name` / argument of `--remote-control` or `/remote-control`; then `/rename`; then the last meaningful
  message; then a generated name (`<host>-<adjective>-<noun>`, prefix via `--remote-control-session-name-prefix`).
- Other sessions find it with `ListAgents` and write with `SendMessage`; people see the same list with `/list-agents`
  (`/peers`). Sessions without a name chosen by a person show as `(unnamed session)` and their directories are hidden
  while Remote Control is linked.
- Sessions on the same machine talk over a local socket restricted to the OS user, with a per-session token
  (`CLAUDE_CODE_MESSAGING_TOKEN`); this path does not touch the vendor's servers.
- Sessions on other machines are reached through the vendor's servers and that machine's Remote Control link. A
  sender that is not linked gets no reply address. The sender is not told whether the message was read.
- Receiving sessions choose `crossSessionInbound`: `accept`, `hold` (wait for the user) or `refuse`.

## Authority

- A message from another session is not the user's consent: it cannot answer a permission prompt, change settings or
  `CLAUDE.md`, and the receiver's own permission rules apply.
- Permission prompts and questions stay open until answered and can be answered remotely; other dialogs close after
  `dialogExpiry` (default 5 minutes) with their default.
- Trusted Devices (beta, Team and Enterprise): enrolled devices only, and a login within 18 hours, renewed with a
  platform authenticator; the vendor stores the device's public key and basic metadata.

## Disconnects and expiry

| Event | Behaviour |
|---|---|
| Network back after sleep | interactive session reconnects; after about 30 minutes of failed heartbeats run `/remote-control` again |
| Server mode, network down about 10 minutes | the process exits |
| Process stopped or machine rebooted | session goes offline; within about 4 hours it can be resumed in the same directory (`claude remote-control`, `--continue`, `--session-id`) |
| Login expiring | warning from 3 days before; after expiry Remote Control stops and does not recover by itself, the local session goes on; `/login` then `/remote-control` |
| App signed out | the session ends |
| Another account signed in | the link drops; old sessions do not move to the new account |
| Organization policy turns it off | the link drops and the session is archived |
| Device removed in account settings | its credential is revoked at once |

## Not in the docs

- The exact lifetime of connection credentials (only "short-lived").
- The format of machine and session identifiers on the vendor side.
- Which permission mode applies to a remote request beyond the session's own mode.
- Whether `/logout` ends a running remote session at once.
- The minimum CLI version for the base feature (only per-feature minimums, for example `--continue` and `--session-id`
  from v2.1.200).

## Observed in the operator's fleet (2026-11-16)

- A session on a Windows workstation (WSL) linked by Remote Control did not receive the first two cross-machine messages
  sent to it, while its own messages arrived; the send result said delivery was not confirmed. A later reply to its
  message arrived. This is the gap SD1 and the at-least-once delivery of `resident-agents.md` address.
- Sessions on a workstation that rebooted dropped out of the list and came back only when started again.

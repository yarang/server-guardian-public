# Operator sessions: remote control of interactive sessions through the hub (design)

English | [한국어](operator-sessions.ko.md)

Status: proposed 2026-11-17; decisions SD1-SD8 and the two follow-ups SD3a and SD6a made by the operator 2026-11-20
(see "Decisions"); SD1 and SD2 revised by the operator the same day after S0 part 2: the workstation opens the link to
the hub, not the other way round; SD9 (`from=` on each link key), SD10 (Guardian owns the hub's `authorized_keys2`) and SD11 (remote approval only in sessions
the person opted in, narrows SD5) decided the same day after S0 part 3; SD12 (the hub address the daemon dials: tailnet
IPv4, then a DNS name) the same day; nothing built. Next: the rest of probe S0. Diagram:
[operator-sessions-architecture.mmd](operator-sessions-architecture.mmd). Reference notes on the product feature this
copies: [../analysis/2026-11-17-remote-control-reference/notes.md](../analysis/2026-11-17-remote-control-reference/notes.md).

## Goal

The operator's request (2026-11-17): build, on Guardian, something that does the work of Claude Code's Remote Control
and cross-session messaging for the operator's own machines, without depending on the vendor's relay.

The operator runs **interactive** Claude Code sessions on several workstations (macOS, WSL on Windows, and native
Windows). Each one is a person's working session, not a resident agent. Today these sessions are reached in two ways,
and both fall short:

- **Claude Code Remote Control.** Sessions register with the vendor's servers, show up by name in other sessions'
  `ListAgents`, take `SendMessage`, and can be driven from claude.ai or the phone app. It needs a claude.ai login (no
  API key, no `claude setup-token` token, no other `ANTHROPIC_BASE_URL`), the transcript is stored on the vendor's
  servers while connected, a long-running session stops when the login expires, and delivery between machines is not
  confirmed to the sender (in practice a message to a session on another machine was lost twice before one arrived).
- **SSH from one workstation to another.** It works with API-key CLIs, but it is a person typing commands; there is no
  name to send to, no mailbox, no presence, and nothing for a session to receive while it is open.

What this adds to Guardian, in one line: an **operator session** is an interactive session on a workstation that
joins the hub's mail network of `resident-agents.md` under a name, so the operator (dashboard, phone over the tailnet),
other sessions and resident agents can see it and write to it, and the operator can answer its permission prompts.

## What exists, and what does not fit

| Need | Already in Guardian | Gap |
|---|---|---|
| Mailbox, envelope, idempotent send, at-least-once delivery, hop and rate limits | `resident-agents.md` (M1, M2 deployed 2026-11-15 as `d72baff`): `mail_store.py`, `mail_relay.py`, mailbox MCP | addresses exist only for agents on Tier-1 nodes |
| Transport to a machine | worker protocol 3: the hub opens one SSH stream per node over the tailnet | workstations are not fleet hosts, are not on the tailnet yet, may sleep, and run no SSH server by default (and need none: they open the stream themselves) |
| Join the tailnet | mesh join (`POST /api/mesh/join`, `docs/design/mesh-join.md`) | no workstation tag; no rule that lets workstations reach only the hub's SSH port |
| Wake an idle CLI | resident `Stop` hook continuation and fixed wake line (M0, M2d) | an interactive session has a person at the keyboard; a wake must never race the person |
| Identity from the socket, not from the envelope | daemon stamps calls per agent and epoch | a workstation has no worker daemon; the hub binds no key to a workstation |
| Dashboard and auth | Go API with `AGENT_TOKEN`, tailnet-only | no view of sessions, no remote prompt, no remote approval, no session summaries |

Resident agents are model sessions the hub starts and supervises. Operator sessions are started by a person, belong to
that person, and stop when the person closes them. The hub never starts, restarts or resumes an operator session.

## Comparison with the product feature

| Claude Code Remote Control | This design |
|---|---|
| Session registers with the vendor API and polls over outbound HTTPS; no inbound port | Workstation joins the tailnet and opens one outbound SSH stream to the hub; no inbound port and no SSH server on the workstation (SD1) |
| claude.ai OAuth login only; short-lived connection credentials renewed from it | One key per workstation, made on it; the hub binds the key to the link entry and the workstation id (`command=` + `restrict`); no `AGENT_TOKEN` on the workstation; works with API-key CLIs and any provider (SD2) |
| Session name from `--name`, `/rename`, or generated | Address `<name>@<workstation>`, name chosen when the session links; the workstation id comes from the hub's line for the key that authenticated, never a client claim |
| `ListAgents` rows: idle, busy, offline | `session_list` and the dashboard: `linked`, `busy`, `idle`, `away`, `offline` (see "States") |
| Message from another session is data and grants nothing | Same rule, unchanged from resident agents |
| Transcript stored on vendor servers while connected | Only model-written turn summaries reach the hub, never tool output, kept 7 days (SD6, SD6a) |
| Remote approval of permission prompts from the app | Remote approval through a `PermissionRequest` hook with a deadline, on only in a session the person started with it, never auto-allow (SD5, SD11) |
| Phone app and claude.ai | Dashboard over the tailnet; no internet exposure |

## Architecture

```
operator (dashboard over the tailnet, AGENT_TOKEN)
        |  Go API spawns guardian.py --session-* (newCmd)
   hub site-a-arm1: mail store (+ address kind "session") + session and workstation registry
        |  link entry guardian.py --session-link, started by the hub's sshd, one per linked workstation
        ^  hub ~/.ssh/authorized_keys2, owned whole by Guardian, one line per workstation:
        |    from="<tailnet address>",command="<python> <guardian.py> --session-link --workstation <id>",restrict <key>
        |  one SSH stream per linked workstation, opened BY THE WORKSTATION over the tailnet (ssh -T)
   ws-mac-1 / ws-wsl-1 / ws-win-1: session link daemon (stdlib Python, user service)
        own key ~/.guardian/session/link-key, the hub's host key pinned; no SSH server
        dials the hub's tailnet IPv4, then its link name on a connection failure (SD12)
        <- per-session socket or named pipe <- session MCP (stdio child of the interactive CLI)
        <- hooks of the interactive CLI: SessionStart, UserPromptSubmit, Stop, PermissionRequest, SessionEnd
        -> tmux wake (macOS, WSL; operator mail only; never mail content)
```

Rules of the shape:

1. **Still a star.** Workstations never reach each other or a fleet node. All mail goes through the hub.
2. **The workstation opens its own link (SD1, SD2).** Each workstation joins the tailnet as a tagged node through the
   existing mesh join, with a new workstation tag. Its link daemon dials the hub's SSH over the tailnet with the
   workstation's **own key** (`~/.guardian/session/link-key`, made by the installer; the private key never leaves the
   workstation). On the hub, that key's line in `~/.ssh/authorized_keys2` (SD10) is
   `from="<workstation tailnet address>",command="<python> <guardian.py> --session-link --workstation <id>",restrict`:
   no shell, no PTY, no forwarding, nothing but the link entry, and only from that workstation's tailnet address
   (SD9). The hub writes the workstation id and the address into that line at registration, so the id comes from the
   key that authenticated, never from the client. Guardian owns `authorized_keys2` whole (SD10): it writes the
   file from the workstation registry, one line per workstation with the comment `guardian-session:<id>`, to a temporary
   file and renames it into place, and never touches the operator's `authorized_keys`. The workstation holds no `AGENT_TOKEN` and no
   other hub credential, and pins the hub's host key at install. The daemon dials the hub's tailnet IPv4, which the
   installer takes from Settings (`--hub-address`); only when that connection fails does it try the hub's link name
   (`--hub-name`, optional), an A record the operator keeps on their own domain, DNS-only, pointing at the same tailnet
   IPv4 (SD12). Both paths dial with `HostKeyAlias <hub id>`, so one pinned entry covers both; a host key mismatch on
   either path ends the dial and is reported, never retried on the other path. When the hub's tailnet IPv4 changes,
   the name keeps the link up until the installer runs again with the new address. Guardian never writes DNS. The link entry takes in the S0 part 2 and 3 findings: it
   refuses a non-empty `SSH_ORIGINAL_COMMAND`, sets its own environment and uses absolute paths, and sends a fixed
   versioned greeting that the daemon checks before anything else; the daemon dials with `-T` and the entry refuses a
   terminal on stdin. Once the greeting and the daemon's reply have both passed, a new stream for a workstation ends
   the old one; before that the entry touches neither the store nor another stream, so a dial that never completes
   cannot cut a healthy link. Both ends send a heartbeat inside the link protocol, because the hub's sshd sends no
   keepalive. Direction of connection is not direction of
   messages: mail, wakes, approval requests and their answers all travel both ways on the stream the workstation
   opened.
3. **No SSH server on a workstation.** The link needs only the SSH client, which macOS, WSL distributions and Windows
   ship. A tailnet ACL that lets workstations reach only the hub's SSH port is a headscale policy change; Guardian never
   writes headscale policy (its only headscale writes are join and remove), so the operator applies it. **The hub reaches
   a workstation only when needed:** it never dials one for the link. If the operator ever wants the hub on a
   workstation (a one-off check, say), the operator turns that workstation's SSH server on and adds a separate key with
   `expiry-time=` (and `command=` and `restrict` where they fit), then takes both away again. Guardian builds no tool for
   this and no phase depends on it.
4. **A sleeping workstation is `offline`, not an error.** The daemon reconnects with backoff when the machine wakes or
   the network comes back; the hub never dials. Mail stays `queued` on the hub until the stream is back or the mail
   expires.
5. **One daemon per workstation is the authority there.** The link daemon opens and holds the stream, a spool per session, the
   per-session sockets (named pipes on native Windows), and stamps every call with the session bound to the socket it
   came in on. It replaces, for workstations, the worker daemon of `remote-agents.md`; it runs no model of its own and
   starts no CLI (the summary call of SD6a runs as a child of the session's hook, see "Summaries").
6. **The store is the resident mail store.** Sessions are a new address kind in `mail.db`; envelope, kinds, idempotence,
   hop, thread and size limits are reused as they are. The task board is not offered to sessions in S1-S3 (open
   point 1).
7. **Calls are synchronous**, as for resident agents: a session MCP call waits for the hub's answer (20 s) and returns an
   error when the stream is down. No offline outbox.
8. **The person comes first.** Mail is surfaced at the two points the CLI offers (prompt submit, turn end). The only
   thing ever typed into a terminal is the fixed wake line of SD3, under the guard in "Wake".

### Addresses

- `<name>@<workstation>`, for example `cleaner@ws-mac-1`. `name` is given by the session when it links
  (`session_link(name)` in the session MCP, or `GUARDIAN_SESSION_NAME`), unique per workstation; a second session that
  asks for a name in use gets a refusal and keeps its old address or none.
- `<workstation>` is the id the hub wrote into the `authorized_keys2` line of the key the stream authenticated with. A
  client never sends it.
- **One address space (SD4).** Sessions, resident agents and `operator` are default peers of each other. The hop, thread,
  size and rate limits apply to every pair, and the operator can narrow `peers` per address. Mail from any peer grants
  nothing.

## Session side

Everything on the workstation is stdlib Python 3 with no packages, installed by `scripts/install-session-link.sh`
(PowerShell wrapper `install-session-link.ps1` on native Windows) into `~/.guardian/session/`.

| Platform | Daemon | Socket | Wake |
|---|---|---|---|
| macOS | LaunchAgent | Unix socket per session | tmux |
| WSL | systemd user unit (WSL must have systemd on; if not, the installer reports and stops) | Unix socket per session | tmux |
| native Windows (SD7) | scheduled task at logon (a Windows service only if S0w shows the task is not enough) | named pipe per session (`multiprocessing.connection`, `AF_PIPE`) | none, queue only |

### Session MCP tools

| Tool | Answered by | What it does |
|---|---|---|
| `session_link` | hub | Take an address for this session (name), or report the current one |
| `session_inbox` | daemon (spool) | Unread mail, oldest first; records it as retrieved in this session |
| `session_send` | hub | One message to one address |
| `session_list` | hub | Addresses this session may write to, with state and last-seen time |
| `session_status` | daemon | Link state, address, unread count, whether remote approval (this session) and the summary mirror are on |

The MCP is added once per user (`claude mcp add --scope user`), so every interactive session gets it without a project
file. It opens no network socket: its only peer is the daemon's socket for this session.

### Hooks

| Hook | Use |
|---|---|
| `SessionStart` | Register the CLI's session id with the daemon; get a per-session socket; record whether the session runs inside tmux (`$TMUX`, pane id); state `linked` (or unlinked until `session_link`) |
| `UserPromptSubmit` | If mail is waiting, add one line of context: "N new messages; read them with session_inbox" (never the content) |
| `Stop` | If mail arrived during the turn and the session is not marked `away`, block the stop once with the fixed reason "new mail: call session_inbox"; never twice without a `session_inbox` call in between (the same guard as the resident `Stop` continuation). With the mirror on, start the turn summary (SD6a) |
| `PermissionRequest` | With remote approval on for this session (SD5, SD11): post the request to the hub and wait for the operator's answer up to 120 s; on timeout or error, fall through to the normal local prompt |
| `SessionEnd` | Release the address; mail sent to it afterwards stays `queued` on the hub until it expires or the name links again |

Bypass-permissions mode does not switch the hook off (S0 part 4, claude 2.1.329). A bypass session skips the prompt for
ordinary tool calls, but a matching `permissions.ask` rule (project or flag settings) still raises it, and the person can
cycle out of bypass during the session (shift+tab: auto, manual, accept edits, plan). In both cases `PermissionRequest`
fires and the hook's answer is honoured. The input's `permission_mode` (`bypassPermissions`, `default`, ...) tells the
cases apart; it is recorded with the request for the operator's information only: SD11's `GUARDIAN_REMOTE_APPROVAL=1`
decides, whatever the mode. An operator who runs bypass sessions can keep chosen commands (say `git push`) behind an ask
rule and answer them remotely. Auto mode (a classifier answers the prompts) appeared in the mode cycle but was not tested.
The daemon and the installer never write `skipDangerousModePermissionPrompt` and never start a CLI with bypass flags: the
person picks the mode.

### Wake (SD3, SD3a)

An idle session with nobody at the keyboard has no hook that fires. The operator allowed a tmux wake for that case,
limited to mail from `operator` (SD3a). The daemon types the wake into the session's tmux pane only when **all** hold:

1. the mail that triggers it comes from `operator` (sent from the dashboard with `AGENT_TOKEN`); mail from sessions and
   resident agents is queued and surfaced by the hooks, never by a wake;
2. the session is `idle` (linked, no turn for 10 minutes) and not `away`;
3. the session's pane was recorded at `SessionStart` and still runs the same CLI process;
4. the tmux client has shown no input for the last 60 s (`client_activity`), so no person is typing;
5. the pane's input line is empty (checked with `tmux capture-pane` against the CLI's empty-prompt pattern found in S0);
6. no wake was typed into this session in the last 10 minutes without a `session_inbox` call since.

The wake is the fixed line "new mail from operator: call session_inbox", sent with `tmux send-keys -l` and Enter. It never
contains any part of the mail. A session outside tmux, and every session on native Windows, is queue only: the mail
waits for the next prompt or turn end, and the dashboard shows it as unread.

### States

| State | Meaning |
|---|---|
| `linked` | stream up, session bound, no turn running |
| `busy` | a turn is running (between `UserPromptSubmit` and `Stop`) |
| `idle` | linked, no turn for 10 minutes |
| `away` | the person marked it away (`session_link(away=true)`): mail is accepted and queued, the `Stop` continuation and the wake are off |
| `offline` | stream down (link-protocol heartbeat failed for 90 s, workstation asleep or off the tailnet), or the CLI exited without `SessionEnd` |

The dashboard also shows per session whether it can be woken (inside tmux, not native Windows).

### Summaries (SD6, SD6a)

The hub keeps short summaries of turns so the operator can follow a session from the dashboard; the transcript itself
never leaves the workstation.

- **Written by a model (SD6a).** At `Stop`, the hook starts the summary as a background child so the turn ends at once.
  The summary is written by the **same provider the session runs on** (for claude, `claude --print` with a small model and
  no tools), so no new party sees the turn. Input: the user prompt and the final assistant text of the turn, never tool
  calls or tool output.
- **Bounded.** At most one summary per session per 10 minutes (turns in between are folded into the next one), output
  capped at 2 KiB, and a summary call that fails or passes 60 s is dropped, not retried.
- **Filtered before it leaves.** The daemon drops lines that look like secrets (the `is_secret` patterns and key/token
  shapes) before sending; the hub checks again and refuses a summary that still matches.
- **Kept 7 days** on the hub, then deleted; not part of the nightly backup.
- **Quota.** Each summary spends the session provider's quota; the mirror is on per workstation and can be turned off in
  Settings. The exact model, interval and cap are checked in S0 and set in S5.

## Authority and limits

- **Mail grants nothing**, from a session or from an agent. It cannot approve an action, a plan or a permission, or pass
  on consent. The session MCP's tool descriptions and the onboarding line say this. Default peers (SD4) do not change it.
- **A remote approval is the operator's**, given in the dashboard with `AGENT_TOKEN`. It is never given by mail, by
  another session or by an agent, and it answers only the one `PermissionRequest` it was asked for (request id,
  session, tool, a hash of the arguments, deadline). The hub records who answered. A timeout or an error never allows.
  The hub can ask at all only in a session the person started with `GUARDIAN_REMOTE_APPROVAL=1` (SD11).
- **A wake is the operator's** (SD3a): only mail from `operator` can wake a session.
- **Limits** reuse the resident table (hop 3, 40 messages per thread, 32 KiB, 24 h lifetime) plus: 30 messages per
  session per hour sent, 10 remote approvals pending per session, one wake per session per 10 minutes, one summary per
  session per 10 minutes.
- **Kill switches:** `GUARDIAN_SESSIONS=off` on the hub makes the link entry refuse every stream; removing a workstation
  in Settings removes its line from `authorized_keys2` and ends its stream at once; on the workstation
  `install-session-link.sh --disable` stops the daemon and deletes the local key.

## Dashboard

A Sessions page under Nodes:

- List of workstations and their sessions: address, state, unread, last seen, wakeable, remote approval on/off.
- Per session: inbox and thread view, a box to send a message (mail from the operator may wake the session), pending
  permission requests with Allow / Deny and the remaining time, the summaries of the last 7 days.
- Settings -> Workstations: register a workstation (id, tailnet node, the public key the installer printed; the page
  shows the hub's host key fingerprint for the installer to pin, the hub's tailnet IPv4 and, when the operator set one,
  the link name, SD12), remove it, turn remote approval and the summary
  mirror on or off per workstation (both on at registration; remote approval on a workstation only allows sessions
  there to opt in, SD11). Registration refuses a key or an id already in use, and
  a tailnet node without an address. The line's `from=` takes the node's tailnet address at registration (SD9); when
  the node is registered again under a new address, the page shows the mismatch and rewrites the line on the operator's
  click (a link from the new address is refused until then).
- Every new string goes into the `ko` and `en` catalogs.

## Decisions

Made by the operator 2026-11-20. "Differs" marks where the answer is not the recommendation of 2026-11-17.

| # | Question | Decision | Differs |
|---|---|---|---|
| SD1 | Who opens the stream | The workstation, over the tailnet, to the link entry on the hub; each workstation joins the tailnet as a tagged node and runs no SSH server; the hub reaches a workstation only when the operator grants it, with a separate expiring key (rule 3) | no (revised 2026-11-20; first answer the same day: the hub dials) |
| SD2 | Workstation credential | One key per workstation, made on it; on the hub bound to the link entry and the workstation id with `command=` + `restrict`; the hub's host key pinned on the workstation; no `AGENT_TOKEN` there | no (revised 2026-11-20; first answer: one hub key in every workstation) |
| SD3 | Remote prompt into an idle session | tmux wake allowed, under the guard in "Wake"; no tmux or native Windows: queue only | yes (was: queue only) |
| SD3a | Which mail may wake a session | Only mail from `operator` | — (asked 2026-11-20 after SD3 and SD4) |
| SD4 | Sessions and resident agents in one address space | Yes, and default peers of each other; limits unchanged | yes (was: no default peers between the kinds) |
| SD5 | Remote approval of permission prompts | Built; on when a workstation is registered, off in Settings; 120 s deadline, timeout falls back to the local prompt, never auto-allow; narrowed by SD11 | default yes (was: opt-in) |
| SD6 | Transcript mirror to the hub | Summaries only, never tool output, kept 7 days; S5 is in scope | yes (was: off) |
| SD6a | Who writes the summary | A model, the session's own provider, at most one per 10 minutes, 2 KiB, secret filter | — (recommended: last assistant text, no model) |
| SD7 | Windows | WSL and native Windows | yes (was: WSL only) |
| SD8 | Relation to the vendor's Remote Control | Keep both; this design is for API-key CLIs and for messages that must arrive; use the vendor feature for the phone UI | no |
| SD9 | Bind each link key to an address | Yes: every link line carries `from="<workstation tailnet address>"`, taken from the tailnet node at registration and rewritten from Settings when the node gets a new address; the hub's sshd also listens on its public address, which the headscale ACL does not cover | — (proposed after S0 part 3, decided 2026-11-20) |
| SD10 | Who the link entry runs as, and where the link lines live | The hub user; the lines live in `~/.ssh/authorized_keys2`, which Guardian owns whole (written from the registry, temporary file and rename, mode 600); the operator's `authorized_keys` is never touched; no sshd change, since the hub's sshd already reads `authorized_keys2`; registration checks that this still holds and refuses if the file exists without Guardian's header | no (recommended; open point 3, decided 2026-11-20) |
| SD12 | Which hub address the link daemon dials | The hub's tailnet IPv4 first, fixed at install from Settings (`--hub-address`); after a connection failure, once, the hub's link name (`--hub-name`): an A record on the operator's own domain (Cloudflare, DNS-only, not proxied) to the same tailnet IPv4, created and kept by the operator, never by Guardian; `HostKeyAlias <hub id>` on both paths, a host key mismatch ends the dial; never the public address, which SD9's `from=` refuses | partly (recommended: tailnet IPv4 alone; the operator added the DNS name, decided 2026-11-20) |
| SD11 | Who turns remote approval on | The person at the workstation, per session: the CLI started with `GUARDIAN_REMOTE_APPROVAL=1` (the hook reads its own environment, so neither mail nor the session's model can turn it on); the session MCP can only turn it off; the workstation switch of SD5 stays as the ceiling | no (recommended; open point 2, decided 2026-11-20; narrows SD5) |

SD1 and SD2 were revised by the operator on 2026-11-20, after S0 part 2. A hub that dials every workstation holds the
control and needs an SSH server on each one. The workstation keeps the control instead: it opens the link and issues
its requests to the hub, and grants the hub SSH only when it is needed. The direction does not change what the hub can
do inside the protocol while a session is linked (remote approval of SD5, the wake of SD3). SD11 narrows the first: a
compromised hub reaches the permission prompts only of sessions the person started with remote approval.

## Phases

| Phase | Content | Exit condition |
|---|---|---|
| S0 probe | On one macOS and one WSL workstation joined to the tailnet: link dial from the workstation to the hub with a per-workstation key and one `command=`,`restrict` line on the hub (no shell, no PTY, no forwarding, `SSH_ORIGINAL_COMMAND` refused, greeting checked, under the hub's own sshd and login shell); user-scope MCP in an interactive session; `UserPromptSubmit` added context; `Stop` block-once guard; `PermissionRequest` hook wait and fall-through, and when bypass mode fires it (done in part 4); `SessionStart` session id and tmux pane; tmux wake guard (idle, `client_activity`, empty input line by `capture-pane`, the CLI's empty-prompt pattern); summary call (model, time, size); behaviour across sleep and resume (dropped 2026-11-20 by the operator: S1 and S2 test a stopped or killed stream instead) | a table in `docs/analysis/` per CLI version, like M0 |
| S0w probe | On one native Windows workstation: the built-in OpenSSH client run from Python (`ssh.exe -T` over pipes), the key file permissions it demands, the hub's pinned host key, named pipes from stdlib, hooks and MCP under the Windows CLI (Python via the `py` launcher or PowerShell), scheduled task across logon and sleep | a table in `docs/analysis/` |
| S1 hub | Address kind `session` in `mail_store.py`, `session_registry.py`, workstation registry (id, tailnet node, public key), link entry `guardian.py --session-link` (forced command, greeting, one stream per workstation), writing the hub's `authorized_keys2` whole from the registry, each line with `from=` (SD9, SD10), and the check that sshd still reads it, `--sessions` CLI, workstation tag in mesh join | unit tests with a fake stream; refusal of an unregistered key, a non-empty `SSH_ORIGINAL_COMMAND`, a terminal on stdin and a reused name; a new stream for a workstation ends the old one only after the handshake |
| S2 workstation | `session_link.py` daemon (dials the hub's tailnet IPv4, then its link name (SD12), reconnects, pins the hub's host key), session MCP, hooks, tmux wake, installer (key generation) for LaunchAgent and systemd user unit | two real sessions on two workstations exchange mail through the hub; a stopped or killed stream (standing in for sleep) is found by the heartbeat on both ends and redialled, and the mail queued meanwhile arrives; an operator mail wakes an idle tmux session and a peer mail does not |
| S2w Windows | named-pipe transport, scheduled task, PowerShell installer | a native Windows session exchanges mail with a macOS session |
| S3 dashboard | Sessions page, Workstations settings | send, read and state shown in a real browser |
| S4 remote approval | `PermissionRequest` path, hub request table, Allow/Deny with deadline | a real prompt answered from the dashboard; timeout falls back locally |
| S5 summaries | summary call in the `Stop` hook, filter, hub table with 7-day expiry, dashboard view | summaries of a real session shown; a planted secret line never reaches the hub |

S0w and S2w run after S0 and S2 so that macOS and WSL are not held up.

On hold since 2026-11-23 (DP5 of [verification-layer.md](verification-layer.md)): S3, S4 and S5 wait after the S2
acceptance test. The mail-store part (a session joins under a name, mail grants nothing, keys held by the hub) stays;
remote-control UX for interactive sessions is not Guardian's job.

## Tests

- Store: session addresses, name collision, default peers between kinds, narrowed peers, limits, expiry.
- Link: the workstation id comes from the hub's `authorized_keys2` line, never a field from the stream; an unregistered
  key, a non-empty `SSH_ORIGINAL_COMMAND` and a terminal on stdin are refused; the daemon refuses a changed hub host
  key; a new stream for a workstation ends the old one only after the greeting and the reply, and a dial that stops
  before them leaves the old stream alone; a dropped stream resends queued mail once (operation ids); removing a
  workstation drops only its own line; every written line carries `from=` with the node's tailnet address, and a new
  address rewrites only that workstation's line; the file is replaced by rename, never edited in place; the operator's
  `authorized_keys` is byte-identical before and after; registration refuses when an `authorized_keys2` without
  Guardian's header exists or sshd no longer reads the file; the daemon tries the stored IPv4 first and the link name
  only after a connection failure, never after a host key mismatch, with the same `HostKeyAlias` entry on both paths;
  a link name that points at the hub's public address is refused by `from=` (SD9, SD12).
- Hooks: `Stop` never blocks twice in a row; `UserPromptSubmit` never includes message content; `PermissionRequest`
  timeout returns "ask locally"; without `GUARDIAN_REMOTE_APPROVAL=1` in the CLI's environment the hook never posts a
  request, whatever the workstation switch or a mail says.
- Wake: only `operator` mail wakes; the typed line is the fixed text and contains no mail content; no wake while busy,
  away, with recent client input, with a non-empty input line, or twice within 10 minutes without `session_inbox`;
  sessions outside tmux and on native Windows are never woken.
- Summaries: input never includes tool calls or output; a secret-looking line is dropped by the daemon and refused by
  the hub; one per 10 minutes; expiry after 7 days.
- Approval: an answer for another request id, a changed argument hash or an expired deadline is refused.
- No real host or account names in fixtures (`ws-mac-1`, `ws-wsl-1`, `ws-win-1`, `site-a-arm1`).

## Risks

- **Peer mail and autonomous sessions.** With default peers (SD4), an agent with open tools can write to a session that
  runs in bypass mode. Mail grants nothing, but in bypass mode only the model's own judgment stands between a mail and
  the shell. Mitigations: mail is surfaced only at the person's own prompt or turn end, a wake is limited to `operator`
  mail (SD3a), the wake line never carries content, the operator can narrow `peers`, and ask rules keep chosen commands
  behind a prompt even in bypass mode, answered locally or remotely (S0 part 4; `deny` rules were not tested).
- **A wake can still collide with a person** who starts typing between the check and the keystroke. The 60 s idle
  window and the empty-line check make it rare; the typed line is harmless on its own.
- **Idle sessions outside tmux and on native Windows do not hear mail** until a person or a turn wakes them.
- **Hook behaviour depends on the CLI version.** The installer records the version S0 passed; the daemon warns when the
  CLI version changes.
- **The link entry runs as the hub user.** sshd starts `--session-link` with the rights of the account that holds
  `AGENT_TOKEN`, the fleet key and production data. `restrict` and the forced command keep the client out of a shell,
  so the entry's own input handling is the attack surface: it reads only the line protocol, never runs anything the
  client sends, and loads only the session link code. A dedicated user was weighed and not chosen (SD10): it needs a new account, an
  sshd change and a split of the mail store's permissions, while the forced command and `restrict` already bound it.
- **A Guardian bug in the key file** can break only the link lines: Guardian writes `authorized_keys2` alone and never
  the operator's `authorized_keys`, so it cannot lock the operator out of the hub (SD10).
- **A stolen workstation key** can read and send mail as that workstation's sessions, but cannot get a shell on the
  hub, reach the fleet or another workstation, or answer approvals (`AGENT_TOKEN`). Removing its line on the hub
  revokes it. No workstation runs an SSH server for this design, so the earlier risk of one on the operator's machine
  is gone.
- **The hub's sshd also listens on its public address.** The headscale ACL limits only the tailnet path; every link
  line therefore carries `from=` with the workstation's tailnet address (SD9), so a stolen link key works only from
  that address. An authenticated `-N` connection opens no session channel, so the link entry never sees it; a stolen key can hold such
  idle connections up to sshd's limits (low risk).
- **The link name is public DNS for a tailnet address (SD12).** It reveals the hub's tailnet IPv4 and a label, so the
  record gets a neutral label, not the hub's alias; the address cannot be reached outside the tailnet. Many home
  routers and resolvers drop public answers that point into private or CGNAT ranges (DNS rebinding protection, for
  example dnsmasq `stop-dns-rebind` or Pi-hole), so the name can fail quietly on some networks; that is why the stored
  IPv4 comes first. A name pointed at another host fails the pinned host key; a name pointed at the hub's public
  address fails `from=` (SD9).
- **Remote approval is a remote control of a person's machine.** It reaches only sessions the person started with
  `GUARDIAN_REMOTE_APPROVAL=1` (SD11), can be turned off per workstation, never auto-allows, and every answer is
  audited.
- **Summaries leave the workstation.** They are model text about the turn and can quote code or names; the filter
  catches secret shapes, not every sensitive line. Kept 7 days, off per workstation.
- **Quota.** Mail between sessions and agents, and every summary, spend model quota; the limits above bound it.

## References

- Vendor feature notes (what Remote Control and cross-session messaging do, with links, checked 2026-11-17):
  [../analysis/2026-11-17-remote-control-reference/notes.md](../analysis/2026-11-17-remote-control-reference/notes.md)
- [resident-agents.md](resident-agents.md): mail store, envelope, delivery, limits, `Stop` continuation (reused)
- [resident-open-tools.md](resident-open-tools.md): sandbox and manual-approval rules for agents with tools
- [remote-agents.md](remote-agents.md): worker protocol and the "hub opens every connection" rule, which holds for fleet
  nodes and resident agents but not for workstations (SD1 revised)
- [mesh-join.md](mesh-join.md): tailnet join used for workstations

## Open points

1. **Task board for sessions.** Not offered to sessions in S1-S3; decide after S2 whether a session may claim tasks.

Open points 2 (per-session remote approval) and 3 (who the link entry runs as) were decided 2026-11-20 as SD11 and SD10.

## 구현 메모

2026-11-20: SD1-SD8, SD3a and SD6a decided, SD9-SD11 after S0 part 3, SD12 (hub address: tailnet IPv4, then a
DNS-only name the operator added to the recommendation) the same day; SD1/SD2 first reversed the original direction (the
relay was to dial the workstations, as for resident nodes). After S0 part 2 the operator revised them the same day: the
workstation opens the link to the forced-command entry on the hub (`guardian.py --session-link`) of the first draft,
with one key per workstation; the hub keeps no session key and workstations run no SSH server.

2026-11-20, S0 part 1 (local probe on `ws-mac-1`, claude 2.1.329, tmux 3.8;
`../analysis/2026-11-20-s0-session-probe/results.md`). Hooks, session MCP binding, the `Stop` guard, both tmux wake
guards and the summary call work. Corrections the later phases must take in:

- `PermissionRequest` races the local dialog instead of waiting before it: the dialog is up at once, a hook answer
  settles it, a local answer sends the hook SIGTERM. SD5's intent (nothing allowed by a timeout or an error) stands;
  its wording "falls back to the local prompt" means "the local prompt stays". The installed hook needs `timeout`
  about 130 s and must withdraw the hub request on SIGTERM. The default mode was auto, where harmless calls raise no
  `PermissionRequest`.
- An Esc interrupt fires no `Stop`; `busy` ends at the next `UserPromptSubmit` or `SessionEnd`, or after a turn age cap.
- Empty input line: the last `❯` line not on the history background, `❯` + U+00A0 followed only by dim runs; tied to
  the CLI version, fail closed.
- `client_activity`: read from `list-clients` (max); `send-keys` does not move it. No client attached passing guard 4
  is a proposal, not decided.
- `--print` children fire the hooks (`CLAUDE_CODE_ENTRYPOINT=sdk-cli`): hooks and session MCP act unless the
  entrypoint is `sdk-cli` or `GUARDIAN_SESSION_CHILD=1`; the summary child runs from an empty directory under
  `~/.guardian/session/`.
- The session MCP must answer `server/discover` (sent before `initialize`) with "method not found"; in manual mode its
  read-only tools need allow rules (installer, operator confirms) and the `PermissionRequest` hook skips them.

2026-11-20, S0 part 2 (hub dial with a throwaway key and one `command=`,`restrict` line on `ws-mac-1`, both removed
afterwards; same results file). The forced command always runs; no terminal, agent, X11 or forwarding gets through;
the pinned host key holds. Corrections, now for the hub's link entry (the probe ran the other way round, with the Mac
as server; they are OpenSSH behaviour, and part 3 confirmed them on the hub's own sshd and login shell):

- A client command and a subsystem (sftp) both land on the forced command in `SSH_ORIGINAL_COMMAND`: the link entry
  exits without output when it is set and not empty.
- sshd runs the forced command through the login shell, whose startup file sets `PATH` (Homebrew's `bin`, and with it
  tmux, was missing) and could print: absolute paths in the `command=` line and in the entry, the entry's own `LANG`
  and `PATH`, and a fixed versioned greeting the daemon checks before anything else.
- A refused `-tt` still starts the forced command: the daemon dials with `-T`, and the entry refuses a terminal on
  stdin.
- Under the revised SD1 the daemon reaches the person's tmux server locally, so the S2 question of reaching it from a
  forced command is gone.

2026-11-20, S0 part 3 (workstation dial to one temporary `command=`,`restrict` line in the hub's `authorized_keys`,
with the operator's OK; line and key removed afterwards, the file byte-identical to its backup; same results file).
The hub's login shell prints nothing before the greeting, `PATH` is the system default without the user's directories,
and every refusal of part 2 holds on the hub. Changes:

- A refused `-tt` starts the forced command on the hub too, and it lives only until its first write. The rule "a new
  stream ends the old one" now waits for the completed handshake (Architecture, rule 2).
- The hub's sshd sends no keepalive (`ClientAliveInterval 0`): the 90 s heartbeat of "States" is a link-protocol
  heartbeat both ways; the daemon also sets `ServerAliveInterval`. Read from the settings; sleep not run (dropped, see below).
- sshd also listens on the public address: risk added, `from=` proposed and verified in the probe; the operator
  decided it the same day as SD9.
- `AuthorizedKeysFile` includes `authorized_keys2`: chosen by the operator the same day as SD10 (Guardian owns that
  file, the operator's `authorized_keys` stays untouched). The same day the operator decided SD11: remote approval only
  in sessions the person started with `GUARDIAN_REMOTE_APPROVAL=1`, so the earlier `session_link(remote_approval=true)`
  idea is dropped (a tool the model can call could be talked into it by mail).

- S0 part 4 (bypass mode, 2026-11-20): the statement that bypass mode never fires `PermissionRequest` was wrong
  (correction 11). "Hooks" now says when it fires (ask rules, leaving bypass), that the hook's `allow` and `deny` are
  honoured there, and that `permission_mode` is information, not the gate. A flag-settings file can pre-consent to bypass
  (`skipDangerousModePermissionPrompt`), so Guardian never writes that key.

S0 is not done: WSL and S0w are open. Sleep and resume was dropped by the operator 2026-11-20: it concerns only the
workstation, and a sleeping workstation has nobody to read mail anyway; what matters is that the link comes back by
itself after the wake. A sleep looks to both ends like a stream that goes silent without a FIN, so S1 tests the hub
side (the entry ends a stream silent past the heartbeat limit, mail stays `queued`) and S2 the daemon side (a stopped
or killed `ssh` child is found by the heartbeat and redialled) with simulated streams; a real sleep is watched in use. No workstation needs an SSH server any
more, so the operator may turn the Mac's Remote Login off again.

2026-11-21, S1 (hub), built; on the hub since the deploy of `8e2179d` the same day (a descendant of the S1 commit
`3a23abe`), sshd check below:

- Store (`mail_sessions.py` on `mail_store.py`, schema 5): tables `links` (one row per workstation: lease, state, reason)
  and `sessions`. Every session write checks the link lease inside the same `_write()` transaction, so a write from a
  stream that lost the link is refused as `superseded` and changes nothing. `GUARDIAN_SESSIONS=off` refuses every call
  (`sessions_off`). A session's mail hop is 1 + the highest hop of the mail it answers (cap `HOP_MAX` 3), 30 sends per
  hour per session, `idle` derived after 10 minutes without a state, sessions of a link that is down released after 10
  minutes (the sweep returns without a write lock when nothing is due). No `peers_changed` notice is sent for sessions:
  resident agents learn of a session from the mail it sends, not from a roster push (open for S2 if the session MCP
  needs a roster).
- Registry (`session_registry.py`): `<data_dir>/session-workstations.json` (id, tailnet IPv4 in `100.64.0.0/10`,
  `ssh-ed25519` key without comment, optional node name), ids may not be `operator`, `hub` or an inventory node (an
  unreadable inventory refuses). `authorized_keys2` is written whole from it with a Guardian header; a file without
  that header is refused, `authorized_keys` is never written. The `command=` keeps the unresolved `~/.guardian/current`
  path so a new release needs no rewrite. `GUARDIAN_SESSION_KEYS_FILE` moves the file (tests). The sshd check tries
  `sshd -T`, then `sudo -n sshd -T`, and says when it could not run.
- CLI: `--session-add WORKSTATION --address IPV4 --key KEY [--node NAME]`, `--session-address`, `--session-remove`,
  `--session-list`, and `--session-link --workstation ID` for the forced command. The phase table says `--sessions`; that
  name is taken by the debate sessions option, so the list is `--session-list`.
- Link entry (`session_link.py`, `--session-link --workstation ID`): refuses with exit 2 and no output when
  `SSH_ORIGINAL_COMMAND` is set, with one stderr line on a terminal stdin, `GUARDIAN_SESSIONS=off` or an unknown
  workstation. A forced command gets no systemd `EnvironmentFile`, so it reads `~/.guardian/agent.env` itself, copies
  only `GUARDIAN_*` keys (so `AGENT_TOKEN` stays out; a key already set is never overwritten) and then pins `LANG=C.UTF-8` and
  `PATH=/usr/bin:/bin`. Handshake 20 s (hub greeting, daemon `hello`; `handshake_timeout` or `protocol_mismatch`
  ends it before any claim, so a failed dial never cuts a live link), then `link_claim` and `welcome` with the lease.
  Poll 2 s (registry removal, kill switch, `link_current`, queued mail out as `deliver`, offline release), ping
  every 30 s, liveness 90 s without any frame (`liveness`, mail stays `queued`), a hub request waits 20 s for the
  daemon's reply (an unanswered `deliver` stays queued and leaves again on the next poll). Writes go through a writer thread with a 256-frame queue; a full queue ends
  the stream (`writer_backlog`), the `bye` gets 5 s of grace. A stream that lost the lease answers `superseded` and
  ends without `link_end`, so the newer link stays up. Deviations from the task spec: the kill switch at entry is read
  from the merged environment (the same value `mail_store.sessions_off()` reads in production); an exception in a
  frame handler answers `internal_error` and a store exception `store_error`, both keep the stream open; an unparsable
  line or a non-object frame ends the stream (`bad_json`, `bad_frame`); one poll runs right after the `welcome` so mail
  queued while the link was down leaves at once.
- Mesh join: `tag:workstation` is usable only when the operator lists it in `GUARDIAN_HEADSCALE_TAGS`, and only alone
  (with any other tag the join is refused as a bad `tags` field); the answer then carries `workstation: true` and a `next`
  line with the `--session-add` command for the joined IPv4.
- Hub sshd check (2026-11-21, with the operator's OK, on the deployed release): `--session-add` of one example
  workstation (throwaway key, the Mac's tailnet IPv4) wrote `authorized_keys2` with one line, mode 600, and
  `--session-list` reported the line ok and that `sshd -T` reads `authorized_keys2`. From the Mac, with
  `ssh -F /dev/null -o IdentitiesOnly=yes`, no agent and the pinned host key:
  - registered key to the hub's tailnet IPv4, `-T`: the greeting frame with the example id. Stdin was `/dev/null`, so
    EOF ended the stream before any `hello` (`bye stream_end`, lease none); the production store was not written;
  - unregistered key: `Permission denied (publickey)`;
  - registered key with a command (`sftp`, `id`): exit 2, no output;
  - registered key with `-tt`: `PTY allocation request failed` (`restrict`), the client gave up with exit 255 and no
    output;
  - registered key to the hub's public address: `Permission denied`; sshd logged "correct key but not from a permitted
    host (... required=<the tailnet IPv4>)", so `from=` (SD9) holds;
  - registered key with `-W 127.0.0.1:9870`: `administratively prohibited`.

  `--session-remove` then left a header-only `authorized_keys2` and `[]` in the registry; both files were deleted, as
  before the check, and `--session-list` says no workstation is registered. The operator's `authorized_keys` kept the
  same sha256. Pitfall: in zsh, options kept in a plain variable reach `ssh` as one argument; use an array or bash.

2026-11-21, S2 (workstation), built, not installed: six workstation modules in `cli/llm-agent/`, standard library only
(the test of `session_local.py` guards their imports): `session_local.py` (the local contract: framing, request
validators, reply shapes, paths, config, fixed texts; mirrored link constants pinned against the hub modules),
`session_uplink.py` (dialer, backoff, spool, hub stream), `session_daemon.py` (the link daemon), `session_mcp.py` (five
tools), `session_hooks.py` (four hooks), `session_wake.py` (tmux wake guards); installer
`scripts/install-session-link.sh` with a LaunchAgent and a systemd user unit template in `deploy/session/`. Hub side: a
`list` frame on the link (the bound session's peers with kind, state and last-seen time; the operator is always listed;
an address with no row stays `unknown`). Nothing is installed on a workstation and nothing on the hub changed besides
the `list` frame (not deployed). Deviations and choices:

- The daemon is `session_daemon.py`, not `session_link.py`: that name is the hub's link entry.
- Sockets: one control socket for the hook ops only (`register`, `prompt_submit`, `stop`, `end`) and one socket per
  registered session for the MCP ops only (`link`, `inbox`, `send`, `list`, `status`); the daemon knows the session from
  the socket a frame arrived on, and a frame that carries an identity field (`cli_session`, `from`, `address`, `sender`)
  is refused as `bad_request`. Hooks never wait on the hub (bind, state and unbind go out in the background); MCP calls
  wait up to 20 s.
- A name that is not a valid session name: the hook drops it (`name` null, the session starts unnamed) before it reaches
  the daemon; a frame that still carries one is refused as `bad_request`, not answered with a warning as the work order
  had it.
- `register` carries `tmux_socket` (the contract's first draft called it `tmux`) next to the pane, so the wake reaches
  the right tmux server.
- The session table is kept in `run/sessions.json` (mode 600, replaced atomically); on start, entries whose CLI pid is
  gone are dropped and the rest get their sockets back. A new lease marks the workstation's rows offline on the hub, so
  after every reconnect the daemon binds every named session again and resends `busy`.
- Hooks take the CLI's pid from `CLAUDE_PID` when it is all digits, else from the hook's parent (S0 part 1: the CLI runs
  the hook as its child). The session MCP sends no identity field at all.
- An inbox ack that fails (link down) leaves the mail `delivered` on the hub; the spool dedupes by mail id, so the
  redelivery after a reconnect is harmless.
- Wake: guard 4 (no client input for 60 s) fails closed when no tmux client is attached; guard 3 accepts the CLI pid as
  the pane process or one of its ancestors up to 8 hops. Refusal reasons are logged; a refused wake is retried on the
  next tick while the mail is unread.
- The uplink's link-up callbacks run on the link thread, so the daemon re-binds in its own thread; deliveries never block
  the link thread (they only mark the session and signal the tick thread).
- ssh runs with `-F /dev/null`, `BatchMode`, `IdentitiesOnly`, the private `known_hosts` with `HostKeyAlias`, no global
  known hosts, `ServerAliveInterval=30` and `ServerAliveCountMax=3`, without `SSH_AUTH_SOCK`.
- Installer: the host key is pinned by `ssh-keyscan` of the tailnet IPv4 compared with the fingerprint the operator
  copies from Settings; only an exact match writes `known_hosts`, a mismatch writes nothing (exit 4). The installer
  prints, and never applies, the public key line, the `claude mcp add` command, the hooks block for
  `~/.claude/settings.json` and a suggested `permissions.allow` list for the three read-only tools (`session_inbox`,
  `session_list`, `session_status`); it never touches `~/.ssh` or `~/.claude/settings.json`. `--disable` is the
  workstation kill switch: it removes the service and the key and keeps config, `known_hosts` and spool.
- Tests: an end-to-end test (`test_session_e2e.py`) runs two real daemons, their uplinks and the hub's `LinkSession` and
  mail store in a scratch data dir; ssh, the CLI sessions, tmux and the clock are simulated (its docstring says exactly
  which). Pitfall: one `MailStore` keeps one sqlite connection usable only in the thread that opened it, so every
  simulated stream gets its own store.
- Built by z.ai in slices (contract and hub `list` frame, hooks, MCP, wake, uplink, daemon, then installer
  and end-to-end test); the last slice came back untested because the z.ai worktree could not run it, so the
  end-to-end test was fixed by hand: the redial gate is a `threading.Event` (`clear`/`set`), an inbox ack leaves
  the mail `acked` on the hub (not `delivered`), a killed stream closes the write ends first (closing a buffered
  reader waits on the lock a blocked `read()` holds), and mail queued while a link was down leaves at the first poll
  after the re-bind, not at the link-up poll (the new lease has just marked the sessions offline). The installer
  test's `launchctl bootout` target was corrected to `gui/<uid>/<label>`.
- First real install on the Mac 2026-11-21 (workstation registered on the hub, the hub's `authorized_keys2` created
  with that one line, link `up` with lease 1): the home directory sits on an external APFS volume with ownership
  turned off (`diskutil info` shows `Owners: Disabled`), and launchd refuses every LaunchAgent plist there
  (`launchctl bootstrap` error 5, the same plist bootstraps from the boot volume). The install was redone with
  `--no-service` and the daemon runs by hand. The installer now checks the home volume in step 1 (launchd only,
  also under `--dry-run`) and stops with exit 3 before writing anything, naming `--no-service` or enabling
  ownership on that volume by the operator (`sudo diskutil enableOwnership <mount>`, never run by the installer); an
  unreadable volume only warns, and a failed bootstrap in step 9 adds the same hint.
- Hub deploy and Mac setup 2026-11-22: deployed `a6aa7e9` to the hub (carrying the `list` frame and the installer
  ownership check; all three resident agents restored `ready`). On the Mac, registered the `guardian-session` user MCP
  (`claude mcp get guardian-session`: Connected), merged the four S2 hooks (`SessionStart`, `UserPromptSubmit`, `Stop`,
  `SessionEnd`, 5 s timeout) and the three read-only `permissions.allow` entries (`session_inbox`, `session_list`,
  `session_status`) into `~/.claude/settings.json`, and started `session_daemon.py` (link `up`, lease 2 on the hub).
- Antigravity (`agy`) adapter 2026-11-22: Antigravity runs one stdio MCP child per process (without
  `CLAUDE_CODE_SESSION_ID`), passes camelCase `conversationId` to hooks under `~/.gemini/config/hooks.json`, uses
  `PreInvocation` (`invocationNum == 0`, `injectSteps`) and `Stop` (`continuationActive`, `decision: "continue"`), and
  has no `SessionEnd` event. Added `--client agy` to `session_mcp.py` (keys the session by `agy-<ppid>`, lazily
  registers on the control socket when the session socket is missing), `agy-session-start` / `agy-pre-invocation` /
  `agy-stop` to `session_hooks.py` (always emits JSON `{}` on no-op or failure; skips re-registering when the socket
  already exists so `/new` keeps the bound address), and dead-PID reaping in `session_daemon.py._tick_once` so exited
  `agy` processes close their sockets and unbind on the hub. Wired into `~/.gemini/config/mcp_config.json`,
  `~/.gemini/antigravity-ide/mcp_config.json`, and `~/.gemini/config/hooks.json` on the Mac, and verified a live `agy`
  run registering `agy-<pid>` with the running daemon.
- Not done: the WSL install and the real two-workstation acceptance test of the S2 exit condition (two workstations
  over the tailnet, a real killed and silent stream).

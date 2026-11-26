# S0 parts 1-4: interactive session probe, hub dial, hub-side link entry and bypass mode on one macOS workstation (2026-11-20)

English | [한국어](results.ko.md)

> Point-in-time record from 2026-11-20 for claude 2.1.329 and tmux 3.8 on macOS; hook inputs, environment names and
> the composer's look change with CLI releases.

Design: `../../design/operator-sessions.md` (phase S0; sections "Hooks", "Wake (SD3, SD3a)", "Summaries (SD6, SD6a)",
"Authority and limits"). **Part 1** covers everything that runs on the workstation alone; **part 2** ("Part 2: hub dial") is the hub's SSH dial
with a dedicated key restricted to a forced command; **part 3** ("Part 3: hub-side link entry") is the revised direction,
the workstation dialing a forced-command line on the hub; **part 4** ("Part 4: bypass mode") is how bypass-permissions
mode treats the `PermissionRequest` hook. Sleep and resume and the WSL and native Windows runs are still open (see "Not
done").

## How it ran

On `ws-mac-1` (the operator's Mac), by hand, in a throwaway directory. Each probe session ran in a private tmux server
(`tmux -L s0probe -f /dev/null`) through a launcher that starts the CLI with `env -i` (only `HOME`, `USER`, `PATH`,
`TERM`, `LANG`, `TMUX`, `TMUX_PANE` and a marker kept) and these flags:

```
claude --model haiku --setting-sources project --settings <probe>/settings.json \
       --mcp-config <probe>/mcp.json --strict-mcp-config [--permission-mode default]
```

User settings were left out on purpose: the operator's own settings carry hooks for ten events, which would have mixed
with the probe hooks. `settings.json` registered one stdlib Python hook script for `SessionStart`, `UserPromptSubmit`,
`Stop`, `PermissionRequest` (matcher `*`) and `SessionEnd`; the script logged its stdin, its parent pid and the
environment (values only for an allow list, names only for other `CLAUDE*`/`ANTHROPIC*` variables) and read a control
file that told it what to answer. `mcp.json` registered a stdio stub server with one tool, `session_inbox`, that returns a
fixed marker. Panes were driven with `tmux send-keys` and read with `tmux capture-pane`. No Guardian code ran, the hub was
not contacted, and the operator's user settings and user-scope MCP were not touched. Session ids and paths below are
placeholders.

## Results

| Check (design item) | Result |
|---|---|
| `SessionStart`: session id, tmux pane | **pass.** Input has `session_id`, `source` (`startup`), `model`. The hook's environment has `TMUX`, `TMUX_PANE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CODE_ENTRYPOINT`; the hook's parent pid is the CLI's pid |
| User-scope MCP in an interactive session | **pass** with `--mcp-config` standing in for `claude mcp add --scope user`; see "MCP" for two findings |
| `UserPromptSubmit` added context | **pass.** `additionalContext` reached the model (it repeated a planted word). Input also has `prompt`, `prompt_id`, `permission_mode` |
| `Stop` block-once guard | **pass.** `{"decision":"block","reason":...}` continued the turn; the second `Stop` arrives with `stop_hook_active: true`. **But** an Esc interrupt fires no `Stop` (correction 2) |
| `PermissionRequest` wait and fall-through | **works, by a different mechanism** (correction 1): the local dialog and the hook run at the same time; whichever answers first wins |
| Bypass mode never fires `PermissionRequest` | **fails as stated** (part 4, correction 11): ask rules and leaving bypass fire it, and the hook's answer is honoured |
| tmux wake guard: `client_activity` | **pass**, with a gap for "no client" (correction 4) |
| tmux wake guard: empty input line | **pass**, with a precise pattern (correction 3) |
| Summary call (model, time, size) | **pass.** `claude --print --model haiku --tools "" --setting-sources project --strict-mcp-config --no-session-persistence < /dev/null` on a 1.2 KB prompt+answer input: 2.6 s, 2.5 s, 2.7 s; 448-475 bytes of output, no secrets or paths copied. Well under the design's 60 s and 2 KiB. Child-session gap: correction 5 |
| `SessionEnd` | **pass.** `/exit` fires it with `reason: prompt_input_exit` |
| Sleep and resume | not run (needs the operator) |
| Hub dial with `command=`,`restrict` | **pass**, with three design corrections (part 2, corrections 6-8) |
| Workstation dial to a `command=`,`restrict` line on the hub | **pass**, with two design corrections and one proposal (part 3, corrections 9-10, `from=`, adopted as SD9) |

## Design corrections

### 1. Remote approval races the local prompt; it does not wait first

With `PermissionRequest` registered and the session in manual mode, the CLI draws its local permission dialog **at
once** and runs the hook in parallel:

- a hook that answers `allow` (or `deny`) while the dialog is open resolves it; the dialog closes with "Allowed by
  PermissionRequest hook" (seen at 3 s and at 8 s);
- if the person answers locally first, or presses Esc, the CLI sends the still-running hook **SIGTERM**; the hook can
  catch it and withdraw its hub request;
- a hook that outlives its configured `timeout` is killed without a signal the hook sees in time, and the dialog simply
  stays.

So the design's "wait up to 120 s, then fall through to the local prompt" is really "the local prompt is up from the
start; a remote answer within 120 s settles it; otherwise the local prompt is still there". SD5's intent is unchanged:
nothing is ever allowed by a timeout or an error. Two consequences for S4:

- the installed hook entry needs `timeout` above the 120 s wait (about 130 s), or the CLI kills the hook first (a 30 s
  `timeout` killed the probe hook silently);
- the hook must handle SIGTERM by withdrawing the hub request, so the dashboard does not show a stale Allow/Deny.

Also: the CLI started in **auto mode** by default even with user settings excluded, and in auto mode a harmless Bash
call raised no `PermissionRequest` at all. Remote approval therefore matters mainly in manual and accept-edits modes;
Shift+Tab cycles manual → accept edits → plan → auto.

### 2. An Esc interrupt fires no `Stop`

The design defines `busy` as the span between `UserPromptSubmit` and `Stop`. An interrupted turn produces no `Stop`, so
the session would stay `busy` forever, never become `idle` and never be woken. The daemon needs a fallback: the next
`UserPromptSubmit` or `SessionEnd` ends any open turn, and a turn older than a cap (for example 30 minutes without hook
traffic) is treated as ended.

### 3. The empty input line has a precise pattern

At 160 and at 100 columns, an empty input line is the prompt character followed by a **no-break space**: bytes
`ESC[39m` `❯` `U+00A0` (`302 240`). Two traps:

- the CLI often shows a **prompt suggestion** as ghost text on that line (dim, SGR 2, for example `❯ ls -l <file>`),
  which is not typed input; typed text is not dim;
- earlier submitted prompts in the history also start with `❯`, drawn on a grey background (`48;5;237`), so a search
  for `❯` matches history too.

Rule for S2: capture with `tmux capture-pane -e -p`, take the **last** line that starts with `❯` and is not on the
history background, and call it empty only if it is `❯` + U+00A0 followed by nothing but dim (SGR 2) runs or spaces.
Anything else, including a pattern that is not found, means no wake (fail closed). The pattern is tied to this CLI
version and must be re-checked when the pinned version moves.

### 4. `client_activity`: what moves it, and the case with no client

- Attaching a client sets `client_activity` to the attach time.
- A key typed **through the client** moves it (checked by driving a client through a pty: +8 s after one key).
- `tmux send-keys` does **not** move it, so the daemon's own wake line does not look like a person typing.
- With no client attached, `client_activity` is empty and `session_attached` is 0.

Read it with `tmux list-clients -t <session> -F '#{client_activity}'` and take the **largest** value; `display -p`
reports a single client. The design does not say what guard 4 does without a client. Proposal (not decided): no client
attached passes guard 4, because nobody can be typing; guard 5 (empty input line) still applies.

### 5. Summary calls and other child CLIs fire the hooks too

A `claude --print` child that sees the hooks (here through `--settings`, which stands in for the user-scope install the
design plans) fired `SessionStart`, `UserPromptSubmit`, `Stop` and `SessionEnd`, with `CLAUDE_CODE_ENTRYPOINT=sdk-cli`.
With a user-scope install, every summary call, every `scripts/zai-agent.sh` run and every other scripted CLI would
register as a session, and a summary started from `Stop` could start another summary.

Entrypoint values seen: `cli` (interactive terminal), `sdk-cli` (`--print`), `claude-desktop` (the desktop app's Code
tab). IDE values are not verified. `CLAUDE_CODE_CHILD_SESSION=1` was set even in the top-level interactive session, so
it does not mark children.

Rule for S2 (inverse of an allow list, so desktop and IDE sessions are not shut out): the hooks and the session MCP act
**unless** `CLAUDE_CODE_ENTRYPOINT` is `sdk-cli` or a marker such as `GUARDIAN_SESSION_CHILD=1` is set. The summary
child sets the marker and runs from an empty working directory under `~/.guardian/session/`, because
`--setting-sources project` still loads the project hooks of whatever directory it starts in.

## MCP

- The MCP child's environment carries `CLAUDE_CODE_SESSION_ID` and `TMUX_PANE`, and its parent pid is the CLI's pid, so
  the session MCP can bind to the same session as the hooks without a handshake through the daemon.
- The first method the CLI sent was `server/discover`, **before** `initialize`. The real session MCP must answer unknown
  methods with JSON-RPC "method not found" and keep running.
- In auto mode the MCP tool ran without a prompt. In **manual mode** calling `session_inbox` raised the local permission
  dialog **and** a `PermissionRequest` for `mcp__s0stub__session_inbox`. Without an allow rule, every `Stop` block and
  every wake would pop a dialog, and remote approval would forward the session's own tool to the hub. S2 needs:
  - allow rules for the read-only session tools (`session_inbox`, `session_status`, `session_list`) written by the
    installer, shown to and confirmed by the operator, since it changes user settings;
  - the `PermissionRequest` hook ignores the session MCP's own tools.

## Other observations

- The hook environment also names `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_ENV_FILE`,
  `CLAUDE_CODE_CHILD_NOTES_DIR` and a messaging socket and token pair (names only; values were not logged).
- `Stop` input has `last_assistant_message`, so the summary needs neither the transcript file nor its format.
  It also has `background_tasks`, `session_crons` and `effort`.
- `PermissionRequest` input has `tool_name`, `tool_input` and `permission_suggestions`; a hash of `tool_input` can bind
  the hub request to the one prompt (design "Authority and limits").
- When a client attaches, tmux resizes the window to the client (80x24 for the probe client), so capture widths change
  with the person's terminal; the rule in correction 3 does not depend on width.
- The probe sessions left one transcript directory under the operator's `~/.claude/projects/` (the throwaway
  directory's name). It holds only the probe prompts.

## Part 2: hub dial

### How it ran

With the operator's OK for one throwaway key and one `authorized_keys` line. On the hub, an ed25519 key in a directory
under `~/guardian-eval/` (mode 700) and a separate `known_hosts` that pinned `ws-mac-1`'s ed25519 host key (the
fingerprint read on the workstation matched `ssh-keyscan` from the hub). On `ws-mac-1` (OpenSSH 10.5p1, Remote Login),
one line tagged `guardian-s0-probe`:

```
command="/usr/bin/python3 <stub> serve",restrict ssh-ed25519 <key> guardian-s0-probe
```

The stub logged its argv, `SSH_ORIGINAL_COMMAND`, whether stdin and stdout were terminals, `SSH_TTY`, `SSH_AUTH_SOCK`,
`DISPLAY`, its cwd, uid and environment names, printed a `HELLO` line and echoed stdin line by line. The hub (OpenSSH
9.6p1) dialed over the tailnet with `-F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o
UserKnownHostsFile=<pinned> -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes`, the
options the relay is to use. Afterwards the line was removed (the file is byte-identical to its backup), a dial with
the old key was refused, and the key directory on the hub was deleted.

### Results

| Attempt from the hub | Result |
|---|---|
| no command, stdin piped | **pass.** The forced command runs with argv `serve`; lines stream both ways; no terminal, no `SSH_AUTH_SOCK`, no `DISPLAY`; cwd is the home directory; exit 0 |
| a client command (`ls /; id`) | **pass.** The forced command runs instead; the client's command only appears in `SSH_ORIGINAL_COMMAND` |
| `-tt` | **refused** ("PTY allocation request failed", client exit 255), **but** the forced command still started on the workstation, without a terminal (correction 8) |
| `-A -X` | **pass.** No agent socket, no `DISPLAY` |
| `-R` with `ExitOnForwardFailure` | **refused** (remote port forwarding failed), exit 255 |
| `-L` | **refused in effect.** The client listens locally, but every channel is "administratively prohibited"; no data passes |
| `-W` | **refused** ("stdio forwarding failed"), exit 255 |
| `sftp` | the subsystem request **lands on the forced command** with `SSH_ORIGINAL_COMMAND=/usr/libexec/sftp-server`; sftp failed only because it read the stub's greeting as a packet length (correction 6) |
| empty pinned `known_hosts` | **refused** ("Host key verification failed") |
| old key after the line was removed | **refused** (publickey) |

### Environment under sshd

sshd runs the forced command through the user's login shell, so the shell's startup file for non-interactive shells
(`~/.zshenv` on this Mac) runs on every dial: 19 environment names, among them `PATH`, `HOME`, `TMPDIR`, `SHELL` and
variables a developer tool exported there; no `LANG`. `PATH` held the user's own tool directories and
`/usr/bin:/bin:/usr/sbin:/sbin`, but **not** Homebrew's `bin`, so `tmux` is not found by name; `claude` was found (in a
user directory). The parent process is `sshd-session: <user>@notty`. The protected folders `~/Documents` and
`~/Desktop` and an external volume were listable from the dial; macOS ties this to Remote Login's full-disk-access
option, whose setting was not read. No default tmux server was running during the dial, so reaching the person's tmux
server from the forced command is not shown (same uid, so expected to work; S2 checks it).

### Design corrections

#### 6. `serve` refuses a non-empty `SSH_ORIGINAL_COMMAND`

`restrict` stops forwarding, the agent, X11 and the terminal, but not what the client asks to run: a command and a
subsystem (sftp) both land on the forced command, with the request in `SSH_ORIGINAL_COMMAND`. `serve` must exit at once
with no output when that variable is set and not empty, and log one line (fail closed). The relay never sends a command.

#### 7. The daemon brings its own environment; the protocol starts with a checked greeting

The login shell's startup file runs before `serve` and decides `PATH`. The `command=` line names the interpreter by
absolute path (as here), the daemon calls tmux and the CLI by absolute paths recorded at install, and it sets its own
`LANG` and `PATH` instead of inheriting them. A startup file that prints anything would put bytes ahead of the
protocol, so `serve` opens with a fixed, versioned greeting and the relay drops the link if the first line is anything
else.

#### 8. A refused PTY does not stop the forced command

With `-tt` the client gave up, but `serve` had already started on the workstation. The relay requests no terminal
(`-T`), and `serve` refuses to run when stdin is a terminal, so a terminal request cannot reach the daemon in any form.

## Part 3: hub-side link entry

### How it ran

After SD1/SD2 were revised (the workstation opens the link), with the operator's OK for one throwaway key on
`ws-mac-1` and one temporary line in the hub's `~/.ssh/authorized_keys`. A probe stub took the place of
`guardian.py --session-link` in a directory under `~/guardian-eval/` (mode 700); the line, tagged
`guardian-session:ws-mac-1`, was:

```
from="<workstation tailnet address>",command="/usr/bin/python3 <stub> --session-link --workstation ws-mac-1",restrict ssh-ed25519 <key> guardian-session:ws-mac-1
```

The stub logged its argv, `SSH_ORIGINAL_COMMAND`, whether stdin and stdout were terminals, its parent process, cwd, uid
and environment names, refused when `SSH_ORIGINAL_COMMAND` was set or stdin was a terminal, printed a versioned greeting
line and echoed stdin. `ws-mac-1` (OpenSSH 10.5p1) dialed `site-a-arm1` (OpenSSH 9.6p1) over the tailnet with
`-T -F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o UserKnownHostsFile=<pinned> -o
GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes`; the pinned file held the hub's ed25519
host key. Afterwards the line was removed (the file is byte-identical to its backup, mode 600), a dial with the old key
was refused, the stub directory on the hub and the key on the workstation were deleted, and no stub process was left.

### Results

| Attempt from the workstation | Result |
|---|---|
| `-T`, no command | **pass.** The greeting is the first line (nothing from the login shell before it); argv carries `--session-link --workstation ws-mac-1`; lines echo both ways; exit 0 |
| a client command (`id; ls /`) | **refused** by the stub: the command arrived in `SSH_ORIGINAL_COMMAND`; exit 1, no output |
| `-tt` | **refused** ("PTY allocation request failed", exit 255), **but** the stub still started on the hub without a terminal. Its parent was already `systemd` (the sshd side had gone) and it ended writing the greeting into the closed channel: no end line in its log, no process left (correction 9) |
| `-A -X` | **pass.** No agent socket, no `DISPLAY` (the hub's sshd has `X11Forwarding yes`; `restrict` wins) |
| `-R` with `ExitOnForwardFailure` | **refused** (remote port forwarding failed), exit 255 |
| `-L` | **refused in effect.** Every channel is "administratively prohibited" |
| `-W` | **refused** ("stdio forwarding failed"), exit 255 |
| `sftp` | the subsystem lands on the forced command with `SSH_ORIGINAL_COMMAND=/usr/lib/openssh/sftp-server`; **refused** by the stub |
| dial to the hub's public address | **refused** (publickey): the line's `from=` binds the key to the workstation's tailnet address (proposal below) |
| empty pinned `known_hosts` | **refused** ("Host key verification failed") |
| `-N` | the authenticated connection stays open, but no session channel opens, so the forced command never runs and the stub logged nothing |
| idle 100 s with `ServerAliveInterval=30` | **pass.** The stream survived; the next line echoed |
| old key after the line was removed | **refused** (publickey) |

### Environment under the hub's sshd

The hub user's login shell is bash; its `.bashrc` returns early for non-interactive shells, so nothing came before the
greeting. The forced command saw 16 environment names, with `LANG` set (pam_env) and `PATH` the system default
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin`), without the
user's own `~/.local/bin`. Its parent is `sshd`, its cwd the home directory. `SSH_CONNECTION` shows the workstation's
tailnet address. PAM and logind open a session for every link (`XDG_SESSION_ID`, `XDG_RUNTIME_DIR`,
`DBUS_SESSION_BUS_ADDRESS` are set). The relevant `sshd -T` values: `ClientAliveInterval 0`, `MaxSessions 10`,
`MaxStartups 10:30:100`, `AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2`, `PermitUserEnvironment no`,
`PasswordAuthentication no`, `AllowTcpForwarding yes`, `PermitTTY yes`, and sshd listens on `0.0.0.0:22` and `[::]:22`,
so on the public address as well as the tailnet. Corrections 6-8 hold on the hub as written: the early return of
`.bashrc` depends on a file the user can edit, so the greeting check stays, and correction 7's absolute paths are
needed because `PATH` lacks the user's directories.

### Design corrections

#### 9. The link entry touches nothing until the handshake is complete

A dial that fails on the client side (here a refused PTY) can still start the forced command on the hub, and such a
process may live only until its first write. The design's rule "a new stream for a workstation ends the old one" must
therefore apply only after the greeting and the workstation's reply have both passed: before that the entry neither
touches the store nor ends an existing stream, so a dial that never completes cannot cut a healthy link. The entry
exits quietly on a broken pipe.

#### 10. The heartbeat belongs to the link protocol

The hub's sshd sends no keepalive (`ClientAliveInterval 0`), and Guardian does not change sshd. The idle stream above
lasted only because the client sent `ServerAliveInterval`. A workstation that sleeps or loses its network without a
FIN would leave the entry blocked on read until TCP keepalive gives up, which takes hours. So "heartbeat failed for 90 s"
in "States" is an application heartbeat inside the link protocol, both ways: the daemon also sets `ServerAliveInterval`,
and the entry ends a stream that has been silent past the limit. This is read from the settings; sleep was not run.

### Proposal: bind each key to its tailnet address with `from=` (adopted as SD9)

The design assumes only tailnet peers reach the hub's SSH port, but sshd listens on the public address too, and the
headscale ACL does not cover that path. Without `from=`, a stolen link key works from anywhere the public port is
reachable. With `from="<workstation tailnet address>"` the dial to the public address was refused. Registration already
reads the workstation's tailnet node, so the address is at hand; if the node is registered again under a new address,
Settings must rewrite the line. The operator adopted it the same day as decision SD9 of the design.

### Notes

- `-N` connections are invisible to the link entry; a stolen key could hold idle connections up to sshd's limits. Low
  risk, nothing to change now.
- Every link opens a logind session for the hub user; relevant to the open point on a dedicated Unix user.
- sshd already reads `~/.ssh/authorized_keys2`. Guardian could own that file whole (atomic rename) and leave the
  operator's `authorized_keys` alone, with no sshd change. It is a distribution default that could change, so
  registration would have to check it (`sshd -T` needs sudo, or a test dial). An option for open point 3, not a
  decision.

## Part 4: bypass mode

### How it ran

Same Mac, launcher and hook script as part 1 (claude 2.1.329, Haiku), in a fresh throwaway directory, started with
`--permission-mode bypassPermissions`. User settings stayed excluded, so the operator's stored consent to bypass mode
did not apply. The consent dialog was not accepted either: the probe's flag-settings file (`--settings`) carried
`skipDangerousModePermissionPrompt: true`, which the CLI honours from flag settings, so no dialog appeared and no
consent was written anywhere. Three sessions:

1. bypass, no rules: plain Bash, a Write into `.claude/` and into `.git/`, one MCP tool call; then one
   `permissions.ask` rule in the flag settings, with the hook only logging and the local dialog cancelled (Esc);
2. bypass, then shift+tab out of bypass during the session;
3. bypass with two ask rules in the project's own `.claude/settings.json` (where operators keep them) and the hook
   answering `deny`, then `allow`.

The operator's `~/.claude/settings.json` was unchanged (hash before and after). As in part 1, the CLI recorded folder
trust for the throwaway directory in `~/.claude.json` and kept transcripts under `~/.claude/projects/`; both were left
for the operator, not edited by hand.

### Results

| Check | Result |
|---|---|
| Ordinary tool calls in bypass | no prompt, no `PermissionRequest`: plain Bash `touch`, Write into `.claude/` and into `.git/`, an MCP tool call (the stub returned its marker) |
| `permissions.ask` rule in bypass (flag settings) | **prompts.** The dialog names the rule and its source ("from command line arguments"); `PermissionRequest` fires with `permission_mode: "bypassPermissions"`, `tool_name`, `tool_input` |
| Same kind of rule in the project's `.claude/settings.json`, hook answers `deny` | command refused with the hook's message ("Denied by PermissionRequest hook"), no dialog left open, no file |
| Same, hook answers `allow` | command ran, file created. The hook answered at once, so it won the race of correction 1 |
| Leaving bypass mid-session | shift+tab cycles bypass, auto, manual, accept edits, plan, bypass. In manual mode a plain `touch` prompted and `PermissionRequest` fired with `permission_mode: "default"`; the dialog offered "Yes, and switch to auto mode" |
| Hook inputs | `UserPromptSubmit`, `Stop` and `PermissionRequest` carry `permission_mode` |
| `SessionEnd` | fired for every session on `/exit` |

### Design correction

#### 11. Bypass mode does not switch remote approval off

The design said a bypass session never raises a prompt, so `PermissionRequest` never fires there and remote approval
does not apply. Both halves are wrong in 2.1.329: an ask rule prompts in bypass mode, and the person can leave bypass at
any time; in both cases the hook fires and its answer decides. The hook therefore assumes nothing from the mode: SD11's
opt-in (`GUARDIAN_REMOTE_APPROVAL=1`) decides, and `permission_mode` is recorded with the request for the operator's
information. Ask rules become a mitigation for bypass sessions (risk "Peer mail and autonomous sessions"): an operator
can keep chosen commands behind a prompt and answer it remotely. Not tested: auto mode, and `deny` rules in bypass.

### Note

A flag-settings file can pre-consent to bypass mode (`skipDangerousModePermissionPrompt`). Guardian's daemon and
installer never write that key and never start a CLI with bypass flags; the person picks the mode.

## Not done

| Item | Needs |
|---|---|
| Reaching the person's tmux server from the forced command | a tmux session running during a dial (S2) |
| Auto mode, and `deny` rules in bypass mode | a probe session, if remote approval is to cover them |
| Sleep and resume of the workstation (and the hub-side end of a silent link, correction 10) | dropped 2026-11-20 by the operator: S1 and S2 test a silent or killed stream instead |
| WSL workstation | a WSL host on the tailnet with tmux and the CLIs (no SSH server needed since SD1/SD2 were revised) |
| S0w (native Windows) | after S0 |

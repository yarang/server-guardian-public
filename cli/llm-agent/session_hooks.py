"""The CLI hooks of operator sessions (S2 of docs/design/operator-sessions.md, "Hooks").

Installed on an operator workstation as ~/.guardian/session/session_hooks.py and wired into the
CLI's settings by the installer, one entry per event:

    python3 ~/.guardian/session/session_hooks.py <event>

with <event> one of session-start, prompt-submit, stop or session-end (the CLI's SessionStart,
UserPromptSubmit, Stop and SessionEnd), and the CLI's hook input JSON on stdin. PermissionRequest
and turn summaries are later phases (S4, SD6a) and are not handled here.

Fail open, always ("Authority and limits"): a hook must never break or hang the person's CLI, so
every failure -- daemon down, timeout, bad input, bad reply, unknown event -- exits 0 with no
stdout, every daemon call waits at most session_local.HOOK_CLIENT_TIMEOUT, and stderr stays empty
unless GUARDIAN_SESSION_DEBUG=1 asks for one short line. Children the CLI spawns for --print runs
and summaries fire these hooks too (S0 correction 5), so session_local.skip_session is checked
before anything else and they cost no daemon call.

What each event does (design "Hooks"; S0 results table):

- session-start sends register: the daemon binds the CLI session id, records the tmux socket and
  pane for a later wake, and the session turns linked. No stdout.
- prompt-submit sends prompt_submit: the daemon marks the turn busy ("States"). When unread mail
  is waiting, the hook adds exactly one context line, session_local.unread_context(n) -- never a
  sender, subject or body (rule 8).
- stop sends stop: the daemon ends the turn. When it answers block=true the hook relays the fixed
  session_local.STOP_REASON; the daemon owns the block-once state, and the CLI's own loop guard
  (stop_hook_active on the second stop) is honoured by ignoring the block answer. An Esc
  interrupt fires no Stop at all (S0 correction 2); the daemon's fallback covers that.
- session-end sends end with the CLI's reason: the daemon releases the address. No stdout.

All socket paths, framing and validators come from session_local, the one local contract, so the
hooks and the daemon cannot drift. This module imports only the standard library and
session_local, because nothing else exists on a workstation (test_session_local.py asserts it).
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, TextIO, Union

import session_local

# $TMUX_PANE is a tmux pane id like %3 (ASCII digits only, like session_local's check); CLAUDE_PID
# is the CLI's pid as decimal digits. Anything else falls back to the safer source.
_TMUX_PANE_RE = re.compile(r"^%\d+$", re.ASCII)
_DIGITS_RE = re.compile(r"^[0-9]+$", re.ASCII)


def _debug_line(stderr: TextIO, env: Mapping[str, str], exc: BaseException) -> None:
    """One short stderr line, and only when GUARDIAN_SESSION_DEBUG=1 asks for it; even a broken
    stderr must never break the hook."""
    if env.get("GUARDIAN_SESSION_DEBUG") != "1":
        return
    text = " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
    try:
        stderr.write(f"session_hooks: {text}\n")
    except Exception:
        pass


def _read_input(stdin: TextIO) -> Dict[str, Any]:
    """The CLI's hook input: one JSON object on stdin. Any deviation raises, and main fails open."""
    data = json.loads(stdin.read())
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    return data


def _cli_session(data: Mapping[str, Any], env: Mapping[str, str]) -> Optional[str]:
    """The CLI session id: the input's session_id when it is a non-empty string, else
    CLAUDE_CODE_SESSION_ID from the environment; None means this run has nothing to say."""
    for value in (data.get("session_id"), env.get("CLAUDE_CODE_SESSION_ID")):
        if isinstance(value, str) and value:
            return value
    return None


def _cli_pid(env: Mapping[str, str]) -> int:
    """The CLI's pid: $CLAUDE_PID when it is all digits, else the hook's parent (S0 part 1: the
    CLI runs the hook as its child, so getppid is the CLI)."""
    value = env.get("CLAUDE_PID")
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value):
        return int(value)
    return os.getppid()


def _tmux_socket(env: Mapping[str, str]) -> Optional[str]:
    """The tmux server socket: the first comma-separated field of $TMUX when it is an absolute
    path, else None. $TMUX is "socket,pid,session" inside tmux and unset outside it."""
    tmux = env.get("TMUX")
    if not isinstance(tmux, str) or not tmux:
        return None
    field = tmux.split(",")[0]
    return field if field.startswith("/") else None


def _tmux_pane(env: Mapping[str, str], socket_path: Optional[str]) -> Optional[str]:
    """The tmux pane id $TMUX_PANE (like %3), and only when a tmux socket was found: with no
    socket there is no pane to wake, so both fields are sent as null."""
    if socket_path is None:
        return None
    pane = env.get("TMUX_PANE")
    if isinstance(pane, str) and _TMUX_PANE_RE.fullmatch(pane):
        return pane
    return None


def _optional_text(value: Any) -> Optional[str]:
    """An optional text field of register as the contract wants it: a non-empty string or null,
    never an empty string or a non-string value."""
    return value if isinstance(value, str) and value else None


def _register_frame(data: Mapping[str, Any], env: Mapping[str, str],
                    cli_session: str) -> Dict[str, Any]:
    """The register request of session-start: everything the daemon needs to bind the session,
    find its tmux socket and pane for a wake, and name it. Keys follow session_local._OP_FIELDS."""
    socket_path = _tmux_socket(env)
    name = env.get("GUARDIAN_SESSION_NAME")
    source = data.get("source")
    return {
        "op": "register",
        "cli_session": cli_session,
        "cli_pid": _cli_pid(env),
        "tmux_socket": socket_path,
        "tmux_pane": _tmux_pane(env, socket_path),
        "project_dir": _optional_text(env.get("CLAUDE_PROJECT_DIR")),
        "source": source if isinstance(source, str) and source else "unknown",
        "name": name if session_local.valid_session_name(name) else None,
    }


def _call_daemon(home: Union[str, Path], frame: Dict[str, Any]) -> Dict[str, Any]:
    """One round trip to the daemon's control socket over a fresh connection, bounded by
    HOOK_CLIENT_TIMEOUT. OSError (daemon down, timeout, broken peer) and ValueError (bad reply)
    propagate to main, which fails open."""
    paths = session_local.SessionPaths(Path(home))
    return session_local.call(paths.control_socket, frame,
                              timeout=session_local.HOOK_CLIENT_TIMEOUT)


def _emit(stdout: TextIO, payload: Dict[str, Any]) -> None:
    """One JSON line on stdout: the only shape a hook may ever answer the CLI with."""
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def _on_session_start(data: Mapping[str, Any], env: Mapping[str, str], cli_session: str,
                      home: Path, stdout: TextIO) -> None:
    _call_daemon(home, _register_frame(data, env, cli_session))
    # No stdout: SessionStart has nothing to add to the session.


def _on_prompt_submit(data: Mapping[str, Any], env: Mapping[str, str], cli_session: str,
                      home: Path, stdout: TextIO) -> None:
    reply = _call_daemon(home, {"op": "prompt_submit", "cli_session": cli_session})
    unread = reply.get("unread") if reply.get("ok") else None
    if isinstance(unread, int) and not isinstance(unread, bool) and unread > 0:
        # Only the count line from session_local: no sender, subject or body ever (rule 8).
        _emit(stdout, {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": session_local.unread_context(unread)}})


def _on_stop(data: Mapping[str, Any], env: Mapping[str, str], cli_session: str,
             home: Path, stdout: TextIO) -> None:
    reply = _call_daemon(home, {"op": "stop", "cli_session": cli_session})
    if data.get("stop_hook_active"):
        return  # the CLI's own loop guard: the turn still ends, but never blocks a second time
    if reply.get("ok") and reply.get("block") is True:
        # The daemon owns the block-once state; the hook only relays its answer.
        _emit(stdout, {"decision": "block", "reason": session_local.STOP_REASON})


def _on_session_end(data: Mapping[str, Any], env: Mapping[str, str], cli_session: str,
                    home: Path, stdout: TextIO) -> None:
    reason = data.get("reason")
    _call_daemon(home, {"op": "end", "cli_session": cli_session,
                        "reason": reason if isinstance(reason, str) and reason else "unknown"})
    # No stdout: SessionEnd has nothing to add to the session.


def _agy_pid(env: Mapping[str, str]) -> int:
    """The Antigravity process pid: $GUARDIAN_AGY_PID when all digits (tests), else getppid()
    (hooks invoked via `exec python3 ...` are direct children of the agy process)."""
    value = env.get("GUARDIAN_AGY_PID")
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value):
        return int(value)
    return os.getppid()


def _agy_project_dir(data: Mapping[str, Any]) -> Optional[str]:
    cwd = _optional_text(data.get("cwd"))
    if cwd is not None:
        return cwd
    paths = data.get("workspacePaths")
    if isinstance(paths, list):
        for item in paths:
            found = _optional_text(item)
            if found is not None:
                return found
    return None


def _agy_register_frame(data: Mapping[str, Any], env: Mapping[str, str],
                        cli_session: str, pid: int) -> Dict[str, Any]:
    socket_path = _tmux_socket(env)
    name = env.get("GUARDIAN_SESSION_NAME")
    return {
        "op": "register",
        "cli_session": cli_session,
        "cli_pid": pid,
        "tmux_socket": socket_path,
        "tmux_pane": _tmux_pane(env, socket_path),
        "project_dir": _agy_project_dir(data),
        "source": "agy",
        "name": name if session_local.valid_session_name(name) else None,
    }


def _on_agy_session_start(data: Mapping[str, Any], env: Mapping[str, str],
                          home: Path, stdout: TextIO) -> None:
    pid = _agy_pid(env)
    cli_session = f"agy-{pid}"
    # An agy process shares one MCP child across conversations; skip re-registering when its
    # session socket already exists so a new conversation never wipes an active link/name.
    if not session_local.SessionPaths(home).session_socket(cli_session).exists():
        _call_daemon(home, _agy_register_frame(data, env, cli_session, pid))
    _emit(stdout, {})


def _on_agy_pre_invocation(data: Mapping[str, Any], env: Mapping[str, str],
                           home: Path, stdout: TextIO) -> None:
    # PreInvocation fires before every model call in a turn; only act at the start of the turn.
    if data.get("invocationNum", 0) != 0:
        _emit(stdout, {})
        return
    pid = _agy_pid(env)
    cli_session = f"agy-{pid}"
    reply = _call_daemon(home, {"op": "prompt_submit", "cli_session": cli_session})
    if reply.get("ok") is not True and reply.get("error") == session_local.NOT_REGISTERED:
        _call_daemon(home, _agy_register_frame(data, env, cli_session, pid))
        reply = _call_daemon(home, {"op": "prompt_submit", "cli_session": cli_session})
    unread = reply.get("unread") if reply.get("ok") else None
    if isinstance(unread, int) and not isinstance(unread, bool) and unread > 0:
        _emit(stdout, {"injectSteps": [{"systemMessage": {
            "systemMessage": session_local.unread_context(unread)}}]})
        return
    _emit(stdout, {})


def _on_agy_stop(data: Mapping[str, Any], env: Mapping[str, str],
                 home: Path, stdout: TextIO) -> None:
    pid = _agy_pid(env)
    cli_session = f"agy-{pid}"
    reply = _call_daemon(home, {"op": "stop", "cli_session": cli_session})
    reason = data.get("terminationReason")
    if data.get("continuationActive") or reason not in (None, "", "NO_TOOL_CALL"):
        _emit(stdout, {})
        return
    if reply.get("ok") and reply.get("block") is True:
        _emit(stdout, {"decision": "continue", "reason": session_local.STOP_REASON})
        return
    _emit(stdout, {})


_EVENTS = {
    "session-start": _on_session_start,
    "prompt-submit": _on_prompt_submit,
    "stop": _on_stop,
    "session-end": _on_session_end,
}

_AGY_EVENTS = {
    "agy-session-start": _on_agy_session_start,
    "agy-pre-invocation": _on_agy_pre_invocation,
    "agy-stop": _on_agy_stop,
}


def _run(argv, stdin: TextIO, stdout: TextIO, env: Mapping[str, str], home: Path) -> int:
    event = argv[0] if argv else None
    agy_handler = _AGY_EVENTS.get(event)
    if agy_handler is not None:
        if session_local.skip_session(env):
            _emit(stdout, {})
            return 0
        data = _read_input(stdin)
        agy_handler(data, env, home, stdout)
        return 0
    handler = _EVENTS.get(event)
    if handler is None:
        return 0  # an event S2 does not wire (yet): nothing to do, nothing to say
    if session_local.skip_session(env):
        return 0  # a --print or summary child of the CLI: no daemon call at all (S0 correction 5)
    data = _read_input(stdin)
    cli_session = _cli_session(data, env)
    if cli_session is None:
        return 0
    handler(data, env, cli_session, home, stdout)
    return 0


def main(argv, stdin: TextIO, stdout: TextIO, env: Mapping[str, str],
         home: Union[str, Path], stderr: Optional[TextIO] = None) -> int:
    """Runs one hook event. argv is sys.argv[1:] (one event name), stdin the CLI's hook input,
    stdout the sink for the one-line answer, env the process environment, home the user's home
    directory (SessionPaths finds the control socket under it) and stderr the optional sink for
    the debug line. Always returns 0: a hook must never break or hang the person's CLI, so every
    error is swallowed here after at most one GUARDIAN_SESSION_DEBUG=1 line on stderr."""
    if stderr is None:
        stderr = sys.stderr
    try:
        return _run(argv, stdin, stdout, env, Path(home))
    except Exception as exc:  # fail open: daemon down, timeout, bad input, bad reply, anything
        _debug_line(stderr, env, exc)
        if argv and argv[0] in _AGY_EVENTS:
            try:
                _emit(stdout, {})
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout, os.environ, Path.home()))

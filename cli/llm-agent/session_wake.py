"""The tmux wake of an idle operator session (S2 of docs/design/operator-sessions.md, "Wake").

The daemon calls wake_policy first (guards 1, 2 and 6: pure state, no tmux), and only then
Waker.try_wake for the terminal side (guards 3, 4 and 5, then the send). Everything fails
closed: an unreadable answer, a dead pane, an unparseable line or a failed send all mean no
wake, because a wake must never race the person at the keyboard. The one line ever typed is
session_local.WAKE_LINE, fixed text with no mail content; try_wake takes no text argument.

Guard 5 follows the empty-input-line pattern the S0 probe pinned (correction 3,
docs/analysis/2026-11-20-s0-session-probe/results.md): an empty input line is the prompt
character followed by U+00A0, prompt suggestions are dim ghost text (SGR 2), and submitted
prompts in the history are drawn on a 48;5;237 background. The pattern is tied to the pinned
CLI version and must be re-checked when that version moves. Guard 4 takes the largest
client_activity of list-clients (correction 4); send-keys does not move it, so the daemon's
own wake never looks like typing.

Standard library and session_local only: this module is installed on operator workstations
(macOS and Linux, Python 3.9+), where no hub module exists (test_session_local.py asserts it).
"""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import session_local

# Guard 5's pattern pieces (S0 correction 3). U+00A0 is the no-break space right after the
# prompt character of an empty input line; the prompt character starts prompt lines, live and
# history ones alike, so the history background tells them apart.
PROMPT_CHAR = "❯"
NBSP = " "  # U+00A0 itself; test_session_wake pins it, so a mangled copy fails loudly

# Guard 3 walks the parent chain of the CLI pid at most this many hops before giving up.
PARENT_HOPS = 8
# Seconds, for every tmux and ps call: a stuck call must never hold the daemon's wake loop.
CALL_TIMEOUT = 5

_PANE_RE = re.compile(r"^%\d+$", re.ASCII)
_INT_RE = re.compile(r"[+-]?[0-9]+")


def _apply_sgr(params: str, dim: bool, bg_history: bool) -> Tuple[bool, bool]:
    """Applies one SGR parameter list (the text between ESC[ and m) to the dim and
    history-background flags. Combined parameters count: "0;2" resets then dims, "2;39" dims
    with a default foreground, "48;5;237" sets the history background."""
    tokens = params.split(";")
    idx = 0
    while idx < len(tokens):
        head, _, rest = tokens[idx].partition(":")
        if head == "":
            head = "0"  # ESC[m, a leading or a doubled ";" all mean reset
        if head == "0":
            dim = False
            bg_history = False
        elif head == "2":
            dim = True
        elif head == "22":
            dim = False
        elif head == "49":
            bg_history = False
        elif head == "48":
            consumed, bg_history = _apply_background(tokens, idx, rest)
            idx += consumed
        idx += 1
    return dim, bg_history


def _apply_background(tokens: List[str], idx: int, rest: str) -> Tuple[int, bool]:
    """One SGR 48 background parameter: returns the extra tokens the colour spec consumed
    (semicolon form) and whether the colour is the history background 48;5;237."""
    if rest:  # colon form, the whole colour spec inside one token: 48:5:237
        return 0, rest.split(":")[:2] == ["5", "237"]
    spec = tokens[idx + 1:idx + 4]
    if len(spec) >= 2 and spec[0] == "5":
        return 2, spec[1] == "237"
    if len(spec) >= 3 and spec[0] == "2":
        return 3, False  # a truecolour background is never the history background
    return 0, False  # truncated or malformed: some background, not provably the history one


def _skip_escape(text: str, i: int, dim: bool,
                 bg_history: bool) -> Tuple[int, bool, bool]:
    """Skips the escape sequence at i (text[i] is ESC), applying it when it is an SGR. Returns
    the index after the sequence; a sequence truncated by the end of the capture ends there."""
    size = len(text)
    if i + 1 >= size:
        return size, dim, bg_history
    kind = text[i + 1]
    if kind == "[":
        j = i + 2
        params_end = j
        while j < size and "0" <= text[j] <= "?":  # parameter bytes 0x30-0x3f
            j += 1
        params = text[params_end:j]
        while j < size and " " <= text[j] <= "/":  # intermediate bytes 0x20-0x2f
            j += 1
        if j < size:
            final = text[j]
            j += 1
            if final == "m":
                dim, bg_history = _apply_sgr(params, dim, bg_history)
        return j, dim, bg_history
    if kind == "]":  # OSC (a window title): skip to BEL or ST, never a visible cell
        j = i + 2
        while j < size:
            if text[j] == "\x07":
                return j + 1, dim, bg_history
            if text[j] == "\x1b" and j + 1 < size and text[j + 1] == "\\":
                return j + 2, dim, bg_history
            j += 1
        return j, dim, bg_history
    return i + 2, dim, bg_history  # a two-character escape (ESC 7, ESC M, ...)


def _render(capture: str) -> List[List[Tuple[str, bool, bool]]]:
    """Turns capture-pane -e -p output into grid lines of (character, dim, history-background)
    cells: escape sequences change the flags and produce no cell, every other character does.
    The flags carry across lines, as in the terminal stream tmux reconstructs."""
    lines: List[List[Tuple[str, bool, bool]]] = []
    cells: List[Tuple[str, bool, bool]] = []
    dim = False
    bg_history = False
    i = 0
    while i < len(capture):
        char = capture[i]
        if char == "\x1b":
            i, dim, bg_history = _skip_escape(capture, i, dim, bg_history)
        else:
            if char == "\n":
                lines.append(cells)
                cells = []
            elif char != "\r":  # a lone CR is not visible input
                cells.append((char, dim, bg_history))
            i += 1
    lines.append(cells)
    return lines


def pane_input_empty(capture: str) -> bool:
    """Guard 5: True when the pane's input line looks empty. The rule (S0 correction 3): the
    last line whose prompt character is NOT on the history background is empty only when the
    prompt character is followed by U+00A0 and then nothing but spaces and dim text (a prompt
    suggestion is dim ghost text; typed input is not). Anything else, and a capture with no
    usable prompt line, is not empty: fail closed, no wake."""
    found: Optional[Tuple[List[Tuple[str, bool, bool]], int]] = None
    for cells in _render(capture):
        for pos, (char, _dim, bg_history) in enumerate(cells):
            if char != PROMPT_CHAR:
                continue
            if not bg_history:
                found = (cells, pos)  # later prompt lines overwrite earlier ones: last wins
            break  # a line's first prompt character decides whether it is a prompt line
    if found is None:
        return False
    cells, pos = found
    tail = cells[pos + 1:]
    if not tail or tail[0][0] != NBSP:
        return False  # no U+00A0 right after the prompt character: not an empty input line
    return all(dim or char == " " for char, dim, _bg in tail[1:])


def clients_quiet(list_clients_output: str, now: float,
                  quiet: float = session_local.INPUT_QUIET,
                  allow_no_client: bool = False) -> bool:
    """Guard 4: True when no tmux client has shown input in the last `quiet` seconds. The input
    is `tmux list-clients -t <target> -F '#{client_activity}'` output, one epoch-seconds value
    per line; the largest value wins (S0 correction 4). Empty output means no client is
    attached: allow_no_client decides (the design leaves that case open, so the daemon passes
    the default False). Any unparseable line is not quiet: fail closed."""
    values: List[int] = []
    for line in list_clients_output.splitlines():
        text = line.strip()
        if not text:
            continue
        if _INT_RE.fullmatch(text) is None:
            return False
        values.append(int(text))
    if not values:
        return allow_no_client
    return now - max(values) >= quiet


def wake_policy(*, from_kind: str, state: str, away: bool, last_wake: Optional[float],
                inbox_since_wake: bool, now: float) -> Tuple[bool, str]:
    """Guards 1, 2 and 6 as one pure decision (no tmux, no clock reading): only operator mail
    wakes an idle, not-away session, and never twice within WAKE_INTERVAL unless the session
    called session_inbox in between. Returns (wake, reason); the reasons are checked in this
    order, so the daemon logs the first refusal that applies."""
    if from_kind != "operator":
        return False, "not_operator"
    if state != "idle":
        return False, "not_idle"
    if away:
        return False, "away"
    if (last_wake is not None and now - last_wake < session_local.WAKE_INTERVAL
            and not inbox_since_wake):
        return False, "recent_wake"
    return True, "ok"


@dataclass(frozen=True)
class WakeTarget:
    """Where to type the wake: the pane recorded at SessionStart, the tmux server socket from
    $TMUX, and the CLI pid registered with them. Nothing is trusted: every field is checked
    (_target_problem) before the first tmux call, because the values come from a register
    frame over the local socket."""

    pane: str
    tmux_socket: str
    cli_pid: int


def _target_problem(target: WakeTarget) -> Optional[str]:
    """Every problem with a wake target, None when it is usable. Mirrors the register checks of
    session_local, with the wake's stricter pid rule (a real process id over 1)."""
    if not isinstance(target.pane, str) or _PANE_RE.fullmatch(target.pane) is None:
        return f"pane must be a tmux pane id like %3, not {target.pane!r}"
    socket = target.tmux_socket
    if not isinstance(socket, str) or not socket.startswith("/") or "\x00" in socket:
        return "tmux_socket must be an absolute path"
    pid = target.cli_pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return f"cli_pid must be an int over 1, not {pid!r}"
    return None


def _tmux_env() -> Dict[str, str]:
    """PATH, HOME and LANG only, and LANG never empty: tmux and the pane text speak UTF-8, and
    a scrubbed environment keeps the wake independent of the daemon's own shell setup."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
    }


class Waker:
    """Runs guards 3, 4 and 5 and types the wake line, through one subprocess runner: every
    tmux call is a list argv behind `-S <socket>` (never a shell string), so a pane id or a
    socket path can never become shell syntax. The runner and clock are injectable, so the
    tests drive the whole guard chain without a real tmux or ps."""

    def __init__(self, tmux_path: str = "tmux", *,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 clock: Callable[[], float] = time.time, allow_no_client: bool = False):
        self.tmux_path = tmux_path
        self._run = run
        self._clock = clock
        self.allow_no_client = allow_no_client

    def _tmux(self, target: WakeTarget, args: List[str]) -> subprocess.CompletedProcess:
        return self._run([self.tmux_path, "-S", target.tmux_socket, *args],
                         timeout=CALL_TIMEOUT, capture_output=True, text=True, check=False,
                         env=_tmux_env())

    def _ps_parent(self, pid: int) -> Optional[int]:
        """The parent pid of one process, or None when ps failed or printed nothing usable."""
        result = self._run(["ps", "-o", "ppid=", "-p", str(pid)],
                           timeout=CALL_TIMEOUT, capture_output=True, text=True, check=False)
        text = (result.stdout or "").strip()
        if result.returncode != 0 or _INT_RE.fullmatch(text) is None:
            return None
        return int(text)

    def pane_runs_cli(self, target: WakeTarget) -> bool:
        """Guard 3: the recorded pane still exists (not dead) and still runs the recorded CLI
        process, as the pane process or as one of its ancestors up to PARENT_HOPS hops. Any
        unreadable answer is False: a pane that changed owner must never be typed into."""
        result = self._tmux(target, ["display-message", "-p", "-t", target.pane,
                                     "#{pane_dead} #{pane_pid}"])
        if result.returncode != 0:
            return False
        fields = (result.stdout or "").strip().split()
        if len(fields) != 2 or fields[0] != "0" or _INT_RE.fullmatch(fields[1]) is None:
            return False  # a dead pane answers 1; anything else is unreadable
        pane_pid = int(fields[1])
        pid = target.cli_pid
        if pid == pane_pid:
            return True
        for _hop in range(PARENT_HOPS):
            ppid = self._ps_parent(pid)
            if ppid is None:
                return False
            if ppid == pane_pid:
                return True
            if ppid <= 1:
                return False  # reached init without meeting the pane process
            pid = ppid
        return False

    def try_wake(self, target: WakeTarget) -> Tuple[bool, str]:
        """Guards 3, 4 and 5, then the wake itself; returns (woke, reason). The typed text is
        always session_local.WAKE_LINE and nothing else: this method takes no text argument.
        Enter is sent only when the line itself was sent, so a failed text send never submits
        whatever the pane happened to hold."""
        if _target_problem(target) is not None:
            return False, "bad_target"
        if not self.pane_runs_cli(target):
            return False, "pane_changed"
        clients = self._tmux(target, ["list-clients", "-t", target.pane,
                                      "-F", "#{client_activity}"])
        if clients.returncode != 0 or not clients_quiet(clients.stdout or "", self._clock(),
                                                        allow_no_client=self.allow_no_client):
            return False, "client_active"
        capture = self._tmux(target, ["capture-pane", "-e", "-p", "-t", target.pane])
        if capture.returncode != 0 or not pane_input_empty(capture.stdout or ""):
            return False, "input_not_empty"
        line = self._tmux(target, ["send-keys", "-t", target.pane, "-l",
                                   session_local.WAKE_LINE])
        if line.returncode != 0:
            return False, "send_failed"
        enter = self._tmux(target, ["send-keys", "-t", target.pane, "Enter"])
        if enter.returncode != 0:
            return False, "send_failed"
        return True, "woken"

"""The workstation-local contract of operator sessions (S2 of docs/design/operator-sessions.md).

This module is installed on an operator workstation next to the pieces S2 builds later: the link
daemon, the session MCP and the CLI hooks. It is the one place the local Unix-socket protocol
between those pieces is written down, so they cannot drift: the framing, the request validators,
the reply shapes, the socket paths, the config file and the fixed texts. It runs on macOS and
Linux with Python 3.9+ and imports only the standard library, because no hub module exists on a
workstation (test_session_local.py asserts both; its pin test also checks the mirrored constants
against session_link.py and mail_relay.py in the hub repository).

Shape of the local protocol (Architecture rules 5, 7 and 8): hooks and the session MCP are
clients, each request opens one connection, carries one NDJSON frame and reads one reply frame.
Hook requests go to the daemon's control socket and carry the CLI session id; MCP requests go to
this session's own socket, and the daemon stamps the session from the socket it read the request
on, so an MCP request that claims an identity (cli_session, from, address, sender) is refused
(rule 5). Calls are synchronous and short (rule 7): a session MCP call waits HUB_CALL_TIMEOUT for
the hub's answer, and a hook never blocks the CLI longer than HOOK_CLIENT_TIMEOUT. The person
comes first (rule 8): the only strings this protocol ever surfaces into a session are WAKE_LINE,
STOP_REASON and unread_context(n), and none of them carries mail content.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

# Mirrored hub constants, copied and never imported (no hub module exists on a workstation).
# test_session_local.py pins each value against session_link.py and mail_relay.py, so a hub
# change that must reach the workstations shows up in the hub's own test run.
LINK_PROTO = "guardian-session-link"
LINK_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024

# The fixed texts of "Hooks" and "Wake": the only strings the protocol ever types or surfaces
# into a session. None of them carries any part of a mail body, whatever the sender wrote.
WAKE_LINE = "new mail from operator: call session_inbox"
STOP_REASON = "new mail: call session_inbox"


def unread_context(n: int) -> str:
    """The one context line UserPromptSubmit adds when mail is waiting (never the content)."""
    if n == 1:
        return "1 new message; read them with session_inbox"
    return f"{n} new messages; read them with session_inbox"


# Limits and timings, seconds unless noted. Rule 7: calls are synchronous, no offline outbox, so
# the session MCP waits HUB_CALL_TIMEOUT for the hub's answer over the daemon's link;
# LOCAL_CLIENT_TIMEOUT is the MCP-side socket timeout (the daemon's own wait plus slack);
# HOOK_CLIENT_TIMEOUT bounds every hook call, because a hook must never hang the CLI.
# BUSY_AGE_CAP: an Esc interrupt fires no Stop hook (S0 part 1), so a busy turn ends at the next
# UserPromptSubmit or SessionEnd, or after this cap. The three wake numbers are "Wake" guards 2,
# 4 and 6. MAIL_BODY_MAX is the 32 KiB envelope limit the hub also enforces on its side.
HUB_CALL_TIMEOUT = 20.0
LOCAL_CLIENT_TIMEOUT = 25.0
HOOK_CLIENT_TIMEOUT = 3.0
BUSY_AGE_CAP = 30 * 60
WAKE_INTERVAL = 600          # no second wake within 10 minutes without a session_inbox call
IDLE_AFTER = 600             # linked, no turn for 10 minutes, counts as idle
INPUT_QUIET = 60             # the tmux client has shown no input for the last 60 s
MAIL_BODY_MAX = 32 * 1024    # bytes, checked on the UTF-8 encoding of the body


def skip_session(env: Mapping[str, str]) -> bool:
    """True when this process must leave the session machinery alone: `--print` children fire the
    hooks with CLAUDE_CODE_ENTRYPOINT=sdk-cli, and GUARDIAN_SESSION_CHILD=1 marks the summary
    child of SD6a, so hooks and the session MCP do nothing in either (S0 part 1)."""
    return (env.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli"
            or env.get("GUARDIAN_SESSION_CHILD") == "1")


# Reply error codes. LINK_DOWN the workstation's link to the hub is down; NOT_REGISTERED the
# session never sent register, so it has no socket; NOT_LINKED the session holds no address
# (session_link not called); TIMEOUT the hub or a hook-side wait ran out; BAD_REQUEST the frame
# failed validate_request; HUB_ERROR the hub refused (the store's reason rides in detail);
# DISABLED the kill switch or `install-session-link.sh --disable` has the machinery off.
LINK_DOWN = "link_down"
NOT_REGISTERED = "not_registered"
NOT_LINKED = "not_linked"
TIMEOUT = "timeout"
BAD_REQUEST = "bad_request"
HUB_ERROR = "hub_error"
DISABLED = "disabled"


def ok_reply(**fields: Any) -> Dict[str, Any]:
    """The successful reply shape; the daemon adds the call's payload as keyword fields."""
    reply: Dict[str, Any] = {"ok": True}
    reply.update(fields)
    return reply


def error_reply(code: str, detail: str = "") -> Dict[str, Any]:
    """The failure reply shape; detail carries the hub's reason or one short explaining line."""
    return {"ok": False, "error": code, "detail": detail}


_SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def valid_session_name(name: Any) -> bool:
    """A session name: lower-case letters, digits and hyphens, 1-32 characters, a letter or digit
    first, unique per workstation ("Addresses")."""
    return isinstance(name, str) and _SESSION_NAME_RE.fullmatch(name) is not None


def address(name: str, workstation: str) -> str:
    """The mail address of a session on a workstation, for example cleaner@ws-mac-1."""
    return f"{name}@{workstation}"


# A Unix socket path is refused over 103 bytes: macOS sun_path holds 104 bytes including the
# terminating NUL, and a longer path makes bind() and connect() fail with no remedy there.
_SOCKET_PATH_MAX = 103


def check_socket_path(path: Union[str, Path]) -> None:
    """Raises ValueError when the socket path does not fit macOS's 104-byte sun_path."""
    encoded = os.fsencode(os.fspath(path))
    if len(encoded) > _SOCKET_PATH_MAX:
        raise ValueError(
            f"socket path is {len(encoded)} bytes, over the {_SOCKET_PATH_MAX}-byte sun_path "
            f"limit: {path}")


@dataclass(frozen=True)
class SessionPaths:
    """Every path the workstation side uses, one tree under <home>/.guardian/session that the
    installer creates and the daemon re-checks; nothing is written anywhere else."""

    home: Path

    @property
    def root(self) -> Path:
        return self.home / ".guardian" / "session"

    @property
    def key(self) -> Path:
        """The workstation's own link key; the private half never leaves the machine (SD2)."""
        return self.root / "link-key"

    @property
    def known_hosts(self) -> Path:
        """The pinned hub host key; both dial paths use HostKeyAlias <hub_alias> over it (SD12)."""
        return self.root / "known_hosts"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def spool(self) -> Path:
        """The daemon's per-session unread mail until the session's next inbox call."""
        return self.root / "spool"

    @property
    def run(self) -> Path:
        """Sockets and pid files; cleared at boot, so nothing here is state."""
        return self.root / "run"

    @property
    def control_socket(self) -> Path:
        """Hooks to daemon: register, prompt_submit, stop, end (shared by all sessions)."""
        return self.run / "control.sock"

    @property
    def log(self) -> Path:
        return self.root / "daemon.log"

    def session_socket(self, cli_session: str) -> Path:
        """The per-session socket the daemon opens after register. Deterministic in the CLI
        session id, so the session MCP finds it from CLAUDE_CODE_SESSION_ID with no handshake
        (rule 5); the 16 hex characters keep the name short for the 104-byte macOS sun_path
        limit with normal homes, and check_socket_path refuses a too-long path outright."""
        digest = hashlib.sha256(cli_session.encode("utf-8")).hexdigest()[:16]
        path = self.run / f"s-{digest}.sock"
        check_socket_path(path)
        return path

    def ensure_dirs(self) -> None:
        """Creates root, spool and run at mode 0o700, and re-applies 0o700 when they exist with
        wider modes, so an over-sharing umask never exposes session state."""
        for path in (self.root, self.spool, self.run):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)


_WORKSTATION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HUB_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
_IPV4_CHARS_RE = re.compile(r"^[0-9.]+$")


def _ipv4_problem(value: Any) -> Optional[str]:
    """hub_address must be an IPv4 literal, the tailnet IPv4 the installer took from Settings."""
    if not isinstance(value, str) or value.count(".") != 3 or not _IPV4_CHARS_RE.fullmatch(value):
        return "hub_address must be an IPv4 literal, for example 100.64.0.5"
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return f"hub_address {value!r} is not an IPv4 address"
    return None


def _dns_problem(value: str) -> Optional[str]:
    """hub_name may be empty (no link name configured); otherwise it must be a DNS name, the
    operator's DNS-only A record of SD12, never something Guardian writes itself."""
    if not value:
        return None
    if len(value) > 253:
        return "hub_name must be a DNS name of at most 253 characters"
    for label in value.split("."):
        if len(label) > 63 or _DNS_LABEL_RE.fullmatch(label) is None:
            return f"hub_name {value!r} is not a DNS name"
    return None


def _is_int(value: Any) -> bool:
    """int and not bool: bool is an int subclass, but True is neither a pid nor a port."""
    return isinstance(value, int) and not isinstance(value, bool)


def _config_problems(cfg: Mapping[str, Any]) -> List[str]:
    """Every problem with the parsed config; empty when the config is well formed."""
    problems: List[str] = []
    keys = {"workstation", "hub_address", "hub_name", "hub_alias", "hub_user", "ssh_port"}
    unknown = sorted(set(cfg) - keys)
    if unknown:
        problems.append(f"unknown config keys: {', '.join(unknown)}")
    for key in sorted(keys - {"ssh_port"}):
        if key not in cfg:
            problems.append(f"missing config key {key!r}")
    if "workstation" in cfg:
        value = cfg["workstation"]
        if not isinstance(value, str) or _WORKSTATION_RE.fullmatch(value) is None:
            problems.append("workstation must match ^[a-z0-9][a-z0-9-]{0,62}$, like ws-mac-1")
    if "hub_address" in cfg:
        problem = _ipv4_problem(cfg["hub_address"])
        if problem:
            problems.append(problem)
    if "hub_name" in cfg:
        value = cfg["hub_name"]
        if not isinstance(value, str):
            problems.append("hub_name must be a DNS name or the empty string")
        else:
            problem = _dns_problem(value)
            if problem:
                problems.append(problem)
    if "hub_alias" in cfg and not (isinstance(cfg["hub_alias"], str) and cfg["hub_alias"]):
        problems.append("hub_alias must be a non-empty string (the ssh HostKeyAlias)")
    if "hub_user" in cfg:
        value = cfg["hub_user"]
        if not isinstance(value, str) or _HUB_USER_RE.fullmatch(value) is None:
            problems.append("hub_user must match ^[a-z_][a-z0-9_-]{0,31}$")
    if "ssh_port" in cfg:
        port = cfg["ssh_port"]
        if not _is_int(port) or not 1 <= port <= 65535:
            problems.append("ssh_port must be an integer from 1 to 65535")
    return problems


def load_config(path: Union[str, Path]) -> Dict[str, Any]:
    """Reads and validates the workstation config (SessionPaths.config). Raises ValueError with
    a clear message on any problem: an unreadable file, invalid JSON, a non-object root, a
    missing key, a bad value or an unknown key. ssh_port is filled in with 22 when absent."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read the session config {config_path}: {exc}") from exc
    try:
        cfg = json.loads(text)
    except ValueError as exc:  # json.JSONDecodeError
        raise ValueError(f"the session config {config_path} is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ValueError(f"the session config {config_path} must be a JSON object")
    problems = _config_problems(cfg)
    if problems:
        raise ValueError(f"the session config {config_path}: {'; '.join(problems)}")
    cfg = dict(cfg)
    cfg.setdefault("ssh_port", 22)
    return cfg


def encode_frame(obj: Any) -> bytes:
    """One NDJSON frame as bytes: JSON with ensure_ascii=False (Korean text stays UTF-8 instead
    of \\uXXXX escapes), no newline inside (json escapes newlines in strings) and exactly one
    newline at the end. ValueError when the payload would exceed MAX_LINE_BYTES, the same line
    limit the hub's link protocol enforces."""
    payload = json.dumps(obj, ensure_ascii=False)
    data = payload.encode("utf-8")
    if b"\n" in data:  # impossible with json's escaping; documents the one-line invariant
        raise ValueError("a frame must be exactly one line")
    if len(data) > MAX_LINE_BYTES:
        raise ValueError(f"frame is {len(data)} bytes, over the {MAX_LINE_BYTES}-byte limit")
    return data + b"\n"


def read_frame(sock_file: Any) -> Dict[str, Any]:
    """Reads one frame line from a binary socket file. ValueError when the peer closed before
    the newline, when the line is not valid UTF-8 JSON, when it is not a JSON object, or when
    it is longer than MAX_LINE_BYTES; the read is bounded, so a broken peer cannot exhaust
    memory."""
    line = sock_file.readline(MAX_LINE_BYTES + 2)
    if not line:
        raise ValueError("connection closed before a frame")
    if not line.endswith(b"\n"):
        # Either the frame is over the limit or EOF cut the line short; one more byte tells.
        if sock_file.readline(1):
            raise ValueError(f"frame exceeds the {MAX_LINE_BYTES}-byte limit")
        raise ValueError("connection closed before the frame's newline")
    if len(line) - 1 > MAX_LINE_BYTES:
        raise ValueError(f"frame exceeds the {MAX_LINE_BYTES}-byte limit")
    try:
        frame = json.loads(line.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"frame is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise ValueError("a frame must be a JSON object")
    return frame


# Hook requests go to the control socket (one for all sessions, so they carry the CLI session
# id); MCP requests go to the session's own socket, where the daemon stamps the session from the
# socket, so a frame that claims an identity is refused (rule 5).
HOOK_OPS = frozenset({"register", "prompt_submit", "stop", "end"})
MCP_OPS = frozenset({"link", "inbox", "send", "list", "status", "resident_query", "resident_query_get",
                     "review_submit", "review_result"})
IDENTITY_FIELDS = ("cli_session", "from", "address", "sender")
SEND_KINDS = ("message", "question", "answer")
_TMUX_PANE_RE = re.compile(r"^%\d+$", re.ASCII)
_QUERY_ID_RE = re.compile(r"^q-\d{8}-\d{4,}$", re.ASCII)
QUERY_WAIT_MAX = 15
# Mirrored hub shapes of the review tools (V2d of docs/design/verification-layer.md), copied and
# never imported; test_session_local.py pins each against review_agent.RUN_ID,
# code_context.REF_PATTERN and the Go hub's project idPattern, so a hub change that must reach
# the workstations shows up in the hub's own test run. The link re-validates everything anyway.
_REVIEW_ID_RE = re.compile(r"^review_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$", re.ASCII)
_CONTEXT_REF_RE = re.compile(r"^[A-Za-z0-9._/~^-]{1,100}$", re.ASCII)
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)


def _need_text(frame: Mapping[str, Any], op: str, key: str, problems: List[str],
               *, empty_ok: bool = False) -> None:
    """A required text field: present, a str, and non-empty unless empty_ok (the hub accepts an
    empty mail subject, for instance)."""
    if key not in frame:
        problems.append(f"{op}: missing field {key!r}")
        return
    value = frame[key]
    if not isinstance(value, str):
        problems.append(f"{op}: {key!r} must be text")
    elif not value and not empty_ok:
        problems.append(f"{op}: {key!r} must not be empty")


def _opt_text(frame: Mapping[str, Any], op: str, key: str, problems: List[str]) -> None:
    """An optional text field: absent or null is fine; a value must be a non-empty str."""
    if key not in frame or frame[key] is None:
        return
    value = frame[key]
    if not isinstance(value, str):
        problems.append(f"{op}: {key!r} must be text or null")
    elif not value:
        problems.append(f"{op}: {key!r} must not be empty when set")


def _check_register(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "register", []
    _need_text(frame, op, "cli_session", problems)
    _need_text(frame, op, "source", problems)
    if "cli_pid" not in frame:
        problems.append(f"{op}: missing field 'cli_pid'")
    else:
        pid = frame["cli_pid"]
        if not _is_int(pid):
            problems.append(f"{op}: 'cli_pid' must be an int")
        elif pid < 1:
            problems.append(f"{op}: 'cli_pid' must be a process id, not {pid}")
    # tmux_socket is the server socket from $TMUX (its first field); absent or null means the
    # session is not in tmux, so the daemon never tries a wake for it ("Wake").
    if "tmux_socket" in frame and frame["tmux_socket"] is not None:
        sock = frame["tmux_socket"]
        if not isinstance(sock, str) or not sock.startswith("/") or "\x00" in sock:
            problems.append(f"{op}: 'tmux_socket' must be an absolute path, or null")
    if "tmux_pane" in frame and frame["tmux_pane"] is not None:
        pane = frame["tmux_pane"]
        if not isinstance(pane, str) or _TMUX_PANE_RE.fullmatch(pane) is None:
            problems.append(f"{op}: 'tmux_pane' must be a tmux pane id like %3, or null")
    _opt_text(frame, op, "project_dir", problems)
    if "name" in frame and frame["name"] is not None and not valid_session_name(frame["name"]):
        problems.append(f"{op}: 'name' must be a session name like cleaner, or null")
    return problems


def _check_session_only(frame: Mapping[str, Any], op: str) -> List[str]:
    """prompt_submit and stop need nothing but the CLI session id."""
    problems: List[str] = []
    _need_text(frame, op, "cli_session", problems)
    return problems


def _check_end(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "end", []
    _need_text(frame, op, "cli_session", problems)
    _need_text(frame, op, "reason", problems)
    return problems


def _check_link(frame: Mapping[str, Any]) -> List[str]:
    """link with no fields reports the current address; name and away change it."""
    op, problems = "link", []
    if "name" in frame and frame["name"] is not None and not valid_session_name(frame["name"]):
        problems.append(f"{op}: 'name' must be a session name like cleaner, or null")
    if "away" in frame and frame["away"] is not None and not isinstance(frame["away"], bool):
        problems.append(f"{op}: 'away' must be true or false, or null")
    return problems


def _check_send(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "send", []
    _need_text(frame, op, "to", problems)
    _need_text(frame, op, "subject", problems, empty_ok=True)
    if "body" not in frame:
        problems.append(f"{op}: missing field 'body'")
    else:
        body = frame["body"]
        if not isinstance(body, str):
            problems.append(f"{op}: 'body' must be text")
        elif len(body.encode("utf-8")) > MAIL_BODY_MAX:
            problems.append(f"{op}: 'body' is over the {MAIL_BODY_MAX}-byte limit")
    if "kind" not in frame:
        problems.append(f"{op}: missing field 'kind'")
    elif frame["kind"] not in SEND_KINDS:
        problems.append(f"{op}: 'kind' must be one of {', '.join(SEND_KINDS)}")
    _opt_text(frame, op, "thread", problems)
    _opt_text(frame, op, "reply_to", problems)
    return problems


_SESSION_KEY_RE = re.compile(
    r"^(?:main|project:[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?::[a-z0-9][a-z0-9-]{0,23})?)$", re.ASCII
)
_QUERY_MODES = ("in_place", "fork")


def _check_resident_query(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "resident_query", []
    _need_text(frame, op, "to", problems)
    _need_text(frame, op, "project", problems)
    if "prompt" not in frame:
        problems.append(f"{op}: missing field 'prompt'")
    else:
        prompt = frame["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            problems.append(f"{op}: 'prompt' must be non-empty text")
        elif len(prompt.encode("utf-8")) > MAIL_BODY_MAX:
            problems.append(f"{op}: 'prompt' is over the {MAIL_BODY_MAX}-byte limit")
    _opt_text(frame, op, "task", problems)
    if "session_key" in frame and frame["session_key"] is not None:
        sk = frame["session_key"]
        if not isinstance(sk, str) or _SESSION_KEY_RE.fullmatch(sk) is None:
            problems.append(f"{op}: 'session_key' must be 'main', 'project:<id>', or 'project:<id>:<label>'")
    if "mode" in frame and frame["mode"] is not None:
        if frame["mode"] not in _QUERY_MODES:
            problems.append(f"{op}: 'mode' must be one of {', '.join(_QUERY_MODES)}")
    if "timeout_sec" in frame and frame["timeout_sec"] is not None:
        t = frame["timeout_sec"]
        if not _is_int(t) or not 10 <= t <= 600:
            problems.append(f"{op}: 'timeout_sec' must be an integer from 10 to 600")
    if "wait_sec" in frame and frame["wait_sec"] is not None:
        w = frame["wait_sec"]
        if not _is_int(w) or not 0 <= w <= QUERY_WAIT_MAX:
            problems.append(f"{op}: 'wait_sec' must be an integer from 0 to {QUERY_WAIT_MAX}")
    return problems


def _check_resident_query_get(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "resident_query_get", []
    if "query_id" not in frame:
        problems.append(f"{op}: missing field 'query_id'")
    else:
        qid = frame["query_id"]
        if not isinstance(qid, str) or _QUERY_ID_RE.fullmatch(qid) is None:
            problems.append(f"{op}: 'query_id' must match q-YYYYMMDD-NNNN")
    if "wait_sec" in frame and frame["wait_sec"] is not None:
        w = frame["wait_sec"]
        if not _is_int(w) or not 0 <= w <= QUERY_WAIT_MAX:
            problems.append(f"{op}: 'wait_sec' must be an integer from 0 to {QUERY_WAIT_MAX}")
    return problems


def _ref_problem(value: Any, key: str) -> Optional[str]:
    """One git ref of the hub's context-ref shape: never option-like, at most 100 characters."""
    if not isinstance(value, str) or value.startswith("-") \
            or _CONTEXT_REF_RE.fullmatch(value) is None:
        return f"'{key}' is not a valid ref (letters, digits . _ / ~ ^ -, no leading '-', " \
               f"at most 100 characters)"
    return None


def _check_review_submit(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "review_submit", []
    if "project" not in frame:
        problems.append(f"{op}: missing field 'project'")
    elif not isinstance(frame["project"], str) or _PROJECT_ID_RE.fullmatch(frame["project"]) is None:
        problems.append(f"{op}: 'project' must be a project id like server-guardian")
    if "ref" not in frame:
        problems.append(f"{op}: missing field 'ref'")
    else:
        problem = _ref_problem(frame["ref"], "ref")
        if problem:
            problems.append(f"{op}: {problem}")
    for key in ("base", "expect_sha", "provider"):
        if key in frame and frame[key] is not None and not isinstance(frame[key], str):
            problems.append(f"{op}: '{key}' must be text or null")
    if frame.get("base") is not None:
        problem = _ref_problem(frame["base"], "base")
        if problem:
            problems.append(f"{op}: {problem}")
    if frame.get("expect_sha") is not None and _SHA1_RE.fullmatch(frame["expect_sha"]) is None:
        problems.append(f"{op}: 'expect_sha' must be 40 lower-case hex characters")
    if frame.get("provider") is not None and not frame["provider"]:
        problems.append(f"{op}: 'provider' must not be empty when set")
    return problems


def _check_review_result(frame: Mapping[str, Any]) -> List[str]:
    op, problems = "review_result", []
    if "review_id" not in frame:
        problems.append(f"{op}: missing field 'review_id'")
    else:
        rid = frame["review_id"]
        if not isinstance(rid, str) or _REVIEW_ID_RE.fullmatch(rid) is None:
            problems.append(f"{op}: 'review_id' must match review_YYYYMMDD_HHMMSS_hex")
    if "wait_sec" in frame and frame["wait_sec"] is not None:
        w = frame["wait_sec"]
        if not _is_int(w) or not 0 <= w <= QUERY_WAIT_MAX:
            problems.append(f"{op}: 'wait_sec' must be an integer from 0 to {QUERY_WAIT_MAX}")
    return problems


_OP_FIELDS: Dict[str, frozenset] = {
    "register": frozenset({"op", "cli_session", "cli_pid", "tmux_socket", "tmux_pane",
                           "project_dir", "source", "name"}),
    "prompt_submit": frozenset({"op", "cli_session"}),
    "stop": frozenset({"op", "cli_session"}),
    "end": frozenset({"op", "cli_session", "reason"}),
    "link": frozenset({"op", "name", "away"}),
    "inbox": frozenset({"op"}),
    "send": frozenset({"op", "to", "kind", "subject", "body", "thread", "reply_to"}),
    "list": frozenset({"op"}),
    "status": frozenset({"op"}),
    "resident_query": frozenset({"op", "to", "project", "prompt", "task", "session_key", "mode",
                                 "timeout_sec", "wait_sec"}),
    "resident_query_get": frozenset({"op", "query_id", "wait_sec"}),
    "review_submit": frozenset({"op", "project", "ref", "base", "expect_sha", "provider"}),
    "review_result": frozenset({"op", "review_id", "wait_sec"}),
}
_OP_CHECKS = {
    "register": _check_register,
    "prompt_submit": lambda frame: _check_session_only(frame, "prompt_submit"),
    "stop": lambda frame: _check_session_only(frame, "stop"),
    "end": _check_end,
    "link": _check_link,
    "inbox": lambda frame: [],
    "send": _check_send,
    "list": lambda frame: [],
    "status": lambda frame: [],
    "resident_query": _check_resident_query,
    "resident_query_get": _check_resident_query_get,
    "review_submit": _check_review_submit,
    "review_result": _check_review_result,
}


def validate_request(frame: Any) -> List[str]:
    """Every problem with one request frame, empty when it is well formed. Hook requests carry
    their cli_session because they arrive on the shared control socket; MCP requests arrive on
    the session's own socket, so a frame that claims an identity (cli_session, from, address,
    sender) is a problem: the daemon stamps the session from the socket (rule 5)."""
    if not isinstance(frame, dict):
        return ["a request must be a JSON object"]
    op = frame.get("op")
    if not isinstance(op, str):
        return ["a request needs a text 'op' field"]
    problems: List[str] = []
    reported = set()
    if op in MCP_OPS:
        for field in IDENTITY_FIELDS:
            if field in frame:
                problems.append(f"{op}: carries {field!r}; the daemon stamps the session from "
                                "the socket, so a session-socket request claims no identity")
                reported.add(field)
    check = _OP_CHECKS.get(op)
    if check is None:
        problems.append(f"unknown op {op!r}")
        return problems
    problems += check(frame)
    for key in sorted(frame):
        if key not in _OP_FIELDS[op] and key not in reported:
            problems.append(f"{op}: unknown field {key!r}")
    return problems


def call(socket_path: Union[str, Path], request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """One blocking round trip over a fresh AF_UNIX stream connection: send one frame, read one
    reply frame, close. OSError (connect refused, timeout, broken peer) and ValueError (frame
    over the line limit, peer not speaking the protocol) reach the caller, who decides fail-open
    or fail-closed: hooks fail open within HOOK_CLIENT_TIMEOUT, the session MCP reports the
    error to the model."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(os.fspath(socket_path))
        read_file = sock.makefile("rb")
        try:
            sock.sendall(encode_frame(request))
            return read_frame(read_file)
        finally:
            read_file.close()
    finally:
        sock.close()

"""The workstation session link daemon (S2 of docs/design/operator-sessions.md).

One daemon per workstation (Architecture rule 5): it owns the hub stream through
session_uplink.Uplink, the durable mail spool, the control socket the CLI hooks talk to and one
socket per registered session that the session MCP talks to. The session a request belongs to is
stamped from the socket it arrived on, never from the frame (rule 5), hook replies never wait for
the hub (rule 7) and the only string ever typed into a terminal stays session_wake's fixed line
(rule 8). Standard library and the three other workstation modules only: this file runs on
operator workstations (test_session_local.py asserts the import guard).

Locking rule: one threading.Lock guards the session table; no handler ever calls the hub
(uplink.request) or tmux (waker) while holding it. Hook and MCP handlers copy what they need,
release, call, then re-take the lock to store the result. Persistence (a small temp-file write)
and spool reads happen under the lock; both are bounded local file operations.

An injected uplink (tests) is wired by attribute when it offers the real Uplink's hook points:
an `on_up` list is appended to and an `on_deliver` attribute (even None) is replaced with the
daemon's own callback, so a fake can fire them exactly like the real one.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socketserver
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import session_local
import session_uplink
import session_wake

TICK_INTERVAL = 30.0        # the tick thread's longest sleep; a deliver signals it at once
PRUNE_INTERVAL = 3600.0     # spool.prune runs at most once an hour
PERSIST_NAME = "sessions.json"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3

# One session record. Every value is JSON-safe because the table is persisted as it is; the
# fields the register frame carries are written once, the rest are daemon state.
RECORD_FIELDS: Dict[str, Any] = {
    "name": None,               # the chosen session name, or None until session_link
    "cli_pid": 0,
    "tmux_socket": None,        # $TMUX's first field, or None outside tmux (no wake then)
    "tmux_pane": None,
    "project_dir": None,
    "busy": False,
    "turn_started": None,       # clock seconds of the UserPromptSubmit that began the turn
    "last_turn": 0.0,           # set at register and at every stop; drives the idle rule
    "mail_during_turn": False,
    "blocked_since_inbox": False,
    "last_wake": None,
    "inbox_since_wake": False,
    "away": False,
    "bound": False,             # the hub confirmed the bind (or the last re-bind)
    "pending_wake": False,
}


def _silent_unlink(path: Path) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _pid_alive(pid: Any) -> bool:
    """Whether the recorded CLI process still exists (a zombie counts as gone only on exit)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True    # alive, just another user's process
    except OSError:
        return False
    return True


def _hub_error(reply: Dict[str, Any]) -> Dict[str, Any]:
    """Maps one hub answer that is not ok to the local error codes of the session MCP."""
    error = str(reply.get("error") or "")
    if error == "link_down":
        return session_local.error_reply(session_local.LINK_DOWN, "")
    if error == "timeout":
        return session_local.error_reply(session_local.TIMEOUT, "")
    return session_local.error_reply(session_local.HUB_ERROR, f"{error}: {reply.get('detail', '')}")


def _local_state(record: Dict[str, Any], now: float) -> str:
    """busy / idle / linked, the local view the wake rule and session_status report."""
    if record.get("busy"):
        return "busy"
    last_turn = record.get("last_turn")
    if isinstance(last_turn, (int, float)) and now - last_turn >= session_local.IDLE_AFTER:
        return "idle"
    return "linked"


def _address_of(name: Optional[str], workstation: str) -> Optional[str]:
    return session_local.address(name, workstation) if name else None


class _Handler(socketserver.StreamRequestHandler):
    """One connection, one frame, one reply; a frame that cannot be read gets no reply."""

    timeout = session_local.LOCAL_CLIENT_TIMEOUT   # a stuck client never holds a thread

    def handle(self) -> None:
        reply = self.server.process(self.rfile)
        if reply is not None:
            try:
                self.wfile.write(session_local.encode_frame(reply))
                self.wfile.flush()
            except (OSError, ValueError):
                pass    # the client hung up: nothing to deliver


class _FrameServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """An AF_UNIX stream server bound to one path, serving one op set for one owner."""

    daemon_threads = True
    allow_reuse_address = False    # a second daemon on the same path must fail, not steal it

    def __init__(self, owner: "Daemon", path: Path, ops: frozenset,
                 cli_session: Optional[str]) -> None:
        self.owner, self.ops, self.cli_session = owner, ops, cli_session
        self.path = Path(path)
        session_local.check_socket_path(self.path)
        try:
            os.unlink(str(self.path))       # a stale socket file from a crash
        except OSError:
            pass
        super().__init__(str(self.path), _Handler)
        os.chmod(str(self.path), 0o600)
        self.serve_thread: Optional[threading.Thread] = None

    def process(self, rfile: Any) -> Optional[Dict[str, Any]]:
        try:
            frame = session_local.read_frame(rfile)
        except (ValueError, OSError):
            return None                      # no reply: close
        problems = session_local.validate_request(frame)
        op = frame.get("op")
        if not problems and op not in self.ops:
            problems = [f"op {op!r} does not run on this socket"]
        if problems:
            return session_local.error_reply(session_local.BAD_REQUEST, "; ".join(problems))
        try:
            if self.cli_session is None:
                return self.owner.hook_op(str(op), frame)
            return self.owner.mcp_op(str(op), frame, self.cli_session)
        except Exception as exc:  # noqa: BLE001 - one poison request never ends the daemon
            return session_local.error_reply(session_local.HUB_ERROR, f"internal: {exc}")


class Daemon:
    """The workstation's session daemon: sockets, the session table, hooks, MCP and the tick."""

    def __init__(self, home: Path, *, config: Optional[Dict[str, Any]] = None,
                 uplink: Optional[Any] = None, waker: Optional[session_wake.Waker] = None,
                 clock: Callable[[], float] = time.time):
        self.home = Path(home)
        self.paths = session_local.SessionPaths(self.home)
        self.paths.ensure_dirs()
        self.config = dict(config) if config is not None \
            else session_local.load_config(self.paths.config)
        self.workstation = self.config["workstation"]
        self._clock = clock
        self.spool = session_uplink.Spool(self.paths)
        self.waker = waker if waker is not None else session_wake.Waker()
        self._wire_uplink(uplink)
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._tick_event = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None
        self._control: Optional[_FrameServer] = None
        self._servers: Dict[str, _FrameServer] = {}
        self._next_prune = 0.0
        self._last_link: Optional[Tuple[str, Any]] = None
        self.log = self._make_logger()
        self._hooks = {"register": self._hook_register, "prompt_submit": self._hook_prompt_submit,
                       "stop": self._hook_stop, "end": self._hook_end}
        self._mcps = {"link": self._mcp_link, "inbox": self._mcp_inbox, "send": self._mcp_send,
                      "list": self._mcp_list, "status": self._mcp_status,
                      "resident_query": self._mcp_resident_query,
                      "resident_query_get": self._mcp_resident_query_get,
                      "review_submit": self._mcp_review_submit,
                      "review_result": self._mcp_review_result}

    def _wire_uplink(self, uplink: Optional[Any]) -> None:
        """The real Uplink gets the callbacks through its constructor; an injected one offers
        the same hook points as plain attributes (see the module docstring)."""
        if uplink is None:
            self.uplink = session_uplink.Uplink(self.config, self.paths, self.spool,
                                                on_up=[self._when_link_up],
                                                on_deliver=self._on_deliver)
            return
        self.uplink = uplink
        if isinstance(getattr(uplink, "on_up", None), list):
            uplink.on_up.append(self._when_link_up)
        if hasattr(uplink, "on_deliver"):
            uplink.on_deliver = self._on_deliver

    def _make_logger(self) -> logging.Logger:
        """A private rotating file logger on paths.log; never mail subjects or bodies."""
        logger = logging.Logger("session-daemon", level=logging.INFO)
        handler = logging.handlers.RotatingFileHandler(str(self.paths.log),
                                                       maxBytes=LOG_MAX_BYTES,
                                                       backupCount=LOG_BACKUPS)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    # --- life cycle -----------------------------------------------------------------------

    def start(self) -> "Daemon":
        self.paths.ensure_dirs()
        with self._lock:
            self.sessions = self._load_sessions()
            self._persist_locked()
        self._control = self._open_server(self.paths.control_socket,
                                          session_local.HOOK_OPS, None)
        for cli_session in list(self.sessions):
            self._reopen_session_socket(cli_session)
        self._next_prune = self._clock() + PRUNE_INTERVAL
        self.uplink.start()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True,
                                             name="session-tick")
        self._tick_thread.start()
        return self

    def stop(self) -> None:
        """Stops everything and removes every socket this daemon created."""
        self._stop_event.set()
        self._tick_event.set()
        thread = self._tick_thread
        if thread is not None:
            thread.join(timeout=5.0)
        self.uplink.stop()
        self._close_server(self._control)
        self._control = None
        for cli_session in list(self._servers):
            self._close_server(self._servers.pop(cli_session))

    # --- sockets --------------------------------------------------------------------------

    def _open_server(self, path: Path, ops: frozenset, cli_session: Optional[str]) -> _FrameServer:
        server = _FrameServer(self, path, ops, cli_session)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                                  daemon=True, name=f"sock-{server.path.name}")
        server.serve_thread = thread
        thread.start()
        return server

    def _close_server(self, server: Optional[_FrameServer]) -> None:
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - closing must always finish
            pass
        thread = server.serve_thread
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        _silent_unlink(server.path)

    def _reopen_session_socket(self, cli_session: str) -> None:
        """Opens (or replaces) one session socket; an impossible path drops the session."""
        try:
            path = self.paths.session_socket(cli_session)
        except ValueError as exc:
            self.log.info("session dropped: socket path for %s: %s", cli_session[:8], exc)
            with self._lock:
                self.sessions.pop(cli_session, None)
                self._persist_locked()
            return
        self._close_server(self._servers.pop(cli_session, None))
        self._servers[cli_session] = self._open_server(path, session_local.MCP_OPS, cli_session)

    # --- the session table and persistence -------------------------------------------------

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        """The persisted table, minus entries whose CLI process is gone."""
        try:
            data = json.loads((self.paths.run / PERSIST_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        table = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(table, dict):
            return {}
        live: Dict[str, Dict[str, Any]] = {}
        for cli_session, record in table.items():
            if isinstance(record, dict) and _pid_alive(record.get("cli_pid")):
                merged = dict(RECORD_FIELDS)
                merged.update({key: value for key, value in record.items()
                               if key in RECORD_FIELDS})
                live[cli_session] = merged
        return live

    def _persist_locked(self) -> None:
        """Writes run/sessions.json (0600, temp file + rename); the caller holds the lock."""
        path = self.paths.run / PERSIST_NAME
        fd, tmp = tempfile.mkstemp(prefix=".sessions.", suffix=".tmp", dir=str(self.paths.run))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"sessions": self.sessions}, handle, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            _silent_unlink(Path(tmp))
            raise

    def _record(self, cli_session: str) -> Optional[Dict[str, Any]]:
        """A copy of one record, or None; for tests and internal reads."""
        with self._lock:
            record = self.sessions.get(cli_session)
            return dict(record) if record is not None else None

    # --- hook ops (never wait on the hub) ---------------------------------------------------

    def hook_op(self, op: str, frame: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._hooks[op](frame)
        except Exception as exc:  # noqa: BLE001 - a hook must never see a crash
            return session_local.error_reply(session_local.HUB_ERROR, f"internal: {exc}")

    def _hook_register(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        cli_session = frame["cli_session"]
        name = frame.get("name")
        warning = None
        if name is not None and not session_local.valid_session_name(name):
            name, warning = None, "bad_name"
        record = dict(RECORD_FIELDS)
        record.update({"name": name, "cli_pid": frame["cli_pid"],
                       "tmux_socket": frame.get("tmux_socket"),
                       "tmux_pane": frame.get("tmux_pane"),
                       "project_dir": frame.get("project_dir"),
                       "last_turn": self._clock()})
        with self._lock:
            self.sessions[cli_session] = record
            self._persist_locked()
        self._reopen_session_socket(cli_session)
        if name is not None:
            self._bind_async(cli_session, name, record.get("away", False))
        self.log.info("register %s name=%s", cli_session[:8], name or "-")
        reply = session_local.ok_reply(
            socket=str(self.paths.session_socket(cli_session)),
            address=_address_of(name, self.workstation))
        if warning is not None:
            reply["warning"] = warning
        return reply

    def _hook_prompt_submit(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        cli_session = frame["cli_session"]
        with self._lock:
            record = self.sessions.get(cli_session)
            if record is None:
                return session_local.error_reply(session_local.NOT_REGISTERED,
                                                 f"no session {cli_session[:8]}")
            record["busy"] = True
            record["turn_started"] = self._clock()
            record["mail_during_turn"] = False
            name, bound = record.get("name"), record.get("bound")
            self._persist_locked()
        if name and bound:
            self._state_async(name, "busy")
        unread = self.spool.unread_count(name) if name else 0
        return session_local.ok_reply(unread=unread)

    def _hook_stop(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        cli_session = frame["cli_session"]
        with self._lock:
            record = self.sessions.get(cli_session)
            if record is None:
                return session_local.error_reply(session_local.NOT_REGISTERED,
                                                 f"no session {cli_session[:8]}")
            record["busy"] = False
            record["last_turn"] = self._clock()
            name, bound = record.get("name"), record.get("bound")
            has_unread = bool(name and self.spool.unread_count(name))
            block = bool(record["mail_during_turn"]) and has_unread and not record["away"] \
                and not record["blocked_since_inbox"]
            if block:
                record["blocked_since_inbox"] = True
                record["mail_during_turn"] = False
            elif not has_unread:
                record["mail_during_turn"] = False
            self._persist_locked()
        if name and bound:
            self._state_async(name, "linked")
        return session_local.ok_reply(block=block)

    def _hook_end(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        cli_session = frame["cli_session"]
        with self._lock:
            record = self.sessions.pop(cli_session, None)
            self._persist_locked()
        self._close_server(self._servers.pop(cli_session, None))
        if record is not None and record.get("name") and record.get("bound"):
            self._unbind_async(record["name"])
        return session_local.ok_reply()

    # --- background hub calls (never under the lock) -----------------------------------------

    def _request(self, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.uplink.request(op, payload, timeout=session_local.HUB_CALL_TIMEOUT)

    def _bind_async(self, cli_session: str, name: str, away: bool) -> None:
        """Binds (or re-binds) one name in the background; the reply never waits for it."""
        def run() -> None:
            reply = self._request("bind", {"name": name, "cli_session": cli_session,
                                           "away": away})
            with self._lock:
                record = self.sessions.get(cli_session)
                if record is not None and record.get("name") == name:
                    record["bound"] = bool(reply.get("ok"))
                    self._persist_locked()
            self._log_bind(name, reply)

        threading.Thread(target=run, daemon=True, name=f"bind-{name}").start()

    def _unbind_async(self, name: str) -> None:
        def run() -> None:
            reply = self._request("unbind", {"name": name})
            self.log.info("unbind %s: %s", name, "ok" if reply.get("ok")
                          else reply.get("error"))

        threading.Thread(target=run, daemon=True, name=f"unbind-{name}").start()

    def _state_async(self, name: str, state: str) -> None:
        def run() -> None:
            reply = self._request("state", {"name": name, "state": state})
            if not reply.get("ok"):
                self.log.info("state %s %s refused: %s", name, state, reply.get("error"))

        threading.Thread(target=run, daemon=True, name=f"state-{name}").start()

    def _log_bind(self, name: str, reply: Dict[str, Any]) -> None:
        if reply.get("ok"):
            self.log.info("bind %s ok %s", name, reply.get("address", ""))
        else:
            self.log.info("bind %s refused %s: %s", name, reply.get("error"),
                          reply.get("detail", ""))

    # --- MCP ops (wait for the hub, up to HUB_CALL_TIMEOUT) ----------------------------------

    def mcp_op(self, op: str, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        try:
            return self._mcps[op](frame, cli_session)
        except Exception as exc:  # noqa: BLE001 - the MCP reports it, the daemon lives on
            return session_local.error_reply(session_local.HUB_ERROR, f"internal: {exc}")

    def _named(self, cli_session: str) -> Optional[str]:
        """The session's name, or None; the caller decides whether that is NOT_LINKED."""
        with self._lock:
            record = self.sessions.get(cli_session)
            return record.get("name") if record is not None else None

    def _mcp_link(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name, away = frame.get("name"), frame.get("away")
        if name is None and away is None:
            record = self._record(cli_session) or {}
            return session_local.ok_reply(
                address=_address_of(record.get("name"), self.workstation),
                away=bool(record.get("away")))
        if name is not None:
            away_now = bool((self._record(cli_session) or {}).get("away"))
            reply = self._request("bind", {"name": name, "cli_session": cli_session,
                                           "away": away_now})
            if not reply.get("ok"):
                return _hub_error(reply)
            with self._lock:
                record = self.sessions.get(cli_session)
                if record is not None:
                    record["name"], record["bound"] = name, True
                    self._persist_locked()
            self._log_bind(name, reply)
        if away is not None:
            held = self._named(cli_session)
            if held is None:
                return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
            reply = self._request("state", {"name": held,
                                            "state": "away" if away else "back"})
            if not reply.get("ok"):
                return _hub_error(reply)
            with self._lock:
                record = self.sessions.get(cli_session)
                if record is not None:
                    record["away"] = bool(away)
                    self._persist_locked()
        record = self._record(cli_session) or {}
        return session_local.ok_reply(address=_address_of(record.get("name"), self.workstation),
                                      away=bool(record.get("away")))

    def _mcp_inbox(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        messages = self.spool.unread(name)
        ids = [mail["id"] for mail in messages]
        self.spool.mark_retrieved(ids)
        self._request("ack", {"name": name, "ids": ids})   # best effort: a failed ack leaves
        with self._lock:                                    # the mail delivered on the hub, and
            record = self.sessions.get(cli_session)        # dedupe makes a redelivery harmless
            if record is not None:
                record["inbox_since_wake"] = True
                record["blocked_since_inbox"] = False
                record["pending_wake"] = False
                if not self.spool.unread_count(name):
                    record["mail_during_turn"] = False
                self._persist_locked()
        return session_local.ok_reply(messages=[
            {"id": mail.get("id"), "from": mail.get("from"), "kind": mail.get("kind"),
             "thread": mail.get("thread"), "reply_to": mail.get("reply_to"),
             "subject": mail.get("subject"), "created": mail.get("created"),
             "body": mail.get("body")}
            for mail in messages])

    def _mcp_send(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        payload: Dict[str, Any] = {"name": name, "to": frame["to"], "kind": frame["kind"],
                                   "subject": frame["subject"], "body": frame["body"],
                                   "op_id": uuid.uuid4().hex}   # one op id per MCP call
        for key in ("thread", "reply_to"):
            if frame.get(key) is not None:
                payload[key] = frame[key]
        reply = self._request("send", payload)
        if not reply.get("ok"):
            return _hub_error(reply)
        return session_local.ok_reply(id=reply.get("id"))

    def _mcp_list(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        reply = self._request("list", {"name": name})
        if not reply.get("ok"):
            return _hub_error(reply)
        return session_local.ok_reply(peers=reply.get("peers"))

    def _mcp_status(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        record = self._record(cli_session) or {}
        name = record.get("name")
        unread = self.spool.unread_count(name) if name else 0
        return session_local.ok_reply(
            link=self.uplink.state, last_error=self.uplink.last_error,
            address=_address_of(name, self.workstation), away=bool(record.get("away")),
            state=_local_state(record, self._clock()), unread=unread,
            remote_approval=False, summary_mirror=False)

    def _poll_resident_query(self, name: str, query_id: str, wait_sec: int,
                             initial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Poll hub `query_get` up to `wait_sec` (capped at QUERY_WAIT_MAX) for terminal state."""
        terminal = ("completed", "failed", "expired", "cancelled")
        latest: Dict[str, Any] = dict(initial) if isinstance(initial, dict) else {"id": query_id}
        deadline = time.monotonic() + max(0, min(int(wait_sec), session_local.QUERY_WAIT_MAX))
        while True:
            reply = self._request("query_get", {"name": name, "query_id": query_id})
            if not reply.get("ok"):
                if initial is not None:
                    return session_local.ok_reply(query=latest, done=latest.get("status") in terminal)
                return _hub_error(reply)
            q = reply.get("query")
            if isinstance(q, dict):
                latest = q
                if q.get("status") in terminal:
                    return session_local.ok_reply(query=latest, done=True)
            if time.monotonic() >= deadline or self._stop_event.is_set():
                return session_local.ok_reply(query=latest, done=latest.get("status") in terminal)
            self._stop_event.wait(1.0)

    def _mcp_resident_query(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        payload: Dict[str, Any] = {
            "name": name,
            "to": frame["to"],
            "project": frame["project"],
            "prompt": frame["prompt"],
            "op_id": uuid.uuid4().hex,
        }
        if frame.get("task") is not None:
            payload["task"] = frame["task"]
        if frame.get("session_key") is not None:
            payload["session_key"] = frame["session_key"]
        if frame.get("mode") is not None:
            payload["mode"] = frame["mode"]
        if frame.get("timeout_sec") is not None:
            payload["timeout_sec"] = frame["timeout_sec"]
        reply = self._request("query_submit", payload)
        if not reply.get("ok"):
            return _hub_error(reply)
        qid = reply.get("id")
        wait_sec = frame.get("wait_sec")
        wait = 12 if wait_sec is None else int(wait_sec)
        initial = {k: v for k, v in reply.items() if k != "ok"}
        if wait > 0 and isinstance(qid, str):
            return self._poll_resident_query(name, qid, wait, initial=initial)
        return session_local.ok_reply(query=initial, done=False)

    def _mcp_resident_query_get(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        wait_sec = frame.get("wait_sec")
        wait = 0 if wait_sec is None else int(wait_sec)
        return self._poll_resident_query(name, frame["query_id"], wait, initial=None)

    def _poll_review(self, name: str, review_id: str, wait_sec: int) -> Dict[str, Any]:
        """Poll the hub's `review_get` up to wait_sec (capped at QUERY_WAIT_MAX) for a terminal
        review status; the same shape of wait as _poll_resident_query."""
        terminal = ("completed", "failed")
        latest: Dict[str, Any] = {"id": review_id, "status": "running"}
        deadline = time.monotonic() + max(0, min(int(wait_sec), session_local.QUERY_WAIT_MAX))
        while True:
            reply = self._request("review_get", {"name": name, "review_id": review_id})
            if not reply.get("ok"):
                return _hub_error(reply)
            review = reply.get("review")
            if isinstance(review, dict):
                latest = review
                if review.get("status") in terminal:
                    return session_local.ok_reply(review=latest, done=True)
            if time.monotonic() >= deadline or self._stop_event.is_set():
                return session_local.ok_reply(review=latest,
                                               done=latest.get("status") in terminal)
            self._stop_event.wait(1.0)

    def _mcp_review_submit(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        payload: Dict[str, Any] = {"name": name, "project": frame["project"], "ref": frame["ref"]}
        for key in ("base", "expect_sha", "provider"):
            if frame.get(key) is not None:
                payload[key] = frame[key]
        reply = self._request("review_submit", payload)
        if not reply.get("ok"):
            return _hub_error(reply)
        return session_local.ok_reply(id=reply.get("id"), status=reply.get("status"),
                                      label=reply.get("label"), note=reply.get("note"))

    def _mcp_review_result(self, frame: Dict[str, Any], cli_session: str) -> Dict[str, Any]:
        name = self._named(cli_session)
        if name is None:
            return session_local.error_reply(session_local.NOT_LINKED, "session_link first")
        wait_sec = frame.get("wait_sec")
        wait = 0 if wait_sec is None else int(wait_sec)
        return self._poll_review(name, frame["review_id"], wait)

    # --- uplink callbacks --------------------------------------------------------------------

    def _when_link_up(self) -> None:
        """A new lease marks the hub's session rows offline, so every named session is
        re-bound; the stream thread must not wait for the binds, hence the thread."""
        threading.Thread(target=self._rebind_all, daemon=True, name="rebind").start()

    def _rebind_all(self) -> None:
        with self._lock:
            items = [(cli_session, record.get("name"), bool(record.get("away")),
                      bool(record.get("busy")))
                     for cli_session, record in self.sessions.items() if record.get("name")]
        for cli_session, name, away, busy in items:
            reply = self._request("bind", {"name": name, "cli_session": cli_session,
                                           "away": away})
            ok = bool(reply.get("ok"))
            with self._lock:
                record = self.sessions.get(cli_session)
                if record is not None and record.get("name") == name:
                    record["bound"] = ok
                    self._persist_locked()
            self._log_bind(name, reply)
            if ok and busy:
                self._request("state", {"name": name, "state": "busy"})

    def _on_deliver(self, mail: Dict[str, Any], fresh: bool) -> None:
        """Runs on the link thread: flags only, no hub call, no tmux, then a tick signal."""
        local = str(mail.get("to") or "").split("@", 1)[0]
        with self._lock:
            for record in self.sessions.values():
                if record.get("name") != local:
                    continue
                if record.get("busy"):
                    record["mail_during_turn"] = True
                if mail.get("from") == "operator":
                    record["pending_wake"] = True
                self._persist_locked()
                break
        self._tick_event.set()

    # --- the tick thread ----------------------------------------------------------------------

    def _tick_loop(self) -> None:
        while not self._stop_event.is_set():
            self._tick_event.wait(TICK_INTERVAL)
            self._tick_event.clear()
            try:
                self._tick_once(self._clock())
            except Exception as exc:  # noqa: BLE001 - one bad tick never ends the loop
                self.log.info("tick failed: %s", exc)

    def _tick_once(self, now: float) -> None:
        self._log_link_state()
        self._wake_pass(now)
        self._busy_cap(now)
        self._reap_dead_sessions()
        if now >= self._next_prune:
            self._next_prune = now + PRUNE_INTERVAL
            self.spool.prune(now)

    def _log_link_state(self) -> None:
        current = (self.uplink.state, self.uplink.last_error)
        if current != self._last_link:
            self._last_link = current
            state, error = current
            self.log.info("link %s%s", state, f" ({error})" if error else "")

    def _wake_pass(self, now: float) -> None:
        """The wake decision of every session with pending operator mail ("Wake")."""
        with self._lock:
            candidates: List[Tuple[Any, ...]] = []
            changed = False
            for cli_session, record in self.sessions.items():
                if not record.get("pending_wake"):
                    continue
                name = record.get("name")
                if name and not self.spool.unread_count(name):
                    record["pending_wake"] = False   # nothing unread: nothing to wake for
                    changed = True
                    continue
                if record.get("tmux_pane") and record.get("tmux_socket"):
                    candidates.append((cli_session, name, record["tmux_pane"],
                                       record["tmux_socket"], record["cli_pid"],
                                       _local_state(record, now), bool(record.get("away")),
                                       record.get("last_wake"),
                                       bool(record.get("inbox_since_wake"))))
            if changed:
                self._persist_locked()
        for cli_session, name, pane, tmux_socket, cli_pid, state, away, last_wake, \
                inbox_since_wake in candidates:
            ok, reason = session_wake.wake_policy(
                from_kind="operator", state=state, away=away, last_wake=last_wake,
                inbox_since_wake=inbox_since_wake, now=now)
            if ok:
                ok, reason = self.waker.try_wake(
                    session_wake.WakeTarget(pane, tmux_socket, cli_pid))
            if ok:
                with self._lock:
                    record = self.sessions.get(cli_session)
                    if record is not None:
                        record["last_wake"] = now
                        record["inbox_since_wake"] = False
                        record["pending_wake"] = False
                        self._persist_locked()
            # a refusal keeps pending_wake: the next tick retries (wake_policy re-decides)
            self.log.info("wake %s: %s", name or "-", reason)

    def _busy_cap(self, now: float) -> None:
        """An Esc interrupt fires no Stop hook, so a busy turn ends here after BUSY_AGE_CAP."""
        with self._lock:
            changed = False
            for record in self.sessions.values():
                started = record.get("turn_started")
                if record.get("busy") and isinstance(started, (int, float)) \
                        and now - started > session_local.BUSY_AGE_CAP:
                    record["busy"] = False
                    record["last_turn"] = now
                    changed = True
            if changed:
                self._persist_locked()

    def _reap_dead_sessions(self) -> None:
        """Drops sessions whose CLI process has exited without SessionEnd (e.g. Antigravity or a
        killed CLI), closing their sockets and unbinding their names on the hub."""
        dead: List[Tuple[str, Optional[str], bool, Any]] = []
        with self._lock:
            for cli_session, record in list(self.sessions.items()):
                if not _pid_alive(record.get("cli_pid")):
                    self.sessions.pop(cli_session, None)
                    dead.append((cli_session, record.get("name"), bool(record.get("bound")),
                                 record.get("cli_pid")))
            if dead:
                self._persist_locked()
        for cli_session, name, bound, pid in dead:
            self._close_server(self._servers.pop(cli_session, None))
            if name and bound:
                self._unbind_async(name)
            self.log.info("reap %s name=%s pid=%s", cli_session[:8], name or "-", pid)


def _check_config(home: Path) -> int:
    try:
        session_local.load_config(session_local.SessionPaths(home).config)
    except ValueError as exc:
        print(exc)
        return 2
    print("ok")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="the workstation session link daemon")
    parser.add_argument("--home", default=str(Path.home()), help="the operator's home directory")
    parser.add_argument("--check-config", action="store_true",
                        help="validate the config and exit (0 ok, 2 a problem)")
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.check_config:
        return _check_config(home)
    try:
        daemon = Daemon(home)
    except ValueError as exc:
        print(exc)
        return 2
    daemon.start()
    done = threading.Event()

    def finish(signum: Any, frame: Any) -> None:
        done.set()

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    try:
        done.wait()
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Node side of the resident agents (docs/design/resident-agents.md, M2c): the daemon's resident duties.

The worker daemon of remote-agents.md gains a relay stream to the hub (`worker.py relay`, protocol 3).
ResidentManager owns everything resident on this node: the per-agent directories under
<GUARDIAN_WORKER_DIR>/agents, the delivered-mail spool with its tombstones and pending
acknowledgements, one unix socket per agent incarnation, and the hub stream itself. The mail store and
the task board live on the hub (mail_store.py); nothing here imports a hub module or reads a hub file.

Frame contract ("M2 relay frame protocol" in the design): one JSON object per line, at most 1 MiB,
ids d<n> from this side and h<n> from the hub, replies in any order, a request with no reply after
20 s fails while the stream stays, 90 s without a frame from the hub ends the stream, and a newer
stream always wins.

Identity comes from the socket: the agent and the epoch of every agent-socket call are those of the
listener the call arrived on; caller-sent `from`, `from_kind`, `hop`, `epoch`, `agent` and `turn`
fields are dropped before anything is forwarded.

Locking rule: the manager lock (`self._lock`) guards in-memory state and the small disk files and is
never held across a `request()` call (which can wait 20 s) nor across a driver callback.
"""

import calendar
import json
import os
import queue
import re
import select
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import resident_open  # O2: the sandbox paths, checks and file duties of open agents
import resident_tasks  # M2e: the owned-task tables and the resident repo router (node side)
from remote_agents import code_version

RELAY_PROTOCOL = 3
# The relay contract caps one frame line at 1 MiB. remote_agents.MAX_MESSAGE_BYTES (16 MiB) is the limit of the
# one-shot client protocol (op run); the relay stream has its own, smaller one.
MAX_FRAME_BYTES = 1024 * 1024
# One mail_inbox reply stays this far under the frame limit (room for the reply's own fields).
INBOX_REPLY_BUDGET = MAX_FRAME_BYTES - 64 * 1024
REQUEST_TIMEOUT = 20.0  # a request with no reply after this fails; the stream stays
SILENCE_SECONDS = 90.0  # no frame from the hub for this long ends the stream
POLL_SECONDS = 0.25     # how often the reader rechecks the silence clock and the stop flag
# sun_path of sockaddr_un minus the terminating NUL on the smallest system involved (macOS: 104 bytes).
SOCKET_PATH_MAX = 103
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MAIL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
QUERY_ID_RE = re.compile(r"^q-\d{8}-\d{4,}$", re.ASCII)
MAX_LOCAL_QUERIES = 5


def _is_open(spec: Any) -> bool:
    """Whether a spec declares an open agent (docs/design/resident-open-tools.md, mode as data)."""
    return isinstance(spec, dict) and spec.get("tools") == "open"

# Fields of a caller that would name an identity or a path the hub computes itself, plus the call log
# only the daemon may attach to a task_update (M2e). A task_update's "patch"/"patch_base" (O3) pass
# through unstripped: the mailbox MCP sets them, the caller cannot, and the hub checks both.
SPOOF_KEYS = ("from", "from_kind", "hop", "epoch", "agent", "turn", "calls")
# The tools the hub answers; mail_inbox is the one tool the local spool answers.
HUB_TOOLS = ("mail_send", "mail_agents", "task_list", "task_create", "task_claim", "task_update",
             "task_release")
# Hub tools available only to agents declared tools: "open" (PD3-a, PD4).
OPEN_HUB_TOOLS = ("artifact_publish", "ops_propose_package", "ops_propose_update")


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _parse_iso(text: Any) -> Optional[float]:
    if not isinstance(text, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _write_json(path: Path, payload: Any) -> None:
    """Atomic write (temp file + rename, 0600); the parent directory must exist."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


class _NullDriver:
    """The driver used until M2d: no CLI to wake, nothing to start or stop."""

    def agents_changed(self, added: List[str], updated: List[str], removed: List[str]) -> None:
        pass

    def wake(self, name: str) -> None:
        pass


class _Stream:
    """One relay stream to the hub. `alive` is False once the stream has ended (EOF, silence, an
    unreadable or oversized line, shutdown, or a newer attach taking over)."""

    def __init__(self, rfile: Any, wfile: Any) -> None:
        self.rfile = rfile
        self.wfile = wfile
        self.alive = True
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.reader: Optional[threading.Thread] = None

    def resolve(self, request_id: Any, reply: Dict[str, Any]) -> None:
        with self.pending_lock:
            entry = self.pending.pop(request_id, None) if isinstance(request_id, str) else None
        if entry is not None:
            entry["reply"] = reply
            entry["event"].set()

    def fail_all(self, error: str) -> None:
        with self.pending_lock:
            entries = list(self.pending.values())
            self.pending.clear()
        for entry in entries:
            entry["reply"] = {"ok": False, "error": error}
            entry["event"].set()


class _Agent:
    """One agent's state: the on-disk directory plus the current incarnation's socket."""

    def __init__(self, name: str, address: str, spec: Dict[str, Any], spec_version: Any,
                 epoch: int = 0, session_id: Optional[str] = None) -> None:
        self.name = name
        self.address = address
        self.spec = spec
        self.spec_version = spec_version
        self.epoch = epoch
        self.session_id = session_id
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.ready = False
        self.current_turn: Optional[Dict[str, Any]] = None  # {"turn": id, "epoch": n}
        self.listener: Optional["_AgentListener"] = None


class _AgentListener:
    """One unix socket per incarnation: each connection is one JSON line in, one line out, then closed."""

    def __init__(self, manager: "ResidentManager", name: str, epoch: int, path: Path) -> None:
        self._manager = manager
        self._name = name
        self.epoch = epoch
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX)
        self._lock = threading.Lock()
        self._conns: List[socket.socket] = []
        self._stopping = False
        self.thread = threading.Thread(target=self._loop, name=f"resident-{name}", daemon=True)

    def start(self, umask: int = 0o177) -> None:
        previous = os.umask(umask)  # closed: the socket file 0600; open (C7): 0660, group gsa-<name>
        try:
            self._socket.bind(str(self.path))
        finally:
            os.umask(previous)
        self._socket.listen(8)
        self.thread.start()

    def _loop(self) -> None:
        self._socket.settimeout(0.5)  # a bounded accept so close() is noticed without a wakeup connection
        while not self._stopping:
            try:
                conn, _addr = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._stopping:
                    _close_quietly(conn)
                    continue
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            self._manager._serve_agent_connection(self._name, self.epoch, conn)
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            _close_quietly(conn)

    def close(self) -> None:
        """Closes the listener and every connection accepted on it, and removes the socket file."""
        if self._stopping:
            return
        self._stopping = True
        _close_quietly(self._socket)
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _close_quietly(conn)
        try:
            self.path.unlink()
        except OSError:
            pass
        if self.thread is not threading.current_thread() and self.thread.is_alive():
            self.thread.join(2.0)


def _close_quietly(side: Any) -> None:
    try:
        side.close()
    except OSError:
        pass


class ResidentManager:
    """The daemon's resident duties (M2c): the hub relay stream, the mail spool and the agent sockets.

    Constructed lazily by the worker daemon on the first `relay` op, so a daemon that never sees a
    relay creates nothing. `clock` and `wall` are injectable for tests (the silence timeout and the
    request timeout run on `clock`, the timestamps on `wall`)."""

    def __init__(self, worker: Any, root: Optional[Path] = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, driver: Any = None,
                 sandbox_paths: Optional["resident_open.SandboxPaths"] = None) -> None:
        self.worker = worker
        if root is None:
            from worker import worker_dir  # lazy: worker.py imports this module lazily too
            root = worker_dir() / "agents"
        self.root = Path(root)
        self.clock = clock
        self.wall = wall
        self.driver = driver if driver is not None else _NullDriver()
        self.sandbox_paths = sandbox_paths if sandbox_paths is not None else resident_open.DEFAULT_PATHS
        self._lock = threading.RLock()
        self._stream: Optional[_Stream] = None
        self._next_id = 0
        self._agents: Dict[str, _Agent] = {}
        self._task_tables: Dict[str, "resident_tasks.TaskTable"] = {}  # M2e, one per agent
        self._repo: Optional["resident_tasks.RepoRouter"] = None
        self._dispatch_queue: Optional["queue.Queue[Any]"] = None
        self._stopping = False
        self._load()

    # --- state on disk -------------------------------------------------------------------------

    def _dir(self, name: str) -> Path:
        return self.root / name

    def _load(self) -> None:
        """Reads the agents found under root (creating nothing; root may not exist) so `sync` can
        report them. Sockets of a previous daemon run are stale: no listener survived, so the files
        go (an MCP child of a dead incarnation must fail to connect, not hang on a dead socket)."""
        if not self.root.is_dir():
            return
        for child in sorted(self.root.iterdir()):
            state = _read_json(child / "state.json", None) if child.is_dir() else None
            if not isinstance(state, dict) or not NAME_RE.fullmatch(child.name):
                continue
            address = state.get("address")
            if not isinstance(address, str) or address.split("@", 1)[0] != child.name:
                continue
            spec = state.get("spec") if isinstance(state.get("spec"), dict) else {}
            session = state.get("session_id") if isinstance(state.get("session_id"), str) else None
            epoch = state.get("epoch")
            agent_obj = _Agent(
                child.name, address, spec, state.get("spec_version"),
                epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else 0, session)
            raw_sessions = state.get("sessions")
            if isinstance(raw_sessions, dict):
                agent_obj.sessions = {
                    str(k): dict(v) for k, v in raw_sessions.items()
                    if isinstance(k, str) and isinstance(v, dict)
                }
            self._agents[child.name] = agent_obj
            for stale in child.glob("e*.sock"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            if _is_open(spec):
                # an open agent's sockets live in the sandbox sock directory, not here; they are
                # stale the same way, but only what lstat says is a socket may be unlinked (C7)
                resident_open.remove_stale_sockets(self.sandbox_paths.sock_dir(child.name))

    def _write_state(self, agent: _Agent) -> None:
        data: Dict[str, Any] = {
            "address": agent.address, "spec": agent.spec, "spec_version": agent.spec_version,
            "epoch": agent.epoch, "session_id": agent.session_id,
        }
        if agent.sessions:
            data["sessions"] = agent.sessions
        _write_json(self._dir(agent.name) / "state.json", data)

    def _spool_dir(self, name: str) -> Path:
        return self._dir(name) / "spool"

    def _spool_file(self, name: str, mail_id: str) -> Path:
        return self._spool_dir(name) / f"{mail_id}.json"

    def _spool_entries(self, agent: _Agent) -> List[Tuple[str, Dict[str, Any]]]:
        """(id, envelope) of everything in the spool, oldest first by created then id."""
        entries: List[Tuple[str, Dict[str, Any]]] = []
        try:
            paths = list(self._spool_dir(agent.name).iterdir())
        except OSError:
            return []
        for path in paths:
            if path.suffix != ".json" or path.name.startswith(".") or path.stem == "tombstones":
                continue
            data = _read_json(path, None)
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                entries.append((data["id"], data))
        entries.sort(key=lambda item: (str(item[1].get("created") or ""), item[0]))
        return entries

    def _store_envelope(self, agent: _Agent, envelope: Dict[str, Any]) -> None:
        record = {**envelope, "_retrieved_turn": None, "_spooled": _iso(self.wall())}
        self._spool_dir(agent.name).mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(self._spool_file(agent.name, envelope["id"]), record)

    def _tombstones(self, agent: _Agent) -> Dict[str, str]:
        data = _read_json(self._dir(agent.name) / "tombstones.json", {})
        return data if isinstance(data, dict) else {}

    def _prune_tombstones(self, agent: _Agent) -> Dict[str, str]:
        now = _iso(self.wall())
        data = {str(k): str(v) for k, v in self._tombstones(agent).items() if str(v) > now}
        _write_json(self._dir(agent.name) / "tombstones.json", data)
        return data

    def _pending_acks(self, agent: _Agent) -> List[Dict[str, Any]]:
        data = _read_json(self._dir(agent.name) / "pending_acks.json", [])
        if not isinstance(data, list):
            return []
        return [dict(item) for item in data if isinstance(item, dict)]

    def _write_pending_acks(self, agent: _Agent, items: List[Dict[str, Any]]) -> None:
        _write_json(self._dir(agent.name) / "pending_acks.json", items)

    def _agent(self, name: str) -> Optional[_Agent]:
        with self._lock:
            return self._agents.get(name)

    def agent_names(self) -> List[str]:
        """Known, not retired agents (retire removes them), sorted; the driver's pass order."""
        with self._lock:
            return sorted(self._agents)

    def agent_info(self, name: str) -> Optional[Dict[str, Any]]:
        """A copy of one agent's public state for the driver: address, spec, spec_version, epoch,
        session_id, ready, current_turn and the agent directory. None when unknown."""
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                return None
            return {"address": agent.address, "spec": dict(agent.spec), "spec_version": agent.spec_version,
                    "epoch": agent.epoch, "session_id": agent.session_id,
                    "sessions": {k: dict(v) for k, v in agent.sessions.items()},
                    "ready": agent.ready,
                    "current_turn": dict(agent.current_turn) if agent.current_turn else None,
                    "dir": str(self._dir(name))}

    def unread_ids(self, name: str) -> List[str]:
        """The ids `spool_pending` counts (same rule), sorted: the driver's progress check."""
        agent = self._agent(name)
        if agent is None:
            return []
        with self._lock:
            turn_id = agent.current_turn["turn"] if agent.current_turn else None
        return sorted(mail_id for mail_id, envelope in self._spool_entries(agent)
                      if envelope.get("_retrieved_turn") is None or envelope.get("_retrieved_turn") != turn_id)

    def _by_address(self, reference: Any) -> Optional[_Agent]:
        for agent in self._agents.values():
            if reference == agent.address or reference == agent.name:
                return agent
        return None

    # --- the hub stream ------------------------------------------------------------------------

    def attach(self, rfile: Any, wfile: Any) -> None:
        """Serves one relay stream; blocks until the stream ends. Called by the daemon's handler for
        the `relay` op. A new attach closes the previous stream first: the newest wins, so a hub relay
        restart that left a half-dead SSH behind cannot keep the node."""
        with self._lock:
            previous, self._stream = self._stream, _Stream(rfile, wfile)
            self._next_id = 0
            stream = self._stream
        if previous is not None:
            self._end_stream(previous, "stream_closed")
        self._ensure_dispatcher()
        stream.reader = threading.Thread(target=self._read_loop, args=(stream,), name="resident-relay",
                                         daemon=True)
        stream.reader.start()
        hello = self.request("hello", {"protocol": RELAY_PROTOCOL, "version": self._version(),
                                       "started": _iso(self.wall()),
                                       "session_query": True,
                                       "multi_session": True,
                                       # O2 (C5): whether this node may run open agents at all
                                       **resident_open.capability(self.sandbox_paths)})
        if not hello.get("ok"):
            self._end_stream(stream, "stream_closed")
            stream.reader.join(5.0)
            return
        sync = self.request("sync", {"agents": self._sync_payload()})
        if not sync.get("ok"):
            self._end_stream(stream, "stream_closed")
            stream.reader.join(5.0)
            return
        self._confirm_pending_acks()  # the hub applied every pending acknowledgement
        stream.reader.join()          # EOF, 90 s of silence, or a newer attach ends it

    def _version(self) -> str:
        version = getattr(self.worker, "STARTED_VERSION", None)  # as the daemon's ping reports it
        return version if isinstance(version, str) and version else code_version()

    def request(self, t: str, fields: Dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> Dict[str, Any]:
        """One request on the hub stream; returns the reply without its `t`/`re` fields. Callable from
        any thread except the stream reader thread. No stream -> hub_unreachable; no reply in time ->
        timeout (the stream stays); a line over the frame limit -> too_large."""
        with self._lock:
            stream = self._stream
            if stream is None or not stream.alive or self._stopping:
                return {"ok": False, "error": "hub_unreachable"}
            self._next_id += 1
            request_id = f"d{self._next_id}"
        line = (json.dumps({"t": t, "id": request_id, **fields}, ensure_ascii=False) + "\n").encode("utf-8")
        if len(line) > MAX_FRAME_BYTES:
            return {"ok": False, "error": "too_large"}
        entry: Dict[str, Any] = {"event": threading.Event(), "reply": None}
        with stream.pending_lock:
            stream.pending[request_id] = entry
        try:
            with stream.write_lock:
                if not stream.alive:
                    raise OSError("stream closed")
                stream.wfile.write(line)
                stream.wfile.flush()
        except (OSError, ValueError):
            self._end_stream(stream, "stream_closed")
            return {"ok": False, "error": "hub_unreachable"}
        deadline = self.clock() + timeout
        while True:
            if entry["event"].wait(POLL_SECONDS):
                reply = entry["reply"]
                return reply if isinstance(reply, dict) else {"ok": False, "error": "hub_unreachable"}
            if self.clock() >= deadline:
                with stream.pending_lock:
                    if stream.pending.pop(request_id, None) is None:
                        continue  # resolved between the wait and this pop; its event is set by now
                return {"ok": False, "error": "timeout"}

    def _read_loop(self, stream: _Stream) -> None:
        """The reader thread: hub frames in, replies and hub requests out. Only this thread reads the
        raw fd (its own buffer), so a line that arrived with an earlier one is never missed, and only
        this thread closes the stream's files."""
        stop = "silence"
        buffer = b""
        try:
            fd = stream.rfile.fileno()
        except (OSError, ValueError):
            fd, stop = -1, "stream_closed"
        deadline = self.clock() + SILENCE_SECONDS  # reset on every chunk received from the hub
        while fd >= 0 and stream.alive and stop == "silence":
            left = deadline - self.clock()
            if left <= 0:
                break
            try:
                readable, _w, _e = select.select([fd], [], [], min(POLL_SECONDS, left))
            except (OSError, ValueError):
                stop = "stream_closed"
                break
            if not readable:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                stop = "stream_closed"
                break
            if not chunk:
                stop = "eof"
                break
            deadline = self.clock() + SILENCE_SECONDS
            buffer += chunk
            if len(buffer) > MAX_FRAME_BYTES and b"\n" not in buffer:
                stop = "frame_too_large"
                break
            while stop == "silence" and b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if len(raw) > MAX_FRAME_BYTES:
                    stop = "frame_too_large"
                    break
                if not raw.strip():
                    continue
                try:
                    frame = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    stop = "unreadable_frame"
                    break
                if not self._dispatch(stream, frame):
                    stop = "stream_closed"
                    break
        try:
            self._end_stream(stream, stop if stream.alive else "stream_closed")
        finally:
            with stream.write_lock:  # a request thread checks `alive` under this lock before writing
                _close_quietly(stream.wfile)
            _close_quietly(stream.rfile)

    def _dispatch(self, stream: _Stream, frame: Any) -> bool:
        """One decoded line: a reply is routed to its waiter, anything else is a hub request."""
        if not isinstance(frame, dict):
            self._send(stream, {"t": "reply", "re": None, "ok": False, "error": "unknown_frame"})
            return True
        if frame.get("t") == "reply":
            stream.resolve(frame.get("re"), {k: v for k, v in frame.items() if k not in ("t", "re")})
            return True
        self._hub_frame(stream, frame)
        return stream.alive

    def _hub_frame(self, stream: _Stream, frame: Dict[str, Any]) -> None:
        """One hub request, handled on the reader thread (disk work only) and answered at once."""
        kind = frame.get("t")
        if kind == "ping":
            reply: Dict[str, Any] = {"ok": True}
        elif kind == "agents":
            reply = self._apply_agents(frame.get("version"), frame.get("agents"))
        elif kind == "deliver":
            reply = self._deliver(frame.get("agent"), frame.get("envelope"))
        elif kind == "query":
            reply = self._accept_query(frame)
        elif kind == "query_cancel":
            reply = self._cancel_query(frame)
        elif kind == "session_reset":
            reply = self._reset_session(frame)
        elif kind in ("task_state", "tasks"):  # the resident-task frames of M2e
            reply = self._task_frame(frame)
        else:
            reply = {"ok": False, "error": "unknown_frame"}
        self._send(stream, {"t": "reply", "re": frame.get("id"), **reply})

    def _send(self, stream: _Stream, frame: Dict[str, Any]) -> None:
        line = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        if len(line) > MAX_FRAME_BYTES:
            line = (json.dumps({"t": "reply", "re": frame.get("re"), "ok": False, "error": "too_large"},
                               ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with stream.write_lock:
                if not stream.alive:
                    return
                stream.wfile.write(line)
                stream.wfile.flush()
        except (OSError, ValueError):
            self._end_stream(stream, "stream_closed")

    def _end_stream(self, stream: _Stream, reason: str) -> None:
        """Ends the stream: nothing more is read or written, and every request in flight on it fails
        with `reason`. Idempotent; the files are closed by the reader thread."""
        with self._lock:
            stream.alive = False
            if self._stream is stream:
                self._stream = None
        stream.fail_all(reason)

    def _sync_payload(self) -> Dict[str, Any]:
        """Per known agent: epoch, whether its incarnation is bound (ready), spool ids, tombstone
        ids and the acknowledgements the hub has not confirmed (the design's "Resend on reconnect"
        handshake)."""
        with self._lock:
            known = list(self._agents.values())
        agents: Dict[str, Any] = {}
        for agent in known:
            agents[agent.address] = {
                "epoch": agent.epoch,
                "ready": bool(agent.ready),
                "spool": [mail_id for mail_id, _envelope in self._spool_entries(agent)],
                "tombstones": sorted(self._prune_tombstones(agent)),
                "pending_acks": self._pending_acks(agent),
            }
        return agents

    def _confirm_pending_acks(self) -> None:
        with self._lock:
            known = list(self._agents.values())
        for agent in known:
            if self._pending_acks(agent):
                self._write_pending_acks(agent, [])

    # --- hub frames: agents and deliver --------------------------------------------------------

    def _apply_agents(self, version: Any, specs: Any) -> Dict[str, Any]:
        """Brings the agent directories in line with the hub's list: create, update, retire (removal
        steps 4-5 on the node). The change is handed to the driver on the dispatcher thread."""
        added: List[str] = []
        updated: List[str] = []
        removed: List[str] = []
        incoming: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        if isinstance(specs, list):
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                address = spec.get("address")
                name = address.split("@", 1)[0] if isinstance(address, str) and "@" in address else ""
                if not NAME_RE.fullmatch(name):
                    continue
                incoming[name] = (address, spec)
        with self._lock:
            for name, (address, spec) in incoming.items():
                current = self._agents.get(name)
                if current is None:
                    agent = _Agent(name, address, spec, version)
                    self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self._dir(name).mkdir(mode=0o700, exist_ok=True)
                    self._agents[name] = agent
                    self._write_state(agent)
                    added.append(name)
                elif (current.address, current.spec, current.spec_version) != (address, spec, version):
                    current.address, current.spec, current.spec_version = address, spec, version
                    self._write_state(current)
                    updated.append(name)
            sandbox_stops: List[str] = []
            for name, agent in list(self._agents.items()):
                if name not in incoming:
                    self._retire(agent)
                    removed.append(name)
                    if _is_open(agent.spec):
                        sandbox_stops.append(name)
        for name in sandbox_stops:
            # Outside the manager lock: --stop waits up to 30 s for the sandbox units to end (C4),
            # and the lock must never sit across that.
            resident_open.stop(name, self.sandbox_paths)
        if added or updated or removed:
            self._post(lambda: self.driver.agents_changed(added, updated, removed))
        return {"ok": True, "applied": version}

    def _retire(self, agent: _Agent) -> None:
        open_agent = _is_open(agent.spec)
        agent.ready = False
        agent.current_turn = None
        if agent.listener is not None:
            agent.listener.close()
            agent.listener = None
        self.worker.release_resident_slot(agent.address)
        self._agents.pop(agent.name, None)
        self._task_tables.pop(agent.name, None)  # tasks.json and the call logs go with the directory
        if open_agent:
            # C7: the daemon's own sockets and MCP copies in the sandbox sock directory go by
            # lstat+unlink; the directory itself and /home/gsa-<name> are never touched (the
            # caller stops the sandbox units outside the lock)
            resident_open.clear_sock_dir(agent.name, self.sandbox_paths)
        shutil.rmtree(self._dir(agent.name), ignore_errors=True)

    def _deliver(self, reference: Any, envelope: Any) -> Dict[str, Any]:
        with self._lock:
            agent = self._by_address(reference) if isinstance(reference, str) else None
            if agent is None:
                return {"ok": True, "result": "unknown_agent"}
            if not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str) \
                    or not MAIL_ID_RE.fullmatch(envelope["id"]):
                return {"ok": False, "error": "invalid envelope"}
            if envelope["id"] in self._prune_tombstones(agent):
                return {"ok": True, "result": "tombstoned"}
            if self._spool_file(agent.name, envelope["id"]).exists():
                return {"ok": True, "result": "duplicate"}
            self._store_envelope(agent, envelope)
            name = agent.name
        self._post(lambda: self.driver.wake(name))
        return {"ok": True, "result": "spooled"}

    # --- hub frames: direct active-session queries (resident-session-query.md, Q2) ------------

    def _queries_dir(self, name: str) -> Path:
        return self._dir(name) / "queries"

    def _query_file(self, name: str, query_id: str) -> Path:
        return self._queries_dir(name) / f"{query_id}.json"

    def _clear_queries(self, name: str) -> None:
        qdir = self._queries_dir(name)
        if qdir.is_dir():
            shutil.rmtree(qdir, ignore_errors=True)

    def _accept_query(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        qid = frame.get("query_id")
        if not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid):
            return {"ok": False, "error": "invalid_query_id"}
        with self._lock:
            agent = self._by_address(frame.get("agent")) if isinstance(frame.get("agent"), str) else None
            if agent is None:
                return {"ok": False, "error": "unknown_agent"}
            if not agent.ready:
                return {"ok": False, "error": "not_ready"}
            if frame.get("epoch") != agent.epoch:
                return {"ok": False, "error": "epoch_mismatch"}
            skey = str(frame.get("session_key") or "main")
            create_if_missing = bool(frame.get("create_if_missing"))
            expected_sid = frame.get("expected_session_id")
            expected_sid_str = expected_sid if isinstance(expected_sid, str) else ""
            if skey == "main":
                if not expected_sid_str or agent.session_id != expected_sid_str:
                    return {"ok": False, "error": "no_active_session"}
            else:
                if not agent.session_id:
                    return {"ok": False, "error": "no_active_session"}
                if not create_if_missing:
                    if not expected_sid_str:
                        return {"ok": False, "error": "no_active_session"}
                    local_entry = agent.sessions.get(skey)
                    if local_entry and local_entry.get("session_id") and local_entry["session_id"] != expected_sid_str:
                        return {"ok": False, "error": "no_active_session"}
            qfile = self._query_file(agent.name, qid)
            if qfile.exists():
                return {"ok": True, "result": "duplicate"}
            qdir = self._queries_dir(agent.name)
            qdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            existing = list(qdir.glob("q-*.json"))
            if len(existing) >= MAX_LOCAL_QUERIES:
                return {"ok": False, "error": "queue_full"}
            payload = {
                "query_id": qid,
                "agent": agent.address,
                "epoch": agent.epoch,
                "session_key": skey,
                "expected_session_id": expected_sid_str,
                "create_if_missing": create_if_missing,
                "project": str(frame.get("project") or ""),
                "project_kind": str(frame.get("project_kind") or "code"),
                "task": frame.get("task"),
                "caller": str(frame.get("caller") or "operator"),
                "caller_kind": str(frame.get("caller_kind") or "operator"),
                "mode": str(frame.get("mode") or "in_place"),
                "prompt": str(frame.get("prompt") or ""),
                "timeout_sec": int(frame.get("timeout_sec") or 180),
                "expires": str(frame.get("expires") or ""),
                "received": _iso(self.wall()),
            }
            _write_json(qfile, payload)
            name = agent.name
        self._post(lambda: self.driver.wake(name))
        return {"ok": True, "result": "accepted"}

    def _reset_session(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        skey = frame.get("session_key")
        if not isinstance(skey, str) or not skey or skey == "main":
            return {"ok": True}
        with self._lock:
            agent = self._by_address(frame.get("agent")) if isinstance(frame.get("agent"), str) else None
            if agent is not None and (frame.get("epoch") is None or frame.get("epoch") == agent.epoch):
                if agent.sessions.pop(skey, None) is not None:
                    self._write_state(agent)
        return {"ok": True}

    def clear_scoped_session(self, name: str, session_key: str) -> None:
        if not session_key or session_key == "main":
            return
        with self._lock:
            agent = self._agents.get(name)
            if agent is not None and agent.sessions.pop(session_key, None) is not None:
                self._write_state(agent)

    def _cancel_query(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        qid = frame.get("query_id")
        if not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid):
            return {"ok": True}
        with self._lock:
            agent = self._by_address(frame.get("agent")) if isinstance(frame.get("agent"), str) else None
            if agent is not None:
                try:
                    self._query_file(agent.name, qid).unlink()
                except OSError:
                    pass
        return {"ok": True}

    def pending_queries(self, name: str) -> List[Dict[str, Any]]:
        """Non-expired queued direct queries for `name`, FIFO by received/query_id."""
        with self._lock:
            qdir = self._queries_dir(name)
            if not qdir.is_dir():
                return []
            now = self.wall()
            items: List[Dict[str, Any]] = []
            for path in sorted(qdir.glob("q-*.json")):
                data = _read_json(path, None)
                if not isinstance(data, dict) or not isinstance(data.get("query_id"), str):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                exp = _parse_iso(data.get("expires"))
                if exp is not None and exp <= now:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                items.append(data)
            items.sort(key=lambda item: (str(item.get("received") or ""), str(item.get("query_id") or "")))
            return items

    def drop_query(self, name: str, query_id: str) -> None:
        if not isinstance(query_id, str) or not QUERY_ID_RE.fullmatch(query_id):
            return
        with self._lock:
            try:
                self._query_file(name, query_id).unlink()
            except OSError:
                pass

    # --- hub frames: resident tasks (M2e) ------------------------------------------------------

    def _task_table(self, name: str) -> Optional["resident_tasks.TaskTable"]:
        """The agent's owned-task table, loaded once and cached; retirement drops it with the dir.
        Every task that later leaves the table (update, release, a `tasks` frame) loses its worktree
        bundle (O3) through the drop callback."""
        with self._lock:
            table = self._task_tables.get(name)
            if table is None:
                if name not in self._agents:
                    return None
                table = resident_tasks.TaskTable.load(self._dir(name), on_drop=self._drop_bundle(name))
                self._task_tables[name] = table
            return table

    def _drop_bundle(self, name: str) -> Callable[[str], None]:
        """The table's drop callback (O3): unlinks the task's bundle in the sandbox socket directory
        after the usual lstat check; a closed agent's path simply does not exist."""
        paths = self.sandbox_paths
        return lambda task: resident_open.remove_task_bundle(name, task, paths)

    def _task_frame(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """One resident-task frame (task_state or tasks), applied to the agent's owned-task table."""
        with self._lock:
            agent = self._by_address(frame.get("agent")) if isinstance(frame.get("agent"), str) else None
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        table = self._task_table(agent.name)
        problem = resident_tasks.apply_frame(frame.get("t"), table, frame) if table else "unknown_agent"
        return {"ok": True} if problem is None else {"ok": False, "error": problem}

    def _attach_calls(self, name: str, fields: Dict[str, Any]) -> None:
        """The daemon (not the agent) attaches the task's call log to a task_update, dropped when the
        frame would not fit so a big log never blocks the update. The probe measures the whole frame,
        a patch the mailbox MCP collected (O3) included, so the log is what gives way - never the
        patch."""
        args, task = fields["args"], fields["args"].get("task")
        if isinstance(task, str) and task:
            args["calls"] = resident_tasks.read_calls(self._dir(name), task)
            probe = json.dumps({"t": "call", "id": "d999999999", **fields}, ensure_ascii=False).encode("utf-8")
            if len(probe) > MAX_FRAME_BYTES - 64 * 1024:
                args["calls"] = {"records": [], "dropped": True}

    def _task_worktree(self, agent: _Agent, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """task_worktree (O3): a git bundle of the owned task's snapshot, written into the agent's
        sandbox socket directory for the sandboxed mailbox MCP to clone its worktree from. Answered
        on the node like the repo tools; nothing about it is forwarded to the hub."""
        if not _is_open(agent.spec):
            return {"ok": False, "error": "not_open"}
        table = self._task_table(name)
        if table is None:
            return {"ok": False, "error": "unknown_agent"}
        problem, entry = resident_tasks.owned_entry(table, args.get("task"), args.get("generation"))
        if problem is not None:
            return {"ok": False, "error": problem}
        assert entry is not None
        if entry["state"] == "none":
            return {"ok": False, "error": "no_snapshot"}  # the task names no project
        router = self._repo or resident_tasks.RepoRouter()
        self._repo = router
        stored = router.stored_snapshot(entry.get("key"))
        if stored is None:
            return {"ok": False, "error": "no_snapshot"}
        git_dir, commit = stored
        try:
            bundle = resident_open.write_task_bundle(name, args["task"], git_dir, commit,
                                                     self.sandbox_paths)
        except OSError:
            bundle = None  # a missing or unwritable socket directory; the call is refused
        if bundle is None:
            return {"ok": False, "error": "bundle_failed"}
        return {"ok": True, "bundle": str(bundle), "commit": commit}

    def _drop_task(self, name: str, task: Any) -> None:
        if isinstance(task, str):
            table = self._task_table(name)
            if table is not None:
                table.drop(task)

    # --- dispatcher ----------------------------------------------------------------------------

    def _ensure_dispatcher(self) -> None:
        """One dispatcher thread: driver callbacks never run on the reader thread (a callback may call
        request(), which needs the reader)."""
        with self._lock:
            if self._dispatch_queue is not None or self._stopping:
                return
            self._dispatch_queue = queue.Queue()
            threading.Thread(target=self._dispatch_loop, name="resident-dispatch", daemon=True).start()

    def _post(self, callback: Callable[[], None]) -> None:
        if self._dispatch_queue is not None and not self._stopping:
            self._dispatch_queue.put(callback)

    def _dispatch_loop(self) -> None:
        assert self._dispatch_queue is not None
        while True:
            item = self._dispatch_queue.get()
            if item is None:
                return
            try:
                item()
            except Exception:  # noqa: BLE001 - a driver callback must not end the dispatcher
                pass

    # --- binding and turns (the driver's API) --------------------------------------------------

    def begin_incarnation(self, name: str) -> Dict[str, Any]:
        """bind_begin on the hub, then this incarnation's socket; closes the previous incarnation
        (its listener, every connection accepted on it, its socket file)."""
        agent = self._agent(name)
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        reply = self.request("bind_begin", {"agent": agent.address})
        if not reply.get("ok"):
            return reply
        epoch = reply.get("epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            return {"ok": False, "error": "hub gave no epoch"}
        abandoned = False
        with self._lock:
            open_agent = _is_open(agent.spec)
            if open_agent:
                # C7: an open agent's incarnation socket sits in the sandbox sock directory
                # (node-owned 2750, group gsa-<name>), so the sandboxed MCP servers can reach it
                directory = self.sandbox_paths.sock_dir(name)
            else:
                directory = self._dir(name)
            path = directory / f"e{epoch}.sock"
            if len(str(path).encode("utf-8")) > SOCKET_PATH_MAX:
                return {"ok": False,
                        "error": f"agent socket path too long ({len(str(path))} characters): {path}"}
            try:
                listener = _AgentListener(self, name, epoch, path)
                listener.start(umask=0o117 if open_agent else 0o177)
            except OSError as exc:
                return {"ok": False, "error": f"cannot open the agent socket: {exc}"}
            if open_agent:
                resident_open.remove_stale_sockets(directory, keep=path)
            previous = agent.listener
            agent.listener = listener
            agent.epoch = epoch
            agent.ready = False
            agent.session_id = None
            agent.sessions.clear()
            self._clear_queries(name)
            if agent.current_turn is not None:
                # a turn of the ended incarnation can never end normally: its retrieved mail is
                # offered again, and the resident slot it held goes free (the hub fences its turn_end)
                self._offer_again(agent, agent.current_turn["turn"])
                agent.current_turn = None
                abandoned = True
            self._write_state(agent)
        if abandoned:
            self.worker.release_resident_slot(agent.address)
        if previous is not None:
            previous.close()
        return {"ok": True, "epoch": epoch, "socket": str(path)}

    def complete_bind(self, name: str, epoch: int, session_id: Optional[str] = None, pid: Optional[int] = None,
                      pane: Optional[str] = None) -> Dict[str, Any]:
        agent = self._agent(name)
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        reply = self.request("bind_complete", {"agent": agent.address, "epoch": epoch, "session_id": session_id,
                                               "pid": pid, "pane": pane})
        if reply.get("ok"):
            with self._lock:
                if isinstance(session_id, str) and session_id:
                    agent.session_id = session_id
                agent.ready = True
                self._write_state(agent)
        return reply

    def report_resume(self, name: str, epoch: int, session_id: str) -> Dict[str, Any]:
        agent = self._agent(name)
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        return self.request("resume_report", {"agent": agent.address, "epoch": epoch,
                                              "session_id": session_id})

    def admit(self, name: str, kind: str, query_id: Optional[str] = None) -> Dict[str, Any]:
        """The node's resident slot first (D5), then the hub's admission. A busy slot is refused
        without asking the hub; a hub refusal releases the slot again."""
        agent = self._agent(name)
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        if not self.worker.take_resident_slot(agent.address):
            return {"ok": False, "error": "slot_busy"}
        payload: Dict[str, Any] = {"agent": agent.address, "epoch": agent.epoch, "kind": kind}
        if query_id:
            payload["query_id"] = query_id
        reply = self.request("admit", payload)
        if not reply.get("ok"):
            self.worker.release_resident_slot(agent.address)
            return reply
        with self._lock:
            agent.current_turn = {"turn": reply.get("turn"), "epoch": agent.epoch}
        return {"ok": True, "turn": reply.get("turn")}

    def end_turn(self, name: str, turn: Any, end_reason: str, session_id: Optional[str] = None,
                 query_id: Optional[str] = None,
                 query_reply: Optional[Dict[str, Any]] = None,
                 session_key: Optional[str] = None) -> Dict[str, Any]:
        """Ends the agent's current turn. A normal end acknowledges the mail retrieved in it (spool
        files deleted, ids tombstoned); any other end offers that mail again, marked redelivered. The
        end is written to pending_acks first and stays there until the hub has answered."""
        agent = self._agent(name)
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        acked: List[str] = []
        with self._lock:
            if query_id:
                try:
                    self._query_file(name, query_id).unlink()
                except OSError:
                    pass
            current = agent.current_turn
            if current is None:
                return {"ok": False, "error": "no_turn"}
            if current["epoch"] != agent.epoch:
                # a turn from an older epoch: nothing is acknowledged (an acknowledgement of an ended
                # incarnation matches nothing on the hub either), the mail it retrieved is offered
                # again, the slot goes free, and no turn_end is sent
                agent.current_turn = None
                self._offer_again(agent, current["turn"])
                stale = True
            elif turn is not None and current["turn"] != turn:
                return {"ok": False, "error": "wrong_turn"}
            else:
                stale = False
                agent.current_turn = None
                if end_reason == "normal":
                    acked = self._acknowledge(agent, current["turn"])
                if end_reason != "normal":
                    self._offer_again(agent, current["turn"])
        if stale:
            self.worker.release_resident_slot(agent.address)
            return {"ok": False, "error": "wrong_epoch"}
        record: Dict[str, Any] = {"epoch": current["epoch"], "turn": current["turn"],
                                  "end_reason": end_reason, "acked": acked}
        if session_id:
            record["session_id"] = session_id
            with self._lock:
                if not session_key or session_key == "main":
                    if not agent.session_id:
                        agent.session_id = session_id
                        self._write_state(agent)
                else:
                    agent.sessions[session_key] = {
                        "session_id": session_id,
                        "first_turn": False,
                        "last_used": _iso(self.wall()),
                    }
                    self._write_state(agent)
        if session_key and session_key != "main":
            record["session_key"] = session_key
        if query_id:
            record["query_id"] = query_id
        if query_reply is not None:
            record["query_reply"] = query_reply
        self._write_pending_acks(agent, self._pending_acks(agent) + [record])
        try:
            reply = self.request("turn_end", {"agent": agent.address, **record})
        finally:
            self.worker.release_resident_slot(agent.address)
        if reply.get("ok"):
            self._write_pending_acks(agent, [item for item in self._pending_acks(agent) if item != record])
        return reply

    def agent_exit(self, name: str, epoch: int, reason: str) -> Dict[str, Any]:
        agent = self._agent(name)
        if agent is None:
            return {"ok": False, "error": "unknown_agent"}
        reply = self.request("agent_exit", {"agent": agent.address, "epoch": epoch, "reason": reason})
        with self._lock:
            agent.ready = False
            if agent.epoch == epoch and agent.current_turn is not None:
                # the incarnation died inside a turn: its retrieved mail goes back to unread
                self._offer_again(agent, agent.current_turn["turn"])
                agent.current_turn = None
        self.worker.release_resident_slot(agent.address)
        return reply

    def _acknowledge(self, agent: _Agent, turn_id: Any) -> List[str]:
        """Deletes the envelopes retrieved in the turn and tombstones their ids until each message's
        `expires` (delivery step 7)."""
        acked: List[str] = []
        tombstones = self._prune_tombstones(agent)
        for mail_id, envelope in self._spool_entries(agent):
            if envelope.get("_retrieved_turn") != turn_id:
                continue
            acked.append(mail_id)
            if isinstance(envelope.get("expires"), str) and envelope["expires"]:
                tombstones[mail_id] = envelope["expires"]
            try:
                self._spool_file(agent.name, mail_id).unlink()
            except OSError:
                pass
        if acked:
            _write_json(self._dir(agent.name) / "tombstones.json", tombstones)
        return acked

    def _offer_again(self, agent: _Agent, turn_id: Any) -> None:
        """A turn that did not end normally: its retrieved mail goes back to unread, marked
        redelivered (the `_spooled` timestamp stays, so the sweep still sees its age)."""
        for mail_id, envelope in self._spool_entries(agent):
            if envelope.get("_retrieved_turn") != turn_id:
                continue
            record = {**envelope, "_retrieved_turn": None, "redelivered": True}
            _write_json(self._spool_file(agent.name, mail_id), record)

    def spool_pending(self, name: str) -> int:
        """Unread envelopes in the agent's spool (for the driver's wake)."""
        agent = self._agent(name)
        if agent is None:
            return 0
        with self._lock:
            turn_id = agent.current_turn["turn"] if agent.current_turn else None
        return sum(1 for _mid, envelope in self._spool_entries(agent)
                   if envelope.get("_retrieved_turn") is None or envelope.get("_retrieved_turn") != turn_id)

    def oldest_unread_age(self, name: str) -> float:
        """Seconds since the oldest unread envelope was spooled, or -1.0 when none (the 30 s sweep)."""
        agent = self._agent(name)
        if agent is None:
            return -1.0
        with self._lock:
            turn_id = agent.current_turn["turn"] if agent.current_turn else None
        oldest: Optional[float] = None
        for _mid, envelope in self._spool_entries(agent):
            if not (envelope.get("_retrieved_turn") is None or envelope.get("_retrieved_turn") != turn_id):
                continue
            stamped = _parse_iso(envelope.get("_spooled"))
            if stamped is None:
                continue
            age = self.wall() - stamped
            oldest = age if oldest is None else min(oldest, age)
        return -1.0 if oldest is None else oldest

    # --- agent sockets ---------------------------------------------------------------------

    def _serve_agent_connection(self, name: str, epoch: int, conn: socket.socket) -> None:
        """One connection on an agent socket: one JSON line in, one line out. The agent and the epoch
        come from the listener; identity fields sent by the caller are dropped."""
        conn.settimeout(30.0)
        try:
            raw = _read_conn_line(conn)
            if raw is None:
                reply: Optional[Dict[str, Any]] = {"ok": False, "error": "too_large"}
            elif not raw.strip():
                reply = {"ok": False, "error": "unknown_tool"}
            else:
                try:
                    request = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    request = None
                reply = self._agent_call(name, epoch, request)
        except OSError:
            return
        if reply is None:
            return
        try:
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _agent_call(self, name: str, epoch: int, request: Any) -> Dict[str, Any]:
        if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
            return {"ok": False, "error": "unknown_tool"}
        tool = request["tool"]
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        args = {k: v for k, v in args.items() if k not in SPOOF_KEYS}
        with self._lock:
            agent = self._agents.get(name)
            if agent is None:
                return {"ok": False, "error": "unknown_agent"}
            if agent.epoch != epoch:
                return {"ok": False, "error": "wrong_epoch"}  # a dying incarnation's leftover socket
        if tool == "mail_inbox":
            return self._agent_inbox(agent, epoch)
        if tool in resident_tasks.REPO_TOOLS:  # answered on the node from the task's snapshot (M2e)
            table = self._task_table(name)
            if table is None:
                return {"ok": False, "error": "unknown_agent"}
            router = self._repo or resident_tasks.RepoRouter()
            self._repo = router
            return router.call(self._dir(name), table, tool, args)
        if tool == "task_worktree":  # answered on the node too (O3); closed agents get not_open
            return self._task_worktree(agent, name, args)
        if tool in OPEN_HUB_TOOLS:
            with self._lock:
                if not _is_open(agent.spec):
                    return {"ok": False, "error": "not_open"}
        elif tool not in HUB_TOOLS:
            return {"ok": False, "error": "unknown_tool"}
        op_id = request.get("op_id")
        if not isinstance(op_id, str) or not op_id:
            op_id = uuid.uuid4().hex
        with self._lock:
            turn = agent.current_turn["turn"] if (agent.current_turn is not None
                                                  and agent.current_turn["epoch"] == epoch) else None
        fields: Dict[str, Any] = {"agent": agent.address, "epoch": epoch, "turn": turn,
                                  "tool": tool, "args": args, "op_id": op_id}
        if tool == "task_update":
            self._attach_calls(name, fields)
        reply = self.request("call", fields)
        if tool in ("task_update", "task_release") and (  # an accepted end drops the task and its log
                reply.get("ok") or (tool == "task_update" and reply.get("error") in ("task_released", "not_owner"))):
            self._drop_task(name, args.get("task"))
        elif tool == "task_claim" and reply.get("ok"):
            table = self._task_table(name)
            if table is not None:
                table.claimed(args.get("task"), reply.get("generation"), reply.get("snapshot"), reply.get("key"))
        return reply

    def _agent_inbox(self, agent: _Agent, epoch: int) -> Dict[str, Any]:
        """mail_inbox, answered from the spool: the envelopes not yet retrieved in this turn, oldest
        first, local fields stripped. An envelope retrieved in an earlier turn is offered again and
        marked redelivered."""
        with self._lock:
            current = agent.current_turn
            if current is None or current["epoch"] != epoch:
                return {"ok": False, "error": "no_turn"}
            turn_id = current["turn"]
            records: List[Tuple[str, Dict[str, Any]]] = []
            used = 0
            more = False
            for mail_id, envelope in self._spool_entries(agent):
                stamp = envelope.get("_retrieved_turn")
                if stamp is not None and stamp == turn_id:
                    continue
                record = {**envelope, "_retrieved_turn": turn_id}
                if stamp is not None:  # retrieved in an earlier turn: offered again
                    record["redelivered"] = True
                # The reply is one line of at most 1 MiB: mail that does not fit stays unread for the
                # next call, so nothing is stamped (and later acknowledged) that the agent never saw.
                size = len(json.dumps({k: v for k, v in record.items() if not k.startswith("_")},
                                      ensure_ascii=False).encode("utf-8")) + 2
                if records and used + size > INBOX_REPLY_BUDGET:
                    more = True
                    break
                used += size
                records.append((mail_id, record))
                _write_json(self._spool_file(agent.name, mail_id), record)
        # A failed `retrieved` does not fail the tool call: the ids are still acknowledged at the
        # turn's normal end, and the hub accepts an acknowledgement for mail it recorded as retrieved
        # or delivered.
        self.request("retrieved", {"agent": agent.address, "epoch": epoch, "turn": turn_id,
                                   "ids": [mail_id for mail_id, _record in records]})
        messages = [{k: v for k, v in record.items() if not k.startswith("_")}
                    for _mail_id, record in records]
        return {"ok": True, "messages": messages, "more": more}

    def shutdown(self) -> None:
        """Closes every listener, every agent connection and the stream; stops the dispatcher."""
        stop_driver = getattr(self.driver, "stop", None)
        if callable(stop_driver):
            # Before the listeners close and never under the manager lock: a turn thread that is
            # still inside end_turn needs both to answer its request.
            stop_driver()
        with self._lock:
            self._stopping = True
            agents = list(self._agents.values())
            stream = self._stream
            dispatch = self._dispatch_queue
        for agent in agents:
            if agent.listener is not None:
                agent.listener.close()
                agent.listener = None
        if stream is not None:
            self._end_stream(stream, "shutdown")
        if dispatch is not None:
            dispatch.put(None)


def _read_conn_line(conn: socket.socket) -> Optional[bytes]:
    """One line (without the newline) from an agent-socket connection; None when the line is over
    the 1 MiB limit."""
    buffer = b""
    while b"\n" not in buffer:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > MAX_FRAME_BYTES:
            return None
    return buffer.split(b"\n", 1)[0]

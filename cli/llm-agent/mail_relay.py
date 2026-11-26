"""The hub relay of the resident agents (docs/design/resident-agents.md, M2): one NDJSON frame
stream per node over `ssh <node> python3 ~/.guardian/worker/worker.py relay`, run by
`guardian.py --mail-relay` (the hub unit `guardian-mail-relay.service`).

Frames (one JSON object per line, UTF-8, at most 1 MiB): a request is {"t": TYPE, "id": ID, ...},
its answer {"t": "reply", "re": ID, "ok": true/false, ...}. Ids are h<n> from the hub, d<n> from
the daemon; replies come in any order; a request with no reply for 20 s is dropped. Handshake: the
daemon sends hello (protocol 3) then sync (per agent: epoch, ready, spool ids, tombstone ids,
pending_acks [{epoch, turn, end_reason, acked, session_id?}]); the hub answers, pushes an `agents`
frame (the declaration for this node) and the reconcile `deliver` frames, oldest first per agent.
After that: ping every 30 s, a 2 s poll (declaration mtime, queued mail, bind timeouts, stale
releases), deliver answers spooled/duplicate/tombstoned/unknown_agent, and the daemon requests
bind_begin, bind_complete, resume_report, admit, retrieved, turn_end, call, agent_exit. No frame
for 90 s closes the stream and marks the node's agents down; the runner reconnects after 5, 10,
30, then every 60 s (reset after a session that completed the handshake). With no declared agents
the relay opens no stream and creates no store file.

A task that names a project is pinned to its commit (M2e): the claim answers `snapshot` none/ready/
pending, the hub ensures the W2 snapshot in the background (mail_snapshots, its own ssh call, never
this stream) and then sends `task_state` per waiting claim; after sync one `tasks` frame per agent
carries its whole in_progress set. The daemon's `task_update` may carry `calls` (the task's repo
tool log), which is filtered against the snapshot and stored with the result.

An agent declared `tools: "open"` runs its turns inside the node sandbox (resident-open-tools.md
O2). The daemon's hello reports `open_tools` and its installed `sandbox_version` (C5); with them
the hub decides each open entry before every agents frame: a daemon that reported neither never
sees the agent at all (it is left out of the frame, down `node_without_open_tools`); a daemon whose
recorded probe verdict is missing or failed (`sandbox_unverified`), recorded for another sandbox
version (`sandbox_stale`) or whose installed version differs from this release's
(`sandbox_version_mismatch`) gets the spec with `blocked` set and the agent down (C6). The decision
is part of the frame signature, so a re-probed node sends a new frame with no declaration change.

A `task_update` of an open agent may carry a patch (O3, rule 5): `patch` (the `git diff --cached
--binary --no-renames` of the worktree the snapshot built in the sandbox) and `patch_base` (the
commit that worktree started at). Both are untrusted. The relay refuses them unless the agent is
declared open, the task names a project and the status is `completed`, converts the patch to
apply_patch edits (task_patch.convert) and records ONE action `jev_mcp:apply_patch` as PROPOSED in
the existing approvals path — never auto-applied (the structural rule lives in action_ops), never
HELD, no jev gate. The action is keyed on the task row, so a replayed op id or a retry after a
crash creates at most one; rows the crash left without their action are reconciled at start.
"""

import json
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import mail_relay_store
import mail_snapshots
import mail_store
import resident_cards
import resident_registry
import sandbox_info
import task_patch

PROTOCOL = 3
MAX_LINE_BYTES = 1024 * 1024
RELAY_REMOTE_COMMAND = "python3 ~/.guardian/worker/worker.py relay"
# Caller fields never trusted from a tool call's args (identity comes from the frame's agent).
TOOL_DROP_KEYS = ("from", "from_kind", "hop", "epoch", "sender", "creator", "caller")
# The open-tool decisions (O2 shared contract C6): the reason a daemon without the hello fields is
# left out entirely, and the three blocked reasons a daemon that knows open tools can carry.
NODE_WITHOUT_OPEN_TOOLS = "node_without_open_tools"
SANDBOX_UNVERIFIED = "sandbox_unverified"
SANDBOX_STALE = "sandbox_stale"
SANDBOX_VERSION_MISMATCH = "sandbox_version_mismatch"
_SANDBOX_VERSION_UNSET = object()  # the cached version may legitimately be None (unreadable tree)


def log(message: str) -> None:
    """One line per state change on stderr; no secrets and no mail bodies."""
    print(f"mail-relay: {message}", file=sys.stderr, flush=True)


def relay_argv(node: str) -> List[str]:
    """The ssh command that starts the relay stream; the node id is the inventory id, as
    remote_agents.py passes it to ssh."""
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=30", "--", node, RELAY_REMOTE_COMMAND]


def envelope(row: Dict[str, Any]) -> Dict[str, Any]:
    """The wire form of a mail row (the design's Envelope section, without the hub-only turn)."""
    return {"id": row["id"], "thread": row["thread"], "from": row["sender"],
            "from_kind": row["from_kind"], "to": row["recipient"], "kind": row["kind"],
            "subject": row["subject"], "body": row["body"], "task": row["task"],
            "reply_to": row["reply_to"], "hop": row["hop"], "created": row["created"],
            "expires": row["expires"], "redelivered": bool(row["redelivered"])}


def card_text(entry: Dict[str, Any]) -> str:
    """The start card of a fresh conversation (resident-agents.md, M2d): built by
    resident_cards.card_text from the declaration entry, pinned by golden/resident_cards.txt."""
    return resident_cards.card_text(entry)


def record_task_patch_action(store: mail_store.MailStore, db: Any, task_id: str, agent: str,
                             project: str, sha: str, converted: Dict[str, Any], result: Any,
                             base: Optional[str] = None) -> Optional[int]:
    """The ONE PROPOSED apply_patch action of a stored task patch (O3, OD4), linked on the task row.

    Keyed on the row: a task that already has its action id returns it and creates nothing, and a
    thread that already holds a PROPOSED apply_patch action (a crash between add_action and the row
    update) adopts that orphan instead of growing a duplicate. Returns the action id, or None when
    the task id cannot form a valid thread id (then logged as task_patch_failed)."""
    thread_id = task_patch.action_thread_id(task_id)
    if thread_id is None:
        store.note("task_patch_failed", agent=agent,
                   detail={"task": task_id, "error": f"task id {task_id!r} cannot form a thread id"})
        return None
    linked = store.task_patch_action(task_id)
    if linked is not None:
        return linked
    existing = [a for a in db.list_actions(thread_id, status="PROPOSED")
                if a["tool"] == task_patch.TOOL]
    if existing:
        action_id = existing[0]["id"]  # the orphan a crash left behind
    else:
        action_id = db.add_action(thread_id, 0, task_patch.TOOL, {"edits": converted["edits"]},
                                  task_patch.action_reason(task_id, agent, result), "PROPOSED",
                                  base_sha=sha, project_id=project)
    if not store.record_task_patch(task_id, action_id, converted["files"], converted["bytes"],
                                   base=base):
        settled = store.task_patch_action(task_id)
        return settled if settled is not None else action_id
    return action_id


def reconcile_task_patches(store: mail_store.MailStore, db_factory: Callable[[], Any]) -> List[int]:
    """The O3 crash window: every completed task row that carries a patch but no action gets its
    action now, re-converted from the stored patch (patch_base was not stored, so the row's pinned
    sha stands in for the 40-hex check). A conversion that no longer passes is logged as event
    `task_patch_failed` and the row is left without an action. Returns the action ids created."""
    created: List[int] = []
    db = None
    for row in store.pending_patch_tasks():
        try:
            converted = task_patch.convert(row["project"], row["sha"], row["patch"], row["sha"])
        except task_patch.TaskPatchError as exc:
            store.note("task_patch_failed", agent=row["owner"],
                       detail={"task": row["id"], "error": f"{exc.code}: {exc.message}"})
            continue
        db = db or db_factory()
        action_id = record_task_patch_action(store, db, row["id"], row["owner"] or "unknown",
                                             row["project"], row["sha"], converted, row["result"])
        if action_id is not None:
            created.append(action_id)
    return created


# The reconcile runs once per relay process (the first session to start), not once per session:
# two sessions of one process must not race over the same pending rows.
_reconcile_lock = threading.Lock()
_reconcile_done = False


def _reconcile_task_patches_once(store: mail_store.MailStore, db_factory: Callable[[], Any]) -> None:
    global _reconcile_done
    with _reconcile_lock:
        if _reconcile_done:
            return
        _reconcile_done = True
    pending = store.pending_patch_tasks()
    if pending:
        log(f"reconcile: {len(pending)} task patch(es) without an action")
        reconcile_task_patches(store, db_factory)


def agent_spec(entry: Dict[str, Any], table: Dict[str, Any],
               peers: Optional[Sequence[str]] = None, sandbox_version: Optional[str] = None,
               blocked: Optional[str] = None) -> Dict[str, Any]:
    """One entry of the `agents` frame. `provider_spec` is the M0 table's driver object as is
    (command, env, env_clear, settings, seed_files, ...): the node resolves the placeholders, the
    hub sends no secret. An entry declared `tools: "open"` adds `tools`, `sandbox` (this release's
    sandbox version, sandbox_info C1) and, when the relay will not let the node run it, `blocked`
    with one of the sandbox reasons; a closed entry's spec is exactly the M2 shape whatever the
    caller passes (C6)."""
    provider = (table.get("providers") or {}).get(entry.get("provider")) or {}
    driver = entry.get("driver") or provider.get("default_driver")
    driver_spec = (provider.get("drivers") or {}).get(driver) or {}
    if peers is None:
        peers = entry.get("peers") or []
    spec = {"address": entry["address"], "name": entry["name"], "provider": entry["provider"],
            "driver": driver, "peers": list(peers), "card": card_text(entry),
            "provider_spec": dict(driver_spec)}
    if entry.get("tools") == "open":
        spec["tools"] = "open"
        spec["sandbox"] = {"version": sandbox_version}
        if blocked:
            spec["blocked"] = blocked
    return spec


def effective_peers(entry: Dict[str, Any], entries: List[Dict[str, Any]]) -> List[str]:
    """The peer list of an agents-frame entry: the declaration's explicit list, or every other
    declared agent when it declares none (the store's implicit rule)."""
    peers = entry.get("peers")
    if peers is not None:
        return list(peers)
    return [other["address"] for other in entries if other.get("address") != entry.get("address")]


def _declaration_signature(entries: List[Dict[str, Any]]) -> str:
    """A canonical form of the entries, so a rewritten file with equal content sends no frame."""
    return json.dumps(sorted(entries, key=lambda e: e.get("address", "")), sort_keys=True,
                      ensure_ascii=False, default=str)


class RelaySession:
    """One stream to one node. A reader thread only parses lines into a queue; run() is the
    single thread that touches the store (one MailStore per session: sqlite connections are per
    thread). Tests run it over socket.socketpair() against a fake daemon."""

    REQUEST_TIMEOUT = 20.0
    POLL_INTERVAL = 2.0
    PING_INTERVAL = 30.0
    LIVENESS_TIMEOUT = 90.0
    WAKE_SECONDS = 0.05  # wall-clock wait for the next frame or tick (never the injected clock)

    def __init__(self, node: str, reader: Any, writer: Any, store: Optional[mail_store.MailStore] = None,
                 clock: Callable[[], float] = time.monotonic,
                 declarations: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                 ensurer: Any = None, db: Any = None):
        self.node = node
        self.reader, self.writer = reader, writer
        self.store = store if store is not None else mail_store.MailStore()
        self.clock = clock
        self._decl_loader = declarations
        self._ensurer = ensurer  # a SnapshotEnsurer, built lazily: no project task, none built
        self._db = db  # the decision DB of task patch actions, built lazily (tests inject one)
        self._decl_path = resident_registry.declaration_path()
        self._decl_mtime: Optional[int] = None
        try:
            self._table = resident_registry.load_table()
        except resident_registry.RegistrationError as exc:
            log(f"{node}: cannot read the resident provider table: {exc}")
            self._table = {}
        self._entries: List[Dict[str, Any]] = []
        self._signature: Optional[str] = None
        self._load_initial_declarations()
        self._frames: "queue.Queue" = queue.Queue()
        self._pending: Dict[str, Tuple[str, Dict[str, Any], float]] = {}
        self._inflight: set = set()
        self._inflight_queries: set = set()
        self._hello_session_query = False
        self._hello_multi_session = False
        self._next_id = 0
        self._agents_version = 0
        self._sent_signature: Optional[str] = None
        # Open tools (O2): the daemon's hello capabilities (C5), this release's sandbox version
        # (computed once, the release tree does not change under a running relay) and the open
        # entries left out of the last agents frame (a daemon that never saw them gets no deliver
        # or tasks frames for them either).
        self._hello_open_tools = False
        self._hello_sandbox_version: Optional[str] = None
        self._sandbox_version: Any = _SANDBOX_VERSION_UNSET
        self._omitted: set = set()
        # open agents the last agents frame left out or sent blocked, with the reason: the hub
        # refuses their bind_begin and admit even if a daemon ignored the frame (C6).
        self._held: Dict[str, str] = {}
        self._first_frame = True
        self._synced = False
        self.handshake_completed = False
        self._closed = False
        # Task snapshots (M2e): keys already told to the daemon, claims waiting for an ensure, and
        # the ensure threads in flight (all touched on the pump thread only).
        self._ready_keys: Dict[Tuple[str, int], str] = {}
        self._waiting: Dict[Tuple[str, str], Dict[str, int]] = {}
        self._ensuring: Dict[Tuple[str, str], threading.Thread] = {}
        self.close_reason: Optional[str] = None
        self._last_recv = self._last_ping = self._last_poll = 0.0
        self._reader_thread: Optional[threading.Thread] = None

    # --- declarations and the node's agents ----------------------------------------------

    def _load_initial_declarations(self) -> None:
        try:
            self._decl_mtime = self._decl_path.stat().st_mtime_ns
        except OSError:
            self._decl_mtime = None
        self._entries = self._load_entries()
        self._signature = _declaration_signature(self._entries)

    def _load_entries(self) -> List[Dict[str, Any]]:
        if self._decl_loader is not None:
            return list(self._decl_loader() or [])
        try:
            entries, problems = resident_registry.load_declarations()
        except resident_registry.RegistrationError as exc:
            log(f"{self.node}: cannot load declarations: {exc}")
            return list(self._entries)
        for problem in problems:
            log(f"{self.node}: declaration problem: {problem}")
        return entries

    def _poll_declarations(self) -> bool:
        """Loads the declaration again when the file mtime changed; True when the content did."""
        if self._decl_loader is not None:
            entries = self._load_entries()
        else:
            try:
                mtime = self._decl_path.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime == self._decl_mtime:
                return False
            self._decl_mtime = mtime
            entries = self._load_entries()
        signature = _declaration_signature(entries)
        if signature == self._signature:
            return False
        self._entries, self._signature = entries, signature
        return True

    def _node_entries(self) -> List[Dict[str, Any]]:
        return [entry for entry in self._entries if entry.get("node") == self.node]

    def _declared_here(self, address: Any) -> bool:
        """A node must never act for another node's agent: every request's agent must be one of
        this node's declared addresses."""
        return isinstance(address, str) and any(entry["address"] == address
                                                for entry in self._node_entries())

    # --- the session loop ----------------------------------------------------------------

    def run(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        # O3: task rows the crash left without their patch action get it before this session serves
        # a node (once per relay process; a session with no such row opens no decision DB).
        _reconcile_task_patches_once(self.store, self._decision_db)
        self._reader_thread = threading.Thread(target=self._read_frames, daemon=True,
                                               name=f"relay-reader-{self.node}")
        self._reader_thread.start()
        now = self.clock()
        self._last_recv, self._last_ping, self._last_poll = now, now, now
        try:
            while not stop_event.is_set() and not self._closed:
                self._pump()
                if self._closed:
                    break
                self._tick(self.clock())
        finally:
            self._shutdown()

    def _pump(self) -> None:
        try:
            item = self._frames.get(timeout=self.WAKE_SECONDS)
        except queue.Empty:
            return
        if isinstance(item, tuple):
            # ("_close", reason) from the reader thread, ("_snapshot", payload) from an ensure thread
            if item[0] == "_close":
                self._close(item[1])
            else:
                self._on_snapshot_result(item[1])
            return
        self._last_recv = self.clock()
        self._handle(item)

    def _tick(self, now: float) -> None:
        self._drop_timed_out(now)
        if self._synced:
            if now - self._last_poll >= self.POLL_INTERVAL:
                self._last_poll = now
                self._poll()
            if now - self._last_ping >= self.PING_INTERVAL:
                self._last_ping = now
                self._request("ping", {})
        if now - self._last_recv > self.LIVENESS_TIMEOUT:
            self._close("liveness")

    def _poll(self) -> None:
        """The 2 s poll (SQLite has no notification): declaration changes, open-tool decisions,
        queued mail, queued direct session queries, bind timeouts and the 10-minute release."""
        self._poll_declarations()
        self._push_agents()  # a no-op unless the declaration or an open decision changed
        self._deliver_queued()
        self._dispatch_queries()
        self.store.check_bind_timeouts()
        self.store.release_stale()

    def _drop_timed_out(self, now: float) -> None:
        for rid, (kind, context, sent) in list(self._pending.items()):
            if now - sent <= self.REQUEST_TIMEOUT:
                continue
            del self._pending[rid]
            if kind == "deliver":  # the mail stays queued and is sent again on the next poll
                self._inflight.discard(context.get("mail_id"))
            elif kind == "query":
                self._inflight_queries.discard(context.get("query_id"))

    # --- reading and writing -------------------------------------------------------------

    def _read_frames(self) -> None:
        while True:
            try:
                line = self.reader.readline()
            except (OSError, ValueError) as exc:
                self._frames.put(("_close", f"read_error: {exc}"))
                return
            if not line:
                self._frames.put(("_close", "stream_end"))
                return
            if len(line) > MAX_LINE_BYTES + 1:
                self._frames.put(("_close", "line_too_long"))
                return
            try:
                frame = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._frames.put(("_close", "bad_json"))
                return
            if not isinstance(frame, dict):
                self._frames.put(("_close", "bad_frame"))
                return
            self._frames.put(frame)

    def _send(self, frame: Dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self.writer.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
            self.writer.flush()
        except (OSError, ValueError, TypeError) as exc:
            self._close(f"write_error: {exc}")

    def _request(self, kind: str, payload: Dict[str, Any],
                 context: Optional[Dict[str, Any]] = None) -> str:
        """Sends a hub request and holds it as pending until its reply or the 20 s timeout."""
        self._next_id += 1
        rid = f"h{self._next_id}"
        self._pending[rid] = (kind, context or {}, self.clock())
        self._send({"t": kind, "id": rid, **payload})
        return rid

    def _reply_ok(self, rid: Any, **payload: Any) -> None:
        self._send({"t": "reply", "re": rid, "ok": True, **payload})

    def _reply_error(self, rid: Any, error: str, detail: Any = "") -> None:
        frame: Dict[str, Any] = {"t": "reply", "re": rid, "ok": False, "error": error}
        if detail:
            frame["detail"] = str(detail)
        self._send(frame)

    def _reply_result(self, rid: Any, answer: Dict[str, Any]) -> None:
        """A store answer {"ok": false, reason, detail} becomes error + detail; an ok answer is
        spread into the reply."""
        if answer.get("ok"):
            self._reply_ok(rid, **{key: value for key, value in answer.items() if key != "ok"})
        else:
            self._reply_error(rid, answer.get("reason", "error"), answer.get("detail", ""))

    # --- frames from the daemon ----------------------------------------------------------

    def _handle(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "reply":
            self._on_reply(frame)
            return
        rid = frame.get("id")
        if self._first_frame:
            self._first_frame = False
            if kind != "hello":
                self._reply_error(rid, "protocol_mismatch", "the first frame must be hello")
                self._close("protocol_mismatch")
                return
        if kind == "hello":
            if frame.get("protocol") == PROTOCOL:
                # Open tools (C5): a daemon that reports neither field keeps the defaults, so no
                # open agent is ever sent to it; sandbox_version may be null on the node side.
                self._hello_open_tools = frame.get("open_tools") is True
                self._hello_session_query = frame.get("session_query") is True
                self._hello_multi_session = frame.get("multi_session") is True
                version = frame.get("sandbox_version")
                self._hello_sandbox_version = version if isinstance(version, str) and version else None
                self._reply_ok(rid)
            else:
                self._reply_error(rid, "protocol_mismatch",
                                  f"relay speaks protocol {PROTOCOL}, not {frame.get('protocol')!r}")
                self._close("protocol_mismatch")
        elif kind == "sync":
            self._on_sync(frame)
        elif not self._synced:
            self._reply_error(rid, "not_synced", "sync must be answered first")
        else:
            handler = self._DAEMON_FRAMES.get(kind)
            if handler is None:
                self._reply_error(rid, "unknown_frame", f"no frame {kind!r}")
            else:
                handler(self, frame)

    def _on_sync(self, frame: Dict[str, Any]) -> None:
        result = self.store.reconcile(self.node, frame.get("agents") or {})
        self._synced = True
        self.handshake_completed = True
        log(f"{self.node}: synced ({len(result['applied'])} acknowledgements applied, "
            f"{len(result['unknown'])} unknown agents, {len(result['restored'])} restored)")
        self._reply_ok(frame.get("id"), applied=result["applied"], unknown=result["unknown"])
        self._push_agents()
        self._deliver_queued()
        self._dispatch_queries()
        self._push_tasks()

    def _agent_frame(self, frame: Dict[str, Any], call: Callable[[str], Dict[str, Any]]) -> None:
        """The node check every daemon request passes, then one store call whose answer is the
        reply."""
        rid, address = frame.get("id"), frame.get("agent")
        if not self._declared_here(address):
            self._reply_error(rid, "unknown_agent", f"no agent {address!r} declared on {self.node}")
            return
        self._reply_result(rid, call(address))

    def _held_frame(self, frame: Dict[str, Any], call: Callable[[str], Dict[str, Any]]) -> None:
        """_agent_frame for the requests that start work (bind_begin, admit): an open agent the
        relay holds (left out or blocked) is refused here, whatever the daemon believes."""
        reason = self._held.get(frame.get("agent"))
        if reason:
            self._reply_error(frame.get("id"), "agent_held", reason)
            return
        self._agent_frame(frame, call)

    def _on_bind_begin(self, frame: Dict[str, Any]) -> None:
        self._held_frame(frame, lambda address: self.store.bind_begin(address))

    def _on_bind_complete(self, frame: Dict[str, Any]) -> None:
        def complete(address: str) -> Dict[str, Any]:
            return self.store.bind_complete(address, frame.get("epoch"), frame.get("session_id"),
                                            pane=frame.get("pane"), pid=frame.get("pid"))
        self._agent_frame(frame, complete)

    def _on_resume_report(self, frame: Dict[str, Any]) -> None:
        def report(address: str) -> Dict[str, Any]:
            answer = self.store.report_resume(address, frame.get("epoch"), frame.get("session_id"))
            if answer.get("ok"):
                return {"ok": True, "resume_new_session": bool(answer.get("new_session"))}
            return answer
        self._agent_frame(frame, report)

    def _on_admit(self, frame: Dict[str, Any]) -> None:
        self._held_frame(frame, lambda address: self.store.admit_turn(
            address, frame.get("epoch"), frame.get("kind"),
            query_id=frame.get("query_id")))

    def _on_retrieved(self, frame: Dict[str, Any]) -> None:
        self._agent_frame(frame, lambda address: self.store.record_retrieved(
            address, frame.get("epoch"), frame.get("turn"), frame.get("ids") or ()))

    def _on_turn_end(self, frame: Dict[str, Any]) -> None:
        def end(address: str) -> Dict[str, Any]:
            return self.store.end_turn(address, frame.get("epoch"), frame.get("turn"),
                                       frame.get("end_reason") or "normal",
                                       acked=frame.get("acked") or (),
                                       session_id=frame.get("session_id"),
                                       query_id=frame.get("query_id"),
                                       query_reply=frame.get("query_reply"),
                                       session_key=frame.get("session_key"))
        self._agent_frame(frame, end)

    def _on_agent_exit(self, frame: Dict[str, Any]) -> None:
        self._agent_frame(frame, lambda address: self.store.set_down(
            address, frame.get("reason") or "agent_exit"))

    # --- the tool calls (the `call` frame) ------------------------------------------------

    def _on_call(self, frame: Dict[str, Any]) -> None:
        rid, address = frame.get("id"), frame.get("agent")
        if not self._declared_here(address):
            self._reply_error(rid, "unknown_agent", f"no agent {address!r} declared on {self.node}")
            return
        tool = frame.get("tool")
        fn = self._TOOLS.get(tool)
        if fn is None:
            self._reply_error(rid, "unknown_tool", f"no tool {tool!r}")
            return
        args = {key: value for key, value in (frame.get("args") or {}).items()
                if key not in TOOL_DROP_KEYS}
        try:
            answer = fn(self, address, args, frame)
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            self._reply_error(rid, "bad_input", str(exc) or exc.__class__.__name__)
            return
        if isinstance(answer, dict):
            self._reply_result(rid, answer)
        else:
            self._reply_ok(rid, result=answer)
        if tool not in ("mail_agents", "task_list"):
            # a write may have queued mail for an agent of this node: push it now, not at the poll
            self._deliver_queued()

    def _tool_mail_send(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        return self.store.send(agent, args["to"], args.get("kind") or "message",
                               args.get("subject") or "", args.get("body") or "",
                               op_id=frame.get("op_id"), thread=args.get("thread"),
                               task=args.get("task"), reply_to=args.get("reply_to"),
                               epoch=frame.get("epoch"), turn=frame.get("turn"))

    def _tool_mail_agents(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        return self.store.peer_view(agent)

    def _tool_task_create(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        project, sha = args.get("project"), args.get("sha")
        if project is not None:
            # A task is pinned here: the store never keeps a moving ref, and a project the hub
            # cannot resolve is refused before the store is touched (no op id is spent).
            try:
                project, sha = mail_snapshots.resolve_project_sha(project, sha)
            except mail_snapshots.TaskSnapshotError as exc:
                return {"ok": False, "reason": "bad_project", "detail": str(exc)}
        return self.store.task_create(agent, args["title"], args.get("detail") or "",
                                      op_id=frame.get("op_id"), assignee=args.get("assignee"),
                                      blocked_by=args.get("blocked_by") or (),
                                      project=project, sha=sha,
                                      epoch=frame.get("epoch"), turn=frame.get("turn"))

    def _tool_task_claim(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        answer = self.store.task_claim(agent, args["task"], op_id=frame.get("op_id"),
                                       epoch=frame.get("epoch"), turn=frame.get("turn"))
        if answer.get("ok"):
            # Also fills a replayed op id answer: the snapshot fields are recomputed from the row.
            self._claim_snapshot(answer, args["task"])
        return answer

    def _tool_task_update(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        # `calls` and the O3 patch fields are the daemon's attachments (the node strips caller-sent
        # copies); the records are kept only for paths the task's snapshot holds, and a filter error
        # never fails the update. A patch is converted and checked BEFORE the store is touched, so
        # any refusal leaves the task in_progress.
        calls = args.pop("calls", None)
        patch = args.pop("patch", None)
        patch_base = args.pop("patch_base", None)
        info = self.store.task_claim_info(args["task"])
        if info is not None:
            try:
                allowed = self._snapshot_ensurer().allowed_paths(info["project"], info["sha"])
                calls = mail_snapshots.filter_calls(calls, allowed)
            except Exception:  # noqa: BLE001 - the update stands, the log does not
                calls = {"records": [], "dropped": True}
        elif calls is not None:
            calls = {"records": [], "dropped": False}
        converted = None
        if patch is not None:
            refusal = self._task_patch_refusal(agent, args.get("status"), info)
            if refusal is not None:
                return refusal
            try:
                converted = task_patch.convert(info["project"], info["sha"], patch, patch_base,
                                               ensurer=self._snapshot_ensurer())
            except task_patch.TaskPatchError as exc:
                log(f"{self.node}: task {args['task']} patch refused: {exc.code}: {exc.message}")
                return {"ok": False, "reason": exc.code, "detail": exc.message}
        answer = self.store.task_update(agent, args["task"], args["generation"], args["status"],
                                        op_id=frame.get("op_id"), result=args.get("result"),
                                        reason=args.get("reason"), epoch=frame.get("epoch"),
                                        turn=frame.get("turn"), calls=calls, patch=patch)
        if converted is not None and answer.get("ok"):
            try:
                action_id = self._record_task_patch(agent, args["task"], info, converted,
                                                    args.get("result"), patch_base)
            except Exception as exc:  # noqa: BLE001 - the row stays pending; the start reconcile has it
                log(f"{self.node}: task {args['task']} patch action failed: {exc}")
                action_id = None
            if action_id is not None:
                answer["action_id"] = action_id
        return answer

    def _task_patch_refusal(self, agent: str, status: Any, info: Optional[Dict[str, str]]
                            ) -> Optional[Dict[str, Any]]:
        """Why this task_update may not carry a patch (resident-open-tools.md rule 5): only an agent
        declared `tools: "open"`, on a task that names a project, finishing it as `completed`."""
        entry = next((item for item in self._node_entries() if item.get("address") == agent), None)
        if entry is None or entry.get("tools") != "open":
            return {"ok": False, "reason": "patch_not_allowed",
                    "detail": 'only an agent declared tools "open" may attach a patch'}
        if info is None:
            return {"ok": False, "reason": "patch_not_allowed",
                    "detail": "a patch needs a task that names a project"}
        if status != "completed":
            return {"ok": False, "reason": "patch_not_allowed",
                    "detail": "a patch rides only a task_update that completes the task"}
        return None

    def _record_task_patch(self, agent: str, task_id: str, info: Dict[str, str],
                           converted: Dict[str, Any], result: Any, patch_base: Any) -> Optional[int]:
        """The patch became a stored completed row: its ONE action, keyed on the row (a replay or a
        retry after a crash returns the existing id and creates nothing)."""
        return record_task_patch_action(self.store, self._decision_db(), task_id, agent,
                                        info["project"], info["sha"], converted, result,
                                        base=patch_base if isinstance(patch_base, str) else None)

    def _decision_db(self) -> Any:
        """The decision DB of task patch actions, built on first use: a session without a patch
        opens none."""
        if self._db is None:
            from db_manager import DecisionDBManager

            self._db = DecisionDBManager()
        return self._db

    def _tool_task_release(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        return self.store.task_release(agent, args["task"], args.get("generation"), args["reason"],
                                       op_id=frame.get("op_id"))

    def _tool_task_list(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        return self.store.task_list(agent)

    def _open_agent_refusal(self, agent: str) -> Optional[Dict[str, Any]]:
        entry = next((item for item in self._node_entries() if item.get("address") == agent), None)
        if entry is None or entry.get("tools") != "open":
            return {"ok": False, "reason": "not_open",
                    "detail": 'only an agent declared tools "open" may call this tool'}
        return None

    def _tool_artifact_publish(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        refusal = self._open_agent_refusal(agent)
        if refusal is not None:
            return refusal
        return self.store.publish_artifact(
            agent,
            args.get("filename") or "",
            args.get("content_b64") or "",
            args.get("title") or "",
            rel_path=args.get("rel_path") or "",
            task=args.get("task"),
            summary=args.get("summary") or "",
            mime=args.get("mime") or "",
            op_id=frame.get("op_id"),
        )

    def _propose_ops_action(self, agent: str, tool: str, action_args: Dict[str, Any],
                            reason: str, task: Any, op_id: Any) -> Dict[str, Any]:
        import ops_actions

        refusal = self._open_agent_refusal(agent)
        if refusal is not None:
            return refusal
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            return {"ok": False, "reason": "bad_request", "detail": "reason must be a non-empty string"}
        # Stamp target_host strictly from this relay stream's node so an agent cannot target other nodes.
        stamped = {**action_args, "target_host": self.node}
        try:
            validated = ops_actions.validate(tool, stamped)
        except ValueError as exc:
            return {"ok": False, "reason": "bad_request", "detail": str(exc)}
        # Hourly proposal cap per agent to protect the Approvals queue.
        cutoff = mail_store.format_ts(self.store.now() - mail_store.timedelta(hours=1))
        recent = [e for e in self.store.events(limit=200)
                  if e.get("kind") == "ops_proposed" and e.get("agent") == agent
                  and str(e.get("at") or "") >= cutoff]
        if len(recent) >= 10:
            return {"ok": False, "reason": "rate_limit",
                    "detail": "at most 10 ops proposals per hour per agent"}
        task_id = task.strip() if isinstance(task, str) and task.strip() else None
        thread_id = f"resident-ops-{task_id}" if task_id else f"resident-ops-{self.node}"
        db = self._decision_db()
        action_id = db.add_action(
            thread_id, 1, tool, validated,
            f"[{agent}] {clean_reason[:400]}",
            status="PROPOSED", project_id="fleet",
        )
        self.store.note("ops_proposed", agent=agent,
                        detail={"action_id": action_id, "tool": tool, "target_host": self.node,
                                "task": task_id, "op_id": op_id})
        return {"ok": True, "action_id": action_id, "tool": tool,
                "target_host": self.node, "status": "PROPOSED"}

    def _tool_ops_propose_package(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        return self._propose_ops_action(
            agent, "ops:package_install",
            {"manager": args.get("manager"), "packages": args.get("packages")},
            args.get("reason") or "", args.get("task"), frame.get("op_id"),
        )

    def _tool_ops_propose_update(self, agent: str, args: Dict[str, Any], frame: Dict[str, Any]) -> Any:
        return self._propose_ops_action(
            agent, "ops:system_update",
            {"scope": args.get("scope")},
            args.get("reason") or "", args.get("task"), frame.get("op_id"),
        )

    _TOOLS = {"mail_send": _tool_mail_send, "mail_agents": _tool_mail_agents,
              "task_create": _tool_task_create, "task_claim": _tool_task_claim,
              "task_update": _tool_task_update, "task_release": _tool_task_release,
              "task_list": _tool_task_list,
              "artifact_publish": _tool_artifact_publish,
              "ops_propose_package": _tool_ops_propose_package,
              "ops_propose_update": _tool_ops_propose_update}

    # --- hub requests --------------------------------------------------------------------

    def _on_reply(self, frame: Dict[str, Any]) -> None:
        entry = self._pending.pop(frame.get("re"), None)
        if entry is None:  # unknown or already timed out
            return
        kind, context, _sent = entry
        if kind == "query":
            qid = context.get("query_id")
            self._inflight_queries.discard(qid)
            if frame.get("ok") and frame.get("result") in ("accepted", "duplicate"):
                self.store.mark_query_dispatched(qid)
            else:
                err = str(frame.get("error") or frame.get("result") or "query_refused")
                self.store.fail_query(qid, err)
            return
        if kind != "deliver":
            return
        mail_id, agent = context.get("mail_id"), context.get("agent")
        self._inflight.discard(mail_id)
        result = frame.get("result") if frame.get("ok") else frame.get("error")
        if result in ("spooled", "duplicate"):
            self.store.mark_delivered(agent, [mail_id])
        elif result == "tombstoned":
            self.store.mark_tombstoned(agent, [mail_id])
        elif result == "unknown_agent":
            self.store.note("deliver_unknown_agent", agent=agent, detail={"id": mail_id})
        else:  # the mail stays queued and is sent again on the next poll
            self.store.note("deliver_unanswered", agent=agent,
                            detail={"id": mail_id, "result": result})

    def _hub_sandbox_version(self) -> Optional[str]:
        """This release's sandbox version (C1), computed once per session. None when the release
        tree does not hold the four sandbox files: the hub can then vouch for no node's sandbox."""
        if self._sandbox_version is _SANDBOX_VERSION_UNSET:
            try:
                self._sandbox_version = sandbox_info.sandbox_version()
            except sandbox_info.SandboxVersionError as exc:
                log(f"{self.node}: cannot compute the sandbox version: {exc}")
                self._sandbox_version = None
        return self._sandbox_version

    def _open_decision(self, entry: Dict[str, Any], version: Optional[str]) -> Optional[str]:
        """Why this node may not run the open entry (None when it may): a daemon whose hello did
        not report open_tools never sees the agent at all; otherwise the inventory verdict that
        worker_probe.record-sandbox wrote (missing or failed -> unverified, another sandbox
        version -> stale) and the version the daemon's hello reported against this release's."""
        if not self._hello_open_tools:
            return NODE_WITHOUT_OPEN_TOOLS
        if version is None:
            return SANDBOX_STALE  # the hub cannot vouch for its own release's sandbox
        verdict = resident_registry.inventory_sandbox(entry.get("node"))
        if not verdict or verdict.get("passed") is not True:
            return SANDBOX_UNVERIFIED
        if verdict.get("version") != version:
            return SANDBOX_STALE
        if self._hello_sandbox_version != version:
            return SANDBOX_VERSION_MISMATCH
        return None

    def _open_state(self) -> Dict[str, Optional[str]]:
        """The decision per open entry of this node, recomputed on every call (each agents frame
        and each poll), so a re-probed node is picked up with no declaration change."""
        open_entries = [entry for entry in self._node_entries() if entry.get("tools") == "open"]
        if not open_entries:
            return {}
        version = self._hub_sandbox_version()
        return {entry["address"]: self._open_decision(entry, version) for entry in open_entries}

    def _push_agents(self) -> None:
        """Sends the agents frame when the declaration or an open-tool decision changed since the
        last one sent (after sync it always differs from nothing, so the first frame always goes
        out). An open entry the daemon must never see is left out of the frame and marked down; a
        blocked one is sent with its reason, so the node knows why it idles (C6)."""
        open_state = self._open_state()
        signature = f"{self._signature}|open:{json.dumps(open_state, sort_keys=True)}"
        if self._sent_signature is not None and signature == self._sent_signature:
            return
        self._sent_signature = signature
        self._agents_version += 1
        version = self._hub_sandbox_version() if open_state else None
        specs: List[Dict[str, Any]] = []
        omitted: set = set()
        downs: List[Tuple[str, str]] = []
        for entry in self._node_entries():
            reason = open_state.get(entry["address"])
            if reason == NODE_WITHOUT_OPEN_TOOLS:
                omitted.add(entry["address"])  # an old daemon must never see the agent
                downs.append((entry["address"], reason))
                continue
            specs.append(agent_spec(entry, self._table, peers=effective_peers(entry, self._entries),
                                    sandbox_version=version, blocked=reason))
            if reason:
                downs.append((entry["address"], reason))
        self._omitted = omitted
        self._held = dict(downs)
        self._request("agents", {"version": self._agents_version, "agents": specs})
        for address, reason in downs:
            self.store.set_down(address, reason)

    def _dispatch_queries(self) -> None:
        """Send queued direct session queries for this node's agents (resident-session-query.md, Q2)."""
        for row in self.store.queued_queries(self.node):
            qid = row["id"]
            if qid in self._inflight_queries:
                continue
            if row["address"] in self._omitted or row["address"] in self._held:
                continue
            if not self._hello_session_query:
                self.store.fail_query(qid, "node_without_session_query")
                continue
            skey = str(row.get("session_key") or "main")
            if skey != "main" and not self._hello_multi_session:
                self.store.fail_query(qid, "node_without_multi_session")
                continue
            self._inflight_queries.add(qid)
            self._request(
                "query",
                {
                    "agent": row["address"],
                    "epoch": row["epoch"],
                    "query_id": qid,
                    "session_key": skey,
                    "expected_session_id": row["session_id"],
                    "create_if_missing": not bool(row.get("session_id")),
                    "project": row["project"],
                    "project_kind": row["project_kind"],
                    "task": row["task"],
                    "caller": row["caller"],
                    "caller_kind": row["caller_kind"],
                    "mode": row["mode"],
                    "prompt": row["prompt"],
                    "timeout_sec": row["timeout_sec"],
                    "expires": row["expires"],
                },
                context={"agent": row["address"], "query_id": qid},
            )

    def _deliver_queued(self) -> None:
        """One deliver frame per queued mail of the node's agents, oldest first; a mail whose
        deliver is still in flight is never sent twice. Mail of an open agent the daemon never saw
        stays queued on the hub."""
        for entry in self._node_entries():
            if entry["address"] in self._omitted:
                continue
            for row in self.store.inbox_queued(entry["address"]):
                if row["id"] in self._inflight:
                    continue
                self._inflight.add(row["id"])
                self._request("deliver", {"agent": entry["address"], "envelope": envelope(row)},
                              context={"agent": entry["address"], "mail_id": row["id"]})

    # --- task snapshots (M2e) ------------------------------------------------------------

    def _snapshot_ensurer(self) -> Any:
        """This node's snapshot ensurer, built on first use: a relay with no project task never
        builds one (and so never opens an ssh call for a snapshot)."""
        if self._ensurer is None:
            self._ensurer = mail_snapshots.SnapshotEnsurer(self.node)
        return self._ensurer

    def _claim_snapshot(self, answer: Dict[str, Any], task_id: str) -> None:
        """The claim reply's snapshot field: none (no project), ready (the node is known to hold the
        key; no task_state follows) or pending (an ensure starts below)."""
        info = self.store.task_claim_info(task_id)
        if info is None:
            answer["snapshot"] = "none"
            return
        pair = (info["project"], info["sha"])
        generation = answer.get("generation") if isinstance(answer.get("generation"), int) else None
        key = self._ready_keys.get((task_id, generation)) if generation is not None else None
        key = key or self._snapshot_ensurer().known_key(*pair)
        answer["project"], answer["sha"] = pair
        if key:
            if generation is not None:
                self._ready_keys[(task_id, generation)] = key
            answer["snapshot"], answer["key"] = "ready", key
            return
        answer["snapshot"] = "pending"
        if generation is not None:
            self._waiting.setdefault(pair, {})[task_id] = generation
        self._start_ensure(*pair)

    def _start_ensure(self, project: str, sha: str) -> None:
        """One daemon thread per distinct (project, sha) in flight: a second claim of the same pair
        waits for the same result. The thread only ensures and queues a ("_snapshot", payload)
        tuple; it never touches the store or the writer."""
        pair = (project, sha)
        if self._closed or pair in self._ensuring:
            return

        def run() -> None:
            payload: Dict[str, Any] = {"project": project, "sha": sha}
            try:
                payload["key"], _allowed = self._snapshot_ensurer().ensure(project, sha)
            except Exception as exc:  # noqa: BLE001 - the short reason is all the pump needs
                payload["error"] = str(exc) or exc.__class__.__name__
            self._frames.put(("_snapshot", payload))

        thread = threading.Thread(target=run, daemon=True, name=f"relay-snapshot-{self.node}")
        self._ensuring[pair] = thread
        thread.start()

    def _on_snapshot_result(self, payload: Dict[str, Any]) -> None:
        """The background ensure finished (the pump thread): every waiting claim of this node's
        agents hears it. Success reaches every in_progress task of the pair (a task_state for a
        generation the daemon no longer holds is ignored there); a failure releases only the claims
        this session saw at the generation it re-checks, so a late failure never releases a newer
        claim."""
        pair = (payload.get("project"), payload.get("sha"))
        self._ensuring.pop(pair, None)
        key, error = payload.get("key"), payload.get("error")
        owners = [entry["address"] for entry in self._node_entries()]
        if key:
            self._waiting.pop(pair, None)  # the waiters hear `ready` through the scan below
            for row in self.store.in_progress_tasks(owners):
                if (row["project"], row["sha"]) != pair:
                    continue
                if self._ready_keys.get((row["id"], row["generation"])) == key:
                    continue
                self._ready_keys[(row["id"], row["generation"])] = key
                self._request("task_state", {"agent": row["owner"], "task": row["id"],
                                             "generation": row["generation"], "state": "ready",
                                             "key": key})
            return
        for task_id, generation in self._waiting.pop(pair, {}).items():
            row = next((item for item in self.store.in_progress_tasks(owners)
                        if item["id"] == task_id and item["generation"] == generation
                        and (item["project"], item["sha"]) == pair), None)
            if row is None:
                continue  # the claim ended or moved on: nothing to release
            released = self.store.task_release("hub", task_id, generation,
                                               f"snapshot_failed: {error}")
            if released.get("ok"):
                self._request("task_state", {"agent": row["owner"], "task": task_id,
                                             "generation": generation, "state": "released",
                                             "key": None})

    def _push_tasks(self) -> None:
        """One `tasks` frame per declared agent right after sync: the full set of its in_progress
        tasks, which recovers the daemon's table after a hub or relay restart. An open agent the
        daemon never saw holds no tasks there."""
        for entry in self._node_entries():
            if entry["address"] in self._omitted:
                continue
            address = entry["address"]
            owned = {row["id"]: self._task_owned(row)
                     for row in self.store.in_progress_tasks([address])}
            self._request("tasks", {"agent": address, "owned": owned})

    def _task_owned(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """One entry of the `tasks` frame: the daemon's table row for a task this agent owns."""
        generation = row["generation"]
        if not row["project"]:
            return {"generation": generation, "state": "none", "key": None}
        pair = (row["project"], row["sha"])
        key = self._ready_keys.get((row["id"], generation)) \
            or self._snapshot_ensurer().known_key(*pair)
        if key:
            self._ready_keys[(row["id"], generation)] = key
            return {"generation": generation, "state": "ready", "key": key}
        self._waiting.setdefault(pair, {})[row["id"]] = generation
        self._start_ensure(*pair)
        return {"generation": generation, "state": "pending", "key": None}

    # --- closing -------------------------------------------------------------------------

    def _close(self, reason: str) -> None:
        if not self._closed:
            self._closed = True
            self.close_reason = reason

    def _shutdown(self) -> None:
        """Closes the stream, drops the pending requests and marks the node's agents down."""
        self._closed = True
        if self.close_reason is None:
            self.close_reason = "stopped"
        try:
            self.writer.close()
        except (OSError, ValueError, AttributeError):
            pass
        # The reader thread holds the reader's buffer lock inside readline(), so closing the
        # reader now would block until the stream ends. The transport's owner ends the stream
        # (the runner stops ssh); the reader is closed here only once its thread has gone.
        thread = self._reader_thread
        if thread is not None:
            thread.join(timeout=0.5)
        if thread is None or not thread.is_alive():
            try:
                self.reader.close()
            except (OSError, ValueError, AttributeError):
                pass
        self._inflight.clear()
        self._inflight_queries.clear()
        self._pending.clear()
        try:
            down = self.store.set_node_down(self.node, mail_relay_store.STREAM_LOST)
            if down:
                log(f"{self.node}: stream lost, agents down: {', '.join(down)}")
        except Exception as exc:  # a broken store must not mask the session's own error
            log(f"{self.node}: marking the node down failed: {exc}")

    _DAEMON_FRAMES = {"bind_begin": _on_bind_begin, "bind_complete": _on_bind_complete,
                      "resume_report": _on_resume_report, "admit": _on_admit,
                      "retrieved": _on_retrieved, "turn_end": _on_turn_end, "call": _on_call,
                      "agent_exit": _on_agent_exit}


class RelayRunner:
    """One thread per node that has declared agents. Each thread spawns the ssh relay stream,
    runs a RelaySession on its stdin/stdout and reconnects with a back-off; the control loop
    starts threads for new nodes and stops the threads of nodes whose agents all disappeared."""

    BACKOFFS = (5.0, 10.0, 30.0, 60.0)

    def __init__(self, spawn: Optional[Callable[[str], Tuple[Any, Any, Any]]] = None,
                 poll: float = 2.0, backoffs: Optional[Sequence[float]] = None,
                 session_factory: Optional[Callable[[str, Any, Any], RelaySession]] = None):
        self.spawn = spawn or self._spawn_ssh
        self.poll = poll
        self.backoffs = tuple(backoffs) if backoffs is not None else self.BACKOFFS
        self.session_factory = session_factory or self._make_session
        self.waits: List[float] = []  # the back-off seconds chosen (tests)
        self._threads: Dict[str, threading.Thread] = {}
        self._stops: Dict[str, threading.Event] = {}

    @staticmethod
    def _make_session(node: str, reader: Any, writer: Any) -> RelaySession:
        return RelaySession(node, reader, writer)

    @staticmethod
    def _spawn_ssh(node: str) -> Tuple[Any, Any, Any]:
        proc = subprocess.Popen(relay_argv(node), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        return proc, proc.stdout, proc.stdin

    def run(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        log("relay starting")
        while not stop_event.is_set():
            self._reconcile_threads()
            stop_event.wait(self.poll)
        for event in self._stops.values():
            event.set()
        for thread in self._threads.values():
            thread.join(timeout=5.0)
        log("relay stopped")

    def _reconcile_threads(self) -> None:
        try:
            entries, problems = resident_registry.load_declarations()
        except resident_registry.RegistrationError as exc:
            log(f"cannot load declarations: {exc}")
            entries, problems = [], []
        for problem in problems:
            log(f"declaration problem: {problem}")
        nodes = {entry["node"] for entry in entries}
        for node in sorted(nodes - set(self._threads)):
            self._start_node(node)
        for node, event in self._stops.items():
            if node not in nodes:
                log(f"{node}: no declared agents left, stopping its relay")
                event.set()
        for node, thread in list(self._threads.items()):
            if not thread.is_alive():
                self._threads.pop(node, None)
                self._stops.pop(node, None)

    def _start_node(self, node: str) -> None:
        stop = threading.Event()
        self._stops[node] = stop
        thread = threading.Thread(target=self._node_loop, args=(node, stop),
                                  name=f"relay-{node}", daemon=True)
        self._threads[node] = thread
        thread.start()
        log(f"{node}: relay thread started")

    def _node_loop(self, node: str, stop: threading.Event) -> None:
        index = 0
        while not stop.is_set():
            completed = self._run_one_session(node, stop)
            if stop.is_set():
                break
            wait = self.backoffs[min(index, len(self.backoffs) - 1)]
            index = 0 if completed else index + 1
            self.waits.append(wait)
            log(f"{node}: reconnecting in {wait:g}s")
            stop.wait(wait)

    def _run_one_session(self, node: str, stop: threading.Event) -> bool:
        """One spawn/session/teardown cycle; returns whether the handshake completed (the back-off
        resets only then)."""
        process = reader = writer = None
        completed = False
        try:
            process, reader, writer = self.spawn(node)
            session = self.session_factory(node, reader, writer)
            session.run(stop)
            completed = session.handshake_completed
            log(f"{node}: relay session ended ({session.close_reason})")
        except Exception as exc:
            log(f"{node}: relay session failed: {exc}")
        finally:
            self._close_transport(process, reader, writer)
        return completed

    @staticmethod
    def _close_transport(process: Any, reader: Any, writer: Any) -> None:
        for stream in (reader, writer):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                pass


def main(stop_event: Optional[threading.Event] = None) -> int:
    """`guardian.py --mail-relay`: runs the runner until SIGTERM (or SIGINT) arrives."""
    stop = stop_event or threading.Event()
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    except ValueError:  # not the main thread (tests): the stop event is the only way out
        pass
    RelayRunner().run(stop)
    return 0

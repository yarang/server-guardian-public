"""The node side of the resident relay (docs/design/resident-agents.md, M2c): spool, sockets, turns.

The fake hub is a socketpair in this process: it records every frame the daemon sends, auto-answers
the daemon's requests from a per-test table and can stay silent to drive the timeouts. The clock and
the wall clock are injected, so the 90 s silence rule and the 20 s request timeout run without
sleeping. No network, no ssh, no real CLI; the neutral names node-1 and claude@node-1 are used.
"""

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import resident_node
import resident_open
import worker
from repo_snapshot import build_snapshot, import_stream
from repo_tools import RepoTools
from resident_node import ResidentManager
from test_repo_tools import git, make_repo


def wait_until(predicate: Any, what: str = "condition", timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWall:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingDriver:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    def agents_changed(self, added: List[str], updated: List[str], removed: List[str]) -> None:
        self.calls.append(("agents_changed", added, updated, removed))

    def wake(self, name: str) -> None:
        self.calls.append(("wake", name))


class FakeHub:
    """The hub's end of one relay stream over a socketpair, plus the daemon's end as file objects.

    Every frame from the daemon is recorded. `auto` maps a request type to a callable that returns
    the reply payload (None to stay silent); requests without an entry are never answered."""

    def __init__(self, auto: Optional[Dict[str, Any]] = None) -> None:
        hub, daemon = socket.socketpair()
        hub.settimeout(10.0)
        daemon.settimeout(10.0)
        self._hub, self._daemon = hub, daemon
        self.daemon_rfile = daemon.makefile("rb")
        self.daemon_wfile = daemon.makefile("wb")
        self.rfile = hub.makefile("rb")
        self.wfile = hub.makefile("wb")
        self.frames: List[Dict[str, Any]] = []
        self.auto = auto or {}
        self._lock = threading.Lock()
        self._alive = True
        self._next_h = 0
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while self._alive:
            try:
                line = self.rfile.readline(2 * 1024 * 1024)
            except (OSError, ValueError):
                break
            if not line:
                break
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            with self._lock:
                self.frames.append(frame)
            handler = self.auto.get(frame.get("t"))
            if callable(handler):
                payload = handler(frame)
                if payload is not None:
                    self.reply(frame.get("id"), payload)

    def send(self, frame: Dict[str, Any]) -> None:
        self.wfile.write((json.dumps(frame) + "\n").encode("utf-8"))
        self.wfile.flush()

    def reply(self, request_id: Any, payload: Dict[str, Any]) -> None:
        self.send({"t": "reply", "re": request_id, **payload})

    def frames_of(self, kind: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [frame for frame in self.frames if frame.get("t") == kind]

    def wait_frame(self, kind: str, count: int = 1) -> Dict[str, Any]:
        wait_until(lambda: len(self.frames_of(kind)) >= count, what=f"a {kind} frame")
        return self.frames_of(kind)[count - 1]

    def request(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """One hub -> daemon request; returns the daemon's reply frame."""
        self._next_h += 1
        frame = {"id": f"h{self._next_h}", **frame}
        self.send(frame)
        rid = frame["id"]
        wait_until(lambda: any(f.get("t") == "reply" and f.get("re") == rid for f in self.frames_of("reply")),
                   what=f"the reply to {rid}")
        return next(f for f in self.frames_of("reply") if f.get("re") == rid)

    def close(self) -> None:
        self._alive = False
        try:
            # Wake the reader thread first: closing a buffered reader waits for the lock its
            # readline holds, so a plain close would block until the socket timeout.
            self._hub.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._thread.join(5.0)
        for side in (self.rfile, self.wfile, self.daemon_rfile, self.daemon_wfile, self._hub, self._daemon):
            try:
                side.close()
            except OSError:
                pass


def envelope(mail_id: str, created: str = "2026-11-15T10:00:00Z", **extra: Any) -> Dict[str, Any]:
    base = {"id": mail_id, "thread": "t-1", "from": "operator", "from_kind": "operator",
            "to": "claude@node-1", "kind": "message", "subject": "s", "body": "b", "hop": 0,
            "created": created, "expires": "2026-11-16T10:00:00Z", "redelivered": False}
    return {**base, **extra}


def spec(address: str, **extra: Any) -> Dict[str, Any]:
    name = address.split("@", 1)[0]
    base = {"address": address, "provider": name, "driver": "headless", "peers": [],
            "card": "start card", "provider_spec": {"argv": ["cli"], "env": {}, "seed_files": []}}
    return {**base, **extra}


class ResidentCase(unittest.TestCase):
    """One manager, one fake hub, injected clocks, a temp worker dir."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="sgr", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = self.dir / "agents"
        self.clock = FakeClock()
        self.wall = FakeWall(1794700800.0)  # 2026-11-15T00:00:00Z
        self.driver = RecordingDriver()
        self.daemon = worker.Worker(max_jobs=2, queue_seconds=1.0)
        env = patch.dict(os.environ, {"GUARDIAN_WORKER_DIR": str(self.dir),
                                      "GUARDIAN_WORKER_SOCKET": str(self.dir / "w.sock")})
        env.start()
        self.addCleanup(env.stop)
        self.hubs: List[FakeHub] = []
        self.threads: List[threading.Thread] = []
        self.state: Dict[str, Any] = {"epochs": {}, "turns": 0}
        self.mgr = ResidentManager(self.daemon, root=self.root, clock=self.clock, wall=self.wall,
                                   driver=self.driver)

    def auto(self) -> Dict[str, Any]:
        def bind_begin(frame: Dict[str, Any]) -> Dict[str, Any]:
            count = self.state["epochs"].get(frame["agent"], 0) + 1
            self.state["epochs"][frame["agent"]] = count
            return {"ok": True, "epoch": count}

        def admit(frame: Dict[str, Any]) -> Dict[str, Any]:
            self.state["turns"] += 1
            return {"ok": True, "turn": f"u-{self.state['turns']}"}

        return {"hello": lambda frame: {"ok": True},
                "sync": lambda frame: {"ok": True},
                "bind_begin": bind_begin,
                "bind_complete": lambda frame: {"ok": True},
                "resume_report": lambda frame: {"ok": True},
                "admit": admit,
                "retrieved": lambda frame: {"ok": True},
                "turn_end": lambda frame: {"ok": True},
                "agent_exit": lambda frame: {"ok": True},
                "call": lambda frame: {"ok": True, "id": "m-x"}}

    def hub(self, auto: Optional[Dict[str, Any]] = None) -> FakeHub:
        made = FakeHub(auto if auto is not None else self.auto())
        self.hubs.append(made)
        return made

    def start_stream(self, hub: Optional[FakeHub] = None) -> FakeHub:
        hub = hub or self.hub()
        thread = threading.Thread(target=self.mgr.attach, args=(hub.daemon_rfile, hub.daemon_wfile),
                                  daemon=True)
        self.threads.append(thread)
        thread.start()
        hub.wait_frame("sync")  # hello answered, sync answered: the stream is ready
        return hub

    def tearDown(self) -> None:
        self.mgr.shutdown()
        for thread in self.threads:
            thread.join(5.0)
        for hub in self.hubs:
            hub.close()

    # -- helpers -------------------------------------------------------------------------------

    def declare(self, hub: FakeHub, *agents: str, version: int = 5, **extra: Any) -> Dict[str, Any]:
        return hub.request({"t": "agents", "version": version, "agents": [spec(a, **extra) for a in agents]})

    def deliver(self, hub: FakeHub, envelope_: Dict[str, Any], address: str = "claude@node-1") -> Any:
        reply = hub.request({"t": "deliver", "agent": address, "envelope": envelope_})
        return reply.get("result")

    def agent_call(self, socket_path: Path, request: Dict[str, Any]) -> Dict[str, Any]:
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(10.0)
        try:
            conn.connect(str(socket_path))
            conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        finally:
            conn.close()
        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))

    def bind_and_admit(self, hub: FakeHub, name: str = "claude") -> Dict[str, Any]:
        bound = self.mgr.begin_incarnation(name)
        self.assertTrue(bound["ok"], bound)
        admitted = self.mgr.admit(name, "wake")
        self.assertTrue(admitted["ok"], admitted)
        return {**bound, **admitted, "epoch": bound["epoch"], "turn": admitted["turn"]}

    def read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))


class HandshakeTests(ResidentCase):
    def test_hello_then_sync_with_disk_state_and_the_sync_reply_confirms_pending_acks(self) -> None:
        agent = self.root / "claude"
        (agent / "spool").mkdir(parents=True)
        (agent / "state.json").write_text(json.dumps(
            {"address": "claude@node-1", "spec": {"provider": "claude"}, "spec_version": 4,
             "epoch": 3, "session_id": "s9"}))
        (agent / "spool" / "m-1.json").write_text(json.dumps(envelope("m-1")))
        (agent / "tombstones.json").write_text(json.dumps({"m-0": "2099-01-01T00:00:00Z"}))
        pending = [{"epoch": 3, "turn": "u-0", "end_reason": "normal", "acked": ["m-0"]}]
        (agent / "pending_acks.json").write_text(json.dumps(pending))
        fresh = ResidentManager(self.daemon, root=self.root, clock=self.clock, wall=self.wall)
        self.mgr = fresh  # tearDown shuts the fresh one down
        hub = self.hub()
        self.start_stream(hub)
        kinds = [frame["t"] for frame in hub.frames if frame["t"] != "reply"]
        self.assertEqual(kinds[:2], ["hello", "sync"])  # the handshake order
        hello = hub.frames_of("hello")[0]
        self.assertEqual((hello["protocol"], hello["version"]),
                         (3, worker.STARTED_VERSION))
        self.assertEqual(hello["started"], "2026-11-15T00:00:00Z")
        sync = hub.frames_of("sync")[0]
        self.assertEqual(sync["agents"]["claude@node-1"],
                         {"epoch": 3, "ready": False, "spool": ["m-1"], "tombstones": ["m-0"],
                          "pending_acks": pending})  # ready False: loaded from disk, no incarnation
        wait_until(lambda: self.read_json(agent / "pending_acks.json") == [],
                   what="the confirmed pending acks to be dropped")
        self.assertEqual(self.read_json(agent / "pending_acks.json"), [])

    def test_sync_reports_the_local_ready_state_per_agent(self) -> None:
        first = self.start_stream()
        self.declare(first, "claude@node-1", "agy@node-1")
        bound = self.mgr.begin_incarnation("claude")
        self.assertTrue(bound["ok"], bound)
        self.assertTrue(self.mgr.complete_bind("claude", bound["epoch"], session_id="s-1")["ok"])
        first.close()
        wait_until(lambda: self.mgr._stream is None, what="the first stream to end")
        second = self.start_stream()  # the daemon reconnects and syncs its state again
        sync = second.wait_frame("sync")
        self.assertEqual(sync["agents"]["claude@node-1"]["ready"], True)  # the bound incarnation
        self.assertEqual(sync["agents"]["claude@node-1"]["epoch"], bound["epoch"])
        self.assertEqual(sync["agents"]["agy@node-1"]["ready"], False)  # never bound

    def test_without_a_relay_nothing_is_created(self) -> None:
        self.assertFalse(self.root.exists())
        self.assertEqual(self.mgr.spool_pending("claude"), 0)
        self.assertFalse(self.root.exists())

    def test_ping_is_answered(self) -> None:
        hub = self.start_stream()
        reply = hub.request({"t": "ping"})
        self.assertEqual((reply.get("ok"), reply.get("re") is not None), (True, True))

    def test_an_unknown_hub_frame_is_refused(self) -> None:
        hub = self.start_stream()
        reply = hub.request({"t": "shell", "command": "rm -rf /"})
        self.assertFalse(reply.get("ok"))
        self.assertEqual(reply.get("error"), "unknown_frame")


class AgentsFrameTests(ResidentCase):
    def test_the_frame_creates_the_directory_and_the_state(self) -> None:
        hub = self.start_stream()
        reply = self.declare(hub, "claude@node-1", version=5)
        self.assertEqual(reply, {"t": "reply", "re": reply["re"], "ok": True, "applied": 5})
        agent = self.root / "claude"
        wait_until(lambda: agent.is_dir(), what="the agent directory")
        self.assertEqual(agent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.read_json(agent / "state.json"),
                         {"address": "claude@node-1", "spec": spec("claude@node-1"), "spec_version": 5,
                          "epoch": 0, "session_id": None})
        wait_until(lambda: any(c[0] == "agents_changed" for c in self.driver.calls), what="the driver change")
        self.assertEqual(self.driver.calls[0], ("agents_changed", ["claude"], [], []))

    def test_a_later_frame_without_the_agent_retires_it(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        self.bind_and_admit(hub)
        socket_path = self.root / "claude" / "e1.sock"
        wait_until(lambda: socket_path.exists(), what="the incarnation socket")
        self.declare(hub, version=6)  # the list no longer names claude
        agent = self.root / "claude"
        wait_until(lambda: not agent.exists(), what="the agent to be retired")
        self.assertFalse(socket_path.exists())
        wait_until(lambda: any(c[0] == "agents_changed" and c[3] == ["claude"] for c in self.driver.calls),
                   what="the retirement change")
        self.assertEqual(self.mgr.spool_pending("claude"), 0)  # unknown agent now

    def test_a_changed_spec_is_an_update(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        self.declare(hub, "claude@node-1", version=6, card="a new card")
        wait_until(lambda: any(c[0] == "agents_changed" and c[2] == ["claude"] for c in self.driver.calls),
                   what="the update change")
        self.assertEqual(self.read_json(self.root / "claude" / "state.json")["spec_version"], 6)


class DeliverTests(ResidentCase):
    def test_deliver_spooled_duplicate_tombstoned_unknown_and_wake(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        self.assertEqual(self.deliver(hub, envelope("m-1")), "spooled")
        self.assertEqual(self.deliver(hub, envelope("m-1")), "duplicate")
        (self.root / "claude" / "tombstones.json").write_text(json.dumps({"m-9": "2099-01-01T00:00:00Z"}))
        self.assertEqual(self.deliver(hub, envelope("m-9")), "tombstoned")
        self.assertEqual(self.deliver(hub, envelope("m-x"), address="agy@node-9"), "unknown_agent")
        wait_until(lambda: self.driver.calls.count(("wake", "claude")) == 1, what="the wake")
        self.assertEqual(self.mgr.spool_pending("claude"), 1)
        self.wall.advance(35)
        self.assertAlmostEqual(self.mgr.oldest_unread_age("claude"), 35.0, delta=1.0)
        record = self.read_json(self.root / "claude" / "spool" / "m-1.json")
        self.assertEqual(record["_retrieved_turn"], None)
        self.assertTrue(record["_spooled"])

    def test_a_bad_envelope_is_refused(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        reply = hub.request({"t": "deliver", "agent": "claude@node-1", "envelope": {"id": "../escape"}})
        self.assertFalse(reply.get("ok"))
        self.assertEqual(reply.get("error"), "invalid envelope")


class SocketTests(ResidentCase):
    def test_each_incarnation_gets_its_own_socket_and_the_old_one_is_closed(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        first = self.mgr.begin_incarnation("claude")
        self.assertTrue(first["ok"], first)
        self.assertEqual((first["epoch"], Path(first["socket"]).name), (1, "e1.sock"))
        second = self.mgr.begin_incarnation("claude")
        self.assertEqual(second["epoch"], 2)
        self.assertTrue((self.root / "claude" / "e2.sock").exists())
        self.assertFalse((self.root / "claude" / "e1.sock").exists())
        conn = socket.socket(socket.AF_UNIX)
        try:
            with self.assertRaises(OSError):
                conn.connect(str(self.root / "claude" / "e1.sock"))
        finally:
            conn.close()
        self.assertEqual(self.read_json(self.root / "claude" / "state.json")["epoch"], 2)

    def test_a_socket_path_past_the_unix_limit_is_refused_not_crashed(self) -> None:
        hub = self.start_stream()
        segment = "d" * 60
        deep = self.dir / segment / segment
        with patch.object(self.mgr, "root", deep):  # a root whose agent sockets exceed sun_path
            self.declare(hub, "claude@node-1")
            reply = self.mgr.begin_incarnation("claude")
        self.assertFalse(reply["ok"])
        self.assertIn("too long", reply["error"])


class TurnTests(ResidentCase):
    def test_mail_inbox_without_a_turn_then_oldest_first_then_only_new(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.mgr.begin_incarnation("claude")
        socket_path = Path(bound["socket"])
        self.assertEqual(self.agent_call(socket_path, {"tool": "mail_inbox", "args": {}}),
                         {"ok": False, "error": "no_turn"})
        self.mgr.admit("claude", "wake")
        self.deliver(hub, envelope("m-2", created="2026-11-15T10:01:00Z"))
        self.deliver(hub, envelope("m-1", created="2026-11-15T10:00:00Z"))
        first = self.agent_call(socket_path, {"tool": "mail_inbox", "args": {}})
        self.assertTrue(first["ok"], first)
        self.assertEqual([message["id"] for message in first["messages"]], ["m-1", "m-2"])  # oldest first
        self.assertTrue(all(not key.startswith("_") for message in first["messages"] for key in message))
        retrieved = hub.wait_frame("retrieved")
        self.assertEqual((retrieved["agent"], retrieved["epoch"], retrieved["turn"], retrieved["ids"]),
                         ("claude@node-1", 1, "u-1", ["m-1", "m-2"]))
        self.assertEqual(self.agent_call(socket_path, {"tool": "mail_inbox", "args": {}})["messages"], [])
        self.deliver(hub, envelope("m-3", created="2026-11-15T10:02:00Z"))
        again = self.agent_call(socket_path, {"tool": "mail_inbox", "args": {}})
        self.assertEqual([message["id"] for message in again["messages"]], ["m-3"])

    def test_forwarded_calls_carry_the_socket_identity_and_drop_spoofed_fields(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.mgr.begin_incarnation("claude")
        self.mgr.admit("claude", "wake")
        reply = self.agent_call(Path(bound["socket"]), {
            "tool": "mail_send",
            "args": {"to": "agy@node-1", "subject": "s", "body": "b", "from": "forged@node-2",
                     "from_kind": "agent", "hop": 9, "epoch": 77, "agent": "agy@node-1", "turn": "u-999"},
            "op_id": "op-1"})
        call = hub.wait_frame("call")
        self.assertEqual((call["agent"], call["epoch"], call["turn"], call["tool"], call["op_id"]),
                         ("claude@node-1", 1, "u-1", "mail_send", "op-1"))
        self.assertEqual(call["args"], {"to": "agy@node-1", "subject": "s", "body": "b"})
        self.assertEqual(reply, {"ok": True, "id": "m-x"})  # the hub reply minus t/re
        self.assertEqual(self.agent_call(Path(bound["socket"]), {"tool": "shell", "args": {"cmd": "id"}}),
                         {"ok": False, "error": "unknown_tool"})

    def test_a_normal_turn_end_acks_the_retrieved_mail(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.bind_and_admit(hub)
        self.deliver(hub, envelope("m-1"))
        self.agent_call(Path(bound["socket"]), {"tool": "mail_inbox", "args": {}})
        reply = self.mgr.end_turn("claude", "u-1", "normal")
        self.assertTrue(reply["ok"], reply)
        end = hub.wait_frame("turn_end")
        self.assertEqual((end["agent"], end["epoch"], end["turn"], end["end_reason"], end["acked"]),
                         ("claude@node-1", 1, "u-1", "normal", ["m-1"]))
        self.assertFalse((self.root / "claude" / "spool" / "m-1.json").exists())
        self.assertEqual(self.read_json(self.root / "claude" / "tombstones.json"),
                         {"m-1": "2026-11-16T10:00:00Z"})
        self.assertEqual(self.read_json(self.root / "claude" / "pending_acks.json"), [])
        self.assertTrue(self.daemon.take_resident_slot("probe"))  # end_turn released the slot
        self.daemon.release_resident_slot("probe")
        self.assertEqual(self.deliver(hub, envelope("m-1")), "tombstoned")

    def test_a_lost_turn_offers_the_mail_again_as_redelivered(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.bind_and_admit(hub)
        self.deliver(hub, envelope("m-1"))
        self.agent_call(Path(bound["socket"]), {"tool": "mail_inbox", "args": {}})
        self.assertTrue(self.mgr.end_turn("claude", "u-1", "lost")["ok"])
        end = hub.wait_frame("turn_end")
        self.assertEqual(end["acked"], [])
        self.assertTrue((self.root / "claude" / "spool" / "m-1.json").exists())
        self.mgr.admit("claude", "sweep")  # a new turn, u-2
        again = self.agent_call(Path(bound["socket"]), {"tool": "mail_inbox", "args": {}})
        self.assertEqual([message["id"] for message in again["messages"]], ["m-1"])
        self.assertTrue(again["messages"][0]["redelivered"])

    def test_a_turn_of_an_older_epoch_is_refused_and_its_mail_offered_again(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.bind_and_admit(hub)
        self.deliver(hub, envelope("m-1"))
        self.agent_call(Path(bound["socket"]), {"tool": "mail_inbox", "args": {}})
        self.mgr._agents["claude"].epoch = 9  # a newer incarnation the manager learned of
        self.assertEqual(self.mgr.end_turn("claude", "u-1", "normal"), {"ok": False, "error": "wrong_epoch"})
        self.assertEqual(hub.frames_of("turn_end"), [])  # nothing sent to the hub
        self.assertTrue(self.daemon.take_resident_slot("probe"))  # the slot still went free
        self.daemon.release_resident_slot("probe")
        record = self.read_json(self.root / "claude" / "spool" / "m-1.json")
        self.assertEqual((record["_retrieved_turn"], record["redelivered"]), (None, True))

    def test_end_turn_with_no_current_turn_is_refused(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        self.assertEqual(self.mgr.end_turn("claude", "u-1", "normal"), {"ok": False, "error": "no_turn"})

    def test_a_turn_end_offline_stays_pending_and_reaches_the_next_sync(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.bind_and_admit(hub)
        self.deliver(hub, envelope("m-1"))
        self.agent_call(Path(bound["socket"]), {"tool": "mail_inbox", "args": {}})
        hub.close()
        wait_until(lambda: self.mgr._stream is None, what="the stream to end")
        reply = self.mgr.end_turn("claude", "u-1", "normal")
        self.assertEqual(reply, {"ok": False, "error": "hub_unreachable"})
        agent = self.root / "claude"
        self.assertEqual(self.read_json(agent / "pending_acks.json"),
                         [{"epoch": 1, "turn": "u-1", "end_reason": "normal", "acked": ["m-1"]}])
        second = self.start_stream()
        sync = second.wait_frame("sync")
        self.assertEqual(sync["agents"]["claude@node-1"]["pending_acks"][0]["acked"], ["m-1"])
        self.assertEqual(sync["agents"]["claude@node-1"]["tombstones"], ["m-1"])
        wait_until(lambda: self.read_json(agent / "pending_acks.json") == [],
                   what="the sync reply to confirm the pending ack")
        self.assertEqual(second.frames_of("turn_end"), [])


class InboxBudgetTests(ResidentCase):
    def test_mail_over_the_reply_budget_stays_unread_for_the_next_call(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.bind_and_admit(hub)
        socket_path = Path(bound["socket"])
        for index in range(3):
            self.deliver(hub, envelope(f"m-{index}", created=f"2026-11-15T10:0{index}:00Z", body="x" * 400))
        with patch.object(resident_node, "INBOX_REPLY_BUDGET", 1600):
            first = self.agent_call(socket_path, {"tool": "mail_inbox", "args": {}})
            self.assertEqual([m["id"] for m in first["messages"]], ["m-0", "m-1"])
            self.assertTrue(first["more"])
            self.assertEqual(hub.wait_frame("retrieved")["ids"], ["m-0", "m-1"])  # only what was returned
            second = self.agent_call(socket_path, {"tool": "mail_inbox", "args": {}})
            self.assertEqual([m["id"] for m in second["messages"]], ["m-2"])
            self.assertFalse(second["more"])
        with patch.object(resident_node, "INBOX_REPLY_BUDGET", 10):  # one message always goes out
            self.deliver(hub, envelope("m-9", created="2026-11-15T10:09:00Z"))
            self.assertEqual([m["id"] for m in self.agent_call(
                socket_path, {"tool": "mail_inbox", "args": {}})["messages"]], ["m-9"])

    def test_an_agent_exit_inside_a_turn_offers_its_mail_again(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        bound = self.bind_and_admit(hub)
        self.deliver(hub, envelope("m-1"))
        self.agent_call(Path(bound["socket"]), {"tool": "mail_inbox", "args": {}})
        self.mgr.agent_exit("claude", bound["epoch"], "crashed")
        record = self.read_json(self.root / "claude" / "spool" / "m-1.json")
        self.assertIsNone(record["_retrieved_turn"])
        self.assertTrue(record["redelivered"])


class SlotTests(ResidentCase):
    def test_a_busy_resident_slot_is_refused_without_asking_the_hub(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1", "agy@node-1")
        self.bind_and_admit(hub)
        self.assertTrue(self.mgr.begin_incarnation("agy")["ok"])
        self.assertEqual(self.mgr.admit("agy", "wake"), {"ok": False, "error": "slot_busy"})
        self.assertEqual([frame for frame in hub.frames if frame.get("t") == "admit"
                          and frame.get("agent") == "agy@node-1"], [])
        self.assertTrue(self.mgr.end_turn("claude", "u-1", "normal")["ok"])
        self.assertTrue(self.mgr.admit("agy", "wake")["ok"])  # the slot is free again
        self.assertEqual(self.daemon._ping()["resident_slot_free"], False)

    def test_a_hub_refusal_releases_the_slot(self) -> None:
        auto = self.auto()
        auto["admit"] = lambda frame: {"ok": False, "error": "budget"}  # the hub refuses every turn
        hub = self.start_stream(self.hub(auto))
        self.declare(hub, "claude@node-1")
        self.mgr.begin_incarnation("claude")
        self.assertEqual(self.mgr.admit("claude", "wake"), {"ok": False, "error": "budget"})
        self.assertTrue(self.daemon.take_resident_slot("probe"))  # the refusal released it again
        self.daemon.release_resident_slot("probe")

    def test_agent_exit_releases_the_slot(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        self.bind_and_admit(hub)
        self.assertTrue(self.mgr.agent_exit("claude", 1, "cli exited")["ok"])
        self.assertEqual(hub.wait_frame("agent_exit")["reason"], "cli exited")
        self.assertTrue(self.daemon.take_resident_slot("probe"))


class StreamTests(ResidentCase):
    def test_a_second_attach_closes_the_first_and_fails_its_in_flight_request(self) -> None:
        first = self.start_stream()
        silent = self.auto()
        silent["call"] = lambda frame: None  # never answered: the request stays in flight
        first.auto = silent
        box: Dict[str, Any] = {}

        def pending() -> None:
            box["reply"] = self.mgr.request("call", {"tool": "x", "args": {}})

        thread = threading.Thread(target=pending, daemon=True)
        thread.start()
        first.wait_frame("call")
        second = self.start_stream()
        second.wait_frame("hello")
        thread.join(5.0)
        self.assertEqual(box["reply"], {"ok": False, "error": "stream_closed"})
        self.assertTrue(self.mgr.request("call", {"tool": "x", "args": {}})["ok"])  # the new stream answers

    def test_ninety_seconds_of_silence_close_the_stream(self) -> None:
        hub = self.start_stream()
        self.clock.advance(resident_node.SILENCE_SECONDS + 1)
        for thread in self.threads:
            thread.join(5.0)
        self.assertFalse(any(thread.is_alive() for thread in self.threads))
        self.assertEqual(self.mgr.request("ping", {}), {"ok": False, "error": "hub_unreachable"})

    def test_a_request_without_a_reply_times_out_and_the_stream_stays(self) -> None:
        hub = self.start_stream()
        silent = self.auto()
        silent["call"] = lambda frame: None
        hub.auto = silent
        box: Dict[str, Any] = {}

        def pending() -> None:
            box["reply"] = self.mgr.request("call", {"tool": "x", "args": {}})

        thread = threading.Thread(target=pending, daemon=True)
        thread.start()
        hub.wait_frame("call")
        self.clock.advance(resident_node.REQUEST_TIMEOUT + 1)
        thread.join(5.0)
        self.assertEqual(box["reply"], {"ok": False, "error": "timeout"})
        reply = hub.request({"t": "deliver", "agent": "claude@node-9", "envelope": envelope("m-1")})
        self.assertEqual(reply.get("result"), "unknown_agent")  # the stream still serves frames


class TaskFrameTests(ResidentCase):
    """The resident-task frames of M2e: task_state and tasks against the owned-task table."""

    def ready(self, hub: FakeHub, task: str = "k-1", generation: int = 1, key: Optional[str] = None,
              agent: str = "claude@node-1") -> Dict[str, Any]:
        return hub.request({"t": "task_state", "agent": agent, "task": task, "generation": generation,
                            "state": "ready", "key": key or "a" * 64})

    def test_task_state_ready_writes_the_table_and_bad_frames_are_refused(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        key = "a" * 64
        self.assertTrue(self.ready(hub, key=key).get("ok"))
        table = self.mgr._task_table("claude")
        self.assertIsNotNone(table)
        self.assertEqual(table.entry("k-1"), {"generation": 1, "state": "ready", "key": key})
        self.assertEqual(self.read_json(self.root / "claude" / "tasks.json"),
                         {"k-1": {"generation": 1, "state": "ready", "key": key}})
        for frame in ({"t": "task_state", "agent": "claude@node-1", "task": "../x", "generation": 1,
                       "state": "ready", "key": key},
                      {"t": "task_state", "agent": "claude@node-1", "task": "k-1", "generation": "one",
                       "state": "ready", "key": key},
                      {"t": "task_state", "agent": "claude@node-1", "task": "k-1", "generation": 1,
                       "state": "pending", "key": key},
                      {"t": "task_state", "agent": "claude@node-1", "task": "k-1", "generation": 1,
                       "state": "ready", "key": "nope"},
                      {"t": "tasks", "agent": "claude@node-1", "owned": ["k-1"]}):
            self.assertEqual(hub.request(frame).get("error"), "bad_input", frame)
        self.assertEqual(hub.request({"t": "task_state", "agent": "agy@node-9", "task": "k-1",
                                      "generation": 1, "state": "ready", "key": key}).get("error"),
                         "unknown_agent")

    def test_a_released_frame_removes_and_an_older_generation_is_ignored(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        table = self.mgr._task_table("claude")
        assert table is not None
        self.ready(hub, generation=3, key="a" * 64)
        self.assertTrue(hub.request({"t": "task_state", "agent": "claude@node-1", "task": "k-1",
                                     "generation": 2, "state": "released", "key": None}).get("ok"))
        self.assertEqual(table.entry("k-1")["state"], "ready")  # the stale frame changed nothing
        self.assertTrue(hub.request({"t": "task_state", "agent": "claude@node-1", "task": "k-1",
                                     "generation": 3, "state": "released", "key": None}).get("ok"))
        self.assertIsNone(table.entry("k-1"))

    def test_the_tasks_frame_replaces_the_whole_table(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")
        table = self.mgr._task_table("claude")
        assert table is not None
        self.ready(hub, task="k-1", key="a" * 64)
        self.assertTrue(hub.request({"t": "tasks", "agent": "claude@node-1",
                                     "owned": {"k-2": {"generation": 1, "state": "pending", "key": None},
                                               "k-3": {"generation": 2, "state": "none", "key": None}}}).get("ok"))
        self.assertEqual(sorted(table.entries()), ["k-2", "k-3"])


class RepoToolTests(ResidentCase):
    """The five resident repo tools answered on the node (M2e), from a real imported snapshot."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        streams = Path(self.tmp.name) / "streams"
        streams.mkdir(mode=0o700)
        self.snap = build_snapshot(RepoTools(self.repo, self.sha), streams)
        with open(self.snap.path, "rb") as source:
            import_stream(worker.snapshot_root(), self.snap.key, source)

    def make_ready(self, hub: FakeHub, task: str = "k-1") -> None:
        self.declare(hub, "claude@node-1")
        reply = hub.request({"t": "task_state", "agent": "claude@node-1", "task": task,
                             "generation": 1, "state": "ready", "key": self.snap.key})
        self.assertTrue(reply.get("ok"), reply)

    def auto_calls(self) -> Dict[str, Any]:
        def call(frame: Dict[str, Any]) -> Dict[str, Any]:
            if frame["tool"] == "task_claim":
                return {"ok": True, "generation": 2, "snapshot": "ready", "key": self.snap.key}
            return {"ok": True}

        auto = self.auto()
        auto["call"] = call
        return auto

    def test_a_repo_call_is_answered_on_the_node_and_never_reaches_the_hub(self) -> None:
        hub = self.start_stream()
        self.make_ready(hub)
        bound = self.bind_and_admit(hub)
        reply = self.agent_call(Path(bound["socket"]),
                                {"tool": "repo_read", "args": {"task": "k-1", "path": "apps/agent/notes.txt"}})
        self.assertTrue(reply["ok"], reply)
        self.assertIn("alpha", reply["result"]["content"][0]["text"])
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(hub.frames_of("call"), [])  # nothing was forwarded
        self.assertTrue((self.root / "claude" / "tasks" / "k-1.calls.jsonl").exists())

    def test_a_repo_call_for_an_unknown_task_and_a_bad_task_id(self) -> None:
        hub = self.start_stream()
        self.make_ready(hub)
        bound = self.bind_and_admit(hub)
        socket_path = Path(bound["socket"])
        self.assertEqual(self.agent_call(socket_path, {"tool": "repo_list", "args": {"task": "k-9"}}),
                         {"ok": False, "error": "no_task"})
        self.assertEqual(self.agent_call(socket_path, {"tool": "repo_list", "args": {"task": "../x"}}),
                         {"ok": False, "error": "bad_input"})

    def test_a_claim_records_the_task_and_the_update_carries_the_call_log(self) -> None:
        hub = self.start_stream(self.hub(self.auto_calls()))
        self.make_ready(hub)
        bound = self.bind_and_admit(hub)
        socket_path = Path(bound["socket"])
        table = self.mgr._task_table("claude")
        assert table is not None
        # task_state ready arrives before the claim reply of the same generation (the reader thread
        # can run ahead): the claim must keep the ready entry, not downgrade it
        self.assertTrue(hub.request({"t": "task_state", "agent": "claude@node-1", "task": "k-2",
                                     "generation": 2, "state": "ready", "key": self.snap.key}).get("ok"))
        self.assertTrue(self.agent_call(socket_path, {"tool": "task_claim", "args": {"task": "k-2"},
                                                      "op_id": "op-0"})["ok"])
        self.assertEqual(table.entry("k-2"), {"generation": 2, "state": "ready", "key": self.snap.key})
        self.assertTrue(self.agent_call(socket_path, {"tool": "repo_read",
                                                      "args": {"task": "k-2", "path": "apps/agent/notes.txt"}})["ok"])
        reply = self.agent_call(socket_path, {
            "tool": "task_update",
            "args": {"task": "k-2", "generation": 2, "status": "completed", "result": "done",
                     "calls": {"records": [{"op": "forged"}], "dropped": False}},
            "op_id": "op-1"})
        self.assertTrue(reply["ok"], reply)
        frame = hub.frames_of("call")[-1]
        self.assertEqual(frame["tool"], "task_update")
        self.assertEqual([record["op"] for record in frame["args"]["calls"]["records"]], ["read"])
        self.assertFalse(frame["args"]["calls"]["dropped"])  # the daemon's log, not the forged one
        self.assertIsNone(table.entry("k-2"))  # the accepted update dropped the task
        self.assertFalse((self.root / "claude" / "tasks" / "k-2.calls.jsonl").exists())

    def test_a_log_that_would_not_fit_is_dropped_not_blocking_the_update(self) -> None:
        hub = self.start_stream(self.hub(self.auto_calls()))
        self.make_ready(hub)
        bound = self.bind_and_admit(hub)
        log = self.root / "claude" / "tasks" / "k-1.calls.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log.write_text(json.dumps({"op": "read", "target": "x", "pad": "y" * 4000}) + "\n",
                       encoding="utf-8")
        with patch.object(resident_node, "MAX_FRAME_BYTES", 64 * 1024 + 2048):
            reply = self.agent_call(Path(bound["socket"]), {"tool": "task_update",
                                                            "args": {"task": "k-1", "generation": 1,
                                                                     "status": "failed", "reason": "stuck"},
                                                            "op_id": "op-2"})
        self.assertTrue(reply["ok"], reply)
        frame = hub.frames_of("call")[-1]
        self.assertEqual(frame["args"]["calls"], {"records": [], "dropped": True})

    def test_a_release_and_a_refused_update_drop_the_task(self) -> None:
        hub = self.start_stream(self.hub(self.auto_calls()))
        self.make_ready(hub)
        bound = self.bind_and_admit(hub)
        socket_path = Path(bound["socket"])
        table = self.mgr._task_table("claude")
        assert table is not None
        self.assertTrue(self.agent_call(socket_path, {"tool": "task_release",
                                                      "args": {"task": "k-1", "generation": 1,
                                                               "reason": "stuck"}, "op_id": "op-3"})["ok"])
        self.assertIsNone(table.entry("k-1"))
        # a task that moved on: the hub refuses the update, the entry still goes
        self.make_ready(hub, task="k-3")
        auto = hub.auto
        auto["call"] = lambda frame: {"ok": False, "error": "task_released"}
        hub.auto = auto
        reply = self.agent_call(socket_path, {"tool": "task_update",
                                              "args": {"task": "k-3", "generation": 1,
                                                       "status": "completed", "result": "done"},
                                              "op_id": "op-4"})
        self.assertEqual(reply, {"ok": False, "error": "task_released"})
        self.assertIsNone(table.entry("k-3"))

    def test_retiring_the_agent_removes_the_table_with_the_directory(self) -> None:
        hub = self.start_stream()
        self.make_ready(hub)
        self.declare(hub, version=7)  # the list no longer names claude
        agent = self.root / "claude"
        wait_until(lambda: not agent.exists(), what="the agent to be retired")
        self.assertIsNone(self.mgr._task_table("claude"))


class OpenSandboxCase(ResidentCase):
    """The shared layout of the open-agent tests (O2/O3): a temp sandbox with SandboxPaths injected
    (the root uid and the gsa-<name> user and group are the test's own), so no root and no sudo."""

    VERSION = "2747b475ef4a"  # a C1-shaped sandbox version

    def setUp(self) -> None:
        super().setUp()
        self.sbox = Path(tempfile.mkdtemp(prefix="sgs", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.sbox, ignore_errors=True)
        wrapper = self._sandbox_file("guardian-sandbox-run")
        launcher = self._sandbox_file("launch.py")
        installed = self.sbox / "installed.json"
        installed.write_text(json.dumps(
            {"version": self.VERSION, "agents": ["claude", "agy"],
             "cli": resident_open.expected_cli_versions()}), encoding="utf-8")
        sock_root = self.sbox / "sock"
        sock_root.mkdir()
        for name in ("claude", "agy"):
            (sock_root / name).mkdir(mode=0o2750)
            os.chown(sock_root / name, -1, os.getgid())  # BSD mkdir takes the parent's group
        self.paths = resident_open.SandboxPaths(
            wrapper=wrapper, launcher=launcher, installed=installed, sock_root=sock_root,
            home_root=self.sbox / "home", root_uid=os.getuid(),
            daemon_uid=os.getuid, agent_gid=lambda name: os.getgid(),
            member_gids=lambda: [os.getgid()])
        # the case's manager again, now with the sandbox layout (no stream has started yet)
        self.mgr = resident_node.ResidentManager(self.daemon, root=self.root, clock=self.clock,
                                                 wall=self.wall, driver=self.driver,
                                                 sandbox_paths=self.paths)

    def _sandbox_file(self, name: str) -> Path:
        path = self.sbox / name
        path.write_text("#!/bin/sh\n# a stand-in for the root-installed file\n", encoding="utf-8")
        os.chmod(path, 0o755)
        return path

    def declare_open(self, hub: FakeHub, *agents: str, version: int = 5) -> Dict[str, Any]:
        """One agents frame in which every named agent is open and carries the sandbox version."""
        specs = [spec(address, tools="open", sandbox={"version": self.VERSION}) for address in agents]
        return hub.request({"t": "agents", "version": version, "agents": specs})


class OpenAgentTests(OpenSandboxCase):
    """Open agents (docs/design/resident-open-tools.md, O2): the hello capability (C5), the sandbox
    socket directory (C7) and retirement."""

    def test_hello_carries_the_capability_and_tracks_the_sandbox(self) -> None:
        hub = self.start_stream()
        hello = hub.frames_of("hello")[0]
        self.assertEqual((hello["open_tools"], hello["sandbox_version"]), (True, self.VERSION))
        self.paths.launcher.unlink()  # the sandbox broke (an update removed the launcher)
        second = self.start_stream()
        hello = second.frames_of("hello")[0]
        self.assertEqual((hello["open_tools"], hello["sandbox_version"]), (False, self.VERSION))

    def test_the_incarnation_socket_of_an_open_agent_sits_in_the_sandbox_directory(self) -> None:
        hub = self.start_stream()
        hub.request({"t": "agents", "version": 5, "agents": [
            spec("claude@node-1", tools="open", sandbox={"version": self.VERSION}),
            spec("agy@node-1")]})
        open_bound = self.mgr.begin_incarnation("claude")
        self.assertTrue(open_bound["ok"], open_bound)
        open_path = Path(open_bound["socket"])
        self.assertEqual(open_path, self.paths.sock_dir("claude") / "e1.sock")
        self.assertEqual(stat.S_IMODE(open_path.stat().st_mode), 0o660)  # umask 0117 around bind
        closed_bound = self.mgr.begin_incarnation("agy")
        closed_path = Path(closed_bound["socket"])
        self.assertEqual(closed_path, self.root / "agy" / "e1.sock")    # exactly as today
        self.assertEqual(stat.S_IMODE(closed_path.stat().st_mode), 0o600)
        # the mailbox MCP answers on the sandbox socket like on any other
        self.mgr.admit("claude", "wake")
        inbox = self.agent_call(open_path, {"tool": "mail_inbox", "args": {}})
        self.assertEqual((inbox["ok"], inbox["messages"]), (True, []))
        self.mgr.end_turn("claude", "u-1", "lost")

    def test_old_epoch_sockets_in_the_sandbox_directory_go_only_when_they_are_sockets(self) -> None:
        hub = self.start_stream()
        self.declare_open(hub, "claude@node-1")
        directory = self.paths.sock_dir("claude")
        stale = socket.socket(socket.AF_UNIX)
        self.addCleanup(stale.close)
        stale.bind(str(directory / "e9.sock"))
        plain = directory / "e4.sock"
        plain.write_text("not a socket", encoding="utf-8")
        bound = self.mgr.begin_incarnation("claude")
        self.assertEqual(Path(bound["socket"]).name, "e1.sock")
        self.assertFalse((directory / "e9.sock").exists())  # a socket: removed
        self.assertTrue(plain.exists())                     # a plain file: kept
        self.assertTrue(Path(bound["socket"]).exists())     # the current one: kept

    def test_a_restart_prunes_stale_sockets_of_open_agents_from_the_sandbox_directory(self) -> None:
        hub = self.start_stream()
        self.declare_open(hub, "claude@node-1")
        bound = self.mgr.begin_incarnation("claude")
        path = Path(bound["socket"])
        self.assertTrue(path.exists())
        fresh = resident_node.ResidentManager(self.daemon, root=self.root, clock=self.clock,
                                              wall=self.wall, sandbox_paths=self.paths)
        self.mgr = fresh  # tearDown shuts the fresh one down
        wait_until(lambda: not path.exists(), what="the stale sandbox socket to be pruned")

    def test_retiring_an_open_agent_clears_the_sock_directory_and_stops_the_units(self) -> None:
        hub = self.start_stream()
        self.declare_open(hub, "claude@node-1")
        bound = self.mgr.begin_incarnation("claude")
        directory = self.paths.sock_dir("claude")
        mcp = directory / "mcp" / "abc123def456"
        mcp.mkdir(parents=True)
        (mcp / "mailbox_mcp.py").write_text("x", encoding="utf-8")
        planted = directory / "planted.txt"
        planted.write_text("x", encoding="utf-8")
        with patch.object(resident_open, "stop", return_value=True) as stopped:
            reply = self.declare(hub, version=6)  # the list no longer names claude
            self.assertTrue(reply.get("ok"))
        stopped.assert_called_once_with("claude", self.paths)
        wait_until(lambda: not (self.root / "claude").exists(), what="the agent to be retired")
        self.assertTrue(directory.is_dir())       # the installer's directory stays (C7)
        self.assertFalse(mcp.exists())
        self.assertFalse(planted.exists())
        self.assertFalse(Path(bound["socket"]).exists())


class WorktreeTests(OpenSandboxCase):
    """task_worktree (O3): the daemon answers it on the node with a git bundle of the owned task's
    snapshot in the sandbox socket directory, and removes the bundle when the task leaves the table.
    The snapshot is a real imported one, so git runs in temp dirs."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        streams = Path(self.tmp.name) / "streams"
        streams.mkdir(mode=0o700)
        self.snap = build_snapshot(RepoTools(self.repo, self.sha), streams)
        with open(self.snap.path, "rb") as source:
            self.commit = import_stream(worker.snapshot_root(), self.snap.key, source)

    def ready(self, hub: FakeHub, task: str = "k-1", generation: int = 1,
              key: Optional[str] = None) -> None:
        reply = hub.request({"t": "task_state", "agent": "claude@node-1", "task": task,
                             "generation": generation, "state": "ready",
                             "key": key or self.snap.key})
        self.assertTrue(reply.get("ok"), reply)

    def worktree(self, socket_path: Path, task: Any = "k-1", generation: Any = 1) -> Dict[str, Any]:
        return self.agent_call(socket_path, {"tool": "task_worktree",
                                             "args": {"task": task, "generation": generation}})

    def open_bound(self, hub: FakeHub) -> Dict[str, Any]:
        self.declare_open(hub, "claude@node-1")
        return self.bind_and_admit(hub)

    def test_the_owned_task_gets_its_bundle_in_the_sandbox_socket_directory(self) -> None:
        hub = self.start_stream()
        bound = self.open_bound(hub)
        self.ready(hub)
        bundle = self.paths.sock_dir("claude") / "tasks" / "k-1.bundle"
        reply = self.worktree(Path(bound["socket"]))
        self.assertEqual(reply, {"ok": True, "bundle": str(bundle), "commit": self.commit})
        self.assertTrue(bundle.is_file())
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.paths.sock_dir("claude") / "tasks").stat().st_mode),
                         0o2750)
        self.assertEqual(hub.frames_of("call"), [])  # nothing was forwarded
        # a second call of the same task and commit just answers
        stamp = bundle.stat().st_mtime_ns
        self.assertEqual(self.worktree(Path(bound["socket"])), reply)
        self.assertEqual(bundle.stat().st_mtime_ns, stamp)

    def test_a_closed_agent_is_refused_and_nothing_is_written_for_it(self) -> None:
        hub = self.start_stream()
        self.declare(hub, "claude@node-1")  # a closed agent owns tasks just as well
        self.ready(hub)
        bound = self.bind_and_admit(hub)
        socket_path = Path(bound["socket"])
        self.assertEqual(self.worktree(socket_path), {"ok": False, "error": "not_open"})
        self.assertEqual(self.worktree(socket_path, task="k-9"), {"ok": False, "error": "not_open"})
        self.assertFalse((self.paths.sock_dir("claude") / "tasks").exists())
        self.assertEqual(hub.frames_of("call"), [])

    def test_a_bad_call_and_tasks_the_agent_does_not_own(self) -> None:
        hub = self.start_stream()
        bound = self.open_bound(hub)
        self.ready(hub)
        socket_path = Path(bound["socket"])
        for bad in ("../x", "a/b", "", "a b", "x" * 65):
            self.assertEqual(self.worktree(socket_path, task=bad), {"ok": False, "error": "bad_input"},
                             bad)
        self.assertEqual(self.worktree(socket_path, generation="one"),
                         {"ok": False, "error": "bad_input"})
        self.assertEqual(self.worktree(socket_path, task="k-9"), {"ok": False, "error": "not_owner"})
        self.assertEqual(self.worktree(socket_path, generation=2), {"ok": False, "error": "not_owner"})
        self.assertEqual(hub.frames_of("call"), [])
        self.assertFalse((self.paths.sock_dir("claude") / "tasks").exists())

    def test_a_task_without_a_snapshot_and_one_this_node_does_not_hold(self) -> None:
        hub = self.start_stream()
        self.declare_open(hub, "claude@node-1")
        table = self.mgr._task_table("claude")
        assert table is not None
        table.claimed("k-1", 1, "none", None)  # the task names no project
        self.ready(hub, task="k-2", key="b" * 64)  # a snapshot key this node does not hold
        bound = self.bind_and_admit(hub)
        socket_path = Path(bound["socket"])
        self.assertEqual(self.worktree(socket_path), {"ok": False, "error": "no_snapshot"})
        self.assertEqual(self.worktree(socket_path, task="k-2"), {"ok": False, "error": "no_snapshot"})
        self.assertFalse((self.paths.sock_dir("claude") / "tasks").exists())

    def test_the_bundle_goes_when_the_task_leaves_the_table(self) -> None:
        hub = self.start_stream()
        bound = self.open_bound(hub)
        self.ready(hub)
        socket_path = Path(bound["socket"])
        bundle = self.paths.sock_dir("claude") / "tasks" / "k-1.bundle"
        self.assertTrue(self.worktree(socket_path)["ok"])
        # an accepted task_update drops the task and with it the bundle (the call log already went)
        update = self.agent_call(socket_path, {"tool": "task_update",
                                               "args": {"task": "k-1", "generation": 1,
                                                        "status": "completed", "result": "done"},
                                               "op_id": "op-1"})
        self.assertTrue(update["ok"], update)
        self.assertFalse(bundle.exists())
        # a released task_state frame drops a re-readied task's bundle too
        self.ready(hub, generation=2)
        self.assertTrue(self.worktree(socket_path, generation=2)["ok"])
        reply = hub.request({"t": "task_state", "agent": "claude@node-1", "task": "k-1",
                             "generation": 2, "state": "released", "key": None})
        self.assertTrue(reply.get("ok"), reply)
        self.assertFalse(bundle.exists())
        # and so does a `tasks` frame that no longer names the task
        self.ready(hub, generation=3)
        self.assertTrue(self.worktree(socket_path, generation=3)["ok"])
        reply = hub.request({"t": "tasks", "agent": "claude@node-1", "owned": {}})
        self.assertTrue(reply.get("ok"), reply)
        self.assertFalse(bundle.exists())

    def test_a_task_update_carries_the_patch_and_drops_only_the_calls(self) -> None:
        hub = self.start_stream()
        bound = self.open_bound(hub)
        self.ready(hub)
        socket_path = Path(bound["socket"])
        self.assertTrue(self.worktree(socket_path)["ok"])
        log = self.root / "claude" / "tasks" / "k-1.calls.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log.write_text(json.dumps({"op": "read", "pad": "y" * 4000}) + "\n", encoding="utf-8")
        with patch.object(resident_node, "MAX_FRAME_BYTES", 64 * 1024 + 2048):
            reply = self.agent_call(socket_path, {
                "tool": "task_update",
                "args": {"task": "k-1", "generation": 1, "status": "completed", "result": "done",
                         "patch": "diff --git a/x b/x\n" + "+" + "x" * 4096 + "\n",
                         "patch_base": self.commit},
                "op_id": "op-2"})
        self.assertTrue(reply["ok"], reply)
        frame = hub.frames_of("call")[-1]
        self.assertEqual(frame["tool"], "task_update")
        self.assertIn("a/x", frame["args"]["patch"])         # the patch stayed
        self.assertEqual(frame["args"]["patch_base"], self.commit)
        self.assertEqual(frame["args"]["calls"], {"records": [], "dropped": True})  # the log gave way


if __name__ == "__main__":
    unittest.main()

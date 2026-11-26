"""mail_relay: the hub side of the M2 relay frame protocol, run over socket.socketpair() against
a fake daemon, with an injected clock (no real ssh, no network, no real sleeps over 0.5 s)."""

import json
import os
import queue
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import mail_relay
import mail_snapshots
import mail_store
import remote_agents
import resident_cards
import resident_registry
import testenv
from project_registry import Project, ProjectManager, ProjectValidationError
from repo_snapshot import build_snapshot
from test_mail_snapshots import FakeTransport
from test_repo_tools import git, make_repo


class FakeMonotonic:
    """A fake time.monotonic the tests advance by hand (the session reads only this)."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeDaemon:
    """The node's daemon: one end of a socket pair, one reader thread into a frame queue."""

    def __init__(self):
        self.hub_socket, self._sock = socket.socketpair()
        self._frames = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(4096)
            except OSError:
                chunk = b""
            if not chunk:
                self._frames.put(None)  # the hub closed the stream
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    self._frames.put(json.loads(line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._frames.put(None)
                    return

    def send(self, frame):
        self._sock.sendall((json.dumps(frame) + "\n").encode("utf-8"))

    def expect(self, kinds, timeout=2.0):
        """The next frame whose t is in kinds (a str or a set); others (pings) are skipped."""
        if isinstance(kinds, str):
            kinds = {kinds}
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {sorted(kinds)} frame within {timeout}s")
            item = self._frames.get(timeout=remaining)
            if item is None:
                raise AssertionError("the hub closed the stream")
            if item.get("t") in kinds:
                return item

    def expect_nothing(self, timeout=0.3):
        try:
            item = self._frames.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError(f"unexpected frame: {item}")

    def reply(self, rid, ok=True, **payload):
        self.send({"t": "reply", "re": rid, "ok": ok, **payload})

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def declaration_entry(name="claude", node="node-1", provider="claude"):
    # provider/driver must pass the repository's M0 table (claude and agy are resident headless)
    return {"name": name, "node": node, "provider": provider, "driver": "headless"}


def open_entry(name="claude", node="node-1", provider="claude"):
    # tools: "open" (resident-open-tools.md O2) on top of the closed declaration's shape
    return {**declaration_entry(name, node, provider), "tools": "open"}


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = FakeMonotonic()
        self.daemon = FakeDaemon()
        self.addCleanup(self.daemon.close)
        self.session = None
        self.thread = None
        self.stop = threading.Event()

    def tearDown(self):
        if self.thread is not None:
            self.stop.set()
            self.thread.join(timeout=5.0)
            self.assertFalse(self.thread.is_alive(), "the session thread did not stop")
        hub = getattr(self.daemon, "hub_socket", None)
        if hub is not None:
            try:
                hub.close()
            except OSError:
                pass

    def data_entries(self):
        return os.listdir(os.environ["GUARDIAN_DATA_DIR"])

    def write_declaration(self, *entries):
        path = resident_registry.declaration_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "agents": list(entries)}), encoding="utf-8")
        return path

    def seed(self):
        """A store the test thread seeds with (the session opens its own connection)."""
        return mail_store.MailStore()

    def register(self, name="claude", node="node-1"):
        address = f"{name}@{node}"
        self.seed().upsert_agent({"name": name, "node": node, "provider": "claude",
                                  "driver": "headless", "address": address})
        return address

    def start_session(self, node="node-1", ensurer=None):
        reader = self.daemon.hub_socket.makefile("rb")
        writer = self.daemon.hub_socket.makefile("wb")
        self.session = mail_relay.RelaySession(node, reader, writer, store=mail_store.MailStore(),
                                               clock=self.clock, ensurer=ensurer)
        self.session.POLL_INTERVAL = 0.05
        self.thread = threading.Thread(target=self.session.run, args=(self.stop,), daemon=True)
        self.thread.start()
        return self.session

    def settle(self, seconds=0.25):
        time.sleep(seconds)

    def hello(self):
        self.daemon.send({"t": "hello", "id": "d1", "protocol": mail_relay.PROTOCOL,
                          "version": "w-test", "started": "now"})
        return self.daemon.expect("reply")

    def sync(self, agents=None):
        self.daemon.send({"t": "sync", "id": "d2", "agents": agents or {}})
        return self.daemon.expect("reply")

    def handshake(self, agents=None):
        reply = self.hello()
        self.assertTrue(reply["ok"], reply)
        return self.sync(agents)

    def agent_row(self, address):
        rows = {row["address"]: row for row in mail_store.MailStore().agent_rows()}
        return rows[address]

    def mail_states(self):
        return {m["id"]: m["state"] for m in mail_store.MailStore().list_mail(limit=200)}


class Handshake(RelayTestCase):
    def test_protocol_mismatch_closes_and_creates_nothing(self):
        self.start_session()
        self.daemon.send({"t": "hello", "id": "d1", "protocol": 2})
        reply = self.daemon.expect("reply")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "protocol_mismatch")
        self.thread.join(timeout=2.0)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.session.close_reason, "protocol_mismatch")
        self.assertEqual(self.data_entries(), [])

    def test_first_frame_must_be_hello(self):
        self.start_session()
        self.daemon.send({"t": "sync", "id": "d1", "agents": {}})
        reply = self.daemon.expect("reply")
        self.assertEqual(reply["error"], "protocol_mismatch")
        self.thread.join(timeout=2.0)
        self.assertFalse(self.thread.is_alive())

    def test_request_before_sync_is_refused(self):
        self.start_session()
        self.assertTrue(self.hello()["ok"])
        self.daemon.send({"t": "bind_begin", "id": "d2", "agent": "claude@node-1"})
        reply = self.daemon.expect("reply")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "not_synced")


class SyncAndDelivery(RelayTestCase):
    def test_sync_reconciles_then_agents_then_deliver(self):
        address = self.register()
        self.write_declaration(declaration_entry())
        seed = self.seed()
        seed.bind_begin(address)
        seed.bind_complete(address, 1, "s-1")
        acked = seed.send("operator", address, "message", "read", "b", op_id="o1")["id"]
        turn = seed.admit_turn(address, 1, "wake")["turn"]
        seed.record_retrieved(address, 1, turn, [acked])
        spooled = seed.send("operator", address, "message", "spool", "b", op_id="o2")["id"]
        tombstoned = seed.send("operator", address, "message", "gone", "b", op_id="o3")["id"]
        lost = seed.send("operator", address, "message", "lost", "b", op_id="o4")["id"]
        seed.mark_delivered(address, [lost])

        self.start_session()
        reply = self.handshake({address: {"epoch": 1, "spool": [spooled],
                                          "tombstones": [tombstoned],
                                          "pending_acks": [{"epoch": 1, "turn": turn,
                                                            "end_reason": "normal",
                                                            "acked": [acked]}]}})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["applied"], [{"turn": turn, "ok": True}])
        self.assertEqual(reply["unknown"], [])
        states = self.mail_states()
        self.assertEqual(states[acked], "acked")        # the pending acknowledgement applied
        self.assertEqual(states[tombstoned], "acked")   # the daemon's tombstone
        self.assertEqual(states[spooled], "delivered")  # the node already holds it

        agents = self.daemon.expect("agents")
        self.assertEqual(agents["version"], 1)
        table = resident_registry.load_table()
        self.assertEqual(agents["agents"], [{
            "address": address, "name": "claude", "provider": "claude", "driver": "headless",
            "peers": [], "card": resident_cards.card_text({"address": address, "name": "claude",
                                                           "provider": "claude"}),
            "provider_spec": table["providers"]["claude"]["drivers"]["headless"]}])

        deliver = self.daemon.expect("deliver")  # the lost mail, redelivered, oldest first
        self.assertEqual(deliver["agent"], address)
        self.assertEqual(deliver["envelope"]["id"], lost)
        self.assertTrue(deliver["envelope"]["redelivered"])
        self.assertEqual(deliver["envelope"]["from"], "operator")
        self.daemon.reply(deliver["id"], result="spooled")
        self.settle()
        self.assertEqual(self.mail_states()[lost], "delivered")
        self.assertEqual(self.mail_states()[spooled], "delivered")

    def test_delivered_mail_is_not_resent_on_the_next_poll(self):
        address = self.register()
        self.write_declaration(declaration_entry())
        mail_id = self.seed().send("operator", address, "message", "s", "b", op_id="o1")["id"]
        self.start_session()
        self.handshake({address: {"epoch": 0, "spool": [], "tombstones": [],
                                  "pending_acks": []}})
        agents = self.daemon.expect("agents")
        self.daemon.reply(agents["id"], applied=agents["version"])
        deliver = self.daemon.expect("deliver")
        self.assertEqual(deliver["envelope"]["id"], mail_id)
        self.daemon.reply(deliver["id"], result="spooled")
        self.settle()
        self.assertEqual(self.mail_states()[mail_id], "delivered")
        self.daemon.expect("tasks")  # the agent owns nothing: an empty table
        self.clock.advance(3)
        self.settle()
        self.daemon.expect_nothing()

    def test_a_lost_deliver_reply_is_resent_after_the_timeout(self):
        address = self.register()
        self.write_declaration(declaration_entry())
        mail_id = self.seed().send("operator", address, "message", "s", "b", op_id="o1")["id"]
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        first = self.daemon.expect("deliver")
        self.assertEqual(first["envelope"]["id"], mail_id)
        # no answer: after REQUEST_TIMEOUT the request is dropped and the next poll resends it
        self.clock.advance(self.session.REQUEST_TIMEOUT + 5)
        self.settle()
        second = self.daemon.expect("deliver")
        self.assertEqual(second["envelope"]["id"], mail_id)
        self.assertNotEqual(second["id"], first["id"])
        self.daemon.reply(second["id"], result="spooled")
        self.settle()
        self.assertEqual(self.mail_states()[mail_id], "delivered")

    def test_tombstoned_and_unknown_agent_deliver_answers(self):
        address = self.register()
        self.write_declaration(declaration_entry())
        seed = self.seed()
        first = seed.send("operator", address, "message", "a", "b", op_id="o1")["id"]
        second = seed.send("operator", address, "message", "b", "b", op_id="o2")["id"]
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        deliver_a = self.daemon.expect("deliver")
        deliver_b = self.daemon.expect("deliver")
        self.daemon.reply(deliver_a["id"], result="tombstoned")
        self.daemon.reply(deliver_b["id"], result="unknown_agent")
        self.settle()
        states = self.mail_states()
        self.assertEqual(states[first], "acked")
        self.assertEqual(states[second], "queued")
        kinds = [e["kind"] for e in mail_store.MailStore().events()]
        self.assertIn("deliver_unknown_agent", kinds)


class DaemonRequests(RelayTestCase):
    def ready_agent(self, name="claude", node="node-1", session="s-1"):
        address = self.register(name, node)
        seed = self.seed()
        seed.bind_begin(address)
        seed.bind_complete(address, 1, session)
        return address

    def test_bind_admit_retrieve_turn_end_round_trip(self):
        self.write_declaration(declaration_entry())
        self.register()  # --resident-add writes the declaration and the store row together
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        address = "claude@node-1"

        self.daemon.send({"t": "bind_begin", "id": "d3", "agent": address})
        self.assertEqual(self.daemon.expect("reply")["epoch"], 1)
        self.daemon.send({"t": "bind_complete", "id": "d4", "agent": address, "epoch": 1})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply["ok"], reply)  # agy: no session id yet

        mail_id = self.seed().send("operator", address, "message", "s", "b", op_id="o1")["id"]
        self.daemon.send({"t": "admit", "id": "d5", "agent": address, "epoch": 1, "kind": "wake"})
        turn = self.daemon.expect("reply")["turn"]
        self.daemon.send({"t": "retrieved", "id": "d6", "agent": address, "epoch": 1,
                          "turn": turn, "ids": [mail_id]})
        self.assertTrue(self.daemon.expect("reply")["ok"])
        self.daemon.send({"t": "turn_end", "id": "d7", "agent": address, "epoch": 1,
                          "turn": turn, "end_reason": "normal", "acked": [mail_id],
                          "session_id": "sess-learned"})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply["ok"], reply)
        self.assertNotIn("resume_new_session", reply)  # the first id is stored, not a resume
        self.settle()
        self.assertEqual(self.mail_states()[mail_id], "acked")
        self.assertEqual(self.agent_row(address)["session_id"], "sess-learned")

        # a later turn with a different id ends the incarnation
        self.daemon.send({"t": "admit", "id": "d8", "agent": address, "epoch": 1,
                          "kind": "continuation"})
        turn = self.daemon.expect("reply")["turn"]
        self.daemon.send({"t": "turn_end", "id": "d9", "agent": address, "epoch": 1,
                          "turn": turn, "end_reason": "normal", "acked": [],
                          "session_id": "sess-fresh"})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply.get("resume_new_session"))
        store = mail_store.MailStore()
        row = next(e for e in store.events() if e["kind"] == "resume_new_session")
        self.assertEqual(row["agent"], address)

    def test_resume_report_answers_new_session_flag(self):
        self.write_declaration(declaration_entry())
        self.ready_agent(session="s-1")
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "resume_report", "id": "d3", "agent": "claude@node-1",
                          "epoch": 1, "session_id": "s-1"})
        self.assertEqual(self.daemon.expect("reply")["resume_new_session"], False)
        self.daemon.send({"t": "resume_report", "id": "d4", "agent": "claude@node-1",
                          "epoch": 1, "session_id": "s-2"})
        self.assertEqual(self.daemon.expect("reply")["resume_new_session"], True)

    def test_agent_of_another_node_is_refused(self):
        self.write_declaration(declaration_entry())
        other = self.register("agy", "node-2")  # exists in the store, declared on node-2
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        for t in ("bind_begin", "admit"):
            self.daemon.send({"t": t, "id": f"d-{t}", "agent": other, "epoch": 1, "kind": "wake"})
            reply = self.daemon.expect("reply")
            self.assertFalse(reply["ok"])
            self.assertEqual(reply["error"], "unknown_agent")
        self.daemon.send({"t": "call", "id": "d-call", "agent": other, "epoch": 1, "turn": "u-1",
                          "tool": "mail_agents", "args": {}})
        self.assertEqual(self.daemon.expect("reply")["error"], "unknown_agent")

    def test_agent_exit_marks_the_agent_down(self):
        self.write_declaration(declaration_entry())
        self.ready_agent()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "agent_exit", "id": "d3", "agent": "claude@node-1", "epoch": 1,
                          "reason": "cli exited"})
        self.assertTrue(self.daemon.expect("reply")["ok"])
        self.settle()
        self.assertEqual(self.agent_row("claude@node-1")["state"], "down")


class ToolCalls(RelayTestCase):
    def turn_ready(self, peers=("agy",)):
        entries = [declaration_entry("claude")]
        entries.extend(declaration_entry(name) for name in peers)
        self.write_declaration(*entries)
        address = self.register("claude")
        for name in peers:
            self.register(name)
        seed = self.seed()
        seed.bind_begin(address)
        seed.bind_complete(address, 1, "s-1")
        turn = seed.admit_turn(address, 1, "wake")["turn"]
        return address, turn

    def test_mail_send_sender_comes_from_the_frame(self):
        address, turn = self.turn_ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "call", "id": "d3", "agent": address, "epoch": 1, "turn": turn,
                          "op_id": "op-1", "tool": "mail_send",
                          "args": {"to": "agy@node-1", "subject": "hi", "body": "there",
                                   "from": "operator", "from_kind": "operator", "hop": 9,
                                   "sender": "spoof", "creator": "spoof", "caller": "spoof"}})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["hop"], 1)  # computed from the turn, not the caller's field
        self.settle()
        row = mail_store.MailStore().list_mail(limit=10)[0]
        self.assertEqual((row["sender"], row["from_kind"], row["recipient"], row["hop"]),
                         (address, "agent", "agy@node-1", 1))
        # the queued mail is pushed to the node's agent on the next poll
        deliver = self.daemon.expect("deliver")
        self.assertEqual(deliver["agent"], "agy@node-1")
        self.assertEqual(deliver["envelope"]["from"], address)
        self.daemon.reply(deliver["id"], result="spooled")

    def test_mail_agents_lists_peers(self):
        address, turn = self.turn_ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "call", "id": "d3", "agent": address, "epoch": 1, "turn": turn,
                          "tool": "mail_agents", "args": {}})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], [{"address": "agy@node-1", "name": "agy",
                                            "node": "node-1", "provider": "claude",
                                            "state": "down"}])

    def test_task_create_and_list_through_the_relay(self):
        address, turn = self.turn_ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "call", "id": "d3", "agent": address, "epoch": 1, "turn": turn,
                          "op_id": "op-2", "tool": "task_create",
                          "args": {"title": "review", "detail": "the mail store"}})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply["ok"], reply)
        task_id = reply["id"]
        self.daemon.send({"t": "call", "id": "d4", "agent": address, "tool": "task_list",
                          "args": {}})
        reply = self.daemon.expect("reply")
        self.assertEqual(reply["result"][0]["id"], task_id)

    def test_unknown_tool_and_bad_args(self):
        address, turn = self.turn_ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "call", "id": "d3", "agent": address, "epoch": 1, "turn": turn,
                          "tool": "rm_rf", "args": {"path": "/"}})
        self.assertEqual(self.daemon.expect("reply")["error"], "unknown_tool")
        self.daemon.send({"t": "call", "id": "d4", "agent": address, "epoch": 1, "turn": turn,
                          "tool": "mail_send", "args": {"subject": "no recipient"}})
        self.assertEqual(self.daemon.expect("reply")["error"], "bad_input")
        # the session is still alive and answering
        self.daemon.send({"t": "call", "id": "d5", "agent": address, "tool": "task_list",
                          "args": {}})
        self.assertTrue(self.daemon.expect("reply")["ok"])

    def test_unknown_frame(self):
        address, turn = self.turn_ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.send({"t": "wat", "id": "d3"})
        self.assertEqual(self.daemon.expect("reply")["error"], "unknown_frame")


class OpenTools(RelayTestCase):
    """The open-tool decisions of the agents frame (resident-open-tools.md O2, contract C5/C6).
    The hub's sandbox version is faked (the checkout may not hold the launcher file yet) and the
    inventory is a temp servers.json, so each case turns only the hello fields and the recorded
    verdict."""

    HUB_VERSION = "1a2b3c4d5e6f"

    def setUp(self):
        super().setUp()
        patcher = patch.object(mail_relay.sandbox_info, "sandbox_version",
                               return_value=self.HUB_VERSION)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.servers_path = Path(self._tmp.name) / "servers.json"
        self.record_verdict(version=self.HUB_VERSION, passed=True)
        env = patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(self.servers_path)})
        env.start()
        self.addCleanup(env.stop)

    def record_verdict(self, version, passed=True):
        """The inventory's `resident_sandbox` (what worker_probe.record-sandbox stores)."""
        verdict = {"version": version, "passed": passed, "checked": "2026-11-19",
                   "failures": [] if passed else ["sudo allowed"]}
        self.servers_path.write_text(
            json.dumps([{"id": "node-1", "resident_sandbox": verdict}]), encoding="utf-8")

    def open_hello(self, open_tools=True, sandbox_version="keep"):
        version = self.HUB_VERSION if sandbox_version == "keep" else sandbox_version
        self.daemon.send({"t": "hello", "id": "d1", "protocol": mail_relay.PROTOCOL,
                          "version": "w-test", "started": "now", "open_tools": open_tools,
                          "sandbox_version": version})
        return self.daemon.expect("reply")

    def handshake_open(self, **hello):
        reply = self.open_hello(**hello)
        self.assertTrue(reply["ok"], reply)
        return self.sync({})

    def downs(self):
        return [e for e in mail_store.MailStore().events() if e["kind"] == "agent_down"]

    def test_an_old_daemon_never_sees_the_open_agent(self):
        address = self.register()
        self.register("agy")
        self.write_declaration(open_entry(), declaration_entry("agy"))
        self.start_session()
        self.handshake({})  # the old daemon's hello: no open_tools, no sandbox_version
        agents = self.daemon.expect("agents")
        self.assertEqual([spec["name"] for spec in agents["agents"]], ["agy"])
        self.assertNotIn("tools", agents["agents"][0])
        self.assertEqual(self.agent_row(address)["state"], "down")
        events = [e for e in self.downs() if e["agent"] == address]
        self.assertEqual(events[-1]["detail"]["reason"], "node_without_open_tools")
        self.daemon.expect("tasks")  # the closed agent's empty task set still follows
        mail_id = self.seed().send("operator", address, "message", "s", "b", op_id="o1")["id"]
        self.clock.advance(3)
        self.settle()
        self.daemon.expect_nothing()  # no deliver for an agent the daemon never saw
        self.assertEqual(self.mail_states()[mail_id], "queued")

    def test_a_missing_verdict_blocks_the_agent(self):
        address = self.register()
        self.write_declaration(open_entry())
        self.servers_path.write_text(json.dumps([{"id": "node-1"}]), encoding="utf-8")
        self.start_session()
        self.handshake_open()
        spec = self.daemon.expect("agents")["agents"][0]
        self.assertEqual(spec["blocked"], "sandbox_unverified")
        self.assertEqual(self.agent_row(address)["state"], "down")
        self.assertEqual([e["detail"]["reason"] for e in self.downs()], ["sandbox_unverified"])

    def test_a_failed_verdict_blocks_the_agent(self):
        self.register()
        self.write_declaration(open_entry())
        self.record_verdict(version=self.HUB_VERSION, passed=False)
        self.start_session()
        self.handshake_open()
        spec = self.daemon.expect("agents")["agents"][0]
        self.assertEqual(spec["blocked"], "sandbox_unverified")

    def test_a_verdict_for_another_sandbox_version_blocks(self):
        self.register()
        self.write_declaration(open_entry())
        self.record_verdict(version="000000000000")
        self.start_session()
        self.handshake_open()
        spec = self.daemon.expect("agents")["agents"][0]
        self.assertEqual(spec["blocked"], "sandbox_stale")

    def test_a_node_sandbox_of_another_version_blocks(self):
        self.register()
        self.write_declaration(open_entry())
        self.start_session()
        self.handshake_open(sandbox_version="000000000000")
        spec = self.daemon.expect("agents")["agents"][0]
        self.assertEqual(spec["blocked"], "sandbox_version_mismatch")

    def test_a_matching_pair_sends_the_open_spec_unblocked(self):
        address = self.register()
        self.write_declaration(open_entry())
        self.start_session()
        self.handshake_open()
        spec = self.daemon.expect("agents")["agents"][0]
        self.assertEqual(spec["tools"], "open")
        self.assertEqual(spec["sandbox"], {"version": self.HUB_VERSION})
        self.assertNotIn("blocked", spec)
        self.assertIn("8. Open tools", spec["card"])  # the open card reaches the node too
        self.assertEqual(self.downs(), [])  # the open decision never set the agent down
        self.daemon.expect("tasks")

    def test_a_re_probed_node_is_unblocked_without_a_declaration_change(self):
        self.register()
        self.write_declaration(open_entry())
        self.record_verdict(version="000000000000")  # stale at first
        self.start_session()
        self.handshake_open()
        blocked = self.daemon.expect("agents")
        self.assertEqual(blocked["agents"][0]["blocked"], "sandbox_stale")
        # a daemon that ignored `blocked` is refused by the hub as well
        for t in ("bind_begin", "admit"):
            self.daemon.send({"t": t, "id": f"h-{t}", "agent": "claude@node-1", "epoch": 1,
                              "kind": "wake"})
            reply = self.daemon.expect("reply")
            self.assertEqual((reply["ok"], reply["error"], reply["detail"]),
                             (False, "agent_held", "sandbox_stale"))
        self.record_verdict(version=self.HUB_VERSION)  # the operator re-ran the probe
        self.clock.advance(3)
        self.settle()
        unblocked = self.daemon.expect("agents")
        self.assertEqual(unblocked["version"], blocked["version"] + 1)
        self.assertNotIn("blocked", unblocked["agents"][0])
        # and now the agent binds and comes up
        self.daemon.send({"t": "bind_begin", "id": "b1", "agent": "claude@node-1"})
        epoch = self.daemon.expect("reply")["epoch"]
        self.daemon.send({"t": "bind_complete", "id": "b2", "agent": "claude@node-1",
                          "epoch": epoch, "session_id": "s-1"})
        self.assertTrue(self.daemon.expect("reply")["ok"])
        self.assertEqual(self.agent_row("claude@node-1")["state"], "ready")

    def test_closed_agents_are_unchanged_next_to_a_blocked_open_agent(self):
        self.register()
        self.register("agy")
        self.write_declaration(open_entry(), declaration_entry("agy"))
        self.record_verdict(version="000000000000")
        self.start_session()
        self.handshake_open()
        specs = self.daemon.expect("agents")["agents"]
        by_name = {spec["name"]: spec for spec in specs}
        self.assertEqual(by_name["claude"]["blocked"], "sandbox_stale")
        for key in ("tools", "sandbox", "blocked"):
            self.assertNotIn(key, by_name["agy"])
        self.assertEqual([e["agent"] for e in self.downs()], ["claude@node-1"])


class TaskSnapshots(RelayTestCase):
    """The M2e claim path: sha pinning at creation, the pending/ready claim answer, the background
    ensure with its task_state frames, the post-sync tasks frame and the stored call log. The
    ensurer runs the real snapshot build against a temp repo; only ssh is faked."""

    def setUp(self):
        super().setUp()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(Path(self._tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        patcher = patch.object(ProjectManager, "load",
                               return_value=Project(id="projx", workspace_raw=str(self.repo)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.transport = FakeTransport()
        self.ensurer = mail_snapshots.SnapshotEnsurer("node-1", transport=self.transport)

    def agents_ready(self, *names):
        """Declares and binds the named agents on node-1; one name returns its address alone."""
        self.write_declaration(*(declaration_entry(name) for name in names))
        addresses = []
        for name in names:
            address = self.register(name)
            seed = self.seed()
            seed.bind_begin(address)
            seed.bind_complete(address, 1, f"s-{name}")
            addresses.append(address)
        return addresses[0] if len(addresses) == 1 else addresses

    def project_task(self, assignee=None, op="op-task"):
        result = self.seed().task_create("operator", "read the code", "detail", op_id=op,
                                         assignee=assignee, project="projx", sha=self.sha)
        self.assertTrue(result["ok"], result)
        return result["id"]

    def turn(self, address):
        return self.seed().admit_turn(address, 1, "wake")["turn"]

    def claim(self, address, turn, task_id, id="d-claim", op_id=None):
        self.daemon.send({"t": "call", "id": id, "agent": address, "epoch": 1, "turn": turn,
                          "op_id": op_id, "tool": "task_claim", "args": {"task": task_id}})
        return self.daemon.expect("reply")

    def sync_tasks(self):
        """Starts the session and drains the handshake; returns the agent's `tasks` frame."""
        self.start_session(ensurer=self.ensurer)
        self.handshake({})
        self.daemon.expect("agents")
        return self.daemon.expect("tasks")

    def task_row(self, task_id):
        return next(t for t in mail_store.MailStore().task_list("operator") if t["id"] == task_id)

    def test_task_create_through_the_relay_pins_the_resolved_sha(self):
        address = self.agents_ready("claude")
        turn = self.turn(address)
        self.sync_tasks()
        self.daemon.send({"t": "call", "id": "d-new", "agent": address, "epoch": 1, "turn": turn,
                          "op_id": "op-new", "tool": "task_create",
                          "args": {"title": "review", "project": "projx"}})
        reply = self.daemon.expect("reply")
        self.assertTrue(reply["ok"], reply)
        row = self.task_row(reply["id"])
        self.assertEqual((row["project"], row["sha"]), ("projx", self.sha))

    def test_task_create_with_an_unresolvable_project_is_refused_before_the_store(self):
        address = self.agents_ready("claude")
        turn = self.turn(address)
        self.sync_tasks()
        with patch.object(ProjectManager, "load",
                          side_effect=ProjectValidationError("no project 'ghost'")):
            self.daemon.send({"t": "call", "id": "d-bad", "agent": address, "epoch": 1,
                              "turn": turn, "op_id": "op-bad", "tool": "task_create",
                              "args": {"title": "x", "project": "ghost"}})
            reply = self.daemon.expect("reply")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad_project")
        self.assertEqual(self.seed().task_list("operator"), [])  # the store was never called

    def test_a_known_key_answers_ready_and_sends_no_task_state(self):
        address = self.agents_ready("claude")
        key, _allowed = self.ensurer.ensure("projx", self.sha)  # the node already holds it
        self.assertEqual(self.sync_tasks()["owned"], {})  # the agent owns nothing yet
        task = self.project_task(assignee=address)
        # the assignment queued a wake notice for the agent: answer its deliver, so the stream is
        # quiet below (the assertion is about task_state, not about this correct notice); the
        # session polls on the fake clock, so move it past one poll interval
        self.clock.advance(3)
        notice = self.daemon.expect("deliver")
        self.assertEqual(notice["envelope"]["subject"], "task_assigned")
        self.daemon.reply(notice["id"], result="spooled")
        reply = self.claim(address, self.turn(address), task)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["snapshot"], "ready")
        self.assertEqual(reply["key"], key)
        self.assertEqual((reply["project"], reply["sha"]), ("projx", self.sha))
        self.settle()
        self.daemon.expect_nothing()  # no task_state: the daemon already has the key

    def test_a_pending_claim_gets_task_state_ready_after_the_ensure(self):
        address = self.agents_ready("claude")
        self.sync_tasks()
        task = self.project_task(assignee=address)
        reply = self.claim(address, self.turn(address), task)
        self.assertEqual(reply["snapshot"], "pending")
        state = self.daemon.expect("task_state")
        self.assertEqual((state["agent"], state["task"], state["state"], state["generation"]),
                         (address, task, "ready", reply["generation"]))
        self.assertEqual(state["key"], self.ensurer.known_key("projx", self.sha))
        self.assertEqual(self.transport.uploads[0][1], state["key"])

    def test_a_replayed_claim_answer_gets_the_ready_snapshot(self):
        address = self.agents_ready("claude")
        self.sync_tasks()
        task = self.project_task(assignee=address)
        turn = self.turn(address)
        first = self.claim(address, turn, task, op_id="op-again")
        self.assertEqual(first["snapshot"], "pending")
        self.daemon.expect("task_state")
        again = self.claim(address, turn, task, id="d-again", op_id="op-again")
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["generation"], first["generation"])
        self.assertEqual(again["snapshot"], "ready")
        self.assertEqual(again["key"], self.ensurer.known_key("projx", self.sha))

    def test_a_failed_upload_releases_the_task(self):
        address = self.agents_ready("claude")
        self.transport.fail_upload = True
        self.sync_tasks()
        task = self.project_task(assignee=address)
        reply = self.claim(address, self.turn(address), task)
        self.assertEqual(reply["snapshot"], "pending")
        state = self.daemon.expect("task_state")
        self.assertEqual((state["state"], state["key"], state["generation"]),
                         ("released", None, reply["generation"]))
        row = self.task_row(task)
        self.assertEqual((row["status"], row["generation"]), ("pending", reply["generation"] + 1))
        self.assertTrue(row["reason"].startswith("snapshot_failed"))

    def test_a_late_failure_releases_nothing_after_a_reclaim(self):
        address, other = self.agents_ready("claude", "agy")
        gate = threading.Event()

        def builder(tools, directory):
            gate.wait(5.0)
            raise mail_snapshots.TaskSnapshotError("snapshot upload refused: no space")

        self.ensurer = mail_snapshots.SnapshotEnsurer(
            "node-1", transport=FakeTransport(fail_upload=True), builder=builder)
        self.sync_tasks()
        task = self.project_task()  # open: the other agent may claim it after the release
        reply = self.claim(address, self.turn(address), task)
        self.assertEqual(reply["snapshot"], "pending")
        # behind the relay's back (the 10-minute rule, another hub path): the claim is released and
        # the task re-claimed at a newer generation this session never saw
        seed = self.seed()
        self.assertTrue(seed.task_release(address, task, reply["generation"], "gave up")["ok"])
        other_turn = seed.admit_turn(other, 1, "wake")["turn"]
        reclaimed = seed.task_claim(other, task, op_id="op-re", epoch=1, turn=other_turn)
        self.assertTrue(reclaimed["ok"], reclaimed)
        gate.set()  # the ensure now fails, long after the claim it was started for
        self.settle(0.5)
        row = self.task_row(task)
        self.assertEqual((row["status"], row["generation"], row["owner"]),
                         ("in_progress", reclaimed["generation"], other))
        self.daemon.expect_nothing(0.3)  # no task_state: a late failure releases no newer claim

    def test_two_claims_of_the_same_pair_share_one_ensure(self):
        first, second = self.agents_ready("claude", "agy")
        gate = threading.Event()

        def builder(tools, directory):
            gate.wait(5.0)
            return build_snapshot(tools, directory)

        transport = FakeTransport()
        self.ensurer = mail_snapshots.SnapshotEnsurer("node-1", transport=transport, builder=builder)
        self.sync_tasks()
        task_a = self.project_task(assignee=first, op="op-a")
        task_b = self.project_task(assignee=second, op="op-b")
        reply_a = self.claim(first, self.turn(first), task_a, id="d-a")
        reply_b = self.claim(second, self.turn(second), task_b, id="d-b")
        self.assertEqual([reply_a["snapshot"], reply_b["snapshot"]], ["pending", "pending"])
        self.assertEqual(len(self.session._ensuring), 1)  # one thread for the shared pair
        gate.set()
        states = sorted((self.daemon.expect("task_state"), self.daemon.expect("task_state")),
                        key=lambda frame: frame["task"])
        self.assertEqual([frame["task"] for frame in states], sorted([task_a, task_b]))
        self.assertEqual([frame["state"] for frame in states], ["ready", "ready"])
        self.assertEqual(transport.ops(), ["snapshot_has"])  # one check, one upload
        self.assertEqual(len(transport.uploads), 1)

    def test_the_tasks_frame_after_sync_lists_the_in_progress_tasks(self):
        address = self.agents_ready("claude")
        seed = self.seed()
        project_task = self.project_task(assignee=address)
        plain_task = seed.task_create("operator", "no code", op_id="op-plain",
                                      assignee=address)["id"]
        turn = seed.admit_turn(address, 1, "wake")["turn"]
        for task in (project_task, plain_task):
            self.assertTrue(seed.task_claim(address, task, op_id=f"op-c{task}", epoch=1,
                                            turn=turn)["ok"])
        self.start_session(ensurer=self.ensurer)
        self.handshake({})
        self.daemon.expect("agents")
        owned = self.daemon.expect("tasks")["owned"]
        self.assertEqual(sorted(owned), sorted([project_task, plain_task]))
        self.assertEqual(owned[plain_task], {"generation": 1, "state": "none", "key": None})
        self.assertEqual(owned[project_task]["state"], "pending")
        state = self.daemon.expect("task_state")  # the sync's pending entry started the ensure
        self.assertEqual((state["task"], state["state"]), (project_task, "ready"))

    def call_record(self, **overrides):
        record = {"op": "read", "target": "cli/tool.py", "ok": True, "bytes": 9,
                  "truncated": False, "error": None, "paths": ["cli/tool.py"],
                  "spans": {"cli/tool.py": [[1, 9]]}, "mentions": ["cli/tool.py"], "limit": None}
        record.update(overrides)
        return record

    def test_task_update_stores_the_filtered_calls(self):
        address = self.agents_ready("claude")
        self.ensurer.ensure("projx", self.sha)  # the snapshot's paths are then known without a build
        self.sync_tasks()
        task = self.project_task(assignee=address)
        turn = self.turn(address)
        claimed = self.claim(address, turn, task)
        self.assertEqual(claimed["snapshot"], "ready")
        self.daemon.send({"t": "call", "id": "d-done", "agent": address, "epoch": 1, "turn": turn,
                          "op_id": "op-done", "tool": "task_update",
                          "args": {"task": task, "generation": claimed["generation"],
                                   "status": "completed", "result": "two findings",
                                   "calls": {"records": [self.call_record(
                                       paths=["cli/tool.py", "/etc/shadow"],
                                       spans={"cli/tool.py": [[1, 9]], "gone.py": [[2, 4]]},
                                       mentions=["cli/tool.py", "outside/secret.py"], spoof=True)],
                                       "dropped": False}}})
        self.assertTrue(self.daemon.expect("reply")["ok"])
        row = self.task_row(task)
        self.assertEqual(row["calls"], {"records": 1, "dropped": False})  # counts only
        full = mail_store.MailStore().task_calls("operator", task)["calls"]
        record = full["records"][0]
        self.assertEqual(record["paths"], ["cli/tool.py"])
        self.assertEqual(record["spans"], {"cli/tool.py": [[1, 9]]})
        self.assertEqual(record["mentions"], ["cli/tool.py"])
        self.assertNotIn("spoof", record)
        self.assertEqual(full["filtered"], 3)  # /etc/shadow, gone.py, outside/secret.py

    def test_oversize_calls_are_dropped_not_stored(self):
        address = self.agents_ready("claude")
        self.sync_tasks()
        task = self.project_task(assignee=address)
        turn = self.turn(address)
        claimed = self.claim(address, turn, task)
        self.daemon.expect("task_state")
        with patch.object(remote_agents, "MAX_CALLS_BYTES", 1):
            self.daemon.send({"t": "call", "id": "d-big", "agent": address, "epoch": 1,
                              "turn": turn, "op_id": "op-big", "tool": "task_update",
                              "args": {"task": task, "generation": claimed["generation"],
                                       "status": "completed", "result": "r",
                                       "calls": {"records": [self.call_record()],
                                                 "dropped": False}}})
            self.assertTrue(self.daemon.expect("reply")["ok"])
        self.assertEqual(self.task_row(task)["calls"], {"records": 0, "dropped": True})

    def test_task_update_without_a_project_keeps_no_records(self):
        address = self.agents_ready("claude")
        self.sync_tasks()
        plain = self.seed().task_create("operator", "no code", op_id="op-plain",
                                        assignee=address)["id"]
        turn = self.turn(address)
        claimed = self.claim(address, turn, plain)
        self.assertEqual(claimed["snapshot"], "none")
        self.daemon.send({"t": "call", "id": "d-done", "agent": address, "epoch": 1, "turn": turn,
                          "op_id": "op-done", "tool": "task_update",
                          "args": {"task": plain, "generation": claimed["generation"],
                                   "status": "completed", "result": "done",
                                   "calls": {"records": [self.call_record()], "dropped": False}}})
        self.assertTrue(self.daemon.expect("reply")["ok"])
        self.assertEqual(self.task_row(plain)["calls"], {"records": 0, "dropped": False})


class NoAgentsSnapshots(RelayTestCase):
    def test_no_declared_agent_starts_no_thread_and_builds_no_ensurer(self):
        self.start_session()  # no declaration, no injected ensurer
        self.handshake({})
        self.daemon.expect("agents")  # the empty declaration
        self.daemon.expect_nothing()  # no tasks frame, no ensure, nothing else
        self.assertIsNone(self.session._ensurer)  # never built: no transport exists to call
        self.assertEqual(self.session._ensuring, {})
        self.assertEqual(self.session._waiting, {})


class StreamLoss(RelayTestCase):
    def ready(self):
        self.register()
        self.write_declaration(declaration_entry())
        seed = self.seed()
        seed.bind_begin("claude@node-1")
        seed.bind_complete("claude@node-1", 1, "s-1")

    def test_eof_marks_the_node_down(self):
        self.ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.expect("tasks")  # M2e: the declared agent's (empty) task set follows the sync
        self.daemon.close()
        self.thread.join(timeout=2.0)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.session.close_reason, "stream_end")
        self.assertEqual(self.agent_row("claude@node-1")["state"], "down")
        events = [e for e in mail_store.MailStore().events() if e["kind"] == "agent_down"]
        self.assertEqual(events[0]["detail"]["reason"], "stream_lost")

    def test_the_reconnecting_sync_restores_the_stream_lost_agent(self):
        address = self.register()
        self.write_declaration(declaration_entry())
        seed = self.seed()
        seed.bind_begin(address)
        seed.bind_complete(address, 1, "s-1")
        self.start_session()
        self.handshake({address: {"epoch": 1, "spool": [], "tombstones": [],
                                  "pending_acks": []}})
        self.stop.set()  # the relay restarts (a hub deploy): the stream is lost, the agent goes down
        self.thread.join(timeout=5.0)
        self.assertFalse(self.thread.is_alive())
        row = self.agent_row(address)
        self.assertEqual((row["state"], row["session_id"]), ("down", "s-1"))
        events = [e for e in mail_store.MailStore().events() if e["kind"] == "agent_down"]
        self.assertEqual(events[0]["detail"]["reason"], "stream_lost")

        self.daemon = FakeDaemon()  # the node daemon reconnects within a second...
        self.addCleanup(self.daemon.close)
        self.stop = threading.Event()
        self.start_session()
        reply = self.handshake({address: {"epoch": 1, "ready": True, "spool": [],  # ...still bound
                                          "tombstones": [], "pending_acks": []}})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.agent_row(address)["state"], "ready")
        self.assertTrue(mail_store.MailStore().admit_turn(address, 1, "wake")["ok"])

    def test_liveness_closes_after_ninety_seconds(self):
        self.ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.clock.advance(self.session.LIVENESS_TIMEOUT + 1)
        self.thread.join(timeout=2.0)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.session.close_reason, "liveness")
        self.assertEqual(self.agent_row("claude@node-1")["state"], "down")

    def test_ping_is_sent_every_thirty_seconds(self):
        self.ready()
        self.start_session()
        self.handshake({})
        self.daemon.expect("agents")
        self.daemon.expect("tasks")
        self.daemon.expect_nothing(0.15)  # quiet before the first interval
        self.clock.advance(self.session.PING_INTERVAL)
        self.settle()
        ping = self.daemon.expect("ping")
        self.daemon.reply(ping["id"])
        self.daemon.expect_nothing(0.15)  # one ping per interval, not a burst
        self.clock.advance(self.session.PING_INTERVAL)
        self.settle()
        self.daemon.expect("ping")


class WireForms(unittest.TestCase):
    def test_relay_argv(self):
        self.assertEqual(mail_relay.relay_argv("node-1"), [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=30", "--", "node-1",
            "python3 ~/.guardian/worker/worker.py relay"])

    def test_envelope_and_agent_spec(self):
        row = {"id": "m-1", "thread": "t-1", "sender": "claude@node-1", "from_kind": "agent",
               "recipient": "agy@node-1", "kind": "message", "subject": "s", "body": "b",
               "task": None, "reply_to": None, "hop": 2, "created": "c", "expires": "e",
               "redelivered": 1, "state": "queued", "turn": "u-1"}
        self.assertEqual(mail_relay.envelope(row), {
            "id": "m-1", "thread": "t-1", "from": "claude@node-1", "from_kind": "agent",
            "to": "agy@node-1", "kind": "message", "subject": "s", "body": "b", "task": None,
            "reply_to": None, "hop": 2, "created": "c", "expires": "e", "redelivered": True})
        table = {"providers": {"claude": {"default_driver": "headless",
                                          "drivers": {"headless": {"command": ["claude"]}}}}}
        spec = mail_relay.agent_spec({"address": "claude@node-1", "name": "claude",
                                      "provider": "claude"}, table, peers=["agy@node-1"])
        self.assertEqual(spec, {"address": "claude@node-1", "name": "claude", "provider": "claude",
                                "driver": "headless", "peers": ["agy@node-1"],
                                "card": resident_cards.card_text({"address": "claude@node-1",
                                                                  "name": "claude",
                                                                  "provider": "claude"}),
                                "provider_spec": {"command": ["claude"]}})
        card = mail_relay.card_text({"address": "a@node-1", "name": "a", "provider": "claude"})
        self.assertIn("a@node-1", card)
        self.assertIn(f"[resident card v{resident_cards.RESIDENT_CARD_VERSION}]", card)

    def test_open_agent_spec_and_closed_immutability(self):
        table = {"providers": {"claude": {"default_driver": "headless",
                                          "drivers": {"headless": {"command": ["claude"]}}}}}
        entry = {"address": "claude@node-1", "name": "claude", "provider": "claude",
                 "tools": "open"}
        spec = mail_relay.agent_spec(entry, table, peers=[], sandbox_version="abc123def456")
        self.assertEqual(spec["tools"], "open")
        self.assertEqual(spec["sandbox"], {"version": "abc123def456"})
        self.assertNotIn("blocked", spec)
        blocked = mail_relay.agent_spec(entry, table, peers=[], sandbox_version="abc123def456",
                                        blocked="sandbox_stale")
        self.assertEqual(blocked["blocked"], "sandbox_stale")
        # a closed entry ignores the open arguments: its spec stays exactly the M2 shape
        closed = mail_relay.agent_spec({"address": "a@node-1", "name": "a", "provider": "claude"},
                                       table, peers=[], sandbox_version="abc123def456",
                                       blocked="sandbox_stale")
        for key in ("tools", "sandbox", "blocked"):
            self.assertNotIn(key, closed)

    def test_effective_peers(self):
        entries = [{"address": "a@n1"}, {"address": "b@n1"}, {"address": "c@n2"}]
        self.assertEqual(mail_relay.effective_peers(entries[0], entries), ["b@n1", "c@n2"])
        self.assertEqual(mail_relay.effective_peers({"address": "c@n2", "peers": ["a@n1"]},
                                                    entries), ["a@n1"])


class Runner(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)

    def data_entries(self):
        return os.listdir(os.environ["GUARDIAN_DATA_DIR"])

    def run_runner(self, until, timeout=5.0, **kwargs):
        """Runs a RelayRunner until the condition holds (or the timeout passes), then stops it."""
        runner = mail_relay.RelayRunner(poll=0.05, **kwargs)
        stop = threading.Event()
        thread = threading.Thread(target=runner.run, args=(stop,), daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while not until(runner) and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        return runner

    def test_no_declaration_starts_nothing_and_creates_no_file(self):
        spawns = []

        def spawn(node):
            spawns.append(node)
            raise AssertionError("no node is declared")

        self.run_runner(lambda runner: False, timeout=0.3, spawn=spawn)
        self.assertEqual(spawns, [])
        self.assertEqual(self.data_entries(), [])

    def test_one_node_spawns_and_backs_off(self):
        self.node_spawned = []

        def spawn(node):
            self.node_spawned.append(node)
            hub, peer = socket.socketpair()
            peer.close()  # EOF at once: the session ends without a handshake
            return None, hub.makefile("rb"), hub.makefile("wb")

        path = resident_registry.declaration_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "agents": [declaration_entry()]}),
                        encoding="utf-8")
        backoffs = (0.05, 0.06, 0.07, 0.08)
        runner = self.run_runner(lambda r: len(r.waits) >= 5, timeout=8.0, spawn=spawn,
                                 backoffs=backoffs)
        self.assertTrue(self.node_spawned)
        self.assertEqual(set(self.node_spawned), {"node-1"})
        self.assertEqual(runner.waits[:5], [0.05, 0.06, 0.07, 0.08, 0.08])
        # no store file: the empty declaration wrote nothing but the declaration itself
        self.assertEqual(self.data_entries(), ["resident-agents.json"])
        self.assertEqual(mail_relay.RelayRunner.BACKOFFS, (5.0, 10.0, 30.0, 60.0))


class GuardianFlag(unittest.TestCase):
    def test_mail_relay_flag_runs_main(self):
        import guardian

        with patch("mail_relay.main", return_value=0) as mock_main:
            handled = guardian.handle_resident_commands(SimpleNamespace(mail_relay=True))
        self.assertTrue(handled)
        mock_main.assert_called_once_with()

    def test_other_flags_do_not_run_main(self):
        import guardian

        with patch("mail_relay.main", return_value=0) as mock_main:
            handled = guardian.handle_resident_commands(SimpleNamespace())
        self.assertFalse(handled)
        mock_main.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""The M2 store additions of mail_store: schema 2 (bind columns), the bind timeout and retry
rule, session ids learned on turn_end, the relay marks (delivered, tombstoned), the node view and
the `sync` reconcile (docs/design/resident-agents.md, "Resend on reconnect" and "Binding")."""

import os
import sqlite3
import unittest

import mail_store
import testenv
from test_mail_store import make_clock, make_ready, open_turn, refuse, register


class RelayStoreTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = make_clock()
        self.store = mail_store.MailStore(now=self.clock)

    def data_entries(self):
        return os.listdir(os.environ["GUARDIAN_DATA_DIR"])

    def agent_row(self, address):
        row = next(a for a in self.store.agent_rows() if a["address"] == address)
        return row

    def mail_states(self):
        return {m["id"]: (m["state"], m["redelivered"]) for m in self.store.list_mail(limit=200)}


class SchemaV2(RelayStoreTestCase):
    def test_fresh_store_has_the_bind_columns(self):
        register(self.store)
        conn = self.store._ready()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
        self.assertIn("bind_started", columns)
        self.assertIn("bind_failures", columns)
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(version["value"], str(mail_store.SCHEMA_VERSION))
        self.assertEqual(self.agent_row("claude@node-1")["bind_failures"], 0)

    def test_version_1_store_is_migrated(self):
        path = mail_store.default_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(path)
        old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE agents (
            address TEXT PRIMARY KEY, name TEXT NOT NULL, node TEXT NOT NULL, provider TEXT NOT NULL,
            driver TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'down', epoch INTEGER NOT NULL DEFAULT 0,
            session_id TEXT, not_ready_since TEXT, peers_json TEXT, created TEXT NOT NULL,
            modified TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '1');
        INSERT INTO agents VALUES('claude@node-1', 'claude', 'node-1', 'claude', 'headless',
                                  'ready', 4, 's-old', NULL, NULL, '2026-11-15T09:00:00Z',
                                  '2026-11-15T09:00:00Z');
        """)
        old.commit()
        old.close()
        store = mail_store.MailStore(now=self.clock)
        rows = store.agent_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["epoch"], 4)
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["session_id"], "s-old")
        self.assertEqual(row["bind_failures"], 0)
        self.assertIsNone(row["bind_started"])
        conn = store._ready()
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(version["value"], str(mail_store.SCHEMA_VERSION))
        # the migrated columns work: a bind fills them
        self.assertEqual(store.bind_begin("claude@node-1")["epoch"], 5)


class BindTimeout(RelayStoreTestCase):
    def address(self):
        address = register(self.store)
        make_ready(self.store, address)
        return address

    def timeout_once(self, address):
        self.store.bind_begin(address)
        self.clock.advance(int(mail_store.BIND_TIMEOUT.total_seconds()) - 1)
        self.assertEqual(self.store.check_bind_timeouts(), [])  # one second early: still binding
        self.clock.advance(1)
        self.assertEqual(self.store.check_bind_timeouts(), [address])

    def test_binding_times_out_to_bind_failed(self):
        address = self.address()
        self.timeout_once(address)
        row = self.agent_row(address)
        self.assertEqual(row["state"], "bind_failed")
        self.assertEqual(row["bind_failures"], 1)
        self.assertIsNone(row["bind_started"])
        self.assertTrue(any(e["kind"] == "bind_failed" for e in self.store.events()))

    def test_third_failure_refuses_until_declaration_changes(self):
        address = self.address()
        for _ in range(3):
            self.timeout_once(address)
        refused = self.store.bind_begin(address)
        self.assertTrue(refuse(refused, "bind_retries_exhausted"))
        # a declaration change (provider or driver) ends the refusal
        entry = {"name": "claude", "node": "node-1", "provider": "agy", "driver": "headless",
                 "address": address}
        self.assertTrue(self.store.upsert_agent(entry)["ok"])
        self.assertEqual(self.agent_row(address)["bind_failures"], 0)
        self.assertTrue(self.store.bind_begin(address)["ok"])

    def test_successful_bind_resets_the_count(self):
        address = self.address()
        for _ in range(2):
            self.timeout_once(address)
        bind = self.store.bind_begin(address)  # the third attempt succeeds
        self.assertTrue(self.store.bind_complete(address, bind["epoch"])["ok"])
        self.assertEqual(self.agent_row(address)["bind_failures"], 0)
        self.assertIsNone(self.agent_row(address)["bind_started"])
        self.assertTrue(self.store.bind_begin(address)["ok"])  # not refused

    def test_check_bind_timeouts_creates_no_store(self):
        self.assertEqual(self.store.check_bind_timeouts(), [])
        self.assertEqual(self.data_entries(), [])


class SessionsOnTurnEnd(RelayStoreTestCase):
    def test_first_session_id_is_learned_on_turn_end(self):
        address = register(self.store)
        bind = self.store.bind_begin(address)
        self.assertTrue(self.store.bind_complete(address, bind["epoch"])["ok"])  # agy: no id yet
        self.assertIsNone(self.agent_row(address)["session_id"])
        turn = open_turn(self.store, address, bind["epoch"])
        answer = self.store.end_turn(address, bind["epoch"], turn, session_id="sess-1")
        self.assertEqual(answer, {"ok": True, "turn": turn, "end_reason": "normal"})
        self.assertEqual(self.agent_row(address)["session_id"], "sess-1")
        conn = self.store._ready()
        row = conn.execute("SELECT session_id FROM incarnations WHERE address=? AND epoch=?",
                           (address, bind["epoch"])).fetchone()
        self.assertEqual(row["session_id"], "sess-1")

    def test_later_different_session_ends_the_incarnation(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)  # binds with a generated session id
        turn = open_turn(self.store, address, epoch)
        answer = self.store.end_turn(address, epoch, turn, session_id="sess-fresh")
        self.assertTrue(answer.pop("resume_new_session"))
        self.assertEqual(answer["ok"], True)
        conn = self.store._ready()
        row = conn.execute("SELECT end_reason FROM incarnations WHERE address=? AND epoch=?",
                           (address, epoch)).fetchone()
        self.assertEqual(row["end_reason"], "resume_new_session")
        # the turn itself still ended normally: nothing is left open
        turns = conn.execute("SELECT ended IS NULL AS open FROM turns WHERE id=?", (turn,)).fetchone()
        self.assertEqual(turns["open"], 0)

    def test_same_or_empty_session_id_changes_nothing(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        stored = self.agent_row(address)["session_id"]
        turn = open_turn(self.store, address, epoch)
        self.assertNotIn("resume_new_session",
                         self.store.end_turn(address, epoch, turn, session_id=stored))
        turn = open_turn(self.store, address, epoch)
        self.assertNotIn("resume_new_session",
                         self.store.end_turn(address, epoch, turn, session_id=None))
        self.assertEqual(self.agent_row(address)["session_id"], stored)


class DeliveryMarks(RelayStoreTestCase):
    def test_mark_delivered_only_queued_mail_of_the_recipient(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        mail_a = self.store.send("operator", first, "message", "s", "b", op_id="d1")["id"]
        mail_b = self.store.send("operator", second, "message", "s", "b", op_id="d2")["id"]
        self.assertEqual(self.store.mark_delivered(first, [mail_a, mail_b, "m-ghost"]),
                         [mail_a])
        self.assertEqual(self.store.mark_delivered(first, [mail_a]), [])  # already delivered
        self.assertEqual(self.store.mark_delivered(first, []), [])
        states = self.mail_states()
        self.assertEqual(states[mail_a], ("delivered", 0))
        self.assertEqual(states[mail_b], ("queued", 0))

    def test_mark_tombstoned_never_touches_retrieved(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        queued = self.store.send("operator", first, "message", "s", "b", op_id="t1")["id"]
        delivered = self.store.send("operator", first, "message", "s", "b", op_id="t2")["id"]
        retrieved = self.store.send("operator", first, "message", "s", "b", op_id="t3")["id"]
        self.store.mark_delivered(first, [delivered])
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        self.store.record_retrieved(first, epoch, turn, [retrieved])
        self.assertEqual(self.store.mark_tombstoned(first, [queued, delivered, retrieved]),
                         [queued, delivered])
        self.assertEqual(self.store.mark_tombstoned(first, [queued]), [])  # acked already
        states = self.mail_states()
        self.assertEqual(states[queued][0], "acked")
        self.assertEqual(states[delivered][0], "acked")
        self.assertEqual(states[retrieved][0], "retrieved")  # its turn settles it
        kinds = {e["kind"] for e in self.store.events()}
        self.assertIn("mail_tombstoned", kinds)

    def test_marks_create_no_store(self):
        self.assertEqual(self.store.mark_delivered("claude@node-1", ["m-1"]), [])
        self.assertEqual(self.store.mark_tombstoned("claude@node-1", ["m-1"]), [])
        self.assertEqual(self.data_entries(), [])


class NodeView(RelayStoreTestCase):
    def setUp(self):
        super().setUp()
        self.first = register(self.store, "claude")
        self.second = register(self.store, "agy")
        self.other = register(self.store, "solo", node="node-2")

    def test_node_agents(self):
        rows = self.store.node_agents("node-1")
        self.assertEqual([row["address"] for row in rows], sorted([self.first, self.second]))
        self.assertEqual(self.store.node_agents("ghost-node"), [])

    def test_set_node_down_skips_retiring_and_down(self):
        make_ready(self.store, self.first)
        make_ready(self.store, self.second)
        make_ready(self.store, self.other)
        self.store.set_retiring(self.second)
        self.assertEqual(self.store.set_node_down("node-1", "stream_lost"), [self.first])
        self.assertEqual(self.agent_row(self.first)["state"], "down")
        self.assertEqual(self.agent_row(self.second)["state"], "retiring")
        self.assertEqual(self.agent_row(self.other)["state"], "ready")  # other node untouched
        events = [e for e in self.store.events() if e["kind"] == "agent_down"]
        self.assertEqual(events[0]["detail"]["reason"], "stream_lost")
        self.assertEqual(self.store.set_node_down("node-1", "stream_lost"), [])  # already down

    def test_peer_view_holds_no_session_ids_or_epochs(self):
        register(self.store, "buddy", peers=[self.first])
        for address in (self.first, self.second):
            make_ready(self.store, address)  # binds store session ids and epochs
        view = self.store.peer_view(self.second)  # implicit peers: everyone else
        by_address = {row["address"]: row for row in view}
        self.assertEqual(sorted(by_address), sorted([self.first, self.other, "buddy@node-1"]))
        for row in view:
            self.assertEqual(sorted(row), ["address", "name", "node", "provider", "state"])
        self.assertEqual(by_address[self.other]["node"], "node-2")
        self.assertEqual(by_address[self.first]["state"], "ready")
        self.assertEqual(self.store.peer_view("buddy@node-1"),
                         [{"address": self.first, "name": "claude", "node": "node-1",
                           "provider": "claude", "state": "ready"}])
        self.assertEqual(self.store.peer_view("ghost@node-1"), [])


class Reconcile(RelayStoreTestCase):
    def setUp(self):
        super().setUp()
        self.address = register(self.store, "claude")
        self.epoch = make_ready(self.store, self.address)

    def queued(self, op):
        return self.store.send("operator", self.address, "message", "s", "b", op_id=op)["id"]

    def test_sync_applies_acks_tombstones_and_spool(self):
        acked = self.queued("a1")
        turn = open_turn(self.store, self.address, self.epoch)
        self.store.record_retrieved(self.address, self.epoch, turn, [acked])
        tombstoned = self.queued("a2")
        spooled = self.queued("a3")
        lost = self.queued("a4")
        self.store.mark_delivered(self.address, [lost])
        result = self.store.reconcile("node-1", {self.address: {
            "epoch": self.epoch, "spool": [spooled], "tombstones": [tombstoned],
            "pending_acks": [{"epoch": self.epoch, "turn": turn, "end_reason": "normal",
                              "acked": [acked]}]}})
        self.assertEqual(result, {"ok": True, "applied": [{"turn": turn, "ok": True}],
                                  "unknown": [], "restored": []})
        states = self.mail_states()
        self.assertEqual(states[acked][0], "acked")      # the pending acknowledgement
        self.assertEqual(states[tombstoned][0], "acked")  # the daemon's tombstone
        self.assertEqual(states[spooled][0], "delivered")  # the node already holds it
        self.assertEqual(states[lost], ("queued", 1))      # the node lost it: redelivered

    def test_sync_lists_unknown_and_other_nodes_agents(self):
        result = self.store.reconcile("node-1", {"ghost@node-1": {}, "solo@node-2": {},
                                                 self.address: {"spool": [], "tombstones": [],
                                                                "pending_acks": []}})
        self.assertEqual(result["unknown"], ["ghost@node-1", "solo@node-2"])
        self.assertEqual(result["applied"], [])

    def test_wrong_epoch_ack_counts_as_applied(self):
        turn = open_turn(self.store, self.address, self.epoch)
        self.store.bind_begin(self.address)  # the epoch moved on: the ack cannot match
        result = self.store.reconcile("node-1", {self.address: {
            "pending_acks": [{"epoch": self.epoch, "turn": turn, "end_reason": "normal",
                              "acked": []}]}})
        self.assertEqual(result["applied"], [{"turn": turn, "ok": False}])

    def test_expired_mail_is_never_put_back(self):
        mail_id = self.queued("e1")
        self.store.mark_delivered(self.address, [mail_id])
        self.clock.advance(int(mail_store.LIFETIME.total_seconds()) + 1)
        result = self.store.reconcile("node-1", {self.address: {"spool": [], "tombstones": []}})
        self.assertEqual(result["ok"], True)
        self.assertEqual(self.mail_states()[mail_id][0], "expired")

    def test_session_id_of_a_pending_ack_is_learned(self):
        # a second agent bound without a session id (agy): the sync's ack carries the first one
        address = register(self.store, "agy")
        bind = self.store.bind_begin(address)
        self.assertTrue(self.store.bind_complete(address, bind["epoch"])["ok"])
        turn = open_turn(self.store, address, bind["epoch"])
        result = self.store.reconcile("node-1", {address: {
            "epoch": bind["epoch"], "spool": [], "tombstones": [],
            "pending_acks": [{"epoch": bind["epoch"], "turn": turn, "end_reason": "normal",
                              "acked": [], "session_id": "sess-from-sync"}]}})
        self.assertEqual(result["applied"][0]["ok"], True)
        self.assertEqual(self.agent_row(address)["session_id"], "sess-from-sync")


class ReconcileRestore(RelayStoreTestCase):
    """A hub-side relay restart marks the node's agents `down` although their incarnations live on
    (the node never rebinds: its local state did not change). The daemon's `sync` reports each
    agent's `ready`, and only a `down` agent at the store's exact epoch goes back to `ready`."""

    def setUp(self):
        super().setUp()
        self.address = register(self.store, "claude")
        self.epoch = make_ready(self.store, self.address)

    def stream_lost(self):
        """The production shape: bound and ready, then the relay stream lost."""
        self.assertEqual(self.store.set_node_down("node-1", "stream_lost"), [self.address])

    def restored_events(self):
        return [e for e in self.store.events() if e["kind"] == "agent_restored"]

    def test_ready_at_the_epoch_restores_a_down_agent(self):
        self.stream_lost()
        result = self.store.reconcile("node-1", {self.address: {
            "epoch": self.epoch, "ready": True, "spool": [], "tombstones": []}})
        self.assertEqual(result["restored"], [self.address])
        row = self.agent_row(self.address)
        self.assertEqual(row["state"], "ready")
        self.assertIsNone(row["not_ready_since"])
        events = self.restored_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent"], self.address)
        self.assertEqual(events[0]["detail"], {"epoch": self.epoch, "reason": "stream_resumed"})

    def test_ready_false_or_missing_restores_nothing(self):
        self.stream_lost()
        result = self.store.reconcile("node-1", {self.address: {
            "epoch": self.epoch, "ready": False, "spool": [], "tombstones": []}})
        self.assertEqual(result["restored"], [])
        result = self.store.reconcile("node-1", {self.address: {  # an older node sends no ready
            "epoch": self.epoch, "spool": [], "tombstones": []}})
        self.assertEqual(result["restored"], [])
        row = self.agent_row(self.address)
        self.assertEqual(row["state"], "down")
        self.assertIsNotNone(row["not_ready_since"])
        self.assertEqual(self.restored_events(), [])

    def test_an_epoch_mismatch_restores_nothing(self):
        self.stream_lost()
        for epoch in (self.epoch + 1, True):  # a newer epoch, and a bool is not an epoch
            result = self.store.reconcile("node-1", {self.address: {
                "epoch": epoch, "ready": True, "spool": [], "tombstones": []}})
            self.assertEqual(result["restored"], [], epoch)
        self.assertEqual(self.agent_row(self.address)["state"], "down")
        self.assertEqual(self.restored_events(), [])

    def test_a_down_for_another_reason_is_never_restored(self):
        # the daemon reported the CLI exited: the hub's word stands until a fresh bind
        self.assertTrue(self.store.set_down(self.address, "agent_exit")["ok"])
        result = self.store.reconcile("node-1", {self.address: {
            "epoch": self.epoch, "ready": True, "spool": [], "tombstones": []}})
        self.assertEqual(result["restored"], [])
        self.assertEqual(self.agent_row(self.address)["state"], "down")
        # the latest down decides: a stream loss after an exit leaves the agent already down,
        # so set_node_down skips it and the exit reason stays the last one
        self.assertEqual(self.store.set_node_down("node-1", "stream_lost"), [])
        result = self.store.reconcile("node-1", {self.address: {
            "epoch": self.epoch, "ready": True, "spool": [], "tombstones": []}})
        self.assertEqual(result["restored"], [])
        self.assertEqual(self.restored_events(), [])

    def test_binding_bind_failed_and_retiring_are_never_touched(self):
        failing = register(self.store, "codex")
        self.store.bind_begin(failing)
        self.clock.advance(int(mail_store.BIND_TIMEOUT.total_seconds()) + 1)
        self.assertEqual(self.store.check_bind_timeouts(), [failing])
        binding = register(self.store, "agy")
        self.store.bind_begin(binding)  # a fresh bind_started: still binding, not timed out
        retiring = register(self.store, "gemini")
        make_ready(self.store, retiring)
        self.store.set_retiring(retiring)
        for address, state in ((binding, "binding"), (failing, "bind_failed"),
                               (retiring, "retiring")):
            result = self.store.reconcile("node-1", {address: {
                "epoch": self.agent_row(address)["epoch"], "ready": True,
                "spool": [], "tombstones": []}})
            self.assertEqual(result["restored"], [])
            self.assertEqual(self.agent_row(address)["state"], state)
        self.assertEqual(self.restored_events(), [])

    def test_pending_acks_apply_before_the_restore(self):
        turn = open_turn(self.store, self.address, self.epoch)
        self.stream_lost()  # the stream died while the turn was open
        result = self.store.reconcile("node-1", {self.address: {
            "epoch": self.epoch, "ready": True, "spool": [], "tombstones": [],
            "pending_acks": [{"epoch": self.epoch, "turn": turn, "end_reason": "normal",
                              "acked": []}]}})
        self.assertEqual(result["applied"], [{"turn": turn, "ok": True}])
        self.assertEqual(result["restored"], [self.address])
        self.assertEqual(self.agent_row(self.address)["state"], "ready")
        self.assertTrue(self.store.admit_turn(self.address, self.epoch, "wake")["ok"])


if __name__ == "__main__":
    unittest.main()

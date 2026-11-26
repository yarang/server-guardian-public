"""The session address kind of the mail store (S1a of docs/design/operator-sessions.md): link
leases and fencing, session binds, the derived states, the offline release, session mail (peers,
kinds, rate limit, the session hop rule), the session peers list and the store's views."""

import os
import sqlite3
import unittest
from unittest.mock import patch

import mail_store
import mail_sessions
import testenv
from test_mail_store import make_clock, make_ready, open_turn, refuse, register


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = make_clock()
        self.store = mail_store.MailStore(now=self.clock)

    def claim(self, workstation="ws-mac-1"):
        result = self.store.link_claim(workstation)
        self.assertTrue(result["ok"], result)
        return result["lease"]

    def bind(self, workstation, lease, name, cli_session="cli-1", **kwargs):
        result = self.store.session_bind(workstation, lease, name, cli_session, **kwargs)
        self.assertTrue(result["ok"], result)
        return result["address"]

    def link_row(self, workstation):
        conn = self.store._ready()
        return dict(conn.execute("SELECT * FROM links WHERE workstation=?",
                                 (workstation,)).fetchone())

    def session_row(self, address):
        return next(row for row in self.store.session_rows() if row["address"] == address)

    def mail_row(self, mail_id):
        return next(m for m in self.store.list_mail(limit=200) if m["id"] == mail_id)


class Schema(SessionTestCase):
    def tables(self, conn):
        return {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def version(self, conn):
        return conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]

    def test_fresh_store_is_version_5_with_both_tables(self):
        self.claim()
        conn = self.store._ready()
        tables = self.tables(conn)
        self.assertIn("links", tables)
        self.assertIn("sessions", tables)
        self.assertEqual(self.version(conn), str(mail_store.SCHEMA_VERSION))
        self.assertEqual(self.store.session_rows(), [])  # reads create nothing to list

    def test_version_4_store_gains_both_tables_and_stamps_5(self):
        self.claim()
        self.store.close()
        path = mail_store.default_store_path()
        old = sqlite3.connect(path)
        old.execute("DROP TABLE links")
        old.execute("DROP TABLE sessions")
        old.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        old.commit()
        old.close()
        fresh = mail_store.MailStore(now=self.clock)
        self.assertEqual(fresh.agent_rows(), [])  # any open migrates and stamps the version
        conn = fresh._ready()
        tables = self.tables(conn)
        self.assertIn("links", tables)
        self.assertIn("sessions", tables)
        self.assertEqual(self.version(conn), str(mail_store.SCHEMA_VERSION))
        self.assertEqual(fresh.link_claim("ws-mac-1")["lease"], 1)  # the gained tables work


class LinkFencing(SessionTestCase):
    def test_second_claim_supersedes_the_old_lease_everywhere(self):
        first = self.claim()
        address = self.bind("ws-mac-1", first, "alpha")
        second = self.claim()  # the daemon redials: the new stream ends the old one
        self.assertEqual(self.session_row(address)["state"], "offline")
        self.assertTrue(refuse(self.store.session_bind("ws-mac-1", first, "beta", "cli-beta"),
                               "superseded"))
        self.assertTrue(refuse(self.store.session_send("ws-mac-1", first, "alpha", "operator",
                                                       "message", "s", "b", op_id="s1"),
                               "superseded"))
        self.assertTrue(refuse(self.store.session_peers_list("ws-mac-1", first, "alpha"),
                               "superseded"))
        mail_id = self.store.send("operator", address, "message", "s", "b", op_id="o1")["id"]
        self.assertTrue(refuse(self.store.session_mark_delivered("ws-mac-1", first, [mail_id]),
                               "superseded"))
        self.assertEqual(self.mail_row(mail_id)["state"], "queued")  # nothing changed
        self.assertTrue(refuse(self.store.link_end("ws-mac-1", first, "stale"), "superseded"))
        row = self.link_row("ws-mac-1")
        self.assertEqual((row["lease"], row["state"]), (second, "up"))
        self.assertFalse(self.store.link_current("ws-mac-1", first))
        self.assertTrue(self.store.link_current("ws-mac-1", second))

    def test_link_end_makes_sessions_offline(self):
        lease = self.claim()
        address = self.bind("ws-mac-1", lease, "alpha")
        self.assertTrue(self.store.link_end("ws-mac-1", lease, "daemon stop")["ok"])
        row = self.link_row("ws-mac-1")
        self.assertEqual((row["state"], row["end_reason"]), ("down", "daemon stop"))
        self.assertIsNotNone(row["ended"])
        self.assertEqual(self.session_row(address)["state"], "offline")
        self.assertFalse(self.store.link_current("ws-mac-1", lease))
        self.assertTrue(refuse(self.store.link_end("ws-mac-1", lease, "again"), "superseded"))
        self.assertTrue(any(e["kind"] == "link_claimed" for e in self.store.events()))
        self.assertTrue(any(e["kind"] == "link_ended" for e in self.store.events()))

    def test_link_current_creates_nothing(self):
        self.assertFalse(self.store.link_current("ws-mac-1", 1))
        self.assertEqual(os.listdir(os.environ["GUARDIAN_DATA_DIR"]), [])


class Bind(SessionTestCase):
    def test_name_reuse_rules(self):
        lease = self.claim()
        self.assertEqual(self.bind("ws-mac-1", lease, "alpha", "cli-a"), "alpha@ws-mac-1")
        self.assertTrue(refuse(self.store.session_bind("ws-mac-1", lease, "alpha", "cli-b"),
                               "name_in_use"))
        # a reconnect keeps the address: a new claim, the same cli session rebinds
        second = self.claim()
        self.assertEqual(self.bind("ws-mac-1", second, "alpha", "cli-a"), "alpha@ws-mac-1")
        self.assertEqual(self.session_row("alpha@ws-mac-1")["state"], "linked")
        # SessionEnd releases the name: any cli session may take it again
        self.assertTrue(self.store.session_unbind("ws-mac-1", second, "alpha")["ok"])
        self.assertTrue(refuse(self.store.session_unbind("ws-mac-1", second, "alpha"), "not_bound"))
        self.assertEqual(self.bind("ws-mac-1", second, "alpha", "cli-b"), "alpha@ws-mac-1")
        self.assertTrue(any(e["kind"] == "session_bound" for e in self.store.events()))

    def test_bad_addresses_and_names(self):
        lease = self.claim("node-1")
        register(self.store, "claude")  # holds claude@node-1
        self.assertTrue(refuse(self.store.session_bind("node-1", lease, "claude", "cli"),
                               "bad_address"))
        mac = self.claim("ws-mac-1")
        for name in ("operator", "hub"):
            self.assertTrue(refuse(self.store.session_bind("ws-mac-1", mac, name, "cli"),
                                   "bad_address"), name)
        self.assertTrue(refuse(self.store.session_bind("ws-mac-1", mac, "Alpha", "cli"),
                               "bad_input"))
        self.assertTrue(refuse(self.store.link_claim("ws mac"), "bad_input"))

    def test_state_marks(self):
        lease = self.claim()
        address = self.bind("ws-mac-1", lease, "alpha")
        self.assertTrue(self.store.session_state("ws-mac-1", lease, "alpha", "busy")["ok"])
        row = self.session_row(address)
        self.assertEqual(row["state"], "busy")
        self.assertIsNotNone(row["last_turn"])
        self.assertTrue(refuse(self.store.session_state("ws-mac-1", lease, "alpha", "sleeping"),
                               "bad_input"))


class OfflineRelease(SessionTestCase):
    def test_offline_released_after_ten_minutes(self):
        first = self.claim()
        address = self.bind("ws-mac-1", first, "alpha")
        second = self.claim()  # alpha goes offline now
        self.clock.advance(599)
        self.store.list_mail()  # the sweep runs at the start of a public operation
        self.assertEqual(self.session_row(address)["state"], "offline")
        self.clock.advance(1)
        self.store.list_mail()
        self.assertEqual(self.session_row(address)["state"], "ended")
        self.assertTrue(any(e["kind"] == "session_released" for e in self.store.events()))


class SessionSend(SessionTestCase):
    def setUp(self):
        super().setUp()
        self.agent = register(self.store, "claude")
        self.lease = self.claim()
        self.alpha = self.bind("ws-mac-1", self.lease, "alpha")

    def send(self, name, to, kind="message", op="x", **kwargs):
        return self.store.session_send("ws-mac-1", self.lease, name, to, kind, "s", "b",
                                       op_id=op, **kwargs)

    def test_operator_and_agent_mail_to_a_session(self):
        from_operator = self.store.send("operator", self.alpha, "message", "s", "b", op_id="o1")
        self.assertTrue(from_operator["ok"], from_operator)
        self.assertEqual(from_operator["hop"], 0)
        row = self.mail_row(from_operator["id"])
        self.assertEqual((row["state"], row["from_kind"]), ("queued", "operator"))
        epoch = make_ready(self.store, self.agent)
        turn = open_turn(self.store, self.agent, epoch)
        from_agent = self.store.send(self.agent, self.alpha, "message", "s", "b", op_id="a1",
                                     epoch=epoch, turn=turn)
        self.assertTrue(from_agent["ok"], from_agent)
        self.assertEqual(self.mail_row(from_agent["id"])["from_kind"], "agent")

    def test_agent_with_declared_peers_has_no_session_peer(self):
        narrow = register(self.store, "agy", peers=["claude@node-1"])
        epoch = make_ready(self.store, narrow)
        turn = open_turn(self.store, narrow, epoch)
        self.assertTrue(refuse(self.store.send(narrow, self.alpha, "message", "s", "b",
                                               op_id="n1", epoch=epoch, turn=turn), "not_a_peer"))

    def test_session_sends_to_agent_operator_and_other_session(self):
        to_agent = self.send("alpha", self.agent, op="s1")
        self.assertTrue(to_agent["ok"], to_agent)
        self.assertEqual(to_agent["hop"], 1)  # a fresh thread starts a session at hop 1
        to_operator = self.send("alpha", "operator", op="s2")
        self.assertTrue(to_operator["ok"], to_operator)
        self.assertEqual(self.mail_row(to_operator["id"])["state"], "delivered")  # no node, no spool
        other = self.claim("ws-wsl-1")
        beta = self.bind("ws-wsl-1", other, "beta")
        to_session = self.send("alpha", beta, op="s3")
        self.assertTrue(to_session["ok"], to_session)
        row = self.mail_row(to_session["id"])
        self.assertEqual((row["from_kind"], row["state"], row["task"]), ("session", "queued", None))

    def test_narrowed_session_peers(self):
        gamma = self.bind("ws-mac-1", self.lease, "gamma", peers=["operator"])
        self.assertTrue(refuse(self.send("gamma", self.agent, op="g1"), "not_a_peer"))
        self.assertTrue(self.send("gamma", "operator", op="g2")["ok"])
        self.assertEqual(self.session_row(gamma)["peers"], ["operator"])

    def test_sender_must_be_bound(self):
        self.assertTrue(refuse(self.send("ghost", "operator", op="b1"), "not_bound"))
        self.assertTrue(self.store.session_unbind("ws-mac-1", self.lease, "alpha")["ok"])
        self.assertTrue(refuse(self.send("alpha", "operator", op="b2"), "not_bound"))

    def test_kinds_refused(self):
        for kind, reason in (("task_offer", "bad_kind"), ("task_result", "bad_kind"),
                             ("status", "reserved_kind"), ("shutdown", "reserved_kind")):
            self.assertTrue(refuse(self.send("alpha", "operator", kind=kind, op=f"k-{kind}"),
                                   reason), kind)

    def test_reply_to_and_thread_visibility(self):
        other = self.claim("ws-wsl-1")
        beta = self.bind("ws-wsl-1", other, "beta")
        hidden = self.store.send("operator", beta, "message", "s", "b", op_id="h1")
        self.assertTrue(refuse(self.send("alpha", "operator", kind="answer", op="r1",
                                         reply_to=hidden["id"]), "invisible_reply_to"))
        self.assertTrue(refuse(self.send("alpha", "operator", op="r2",
                                         thread=hidden["thread"]), "not_in_thread"))
        own = self.store.send("operator", self.alpha, "message", "s", "b", op_id="h2")
        answer = self.send("alpha", "operator", kind="answer", op="r3", reply_to=own["id"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["thread"], own["thread"])
        self.assertEqual(answer["hop"], 1)  # 1 + the hop 0 mail it answers

    def test_hourly_rate_limit(self):
        for index in range(mail_sessions.SESSION_MAIL_MAX):
            self.assertTrue(self.send("alpha", "operator", op=f"m{index}")["ok"])
        self.assertTrue(refuse(self.send("alpha", "operator", op="over"), "rate_limit"))
        self.clock.advance(3601)
        self.assertTrue(self.send("alpha", "operator", op="later")["ok"])

    def test_agent_turn_hop_three_refuses_the_session_reply(self):
        one, two = register(self.store, "agy"), register(self.store, "codex")
        epoch_one, epoch_two = make_ready(self.store, one), make_ready(self.store, two)
        opener = self.store.send("operator", one, "message", "s", "b", op_id="c0")
        turn_one = open_turn(self.store, one, epoch_one)
        self.store.record_retrieved(one, epoch_one, turn_one, [opener["id"]])
        to_two = self.store.send(one, two, "message", "s", "b", op_id="c1", epoch=epoch_one,
                                 turn=turn_one)
        self.assertEqual(to_two["hop"], 1)
        turn_two = open_turn(self.store, two, epoch_two)
        self.store.record_retrieved(two, epoch_two, turn_two, [to_two["id"]])
        back = self.store.send(two, one, "message", "s", "b", op_id="c2", epoch=epoch_two,
                               turn=turn_two)
        self.assertEqual(back["hop"], 2)
        turn_again = open_turn(self.store, one, epoch_one)
        self.store.record_retrieved(one, epoch_one, turn_again, [back["id"]])
        to_session = self.store.send(one, self.alpha, "message", "s", "b", op_id="c3",
                                     epoch=epoch_one, turn=turn_again)
        self.assertEqual(to_session["hop"], 3)  # the agent's turn sits at hop 3
        self.assertTrue(refuse(self.send("alpha", one, kind="answer", op="c4",
                                         reply_to=to_session["id"]), "hop_limit"))
        self.assertTrue(self.send("alpha", "operator", op="c5")["ok"])  # a fresh thread still goes

    def test_repeated_op_id_returns_the_first_answer(self):
        first = self.send("alpha", "operator", op="same")
        again = self.store.session_send("ws-mac-1", self.lease, "alpha", "operator", "message",
                                        "s", "different body", op_id="same")
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.list_mail(limit=100)), 1)


class SessionPeersList(SessionTestCase):
    def setUp(self):
        super().setUp()
        self.agent = register(self.store, "claude")
        self.lease = self.claim()
        self.alpha = self.bind("ws-mac-1", self.lease, "alpha")

    def test_default_peers_with_state_and_last_seen(self):
        make_ready(self.store, self.agent)
        other = self.claim("ws-wsl-1")
        beta = self.bind("ws-wsl-1", other, "beta")
        self.assertTrue(self.store.session_state("ws-wsl-1", other, "beta", "busy")["ok"])
        gamma = self.bind("ws-mac-1", self.lease, "gamma")
        self.assertTrue(self.store.session_state("ws-mac-1", self.lease, "gamma", "away")["ok"])
        delta = self.bind("ws-mac-1", self.lease, "delta")
        self.assertTrue(self.store.session_unbind("ws-mac-1", self.lease, "delta")["ok"])
        self.clock.advance(601)  # past SESSION_IDLE_AFTER: a linked session derives idle
        result = self.store.session_peers_list("ws-mac-1", self.lease, "alpha")
        self.assertTrue(result["ok"], result)
        sessions = {row["address"]: row for row in self.store.session_rows()}
        agents = {row["address"]: row for row in self.store.agent_rows()}
        self.assertEqual(result["peers"], [
            {"address": beta, "kind": "session", "state": "busy", "away": False,
             "last_seen": sessions[beta]["last_turn"]},  # busy never derives idle
            {"address": self.agent, "kind": "agent", "state": agents[self.agent]["state"],
             "last_seen": agents[self.agent]["modified"]},
            {"address": gamma, "kind": "session", "state": "idle", "away": True,
             "last_seen": sessions[gamma]["linked"]},  # no turn: the bind stamp is the last seen
            {"address": "operator", "kind": "operator", "state": "operator", "last_seen": None},
        ])  # neither alpha itself nor the ended delta is a peer

    def test_stored_peers_keep_the_unknown_address_and_gain_operator(self):
        other = self.claim("ws-wsl-1")
        beta = self.bind("ws-wsl-1", other, "beta")
        self.bind("ws-mac-1", self.lease, "narrow", peers=[self.agent, "ghost@ws-mac-1"])
        result = self.store.session_peers_list("ws-mac-1", self.lease, "narrow")
        self.assertTrue(result["ok"], result)
        agents = {row["address"]: row for row in self.store.agent_rows()}
        self.assertEqual(result["peers"], [
            {"address": self.agent, "kind": "agent", "state": agents[self.agent]["state"],
             "last_seen": agents[self.agent]["modified"]},
            {"address": "ghost@ws-mac-1", "kind": "unknown", "state": "unknown",
             "last_seen": None},
            {"address": "operator", "kind": "operator", "state": "operator", "last_seen": None},
        ])  # beta is not a peer of a narrowed list; operator always is

    def test_the_caller_must_be_a_bound_session(self):
        self.assertTrue(refuse(self.store.session_peers_list("ws-mac-1", self.lease, "ghost"),
                               "not_bound"))
        self.assertTrue(self.store.session_unbind("ws-mac-1", self.lease, "alpha")["ok"])
        self.assertTrue(refuse(self.store.session_peers_list("ws-mac-1", self.lease, "alpha"),
                               "not_bound"))


class Views(SessionTestCase):
    def test_peer_view_lists_sessions_agent_lists_never_do(self):
        agent = register(self.store, "claude")
        register(self.store, "agy")
        lease = self.claim()
        self.bind("ws-mac-1", lease, "alpha")
        view = {row["address"]: row for row in self.store.peer_view(agent)}
        self.assertEqual(view["alpha@ws-mac-1"],
                         {"address": "alpha@ws-mac-1", "name": "alpha", "node": "ws-mac-1",
                          "provider": "session", "state": "linked"})
        self.assertIn("agy@node-1", view)
        self.assertEqual([row["address"] for row in self.store.agent_rows()],
                         ["agy@node-1", "claude@node-1"])  # sessions never enter agents
        self.assertEqual([row["address"] for row in self.store.node_agents("node-1")],
                         ["agy@node-1", "claude@node-1"])
        self.assertEqual(self.store.node_agents("ws-mac-1"), [])

    def test_derived_states_and_away(self):
        lease = self.claim()
        address = self.bind("ws-mac-1", lease, "alpha")
        row = self.session_row(address)
        self.assertEqual(row["state"], "linked")
        self.assertFalse(row["away"])
        self.assertIsNone(row["peers"])
        self.clock.advance(599)
        self.assertEqual(self.session_row(address)["state"], "linked")
        self.clock.advance(2)
        self.assertEqual(self.session_row(address)["state"], "idle")
        # a turn restarts the idle clock, and busy never derives idle
        self.assertTrue(self.store.session_state("ws-mac-1", lease, "alpha", "busy")["ok"])
        self.clock.advance(3600)
        self.assertEqual(self.session_row(address)["state"], "busy")
        self.assertTrue(self.store.session_state("ws-mac-1", lease, "alpha", "linked")["ok"])
        self.assertEqual(self.session_row(address)["state"], "linked")
        self.assertTrue(self.store.session_state("ws-mac-1", lease, "alpha", "away")["ok"])
        self.assertTrue(self.session_row(address)["away"])
        self.assertTrue(self.store.session_state("ws-mac-1", lease, "alpha", "back")["ok"])
        self.assertFalse(self.session_row(address)["away"])


class KillSwitch(SessionTestCase):
    def test_sessions_off_refuses_every_operation(self):
        with patch.dict(os.environ, {"GUARDIAN_SESSIONS": "off"}):
            self.assertTrue(refuse(self.store.link_claim("ws-mac-1"), "sessions_off"))
        lease = self.claim()
        self.bind("ws-mac-1", lease, "alpha")
        with patch.dict(os.environ, {"GUARDIAN_SESSIONS": "OFF"}):  # case-insensitive
            self.assertTrue(refuse(self.store.session_bind("ws-mac-1", lease, "beta", "cli-b"),
                                   "sessions_off"))
            self.assertTrue(refuse(self.store.session_send("ws-mac-1", lease, "alpha", "operator",
                                                           "message", "s", "b", op_id="s1"),
                                   "sessions_off"))
            self.assertTrue(refuse(self.store.session_peers_list("ws-mac-1", lease, "alpha"),
                                   "sessions_off"))
            self.assertFalse(self.store.link_current("ws-mac-1", lease))

    def test_resident_off_does_not_touch_sessions(self):
        lease = self.claim()
        self.bind("ws-mac-1", lease, "alpha")
        with patch.dict(os.environ, {"GUARDIAN_RESIDENT": "off"}):
            self.assertEqual(self.store.link_claim("ws-mac-1")["lease"], lease + 1)
            self.bind("ws-mac-1", lease + 1, "alpha")  # the rebind after the reconnect
            sent = self.store.session_send("ws-mac-1", lease + 1, "alpha", "operator", "message",
                                           "s", "b", op_id="s1")
            self.assertTrue(sent["ok"], sent)


class Inbox(SessionTestCase):
    def test_covers_the_workstations_live_sessions_only(self):
        lease = self.claim()
        alpha = self.bind("ws-mac-1", lease, "alpha")
        beta = self.bind("ws-mac-1", lease, "beta")
        other = self.claim("ws-wsl-1")
        gamma = self.bind("ws-wsl-1", other, "gamma")
        to_alpha = self.store.send("operator", alpha, "message", "s", "b", op_id="i1")["id"]
        to_beta = self.store.send("operator", beta, "message", "s", "b", op_id="i2")["id"]
        to_gamma = self.store.send("operator", gamma, "message", "s", "b", op_id="i3")["id"]
        result = self.store.session_inbox("ws-mac-1", lease)
        self.assertTrue(result["ok"], result)
        self.assertEqual([m["id"] for m in result["mail"]], [to_alpha, to_beta])  # oldest first
        # the link went down and came back: both mac sessions stay offline until they rebind
        self.store.link_end("ws-mac-1", lease, "stream lost")
        rebound = self.claim()
        self.assertEqual(self.store.session_inbox("ws-mac-1", rebound)["mail"], [])
        self.bind("ws-mac-1", rebound, "alpha")  # the same cli session keeps the address
        self.assertEqual([m["id"] for m in self.store.session_inbox("ws-mac-1", rebound)["mail"]],
                         [to_alpha])
        self.assertEqual(self.mail_row(to_gamma)["state"], "queued")  # not this workstation's

    def test_ended_session_mail_stays_queued_until_rebind(self):
        lease = self.claim()
        alpha = self.bind("ws-mac-1", lease, "alpha")
        mail_id = self.store.send("operator", alpha, "message", "s", "b", op_id="q1")["id"]
        self.assertTrue(self.store.session_unbind("ws-mac-1", lease, "alpha")["ok"])
        rebound = self.claim()
        self.assertEqual(self.store.session_inbox("ws-mac-1", rebound)["mail"], [])
        self.assertEqual(self.mail_row(mail_id)["state"], "queued")
        self.bind("ws-mac-1", rebound, "alpha", "cli-other")  # ended: the name is free
        self.assertEqual([m["id"] for m in self.store.session_inbox("ws-mac-1", rebound)["mail"]],
                         [mail_id])

    def test_mark_delivered_and_ack_scope(self):
        lease = self.claim()
        alpha = self.bind("ws-mac-1", lease, "alpha")
        other = self.claim("ws-wsl-1")
        gamma = self.bind("ws-wsl-1", other, "gamma")
        agent = register(self.store, "claude")
        to_alpha = self.store.send("operator", alpha, "message", "s", "b", op_id="d1")["id"]
        to_gamma = self.store.send("operator", gamma, "message", "s", "b", op_id="d2")["id"]
        to_agent = self.store.send("operator", agent, "message", "s", "b", op_id="d3")["id"]
        result = self.store.session_mark_delivered("ws-mac-1", lease, [to_alpha, to_gamma, to_agent])
        self.assertEqual(result, {"ok": True, "ids": [to_alpha]})
        self.assertEqual(self.mail_row(to_gamma)["state"], "queued")
        self.assertEqual(self.mail_row(to_agent)["state"], "queued")
        self.assertEqual(self.store.session_ack("ws-mac-1", lease, "alpha", [to_alpha]),
                         {"ok": True, "ids": [to_alpha]})
        self.assertEqual(self.mail_row(to_alpha)["state"], "acked")
        second = self.store.send("operator", alpha, "message", "s", "b", op_id="d4")["id"]
        self.assertEqual(self.store.session_ack("ws-mac-1", lease, "alpha", [second]),
                         {"ok": True, "ids": [second]})  # ack works from queued too
        self.assertEqual(self.mail_row(second)["state"], "acked")


if __name__ == "__main__":
    unittest.main()

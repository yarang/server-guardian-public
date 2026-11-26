"""MailStore: turns and epochs, mail limits and hop, the task board, expiry, idempotence and the
authority rule (docs/design/resident-agents.md, phase M1)."""

import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mail_store
import testenv


class SimpleClock:
    """A fake now() the tests advance by hand."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def make_clock(start="2026-11-15T10:00:00"):
    clock = SimpleClock()
    clock.now = datetime.fromisoformat(start + "+00:00")
    return clock


def register(store, name="claude", node="node-1", peers=None, provider="claude"):
    address = f"{name}@{node}"
    entry = {"name": name, "node": node, "provider": provider, "driver": "headless",
             "address": address}
    if peers is not None:
        entry["peers"] = peers
    assert store.upsert_agent(entry)["ok"]
    return address


def make_ready(store, address):
    bind = store.bind_begin(address)
    assert bind["ok"], bind
    assert store.bind_complete(address, bind["epoch"], f"session-{address}-{bind['epoch']}")["ok"]
    return bind["epoch"]


def open_turn(store, address, epoch, kind="wake"):
    got = store.admit_turn(address, epoch, kind)
    assert got["ok"], got
    return got["turn"]


def refuse(result, reason):
    return not result.get("ok") and result.get("reason") == reason


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = make_clock()
        self.store = mail_store.MailStore(now=self.clock)

    def data_entries(self):
        return os.listdir(os.environ["GUARDIAN_DATA_DIR"])


class NoAgentPath(StoreTestCase):
    """Reads on an empty data dir create no file and no directory."""

    def test_reads_create_nothing(self):
        self.assertEqual(self.store.task_list("operator"), [])
        self.assertEqual(self.store.list_mail(), [])
        self.assertEqual(self.store.inbox_queued("claude@node-1"), [])
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.data_entries(), [])

    def test_refused_send_without_store_creates_nothing(self):
        with patch.dict(os.environ, {"GUARDIAN_RESIDENT": "off"}):
            result = self.store.send("operator", "claude@node-1", "message", "s", "b", op_id="o1")
        self.assertTrue(refuse(result, "resident_off"))
        self.assertEqual(self.data_entries(), [])

    def test_send_to_unknown_agent_without_store_creates_nothing(self):
        result = self.store.send("operator", "claude@node-1", "message", "s", "b", op_id="o2")
        self.assertTrue(refuse(result, "unknown_agent"))
        result = self.store.send("claude@node-1", "operator", "message", "s", "b", op_id="o3")
        self.assertTrue(refuse(result, "unknown_agent"))
        self.assertEqual(self.data_entries(), [])


class Admission(StoreTestCase):
    def test_needs_ready_at_current_epoch(self):
        address = register(self.store)
        self.assertTrue(refuse(self.store.admit_turn("ghost@node-1", 1, "wake"), "unknown_agent"))
        self.assertTrue(refuse(self.store.admit_turn(address, 0, "wake"), "wrong_epoch"))
        bind = self.store.bind_begin(address)
        self.assertEqual(bind["epoch"], 1)
        self.assertTrue(refuse(self.store.admit_turn(address, 1, "wake"), "wrong_epoch"))
        self.store.bind_complete(address, 1, "sess-1")
        self.assertTrue(self.store.admit_turn(address, 1, "wake")["ok"])
        self.assertTrue(refuse(self.store.admit_turn(address, 0, "wake"), "wrong_epoch"))

    def test_hourly_budget(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        for _ in range(mail_store.TURNS_PER_HOUR):
            self.assertTrue(self.store.admit_turn(address, epoch, "wake")["ok"])
        self.assertTrue(refuse(self.store.admit_turn(address, epoch, "wake"), "turn_budget"))
        self.clock.advance(3601)
        self.assertTrue(self.store.admit_turn(address, epoch, "wake")["ok"])

    def test_kill_switch(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        with patch.dict(os.environ, {"GUARDIAN_RESIDENT": "off"}):
            self.assertTrue(refuse(self.store.admit_turn(address, epoch, "wake"), "resident_off"))
            self.assertTrue(refuse(self.store.send("operator", address, "message", "s", "b",
                                                   op_id="o1"), "resident_off"))
        self.assertEqual(self.store.list_mail(), [])
        with patch.dict(os.environ, {"GUARDIAN_RESIDENT": "off"}):
            # a fresh store instance must refuse too: the switch is read per call
            fresh = mail_store.MailStore(now=self.clock)
            self.assertTrue(refuse(fresh.send("operator", address, "message", "s", "b", op_id="o2"),
                                   "resident_off"))


class EpochFencing(StoreTestCase):
    def test_rebind_fences_old_turn_and_redelivers(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        mail_id = self.store.send("operator", address, "message", "s", "b", op_id="o1")["id"]
        turn = open_turn(self.store, address, epoch)
        self.assertTrue(self.store.record_retrieved(address, epoch, turn, [mail_id])["ok"])
        rebind = self.store.bind_begin(address)  # a restart: epoch 2, open turns end as lost
        self.assertEqual(rebind["epoch"], epoch + 1)
        result = self.store.end_turn(address, epoch, turn, acked=[mail_id])
        self.assertTrue(refuse(result, "wrong_epoch"))
        queued = self.store.inbox_queued(address)
        self.assertEqual([m["id"] for m in queued], [mail_id])
        self.assertEqual(queued[0]["state"], "queued")
        self.assertEqual(queued[0]["redelivered"], 1)

    def test_normal_end_acks_only_listed_mail(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        first = self.store.send("operator", address, "message", "s", "b", op_id="o1")["id"]
        second = self.store.send("operator", address, "message", "s", "b", op_id="o2")["id"]
        turn = open_turn(self.store, address, epoch)
        self.store.record_retrieved(address, epoch, turn, [first, second])
        self.assertTrue(self.store.end_turn(address, epoch, turn, acked=[first])["ok"])
        states = {m["id"]: m["state"] for m in self.store.list_mail()}
        self.assertEqual(states[first], "acked")
        self.assertEqual(states[second], "queued")
        redelivered = {m["id"]: m["redelivered"] for m in self.store.list_mail()}
        self.assertEqual(redelivered[second], 1)

    def test_lost_turn_returns_mail(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        mail_id = self.store.send("operator", address, "message", "s", "b", op_id="o1")["id"]
        turn = open_turn(self.store, address, epoch)
        self.store.record_retrieved(address, epoch, turn, [mail_id])
        self.assertTrue(self.store.end_turn(address, epoch, turn, end_reason="lost")["ok"])
        queued = self.store.inbox_queued(address)
        self.assertEqual(queued[0]["id"], mail_id)
        self.assertEqual(queued[0]["redelivered"], 1)

    def test_report_resume(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        self.assertEqual(self.store.report_resume(address, epoch, f"session-{address}-1"),
                         {"ok": True, "new_session": False})
        result = self.store.report_resume(address, epoch, "session-fresh")
        self.assertEqual(result, {"ok": True, "new_session": True})
        conn = self.store._ready()
        row = conn.execute("SELECT end_reason FROM incarnations WHERE address=? AND epoch=?",
                           (address, epoch)).fetchone()
        self.assertEqual(row["end_reason"], "resume_new_session")


class HopAndLimits(StoreTestCase):
    def test_hop_chain_from_operator_to_limit(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch_a = make_ready(self.store, first)
        epoch_b = make_ready(self.store, second)
        opener = self.store.send("operator", first, "message", "s", "b", op_id="o0")
        self.assertEqual(opener["hop"], 0)
        turn_a = open_turn(self.store, first, epoch_a)
        self.store.record_retrieved(first, epoch_a, turn_a, [opener["id"]])
        reply = self.store.send(first, second, "message", "s", "b", op_id="o1",
                                epoch=epoch_a, turn=turn_a)
        self.assertEqual(reply["hop"], 1)
        turn_b = open_turn(self.store, second, epoch_b)
        self.store.record_retrieved(second, epoch_b, turn_b, [reply["id"]])
        back = self.store.send(second, first, "message", "s", "b", op_id="o2",
                               epoch=epoch_b, turn=turn_b)
        self.assertEqual(back["hop"], 2)
        turn_a2 = open_turn(self.store, first, epoch_a)
        self.store.record_retrieved(first, epoch_a, turn_a2, [back["id"]])
        third = self.store.send(first, second, "message", "s", "b", op_id="o3",
                                epoch=epoch_a, turn=turn_a2)
        self.assertEqual(third["hop"], 3)
        turn_b2 = open_turn(self.store, second, epoch_b)
        self.store.record_retrieved(second, epoch_b, turn_b2, [third["id"]])
        over = self.store.send(second, first, "message", "s", "b", op_id="o4",
                               epoch=epoch_b, turn=turn_b2)
        self.assertTrue(refuse(over, "hop_limit"))
        # operator mail always carries hop 0, whatever the chain before it
        self.assertEqual(self.store.send("operator", first, "message", "s", "b", op_id="o5")["hop"], 0)

    def test_send_api_has_no_caller_settable_envelope_fields(self):
        parameters = set(inspect.signature(mail_store.MailStore.send).parameters)
        for forbidden in ("from_kind", "hop", "created", "expires", "id", "sender_forge"):
            self.assertNotIn(forbidden, parameters)
        self.assertIn("op_id", parameters)

    def test_thread_limit(self):
        address = register(self.store)
        first = self.store.send("operator", address, "message", "s", "b", op_id="t0")
        thread = first["thread"]
        for index in range(mail_store.THREAD_MAX - 1):
            self.assertTrue(self.store.send("operator", address, "message", "s", "b",
                                            op_id=f"t{index + 1}", thread=thread)["ok"])
        result = self.store.send("operator", address, "message", "s", "b", op_id="over", thread=thread)
        self.assertTrue(refuse(result, "thread_limit"))

    def test_body_limit(self):
        address = register(self.store)
        body = "x" * mail_store.BODY_MAX
        self.assertTrue(self.store.send("operator", address, "message", "", body[:-1],
                                        op_id="fit")["ok"])  # exactly BODY_MAX fits
        result = self.store.send("operator", address, "message", "s", body, op_id="big")
        self.assertTrue(refuse(result, "too_large"))

    def test_peers_and_kinds(self):
        first = register(self.store, "claude", peers=["agy@node-1"])
        register(self.store, "agy")
        outsider = register(self.store, "solo", node="node-2")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        self.assertTrue(refuse(self.store.send(first, outsider, "status", "s", "b", op_id="k1",
                                               epoch=epoch, turn=turn), "reserved_kind"))
        self.assertTrue(refuse(self.store.send(first, outsider, "nonsense", "s", "b", op_id="k2",
                                               epoch=epoch, turn=turn), "bad_kind"))
        self.assertTrue(refuse(self.store.send(first, outsider, "message", "s", "b", op_id="k3",
                                               epoch=epoch, turn=turn), "not_a_peer"))
        self.assertTrue(refuse(self.store.send(first, "operator", "message", "s", "b", op_id="k6"),
                               "no_turn"))
        self.assertTrue(self.store.send(first, "operator", "answer", "s", "b", op_id="k4",
                                        epoch=epoch, turn=turn)["ok"])
        self.assertTrue(self.store.send(first, "agy@node-1", "message", "s", "b", op_id="k5",
                                        epoch=epoch, turn=turn)["ok"])

    def test_special_senders(self):
        address = register(self.store)
        self.assertTrue(refuse(self.store.send("operator", address, "status", "idle", "b",
                                               op_id="h1"), "bad_kind"))
        self.assertTrue(self.store.send("operator", address, "shutdown", "stop", "now",
                                        op_id="h2")["ok"])
        self.assertTrue(self.store.send("hub", address, "status", "idle", "b", op_id="h3")["ok"])
        self.assertTrue(refuse(self.store.send("hub", address, "status", "nope", "b",
                                               op_id="h4"), "bad_input"))
        self.assertTrue(refuse(self.store.send("operator", "hub", "message", "s", "b",
                                               op_id="h5"), "bad_address"))
        self.assertTrue(refuse(self.store.send("operator", "ghost@node-1", "message", "s", "b",
                                               op_id="h6"), "unknown_agent"))

    def test_from_kind_is_set_by_the_store(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch = make_ready(self.store, first)
        self.store.send("operator", first, "message", "s", "b", op_id="f1")
        turn = open_turn(self.store, first, epoch)
        self.store.send(first, second, "message", "s", "b", op_id="f2", epoch=epoch, turn=turn)
        kinds = {m["sender"]: m["from_kind"] for m in self.store.list_mail()}
        self.assertEqual(kinds["operator"], "operator")
        self.assertEqual(kinds[first], "agent")

    def test_reply_to_and_thread_visibility(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        hidden = self.store.send("operator", second, "message", "s", "b", op_id="v1")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        self.assertTrue(refuse(self.store.send(first, "operator", "answer", "s", "b", op_id="v2",
                                               epoch=epoch, turn=turn,
                                               reply_to=hidden["id"]), "invisible_reply_to"))
        self.assertTrue(refuse(self.store.send(first, "operator", "answer", "s", "b", op_id="v3",
                                               epoch=epoch, turn=turn,
                                               thread=hidden["thread"]), "not_in_thread"))
        own = self.store.send("operator", first, "message", "s", "b", op_id="v4")
        self.store.record_retrieved(first, epoch, turn, [own["id"]])
        answer = self.store.send(first, "operator", "answer", "s", "b", op_id="v5",
                                 epoch=epoch, turn=turn, reply_to=own["id"])
        self.assertEqual(answer["thread"], own["thread"])

    def test_task_reference_must_be_visible(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        hidden = self.store.task_create("operator", "hidden", op_id="w0", assignee=second)["id"]
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        self.assertTrue(refuse(self.store.send(first, "operator", "task_result", "s", "b",
                                               op_id="w1", epoch=epoch, turn=turn,
                                               task=hidden), "invisible_task"))

    def test_reply_may_tag_the_task_of_the_mail_it_answers(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        task_id = self.store.task_create("operator", "shared", op_id="r0", assignee=first)["id"]
        epoch_a = make_ready(self.store, first)
        turn_a = open_turn(self.store, first, epoch_a)
        question = self.store.send(first, second, "question", "s", "b", op_id="r1",
                                   epoch=epoch_a, turn=turn_a, task=task_id)
        self.assertTrue(question["ok"], question)  # the assignee sees its own task
        epoch_b = make_ready(self.store, second)
        turn_b = open_turn(self.store, second, epoch_b)
        self.assertTrue(self.store.record_retrieved(second, epoch_b, turn_b,
                                                    [question["id"]])["ok"])
        answer = self.store.send(second, first, "answer", "s", "b", op_id="r2",
                                 epoch=epoch_b, turn=turn_b, reply_to=question["id"],
                                 task=task_id)
        self.assertTrue(answer["ok"], answer)  # the task is invisible to second, the reply is not
        stored = {m["id"]: m for m in self.store.list_mail(thread=answer["thread"])}
        self.assertEqual(stored[answer["id"]]["task"], task_id)

    def test_task_tag_refused_outside_the_answered_mail(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        task_id = self.store.task_create("operator", "shared", op_id="p0", assignee=first)["id"]
        other = self.store.task_create("operator", "other", op_id="p1", assignee=first)["id"]
        epoch_a = make_ready(self.store, first)
        turn_a = open_turn(self.store, first, epoch_a)
        question = self.store.send(first, second, "question", "s", "b", op_id="p2",
                                   epoch=epoch_a, turn=turn_a, task=task_id)["id"]
        epoch_b = make_ready(self.store, second)
        turn_b = open_turn(self.store, second, epoch_b)
        self.store.record_retrieved(second, epoch_b, turn_b, [question])
        # a new mail with the hidden task, no reply_to to inherit it from
        self.assertTrue(refuse(self.store.send(second, first, "message", "s", "b", op_id="p3",
                                               epoch=epoch_b, turn=turn_b,
                                               task=task_id), "invisible_task"))
        # a reply whose answered mail carries a different task
        self.assertTrue(refuse(self.store.send(second, first, "answer", "s", "b", op_id="p4",
                                               epoch=epoch_b, turn=turn_b, reply_to=question,
                                               task=other), "invisible_task"))

    def test_operator_mail_starts_delivered_agent_mail_stays_queued(self):
        address = register(self.store)
        from_operator = self.store.send("operator", address, "message", "s", "b", op_id="d0")
        self.assertTrue(from_operator["ok"], from_operator)
        epoch = make_ready(self.store, address)
        turn = open_turn(self.store, address, epoch)
        to_operator = self.store.send(address, "operator", "answer", "s", "b", op_id="d1",
                                      epoch=epoch, turn=turn)
        self.assertTrue(to_operator["ok"], to_operator)
        states = {m["id"]: m["state"] for m in self.store.list_mail(address="operator")}
        self.assertEqual(states[to_operator["id"]], "delivered")  # no node, no spool, no push
        self.assertEqual(states[from_operator["id"]], "queued")  # the relay still delivers this

    def test_note_when_recipient_down(self):
        address = register(self.store)  # registered agents start `down`
        result = self.store.send("operator", address, "message", "s", "b", op_id="n1")
        self.assertEqual(result["note"], "recipient down, queued")
        make_ready(self.store, address)
        result = self.store.send("operator", address, "message", "s", "b", op_id="n2")
        self.assertNotIn("note", result)


class Idempotence(StoreTestCase):
    def test_repeated_op_id_returns_first_answer(self):
        address = register(self.store)
        first = self.store.send("operator", address, "message", "s", "first", op_id="same")
        second = self.store.send("operator", address, "message", "s", "second", op_id="same")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_mail(limit=100)), 1)

    def test_refused_call_is_not_cached(self):
        first = register(self.store, "claude", peers=["agy@node-1"])
        second = register(self.store, "agy")
        register(self.store, "solo", node="node-2")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        refused = self.store.send(first, "solo@node-2", "message", "s", "b", op_id="retry",
                                  epoch=epoch, turn=turn)
        self.assertTrue(refuse(refused, "not_a_peer"))
        accepted = self.store.send(first, second, "message", "s", "b", op_id="retry",
                                   epoch=epoch, turn=turn)
        self.assertTrue(accepted["ok"])
        self.assertEqual(len(self.store.list_mail(limit=100)), 1)

    def test_task_op_ids(self):
        address = register(self.store)
        first = self.store.task_create("operator", "title", op_id="dup")
        second = self.store.task_create("operator", "other title", op_id="dup")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.task_list("operator")), 1)


class Claims(StoreTestCase):
    def test_two_stores_one_winner(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        self.store.bind_begin(first)
        self.store.bind_complete(first, 1, "s-a")
        self.store.bind_begin(second)
        self.store.bind_complete(second, 1, "s-b")
        task = self.store.task_create("operator", "race", op_id="c0")
        other = mail_store.MailStore(now=make_clock())  # a second connection on the same db
        turn_a = open_turn(self.store, first, 1)
        turn_b = open_turn(other, second, 1)
        claim_a = self.store.task_claim(first, task["id"], op_id="c1", epoch=1, turn=turn_a)
        claim_b = other.task_claim(second, task["id"], op_id="c2", epoch=1, turn=turn_b)
        self.assertEqual(sorted([claim_a.get("ok"), claim_b.get("ok")]), [False, True])
        loser, winner = (claim_b, claim_a) if claim_a.get("ok") else (claim_a, claim_b)
        self.assertEqual(loser["reason"], "not_claimable")
        self.assertEqual(winner["generation"], 1)

    def test_assigned_task_only_assignee_claims(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        task = self.store.task_create("operator", "assigned", op_id="a0", assignee=first)
        epoch = make_ready(self.store, second)
        turn = open_turn(self.store, second, epoch)
        # a task assigned to another agent is not visible to the claimer: refused before the
        # claim SQL, so its existence is not revealed (the SQL's assignee clause is the backstop)
        self.assertTrue(refuse(self.store.task_claim(second, task["id"], op_id="a1", epoch=epoch,
                                                     turn=turn), "invisible_task"))
        epoch_first = make_ready(self.store, first)
        turn_first = open_turn(self.store, first, epoch_first)
        self.assertTrue(self.store.task_claim(first, task["id"], op_id="a2", epoch=epoch_first,
                                              turn=turn_first)["ok"])

    def assign_notices(self, address):
        return [m for m in self.store.list_mail(address)
                if m["sender"] == "hub" and m["subject"] == "task_assigned"]

    def test_create_with_assignee_queues_one_wake_notice(self):
        first = register(self.store, "claude")
        task = self.store.task_create("operator", "assigned", op_id="n0", assignee=first)
        self.assertEqual(task["status"], "pending")
        notices = self.assign_notices(first)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["state"], "queued")
        self.assertEqual((notices[0]["kind"], notices[0]["from_kind"]), ("status", "hub"))
        self.assertEqual(notices[0]["hop"], task["hop"])
        self.assertIn(task["id"], notices[0]["body"])
        self.assertIsNone(notices[0]["task"])  # the id is in the body only

    def test_blocked_task_is_noticed_when_it_unblocks(self):
        first = register(self.store, "claude")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        blocker = self.store.task_create("operator", "first", op_id="n1")
        claim = self.store.task_claim(first, blocker["id"], op_id="n2", epoch=epoch, turn=turn)
        blocked = self.store.task_create("operator", "later", op_id="n3", assignee=first,
                                         blocked_by=[blocker["id"]])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.assign_notices(first), [])
        self.store.task_update(first, blocker["id"], claim["generation"], "completed", op_id="n4",
                               result="done", epoch=epoch, turn=turn)
        notices = self.assign_notices(first)
        self.assertEqual(len(notices), 1)
        self.assertIn(blocked["id"], notices[0]["body"])
        self.assertEqual(notices[0]["hop"], blocked["hop"])

    def test_task_assign_notices_pending_but_not_blocked(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        pending = self.store.task_create("operator", "free", op_id="n5")
        self.assertTrue(self.store.task_assign("operator", pending["id"], second)["ok"])
        notices = self.assign_notices(second)
        self.assertEqual(len(notices), 1)
        self.assertIn(pending["id"], notices[0]["body"])
        blocker = self.store.task_create("operator", "hold", op_id="n6")
        blocked = self.store.task_create("operator", "waiting", op_id="n7",
                                         blocked_by=[blocker["id"]])
        self.assertTrue(self.store.task_assign("operator", blocked["id"], second)["ok"])
        self.assertEqual(len(self.assign_notices(second)), 1)  # still blocked: no notice yet

    def test_agent_create_notices_peer_but_not_itself(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        mine = self.store.task_create(first, "mine", op_id="n8", assignee=first,
                                      epoch=epoch, turn=turn)
        self.assertTrue(mine["ok"])
        self.assertEqual(self.assign_notices(first), [])  # self-assignment: already mid-turn
        yours = self.store.task_create(first, "yours", op_id="n9", assignee=second,
                                       epoch=epoch, turn=turn)
        notices = self.assign_notices(second)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["hop"], yours["hop"])  # the task's hop, not hop 0
        self.assertGreaterEqual(notices[0]["hop"], 1)  # created inside a turn: the chain counts
        self.assertIn(yours["id"], notices[0]["body"])

    def test_refused_create_queues_no_notice(self):
        first = register(self.store, "claude")
        self.assertTrue(refuse(self.store.task_create("operator", "ghost", op_id="n10",
                                                      assignee="ghost@node-1"), "unknown_agent"))
        for index in range(mail_store.OPEN_TASKS_MAX):
            self.assertTrue(self.store.task_create("operator", f"t{index}",
                                                   op_id=f"n11-{index}", assignee=first)["ok"])
        self.assertTrue(refuse(self.store.task_create("operator", "one more", op_id="n12",
                                                      assignee=first), "open_task_limit"))
        self.assertEqual(len(self.assign_notices(first)), mail_store.OPEN_TASKS_MAX)

    def test_late_update_after_release(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        task = self.store.task_create("operator", "work", op_id="r0", assignee=address)
        turn = open_turn(self.store, address, epoch)
        claim = self.store.task_claim(address, task["id"], op_id="r1", epoch=epoch, turn=turn)
        self.store.task_release("hub", task["id"], None, "node restarted", op_id="r2")
        late = self.store.task_update(address, task["id"], claim["generation"], "completed",
                                      op_id="r3", result="done", epoch=epoch, turn=turn)
        self.assertTrue(refuse(late, "task_released"))
        # the same fencing after a release by the owner
        again = self.store.task_claim(address, task["id"], op_id="r4", epoch=epoch, turn=turn)
        self.store.task_release(address, task["id"], again["generation"], "cannot finish",
                                op_id="r5")
        late2 = self.store.task_update(address, task["id"], again["generation"], "completed",
                                       op_id="r6", result="done", epoch=epoch, turn=turn)
        self.assertTrue(refuse(late2, "task_released"))

    def test_release_stale_after_ten_minutes(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        task = self.store.task_create("operator", "work", op_id="s0", assignee=address)
        turn = open_turn(self.store, address, epoch)
        self.assertTrue(self.store.task_claim(address, task["id"], op_id="s1", epoch=epoch,
                                              turn=turn)["ok"])
        self.store.set_down(address, "stream lost")
        self.clock.advance(599)
        self.store.task_list("operator")  # the sweep runs at the start of every operation
        row = self.store.task_list("operator")[0]
        self.assertEqual(row["status"], "in_progress")
        self.clock.advance(1)
        row = self.store.task_list("operator")[0]
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["owner"])
        self.assertIsNone(row["assignee"])
        self.assertEqual(row["reason"], "agent_not_ready")


class Blockers(StoreTestCase):
    def agent_ready(self, name="claude"):
        address = register(self.store, name)
        return address, make_ready(self.store, address)

    def test_refused_blockers(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        self.assertTrue(refuse(self.store.task_create(first, "x", op_id="b1", epoch=epoch, turn=turn,
                                                      blocked_by=["k-nope"]), "unknown_blocker"))
        hidden = self.store.task_create("operator", "hidden", op_id="b0", assignee=second)
        self.assertTrue(refuse(self.store.task_create(first, "x", op_id="b2", epoch=epoch, turn=turn,
                                                      blocked_by=[hidden["id"]]), "invisible_blocker"))

    def test_dead_blockers_refused(self):
        address, epoch = self.agent_ready()
        turn = open_turn(self.store, address, epoch)
        cancelled = self.store.task_create("operator", "gone", op_id="d0")
        self.store.task_cancel("operator", cancelled["id"], "not needed")
        failed = self.store.task_create("operator", "boom", op_id="d1", assignee=address)
        claim = self.store.task_claim(address, failed["id"], op_id="d2", epoch=epoch, turn=turn)
        self.store.task_update(address, failed["id"], claim["generation"], "failed", op_id="d3",
                               reason="broken", epoch=epoch, turn=turn)
        expired = self.store.task_create("operator", "stale", op_id="d4")
        self.clock.advance(int(mail_store.LIFETIME.total_seconds()) + 1)
        self.store.task_list("operator")  # lazy expiry
        for blocker in (cancelled["id"], failed["id"], expired["id"]):
            result = self.store.task_create(address, "x", op_id=f"d5-{blocker}", epoch=epoch,
                                            turn=turn, blocked_by=[blocker])
            self.assertTrue(refuse(result, "dead_blocker"), blocker)

    def test_completed_blocker_dropped_and_last_unblocks(self):
        first = register(self.store, "claude")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)

        def finish(title, op):
            task = self.store.task_create("operator", title, op_id=f"{op}0", assignee=first)
            claim = self.store.task_claim(first, task["id"], op_id=f"{op}1", epoch=epoch, turn=turn)
            self.store.task_update(first, task["id"], claim["generation"], "completed",
                                   op_id=f"{op}2", result="finished", epoch=epoch, turn=turn)
            return task["id"]

        done = finish("done", "u1")
        created = self.store.task_create(first, "dependent", op_id="u3", epoch=epoch, turn=turn,
                                         blocked_by=[done])
        self.assertEqual(created["status"], "pending")  # a completed blocker is dropped at creation
        # a task blocked by one open task becomes pending when that task completes
        open_task = self.store.task_create("operator", "open", op_id="u4", assignee=first)
        claim = self.store.task_claim(first, open_task["id"], op_id="u5", epoch=epoch, turn=turn)
        blocked = self.store.task_create(first, "blocked", op_id="u6", epoch=epoch, turn=turn,
                                         blocked_by=[open_task["id"], done])
        self.assertEqual(blocked["status"], "blocked")  # done is completed, open_task is open
        self.store.task_update(first, open_task["id"], claim["generation"], "completed",
                               op_id="u7", result="finished", epoch=epoch, turn=turn)
        row = next(t for t in self.store.task_list("operator") if t["id"] == blocked["id"])
        self.assertEqual(row["status"], "pending")

    def test_failure_cancels_dependent_and_notifies_creator(self):
        maker, worker = register(self.store, "claude"), register(self.store, "agy")
        epoch_maker = make_ready(self.store, maker)
        turn_maker = open_turn(self.store, maker, epoch_maker)
        # the maker creates the blocker itself: a blocker must be visible to the creator
        blocker = self.store.task_create(maker, "blocker", op_id="f0", assignee=worker,
                                         epoch=epoch_maker, turn=turn_maker)
        dependent = self.store.task_create(maker, "dependent", op_id="f1", epoch=epoch_maker,
                                           turn=turn_maker, blocked_by=[blocker["id"]])
        self.assertEqual(dependent["status"], "blocked")
        epoch_worker = make_ready(self.store, worker)
        turn_worker = open_turn(self.store, worker, epoch_worker)
        claim = self.store.task_claim(worker, blocker["id"], op_id="f2", epoch=epoch_worker,
                                      turn=turn_worker)
        self.store.task_update(worker, blocker["id"], claim["generation"], "failed", op_id="f3",
                               reason="stuck", epoch=epoch_worker, turn=turn_worker)
        row = next(t for t in self.store.task_list("operator") if t["id"] == dependent["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["reason"], "blocker_failed")
        notice = self.store.inbox_queued(maker)
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["sender"], "hub")
        self.assertEqual(notice[0]["kind"], "status")
        self.assertIn(dependent["id"], notice[0]["body"])

    def test_operator_creator_gets_event_not_mail(self):
        address, epoch = self.agent_ready()
        turn = open_turn(self.store, address, epoch)
        blocker = self.store.task_create("operator", "blocker", op_id="e0", assignee=address)
        dependent = self.store.task_create("operator", "dependent", op_id="e1",
                                           blocked_by=[blocker["id"]])
        claim = self.store.task_claim(address, blocker["id"], op_id="e2", epoch=epoch, turn=turn)
        self.store.task_update(address, blocker["id"], claim["generation"], "failed", op_id="e3",
                               reason="stuck", epoch=epoch, turn=turn)
        row = next(t for t in self.store.task_list("operator") if t["id"] == dependent["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertTrue(any(e["kind"] == "task_notice" for e in self.store.events()))


class OpenTaskLimit(StoreTestCase):
    def test_five_open_tasks(self):
        address = register(self.store)
        epoch = make_ready(self.store, address)
        turn = open_turn(self.store, address, epoch)
        for index in range(mail_store.OPEN_TASKS_MAX):
            self.assertTrue(self.store.task_create("operator", f"t{index}",
                                                   op_id=f"l{index}", assignee=address)["ok"])
        refused = self.store.task_create("operator", "one more", op_id="l9", assignee=address)
        self.assertTrue(refuse(refused, "open_task_limit"))
        # unassigned tasks are not counted against a claimer until it claims
        spare = self.store.task_create("operator", "spare", op_id="l10")
        claimed = self.store.task_claim(address, spare["id"], op_id="l11", epoch=epoch, turn=turn)
        self.assertTrue(refuse(claimed, "open_task_limit"))


class Expiry(StoreTestCase):
    def test_mail_and_tasks_expire_lazily(self):
        address = register(self.store)
        mail_id = self.store.send("operator", address, "message", "s", "b", op_id="x0")["id"]
        task = self.store.task_create("operator", "work", op_id="x1", assignee=address)
        self.clock.advance(int(mail_store.LIFETIME.total_seconds()) + 1)
        self.assertEqual(self.store.inbox_queued(address), [])
        states = {m["id"]: m["state"] for m in self.store.list_mail()}
        self.assertEqual(states[mail_id], "expired")
        row = next(t for t in self.store.task_list("operator") if t["id"] == task["id"])
        self.assertEqual(row["status"], "expired")
        kinds = {e["kind"] for e in self.store.events()}
        self.assertIn("mail_expired", kinds)
        self.assertIn("task_expired", kinds)

    def test_dependent_of_expired_blocker_cancelled(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        blocker = self.store.task_create(first, "blocker", op_id="y0", assignee=second,
                                         epoch=epoch, turn=turn)
        self.clock.advance(3600)  # the dependent is younger, so only the blocker expires
        dependent = self.store.task_create(first, "dependent", op_id="y1", epoch=epoch,
                                           turn=turn, blocked_by=[blocker["id"]])
        self.clock.advance(int(mail_store.LIFETIME.total_seconds()) - 3600 + 1)
        self.store.task_list("operator")
        row = next(t for t in self.store.task_list("operator") if t["id"] == dependent["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["reason"], "blocker_failed")


class Undeliverable(StoreTestCase):
    def test_unacked_mail_and_sender_notice(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch = make_ready(self.store, second)
        turn = open_turn(self.store, second, epoch)
        operator_mail = self.store.send("operator", first, "message", "s", "b", op_id="z0")
        agent_mail = self.store.send(second, first, "message", "s", "b", op_id="z1",
                                     epoch=epoch, turn=turn)
        ids = self.store.mark_undeliverable(first)
        self.assertEqual(set(ids), {operator_mail["id"], agent_mail["id"]})
        states = {m["id"]: m["state"] for m in self.store.list_mail()}
        self.assertEqual(states[operator_mail["id"]], "undeliverable")
        self.assertEqual(states[agent_mail["id"]], "undeliverable")
        notices = self.store.inbox_queued(second)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["subject"], "undeliverable")
        self.assertEqual(notices[0]["from_kind"], "hub")
        self.assertIn(agent_mail["id"], notices[0]["body"])
        self.assertTrue(any(e["kind"] == "mail_undeliverable_sender" for e in self.store.events()))


class Authority(StoreTestCase):
    """Mail from an agent grants nothing at store level."""

    def test_agent_cannot_send_reserved_kinds_or_assign_foreign_tasks(self):
        first, second = register(self.store, "claude"), register(self.store, "agy")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        for kind in ("status", "shutdown"):
            result = self.store.send(first, "operator", kind, "idle", "b", op_id=f"au-{kind}",
                                     epoch=epoch, turn=turn)
            self.assertTrue(refuse(result, "reserved_kind"), kind)
        foreign = self.store.task_create("operator", "theirs", op_id="au1", assignee=second)
        self.assertTrue(refuse(self.store.task_assign(first, foreign["id"], first), "not_creator"))
        self.assertTrue(refuse(self.store.task_cancel(first, foreign["id"], "mine"), "not_creator"))

    def test_operator_mail_refused_when_off(self):
        address = register(self.store)
        with patch.dict(os.environ, {"GUARDIAN_RESIDENT": "off"}):
            result = self.store.send("operator", address, "message", "s", "b", op_id="au2")
        self.assertTrue(refuse(result, "resident_off"))

    def test_refusal_reasons_are_events(self):
        address = register(self.store)
        result = self.store.send("operator", "ghost@node-1", "message", "s", "b", op_id="au3")
        self.assertTrue(refuse(result, "unknown_agent"))
        refusals = [e for e in self.store.events() if e["kind"] == "refused"]
        self.assertEqual(refusals[0]["detail"]["reason"], "unknown_agent")


if __name__ == "__main__":
    unittest.main()

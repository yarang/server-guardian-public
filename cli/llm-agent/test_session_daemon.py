"""session_daemon: the workstation session link daemon (S2 of docs/design/operator-sessions.md),
driven over real AF_UNIX sockets in a short temp dir (macOS's 104-byte sun_path limit) with
session_local.call as the client, a FakeUplink that records every request and can block one,
a FakeWaker, a real Spool and an injected clock. No ssh, no network, no real tmux; the daemon's
tick thread is poked through its event, and background hub calls are waited on by polling."""

import calendar
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import session_daemon
import session_local
import session_wake

CONFIG = {"workstation": "ws-mac-1", "hub_address": "100.64.0.1", "hub_name": "",
          "hub_alias": "site-a-arm1", "hub_user": "ubuntu", "ssh_port": 22}
TMUX_SOCKET = "/tmp/tmux-1000/default"
DEAD_PID = 999_999_999            # over every kernel's pid_max: never a live process


def _epoch(stamp):
    """An ISO-UTC stamp as epoch seconds, so a fake clock can sit on the mail's timeline."""
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def wait_for(fn, timeout=5.0):
    """Polls fn (real time) until it is not None; the tests' only real waiting."""
    deadline = time.monotonic() + timeout
    value = fn()
    while value is None and time.monotonic() < deadline:
        time.sleep(0.01)
        value = fn()
    if value is None:
        raise AssertionError("condition not reached in time")
    return value


class FakeClock:
    """A fake wall clock the tests advance by hand (the daemon takes it as `clock`)."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeUplink:
    """The daemon's hub connection: records (op, payload), answers from a script (a dict per
    call in order, or a callable (op, payload) -> dict; the default is ok), and can fire the
    on_up callbacks and deliver mail exactly like the real Uplink would."""

    def __init__(self, state="up", last_error=None):
        self.state, self.last_error = state, last_error
        self.requests = []
        self.script = []
        self.on_up = []
        self.on_deliver = None
        self.spool = None            # the test wires the daemon's real spool in
        self.starts = self.stops = 0

    def start(self):
        self.starts += 1
        return self

    def stop(self):
        self.stops += 1

    def request(self, op, payload=None, timeout=None):
        self.requests.append((op, dict(payload or {})))
        answer = self.script.pop(0) if self.script else {"ok": True}
        if callable(answer):
            answer = answer(op, dict(payload or {}))
        return dict(answer)

    def fire_up(self):
        for callback in list(self.on_up):
            callback()

    def deliver(self, mail):
        """The real LinkClient stores the mail durably before on_deliver; so does the fake."""
        if self.spool is None or self.on_deliver is None:
            raise AssertionError("the daemon wired no spool or on_deliver")
        self.spool.store(mail)
        self.on_deliver(mail, True)

    def asked(self, op):
        return [(asked_op, payload) for asked_op, payload in self.requests if asked_op == op]


class FakeWaker:
    """Records every WakeTarget and answers try_wake from a script of (woke, reason)."""

    def __init__(self, results=None):
        self.targets = []
        self.results = list(results or [])

    def try_wake(self, target):
        self.targets.append(target)
        return self.results.pop(0) if self.results else (True, "woken")


class DaemonTestCase(unittest.TestCase):
    """A short temp home with a real config file, sockets under /tmp, fakes and a fake clock."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="sd-", dir="/tmp")   # short paths for macOS sun_path
        self.addCleanup(shutil.rmtree, tmp, True)
        self.home = Path(tmp)
        self.paths = session_local.SessionPaths(self.home)
        self.paths.ensure_dirs()
        self.paths.config.write_text(json.dumps(CONFIG), encoding="utf-8")
        self.clock = FakeClock()
        self.uplink = FakeUplink()
        self.waker = FakeWaker()
        self.daemon = None

    def tearDown(self):
        if self.daemon is not None:
            self.daemon.stop()
            self.daemon = None

    def start(self, **kwargs):
        kwargs.setdefault("uplink", self.uplink)
        kwargs.setdefault("waker", self.waker)
        kwargs.setdefault("clock", self.clock)
        self.daemon = session_daemon.Daemon(self.home, **kwargs)
        self.daemon.start()
        self.uplink.spool = self.daemon.spool
        return self.daemon

    def register(self, cli="cli-1", name=None, pid=None, pane=None, tmux_socket=None):
        frame = {"op": "register", "cli_session": cli, "cli_pid": pid or os.getpid(),
                 "tmux_socket": tmux_socket, "tmux_pane": pane, "source": "startup"}
        if name is not None:
            frame["name"] = name
        return session_local.call(self.paths.control_socket, frame,
                                  session_local.HOOK_CLIENT_TIMEOUT)

    def register_named(self, cli="cli-1", name="alpha", **kwargs):
        """register plus the wait for the background bind, so later tests see bound True."""
        reply = self.register(cli=cli, name=name, **kwargs)
        wait_for(lambda: (self.daemon._record(cli) or {}).get("bound") or None)
        return reply

    def hook(self, op, cli="cli-1", **fields):
        return session_local.call(self.paths.control_socket, {"op": op, "cli_session": cli,
                                                              **fields},
                                  session_local.HOOK_CLIENT_TIMEOUT)

    def mcp(self, op, cli="cli-1", **fields):
        return session_local.call(self.paths.session_socket(cli), {"op": op, **fields},
                                  session_local.LOCAL_CLIENT_TIMEOUT)

    def mail(self, mail_id, to="alpha@ws-mac-1", frm="beta@ws-wsl-1", subject="s", body="b",
             created="2026-11-21T10:00:00Z", expires="2026-11-22T10:00:00Z"):
        return {"id": mail_id, "thread": "t-1", "from": frm, "from_kind": "peer", "to": to,
                "kind": "message", "subject": subject, "body": body, "task": None,
                "reply_to": None, "hop": 0, "created": created,
                "expires": expires, "redelivered": False}

    def operator_mail(self, mail_id, **kwargs):
        return self.mail(mail_id, frm="operator", **kwargs)

    def record(self, cli="cli-1"):
        return self.daemon._record(cli) or {}


class RegisterTests(DaemonTestCase):
    def test_register_with_a_name_replies_at_once_and_binds_in_background(self):
        released = threading.Event()

        def blocked(op, payload):
            released.wait(5.0)
            return {"ok": True, "address": "alpha@ws-mac-1"}

        self.uplink.script = [blocked]
        self.start()
        began = time.monotonic()
        reply = self.register(name="alpha")
        self.assertLess(time.monotonic() - began, 2.0)     # the hook never waits for the hub
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["socket"], str(self.paths.session_socket("cli-1")))
        self.assertTrue(os.path.exists(reply["socket"]))
        self.assertEqual(reply["address"], "alpha@ws-mac-1")
        self.assertFalse(self.record()["bound"])           # not yet: the bind is still blocked
        released.set()
        wait_for(lambda: self.uplink.asked("bind") or None)
        self.assertEqual(self.uplink.asked("bind"),
                         [("bind", {"name": "alpha", "cli_session": "cli-1", "away": False})])
        wait_for(lambda: self.record().get("bound") or None)
        self.assertTrue(self.record()["bound"])

    def test_register_without_a_name_opens_the_socket_and_binds_nothing(self):
        self.start()
        reply = self.register()
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(reply["address"])
        self.assertTrue(os.path.exists(reply["socket"]))
        time.sleep(0.2)                                    # no bind may appear later
        self.assertEqual(self.uplink.requests, [])
        self.assertEqual(self.mcp("status")["state"], "linked")

    def test_register_replaces_the_record_on_a_second_register(self):
        self.start()
        self.register_named(name="alpha")
        self.assertTrue(self.hook("prompt_submit")["ok"])
        reply = self.register(name="beta")
        self.assertEqual(reply["address"], "beta@ws-mac-1")
        wait_for(lambda: len(self.uplink.asked("bind")) >= 2 or None)
        record = self.record()
        self.assertEqual(record["name"], "beta")
        self.assertFalse(record["busy"])                   # fresh record, no running turn

    def test_stop_removes_every_socket_the_daemon_created(self):
        self.start()
        self.register(cli="cli-1")
        self.assertTrue(os.path.exists(self.paths.control_socket))
        self.daemon.stop()
        self.daemon = None
        self.assertFalse(os.path.exists(self.paths.control_socket))
        self.assertFalse(os.path.exists(self.paths.session_socket("cli-1")))


class SocketContractTests(DaemonTestCase):
    def test_an_mcp_op_on_the_control_socket_is_bad_request(self):
        self.start()
        reply = session_local.call(self.paths.control_socket, {"op": "status"},
                                   session_local.HOOK_CLIENT_TIMEOUT)
        self.assertEqual(reply, session_local.error_reply(
            session_local.BAD_REQUEST, "op 'status' does not run on this socket"))

    def test_a_hook_op_on_a_session_socket_is_bad_request(self):
        self.start()
        self.register()
        reply = session_local.call(self.paths.session_socket("cli-1"),
                                   {"op": "stop", "cli_session": "cli-1"},
                                   session_local.LOCAL_CLIENT_TIMEOUT)
        self.assertEqual(reply, session_local.error_reply(
            session_local.BAD_REQUEST, "op 'stop' does not run on this socket"))

    def test_identity_fields_in_an_mcp_frame_are_bad_request(self):
        self.start()
        self.register()
        for field in ("cli_session", "from", "address", "sender"):
            with self.subTest(field=field):
                reply = self.mcp("status", **{field: "cli-1"})
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], session_local.BAD_REQUEST)

    def test_a_frame_that_cannot_be_read_gets_no_reply(self):
        self.start()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        self.addCleanup(sock.close)
        sock.connect(str(self.paths.control_socket))
        sock.sendall(b"this is not json\n")
        with self.assertRaises(ValueError):                # closed before a reply frame
            session_local.read_frame(sock.makefile("rb"))

    def test_an_invalid_register_name_is_refused_by_the_validator(self):
        self.start()
        reply = self.register(name="Not A Name")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], session_local.BAD_REQUEST)
        self.assertEqual(self.uplink.requests, [])


class HookStateTests(DaemonTestCase):
    def test_prompt_submit_reports_the_unread_count(self):
        self.start()
        self.register_named()
        self.assertEqual(self.hook("prompt_submit")["unread"], 0)
        self.daemon.spool.store(self.mail("m-1"))
        self.daemon.spool.store(self.mail("m-2", created="2026-11-21T10:00:01Z"))
        self.assertEqual(self.hook("prompt_submit")["unread"], 2)

    def test_prompt_submit_and_stop_refuse_an_unknown_session(self):
        self.start()
        self.assertEqual(self.hook("prompt_submit", cli="ghost")["error"],
                         session_local.NOT_REGISTERED)
        self.assertEqual(self.hook("stop", cli="ghost")["error"],
                         session_local.NOT_REGISTERED)

    def test_stop_blocks_once_and_not_again_without_an_inbox(self):
        self.start()
        self.register_named()
        self.assertTrue(self.hook("prompt_submit")["ok"])
        self.uplink.deliver(self.mail("m-1"))
        self.assertTrue(self.record()["mail_during_turn"])
        self.assertTrue(self.hook("stop")["block"])
        self.assertFalse(self.hook("stop")["block"])      # never twice without session_inbox

    def test_stop_does_not_block_an_away_session(self):
        self.start()
        self.register_named()
        self.assertTrue(self.mcp("link", away=True)["ok"])
        self.assertTrue(self.hook("prompt_submit")["ok"])
        self.uplink.deliver(self.mail("m-1"))
        self.assertFalse(self.hook("stop")["block"])

    def test_an_inbox_call_resets_the_block_guard(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        self.uplink.deliver(self.mail("m-1"))
        self.assertTrue(self.hook("stop")["block"])
        self.assertTrue(self.mcp("inbox")["ok"])
        self.hook("prompt_submit")
        self.uplink.deliver(self.mail("m-2"))
        self.assertTrue(self.hook("stop")["block"])       # the inbox call un-armed the guard

    def test_stop_does_not_block_when_inbox_already_read_during_turn(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        self.uplink.deliver(self.mail("m-1"))
        self.assertTrue(self.record()["mail_during_turn"])
        reply = self.mcp("inbox")
        self.assertEqual([m["id"] for m in reply["messages"]], ["m-1"])
        self.assertFalse(self.record()["mail_during_turn"])
        self.assertFalse(self.hook("stop")["block"])

    def test_stop_still_blocks_when_new_mail_arrives_after_mid_turn_inbox(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        self.uplink.deliver(self.mail("m-1"))
        self.assertTrue(self.mcp("inbox")["ok"])
        self.uplink.deliver(self.mail("m-2", created="2026-11-21T10:00:01Z"))
        self.assertTrue(self.record()["mail_during_turn"])
        self.assertTrue(self.hook("stop")["block"])

    def test_inbox_keeps_mail_during_turn_when_mail_arrives_during_ack(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        self.uplink.deliver(self.mail("m-1"))

        def deliver_during_ack(op, payload):
            if op == "ack":
                self.uplink.deliver(self.mail("m-2", created="2026-11-21T10:00:01Z"))
            return {"ok": True}

        self.uplink.script.append(deliver_during_ack)
        reply = self.mcp("inbox")
        self.assertEqual([m["id"] for m in reply["messages"]], ["m-1"])
        self.assertTrue(self.record()["mail_during_turn"])
        self.assertTrue(self.hook("stop")["block"])

    def test_prompt_submit_and_stop_send_state_in_the_background(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        wait_for(lambda: self.uplink.asked("state") or None)
        self.assertEqual(self.uplink.asked("state"),
                         [("state", {"name": "alpha", "state": "busy"})])
        self.hook("stop")
        wait_for(lambda: len(self.uplink.asked("state")) >= 2 or None)
        self.assertEqual(self.uplink.asked("state")[-1],
                         ("state", {"name": "alpha", "state": "linked"}))

    def test_end_removes_the_record_the_socket_and_unbinds(self):
        self.start()
        self.register_named()
        self.assertTrue(os.path.exists(self.paths.session_socket("cli-1")))
        self.assertTrue(self.hook("end", reason="clean")["ok"])
        self.assertFalse(os.path.exists(self.paths.session_socket("cli-1")))
        self.assertEqual(self.hook("prompt_submit")["error"], session_local.NOT_REGISTERED)
        wait_for(lambda: self.uplink.asked("unbind") or None)
        self.assertEqual(self.uplink.asked("unbind"), [("unbind", {"name": "alpha"})])


class McpLinkTests(DaemonTestCase):
    def test_link_with_no_fields_reports_the_current_address_and_away(self):
        self.start()
        self.register()
        reply = self.mcp("link")
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(reply["address"])
        self.assertFalse(reply["away"])

    def test_link_rename_succeeds_and_name_in_use_keeps_the_old_address(self):
        self.start()
        self.register_named()
        self.assertEqual(self.mcp("link", name="beta")["address"], "beta@ws-mac-1")
        self.assertEqual(self.record()["name"], "beta")
        self.uplink.script.append({"ok": False, "error": "name_in_use",
                                   "detail": "gamma is in use on ws-mac-1"})
        reply = self.mcp("link", name="gamma")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], session_local.HUB_ERROR)
        self.assertEqual(reply["detail"], "name_in_use: gamma is in use on ws-mac-1")
        self.assertEqual(self.record()["name"], "beta")   # the old name stays
        self.assertEqual(self.mcp("link")["address"], "beta@ws-mac-1")

    def test_link_maps_link_down_and_timeout(self):
        self.start()
        self.register_named()
        for answer, code in (({"ok": False, "error": "link_down"}, session_local.LINK_DOWN),
                             ({"ok": False, "error": "timeout"}, session_local.TIMEOUT)):
            with self.subTest(code=code):
                self.uplink.script.append(answer)
                reply = self.mcp("link", name="beta")
                self.assertEqual(reply["error"], code)
                self.assertEqual(self.record()["name"], "alpha")

    def test_link_away_and_back_send_state_and_store_the_flag(self):
        self.start()
        self.register_named()
        reply = self.mcp("link", away=True)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["away"])
        self.assertTrue(self.record()["away"])
        self.assertEqual(self.uplink.asked("state"),
                         [("state", {"name": "alpha", "state": "away"})])
        self.assertFalse(self.mcp("link", away=False)["away"])
        self.assertFalse(self.record()["away"])
        self.assertEqual(self.uplink.asked("state")[-1],
                         ("state", {"name": "alpha", "state": "back"}))

    def test_link_away_without_a_name_is_not_linked(self):
        self.start()
        self.register()
        reply = self.mcp("link", away=True)
        self.assertEqual(reply["error"], session_local.NOT_LINKED)
        self.assertEqual(self.uplink.requests, [])


class McpInboxTests(DaemonTestCase):
    def test_inbox_returns_oldest_first_marks_retrieved_and_acks(self):
        self.start()
        self.register_named()
        self.daemon.spool.store(self.mail("m-2", created="2026-11-21T10:00:02Z"))
        self.daemon.spool.store(self.mail("m-1", created="2026-11-21T10:00:01Z"))
        self.daemon.spool.store(self.mail("x-1", to="beta@ws-mac-1"))
        reply = self.mcp("inbox")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([mail["id"] for mail in reply["messages"]], ["m-1", "m-2"])
        self.assertEqual(set(reply["messages"][0]),
                         {"id", "from", "kind", "thread", "reply_to", "subject", "created",
                          "body"})
        wait_for(lambda: self.uplink.asked("ack") or None)
        self.assertEqual(self.uplink.asked("ack"),
                         [("ack", {"name": "alpha", "ids": ["m-1", "m-2"]})])
        self.assertEqual(self.mcp("inbox")["messages"], [])   # retrieved: not returned twice

    def test_inbox_survives_a_failed_ack(self):
        self.start()
        self.register_named()
        self.daemon.spool.store(self.mail("m-1"))
        self.uplink.script.append({"ok": False, "error": "link_down"})
        reply = self.mcp("inbox")
        self.assertTrue(reply["ok"], reply)               # the ack is best effort
        self.assertEqual([mail["id"] for mail in reply["messages"]], ["m-1"])

    def test_inbox_without_a_name_is_not_linked(self):
        self.start()
        self.register()
        self.assertEqual(self.mcp("inbox")["error"], session_local.NOT_LINKED)


class McpSendListTests(DaemonTestCase):
    def send_frame(self):
        return {"to": "operator", "kind": "message", "subject": "hello", "body": "text"}

    def test_send_carries_the_name_and_a_fresh_op_id_per_call(self):
        self.start()
        self.register_named()
        self.uplink.script.append({"ok": True, "id": "m-9", "thread": "t-1", "hop": 1})
        self.assertEqual(self.mcp("send", **self.send_frame())["id"], "m-9")
        self.mcp("send", **self.send_frame())
        sends = self.uplink.asked("send")
        self.assertEqual(len(sends), 2)
        payloads = [payload for _, payload in sends]
        first = payloads[0]
        self.assertEqual([first["name"], first["to"], first["kind"], first["subject"],
                          first["body"]],
                         ["alpha", "operator", "message", "hello", "text"])
        self.assertNotEqual(payloads[0]["op_id"], payloads[1]["op_id"])
        for payload in payloads:
            self.assertEqual(len(payload["op_id"]), 32)

    def test_send_maps_link_down_timeout_and_refusals(self):
        self.start()
        self.register_named()
        for answer, code, detail in (
                ({"ok": False, "error": "link_down"}, session_local.LINK_DOWN, ""),
                ({"ok": False, "error": "timeout"}, session_local.TIMEOUT, ""),
                ({"ok": False, "error": "rate_limit", "detail": "31 mails in the last hour"},
                 session_local.HUB_ERROR, "rate_limit: 31 mails in the last hour")):
            with self.subTest(code=code):
                self.uplink.script.append(answer)
                reply = self.mcp("send", **self.send_frame())
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], code)
                self.assertEqual(reply["detail"], detail)

    def test_list_passes_the_hub_peer_list_through(self):
        self.start()
        self.register_named()
        peers = [{"address": "operator", "kind": "operator", "state": "operator",
                  "last_seen": None}]
        self.uplink.script.append({"ok": True, "peers": peers})
        reply = self.mcp("list")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["peers"], peers)
        self.assertEqual(self.uplink.asked("list"), [("list", {"name": "alpha"})])

    def test_list_and_send_without_a_name_are_not_linked(self):
        self.start()
        self.register()
        self.assertEqual(self.mcp("list")["error"], session_local.NOT_LINKED)
        self.assertEqual(self.mcp("send", **self.send_frame())["error"],
                         session_local.NOT_LINKED)

    def test_status_reports_the_local_view(self):
        self.start()
        self.register_named()
        self.daemon.spool.store(self.mail("m-1"))
        reply = self.mcp("status")
        self.assertEqual(reply["link"], "up")
        self.assertIsNone(reply["last_error"])
        self.assertEqual(reply["address"], "alpha@ws-mac-1")
        self.assertFalse(reply["away"])
        self.assertEqual(reply["state"], "linked")
        self.assertEqual(reply["unread"], 1)
        self.assertFalse(reply["remote_approval"])
        self.assertFalse(reply["summary_mirror"])
        self.clock.advance(session_local.IDLE_AFTER)
        self.assertEqual(self.mcp("status")["state"], "idle")


class McpReviewTests(DaemonTestCase):
    """The review tools of V2d: submit goes to the hub as one review_submit frame, result polls
    review_get exactly like resident_query_get polls query_get, and both need a linked session."""

    def test_submit_needs_a_linked_session(self):
        self.start()
        self.register()
        reply = self.mcp("review_submit", project="server-guardian", ref="feature/retry")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], session_local.NOT_LINKED)
        self.assertEqual(self.uplink.requests, [])

    def test_submit_sends_the_documented_fields_and_returns_the_hub_answer(self):
        self.start()
        self.register_named()
        self.uplink.script.append({"ok": True, "id": "review_20261124_090101_ab12cd",
                                   "status": "running", "label": "REVIEW",
                                   "note": "advisory, no Judge"})
        reply = self.mcp("review_submit", project="server-guardian", ref="feature/retry",
                         base="main", expect_sha="a" * 40, provider="claude")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["id"], "review_20261124_090101_ab12cd")
        self.assertEqual(reply["status"], "running")
        self.assertEqual(self.uplink.asked("review_submit"), [(
            "review_submit", {"name": "alpha", "project": "server-guardian",
                              "ref": "feature/retry", "base": "main", "expect_sha": "a" * 40,
                              "provider": "claude"})])

    def test_submit_without_the_optional_fields_sends_only_the_required_ones(self):
        self.start()
        self.register_named()
        self.uplink.script.append({"ok": True, "id": "review_20261124_090102_cd34ef",
                                   "status": "running"})
        reply = self.mcp("review_submit", project="server-guardian", ref="main")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.uplink.asked("review_submit"), [(
            "review_submit", {"name": "alpha", "project": "server-guardian", "ref": "main"})])

    def test_submit_maps_hub_refusals(self):
        self.start()
        self.register_named()
        for answer, code, detail in (
                ({"ok": False, "error": "link_down"}, session_local.LINK_DOWN, ""),
                ({"ok": False, "error": "timeout"}, session_local.TIMEOUT, ""),
                ({"ok": False, "error": "hub_error", "detail": "invalid project"},
                 session_local.HUB_ERROR, "hub_error: invalid project")):
            with self.subTest(code=code):
                self.uplink.script.append(answer)
                reply = self.mcp("review_submit", project="server-guardian", ref="main")
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], code)
                self.assertEqual(reply["detail"], detail)

    def test_result_polls_until_completed(self):
        self.start()
        self.register_named()
        review_id = "review_20261124_090101_ab12cd"
        self.uplink.script.append({"ok": True, "review": {"id": review_id, "status": "running"}})
        self.uplink.script.append({"ok": True, "review": {
            "id": review_id, "status": "completed", "state": "done", "summary": "1 finding",
            "counts": {"high": 1, "medium": 0, "low": 0},
            "findings": [{"severity": "high", "title": "off by one", "file": "a.go", "line": 9,
                          "detail": "wrong index"}]}})
        reply = self.mcp("review_result", review_id=review_id, wait_sec=15)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["done"])
        self.assertEqual(reply["review"]["state"], "done")
        self.assertEqual(reply["review"]["findings"][0]["title"], "off by one")
        asked = self.uplink.asked("review_get")
        self.assertEqual(len(asked), 2)      # one poll per second until the terminal status
        self.assertEqual(asked[0], ("review_get", {"name": "alpha", "review_id": review_id}))

    def test_result_with_no_wait_returns_the_first_snapshot(self):
        self.start()
        self.register_named()
        self.uplink.script.append({"ok": True, "review": {
            "id": "review_20261124_090101_ab12cd", "status": "running"}})
        reply = self.mcp("review_result", review_id="review_20261124_090101_ab12cd")
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(reply["done"])
        self.assertEqual(reply["review"]["status"], "running")
        self.assertEqual(len(self.uplink.asked("review_get")), 1)

    def test_result_maps_not_found_and_needs_a_linked_session(self):
        self.start()
        self.register()
        self.assertEqual(self.mcp("review_result",
                                  review_id="review_20261124_090101_ab12cd")["error"],
                         session_local.NOT_LINKED)
        self.register_named()
        self.uplink.script.append({"ok": False, "error": "not_found",
                                   "detail": "review not found"})
        reply = self.mcp("review_result", review_id="review_20261124_090101_ab12cd")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], session_local.HUB_ERROR)
        self.assertEqual(reply["detail"], "not_found: review not found")

    def test_result_wait_is_capped_at_query_wait_max(self):
        """A frame that skipped the validator (the cap is the handler's own guard, as in
        _poll_resident_query): a huge wait_sec is cut to QUERY_WAIT_MAX, one poll per second, and
        with the max at 0 exactly one poll happens however long the ask was."""
        self.start()
        self.register_named()
        self.uplink.script.append(
            lambda op, payload: {"ok": True, "review": {"id": payload["review_id"],
                                                        "status": "running"}})
        with patch.object(session_local, "QUERY_WAIT_MAX", 0):
            reply = self.daemon._mcp_review_result(
                {"review_id": "review_20261124_090101_ab12cd", "wait_sec": 9999}, "cli-1")
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(reply["done"])
        self.assertEqual(reply["review"]["status"], "running")
        self.assertEqual(len(self.uplink.asked("review_get")), 1)


class UplinkCallbackTests(DaemonTestCase):
    def test_on_up_rebinds_named_sessions_and_resends_busy(self):
        self.start()
        self.register_named(cli="cli-1", name="alpha")
        self.register(cli="cli-2")                        # no name: never re-bound
        self.hook("prompt_submit", cli="cli-1")
        wait_for(lambda: self.uplink.asked("state") or None)   # the prompt's own busy state
        self.uplink.requests.clear()
        self.uplink.fire_up()
        wait_for(lambda: self.uplink.asked("bind") or None)
        self.assertEqual(self.uplink.asked("bind"),
                         [("bind", {"name": "alpha", "cli_session": "cli-1", "away": False})])
        wait_for(lambda: self.uplink.asked("state") or None)
        self.assertEqual(self.uplink.asked("state"),
                         [("state", {"name": "alpha", "state": "busy"})])

    def test_a_refused_rebind_leaves_the_session_unbound(self):
        self.start()
        self.register_named()
        self.uplink.script.append({"ok": False, "error": "name_in_use",
                                   "detail": "alpha is in use on ws-mac-1"})
        self.uplink.requests.clear()
        self.uplink.fire_up()
        wait_for(lambda: self.record().get("bound") is False or None)
        self.assertFalse(self.record()["bound"])

    def test_on_deliver_marks_mail_that_arrived_during_a_turn(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        self.uplink.deliver(self.mail("m-1"))
        self.assertTrue(self.record()["mail_during_turn"])
        self.hook("prompt_submit")                        # a new turn clears the flag
        self.assertFalse(self.record()["mail_during_turn"])


class WakeTests(DaemonTestCase):
    def register_idle_tmux(self, name="alpha"):
        self.register_named(name=name, pane="%3", tmux_socket=TMUX_SOCKET)
        self.clock.advance(session_local.IDLE_AFTER + 1)  # linked, no turn for 10 minutes

    def test_operator_mail_to_an_idle_tmux_session_wakes_once(self):
        self.start()
        self.register_idle_tmux()
        self.uplink.deliver(self.operator_mail("m-1"))
        wait_for(lambda: self.waker.targets or None)
        self.assertEqual(self.waker.targets,
                         [session_wake.WakeTarget("%3", TMUX_SOCKET, os.getpid())])
        record = self.record()
        self.assertIsNotNone(record["last_wake"])
        self.assertFalse(record["pending_wake"])
        self.assertFalse(record["inbox_since_wake"])

    def test_peer_mail_never_wakes(self):
        self.start()
        self.register_idle_tmux()
        self.uplink.deliver(self.mail("m-1"))             # from another session, not operator
        wait_for(lambda: self.daemon.spool.unread_count("alpha") == 1 or None)
        time.sleep(0.2)                                   # the deliver signalled the tick
        self.assertEqual(self.waker.targets, [])

    def test_no_second_wake_without_an_inbox_call(self):
        self.start()
        self.register_idle_tmux()
        self.uplink.deliver(self.operator_mail("m-1"))
        wait_for(lambda: self.waker.targets or None)
        self.uplink.deliver(self.operator_mail("m-2"))
        wait_for(lambda: self.daemon.spool.unread_count("alpha") == 2 or None)
        time.sleep(0.2)
        self.assertEqual(len(self.waker.targets), 1)      # guard 6: recent_wake

    def test_a_session_without_tmux_only_queues_the_mail(self):
        self.start()
        self.register_named()                             # no pane, no tmux socket
        self.clock.advance(session_local.IDLE_AFTER + 1)
        self.uplink.deliver(self.operator_mail("m-1"))
        wait_for(lambda: self.daemon.spool.unread_count("alpha") == 1 or None)
        time.sleep(0.2)
        self.assertEqual(self.waker.targets, [])
        self.assertEqual(self.hook("prompt_submit")["unread"], 1)

    def test_a_refused_wake_is_retried_on_the_next_tick(self):
        self.waker = FakeWaker(results=[(False, "client_active"), (True, "woken")])
        self.start()
        self.register_idle_tmux()
        self.uplink.deliver(self.operator_mail("m-1"))
        wait_for(lambda: len(self.waker.targets) == 1 or None)
        self.assertTrue(self.record()["pending_wake"])    # kept: the next tick retries
        self.daemon._tick_event.set()
        wait_for(lambda: len(self.waker.targets) == 2 or None)
        self.assertFalse(self.record()["pending_wake"])

    def test_pending_wake_clears_once_the_spool_has_no_unread_mail(self):
        self.start()
        self.register_idle_tmux()
        self.uplink.deliver(self.operator_mail("m-1"))
        wait_for(lambda: self.waker.targets or None)
        self.waker.results = [(False, "client_active")]   # force the pending flag to stay
        self.clock.advance(session_local.WAKE_INTERVAL + 1)
        self.uplink.deliver(self.operator_mail("m-2"))
        wait_for(lambda: len(self.waker.targets) == 2 or None)
        self.assertTrue(self.record()["pending_wake"])
        self.daemon.spool.mark_retrieved(["m-1", "m-2"])  # everything read by hand
        self.daemon._tick_event.set()
        wait_for(lambda: self.record().get("pending_wake") is False or None)


class TickTests(DaemonTestCase):
    def test_a_busy_turn_ends_at_the_age_cap(self):
        self.start()
        self.register_named()
        self.hook("prompt_submit")
        self.assertTrue(self.record()["busy"])
        self.clock.advance(session_local.BUSY_AGE_CAP + 1)
        self.daemon._tick_event.set()
        wait_for(lambda: self.record().get("busy") is False or None)
        self.assertEqual(self.record()["last_turn"], self.clock.now)

    def test_the_spool_is_pruned_from_the_tick(self):
        self.clock = FakeClock(_epoch("2026-11-21T12:00:00Z"))
        self.start()
        self.register_named()
        self.daemon.spool.store(self.mail("gone", expires="2026-11-21T11:00:00Z"))
        self.assertEqual(self.daemon.spool.unread_count("alpha"), 1)
        self.clock.advance(session_daemon.PRUNE_INTERVAL + 1)
        self.daemon._tick_event.set()
        wait_for(lambda: self.daemon.spool.unread_count("alpha") == 0 or None)


class PersistenceTests(DaemonTestCase):
    def test_restart_restores_live_sessions_and_drops_a_dead_pid(self):
        self.start()
        self.register_named(cli="live-1", name="alpha")
        self.register(cli="dead-1", pid=DEAD_PID, name="beta")
        wait_for(lambda: len(self.uplink.asked("bind")) == 2 or None)
        self.assertTrue(os.path.exists(self.paths.session_socket("dead-1")))
        self.daemon.stop()
        self.daemon = None
        self.uplink = FakeUplink()
        self.start()
        self.assertTrue(os.path.exists(self.paths.session_socket("live-1")))
        self.assertFalse(os.path.exists(self.paths.session_socket("dead-1")))
        reply = self.mcp("status", cli="live-1")
        self.assertEqual(reply["address"], "alpha@ws-mac-1")
        with self.assertRaises(OSError):                  # the dead session's socket is gone
            session_local.call(self.paths.session_socket("dead-1"), {"op": "status"},
                               session_local.LOCAL_CLIENT_TIMEOUT)

    def test_tick_reaps_dead_pid_and_unbinds_on_hub(self):
        self.start()
        self.register_named(cli="live-1", name="alpha")
        self.register_named(cli="dead-1", name="beta", pid=DEAD_PID)
        self.assertTrue(os.path.exists(self.paths.session_socket("dead-1")))
        self.daemon._tick_once(self.clock())
        self.assertIsNotNone(self.daemon._record("live-1"))
        self.assertIsNone(self.daemon._record("dead-1"))
        self.assertFalse(os.path.exists(self.paths.session_socket("dead-1")))
        wait_for(lambda: self.uplink.asked("unbind") or None)
        self.assertEqual(self.uplink.asked("unbind"), [("unbind", {"name": "beta"})])


class LoggingTests(DaemonTestCase):
    def test_the_log_holds_events_but_never_mail_content(self):
        subject, body = "SUBJECT-CANARY", "BODY-CANARY"
        self.start()
        self.register_named(pane="%3", tmux_socket=TMUX_SOCKET)
        self.clock.advance(session_local.IDLE_AFTER + 1)
        self.mcp("send", to="operator", kind="message", subject=subject, body=body)
        self.uplink.deliver(self.operator_mail("m-1", subject=subject, body=body))
        wait_for(lambda: self.waker.targets or None)
        wait_for(lambda: self.paths.log.exists() and self.paths.log.stat().st_size or None)
        text = self.paths.log.read_text(encoding="utf-8")
        self.assertTrue(text.strip())                     # events were written
        self.assertNotIn(subject, text)                  # never a subject
        self.assertNotIn(body, text)                     # never a body


class CommandLineTests(DaemonTestCase):
    def test_check_config_prints_ok_and_exits_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = session_daemon.main(["--home", str(self.home), "--check-config"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "ok")

    def test_check_config_reports_a_bad_config_and_exits_two(self):
        self.paths.config.write_text(json.dumps({"workstation": "ws-mac-1"}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = session_daemon.main(["--home", str(self.home), "--check-config"])
        self.assertEqual(code, 2)
        self.assertIn("hub_address", out.getvalue())


if __name__ == "__main__":
    unittest.main()

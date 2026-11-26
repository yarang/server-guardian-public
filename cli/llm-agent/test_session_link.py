"""session_link: the hub's end of one workstation link (S1c of docs/design/operator-sessions.md),
driven over fake streams with an injected clock: the entry refusals, the handshake and the
takeover rule (a failed dial never cuts a healthy link), the frame handlers, mail delivery,
liveness and the poll ends. No real ssh, no network, no real sleeps over 0.5 s."""

import base64
import io
import json
import os
import queue
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import mail_store
import session_link
import session_registry
import testenv
from test_mail_store import register

# A public key built here (the wire format of an ed25519 key), so no fixture carries a real key.
KEY = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32).decode("ascii")


def register_workstation(ws_id="ws-mac-1"):
    """Writes the workstation registry entry the entry checks and the poll re-reads."""
    path = session_registry.declaration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": ws_id, "address": "100.64.0.5", "key": KEY, "node": None,
             "added": "2026-11-20T10:00:00Z"}
    path.write_text(json.dumps([entry]), encoding="utf-8")
    return path


class FakeMonotonic:
    """A fake time.monotonic the tests advance by hand (the session reads only this)."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeReader:
    """Queue-backed stdin: send() queues one frame line, eof() ends the stream. No fileno(), so
    it never looks like a terminal."""

    def __init__(self):
        self._lines = queue.Queue()

    def send(self, frame):
        self._lines.put((json.dumps(frame) + "\n").encode("utf-8"))

    def eof(self):
        self._lines.put(None)

    def readline(self):
        item = self._lines.get()
        return b"" if item is None else item


class TtyReader(FakeReader):
    """A stdin that reports a terminal (entry must refuse it)."""

    def fileno(self):
        return 0


class FakeWriter:
    """List-backed stdout: the writer thread appends raw frame lines; tests parse them."""

    def __init__(self):
        self.chunks = []
        self._lock = threading.Lock()

    def write(self, data):
        with self._lock:
            self.chunks.append(bytes(data))

    def flush(self):
        pass

    def frames(self):
        with self._lock:
            raw = b"".join(self.chunks)
        out = []
        for line in raw.splitlines():
            try:
                out.append(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass  # a partial line still being written
        return out

    def expect(self, kind, timeout=2.0):
        """The first frame with this t, once the writer thread has produced it."""
        found = self.wait_for(lambda: next((f for f in self.frames() if f.get("t") == kind),
                                           None), timeout)
        return found

    @staticmethod
    def wait_for(fn, timeout=2.0):
        deadline = time.monotonic() + timeout
        value = fn()
        while value is None and time.monotonic() < deadline:
            time.sleep(0.01)
            value = fn()
        if value is None:
            raise AssertionError("condition not reached in time")
        return value


class LinkEndSpy:
    """A store proxy that records link_end calls (the superseded end must make none)."""

    def __init__(self, store):
        self._store = store
        self.link_end_calls = []

    def __getattr__(self, name):
        return getattr(self._store, name)

    def link_end(self, *args):
        self.link_end_calls.append(args)
        return self._store.link_end(*args)


class LinkTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        # Never let a registry call fall back to the real ~/.ssh.
        env = patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE":
                                      str(Path(os.environ["GUARDIAN_DATA_DIR"]) / "authorized_keys2")})
        env.start()
        self.addCleanup(env.stop)
        register_workstation()
        self.data_dir = Path(os.environ["GUARDIAN_DATA_DIR"])
        self.clock = FakeMonotonic()
        self.reader, self.writer = FakeReader(), FakeWriter()
        self.hub = mail_store.MailStore()
        self.session = None
        self.thread = None
        self.result = None
        self.extras = []

    def tearDown(self):
        if self.thread is not None:
            self.thread.join(timeout=5.0)
            self.assertFalse(self.thread.is_alive(), "the link thread did not stop")
        for stream in self.extras:
            stream.reader.eof()
            stream.thread.join(timeout=5.0)
            self.assertFalse(stream.thread.is_alive(), "an extra stream did not stop")
        self.reader.eof()

    # --- harness -------------------------------------------------------------------------

    def start(self, **kwargs):
        kwargs.setdefault("store", mail_store.MailStore())
        self.session = session_link.LinkSession("ws-mac-1", self.reader, self.writer,
                                                clock=self.clock, wake=0.05, **kwargs)

        def run():
            self.result = self.session.run()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self.session

    def dial(self, **kwargs):
        """A competing stream to the same workstation: its own fakes, clock and thread."""
        reader, writer, clock = FakeReader(), FakeWriter(), FakeMonotonic()
        session = session_link.LinkSession("ws-mac-1", reader, writer, clock=clock, wake=0.05,
                                           **kwargs)
        holder = []
        thread = threading.Thread(target=lambda: holder.append(session.run()), daemon=True)
        thread.start()
        stream = SimpleNamespace(reader=reader, writer=writer, clock=clock, session=session,
                                 thread=thread, result=holder)
        self.extras.append(stream)
        return stream

    def handshake(self):
        self.reader.send({"t": "hello", "proto": session_link.LINK_PROTO,
                          "version": session_link.LINK_VERSION})
        return self.writer.expect("welcome")

    def hello_frame(self, **overrides):
        frame = {"t": "hello", "proto": session_link.LINK_PROTO,
                 "version": session_link.LINK_VERSION}
        frame.update(overrides)
        return frame

    def join(self, timeout=5.0):
        self.thread.join(timeout=timeout)
        self.assertFalse(self.thread.is_alive(), "the link thread did not stop")
        self.assertIsNotNone(self.result)
        return self.result

    def wait_for(self, fn, timeout=2.0):
        return FakeWriter.wait_for(fn, timeout)

    def expect_reply(self, re_id, timeout=2.0):
        return self.wait_for(
            lambda: next((f for f in self.writer.frames()
                          if f.get("t") == "reply" and f.get("re") == re_id), None), timeout)

    def mailbox_exists(self):
        return (self.data_dir / "mailbox").exists()

    def link_row(self):
        conn = self.hub._ready()
        return dict(conn.execute("SELECT * FROM links WHERE workstation='ws-mac-1'").fetchone())

    def mail_row(self, mail_id):
        return next(m for m in self.hub.list_mail(limit=200) if m["id"] == mail_id)

    def bind(self, name="alpha", cli_session="cli-1"):
        self.reader.send({"t": "bind", "id": "d1", "name": name, "cli_session": cli_session})
        return self.expect_reply("d1")


class EntryRefusals(LinkTestCase):
    def entry(self, workstation="ws-mac-1", env=None, stdin=None, stderr=None, path=None):
        return session_link.entry(workstation, env=env if env is not None else {},
                                  stdin=stdin if stdin is not None else self.reader,
                                  stdout=self.writer,
                                  stderr=stderr if stderr is not None else io.StringIO(),
                                  agent_env_path=path or self.data_dir / "none.env")

    def test_ssh_original_command_exits_without_any_output(self):
        env = {"SSH_ORIGINAL_COMMAND": "sftp"}
        stderr = io.StringIO()
        code = self.entry(env=env, stderr=stderr)
        self.assertEqual(code, 2)
        self.assertEqual(env, {"SSH_ORIGINAL_COMMAND": "sftp"})  # refused before anything runs
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.writer.chunks, [])
        self.assertFalse(self.mailbox_exists())

    def test_terminal_stdin_refused(self):
        stderr = io.StringIO()
        with patch("os.isatty", return_value=True):
            code = self.entry(stdin=TtyReader(), stderr=stderr)
        self.assertEqual(code, 2)
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertFalse(self.mailbox_exists())

    def test_agent_env_loads_guardian_keys_only(self):
        with TemporaryDirectory() as tmp:
            agent_env = Path(tmp) / "agent.env"
            agent_env.write_text("# comment\n"
                                 "\n"
                                 "GUARDIAN_SESSIONS=off\n"
                                 "AGENT_TOKEN=secret\n"
                                 "NO_PREFIX=x\n"
                                 "GUARDIAN_PRESENT=from-file\n"
                                 'GUARDIAN_QUOTED="quoted value"\n', encoding="utf-8")
            env = {"GUARDIAN_PRESENT": "from-env"}
            stderr = io.StringIO()
            code = self.entry(env=env, stderr=stderr, path=agent_env)
        self.assertEqual(code, 2)
        self.assertIn("sessions off", stderr.getvalue())
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        # the file is loaded, filtered and never overwrites the caller's environment
        self.assertEqual(env, {"GUARDIAN_PRESENT": "from-env", "GUARDIAN_QUOTED": "quoted value",
                               "GUARDIAN_SESSIONS": "off", "LANG": "C.UTF-8",
                               "PATH": "/usr/bin:/bin"})
        self.assertNotIn("GUARDIAN_QUOTED", os.environ)  # os.environ is never mutated

    def test_unknown_workstation_refused(self):
        stderr = io.StringIO()
        code = self.entry(workstation="ws-ghost-1", stderr=stderr)
        self.assertEqual(code, 2)
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertFalse(self.mailbox_exists())
        code = self.entry(workstation="bad id!", stderr=io.StringIO())
        self.assertEqual(code, 2)

    def test_entry_serves_one_full_stream(self):
        self.reader.send(self.hello_frame())
        self.reader.eof()
        stderr = io.StringIO()
        code = self.entry(stderr=stderr)
        self.assertEqual(code, 0)
        frames = self.writer.frames()
        self.assertEqual(frames[0], {"t": "greeting", "proto": session_link.LINK_PROTO,
                                     "version": session_link.LINK_VERSION,
                                     "workstation": "ws-mac-1"})
        self.assertEqual((frames[1]["t"], frames[1]["lease"]), ("welcome", 1))
        self.assertEqual(frames[-1], {"t": "bye", "reason": "stream_end"})


class Handshake(LinkTestCase):
    def test_greeting_first_then_welcome_lease(self):
        self.start()
        self.assertEqual(self.handshake()["lease"], 1)
        frames = self.writer.frames()
        self.assertEqual(frames[0]["t"], "greeting")
        self.assertEqual(frames[1]["t"], "welcome")
        self.reader.eof()
        self.assertEqual(self.join(), 0)
        self.assertEqual(self.writer.frames()[-1]["reason"], "stream_end")

    def test_wrong_hello_version_claims_nothing(self):
        self.start()
        self.reader.send(self.hello_frame(version=2))
        self.assertEqual(self.writer.expect("bye")["reason"], "protocol_mismatch")
        self.assertEqual(self.join(), 1)
        self.assertFalse(self.mailbox_exists())  # link_claim never ran

    def test_wrong_first_frame_refused(self):
        self.start()
        self.reader.send({"t": "bind", "id": "d1", "name": "alpha", "cli_session": "cli-1"})
        self.assertEqual(self.writer.expect("bye")["reason"], "protocol_mismatch")
        self.assertEqual(self.join(), 1)
        self.assertFalse(self.mailbox_exists())

    def test_handshake_timeout(self):
        self.start()
        time.sleep(0.1)  # let the session compute its deadline first
        self.clock.advance(session_link.LinkSession.HANDSHAKE_TIMEOUT + 1)
        self.assertEqual(self.writer.expect("bye")["reason"], "handshake_timeout")
        self.assertEqual(self.join(), 1)
        self.assertFalse(self.mailbox_exists())

    def test_stream_end_before_handshake_is_an_error_end(self):
        self.start()
        self.reader.eof()
        self.assertEqual(self.writer.expect("bye")["reason"], "stream_end")
        self.assertEqual(self.join(), 1)
        self.assertFalse(self.mailbox_exists())


class Takeover(LinkTestCase):
    def test_failed_dial_never_cuts_a_live_link(self):
        self.start()
        self.assertEqual(self.handshake()["lease"], 1)
        bad = self.dial(store=mail_store.MailStore())
        bad.reader.send(self.hello_frame(version=9))
        bad.writer.expect("bye")
        bad.thread.join(timeout=3.0)
        self.assertFalse(bad.thread.is_alive())
        self.assertEqual(bad.result, [1])
        row = self.link_row()  # the failed dial changed neither the lease nor the state
        self.assertEqual((row["lease"], row["state"]), (1, "up"))
        self.assertTrue(self.hub.link_current("ws-mac-1", 1))
        self.reader.send({"t": "ping", "id": "d9"})  # A still serves
        reply = self.expect_reply("d9")
        self.assertTrue(reply["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_completed_handshake_supersedes_without_link_end(self):
        spy = LinkEndSpy(mail_store.MailStore())
        self.start(store=spy)
        self.assertEqual(self.handshake()["lease"], 1)
        new = self.dial(store=mail_store.MailStore())
        new.reader.send(self.hello_frame())
        self.assertEqual(new.writer.expect("welcome")["lease"], 2)
        self.clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
        self.assertEqual(self.writer.expect("bye")["reason"], "superseded")
        self.assertEqual(self.join(), 0)
        self.assertEqual(spy.link_end_calls, [])  # the link is the new stream's now
        row = self.link_row()
        self.assertEqual((row["lease"], row["state"]), (2, "up"))


class DaemonFrames(LinkTestCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.handshake()

    def test_bind_state_ack_unbind_and_unknown_frame(self):
        reply = self.bind()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["address"], "alpha@ws-mac-1")
        self.reader.send({"t": "state", "id": "d2", "name": "alpha", "state": "busy"})
        self.assertTrue(self.expect_reply("d2")["ok"])
        self.reader.send({"t": "state", "id": "d3", "name": "alpha", "state": "away"})
        self.assertTrue(self.expect_reply("d3")["ok"])
        self.reader.send({"t": "ack", "id": "d4", "name": "alpha", "ids": []})
        self.assertEqual(self.expect_reply("d4")["ids"], [])
        self.reader.send({"t": "unbind", "id": "d5", "name": "alpha"})
        self.assertEqual(self.expect_reply("d5")["state"], "ended")
        self.reader.send({"t": "wat", "id": "d6"})
        reply = self.expect_reply("d6")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "unknown_frame")
        self.reader.send({"t": "ping", "id": "d7"})  # the stream stays up
        self.assertTrue(self.expect_reply("d7")["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_send_drops_injected_caller_keys(self):
        self.assertTrue(self.bind()["ok"])
        self.reader.send({"t": "send", "id": "d2", "name": "alpha", "to": "operator",
                          "kind": "message", "subject": "s", "body": "b", "op_id": "o1",
                          "from": "operator", "from_kind": "operator", "hop": 9, "epoch": 7,
                          "sender": "operator", "creator": "x", "caller": "y"})
        reply = self.expect_reply("d2")
        self.assertTrue(reply["ok"], reply)
        row = self.mail_row(reply["id"])
        self.assertEqual((row["sender"], row["from_kind"], row["hop"], row["recipient"]),
                         ("alpha@ws-mac-1", "session", 1, "operator"))
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_missing_fields_are_bad_frame(self):
        self.assertTrue(self.bind()["ok"])
        self.reader.send({"t": "send", "id": "d2", "name": "alpha", "to": "operator",
                          "kind": "message", "subject": "s", "op_id": "o1"})  # no body
        reply = self.expect_reply("d2")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad_frame")
        self.reader.send({"t": "bind", "id": "d3", "name": "beta"})  # no cli_session
        self.assertEqual(self.expect_reply("d3")["error"], "bad_frame")
        self.reader.send({"t": "ping", "id": "d4"})  # the stream stays up
        self.assertTrue(self.expect_reply("d4")["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_list_answers_the_sessions_peers(self):
        """Default peers: operator, an agent and another live session, sorted by address; neither
        the caller itself nor an ended session is a peer."""
        self.assertTrue(self.bind()["ok"])  # alpha, the caller
        self.reader.send({"t": "bind", "id": "d2", "name": "beta", "cli_session": "cli-2"})
        self.assertTrue(self.expect_reply("d2")["ok"])
        self.reader.send({"t": "bind", "id": "d3", "name": "gamma", "cli_session": "cli-3"})
        self.assertTrue(self.expect_reply("d3")["ok"])
        self.reader.send({"t": "unbind", "id": "d4", "name": "gamma"})
        self.assertTrue(self.expect_reply("d4")["ok"])  # ended: never listed
        register(self.hub, "claude")  # a resident agent is a default peer
        self.reader.send({"t": "list", "id": "d5", "name": "alpha"})
        reply = self.expect_reply("d5")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([p["address"] for p in reply["peers"]],
                         ["beta@ws-mac-1", "claude@node-1", "operator"])
        by_address = {p["address"]: p for p in reply["peers"]}
        self.assertEqual(by_address["operator"], {"address": "operator", "kind": "operator",
                                                  "state": "operator", "last_seen": None})
        agent = by_address["claude@node-1"]
        self.assertEqual((agent["kind"], agent["state"]), ("agent", "down"))
        self.assertIsInstance(agent["last_seen"], str)  # the agents.modified stamp
        beta = by_address["beta@ws-mac-1"]
        self.assertEqual((beta["kind"], beta["state"], beta["away"]), ("session", "linked", False))
        self.assertIsInstance(beta["last_seen"], str)  # linked: the bind stamp, no turn yet
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_list_with_stored_peers_keeps_the_unknown_and_adds_operator(self):
        self.reader.send({"t": "bind", "id": "d1", "name": "alpha", "cli_session": "cli-1",
                          "peers": ["ghost@ws-mac-1"]})
        self.assertTrue(self.expect_reply("d1")["ok"])
        self.reader.send({"t": "list", "id": "d2", "name": "alpha"})
        reply = self.expect_reply("d2")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["peers"], [
            {"address": "ghost@ws-mac-1", "kind": "unknown", "state": "unknown",
             "last_seen": None},
            {"address": "operator", "kind": "operator", "state": "operator", "last_seen": None},
        ])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_list_refuses_an_unbound_name(self):
        self.reader.send({"t": "list", "id": "d1", "name": "ghost"})
        reply = self.expect_reply("d1")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "not_bound")
        self.reader.send({"t": "ping", "id": "d2"})  # the stream stays up
        self.assertTrue(self.expect_reply("d2")["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_list_on_a_stale_lease_answers_superseded_and_ends_the_stream(self):
        self.assertTrue(self.bind()["ok"])
        self.hub.link_claim("ws-mac-1")  # a new stream took the link over
        self.reader.send({"t": "list", "id": "d2", "name": "alpha"})
        reply = self.expect_reply("d2")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "superseded")
        self.assertEqual(self.writer.expect("bye")["reason"], "superseded")
        self.assertEqual(self.join(), 0)  # a superseded end is a normal one

    def test_list_without_a_name_is_a_bad_frame(self):
        self.assertTrue(self.bind()["ok"])
        self.reader.send({"t": "list", "id": "d2"})
        reply = self.expect_reply("d2")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad_frame")
        self.reader.send({"t": "ping", "id": "d3"})  # the stream stays up
        self.assertTrue(self.expect_reply("d3")["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)


class FakeReviewAPI:
    """A recording stand-in for session_link.ReviewAPI: the submit bodies and get ids it saw,
    with scripted answers (a dict per call, in order; an Exception is raised as given)."""

    def __init__(self, script=None):
        self.submits: list = []
        self.gets: list = []
        self.script = list(script or [])

    def _next(self) -> dict:
        answer = self.script.pop(0) if self.script else {"id": "review_20261124_090101_ab12cd",
                                                         "status": "running"}
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def submit(self, body):
        self.submits.append(dict(body))
        return self._next()

    def get(self, review_id):
        self.gets.append(review_id)
        return self._next()


def completed_view(review_id="review_20261124_090101_ab12cd", workstation="ws-mac-1",
                   findings=None):
    """One GET view of a finished session-started review, as the hub's API answers it."""
    findings = findings if findings is not None else [
        {"path": "cli/llm-agent/x.py", "line": 9, "quote": "return items[9]",
         "claim": "off-by-one index", "scenario": "a 9-item list raises IndexError",
         "severity": "high", "verified": True, "reason": ""},
        {"path": "apps/agent/y.go", "line": 44, "quote": "", "claim": "unclosed body",
         "scenario": "a broken pipe hangs the handler", "severity": "low", "verified": True,
         "reason": ""},
    ]
    return {"id": review_id, "status": "completed", "label": "REVIEW",
            "note": "advisory, no Judge",
            "result": {"state": "done", "summary": "2 findings (1 high, 0 medium, 1 low), 0 notes",
                       "findings": findings, "notes": []},
            "request": {"project": "server-guardian",
                        "source": {"kind": "ref", "ref": "feature/retry", "base": "a" * 40,
                                   "head": "b" * 40, "base_ref": ""},
                        "started_by": f"session:{workstation}"}}


class ReviewFrames(LinkTestCase):
    """V2d: the review ops on the wire. The hub side re-validates every field (the workstation is
    untrusted), stamps started_by from the link's own workstation, and answers another
    workstation's review exactly as an unknown id."""

    def setUp(self):
        super().setUp()
        self.api = FakeReviewAPI()
        self.start(review_api=self.api)
        self.handshake()

    def review_submit(self, rid="d2", **fields):
        frame = {"t": "review_submit", "id": rid, "name": "alpha"}
        frame.update(fields)
        self.reader.send(frame)
        return self.expect_reply(rid)

    def review_get(self, rid="d3", **fields):
        frame = {"t": "review_get", "id": rid, "name": "alpha"}
        frame.update(fields)
        self.reader.send(frame)
        return self.expect_reply(rid)

    def test_submit_posts_with_the_links_own_workstation_and_answers_the_hub_view(self):
        self.assertTrue(self.bind()["ok"])
        reply = self.review_submit(project="server-guardian", ref="feature/retry", base="main",
                                   expect_sha="a" * 40, provider="claude")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["id"], "review_20261124_090101_ab12cd")
        self.assertEqual(reply["status"], "running")
        self.assertEqual(self.api.submits, [{
            "project": "server-guardian", "ref": "feature/retry", "base": "main",
            "expect_sha": "a" * 40, "provider": "claude",
            "started_by": "session:ws-mac-1"}])   # the link line's workstation, never a client value
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_submit_refuses_started_by_and_every_other_extra_field(self):
        self.assertTrue(self.bind()["ok"])
        for extra in ({"started_by": "session:ws-ghost-1"}, {"from": "operator"},
                      {"patch": "diff --git"}, {"range": "main..head"}, {"action": 7},
                      {"caller": "operator"}):
            with self.subTest(extra=extra):
                reply = self.review_submit(project="server-guardian", ref="main", **extra)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], "bad_frame")
        self.assertEqual(self.api.submits, [])    # nothing crossed
        self.reader.send({"t": "ping", "id": "d9"})  # the stream stays up
        self.assertTrue(self.expect_reply("d9")["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_submit_refuses_invalid_fields_even_though_the_workstation_sent_them(self):
        self.assertTrue(self.bind()["ok"])
        for fields in ({"project": "server-guardian", "ref": "-head"},
                       {"project": "server-guardian", "ref": "bad ref"},
                       {"project": "server-guardian", "ref": "feature/retry", "base": "--glob"},
                       {"project": "server-guardian", "ref": "feature/retry",
                        "expect_sha": "A" * 40},
                       {"project": "server-guardian", "ref": "feature/retry",
                        "expect_sha": "a" * 39},
                       {"project": "Server-Guardian", "ref": "main"},
                       {"project": "server-guardian", "ref": "feature/retry",
                        "provider": "not-a-provider"},
                       {"project": "server-guardian"},           # no ref
                       {"ref": "main"}):                          # no project
            with self.subTest(fields=fields):
                reply = self.review_submit(**fields)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], "bad_frame")
        self.assertEqual(self.api.submits, [])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_submit_accepts_an_empty_provider_and_sends_no_provider_key(self):
        self.assertTrue(self.bind()["ok"])
        reply = self.review_submit(project="server-guardian", ref="main", provider="")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.api.submits, [{"project": "server-guardian", "ref": "main",
                                             "started_by": "session:ws-mac-1"}])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_submit_from_an_unbound_name_is_not_bound(self):
        self.assertTrue(self.bind()["ok"])
        reply = self.review_submit(project="server-guardian", ref="main")
        self.assertTrue(reply["ok"], reply)
        self.reader.send({"t": "review_submit", "id": "d5", "name": "ghost",
                          "project": "server-guardian", "ref": "main"})
        reply = self.expect_reply("d5")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "not_bound")
        self.assertEqual(len(self.api.submits), 1)
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_submit_on_a_stale_lease_answers_superseded_and_ends_the_stream(self):
        self.assertTrue(self.bind()["ok"])
        self.hub.link_claim("ws-mac-1")  # a new stream took the link over
        reply = self.review_submit(project="server-guardian", ref="main")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "superseded")
        self.assertEqual(self.api.submits, [])   # nothing was started
        self.assertEqual(self.writer.expect("bye")["reason"], "superseded")
        self.assertEqual(self.join(), 0)

    def test_submit_maps_http_errors_to_a_plain_error_reply(self):
        self.assertTrue(self.bind()["ok"])
        self.api.script.append(session_link.ReviewAPIError("invalid project", status=400))
        reply = self.review_submit(project="server-guardian", ref="main")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "hub_error")
        self.assertEqual(reply["detail"], "invalid project")
        self.assertNotIn("Bearer", json.dumps(reply))   # no token, no headers
        self.api.script.append(session_link.ReviewAPIError("cannot reach the hub's review API: boom"))
        reply = self.review_submit(rid="d4", project="server-guardian", ref="main")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "hub_error")
        self.assertIn("cannot reach", reply["detail"])
        self.reader.send({"t": "ping", "id": "d8"})     # the stream stays up
        self.assertTrue(self.expect_reply("d8")["ok"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_get_answers_a_completed_review_trimmed(self):
        self.assertTrue(self.bind()["ok"])
        self.api.script.append(completed_view())
        reply = self.review_get(review_id="review_20261124_090101_ab12cd")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["review"], {
            "id": "review_20261124_090101_ab12cd", "status": "completed", "label": "REVIEW",
            "note": "advisory, no Judge", "state": "done",
            "summary": "2 findings (1 high, 0 medium, 1 low), 0 notes",
            "counts": {"high": 1, "medium": 0, "low": 1}, "truncated": False,
            "findings": [
                {"severity": "high", "title": "off-by-one index", "file": "cli/llm-agent/x.py",
                 "line": 9, "detail": "a 9-item list raises IndexError"},
                {"severity": "low", "title": "unclosed body", "file": "apps/agent/y.go",
                 "line": 44, "detail": "a broken pipe hangs the handler"},
            ]})
        self.assertEqual(self.api.gets, ["review_20261124_090101_ab12cd"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_get_answers_a_running_review_without_findings(self):
        self.assertTrue(self.bind()["ok"])
        self.api.script.append({"id": "review_20261124_090101_ab12cd", "status": "running",
                                "label": "REVIEW", "note": "advisory, no Judge",
                                "request": {"started_by": "session:ws-mac-1"}})
        reply = self.review_get(review_id="review_20261124_090101_ab12cd")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["review"]["status"], "running")
        self.assertNotIn("findings", reply["review"])
        self.assertNotIn("counts", reply["review"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_get_answers_a_foreign_review_exactly_as_an_unknown_id(self):
        self.assertTrue(self.bind()["ok"])
        # another workstation's review, a record without request.json, and a real 404
        self.api.script.append(completed_view(workstation="ws-other-1"))
        self.api.script.append({"id": "review_20261124_090102_cd34ef", "status": "completed"})
        self.api.script.append(session_link.ReviewAPIError("review not found", status=404))
        first = self.review_get(review_id="review_20261124_090101_ab12cd")
        second = self.review_get(rid="d4", review_id="review_20261124_090102_cd34ef")
        third = self.review_get(rid="d5", review_id="review_20261124_090103_ef56a9")

        def shape(frame):
            return {key: value for key, value in frame.items() if key not in ("t", "re")}

        self.assertFalse(first["ok"])
        self.assertEqual(first["error"], "not_found")
        self.assertEqual(first["detail"], "review not found")
        self.assertEqual(shape(second), shape(first))   # no request.json: not ours, so unknown
        self.assertEqual(shape(third), shape(first))    # the hub's own 404 answers the same
        self.assertEqual(self.api.gets, ["review_20261124_090101_ab12cd",
                                         "review_20261124_090102_cd34ef",
                                         "review_20261124_090103_ef56a9"])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_get_refuses_a_bad_id_and_extra_fields(self):
        self.assertTrue(self.bind()["ok"])
        for fields in ({"review_id": "not-a-review-id"}, {"review_id": "../etc/passwd"},
                       {"review_id": "review_20261124_090101_ab12cd", "wait_sec": 5},
                       {"review_id": "review_20261124_090101_ab12cd", "caller": "operator"}):
            with self.subTest(fields=fields):
                reply = self.review_get(**fields)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], "bad_frame")
        self.assertEqual(self.api.gets, [])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_get_from_an_unbound_name_is_not_bound(self):
        self.assertTrue(self.bind()["ok"])
        self.reader.send({"t": "review_get", "id": "d5", "name": "ghost",
                          "review_id": "review_20261124_090101_ab12cd"})
        reply = self.expect_reply("d5")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "not_bound")
        self.assertEqual(self.api.gets, [])
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_get_caps_the_trimmed_result_at_64_kib(self):
        self.assertTrue(self.bind()["ok"])
        fat = {"path": "big/file.go", "line": 1, "quote": "", "claim": "c" * 800,
               "scenario": "s" * 800, "severity": "medium", "verified": True, "reason": ""}
        self.api.script.append(completed_view(findings=[dict(fat) for _ in range(300)]))
        reply = self.review_get(review_id="review_20261124_090101_ab12cd")
        self.assertTrue(reply["ok"], reply)
        review = reply["review"]
        self.assertTrue(review["truncated"])
        self.assertEqual(review["counts"], {"high": 0, "medium": 300, "low": 0})  # real totals
        self.assertLess(len(review["findings"]), 300)
        self.assertLessEqual(len(json.dumps(review).encode("utf-8")),
                             session_link.REVIEW_RESULT_MAX_BYTES)
        self.assertGreater(len(review["findings"]), 10)    # the cap drops findings, not all
        self.reader.eof()
        self.assertEqual(self.join(), 0)


class Delivery(LinkTestCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.handshake()
        self.assertTrue(self.bind()["ok"])

    def seed_mail(self):
        mail = self.hub.send("operator", "alpha@ws-mac-1", "message", "s", "b", op_id="o1")
        self.assertTrue(mail["ok"], mail)
        return mail["id"]

    def deliveries(self):
        return [f for f in self.writer.frames() if f.get("t") == "deliver"]

    def test_queued_operator_mail_delivered_on_ok_reply(self):
        mail_id = self.seed_mail()
        self.clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
        deliver = self.writer.expect("deliver")
        self.assertEqual(deliver["mail"]["id"], mail_id)
        self.assertEqual(deliver["mail"]["from"], "operator")
        self.assertEqual(deliver["mail"]["to"], "alpha@ws-mac-1")
        self.reader.send({"t": "reply", "re": deliver["id"], "ok": True})
        self.wait_for(lambda: self.mail_row(mail_id)["state"] == "delivered" or None)
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_error_reply_leaves_mail_queued_and_resent(self):
        mail_id = self.seed_mail()
        self.clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
        first = self.writer.expect("deliver")
        self.reader.send({"t": "reply", "re": first["id"], "ok": False, "error": "spool_full"})
        self.assertEqual(self.mail_row(mail_id)["state"], "queued")
        self.clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
        self.wait_for(lambda: len(self.deliveries()) >= 2 or None)
        second = self.deliveries()[1]
        self.assertEqual(second["mail"]["id"], mail_id)  # the same mail, a new request id
        self.assertNotEqual(second["id"], first["id"])
        self.reader.send({"t": "reply", "re": second["id"], "ok": True})
        self.wait_for(lambda: self.mail_row(mail_id)["state"] == "delivered" or None)
        self.reader.eof()
        self.assertEqual(self.join(), 0)

    def test_hub_pings_every_interval(self):
        self.clock.advance(session_link.LinkSession.PING_INTERVAL + 1)
        ping = self.writer.expect("ping")
        self.assertTrue(ping["id"].startswith("h"))
        self.reader.send({"t": "reply", "re": ping["id"], "ok": True})
        self.reader.eof()
        self.assertEqual(self.join(), 0)


class StreamEnds(LinkTestCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.handshake()
        self.assertTrue(self.bind()["ok"])

    def test_liveness_timeout_ends_with_mail_queued(self):
        mail_id = self.hub.send("operator", "alpha@ws-mac-1", "message", "s", "b", op_id="o1")["id"]
        self.clock.advance(session_link.LinkSession.LIVENESS_TIMEOUT + 1)
        self.assertEqual(self.writer.expect("bye")["reason"], "liveness")
        self.assertEqual(self.join(), 1)
        row = self.link_row()
        self.assertEqual((row["state"], row["end_reason"]), ("down", "liveness"))
        self.assertEqual(self.mail_row(mail_id)["state"], "queued")

    def test_workstation_removal_ends_stream(self):
        session_registry.declaration_path().write_text("[]", encoding="utf-8")
        self.clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
        self.assertEqual(self.writer.expect("bye")["reason"], "removed")
        self.assertEqual(self.join(), 0)
        row = self.link_row()
        self.assertEqual((row["state"], row["end_reason"]), ("down", "removed"))

    def test_kill_switch_ends_stream(self):
        with patch.dict(os.environ, {"GUARDIAN_SESSIONS": "off"}):
            self.clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
            self.assertEqual(self.writer.expect("bye")["reason"], "sessions_off")
            self.assertEqual(self.join(), 0)


if __name__ == "__main__":
    unittest.main()

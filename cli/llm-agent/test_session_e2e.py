"""A simulated end-to-end test of the S2 exit condition (docs/design/operator-sessions.md).

Real: two session_daemon.Daemon instances (the workstations ws-mac-1 and ws-wsl-1, separate homes
under /tmp), their whole session_uplink stack (Uplink, Dialer, LinkClient, Spool), the hub's
session_link.LinkSession and mail_store.MailStore under testenv.isolate_data_dir() with both
workstations registered the way test_session_link.py registers one, the local Unix sockets, and
session_local.call as the hook and MCP client.

Simulated: ssh (each Dialer's spawn starts the hub LinkSession for that workstation in a thread
over two os.pipe pairs and returns a Popen-like object, which is what the hub sshd's forced
command does to the daemon's ssh child); the CLI sessions (the test sends the hook and MCP frames
the CLI would cause); tmux (a FakeWaker records try_wake calls); and time (every clock is injected
and advanced by hand, and the Dialer's backoff sleep is shortened, so no test sleeps more than
0.3 s of real time and the whole file stays well under 30 s).

Scenarios: 1 mail between two sessions through the hub (send, unread count, inbox, reply_to);
2 a killed stream (a killed ssh child) is redialled and the mail queued meanwhile arrives exactly
once; 3 a stream that goes silent without closing is ended by the liveness heartbeat on BOTH ends
and then redialled; 4 operator mail wakes an idle tmux session exactly once and peer mail does not;
5 a session submits a quick review over the whole stack and reads the trimmed result (the hub's
/api/reviews is a fake injected into the hub side of the link, the only simulated piece of it).
"""

import base64
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mail_store
import session_daemon
import session_link
import session_local
import session_registry
import session_uplink
import session_wake
import testenv

TMUX_SOCKET = "/tmp/tmux-1000/default"
PANE = "%3"
POLL = session_link.LinkSession.POLL_INTERVAL

CONFIG_MAC = {"workstation": "ws-mac-1", "hub_address": "100.64.0.1", "hub_name": "",
              "hub_alias": "site-a-arm1", "hub_user": "guardian", "ssh_port": 22}
CONFIG_WSL = {"workstation": "ws-wsl-1", "hub_address": "100.64.0.1", "hub_name": "",
              "hub_alias": "site-a-arm1", "hub_user": "guardian", "ssh_port": 22}

# Public keys built here (the wire format of an ed25519 key), as in test_session_link.py; each
# workstation gets its own, because the registry refuses a key used by two workstations.
def _synthetic_key(fill: bytes) -> str:
    return "ssh-ed25519 " + base64.b64encode(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + fill * 32).decode("ascii")


KEYS = {"ws-mac-1": _synthetic_key(b"k"), "ws-wsl-1": _synthetic_key(b"w")}


def register_workstations() -> None:
    """Writes both workstations into the hub registry, as the Settings page would."""
    path = session_registry.declaration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"id": ws, "address": f"100.64.0.{5 + i}", "key": KEYS[ws], "node": None,
                "added": "2026-11-20T10:00:00Z"}
               for i, ws in enumerate(("ws-mac-1", "ws-wsl-1"))]
    path.write_text(json.dumps(entries), encoding="utf-8")


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


class FakeMonotonic:
    """A fake monotonic clock the tests advance by hand (uplink and hub sessions read only it)."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeClock:
    """A fake wall clock the daemon reads (idle, wake and busy-cap decisions)."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeWaker:
    """Records every WakeTarget and answers try_wake with a successful wake."""

    def __init__(self):
        self.targets = []

    def try_wake(self, target):
        self.targets.append(target)
        return True, "woken"


class SilentWriter:
    """A pipe writer that can turn into a black hole: in silent mode writes are recorded and
    discarded and close() no longer reaches the pipe, so the peer sees neither frames nor EOF —
    a stream that stopped passing frames without closing. The records are how the tests read
    what an end sent into the black hole."""

    def __init__(self, stream):
        self.stream = stream
        self.silent = False
        self.written = []
        self._lock = threading.Lock()

    def write(self, data):
        with self._lock:
            self.written.append(bytes(data))
        if not self.silent:
            self.stream.write(data)

    def flush(self):
        if not self.silent:
            self.stream.flush()

    def close(self):
        if self.silent:
            return            # the FIN is swallowed with the frames
        self.close_hard()

    def close_hard(self):
        """Closes the underlying pipe whatever the mode (cleanup, or a killed ssh child)."""
        try:
            self.stream.close()
        except (OSError, ValueError):
            pass

    def frames(self):
        with self._lock:
            raw = b"".join(self.written)
        out = []
        for line in raw.splitlines():
            try:
                frame = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(frame, dict):
                out.append(frame)
        return out


def _close_any(obj) -> None:
    closer = getattr(obj, "close_hard", None) or getattr(obj, "close", None)
    try:
        if closer is not None:
            closer()
    except (OSError, ValueError):
        pass


class HubSide:
    """One workstation's stand-in for the hub sshd's forced command: every dial starts a real
    session_link.LinkSession for that workstation in its own thread over two fresh os.pipe pairs
    and returns the Popen-like object the real Dialer/SshChild expect. The two stream directions
    go through SilentWriters, so a test can silence or kill the current stream. `review_api` is
    the hub's /api/reviews stand-in the review ops run against (a fake; the real one answers on
    127.0.0.1:9870 with the hub's AGENT_TOKEN)."""

    def __init__(self, workstation, review_api=None):
        self.workstation = workstation
        self.review_api = review_api
        self.clock = FakeMonotonic(5000.0)   # every hub session of this workstation shares it
        self.gate = threading.Event()
        self.gate.set()
        self.streams = []
        self.calls = []

    def __call__(self, argv, *, stdin=None, stdout=None, stderr=None, env=None):
        self.calls.append(list(argv))
        a_read, a_write = os.pipe()   # the daemon writes, the hub reads
        b_read, b_write = os.pipe()   # the hub writes, the daemon reads
        hub_reader = os.fdopen(a_read, "rb")
        hub_writer = SilentWriter(os.fdopen(b_write, "wb"))
        session = session_link.LinkSession(self.workstation, hub_reader, hub_writer,
                                           store=mail_store.MailStore(), clock=self.clock,
                                           wake=0.05, review_api=self.review_api)
        holder = []
        thread = threading.Thread(target=lambda: holder.append(session.run()), daemon=True,
                                  name=f"e2e-hub-{self.workstation}")
        thread.start()
        daemon_writer = SilentWriter(os.fdopen(a_write, "wb"))
        daemon_reader = os.fdopen(b_read, "rb")
        stream = SimpleNamespace(session=session, thread=thread, result=holder,
                                 hub_writer=hub_writer, daemon_writer=daemon_writer,
                                 daemon_reader=daemon_reader, hub_reader=hub_reader)
        self.streams.append(stream)
        proc = SimpleNamespace(stdin=daemon_writer, stdout=daemon_reader,
                               stderr=io.BytesIO(b""), argv=list(argv), killed=False,
                               wait=lambda timeout=None: (thread.join(timeout), 0)[1],
                               kill=lambda: None)
        return proc

    def silence(self) -> None:
        """The newest stream stops passing frames in both directions, without closing."""
        stream = self.streams[-1]
        stream.hub_writer.silent = True
        stream.daemon_writer.silent = True

    def kill_stream(self, stream) -> None:
        """Closes both pipes: EOF for each reader, EPIPE for each writer — what a killed ssh
        child looks like to both ends. The write ends go first so the blocked reader threads
        wake on EOF; the read ends are closed off the test thread, because closing a buffered
        reader waits for the lock a blocked read() holds."""
        stream.daemon_writer.close_hard()
        stream.hub_writer.close_hard()
        for reader in (stream.daemon_reader, stream.hub_reader):
            threading.Thread(target=_close_any, args=(reader,), daemon=True,
                             name="e2e-close-reader").start()

    def close_all(self) -> None:
        for stream in self.streams:
            self.kill_stream(stream)


class Workstation:
    """One workstation's whole real stack: a Daemon over a real Uplink whose Dialer dials the
    HubSide bridge instead of ssh. The daemon's wall clock and the uplink's monotonic clock are
    injected separately, as they are in production; the Dialer's sleep advances the monotonic
    clock (so a backoff ends without real waiting) and holds at the bridge's gate while a test
    keeps it closed."""

    def __init__(self, config, waker, review_api=None):
        self.config = config
        tmp = tempfile.mkdtemp(prefix=f"e2e-{config['workstation']}-", dir="/tmp")
        self.home = Path(tmp)
        self.paths = session_local.SessionPaths(self.home)
        self.paths.ensure_dirs()
        self.paths.config.write_text(json.dumps(config), encoding="utf-8")
        self.clock = FakeClock()
        self.mono = FakeMonotonic(1000.0)
        self.bridge = HubSide(config["workstation"], review_api=review_api)
        self.spool = session_uplink.Spool(self.paths)
        dialer = session_uplink.Dialer(config, self.paths, spawn=self.bridge,
                                       clock=self.mono, sleep=self._sleep, rng=lambda: 0.5)
        self.uplink = session_uplink.Uplink(config, self.paths, self.spool, dialer=dialer,
                                            clock=self.mono, wake=0.05)
        self.daemon = session_daemon.Daemon(self.home, uplink=self.uplink, waker=waker,
                                            clock=self.clock)
        # The daemon wires a FAKE uplink through its public on_up/on_deliver attributes; the real
        # Uplink keeps them private (_on_up, _on_deliver), so wire the same two hook points by
        # hand here, before start() opens the first stream (the constructor copies on_up, so the
        # list must be the one the uplink holds).
        self.uplink._on_up.append(self.daemon._when_link_up)
        self.uplink._on_deliver = self.daemon._on_deliver
        self.waker = waker
        self.daemon.start()

    def _sleep(self, seconds):
        """The Dialer's injected backoff sleep: advances the injected clock so the backoff loop
        ends, sleeps a token real moment for the thread scheduler, and holds the redial at the
        gate while a scenario keeps it closed."""
        self.mono.advance(seconds)
        time.sleep(0.01)
        self.bridge.gate.wait(timeout=5.0)

    # -- the client side the CLI's hooks and session MCP would drive ----------------------------

    def register(self, cli, name=None, pane=None, tmux_socket=None):
        frame = {"op": "register", "cli_session": cli, "cli_pid": os.getpid(),
                 "tmux_socket": tmux_socket, "tmux_pane": pane, "source": "startup"}
        if name is not None:
            frame["name"] = name
        return session_local.call(self.paths.control_socket, frame,
                                  session_local.HOOK_CLIENT_TIMEOUT)

    def hook(self, op, cli, **fields):
        return session_local.call(self.paths.control_socket,
                                  {"op": op, "cli_session": cli, **fields},
                                  session_local.HOOK_CLIENT_TIMEOUT)

    def mcp(self, op, cli, **fields):
        return session_local.call(self.paths.session_socket(cli), {"op": op, **fields},
                                  session_local.LOCAL_CLIENT_TIMEOUT)

    def bound(self, cli):
        return (self.daemon._record(cli) or {}).get("bound") or None

    def wait_up(self):
        wait_for(lambda: self.uplink.state == "up" or None)

    def unread(self, name):
        return self.daemon.spool.unread(name)


class FakeReviewAPI:
    """The hub's /api/reviews as a fake: records the submit bodies and get ids, answers from a
    script (a dict per call in order; an Exception is raised as given)."""

    def __init__(self, script=None):
        self.submits = []
        self.gets = []
        self.script = list(script or [])

    def _next(self):
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


class SessionEndToEndTests(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        keys = patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE":
                                       str(Path(os.environ["GUARDIAN_DATA_DIR"])
                                           / "authorized_keys2")})
        keys.start()
        self.addCleanup(keys.stop)
        register_workstations()
        self.hub = mail_store.MailStore()   # the tests write operator mail through it
        self.review_api = FakeReviewAPI()
        self.mac = Workstation(CONFIG_MAC, FakeWaker(), review_api=self.review_api)
        self.wsl = Workstation(CONFIG_WSL, FakeWaker())
        self.addCleanup(self._teardown_world)

    def _teardown_world(self):
        for ws in (self.mac, self.wsl):
            ws.bridge.gate.set()
            ws.daemon.stop()
            ws.bridge.close_all()
        for ws in (self.mac, self.wsl):
            for stream in ws.bridge.streams:
                stream.thread.join(timeout=5.0)
        self.hub.close()
        for ws in (self.mac, self.wsl):
            shutil.rmtree(ws.home, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------------------------

    def mail_state(self, mail_id):
        return next((m["state"] for m in self.hub.list_mail(limit=100) if m["id"] == mail_id),
                    None)

    def hub_state(self, address):
        return next((r["state"] for r in self.hub.session_rows() if r["address"] == address),
                    None)

    def link_both(self, alpha="alpha", beta="beta"):
        """Both workstations up, one named session each, both bound on the hub."""
        self.mac.wait_up()
        self.wsl.wait_up()
        reply_a = self.mac.register(cli="cli-a", name=alpha)
        self.assertTrue(reply_a["ok"], reply_a)
        reply_b = self.wsl.register(cli="cli-b", name=beta)
        self.assertTrue(reply_b["ok"], reply_b)
        wait_for(lambda: (self.mac.bound("cli-a") and self.wsl.bound("cli-b")) or None)

    def deliver_to_wsl(self):
        """One hub poll of the wsl link: queued mail leaves towards the workstation."""
        self.wsl.bridge.clock.advance(POLL + 0.1)

    def deliver_to_mac(self):
        self.mac.bridge.clock.advance(POLL + 0.1)

    # -- scenario 1 ------------------------------------------------------------------------------

    def test_1_two_sessions_exchange_mail_through_the_hub(self):
        self.link_both()
        sent = self.mac.mcp("send", cli="cli-a", to="beta@ws-wsl-1", kind="message",
                            subject="deploy check", body="is the release out?")
        self.assertTrue(sent["ok"], sent)
        self.deliver_to_wsl()
        wait_for(lambda: self.wsl.daemon.spool.unread_count("beta") == 1 or None)
        # B's UserPromptSubmit reports the unread count and nothing else (rule 8)
        prompt = self.wsl.hook("prompt_submit", cli="cli-b")
        self.assertEqual(prompt, session_local.ok_reply(unread=1))
        # B's inbox returns the mail, marks it retrieved and acks it on the hub
        inbox = self.wsl.mcp("inbox", cli="cli-b")
        self.assertTrue(inbox["ok"], inbox)
        self.assertEqual([m["id"] for m in inbox["messages"]], [sent["id"]])
        self.assertEqual(inbox["messages"][0]["from"], "alpha@ws-mac-1")
        wait_for(lambda: self.mail_state(sent["id"]) == "acked" or None)
        # B replies in the thread; A reads the reply from its own spool
        answer = self.wsl.mcp("send", cli="cli-b", to="alpha@ws-mac-1", kind="answer",
                              subject="re: deploy check", body="yes, out since 12:40Z",
                              reply_to=sent["id"])
        self.assertTrue(answer["ok"], answer)
        self.deliver_to_mac()
        wait_for(lambda: self.mac.daemon.spool.unread_count("alpha") == 1 or None)
        got = self.mac.mcp("inbox", cli="cli-a")
        self.assertEqual([m["id"] for m in got["messages"]], [answer["id"]])
        self.assertEqual(got["messages"][0]["reply_to"], sent["id"])
        self.assertEqual(got["messages"][0]["from"], "beta@ws-wsl-1")

    # -- scenario 2 ------------------------------------------------------------------------------

    def test_2_killed_stream_redials_and_queued_mail_arrives_once(self):
        self.link_both()
        first = self.mac.mcp("send", cli="cli-a", to="beta@ws-wsl-1", kind="message",
                             subject="one", body="before the stream dies")
        self.assertTrue(first["ok"], first)
        self.deliver_to_wsl()
        wait_for(lambda: self.wsl.daemon.spool.unread_count("beta") == 1 or None)
        self.assertTrue(self.wsl.mcp("inbox", cli="cli-b")["ok"])   # read: unread is 0 again
        # kill ws-wsl-1's stream (a killed ssh child), holding the redial at the gate
        dead = self.wsl.bridge.streams[-1]
        self.wsl.bridge.gate.clear()
        self.wsl.bridge.kill_stream(dead)
        dead.thread.join(timeout=5.0)
        self.assertFalse(dead.thread.is_alive())
        self.assertEqual(dead.session.close_reason, "stream_end")
        # while the link is down, A's mail queues on the hub
        second = self.mac.mcp("send", cli="cli-a", to="beta@ws-wsl-1", kind="message",
                              subject="two", body="while the link is down")
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.mail_state(second["id"]), "queued")
        # the redial: a new lease, a re-bind, and the queued mail arrives exactly once
        self.wsl.bridge.gate.set()
        wait_for(lambda: len(self.wsl.bridge.streams) >= 2 or None)
        wait_for(lambda: self.wsl.uplink.state == "up" or None)
        fresh = self.wsl.bridge.streams[-1]
        self.assertGreaterEqual(fresh.session.lease, 2)
        wait_for(lambda: next((True for f in fresh.daemon_writer.frames()
                               if f.get("t") == "bind" and f.get("name") == "beta"), None))
        # the link-up poll ran before the re-bind (the new lease had marked beta offline), so
        # the mail leaves at the first poll after the hub has the bind
        wait_for(lambda: self.hub_state("beta@ws-wsl-1") not in (None, "offline") or None)
        self.deliver_to_wsl()
        wait_for(lambda: self.wsl.daemon.spool.unread_count("beta") == 1 or None)
        self.assertEqual([m["id"] for m in self.wsl.unread("beta")], [second["id"]])
        wait_for(lambda: self.mail_state(second["id"]) == "delivered" or None)

    # -- scenario 3 ------------------------------------------------------------------------------

    def test_3_silent_stream_ends_by_liveness_on_both_ends_and_redials(self):
        self.link_both()
        silent = self.mac.bridge.streams[-1]
        self.mac.bridge.gate.clear()      # hold the redial until both ends have ended
        self.mac.bridge.silence()
        # daemon side: 90 s without a frame (injected clock) ends the LinkClient with liveness
        self.mac.mono.advance(session_uplink.LinkClient.LIVENESS_TIMEOUT + 1)
        wait_for(lambda: next((True for f in silent.daemon_writer.frames()
                               if f.get("t") == "bye" and f.get("reason") == "liveness"), None))
        # hub side: the same heartbeat on LinkSession, then the stream is redialled
        self.mac.bridge.clock.advance(session_link.LinkSession.LIVENESS_TIMEOUT + 1)
        silent.thread.join(timeout=5.0)
        self.assertFalse(silent.thread.is_alive())
        self.assertEqual(silent.session.close_reason, "liveness")
        self.assertEqual(silent.result, [1])
        self.assertTrue(any(f.get("t") == "bye" and f.get("reason") == "liveness"
                            for f in silent.hub_writer.frames()))
        self.mac.bridge.gate.set()
        wait_for(lambda: len(self.mac.bridge.streams) >= 2 or None)
        wait_for(lambda: self.mac.uplink.state == "up" or None)
        self.assertGreaterEqual(self.mac.bridge.streams[-1].session.lease, 2)
        self.assertEqual(self.mac.mcp("status", cli="cli-a")["link"], "up")

    # -- scenario 4 ------------------------------------------------------------------------------

    def test_4_operator_mail_wakes_an_idle_tmux_session_peer_mail_does_not(self):
        self.link_both()
        # B re-registers with its tmux pane recorded, then goes idle on the daemon's clock
        reply = self.wsl.register(cli="cli-b", name="beta", pane=PANE, tmux_socket=TMUX_SOCKET)
        self.assertTrue(reply["ok"], reply)
        wait_for(lambda: self.wsl.bound("cli-b") or None)
        self.wsl.clock.advance(session_local.IDLE_AFTER + 1)
        # operator mail, inserted through the hub store, is delivered and wakes B exactly once
        mail = self.hub.send("operator", "beta@ws-wsl-1", "message", "status?", "please report",
                             op_id="wake-1")
        self.assertTrue(mail["ok"], mail)
        self.deliver_to_wsl()
        wait_for(lambda: len(self.wsl.waker.targets) == 1 or None)
        self.assertEqual(self.wsl.waker.targets,
                         [session_wake.WakeTarget(PANE, TMUX_SOCKET, os.getpid())])
        # a peer mail from alpha queues without a second wake
        peer = self.mac.mcp("send", cli="cli-a", to="beta@ws-wsl-1", kind="message",
                            subject="hi", body="from a peer session")
        self.assertTrue(peer["ok"], peer)
        self.deliver_to_wsl()
        wait_for(lambda: self.wsl.daemon.spool.unread_count("beta") == 2 or None)
        time.sleep(0.3)   # the deliver signalled the tick: give it the one pass it may take
        self.assertEqual(len(self.wsl.waker.targets), 1)
        self.assertEqual(self.wsl.hook("prompt_submit", cli="cli-b")["unread"], 2)

    # -- scenario 5 ------------------------------------------------------------------------------

    def test_5_a_session_submits_a_review_and_reads_the_trimmed_result(self):
        """V2d end to end: MCP tool -> daemon -> link frame -> the hub's own review API, and the
        answer trimmed on the way back. started_by is the link's workstation, and a review
        another workstation started answers exactly as an unknown id."""
        self.link_both()
        # the API script: the first submit answers running, then one completed view, then a
        # foreign one, and the hub's 404 for an unknown id
        review_id = "review_20261124_090101_ab12cd"
        self.review_api.script = [
            {"id": review_id, "status": "running", "label": "REVIEW",
             "note": "advisory, no Judge"},
            {"id": review_id, "status": "completed", "label": "REVIEW",
             "note": "advisory, no Judge",
             "result": {"state": "done", "summary": "1 findings (1 high, 0 medium, 0 low), 0 notes",
                        "findings": [{"path": "cli/llm-agent/x.py", "line": 9, "quote": "",
                                      "claim": "off-by-one index",
                                      "scenario": "a 9-item list raises IndexError",
                                      "severity": "high", "verified": True, "reason": ""}],
                        "notes": []},
             "request": {"project": "server-guardian",
                         "source": {"kind": "ref", "ref": "feature/retry", "base": "a" * 40,
                                    "head": "b" * 40, "base_ref": ""},
                         "started_by": "session:ws-mac-1"}},
            {"id": review_id, "status": "completed",
             "request": {"started_by": "session:ws-other-1"}},
            session_link.ReviewAPIError("review not found", status=404),
        ]
        submitted = self.mac.mcp("review_submit", cli="cli-a", project="server-guardian",
                                 ref="feature/retry", base="main", expect_sha="a" * 40,
                                 provider="claude")
        self.assertTrue(submitted["ok"], submitted)
        self.assertEqual(submitted["id"], review_id)
        self.assertEqual(submitted["status"], "running")
        self.assertEqual(self.review_api.submits, [{
            "project": "server-guardian", "ref": "feature/retry", "base": "main",
            "expect_sha": "a" * 40, "provider": "claude",
            "started_by": "session:ws-mac-1"}])   # the link stamped it, no workstation value
        result = self.mac.mcp("review_result", cli="cli-a", review_id=review_id)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["done"])
        self.assertEqual(result["review"]["state"], "done")
        self.assertEqual(result["review"]["counts"], {"high": 1, "medium": 0, "low": 0})
        self.assertEqual(result["review"]["findings"], [
            {"severity": "high", "title": "off-by-one index", "file": "cli/llm-agent/x.py",
             "line": 9, "detail": "a 9-item list raises IndexError"}])
        # another workstation's review and an unknown id answer the same, through the whole stack
        foreign = self.mac.mcp("review_result", cli="cli-a", review_id=review_id)
        self.assertFalse(foreign["ok"])
        self.assertEqual(foreign["error"], session_local.HUB_ERROR)
        self.assertEqual(foreign["detail"], "not_found: review not found")
        unknown = self.mac.mcp("review_result", cli="cli-a", review_id=review_id)
        self.assertEqual(unknown, foreign)
        self.assertEqual(self.review_api.gets, [review_id, review_id, review_id])


if __name__ == "__main__":
    unittest.main()

"""session_uplink: the uplink half of the workstation link daemon (S2 of
docs/design/operator-sessions.md), driven with fakes and an injected clock: the ssh
argument vector and failure classes, the SD12 dial cycle and its terminal host-key state,
the backoff, the durable spool, and the LinkClient handshake, requests, delivery, heartbeat
and ends. The last two tests run a real hub session_link.LinkSession over two os.pipe
pairs (once directly against LinkClient, once through Uplink with a bridging spawn), with
testenv.isolate_data_dir() and a registered workstation; no real ssh, no network, no real
sleeps over 0.5 s."""

import calendar
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

import session_local
import session_uplink

CONFIG = {"workstation": "ws-mac-1", "hub_address": "100.64.0.1",
          "hub_name": "hub.example.test", "hub_alias": "site-a-arm1",
          "hub_user": "ubuntu", "ssh_port": 22}
GREETING = {"t": "greeting", "proto": session_local.LINK_PROTO,
            "version": session_local.LINK_VERSION, "workstation": "ws-mac-1"}


def greeting(**overrides):
    frame = dict(GREETING)
    frame.update(overrides)
    return frame


def greeting_bytes():
    return (json.dumps(GREETING) + "\n").encode("utf-8")


def _iso(value):
    """The stored stamps parse back as ISO-UTC."""
    time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


def _epoch(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class FakeMonotonic:
    """A fake time.monotonic the tests advance by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


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


class QueueReader:
    """Queue-backed stdin for the client: send() queues one frame, eof() ends the stream."""

    def __init__(self):
        self._lines = queue.Queue()

    def send(self, frame):
        self._lines.put((json.dumps(frame) + "\n").encode("utf-8"))

    def eof(self):
        self._lines.put(None)

    def readline(self):
        item = self._lines.get()
        return b"" if item is None else item


class HookedWriter:
    """List-backed stdout with an on_write hook, so a test can answer a request the moment
    its frame is written (or check the spool exactly when a reply leaves)."""

    def __init__(self, on_write=None):
        self.chunks = []
        self._lock = threading.Lock()
        self._on_write = on_write

    def write(self, data):
        with self._lock:
            self.chunks.append(bytes(data))
        if self._on_write is not None:
            self._on_write(bytes(data))

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
        return wait_for(lambda: next((f for f in self.frames() if f.get("t") == kind), None),
                        timeout)


class FakeProc:
    """A Popen stand-in with BytesIO pipe ends and a scripted exit."""

    def __init__(self, stdout_data=b"", stderr_data=b"", returncode=255):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(stdout_data)
        self.stderr = io.BytesIO(stderr_data)
        self.returncode = returncode
        self.killed = False
        self.argv = None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True

    def poll(self):
        return self.returncode


class RecordingSpawn:
    """spawn stand-in: pops one scripted child per call and records every argv and env."""

    def __init__(self, scripts=()):
        self.scripts = list(scripts)
        self.calls = []

    def __call__(self, argv, *, stdin=None, stdout=None, stderr=None, env=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {})})
        script = self.scripts.pop(0) if self.scripts else {}
        if "raise" in script:
            raise script["raise"]
        proc = FakeProc(script.get("stdout", b""), script.get("stderr", b""),
                        script.get("returncode", 255))
        proc.argv = list(argv)
        return proc


def saw_greeting(child):
    """A stand-in for the Uplink's stream serving: reports whether the child's first line
    was a greeting (any greeting marks the dial as connected)."""
    line = child.proc.stdout.readline()
    try:
        frame = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(frame, dict) and frame.get("t") == "greeting"


class SpoolTestCase(unittest.TestCase):
    """A temp home, SessionPaths and a Spool; mail() builds hub-shaped envelopes."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="session-uplink-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.paths = session_local.SessionPaths(self.home)
        self.spool = session_uplink.Spool(self.paths)

    def mail(self, mail_id, created="2026-11-21T10:00:00Z",
             expires="2026-11-22T10:00:00Z", to="alpha@ws-mac-1"):
        return {"id": mail_id, "thread": "t-1", "from": "operator", "from_kind": "operator",
                "to": to, "kind": "message", "subject": "s", "body": "b", "task": None,
                "reply_to": None, "hop": 0, "created": created, "expires": expires,
                "redelivered": False}


class SshArgvTests(SpoolTestCase):
    def test_argv_is_the_pinned_strict_dial_list(self):
        argv = session_uplink.ssh_argv(CONFIG, self.paths, "100.64.0.1")
        self.assertEqual(argv, ["ssh", "-T", "-F", "/dev/null", "-o", "BatchMode=yes",
                                "-o", "IdentitiesOnly=yes", "-i", str(self.paths.key),
                                "-o", "HostKeyAlias=site-a-arm1",
                                "-o", f"UserKnownHostsFile={self.paths.known_hosts}",
                                "-o", "StrictHostKeyChecking=yes",
                                "-o", "GlobalKnownHostsFile=/dev/null",
                                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                                "-o", "ConnectTimeout=15", "-p", "22", "ubuntu@100.64.0.1"])
        self.assertNotIn("accept-new", argv)          # never learn-on-first-use
        self.assertNotIn("StrictHostKeyChecking=no", argv)

    def test_no_remote_command_and_the_same_alias_on_both_targets(self):
        for target in (CONFIG["hub_address"], CONFIG["hub_name"]):
            argv = session_uplink.ssh_argv(CONFIG, self.paths, target)
            self.assertEqual(argv[-1], f"ubuntu@{target}")   # nothing after user@host
            self.assertIn(f"HostKeyAlias={CONFIG['hub_alias']}", argv)

    def test_classify_every_class_and_the_host_key_priority(self):
        host_key = ["Host key verification failed.", "@@ WARNING: REMOTE HOST "
                    "IDENTIFICATION HAS CHANGED!", "No ED25519 host key is known for x.",
                    "No ECDSA host key is known for x.", "No RSA host key is known for x.",
                    "host key for x has changed"]
        connect = ["ssh: connect to host 100.64.0.1 port 22: Connection refused",
                   "Connection timed out", "Operation timed out", "No route to host",
                   "Network is unreachable", "Could not resolve hostname x",
                   "Name or service not known", "nodename nor servname provided"]
        for text in host_key:
            self.assertEqual(session_uplink.classify_ssh_failure(255, text), "host_key")
        for text in connect:
            self.assertEqual(session_uplink.classify_ssh_failure(255, text), "connect")
        self.assertEqual(session_uplink.classify_ssh_failure(255, "Permission denied "
                         "(publickey)."), "auth")
        # host_key wins over a connect marker in the same tail; closed is other, not connect
        self.assertEqual(session_uplink.classify_ssh_failure(
            255, "Connection timed out; Host key verification failed."), "host_key")
        self.assertEqual(session_uplink.classify_ssh_failure(
            255, "Connection closed by remote host"), "other")
        self.assertEqual(session_uplink.classify_ssh_failure(255, ""), "other")
        self.assertEqual(session_uplink.classify_ssh_failure(1, "kex_exchange_identification"),
                         "other")


class DialerTests(SpoolTestCase):
    def dialer(self, spawn, **kwargs):
        kwargs.setdefault("clock", FakeMonotonic())
        return session_uplink.Dialer(CONFIG, self.paths, spawn=spawn, **kwargs)

    def test_targets_address_first_then_the_name_only_when_set(self):
        self.assertEqual(self.dialer(RecordingSpawn()).targets(),
                         ["100.64.0.1", "hub.example.test"])
        empty = session_uplink.Dialer(dict(CONFIG, hub_name=""), self.paths)
        self.assertEqual(empty.targets(), ["100.64.0.1"])

    def test_connect_failure_on_the_ipv4_falls_back_to_the_name_once(self):
        refused = {"stderr": b"ssh: connect to host 100.64.0.1 port 22: Connection refused"}
        spawn = RecordingSpawn([refused, refused])
        self.assertEqual(self.dialer(spawn).dial_cycle(saw_greeting), "connect")
        self.assertEqual(len(spawn.calls), 2)
        self.assertTrue(spawn.calls[0]["argv"][-1].endswith("@100.64.0.1"))
        self.assertTrue(spawn.calls[1]["argv"][-1].endswith("@hub.example.test"))

    def test_greeting_on_the_ipv4_never_dials_the_name(self):
        spawn = RecordingSpawn([{"stdout": greeting_bytes()}])
        dialer = self.dialer(spawn)
        self.assertEqual(dialer.dial_cycle(saw_greeting), "connected")
        self.assertEqual(len(spawn.calls), 1)
        self.assertIsNone(dialer.last_error)

    def test_host_key_on_the_ipv4_is_terminal_no_name_no_further_dials(self):
        mismatch = {"stderr": b"@@@@@@@@@@\n@ WARNING: REMOTE HOST IDENTIFICATION HAS "
                              b"CHANGED! @\nHost key verification failed."}
        spawn = RecordingSpawn([mismatch, mismatch, mismatch])
        dialer = self.dialer(spawn)
        self.assertEqual(dialer.dial_cycle(saw_greeting), "host_key")
        self.assertEqual(dialer.state, "host_key_mismatch")
        self.assertIn("host_key", dialer.last_error)
        self.assertEqual(len(spawn.calls), 1)                 # the name was never dialled
        for _ in range(2):
            self.assertEqual(dialer.dial_cycle(saw_greeting), "host_key")
        self.assertEqual(len(spawn.calls), 1)                 # and never dialled again

    def test_host_key_on_the_name_is_terminal_too(self):
        spawn = RecordingSpawn([{"stderr": b"Connection refused"},
                                {"stderr": b"Host key verification failed."}])
        dialer = self.dialer(spawn)
        self.assertEqual(dialer.dial_cycle(saw_greeting), "host_key")
        self.assertEqual(dialer.state, "host_key_mismatch")
        self.assertEqual(len(spawn.calls), 2)

    def test_auth_and_other_never_fall_back(self):
        for stderr, expected in ((b"Permission denied (publickey).", "auth"),
                                 (b"kex_exchange_identification: banner line", "other"),
                                 (b"", "other")):
            with self.subTest(stderr=stderr):
                spawn = RecordingSpawn([{"stderr": stderr}])
                self.assertEqual(self.dialer(spawn).dial_cycle(saw_greeting), expected)
                self.assertEqual(len(spawn.calls), 1)

    def test_spawn_failure_is_other_and_never_falls_back(self):
        spawn = RecordingSpawn([{"raise": OSError("no ssh in PATH")}])
        dialer = self.dialer(spawn)
        self.assertEqual(dialer.dial_cycle(saw_greeting), "other")
        self.assertEqual(len(spawn.calls), 1)
        self.assertIn("cannot start ssh", dialer.last_error)

    def test_empty_hub_name_has_no_fallback_path(self):
        spawn = RecordingSpawn([{"stderr": b"Connection refused"}])
        dialer = session_uplink.Dialer(dict(CONFIG, hub_name=""), self.paths, spawn=spawn,
                                       clock=FakeMonotonic())
        self.assertEqual(dialer.dial_cycle(saw_greeting), "connect")
        self.assertEqual(len(spawn.calls), 1)

    def test_child_env_passes_only_the_allowed_keys(self):
        spawn = RecordingSpawn([{"stderr": b"Connection refused"}])
        base = {"PATH": "/usr/bin:/bin", "HOME": "/Users/op", "LANG": "C", "USER": "op",
                "LOGNAME": "op", "SSH_AUTH_SOCK": "/tmp/agent.sock", "AGENT_TOKEN": "x"}
        self.dialer(spawn, env=base).dial_cycle(saw_greeting)
        self.assertEqual(spawn.calls[0]["env"],
                         {"PATH": "/usr/bin:/bin", "HOME": "/Users/op", "LANG": "C",
                          "USER": "op", "LOGNAME": "op"})

    def test_backoff_growth_cap_and_reset(self):
        dialer = self.dialer(RecordingSpawn(), rng=lambda: 0.5)   # no jitter
        self.assertEqual([round(dialer.backoff_delay("connect", 0.0), 6) for _ in range(8)],
                         [1, 2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(dialer.backoff_delay("connected", 60.0), 1.0)   # reset (>= 60 s up)
        self.assertEqual(dialer.backoff_delay("connect", 0.0), 2.0)      # grows from 1 again

    def test_backoff_jitter_within_twenty_percent(self):
        for rng_value, factor in ((0.0, 0.8), (1.0, 1.2), (0.5, 1.0)):
            with self.subTest(rng=rng_value):
                dialer = self.dialer(RecordingSpawn(), rng=lambda: rng_value)
                self.assertAlmostEqual(dialer.backoff_delay("connect", 0.0), factor)

    def test_backoff_reset_needs_a_steady_stream(self):
        dialer = self.dialer(RecordingSpawn(), rng=lambda: 0.5)
        dialer.backoff_delay("connect", 0.0)
        dialer.backoff_delay("connect", 0.0)
        self.assertEqual(dialer.backoff_delay("connected", 59.9), 4.0)  # short: no reset

    def test_wait_before_retry_sleeps_in_slices_and_honours_stop(self):
        clock, sleeps = FakeMonotonic(), []

        def fake_sleep(seconds):
            sleeps.append(round(seconds, 3))
            clock.advance(seconds)

        dialer = session_uplink.Dialer(CONFIG, self.paths, spawn=RecordingSpawn(),
                                       clock=clock, sleep=fake_sleep, rng=lambda: 0.5)
        dialer.dial_cycle(lambda child: False)   # one failed cycle: a 1 s backoff
        dialer.wait_before_retry(lambda: False)
        self.assertEqual(sleeps, [0.5, 0.5])
        dialer.wait_before_retry(lambda: True)   # stopped: no sleep at all
        self.assertEqual(sleeps, [0.5, 0.5])


class SpoolTests(SpoolTestCase):
    def test_store_writes_a_durable_0600_record(self):
        self.assertTrue(self.spool.store(self.mail("m-1")))
        path = self.paths.spool / "m-1.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["mail"]["id"], "m-1")
        self.assertEqual(record["session"], "alpha")
        self.assertEqual(record["state"], "unread")
        self.assertEqual(record["stored"], _iso(record["stored"]))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_store_dedupes_without_rewriting(self):
        self.assertTrue(self.spool.store(self.mail("m-1")))
        before = (self.paths.spool / "m-1.json").read_text(encoding="utf-8")
        self.assertFalse(self.spool.store(self.mail("m-1")))
        self.assertEqual((self.paths.spool / "m-1.json").read_text(encoding="utf-8"), before)

    def test_store_refuses_bad_envelopes(self):
        good = self.mail("m-1")
        bad_mails = [{k: v for k, v in good.items() if k != "id"}]        # no id at all
        for field in ("to", "from", "kind", "body"):                      # a missing field
            bad_mails.append({k: v for k, v in good.items() if k != field})
        bad_mails += [{**good, "id": "bad id!"}, {**good, "id": "a" * 81},
                      {**good, "id": "../escape"}, {**good, "id": 5}, "not a dict"]
        for bad in bad_mails:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.spool.store(bad)
        self.assertEqual(list(self.paths.spool.glob("*.json")), [])

    def test_unread_orders_by_created_then_id_and_filters(self):
        self.spool.store(self.mail("m-2", created="2026-11-21T10:00:02Z"))
        self.spool.store(self.mail("m-1", created="2026-11-21T10:00:02Z"))
        self.spool.store(self.mail("m-0", created="2026-11-21T09:59:59Z"))
        self.spool.store(self.mail("x-0", to="beta@ws-mac-1"))
        self.spool.mark_retrieved(["m-0"])
        self.assertEqual([m["id"] for m in self.spool.unread("alpha")], ["m-1", "m-2"])
        self.assertEqual(self.spool.unread_count("alpha"), 2)
        self.assertEqual(self.spool.unread("beta")[0]["id"], "x-0")

    def test_mark_retrieved_is_durable_and_skips_the_unknown(self):
        self.spool.store(self.mail("m-1"))
        self.assertEqual(self.spool.mark_retrieved(["m-1", "ghost", 5, "m-1"]), ["m-1"])
        self.assertEqual(self.spool.unread_count("alpha"), 0)
        self.assertEqual(session_uplink.Spool(self.paths).unread_count("alpha"), 0)
        record = json.loads((self.paths.spool / "m-1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "retrieved")
        self.assertEqual(record["retrieved"], _iso(record["retrieved"]))

    def test_prune_removes_stale_retrieved_and_expired_mail(self):
        far = "2026-11-30T10:00:00Z"
        self.spool.store(self.mail("old", expires=far))       # retrieved 26 h ago: removed
        self.spool.store(self.mail("fresh", expires=far))     # retrieved 1 h ago: kept
        self.spool.store(self.mail("gone", expires="2026-11-21T11:00:00Z"))  # expired
        self.spool.store(self.mail("kept", expires=far))
        self.assertEqual(self.spool.mark_retrieved(["old", "fresh"]), ["old", "fresh"])
        stamps = {"old": "2026-11-22T10:00:00Z", "fresh": "2026-11-23T11:00:00Z"}
        for mail_id, stamp in stamps.items():                 # mark_retrieved stamps now
            path = self.paths.spool / f"{mail_id}.json"       # so backdate by hand
            record = json.loads(path.read_text(encoding="utf-8"))
            record["retrieved"] = stamp
            path.write_text(json.dumps(record), encoding="utf-8")
        now = _epoch("2026-11-23T12:00:00Z")
        self.assertEqual(self.spool.prune(now), 2)
        self.assertEqual(sorted(p.name for p in self.paths.spool.glob("*.json")),
                         ["fresh.json", "kept.json"])
        self.assertEqual(self.spool.prune(now), 0)

    def test_corrupt_records_are_skipped_and_logged(self):
        self.spool.store(self.mail("m-1"))
        (self.paths.spool / "corrupt.json").write_text("not json", encoding="utf-8")
        (self.paths.spool / "list.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual([m["id"] for m in self.spool.unread("alpha")], ["m-1"])
        self.assertEqual(self.spool.mark_retrieved(["corrupt"]), [])   # never rewritten
        self.assertTrue((self.paths.spool / "corrupt.json").exists())
        self.assertEqual(self.spool.prune(_epoch("2026-11-24T12:00:00Z")), 1)  # m-1 expires


class ClientTestCase(SpoolTestCase):
    """A LinkClient on queue/hooked fakes; run() sits in a thread of its own, so requests
    really do come from another thread. Tests call start() themselves."""

    def setUp(self):
        super().setUp()
        self.clock = FakeMonotonic()
        self.reader = QueueReader()
        self.writer = HookedWriter()
        self.states = []
        self.client = None
        self.thread = None
        self.result = []

    def tearDown(self):
        if self.thread is not None and self.thread.is_alive():
            self.reader.eof()
            self.thread.join(timeout=5.0)
            self.assertFalse(self.thread.is_alive(), "the client thread did not stop")

    def start(self, **kwargs):
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("wake", 0.02)
        self.client = session_uplink.LinkClient(self.reader, self.writer, "ws-mac-1",
                                                self.spool, on_state=lambda *a: self.states.append(a),
                                                **kwargs)
        self.thread = threading.Thread(target=lambda: self.result.append(self.client.run()),
                                       daemon=True)
        self.thread.start()
        return self.client

    def join(self, timeout=5.0):
        self.thread.join(timeout=timeout)
        self.assertFalse(self.thread.is_alive(), "the client thread did not stop")
        return self.result[-1] if self.result else None   # the latest client after a restart

    def handshake(self):
        self.reader.send(GREETING)
        self.writer.expect("hello")
        self.reader.send({"t": "welcome", "lease": 1})
        wait_for(lambda: self.client.lease or None)
        wait_for(lambda: self.states or None)

    def restart_fakes(self):
        """Fresh queue ends, because a client's reader thread parks on its queue forever
        (the fakes have no real EOF until eof())."""
        self.reader, self.writer = QueueReader(), HookedWriter()


class HandshakeTests(ClientTestCase):
    def test_good_handshake_records_the_lease_and_the_states(self):
        self.start()
        self.handshake()
        self.assertEqual(self.client.lease, 1)
        self.assertTrue(self.client.up)
        self.assertEqual(self.states, [("up", "")])
        self.reader.eof()
        self.assertEqual(self.join(), "stream_end")
        self.assertEqual(self.states[-1], ("down", "stream_end"))

    def test_bad_proto_version_and_workstation_end_as_bad_greeting(self):
        for overrides in ({"proto": "other"}, {"version": 2}, {"workstation": "ws-ghost-1"}):
            with self.subTest(overrides=overrides):
                self.restart_fakes()
                case = self.start()
                self.reader.send(greeting(**overrides))
                self.assertEqual(self.writer.expect("bye")["reason"], "bad_greeting")
                self.assertEqual(self.join(), "bad_greeting")
                self.assertFalse(case.up)
                self.assertTrue(case.greeted)     # a greeting at all: the dial connected
                self.assertEqual([f.get("t") for f in self.writer.frames()], ["bye"])

    def test_a_non_greeting_first_frame_is_bad_greeting_and_not_connected(self):
        self.start()
        self.reader.send({"t": "welcome", "lease": 1})
        self.assertEqual(self.writer.expect("bye")["reason"], "bad_greeting")
        self.assertEqual(self.join(), "bad_greeting")
        self.assertFalse(self.client.greeted)

    def test_greeting_and_welcome_time_out(self):
        self.start()
        time.sleep(0.1)                      # let the client compute its deadline first
        self.clock.advance(session_uplink.LinkClient.HANDSHAKE_TIMEOUT + 1)
        self.assertEqual(self.writer.expect("bye")["reason"], "greeting_timeout")
        self.join()
        self.restart_fakes()
        self.start()
        self.reader.send(GREETING)
        self.writer.expect("hello")
        self.clock.advance(session_uplink.LinkClient.HANDSHAKE_TIMEOUT + 1)
        self.assertEqual(self.writer.expect("bye")["reason"], "welcome_timeout")
        self.assertEqual(self.join(), "welcome_timeout")

    def test_request_timeout_when_the_hub_stays_silent(self):
        self.start(clock=time.monotonic)     # a real clock, so the 0.3 s really passes
        self.handshake()
        self.assertEqual(self.client.request("list", {"name": "alpha"}, timeout=0.3),
                         {"ok": False, "error": "timeout"})
        self.reader.eof()
        self.join()


class StreamTests(ClientTestCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.handshake()

    def answer_ping(self):
        def on_write(data):
            frame = json.loads(data)
            if frame.get("t") == "ping":
                self.reader.send({"t": "reply", "re": frame["id"], "ok": True})
        self.writer._on_write = on_write

    def test_request_round_trip_from_another_thread(self):
        self.answer_ping()                  # the main thread is another thread for run()
        reply = self.client.request("ping")
        self.assertEqual(reply, {"t": "reply", "re": "d1", "ok": True})
        sent = next(f for f in self.writer.frames() if f.get("t") == "ping")
        self.assertEqual(sent, {"t": "ping", "id": "d1"})
        with self.assertRaises(ValueError):
            self.client.request("restart")

    def test_review_ops_round_trip_with_their_payloads_intact(self):
        """V2d: the uplink lists review_submit and review_get and carries their payloads to the
        hub untouched (the daemon's fields, no identity added)."""

        def on_write(data):
            frame = json.loads(data)
            if frame.get("t") == "ping":
                self.reader.send({"t": "reply", "re": frame["id"], "ok": True})
            elif frame.get("t") == "review_submit":
                self.reader.send({"t": "reply", "re": frame["id"], "ok": True,
                                  "id": "review_20261124_090101_ab12cd", "status": "running"})
            elif frame.get("t") == "review_get":
                self.reader.send({"t": "reply", "re": frame["id"], "ok": True,
                                  "review": {"id": frame.get("review_id"),
                                             "status": "completed"}})

        self.writer._on_write = on_write
        self.assertIn("review_submit", session_uplink.LinkClient.OPS)
        self.assertIn("review_get", session_uplink.LinkClient.OPS)
        submitted = self.client.request(
            "review_submit", {"name": "alpha", "project": "server-guardian",
                              "ref": "feature/retry", "base": "main",
                              "expect_sha": "a" * 40, "provider": "claude"})
        self.assertTrue(submitted["ok"], submitted)
        self.assertEqual(submitted["id"], "review_20261124_090101_ab12cd")
        got = self.client.request("review_get", {"name": "alpha",
                                                 "review_id": "review_20261124_090101_ab12cd"})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["review"]["status"], "completed")
        sent = next(f for f in self.writer.frames() if f.get("t") == "review_submit")
        self.assertEqual({k: v for k, v in sent.items() if k not in ("t", "id")}, {
            "name": "alpha", "project": "server-guardian", "ref": "feature/retry",
            "base": "main", "expect_sha": "a" * 40, "provider": "claude"})
        self.reader.eof()
        self.join()

    def test_request_link_down_when_no_stream_is_up(self):
        idle = session_uplink.LinkClient(QueueReader(), HookedWriter(), "ws-mac-1", self.spool)
        self.assertEqual(idle.request("ping"), {"ok": False, "error": "link_down"})

    def test_deliver_is_durable_before_the_reply_and_dedupes_ok(self):
        seen = {}

        def on_write(data):
            frame = json.loads(data)
            if frame.get("t") == "reply" and frame.get("re") == "h1":
                seen["at_reply"] = self.spool.unread_count("alpha")   # inside the write
        self.writer._on_write = on_write
        self.reader.send({"t": "deliver", "id": "h1", "mail": self.mail("m-1")})
        self.assertEqual(self.writer.expect("reply"),
                         {"t": "reply", "re": "h1", "ok": True})
        self.assertEqual(seen["at_reply"], 1)          # the record exists before the ack
        self.reader.send({"t": "deliver", "id": "h2", "mail": self.mail("m-1")})
        wait_for(lambda: next((f for f in self.writer.frames() if f.get("re") == "h2"), None))
        second = next(f for f in self.writer.frames() if f.get("re") == "h2")
        self.assertEqual(second, {"t": "reply", "re": "h2", "ok": True})   # dedupe is ok
        self.assertEqual(self.spool.unread_count("alpha"), 1)

    def test_hub_ping_is_answered(self):
        self.reader.send({"t": "ping", "id": "h9"})
        self.assertEqual(self.writer.expect("reply"), {"t": "reply", "re": "h9", "ok": True})

    def test_client_pings_every_interval(self):
        self.clock.advance(session_uplink.LinkClient.PING_INTERVAL + 1)
        ping = self.writer.expect("ping")
        self.assertTrue(ping["id"].startswith("d"))

    def test_liveness_ends_the_stream_after_90_s_of_silence(self):
        self.clock.advance(session_uplink.LinkClient.LIVENESS_TIMEOUT + 1)
        self.assertEqual(self.writer.expect("bye")["reason"], "liveness")
        self.assertEqual(self.join(), "liveness")

    def test_bye_superseded_ends_the_stream_and_fails_the_pending_request(self):
        def bye_instead(data):
            frame = json.loads(data)
            if frame.get("t") == "list":
                self.reader.send({"t": "bye", "reason": "superseded"})
        self.writer._on_write = bye_instead
        replies = []
        asker = threading.Thread(target=lambda: replies.append(
            self.client.request("list", {"name": "alpha"})))
        asker.start()
        asker.join(timeout=5.0)
        self.assertEqual(replies, [{"ok": False, "error": "link_down"}])
        self.assertEqual(self.join(), "superseded")
        self.assertEqual(self.client.request("ping"), {"ok": False, "error": "link_down"})


class DeliverTests(ClientTestCase):
    def test_deliver_reports_each_mail_and_the_spool_error(self):
        arrived = []
        self.start(on_deliver=lambda mail, fresh: arrived.append((mail["id"], fresh)))
        self.handshake()
        self.reader.send({"t": "deliver", "id": "h1", "mail": self.mail("m-1")})
        self.reader.send({"t": "deliver", "id": "h2", "mail": self.mail("m-1")})
        wait_for(lambda: len(arrived) == 2 or None)
        self.assertEqual(arrived, [("m-1", True), ("m-1", False)])   # new, then dedupe
        self.reader.send({"t": "deliver", "id": "h3", "mail": {"no": "envelope"}})
        reply = wait_for(lambda: next((f for f in self.writer.frames()
                                       if f.get("re") == "h3"), None))
        self.assertEqual(reply["ok"], False)
        self.assertEqual(reply["error"], "spool_error")
        self.assertEqual(self.spool.unread_count("alpha"), 1)
        self.reader.eof()
        self.join()


class HubEndTestCase(SpoolTestCase):
    """The shared setup of the tests that run a real hub LinkSession: an isolated data dir,
    a registered workstation and the hub's own store (as test_session_link.py)."""

    def setUp(self):
        super().setUp()
        import testenv
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        keys = patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE":
                                       str(Path(os.environ["GUARDIAN_DATA_DIR"])
                                           / "authorized_keys2")})
        keys.start()
        self.addCleanup(keys.stop)
        from test_session_link import register_workstation
        register_workstation()
        import mail_store
        self.hub = mail_store.MailStore()


class EndToEndWithHubTests(HubEndTestCase):
    def test_link_client_against_a_real_link_session_over_two_pipes(self):
        import mail_store
        import session_link
        a_read, a_write = os.pipe()   # the daemon writes, the hub reads
        b_read, b_write = os.pipe()   # the hub writes, the daemon reads
        hub_clock = FakeMonotonic()
        hub_reader, hub_writer = os.fdopen(a_read, "rb"), os.fdopen(b_write, "wb")
        # The link thread gets its own store: a MailStore holds one sqlite connection, which
        # may only be used by the thread that opened it (as test_session_link.py does).
        hub = session_link.LinkSession("ws-mac-1", hub_reader, hub_writer,
                                       store=mail_store.MailStore(), clock=hub_clock, wake=0.05)
        hub_result = []
        hub_thread = threading.Thread(target=lambda: hub_result.append(hub.run()),
                                      daemon=True)
        hub_thread.start()
        delivered = []
        client_reader, client_writer = os.fdopen(b_read, "rb"), os.fdopen(a_write, "wb")
        client = session_uplink.LinkClient(client_reader, client_writer, "ws-mac-1",
                                           self.spool, clock=FakeMonotonic(), wake=0.05,
                                           on_deliver=lambda mail, fresh:
                                           delivered.append((mail["id"], fresh)))
        client_result = []
        client_thread = threading.Thread(target=lambda: client_result.append(client.run()),
                                         daemon=True)
        client_thread.start()
        self.assertEqual(wait_for(lambda: client.lease or None), 1)   # handshake completed
        reply = client.request("bind", {"name": "alpha", "cli_session": "cli-1"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["address"], "alpha@ws-mac-1")
        sent = self.hub.send("operator", "alpha@ws-mac-1", "message", "s", "b", op_id="o1")
        self.assertTrue(sent["ok"], sent)
        hub_clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
        wait_for(lambda: self.spool.unread_count("alpha") == 1 or None)
        self.assertEqual(delivered, [(sent["id"], True)])
        self.assertEqual(self.spool.unread("alpha")[0]["body"], "b")
        wait_for(lambda: next((True for m in self.hub.list_mail(limit=50)
                               if m["id"] == sent["id"] and m["state"] == "delivered"), None))
        client_writer.close()   # the daemon goes away: the hub sees EOF, not an error
        client_thread.join(timeout=5.0)
        self.assertFalse(client_thread.is_alive())
        self.assertEqual(client_result, ["stream_end"])
        hub_thread.join(timeout=5.0)
        self.assertFalse(hub_thread.is_alive())
        self.assertEqual(hub_result, [0])   # stream_end with a lease is a normal end


class BridgeSpawn:
    """A spawn stand-in that wires each dial to a real hub LinkSession over two fresh pipe
    pairs, so Uplink runs its whole stack (Dialer decisions included) against the real hub
    end of the stream. Still no ssh and no network."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def __call__(self, argv, *, stdin=None, stdout=None, stderr=None, env=None):
        import mail_store
        import session_link
        self.calls.append(list(argv))
        a_read, a_write = os.pipe()
        b_read, b_write = os.pipe()
        session = session_link.LinkSession("ws-mac-1", os.fdopen(a_read, "rb"),
                                           os.fdopen(b_write, "wb"), store=mail_store.MailStore(),
                                           clock=self.clock, wake=0.05)
        holder = []
        thread = threading.Thread(target=lambda: holder.append(session.run()), daemon=True)
        thread.start()
        return SimpleNamespace(stdin=os.fdopen(a_write, "wb"), stdout=os.fdopen(b_read, "rb"),
                               stderr=io.BytesIO(b""), argv=list(argv), killed=False,
                               wait=lambda timeout=None: (thread.join(timeout), 0)[1],
                               kill=lambda: None)


class UplinkTests(HubEndTestCase):
    def test_uplink_serves_a_real_hub_stream_until_stopped(self):
        import session_link
        hub_clock = FakeMonotonic()
        bridge = BridgeSpawn(hub_clock)
        dialer = session_uplink.Dialer(CONFIG, self.paths, spawn=bridge,
                                       clock=FakeMonotonic(), rng=lambda: 0.5)
        up_event = threading.Event()
        uplink = session_uplink.Uplink(CONFIG, self.paths, self.spool, dialer=dialer,
                                       clock=FakeMonotonic(), on_up=[up_event.set]).start()
        try:
            self.assertTrue(up_event.wait(timeout=5.0))
            self.assertEqual(uplink.state, "up")
            reply = uplink.request("bind", {"name": "alpha", "cli_session": "cli-1"})
            self.assertTrue(reply["ok"], reply)
            sent = self.hub.send("operator", "alpha@ws-mac-1", "message", "s", "b", op_id="o1")
            hub_clock.advance(session_link.LinkSession.POLL_INTERVAL + 0.1)
            wait_for(lambda: self.spool.unread_count("alpha") == 1 or None)
            self.assertEqual(self.spool.unread("alpha")[0]["id"], sent["id"])
            self.assertEqual(len(bridge.calls), 1)   # the first dial held; no redial loop
        finally:
            uplink.stop()
        self.assertEqual(uplink.state, "stopped")
        self.assertEqual(uplink.request("ping"), {"ok": False, "error": "link_down"})
        self.assertEqual(len(bridge.calls), 1)

    def test_terminal_dialer_state_is_shown_and_never_dialled(self):
        stub = SimpleNamespace(state="host_key_mismatch", last_error="host_key: the pinned "
                            "key changed", dial_cycle=lambda run_stream: self.fail("no dial"),
                               wait_before_retry=lambda should_stop: None)
        uplink = session_uplink.Uplink(CONFIG, self.paths, self.spool, dialer=stub,
                                       clock=FakeMonotonic()).start()
        try:
            wait_for(lambda: uplink.state == "host_key_mismatch" or None)
            self.assertIn("host_key", uplink.last_error)
            self.assertEqual(uplink.request("ping"), {"ok": False, "error": "link_down"})
        finally:
            uplink.stop()
        self.assertEqual(uplink.state, "stopped")

    def test_stop_after_a_down_cycle_ends_in_stopped(self):
        cycled = threading.Event()

        def dial_cycle(run_stream):
            cycled.set()
            return "connect"

        stub = SimpleNamespace(state="idle", last_error=None, dial_cycle=dial_cycle,
                               wait_before_retry=lambda should_stop: time.sleep(0.05))
        uplink = session_uplink.Uplink(CONFIG, self.paths, self.spool, dialer=stub,
                                       clock=FakeMonotonic())
        self.assertEqual(uplink.request("bind", {"name": "alpha"}),
                         {"ok": False, "error": "link_down"})   # nothing started yet
        uplink.start()
        try:
            self.assertTrue(cycled.wait(timeout=5.0))
        finally:
            uplink.stop()
        self.assertEqual(uplink.state, "stopped")


if __name__ == "__main__":
    unittest.main()

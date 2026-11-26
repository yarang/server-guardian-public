"""Tests for direct querying of active resident agent sessions (docs/design/resident-session-query.md, Q1-Q3)."""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import mail_queries
import mail_relay
import mail_store
import project_registry
import resident_driver
import resident_monitor
import resident_node
import resident_registry
import session_daemon
import session_link
import session_local
import session_uplink
import testenv


class _Clock:
    def __init__(self, start: str = "2026-11-22T12:00:00"):
        self.now = datetime.fromisoformat(start + "+00:00")

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class _FakeWorker:
    STARTED_VERSION = "testver123"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder = None

    def take_resident_slot(self, address: str) -> bool:
        with self._lock:
            if self._holder is not None and self._holder != address:
                return False
            self._holder = address
            return True

    def release_resident_slot(self, address: str) -> None:
        with self._lock:
            if self._holder == address:
                self._holder = None


class SessionQueryStoreAndPolicyTests(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = _Clock()
        self.store = mail_store.MailStore(now=self.clock)
        self.data = mail_store.data_dir()
        self.store.upsert_agent({
            "address": "codex@node-1", "name": "codex", "node": "node-1",
            "provider": "codex", "driver": "headless",
        })
        self.store.upsert_agent({
            "address": "claude@node-1", "name": "claude", "node": "node-1",
            "provider": "claude", "driver": "headless",
        })

    def _activate(self, address: str, sid: str = "sess-1") -> int:
        b = self.store.bind_begin(address)
        self.assertTrue(b["ok"], b)
        epoch = b["epoch"]
        self.assertTrue(self.store.bind_complete(address, epoch, session_id=sid)["ok"])
        adm = self.store.admit_turn(address, epoch, "notice")
        self.assertTrue(adm["ok"], adm)
        end = self.store.end_turn(address, epoch, adm["turn"], "normal", session_id=sid)
        self.assertTrue(end["ok"], end)
        return epoch

    def _write_project(self, pid: str, data: dict) -> None:
        pdir = self.data / "projects"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{pid}.json").write_text(json.dumps({"id": pid, **data}), encoding="utf-8")

    def test_active_session_gate_and_project_policy_enforcement(self):
        # Bound with pre-generated session_id (like claude before first turn) -> session_ready is 0 -> refused
        b = self.store.bind_begin("claude@node-1")
        self.store.bind_complete("claude@node-1", b["epoch"], session_id="pre-uuid")
        refused = self.store.submit_query("claude@node-1", "server-guardian", "status?")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "no_active_session")

        # Unknown project refused fail-closed
        self._activate("codex@node-1", "th-1")
        unk = self.store.submit_query("codex@node-1", "does-not-exist", "hi")
        self.assertFalse(unk["ok"])
        self.assertEqual(unk["reason"], "unknown_project")

        # Project policy allowlist: disabled ([]), allowlisted, and code project policy support
        self._write_project("disabled-proj", {
            "workspace": "~/guardian-workspaces/disabled-proj",
            "policy": {"resident_query_agents": []},
        })
        dis = self.store.submit_query("codex@node-1", "disabled-proj", "hi")
        self.assertFalse(dis["ok"])
        self.assertEqual(dis["reason"], "queries_disabled_for_project")

        self._write_project("restricted-proj", {
            "workspace": "~/guardian-workspaces/restricted-proj",
            "policy": {"resident_query_agents": ["claude@node-1"]},
        })
        not_allowed = self.store.submit_query("codex@node-1", "restricted-proj", "hi")
        self.assertFalse(not_allowed["ok"])
        self.assertEqual(not_allowed["reason"], "agent_not_allowed_for_project")

        # Allowed default builtin code project succeeds
        ok = self.store.submit_query("codex@node-1", "server-guardian", "check token=supersecret123")
        self.assertTrue(ok["ok"], ok)
        self.assertRegex(ok["id"], mail_queries.QUERY_ID_RE)
        self.assertEqual(ok["status"], "queued")

        # Read-only monitor snapshot exposes session_ready, queries_pending, and metadata (no prompt/answer)
        snap = resident_monitor.snapshot()
        by_addr = {a["address"]: a for a in snap["agents"]}
        self.assertTrue(by_addr["codex@node-1"]["session_ready"])
        self.assertEqual(by_addr["codex@node-1"]["queries_pending"], 1)
        self.assertFalse(by_addr["claude@node-1"]["session_ready"])
        self.assertEqual(len(snap["queries"]), 1)
        self.assertNotIn("prompt", snap["queries"][0])
        self.assertNotIn("answer", snap["queries"][0])

        # Detail read masks secrets
        detail = resident_monitor.query_get(ok["id"])
        self.assertIsNotNone(detail)
        self.assertNotIn("supersecret123", detail["prompt"])

    def test_separate_hourly_budget_queue_cap_cancel_expiry_and_epoch_fencing(self):
        epoch = self._activate("codex@node-1", "th-budget")
        # Exhaust 12 mailbox turns (notice was #1, add 11 more)
        for _ in range(11):
            a = self.store.admit_turn("codex@node-1", epoch, "wake")
            self.assertTrue(a["ok"], a)
            self.store.end_turn("codex@node-1", epoch, a["turn"], "normal", session_id="th-budget")
        # 13th mailbox turn refused
        self.assertEqual(self.store.admit_turn("codex@node-1", epoch, "wake")["reason"], "turn_budget")

        # Direct query STILL admitted because it has its own 20/hour budget (QD4)
        q1 = self.store.submit_query("codex@node-1", "server-guardian", "q1")
        self.assertTrue(q1["ok"], q1)
        adm = self.store.admit_turn("codex@node-1", epoch, "query", query_id=q1["id"])
        self.assertTrue(adm["ok"], adm)
        # Running query cannot be cancelled
        self.assertEqual(self.store.cancel_query(q1["id"])["reason"], "already_running")
        self.store.end_turn(
            "codex@node-1", epoch, adm["turn"], "normal", session_id="th-budget",
            query_id=q1["id"],
            query_reply={"status": "completed", "answer": "Authorization: Bearer sk-1234567890abcdef123456 ok", "truncated": False},
        )
        got = self.store.get_query(q1["id"])
        self.assertEqual(got["status"], "completed")
        self.assertNotIn("sk-1234567890abcdef123456", got["answer"])

        # Queue cap = 5
        ids = []
        for i in range(5):
            r = self.store.submit_query("codex@node-1", "server-guardian", f"pending {i}")
            self.assertTrue(r["ok"], r)
            ids.append(r["id"])
        self.assertEqual(self.store.submit_query("codex@node-1", "server-guardian", "overflow")["reason"], "queue_full")

        # Cancel queued query
        self.assertTrue(self.store.cancel_query(ids[0])["ok"])
        self.assertEqual(self.store.get_query(ids[0])["status"], "cancelled")

        # Epoch change fails remaining queued queries as epoch_changed
        self.store.bind_begin("codex@node-1")
        self.assertEqual(self.store.get_query(ids[1])["status"], "failed")
        self.assertEqual(self.store.get_query(ids[1])["error"], "epoch_changed")

        # Unstarted expiry (10 min): advance past the 1-hour mailbox turn window first
        self.clock.advance(minutes=65)
        epoch2 = self._activate("codex@node-1", "th-exp")
        qexp = self.store.submit_query("codex@node-1", "server-guardian", "will expire")
        self.assertTrue(qexp["ok"])
        self.clock.advance(minutes=11)
        self.assertEqual(self.store.get_query(qexp["id"])["status"], "expired")

        # Reconcile pending_acks delivers query reply after stream drop
        qrec = self.store.submit_query("codex@node-1", "server-guardian", "survives drop")
        self.assertTrue(qrec["ok"])
        adm2 = self.store.admit_turn("codex@node-1", epoch2, "query", query_id=qrec["id"])
        self.assertTrue(adm2["ok"])
        self.store.reconcile("node-1", {
            "codex@node-1": {
                "epoch": epoch2, "ready": True, "spool": [], "tombstones": [],
                "pending_acks": [{
                    "epoch": epoch2, "turn": adm2["turn"], "end_reason": "normal", "acked": [],
                    "session_id": "th-exp", "query_id": qrec["id"],
                    "query_reply": {"status": "completed", "answer": "recovered answer", "truncated": False},
                }],
            }
        })
        self.assertEqual(self.store.get_query(qrec["id"])["answer"], "recovered answer")


class SessionQueryRelayE2ETests(unittest.TestCase):
    """End-to-end test over socketpair: RelaySession <-> ResidentManager <-> HeadlessDriver."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix="sgq-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = mail_store.MailStore()
        self.entries = [
            {"name": "claude", "node": "node-1", "provider": "claude",
             "driver": "headless", "address": "claude@node-1"},
        ]
        self.store.upsert_agent(self.entries[0])

    def test_e2e_query_dispatch_execution_answer_and_missing_session(self):
        hub_sock, node_sock = socket.socketpair()
        self.addCleanup(hub_sock.close)
        self.addCleanup(node_sock.close)

        prompts_seen = []
        fail_next_missing = [False]

        class _FakeRunner:
            def run(self, ctx, prompt):
                prompts_seen.append((ctx.session_id, prompt))
                if fail_next_missing[0]:
                    fail_next_missing[0] = False
                    return resident_driver.TurnResult(ok=False, error="no conversation found", missing_session=True)
                return resident_driver.TurnResult(
                    ok=True, session_id=ctx.session_id or "s-live",
                    text="Direct answer ok (api_key=should_be_masked)",
                )

            def interrupt(self):
                pass

        driver = resident_driver.HeadlessDriver(runner=_FakeRunner(), tick=0.05)
        mgr = resident_node.ResidentManager(_FakeWorker(), root=self.tmp / "agents", driver=driver)
        driver.attach(mgr)
        self.addCleanup(driver.stop)
        self.addCleanup(mgr.shutdown)

        stop_relay = threading.Event()
        self.addCleanup(stop_relay.set)
        session = mail_relay.RelaySession(
            "node-1",
            hub_sock.makefile("rb"),
            hub_sock.makefile("wb"),
            store=mail_store.MailStore(),
            declarations=lambda: list(self.entries),
        )
        session.POLL_INTERVAL = 0.05
        t_relay = threading.Thread(target=session.run, args=(stop_relay,), daemon=True)
        t_node = threading.Thread(
            target=mgr.attach,
            args=(node_sock.makefile("rb"), node_sock.makefile("wb")),
            daemon=True,
        )
        t_relay.start()
        t_node.start()

        # Wait for start-card turn to flip session_ready = True
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            snap = resident_monitor.snapshot()
            ag = next((a for a in snap["agents"] if a["address"] == "claude@node-1"), None)
            if ag and ag["state"] == "ready" and ag["session_ready"]:
                break
            time.sleep(0.05)
        else:
            self.fail("claude@node-1 never reached session_ready=True")

        # Submit direct query
        q = self.store.submit_query("claude@node-1", "server-guardian", "What did you check?")
        self.assertTrue(q["ok"], q)
        deadline = time.monotonic() + 8.0
        detail = None
        while time.monotonic() < deadline:
            detail = resident_monitor.query_get(q["id"])
            if detail and detail["status"] in mail_queries.QUERY_TERMINAL_STATES:
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["status"], "completed", detail)
        self.assertIn("Direct answer ok", detail["answer"])
        self.assertNotIn("should_be_masked", detail["answer"])
        self.assertTrue(any("[guardian-session-query id=" in p for _, p in prompts_seen))

        # Now trigger missing_session on a second query -> fails as session_lost and rebinds
        fail_next_missing[0] = True
        q2 = self.store.submit_query("claude@node-1", "server-guardian", "Lost session check")
        self.assertTrue(q2["ok"], q2)
        deadline = time.monotonic() + 8.0
        detail2 = None
        while time.monotonic() < deadline:
            detail2 = resident_monitor.query_get(q2["id"])
            if detail2 and detail2["status"] in mail_queries.QUERY_TERMINAL_STATES:
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail2)
        self.assertEqual(detail2["status"], "failed", detail2)
        self.assertEqual(detail2["error"], "session_lost")

        stop_relay.set()
        mgr.shutdown()
        t_relay.join(3.0)
        t_node.join(3.0)


class WorkstationSessionQueryQ4Tests(unittest.TestCase):
    """Q4 tests: workstation operator session MCP direct resident queries."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = _Clock()
        self.store = mail_store.MailStore(now=self.clock)
        self.data = mail_store.data_dir()
        self.store.upsert_agent({
            "address": "codex@node-1", "name": "codex", "node": "node-1",
            "provider": "codex", "driver": "headless",
        })
        b = self.store.bind_begin("codex@node-1")
        self.epoch = b["epoch"]
        self.store.bind_complete("codex@node-1", self.epoch, session_id="th-q4")
        adm = self.store.admit_turn("codex@node-1", self.epoch, "notice")
        self.store.end_turn("codex@node-1", self.epoch, adm["turn"], "normal", session_id="th-q4")

    def _claim_and_bind(self, ws: str = "ws-1", name: str = "work", peers=None):
        claim = self.store.link_claim(ws)
        self.assertTrue(claim["ok"], claim)
        lease = claim["lease"]
        bound = self.store.session_bind(ws, lease, name, f"cli-{name}", peers=peers)
        self.assertTrue(bound["ok"], bound)
        return lease

    def test_store_fencing_peers_policy_and_caller_isolation(self):
        lease = self._claim_and_bind("ws-1", "work")
        self.assertTrue(self.store.session_bind("ws-1", lease, "other", "cli-other")["ok"])

        # Stale lease rejected
        stale = self.store.session_resident_query("ws-1", lease + 99, "work", "codex@node-1",
                                                  "server-guardian", "hi")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["reason"], "superseded")

        # Unbound session rejected
        unbound = self.store.session_resident_query("ws-1", lease, "ghost", "codex@node-1",
                                                    "server-guardian", "hi")
        self.assertFalse(unbound["ok"])
        self.assertEqual(unbound["reason"], "not_bound")

        # Restricted peer list refuses non-peer
        self.assertTrue(self.store.session_bind("ws-1", lease, "narrow", "cli-narrow",
                                                peers=["operator"])["ok"])
        not_peer = self.store.session_resident_query("ws-1", lease, "narrow", "codex@node-1",
                                                     "server-guardian", "hi")
        self.assertFalse(not_peer["ok"])
        self.assertEqual(not_peer["reason"], "not_a_peer")

        # Project policy still enforced fail-closed
        unk = self.store.session_resident_query("ws-1", lease, "work", "codex@node-1",
                                                "no-such-proj", "hi")
        self.assertFalse(unk["ok"])
        self.assertEqual(unk["reason"], "unknown_project")

        # Valid submit sets caller_kind='session' and caller='work@ws-1'
        res = self.store.session_resident_query("ws-1", lease, "work", "codex@node-1",
                                                "server-guardian", "check status", op_id="op-q4-1")
        self.assertTrue(res["ok"], res)
        qid = res["id"]

        # Owner reads back; caller_kind is 'session'
        got = self.store.session_resident_query_get("ws-1", lease, "work", qid)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["query"]["caller"], "work@ws-1")
        self.assertEqual(got["query"]["caller_kind"], "session")

        # Another session on the same workstation cannot read work@ws-1's query
        iso = self.store.session_resident_query_get("ws-1", lease, "other", qid)
        self.assertFalse(iso["ok"])
        self.assertEqual(iso["reason"], "not_found")

        # Operator's query cannot be read by a workstation session either
        op_q = self.store.submit_query("codex@node-1", "server-guardian", "op only")
        self.assertTrue(op_q["ok"])
        iso_op = self.store.session_resident_query_get("ws-1", lease, "work", op_q["id"])
        self.assertFalse(iso_op["ok"])
        self.assertEqual(iso_op["reason"], "not_found")

        # Invalid query id rejected
        bad = self.store.session_resident_query_get("ws-1", lease, "work", "bad-id")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "bad_input")

    def test_session_link_query_submit_and_get_wire_round_trip(self):
        hub_sock, ws_sock = socket.socketpair()
        self.addCleanup(hub_sock.close)
        self.addCleanup(ws_sock.close)

        link = session_link.LinkSession(
            "ws-1",
            hub_sock.makefile("rb"),
            hub_sock.makefile("wb"),
            store=mail_store.MailStore(),
            registry_get=lambda ws: {"id": ws},
            wake=0.05,
        )
        t = threading.Thread(target=link.run, daemon=True)
        t.start()

        r = ws_sock.makefile("rb")
        w = ws_sock.makefile("wb")
        self.addCleanup(r.close)
        self.addCleanup(w.close)

        def send(obj):
            w.write((json.dumps(obj) + "\n").encode("utf-8"))
            w.flush()

        def recv():
            line = r.readline()
            self.assertTrue(line, "unexpected EOF on session_link")
            return json.loads(line.decode("utf-8"))

        greeting = recv()
        self.assertEqual(greeting["t"], "greeting")
        send({"t": "hello", "proto": session_link.LINK_PROTO, "version": session_link.LINK_VERSION})
        welcome = recv()
        self.assertEqual(welcome["t"], "welcome")

        send({"t": "bind", "id": "d1", "name": "work", "cli_session": "s-1"})
        self.assertTrue(recv()["ok"])

        send({
            "t": "query_submit", "id": "d2", "name": "work",
            "to": "codex@node-1", "project": "server-guardian",
            "prompt": "wire test", "op_id": "op-wire-1",
            "caller": "operator",  # spoofed key must be dropped
        })
        sub = recv()
        self.assertTrue(sub["ok"], sub)
        qid = sub["id"]

        send({"t": "query_get", "id": "d3", "name": "work", "query_id": qid})
        got = recv()
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["query"]["caller"], "work@ws-1")
        self.assertEqual(got["query"]["caller_kind"], "session")

        ws_sock.shutdown(socket.SHUT_WR)
        t.join(3.0)

    def test_session_daemon_mcp_resident_query_and_polling(self):
        tmp = Path(tempfile.mkdtemp(prefix="sg-q4d-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        paths = session_local.SessionPaths(tmp)
        paths.ensure_dirs()

        calls = []
        poll_count = [0]

        class _FakeUplink:
            state = "up"
            last_error = None

            def start(self):
                pass

            def stop(self):
                pass

            def request(self, frame_type, payload, timeout=20.0):
                calls.append((frame_type, dict(payload)))
                if frame_type == "query_submit":
                    return {"ok": True, "id": "q-20261123-0001", "status": "queued"}
                if frame_type == "query_get":
                    poll_count[0] += 1
                    status = "completed" if poll_count[0] >= 1 else "running"
                    return {"ok": True, "query": {
                        "id": payload["query_id"], "status": status, "answer": "all green",
                    }}
                return {"ok": True}

        cfg = {
            "workstation": "ws-1", "hub_address": "100.64.0.1", "hub_name": "",
            "hub_alias": "hub", "hub_user": "ubuntu", "ssh_port": 22,
        }
        daemon = session_daemon.Daemon(tmp, config=cfg, uplink=_FakeUplink())
        self.addCleanup(lambda: [h.close() for h in daemon.log.handlers])
        # Unlinked session refused
        daemon.sessions["s-1"] = {"cli_session": "s-1", "cli_pid": os.getpid(), "name": None}
        unlinked = daemon._mcp_resident_query(
            {"op": "resident_query", "to": "codex@node-1", "project": "server-guardian", "prompt": "hi"},
            "s-1",
        )
        self.assertFalse(unlinked["ok"])
        self.assertEqual(unlinked["error"], session_local.NOT_LINKED)

        # Linked session submits and polls until terminal
        daemon.sessions["s-1"]["name"] = "work"
        res = daemon._mcp_resident_query(
            {"op": "resident_query", "to": "codex@node-1", "project": "server-guardian",
             "prompt": "hi", "wait_sec": 5},
            "s-1",
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["query"]["status"], "completed")
        self.assertEqual(res["query"]["answer"], "all green")
        self.assertEqual([c[0] for c in calls], ["query_submit", "query_get"])

        res_get = daemon._mcp_resident_query_get(
            {"op": "resident_query_get", "query_id": "q-20261123-0001", "wait_sec": 0},
            "s-1",
        )
        self.assertTrue(res_get["ok"], res_get)
        self.assertEqual(res_get["query"]["answer"], "all green")

    def test_uplink_ops_match_session_link_daemon_frames(self):
        self.assertEqual(session_uplink.LinkClient.OPS,
                         frozenset(session_link.LinkSession._DAEMON_FRAMES))


class MultiSessionMS1ToMS4Tests(unittest.TestCase):
    """MS1-MS4 tests (docs/design/resident-multi-session.md, MD1-MD5)."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix="sgms-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = _Clock()
        self.store = mail_store.MailStore(now=self.clock)
        self.store.upsert_agent({
            "address": "claude@node-1", "name": "claude", "node": "node-1",
            "provider": "claude", "driver": "headless",
        })
        self.store.upsert_agent({
            "address": "codex@node-1", "name": "codex", "node": "node-1",
            "provider": "codex", "driver": "headless",
        })

    def _activate(self, address: str, sid: str) -> int:
        b = self.store.bind_begin(address)
        epoch = b["epoch"]
        self.store.bind_complete(address, epoch, session_id=sid)
        adm = self.store.admit_turn(address, epoch, "notice")
        self.store.end_turn(address, epoch, adm["turn"], "normal", session_id=sid)
        return epoch

    def test_ms1_default_project_key_cap_reset_idle_archive_and_fork_gating(self):
        epoch = self._activate("claude@node-1", "cl-main-1")
        self._activate("codex@node-1", "cx-main-1")

        # MD1: omitting session_key defaults to project:<id>
        q_def = self.store.submit_query("claude@node-1", "server-guardian", "hello")
        self.assertTrue(q_def["ok"], q_def)
        self.assertEqual(q_def["session_key"], "project:server-guardian")
        self.assertEqual(q_def["session_id"], "")  # lazy create on first turn

        # Cross-project session_key refused fail-closed
        mismatch = self.store.submit_query("claude@node-1", "server-guardian", "bad",
                                           session_key="project:fleet")
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["reason"], "session_key_project_mismatch")

        # Invalid label refused
        bad_label = self.store.submit_query("claude@node-1", "server-guardian", "bad",
                                            session_key="project:server-guardian:BAD_LABEL")
        self.assertFalse(bad_label["ok"])
        self.assertEqual(bad_label["reason"], "bad_input")

        # Complete first turn on project:server-guardian so it gets a provider_session_id
        adm = self.store.admit_turn("claude@node-1", epoch, "query", query_id=q_def["id"])
        self.assertTrue(adm["ok"], adm)
        self.store.end_turn("claude@node-1", epoch, adm["turn"], "normal",
                            session_id="cl-sg-1", query_id=q_def["id"],
                            query_reply={"status": "completed", "answer": "ok", "truncated": False},
                            session_key="project:server-guardian")
        # main stays untouched!
        sessions = {s["session_key"]: s for s in self.store.list_agent_sessions(agent="claude@node-1")}
        self.assertEqual(sessions["main"]["provider_session_id"], "cl-main-1")
        self.assertEqual(sessions["project:server-guardian"]["provider_session_id"], "cl-sg-1")
        self.assertTrue(sessions["project:server-guardian"]["session_ready"])

        # MD3: fork supported on claude (when target ready), refused on codex (fork_unsupported)
        fork_codex = self.store.submit_query("codex@node-1", "server-guardian", "fork?",
                                             session_key="main", mode="fork")
        self.assertFalse(fork_codex["ok"])
        self.assertEqual(fork_codex["reason"], "fork_unsupported")

        fork_unready = self.store.submit_query("claude@node-1", "server-guardian", "fork?",
                                               session_key="project:server-guardian:fresh", mode="fork")
        self.assertFalse(fork_unready["ok"])
        self.assertEqual(fork_unready["reason"], "no_active_session")

        fork_ok = self.store.submit_query("claude@node-1", "server-guardian", "read-only review",
                                          session_key="project:server-guardian", mode="fork")
        self.assertTrue(fork_ok["ok"], fork_ok)
        self.assertEqual(fork_ok["session_id"], "cl-sg-1")
        adm_f = self.store.admit_turn("claude@node-1", epoch, "query", query_id=fork_ok["id"])
        self.store.end_turn("claude@node-1", epoch, adm_f["turn"], "normal",
                            session_id="cl-fork-ephemeral", query_id=fork_ok["id"],
                            query_reply={"status": "completed", "answer": "forked ok", "truncated": False},
                            session_key="project:server-guardian")
        # Source provider_session_id must NOT be mutated by fork
        sessions_after_fork = {s["session_key"]: s for s in self.store.list_agent_sessions(agent="claude@node-1")}
        self.assertEqual(sessions_after_fork["project:server-guardian"]["provider_session_id"], "cl-sg-1")

        # MD2 + MD4: fill up to MAX_SESSIONS_PER_AGENT (8: main + project:server-guardian + 6 labels)
        for i in range(6):
            r = self.store.submit_query("claude@node-1", "server-guardian", f"lbl {i}",
                                        session_key=f"project:server-guardian:s{i}")
            self.assertTrue(r["ok"], r)
            # cancel immediately so queue cap (5) doesn't block creating 6 sessions
            self.assertTrue(self.store.cancel_query(r["id"])["ok"])
        over = self.store.submit_query("claude@node-1", "server-guardian", "overflow",
                                       session_key="project:server-guardian:s6")
        self.assertFalse(over["ok"])
        self.assertEqual(over["reason"], "session_limit_reached")

        # Reset main refused; reset scoped frees slot
        self.assertEqual(self.store.reset_agent_session("claude@node-1", "main")["reason"], "cannot_reset_main")
        rst = self.store.reset_agent_session("claude@node-1", "project:server-guardian:s0")
        self.assertTrue(rst["ok"], rst)
        now_ok = self.store.submit_query("claude@node-1", "server-guardian", "fits now",
                                         session_key="project:server-guardian:s6")
        self.assertTrue(now_ok["ok"], now_ok)
        self.assertTrue(self.store.cancel_query(now_ok["id"])["ok"])

        # MD4: 14-day idle archival archives scoped sessions while keeping main active
        self.clock.advance(days=15)
        remaining = {s["session_key"]: s for s in self.store.list_agent_sessions(agent="claude@node-1")}
        self.assertEqual(list(remaining.keys()), ["main"])

        # Read-only monitor lists sessions cleanly
        mon = resident_monitor.list_sessions(agent="claude@node-1")
        self.assertEqual([s["session_key"] for s in mon["sessions"]], ["main"])

    def test_ms2_claude_fork_argv_and_fail_closed_runner(self):
        runner = resident_driver.CliRunner()
        ctx_fork = resident_driver.TurnContext(
            name="claude", address="claude@node-1", provider="claude", epoch=1,
            socket="/tmp/s.sock", session_id="src-uuid", first=False, card="",
            workdir="/tmp", agent_dir="/tmp", session_key="project:server-guardian", mode="fork",
        )
        argv_closed = runner.claude_argv(ctx_fork, "/tmp/mcp.json")
        self.assertIn("--fork-session", argv_closed)
        self.assertIn("--no-session-persistence", argv_closed)
        self.assertIn("src-uuid", argv_closed)

        argv_open = runner.open_claude_argv(ctx_fork)
        self.assertIn("--fork-session", argv_open)
        self.assertIn("--no-session-persistence", argv_open)

        ctx_codex_fork = resident_driver.TurnContext(
            name="codex", address="codex@node-1", provider="codex", epoch=1,
            socket="/tmp/s.sock", session_id="t-1", first=False, card="",
            workdir="/tmp", agent_dir="/tmp", mode="fork",
        )
        res = runner.run(ctx_codex_fork, "hello")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "fork_unsupported")

    def test_ms2_e2e_scoped_sessions_and_isolated_scoped_loss(self):
        hub_sock, node_sock = socket.socketpair()
        self.addCleanup(hub_sock.close)
        self.addCleanup(node_sock.close)

        entries = [{"name": "claude", "node": "node-1", "provider": "claude",
                    "driver": "headless", "address": "claude@node-1"}]
        seen = []
        fail_review_missing = [False]

        class _MultiRunner:
            def run(self, ctx, prompt):
                seen.append((ctx.session_key, ctx.mode, ctx.session_id, ctx.first))
                if ctx.session_key == "project:server-guardian:review" and fail_review_missing[0]:
                    fail_review_missing[0] = False
                    return resident_driver.TurnResult(ok=False, error="no conversation found", missing_session=True)
                sid = ctx.session_id or f"sid-{ctx.session_key}"
                return resident_driver.TurnResult(ok=True, session_id=sid, text=f"ok:{ctx.session_key}:{ctx.mode}")

            def interrupt(self):
                pass

        driver = resident_driver.HeadlessDriver(runner=_MultiRunner(), tick=0.05)
        mgr = resident_node.ResidentManager(_FakeWorker(), root=self.tmp / "agents", driver=driver)
        driver.attach(mgr)
        self.addCleanup(driver.stop)
        self.addCleanup(mgr.shutdown)

        stop_relay = threading.Event()
        self.addCleanup(stop_relay.set)
        session = mail_relay.RelaySession(
            "node-1", hub_sock.makefile("rb"), hub_sock.makefile("wb"),
            store=mail_store.MailStore(), declarations=lambda: list(entries),
        )
        session.POLL_INTERVAL = 0.05
        t_relay = threading.Thread(target=session.run, args=(stop_relay,), daemon=True)
        t_node = threading.Thread(target=mgr.attach,
                                  args=(node_sock.makefile("rb"), node_sock.makefile("wb")), daemon=True)
        t_relay.start()
        t_node.start()

        # Wait for main start-card turn
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            snap = resident_monitor.snapshot()
            ag = next((a for a in snap["agents"] if a["address"] == "claude@node-1"), None)
            if ag and ag["state"] == "ready" and ag["session_ready"]:
                main_epoch = ag["epoch"]
                break
            time.sleep(0.05)
        else:
            self.fail("claude@node-1 never reached session_ready=True")

        def run_q(project, prompt, **kw):
            r = mail_store.MailStore().submit_query("claude@node-1", project, prompt, **kw)
            self.assertTrue(r["ok"], r)
            dl = time.monotonic() + 8.0
            while time.monotonic() < dl:
                d = resident_monitor.query_get(r["id"])
                if d and d["status"] in mail_queries.QUERY_TERMINAL_STATES:
                    return d
                time.sleep(0.05)
            self.fail(f"query {r['id']} timed out")

        # 1) Default scoped session (project:server-guardian) + named review session
        d1 = run_q("server-guardian", "q1")
        self.assertEqual(d1["status"], "completed")
        self.assertEqual(d1["answer"], "ok:project:server-guardian:in_place")

        d2 = run_q("server-guardian", "q2", session_key="project:server-guardian:review")
        self.assertEqual(d2["status"], "completed")
        self.assertNotEqual(d1["session_id"], d2["session_id"])

        # 2) Read-only fork on project:server-guardian
        d_fork = run_q("server-guardian", "fork q", session_key="project:server-guardian", mode="fork")
        self.assertEqual(d_fork["status"], "completed")
        self.assertEqual(d_fork["answer"], "ok:project:server-guardian:fork")

        # 3) Missing session on scoped review isolates failure -> main stays at SAME epoch!
        fail_review_missing[0] = True
        d_lost = run_q("server-guardian", "lost q", session_key="project:server-guardian:review")
        self.assertEqual(d_lost["status"], "failed")
        self.assertEqual(d_lost["error"], "session_lost")
        snap_after = resident_monitor.snapshot()
        ag_after = next(a for a in snap_after["agents"] if a["address"] == "claude@node-1")
        self.assertEqual(ag_after["epoch"], main_epoch)  # main was NOT rebound!
        self.assertTrue(ag_after["session_ready"])

        stop_relay.set()
        mgr.shutdown()
        t_relay.join(3.0)
        t_node.join(3.0)


if __name__ == "__main__":
    unittest.main()

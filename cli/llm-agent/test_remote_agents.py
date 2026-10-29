"""Debate agents on worker hosts (docs/design/remote-agents.md, phase W1): placement, transport, fallback, session pinning.

No test starts ssh or a model CLI. The router gets a scripted Transport; the worker daemon runs in-process on a
temporary unix socket with a fake job command; engine tests replace the hub's runner.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import remote_agents
import worker
from db_manager import DecisionDBManager
from debate_engine import DebateEngine
from llm_runner import LLMResult
from remote_agents import PROTOCOL, RemoteRouter, SshTransport, Transport, WorkerError, parse_reply
from team_manager import Member, TeamValidationError, parse_lineup, validate_lineup
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def ok_ping(free: int = 2) -> Dict[str, Any]:
    return {"ok": True, "protocol": PROTOCOL, "version": "abc", "free_slots": free, "max_jobs": 2,
            "queue_seconds": 5, "provider_timeout": 100.0, "providers": ["claude"]}


def ok_run(text: str = "remote answer", session: Optional[str] = "r1", resumed: bool = False) -> Dict[str, Any]:
    return {"ok": True, "result": {"provider": "claude-cli", "text": text, "session_id": session, "resumed": resumed,
                                   "failures": []}}


class FakeTransport(Transport):
    """Answers per host from a script: {host: callable(request) -> reply or raises WorkerError}."""

    def __init__(self, script: Dict[str, Any]) -> None:
        self.script = script
        self.requests: List[Any] = []

    def call(self, host: str, request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self.requests.append((host, request, timeout))
        handler = self.script.get(host)
        if handler is None:
            raise WorkerError("unreachable (no route)")
        reply = handler(request) if callable(handler) else handler
        if isinstance(reply, Exception):
            raise reply
        return reply

    def upload(self, host: str, key: str, stream: Path, timeout: float) -> Dict[str, Any]:
        self.requests.append((host, {"op": "put-snapshot", "key": key, "bytes": stream.stat().st_size}, timeout))
        if self.script.get(host) is None:
            raise WorkerError("unreachable (no route)")
        return {"ok": True}


def by_op(**ops: Any) -> Any:
    return lambda request: (ops[request["op"]](request) if callable(ops[request["op"]]) else ops[request["op"]])


class HubRunner:
    """The hub's own runner: records calls, answers like a provider CLI."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def run_session_prompt(self, prompt: str, context: Any = None, **kwargs: Any) -> LLMResult:
        self.calls.append({"prompt": prompt, "context": context, **kwargs})
        return LLMResult("codex-cli", "hub answer", "h1", failures=[])


def workers(*ids: str) -> Any:
    return patch.object(remote_agents, "worker_hosts", return_value=[{"id": i, "worker": True} for i in ids])


class PlacementSyntaxTests(unittest.TestCase):
    def test_labels_and_ids(self) -> None:
        self.assertIsNone(remote_agents.host_syntax_error("worker:any"))
        self.assertIsNone(remote_agents.host_syntax_error("node-1"))
        for bad in ("", "worker:best", "a b", "-x", "x;y", 5, "../x"):
            self.assertIsNotNone(remote_agents.host_syntax_error(bad), bad)

    JUDGE = {"persona": "jev_judge"}

    def test_member_host_round_trip(self) -> None:
        lineup = parse_lineup({"id": "t", "name": "T", "judge": self.JUDGE,
                               "expert": {"persona": "domain_architect", "host": "worker:any"},
                               "critic": [{"persona": "security_critic", "host": "node-1"}]})
        self.assertEqual(lineup.expert.host, "worker:any")
        self.assertEqual(lineup.expert.to_dict()["host"], "worker:any")
        self.assertEqual(lineup.critics[0].host, "node-1")
        plain = parse_lineup({"id": "t", "name": "T", "judge": self.JUDGE,
                              "expert": {"persona": "domain_architect"}, "critic": {"persona": "security_critic"}})
        self.assertNotIn("host", plain.expert.to_dict())
        self.assertNotIn("code_tools", plain.expert.to_dict())

    def test_a_malformed_host_is_refused_at_parse(self) -> None:
        with self.assertRaises(TeamValidationError):
            parse_lineup({"id": "t", "judge": self.JUDGE, "critic": {"persona": "security_critic"},
                          "expert": {"persona": "domain_architect", "host": "worker:best"}})

    def test_host_with_repo_mcp_tools_is_accepted(self) -> None:
        # W2: a remote member reads a snapshot through the repo tools, so the two settings combine. Without a persona
        # manager validate_lineup still complains about the personas, but never about the combination.
        lineup = parse_lineup({"id": "t", "name": "T", "judge": self.JUDGE, "critic": {"persona": "security_critic"},
                               "expert": {"persona": "domain_architect", "host": "worker:any", "code_tools": "mcp"}})
        self.assertEqual((lineup.expert.host, lineup.expert.code_tools), ("worker:any", "mcp"))
        try:
            validate_lineup(lineup)
        except TeamValidationError as exc:
            self.assertNotIn("cannot be combined", str(exc))

    def test_unknown_inventory_host_is_refused_only_when_the_inventory_loads(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "servers.json"
            path.write_text(json.dumps([{"id": "node-1", "worker": True}, {"id": "node-2"}]))
            with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(path)}):
                self.assertIsNone(remote_agents.inventory_host_error("node-1"))
                self.assertIn("not in the inventory", remote_agents.inventory_host_error("node-9") or "")
                self.assertIn("not an enabled worker", remote_agents.inventory_host_error("node-2") or "")
                self.assertIsNone(remote_agents.inventory_host_error("worker:any"))
            with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(Path(tmp) / "missing.json")}):
                self.assertIsNone(remote_agents.inventory_host_error("node-9"))


class TransportTests(unittest.TestCase):
    def test_ssh_argv_carries_neither_prompt_nor_request(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_run(argv: List[str], **kwargs: Any) -> Any:
            captured.update(argv=argv, kwargs=kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True}) + "\n", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            SshTransport().call("node-1", {"op": "run", "prompt": "SECRET-PROMPT"}, timeout=5)
        self.assertEqual(captured["argv"][:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4"])
        self.assertEqual(captured["argv"][-3:-1], ["--", "node-1"])
        self.assertNotIn("SECRET-PROMPT", " ".join(captured["argv"]))
        self.assertIn("SECRET-PROMPT", captured["kwargs"]["input"])

    def test_ssh_failures_become_worker_errors(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 5)):
            with self.assertRaises(WorkerError):
                SshTransport().call("node-1", {"op": "ping"}, timeout=5)
        with patch("subprocess.run", side_effect=FileNotFoundError("ssh")):
            with self.assertRaises(WorkerError):
                SshTransport().call("node-1", {"op": "ping"}, timeout=5)

    def test_parse_reply(self) -> None:
        self.assertEqual(parse_reply(0, 'noise\n{"ok": true}\n', ""), {"ok": True})
        with self.assertRaises(WorkerError) as caught:
            parse_reply(255, "", "ssh: connect to host node-1 port 22: Connection refused")
        self.assertIn("unreachable", str(caught.exception))
        for out in ("not json", "[1]"):
            with self.assertRaises(WorkerError):
                parse_reply(0, out + "\n", "")


class RouterTests(unittest.TestCase):
    def router(self, script: Dict[str, Any]) -> tuple:
        hub = HubRunner()
        transport = FakeTransport(script)
        return RemoteRouter(hub, transport), hub, transport

    def test_a_working_worker_answers_and_the_hub_is_not_used(self) -> None:
        router, hub, transport = self.router({"node-1": by_op(ping=ok_ping(), run=ok_run())})
        with workers("node-1"):
            result = router.run("node-1", "hello", {"k": 1}, session_id="r0", provider="claude")
        self.assertEqual((result.text, result.host, result.session_id), ("remote answer", "node-1", "r1"))
        self.assertEqual(hub.calls, [])
        run = next(r for h, r, t in transport.requests if r["op"] == "run")
        self.assertEqual((run["prompt"], run["session_id"], run["provider"]), ("hello", "r0", "claude"))
        self.assertEqual(run["protocol"], PROTOCOL)

    def test_unreachable_worker_falls_back_to_a_new_hub_session(self) -> None:
        router, hub, _ = self.router({})
        with workers("node-1"):
            result = router.run("node-1", "hello", None, session_id="r0", provider="claude", plain_prompt="plain")
        self.assertEqual((result.text, result.host), ("hub answer", ""))
        self.assertIn("node-1: ", result.failures[0])
        self.assertIn("the call ran on the hub", result.failures[0])
        self.assertIsNone(hub.calls[0].get("session_id"))
        self.assertIsNone(hub.calls[0].get("provider"))
        self.assertEqual(hub.calls[0]["plain_prompt"], "plain")

    def test_protocol_mismatch_falls_back_and_marks_the_host_down(self) -> None:
        old = dict(ok_ping(), protocol=PROTOCOL + 1)
        router, hub, transport = self.router({"node-1": by_op(ping=old, run=ok_run())})
        with workers("node-1"):
            first = router.run("node-1", "a")
            second = router.run("node-1", "b")
        self.assertIn("protocol", first.failures[0])
        self.assertEqual(len(hub.calls), 2)
        self.assertEqual(sum(1 for h, r, t in transport.requests if r["op"] == "ping"), 1)  # negative cache
        self.assertIn("node-1", second.failures[0])

    def test_busy_worker_falls_back_but_stays_usable(self) -> None:
        busy = {"ok": False, "busy": True, "error": "busy: 2 jobs running"}
        router, hub, _ = self.router({"node-1": by_op(ping=ok_ping(), run=busy)})
        with workers("node-1"):
            result = router.run("node-1", "a")
            self.assertEqual(result.host, "")
            self.assertEqual(router.ping("node-1")["protocol"], PROTOCOL)  # not marked down

    def test_timeout_sends_a_cancel_for_the_same_job(self) -> None:
        def run(_request: Dict[str, Any]) -> Any:
            return WorkerError("no reply within 5s")

        router, hub, transport = self.router({"node-1": by_op(ping=ok_ping(), run=run, cancel={"ok": True})})
        with workers("node-1"):
            result = router.run("node-1", "a")
        self.assertEqual(result.host, "")
        ops = [r["op"] for h, r, t in transport.requests]
        self.assertEqual(ops, ["ping", "run", "cancel"])
        jobs = {r["job"] for h, r, t in transport.requests if r["op"] != "ping"}
        self.assertEqual(len(jobs), 1)

    def test_host_that_is_not_an_enabled_worker_is_never_contacted(self) -> None:
        router, hub, transport = self.router({"node-1": by_op(ping=ok_ping(), run=ok_run())})
        with workers("node-2"):
            result = router.run("node-1", "a")
        self.assertEqual(transport.requests, [])
        self.assertIn("not an enabled worker", result.failures[0])

    def test_a_reply_without_a_result_is_an_error(self) -> None:
        router, hub, _ = self.router({"node-1": by_op(ping=ok_ping(), run={"ok": True, "result": {"text": 1}})})
        with workers("node-1"):
            self.assertEqual(router.run("node-1", "a").host, "")

    def test_pick_prefers_free_slots_then_fewer_roles_then_id(self) -> None:
        script = {"node-a": by_op(ping=ok_ping(1)), "node-b": by_op(ping=ok_ping(2)), "node-c": by_op(ping=ok_ping(2)),
                  "node-d": WorkerError("unreachable")}
        router, _, _ = self.router(script)
        with workers("node-a", "node-b", "node-c", "node-d"):
            self.assertEqual(router.pick("worker:any"), "node-b")
            self.assertEqual(router.pick("worker:any", {"node-b"}), "node-c")
            self.assertEqual(router.pick("worker:other", {"node-b"}), "node-c")
            self.assertEqual(router.pick("worker:other", {"node-a", "node-b", "node-c"}), "node-b")  # all used: still one

    def test_pick_returns_none_without_a_usable_worker(self) -> None:
        router, _, _ = self.router({})
        with workers("node-1"):
            self.assertIsNone(router.pick("worker:any"))
        with workers():
            self.assertIsNone(router.pick("worker:any"))


class SessionHostColumnTests(unittest.TestCase):
    def test_old_database_gains_the_column_and_sessions_default_to_the_hub(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE agent_sessions (thread_id TEXT NOT NULL, role TEXT NOT NULL, "
                             "provider TEXT NOT NULL, session_id TEXT NOT NULL, "
                             "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                             "last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (thread_id, role));")
                conn.execute("INSERT INTO agent_sessions (thread_id, role, provider, session_id) "
                             "VALUES ('t1', 'expert', 'claude', 's1');")
            db = DecisionDBManager(path)
            self.assertEqual(db.get_agent_session("t1", "expert")["host"], "")
            db.save_agent_session("t1", "expert", "claude", "s2", host="node-1")
            self.assertEqual(db.get_agent_session("t1", "expert")["host"], "node-1")
            db.save_agent_session("t1", "expert", "claude", "s3")
            self.assertEqual(db.get_agent_session("t1", "expert")["host"], "")


class EnginePlacementTests(unittest.TestCase):
    def engine(self, tmp: str, script: Optional[Dict[str, Any]] = None) -> DebateEngine:
        engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "d.db")
        engine.remote = RemoteRouter(engine.llm, FakeTransport(script or {}))
        return engine

    def hub(self, engine: DebateEngine) -> Any:
        return patch.object(engine.llm, "run_session_prompt",
                            return_value=LLMResult("codex-cli", "hub answer", "h1"))

    def test_a_member_without_host_runs_on_the_hub_and_stores_no_host(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp)
            with self.hub(engine) as run:
                provider, text, failures = engine._ask("t1", "expert", "task", {}, "")
            self.assertEqual((text, failures), ("hub answer", []))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(engine.db.get_agent_session("t1", "expert")["host"], "")

    def test_a_placed_member_runs_on_the_worker_and_the_session_is_pinned_there(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp, {"node-1": by_op(ping=ok_ping(), run=ok_run(session="r1"))})
            with workers("node-1"), self.hub(engine) as run:
                _, text, _ = engine._ask("t1", "expert", "task", {}, "earlier", host="node-1")
            self.assertEqual(text, "remote answer")
            run.assert_not_called()
            self.assertEqual(engine.db.get_agent_session("t1", "expert")["host"], "node-1")

    def test_the_next_turn_resumes_on_the_same_host_with_the_stored_session_id(self) -> None:
        with TemporaryDirectory() as tmp:
            transport_script = {"node-1": by_op(ping=ok_ping(), run=ok_run(session="r1", resumed=True))}
            engine = self.engine(tmp, transport_script)
            engine.db.save_agent_session("t1", "expert", "claude", "r0", host="node-1")
            with workers("node-1"), self.hub(engine):
                engine._ask("t1", "expert", "again", {}, "history that a live session already has", host="node-1")
            run = next(r for h, r, t in engine.remote.transport.requests if r["op"] == "run")
            self.assertEqual(run["session_id"], "r0")
            self.assertNotIn("history that a live session", run["prompt"])

    def test_a_session_on_another_host_is_dropped_and_the_history_is_resent(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp, {"node-2": by_op(ping=ok_ping(), run=ok_run(session="r9"))})
            engine.db.save_agent_session("t1", "expert", "claude", "r0", host="node-1")
            with workers("node-1", "node-2"), self.hub(engine):
                _, _, failures = engine._ask("t1", "expert", "again", {}, "THE-HISTORY", host="node-2")
            run = next(r for h, r, t in engine.remote.transport.requests if r["op"] == "run")
            self.assertIsNone(run["session_id"])
            self.assertIn("THE-HISTORY", run["prompt"])
            self.assertTrue(any("session not resumed" in f for f in failures))
            self.assertEqual(engine.db.get_agent_session("t1", "expert")["host"], "node-2")

    def test_a_member_moved_back_to_the_hub_drops_the_worker_session(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp)
            engine.db.save_agent_session("t1", "expert", "claude", "r0", host="node-1")
            with self.hub(engine) as run:
                engine._ask("t1", "expert", "again", {}, "THE-HISTORY")
            self.assertIsNone(run.call_args.kwargs["session_id"])
            self.assertIn("THE-HISTORY", run.call_args.args[0])
            self.assertEqual(engine.db.get_agent_session("t1", "expert")["host"], "")

    def test_a_dead_worker_means_a_hub_session_that_gets_the_full_history(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp, {})  # nothing answers
            engine.db.save_agent_session("t1", "expert", "claude", "r0", host="node-1")
            calls: List[Dict[str, Any]] = []

            def hub(prompt: str, context: Any = None, **kwargs: Any) -> LLMResult:
                calls.append({"prompt": prompt, **kwargs})
                return LLMResult("codex-cli", "hub answer", "h1", resumed=False)

            with workers("node-1"), patch.object(engine.llm, "run_session_prompt", side_effect=hub):
                _, text, failures = engine._ask("t1", "expert", "again", {}, "THE-HISTORY", host="node-1")
            self.assertEqual(text, "hub answer")
            # One hub call, in a new session, and it already carries the history (no discarded short-prompt answer).
            self.assertEqual(len(calls), 1)
            self.assertIn("THE-HISTORY", calls[0]["prompt"])
            self.assertIsNone(calls[0].get("session_id"))
            self.assertTrue(any("the call ran on the hub" in f for f in failures))
            self.assertEqual(engine.db.get_agent_session("t1", "expert")["host"], "")

    def test_labels_are_resolved_once_per_thread_and_spread_over_workers(self) -> None:
        with TemporaryDirectory() as tmp:
            script = {"node-a": by_op(ping=ok_ping(2), run=ok_run(session="ra")),
                      "node-b": by_op(ping=ok_ping(2), run=ok_run(session="rb"))}
            engine = self.engine(tmp, script)
            with workers("node-a", "node-b"), self.hub(engine):
                engine._ask("t1", "expert", "task", {}, "", host="worker:any")
                engine._ask("t1", "critic", "task", {}, "", host="worker:other")
                engine._ask("t1", "expert", "task", {}, "", host="worker:any")
            hosts = {r: engine.db.get_agent_session("t1", r)["host"] for r in ("expert", "critic")}
            self.assertEqual(hosts, {"expert": "node-a", "critic": "node-b"})

    def test_a_label_with_no_usable_worker_runs_on_the_hub(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp, {})
            with workers("node-1"), self.hub(engine) as run:
                _, text, _ = engine._ask("t1", "expert", "task", {}, "", host="worker:any")
            self.assertEqual(text, "hub answer")
            run.assert_called()

    def test_code_tools_mcp_without_a_repository_is_a_plain_remote_call(self) -> None:
        # No repository in the engine: there is no MCP turn, so the call carries no `repo` field (the engine tests
        # of the snapshot path are in test_remote_repo.py).
        with TemporaryDirectory() as tmp:
            engine = self.engine(tmp, {"node-1": by_op(ping=ok_ping(), run=ok_run())})
            with workers("node-1"), self.hub(engine) as run:
                _, text, _ = engine._ask("t1", "expert", "task", {}, "", host="node-1", code_tools="mcp")
            self.assertEqual(text, "remote answer")
            run.assert_not_called()
            sent = [r for h, r, t in engine.remote.transport.requests if r["op"] == "run"]
            self.assertEqual(len(sent), 1)
            self.assertNotIn("repo", sent[0])


class WorkerReportTests(unittest.TestCase):
    """The Settings view of the workers (W4b): the probe record plus a live ping of every enabled host."""

    SERVERS = [
        {"id": "node-1", "name": "Node one", "worker": True,
         "worker_capable": {"passed": True, "checked": "2026-10-27T01:00:00Z", "max_jobs": 2}},
        {"id": "node-2", "worker": True, "worker_capable": {"passed": True, "max_jobs": 1}},
        {"id": "node-3", "worker": False, "worker_capable": {"passed": False, "checked": "2026-10-26T01:00:00Z"}},
        {"id": "node-4", "worker": False, "worker_capable": {"passed": True}},
        {"id": "plain"},
    ]

    def report(self, script: Dict[str, Any]) -> Dict[str, Any]:
        transport = FakeTransport(script)
        report = remote_agents.worker_report(self.SERVERS, transport)
        self.requests = transport.requests
        return report

    def test_lists_probed_and_enabled_hosts_and_pings_only_the_enabled(self) -> None:
        current = remote_agents.code_version()
        report = self.report({
            "node-1": by_op(ping={**ok_ping(1), "version": current, "git": True}),
            "node-2": WorkerError("no reply within 10s"),
        })
        rows = {row["id"]: row for row in report["workers"]}
        self.assertEqual(sorted(rows), ["node-1", "node-2", "node-3", "node-4"])
        self.assertEqual(sorted(h for h, _r, _t in self.requests), ["node-1", "node-2"])
        up = rows["node-1"]
        self.assertEqual((up["state"], up["current"], up["free_slots"], up["max_jobs"], up["providers"], up["git"]),
                         ("up", True, 1, 2, ["claude"], True))
        self.assertEqual((up["enabled"], up["capable"], up["checked"], up["name"]), (True, True, "2026-10-27T01:00:00Z", "Node one"))
        self.assertEqual((rows["node-2"]["state"], rows["node-2"]["error"]), ("down", "no reply within 10s"))
        self.assertEqual((rows["node-3"]["state"], rows["node-3"]["capable"], rows["node-3"]["enabled"]), ("off", False, False))
        self.assertEqual((rows["node-4"]["state"], rows["node-4"]["capable"]), ("off", True))
        self.assertEqual(report["protocol"], PROTOCOL)

    def test_a_stale_code_version_and_an_old_protocol_are_shown_not_hidden(self) -> None:
        report = self.report({
            "node-1": by_op(ping={**ok_ping(), "version": "old"}),
            "node-2": {"ok": False, "error": "protocol mismatch: worker speaks 1", "protocol": 1},
        })
        rows = {row["id"]: row for row in report["workers"]}
        self.assertEqual((rows["node-1"]["state"], rows["node-1"]["current"]), ("up", False))
        self.assertEqual(rows["node-2"]["state"], "down")
        self.assertIn("protocol mismatch", rows["node-2"]["error"])

    def test_the_report_asks_for_a_fresh_ping_at_the_current_protocol(self) -> None:
        self.report({"node-1": by_op(ping=ok_ping()), "node-2": by_op(ping=ok_ping())})
        for _host, request, timeout in self.requests:
            self.assertEqual(request, {"op": "ping", "protocol": PROTOCOL})
            self.assertEqual(timeout, remote_agents.REPORT_PING_SECONDS)

    def test_choices_are_the_enabled_ids_only(self) -> None:
        self.assertEqual(remote_agents.worker_choices(self.SERVERS), ["node-1", "node-2"])

    def test_an_unreadable_inventory_is_reported_in_the_payload(self) -> None:
        with patch("group_manager.load_servers", return_value=([], "servers.json not found", None)):
            report = remote_agents.worker_report(None, FakeTransport({}))
        self.assertEqual((report["workers"], report["error"]), ([], "servers.json not found"))


class WorkerDaemonTests(unittest.TestCase):
    """The real daemon and client on a unix socket, with a fake job command instead of a model CLI."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="gw", dir="/tmp")  # unix socket paths are short on macOS
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.dir]))
        self.sock = Path(self.dir) / "w.sock"
        patcher = patch.dict(os.environ, {"GUARDIAN_WORKER_SOCKET": str(self.sock), "GUARDIAN_WORKER_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, job_source: str, max_jobs: int = 2, queue_seconds: float = 5.0) -> worker.Worker:
        script = Path(self.dir) / "job.py"
        script.write_text(job_source)
        os.environ["GUARDIAN_WORKER_EXEC_CMD"] = json.dumps([sys.executable, str(script)])
        daemon = worker.Worker(max_jobs=max_jobs, queue_seconds=queue_seconds)
        server = worker.make_server(self.sock, daemon)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            daemon.shutdown()
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)
        return daemon

    def ask(self, request: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """The hub's side: `worker.py client` with the request on stdin."""
        proc = subprocess.run([sys.executable, str(Path(worker.__file__)), "client"], input=json.dumps(request),
                              capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "PYTHONPATH": str(Path(worker.__file__).parent)})
        return json.loads(proc.stdout.strip().splitlines()[-1])

    ECHO = ("import json,sys\n"
            "p=json.loads(sys.stdin.read())\n"
            "print(json.dumps({'provider':'claude-cli','text':'echo:'+p['prompt'],'session_id':'s1','resumed':False,"
            "'failures':[]}))\n")
    SLEEP = "import time\ntime.sleep(60)\n"

    def run_request(self, **extra: Any) -> Dict[str, Any]:
        return {"op": "run", "protocol": PROTOCOL, "job": "0123456789abcdef", "prompt": "hi", **extra}

    def test_socket_is_private_and_ping_reports_slots(self) -> None:
        self.start(self.ECHO)
        self.assertEqual(self.sock.stat().st_mode & 0o777, 0o600)
        reply = self.ask({"op": "ping", "protocol": PROTOCOL})
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["protocol"], reply["free_slots"], reply["max_jobs"]), (PROTOCOL, 2, 2))

    def test_ping_reports_the_version_loaded_at_start_not_the_files_on_disk(self) -> None:
        # After an update whose restart never happened, the old process must not claim the new version.
        self.start(self.ECHO)
        with patch.object(worker, "STARTED_VERSION", "loaded-at-start"), \
                patch("remote_agents.code_version", return_value="changed-on-disk"):
            reply = self.ask({"op": "ping", "protocol": PROTOCOL})  # a separate client process, same daemon
        self.assertEqual(reply["version"], "loaded-at-start")

    def test_a_run_round_trips_through_the_client(self) -> None:
        self.start(self.ECHO)
        reply = self.ask(self.run_request())
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["text"], "echo:hi")

    def test_protocol_mismatch_and_bad_requests_are_refused(self) -> None:
        self.start(self.ECHO)
        self.assertFalse(self.ask({"op": "ping", "protocol": PROTOCOL + 1})["ok"])
        self.assertFalse(self.ask(self.run_request(providers=["rm -rf"]))["ok"])
        self.assertFalse(self.ask(dict(self.run_request(), job="../etc"))["ok"])
        self.assertFalse(self.ask({"op": "shell", "protocol": PROTOCOL})["ok"])

    def test_client_without_a_daemon_says_so(self) -> None:
        reply = self.ask({"op": "ping", "protocol": PROTOCOL})
        self.assertFalse(reply["ok"])
        self.assertIn("not running", reply["error"])

    def test_a_job_that_runs_too_long_is_killed(self) -> None:
        daemon = self.start(self.SLEEP)
        started = time.monotonic()
        reply = self.ask(self.run_request(timeout=1))
        self.assertFalse(reply["ok"])
        self.assertIn("timed out", reply["error"])
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(daemon._jobs, {})

    def test_cancel_kills_a_running_job(self) -> None:
        daemon = self.start(self.SLEEP)
        result: Dict[str, Any] = {}
        thread = threading.Thread(target=lambda: result.update(self.ask(self.run_request(timeout=60))))
        thread.start()
        deadline = time.monotonic() + 10
        while not daemon._jobs and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.ask({"op": "cancel", "protocol": PROTOCOL, "job": "0123456789abcdef"})["cancelled"])
        thread.join(15)
        self.assertEqual(result.get("error"), "cancelled")
        self.assertEqual(daemon._jobs, {})

    def test_a_hub_that_hangs_up_stops_its_job(self) -> None:
        daemon = self.start(self.SLEEP)
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(str(self.sock))
        conn.sendall((json.dumps(self.run_request(timeout=60)) + "\n").encode())
        deadline = time.monotonic() + 10
        while not daemon._jobs and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(daemon._jobs)
        conn.close()
        deadline = time.monotonic() + 10
        while daemon._jobs and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(daemon._jobs, {})

    def test_a_full_worker_answers_busy_after_the_queue_wait(self) -> None:
        daemon = self.start(self.SLEEP, max_jobs=1, queue_seconds=0.3)
        first = threading.Thread(target=lambda: self.ask(self.run_request(timeout=2)))
        first.start()
        deadline = time.monotonic() + 10
        while not daemon._jobs and time.monotonic() < deadline:
            time.sleep(0.05)
        second = self.ask(dict(self.run_request(timeout=2), job="fedcba9876543210"))
        self.assertTrue(second.get("busy"), second)
        first.join(20)

    def test_router_through_the_real_client_command(self) -> None:
        """RemoteRouter -> a transport that runs `worker.py client` locally instead of ssh -> daemon -> job."""
        self.start(self.ECHO)
        outer = self

        class Local(Transport):
            def call(self, host: str, request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
                return outer.ask(request, timeout)

        hub = HubRunner()
        router = RemoteRouter(hub, Local())
        with workers("node-1"):
            result = router.run("node-1", "hi", None, providers=["claude"])
        self.assertEqual((result.text, result.host, result.session_id), ("echo:hi", "node-1", "s1"))
        self.assertEqual(hub.calls, [])


if __name__ == "__main__":
    unittest.main()

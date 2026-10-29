import json
import os
import sys
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from action_ops import release_held_actions, run_action
from db_manager import DecisionDBManager
from debate_engine import DebateEngine
from jev_gate import DISPUTED, PASSED, PENDING, JevGate, build_decision, gate_from_evaluation, infer_category
from llm_runner import LLMResult
from mcp_client import MCPClient, parse_decision, parse_rationale
from mcp_stdio import MCPError, StdioMCPClient

# A tiny MCP server speaking newline-delimited JSON-RPC, standing in for jev-mcp.
FAKE_SERVER = textwrap.dedent(r'''
    import json, os, sys
    MODE = os.environ.get("FAKE_MODE", "pass")
    decisions = {}

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

    def evaluation():
        if MODE == "pass":
            return "done", {"checks": [{"constraint": "c1", "noul": 0.9, "verdict": "yes"}],
                            "rationale": {"noul": 0.85, "verdict": "yes"},
                            "reversibility": {"score": 1.0, "max": 3, "label": "Reversible with moderate effort"},
                            "findings": []}
        if MODE == "dispute":
            return "done", {"checks": [{"constraint": "c1", "noul": 0.9, "verdict": "yes"},
                                       {"constraint": "SSH commands time out after 5s", "noul": 0.2, "verdict": "no"}],
                            "rationale": {"noul": 0.4, "verdict": "uncertain"},
                            "reversibility": {"score": 2.0, "max": 3, "label": "Costly to reverse"},
                            "findings": ["Constraint no: SSH commands time out after 5s",
                                         "Rationale looks weak; add concrete reasons and trade-offs."]}
        return "queued", None

    for line in sys.stdin:
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hello"}})
            send({"jsonrpc": "2.0", "id": 99, "method": "roots/list"})
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-jev", "version": "0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "jev_submit_decision"}]}})
        elif method == "tools/call":
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            if name == "boom":
                send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": "kaboom"}]}})
                continue
            if name == "env":
                send({"jsonrpc": "2.0", "id": mid, "result": {"structuredContent": {"secret": os.environ.get("UNRELATED_SECRET"),
                      "key": os.environ.get("TYPESAFE_API_KEY")}}})
                continue
            if name == "jev_submit_decision":
                status, ev = evaluation()
                did = len(decisions) + 1
                decisions[did] = {"id": did, "eval_status": status, "evaluation": ev, "chosen": args.get("chosen"),
                                  "evaluation_note": "queued: TYPESAFE_API_KEY is not set" if status != "done" else "evaluated"}
                result = decisions[did]
            elif name == "jev_get_decision":
                result = decisions.get(args["decision_id"], {"id": args["decision_id"], "eval_status": "queued"})
            elif name == "jev_process_queue":
                result = {"processed": 0}
            else:
                result = {}
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}})
        elif method == "exit_now":
            sys.exit(3)
''')


class FakeServerMixin:
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.server = self.dir / "fake_jev.py"
        self.server.write_text(FAKE_SERVER, encoding="utf-8")
        self.config = self.dir / "mcp_servers.json"
        self.config.write_text(json.dumps({"mcp_servers": {"jev": {
            "command": sys.executable, "args": [str(self.server)],
            "env": {"FAKE_MODE": "pass"}, "pass_env": ["TYPESAFE_API_KEY"],
        }}}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def set_mode(self, mode: str) -> None:
        data = json.loads(self.config.read_text())
        data["mcp_servers"]["jev"]["env"]["FAKE_MODE"] = mode
        self.config.write_text(json.dumps(data))

    def gate(self) -> JevGate:
        gate = JevGate(timeout=20)
        original = StdioMCPClient.from_config
        config = self.config

        def from_config(name, base_env=None, timeout=60.0, config_path=None):
            return original(name, base_env=base_env, timeout=timeout, config_path=config)

        patcher = patch("jev_gate.StdioMCPClient.from_config", side_effect=from_config)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gate


class StdioClientTests(FakeServerMixin, unittest.TestCase):
    def test_handshake_notifications_and_server_requests(self) -> None:
        with StdioMCPClient.from_config("jev", config_path=self.config, timeout=20) as client:
            self.assertEqual(client.server_info["name"], "fake-jev")
            self.assertEqual([t["name"] for t in client.list_tools()], ["jev_submit_decision"])
            self.assertEqual(client.call_tool("jev_process_queue", {}), {"processed": 0})

    def test_tool_error_raises(self) -> None:
        with StdioMCPClient.from_config("jev", config_path=self.config, timeout=20) as client:
            with self.assertRaises(MCPError):
                client.call_tool("boom", {})

    def test_only_pass_env_is_forwarded(self) -> None:
        base = {"PATH": os.environ["PATH"], "UNRELATED_SECRET": "s", "TYPESAFE_API_KEY": "k"}
        with StdioMCPClient.from_config("jev", base_env=base, config_path=self.config, timeout=20) as client:
            self.assertEqual(client.call_tool("env", {}), {"secret": None, "key": "k"})

    def test_missing_server_and_unknown_config(self) -> None:
        with self.assertRaises(MCPError):
            StdioMCPClient("/nonexistent/jev-mcp").start()
        with self.assertRaises(MCPError):
            StdioMCPClient.from_config("nope", config_path=self.config)

    def test_server_exit_is_reported(self) -> None:
        client = StdioMCPClient(sys.executable, ["-c", "import sys; sys.stderr.write('dead'); sys.exit(1)"], timeout=10)
        with self.assertRaises(MCPError) as ctx:
            client.start()
        self.assertIn("dead", str(ctx.exception))
        client.close()


class GateRuleTests(unittest.TestCase):
    def ev(self, **over):
        base = {"checks": [{"constraint": "a", "noul": 0.9, "verdict": "yes"}, {"constraint": "b", "noul": 0.8, "verdict": "yes"}],
                "rationale": {"noul": 0.8, "verdict": "yes"}, "reversibility": {"score": 1.0, "label": "moderate"}}
        base.update(over)
        return base

    def test_rules(self) -> None:
        self.assertEqual(gate_from_evaluation("done", self.ev()), (PASSED, []))
        self.assertEqual(gate_from_evaluation("queued", None)[0], PENDING)
        failing = self.ev(checks=[{"constraint": "a", "noul": 0.9, "verdict": "yes"}, {"constraint": "b", "noul": 0.5, "verdict": "uncertain"}])
        gate, reasons = gate_from_evaluation("done", failing)
        self.assertEqual(gate, DISPUTED)
        self.assertEqual(len(reasons), 1)
        self.assertIn(": b", reasons[0])
        self.assertEqual(gate_from_evaluation("done", self.ev(checks=[]))[0], DISPUTED)
        self.assertEqual(gate_from_evaluation("done", self.ev(rationale={"noul": 0.5, "verdict": "uncertain"}))[0], DISPUTED)
        self.assertEqual(gate_from_evaluation("done", self.ev(reversibility={"score": 2.0, "label": "costly"}))[0], DISPUTED)

    def test_legacy_selection_rules(self) -> None:
        legacy = {"fit": {"recommended": "adopt", "confidence": 0.34, "band": "low"}, "rationale": {"verdict": "yes"}}
        self.assertEqual(gate_from_evaluation("done", legacy)[0], DISPUTED)
        legacy["fit"] = {"recommended": "adopt", "confidence": 0.9, "band": "high"}
        self.assertEqual(gate_from_evaluation("done", legacy)[0], PASSED)

    def test_build_decision_shows_proposed_changes(self) -> None:
        actions = [{"tool": "jev_mcp:execute_code_action",
                    "arguments": {"file_path": "apps/agent/timeout.go", "content": "func clampTimeout() {}"}}]
        d = build_decision("p", "t", "task", "draft", "crit", "x", decision="Reject over-limit values with 400.",
                           actions=actions)
        self.assertEqual(d["chosen"], "Reject over-limit values with 400.")
        self.assertIn("must agree with it", d["context"])
        self.assertIn("jev_mcp:execute_code_action apps/agent/timeout.go: func clampTimeout() {}", d["context"])

    def test_build_decision_uses_judge_constraints(self) -> None:
        judgment = 'Approve.\n```json\n{"constraints": ["x"], "actions": []}\n```\nVERDICT: APPROVED'
        d = build_decision("p", "t1", "Require token for SSH agent API", "proposal text", "critique", judgment,
                           constraints=["Token compared in constant time", "Port 9870 closed publicly"])
        self.assertEqual(d["shape"], "verification")
        self.assertEqual(d["constraints"], ["Token compared in constant time", "Port 9870 closed publicly"])
        # The approach under review is the judge's adjusted decision, not the expert's draft.
        self.assertEqual(d["chosen"], "Approve.")
        self.assertIn("Original proposal:\nproposal text", d["context"])
        self.assertNotIn("constraints", d["rationale"])
        self.assertNotIn("VERDICT", d["rationale"])
        self.assertEqual(d["category"], "security")
        self.assertIn("constraints:judge", d["tags"])
        self.assertEqual(infer_category("unrelated words"), "other")

    def test_build_decision_prefers_judge_rationale_field(self) -> None:
        # After a repair re-ask the prose can be only a note about the correction.
        judgment = ('Repaired the patch.\n```json\n{"decision": "Use timeout_sec.", "rationale": '
                    '"fleet_handler.go:468 hardcodes 15; rejected a global setting; default keeps 15.", "constraints": ["c"], "actions": []}\n```\n'
                    'VERDICT: APPROVED')
        rationale = parse_rationale(judgment)
        self.assertIn("rejected a global setting", rationale)
        d = build_decision("p", "t", "task", "draft", "crit", judgment, decision=parse_decision(judgment),
                           rationale=rationale)
        self.assertEqual(d["rationale"], rationale)
        self.assertEqual(d["chosen"], "Use timeout_sec.")
        legacy = build_decision("p", "t", "task", "draft", "crit", judgment)
        self.assertIn("Repaired the patch.", legacy["rationale"])

    def test_build_decision_falls_back_to_critique_points(self) -> None:
        critique = "Issues:\n1. **SSH timeout blocking**: single-threaded calls stall\n- Plaintext AuthKey in export script\n- ok\n"
        d = build_decision("p", "t1", "task", "proposal", critique, "VERDICT: APPROVED")
        self.assertEqual(len(d["constraints"]), 2)
        self.assertIn("SSH timeout blocking", d["constraints"][0])
        self.assertNotIn("**", d["constraints"][0])
        self.assertIn("constraints:critique", d["tags"])
        bare = build_decision("p", "t1", "task", "proposal", "no list here", "ok")
        self.assertEqual(len(bare["constraints"]), 1)
        self.assertIn("constraints:fallback", bare["tags"])

    def test_constraints_are_capped(self) -> None:
        d = build_decision("p", "t", "task", "p", "c", "ok", constraints=[f"c{i}" for i in range(15)])
        self.assertEqual(len(d["constraints"]), 10)


class GateIntegrationTests(FakeServerMixin, unittest.TestCase):
    def test_review_modes(self) -> None:
        gate = self.gate()
        request = build_decision("p", "t", "task", "prop", "crit", "ok")
        self.assertEqual(gate.review(request)["gate"], PASSED)
        self.set_mode("dispute")
        disputed = gate.review(request)
        self.assertEqual(disputed["gate"], DISPUTED)
        self.assertTrue(any("SSH commands time out" in r for r in disputed["reasons"]))
        self.assertFalse(any(r.startswith("jev finding: Constraint") for r in disputed["reasons"]))
        self.set_mode("queue")
        pending = gate.review(request)
        self.assertEqual(pending["gate"], PENDING)
        self.assertTrue(any("TYPESAFE_API_KEY" in r for r in pending["reasons"]))

    def test_unreachable_server_is_pending(self) -> None:
        data = json.loads(self.config.read_text())
        data["mcp_servers"]["jev"]["command"] = "/nonexistent/uv"
        self.config.write_text(json.dumps(data))
        review = self.gate().review(build_decision("p", "t", "task", "prop", "crit", "ok"))
        self.assertEqual(review["gate"], PENDING)


class FakeGate:
    def __init__(self, gate: str):
        self.gate = gate
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        return {"gate": self.gate, "reasons": [] if self.gate == PASSED else ["jev says revise"],
                "jev_decision_id": 7, "eval_status": "done", "evaluation": {}}


class EngineGateTests(unittest.TestCase):
    def run_debate(self, gate: FakeGate):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = DebateEngine(max_rounds=1, db_path=Path(tmp.name) / "d.db", jev_gate=gate)
        engine.mcp = MCPClient(workspace=Path(tmp.name))
        judge = ('```json\n{"decision": "Keep key auth only, as the critic asked.", '
                 '"constraints": ["sshd keeps key auth only"], "checks": ["sshd_config has PasswordAuthentication no"], '
                 '"actions": [{"tool": "jev_mcp:run_tests", '
                 '"arguments": {"test_cmd": "pytest"}, "reason": "verify"}]}\n```\nVERDICT: APPROVED')
        replies = iter([LLMResult("claude-cli", "proposal", "s1"), LLMResult("claude-cli", "critique", "s2"),
                        LLMResult("claude-cli", judge, "s3")])
        with patch.object(engine.llm, "run_session_prompt", side_effect=lambda *a, **k: next(replies)):
            with patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                engine.db.save_debate_session(*a[:4], decision=a[4]) or Path(tmp.name) / "log.md"
            )):
                result = engine.run_debate("Harden sshd", project_id="server-guardian")
        return engine, result

    def test_passed_gate_proposes_actions(self) -> None:
        engine, result = self.run_debate(FakeGate(PASSED))
        self.assertTrue(result["consensus_reached"])
        self.assertEqual(result["final_decision"]["actions"][0]["status"], "PROPOSED")
        self.assertEqual(engine.db.get_thread_history(result["thread_id"])["status"], "APPROVED")

    def test_disputed_gate_holds_actions(self) -> None:
        gate = FakeGate(DISPUTED)
        engine, result = self.run_debate(gate)
        tid = result["thread_id"]
        self.assertFalse(result["consensus_reached"])
        self.assertEqual(result["final_decision"]["jev_gate"], DISPUTED)
        self.assertEqual(engine.db.get_thread_history(tid)["status"], "DISPUTED")
        [action] = engine.db.list_actions(tid)
        self.assertEqual(action["status"], "HELD")
        with self.assertRaises(RuntimeError) as ctx:
            run_action(engine.db, engine.mcp, action["id"], lambda a: True)
        self.assertIn("jev-recheck", str(ctx.exception))
        self.assertEqual(engine.db.latest_jev_review(tid)["gate"], DISPUTED)
        self.assertEqual(gate.requests[0]["constraints"], ["sshd keeps key auth only"])
        self.assertEqual(gate.requests[0]["chosen"], "Keep key auth only, as the critic asked.")
        self.assertIn("jev_mcp:run_tests pytest", gate.requests[0]["context"])
        self.assertNotIn("PasswordAuthentication", json.dumps(gate.requests[0]))  # checks stay out of jev
        self.assertEqual(result["final_decision"]["checks"], ["sshd_config has PasswordAuthentication no"])
        stored = engine.db.get_thread_history(tid)["decision"]
        self.assertEqual(stored["decision"], "Keep key auth only, as the critic asked.")
        self.assertEqual(stored["checks"], ["sshd_config has PasswordAuthentication no"])

        self.assertEqual(release_held_actions(engine.db, tid), [action["id"]])
        self.assertEqual(engine.db.get_action(action["id"])["status"], "PROPOSED")

    def test_pending_gate_status(self) -> None:
        engine, result = self.run_debate(FakeGate(PENDING))
        self.assertEqual(engine.db.get_thread_history(result["thread_id"])["status"], "PENDING_JEV")


class RecheckCommandTests(unittest.TestCase):
    def test_recheck_releases_held_actions(self) -> None:
        import guardian
        from argparse import Namespace

        with TemporaryDirectory() as tmp:
            db = DecisionDBManager(Path(tmp) / "d.db")
            db.save_debate_session("p", "t1", "task", [], {"approved": False, "jev_gate": "PENDING"})
            db.add_action("t1", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "r", "HELD")
            db.save_jev_review("t1", 1, {"gate": PENDING, "reasons": [], "jev_decision_id": 3}, {"title": "x"})

            class PassingGate:
                def recheck(self, decision_id, request):
                    assert decision_id == 3
                    return {"gate": PASSED, "reasons": [], "jev_decision_id": 3, "eval_status": "done", "evaluation": {}}

            args = Namespace(actions=None, show_action=None, run_action=None, reject_action=None, yes=False,
                             jev_status=None, jev_recheck="t1", json=False)
            with patch("db_manager.DecisionDBManager", return_value=db), patch("jev_gate.JevGate", return_value=PassingGate()):
                self.assertTrue(guardian.handle_action_commands(args))
            self.assertEqual(db.list_actions("t1")[0]["status"], "PROPOSED")
            self.assertEqual(db.get_thread_history("t1")["status"], "APPROVED")
            self.assertEqual(db.latest_jev_review("t1")["gate"], PASSED)


if __name__ == "__main__":
    unittest.main()

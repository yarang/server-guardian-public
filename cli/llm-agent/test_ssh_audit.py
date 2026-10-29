import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from group_manager import UNGROUPED
from ssh_audit import ALL_NODES, group_rows, llm_context, render_ssh_audit, worst_rule


def rule(rule_id, status, title=None, cur="", exp="", fix=""):
    return {"rule_id": rule_id, "title": title or rule_id, "status": status, "severity": "HIGH",
            "current_value": cur, "expected_value": exp, "remediation": fix}


def node(node_id, status="ok", score=None, rules=None, **extra):
    n = {"id": node_id, "name": f"name-{node_id}", "status": status, "source": "sshd -T (sudo)", "keys": [], "notes": []}
    if score is not None:
        rules = rules or []
        n["report"] = {"score": score, "rules": rules, "failed_count": sum(1 for r in rules if r["status"] == "FAIL")}
    n.update(extra)
    return n


REPORT = {
    "generated_at": "2026-10-05T03:00:00Z",
    "cached": True,
    "nodes": [
        node("hub", score=90, rules=[rule("SEC-SSH-001", "WARN"), rule("SEC-SSH-002", "PASS")]),
        node("b1", score=45, rules=[rule("SEC-SSH-001", "FAIL", "Root login", "yes", "no", "Set PermitRootLogin no"),
                                    rule("SEC-SSH-002", "FAIL", "Password auth", "yes", "no")]),
        node("b2", status="partial", score=65, rules=[rule("SEC-SSH-001", "PASS"), rule("SEC-SSH-002", "FAIL")],
             source="sshd_config (fallback)", notes=["sshd -T could not run"]),
        node("ws", status="unreachable", error="ssh: connect to host ws port 22: No route to host"),
    ],
    "summary": {"nodes": 4, "audited": 3, "unreachable": 1, "errors": 0, "avg_score": 66.7,
                "failing_nodes": ["b1", "b2"], "rules": [], "by_rule": {}},
}

GROUPS = [
    {"id": "site-a", "name": "Site A", "members": ["hub"]},
    {"id": "site-b", "name": "Site B", "members": ["b1", "b2"]},
    {"id": "arm", "name": "ARM", "members": ["hub", "b1"]},
    {"id": "empty", "name": "Empty", "members": []},
]


class GroupRowTests(unittest.TestCase):
    def test_rows_count_a_node_in_each_group_and_skip_empty_groups(self) -> None:
        rows = {r["group"]: r for r in group_rows(REPORT, GROUPS)}
        self.assertEqual(list(rows), ["Site A", "Site B", "ARM", UNGROUPED])
        self.assertEqual(rows["Site B"]["nodes"], 2)
        self.assertEqual(rows["Site B"]["avg_score"], 55.0)
        self.assertEqual(rows["Site B"]["failing_nodes"], ["name-b1", "name-b2"])
        self.assertEqual(rows["Site B"]["worst_rule"]["rule_id"], "SEC-SSH-002")
        self.assertEqual(rows["ARM"]["avg_score"], 67.5)  # hub 90 + b1 45: b1 counts here too
        self.assertIsNone(rows["Site A"]["worst_rule"])  # WARN alone is not a failure
        self.assertEqual(rows[UNGROUPED], {"group": UNGROUPED, "nodes": 1, "audited": 0, "unreachable": 1,
                                           "avg_score": None, "failing_nodes": [], "worst_rule": None})

    def test_without_groups_everything_is_one_bucket(self) -> None:
        rows = group_rows(REPORT, None)
        self.assertEqual([(r["group"], r["nodes"], r["audited"]) for r in rows], [(ALL_NODES, 4, 3)])

    def test_worst_rule_tie_keeps_evaluation_order(self) -> None:
        nodes = [node("x", score=10, rules=[rule("A", "FAIL"), rule("B", "FAIL")])]
        self.assertEqual(worst_rule(nodes)["rule_id"], "A")
        self.assertIsNone(worst_rule([]))


class RenderTests(unittest.TestCase):
    def test_human_report(self) -> None:
        text = render_ssh_audit(REPORT, GROUPS)
        self.assertIn("4 nodes, 3 audited, 1 unreachable", text)
        self.assertIn("average score 66.7", text)
        self.assertIn("(cached)", text)
        self.assertRegex(text, r"Site B\s+2\s+2\s+55\s+2 \(name-b1, name-b2\)")
        self.assertRegex(text, r"Ungrouped\s+1\s+0\s+—\s+0")
        self.assertIn("[SEC-SSH-001] Root login: yes (expected no)", text)
        self.assertIn("fix: Set PermitRootLogin no", text)
        self.assertNotIn("name-hub (score", text)  # no FAIL rule, not listed as failing
        self.assertIn("name-b2: sshd_config (fallback)", text)
        self.assertIn("name-ws unreachable: ssh: connect to host ws port 22: No route to host", text)

    def test_missing_groups_and_empty_fleet_are_stated(self) -> None:
        self.assertIn("Node groups unavailable", render_ssh_audit(REPORT, None))
        empty = render_ssh_audit({"nodes": [], "summary": {"nodes": 0, "avg_score": None}}, [])
        self.assertIn("average score —", empty)
        self.assertIn("No nodes were audited.", empty)

    def test_llm_context_keeps_only_non_passing_findings(self) -> None:
        ctx = llm_context(REPORT, GROUPS)
        by_id = {n["id"]: n for n in ctx["nodes"]}
        self.assertEqual([f["rule_id"] for f in by_id["b1"]["findings"]], ["SEC-SSH-001", "SEC-SSH-002"])
        self.assertEqual([f["status"] for f in by_id["hub"]["findings"]], ["WARN"])
        self.assertIsNone(by_id["ws"]["score"])
        self.assertEqual(ctx["groups"][0]["group"], "Site A")


class ClientTests(unittest.TestCase):
    def test_query_string(self) -> None:
        from agent_client import NodeAgentClient

        client = NodeAgentClient("http://hub.test", token="")
        with patch.object(client, "_fetch_json", return_value={}) as fetch:
            client.fleet_ssh_audit()
            self.assertEqual(fetch.call_args.args[0], "/api/fleet/ssh")
            self.assertEqual(fetch.call_args.kwargs["method"], "GET")
            client.fleet_ssh_audit(["a b", "c"], refresh=True)
            self.assertEqual(fetch.call_args.args[0], "/api/fleet/ssh?targets=a+b%2Cc&refresh=1")


class CliTests(unittest.TestCase):
    def run_audit(self, client, resolved, as_json):
        import guardian

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = guardian.run_ssh_audit(client, resolved, as_json, refresh=True)
        return code, out.getvalue(), err.getvalue()

    def test_group_target_json_and_text(self) -> None:
        client = MagicMock()
        client.fleet_ssh_audit.return_value = REPORT
        client.get_groups.return_value = {"groups": GROUPS}
        resolved = {"kind": "group", "id": "site-b", "name": "Site B", "ids": ["b1", "b2"]}

        code, out, _ = self.run_audit(client, resolved, True)
        self.assertEqual(code, 0)
        client.fleet_ssh_audit.assert_called_with(["b1", "b2"], refresh=True)
        data = json.loads(out)
        self.assertEqual(data["groups"]["ARM"], ["hub", "b1"])
        self.assertEqual(data["groups"][UNGROUPED], ["ws"])
        self.assertEqual(data["summary"]["avg_score"], 66.7)
        self.assertEqual(data["group_rows"][1]["group"], "Site B")

        code, out, _ = self.run_audit(client, None, False)
        self.assertEqual(code, 0)
        client.fleet_ssh_audit.assert_called_with(None, refresh=True)
        self.assertIn("Failing rules by node:", out)

    def test_hub_failure_exits_1_and_groups_fall_back(self) -> None:
        client = MagicMock()
        client.fleet_ssh_audit.side_effect = RuntimeError("HTTP 400: unknown target host")
        code, out, err = self.run_audit(client, None, False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unknown target host", err)

        client.fleet_ssh_audit.side_effect = None
        client.fleet_ssh_audit.return_value = REPORT
        client.get_groups.side_effect = RuntimeError("HTTP 404")
        with patch("guardian._local_group_manager", return_value=None):
            code, out, _ = self.run_audit(client, None, True)
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["groups"])


class SecurityAuditorTests(unittest.TestCase):
    def make_system(self):
        from multi_agent import MultiAgentSystem

        system = MultiAgentSystem.__new__(MultiAgentSystem)
        system.client = MagicMock()
        system.client.fleet_ssh_audit.return_value = REPORT
        system.client.get_groups.return_value = {"groups": GROUPS}
        system.client.get_ssh_security.return_value = {"score": 80, "rules": []}
        system.llm = MagicMock()
        system.llm.run_prompt.return_value = ("builtin", "analysis")
        system.active_persona = {"id": "server_guardian"}
        return system

    def test_group_and_remote_targets_use_the_fleet_audit(self) -> None:
        system = self.make_system()
        with redirect_stdout(io.StringIO()):
            res = system.handle_query("ssh 보안 점검", target_host="site-b", target_ids=["b1", "b2"])["security"]
        system.client.fleet_ssh_audit.assert_called_once_with(["b1", "b2"])
        self.assertEqual(res["security_score"], 66.7)
        context = system.llm.run_prompt.call_args_list[-1].args[1]
        self.assertIn("fleet_ssh", context)
        self.assertNotIn("fleet_summary", context)  # would route the built-in engine to the uptime summary
        self.assertIn("Site B", context["fleet_ssh_report"])

        system = self.make_system()
        with redirect_stdout(io.StringIO()):
            system.handle_query("ssh audit", target_host="b1")
        system.client.fleet_ssh_audit.assert_called_once_with(["b1"])
        system.client.get_ssh_security.assert_not_called()

    def test_local_target_keeps_the_hub_local_audit(self) -> None:
        system = self.make_system()
        with redirect_stdout(io.StringIO()):
            res = system.handle_query("ssh audit", target_host="local")["security"]
        system.client.fleet_ssh_audit.assert_not_called()
        self.assertEqual(res["security_score"], 80)

    def test_hub_failure_is_reported(self) -> None:
        system = self.make_system()
        system.client.fleet_ssh_audit.side_effect = RuntimeError("down")
        with redirect_stdout(io.StringIO()):
            res = system.run_security_auditor(None, fleet=True)
        self.assertEqual(res, {"agent": "SecurityAuditor", "target": "all", "error": "down"})

    def test_builtin_engine_repeats_the_hub_report(self) -> None:
        from llm_runner import LLMRunner

        with patch.dict(os.environ, {"GUARDIAN_LLM_PROVIDERS": "none"}):
            runner = LLMRunner()
        text = runner._expert_reasoning("audit ssh", {"fleet_ssh": {}, "fleet_ssh_report": "REPORT TEXT"})
        self.assertEqual(text, "REPORT TEXT")


if __name__ == "__main__":
    unittest.main()

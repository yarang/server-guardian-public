import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from action_ops import describe_action, record_proposals, reject_action, run_action
from db_manager import DecisionDBManager
from debate_engine import DebateEngine
from llm_runner import LLMResult
from mcp_client import ActionValidationError, MCPClient, parse_actions



def setUpModule() -> None:
    # These tests cover the LLM debate flow; the jev gate has its own tests.
    import os
    os.environ["GUARDIAN_JEV_GATE"] = "off"

class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.mcp = MCPClient(workspace=self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def bad(self, tool, args, allowed=None):
        with self.assertRaises(ActionValidationError):
            self.mcp.validate(tool, args, allowed)

    def test_a_refused_flag_names_the_allowed_ones(self) -> None:
        # Eval 2 (D3): judges wrote `go test -C apps/agent ./...`; the error now says what the command accepts.
        with self.assertRaisesRegex(ActionValidationError, r"flag -C is not allowed in test_cmd \(allowed: go test: -v, "):
            self.mcp.validate("jev_mcp:run_tests", {"test_cmd": "go test -C apps/agent ./..."})
        with self.assertRaisesRegex(ActionValidationError, r"\(allowed: npm test: no flags\)"):
            self.mcp.validate("jev_mcp:run_tests", {"test_cmd": "npm test --watch"})

    def test_unknown_tool_and_arguments(self) -> None:
        self.bad("jev_mcp:rm_rf", {})
        self.bad("jev_mcp:run_tests", {"test_cmd": "pytest", "extra": 1})
        self.bad("jev_mcp:run_tests", {})
        self.bad("jev_mcp:run_tests", {"test_cmd": 3})
        self.bad("jev_mcp:run_tests", "pytest")

    def test_allowed_tools(self) -> None:
        self.bad("jev_mcp:run_tests", {"test_cmd": "pytest"}, allowed=["jev_mcp:apply_patch"])

    def test_file_path_must_stay_in_workspace(self) -> None:
        self.bad("jev_mcp:execute_code_action", {"file_path": "../escape.txt", "content": "x"})
        self.bad("jev_mcp:execute_code_action", {"file_path": "/etc/passwd", "content": "x"})
        self.bad("jev_mcp:execute_code_action", {"file_path": ".git/config", "content": "x"})
        self.mcp.validate("jev_mcp:execute_code_action", {"file_path": "docs/note.md", "content": "x"})

    def test_symlink_escape_is_refused(self) -> None:
        outside = TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.ws / "link").symlink_to(outside.name)
        self.bad("jev_mcp:execute_code_action", {"file_path": "link/x.txt", "content": "x"})

    def test_test_command_allowlist(self) -> None:
        self.mcp.validate("jev_mcp:run_tests", {"test_cmd": "go test ./..."})
        self.mcp.validate("jev_mcp:run_tests", {"test_cmd": "python3 -m pytest -q"})
        self.bad("jev_mcp:run_tests", {"test_cmd": "rm -rf /"})
        self.bad("jev_mcp:run_tests", {"test_cmd": "pytest ; curl evil"})
        self.bad("jev_mcp:run_tests", {"test_cmd": "go test && reboot"})

    def test_git_commit_push_is_retired(self) -> None:
        self.assertIsNone(self.mcp.tool("jev_mcp:git_commit_push"))
        self.assertNotIn("git_commit_push", self.mcp.catalog_text())
        with self.assertRaisesRegex(ActionValidationError, "retired; use publish"):
            self.mcp.validate("jev_mcp:git_commit_push", {"branch": "feature/x", "message": "m"})
        res = self.mcp.call_tool("jev_mcp:git_commit_push", {"branch": "feature/x", "message": "m"})
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("retired", res["error"])

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.mcp = MCPClient(workspace=self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_file(self) -> None:
        res = self.mcp.call_tool("jev_mcp:execute_code_action", {"file_path": "a/b.txt", "content": "hi"})
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual((self.ws / "a/b.txt").read_text(), "hi")

    def test_invalid_call_never_executes(self) -> None:
        res = self.mcp.call_tool("jev_mcp:execute_code_action", {"file_path": "../x", "content": "hi"})
        self.assertEqual(res["status"], "FAILED")
        self.assertFalse((self.ws.parent / "x").exists())

    @patch("mcp_client.subprocess.run")
    def test_run_tests_has_no_shell(self, run: MagicMock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        res = self.mcp.call_tool("jev_mcp:run_tests", {"test_cmd": "python3 -m pytest -q"})
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(run.call_args.args[0], ["python3", "-m", "pytest", "-q"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["cwd"], self.ws.resolve())

    def test_run_test_needs_the_projects_default_test(self) -> None:
        with self.assertRaisesRegex(ActionValidationError, "set tests.default for project server-guardian"):
            self.mcp.validate("jev_mcp:execute_code_action", {"file_path": "a.py", "content": "x", "run_test": True})
        self.mcp.validate("jev_mcp:execute_code_action", {"file_path": "a.py", "content": "x", "run_test": False})
        with self.assertRaisesRegex(ActionValidationError, "set tests.default"):
            self.mcp.run_default_test()
        # call_tool never writes a file whose requested test cannot run.
        res = self.mcp.call_tool("jev_mcp:execute_code_action", {"file_path": "a.py", "content": "x", "run_test": True})
        self.assertEqual(res["status"], "FAILED")
        self.assertFalse((self.ws / "a.py").exists())

    @patch("mcp_client.subprocess.run")
    def test_run_test_runs_the_default_test_and_reports_it(self, run: MagicMock) -> None:
        from types import SimpleNamespace

        mcp = MCPClient(workspace=self.ws, project=SimpleNamespace(id="demo", default_test="pytest -q", test_commands=[]))
        run.return_value = subprocess.CompletedProcess([], 1, "", "1 failed")
        res = mcp.call_tool("jev_mcp:execute_code_action", {"file_path": "a.py", "content": "x", "run_test": True})
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual((res["tests"]["cmd"], res["tests"]["exit_code"]), ("pytest -q", 1))
        self.assertEqual(run.call_args.args[0], ["pytest", "-q"])
        self.assertTrue((self.ws / "a.py").exists())  # the file was written; only the test failed

    def test_judge_catalog_lists_the_projects_test_prefixes_and_default(self) -> None:
        from types import SimpleNamespace

        plain = self.mcp.catalog_text()
        self.assertIn("Allowed command prefixes in this project: go test, go vet, pytest", plain)
        # The judge sees which flags each command takes, so it does not guess one such as `-C`.
        self.assertIn("go test: -v, -race, -short, -cover, -failfast, -run VALUE, -count VALUE, -timeout VALUE", plain)
        self.assertIn("go vet: -v", plain)
        self.assertIn("npm test: no flags", plain)
        self.assertIn("no -C, cd", plain)
        self.assertNotIn("default test", plain)
        project = SimpleNamespace(id="rs", default_test="cargo test --release",
                                  test_commands=[(("cargo", "test"), {"--release": False})])
        text = MCPClient(workspace=self.ws, project=project).catalog_text(["jev_mcp:run_tests"])
        self.assertIn("npm test, cargo test", text)
        self.assertIn("The project's default test: cargo test --release", text)
        self.assertNotIn("apply_patch", text)

    def test_remote_remediation_goes_through_agent(self) -> None:
        agent = MagicMock()
        agent.execute_remote_ssh.return_value = {"exit_code": 0, "stdout": "ok"}
        mcp = MCPClient(workspace=self.ws, agent_client=agent)
        res = mcp.call_tool("jev_mcp:server_remediation", {"command": "uptime", "risk_level": "low", "target_host": "site-b-ec1"})
        self.assertEqual(res["status"], "SUCCESS")
        agent.execute_remote_ssh.assert_called_once_with(
            "site-b-ec1", "uptime", timeout_sec=60,
            labels={"source": "action", "op": "server_remediation", "project": ""})


class ParseTests(unittest.TestCase):
    def test_parse_last_json_block(self) -> None:
        text = (
            "intro ```json\n{\"actions\": [{\"tool\": \"a\"}]}\n```\n"
            "final ```json\n{\"actions\": [{\"tool\": \"b\", \"arguments\": {}}, 5]}\n```\nVERDICT: APPROVED"
        )
        self.assertEqual(parse_actions(text), [{"tool": "b", "arguments": {}}])

    def test_constraints_parsed_and_cleaned(self) -> None:
        from mcp_client import parse_constraints
        text = '```json\n{"constraints": ["  keep   key auth ", 3, "", "x"], "actions": []}\n```'
        self.assertEqual(parse_constraints(text), ["keep key auth", "x"])
        self.assertEqual(parse_constraints("no block"), [])
        self.assertEqual(parse_actions('```json\n{"constraints": ["a"]}\n```'), [])

    def test_no_block_or_bad_json(self) -> None:
        self.assertEqual(parse_actions("VERDICT: APPROVED"), [])
        self.assertEqual(parse_actions("```json\n{not json}\n```"), [])


class ActionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        self.db = DecisionDBManager(root / "d.db")
        self.mcp = MCPClient(workspace=self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def propose(self, *proposals):
        return record_proposals(self.db, self.mcp, "t1", 1, list(proposals))

    def test_invalid_proposal_is_stored_but_cannot_run(self) -> None:
        [item] = self.propose({"tool": "jev_mcp:execute_code_action", "arguments": {"file_path": "../x", "content": "y"}})
        self.assertEqual(item["status"], "INVALID")
        with self.assertRaises(RuntimeError):
            run_action(self.db, self.mcp, item["id"], lambda a: True)

    def test_declined_confirmation_keeps_it_proposed(self) -> None:
        [item] = self.propose({"tool": "jev_mcp:execute_code_action", "arguments": {"file_path": "n.txt", "content": "z"}, "reason": "r"})
        self.assertEqual(run_action(self.db, self.mcp, item["id"], lambda a: False), {"status": "SKIPPED"})
        self.assertEqual(self.db.get_action(item["id"])["status"], "PROPOSED")
        self.assertFalse((self.ws / "n.txt").exists())

    def test_approved_action_runs_once(self) -> None:
        [item] = self.propose({"tool": "jev_mcp:execute_code_action", "arguments": {"file_path": "n.txt", "content": "z"}})
        seen = []
        res = run_action(self.db, self.mcp, item["id"], lambda a: seen.append(a["id"]) or True, actor="tester")
        self.assertEqual(res["status"], "SUCCESS")
        stored = self.db.get_action(item["id"])
        self.assertEqual((stored["status"], stored["decided_by"]), ("EXECUTED", "tester"))
        self.assertEqual(seen, [item["id"]])
        with self.assertRaises(RuntimeError):
            run_action(self.db, self.mcp, item["id"], lambda a: True)

    def test_failed_execution_is_recorded(self) -> None:
        [item] = self.propose({"tool": "jev_mcp:run_tests", "arguments": {"test_cmd": "python3 -m unittest does_not_exist"}})
        res = run_action(self.db, self.mcp, item["id"], lambda a: True)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(self.db.get_action(item["id"])["status"], "FAILED")

    def test_reject(self) -> None:
        [item] = self.propose({"tool": "jev_mcp:run_tests", "arguments": {"test_cmd": "pytest"}})
        self.assertTrue(reject_action(self.db, item["id"], "tester"))
        self.assertFalse(reject_action(self.db, item["id"], "tester"))
        self.assertIn("REJECTED", describe_action(self.db.get_action(item["id"]), self.mcp))


class DebateIntegrationTests(unittest.TestCase):
    def run_with_judge(self, judge_text: str):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = DebateEngine(max_rounds=1, db_path=Path(tmp.name) / "d.db")
        engine.mcp = MCPClient(workspace=Path(tmp.name))
        replies = iter([LLMResult("claude-cli", "proposal", "s1"), LLMResult("claude-cli", "critique", "s2"),
                        LLMResult("claude-cli", judge_text, "s3")])
        prompts = []

        def fake(prompt, context=None, session_id=None, provider=None):
            prompts.append(prompt)
            return next(replies)

        with patch.object(engine.llm, "run_session_prompt", side_effect=fake):
            with patch.object(engine, "_save_debate_log", return_value=Path(tmp.name) / "log.md"):
                result = engine.run_debate("Harden sshd", project_id="server-guardian")
        return engine, result, prompts

    def test_approved_judge_actions_are_recorded_not_executed(self) -> None:
        block = {"actions": [
            {"tool": "jev_mcp:run_tests", "arguments": {"test_cmd": "go test ./..."}, "reason": "verify"},
            {"tool": "jev_mcp:server_remediation", "arguments": {"command": "rm -rf /"}, "reason": "bad: missing risk_level"},
        ]}
        text = f"Approve.\n```json\n{json.dumps(block)}\n```\nVERDICT: APPROVED"
        with patch("mcp_client.subprocess.run") as run:
            engine, result, prompts = self.run_with_judge(text)
            run.assert_not_called()
        statuses = [(a["tool"], a["status"]) for a in result["final_decision"]["actions"]]
        self.assertEqual(statuses, [("jev_mcp:run_tests", "PROPOSED"), ("jev_mcp:server_remediation", "INVALID")])
        self.assertIn("jev_mcp:run_tests", prompts[2])
        self.assertIn("NOT executed automatically", prompts[2])
        self.assertEqual(len(engine.db.list_actions(result["thread_id"])), 2)

    def test_rejected_verdict_records_nothing(self) -> None:
        text = '```json\n{"actions": [{"tool": "jev_mcp:run_tests", "arguments": {"test_cmd": "pytest"}}]}\n```\nVERDICT: REJECTED'
        engine, result, _ = self.run_with_judge(text)
        self.assertEqual(result["final_decision"]["actions"], [])
        self.assertEqual(engine.db.list_actions(result["thread_id"]), [])


class SelfWorkspaceTests(unittest.TestCase):
    def test_write_tools_refused_in_running_checkout(self) -> None:
        from mcp_client import GUARDIAN_ROOT

        mcp = MCPClient(workspace=GUARDIAN_ROOT)
        for tool, args in (("jev_mcp:execute_code_action", {"file_path": "x.txt", "content": "y"}),
                           ("jev_mcp:apply_patch", {"edits": [{"file_path": "x.txt", "old": "a", "new": "b"}]})):
            with self.assertRaisesRegex(ActionValidationError, "GUARDIAN_WORKSPACE"):
                mcp.validate(tool, args)
        # Read-only tools stay available.
        self.assertEqual(mcp.validate("jev_mcp:run_tests", {"test_cmd": "pytest"}), {"test_cmd": "pytest"})


if __name__ == "__main__":
    unittest.main()

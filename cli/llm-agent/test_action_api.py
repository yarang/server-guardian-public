import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import action_ops
from db_manager import DecisionDBManager
from jev_gate import PASSED, PENDING
from mcp_client import MCPClient

HERE = Path(__file__).resolve().parent


class ActionJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = DecisionDBManager(root / "d.db")
        self.mcp = MCPClient(workspace=root)
        self.db.save_debate_session("p", "t1", "task", [], {"approved": True})
        self.proposed = self.db.add_action("t1", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "check", "PROPOSED")
        self.held = self.db.add_action("t1", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "later", "HELD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_newest_first_with_risk(self) -> None:
        code, out = action_ops.list_actions_json(self.db, self.mcp, "t1")
        self.assertEqual(code, 0)
        self.assertEqual([a["id"] for a in out["actions"]], [self.held, self.proposed])
        self.assertEqual(out["actions"][0]["risk"], "high")  # tests execute workspace code
        self.assertIn("allowlisted", out["actions"][0]["tool_description"])
        self.assertEqual(action_ops.list_actions_json(self.db, self.mcp, "")[1]["actions"][0]["id"], self.held)

    def test_show_and_not_found(self) -> None:
        self.assertEqual(action_ops.show_action_json(self.db, self.mcp, self.proposed)[1]["action"]["reason"], "check")
        code, out = action_ops.show_action_json(self.db, self.mcp, 999)
        self.assertEqual((code, out["ok"]), (action_ops.NOT_FOUND, False))

    def test_run_records_actor_and_runs_once(self) -> None:
        with patch.object(self.mcp, "call_tool", return_value={"status": "SUCCESS", "exit_code": 0}) as call:
            code, out = action_ops.run_action_json(self.db, self.mcp, self.proposed, "dashboard@100.64.0.9")
            again, second = action_ops.run_action_json(self.db, self.mcp, self.proposed, "dashboard@100.64.0.9")
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"]["status"], "EXECUTED")
        self.assertEqual(out["action"]["decided_by"], "dashboard@100.64.0.9")
        self.assertEqual(again, action_ops.CONFLICT)
        self.assertIn("only PROPOSED", second["error"])
        call.assert_called_once()

    def test_failed_tool_is_exit_0_with_ok_false(self) -> None:
        with patch.object(self.mcp, "call_tool", return_value={"status": "FAILED", "exit_code": 1}):
            code, out = action_ops.run_action_json(self.db, self.mcp, self.proposed, "dashboard")
        self.assertEqual(code, 0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["action"]["status"], "FAILED")

    def test_a_tool_that_raises_leaves_the_action_failed_not_running(self) -> None:
        with patch.object(self.mcp, "call_tool", side_effect=ConnectionError("agent unreachable")):
            code, out = action_ops.run_action_json(self.db, self.mcp, self.proposed, "dashboard")
        self.assertEqual(code, 0)
        self.assertFalse(out["ok"])
        stored = self.db.get_action(self.proposed)
        self.assertEqual(stored["status"], "FAILED")
        self.assertIn("ConnectionError: agent unreachable", stored["result"]["error"])
        self.assertEqual(stored["decided_by"], "dashboard")

    def test_an_interrupt_during_the_tool_marks_the_action_failed_and_propagates(self) -> None:
        with patch.object(self.mcp, "call_tool", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                action_ops.run_action(self.db, self.mcp, self.proposed, lambda _a: True, actor="op")
        self.assertEqual(self.db.get_action(self.proposed)["status"], "FAILED")

    def test_operator_fails_a_stale_running_action(self) -> None:
        self.assertEqual(action_ops.fail_stale_action_json(self.db, self.mcp, self.proposed, "op")[0],
                         action_ops.CONFLICT)  # PROPOSED is not stuck
        self.assertEqual(action_ops.fail_stale_action_json(self.db, self.mcp, 999, "op")[0], action_ops.NOT_FOUND)
        self.assertEqual(action_ops.fail_stale_action_json(self.db, self.mcp, self.proposed, "a b")[0],
                         action_ops.INVALID_INPUT)
        self.db.update_action(self.proposed, "RUNNING", decided_by="approver", expected_status="PROPOSED")
        code, out = action_ops.fail_stale_action_json(self.db, self.mcp, self.proposed, "op")
        self.assertEqual((code, out["action"]["status"], out["action"]["decided_by"]), (0, "FAILED", "approver"))
        self.assertIn("interrupted", out["action"]["result"]["error"])
        self.assertEqual(out["action"]["result"]["marked_failed_by"], "op")
        self.assertIn("marked_failed_at", out["action"]["result"])

    def test_a_late_run_result_does_not_overwrite_an_operator_failed_action(self) -> None:
        def late(*_a, **_k):
            # The operator marks the still-running action failed while the tool is working.
            self.assertTrue(action_ops.fail_stale_action(self.db, self.proposed, "op"))
            return {"status": "SUCCESS", "output": "done"}

        with patch.object(self.mcp, "call_tool", side_effect=late):
            result = action_ops.run_action(self.db, self.mcp, self.proposed, lambda _a: True, actor="approver")
        stored = self.db.get_action(self.proposed)
        self.assertEqual((stored["status"], stored["decided_by"]), ("FAILED", "approver"))
        self.assertEqual(stored["result"]["marked_failed_by"], "op")
        self.assertEqual(stored["result"]["late_result"]["status"], "SUCCESS")
        self.assertEqual((result["late"], result["recorded_status"]), (True, "FAILED"))

    def test_run_conflicts_and_bad_input(self) -> None:
        self.assertEqual(action_ops.run_action_json(self.db, self.mcp, self.held, "dashboard")[0], action_ops.CONFLICT)
        self.assertEqual(action_ops.run_action_json(self.db, self.mcp, 999, "dashboard")[0], action_ops.NOT_FOUND)
        self.assertEqual(action_ops.run_action_json(self.db, self.mcp, self.proposed, "a b")[0],
                         action_ops.INVALID_INPUT)
        bad = self.db.add_action("t1", 1, "jev_mcp:run_tests", {"test_cmd": "rm -rf /"}, "x", "PROPOSED")
        code, out = action_ops.run_action_json(self.db, self.mcp, bad, "dashboard")
        self.assertEqual(code, action_ops.CONFLICT)
        self.assertEqual(self.db.get_action(bad)["status"], "INVALID")

    def test_reject(self) -> None:
        code, out = action_ops.reject_action_json(self.db, self.mcp, self.held, "dashboard@x")
        self.assertEqual((code, out["action"]["status"], out["action"]["decided_by"]), (0, "REJECTED", "dashboard@x"))
        self.assertEqual(action_ops.reject_action_json(self.db, self.mcp, self.held, "d")[0], action_ops.CONFLICT)
        self.assertEqual(action_ops.reject_action_json(self.db, self.mcp, 999, "d")[0], action_ops.NOT_FOUND)

    def test_recheck(self) -> None:
        self.assertEqual(action_ops.recheck_thread_json(self.db, "t1")[0], action_ops.NOT_FOUND)
        self.db.save_jev_review("t1", 1, {"gate": PENDING, "reasons": [], "jev_decision_id": 3}, {"title": "x"})

        class Gate:
            def __init__(self, gate):
                self.gate = gate

            def recheck(self, decision_id, request):
                return {"gate": self.gate, "reasons": ["r"], "jev_decision_id": 3, "eval_status": "done",
                        "evaluation": {}}

        code, out = action_ops.recheck_thread_json(self.db, "t1", Gate(PENDING))
        self.assertEqual((code, out["gate"], out["released"]), (0, PENDING, []))
        code, out = action_ops.recheck_thread_json(self.db, "t1", Gate(PASSED))
        self.assertEqual((out["gate"], out["released"]), (PASSED, [self.held]))
        self.assertEqual(out["review"]["reasons"], ["r"])
        self.assertEqual(self.db.get_action(self.held)["status"], "PROPOSED")


class RecheckRoundTests(unittest.TestCase):
    """A recheck covers the round of its review: older and newer rounds are left alone."""

    class Gate:
        def recheck(self, decision_id, request):
            return {"gate": PASSED, "reasons": [], "jev_decision_id": 3, "eval_status": "done", "evaluation": {}}

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = DecisionDBManager(Path(self._tmp.name) / "d.db")
        self.db.save_debate_session("p", "t1", "task", [], {"approved": True, "final_round": 2})
        self.db.save_jev_review("t1", 2, {"gate": PENDING, "reasons": [], "jev_decision_id": 3}, {"title": "x"})
        self.older = self.db.add_action("t1", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "old", "HELD")
        self.current = self.db.add_action("t1", 2, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "new", "HELD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_held_actions_of_the_reviewed_round_are_released(self) -> None:
        code, out = action_ops.recheck_thread_json(self.db, "t1", self.Gate())
        self.assertEqual((code, out["released"]), (0, [self.current]))
        self.assertEqual(self.db.get_action(self.older)["status"], "HELD")

    def test_a_newer_unreviewed_debate_makes_the_recheck_a_conflict(self) -> None:
        # The continued debate ended in round 4 without approval, so it wrote no review and left REVISION_REQUIRED.
        self.db.save_debate_session("p", "t1", "task", [], {"approved": False, "final_round": 4})
        code, out = action_ops.recheck_thread_json(self.db, "t1", self.Gate())
        self.assertEqual((code, out["ok"]), (action_ops.CONFLICT, False))
        self.assertIn("round 2", out["error"])
        self.assertIn("round 4", out["error"])
        self.assertEqual(self.db.get_action(self.current)["status"], "HELD")
        self.assertEqual(self.db.get_thread_status("t1"), "REVISION_REQUIRED")
        with self.assertRaises(action_ops.RecheckConflict):
            action_ops.recheck_thread(self.db, "t1", self.Gate())


class ActionCliContractTests(unittest.TestCase):
    """guardian.py as the hub API calls it: JSON on stdout and the documented exit codes."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.env = {**os.environ, "GUARDIAN_DATA_DIR": self._tmp.name, "GUARDIAN_WORKSPACE": self._tmp.name}
        db = DecisionDBManager(Path(self._tmp.name) / "guardian_decisions.db")
        db.save_debate_session("server-guardian", "t1", "task", [], {"approved": True})
        # A tool that is not in the catalog: re-validation at run time refuses it (git_commit_push cannot serve
        # here, because opening the database settles undecided rows of that retired tool as INVALID).
        self.action = db.add_action("t1", 1, "jev_mcp:no_such_tool", {"branch": "main", "message": "m"}, "r",
                                    "PROPOSED")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_codes(self) -> None:
        listed = self._run("--actions", "--json")
        self.assertEqual(json.loads(listed.stdout)["actions"][0]["id"], self.action)
        self.assertEqual(self._run("--show-action", "999", "--json").returncode, 3)
        self.assertEqual(self._run("--run-action", str(self.action), "--json").returncode, 2)  # --yes missing
        # A protected branch fails re-validation: the action becomes INVALID and nothing runs.
        run = self._run("--run-action", str(self.action), "--yes", "--json", "--actor", "dashboard@1.2.3.4")
        self.assertEqual(run.returncode, 4)
        self.assertIn("no longer passes validation", json.loads(run.stdout)["error"])
        self.assertEqual(self._run("--jev-recheck", "t1", "--json").returncode, 3)

    def test_plain_forms_use_the_same_codes_as_the_json_forms(self) -> None:
        self.assertEqual(self._run("--show-action", "999").returncode, 3)
        self.assertEqual(self._run("--reject-action", "999").returncode, 3)
        self.assertEqual(self._run("--run-action", "999", "--yes").returncode, 3)
        self.assertEqual(self._run("--jev-recheck", "t1").returncode, 3)
        self.assertEqual(self._run("--publish-thread", "t_missing", "--yes").returncode, 3)
        self.assertEqual(self._run("--fail-stale-action", "999").returncode, 3)
        # A protected branch fails re-validation: a state conflict, as in the JSON form.
        self.assertEqual(self._run("--run-action", str(self.action), "--yes").returncode, 4)
        # The failed run left the action INVALID, which can no longer be rejected.
        rejected = self._run("--reject-action", str(self.action))
        self.assertEqual(rejected.returncode, 4)
        self.assertIn("only PROPOSED or HELD", rejected.stderr)

    def test_actor_is_validated_once_for_every_form(self) -> None:
        plain = self._run("--reject-action", str(self.action), "--actor", "a b")
        self.assertEqual(plain.returncode, 2)
        self.assertIn("actor", plain.stderr)
        as_json = self._run("--reject-action", str(self.action), "--json", "--actor", "a b")
        self.assertEqual(as_json.returncode, 2)
        self.assertFalse(json.loads(as_json.stdout)["ok"])
        # Not only the action commands: every command that records an actor is behind the same check.
        self.assertEqual(self._run("--publish-thread", "t1", "--yes", "--actor", "x" * 65).returncode, 2)
        db = DecisionDBManager(Path(self._tmp.name) / "guardian_decisions.db")
        self.assertEqual(db.get_action(self.action)["status"], "PROPOSED")  # nothing ran for the bad actors
        self.assertEqual(self._run("--reject-action", str(self.action), "--actor", "ok@host:1.2").returncode, 0)
        self.assertEqual(db.get_action(self.action)["decided_by"], "ok@host:1.2")

    def test_publish_plan_codes(self) -> None:
        self.assertEqual(self._run("--publish-thread", "t_missing", "--plan", "--json").returncode, 3)
        refused = self._run("--publish-thread", "t1", "--plan", "--json")  # --yes is not needed
        self.assertEqual(refused.returncode, 4)
        self.assertFalse(json.loads(refused.stdout)["ok"])
        self.assertEqual(self._run("--publish-thread", "t1", "--plan", "--json", "--branch=main").returncode, 4)

    def test_fail_stale_action_codes(self) -> None:
        self.assertEqual(self._run("--fail-stale-action", "999", "--json").returncode, 3)
        self.assertEqual(self._run("--fail-stale-action", str(self.action), "--json").returncode, 4)  # not RUNNING
        db = DecisionDBManager(Path(self._tmp.name) / "guardian_decisions.db")
        db.update_action(self.action, "RUNNING", expected_status="PROPOSED")
        done = self._run("--fail-stale-action", str(self.action), "--json", "--actor", "op")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["action"]["status"], "FAILED")

    def test_action_of_an_unknown_project_is_refused(self) -> None:
        db = DecisionDBManager(Path(self._tmp.name) / "guardian_decisions.db")
        orphan = db.add_action("t9", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "r", "PROPOSED", project_id="gone")
        run = self._run("--run-action", str(orphan), "--yes", "--json", "--actor", "dashboard")
        self.assertEqual(run.returncode, 4)
        self.assertIn("no project 'gone'", json.loads(run.stdout)["error"])
        self.assertEqual(db.get_action(orphan)["status"], "PROPOSED")


if __name__ == "__main__":
    unittest.main()

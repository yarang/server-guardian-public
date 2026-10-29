"""Code-change actions skip the approval tab (action_ops.auto_run_actions); everything else stays manual."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import action_ops
import testenv
from db_manager import DecisionDBManager
from mcp_client import MCPClient

PATCH = "jev_mcp:apply_patch"
WRITE = "jev_mcp:execute_code_action"
TESTS = "jev_mcp:run_tests"
SHELL = "jev_mcp:server_remediation"


class AutoApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = DecisionDBManager(root / "d.db")
        self.mcp = MCPClient(workspace=root)
        self.db.save_debate_session("p", "t1", "task", [], {"approved": True})
        env = patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        for name in ("GUARDIAN_AUTO_APPLY", "GUARDIAN_UI_ACTIONS"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, tool: str, arguments: dict, status: str = "PROPOSED") -> int:
        return self.db.add_action("t1", 1, tool, arguments, "why", status)

    def run_auto(self, ids, results=None):
        results = results or {}
        calls = []

        def fake(tool, arguments, **_kw):
            calls.append(tool)
            return results.get(len(calls), {"status": "SUCCESS"})

        with patch.object(self.mcp, "call_tool", side_effect=fake), \
                patch.object(self.mcp, "validate", side_effect=lambda t, a, allowed=None: a):
            return action_ops.auto_run_actions(self.db, self.mcp, ids), calls

    def test_predicate_only_accepts_plain_file_changes(self) -> None:
        ok = lambda tool, **args: action_ops.is_auto_approvable({"tool": tool, "arguments": args})  # noqa: E731
        self.assertTrue(ok(PATCH, edits=[]))
        self.assertTrue(ok(WRITE, file_path="a.py", content="x"))
        self.assertTrue(ok(WRITE, file_path="a.py", content="x", run_test=False))
        self.assertFalse(ok(WRITE, file_path="a.py", content="x", run_test=True))
        self.assertFalse(ok(TESTS, test_cmd="go test"))
        self.assertFalse(ok(SHELL, command="ls"))

    def test_file_changes_run_as_the_engine_and_others_stay_proposed(self) -> None:
        patch_id = self.add(PATCH, {"edits": []})
        tests_id = self.add(TESTS, {"test_cmd": "pytest"})
        outcomes, calls = self.run_auto([patch_id, tests_id])
        self.assertEqual(calls, [PATCH])
        self.assertEqual([o["status"] for o in outcomes], ["EXECUTED"])
        done = self.db.get_action(patch_id)
        self.assertEqual((done["status"], done["decided_by"]), ("EXECUTED", action_ops.AUTO_ACTOR))
        self.assertTrue(action_ops.ACTOR_PATTERN.match(action_ops.AUTO_ACTOR))
        self.assertEqual(self.db.get_action(tests_id)["status"], "PROPOSED")

    def test_run_test_request_is_left_for_a_person(self) -> None:
        action_id = self.add(WRITE, {"file_path": "a.py", "content": "x", "run_test": True})
        outcomes, calls = self.run_auto([action_id])
        self.assertEqual((outcomes, calls), ([], []))
        self.assertEqual(self.db.get_action(action_id)["status"], "PROPOSED")

    def test_held_and_invalid_actions_never_run(self) -> None:
        held = self.add(PATCH, {"edits": []}, "HELD")
        invalid = self.add(PATCH, {"edits": []}, "INVALID")
        outcomes, calls = self.run_auto([held, invalid])
        self.assertEqual((outcomes, calls), ([], []))

    def test_chain_stops_at_the_first_failure_and_runs_in_id_order(self) -> None:
        first, second, third = (self.add(PATCH, {"edits": [i]}) for i in range(3))
        outcomes, calls = self.run_auto([third, second, first], {2: {"status": "FAILED", "error": "build broke"}})
        self.assertEqual([(o["id"], o["status"]) for o in outcomes], [(first, "EXECUTED"), (second, "FAILED")])
        self.assertEqual(outcomes[1]["error"], "build broke")
        self.assertEqual(self.db.get_action(third)["status"], "PROPOSED")

    def test_stale_workspace_stops_the_chain_and_keeps_the_action_proposed(self) -> None:
        first = self.add(PATCH, {"edits": []})
        with patch.object(action_ops, "stale_reason", return_value="workspace moved"):
            outcomes, calls = self.run_auto([first])
        self.assertEqual(calls, [])
        self.assertEqual([(o["status"], o["error"]) for o in outcomes], [("PROPOSED", f"action #{first}: workspace moved")])

    def test_switches_turn_it_off(self) -> None:
        action_id = self.add(PATCH, {"edits": []})
        for name, value in (("GUARDIAN_AUTO_APPLY", "off"), ("GUARDIAN_AUTO_APPLY", "0"), ("GUARDIAN_UI_ACTIONS", "OFF")):
            with patch.dict("os.environ", {name: value}):
                self.assertFalse(action_ops.auto_apply_enabled())
                outcomes, calls = self.run_auto([action_id])
            self.assertEqual((outcomes, calls), ([], []))
        self.assertTrue(action_ops.auto_apply_enabled({}))
        self.assertTrue(action_ops.auto_apply_enabled({"GUARDIAN_UI_ACTIONS": "on"}))
        self.assertEqual(self.db.get_action(action_id)["status"], "PROPOSED")

    def test_release_after_a_passing_recheck_applies_the_held_changes(self) -> None:
        held = self.add(PATCH, {"edits": []}, "HELD")
        manual = self.add(TESTS, {"test_cmd": "pytest"}, "HELD")
        released = action_ops.release_held_actions(self.db, "t1", 1)
        self.assertEqual(sorted(released), [held, manual])
        outcomes, calls = self.run_auto(released)
        self.assertEqual(calls, [PATCH])
        self.assertEqual(self.db.get_action(manual)["status"], "PROPOSED")


if __name__ == "__main__":
    unittest.main()

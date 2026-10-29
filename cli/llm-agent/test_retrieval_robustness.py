"""P2.1 retrieval_warning: a step that serves more than GUARDIAN_RETRIEVAL_WARN_BYTES emits an event, nothing more."""
import os
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

from debate_engine import DEFAULT_RETRIEVAL_WARN_BYTES, retrieval_warn_bytes
from repo_tools import RepoTools
from team_manager import Lineup, Member
from test_repo_mcp_engine import McpRunner
from test_retrieval_loop import READ_NOTES, EngineHarness, FakeRunner, block
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()  # the symbol index cache must not land in the real data directory


def tearDownModule() -> None:
    restore_data_dir()


class WarnThresholdTests(unittest.TestCase):
    def test_default_and_parsing(self) -> None:
        self.assertEqual(DEFAULT_RETRIEVAL_WARN_BYTES, 256 * 1024)
        for value, expected in ((None, DEFAULT_RETRIEVAL_WARN_BYTES), ("", DEFAULT_RETRIEVAL_WARN_BYTES),
                                ("abc", DEFAULT_RETRIEVAL_WARN_BYTES), ("-5", DEFAULT_RETRIEVAL_WARN_BYTES),
                                ("0", 0), (" 1234 ", 1234)):
            env = {} if value is None else {"GUARDIAN_RETRIEVAL_WARN_BYTES": value}
            with patch.dict(os.environ, env, clear=False):
                if value is None:
                    os.environ.pop("GUARDIAN_RETRIEVAL_WARN_BYTES", None)
                self.assertEqual(retrieval_warn_bytes(), expected, value)


class BlockStepWarningTests(EngineHarness):
    def warnings(self) -> List[Dict[str, Any]]:
        return [e for e in self.events() if e["type"] == "retrieval_warning"]

    def run_with(self, limit: str, requests: List[Dict[str, Any]]) -> FakeRunner:
        runner = FakeRunner(self.plain_scripts([block(*requests), "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_WARN_BYTES": limit}):
            self.run_debate(self.engine(), runner)
        return runner

    def test_a_step_over_the_threshold_emits_one_event_after_retrieval_served(self) -> None:
        self.run_with("50", [{"op": "read", "path": "apps/agent/notes.txt"}, {"op": "list"}])
        warnings = self.warnings()
        self.assertEqual(len(warnings), 1)
        data = warnings[0]["data"]
        self.assertEqual(set(data), {"step", "bytes", "threshold", "via"})
        served = next(e for e in self.events() if e["type"] == "retrieval_served")
        self.assertEqual((data["step"], data["via"], data["threshold"]), (1, "block", 50))
        self.assertEqual(data["bytes"], sum(r["bytes"] for r in served["data"]["results"]))
        self.assertGreater(data["bytes"], 50)
        types = [e["type"] for e in self.events()]
        self.assertEqual(types.index("retrieval_warning"), types.index("retrieval_served") + 1)
        self.assertEqual((warnings[0]["role"], warnings[0]["agent"]), (served["role"], served["agent"]))

    def test_the_bytes_of_the_whole_step_are_compared_not_of_one_result(self) -> None:
        one = RepoTools(self.repo, self.sha).run({"op": "read", "path": "apps/agent/notes.txt"})["bytes"]
        read = {"op": "read", "path": "apps/agent/notes.txt"}
        self.run_with(str(2 * one - 1), [read, read])  # each result is under the threshold, the step is over it
        self.assertEqual([w["data"]["bytes"] for w in self.warnings()], [2 * one])
        self.events_path.unlink()
        self.run_with(str(2 * one), [read, read])  # exactly the threshold: no warning
        self.assertEqual(self.warnings(), [])

    def test_equal_to_the_threshold_and_disabled_do_not_warn(self) -> None:
        self.run_with("1000000", [{"op": "list"}])
        self.assertEqual(self.warnings(), [])
        self.events_path.unlink()
        self.run_with("0", [{"op": "read", "path": "apps/agent/big.txt"}])
        self.assertEqual(self.warnings(), [])

    def test_warning_does_not_limit_anything(self) -> None:
        runner = self.run_with("10", [{"op": "read", "path": "apps/agent/notes.txt"}])
        self.assertEqual(len(self.warnings()), 1)
        second_prompt = runner.prompts("expert")[1]
        self.assertIn("apps/agent/notes.txt", second_prompt)
        self.assertFalse([e for e in self.events() if e["type"] == "retrieval_exhausted"])


class McpStepWarningTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        self._env = patch.dict(os.environ, {"GUARDIAN_REPO_MCP": "1", "GUARDIAN_RETRIEVAL_WARN_BYTES": "100"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        super().tearDown()

    def test_each_mcp_call_is_its_own_step(self) -> None:
        runner = McpRunner(self.plain_scripts(["FINAL"]), {"expert": [
            ("repo_read", {"path": "apps/agent/big.txt"}),   # large
            ("repo_read", {"path": "apps/agent/notes.txt"}),  # small
            ("repo_list", {"path": "apps/agent"}),
        ]})
        lineup = Lineup(id="t", name="T", expert=Member("domain_architect", ["claude"], "mcp"),
                        critics=[Member("critic_redteam", ["claude"], None)],
                        judge=Member("jev_mcp_decision", ["claude"], None))
        engine = self.engine()
        with patch.object(engine, "_resolve_lineup", return_value=lineup):
            self.run_debate(engine, runner)
        served = [e for e in self.events() if e["type"] == "retrieval_served" and e.get("role") == "Domain Expert"]
        warnings = [e for e in self.events() if e["type"] == "retrieval_warning"]
        over = [s for s in served if s["data"]["results"][0]["bytes"] > 100]
        self.assertGreaterEqual(len(over), 1)
        self.assertEqual([w["data"]["step"] for w in warnings], [s["data"]["step"] for s in over])
        for warning in warnings:
            self.assertEqual((warning["data"]["via"], warning["data"]["threshold"]), ("mcp", 100))
            self.assertGreater(warning["data"]["bytes"], 100)


if __name__ == "__main__":
    unittest.main()

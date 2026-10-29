"""Judge patch guidance and the one-time action repair ask (docs/design/debate-agent-retrieval.md, countermeasure 11)."""
import json
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from debate_engine import (
    ANSWER_LANGUAGE_RULE,
    answer_language_rule,
    detect_language,
    JUDGE_PATCH_READ_BLOCK,
    JUDGE_PATCH_READ_MCP,
    JUDGE_PATCH_RULES,
    JUDGE_VERDICT_RULE,
    judge_patch_guidance,
)
from llm_runner import LLMResult
from repo_mcp import RepoMcpServer
from repo_tools import RepoTools
from team_manager import Lineup, Member
from test_repo_mcp_engine import McpRunner
from test_retrieval_loop import EngineHarness, FakeRunner, block
from test_repo_tools import COMMIT, git
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # Keep the symbol index cache (and anything else) out of the real data directory.
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


TYPES_GO = (
    "package main\n\n// FleetStreamRequest is sent by the client.\n"
    "type FleetStreamRequest struct {\n\tServer string\n\tCount  int\n}\n\nfunc other() {}\n"
)
DUP_GO = 'package main\n\nfunc a() {\n\tlog("x")\n}\n\nfunc b() {\n\tlog("x")\n}\n'
STRUCT_OLD = "type FleetStreamRequest struct {\n\tServer string\n\tCount  int\n}"
STRUCT_NEW = "type FleetStreamRequest struct {\n\tServer string\n\tCount  int\n\tLimit  int\n}"


def edit(path: str, old: str, new: str) -> Dict[str, Any]:
    return {"tool": "jev_mcp:apply_patch", "arguments": {"edits": [{"file_path": path, "old": old, "new": new}]},
            "reason": "change"}


def answer(*actions: Dict[str, Any], verdict: bool = True) -> str:
    body = {"decision": "Add a limit.", "constraints": ["Limit is added."], "checks": [], "actions": list(actions)}
    return "Analysis.\n```json\n" + json.dumps(body) + "\n```\n" + ("VERDICT: APPROVED" if verdict else "VERDICT: REJECTED")


class RepairHarness(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "apps/agent/types.go").write_text(TYPES_GO)
        (self.repo / "apps/agent/dup.go").write_text(DUP_GO)
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-q", "-m", "types")
        self.sha = git(self.repo, "rev-parse", "HEAD")

    def run_judge(self, judge: List[Any]):
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": judge})
        return runner, self.run_debate(self.engine(), runner)

    def repair_events(self) -> List[Dict[str, Any]]:
        return [e for e in self.events() if e["type"] == "action_repair"]

    @staticmethod
    def statuses(result: Dict[str, Any]) -> List[str]:
        return [a["status"] for a in result["final_decision"]["actions"]]


READ_TYPES = block({"op": "read", "path": "apps/agent/types.go"})
READ_PART = block({"op": "read", "path": "apps/agent/types.go:1-3"})  # a narrow range: not the struct
READ_NOTES_FILE = block({"op": "read", "path": "apps/agent/notes.txt"})
READ_DUP = block({"op": "read", "path": "apps/agent/dup.go"})


class GuidanceTests(RepairHarness):
    def test_guidance_is_built_from_shared_constants(self) -> None:
        self.assertIn(JUDGE_PATCH_RULES, judge_patch_guidance(False))
        self.assertIn(JUDGE_PATCH_READ_BLOCK, judge_patch_guidance(False))
        self.assertIn(JUDGE_PATCH_RULES, judge_patch_guidance(True))
        self.assertIn(JUDGE_PATCH_READ_MCP, judge_patch_guidance(True))
        for phrase in ("enclosing declaration", "path#Symbol", "verbatim", "exactly once", "Never edit lines you have not been shown",
                       "read more first", "Change only what the task asks for", "do not add limits, caps",
                       "[Extra] decision"):
            self.assertIn(phrase, JUDGE_PATCH_RULES)

    def test_judge_prompt_carries_the_block_guidance_and_the_repair_ask_the_same_text(self) -> None:
        runner, _ = self.run_judge([READ_PART, answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW)), READ_TYPES,
                                    answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))])
        first, ask = runner.prompts("judge")[0], runner.prompts("judge")[2]
        self.assertIn(judge_patch_guidance(False), first)
        self.assertNotIn(JUDGE_PATCH_READ_MCP, first)
        self.assertIn(judge_patch_guidance(False), ask)


class LanguageRuleTests(RepairHarness):
    def test_every_role_and_the_repair_ask_carry_the_language_rule(self) -> None:
        runner, _ = self.run_judge([READ_PART, answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW)), READ_TYPES,
                                    answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))])
        for role in ("expert", "critic", "judge"):
            self.assertIn(ANSWER_LANGUAGE_RULE, runner.prompts(role)[0], role)
        # Follow-up messages repeat the rule, so a re-asked answer keeps the task's language.
        for prompt in runner.prompts("judge"):
            self.assertIn(ANSWER_LANGUAGE_RULE, prompt)

    def test_a_korean_task_names_korean_in_every_judge_message(self) -> None:
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": [READ_PART, answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW)), READ_TYPES,
                                       answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))]})
        self.run_debate(self.engine(), runner, task="동작하지 않는 API를 알려줘")
        prompts = runner.prompts("judge")
        self.assertGreaterEqual(len(prompts), 3)  # first prompt, repo results and the repair ask
        for prompt in prompts:
            self.assertIn("Write your whole reply in Korean", prompt)

    def test_the_rule_names_the_task_language(self) -> None:
        self.assertEqual(detect_language("코드를 검토해보고 알려줘"), "Korean")
        self.assertEqual(detect_language("コードを確認して"), "Japanese")
        self.assertEqual(detect_language("检查代码"), "Chinese")
        self.assertIsNone(detect_language("Review the code"))
        self.assertIsNone(detect_language("fix apps/agent/main.go"))
        rule = answer_language_rule("Korean")
        self.assertIn("Write your whole reply in Korean", rule)
        self.assertIn("not the language of these instructions or of any follow-up message", rule)
        self.assertEqual(answer_language_rule(None), ANSWER_LANGUAGE_RULE)


class RepairTests(RepairHarness):
    def test_not_shown_edit_is_repaired_after_the_judge_reads_the_struct(self) -> None:
        good = answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))
        runner, result = self.run_judge([READ_PART, answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW)), READ_TYPES, good])
        self.assertEqual(self.statuses(result), ["PROPOSED"])
        self.assertEqual(result["transcript"][2]["content"], good)
        ask = runner.prompts("judge")[2]
        self.assertIn("ACTION CHECK", ask)
        self.assertIn("actions[0] jev_mcp:apply_patch", ask)
        self.assertIn("the judge was not shown this code in apps/agent/types.go", ask)
        self.assertIn("COMPLETE final answer", ask)
        self.assertIn(JUDGE_VERDICT_RULE, ask)
        self.assertIn("Do not mention this correction", ask)
        sessions = [c["session_id"] for c in runner.calls if c["role"] == "judge"]
        self.assertIsNone(sessions[0])
        self.assertEqual(len(set(sessions[1:])), 1)  # the repair and the read stay in the judge's session
        events = self.repair_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["role"], "JEV-MCP Judge")
        self.assertEqual(events[0]["data"]["fixed"], 1)

    def test_non_unique_old_text_is_repaired(self) -> None:
        ambiguous = answer(edit("apps/agent/dup.go", '\tlog("x")\n', '\tlog("y")\n'))
        unique = answer(edit("apps/agent/dup.go", 'func b() {\n\tlog("x")\n', 'func b() {\n\tlog("y")\n'))
        runner, result = self.run_judge([READ_DUP, ambiguous, unique])
        self.assertEqual(self.statuses(result), ["PROPOSED"])
        self.assertIn("found 2 times", runner.prompts("judge")[2])
        data = self.repair_events()[0]["data"]
        self.assertIn("found 2 times", data["invalid"][0]["error"])
        self.assertEqual(data["fixed"], 1)

    def test_repair_that_loses_the_verdict_keeps_the_original(self) -> None:
        bad = answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))
        runner, result = self.run_judge([READ_PART, bad, "A rewritten analysis with no decision at all.", "Still nothing usable."])
        self.assertEqual(result["transcript"][2]["content"], bad)
        self.assertEqual(self.statuses(result), ["INVALID"])
        self.assertEqual(self.repair_events()[0]["data"]["fixed"], 0)
        self.assertEqual(len(self.repair_events()), 1)

    def test_unrepairable_error_is_not_retried(self) -> None:
        # An empty old text only creates new files: reading more code cannot fix that.
        runner, result = self.run_judge([READ_NOTES_FILE, answer(edit("apps/agent/notes.txt", "", "x"))])
        self.assertEqual(len(runner.prompts("judge")), 2)
        self.assertEqual(self.statuses(result), ["INVALID"])
        self.assertEqual(self.repair_events(), [])

    def test_a_rejected_verdict_is_not_checked(self) -> None:
        runner, _ = self.run_judge([READ_PART, answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW), verdict=False)])
        self.assertEqual(len(runner.prompts("judge")), 2)
        self.assertEqual(self.repair_events(), [])

    def test_repair_is_asked_once_and_remaining_invalid_actions_are_recorded(self) -> None:
        bad = answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))
        runner, result = self.run_judge([READ_PART, bad])  # the same bad answer every time
        self.assertEqual(len(runner.prompts("judge")), 3)
        self.assertEqual(self.statuses(result), ["INVALID"])
        self.assertEqual(self.repair_events()[0]["data"]["fixed"], 0)

    def test_event_shape_with_a_partly_fixed_answer(self) -> None:
        unknown = {"tool": "jev_mcp:no_such_tool", "arguments": {}, "reason": "x"}
        good = edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW)
        _, result = self.run_judge([READ_PART, answer(good, unknown), READ_TYPES, answer(good, unknown)])
        self.assertEqual(self.statuses(result), ["PROPOSED", "INVALID"])
        event = self.repair_events()[0]
        self.assertEqual(set(event["data"]), {"invalid", "fixed"})
        self.assertEqual([i["index"] for i in event["data"]["invalid"]], [0, 1])
        self.assertEqual([i["tool"] for i in event["data"]["invalid"]], ["jev_mcp:apply_patch", "jev_mcp:no_such_tool"])
        for item in event["data"]["invalid"]:
            self.assertEqual(set(item), {"index", "tool", "error"})
            self.assertIsInstance(item["error"], str)
        self.assertEqual(event["data"]["fixed"], 1)

    def test_no_repair_without_retrieval(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL": "0"}):
            runner, result = self.run_judge([answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))])
        self.assertEqual(len(runner.prompts("judge")), 1)
        self.assertEqual(self.repair_events(), [])


class RepairMcpRunner(McpRunner):
    """McpRunner that also makes MCP tool calls during the repair turn (a continuation call)."""

    def __init__(self, scripts: Dict[str, Any], repair_calls: List[Any], calls: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(scripts, calls or {})
        self.repair_calls = repair_calls

    def __call__(self, prompt, context=None, session_id=None, provider=None, providers=None, repo_mcp=None, **kw) -> LLMResult:
        if repo_mcp and "ACTION CHECK" in prompt:
            limits = repo_mcp["limits"]
            server = RepoMcpServer(RepoTools(Path(repo_mcp["workspace"]), repo_mcp["sha"]), repo_mcp["log"],
                                   max_steps=limits["steps"], max_step_bytes=limits["step_bytes"],
                                   max_turn_bytes=limits["turn_bytes"])
            for name, arguments in self.repair_calls:
                server.call_tool(name, arguments)
        return super().__call__(prompt, context, session_id, provider, providers, repo_mcp, **kw)


class McpRepairTests(RepairHarness):
    def setUp(self) -> None:
        super().setUp()
        self._env = patch.dict(os.environ, {"GUARDIAN_REPO_MCP": "1"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        super().tearDown()

    def test_reads_during_the_repair_turn_are_credited_to_the_judge(self) -> None:
        good = answer(edit("apps/agent/types.go", STRUCT_OLD, STRUCT_NEW))
        runner = RepairMcpRunner({"expert": ["plan"], "critic": ["critique"], "judge": [good, good]},
                                 [("repo_read", {"path": "apps/agent/types.go"})],
                                 {"judge": [("repo_read", {"path": "apps/agent/types.go:1-3"})]})
        lineup = Lineup(id="t", name="T", expert=Member("domain_architect", ["claude"], None),
                        critics=[Member("critic_redteam", ["claude"], None)],
                        judge=Member("jev_mcp_decision", ["claude"], "mcp"))
        engine = self.engine()
        with patch.object(engine, "_resolve_lineup", return_value=lineup):
            result = self.run_debate(engine, runner)
        self.assertEqual(self.statuses(result), ["PROPOSED"])
        self.assertIn("apps/agent/types.go", engine._pack.whole)
        ask = runner.prompts("judge")[1]
        self.assertIn(judge_patch_guidance(True), ask)
        self.assertIn(judge_patch_guidance(True), runner.system_texts("judge")[0])  # claude: standing rule
        data = self.repair_events()[0]["data"]
        self.assertEqual((len(data["invalid"]), data["fixed"]), (1, 1))
        served = [e for e in self.events() if e["type"] == "retrieval_served" and e["data"]["via"] == "mcp"]
        self.assertEqual(len(served), 2)  # the narrow first read and the full read of the repair turn


if __name__ == "__main__":
    unittest.main()

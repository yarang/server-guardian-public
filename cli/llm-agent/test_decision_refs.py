"""Requirement ids (refs) in the judge's decision block: parsing, validation, final_decision and the report."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import debate_report as dr
from debate_engine import JUDGE_JSON_EXAMPLE, JUDGE_REQUIREMENT_RULE, DebateEngine
from jev_gate import PASSED
from llm_runner import LLMResult
from mcp_client import (MCPClient, clean_refs, parse_action_refs, parse_actions, parse_check_refs, parse_checks,
                        parse_constraint_refs, parse_constraints, parse_rationale)


def block(data: dict) -> str:
    return "```json\n" + json.dumps(data) + "\n```\nVERDICT: APPROVED"


MIXED = block({
    "constraints": ["plain one", {"text": "  with   refs ", "refs": ["R1", "R3", "R1", "X9", "r2", 5, "R123"]},
                    {"refs": ["R1"]}, {"text": "", "refs": ["R1"]}, 7, {"text": "no refs"}],
    "checks": [{"text": "c1", "refs": ["R2"]}, "c2"],
    "actions": [{"tool": "t", "arguments": {}, "refs": ["R2", "bad"]}, {"tool": "u", "arguments": {}}, 3],
    "rationale": "para one\n\n\npara   two\nline",
})


class ParseTests(unittest.TestCase):
    def test_mixed_strings_and_objects(self) -> None:
        self.assertEqual(parse_constraints(MIXED), ["plain one", "with refs", "no refs"])
        self.assertEqual(parse_checks(MIXED), ["c1", "c2"])

    def test_refs_aligned_and_invalid_dropped(self) -> None:
        self.assertEqual(parse_constraint_refs(MIXED), [[], ["R1", "R3"], []])
        self.assertEqual(parse_check_refs(MIXED), [["R2"], []])
        self.assertEqual(len(parse_constraint_refs(MIXED)), len(parse_constraints(MIXED)))

    def test_limits_keep_lists_aligned(self) -> None:
        text = block({"constraints": [{"text": f"c{i}", "refs": ["R1"]} for i in range(15)]})
        self.assertEqual(len(parse_constraints(text)), 10)
        self.assertEqual(len(parse_constraint_refs(text)), 10)

    def test_action_refs_are_index_aligned_and_stripped(self) -> None:
        self.assertEqual(parse_action_refs(MIXED), [["R2"], []])
        actions = parse_actions(MIXED)
        self.assertEqual(actions, [{"tool": "t", "arguments": {}}, {"tool": "u", "arguments": {}}])

    def test_rationale_keeps_paragraphs_only_on_request(self) -> None:
        self.assertEqual(parse_rationale(MIXED), "para one para two line")
        self.assertEqual(parse_rationale(MIXED, 4000, keep_paragraphs=True), "para one\n\npara two\nline")

    def test_clean_refs(self) -> None:
        self.assertEqual(clean_refs(["R1", "R01", "R100", "R1"]), ["R1", "R01"])
        self.assertEqual(clean_refs("R1"), [])

    def test_prompt_mentions_ids_and_refs(self) -> None:
        self.assertIn("[R1]", JUDGE_REQUIREMENT_RULE)
        self.assertIn("refs", JUDGE_JSON_EXAMPLE)


class FakeGate:
    def review(self, request):
        return {"gate": PASSED, "reasons": [], "jev_decision_id": 7, "eval_status": "done", "evaluation": {}}


class EngineTests(unittest.TestCase):
    JUDGE = block({
        "decision": "[R1] Timeout: set 5s.\n\n[R2] Retry: add one retry.\n\n[Extra] note",
        "rationale": "Evidence a.py:3.\n\nAlternative rejected.",
        "constraints": ["plain", {"text": "timeout is 5s", "refs": ["R1"]}],
        "checks": [{"text": "retry exists", "refs": ["R2", "bad"]}],
        "actions": [
            {"tool": "jev_mcp:run_tests", "arguments": {"test_cmd": "pytest"}, "reason": "verify", "refs": ["R1", "R2"]},
            {"tool": "jev_mcp:execute_code_action", "arguments": {"file_path": "src/a.py", "content": "x = 1\n"},
             "reason": "new file"},
        ],
    })

    def run_debate(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = DebateEngine(max_rounds=1, db_path=Path(tmp.name) / "d.db", jev_gate=FakeGate())
        engine.mcp = MCPClient(workspace=Path(tmp.name))
        replies = iter([LLMResult("claude-cli", "proposal", "s1"), LLMResult("claude-cli", "critique", "s2"),
                        LLMResult("claude-cli", self.JUDGE, "s3")])
        with patch.object(engine.llm, "run_session_prompt", side_effect=lambda *a, **k: next(replies)):
            with patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                engine.db.save_debate_session(*a[:4], decision=a[4]) or Path(tmp.name) / "log.md"
            )):
                result = engine.run_debate("Harden", project_id="server-guardian")
        return engine, result

    def test_final_decision_carries_refs(self) -> None:
        engine, result = self.run_debate()
        fd = result["final_decision"]
        self.assertTrue(fd["approved"])
        self.assertEqual(fd["rationale"], "Evidence a.py:3.\n\nAlternative rejected.")
        self.assertEqual(fd["constraints"], ["plain", "timeout is 5s"])
        self.assertEqual(fd["constraint_refs"], [[], ["R1"]])
        self.assertEqual(fd["checks"], ["retry exists"])
        self.assertEqual(fd["check_refs"], [["R2"]])
        first, second = fd["actions"]
        self.assertEqual((first["status"], first["refs"]), ("PROPOSED", ["R1", "R2"]))
        self.assertNotIn("file_path", first)
        self.assertEqual((second["refs"], second["file_path"]), ([], "src/a.py"))
        self.assertEqual(len(fd["constraints"]), len(fd["constraint_refs"]))
        # refs never reach the stored tool input
        for action in engine.db.list_actions(result["thread_id"]):
            self.assertNotIn("refs", action["arguments"])
        # and the decision DB keeps the trace
        stored = engine.db.get_thread_history(result["thread_id"])["decision"]
        self.assertEqual(stored["rationale"], fd["rationale"])
        self.assertEqual(stored["constraint_refs"], [[], ["R1"]])
        self.assertEqual(stored["check_refs"], [["R2"]])

    def test_actions_with_refs_pass_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            mcp = MCPClient(workspace=Path(tmp))
            from action_ops import invalid_proposals
            self.assertEqual(invalid_proposals(mcp, parse_actions(self.JUDGE), None, None, None), [])


class ReportTests(unittest.TestCase):
    DECISION = {
        "approved": True, "final_round": 1,
        "decision": "[R1] Timeout: set 5s.\n\n[R2] Retry: add one retry.\n\n[R3] Docs: update",
        "rationale": "Evidence.\n\nAlternative.",
        "constraints": ["plain", "timeout is 5s"], "constraint_refs": [[], ["R1", "R3"]],
        "checks": ["retry exists"], "check_refs": [["R2"]],
        "actions": [{"id": 1, "tool": "jev_mcp:apply_patch", "status": "PROPOSED", "error": None,
                     "refs": ["R1"], "file_path": "a.py"},
                    {"id": 2, "tool": "jev_mcp:run_tests", "status": "PROPOSED", "error": None, "refs": []}],
    }

    def lines(self, decision):
        return "\n".join(dr._summary_lines(decision, "APPROVED"))

    def test_sections(self) -> None:
        md = self.lines(self.DECISION)
        self.assertIn("### 근거\n\nEvidence.\n\nAlternative.", md)
        self.assertIn("### 제약\n\n- plain\n- timeout is 5s (R1, R3)", md)
        self.assertIn("### 확인 항목\n\n- retry exists (R2)", md)
        self.assertIn("| # | 도구 | 상태 | 비고 | 요구사항 |", md)
        self.assertIn("| 1 | jev_mcp:apply_patch | PROPOSED | - | R1 |", md)

    def test_trace_table(self) -> None:
        md = self.lines(self.DECISION)
        self.assertIn("### 추적", md)
        self.assertIn("| R1 | Timeout | 1 | 0 | a.py |", md)
        self.assertIn("| R2 | Retry | 0 | 1 | 변경 없음 |", md)
        self.assertIn("| R3 | Docs | 1 | 0 | 변경 없음 |", md)

    def test_old_decision_has_no_new_sections(self) -> None:
        old = {"approved": True, "decision": "Do X.", "checks": ["c"],
               "actions": [{"id": 3, "tool": "t", "status": "s", "error": None}]}
        md = self.lines(old)
        for heading in ("### 근거", "### 제약", "### 추적", "요구사항 |"):
            self.assertNotIn(heading, md)
        self.assertIn("- c", md)


if __name__ == "__main__":
    unittest.main()

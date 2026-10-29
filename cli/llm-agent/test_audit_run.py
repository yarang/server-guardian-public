"""Audit mode A2: the shard prompt, the deterministic coverage and finding checks, the run (resume, deferral, re-ask),
the report and the CLI commands. The model is a FakeRunner; nothing here touches the real data dir."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import audit_ops
import audit_run
import repo_audit
from audit_run import AuditRunner, RunError
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from project_registry import Project, ProjectManager
from repo_audit import AuditStore, build_plan, build_report
from repo_tools import RepoTools
from test_repo_audit import QUESTION, git, make_repo
from test_retrieval_loop import FakeRunner, block
from testenv import isolate_data_dir, restore_data_dir

FILES = ["apps/agent/main.go", "apps/agent/notes.txt", "cli/tool.py", "conf/app.yaml"]
READ_ALL = block(*[{"op": "read", "path": p} for p in FILES])
# cli/tool.py line 2 is "    return 1"; conf/app.yaml line 2 is "port: 9870"
GOOD = {"path": "cli/tool.py", "line": 2, "quote": "return 1", "claim": "returns a constant", "severity": "low"}
OTHER = {"path": "conf/app.yaml", "line": 2, "quote": "port: 9870", "claim": "fixed port", "severity": "high"}
INVENTED = {"path": "cli/tool.py", "line": 2, "quote": "return secret_token", "claim": "made up", "severity": "high"}


def findings(*items: Dict[str, Any]) -> str:
    return "Checked everything.\n```findings\n" + json.dumps(list(items)) + "\n```"


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # The A2 tests check the shard flow without the precision step; PrecisionTests switch it back on.
    os.environ["GUARDIAN_AUDIT_RECHECK"] = "off"
    os.environ["GUARDIAN_AUDIT_MIN_SEVERITY"] = "low"
    isolate_data_dir()


def tearDownModule() -> None:
    os.environ.pop("GUARDIAN_AUDIT_RECHECK", None)
    os.environ.pop("GUARDIAN_AUDIT_MIN_SEVERITY", None)
    restore_data_dir()


class RunCase(unittest.TestCase):
    shard_limit: Optional[int] = None

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.project = Project(id="audittest", workspace_raw=str(self.repo))
        patcher = patch.object(ProjectManager, "load", return_value=self.project)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = AuditStore(self.root / "audits")
        plan = build_plan(RepoTools(self.repo, self.sha), "audittest", self.sha, QUESTION, shard_limit=self.shard_limit)
        self.audit = self.store.create(plan)
        self.events_path = self.root / "events.jsonl"
        self.db_path = self.root / "d.db"

    def runner(self, **kwargs: Any) -> AuditRunner:
        return AuditRunner("audittest", self.audit, events=EventWriter(str(self.events_path)),
                           db_path=self.db_path, audit_dir=self.root / "audits", **kwargs)

    def run_audit(self, fake: Any, runner: Optional[AuditRunner] = None, **run: Any) -> Dict[str, Any]:
        runner = runner or self.runner()
        with patch.object(runner.engine.llm, "run_session_prompt", side_effect=fake):
            try:
                return runner.run(**run)
            finally:
                runner.events.close()

    def events(self, kind: str) -> List[Dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows = [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()]
        return [e["data"] for e in rows if e["type"] == kind]

    @staticmethod
    def expert(*replies: str) -> FakeRunner:
        return FakeRunner({"expert": list(replies), "critic": ["c"], "judge": ["j"]})


# --- pure parts ----------------------------------------------------------------------------------------------------

class PureTests(RunCase):
    def test_prompt_names_question_files_units_and_format(self) -> None:
        plan = self.store.load_plan(self.audit)
        prompt = audit_run.build_shard_prompt(plan, plan["shards"][0], "Write in Korean.")
        self.assertIn(QUESTION, prompt)
        for path in FILES:
            self.assertIn(path, prompt)
        self.assertIn("function Serve: lines 3-5", prompt.replace("func ", "function "))
        self.assertIn("```findings", prompt)
        self.assertIn("Write in Korean.", prompt)
        self.assertNotIn("TOKEN=secret", prompt)

    def test_unseen_units_needs_every_line_shown(self) -> None:
        units = [{"id": "a:1-10", "path": "a.py", "start": 1, "end": 10},
                 {"id": "b:1-4", "path": "b.py", "start": 1, "end": 4},
                 {"id": "c:1-4", "path": "c.py", "start": 1, "end": 4}]
        spans = {"a.py": [(1, 9)], "b.py": [(1, 1_000_000_000)], "c.py": []}
        missed = audit_run.unseen_units(units, lambda p: spans[p], set())
        self.assertEqual([u["id"] for u in missed], ["a:1-10", "c:1-4"])
        self.assertEqual(audit_run.unseen_units(units, lambda p: spans[p], {"a.py", "c.py"}), [])

    def test_unit_of_picks_the_smallest_enclosing_unit(self) -> None:
        units = [{"id": "x:1-20", "path": "x.py", "start": 1, "end": 20},
                 {"id": "x:5-8", "path": "x.py", "start": 5, "end": 8}]
        self.assertEqual(audit_run.unit_of(units, "x.py", 6), "x:5-8")
        self.assertEqual(audit_run.unit_of(units, "x.py", 15), "x:1-20")
        self.assertIsNone(audit_run.unit_of(units, "x.py", 99))
        self.assertIsNone(audit_run.unit_of(units, "y.py", 6))

    def test_rate_limit_words(self) -> None:
        for text in ("claude: usage limit reached, resets at 5pm", "HTTP 429 Too Many Requests", "rate_limit_error"):
            self.assertTrue(audit_run.is_rate_limited(text), text)
        for text in ("claude: not installed", "", None):
            self.assertFalse(audit_run.is_rate_limited(text), text)

    def test_step_limit_is_set_and_restored(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_AUDIT_STEP_BYTES": "1000"}, clear=False):
            os.environ.pop("GUARDIAN_RETRIEVAL_STEP_BYTES", None)
            with audit_run.step_limits():
                self.assertEqual(os.environ["GUARDIAN_RETRIEVAL_STEP_BYTES"], "1000")
            self.assertNotIn("GUARDIAN_RETRIEVAL_STEP_BYTES", os.environ)


# --- one shard -----------------------------------------------------------------------------------------------------

class ShardTests(RunCase):
    def test_a_complete_shard_stores_verified_findings_and_a_report(self) -> None:
        result = self.run_audit(self.expert(READ_ALL, findings(GOOD, OTHER)))
        self.assertEqual((result["state"], result["done"], result["pending"]), ("done", 1, 0))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual((record["state"], record["coverage"], record["unseen"]), ("done", "complete", []))
        self.assertEqual([(f["path"], f["line"], f["verified"]) for f in record["findings"]],
                         [("cli/tool.py", 2, True), ("conf/app.yaml", 2, True)])
        self.assertIn("return 1", record["findings"][0]["quote"])
        self.assertTrue(record["findings"][0]["unit"].startswith("cli/tool.py:"))
        report = self.store.load_report(self.audit)
        self.assertEqual(report["state"], "done")
        self.assertEqual(report["totals"]["findings"], 2)
        self.assertEqual(report["totals"]["units_unseen"], 0)
        self.assertEqual(self.store.meta(self.audit)["state"], "done")
        self.assertEqual(len(self.events("audit_shard_finished")), 1)

    def test_a_finding_with_a_quote_that_is_not_there_is_kept_as_unverified(self) -> None:
        self.run_audit(self.expert(READ_ALL, findings(GOOD, INVENTED)))
        report = self.store.load_report(self.audit)
        self.assertEqual([f["path"] for f in report["findings"]], ["cli/tool.py"])
        self.assertEqual(len(report["unverified"]), 1)
        self.assertIn("not found", report["unverified"][0]["reason"])

    def test_a_finding_on_a_line_that_was_never_shown_is_unverified(self) -> None:
        only_tool = block({"op": "read", "path": "cli/tool.py"})
        self.run_audit(self.expert(only_tool, findings(OTHER), findings(OTHER)))
        report = self.store.load_report(self.audit)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["unverified"][0]["reason"], "line was not shown to the session")

    def test_units_not_shown_are_asked_once_and_then_reported_unseen(self) -> None:
        only_tool = block({"op": "read", "path": "cli/tool.py"})
        fake = self.expert(only_tool, findings(), findings())
        self.run_audit(fake)
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual(record["coverage"], "incomplete")
        self.assertTrue(record["unseen"])
        self.assertFalse(any(u.startswith("cli/tool.py") for u in record["unseen"]))
        self.assertEqual(sum("AUDIT CHECK" in p for p in fake.prompts("expert")), 1)  # asked once, not forever
        report = self.store.load_report(self.audit)
        self.assertEqual(report["totals"]["units_unseen"], len(record["unseen"]))
        self.assertTrue(all(u["path"] != "cli/tool.py" for u in report["unseen"]))

    def test_the_reask_lets_the_session_read_what_it_missed(self) -> None:
        only_tool = block({"op": "read", "path": "cli/tool.py"})
        rest = block(*[{"op": "read", "path": p} for p in FILES if p != "cli/tool.py"])
        self.run_audit(self.expert(only_tool, findings(), rest, findings(OTHER)))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual((record["coverage"], record["unseen"]), ("complete", []))
        self.assertTrue(record["findings"][0]["verified"])

    def test_an_answer_without_a_findings_block_is_asked_again(self) -> None:
        fake = self.expert(READ_ALL, "I looked at everything, all fine.", findings(GOOD))
        self.run_audit(fake)
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual((record["state"], len(record["findings"])), ("done", 1))

    def test_no_findings_block_after_the_reask_fails_the_shard(self) -> None:
        result = self.run_audit(self.expert(READ_ALL, "all fine", "really, all fine"))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual(record["state"], "failed")
        self.assertTrue(record["error"].startswith("bad_format"))
        self.assertEqual((result["state"], result["failed"], result["pending"]), ("incomplete", 1, 1))
        self.assertEqual(self.store.load_report(self.audit)["unaudited"][0]["reason"], "failed")

    def test_an_empty_list_is_an_answer(self) -> None:
        self.run_audit(self.expert(READ_ALL, findings()))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual((record["state"], record["findings"], record["coverage"]), ("done", [], "complete"))

    def test_the_audit_never_creates_actions(self) -> None:
        action = ('```json\n{"actions": [{"tool": "apply_patch", "path": "cli/tool.py", "old": "return 1", '
                  '"new": "return 3"}]}\n```')
        self.run_audit(self.expert(READ_ALL, findings(GOOD) + "\n" + action))
        self.assertEqual(self.store.load_shard(self.audit, "s0001")["state"], "done")
        with sqlite3.connect(self.db_path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("decision_actions", tables)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_actions").fetchone()[0], 0)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # and the workspace is untouched


# --- several shards: deferral, resume, failure ---------------------------------------------------------------------

class MultiShardTests(RunCase):
    shard_limit = 40

    def reading(self, shard: Dict[str, Any]) -> str:
        return block(*[{"op": "read", "path": f["path"]} for f in shard["files"]])

    def test_the_plan_has_several_shards(self) -> None:
        self.assertGreaterEqual(len(self.store.load_plan(self.audit)["shards"]), 3)

    def script(self, quota_on: Optional[int] = None, calls: Optional[List[str]] = None) -> Any:
        """Reads the files named in each shard task, then answers with no findings; the `quota_on`-th shard session
        (1-based) gets the provider's quota failure instead."""
        plan = self.store.load_plan(self.audit)
        order = {"n": 0}

        def fake(prompt: str, context: Any = None, session_id: Any = None, **kwargs: Any) -> LLMResult:
            if session_id:  # the continuation after the repo block
                return LLMResult("claude-cli", findings(), session_id, resumed=True)
            order["n"] += 1
            part = next(s for s in plan["shards"] if f"part {s['id']} " in prompt)
            if calls is not None:
                calls.append(part["id"])
            if quota_on == order["n"]:
                return LLMResult(BUILTIN_PROVIDER, "canned", failures=["claude: usage limit reached, resets at 5pm"])
            return LLMResult("claude-cli", self.reading(part), f"expert-s{order['n']}")
        return fake

    def test_a_quota_failure_defers_the_shard_stops_the_run_and_resumes_later(self) -> None:
        calls: List[str] = []
        first = self.run_audit(self.script(quota_on=2, calls=calls))
        shards = self.store.load_plan(self.audit)["shards"]
        self.assertEqual((first["state"], first["done"], first["deferred"]), ("paused", 1, 1))
        self.assertEqual(calls, ["s0001", "s0002"])  # the third shard was not tried
        self.assertEqual(self.store.load_shard(self.audit, "s0002")["state"], "deferred")
        self.assertEqual([s["id"] for s in self.store.pending(self.audit)], [s["id"] for s in shards[1:]])
        self.assertEqual(self.store.meta(self.audit)["state"], "paused")
        report = self.store.load_report(self.audit)
        self.assertEqual(report["unaudited"][0]["reason"], "deferred")
        self.assertEqual(report["totals"]["shards_by_state"]["pending"], len(shards) - 2)

        calls.clear()
        second = self.run_audit(self.script(calls=calls))
        self.assertEqual(second["state"], "done")
        self.assertNotIn("s0001", calls)  # the finished shard is not run again
        self.assertEqual(self.store.pending(self.audit), [])
        self.assertEqual(self.store.load_report(self.audit)["totals"]["units_unaudited"], 0)

    def test_a_failed_shard_does_not_stop_the_others(self) -> None:
        def fake(prompt: str, context: Any = None, session_id: Any = None, **kwargs: Any) -> LLMResult:
            if session_id:
                return LLMResult("claude-cli", findings(), session_id, resumed=True)
            if "part s0001 " in prompt:
                return LLMResult(BUILTIN_PROVIDER, "canned", failures=["claude: not installed"])
            part = next(s for s in self.store.load_plan(self.audit)["shards"] if f"part {s['id']} " in prompt)
            return LLMResult("claude-cli", self.reading(part), "expert-s9")
        result = self.run_audit(fake)
        self.assertEqual((result["state"], result["failed"], result["done"]), ("incomplete", 1, result["shards_run"] - 1))
        self.assertTrue(self.store.load_shard(self.audit, "s0001")["error"].startswith("llm_fallback"))

    def test_max_shards_pauses_the_audit(self) -> None:
        result = self.run_audit(self.script(), max_shards=1)
        self.assertEqual((result["state"], result["shards_run"]), ("paused", 1))

    def test_a_changed_key_makes_a_finished_shard_stale(self) -> None:
        self.run_audit(self.script())
        record = self.store.load_shard(self.audit, "s0001")
        self.store.save_shard(self.audit, "s0001", {**record, "key": "0" * 24})
        self.assertEqual([s["id"] for s in self.store.pending(self.audit)], ["s0001"])
        report = build_report(self.store.load_plan(self.audit), self.store.records(self.audit), "paused")
        self.assertEqual(report["unaudited"][0]["reason"], "stale")



# --- precision step: severity floor and re-check -------------------------------------------------------------------

MEDIUM = {**GOOD, "severity": "medium", "scenario": "callers always get 1"}


def verdicts(*items: Dict[str, Any]) -> str:
    return "Checked.\n```verdicts\n" + json.dumps(list(items)) + "\n```"


class PrecisionTests(RunCase):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"GUARDIAN_AUDIT_RECHECK": "on", "GUARDIAN_AUDIT_MIN_SEVERITY": "medium"})
        env.start()
        self.addCleanup(env.stop)
        self.calls: List[Dict[str, Any]] = []

    def fake(self, answer: str, recheck: Any) -> Any:
        """The shard session reads everything and answers `answer`; the re-check session gets `recheck(prompt)`."""
        def run(prompt: str, context: Any = None, session_id: Any = None, **kwargs: Any) -> LLMResult:
            kind = "recheck" if "Re-check of audit findings" in prompt or (session_id or "").startswith("recheck") \
                else "shard"
            self.calls.append({"kind": kind, "prompt": prompt, "instructions": kwargs.get("instructions")})
            if kind == "recheck":
                return recheck(prompt)
            if session_id:
                return LLMResult("claude-cli", answer, session_id, resumed=True)
            return LLMResult("claude-cli", READ_ALL, "expert-s1")
        return run

    def prompts(self, kind: str) -> List[str]:
        return [c["prompt"] for c in self.calls if c["kind"] == kind]

    def test_the_shard_prompt_carries_the_precision_rules_and_the_scenario_field(self) -> None:
        plan = self.store.load_plan(self.audit)
        prompt = audit_run.build_shard_prompt(plan, plan["shards"][0], "Write in Korean.")
        self.assertIn("states as intended", prompt)
        self.assertIn("hardening suggestions", prompt)
        self.assertIn('"scenario"', prompt)

    def test_confirmed_findings_stay_and_rejected_ones_become_notes(self) -> None:
        answer = verdicts({"id": 1, "verdict": "rejected", "reason": "documented default"},
                          {"id": 2, "verdict": "confirmed", "reason": "port clash on every host"})
        self.run_audit(self.fake(findings(MEDIUM, OTHER),
                                 lambda _p: LLMResult("claude-cli", answer, "recheck-s1")))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual([(f["path"], f["recheck"]) for f in record["findings"]], [("conf/app.yaml", "confirmed")])
        self.assertEqual([(n["path"], n["recheck"], n["recheck_reason"]) for n in record["notes"]],
                         [("cli/tool.py", "rejected", "documented default")])
        self.assertEqual(record["notes"][0]["scenario"], "callers always get 1")
        self.assertEqual((record["recheck"]["state"], record["recheck"]["confirmed"], record["recheck"]["rejected"]),
                         ("done", 1, 1))
        report = self.store.load_report(self.audit)
        self.assertEqual((report["totals"]["findings"], report["totals"]["notes"]), (1, 1))
        self.assertEqual(report["notes"][0]["shard"], "s0001")
        prompt = self.prompts("recheck")[0]
        self.assertIn("### Finding 1: cli/tool.py:2 (severity medium)", prompt)
        self.assertIn("    2      return 1", prompt)  # the unit's numbered code
        self.assertIn("Auditor's scenario: callers always get 1", prompt)
        self.assertIn(QUESTION, prompt)
        instructions = [c["instructions"] for c in self.calls if c["kind"] == "recheck"][0]
        self.assertIn("reviewer of code audit findings", instructions.text())
        shard_instructions = [c["instructions"] for c in self.calls if c["kind"] == "shard"][0]
        self.assertIn("code auditor", shard_instructions.text())  # the auditor's system prompt is restored

    def test_a_finding_without_a_verdict_is_not_confirmed(self) -> None:
        answer = verdicts({"id": 2, "verdict": "confirmed", "reason": "x"}, {"id": "1", "verdict": "confirmed"})
        self.run_audit(self.fake(findings(MEDIUM, OTHER), lambda _p: LLMResult("claude-cli", answer, "recheck-s1")))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual([f["path"] for f in record["findings"]], ["conf/app.yaml"])
        self.assertEqual((record["notes"][0]["recheck"], record["notes"][0]["note"]),
                         ("no verdict", "the re-check gave no verdict"))

    def test_findings_below_the_floor_become_notes_without_a_recheck(self) -> None:
        self.run_audit(self.fake(findings(GOOD), lambda _p: self.fail("no re-check expected")))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual(record["findings"], [])
        self.assertEqual(record["notes"][0]["note"], "below the severity floor (medium)")
        self.assertEqual(record["recheck"], {"state": "skipped", "candidates": 0})
        self.assertEqual(self.prompts("recheck"), [])

    def test_unverified_findings_stay_unverified_and_are_not_rechecked(self) -> None:
        answer = verdicts({"id": 1, "verdict": "confirmed", "reason": "x"})
        self.run_audit(self.fake(findings(OTHER, INVENTED), lambda _p: LLMResult("claude-cli", answer, "recheck-s1")))
        report = self.store.load_report(self.audit)
        self.assertEqual([f["path"] for f in report["findings"]], ["conf/app.yaml"])
        self.assertEqual(len(report["unverified"]), 1)
        self.assertNotIn("Finding 2", self.prompts("recheck")[0])

    def test_a_quota_failure_of_the_recheck_defers_the_shard(self) -> None:
        quota = lambda _p: LLMResult(BUILTIN_PROVIDER, "canned", failures=["claude: usage limit reached"])  # noqa: E731
        result = self.run_audit(self.fake(findings(OTHER), quota))
        self.assertEqual(result["state"], "paused")
        self.assertEqual(self.store.load_shard(self.audit, "s0001")["state"], "deferred")

    def test_a_recheck_without_a_verdicts_block_moves_the_candidates_to_notes(self) -> None:
        self.run_audit(self.fake(findings(OTHER), lambda _p: LLMResult("claude-cli", "looks right", "recheck-s1")))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual((record["state"], record["findings"]), ("done", []))
        self.assertEqual(record["recheck"]["state"], "failed")
        self.assertIn("no verdicts block", record["notes"][0]["note"])

    def test_the_recheck_can_be_switched_off(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_AUDIT_RECHECK": "off"}):
            self.run_audit(self.fake(findings(OTHER), lambda _p: self.fail("no re-check expected")))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual([f["path"] for f in record["findings"]], ["conf/app.yaml"])
        self.assertEqual(record["recheck"]["state"], "off")

    def test_parse_verdicts(self) -> None:
        self.assertIsNone(audit_run.parse_verdicts("no block"))
        self.assertEqual(audit_run.parse_verdicts("```verdicts\nnot json\n```"), {})
        parsed = audit_run.parse_verdicts(verdicts(
            {"id": 1, "verdict": "confirmed", "reason": "token=abc123"}, {"id": 1, "verdict": "rejected"},
            {"id": True, "verdict": "confirmed"}, {"id": 3, "verdict": "maybe"}, "x"))
        self.assertEqual(parsed, {1: ("confirmed", "token=***")})

    def test_floor_setting(self) -> None:
        for raw, floor in (("", "medium"), ("HIGH", "high"), ("low", "low"), ("bogus", "medium")):
            with patch.dict(os.environ, {"GUARDIAN_AUDIT_MIN_SEVERITY": raw}):
                self.assertEqual(audit_run.audit_min_severity(), floor)
        self.assertTrue(audit_run.below_floor("low", "medium"))
        self.assertFalse(audit_run.below_floor("high", "medium"))

    def test_unit_excerpt_is_capped_around_the_line(self) -> None:
        class Source:
            @staticmethod
            def source_text(_path: str) -> str:
                return "\n".join(f"line {n}" for n in range(1, 501))
        units = {"big": {"id": "big", "path": "a.py", "start": 10, "end": 400}}
        excerpt = audit_run.unit_excerpt(Source(), units, {"path": "a.py", "line": 300, "unit": "big"})
        numbers = [int(row.split()[0]) for row in excerpt.splitlines()]
        self.assertEqual(len(numbers), audit_run.RECHECK_EXCERPT_LINES)
        self.assertIn(300, numbers)
        small = audit_run.unit_excerpt(Source(), {"s": {"id": "s", "path": "a.py", "start": 3, "end": 4}},
                                       {"path": "a.py", "line": 3, "unit": "s"})
        self.assertEqual([int(r.split()[0]) for r in small.splitlines()], list(range(1, 10)))

# --- lock, setup errors, report, CLI -------------------------------------------------------------------------------

class SetupTests(RunCase):
    def test_a_second_run_of_the_same_audit_is_refused(self) -> None:
        with self.store.run_lock(self.audit):
            with self.assertRaises(RunError) as ctx:
                self.run_audit(self.expert(READ_ALL, findings()))
        self.assertEqual(ctx.exception.code, 4)

    def test_unknown_audit_and_ops_projects_are_refused(self) -> None:
        with self.assertRaises(RunError) as ctx:
            AuditRunner("audittest", "audit_nope", audit_dir=self.root / "audits")
        self.assertEqual(ctx.exception.code, 3)
        ops = Project(id="opsproj", workspace_raw=str(self.repo), kind="ops")
        with patch.object(ProjectManager, "load", return_value=ops):
            with self.assertRaises(RunError):
                AuditRunner("opsproj", self.audit, audit_dir=self.root / "audits")

    def test_retrieval_off_refuses_to_run(self) -> None:
        runner = self.runner()
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL": "0"}):
            with self.assertRaises(RunError):
                runner.run()
        runner.events.close()

    def test_a_plan_commit_that_is_gone_is_refused(self) -> None:
        plan = self.store.load_plan(self.audit)
        plan["sha"] = "f" * 40
        repo_audit._write_json(self.root / "audits" / self.audit / "plan.json", plan)
        with self.assertRaises(RunError):
            self.runner()


class ReviewFixTests(RunCase):
    """Regression tests for the code and security review of A2."""

    def only_tool_then(self, follow_up: Any) -> Any:
        """Reads one file, answers an empty list, and gives `follow_up(prompt)` to the AUDIT CHECK question."""
        def fake(prompt: str, context: Any = None, session_id: Any = None, **_: Any) -> LLMResult:
            if "AUDIT CHECK" in prompt:
                return follow_up(prompt)
            if session_id:
                return LLMResult("claude-cli", findings(), session_id, resumed=True)
            return LLMResult("claude-cli", block({"op": "read", "path": "cli/tool.py"}), "expert-s1")
        return fake

    def test_a_quota_failure_on_the_followup_defers_the_shard(self) -> None:
        quota = lambda _p: LLMResult(BUILTIN_PROVIDER, "canned", failures=["claude: usage limit reached"])  # noqa: E731
        result = self.run_audit(self.only_tool_then(quota))
        self.assertEqual(result["state"], "paused")
        self.assertEqual(self.store.load_shard(self.audit, "s0001")["state"], "deferred")
        self.assertEqual(len(self.store.pending(self.audit)), 1)

    def test_another_failure_on_the_followup_keeps_the_first_answer(self) -> None:
        broken = lambda _p: LLMResult(BUILTIN_PROVIDER, "canned", failures=["claude: not installed"])  # noqa: E731
        self.run_audit(self.only_tool_then(broken))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual((record["state"], record["coverage"]), ("done", "incomplete"))

    def test_a_plan_of_another_prompt_version_is_refused(self) -> None:
        plan = self.store.load_plan(self.audit)
        plan["prompt_version"] = repo_audit.AUDIT_PROMPT_VERSION - 1
        repo_audit._write_json(self.root / "audits" / self.audit / "plan.json", plan)
        with self.assertRaises(RunError) as ctx:
            self.runner()
        self.assertIn("plan the audit again", str(ctx.exception))

    def test_an_error_in_the_middle_leaves_a_paused_audit_with_a_report_and_a_free_lock(self) -> None:
        with patch.object(AuditStore, "save_shard", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_audit(self.expert(READ_ALL, findings(GOOD)))
        self.assertEqual(self.store.meta(self.audit)["state"], "paused")
        self.assertEqual(self.store.meta(self.audit)["interrupted"], "OSError")
        self.assertEqual(self.store.load_report(self.audit)["state"], "paused")
        with self.store.run_lock(self.audit):
            pass
        again = self.run_audit(self.expert(READ_ALL, findings(GOOD)))
        self.assertEqual(again["state"], "done")

    def test_the_ops_command_turns_an_error_into_json(self) -> None:
        with patch("llm_runner.LLMRunner.run_session_prompt", side_effect=self.expert(READ_ALL, findings())), \
                patch.object(audit_run, "default_audit_dir", return_value=self.root / "audits"), \
                patch.object(AuditStore, "save_shard", side_effect=OSError("disk full")):
            code, payload = audit_ops.run_audit("audittest", self.audit)
        self.assertEqual((code, payload["ok"]), (1, False))
        self.assertIn("OSError", payload["error"])

    def test_a_retried_shard_does_not_resume_the_session_of_the_failed_one(self) -> None:
        seen: List[Any] = []

        def fake(prompt: str, context: Any = None, session_id: Any = None, **_: Any) -> LLMResult:
            seen.append(session_id)
            return LLMResult("claude-cli", "no block here", session_id or "expert-s1")
        self.run_audit(fake)
        self.assertEqual(self.store.load_shard(self.audit, "s0001")["state"], "failed")
        seen.clear()
        self.run_audit(fake)
        self.assertIsNone(seen[0])

    def test_an_expert_without_providers_is_limited_to_claude(self) -> None:
        runner = self.runner()
        providers: List[Any] = []
        original = runner.engine._ask

        def spy(*args: Any, **kwargs: Any) -> Any:
            providers.append(args[6])
            return original(*args, **kwargs)
        with patch.object(runner.engine, "_ask", side_effect=spy):
            self.run_audit(self.expert(READ_ALL, findings()), runner=runner)
        self.assertEqual(providers[0], audit_run.AUDIT_PROVIDERS)

    def test_non_object_findings_are_kept_unverified_and_unverified_quotes_are_not_stored(self) -> None:
        self.run_audit(self.expert(READ_ALL, findings("just a string", INVENTED)))
        record = self.store.load_shard(self.audit, "s0001")
        self.assertEqual([f["verified"] for f in record["findings"]], [False, False])
        self.assertEqual(record["findings"][0]["reason"], "the finding is not an object")
        self.assertEqual(record["findings"][1]["quote"], "")

    def test_credentials_are_masked_in_what_is_stored(self) -> None:
        leaky = {**GOOD, "claim": "uses password = \"hunter2\" in the open", "severity": "high"}
        self.run_audit(self.expert(READ_ALL, findings(leaky)))
        stored = self.store.load_shard(self.audit, "s0001")["findings"][0]
        self.assertNotIn("hunter2", stored["claim"])
        self.assertIn("***", stored["claim"])
        self.assertEqual(audit_run.mask_secrets("return 1"), "return 1")
        self.assertNotIn("MIIE", audit_run.mask_secrets("-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----"))

    def test_zero_shards_is_refused(self) -> None:
        runner = self.runner()
        with self.assertRaises(RunError):
            runner.run(max_shards=0)
        runner.events.close()

    def test_ids_with_a_trailing_newline_and_a_linked_lock_are_refused(self) -> None:
        with self.assertRaises(repo_audit.AuditError):
            self.store.load_plan(self.audit + "\n")
        lock = self.root / "audits" / self.audit / "run.lock"
        if lock.exists():
            lock.unlink()
        lock.symlink_to(self.root / "elsewhere")
        with self.assertRaises(OSError):
            with self.store.run_lock(self.audit):
                pass
        self.assertFalse((self.root / "elsewhere").exists())

    def test_the_report_command_does_not_overwrite_the_report_of_a_running_audit(self) -> None:
        self.store.set_state(self.audit, "running")
        with patch.object(audit_ops, "default_audit_dir", return_value=self.root / "audits"), \
                patch.object(audit_ops, "open_store", return_value=(self.project, None)):
            code, _ = audit_ops.audit_report("audittest", self.audit)
        self.assertEqual(code, 0)
        self.assertIsNone(self.store.load_report(self.audit))


class ImportTests(unittest.TestCase):
    def test_the_audit_import_path_does_not_load_the_action_machinery(self) -> None:
        import subprocess

        code = ("import sys; import audit_ops, repo_audit; "
                "bad = [m for m in ('action_ops', 'patch_ops', 'publish_ops', 'workspace_ops', 'debate_engine', 'db_manager') "
                "if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r}); {code}"],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "[]")


class OpsAndCliTests(RunCase):
    def test_run_and_report_commands(self) -> None:
        fake = self.expert(READ_ALL, findings(GOOD))
        with patch("llm_runner.LLMRunner.run_session_prompt", side_effect=fake), \
                patch.object(repo_audit, "default_audit_dir", return_value=self.root / "audits"), \
                patch.object(audit_ops, "default_audit_dir", return_value=self.root / "audits"), \
                patch.object(audit_ops, "open_store", return_value=(self.project, None)), \
                patch("audit_run.default_audit_dir", return_value=self.root / "audits"):
            code, payload = audit_ops.run_audit("audittest", self.audit)
            self.assertEqual((code, payload["ok"], payload["state"]), (0, True, "done"))
            code, payload = audit_ops.audit_report("audittest", self.audit)
            self.assertEqual((code, payload["report"]["totals"]["findings"]), (0, 1))
            code, payload = audit_ops.audit_report("audittest", "audit_missing")
            self.assertEqual(code, 3)
            code, payload = audit_ops.run_audit("audittest", "audit_missing")
            self.assertEqual(code, 3)

    def test_report_of_a_planned_audit_is_empty_and_not_stored(self) -> None:
        with patch.object(audit_ops, "default_audit_dir", return_value=self.root / "audits"), \
                patch.object(audit_ops, "open_store", return_value=(self.project, None)):
            code, payload = audit_ops.audit_report("audittest", self.audit)
        self.assertEqual(code, 0)
        self.assertEqual(payload["report"]["totals"]["units_audited"], 0)
        self.assertGreater(payload["report"]["totals"]["units_unaudited"], 0)
        self.assertIsNone(self.store.load_report(self.audit))

    def test_cli_flags_exist(self) -> None:
        import guardian

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(sys, "argv", ["guardian.py", "--help"]):
            with self.assertRaises(SystemExit):
                guardian.main()
        text = out.getvalue()
        for flag in ("--repo-audit-run", "--repo-audit-report", "--audit-max-shards"):
            self.assertIn(flag, text)


if __name__ == "__main__":
    unittest.main()

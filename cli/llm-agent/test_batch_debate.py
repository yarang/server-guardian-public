"""The vote pass of a debate (docs/design/batch-execution.md, chapter 6): who is asked, what is stored, what runs."""
import json
import random
import re
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import batch_gate
import batch_votes
import plan_ops
import test_plan_ops as tp
from debate_engine import DebateEngine
from debate_events import EventWriter
from ops_checks import OpsChecks
from ops_packages import INSTALL, REMOVE
from project_registry import ProjectManager, parse_project
from test_ops_project import OPS
from test_retrieval_loop import FakeRunner
from testenv import restore_data_dir

BATCH = {"auto": True, "max_plans_per_day": 2}
ASK_DISK = '```ops\n{"requests": [{"op": "disk_usage", "host": "n1"}]}\n```'


class PassingGate:
    def review(self, _request: Any) -> Dict[str, Any]:
        return {"gate": "PASSED", "reasons": [], "jev_decision_id": 7, "eval_status": "done", "evaluation": {}}


def setUpModule() -> None:
    tp.setUpModule()


def tearDownModule() -> None:
    restore_data_dir()


def judge_answer() -> str:
    body = {"decision": "Install htop on n1.", "constraints": ["Only n1."], "checks": [], "actions": [],
            "plan": tp.plan(tp.step("a", "n1", "htop"))}
    return "Analysis.\n```json\n" + json.dumps(body) + "\n```\nVERDICT: APPROVED"


def vote_block(plan: str, kind: str = "approve", cites: Optional[List[str]] = None, reason: str = "ok") -> str:
    return "```vote\n" + json.dumps({"plan": plan, "vote": kind, "reason": reason, "cites": cites or []}) + "\n```"


class CriticScript:
    """A critic that critiques, then answers the plan vote the way `answer(plan_hash, prompt)` says."""

    def __init__(self, answer: Callable[[str, str], str]) -> None:
        self.answer = answer
        self.plan = ""
        self.vote_prompts = 0

    def __call__(self, prompt: str) -> str:
        found = re.search(r"PLAN VOTE: plan (\w{12})", prompt)
        if found:
            self.plan = found.group(1)
        if found or any(mark in prompt for mark in ("VOTE CHECK", "SERVER CHECK RESULTS", "CITATION CHECK")):
            self.vote_prompts += 1
            return self.answer(self.plan, prompt)
        return "critique"


class VotePassTests(tp.PlanCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._tmp.name)
        (self.root / "builtin").mkdir()
        self.manager = ProjectManager(directory=self.root / "custom", builtin_dir=self.root / "builtin")
        self.manager.directory.mkdir()
        self.events_path = self.root / "events.jsonl"
        self.host = tp.HostFake()
        self.saves: List[int] = []
        self.gate: Any = PassingGate()
        self.project(batch=BATCH)

    def project(self, **policy: Any) -> None:
        (self.manager.directory / "ops1.json").write_text(json.dumps(
            {**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE], **policy}}))

    def debate(self, critic: Callable[[str, str], str]) -> Dict[str, Any]:
        script = CriticScript(critic)
        self.script = script
        runner = FakeRunner({"expert": ["proposal"], "critic": script, "judge": [judge_answer()]})
        self.runner = runner
        engine = DebateEngine(max_rounds=1, db_path=self.root / "d.db", events=EventWriter(str(self.events_path)),
                              jev_gate=self.gate)
        engine.project_mgr = self.manager
        self.engine = engine
        checks = MagicMock()
        checks.execute_remote_ssh.return_value = {"exit_code": 0, "stdout": "/dev/sda1 96% /", "stderr": ""}
        with patch("agent_client.NodeAgentClient", return_value=self.host), \
                patch("ops_checks.OpsChecks._agent", return_value=checks), \
                patch.object(engine.llm, "run_session_prompt", side_effect=runner), \
                patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.saves.append(len(self.host.starts))
                    or engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5], context=a[6])
                    or self.root / "l.md")):
            try:
                return engine.run_debate("install htop on n1", project_id="ops1", team="fleet-provision")
            finally:
                engine.events.close()

    def events(self, name: str) -> List[Dict[str, Any]]:
        lines = [json.loads(line) for line in self.events_path.read_text().splitlines()]
        return [e.get("data", e) for e in lines if e.get("type") == name]

    def plan_of(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return result["final_decision"]["plan"]

    def test_unanimous_approval_is_stored_and_the_released_plan_runs_by_itself(self) -> None:
        result = self.debate(lambda plan, _p: vote_block(plan))
        plan = self.plan_of(result)
        self.assertEqual([v["vote"] for v in plan["votes"]], ["approve", "approve"])
        self.assertEqual(len(plan["plan_hash"]), 64)
        self.assertTrue(plan["gate"]["would_auto"], plan["gate"]["reasons"])
        self.assertTrue(plan["gate"]["auto"])
        stored = self.engine.db.get_plan(plan["plan_id"])
        self.assertEqual((stored["plan_hash"], stored["votes"]), (plan["plan_hash"], plan["votes"]))
        self.assertEqual(stored["gate"]["auto_run"]["actor"], "autoplan:ops1")
        self.assertEqual(stored["gate"]["auto_run"]["counts"], {"EXECUTED": 1})
        self.assertEqual(self.host.starts, [("n1", "htop")])
        self.assertEqual(self.statuses(plan["plan_id"]), {"a": "EXECUTED"})
        self.assertEqual([s["status"] for s in plan["steps"]], ["EXECUTED"])
        self.assertTrue(plan["auto_run"]["started"])
        self.assertEqual(len(self.events("plan_vote")), 2)
        self.assertEqual(self.events("plan_gate")[0]["would_auto"], True)
        run = self.events("plan_auto_run")
        self.assertEqual([(e["started"], e["counts"]) for e in run], [(True, {"EXECUTED": 1})])

    def test_without_the_opt_in_the_plan_is_stored_and_nothing_runs(self) -> None:
        self.project(batch={}, allowed_actions=[INSTALL, REMOVE, "ops:run_command_set"])  # a batch tool, no opt-in
        plan = self.plan_of(self.debate(lambda plan, _p: vote_block(plan)))
        self.assertTrue(plan["gate"]["would_auto"])
        self.assertFalse(plan["gate"]["auto"])
        self.assertEqual(self.host.starts, [])
        self.assertEqual(self.statuses(plan["plan_id"]), {"a": "PROPOSED"})
        self.assertEqual(self.events("plan_auto_run"), [])
        self.assertNotIn("auto_run", plan)

    def test_a_refused_release_leaves_its_reasons_on_the_plan(self) -> None:
        def critic(plan: str, _p: str) -> str:
            self.project(batch={**BATCH, "auto": False})
            return vote_block(plan)
        plan = self.plan_of(self.debate(critic))
        stored = self.engine.db.get_plan(plan["plan_id"])["gate"]
        self.assertTrue(any("auto" in r for r in stored["release_refused"]["reasons"]), stored)
        self.assertNotIn("auto_run", stored)

    def test_a_kill_switch_keeps_the_plan_for_a_person(self) -> None:
        with patch.dict("os.environ", {"GUARDIAN_AUTO_APPLY": "off"}):
            plan = self.plan_of(self.debate(lambda plan, _p: vote_block(plan)))
        self.assertFalse(plan["gate"]["auto"])
        self.assertEqual(self.host.starts, [])
        self.assertEqual(self.statuses(plan["plan_id"]), {"a": "PROPOSED"})

    def test_a_failure_while_releasing_does_not_end_the_debate(self) -> None:
        with patch("batch_auto.release", side_effect=RuntimeError("database is locked")):
            result = self.debate(lambda plan, _p: vote_block(plan))
        plan = self.plan_of(result)
        self.assertTrue(result["final_decision"]["approved"])
        self.assertFalse(plan["auto_run"]["started"])
        self.assertTrue(any("database is locked" in r for r in plan["auto_run"]["reasons"]))
        self.assertEqual(self.statuses(plan["plan_id"]), {"a": "PROPOSED"})
        self.assertEqual([e["started"] for e in self.events("plan_auto_run")], [False])

    def test_the_debate_is_saved_before_the_plan_starts_and_again_after(self) -> None:
        self.debate(lambda plan, _p: vote_block(plan))
        self.assertEqual(self.saves, [0, 1])  # host starts seen at each save

    def test_a_plan_that_is_not_released_is_saved_once(self) -> None:
        self.project(batch={}, allowed_actions=[INSTALL, REMOVE, "ops:run_command_set"])
        self.debate(lambda plan, _p: vote_block(plan))
        self.assertEqual(self.saves, [0])

    def test_a_start_claimed_before_a_failure_is_reported_as_started(self) -> None:
        def claim_then_fail(db: Any, _mcp: Any, plan_id: str, **_kw: Any) -> Dict[str, Any]:
            db.claim_auto_plan(plan_id, "ops1", 5, "autoplan:ops1", time.strftime("%Y-%m-%d", time.gmtime()),
                               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            raise OSError("disk went away")
        with patch("batch_auto.release", side_effect=claim_then_fail):
            plan = self.plan_of(self.debate(lambda plan, _p: vote_block(plan)))
        self.assertTrue(plan["auto_run"]["started"])
        self.assertIn("disk went away", plan["auto_run"]["error"])
        self.assertEqual(self.events("plan_auto_run")[0]["started"], True)

    def test_a_policy_saved_during_the_debate_counts_at_release(self) -> None:
        def critic(plan: str, _p: str) -> str:
            self.project(batch={**BATCH, "auto": False})  # the operator switches it off while the critics work
            return vote_block(plan)
        plan = self.plan_of(self.debate(critic))
        self.assertTrue(plan["gate"]["auto"])  # decided on the project of the debate's start
        self.assertFalse(plan["auto_run"]["started"])
        self.assertEqual(self.host.starts, [])

    def test_a_project_without_batch_tools_or_opt_in_is_never_asked(self) -> None:
        self.project()  # neither policy.batch.auto nor a batch tool
        result = self.debate(lambda plan, _p: vote_block(plan))
        self.assertEqual(self.script.vote_prompts, 0)
        self.assertNotIn("votes", self.plan_of(result))
        self.assertIsNone(self.engine.db.get_plan(self.plan_of(result)["plan_id"])["gate"])
        self.assertEqual(self.events("plan_gate"), [])

    def test_an_objection_with_a_checked_citation_sends_the_plan_to_a_person(self) -> None:
        def answer(plan: str, prompt: str) -> str:
            found = re.search(r"--- #(\d+) disk_usage n1", prompt)
            if not found:
                return ASK_DISK
            return vote_block(plan, "oppose", [f"[#{found.group(1)} disk_usage n1]"], "n1 is at 96 percent")
        result = self.debate(answer)
        plan = self.plan_of(result)
        self.assertEqual([(v["vote"], v["counted"]) for v in plan["votes"]], [("oppose", True), ("oppose", True)])
        self.assertFalse(plan["gate"]["would_auto"])
        self.assertTrue(any("objection" in r for r in plan["gate"]["reasons"]))
        self.assertEqual(plan["gate"]["tally"]["objections"], 2)

    def test_an_objection_citing_nothing_is_only_advice(self) -> None:
        def answer(plan: str, prompt: str) -> str:  # an invented citation is sent back once; the critic then drops it
            cites = [] if "CITATION CHECK" in prompt else ["[#9 disk_usage n1]"]
            return vote_block(plan, "oppose", cites, "feels risky")
        result = self.debate(answer)
        plan = self.plan_of(result)
        self.assertEqual([(v["vote"], v["counted"], v["cites"]) for v in plan["votes"]], [("oppose", False, [])] * 2)
        self.assertTrue(plan["gate"]["would_auto"])

    def test_a_critic_without_a_usable_vote_is_asked_once_more_and_then_abstains(self) -> None:
        result = self.debate(lambda _plan, _p: "I think it is fine.")
        plan = self.plan_of(result)
        self.assertEqual([v["vote"] for v in plan["votes"]], ["abstain", "abstain"])
        self.assertEqual(self.script.vote_prompts, 4)  # the question and one repair, per critic
        self.assertTrue(plan["gate"]["would_auto"])
        self.assertIn(batch_gate.NO_VOTE, plan["gate"]["notes"])

    def test_a_repaired_vote_counts(self) -> None:
        def answer(plan: str, prompt: str) -> str:
            return vote_block(plan) if "VOTE CHECK" in prompt else "I think it is fine."
        plan = self.plan_of(self.debate(answer))
        self.assertEqual([v["vote"] for v in plan["votes"]], ["approve", "approve"])

    def test_a_vote_for_another_plan_is_an_abstention(self) -> None:
        plan = self.plan_of(self.debate(lambda _plan, _p: vote_block("0" * 12, "oppose", ["[#1 disk_usage n1]"])))
        self.assertEqual([(v["vote"], v["counted"]) for v in plan["votes"]], [("abstain", False)] * 2)
        self.assertTrue(plan["gate"]["would_auto"])

    def test_the_shadow_decision_follows_the_opt_in(self) -> None:
        self.project(batch={}, allowed_actions=[INSTALL, REMOVE, "ops:run_command_set"])  # a batch tool, no opt-in
        plan = self.plan_of(self.debate(lambda plan, _p: vote_block(plan)))
        self.assertFalse(plan["gate"]["auto"])
        self.assertTrue(plan["gate"]["would_auto"])
        self.assertIn("policy.batch.auto is off", plan["gate"]["reasons"])

    def test_without_a_jev_gate_nobody_is_asked(self) -> None:
        self.gate = None
        with patch.dict("os.environ", {"GUARDIAN_JEV_GATE": "off"}):
            result = self.debate(lambda plan, _p: vote_block(plan))
        self.assertEqual(self.script.vote_prompts, 0)
        self.assertNotIn("gate", self.plan_of(result))

    def test_a_failure_in_the_vote_pass_does_not_end_the_debate(self) -> None:
        with patch("db_manager.DecisionDBManager.set_plan_gate", side_effect=[RuntimeError("database is locked"), True]):
            result = self.debate(lambda plan, _p: vote_block(plan))
        plan = self.plan_of(result)
        self.assertFalse(plan["gate"]["would_auto"])
        self.assertTrue(any("vote pass failed" in r for r in plan["gate"]["reasons"]))
        self.assertTrue(result["final_decision"]["approved"])
        self.assertFalse(self.events("plan_gate")[0]["would_auto"])

    def test_a_bug_while_reading_a_vote_is_not_an_abstention(self) -> None:
        """Only an unreachable critic abstains (which counts as approving); a failure in parsing or checking fails closed."""
        with patch("batch_votes.parse_vote", side_effect=RuntimeError("boom")):
            plan = self.plan_of(self.debate(lambda plan, _p: vote_block(plan)))
        self.assertFalse(plan["gate"]["would_auto"])
        self.assertTrue(any("vote pass failed" in r for r in plan["gate"]["reasons"]))

    def test_an_unreachable_critic_abstains_with_a_reason(self) -> None:
        def answer(plan: str, _p: str) -> str:
            raise OSError("no route")
        plan = self.plan_of(self.debate(answer))
        self.assertEqual([v["vote"] for v in plan["votes"]], ["abstain", "abstain"])
        self.assertTrue(all("asking failed" in v["note"] for v in plan["votes"]))

    def test_the_critic_sees_the_stored_script_and_arguments(self) -> None:
        seen: List[str] = []

        def answer(plan: str, prompt: str) -> str:
            seen.append(prompt)
            return vote_block(plan)
        self.debate(answer)
        self.assertTrue(any("PLAN VOTE" in p and "htop" in p for p in seen))

    def test_a_vote_may_name_the_whole_hash(self) -> None:
        def answer(_plan: str, prompt: str) -> str:
            full = plan_ops.plan_hash(self.engine.db, self.engine.db.list_plans()[0]["id"])
            return vote_block(full.upper())
        plan = self.plan_of(self.debate(answer))
        self.assertEqual([v["vote"] for v in plan["votes"]], ["approve", "approve"])

    def test_parallel_checks_keep_their_own_numbers(self) -> None:
        """Two critics run checks at once; every result's number must name that very check (an objection citing it
        would otherwise be dropped as unverifiable, which counts as approval)."""
        agent = MagicMock()

        def slow(*_a: Any, **_k: Any) -> Dict[str, Any]:
            time.sleep(random.uniform(0.005, 0.03))
            return {"exit_code": 0, "stdout": "/dev/sda1 96% /", "stderr": ""}

        agent.execute_remote_ssh.side_effect = slow
        ops = OpsChecks(parse_project({**OPS, "policy": {"allowed_actions": []}}), agent=agent)
        results: List[Dict[str, Any]] = []

        def critic() -> None:
            for _ in range(5):
                results.append(ops.run({"op": "disk_usage", "host": "n1"}))

        threads = [threading.Thread(target=critic) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(r["ok"] for r in results), [r["error"] for r in results if not r["ok"]][:1])
        numbers = sorted(r["call"] for r in results)
        self.assertEqual(numbers, list(range(1, 21)))
        verified = batch_votes.verifier(ops)
        for result in results:
            self.assertTrue(verified(f"[#{result['call']} disk_usage n1]"), result["call"])


if __name__ == "__main__":
    unittest.main()

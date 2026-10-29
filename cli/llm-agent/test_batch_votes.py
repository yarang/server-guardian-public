"""Votes on a plan (docs/design/batch-execution.md, chapter 6): parsing, counting, the plan hash and the stored gate."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

import batch_gate
import batch_votes
import plan_ops
import test_plan_ops as tp
from db_manager import DecisionDBManager
from ops_checks import OpsChecks
from project_registry import parse_project
from testenv import isolate_data_dir, restore_data_dir

HASH = "ab" * 32


def setUpModule() -> None:
    tp.setUpModule()


def tearDownModule() -> None:
    restore_data_dir()


def block(**fields: Any) -> str:
    body = {"plan": batch_votes.short_hash(HASH), "vote": "approve", "reason": "fine", "cites": [], **fields}
    return "analysis\n```vote\n" + json.dumps(body) + "\n```\n"


def accept_all(_cite: str) -> bool:
    return True


class ParseVoteTests(unittest.TestCase):
    def test_a_valid_vote(self) -> None:
        vote, problem = batch_votes.parse_vote(block(vote="oppose", reason="disk is full", cites=["[#1 disk_usage n1]"]), HASH)
        self.assertIsNone(problem)
        self.assertEqual(vote["vote"], "oppose")
        self.assertEqual(vote["cites"], ["[#1 disk_usage n1]"])

    def test_an_objection_wins_over_a_later_approval(self) -> None:
        """A quoted or injected approve block must not erase an objection."""
        text = block(vote="oppose") + "\nchanged my mind\n" + block(vote="approve")
        self.assertEqual(batch_votes.parse_vote(text, HASH)[0]["vote"], "oppose")

    def test_the_last_of_equal_votes_counts(self) -> None:
        text = block(vote="approve", reason="first") + block(vote="approve", reason="second")
        self.assertEqual(batch_votes.parse_vote(text, HASH)[0]["reason"], "second")

    def test_a_broken_block_is_ignored_when_another_is_valid(self) -> None:
        text = "```vote\nnot json\n```\n" + block(vote="approve")
        self.assertEqual(batch_votes.parse_vote(text, HASH)[0]["vote"], "approve")

    def test_a_long_script_and_arguments_are_shown_whole_up_to_the_limit_and_marked_when_cut(self) -> None:
        body = "\n".join(f"echo line {i}" for i in range(300))
        step = {"step": "a", "tool": "ops:run_script", "arguments": {"target_host": "n1", "script": body},
                "risk": "high"}
        shown = batch_votes.vote_task("t", "g", HASH, [step], {"a": {"script": body}})
        self.assertIn("echo line 299", shown)
        self.assertNotIn("(cut)", shown)
        huge = {**step, "arguments": {"target_host": "n1", "x": "y" * 5000}}
        self.assertIn("(cut)", batch_votes.vote_task("t", "g", HASH, [huge]))
        long_script = "x\n" * (batch_votes.MAX_SCRIPT_SHOWN)
        self.assertIn("(cut)", batch_votes.vote_task("t", "g", HASH, [step], {"a": {"script": long_script}}))

    def test_a_vote_for_another_plan_does_not_count(self) -> None:
        vote, problem = batch_votes.parse_vote(block(plan="0" * 12), HASH)
        self.assertIsNone(vote)
        self.assertIn("names plan", problem)

    def test_malformed_votes_are_refused_with_a_reason(self) -> None:
        cases = {
            "no block": "I approve.",
            "not json": "```vote\nyes\n```",
            "not an object": "```vote\n[1]\n```",
            "bad kind": block(vote="maybe"),
            "cites not a list": block(cites="[#1 disk_usage n1]"),
            "empty": "",
        }
        for name, text in cases.items():
            with self.subTest(name):
                vote, problem = batch_votes.parse_vote(text, HASH)
                self.assertIsNone(vote)
                self.assertTrue(problem)


class CastVoteTests(unittest.TestCase):
    def test_an_approval(self) -> None:
        out = batch_votes.cast_vote("c1", block(), HASH, accept_all)
        self.assertEqual((out["vote"], out["counted"]), ("approve", False))

    def test_an_objection_with_a_verified_citation_counts(self) -> None:
        out = batch_votes.cast_vote("c1", block(vote="oppose", cites=["[#2 disk_usage n1]"]), HASH, accept_all)
        self.assertEqual((out["vote"], out["counted"]), ("oppose", True))

    def test_an_objection_without_evidence_is_advice(self) -> None:
        for cites in ([], ["the disk looks full"]):
            with self.subTest(cites=cites):
                out = batch_votes.cast_vote("c1", block(vote="oppose", cites=cites), HASH, lambda _c: False)
                self.assertEqual((out["vote"], out["counted"], out["cites"]), ("oppose", False, []))
                self.assertIn("advice", out["note"])

    def test_only_the_verified_citations_are_kept(self) -> None:
        out = batch_votes.cast_vote(
            "c1", block(vote="oppose", cites=["[#1 disk_usage n1]", "[#9 disk_usage n1]"]), HASH,
            lambda cite: "#1" in cite)
        self.assertEqual(out["cites"], ["[#1 disk_usage n1]"])
        self.assertTrue(out["counted"])

    def test_no_valid_vote_is_an_abstention(self) -> None:
        out = batch_votes.cast_vote("c1", "no block here", HASH, accept_all)
        self.assertEqual((out["vote"], out["counted"]), ("abstain", False))
        self.assertIn("no valid vote", out["note"])

    def test_a_critic_that_could_not_be_asked_abstains(self) -> None:
        out = batch_votes.cast_vote("c1", "", HASH, accept_all, failed="the critic fell back to canned text")
        self.assertEqual(out["vote"], "abstain")
        self.assertIn("canned", out["note"])

    def test_a_citation_that_names_no_check_is_not_verified_by_ops_checks(self) -> None:
        """OpsChecks.unverified_citations answers (0, []) for a string with no [#n op host]: it must not pass."""
        ops = OpsChecks(parse_project({**tp.OPS, "policy": {"allowed_actions": []}}), resolved={})
        ops.served[1] = ("disk_usage", "n1")
        verified = batch_votes.verifier(ops)
        self.assertTrue(verified("[#1 disk_usage n1]"))
        self.assertFalse(verified("the disk is full"))
        self.assertFalse(verified("[#1 disk_usage n2]"))
        self.assertFalse(verified("[#7 disk_usage n1]"))
        self.assertFalse(verified("[#1 disk_usage n1] and [#7 disk_usage n1]"))


class VotesEnabledTests(unittest.TestCase):
    def project(self, **policy: Any) -> Any:
        return parse_project({**tp.OPS, "policy": policy})

    def test_a_plain_fleet_ops_project_is_never_asked(self) -> None:
        self.assertFalse(batch_votes.votes_enabled(self.project()))

    def test_a_batch_tool_or_the_opt_in_turns_it_on(self) -> None:
        self.assertTrue(batch_votes.votes_enabled(self.project(allowed_actions=["ops:run_command_set"])))
        self.assertTrue(batch_votes.votes_enabled(self.project(batch={"auto": True, "max_plans_per_day": 1})))
        self.assertFalse(batch_votes.votes_enabled(self.project(allowed_actions=["ops:apt_clean"])))


class VoteTaskTests(unittest.TestCase):
    def test_the_question_names_the_plan_and_shows_every_step(self) -> None:
        steps = [{"step": "s1", "tool": "ops:apt_clean", "arguments": {"target_host": "n1"}, "risk": "low",
                  "status": "PROPOSED"},
                 {"step": "s2", "tool": "ops:run_script", "arguments": {"target_host": "n2", "script": "echo hi\n",
                                                                         "reason": "x"},
                  "risk": "high", "status": "PROPOSED"}]
        text = batch_votes.vote_task("clean up", "free disk", HASH, steps)
        self.assertIn(batch_votes.short_hash(HASH), text)
        self.assertIn("s1 on n1: ops:apt_clean", text)
        self.assertIn("NOT undoable", text)
        self.assertIn("echo hi", text)
        self.assertIn("```vote", text)

    def test_a_long_script_is_cut(self) -> None:
        steps = [{"step": "s1", "tool": "ops:run_script", "arguments": {"target_host": "n1", "script": "x" * 9000},
                  "risk": "high", "status": "PROPOSED"}]
        text = batch_votes.vote_task("t", "g", HASH, steps)
        self.assertLess(len(text), 9000)
        self.assertIn("(cut)", text)


class PlanHashTests(tp.PlanCase):
    def stored(self, *steps: Dict[str, Any], **extra: Any) -> str:
        return self.record(tp.HostFake(), tp.plan(*steps, **extra))["plan_id"]

    def test_it_is_stable_and_names_the_stored_plan(self) -> None:
        plan_id = self.stored(tp.step("a", "n1", "htop"), tp.step("b", "n2", "jq", requires=["a"]))
        first = plan_ops.plan_hash(self.db, plan_id)
        self.assertEqual(len(first), 64)
        self.assertEqual(plan_ops.plan_hash(self.db, plan_id), first)
        self.assertIsNone(plan_ops.plan_hash(self.db, "plan_missing"))

    def test_the_same_plan_has_the_same_hash_whatever_its_id(self) -> None:
        one = self.stored(tp.step("a", "n1", "htop"))
        two = self.record(tp.HostFake(), tp.plan(tp.step("a", "n1", "htop")), round_num=2)["plan_id"]
        self.assertNotEqual(one, two)
        self.assertEqual(plan_ops.plan_hash(self.db, one), plan_ops.plan_hash(self.db, two))

    def test_any_change_to_the_plan_changes_the_hash(self) -> None:
        base = plan_ops.plan_hash(self.db, self.stored(tp.step("a", "n1", "htop")))
        variants = {
            "host": tp.plan(tp.step("a", "n2", "htop")),
            "package": tp.plan(tp.step("a", "n1", "jq")),
            "step id": tp.plan(tp.step("b", "n1", "htop")),
            "goal": {**tp.plan(tp.step("a", "n1", "htop")), "goal": "something else"},
            "stop_plan": tp.plan(tp.step("a", "n1", "htop"), stop_plan=True),
            "on_failure": tp.plan(tp.step("a", "n1", "htop", on_failure="continue")),
            "more steps": tp.plan(tp.step("a", "n1", "htop"), tp.step("b", "n2", "jq")),
        }
        for name, data in variants.items():
            with self.subTest(name):
                other = self.record(tp.HostFake(), data, round_num=len(name))["plan_id"]
                self.assertNotEqual(plan_ops.plan_hash(self.db, other), base)

    def test_a_changed_step_row_changes_the_hash(self) -> None:
        plan_id = self.stored(tp.step("a", "n1", "htop"))
        before = plan_ops.plan_hash(self.db, plan_id)
        action = self.db.plan_steps(plan_id)[0]
        with self.db._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET arguments = ? WHERE id = ?;",
                         (json.dumps({**action["arguments"], "packages": [{"name": "tree"}]}), action["id"]))
            conn.commit()
        self.assertNotEqual(plan_ops.plan_hash(self.db, plan_id), before)

    def test_gate_steps_carry_risk_and_status(self) -> None:
        plan_id = self.stored(tp.step("a", "n1", "htop"))
        steps = plan_ops.gate_steps(self.db, plan_id)
        self.assertEqual([(s["step"], s["tool"], s["status"]) for s in steps], [("a", tp.INSTALL, "PROPOSED")])
        self.assertIn(steps[0]["risk"], ("low", "medium", "high"))

    def test_a_step_whose_measurement_failed_is_flagged_unmeasured(self) -> None:
        plan_id = self.stored(tp.step("a", "n1", "htop"))
        self.assertFalse(plan_ops.gate_steps(self.db, plan_id)[0].get("unmeasured"))
        action = self.db.plan_steps(plan_id)[0]
        with self.db._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET preview = ? WHERE id = ?;",
                         (json.dumps({"error": "host did not answer"}), action["id"]))
            conn.commit()
        self.assertTrue(plan_ops.gate_steps(self.db, plan_id)[0]["unmeasured"])


class StoredGateTests(tp.PlanCase):
    def test_votes_and_gate_round_trip(self) -> None:
        plan_id = self.record(tp.HostFake(), tp.plan(tp.step("a", "n1", "htop")))["plan_id"]
        self.assertIsNone(self.db.get_plan(plan_id)["votes"])
        votes: List[Dict[str, Any]] = [{"critic": "c1", "vote": "approve", "reason": "", "cites": [], "counted": False}]
        gate = {"auto": False, "would_auto": True, "reasons": ["policy.batch.auto is off"], "at": "now"}
        self.assertTrue(self.db.set_plan_gate(plan_id, "h" * 64, votes, gate))
        row = self.db.get_plan(plan_id)
        self.assertEqual((row["plan_hash"], row["votes"], row["gate"]), ("h" * 64, votes, gate))
        self.assertFalse(self.db.set_plan_gate("plan_missing", "h", [], {}))
        self.assertEqual(plan_ops.plan_view(self.db, self.client(tp.HostFake()), plan_id)["gate"], gate)

    def test_a_database_made_before_the_columns_existed_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE decision_plans (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
                             "round_num INTEGER NOT NULL, project_id TEXT, goal TEXT, "
                             "stop_plan INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
                conn.execute("INSERT INTO decision_plans (id, thread_id, round_num) VALUES ('plan_old', 't', 1);")
            db = DecisionDBManager(db_path=path)
            old = db.get_plan("plan_old")
            self.assertEqual((old["plan_hash"], old["votes"], old["gate"]), (None, None, None))
            self.assertTrue(db.set_plan_gate("plan_old", "h", [], {"auto": False}))
            DecisionDBManager(db_path=path)  # opening it again is harmless


class AutoPlansTodayTests(tp.PlanCase):
    def test_counts_plans_the_engine_started_not_steps_or_human_runs(self) -> None:
        agent = tp.HostFake()
        first = self.record(agent, tp.plan(tp.step("a", "n1", "htop"), tp.step("b", "n2", "jq")))["plan_id"]
        second = self.record(agent, tp.plan(tp.step("a", "n1", "tree")), round_num=2)["plan_id"]
        for plan_id in (first, second):
            self.db.set_plan_gate(plan_id, "h", [], {"auto": True})
        self.assertEqual(plan_ops.auto_plans_today(self.db, "ops1"), 0)
        for row in self.db.plan_steps(second):  # a person running a plan is not an automatic start
            self.db.update_action(row["id"], "EXECUTED", decided_by="alice")
        self.assertEqual(plan_ops.auto_plans_today(self.db, "ops1"), 0)
        self.assertIsNone(self.db.claim_auto_plan(first, "ops1", 5, "autoplan:ops1", time.strftime("%Y-%m-%d", time.gmtime()), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self.assertEqual(plan_ops.auto_plans_today(self.db, "ops1"), 1)
        self.assertEqual(plan_ops.auto_plans_today(self.db, "other"), 0)


class GateAgreesWithVotesTests(unittest.TestCase):
    def test_a_stored_vote_feeds_the_gate_unchanged(self) -> None:
        votes = [batch_votes.cast_vote("c1", block(vote="oppose", cites=["[#1 disk_usage n1]"]), HASH, accept_all)]
        steps = [{"step": "a", "tool": "ops:apt_clean", "arguments": {}, "risk": "low", "status": "PROPOSED"}]
        policy = {"auto": True, "max_plans_per_day": 1, "max_steps": 5}
        out = batch_gate.decide(steps, votes, policy, 0, True, env={})
        self.assertFalse(out["auto"])
        self.assertEqual(out["tally"]["objections"], 1)


if __name__ == "__main__":
    unittest.main()

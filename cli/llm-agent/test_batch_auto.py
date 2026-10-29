"""Automatic running of a released plan (docs/design/batch-execution.md, B3): release conditions, claim, run-time guard.

No model and no host: `HostFake` of test_plan_ops answers the measure and run commands of apt installs."""
import sqlite3
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

import action_ops
import batch_auto
import batch_gate
import plan_ops
import test_plan_ops as tp
from mcp_client import MCPClient
from ops_packages import INSTALL, REMOVE
from project_registry import parse_project
from test_ops_project import OPS
from test_plan_ops import setUpModule, tearDownModule  # noqa: F401  (isolated data dir and jev gate off)

BATCH = {"auto": True, "max_plans_per_day": 2, "max_steps": 10, "max_parallel_hosts": 2}
OPTIONS = {"host_wait": 0.2, "host_poll": 0.05}


def objection() -> Dict[str, Any]:
    return {"critic": "c1", "vote": "oppose", "reason": "disk is nearly full", "cites": ["[#1 disk_usage n1]"],
            "counted": True}


class AutoCase(tp.PlanCase):
    batch: Dict[str, Any] = BATCH

    def client(self, agent: Any) -> MCPClient:
        policy = {"allowed_actions": [INSTALL, REMOVE], "batch": self.batch}
        return MCPClient(project=parse_project({**OPS, "policy": policy}), agent_client=agent)

    def released(self, agent: Any, the_plan: Dict[str, Any], round_num: int = 1, votes: Optional[List[Dict[str, Any]]] = None,
                 force: bool = False) -> str:
        """Records a plan and stores the votes and gate the engine would have stored. force: store `auto` whatever
        the gate said, to show that the release does not trust what is stored."""
        plan_id = self.record(agent, the_plan, round_num=round_num)["plan_id"]
        votes = votes if votes is not None else []
        decision = batch_gate.decide(plan_ops.gate_steps(self.db, plan_id), votes, BATCH,
                                     plan_ops.auto_plans_today(self.db, "ops1"), True, env={})
        if force:
            decision["auto"] = True
        plan_hash = plan_ops.plan_hash(self.db, plan_id)
        self.db.set_plan_gate(plan_id, plan_hash, votes, {**decision, "plan_hash": plan_hash})
        return plan_id

    def release(self, agent: Any, plan_id: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return batch_auto.release(self.db, self.client(agent), plan_id, env=env if env is not None else {}, **OPTIONS)


class ReleaseTests(AutoCase):
    def test_a_released_plan_runs_under_its_own_actor(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop"), tp.step("b", "n2", "jq")))
        out = self.release(agent, plan_id)
        self.assertTrue(out["started"], out)
        self.assertEqual(self.statuses(plan_id), {"a": "EXECUTED", "b": "EXECUTED"})
        self.assertEqual({r["decided_by"] for r in self.db.plan_steps(plan_id)}, {"autoplan:ops1"})
        run = self.db.get_plan(plan_id)["gate"]["auto_run"]
        self.assertEqual(run["actor"], "autoplan:ops1")
        self.assertEqual(run["counts"], {"EXECUTED": 2})
        self.assertTrue(run["started_at"] and run["finished_at"])

    def test_it_does_not_use_up_the_cap_of_single_actions(self) -> None:
        agent = tp.HostFake()
        self.release(agent, self.released(agent, tp.plan(tp.step("a", "n1", "htop"))))
        self.assertEqual(action_ops.auto_runs_today(self.db, "ops1"), 0)
        self.assertEqual(plan_ops.auto_plans_today(self.db, "ops1"), 1)

    def test_an_unknown_plan_is_refused(self) -> None:
        out = self.release(tp.HostFake(), "plan_nope")
        self.assertFalse(out["started"])

    def test_a_plan_without_a_stored_gate_is_never_run(self) -> None:
        agent = tp.HostFake()
        plan_id = self.record(agent, tp.plan(tp.step("a", "n1", "htop")))["plan_id"]
        out = self.release(agent, plan_id)
        self.assertFalse(out["started"])
        self.assertEqual(agent.starts, [])
        self.assertEqual(self.statuses(plan_id), {"a": "PROPOSED"})

    def test_a_gate_that_sent_the_plan_to_a_person_stays_that_way(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")), votes=[objection()])
        out = self.release(agent, plan_id)
        self.assertFalse(out["started"])
        self.assertEqual(agent.starts, [])

    def test_the_gate_is_decided_again_from_the_stored_votes(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")), votes=[objection()], force=True)
        out = self.release(agent, plan_id)
        self.assertFalse(out["started"])
        self.assertTrue(any("objection" in r for r in out["reasons"]), out)
        self.assertEqual(agent.starts, [])

    def test_an_irreversible_step_is_refused_even_when_a_gate_record_says_auto(self) -> None:
        agent = tp.HostFake()
        agent.installed = {"n1": {"htop"}}
        removal = {**tp.step("a", "n1", "htop"), "tool": REMOVE}
        plan_id = self.released(agent, tp.plan(removal), force=True)
        out = self.release(agent, plan_id)
        self.assertFalse(out["started"])
        self.assertTrue(any("irreversible" in r for r in out["reasons"]), out)
        self.assertEqual(agent.starts, [])

    def test_a_plan_changed_after_the_vote_is_refused(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        gate = {**self.db.get_plan(plan_id)["gate"], "plan_hash": "0" * 64}
        self.db.set_plan_gate(plan_id, "0" * 64, [], gate)
        out = self.release(agent, plan_id)
        self.assertFalse(out["started"])
        self.assertTrue(any("changed" in r for r in out["reasons"]), out)
        self.assertEqual(agent.starts, [])

    def test_the_kill_switches_apply_at_release_time(self) -> None:
        for name in ("GUARDIAN_AUTO_APPLY", "GUARDIAN_UI_ACTIONS"):
            with self.subTest(name):
                agent = tp.HostFake()
                plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")), round_num=1 + len(name))
                out = self.release(agent, plan_id, env={name: "off"})
                self.assertFalse(out["started"])
                self.assertEqual(agent.starts, [])

    def test_a_policy_switched_off_since_the_vote_is_respected(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        self.batch = {**BATCH, "auto": False}
        out = self.release(agent, plan_id)
        self.assertFalse(out["started"])
        self.assertIn("policy.batch.auto is off", out["reasons"])
        self.assertEqual(agent.starts, [])


class CapTests(AutoCase):
    def test_the_daily_cap_counts_started_plans(self) -> None:
        self.batch = {**BATCH, "max_plans_per_day": 1}
        agent = tp.HostFake()
        first = self.released(agent, tp.plan(tp.step("a", "n1", "htop")), round_num=1)
        second = self.released(agent, tp.plan(tp.step("a", "n2", "jq")), round_num=2)
        self.assertTrue(self.release(agent, first)["started"])
        out = self.release(agent, second)
        self.assertFalse(out["started"])
        self.assertTrue(any("per day" in r for r in out["reasons"]), out)
        self.assertEqual(self.statuses(second), {"a": "PROPOSED"})

    def test_a_run_that_could_not_be_driven_still_used_its_slot(self) -> None:
        self.batch = {**BATCH, "max_plans_per_day": 1}
        agent = tp.HostFake()
        first = self.released(agent, tp.plan(tp.step("a", "n1", "htop")), round_num=1)
        with mock.patch.object(plan_ops, "run_plan", side_effect=RuntimeError("plan is already running")):
            out = self.release(agent, first)
        self.assertTrue(out["started"])
        self.assertIn("already running", out["error"])
        self.assertEqual(plan_ops.auto_plans_today(self.db, "ops1"), 1)
        self.assertIn("already running", self.db.get_plan(first)["gate"]["auto_run"]["error"])
        self.assertEqual(self.statuses(first), {"a": "PROPOSED"})

    def test_a_database_error_while_driving_the_plan_is_recorded_not_raised(self) -> None:
        self.batch = {**BATCH, "max_plans_per_day": 1}
        agent = tp.HostFake()
        first = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        with mock.patch.object(plan_ops, "run_plan", side_effect=sqlite3.OperationalError("database is locked")):
            out = self.release(agent, first)
        self.assertTrue(out["started"])
        self.assertIn("database is locked", out["error"])
        run = self.db.get_plan(first)["gate"]["auto_run"]
        self.assertIn("OperationalError", run["error"])
        self.assertTrue(run["finished_at"])


class ClaimTests(AutoCase):
    DAY = time.strftime("%Y-%m-%d", time.gmtime())
    AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def two_plans(self) -> List[str]:
        agent = tp.HostFake()
        ids = [self.released(agent, tp.plan(tp.step("a", host, pkg)), round_num=n)
               for n, (host, pkg) in enumerate((("n1", "htop"), ("n2", "jq")), 1)]
        return ids

    def test_a_plan_can_be_claimed_once(self) -> None:
        first, _ = self.two_plans()
        self.assertIsNone(self.db.claim_auto_plan(first, "ops1", 5, "autoplan:ops1", self.DAY, self.AT))
        self.assertEqual(self.db.claim_auto_plan(first, "ops1", 5, "autoplan:ops1", self.DAY, self.AT),
                         "the plan was already started")

    def test_the_cap_is_enforced_by_the_claim_itself(self) -> None:
        first, second = self.two_plans()
        self.assertIsNone(self.db.claim_auto_plan(first, "ops1", 1, "autoplan:ops1", self.DAY, self.AT))
        why = self.db.claim_auto_plan(second, "ops1", 1, "autoplan:ops1", self.DAY, self.AT)
        self.assertIn("at most 1 per day", why or "")

    def test_the_claim_checks_the_gate_and_hash_it_is_given(self) -> None:
        first, second = self.two_plans()
        why = self.db.claim_auto_plan(first, "ops1", 5, "autoplan:ops1", self.DAY, self.AT, "not-the-hash")
        self.assertEqual(why, "the stored gate no longer releases this plan")
        self.assertEqual(self.db.count_auto_plans("ops1", self.DAY), 0)
        good = plan_ops.plan_hash(self.db, second)
        self.assertIsNone(self.db.claim_auto_plan(second, "ops1", 5, "autoplan:ops1", self.DAY, self.AT, good))

    def test_a_plan_whose_gate_says_no_cannot_be_claimed_with_a_hash(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")), votes=[objection()])
        good = plan_ops.plan_hash(self.db, plan_id)
        self.assertEqual(self.db.claim_auto_plan(plan_id, "ops1", 5, "autoplan:ops1", self.DAY, self.AT, good),
                         "the stored gate no longer releases this plan")

    def test_a_plan_without_a_gate_cannot_be_claimed(self) -> None:
        plan_id = self.record(tp.HostFake(), tp.plan(tp.step("a", "n1", "htop")))["plan_id"]
        self.assertEqual(self.db.claim_auto_plan(plan_id, "ops1", 5, "autoplan:ops1", self.DAY, self.AT),
                         "the plan has no stored gate record")

    def test_other_days_and_projects_do_not_count(self) -> None:
        first, second = self.two_plans()
        self.assertIsNone(self.db.claim_auto_plan(first, "ops1", 1, "autoplan:ops1", "2020-01-01", "2020-01-01T00:00:00Z"))
        self.assertIsNone(self.db.claim_auto_plan(second, "ops1", 1, "autoplan:ops1", self.DAY, self.AT))
        self.assertEqual(self.db.count_auto_plans("ops1", self.DAY), 1)
        self.assertEqual(self.db.count_auto_plans("other", self.DAY), 0)

    def test_finish_adds_to_the_record_and_needs_a_claim(self) -> None:
        first, second = self.two_plans()
        self.assertFalse(self.db.finish_auto_plan(first, {"finished_at": "x"}))
        self.db.claim_auto_plan(first, "ops1", 5, "autoplan:ops1", self.DAY, self.AT)
        self.assertTrue(self.db.finish_auto_plan(first, {"finished_at": "x", "counts": {"EXECUTED": 1}}))
        run = self.db.get_plan(first)["gate"]["auto_run"]
        self.assertEqual((run["actor"], run["finished_at"], run["counts"]), ("autoplan:ops1", "x", {"EXECUTED": 1}))


class GuardTests(AutoCase):
    def action(self, agent: Any) -> Dict[str, Any]:
        self.plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        return self.db.plan_steps(self.plan_id)[0]

    def guard(self, plan_id: str, reload: Any = None, env: Optional[Dict[str, str]] = None) -> Any:
        return batch_auto.step_guard(self.db, plan_id, plan_ops.plan_hash(self.db, plan_id), reload, env or {}, BATCH)

    def test_a_clean_measurement_passes(self) -> None:
        row = self.action(tp.HostFake())
        self.assertIsNone(self.guard(self.plan_id)(row, row["preview"]))

    def test_an_install_that_would_now_remove_something_is_stopped(self) -> None:
        row = self.action(tp.HostFake())
        now = {**row["preview"], "items": ["install:htop", "remove:libfoo"]}
        self.assertIn("high risk", self.guard(self.plan_id)(row, now) or "")

    def test_a_measurement_that_failed_is_stopped(self) -> None:
        row = self.action(tp.HostFake())
        self.assertIn("not measured", self.guard(self.plan_id)(row, {"error": "ssh failed"}) or "")

    def test_a_kill_switch_set_while_the_plan_runs_stops_the_next_step(self) -> None:
        row = self.action(tp.HostFake())
        why = self.guard(self.plan_id, env={"GUARDIAN_AUTO_APPLY": "off"})(row, row["preview"])
        self.assertIn("switched off", why or "")

    def test_a_policy_saved_without_the_opt_in_stops_the_next_step(self) -> None:
        row = self.action(tp.HostFake())
        off = parse_project({**OPS, "policy": {"allowed_actions": [INSTALL], "batch": {**BATCH, "auto": False}}})
        why = self.guard(self.plan_id, reload=lambda: off)(row, row["preview"])
        self.assertIn("policy.batch.auto", why or "")

    def test_a_policy_that_cannot_be_read_stops_the_step(self) -> None:
        row = self.action(tp.HostFake())

        def broken() -> Any:
            raise OSError("gone")
        self.assertIn("could not be read", self.guard(self.plan_id, reload=broken)(row, row["preview"]) or "")

    def test_a_plan_edited_while_it_runs_stops_the_next_step(self) -> None:
        row = self.action(tp.HostFake())
        guard = self.guard(self.plan_id)
        with self.db._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET arguments = ? WHERE id = ?;",
                         ('{"manager": "apt", "packages": ["telnetd"], "target_host": "n1"}', row["id"]))
            conn.commit()
        self.assertIn("changed while it ran", guard(row, row["preview"]) or "")

    def test_the_guard_ends_the_step_stale_and_runs_nothing(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop"), tp.step("b", "n2", "jq", requires=["a"])))
        first = self.db.plan_steps(plan_id)[0]
        self.db.set_action_preview(first["id"], {"deferred": True})  # nothing to compare with, so no drift check
        original = agent.execute_remote_ssh

        def with_removal(target: str, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
            out = original(target, command, timeout_sec, labels)
            if (labels or {}).get("op", "").endswith(":preview") and out["exit_code"] == 0 and target == "n1":
                out = {**out, "stdout": out["stdout"].replace("\n@@exit", "\nRemv libfoo [1.0]\n@@exit", 1)}
            return out

        agent.execute_remote_ssh = with_removal  # type: ignore[method-assign]
        plan_ops.run_plan(self.db, self.client(agent), plan_id, "autoplan:ops1", guard=self.guard(plan_id), **OPTIONS)
        self.assertEqual(self.statuses(plan_id), {"a": "STALE", "b": "SKIPPED"})
        self.assertEqual(agent.starts, [])


class RefusalRecordTests(AutoCase):
    def test_a_refusal_is_stored_with_its_reasons_for_the_dashboard(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        self.assertTrue(self.db.note_release_refusal(plan_id, ["policy.batch.auto is off", "x" * 600], "2026-10-20T00:00:00Z"))
        gate = self.db.get_plan(plan_id)["gate"]
        self.assertEqual(gate["release_refused"]["reasons"][0], "policy.batch.auto is off")
        self.assertEqual(len(gate["release_refused"]["reasons"][1]), 300)
        self.assertTrue(gate["auto"])  # the gate itself is not rewritten

    def test_a_started_plan_has_no_refusal_record(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        self.assertTrue(self.release(agent, plan_id)["started"])
        self.assertFalse(self.db.note_release_refusal(plan_id, ["late"], "2026-10-20T00:00:00Z"))
        self.assertNotIn("release_refused", self.db.get_plan(plan_id)["gate"])

    def test_a_plan_without_a_gate_has_nowhere_to_keep_one(self) -> None:
        plan_id = self.record(tp.HostFake(), tp.plan(tp.step("a", "n1", "htop")))["plan_id"]
        self.assertFalse(self.db.note_release_refusal(plan_id, ["no"], "2026-10-20T00:00:00Z"))


class PlanViewTests(AutoCase):
    def test_every_step_says_whether_it_can_be_undone(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        view = plan_ops.plan_view(self.db, self.client(agent), plan_id)
        self.assertEqual([s["reversible"] for s in view["steps"]], [True])
        self.assertEqual(view["gate"]["plan_hash"], view["plan_hash"])


class FreshPolicyTests(AutoCase):
    def test_release_reads_the_project_as_saved_now(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        off = parse_project({**OPS, "policy": {"allowed_actions": [INSTALL], "batch": {**BATCH, "auto": False}}})
        out = batch_auto.release(self.db, self.client(agent), plan_id, env={}, load_project=lambda _pid: off, **OPTIONS)
        self.assertFalse(out["started"])
        self.assertEqual(agent.starts, [])

    def test_a_project_that_cannot_be_loaded_refuses(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))

        def broken(_pid: str) -> Any:
            raise OSError("gone")
        out = batch_auto.release(self.db, self.client(agent), plan_id, env={}, load_project=broken, **OPTIONS)
        self.assertFalse(out["started"])
        self.assertEqual(agent.starts, [])

    def test_a_project_with_another_id_is_refused(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        other = parse_project({**OPS, "id": "ops2", "policy": {"allowed_actions": [INSTALL], "batch": BATCH}})
        out = batch_auto.release(self.db, self.client(agent), plan_id, env={}, load_project=lambda _pid: other, **OPTIONS)
        self.assertFalse(out["started"])
        self.assertEqual(agent.starts, [])

    def test_the_run_uses_the_allowed_actions_as_saved_now(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        narrowed = parse_project({**OPS, "policy": {"allowed_actions": [REMOVE], "batch": BATCH}})
        out = batch_auto.release(self.db, self.client(agent), plan_id, env={}, load_project=lambda _pid: narrowed,
                                 **OPTIONS)
        self.assertEqual(agent.starts, [], out)
        self.assertNotEqual(self.statuses(plan_id), {"a": "EXECUTED"})

    def test_the_gate_of_a_started_plan_cannot_be_overwritten(self) -> None:
        agent = tp.HostFake()
        plan_id = self.released(agent, tp.plan(tp.step("a", "n1", "htop")))
        self.assertTrue(self.release(agent, plan_id)["started"])
        self.assertFalse(self.db.set_plan_gate(plan_id, "h", [], {"auto": False}))
        self.assertEqual(self.db.count_auto_plans("ops1", time.strftime("%Y-%m-%d", time.gmtime())), 1)


if __name__ == "__main__":
    unittest.main()

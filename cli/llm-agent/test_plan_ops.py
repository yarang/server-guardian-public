"""Plans of catalog actions (docs/design/fleet-provisioning.md, P3): validation, recording, scheduling, JSON commands.

No host and no model: `HostFake` answers the measure and run commands of apt installs from a small per-host state."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from unittest.mock import MagicMock

import action_ops
import ops_facts
import plan_ops
from db_manager import DecisionDBManager
from debate_engine import DebateEngine
from mcp_client import MCPClient, parse_judge_block
from ops_packages import INSTALL, REMOVE
from project_registry import parse_project
from test_ops_packages import facts, sections
from test_ops_project import OPS, _FixedTargets
from testenv import isolate_data_dir, restore_data_dir

THREAD = "thread_t1"
PACKAGES = ("htop", "jq", "tree", "curl")


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def step(step_id: str, host: str, package: str, **extra: Any) -> Dict[str, Any]:
    return {"id": step_id, "host": host, "tool": INSTALL,
            "args": {"manager": "apt", "packages": [{"name": package}]}, **extra}


def plan(*steps: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"goal": "install tools", "steps": list(steps), **extra}


class HostFake:
    """apt hosts: a preview shows the simulation of the package until it is installed, a run installs it."""

    def __init__(self, fail_run: Optional[Set[str]] = None, unreachable: Optional[Set[str]] = None,
                 delay: float = 0.0):
        self.installed: Dict[str, Set[str]] = {}
        self.fail_run = set(fail_run or ())
        self.unreachable = set(unreachable or ())  # hosts whose measurements fail (exit 255)
        self.extra: Dict[str, str] = {}  # host -> a package its simulation adds to every install
        self.delay = delay
        self.calls: List[Dict[str, Any]] = []
        self.starts: List[tuple] = []  # (host, package) in the order the runs began
        self.finished: List[tuple] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.on_run: Optional[Callable[[str, str], None]] = None  # hook(host, package) during a run

    @staticmethod
    def _package(command: str) -> str:
        return next((p for p in PACKAGES if p in command), "htop")

    def execute_remote_ssh(self, target: str, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        op = (labels or {}).get("op", "")
        package = self._package(command)
        with self.lock:
            self.calls.append({"target": target, "op": op, "package": package})
        if op == "host_facts":
            return {"exit_code": 1, "stdout": "", "stderr": ""}
        if op.endswith(":preview"):
            if target in self.unreachable:
                return {"exit_code": 255, "stdout": "", "stderr": "unreachable"}
            with self.lock:
                have = package in self.installed.get(target, set())
            simulation = "" if have else f"Inst {package} (1.0 Ubuntu:24.04/noble [arm64])\n"
            if simulation and target in self.extra:
                simulation += f"Inst {self.extra[target]} (1.0 Ubuntu:24.04/noble [arm64])\n"
            text = sections(f"{package} 1.0 ii" if have else "", simulation)
            return {"exit_code": 0, "stdout": text, "stderr": ""}
        with self.lock:
            self.starts.append((target, package))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.on_run:
                self.on_run(target, package)
            if self.delay:
                time.sleep(self.delay)
            if target in self.fail_run:
                return {"exit_code": 1, "stdout": "", "stderr": "E: failed"}
            with self.lock:
                self.installed.setdefault(target, set()).add(package)
            return {"exit_code": 0, "stdout": "done", "stderr": ""}
        finally:
            with self.lock:
                self.active -= 1
                self.finished.append((target, package))

    def execute_command(self, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        return self.execute_remote_ssh("local", command, timeout_sec, labels)


class PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = DecisionDBManager(db_path=Path(self._tmp.name) / "d.db")
        for host in ("n1", "n2"):
            ops_facts.store_facts(host, facts())
        self._targets = _FixedTargets()
        self._targets.__enter__()

    def tearDown(self) -> None:
        self._targets.__exit__(None, None, None)
        for host in ("n1", "n2"):
            path = ops_facts.facts_dir() / f"{host}.json"
            if path.exists():
                path.unlink()
        self._tmp.cleanup()

    def client(self, agent: Any) -> MCPClient:
        return MCPClient(project=parse_project({**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE]}}),
                         agent_client=agent)

    def record(self, agent: Any, the_plan: Dict[str, Any], held: bool = False, round_num: int = 1) -> Dict[str, Any]:
        return plan_ops.record_plan(self.db, self.client(agent), THREAD, round_num, the_plan, held=held)

    def go(self, agent: Any, plan_id: str, **options: Any) -> Dict[str, Any]:
        options.setdefault("host_wait", 0.2)
        options.setdefault("host_poll", 0.05)
        return plan_ops.run_plan(self.db, self.client(agent), plan_id, "tester", **options)

    def statuses(self, plan_id: str) -> Dict[str, str]:
        return {s["step"]: s["status"] for s in self.db.plan_steps(plan_id)}


class NormalizeTests(unittest.TestCase):
    def test_a_good_plan_is_normalized_in_order(self) -> None:
        out = plan_ops.normalize_plan(plan(step("a", "n1", "htop"), step("b", "n2", "jq", requires=["a"]), stop_plan=True))
        self.assertEqual([s["id"] for s in out["steps"]], ["a", "b"])
        self.assertEqual(out["steps"][1]["requires"], ["a"])
        self.assertEqual(out["steps"][0]["on_failure"], "stop")
        self.assertTrue(out["stop_plan"])

    def test_structural_errors_refuse_the_plan(self) -> None:
        bad = {
            "not an object": "plan",
            "no steps": {"steps": []},
            "duplicate id": plan(step("a", "n1", "htop"), step("a", "n2", "jq")),
            "bad id": plan(step("1 a", "n1", "htop")),
            "unknown requires": plan(step("a", "n1", "htop", requires=["zz"])),
            "requires itself": plan(step("a", "n1", "htop", requires=["a"])),
            "bad on_failure": plan(step("a", "n1", "htop", on_failure="ignore")),
            "not a catalog tool": plan({**step("a", "n1", "htop"), "tool": "jev_mcp:apply_patch"}),
            "bad host": plan(step("a", "../x", "htop")),
            "args not an object": plan({**step("a", "n1", "htop"), "args": []}),
            "requires not a list": plan(step("a", "n1", "htop", requires="b")),
        }
        for name, data in bad.items():
            with self.subTest(name), self.assertRaises(plan_ops.PlanError):
                plan_ops.normalize_plan(data)

    def test_limits(self) -> None:
        many = [step(f"s{i}", f"n{i}", "htop") for i in range(plan_ops.MAX_STEPS + 1)]
        with self.assertRaisesRegex(plan_ops.PlanError, "at most 100"):
            plan_ops.normalize_plan(plan(*many))
        one_host = [step(f"s{i}", "n1", "htop") for i in range(plan_ops.MAX_STEPS_PER_HOST + 1)]
        with self.assertRaisesRegex(plan_ops.PlanError, "per host"):
            plan_ops.normalize_plan(plan(*one_host))

    def test_explicit_cycle(self) -> None:
        with self.assertRaisesRegex(plan_ops.PlanError, "cycle"):
            plan_ops.normalize_plan(plan(step("a", "n1", "htop", requires=["b"]), step("b", "n2", "jq", requires=["a"])))

    def test_the_order_on_a_host_takes_part_in_the_cycle(self) -> None:
        # a runs before b on n1, so "a requires c, c requires b" waits for itself although no step requires itself.
        with self.assertRaisesRegex(plan_ops.PlanError, "cycle"):
            plan_ops.normalize_plan(plan(step("a", "n1", "htop", requires=["c"]), step("b", "n1", "jq"),
                                         step("c", "n2", "tree", requires=["b"])))

    def test_plan_as_actions_shows_the_steps_and_ignores_a_bad_plan(self) -> None:
        shown = plan_ops.plan_as_actions(plan(step("a", "n1", "htop")))
        self.assertEqual(shown[0]["tool"], INSTALL)
        self.assertEqual(shown[0]["arguments"]["target_host"], "n1")
        self.assertEqual(plan_ops.plan_as_actions({"steps": "x"}), [])

    def test_the_judge_block_may_carry_only_a_plan(self) -> None:
        text = "analysis\n```json\n" + json.dumps({"plan": plan(step("a", "n1", "htop"))}) + "\n```\n"
        self.assertEqual(parse_judge_block(text)["plan"]["steps"][0]["id"], "a")
        self.assertEqual(plan_ops.parse_plan(text)["goal"], "install tools")
        self.assertIsNone(plan_ops.parse_plan("```json\n{\"actions\": []}\n```"))
        self.assertIsNone(plan_ops.parse_plan("no block"))


class RecordTests(PlanCase):
    def test_steps_are_stored_as_actions_with_their_previews(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq", requires=["a"], on_failure="continue")))
        self.assertEqual(out["plan_id"], "plan_t1_r1")
        self.assertEqual([s["status"] for s in out["steps"]], ["PROPOSED", "PROPOSED"])
        rows = self.db.plan_steps(out["plan_id"])
        self.assertEqual([(r["step"], r["requires"], r["on_failure"]) for r in rows], [("a", [], "stop"), ("b", ["a"], "continue")])
        self.assertTrue(all(r["preview"].get("items") for r in rows))
        self.assertEqual(self.db.get_plan(out["plan_id"])["goal"], "install tools")
        self.assertEqual(agent.starts, [])  # recording never runs anything

    def test_plan_ids_do_not_collide(self) -> None:
        agent = HostFake()
        first = self.record(agent, plan(step("a", "n1", "htop")))
        second = self.record(agent, plan(step("a", "n1", "htop")))
        self.assertEqual((first["plan_id"], second["plan_id"]), ("plan_t1_r1", "plan_t1_r1_2"))

    def test_a_step_that_does_not_validate_is_stored_invalid_and_the_plan_stays(self) -> None:
        bad = {**step("b", "n1", "jq"), "args": {"manager": "dnf", "packages": [{"name": "jq"}]}}
        out = self.record(HostFake(), plan(step("a", "n1", "htop"), bad))
        self.assertEqual([s["status"] for s in out["steps"]], ["PROPOSED", "INVALID"])
        self.assertIn("not installed", out["steps"][1]["error"])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "PROPOSED", "b": "INVALID"})

    def test_a_structural_error_stores_nothing(self) -> None:
        with self.assertRaises(plan_ops.PlanError):
            self.record(HostFake(), plan(step("a", "n1", "htop", requires=["a"])))
        self.assertEqual(self.db.list_plans(), [])
        self.assertEqual(self.db.list_actions(THREAD), [])

    def test_a_held_plan_holds_every_step(self) -> None:
        out = self.record(HostFake(), plan(step("a", "n1", "htop")), held=True)
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "HELD"})
        with self.assertRaisesRegex(RuntimeError, "HELD"):
            self.go(HostFake(), out["plan_id"])

    def test_a_dependent_step_may_defer_its_preview(self) -> None:
        agent = HostFake(unreachable={"n2"})
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq", requires=["a"])))
        rows = {r["step"]: r for r in self.db.plan_steps(out["plan_id"])}
        self.assertEqual(rows["b"]["status"], "PROPOSED")
        self.assertTrue(rows["b"]["preview"]["deferred"])
        self.assertIn("not reached", rows["b"]["preview"]["reason"])
        # without requires the same measurement error is kept as an error
        out = self.record(agent, plan(step("c", "n2", "jq")), round_num=2)
        self.assertIn("not reached", self.db.plan_steps(out["plan_id"])[0]["preview"]["error"])


class RunTests(PlanCase):
    def test_runs_every_step_and_records_the_result(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n1", "jq"), step("c", "n2", "tree")))
        result = self.go(agent, out["plan_id"])
        self.assertEqual(result["counts"], {"EXECUTED": 3})
        rows = self.db.plan_steps(out["plan_id"])
        self.assertTrue(all(r["decided_by"] == "tester" for r in rows))
        self.assertEqual(agent.installed, {"n1": {"htop", "jq"}, "n2": {"tree"}})
        # plan order on a host
        self.assertLess(agent.starts.index(("n1", "htop")), agent.starts.index(("n1", "jq")))

    def test_requires_orders_steps_across_hosts(self) -> None:
        agent = HostFake(delay=0.05)
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq", requires=["a"])))
        self.go(agent, out["plan_id"])
        self.assertLess(agent.finished.index(("n1", "htop")), agent.starts.index(("n2", "jq")))

    def test_hosts_run_in_parallel(self) -> None:
        agent = HostFake(delay=0.2)
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq")))
        self.go(agent, out["plan_id"])
        self.assertEqual(agent.max_active, 2)
        agent = HostFake(delay=0.05)
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq")), round_num=2)
        self.go(agent, out["plan_id"], max_parallel=1)
        self.assertEqual(agent.max_active, 1)

    def test_a_failed_step_stops_its_host_but_not_the_others(self) -> None:
        agent = HostFake(fail_run={"n1"})
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n1", "jq"), step("c", "n2", "tree")))
        result = self.go(agent, out["plan_id"])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "FAILED", "b": "SKIPPED", "c": "EXECUTED"})
        self.assertEqual(result["counts"], {"FAILED": 1, "SKIPPED": 1, "EXECUTED": 1})
        skipped = self.db.plan_steps(out["plan_id"])[1]
        self.assertIn("on_failure is stop", skipped["result"]["error"])

    def test_on_failure_continue_lets_the_host_go_on(self) -> None:
        agent = HostFake(fail_run={"n1"})
        out = self.record(agent, plan(step("a", "n1", "htop", on_failure="continue"), step("b", "n1", "jq")))
        self.go(agent, out["plan_id"])
        # the later step still ran (and failed, because every run on n1 fails here)
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "FAILED", "b": "FAILED"})
        self.assertEqual(len(agent.starts), 2)

    def test_a_step_that_requires_a_failed_step_is_skipped_transitively(self) -> None:
        agent = HostFake(fail_run={"n1"})
        out = self.record(agent, plan(step("a", "n1", "htop", on_failure="continue"), step("b", "n2", "jq", requires=["a"]),
                                      step("c", "n2", "tree", requires=["b"])))
        self.go(agent, out["plan_id"])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "FAILED", "b": "SKIPPED", "c": "SKIPPED"})
        self.assertEqual(agent.starts, [("n1", "htop")])

    def test_stop_plan_skips_every_step_that_has_not_started(self) -> None:
        agent = HostFake(fail_run={"n1"}, delay=0.05)
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq", requires=["a"]),
                                      step("c", "n2", "tree"), stop_plan=True))
        self.go(agent, out["plan_id"], max_parallel=1)
        statuses = self.statuses(out["plan_id"])
        self.assertEqual(statuses["a"], "FAILED")
        self.assertEqual(statuses["b"], "SKIPPED")
        self.assertIn(statuses["c"], ("SKIPPED", "EXECUTED"))  # c may have started before a failed
        if statuses["c"] == "SKIPPED":
            self.assertIn("stop_plan", self.db.plan_steps(out["plan_id"])[2]["result"]["error"])

    def test_an_invalid_step_counts_as_failed(self) -> None:
        bad = {**step("a", "n1", "htop"), "args": {"manager": "dnf", "packages": [{"name": "htop"}]}}
        agent = HostFake()
        out = self.record(agent, plan(bad, step("b", "n1", "jq"), step("c", "n2", "tree")))
        self.go(agent, out["plan_id"])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "INVALID", "b": "SKIPPED", "c": "EXECUTED"})

    def test_drift_before_a_step_marks_it_stale_and_skips_its_dependents(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n1", "jq")))
        agent.extra["n1"] = "evil"  # the simulation now also shows a package the proposal did not
        self.go(agent, out["plan_id"])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "STALE", "b": "SKIPPED"})
        self.assertIn("also act on", self.db.plan_steps(out["plan_id"])[0]["result"]["error"])
        self.assertEqual(agent.starts, [])

    def test_a_deferred_preview_is_measured_right_before_the_step(self) -> None:
        agent = HostFake(unreachable={"n2"})
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq", requires=["a"])))
        agent.on_run = lambda host, package: agent.unreachable.discard("n2")  # the host comes back during step a
        self.go(agent, out["plan_id"])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "EXECUTED", "b": "EXECUTED"})
        self.assertTrue(self.db.plan_steps(out["plan_id"])[1]["preview"]["items"])  # replaced by the fresh measurement

    def test_a_host_that_cannot_be_measured_fails_the_step_without_running_it(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop")))
        agent.unreachable.add("n1")
        self.go(agent, out["plan_id"])
        row = self.db.plan_steps(out["plan_id"])[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["result"]["not_run"])
        self.assertEqual(agent.starts, [])

    def test_a_busy_host_fails_the_step_after_the_wait(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq")))
        started = time.monotonic()
        with action_ops.host_lock("n1"):
            self.go(agent, out["plan_id"], host_wait=0.2, host_poll=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        row = self.db.plan_steps(out["plan_id"])[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("another action is running", row["result"]["error"])
        self.assertTrue(row["result"]["not_run"])
        self.assertEqual(self.statuses(out["plan_id"])["b"], "EXECUTED")

    def test_a_plan_runs_again_from_where_it_stopped(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n1", "jq")))
        first = self.db.plan_steps(out["plan_id"])[0]
        self.assertTrue(self.db.update_action(first["id"], "EXECUTED", result={"status": "SUCCESS", "verify": {"ok": True}},
                                              expected_status="PROPOSED"))
        agent.installed["n1"] = {"htop"}
        result = self.go(agent, out["plan_id"])
        self.assertEqual(result["counts"], {"EXECUTED": 2})
        self.assertEqual(agent.starts, [("n1", "jq")])  # step a was not run again
        with self.assertRaisesRegex(RuntimeError, "no step waiting"):
            self.go(agent, out["plan_id"])

    def test_refuses_a_running_step_an_unknown_plan_and_a_concurrent_run(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq")))
        with self.assertRaises(LookupError):
            self.go(agent, "plan_nope")
        row = self.db.plan_steps(out["plan_id"])[0]
        self.db.update_action(row["id"], "RUNNING", expected_status="PROPOSED")
        with self.assertRaisesRegex(RuntimeError, "RUNNING"):
            self.go(agent, out["plan_id"])
        self.db.update_action(row["id"], "PROPOSED", expected_status="RUNNING")
        with action_ops.host_lock(out["plan_id"]), self.assertRaisesRegex(RuntimeError, "already running"):
            self.go(agent, out["plan_id"])


class GuardTests(PlanCase):
    def test_a_plan_step_cannot_be_run_or_auto_run_alone(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop")))
        action_id = self.db.plan_steps(out["plan_id"])[0]["id"]
        with self.assertRaisesRegex(RuntimeError, "run the plan"):
            action_ops.run_action(self.db, self.client(agent), action_id, confirm=lambda _a: True, actor="tester")
        self.assertEqual(agent.starts, [])
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "PROPOSED"})

    def test_reject_plan_rejects_the_waiting_steps(self) -> None:
        agent = HostFake()
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq")))
        self.assertEqual(plan_ops.reject_plan(self.db, out["plan_id"], "tester"), 2)
        self.assertEqual(self.statuses(out["plan_id"]), {"a": "REJECTED", "b": "REJECTED"})
        self.assertEqual(plan_ops.reject_plan(self.db, out["plan_id"], "tester"), 0)


class JsonTests(PlanCase):
    def test_list_show_run_and_reject(self) -> None:
        agent = HostFake()
        mcp = self.client(agent)
        out = self.record(agent, plan(step("a", "n1", "htop"), step("b", "n2", "jq")))
        plan_id = out["plan_id"]

        code, listed = plan_ops.list_plans_json(self.db, mcp, THREAD)
        self.assertEqual((code, [p["id"] for p in listed["plans"]]), (0, [plan_id]))
        code, shown = plan_ops.show_plan_json(self.db, mcp, plan_id)
        view = shown["plan"]
        self.assertEqual((code, view["runnable"], view["counts"]), (0, True, {"PROPOSED": 2}))
        self.assertEqual({h["host"]: h["steps"] for h in view["hosts"]}, {"n1": ["a"], "n2": ["b"]})
        self.assertIn(view["risk"], ("low", "medium", "high"))
        self.assertTrue(all(s["tool"] == INSTALL for s in view["steps"]))

        self.assertEqual(plan_ops.show_plan_json(self.db, mcp, "plan_nope")[0], action_ops.NOT_FOUND)
        self.assertEqual(plan_ops.show_plan_json(self.db, mcp, "../x")[0], action_ops.INVALID_INPUT)
        # "$" also matches just before a trailing newline, so only fullmatch refuses "plan_nope\n".
        self.assertEqual(plan_ops.show_plan_json(self.db, mcp, "plan_nope\n")[0], action_ops.INVALID_INPUT)
        self.assertEqual(plan_ops.run_plan_json(self.db, mcp, plan_id, "tester\n")[0], action_ops.INVALID_INPUT)
        self.assertEqual(plan_ops.run_plan_json(self.db, mcp, plan_id, "bad actor!")[0], action_ops.INVALID_INPUT)

        code, ran = plan_ops.run_plan_json(self.db, mcp, plan_id, "tester", host_wait=0.2, host_poll=0.05)
        self.assertEqual(code, 0)
        self.assertTrue(ran["ok"])
        self.assertEqual(ran["plan"]["counts"], {"EXECUTED": 2})
        self.assertFalse(ran["plan"]["runnable"])
        self.assertEqual(plan_ops.run_plan_json(self.db, mcp, plan_id, "tester")[0], action_ops.CONFLICT)
        self.assertEqual(plan_ops.reject_plan_json(self.db, mcp, plan_id, "tester")[0], action_ops.CONFLICT)

    def test_a_failed_run_reports_not_ok_and_reject_works_before_running(self) -> None:
        agent = HostFake(fail_run={"n1"})
        mcp = self.client(agent)
        out = self.record(agent, plan(step("a", "n1", "htop")))
        code, ran = plan_ops.run_plan_json(self.db, mcp, out["plan_id"], "tester")
        self.assertEqual((code, ran["ok"]), (0, False))
        out = self.record(agent, plan(step("a", "n2", "jq")), round_num=2)
        code, rejected = plan_ops.reject_plan_json(self.db, mcp, out["plan_id"], "tester")
        self.assertEqual((code, rejected["rejected"]), (0, 1))
        self.assertEqual(plan_ops.reject_plan_json(self.db, mcp, "plan_nope", "tester")[0], action_ops.NOT_FOUND)


class EngineTests(PlanCase):
    def engine(self, agent: Any) -> DebateEngine:
        engine = DebateEngine.__new__(DebateEngine)  # only the parts _record_plan touches
        engine.db, engine.mcp, engine.events = self.db, self.client(agent), MagicMock()
        return engine

    def test_no_plan_records_nothing(self) -> None:
        self.assertIsNone(self.engine(HostFake())._record_plan(THREAD, 1, None, None, False, None, None))
        self.assertEqual(self.db.list_plans(), [])

    def test_the_engine_stores_the_plan_and_announces_it(self) -> None:
        engine = self.engine(HostFake())
        stored = engine._record_plan(THREAD, 1, plan(step("a", "n1", "htop")), None, False, None, None)
        self.assertEqual(stored["plan_id"], "plan_t1_r1")
        self.assertEqual(self.statuses(stored["plan_id"]), {"a": "PROPOSED"})
        self.assertEqual(engine.events.emit.call_args.args[0], "plan_proposed")
        held = engine._record_plan(THREAD, 2, plan(step("a", "n1", "htop")), None, True, None, None)
        self.assertEqual(self.statuses(held["plan_id"]), {"a": "HELD"})

    def test_a_refused_plan_is_reported_and_not_stored(self) -> None:
        engine = self.engine(HostFake())
        out = engine._record_plan(THREAD, 1, plan(step("a", "n1", "htop", requires=["a"])), None, False, None, None)
        self.assertIsNone(out["plan_id"])
        self.assertIn("requires itself", out["refused"][0])
        self.assertEqual(engine.events.emit.call_args.args[0], "plan_refused")
        self.assertEqual(self.db.list_plans(), [])


class MigrationTests(unittest.TestCase):
    def test_an_old_database_gains_the_plan_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            DecisionDBManager(db_path=path).add_action("t", 1, "x", {}, "kept")
            # Take the P3 schema away again: what a database of an earlier release looks like.
            conn = sqlite3.connect(path)
            conn.execute("DROP INDEX idx_decision_actions_plan")
            for column in ("plan_id", "step", "requires", "on_failure"):
                conn.execute(f"ALTER TABLE decision_actions DROP COLUMN {column}")
            conn.execute("DROP TABLE decision_plans")
            conn.commit()
            conn.close()
            db = DecisionDBManager(db_path=path)
            old = db.list_actions("t")[0]
            self.assertEqual((old["reason"], old["plan_id"], old["step"], old["requires"]), ("kept", None, None, []))
            db.add_plan("plan_x", "t", 1, None, "goal")
            added = db.add_action("t", 1, "x", {}, "r", plan_id="plan_x", step="a", requires=["b"], on_failure="continue")
            row = db.get_action(added)
            self.assertEqual((row["plan_id"], row["step"], row["requires"], row["on_failure"]), ("plan_x", "a", ["b"], "continue"))
            self.assertEqual([s["id"] for s in db.plan_steps("plan_x")], [added])
            self.assertFalse(db.get_plan("plan_x")["stop_plan"])
            self.assertEqual(db.list_actions("t", status="SKIPPED"), [])


if __name__ == "__main__":
    unittest.main()

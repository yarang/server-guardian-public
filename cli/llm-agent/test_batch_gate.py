"""The batch gate (docs/design/batch-execution.md, chapter 7): pure decisions, no model and no host."""
import unittest
from typing import Any, Dict, List, Optional

import batch_gate
from batch_gate import decide


def step(step_id: str = "s1", tool: str = "ops:apt_clean", risk: str = "low", status: str = "PROPOSED",
         reversible: Optional[bool] = None, **args: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"step": step_id, "tool": tool, "arguments": args, "risk": risk, "status": status}
    if reversible is not None:  # what a command set declares (batch_gate.step_facts reads it)
        out["reversible"] = reversible
    return out


def vote(critic: str = "c1", kind: str = "approve", counted: bool = False) -> Dict[str, Any]:
    return {"critic": critic, "vote": kind, "reason": "x", "cites": [], "counted": counted}


POLICY = {"auto": True, "max_plans_per_day": 3, "max_steps": 20, "max_parallel_hosts": 2}


def gate(steps: Optional[List[Dict[str, Any]]] = None, votes: Optional[List[Dict[str, Any]]] = None,
         policy: Optional[Dict[str, Any]] = None, runs_today: int = 0, jev_passed: bool = True,
         env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return decide(steps if steps is not None else [step()], votes if votes is not None else [vote()],
                  policy if policy is not None else POLICY, runs_today, jev_passed, env=env or {})


class UnconfirmedSetTests(unittest.TestCase):
    def test_a_set_the_operator_has_not_confirmed_goes_to_a_person(self) -> None:
        out = gate([step("a", "ops:run_command_set", risk="low", reversible=True, set="mine") | {"unconfirmed": True}])
        self.assertFalse(out["auto"])
        self.assertTrue(any("not been confirmed" in r for r in out["reasons"]))

    def test_a_confirmed_set_is_released(self) -> None:
        self.assertTrue(gate([step("a", "ops:run_command_set", risk="low", reversible=True, set="mine")])["auto"])


class DecideTests(unittest.TestCase):
    def test_a_clean_reversible_plan_is_released(self) -> None:
        out = gate()
        self.assertTrue(out["auto"])
        self.assertEqual(out["reasons"], [])

    def test_the_policy_must_opt_in(self) -> None:
        out = gate(policy={**POLICY, "auto": False})
        self.assertFalse(out["auto"])
        self.assertIn("policy.batch.auto is off", out["reasons"])

    def test_the_kill_switches_stop_it(self) -> None:
        for name in ("GUARDIAN_AUTO_APPLY", "GUARDIAN_UI_ACTIONS"):
            with self.subTest(name):
                out = gate(env={name: "off"})
                self.assertFalse(out["auto"])
                self.assertTrue(any(name.split("_")[1] in r or "switched off" in r for r in out["reasons"]), out)

    def test_jev_must_have_passed(self) -> None:
        out = gate(jev_passed=False)
        self.assertFalse(out["auto"])
        self.assertTrue(any("jev" in r for r in out["reasons"]))

    def test_a_counted_objection_sends_the_plan_to_a_person(self) -> None:
        out = gate(votes=[vote("c1"), vote("c2", "oppose", counted=True)])
        self.assertFalse(out["auto"])
        self.assertTrue(any("c2" in r and "objection" in r for r in out["reasons"]))

    def test_an_objection_without_evidence_is_only_advice(self) -> None:
        out = gate(votes=[vote("c1", "oppose", counted=False)])
        self.assertTrue(out["auto"])

    def test_an_abstention_counts_as_approval(self) -> None:
        self.assertTrue(gate(votes=[vote("c1", "abstain"), vote("c2")])["auto"])

    def test_all_abstaining_still_releases_but_says_so(self) -> None:
        out = gate(votes=[vote("c1", "abstain"), vote("c2", "abstain")])
        self.assertTrue(out["auto"])
        self.assertIn("no vote was cast", out["notes"])

    def test_no_critics_at_all_is_not_a_vote_either(self) -> None:
        out = gate(votes=[])
        self.assertTrue(out["auto"])
        self.assertIn("no vote was cast", out["notes"])

    def test_irreversible_steps_always_need_a_person(self) -> None:
        for tool, args in [("ops:run_script", {}), ("ops:journal_vacuum", {}), ("ops:remove_paths", {}),
                           ("ops:truncate_logs", {}), ("ops:remove_old_kernels", {}), ("ops:package_remove", {}),
                           ("ops:docker_prune", {"scope": "unused_images"}),
                           ("ops:docker_prune", {"scope": "stopped_containers"}),
                           ("ops:something_new", {})]:
            with self.subTest(tool=tool, args=args):
                out = gate(steps=[step("a", "ops:apt_clean"), step("b", tool, **args)])
                self.assertFalse(out["auto"])
                self.assertTrue(any("irreversible" in r and "b" in r for r in out["reasons"]), out)

    def test_reversible_steps_pass(self) -> None:
        for tool, args in [("ops:apt_clean", {}), ("ops:docker_prune", {"scope": "dangling_images"}),
                           ("ops:docker_prune", {"scope": "build_cache"}), ("ops:restart_service", {}),
                           ("ops:package_install", {"manager": "apt"})]:
            with self.subTest(tool=tool):
                self.assertTrue(gate(steps=[step("a", tool, **args)])["auto"])

    def test_an_install_whose_manager_is_unresolved_is_not_released(self) -> None:
        self.assertFalse(gate(steps=[step("a", "ops:package_install", manager="auto")])["auto"])

    def test_a_package_install_is_reversible_unless_high(self) -> None:
        out = gate(steps=[step("a", "ops:package_install", risk="high", manager="apt")])
        self.assertFalse(out["auto"])

    def test_a_high_risk_step_needs_a_person(self) -> None:
        out = gate(steps=[step("a", "ops:restart_service", risk="high")])
        self.assertFalse(out["auto"])
        self.assertTrue(any("high" in r for r in out["reasons"]))

    def test_a_command_set_follows_its_declaration_but_never_when_high(self) -> None:
        reversible = step("a", "ops:run_command_set", risk="low", reversible=True)
        irreversible = step("b", "ops:run_command_set", risk="low", reversible=False)
        high = step("c", "ops:run_command_set", risk="high", reversible=True)
        self.assertTrue(gate(steps=[reversible])["auto"])
        self.assertFalse(gate(steps=[irreversible])["auto"])
        self.assertFalse(gate(steps=[high])["auto"])

    def test_an_invalid_or_held_step_blocks(self) -> None:
        for status in ("INVALID", "HELD", "REJECTED", "FAILED"):
            with self.subTest(status):
                out = gate(steps=[step("a"), step("b", status=status)])
                self.assertFalse(out["auto"])

    def test_size_and_rate_limits(self) -> None:
        many = [step(f"s{i}") for i in range(21)]
        self.assertFalse(gate(steps=many)["auto"])
        self.assertTrue(gate(steps=many[:20])["auto"])
        out = gate(runs_today=3)
        self.assertFalse(out["auto"])
        self.assertTrue(any("per day" in r for r in out["reasons"]))
        self.assertTrue(gate(runs_today=2)["auto"])

    def test_a_cap_of_zero_means_no_auto_runs(self) -> None:
        self.assertFalse(gate(policy={**POLICY, "max_plans_per_day": 0})["auto"])

    def test_a_project_that_never_set_a_cap_is_judged_without_one_in_shadow(self) -> None:
        shadow = gate(policy={**POLICY, "auto": False, "max_plans_per_day": 0})
        self.assertTrue(shadow["would_auto"])
        self.assertFalse(shadow["auto"])
        capped = gate(policy={**POLICY, "auto": False, "max_plans_per_day": 1}, runs_today=1)
        self.assertFalse(capped["would_auto"])

    def test_an_unmeasured_command_set_or_script_step_is_not_released(self) -> None:
        for tool in ("ops:run_command_set", "ops:run_script"):
            unmeasured = {**step("a", tool), "reversible": True, "unmeasured": True}
            out = gate(steps=[unmeasured])
            self.assertFalse(out["auto"], tool)
            self.assertTrue(any("not measured" in r for r in out["reasons"]), out)

    def test_an_unmeasured_package_step_is_not_released(self) -> None:
        unmeasured = {**step("a", "ops:package_install", manager="apt"), "unmeasured": True}
        out = gate(steps=[unmeasured])
        self.assertFalse(out["auto"])
        self.assertTrue(any("not measured" in r for r in out["reasons"]))
        self.assertTrue(gate(steps=[step("a", "ops:apt_clean")])["auto"])

    def test_an_install_that_runs_third_party_code_needs_a_person(self) -> None:
        for manager, scripts_off, released in (("apt", False, True), ("dnf", False, True), ("npm", False, False),
                                               ("npm", True, True), ("brew", False, False), ("pipx", False, False)):
            with self.subTest(manager=manager, scripts_off=scripts_off):
                args = {"manager": manager, **({"ignore_scripts": True} if scripts_off else {})}
                self.assertEqual(gate(steps=[step("a", "ops:package_install", **args)])["auto"], released)

    def test_an_empty_plan_is_not_released(self) -> None:
        self.assertFalse(gate(steps=[])["auto"])

    def test_every_reason_is_reported_not_just_the_first(self) -> None:
        out = gate(steps=[step("a", "ops:run_script", risk="high")], votes=[vote("c1", "oppose", counted=True)],
                   jev_passed=False, runs_today=9)
        text = " | ".join(out["reasons"])
        for word in ("jev", "objection", "high risk", "per day"):
            self.assertIn(word, text)

    def test_would_auto_ignores_only_the_opt_in(self) -> None:
        off = {**POLICY, "auto": False}
        out = gate(policy=off)
        self.assertFalse(out["auto"])
        self.assertTrue(out["would_auto"])
        blocked = gate(policy=off, steps=[step("a", "ops:run_script", risk="high")])
        self.assertFalse(blocked["would_auto"])
        # the kill switches are evaluated as they are: a switched-off host would not auto-run either
        self.assertFalse(gate(policy=off, env={"GUARDIAN_AUTO_APPLY": "off"})["would_auto"])


class VoteCountTests(unittest.TestCase):
    def test_tally(self) -> None:
        tally = batch_gate.tally([vote("a"), vote("b", "oppose", True), vote("c", "oppose", False), vote("d", "abstain")])
        self.assertEqual(tally, {"approve": 1, "oppose": 2, "abstain": 1, "objections": 1})


if __name__ == "__main__":
    unittest.main()

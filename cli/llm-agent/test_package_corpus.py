"""The package corpus stays well-formed and the gates keep their measured recall (fleet-provisioning.md, P5)."""
import io
import os
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace

import package_corpus as corpus


class CorpusShapeTests(unittest.TestCase):
    def test_ten_healthy_actions_and_thirty_defects_of_five_classes(self) -> None:
        self.assertEqual(len(corpus.HEALTHY), 10)
        self.assertEqual(len(corpus.DEFECTS), 30)
        self.assertEqual(Counter(d.cls for d in corpus.DEFECTS), {cls: 6 for cls in corpus.TARGETS})
        self.assertEqual(len({d.ident for d in corpus.DEFECTS}), 30)

    def test_every_defect_changes_a_healthy_scenario_and_names_why_it_is_caught(self) -> None:
        for defect in corpus.DEFECTS:
            with self.subTest(defect=defect.ident):
                self.assertIn(defect.base, corpus.HEALTHY)
                self.assertNotEqual(corpus.apply(defect), replace(corpus.HEALTHY[defect.base], ident=defect.ident))
                self.assertTrue(defect.needle or defect.how == "high", "a refusal must say what it expects to read")

    def test_a_defect_that_changes_nothing_or_an_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            corpus.apply(corpus.Defect("x", "sudo", "apt-install", {}))
        with self.assertRaises(ValueError):
            corpus.apply(corpus.Defect("x", "sudo", "apt-install", {"nope": 1}))

    def test_the_corpus_covers_the_managers_of_a_mixed_fleet(self) -> None:
        managers = {s.args["manager"] for s in corpus.HEALTHY.values()} | {"apt"}
        self.assertGreaterEqual(managers, {"apt", "dnf", "brew", "npm", "apk", "pacman", "auto"})


class HealthyTests(unittest.TestCase):
    def test_healthy_actions_pass_every_gate_at_their_rating(self) -> None:
        for name, sc in corpus.HEALTHY.items():
            with self.subTest(name=name):
                out = corpus.decide(sc)
                self.assertFalse(out.blocked, out.reason)
                self.assertEqual(out.risk, sc.risk)

    def test_deciding_leaves_the_unverified_switch_as_it_was(self) -> None:
        os.environ.pop("GUARDIAN_PACKAGES_ALLOW_UNVERIFIED", None)
        corpus.decide(corpus.HEALTHY["apk-install"])
        self.assertNotIn("GUARDIAN_PACKAGES_ALLOW_UNVERIFIED", os.environ)
        os.environ["GUARDIAN_PACKAGES_ALLOW_UNVERIFIED"] = "bun"
        try:
            corpus.decide(corpus.HEALTHY["apk-install"])
            self.assertEqual(os.environ["GUARDIAN_PACKAGES_ALLOW_UNVERIFIED"], "bun")
        finally:
            del os.environ["GUARDIAN_PACKAGES_ALLOW_UNVERIFIED"]


class MeasureTests(unittest.TestCase):
    def test_every_class_holds_its_target_without_blocking_a_healthy_action(self) -> None:
        result = corpus.measure()
        self.assertEqual(result["false_blocked"], [])
        self.assertEqual(result["healthy_wrong"], [])
        self.assertLessEqual(result["false_block_rate"], corpus.MAX_FALSE_BLOCK)
        for cls, target in corpus.TARGETS.items():
            with self.subTest(cls=cls):
                self.assertGreaterEqual(result["classes"][cls]["recall"], target)
                self.assertEqual(result["classes"][cls]["missed"], [])
                self.assertEqual(result["classes"][cls]["wrong_reason"], [])

    def test_each_class_is_stopped_at_the_stage_that_owns_it(self) -> None:
        stage = {d.ident: corpus.decide(corpus.apply(d)).stage for d in corpus.DEFECTS if d.how == "block"}
        self.assertEqual(stage["sudo-apt-install"], "validate")
        self.assertEqual(stage["mos-apt-on-fedora"], "validate")
        self.assertEqual(stage["prot-role-web"], "validate")
        self.assertEqual(stage["casc-dragged-ssh"], "measure")
        self.assertEqual(stage["stale-new-removal"], "stale")
        self.assertEqual(stage["stale-role-added"], "stale")
        self.assertEqual(stage["sudo-lost-before-run"], "stale")

    def test_the_two_high_risk_defences_stay_possible_but_are_rated_high(self) -> None:
        for ident in ("prot-blind-spot", "casc-many-removals", "casc-install-removes"):
            with self.subTest(ident=ident):
                out = corpus.decide(corpus.apply(next(d for d in corpus.DEFECTS if d.ident == ident)))
                self.assertFalse(out.blocked)
                self.assertEqual(out.risk, "high")

    def test_a_defect_the_gates_miss_is_reported_not_hidden(self) -> None:
        # a "defect" that changes nothing the gates look at: the host is fine, so it is neither blocked nor high
        harmless = corpus.Defect("harmless", "sudo", "apt-install", {"project_extra": ("unrelated",)}, "sudo_required")
        out = corpus.decide(corpus.apply(harmless))
        base = corpus.decide(corpus.HEALTHY["apt-install"])
        self.assertFalse(corpus.caught(harmless, out, base))

    def test_a_block_for_another_reason_does_not_count_as_caught(self) -> None:
        wrong = corpus.Defect("wrong", "sudo", "apt-install", {"facts": None}, "sudo_required")
        out = corpus.decide(corpus.apply(wrong))
        self.assertTrue(out.blocked)
        self.assertFalse(corpus.caught(wrong, out, corpus.decide(corpus.HEALTHY["apt-install"])))

    def test_a_base_that_is_already_blocked_never_makes_a_defect_look_caught(self) -> None:
        blocked = corpus.Outcome(True, "validate", "sudo_required: x")
        defect = corpus.Defect("d", "sudo", "apt-install", {"facts": None}, "sudo_required")
        self.assertFalse(corpus.caught(defect, blocked, blocked))


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = corpus.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_targets_pass_and_print_the_table(self) -> None:
        code, out, err = self.run_main("--targets")
        self.assertEqual(code, 0, err)
        self.assertIn("false-block rate 0.00", out)
        for cls in corpus.TARGETS:
            self.assertIn(cls, out)

    def test_json_output_has_the_numbers(self) -> None:
        import json

        code, out, _ = self.run_main("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["defects"], 30)
        self.assertEqual(data["recall"], 1.0)

    def test_an_unreachable_floor_fails(self) -> None:
        code, _, err = self.run_main("--min-recall", "sudo=1.5")
        self.assertEqual(code, 1)
        self.assertIn("below target", err)

    def test_unknown_class_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main("--min-recall", "nope=1")


if __name__ == "__main__":
    unittest.main()

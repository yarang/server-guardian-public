"""The seeded-defect corpus stays well-formed and the validators keep their measured recall (design-project.md, P2)."""
import io
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout

import artifact_corpus as corpus


class CorpusShapeTests(unittest.TestCase):
    def test_ten_healthy_artifacts_and_thirty_defects_of_five_classes(self) -> None:
        self.assertEqual(len(corpus.HEALTHY), 10)
        self.assertEqual(len(corpus.DEFECTS), 30)
        self.assertEqual(Counter(d.cls for d in corpus.DEFECTS), {cls: 6 for cls in corpus.EXPECT})
        self.assertEqual(len({d.ident for d in corpus.DEFECTS}), 30)

    def test_every_defect_changes_exactly_one_spot_of_a_healthy_artifact(self) -> None:
        for defect in corpus.DEFECTS:
            with self.subTest(defect=defect.ident):
                self.assertNotEqual(corpus.apply(defect), corpus.HEALTHY[defect.base])

    def test_a_defect_that_does_not_match_its_base_is_rejected(self) -> None:
        bad = corpus.Defect("x", "broken-svg", "logo.svg", "not-in-the-file", "y")
        with self.assertRaises(ValueError):
            corpus.apply(bad)

    def test_healthy_artifacts_pass_without_errors_or_warnings(self) -> None:
        for name, text in corpus.HEALTHY.items():
            with self.subTest(name=name):
                self.assertEqual(corpus.findings(name, text), [])


class MeasureTests(unittest.TestCase):
    def test_implemented_detectors_hold_their_recall(self) -> None:
        result = corpus.measure()
        self.assertEqual(result["healthy_flagged"], [])
        for cls, target in corpus.TARGETS.items():
            with self.subTest(cls=cls):
                self.assertGreaterEqual(result["classes"][cls]["recall"], target)
                self.assertEqual(result["classes"][cls]["missed"], [])
                self.assertEqual(result["classes"][cls]["misflagged"], [])
        self.assertEqual(result["precision"], 1.0 if result["recall"] else None)

    def test_a_dead_image_target_is_found_like_a_dead_link(self) -> None:
        defect = next(d for d in corpus.DEFECTS if d.ident == "image-api-legend-dir")
        self.assertTrue(any("image 'figures/legend.svg'" in f for f in corpus.findings(defect.base, corpus.apply(defect))))

    def test_classes_without_a_detector_are_reported_as_gaps_not_hidden(self) -> None:
        result = corpus.measure()
        for cls in ("low-contrast", "false-claim"):
            with self.subTest(cls=cls):
                self.assertFalse(result["classes"][cls]["detector"])
                self.assertEqual(result["classes"][cls]["detected"], 0)
                self.assertEqual(len(result["classes"][cls]["missed"]), 6)

    def test_only_findings_added_by_the_defect_count(self) -> None:
        healthy = corpus.HEALTHY["logo.svg"]
        self.assertEqual(corpus.findings("logo.svg", healthy), [])
        defect = next(d for d in corpus.DEFECTS if d.ident == "svg-no-namespace")
        self.assertTrue(corpus.findings(defect.base, corpus.apply(defect)))

    def test_a_flag_on_a_healthy_artifact_lowers_precision(self) -> None:
        original = corpus.HEALTHY["logo.svg"]
        corpus.HEALTHY["logo.svg"] = original.replace("<title>Guardian mark</title>", "")  # a warning the base now has
        try:
            result = corpus.measure()
        finally:
            corpus.HEALTHY["logo.svg"] = original
        self.assertEqual(result["healthy_flagged"], ["logo.svg"])
        self.assertLess(result["precision"], 1.0)


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return corpus.main(list(argv))

    def test_no_target_is_set_by_default(self) -> None:
        self.assertEqual(self.run_main(), 0)

    def test_min_recall_fails_a_class_below_its_target(self) -> None:
        self.assertEqual(self.run_main("--min-recall", "broken-svg=1.0"), 0)
        self.assertEqual(self.run_main("--min-recall", "false-claim=0.5"), 1)

    def test_targets_flag_holds_the_p2_targets(self) -> None:
        self.assertEqual(self.run_main("--targets"), 0)
        self.assertEqual(self.run_main("--targets", "--min-recall", "low-contrast=0.1"), 1)

    def test_unknown_class_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main("--min-recall", "nope=1")


if __name__ == "__main__":
    unittest.main()

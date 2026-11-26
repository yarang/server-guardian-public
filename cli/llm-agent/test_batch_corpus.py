"""The batch gate against its seeded-defect corpus (no model, no host): the targets hold, and the corpus can fail."""
import unittest
from unittest.mock import patch

import batch_corpus
import batch_gate
import batch_votes


class CorpusTests(unittest.TestCase):
    def test_the_targets_hold(self) -> None:
        result = batch_corpus.measure()
        self.assertEqual(result["healthy_blocked"], [])
        for cls, target in batch_corpus.TARGETS.items():
            with self.subTest(cls):
                self.assertGreaterEqual(result["classes"][cls]["recall"], target, result["classes"][cls]["missed"])
        with patch("sys.stdout"):
            self.assertEqual(batch_corpus.main(["--targets"]), 0)

    def test_the_corpus_has_the_size_it_documents(self) -> None:
        result = batch_corpus.measure()
        self.assertEqual(result["healthy"], 14)
        self.assertEqual(sum(c["total"] for c in result["classes"].values()), 36)

    def test_a_gate_that_calls_everything_reversible_is_caught(self) -> None:
        with patch.object(batch_gate, "is_reversible", return_value=True):
            result = batch_corpus.measure()
            with patch("sys.stdout"), patch("sys.stderr"):
                self.assertEqual(batch_corpus.main(["--targets"]), 1)
        self.assertLess(result["classes"]["irreversible"]["recall"], 1.0)

    def test_a_gate_that_lets_a_script_run_without_an_approval_is_caught(self) -> None:
        real = batch_gate.tally
        with patch.object(batch_gate, "tally", side_effect=lambda votes: {**real(votes), batch_gate.APPROVE: 9}):
            result = batch_corpus.measure()
        self.assertIn("free script, nobody approves", result["classes"]["freescript"]["missed"])

    def test_a_gate_that_counts_advice_blocks_healthy_plans(self) -> None:
        real = batch_votes.cast_vote

        def count_every_oppose(critic, answer, plan_hash, verified, failed=None):
            vote = real(critic, answer, plan_hash, verified, failed)
            return {**vote, "counted": vote["vote"] == "oppose"}

        with patch("batch_votes.cast_vote", side_effect=count_every_oppose):
            result = batch_corpus.measure()
        self.assertIn("advice only", result["healthy_blocked"])


if __name__ == "__main__":
    unittest.main()

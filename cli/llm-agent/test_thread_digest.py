import os
import unittest
from unittest import mock

import thread_digest
from debate_engine import DebateEngine


def entry(round_num, role, content, agent="A"):
    return {"round": round_num, "agent": agent, "role": role, "content": content, "provider": "x"}


def long_thread(rounds=4, size=6000):
    transcript = []
    for number in range(1, rounds + 1):
        transcript.append(entry(number, "Domain Expert", f"proposal {number} " + "p" * size))
        transcript.append(entry(number, "Critic Red Team", f"critique {number} " + "c" * size))
        transcript.append(entry(number, "Judge", f"judge {number} " + "j" * size + "\nVERDICT: REJECTED"))
    return transcript


class ThreadDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GUARDIAN_THREAD_DIGEST", "GUARDIAN_THREAD_DIGEST_MIN_BYTES"):
            os.environ.pop(name, None)

    def test_short_thread_is_replayed_in_full(self) -> None:
        transcript = [entry(1, "Domain Expert", "short"), entry(1, "Judge", "ok")]
        full = "\n\n".join(thread_digest.format_entry(t) for t in transcript)
        self.assertEqual(thread_digest.format_history(transcript), full)

    def test_long_thread_condenses_earlier_rounds_and_keeps_the_latest_whole(self) -> None:
        transcript = long_thread()
        digest = thread_digest.format_history(transcript)
        full = "\n\n".join(thread_digest.format_entry(t) for t in transcript)
        self.assertLess(len(digest), len(full) // 2)
        self.assertIn("navigation only", digest)
        for t in transcript:
            if t["round"] == 4:
                self.assertIn(t["content"], digest)
        self.assertNotIn("p" * 5000, digest.split("[Round 4]")[0])
        self.assertIn("condensed", digest)

    def test_every_earlier_entry_stays_listed(self) -> None:
        digest = thread_digest.format_history(long_thread())
        for number in (1, 2, 3):
            for role in ("Domain Expert", "Critic Red Team", "Judge"):
                self.assertIn(f"[Round {number}] {role}", digest)

    def test_judge_verdict_survives_the_cut(self) -> None:
        digest = thread_digest.format_history(long_thread())
        self.assertIn("VERDICT: REJECTED", digest.split("[Round 4]")[0])

    def test_cut_inside_a_fence_closes_it(self) -> None:
        body = "intro\n```go\n" + "x := 1\n" * 1000 + "```\n"
        transcript = [entry(1, "Domain Expert", body), entry(2, "Domain Expert", "z" * 20000)]
        earlier = thread_digest.format_history(transcript).split("[Round 2]")[0]
        self.assertEqual(earlier.count("```") % 2, 0)

    def test_switch_off_keeps_the_full_replay(self) -> None:
        transcript = long_thread()
        full = "\n\n".join(thread_digest.format_entry(t) for t in transcript)
        with mock.patch.dict(os.environ, {"GUARDIAN_THREAD_DIGEST": "off"}):
            self.assertEqual(thread_digest.format_history(transcript), full)

    def test_threshold_is_configurable(self) -> None:
        transcript = long_thread(rounds=2, size=1500)
        full = "\n\n".join(thread_digest.format_entry(t) for t in transcript)
        self.assertEqual(thread_digest.format_history(transcript), full)
        with mock.patch.dict(os.environ, {"GUARDIAN_THREAD_DIGEST_MIN_BYTES": "100"}):
            self.assertNotEqual(thread_digest.format_history(transcript), full)

    def test_single_round_is_never_condensed(self) -> None:
        transcript = long_thread(rounds=1, size=30000)
        full = "\n\n".join(thread_digest.format_entry(t) for t in transcript)
        self.assertEqual(thread_digest.format_history(transcript), full)

    def test_output_is_deterministic(self) -> None:
        transcript = long_thread()
        self.assertEqual(thread_digest.format_history(transcript), thread_digest.format_history(transcript))

    def test_engine_uses_the_digest(self) -> None:
        transcript = long_thread()
        self.assertEqual(DebateEngine._format_history(transcript), thread_digest.format_history(transcript))

    def test_two_drafts_in_one_round_are_both_condensed_and_replayed(self) -> None:
        transcript = []
        for number in (1, 2):
            transcript.append(entry(number, "Domain Expert", f"lead {number} " + "l" * 6000, agent="Lead"))
            if number == 1:
                transcript.append(entry(number, "Domain Expert", "draft b " + "d" * 6000, agent="Second"))
            transcript.append(entry(number, "Critic Red Team", "c" * 6000))
        digest = thread_digest.format_history(transcript)
        self.assertIn("[Round 1] Domain Expert (Second)", digest)
        self.assertIn("more characters condensed", digest)
        self.assertEqual(digest.count("[Round 1] Domain Expert"), 2)
        self.assertIn("lead 2 " + "l" * 6000, digest)  # the latest round stays whole


if __name__ == "__main__":
    unittest.main()

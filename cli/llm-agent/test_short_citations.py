"""P2.7: short-path and path-less line citations are resolved against the files an agent session was shown."""
import os
import unittest
from typing import Any, Dict, List, Tuple

from evidence import scan_short_refs
from test_code_wiki import PAGE_ANSWER, READ_BOTH, BuildCase, write
from test_repo_tools import COMMIT, git
from test_retrieval_loop import EngineHarness, FakeRunner, block
from testenv import isolate_data_dir, restore_data_dir

GUARD = "cli/llm-agent/guardian.py"
ENGINE = "cli/llm-agent/debate_engine.py"
DUP_A = "cli/llm-agent/util/dup.py"
DUP_B = "tools/dup.py"
KNOWN = {GUARD, ENGINE, DUP_A, DUP_B, "README.md"}


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def scan(text: str) -> List[Tuple[str, Tuple[str, ...]]]:
    by_name: Dict[str, List[str]] = {}
    for path in KNOWN:
        by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    return [(r.raw, r.candidates) for r in scan_short_refs(text, KNOWN, by_name)]


class ScannerTests(unittest.TestCase):
    def test_short_forms_resolve_to_candidates(self) -> None:
        got = scan("See guardian.py:755, (debate_engine.py:437-440), `llm-agent/guardian.py#Run` and dup.py:3.")
        self.assertEqual(got, [
            ("guardian.py:755", (GUARD,)), ("debate_engine.py:437-440", (ENGINE,)),
            ("llm-agent/guardian.py#Run", (GUARD,)), ("dup.py:3", (DUP_A, DUP_B))])

    def test_bare_basename_and_unknown_files_are_not_citations(self) -> None:
        self.assertEqual(scan("guardian.py handles it; other.py:9 and example.com:443 are not in the tree."), [])

    def test_whole_repository_paths_are_left_to_the_path_scanner(self) -> None:
        self.assertEqual(scan(f"{GUARD}:755 and {GUARD} line 3"), [])

    def test_no_false_positives(self) -> None:
        text = ("Built with go1.26.8 at 12:30 and 9:45-10:15, served on http://host:9870/ by Python 3.13 (v1.2.3), "
                "ratio 3:1, port :8080, version 2026-10-13.p4, e.g. foo.bar:12.")
        self.assertEqual(scan(text), [])

    def test_line_only_follows_the_nearest_file_of_the_paragraph(self) -> None:
        got = scan("The loop lives in guardian.py. It starts at line 12 (line 14) and `debate_engine.py` is "
                   "called at lines 30-35.\n\nA new paragraph mentions line 7.")
        self.assertEqual(got, [("line 12", (GUARD,)), ("line 14", (GUARD,)), ("lines 30-35", (ENGINE,)),
                               ("line 7", ())])

    def test_line_only_forms(self) -> None:
        got = scan("guardian.py: lines 490-506, (L12), 줄 33, line 5 of debate_engine.py, but not Line-up or L5.")
        self.assertEqual([r for r, _ in got], ["lines 490-506", "line 12", "line 33", "line 5"])
        self.assertEqual(got[-1][1], (ENGINE,))
        self.assertEqual(got[0][1], (GUARD,))

    def test_path_attached_line_numbers_are_not_counted_twice(self) -> None:
        self.assertEqual(scan("guardian.py line 755 and guardian.py (line 8)"),
                         [("guardian.py:755", (GUARD,)), ("guardian.py:8", (GUARD,))])

    def test_list_items_inherit_from_their_parent_but_not_from_siblings(self) -> None:
        text = ("- guardian.py\n  - run (line 10)\n- debate_engine.py\n  - ask (line 20)\n- orphan (line 30)\n")
        self.assertEqual(scan(text), [("line 10", (GUARD,)), ("line 20", (ENGINE,)), ("line 30", ())])

    def test_text_fences_are_checked_and_code_fences_are_not(self) -> None:
        text = ("Flow in guardian.py:\n\n```\nmain\n\n  run() (line 510)\n```\n\n"
                "```python\nprint('line 5')  # guardian.py:7 here\n```\n")
        self.assertEqual(scan(text), [("line 510", (GUARD,)), ("guardian.py:7", (GUARD,))])

    def test_findings_block_is_skipped(self) -> None:
        self.assertEqual(scan('```findings\n[{"quote": "see guardian.py:3 line 4"}]\n```\n'), [])


class ShortCitationTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        for path in (GUARD, ENGINE, DUP_A, DUP_B, "cli/llm-agent/other_guardian_only.py"):
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 801)))
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-q", "-m", "files")
        self.sha = git(self.repo, "rev-parse", "HEAD")

    def checks(self) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events() if e["type"] == "citation_check"]

    def run_answer(self, first: str, answer: str):
        runner = FakeRunner(self.plain_scripts([first, answer, "Dropped the claims."]))
        return runner, self.run_debate(self.engine(), runner)

    HUB = ("The dispatcher is guardian.py:755 and debate_engine.py:437.\n\n```\nmain (guardian.py)\n"
           "  -> run()  (line 510)\n```\n\nThe setup is in (guardian.py:655) and (line 490-506).\n")

    def shown(self) -> str:
        return block({"op": "read", "path": f"{GUARD}:750-760"}, {"op": "read", "path": f"{GUARD}:480-520"},
                     {"op": "read", "path": f"{GUARD}:650-660"}, {"op": "read", "path": f"{ENGINE}:430-440"})

    def test_hub_example_is_verified_against_shown_files(self) -> None:
        runner, _ = self.run_answer(self.shown(), self.HUB)
        self.assertEqual(len(runner.prompts("expert")), 2)  # no citation question
        check = self.checks()[0]
        self.assertEqual((check["unverified"], check["asked"], check["short"]), ([], False, 5))
        self.assertEqual(check["cited"], 5)  # no whole path is written; five short citations

    def test_short_citations_outside_shown_lines_are_flagged(self) -> None:
        runner, _ = self.run_answer(block({"op": "read", "path": f"{GUARD}:750-760"}),
                                    "guardian.py:755 is fine, guardian.py:200 is not.")
        self.assertEqual(self.checks()[0]["unverified"], [f"{GUARD}:200"])
        self.assertIn("only shown in part", runner.prompts("expert")[2])

    def test_hub_example_is_unverified_when_nothing_was_shown(self) -> None:
        runner, _ = self.run_answer(block({"op": "read", "path": "apps/agent/notes.txt"}), self.HUB)
        unverified = self.checks()[0]["unverified"]
        self.assertIn(f"guardian.py:755 (never shown: {GUARD})", unverified)
        self.assertIn(f"debate_engine.py:437 (never shown: {ENGINE})", unverified)
        self.assertIn(f"line 510 (never shown: {GUARD})", unverified)
        self.assertTrue(any(u.startswith("line 490") or u.startswith("lines 490") for u in unverified))
        self.assertGreaterEqual(self.checks()[0]["short"], 5)
        ask = runner.prompts("expert")[2]
        self.assertIn("full repository path", ask)
        self.assertIn("cli/llm-agent/guardian.py:755", ask)
        self.assertIn(f"guardian.py:755 (never shown: {GUARD})", ask)

    def test_ambiguous_basename_lists_candidates(self) -> None:
        runner, _ = self.run_answer(block({"op": "read", "path": "apps/agent/notes.txt"}), "The bug is in dup.py:12.")
        self.assertEqual(self.checks()[0]["unverified"], [f"dup.py:12 (ambiguous short path: {DUP_A}, {DUP_B})"])
        self.assertEqual(self.checks()[0]["short"], 0)
        self.assertIn(f"dup.py:12 (ambiguous short path: {DUP_A}, {DUP_B})", runner.prompts("expert")[2])

    def test_shown_candidates_win_over_unseen_tree_candidates(self) -> None:
        self.run_answer(block({"op": "read", "path": f"{DUP_B}:5-15"}), "See dup.py:12.")
        self.assertEqual(self.checks()[0]["unverified"], [])
        self.assertEqual(self.checks()[0]["short"], 1)
        self.run_answer(block({"op": "read", "path": f"{DUP_B}:5-15"}), "See dup.py:100.")
        self.assertEqual(self.checks()[1]["unverified"], [f"{DUP_B}:100"])

    def test_two_shown_candidates_are_ambiguous(self) -> None:
        self.run_answer(block({"op": "read", "path": DUP_A}, {"op": "read", "path": DUP_B}), "See dup.py:12.")
        self.assertEqual(self.checks()[0]["unverified"], [f"dup.py:12 (ambiguous short path: {DUP_A}, {DUP_B})"])

    def test_line_reference_without_a_file(self) -> None:
        runner, _ = self.run_answer(block({"op": "read", "path": "apps/agent/notes.txt"}), "The bug is at line 12.")
        self.assertEqual(self.checks()[0]["unverified"], ["line 12 (line reference without a file)"])
        self.assertEqual(self.checks()[0]["cited"], 1)
        self.assertIn("line 12 (line reference without a file)", runner.prompts("expert")[2])

    def test_version_strings_times_and_urls_cite_nothing(self) -> None:
        runner, _ = self.run_answer(block({"op": "read", "path": "apps/agent/notes.txt"}),
                                    "go1.26.8 at 12:30 via http://host:9870/ on Python 3.13; see guardian.py.")
        self.assertEqual(self.checks(), [])
        self.assertEqual(len(runner.prompts("expert")), 2)

    def test_still_unverified_after_the_reask_is_recorded_in_the_transcript(self) -> None:
        runner = FakeRunner(self.plain_scripts([block({"op": "read", "path": "apps/agent/notes.txt"}),
                                                "See guardian.py:5.", "Still guardian.py:5."]))
        result = self.run_debate(self.engine(), runner)
        entry = next(t for t in result["transcript"] if t["role"] == "Domain Expert")
        self.assertEqual(entry["unverified_citations"], [f"guardian.py:5 (never shown: {GUARD})"])


class WikiShortCitationTests(BuildCase):
    ANSWER = ("# 서비스\n## 역할\n토큰은 auth.go:6 에서 확인한다 (auth.go:6).\n"
              "## 흐름\n```\nserver.go -> Serve (line 3)\n```\n그리고 (line 3) 에서 응답한다.\n"
              "## 주의점\n없음\n## 관련 페이지\n없음\n")

    def test_page_with_short_citations_is_saved_with_nonzero_citations(self) -> None:
        report = self.build(self.scripts(READ_BOTH, self.ANSWER))
        self.assertEqual(report["failed"], 0)
        entry = report["written"][0]
        self.assertGreaterEqual(entry["citations"], 2)  # svc/auth.go and svc/server.go, found via short forms
        self.assertEqual(entry["unverified"], [])
        self.assertEqual(self.store.load("svc").unverified, [])

    def test_unshown_short_citation_is_recorded_on_the_page(self) -> None:
        only_auth = block({"op": "read", "path": "svc/auth.go"})
        report = self.build(self.scripts(only_auth, self.ANSWER, self.ANSWER))
        self.assertTrue(any("server.go" in u and "never shown" in u for u in report["written"][0]["unverified"]))
        self.assertGreaterEqual(report["written"][0]["citations"], 1)


if __name__ == "__main__":
    unittest.main()

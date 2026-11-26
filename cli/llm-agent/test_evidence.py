"""P2.5 evidence: line-span provenance of strong citations and the optional findings block."""
import json
import os
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

from debate_engine import FINDINGS_PROMPT, RETRIEVAL_PROMPT_VERSION, DebateEngine, judge_missing_parts
from evidence import (
    WHOLE_FILE_END,
    check_findings,
    citation_slack,
    coalesce,
    describe_spans,
    parse_findings,
    range_covered,
    strip_findings,
)
from mcp_client import parse_actions, parse_decision, parse_judge_block
from repo_tools import RepoTools, provided_spans
from test_repo_mcp_engine import McpRunner
from test_repo_tools import COMMIT, git
from test_retrieval_loop import READ_NOTES, EngineHarness, FakeRunner, block
from testenv import isolate_data_dir, restore_data_dir

FILLER = "".join(f"// filler line {n}\n" for n in range(9, 30))


def handle_source(package: str) -> str:
    """Lines 1-8 declare Handle (line 6); lines 9-29 are filler comments; Tail is declared on line 30."""
    return (f"package {package}\n\nimport \"fmt\"\n\n// Handle in package {package}.\n"
            f"func Handle(name string) string {{\n\treturn fmt.Sprint(name)\n}}\n{FILLER}func Tail() {{}}\n")


def add_handle_files(harness: EngineHarness) -> None:
    """Commits apps/a.go and apps/b.go, both declaring a Handle on line 6 (the same-name bait)."""
    (harness.repo / "apps/a.go").write_text(handle_source("a"))
    (harness.repo / "apps/b.go").write_text(handle_source("b"))
    git(harness.repo, "add", "-A")
    git(harness.repo, *COMMIT, "commit", "-q", "-m", "handle files")
    harness.sha = git(harness.repo, "rev-parse", "HEAD")


GREP_A = block({"op": "grep", "pattern": "Handle", "glob": "apps/a.go"})  # shows lines 5 and 6 of apps/a.go only


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class SpanHelperTests(unittest.TestCase):
    def test_coalesce_merges_touching_and_overlapping_and_drops_garbage(self) -> None:
        self.assertEqual(coalesce([(10, 12), (1, 3), (4, 4), [11, 20], ("x", 1), (5, 2), None]), [(1, 4), (10, 20)])

    def test_range_rule_needs_the_start_and_half_of_the_lines(self) -> None:
        shown = [(10, 20)]
        self.assertTrue(range_covered(shown, 12, 18, 3))
        self.assertTrue(range_covered(shown, 8, 12, 3))        # start inside the slack, all lines covered
        self.assertFalse(range_covered(shown, 5, 12, 3))       # the start is 5 lines away from the span
        self.assertFalse(range_covered(shown, 15, 100, 3))     # covered start, but far less than half the range
        self.assertTrue(range_covered(shown, 15, 30, 3))       # 15..23 of 16 lines
        self.assertFalse(range_covered(shown, 24, 24, 3))
        self.assertTrue(range_covered(shown, 23, 23, 3))
        self.assertFalse(range_covered(shown, 23, 23, 0))

    def test_describe_spans(self) -> None:
        self.assertEqual(describe_spans([(1, WHOLE_FILE_END)]), "the whole file")
        self.assertEqual(describe_spans([(5, 5), (8, 9)]), "lines 5, 8-9")
        many = [(n * 10, n * 10) for n in range(1, 15)]
        self.assertTrue(describe_spans(many).endswith("(+4 more ranges)"))

    def test_slack_environment(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_CITATION_SLACK": ""}):
            self.assertEqual(citation_slack(), 3)
        for value, expected in (("0", 0), ("7", 7), ("-1", 3), ("many", 3)):
            with patch.dict(os.environ, {"GUARDIAN_CITATION_SLACK": value}):
                self.assertEqual(citation_slack(), expected)


class ProvidedSpansTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        self.tools = RepoTools(self.repo, self.sha)

    def spans(self, **request: Any) -> Dict[str, List[Any]]:
        return provided_spans(self.tools.run(request))

    def test_whole_read_is_the_whole_file(self) -> None:
        self.assertEqual(self.spans(op="read", path="apps/agent/notes.txt"), {"apps/agent/notes.txt": [(1, WHOLE_FILE_END)]})

    def test_truncated_whole_read_is_the_lines_that_arrived(self) -> None:
        spans = self.spans(op="read", path="apps/agent/big.txt")["apps/agent/big.txt"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][0], 1)
        self.assertTrue(1000 < spans[0][1] < 8000)
        clipped = provided_spans(self.tools.run({"op": "read", "path": "apps/agent/notes.txt"}, max_bytes=70))
        self.assertEqual(clipped["apps/agent/notes.txt"], [(1, 1)])

    def test_partial_read_has_its_lines_the_import_block_and_outline_entries(self) -> None:
        got = self.spans(op="read", path="apps/agent/main.go#Serve")
        self.assertEqual(got, {"apps/agent/main.go": [(1, 1), (3, 3), (5, 8), (10, 10)]})
        self.assertEqual(self.spans(op="read", path="apps/agent/main.go:11-12")["apps/agent/main.go"],
                         [(1, 1), (3, 3), (6, 6), (10, 12)])

    def test_outline_grep_and_refs_give_matched_lines_defs_map_list_nothing(self) -> None:
        self.assertEqual(self.spans(op="outline", path="apps/agent/main.go"),
                         {"apps/agent/main.go": [(1, 1), (6, 6), (10, 10)]})
        self.assertEqual(self.spans(op="grep", pattern="TARGET", glob="apps/agent/notes.txt"),
                         {"apps/agent/notes.txt": [(2, 2)]})
        add_handle_files(self)
        tools = RepoTools(self.repo, self.sha)
        refs = provided_spans(tools.run({"op": "refs", "symbol": "Handle", "include_defs": True, "glob": "apps/a.go"}))
        self.assertEqual(refs, {"apps/a.go": [(6, 6)]})
        for op in ({"op": "defs", "symbol": "Handle"}, {"op": "map"}, {"op": "list"}):
            self.assertEqual(provided_spans(tools.run(op)), {})
        self.assertEqual(provided_spans(tools.run({"op": "read", "path": "apps/agent/missing.txt"})), {})

    def test_spans_cover_exactly_the_paths_provided_paths_names(self) -> None:
        from repo_tools import provided_paths

        for request in ({"op": "read", "path": "cli/tool.py#run"}, {"op": "outline", "path": "cli/tool.py"},
                        {"op": "grep", "pattern": "TARGET", "glob": "apps/agent/*.txt"}):
            result = self.tools.run(request)
            self.assertEqual(sorted(provided_spans(result)), sorted(provided_paths(result)))

    def test_cited_locations(self) -> None:
        got = self.tools.cited_locations(
            "apps/agent/main.go:5, apps/agent/main.go:8-12 and apps/agent/main.go#Serve; cli/tool.py line 3, "
            "`cli/tool.py` (L4-L6) and bare apps/agent/notes.txt.")
        self.assertEqual(got["apps/agent/main.go"],
                         (True, [("lines", 5, 5), ("lines", 8, 12), ("symbol", "Serve")]))
        self.assertEqual(got["cli/tool.py"], (True, [("lines", 3, 3), ("lines", 4, 6)]))
        self.assertEqual(got["apps/agent/notes.txt"], (False, []))
        self.assertEqual(self.tools.cited_refs("apps/agent/main.go:5 apps/agent/notes.txt"),
                         {"apps/agent/main.go": True, "apps/agent/notes.txt": False})


class SpanCitationTests(EngineHarness):
    """The same-name Handle bait and the line rules, through the engine's citation check."""

    def setUp(self) -> None:
        super().setUp()
        add_handle_files(self)

    def expert_entry(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return next(t for t in result["transcript"] if t["role"] == "Domain Expert")

    def checks(self) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events() if e["type"] == "citation_check"]

    def run_answer(self, answer: str, first: str = GREP_A, **kwargs: Any):
        runner = FakeRunner(self.plain_scripts([first, answer, "Dropped the claim."]))
        return runner, self.run_debate(self.engine(), runner, **kwargs)

    def test_a_shown_line_passes_a_line_of_the_other_file_and_a_far_line_do_not(self) -> None:
        runner, _ = self.run_answer("Handle at apps/a.go:6 checks out.")
        self.assertEqual(len(runner.prompts("expert")), 2)  # no citation question
        self.assertEqual(self.checks(), [{"cited": 1, "unverified": [], "asked": False, "mentioned": 0, "short": 0}])

    def test_bait_same_name_function_in_a_file_that_was_never_shown_is_unverified(self) -> None:
        runner, result = self.run_answer("Handle at apps/a.go:6 and the identical apps/b.go:6.")
        ask = runner.prompts("expert")[2]
        self.assertIn("CITATION CHECK", ask)
        self.assertIn("never given: apps/b.go.", ask)
        self.assertNotIn("only shown in part", ask)
        self.assertEqual(self.checks()[0]["unverified"], ["apps/b.go"])
        self.assertEqual(self.expert_entry(result)["content"], "Dropped the claim.")

    def test_bait_far_line_of_the_shown_file_is_unverified_and_the_ask_lists_the_shown_lines(self) -> None:
        runner, _ = self.run_answer("Handle at apps/a.go:6, and the cleanup at apps/a.go:20.")
        ask = runner.prompts("expert")[2]
        self.assertIn("lines of files you were only shown in part: apps/a.go:20 (you were shown lines 5-6)", ask)
        self.assertNotIn("never given", ask)
        self.assertEqual(self.checks()[0]["unverified"], ["apps/a.go:20"])

    def test_unanswered_bait_is_recorded_with_the_citation(self) -> None:
        runner = FakeRunner(self.plain_scripts([GREP_A, "See apps/a.go:20.", "Still apps/a.go:20 and apps/b.go#Handle."]))
        result = self.run_debate(self.engine(), runner)
        self.assertEqual(self.expert_entry(result)["unverified_citations"], ["apps/b.go", "apps/a.go:20"])

    def test_slack_decides_how_close_a_citation_may_be(self) -> None:
        # Shown: lines 5-6. With the default slack of 3 line 9 is accepted and line 10 is not.
        runner, _ = self.run_answer("apps/a.go:9")
        self.assertEqual(self.checks()[0]["unverified"], [])
        runner, _ = self.run_answer("apps/a.go:10")
        self.assertEqual(self.checks()[1]["unverified"], ["apps/a.go:10"])
        with patch.dict(os.environ, {"GUARDIAN_CITATION_SLACK": "0"}):
            runner, _ = self.run_answer("apps/a.go:7")
        self.assertEqual(self.checks()[2]["unverified"], ["apps/a.go:7"])
        with patch.dict(os.environ, {"GUARDIAN_CITATION_SLACK": "20"}):
            runner, _ = self.run_answer("apps/a.go:26")
        self.assertEqual(self.checks()[3]["unverified"], [])

    def test_ranges_need_their_start_and_half_of_their_lines(self) -> None:
        for citation, ok in (("apps/a.go:4-10", True), ("apps/a.go:5-30", False), ("apps/a.go:12-14", False),
                             ("apps/a.go L5-L8", True), ("apps/a.go lines 1-30", False)):
            with self.subTest(citation=citation):
                self.events_path.unlink(missing_ok=True)
                self.run_answer(f"See {citation}.")
                self.assertEqual(self.checks()[0]["unverified"] == [], ok)

    def test_symbol_citations_need_the_declaration_to_overlap_a_shown_line(self) -> None:
        # The grep showed lines 5-6; Handle's declaration (5-8) overlaps, Tail's (line 30) does not.
        self.run_answer("apps/a.go#Handle does it.")
        self.assertEqual(self.checks()[0]["unverified"], [])
        self.events_path.unlink()
        self.run_answer("apps/a.go#Tail does it.")
        self.assertEqual(self.checks()[0]["unverified"], ["apps/a.go#Tail"])
        self.events_path.unlink()
        # A symbol the parser cannot find only needs the file to have been shown in part; a file never shown fails.
        self.run_answer("apps/a.go#NoSuchThing and apps/b.go#Handle.")
        self.assertEqual(self.checks()[0]["unverified"], ["apps/b.go"])

    def test_partial_read_counts_the_lines_the_import_block_and_the_outline_entries(self) -> None:
        # path#Handle shows 5-8, the import block (3) and outline entries (1, 6, 30).
        first = block({"op": "read", "path": "apps/a.go#Handle"})
        for citation, ok in (("apps/a.go:7", True), ("apps/a.go:3", True), ("apps/a.go:30", True),
                             ("apps/a.go:20", False), ("apps/a.go#Tail", True)):
            with self.subTest(citation=citation):
                self.events_path.unlink(missing_ok=True)
                self.run_answer(f"See {citation}.", first=first)
                self.assertEqual(self.checks()[0]["unverified"] == [], ok)

    def test_whole_file_backs_every_line_and_so_does_a_whole_file_of_the_context_pack(self) -> None:
        self.run_answer("apps/a.go:29 and apps/a.go#Tail.", first=block({"op": "read", "path": "apps/a.go"}))
        self.assertEqual(self.checks()[0]["unverified"], [])
        self.events_path.unlink()
        runner = FakeRunner(self.plain_scripts(["apps/a.go:29 and apps/a.go#Tail.", "Dropped."]))
        self.run_debate(self.engine(), runner, context_paths=["apps/a.go"])
        self.assertEqual(self.checks()[0]["unverified"], [])

    def test_context_pack_selection_backs_only_its_lines(self) -> None:
        runner = FakeRunner(self.plain_scripts(["apps/a.go:7 yes, apps/a.go:20 no.", "Dropped."]))
        self.run_debate(self.engine(), runner, context_paths=["apps/a.go#Handle"])
        self.assertEqual(self.checks()[0]["unverified"], ["apps/a.go:20"])

    def test_truncated_read_backs_only_the_lines_that_arrived(self) -> None:
        first = block({"op": "read", "path": "apps/agent/big.txt"})
        self.run_answer("apps/agent/big.txt:1 and apps/agent/big.txt:7000.", first=first)
        self.assertEqual(self.checks()[0]["unverified"], ["apps/agent/big.txt:7000"])

    def test_defs_and_list_show_no_lines_so_a_strong_citation_stays_unverified(self) -> None:
        first = block({"op": "defs", "symbol": "Handle"}, {"op": "list", "path": "apps"})
        self.run_answer("apps/a.go:6.", first=first)
        self.assertEqual(self.checks()[0]["unverified"], ["apps/a.go"])

    def test_bare_mentions_keep_the_file_level_rule(self) -> None:
        self.run_answer("The Handle in apps/a.go is the one.")
        self.assertEqual(self.checks()[0]["unverified"], [])

    def test_spans_are_persisted_per_session_and_a_resumed_run_still_checks_lines(self) -> None:
        first = self.run_debate(self.engine(), FakeRunner(self.plain_scripts([GREP_A, "Done."])))
        stored = first["context"]["provided_spans"]["Domain Expert"]
        self.assertEqual(stored, {"apps/a.go": [[5, 6]]})
        self.assertEqual(first["context"]["provided_files"]["Domain Expert"], ["apps/a.go"])
        runner = FakeRunner(self.plain_scripts(["apps/a.go:6 yes.", ]))
        self.run_debate(self.engine(), runner, thread_id=first["thread_id"])
        self.assertEqual(len(runner.prompts("expert")), 1)
        self.events_path.unlink()
        runner = FakeRunner(self.plain_scripts(["apps/a.go:20 no.", "Dropped."]))
        second = self.run_debate(self.engine(), runner, thread_id=first["thread_id"])
        self.assertEqual(self.checks()[0]["unverified"], ["apps/a.go:20"])
        self.assertEqual(second["context"]["provided_spans"]["Domain Expert"], {"apps/a.go": [[5, 6]]})

    def test_a_thread_stored_before_p2_5_keeps_the_file_level_rule(self) -> None:
        engine = self.engine()
        first = self.run_debate(engine, FakeRunner(self.plain_scripts([GREP_A, "Done."])))
        history = engine.db.get_thread_history(first["thread_id"])
        context = {k: v for k, v in history["context"].items() if k != "provided_spans"}
        engine.db.save_debate_session("server-guardian", first["thread_id"], history["task"], history["transcript"],
                                      None, team=history["team"], context=context)
        runner = FakeRunner(self.plain_scripts(["apps/a.go:20 was shown in an earlier run."]))
        self.run_debate(self.engine(), runner, thread_id=first["thread_id"])
        self.assertEqual(len(runner.prompts("expert")), 1)  # file-level: no question
        self.assertEqual(self.checks()[0]["unverified"], [])
        # A file read after the upgrade is checked by lines, whatever the old run provided.
        runner = FakeRunner(self.plain_scripts([block({"op": "grep", "pattern": "Handle", "glob": "apps/b.go"}),
                                                "apps/b.go:20 and apps/a.go:20.", "Dropped."]))
        self.events_path.unlink()
        self.run_debate(self.engine(), runner, thread_id=first["thread_id"])
        self.assertEqual(self.checks()[0]["unverified"], ["apps/b.go:20"])

    def test_spans_over_the_cap_fall_back_to_the_file_level_set(self) -> None:
        many = [{"op": "grep", "pattern": f"filler line {n}$", "glob": "apps/a.go"} for n in range(9, 29, 2)]
        with patch("debate_engine.MAX_SPANS_PER_PATH", 3):
            first = self.run_debate(self.engine(), FakeRunner(self.plain_scripts([block(*many), "Done."])))
        self.assertNotIn("Domain Expert", first["context"].get("provided_spans", {}))
        self.assertEqual(first["context"]["provided_files"]["Domain Expert"], ["apps/a.go"])
        runner = FakeRunner(self.plain_scripts(["apps/a.go:29 is covered at file level."]))
        self.run_debate(self.engine(), runner, thread_id=first["thread_id"])
        self.assertEqual(len(runner.prompts("expert")), 1)


class McpSpanTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        add_handle_files(self)
        self._env = patch.dict(os.environ, {"GUARDIAN_REPO_MCP": "1"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        super().tearDown()

    def test_mcp_calls_back_citations_by_the_logged_spans(self) -> None:
        from team_manager import Lineup, Member

        lineup = Lineup(id="t", name="T", expert=Member("domain_architect", ["claude"], "mcp"),
                        critics=[Member("critic_redteam", ["claude"], None)],
                        judge=Member("jev_mcp_decision", ["claude"], None))
        runner = McpRunner(self.plain_scripts(["Handle at apps/a.go:6, far apps/a.go:20, other apps/b.go:6.", "Dropped."]),
                           {"expert": [("repo_grep", {"pattern": "Handle", "glob": "apps/a.go"})]})
        engine = self.engine()
        with patch.object(engine, "_resolve_lineup", return_value=lineup):
            result = self.run_debate(engine, runner)
        check = [e["data"] for e in self.events() if e["type"] == "citation_check"][0]
        self.assertEqual(check["unverified"], ["apps/b.go", "apps/a.go:20"])
        ask = runner.prompts("expert")[1]
        self.assertIn("never given: apps/b.go.", ask)
        self.assertIn("apps/a.go:20 (you were shown lines 5-6)", ask)
        self.assertEqual(result["context"]["provided_spans"]["Domain Expert"], {"apps/a.go": [[5, 6]]})
        self.assertNotIn("filler", json.dumps(self.events()))  # events carry numbers, not code

    def test_a_log_record_without_spans_backs_its_files_at_file_level(self) -> None:
        engine = self.engine()
        engine._reset_spans(None)
        engine._repo = RepoTools(self.repo, self.sha)
        engine._pack = None
        engine._transcript = []
        engine._earlier_context_files, engine._earlier_provided, engine._earlier_mentioned = set(), {}, {}
        engine._provided, engine._mentioned, engine._pack_mentions = {}, {}, set()
        with engine._retrieval_lock:
            engine._note_shown("k", "Domain Expert", "x", {}, unspanned=["apps/a.go"])
        check = engine._citation_check("k", "Domain Expert", "x", "apps/a.go:25 and apps/b.go:3")
        self.assertEqual(check.unverified, ["apps/b.go"])


class FindingsTests(EngineHarness):
    GOOD = '{"claim": "notes has a target", "path": "apps/agent/notes.txt", "line": 2, "quote": "beta TARGET gamma"}'
    FAKE = '{"claim": "notes has a secret", "path": "apps/agent/notes.txt", "line": 2, "quote": "beta SECRET gamma"}'

    @staticmethod
    def with_findings(prose: str, *items: str) -> str:
        return f"{prose}\n```findings\n[{', '.join(items)}]\n```"

    def expert_entry(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return next(t for t in result["transcript"] if t["role"] == "Domain Expert")

    def of(self, kind: str) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events() if e["type"] == kind and e.get("role") == "Domain Expert"]

    def test_matching_findings_pass_without_a_reask_and_are_recorded(self) -> None:
        answer = self.with_findings("Notes hold the target.", self.GOOD)
        runner = FakeRunner(self.plain_scripts([READ_NOTES, answer]))
        result = self.run_debate(self.engine(), runner)
        self.assertEqual(len(runner.prompts("expert")), 2)
        event = self.of("evidence_check")[0]
        self.assertEqual((event["checked"], event["mismatched"]), (1, 0))
        self.assertEqual(event["items"], [{"path": "apps/agent/notes.txt", "line": 2, "ok": True, "reason": "verified"}])
        entry = self.expert_entry(result)
        self.assertEqual(entry["findings"], {"checked": 1, "mismatched": 0})
        self.assertIn("```findings", entry["content"])  # stored answers keep the block
        self.assertEqual(result["retrieval"]["findings"]["claude-cli"],
                         {"answers": 3, "with_findings": 1, "checked": 1, "mismatched": 0})

    def test_a_fabricated_quote_is_a_mismatch_folded_into_the_one_reask(self) -> None:
        bad = self.with_findings("Notes hold a secret.", self.GOOD, self.FAKE)
        fixed = self.with_findings("Notes hold the target.", self.GOOD)
        runner = FakeRunner(self.plain_scripts([READ_NOTES, bad, fixed]))
        result = self.run_debate(self.engine(), runner)
        ask = runner.prompts("expert")[2]
        self.assertIn("CITATION CHECK", ask)
        self.assertIn("findings do not match the code at the pinned commit: apps/agent/notes.txt:2 "
                      "(quote not found within 3 lines of line 2)", ask)
        events = self.of("evidence_check")
        self.assertEqual([(e["checked"], e["mismatched"]) for e in events], [(2, 1), (1, 0)])
        self.assertEqual(self.expert_entry(result)["content"], fixed)
        self.assertEqual(self.expert_entry(result)["findings"], {"checked": 1, "mismatched": 0})
        self.assertEqual(result["retrieval"]["findings"]["claude-cli"]["mismatched"], 0)

    def test_citation_and_findings_problems_share_one_reask(self) -> None:
        answer = self.with_findings("Also cli/tool.py:3.", self.FAKE)
        runner = FakeRunner(self.plain_scripts([READ_NOTES, answer, answer]))
        result = self.run_debate(self.engine(), runner)
        prompts = runner.prompts("expert")
        self.assertEqual(len(prompts), 3)  # results, then one re-ask; the second bad answer is accepted
        self.assertIn("never given: cli/tool.py", prompts[2])
        self.assertIn("findings do not match", prompts[2])
        entry = self.expert_entry(result)
        self.assertEqual(entry["findings"], {"checked": 1, "mismatched": 1})
        self.assertEqual(entry["unverified_citations"], ["cli/tool.py"])
        self.assertEqual(result["retrieval"]["findings"]["claude-cli"]["mismatched"], 1)

    def test_path_outside_readable_paths_and_bad_items_are_mismatches(self) -> None:
        answer = self.with_findings(
            "x", '{"claim": "c", "path": "apps/agent/.env", "line": 1, "quote": "TARGET=ENV_SECRET"}',
            '{"claim": "c", "path": "nope.txt", "line": 1, "quote": "x"}',
            '{"claim": "c", "path": "apps/agent/notes.txt", "line": 99, "quote": "beta"}', '"just a string"')
        reader = RepoTools(self.repo, self.sha)
        items = check_findings(reader, parse_findings(answer) or [], 3)
        self.assertEqual([i["ok"] for i in items], [False] * 4)
        self.assertIn("not a readable file", items[0]["reason"])
        self.assertIn("not a readable file", items[1]["reason"])
        self.assertIn("beyond the end", items[2]["reason"])
        self.assertEqual(items[3]["reason"], "item is not an object")

    def test_quote_matching_rules(self) -> None:
        reader = RepoTools(self.repo, self.sha)

        def ok(line: int, quote: str, path: str = "apps/agent/notes.txt", slack: int = 3) -> bool:
            return check_findings(reader, [{"path": path, "line": line, "quote": quote}], slack)[0]["ok"]

        self.assertTrue(ok(2, "  beta    TARGET\tgamma "))        # whitespace-normalized
        self.assertTrue(ok(2, "2| beta TARGET gamma"))            # the "N| " prefix of a result is dropped
        self.assertTrue(ok(1, "beta TARGET gamma"))               # the line may be off by the slack
        self.assertTrue(ok(1, "alpha\nbeta TARGET gamma\ndelta"))  # several lines
        self.assertTrue(ok(2, "beta TARGET gamma", slack=0))
        self.assertFalse(ok(1, "beta TARGET gamma", slack=0))
        self.assertFalse(ok(2, "gamma TARGET beta"))
        self.assertFalse(ok(2, "   "))
        self.assertTrue(ok(3500, "line 3499 TARGET", path="apps/agent/big.txt", slack=0))
        self.assertFalse(ok(3000, "line 3499 TARGET", path="apps/agent/big.txt"))
        self.assertTrue(ok(2, "beta TARGET gamma" + " " * 1000))  # normalized away before the 300 character cut

    def test_block_parsing_limits_and_invalid_blocks(self) -> None:
        self.assertIsNone(parse_findings("no block ```json\n[]\n```"))
        self.assertEqual(parse_findings("```findings\nnot json\n```"), [])
        self.assertEqual(parse_findings('```findings\n{"a": 1}\n```'), [])
        many = json.dumps([{"path": "a", "line": 1, "quote": "x"}] * 80)
        self.assertEqual(len(parse_findings(f"```findings\n{many}\n```") or []), 50)
        last = parse_findings('```findings\n[1]\n```\ntext\n```findings\n[1, 2]\n```')
        self.assertEqual(last, [1, 2])
        self.assertEqual(strip_findings('a\n```findings\n[1]\n```\nb'), "a\n\nb")

    def test_an_invalid_block_is_present_but_checks_nothing(self) -> None:
        runner = FakeRunner(self.plain_scripts(["Done.\n```findings\nnot json\n```"]))
        result = self.run_debate(self.engine(), runner)
        self.assertEqual((self.of("evidence_check")[0]["checked"], self.of("evidence_check")[0]["mismatched"]), (0, 0))
        self.assertEqual(self.expert_entry(result)["findings"], {"checked": 0, "mismatched": 0})
        self.assertEqual(result["retrieval"]["findings"]["claude-cli"]["with_findings"], 1)

    def test_no_block_no_event_and_the_answer_still_counts(self) -> None:
        result = self.run_debate(self.engine(), FakeRunner(self.plain_scripts(["Plain answer."])))
        self.assertEqual(self.of("evidence_check"), [])
        self.assertNotIn("findings", self.expert_entry(result))
        self.assertEqual(result["retrieval"]["findings"]["claude-cli"],
                         {"answers": 3, "with_findings": 0, "checked": 0, "mismatched": 0})

    def test_items_in_the_event_are_capped_at_50(self) -> None:
        items = [self.GOOD] * 60
        runner = FakeRunner(self.plain_scripts([READ_NOTES, self.with_findings("many", *items)]))
        self.run_debate(self.engine(), runner)
        event = self.of("evidence_check")[0]
        self.assertEqual((event["checked"], len(event["items"])), (50, 50))

    def test_usage_is_reported_per_provider(self) -> None:
        answer = self.with_findings("Notes hold the target.", self.GOOD)

        class TwoProviders(FakeRunner):
            def __call__(self, prompt, context=None, session_id=None, provider=None, providers=None):
                result = super().__call__(prompt, context, session_id, provider, providers)
                if self.role_of(prompt, session_id) != "expert":
                    return type(result)("codex-cli", result.text, result.session_id, result.resumed, result.failures)
                return result

        runner = TwoProviders(self.plain_scripts([READ_NOTES, answer]))
        result = self.run_debate(self.engine(), runner)
        self.assertEqual(result["retrieval"]["findings"], {
            "claude-cli": {"answers": 1, "with_findings": 1, "checked": 1, "mismatched": 0},
            "codex-cli": {"answers": 2, "with_findings": 0, "checked": 0, "mismatched": 0}})
        finished = [e for e in self.events() if e["type"] == "debate_finished"][0]
        self.assertEqual(finished["data"]["retrieval"]["findings"], result["retrieval"]["findings"])


class JudgeFormatTests(EngineHarness):
    DECISION = ('```json\n{"decision": "Use the wrapper.", "rationale": "because", "constraints": ["Retries are capped."], '
                '"checks": [], "actions": []}\n```')
    # Findings quote code that itself contains verdict lines and a fenced json example.
    FINDINGS = ('```findings\n[{"claim": "format", "path": "cli/tool.py", "line": 1, '
                '"quote": "End with VERDICT: REJECTED then ```json {\\"actions\\": [1]} ```"}]\n```')

    def test_findings_do_not_confuse_the_verdict_or_the_decision_block(self) -> None:
        for answer in (f"Analysis.\n{self.DECISION}\n{self.FINDINGS}\nVERDICT: APPROVED",
                       f"Analysis.\n{self.FINDINGS}\n{self.DECISION}\nVERDICT: APPROVED",
                       f"Analysis.\n{self.DECISION}\nVERDICT: APPROVED\n{self.FINDINGS}"):
            with self.subTest(answer=answer[-60:]):
                self.assertTrue(DebateEngine._parse_verdict(answer))
                self.assertEqual(judge_missing_parts(answer), [])
                self.assertEqual(parse_decision(answer), "Use the wrapper.")
                self.assertEqual(parse_actions(answer), [])
                self.assertEqual(parse_judge_block(answer)["constraints"], ["Retries are capped."])
        rejected = f"No.\n{self.FINDINGS.replace('REJECTED', 'APPROVED')}\nVERDICT: REJECTED"
        self.assertFalse(DebateEngine._parse_verdict(rejected))
        self.assertEqual(judge_missing_parts(f"Analysis.\n{self.FINDINGS}"), ["verdict"])

    def test_an_approving_judge_with_findings_reaches_consensus_and_keeps_the_block(self) -> None:
        answer = f"Analysis.\n{self.DECISION}\n{self.FINDINGS}\nVERDICT: APPROVED"
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": [answer]})
        result = self.run_debate(self.engine(), runner)
        self.assertTrue(result["consensus_reached"])
        judge = result["transcript"][2]
        self.assertIn("```findings", judge["content"])
        self.assertEqual(result["final_decision"]["decision"], "Use the wrapper.")


class PromptTests(EngineHarness):
    def test_version_and_findings_instruction_in_the_repo_block_and_mcp_prompts(self) -> None:
        self.assertEqual(RETRIEVAL_PROMPT_VERSION, "2026-11-11.p29")
        runner = FakeRunner(self.plain_scripts(["x"]))
        self.run_debate(self.engine(), runner)
        for role in ("expert", "critic", "judge"):
            first = runner.prompts(role)[0]
            self.assertIn(FINDINGS_PROMPT, first)
            self.assertIn("```findings", first)
        judge_prompt = runner.prompts("judge")[0]
        self.assertIn("VERDICT: APPROVED", judge_prompt)  # the required format is still there
        self.assertIn("before any final marker line", judge_prompt)
        self.assertIn("```findings\n[{", judge_prompt)
        text = DebateEngine._mcp_text(self.engine())
        self.assertIn("```findings", text)


if __name__ == "__main__":
    unittest.main()

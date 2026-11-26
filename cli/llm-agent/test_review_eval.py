"""V1e harness (review_eval.py): bundling, planting, prepare, the runner script and the scoring. No model is called."""
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

import review_eval
from review_eval import EvalError

COMMIT = ("-c", "user.email=t@t", "-c", "user.name=t")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_source(root: Path, files: Dict[str, str]) -> Path:
    repo = root / "src"
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", "base")
    return repo


def defect(ident: str, kind: str, path: str, line: int, original: str, seeded: str) -> Dict[str, Any]:
    return {"id": ident, "type": kind, "path": path, "line": line, "lines": [line, line], "function": f"fn_{ident.lower()}",
            "description": "d", "original": original, "seeded": seeded}


class PureTests(unittest.TestCase):
    def test_bundle_deals_one_kind_per_case(self) -> None:
        items = [defect(f"{k}-{n}", k, "a.py", 1, "x", "y") for k in ("A", "B", "C", "D") for n in (1, 2)]
        cases = review_eval.bundle(items, 4)
        self.assertEqual(len(cases), 2)
        for case in cases:
            self.assertEqual(sorted(d["type"] for d in case), ["A", "B", "C", "D"])

    def test_plant_needs_exactly_one_original(self) -> None:
        item = defect("A-1", "A", "a.py", 2, "return ok", "return not ok")
        self.assertEqual(review_eval.plant("x\nreturn ok\n", item), "x\nreturn not ok\n")
        for text in ("x\n", "return ok\nreturn ok\n"):
            with self.assertRaises(EvalError):
                review_eval.plant(text, item)

    def test_debate_mentions_needs_the_file_and_a_near_line_or_the_function(self) -> None:
        item = defect("A-1", "A", "cli/llm-agent/repo_tools.py", 451, "o", "s")
        self.assertTrue(review_eval.debate_mentions("broken at repo_tools.py:453", item))
        self.assertTrue(review_eval.debate_mentions("repo_tools.py: fn_a-1 is wrong"
                                                    .replace("fn_a-1", "fn_a"), {**item, "function": "fn_a"}))
        self.assertFalse(review_eval.debate_mentions("repo_tools.py:400 looks odd", item))
        self.assertFalse(review_eval.debate_mentions("something at line 451", item))

    def test_decide_applies_the_recall_and_time_shares(self) -> None:
        base = {"review": {"cases": 2, "recall": 0.75, "mean_seconds": 100.0},
                "debate": {"cases": 2, "recall": 0.9, "mean_seconds": 600.0}}
        self.assertTrue(review_eval.decide(base)["recall_ok"])
        self.assertTrue(review_eval.decide(base)["time_ok"])
        slow = {**base, "review": {**base["review"], "mean_seconds": 200.0}}
        self.assertEqual(review_eval.decide(slow)["decision"], "do not adopt yet")
        self.assertEqual(review_eval.decide({**base, "debate": {**base["debate"], "cases": 1}})["decision"], "incomplete")

    def test_run_script_moves_the_tmp_into_place_only_after_success(self) -> None:
        manifest = {"workspace": "/ws", "cases": [{"id": "case-1", "base": "b000", "head": "h000"}]}
        script = review_eval.run_script(Path("/out"), manifest)
        self.assertIn('if [ ! -f "$d/review.json" ]; then', script)  # the rerun check stays on the final file
        self.assertIn('--json > "$d/review.json.tmp" 2> "$d/review.log" &&', script)
        self.assertIn('mv "$d/review.json.tmp" "$d/review.json"', script)
        self.assertNotIn('> "$d/review.json" 2>', script)  # never redirect straight into the final file
        self.assertIn('--result-file "$d/debate-result.json.tmp"', script)
        self.assertIn('mv "$d/debate-result.json.tmp" "$d/debate-result.json"', script)
        self.assertNotIn('--result-file "$d/debate-result.json"', script)

    def test_decide_is_incomplete_when_a_run_is_not_done(self) -> None:
        base = {"review": {"cases": 2, "recall": 0.9, "mean_seconds": 100.0, "not_done": 1},
                "debate": {"cases": 2, "recall": 0.9, "mean_seconds": 600.0}}
        verdict = review_eval.decide(base)
        self.assertEqual(verdict["decision"], "incomplete")
        self.assertIn("1", verdict["reason"])
        clean = {"review": {**base["review"], "not_done": 0}, "debate": {**base["debate"], "not_done": 0}}
        self.assertEqual(review_eval.decide(clean)["decision"], "keep as default first check (precision pending)")

    def test_summarize_counts_unfinished_debate_runs(self) -> None:
        def row(case: str, debate: Dict[str, Any]) -> Dict[str, Any]:
            return {"case": case, "defects": ["A-1"], "review": None, "debate": debate}

        ok = {"found": {"A-1": True}, "seconds": 10.0, "fallback_agents": 0, "finished": True}
        rows = [row("case-1", ok),
                row("case-2", {**ok, "finished": False}),
                row("case-3", {k: v for k, v in ok.items() if k != "finished"})]  # older row without the key
        summary = review_eval.summarize(rows)
        self.assertEqual(summary["debate"]["not_done"], 2)
        self.assertEqual(summary["debate"]["fallback_agents"], 0)
        self.assertEqual(review_eval.summarize([row("case-1", ok)])["debate"]["not_done"], 0)

    def test_decide_is_incomplete_when_a_debate_never_finished(self) -> None:
        def row(finished: bool) -> Dict[str, Any]:
            return {"case": "case-1", "defects": ["A-1"],
                    "review": {"found": {"A-1": True}, "findings": 1, "matched_findings": 1, "in_notes": {},
                               "seconds": 10.0, "state": "done"},
                    "debate": {"found": {"A-1": True}, "seconds": 100.0, "fallback_agents": 0, "finished": finished}}

        verdict = review_eval.decide(review_eval.summarize([row(True), row(False)]))
        self.assertEqual(verdict["decision"], "incomplete")
        self.assertIn("1 debate", verdict["reason"])
        clean = review_eval.decide(review_eval.summarize([row(True), row(True)]))
        self.assertEqual(clean["decision"], "keep as default first check (precision pending)")


class PrepareAndScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = make_source(self.root, {
            "a.py": "def a():\n    return 1\n", "b.py": "def b():\n    return 2\n",
            "c.py": "def c():\n    return 3\n", "d.py": "def d():\n    return 4\n"})
        self.items: List[Dict[str, Any]] = [
            defect("A-1", "A", "a.py", 2, "    return 1", "    return -1"),
            defect("B-1", "B", "b.py", 2, "    return 2", "    return -2"),
            defect("C-1", "C", "c.py", 2, "    return 3", "    return -3"),
            defect("D-1", "D", "d.py", 2, "    return 4", "    return -4"),
        ]
        self.truth = self.root / "truth.json"
        self.truth.write_text(json.dumps(self.items))
        self.dest = self.root / "eval"
        self.data = self.root / "data"

    def prepare(self, **kwargs: Any) -> Dict[str, Any]:
        return review_eval.prepare(self.dest, self.source, base="main", data_dir=self.data, truth=self.truth, **kwargs)

    def test_prepare_commits_one_case_per_bundle_and_writes_the_project_and_runner(self) -> None:
        manifest = self.prepare(per_case=2)
        self.assertEqual([c["id"] for c in manifest["cases"]], ["case-1", "case-2"])
        ws = Path(manifest["workspace"])
        first = manifest["cases"][0]
        diff = git(ws, "diff", first["base"], first["head"])
        self.assertIn("+    return -1", diff)
        self.assertIn("+    return -2", diff)
        self.assertNotIn("return -3", diff)
        self.assertEqual(git(ws, "rev-parse", "HEAD"), manifest["base"])  # left at the base, detached
        project = json.loads((self.data / "projects" / "review-eval.json").read_text())
        self.assertEqual(project["id"], "review-eval")
        script = (self.dest / "run.sh").read_text()
        self.assertIn(f"--range {first['base']}..{first['head']}", script)
        self.assertIn("--team default", script)
        self.assertIn("refusing the production data dir", script)
        self.assertIn("GUARDIAN_AUTO_APPLY=off", script)
        with self.assertRaises(EvalError):
            self.prepare()  # not empty any more

    def test_prepare_refuses_a_defect_that_does_not_apply_at_the_base(self) -> None:
        bad = self.items + [defect("E-1", "E", "a.py", 9, "not in the file", "x")]
        self.truth.write_text(json.dumps(bad))
        with self.assertRaises(EvalError):
            self.prepare()

    def test_score_counts_review_findings_notes_and_debate_mentions(self) -> None:
        manifest = self.prepare(per_case=4)
        run = self.dest / "runs" / "case-1"
        run.mkdir(parents=True)
        (run / "review.json").write_text(json.dumps({
            "ok": True, "state": "done", "elapsed_s": 60.0, "degraded": False,
            "findings": [{"path": "a.py", "line": 2, "claim": "sign flipped"}, {"path": "b.py", "line": 30, "claim": "far"},
                         {"path": "zzz.py", "line": 1, "claim": "unrelated"}],
            "notes": [{"path": "c.py", "line": 3, "claim": "set aside", "reason": "not on a changed line"}]}))
        (run / "debate-result.json").write_text(json.dumps({
            "final_decision": {"decision": "a.py:2 and d.py:2 flip the sign; b.py fn_b-1 too".replace("fn_b-1", "fn_b")},
            "transcript": [{"role": "Critic Agent", "content": "c.py:40 maybe"}]}))
        (run / "debate-events.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "debate_started", "ts": "2026-11-24T10:00:00Z"},
            {"type": "debate_finished", "ts": "2026-11-24T10:10:00Z"}]))
        for item in self.items:  # the scorer reads the function name from the truth file
            item["function"] = f"fn_{item['id'][0].lower()}"
        self.truth.write_text(json.dumps(self.items))
        report = review_eval.score(self.dest)
        row = report["rows"][0]
        self.assertEqual(row["review"]["found"], {"A-1": True, "B-1": False, "C-1": False, "D-1": False})
        self.assertEqual(row["review"]["in_notes"]["C-1"], True)
        self.assertEqual(row["review"]["matched_findings"], 1)
        self.assertEqual(row["debate"]["found"], {"A-1": True, "B-1": True, "C-1": False, "D-1": True})
        self.assertEqual(row["debate"]["seconds"], 600.0)
        self.assertIs(row["debate"]["finished"], True)
        summary = report["summary"]
        self.assertEqual((summary["review"]["recall"], summary["debate"]["recall"]), (0.25, 0.75))
        self.assertAlmostEqual(summary["review"]["precision_lower_bound"], 1 / 3)
        self.assertEqual(report["verdict"]["decision"], "do not adopt yet")
        grading = json.loads((self.dest / "grading.json").read_text())
        self.assertEqual(sorted(g["system"] for g in grading), ["debate", "review", "review"])
        text = review_eval.render(report)
        self.assertIn("verdict:", text)
        self.assertIn("debate agents that fell back: 0, runs not done 0", text)
        self.assertEqual(manifest["cases"][0]["defects"], ["A-1", "B-1", "C-1", "D-1"])

    def test_debate_seconds_reports_whether_debate_finished_was_seen(self) -> None:
        full = self.root / "events-full.jsonl"
        full.write_text("\n".join(json.dumps(e) for e in [
            {"type": "debate_started", "ts": "2026-11-24T10:00:00Z"},
            {"type": "agent_finished", "data": {"fallback": True}},
            {"type": "debate_finished", "ts": "2026-11-24T10:10:00Z"}]) + "\n")
        self.assertEqual(review_eval.debate_seconds(full), (600.0, 1, True))
        cut = self.root / "events-cut.jsonl"  # the engine returned without a decision: no debate_finished
        cut.write_text(json.dumps({"type": "debate_started", "ts": "2026-11-24T10:00:00Z"}) + "\n")
        self.assertEqual(review_eval.debate_seconds(cut), (None, 0, False))
        self.assertEqual(review_eval.debate_seconds(self.root / "missing.jsonl"), (None, 0, False))

    def test_score_marks_a_debate_without_a_decision_as_not_finished(self) -> None:
        self.prepare(per_case=4)
        run = self.dest / "runs" / "case-1"
        run.mkdir(parents=True)
        (run / "debate-result.json").write_text(json.dumps({"final_decision": {}}))
        (run / "debate-events.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "debate_started", "ts": "2026-11-24T10:00:00Z"},
            {"type": "debate_finished", "ts": "2026-11-24T10:10:00Z"}]))
        report = review_eval.score(self.dest)
        self.assertIs(report["rows"][0]["debate"]["finished"], False)
        self.assertEqual(report["summary"]["debate"]["not_done"], 1)
        self.assertIn("runs not done 1", review_eval.render(report))

    def test_the_real_ground_truth_loads_and_bundles_into_four_mixed_cases(self) -> None:
        items = review_eval.load_truth()
        cases = review_eval.bundle(items, 4)
        self.assertEqual(len(items), 16)
        self.assertEqual([len({d["type"] for d in c}) for c in cases], [4, 4, 4, 4])


if __name__ == "__main__":
    unittest.main()

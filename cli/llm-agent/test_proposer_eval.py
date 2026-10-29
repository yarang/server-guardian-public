"""The multi-perspective evaluation harness builds sound tasks and configurations and scores runs (multi-perspective.md, P2)."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import proposer_eval as pe
from team_manager import parse_lineup, validate_lineup
from persona_manager import PersonaManager


def _run(root: Path, config: str, task: str, rep: str, text: str, seconds: int = 100, fallback: bool = False) -> None:
    folder = root / config / task / rep
    folder.mkdir(parents=True)
    result = {"final_decision": {"decision": "x"}, "transcript": [
        {"role": "Domain Expert", "content": "draft " + text},
        {"role": "Critic Agent", "content": text}]}
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
    events = [{"type": "debate_started", "ts": "2026-10-18T10:00:00Z", "data": {}},
              {"type": "agent_finished", "data": {"fallback": fallback}},
              {"type": "debate_finished", "ts": f"2026-10-18T10:{seconds // 60:02d}:{seconds % 60:02d}Z", "data": {}}]
    (folder / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


class TaskTests(unittest.TestCase):
    def test_every_task_has_three_to_five_defects_that_apply_to_distinct_spots(self) -> None:
        tasks = pe.build_tasks()
        self.assertEqual(len(tasks), 5)
        for task in tasks:
            with self.subTest(task=task["id"]):
                self.assertIn(len(task["defects"]), (3, 4, 5))
                changed = [n for n, text in task["files"].items() if text != pe.corpus.HEALTHY[n]]
                self.assertTrue(changed)
                for d in task["defects"]:
                    self.assertIn(d["ident"], pe.KEYS)

    def test_no_defect_is_planted_twice_and_all_classes_without_a_detector_appear(self) -> None:
        idents = [i for t in pe.TASKS for i in t.defects]
        self.assertEqual(len(idents), len(set(idents)))
        classes = {pe._defect(i).cls for i in idents}
        self.assertTrue({"low-contrast", "false-claim"} <= classes)

    def test_keys_compile_and_cover_exactly_the_planted_defects(self) -> None:
        import re
        for pattern in pe.KEYS.values():
            re.compile(pattern)
        self.assertEqual(set(pe.KEYS), {i for t in pe.TASKS for i in t.defects})
        # the pre-grade of a report that names nothing is empty
        task = pe.build_task(pe.TASKS[0])
        self.assertFalse(any(pe.pregrade("nothing wrong", task["defects"]).values()))

    def test_hard_tasks_plant_twelve_defects_of_every_class_that_apply_cleanly(self) -> None:
        import re
        tasks = pe.build_tasks(True)
        self.assertEqual([t["id"] for t in tasks], ["h1", "h2", "h3"])
        for task in tasks:
            with self.subTest(task=task["id"]):
                self.assertEqual(len(task["defects"]), 12)
                classes = {d["class"] for d in task["defects"]}
                self.assertEqual(classes, {"broken-svg", "low-contrast", "dead-link", "missing-section", "false-claim"})
                for d in task["defects"]:
                    re.compile(d["key"])
                self.assertFalse(any(pe.pregrade("nothing wrong", task["defects"]).values()))
                # one low-contrast defect per file: a loose key could not tell two apart
                contrast_files = [d["file"] for d in task["defects"] if d["class"] == "low-contrast"]
                self.assertEqual(len(contrast_files), len(set(contrast_files)))

    def test_hard_keys_for_colours_need_the_hex_value(self) -> None:
        task = pe.build_task(pe.HARD_TASKS[0])
        found = pe.pregrade("logo.svg has low contrast, hard to read", task["defects"])
        self.assertFalse(found["contrast-logo-text"])
        found = pe.pregrade("logo.svg: text #2a4a7b on #1a3a6b", task["defects"])
        self.assertTrue(found["contrast-logo-text"])

    def test_pregrade_needs_the_file_and_a_key_word(self) -> None:
        task = pe.build_task(pe.TASKS[0])
        found = pe.pregrade("overview.md links to flows.mmd which does not exist; logo.svg is not well-formed", task["defects"])
        self.assertTrue(found["link-overview-flow"])
        self.assertTrue(found["svg-unclosed"])
        self.assertFalse(found["claim-cap"])  # overview.md is named but no 64 MiB
        self.assertFalse(pe.pregrade("flows.mmd is missing", task["defects"])["link-overview-flow"])  # no file name


class ConfigTests(unittest.TestCase):
    def test_all_configs_are_valid_teams_and_differ_only_by_the_proposer(self) -> None:
        manager = PersonaManager()
        configs = pe.build_configs()
        self.assertEqual(list(configs), pe.CONFIG_IDS)
        with TemporaryDirectory() as tmp:
            import os
            old = os.environ.get("GUARDIAN_PERSONAS_DIR")
            (Path(tmp) / f"{pe.CLONE_PERSONA}.json").write_text(json.dumps(pe.clone_persona()), encoding="utf-8")
            os.environ["GUARDIAN_PERSONAS_DIR"] = tmp
            try:
                for cid, cfg in configs.items():
                    with self.subTest(config=cid):
                        errors = [w for w in validate_lineup(parse_lineup(cfg["team"]), PersonaManager())
                                  if "at most" in w or "different persona" in w]
                        self.assertEqual(errors, [])
            finally:
                if old is None:
                    os.environ.pop("GUARDIAN_PERSONAS_DIR")
                else:
                    os.environ["GUARDIAN_PERSONAS_DIR"] = old
        self.assertIsNotNone(manager)
        base = {k: v for k, v in configs["C0"]["team"].items() if k not in ("id", "name")}
        for cid in pe.CONFIG_IDS[1:]:
            other = {k: v for k, v in configs[cid]["team"].items() if k not in ("id", "name", "proposers")}
            self.assertEqual(other, base)
            self.assertEqual(len(configs[cid]["team"]["proposers"]), 1)
        self.assertNotIn("proposers", configs["C0"]["team"])

    def test_provider_and_persona_of_the_proposer_follow_the_design_table(self) -> None:
        c = pe.build_configs(lead="claude", other="codex")
        lead = c["C0"]["team"]["expert"]
        p1, p2, p3 = (c[k]["team"]["proposers"][0] for k in ("C1", "C2", "C3"))
        self.assertEqual(p1["providers"], lead["providers"])  # compute control: same provider
        self.assertNotEqual(p1["persona"], lead["persona"])  # only the id differs; the prompt is a copy
        self.assertEqual(pe.clone_persona()["system_prompt"],
                         json.loads((pe.builtin_personas_dir() / "domain_architect.json").read_text())["system_prompt"])
        self.assertEqual(p2["providers"], lead["providers"])
        self.assertNotEqual(p2["persona"], lead["persona"])
        self.assertNotEqual(p3["providers"], lead["providers"])
        self.assertEqual(c["C4"]["env"], {"GUARDIAN_DRAFT_NAMES": "1"})
        self.assertEqual(c["C4"]["team"]["proposers"], c["C3"]["team"]["proposers"])

    def test_prepare_writes_the_scratch_inputs_below_the_destination_only(self) -> None:
        with TemporaryDirectory() as tmp:
            out = pe.prepare(Path(tmp) / "eval")
            self.assertEqual(out["configs"], pe.CONFIG_IDS)
            root = Path(tmp) / "eval"
            self.assertTrue((root / "tasks" / "t1" / "TASK.txt").exists())
            self.assertTrue((root / "teams" / "eval-c3.json").exists())
            self.assertTrue((root / "personas" / f"{pe.CLONE_PERSONA}.json").exists())
            self.assertEqual(json.loads((root / "env.json").read_text())["C4"], {"GUARDIAN_DRAFT_NAMES": "1"})
            self.assertEqual({p.name for p in Path(tmp).iterdir()}, {"eval"})


class ScoreTests(unittest.TestCase):
    def _rows(self, tmp: str, spec):
        root = Path(tmp)
        for config, reps in spec.items():
            for rep, (text, seconds, fallback) in enumerate(reps, start=1):
                _run(root, config, "t1", f"r{rep}", text, seconds, fallback)
        return pe.collect(root)

    ALL = ("logo.svg not well-formed, contrast; overview.md flows.mmd and 64 MiB")
    HALF = "overview.md flows.mmd and 64 MiB"
    NONE = "all good"

    def test_discarded_attempts_and_fallback_runs_do_not_count(self) -> None:
        with TemporaryDirectory() as tmp:
            _run(Path(tmp), "C0", "t1", "r1.fallback-1", self.ALL)
            rows = self._rows(tmp, {"C0": [(self.HALF, 100, False), (self.ALL, 100, True)]})
            self.assertEqual(len(rows), 2)
            summary = pe.summarize(rows)
            self.assertEqual(summary["C0"]["runs"], 1)
            self.assertEqual(summary["C0"]["discarded"], 1)
            self.assertAlmostEqual(summary["C0"]["recall"]["mean"], 0.5)

    def test_decision_rule_needs_recall_gain_over_both_and_bounded_wall_time(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = {
                "C0": [(self.HALF, 100, False), (self.NONE, 100, False), (self.HALF, 100, False)],
                "C1": [(self.HALF, 150, False), (self.NONE, 150, False), (self.HALF, 150, False)],
                "C2": [(self.ALL, 140, False)] * 3,   # better and within +50 %
                "C3": [(self.ALL, 400, False)] * 3,   # better but 4x slower
            }
            summary = pe.summarize(self._rows(tmp, spec))
            verdict = pe.decide(summary)
            self.assertTrue(verdict["configs"]["C2"]["adopt"])
            self.assertFalse(verdict["configs"]["C3"]["adopt"])
            self.assertFalse(verdict["configs"]["C3"]["time_ok"])
            self.assertEqual(verdict["verdict"], "default")
            self.assertEqual(verdict["adopt"], ["C2"])

    def test_a_gain_inside_one_standard_deviation_stays_opt_in(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = {"C0": [(self.HALF, 100, False), (self.NONE, 100, False)],
                    "C1": [(self.HALF, 100, False), (self.NONE, 100, False)],
                    "C2": [(self.HALF, 100, False), (self.HALF, 100, False)],
                    "C3": [(self.HALF, 100, False), (self.NONE, 100, False)]}
            verdict = pe.decide(pe.summarize(self._rows(tmp, spec)))
            self.assertEqual(verdict["verdict"], "opt-in")

    def test_missing_control_is_reported_as_incomplete(self) -> None:
        with TemporaryDirectory() as tmp:
            verdict = pe.decide(pe.summarize(self._rows(tmp, {"C0": [(self.HALF, 100, False)]})))
            self.assertEqual(verdict["verdict"], "incomplete")

    def test_blind_sheet_hides_the_config_and_the_key_restores_it(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = self._rows(tmp, {"C0": [(self.HALF, 100, False)] * 2, "C3": [(self.ALL, 100, False)] * 2,
                                    "C2": [(self.ALL, 100, True)]})
            sheet, key = pe.blind_sheet(rows, seed=3)
            self.assertEqual(len(sheet), 4)  # the fallback run is left out
            self.assertEqual(sorted(e["label"] for e in sheet), ["S01", "S02", "S03", "S04"])
            self.assertNotIn("C3", json.dumps(sheet))
            self.assertEqual({k["config"] for k in key.values()}, {"C0", "C3"})
            self.assertEqual(pe.blind_sheet(rows, seed=3)[1], key)  # reproducible

    def test_cli_prepare_and_score(self) -> None:
        import io
        from contextlib import redirect_stdout
        with TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(pe.main(["--prepare", str(Path(tmp) / "p")]), 0)
            self.assertIn("t1", buffer.getvalue())
            _run(Path(tmp) / "runs", "C0", "t1", "r1", self.HALF)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(pe.main(["--score", str(Path(tmp) / "runs"), "--sheet", str(Path(tmp) / "s")]), 0)
            self.assertIn("incomplete", buffer.getvalue())
            self.assertTrue((Path(tmp) / "s" / "grading-key.json").exists())


if __name__ == "__main__":
    unittest.main()

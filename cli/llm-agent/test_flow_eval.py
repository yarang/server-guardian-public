"""The flow evaluation harness builds sound teams and scores runs (flow-templates.md, T4a). No model is called."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import flow_eval as fe
import proposer_eval as pe
from persona_manager import PersonaManager
from team_manager import parse_lineup, validate_lineup


def _run(root: Path, config: str, task: str, rep: str, text: str, seconds: int = 100, fallback: bool = False,
         calls: int = 3, with_critic: bool = True) -> None:
    folder = root / config / task / rep
    folder.mkdir(parents=True)
    transcript = [{"role": "Domain Expert", "round": 1, "content": "draft " + text}]
    if with_critic:
        transcript.append({"role": "Critic Agent", "round": 1, "content": text})
    transcript.append({"role": "JEV-MCP Judge", "round": 1, "content": "ok"})
    (folder / "result.json").write_text(json.dumps({"final_decision": {"decision": "x"}, "transcript": transcript}),
                                        encoding="utf-8")
    events = [{"type": "debate_started", "ts": "2026-10-30T10:00:00Z", "data": {}}]
    events += [{"type": "agent_started", "data": {}} for _ in range(calls)]
    events += [{"type": "agent_finished", "data": {"fallback": fallback}},
               {"type": "debate_finished", "ts": f"2026-10-30T10:{seconds // 60:02d}:{seconds % 60:02d}Z", "data": {}}]
    (folder / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


class ConfigTests(unittest.TestCase):
    def test_every_config_is_a_valid_team_of_its_flow_kind(self) -> None:
        configs = fe.build_configs()
        self.assertEqual(list(configs), fe.CONFIG_IDS)
        kinds = {cid: cfg["team"].get("flow", {}).get("kind", "debate") for cid, cfg in configs.items()}
        self.assertEqual(kinds, {"F0": "debate", "F1": "verify", "F2": "panel", "F3": "pipeline"})
        for cid, cfg in configs.items():
            with self.subTest(config=cid):
                errors = [w for w in validate_lineup(parse_lineup(cfg["team"]), PersonaManager())
                          if "at most" in w or "different persona" in w]
                self.assertEqual(errors, [])

    def test_lead_and_judge_are_the_same_in_every_config(self) -> None:
        configs = fe.build_configs(lead="claude", other="codex")
        for cid, cfg in configs.items():
            self.assertEqual(cfg["team"]["expert"], configs["F0"]["team"]["expert"], cid)
            self.assertEqual(cfg["team"]["judge"], configs["F0"]["team"]["judge"], cid)
            self.assertEqual(cfg["team"]["max_rounds"], 1 if cid == "F2" else 2, cid)  # a panel has one round
        for cid in ("F0", "F1", "F3"):  # the flows that keep a Critic use the same one
            self.assertEqual(configs[cid]["team"]["critic"], configs["F0"]["team"]["critic"], cid)
        self.assertNotIn("critic", configs["F2"]["team"])

    def test_the_second_voice_runs_on_the_other_provider(self) -> None:
        configs = fe.build_configs(lead="claude", other="codex")
        self.assertEqual(configs["F2"]["team"]["proposers"][0]["providers"], ["codex"])
        stages = configs["F3"]["team"]["flow"]["stages"]
        self.assertEqual([s["providers"] for s in stages], [["claude"], ["codex"]])

    def test_prepare_writes_the_scratch_inputs_below_the_destination_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "eval"
            out = fe.prepare(root, hard=True)
            self.assertEqual(out["configs"], fe.CONFIG_IDS)
            self.assertEqual(out["tasks"], ["h1", "h2", "h3"])
            self.assertTrue((root / "tasks" / "h1" / "TASK.txt").exists())
            self.assertTrue((root / "teams" / "eval-f3.json").exists())
            self.assertEqual(set(json.loads((root / "env.json").read_text())), set(fe.CONFIG_IDS))
            self.assertEqual(len(json.loads((root / "key.json").read_text())["h1"]), 12)
            self.assertEqual({p.name for p in Path(tmp).iterdir()}, {"eval"})


class ScoreTests(unittest.TestCase):
    ALL = "logo.svg not well-formed, contrast; overview.md flows.mmd and 64 MiB"
    HALF = "overview.md flows.mmd and 64 MiB"
    NONE = "all good"

    def _rows(self, tmp: str, spec):
        for config, reps in spec.items():
            for rep, args in enumerate(reps, start=1):
                _run(Path(tmp), config, "t1", f"r{rep}", *args)
        return fe.collect(Path(tmp))

    def test_review_text_counts_every_proposal_of_the_last_round(self) -> None:
        result = {"final_decision": {"decision": "d"}, "transcript": [
            {"role": "Domain Expert", "round": 1, "content": "first round draft"},
            {"role": "Domain Expert", "round": 2, "content": "lead draft"},
            {"role": "Domain Expert", "round": 2, "content": "second draft"},
            {"role": "JEV-MCP Judge", "round": 2, "content": "judge words"}]}
        text = fe.review_text(result)
        self.assertIn("lead draft", text)
        self.assertIn("second draft", text)
        self.assertNotIn("first round draft", text)
        self.assertNotIn("judge words", text)

    def test_a_panel_without_critics_is_scored_on_its_drafts(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = self._rows(tmp, {"F2": [(self.ALL, 100, False, 2, False)]})
            self.assertGreater(rows[0]["recall"], 0.0)  # the critic is absent: only the draft carries the findings

    def test_calls_are_counted_from_agent_started_events(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = self._rows(tmp, {"F0": [(self.HALF, 100, False, 4, True)], "F1": [(self.HALF, 100, False, 2, True)]})
            self.assertEqual({r["config"]: r["calls"] for r in rows}, {"F0": 4, "F1": 2})
            self.assertEqual(fe.agent_calls(Path(tmp) / "missing.jsonl"), 0)

    def test_discarded_attempts_and_fallback_runs_do_not_count(self) -> None:
        with TemporaryDirectory() as tmp:
            _run(Path(tmp), "F0", "t1", "r1.fallback-1", self.ALL)
            rows = self._rows(tmp, {"F0": [(self.HALF, 100, False, 3, True), (self.ALL, 100, True, 3, True)]})
            self.assertEqual(len(rows), 2)
            summary = fe.summarize(rows)
            self.assertEqual(summary["F0"]["runs"], 1)
            self.assertEqual(summary["F0"]["discarded"], 1)

    def test_decision_marks_better_and_cheaper_against_f0(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = {
                "F0": [(self.HALF, 100, False, 4, True), (self.NONE, 100, False, 4, True), (self.HALF, 100, False, 4, True)],
                "F1": [(self.ALL, 130, False, 3, True)] * 3,    # better, within +50 % wall time
                "F2": [(self.HALF, 60, False, 3, False)] * 3,   # as good as F0's mean, faster
                "F3": [(self.ALL, 400, False, 6, True)] * 3,    # better but 4x slower
            }
            verdict = fe.decide(fe.summarize(self._rows(tmp, spec)))
            self.assertEqual(verdict["verdict"], "measured")
            configs = verdict["configs"]
            self.assertTrue(configs["F1"]["better"])
            self.assertTrue(configs["F2"]["cheaper"])
            self.assertFalse(configs["F3"]["better"])
            self.assertFalse(configs["F3"]["cheaper"])

    def test_a_gain_inside_one_standard_deviation_is_not_better(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = {"F0": [(self.HALF, 100, False, 3, True), (self.NONE, 100, False, 3, True)],
                    "F1": [(self.HALF, 100, False, 3, True), (self.HALF, 100, False, 3, True)]}
            verdict = fe.decide(fe.summarize(self._rows(tmp, spec)))
            self.assertFalse(verdict["configs"]["F1"]["better"])

    def test_missing_baseline_is_reported_as_incomplete(self) -> None:
        with TemporaryDirectory() as tmp:
            verdict = fe.decide(fe.summarize(self._rows(tmp, {"F1": [(self.HALF, 100, False, 3, True)]})))
            self.assertEqual(verdict["verdict"], "incomplete")
            self.assertIn("incomplete", fe.render(fe.summarize([]), verdict))

    def test_blind_sheet_hides_the_config(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = self._rows(tmp, {"F0": [(self.HALF, 100, False, 3, True)] * 2, "F2": [(self.ALL, 100, False, 3, False)] * 2})
            sheet, key = pe.blind_sheet(rows, seed=3)
            self.assertEqual(len(sheet), 4)
            self.assertNotIn("F2", json.dumps(sheet))
            self.assertEqual({k["config"] for k in key.values()}, {"F0", "F2"})


if __name__ == "__main__":
    unittest.main()

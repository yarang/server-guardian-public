"""The flow graph evaluation harness (docs/design/flow-graph.md, G5): its flows and its scoring, without a model."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import flow_graph
import flow_graph_eval as fge
import proposer_eval as pe
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class FlowGraphEvalTests(unittest.TestCase):
    def test_the_three_flows_differ_only_in_what_the_close_reads(self) -> None:
        flows = fge.build_flows()
        compiled = {cid: flow_graph.compile_flow(f) for cid, f in flows.items()}
        self.assertEqual(compiled["G0"].condense, {})
        self.assertEqual(compiled["G1"].condense["close_input"], "condensed")
        self.assertEqual(compiled["G2"].condense, {"close_input": "structured", "envelope": True})
        for cid in ("G1", "G2"):   # same lead, critic and judge as the baseline
            for role in ("expert", "critic", "judge"):
                self.assertEqual(compiled[cid].team[role], compiled["G0"].team[role])

    def test_scoring_grades_the_decision_apart_from_the_critiques(self) -> None:
        task = pe.build_tasks(True)[0]
        defect = task["defects"][0]
        with TemporaryDirectory() as tmp:
            runs = Path(tmp)
            for config, decision in (("G0", f"{defect['file']} {defect['key']}"), ("G1", "nothing")):
                run = runs / config / task["id"] / "r1"
                run.mkdir(parents=True)
                result = {"final_decision": {"decision": decision}, "consensus_reached": True,
                          "transcript": [{"role": "Critic Agent", "content": f"{defect['file']} {defect['key']}"}]}
                (run / "result.json").write_text(json.dumps(result))
                (run / "events.jsonl").write_text(
                    json.dumps({"type": "debate_started", "ts": "2026-11-01T00:00:00Z"}) + "\n"
                    + json.dumps({"type": "summary_rejected"}) + "\n"
                    + json.dumps({"type": "debate_finished", "ts": "2026-11-01T00:01:00Z"}) + "\n")
            rows = {r["config"]: r for r in fge.collect(runs)}
        self.assertEqual(rows["G0"]["review"], rows["G1"]["review"])     # the critique names it in both
        self.assertGreater(rows["G0"]["decision"], rows["G1"]["decision"])
        self.assertEqual(rows["G1"]["rejected_summaries"], 1)
        verdict = fge.decide(fge.summarize(list(rows.values())))
        self.assertFalse(verdict["configs"]["G1"]["keeps"])


if __name__ == "__main__":
    unittest.main()

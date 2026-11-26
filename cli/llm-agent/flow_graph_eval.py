"""Evaluation harness for flow graphs (docs/design/flow-graph.md, G5). It never calls a model.

Runs the hard planted-defect review tasks of `proposer_eval.py` through three flows that differ only in what the
close (the Judge) reads; the lead, the Critic and the Judge are those of the flow evaluation's baseline F0
(`flow_eval.build_configs`):

    G0 baseline     F0 converted into a flow (team_to_flow): the Judge reads the proposal and the critique raw
    G1 summariser   G0 with a summariser between the Critic and the close, which reads condensed
    G2 structured   G0 with the close reading structured input (the envelope's key sections), no extra call

    python3 flow_graph_eval.py --prepare DIR       (flows/, env.json; tasks and key come from flow_eval --prepare)
    python3 flow_graph_eval.py --score RUNS_DIR

Two recalls are graded (the automatic pre-grade, an upper bound): `review` over the final decision and the critiques
(the measure of the earlier evaluations) and `decision` over the final decision alone, which is what a summariser can
lose. RUNS_DIR holds `<config>/<task>/r<rep>/{result.json,events.jsonl}`; `r1.fallback-2` is a discarded attempt.
"""
import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import flow_eval
import flow_graph
import proposer_eval as pe

CONFIG_IDS = ["G0", "G1", "G2"]
RECALL_MARGIN_SD = 1.0   # a config "keeps" decision recall when its mean is not below G0's minus one SD of G0


def build_flows(summariser_provider: str = "claude") -> Dict[str, Dict[str, Any]]:
    """G0-G2 as flow files, built from the flow evaluation's F0 team."""
    base = flow_graph.team_to_flow(flow_eval.build_configs()["F0"]["team"])
    base.pop("converted_from", None)
    g0 = {**copy.deepcopy(base), "id": "eval-g0", "name": "G0 baseline"}
    g1 = {**copy.deepcopy(base), "id": "eval-g1", "name": "G1 summariser"}
    g1["seats"].append({"id": "sum", "job": "summarise", "providers": [summariser_provider]})
    g1["links"] = [l for l in g1["links"] if not (l["from"] == "critic-1" and l["to"] == "close")]
    g1["links"] += [{"from": "critic-1", "to": "sum"}, {"from": "sum", "to": "close"}]
    g1["close"]["input"] = "condensed"
    g2 = {**copy.deepcopy(base), "id": "eval-g2", "name": "G2 structured"}
    g2["close"]["input"] = "structured"
    flows = {"G0": g0, "G1": g1, "G2": g2}
    for flow in flows.values():
        flow_graph.validate_flow(flow)
        flow_graph.compile_flow(flow)
    return flows


def prepare(dest: Path) -> Dict[str, Any]:
    (dest / "flows").mkdir(parents=True, exist_ok=True)
    flows = build_flows()
    for flow in flows.values():
        (dest / "flows" / f"{flow['id']}.json").write_text(json.dumps(flow, indent=2) + "\n", encoding="utf-8")
    return {"flows": {cid: f["id"] for cid, f in flows.items()}}


def decision_text(result: Dict[str, Any]) -> str:
    return json.dumps(result.get("final_decision") or "", ensure_ascii=False)


def summary_rejections(events_file: Path) -> int:
    if not events_file.exists():
        return 0
    count = 0
    for line in events_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            count += json.loads(line).get("type") == "summary_rejected"
        except ValueError:
            continue
    return count


def collect(runs: Path) -> List[Dict[str, Any]]:
    by_id = {t["id"]: t for t in pe.build_tasks(True)}
    rows: List[Dict[str, Any]] = []
    for result_file in sorted(runs.glob("*/*/r*/result.json")):
        run_dir = result_file.parent
        if not run_dir.name[1:].isdigit():
            continue
        config, tid = run_dir.parent.parent.name, run_dir.parent.name
        if tid not in by_id:
            continue
        result = json.loads(result_file.read_text(encoding="utf-8"))
        defects = by_id[tid]["defects"]
        review = pe.pregrade(pe.review_text(result), defects)
        decision = pe.pregrade(decision_text(result), defects)
        rows.append({"config": config, "task": tid, "rep": run_dir.name,
                     "review": sum(review.values()) / len(review), "decision": sum(decision.values()) / len(decision),
                     "calls": flow_eval.agent_calls(run_dir / "events.jsonl"),
                     "rounds": sum(1 for t in result.get("transcript") or [] if t.get("role") == "JEV-MCP Judge"),
                     "rejected_summaries": summary_rejections(run_dir / "events.jsonl"),
                     "approved": bool(result.get("consensus_reached")),
                     **pe.run_facts(run_dir / "events.jsonl")})
    return rows


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for config in sorted({r["config"] for r in rows}):
        valid = [r for r in rows if r["config"] == config and not r["fallback_agents"]]
        row: Dict[str, Any] = {"runs": len(valid), "discarded": sum(1 for r in rows if r["config"] == config) - len(valid)}
        if valid:
            for key in ("review", "decision", "calls", "rounds", "rejected_summaries"):
                row[key] = pe._stats([r[key] for r in valid])
            seconds = [r["seconds"] for r in valid if r["seconds"] is not None]
            if seconds:
                row["seconds"] = pe._stats(seconds)
            row["approved"] = sum(r["approved"] for r in valid)
        out[config] = row
    return out


def decide(summary: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """`keeps`: decision recall not below G0's minus one SD of G0. Only a config that keeps recall may be offered as an
    example flow; neither changes a default (D4: the close reads raw unless an operator chooses otherwise)."""
    base = summary.get("G0", {})
    if "decision" not in base:
        return {"verdict": "incomplete", "configs": {}}
    margin = RECALL_MARGIN_SD * base["decision"]["sd"]
    configs: Dict[str, Any] = {}
    for cid, row in summary.items():
        if cid == "G0" or "decision" not in row:
            continue
        gain = row["decision"]["mean"] - base["decision"]["mean"]
        growth = (row["seconds"]["mean"] / base["seconds"]["mean"] - 1) if "seconds" in row and "seconds" in base else None
        configs[cid] = {"keeps": gain >= -margin, "decision_gain": gain, "margin": margin, "wall_growth": growth}
    return {"verdict": "measured", "configs": configs}


def render(summary: Dict[str, Dict[str, Any]], verdict: Dict[str, Any]) -> str:
    def stat(row: Dict[str, Any], key: str, fmt: str = "{:.2f}") -> str:
        return f"{fmt.format(row[key]['mean'])}±{fmt.format(row[key]['sd'])}" if key in row else "-"
    lines = ["config  runs  disc  review     decision   seconds   calls  rounds  rejected  approved"]
    for cid, row in summary.items():
        lines.append(f"{cid:<7} {row['runs']:>4}  {row['discarded']:>4}  {stat(row, 'review'):<9}  "
                     f"{stat(row, 'decision'):<9}  {stat(row, 'seconds', '{:.0f}'):<8}  "
                     f"{row['calls']['mean'] if 'calls' in row else '-':<5}  "
                     f"{row['rounds']['mean'] if 'rounds' in row else '-':<6}  "
                     f"{row['rejected_summaries']['mean'] if 'rejected_summaries' in row else '-':<8}  "
                     f"{row.get('approved', '-')}")
    for cid, v in verdict.get("configs", {}).items():
        lines.append(f"{cid}: {'keeps decision recall' if v['keeps'] else 'loses decision recall'} "
                     f"(gain {v['decision_gain']:+.2f}, margin {v['margin']:.2f})")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--prepare", metavar="DIR", help="write the flows below DIR")
    parser.add_argument("--score", metavar="RUNS_DIR", help="pre-grade finished runs and compare G1, G2 with G0")
    parser.add_argument("--json", action="store_true", help="with --score: print the summary as JSON")
    args = parser.parse_args(argv)
    if args.prepare:
        print(json.dumps(prepare(Path(args.prepare).expanduser())))
        return 0
    if args.score:
        summary = summarize(collect(Path(args.score).expanduser()))
        verdict = decide(summary)
        print(json.dumps({"summary": summary, "verdict": verdict}, indent=2) if args.json else render(summary, verdict))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

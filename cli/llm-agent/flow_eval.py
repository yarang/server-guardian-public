"""Evaluation harness for flow kinds (docs/design/flow-templates.md, T4a). It never calls a model.

Runs the planted-defect review tasks of `proposer_eval.py` (built from the seeded-defect corpus) through the flow kinds
that can find defects, with the same lead, critic and judge in every configuration, so a difference is the flow's:

    F0 debate    the lead drafts, one Critic objects (the baseline, C0 of the multi-draft evaluation)
    F1 verify    the lead drafts, one Critic lists failed checklist items only
    F2 panel     the lead and one more member draft blind to each other, the Judge reads both
    F3 pipeline  two stages (draft, rewrite) and the same Critic as F0

`versus` is not run here: two advocates of opposite stances answer a decision question, not "list every defect"; its
cost is structural (calls per round) and a decision task for it belongs to T4b, when custom step lists exist.

    python3 flow_eval.py --list
    python3 flow_eval.py --prepare DIR [--lead claude --other codex] [--hard]
    python3 flow_eval.py --score RUNS_DIR [--sheet SHEET_DIR] [--seed 7]

RUNS_DIR holds `<config>/<task>/r<rep>/{result.json,events.jsonl}` like evaluation 2; a directory such as
`r1.fallback-2` is a discarded attempt. Standard library only; nothing is read from or written to the data directory
(`--prepare` writes only below DIR). The pre-grade is an upper bound (file named and a key word present); the human
grade of the blind sheet is the authoritative one.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import proposer_eval as pe

CONFIG_IDS = ["F0", "F1", "F2", "F3"]
SECOND_PERSONA = pe.SECOND_PERSONA
RECALL_MARGIN_SD = 1.0  # a flow is "better" only if its mean recall beats F0 by more than one SD of F0
MAX_WALL_GROWTH = 0.5   # and the mean wall time may grow by at most this fraction (same rule as decision 10)


def build_configs(lead: str = "claude", other: str = "codex", critic: str = "codex", judge: str = "claude",
                  code_tools: Optional[str] = "mcp") -> Dict[str, Dict[str, Any]]:
    """F0-F3 as {team, env}. Lead, critic and judge are identical; the flow kind and its extra seats differ."""
    def member(persona: str, provider: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"persona": persona, "providers": [provider]}
        if code_tools:
            out["code_tools"] = code_tools
        return out

    def team(ident: str, name: str, flow: Optional[Dict[str, Any]], proposers: Sequence[Dict[str, Any]] = (),
             critics: bool = True) -> Dict[str, Any]:
        # A panel runs one round by rule (nothing is critiqued); the others may run two.
        out: Dict[str, Any] = {"id": ident, "name": name, "expert": member("domain_architect", lead),
                               "judge": member("jev_mcp_decision", judge), "max_rounds": 2 if critics else 1}
        if critics:
            out["critic"] = [member("critic_redteam", critic)]
        if proposers:
            out["proposers"] = list(proposers)
        if flow:
            out["flow"] = flow
        return out

    return {
        "F0": {"team": team("eval-f0", "F0 debate", None), "env": {}},
        "F1": {"team": team("eval-f1", "F1 verify", {"kind": "verify"}), "env": {}},
        "F2": {"team": team("eval-f2", "F2 panel", {"kind": "panel"}, [member(SECOND_PERSONA, other)], critics=False),
               "env": {}},
        "F3": {"team": team("eval-f3", "F3 pipeline", {"kind": "pipeline", "stages": [
            member("domain_architect", lead), member(SECOND_PERSONA, other)]}), "env": {}},
    }


def prepare(dest: Path, lead: str = "claude", other: str = "codex", hard: bool = False) -> Dict[str, Any]:
    """Same layout as proposer_eval.prepare: tasks/, teams/, personas/ (none needed), env.json and key.json."""
    tasks = pe.build_tasks(hard)
    for task in tasks:
        folder = dest / "tasks" / task["id"]
        for name, text in task["files"].items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        (folder / "TASK.txt").write_text(task["prompt"] + "\n", encoding="utf-8")
    configs = build_configs(lead, other)
    (dest / "teams").mkdir(parents=True, exist_ok=True)
    for cfg in configs.values():
        (dest / "teams" / f"{cfg['team']['id']}.json").write_text(json.dumps(cfg["team"], indent=2) + "\n",
                                                                  encoding="utf-8")
    (dest / "personas").mkdir(parents=True, exist_ok=True)  # builtin personas only; the directory keeps the layout
    (dest / "env.json").write_text(json.dumps({cid: cfg["env"] for cid, cfg in configs.items()}, indent=2) + "\n",
                                   encoding="utf-8")
    (dest / "key.json").write_text(json.dumps({t["id"]: t["defects"] for t in tasks}, indent=2) + "\n",
                                   encoding="utf-8")
    return {"tasks": [t["id"] for t in tasks], "configs": list(configs)}


# ---- scoring ---------------------------------------------------------------------------------------------------


def review_text(result: Dict[str, Any]) -> str:
    """What counts for recall, the same for every flow: the final decision, the critiques and every proposal of the last
    round (a panel has two blind drafts there, a pipeline the stage chain).

    `proposer_eval.review_text` leaves the proposals out, which suits a debate (the Critic and the Judge restate what
    matters) but would unfairly cut a panel without critics down to the Judge's summary."""
    parts = [pe.review_text(result)]
    proposals = [t for t in result.get("transcript") or [] if t.get("role") == "Domain Expert"]
    if proposals:
        last = max(int(t.get("round") or 0) for t in proposals)
        parts += [str(t.get("content") or "") for t in proposals if int(t.get("round") or 0) == last]
    return "\n\n".join(parts)


def agent_calls(events_file: Path) -> int:
    """Model sessions the run started (agent_started events), the structural cost of the flow."""
    if not events_file.exists():
        return 0
    calls = 0
    for line in events_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "agent_started":
            calls += 1
    return calls


def collect(runs: Path, tasks: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    by_id = {t["id"]: t for t in (tasks or pe.build_tasks() + pe.build_tasks(True))}
    rows: List[Dict[str, Any]] = []
    for result_file in sorted(runs.glob("*/*/r*/result.json")):
        run_dir = result_file.parent
        if not run_dir.name[1:].isdigit():
            continue
        config, tid = run_dir.parent.parent.name, run_dir.parent.name
        if tid not in by_id:
            continue
        result = json.loads(result_file.read_text(encoding="utf-8"))
        text = review_text(result)
        found = pe.pregrade(text, by_id[tid]["defects"])
        rows.append({"config": config, "task": tid, "rep": run_dir.name, "found": found,
                     "recall": sum(found.values()) / len(found), "text": text,
                     "calls": agent_calls(run_dir / "events.jsonl"),
                     "rounds": sum(1 for t in result.get("transcript") or [] if t.get("role") == "JEV-MCP Judge"),
                     **pe.run_facts(run_dir / "events.jsonl")})
    return rows


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per config: valid runs (no fallback) with mean/sd of recall, wall seconds and calls; discarded runs apart."""
    out: Dict[str, Dict[str, Any]] = {}
    for config in sorted({r["config"] for r in rows}):
        mine = [r for r in rows if r["config"] == config]
        valid = [r for r in mine if not r["fallback_agents"]]
        row: Dict[str, Any] = {"runs": len(valid), "discarded": len(mine) - len(valid)}
        if valid:
            row["recall"] = pe._stats([r["recall"] for r in valid])
            row["calls"] = pe._stats([r["calls"] for r in valid])
            row["rounds"] = pe._stats([r["rounds"] for r in valid])
            seconds = [r["seconds"] for r in valid if r["seconds"] is not None]
            if seconds:
                row["seconds"] = pe._stats(seconds)
        out[config] = row
    return out


def decide(summary: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """For each flow other than F0: `better` when its mean recall beats F0 by more than one SD of F0 and its wall time
    is at most 50 % above F0's; `cheaper` when recall is not lower than F0's minus that margin and the wall time is
    shorter. A flow is a candidate for a builtin example template only when it is `better` or `cheaper`."""
    base = summary.get("F0", {})
    if "recall" not in base:
        return {"verdict": "incomplete", "reason": "F0 needs valid runs", "configs": {}}
    margin = RECALL_MARGIN_SD * base["recall"]["sd"]
    verdicts: Dict[str, Any] = {}
    for cid in sorted(summary):
        if cid == "F0":
            continue
        row = summary[cid]
        if "recall" not in row:
            verdicts[cid] = {"better": False, "cheaper": False, "reason": "no valid runs"}
            continue
        gain = row["recall"]["mean"] - base["recall"]["mean"]
        growth = None
        if "seconds" in row and "seconds" in base and base["seconds"]["mean"]:
            growth = row["seconds"]["mean"] / base["seconds"]["mean"] - 1
        better = gain > margin and growth is not None and growth <= MAX_WALL_GROWTH
        cheaper = gain >= -margin and growth is not None and growth < 0
        verdicts[cid] = {"better": better, "cheaper": cheaper, "gain_vs_f0": gain, "margin": margin,
                         "wall_growth": growth}
    return {"verdict": "measured", "configs": verdicts}


def render(summary: Dict[str, Dict[str, Any]], verdict: Dict[str, Any]) -> str:
    lines = ["config  runs  discarded  recall(mean±sd)  seconds(mean±sd)  calls(mean)  rounds(mean)"]
    for cid, row in summary.items():
        recall = f"{row['recall']['mean']:.2f}±{row['recall']['sd']:.2f}" if "recall" in row else "-"
        secs = f"{row['seconds']['mean']:.0f}±{row['seconds']['sd']:.0f}" if "seconds" in row else "-"
        calls = f"{row['calls']['mean']:.1f}" if "calls" in row else "-"
        rounds = f"{row['rounds']['mean']:.1f}" if "rounds" in row else "-"
        lines.append(f"{cid:<7} {row['runs']:>4}  {row['discarded']:>9}  {recall:<15}  {secs:<16}  {calls:<11}  {rounds}")
    for cid, v in verdict.get("configs", {}).items():
        tags = [name for name in ("better", "cheaper") if v.get(name)] or ["no gain over F0"]
        lines.append(f"{cid}: {', '.join(tags)}")
    if verdict["verdict"] == "incomplete":
        lines.append(f"incomplete: {verdict['reason']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--list", action="store_true", help="print the tasks and configurations")
    parser.add_argument("--prepare", metavar="DIR", help="write tasks, teams and the key below DIR")
    parser.add_argument("--score", metavar="RUNS_DIR", help="pre-grade finished runs and compare the flows with F0")
    parser.add_argument("--sheet", metavar="DIR", help="with --score: write the blind grading sheet and its key")
    parser.add_argument("--hard", action="store_true", help="with --prepare: the hard tasks h1-h3 instead of t1-t5")
    parser.add_argument("--lead", default="claude")
    parser.add_argument("--other", default="codex")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    if args.list:
        for task in pe.build_tasks() + pe.build_tasks(True):
            print(task["id"], ", ".join(f"{d['ident']}({d['class']})" for d in task["defects"]))
        for cid, cfg in build_configs(args.lead, args.other).items():
            print(cid, cfg["team"]["name"], cfg["env"] or "")
        return 0
    if args.prepare:
        print(json.dumps(prepare(Path(args.prepare).expanduser(), args.lead, args.other, args.hard)))
        return 0
    if args.score:
        rows = collect(Path(args.score).expanduser())
        summary = summarize(rows)
        print(render(summary, decide(summary)))
        if args.sheet:
            sheet, key = pe.blind_sheet(rows, args.seed)
            out = Path(args.sheet).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            (out / "grading-sheet.json").write_text(json.dumps(sheet, indent=2, ensure_ascii=False), encoding="utf-8")
            (out / "grading-key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""Evaluation harness for multi-perspective debates (docs/design/multi-perspective.md, P2). It never calls a model.

Builds the planted-defect review tasks from the seeded-defect corpus (`artifact_corpus.py`), the five team
configurations C0-C4, and scores finished runs: an automatic pre-grade per planted defect, a blind grading sheet with
shuffled labels, and the decision rule of decision 10. The human grade on the sheet is the authoritative one; the
pre-grade only checks that the debate named the file and used a word that belongs to the defect.

    python3 proposer_eval.py --list
    python3 proposer_eval.py --prepare DIR [--lead claude --other codex]   # tasks, scratch repos, teams, personas
    python3 proposer_eval.py --score RUNS_DIR [--sheet SHEET_DIR] [--seed 7]

RUNS_DIR holds `<config>/<task>/r<rep>/{result.json,events.jsonl}` (the layout of evaluation 2; a directory such as
`r1.fallback-2` is a discarded attempt and is ignored). Standard library only; nothing is read from or written to the
data directory (`--prepare` writes only below DIR).
"""
import argparse
import json
import random
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import artifact_corpus as corpus
from persona_manager import builtin_personas_dir

CONFIG_IDS = ["C0", "C1", "C2", "C3", "C4"]
CLONE_PERSONA = "domain_architect_b"  # C1: the lead's own persona under a second id (personas must be unique per team)
SECOND_PERSONA = "devops_specialist"
TASK_PROMPT = ("Review the documents and diagrams in this repository (the files at its root). Several defects were "
               "introduced into them: broken diagrams, links to files that do not exist, missing required sections, "
               "unreadable colour pairs, and statements that contradict the code or the other documents. List every "
               "defect you find, each with the file and the exact text.")
THRESHOLD_SD = 1.0  # decision 10: beat C0 and C1 in mean recall by more than this many standard deviations of C0
MAX_WALL_GROWTH = 0.5  # decision 10: and the mean wall time may grow by at most this fraction


@dataclass(frozen=True)
class PlantedTask:
    ident: str
    defects: Tuple[str, ...]
    strict: bool = False  # hard tasks are pre-graded with HARD_KEYS


# 3-5 defects per task, from different classes; the corpus checks that every replacement still applies.
TASKS: List[PlantedTask] = [
    PlantedTask("t1", ("svg-unclosed", "contrast-logo-text", "link-overview-flow", "claim-cap")),
    PlantedTask("t2", ("svg-dangling-ref", "contrast-topology-text", "claim-retention", "section-context")),
    PlantedTask("t3", ("svg-duplicate-id", "contrast-legend-swatch", "image-api-legend", "claim-route-count")),
    PlantedTask("t4", ("claim-dry-run", "section-rollback", "link-decisions-overview", "svg-script")),
    PlantedTask("t5", ("svg-external-href", "claim-file", "section-headers", "contrast-logo-bg")),
]

# Hard tasks (2026-10-19): the first run of t1-t5 gave every configuration recall 1.00 (4 obvious defects in small
# documents), so there was nothing to tell the configurations apart. These plant 12 defects each, 2-3 per class, never
# two low-contrast defects of one file in one task (a loose key could not tell them apart), and overlap between tasks.
HARD_TASKS: List[PlantedTask] = [
    PlantedTask("h1", ("svg-unclosed", "svg-script", "svg-duplicate-id", "contrast-logo-text", "contrast-topology-node",
                       "contrast-legend-swatch", "link-overview-figure", "link-decisions-overview", "section-decision",
                       "section-headers", "claim-route-count", "claim-file"), True),
    PlantedTask("h2", ("svg-no-namespace", "svg-dangling-ref", "svg-external-href", "contrast-logo-bg",
                       "contrast-topology-text", "contrast-legend-text", "link-overview-flow", "link-runbook-decisions",
                       "image-api-legend", "section-context", "section-limits", "claim-dry-run"), True),
    PlantedTask("h3", ("svg-unclosed", "svg-dangling-ref", "svg-external-href", "contrast-logo-text",
                       "contrast-topology-text", "contrast-legend-swatch", "link-overview-flow",
                       "link-decisions-overview", "image-api-legend", "section-context", "section-headers",
                       "claim-retention"), True),
]

# What a correct report says: the pre-grade counts a defect when the text names the file and matches this pattern.
KEYS: Dict[str, str] = {
    "svg-unclosed": r"unclosed|not closed|not well-formed|well-formed|missing.{0,20}</svg>|closing",
    "contrast-logo-text": r"#2a4a7b|contrast|legib|readab",
    "link-overview-flow": r"flows\.mmd",
    "claim-cap": r"64\s*MiB",
    "svg-dangling-ref": r"#nodes|dangling|undefined|does not exist|no element",
    "contrast-topology-text": r"#0f4030|contrast|legib|readab",
    "claim-retention": r"90\s*days",
    "section-context": r"Context",
    "svg-duplicate-id": r"duplicate|used \d+ times|id=.ok.|(?<![A-Za-z0-9_])ok(?![A-Za-z0-9_])",
    "contrast-legend-swatch": r"swatch|contrast|invisible|white|#ffffff",
    "image-api-legend": r"legends\.svg",
    "claim-route-count": r"seven|\b7\b",
    "claim-dry-run": r"--force",
    "section-rollback": r"Rollback",
    "link-decisions-overview": r"summary\.md",
    "svg-script": r"script|alert",
    "svg-external-href": r"example\.invalid|external|https?://",
    "claim-file": r"artifact_server\.go",
    "section-headers": r"Headers",
    "contrast-logo-bg": r"#e8e8e8|contrast|legib|readab|background",
}

# Stricter keys for the hard tasks: a colour is found only when its hex value is named, a section only when the text says
# it is missing. They under-count (a reviewer may describe a colour without its hex), so the blind sheet is the grade.
HARD_KEYS: Dict[str, str] = dict(KEYS)
HARD_KEYS.update({
    "svg-no-namespace": r"namespace|xmlns",
    "svg-duplicate-id": r"duplicate|used \d+ times|id=.ok.",
    "contrast-logo-text": r"#2a4a7b",
    "contrast-logo-bg": r"#e8e8e8",
    "contrast-topology-text": r"#0f4030",
    "contrast-topology-node": r"#f5f5f5",
    "contrast-legend-text": r"#2b6f30",
    "contrast-legend-swatch": r"#1b5e20|#ffffff",
    "link-overview-figure": r"topologies\.svg",
    "link-runbook-decisions": r"decision-log\.md",
    "section-decision": r"(missing|absent|no|lacks?)[^.\n]{0,60}\bDecision\b|\bDecision\b[^.\n]{0,40}(section|missing|absent)",
    "section-context": r"(missing|absent|no|lacks?)[^.\n]{0,60}\bContext\b|\bContext\b[^.\n]{0,40}(section|missing|absent)",
    "section-limits": r"\bLimits\b",
    "section-headers": r"\bHeaders\b",
})


def _defect(ident: str) -> corpus.Defect:
    for d in corpus.DEFECTS:
        if d.ident == ident:
            return d
    raise KeyError(ident)


def build_task(task: PlantedTask) -> Dict[str, Any]:
    """The task's files (all healthy documents with its defects applied one after the other) and its ground truth."""
    files = dict(corpus.HEALTHY)
    truth: List[Dict[str, Any]] = []
    for ident in task.defects:
        d = _defect(ident)
        text = files[d.base]
        if text.count(d.old) != 1:
            raise ValueError(f"{task.ident}/{ident}: {d.old!r} must occur exactly once in {d.base}")
        files[d.base] = text.replace(d.old, d.new)
        truth.append({"ident": ident, "class": d.cls, "file": d.base,
                      "key": (HARD_KEYS if task.strict else KEYS)[ident]})
    return {"id": task.ident, "prompt": TASK_PROMPT, "files": files, "defects": truth}


def build_tasks(hard: bool = False) -> List[Dict[str, Any]]:
    return [build_task(t) for t in (HARD_TASKS if hard else TASKS)]


def build_configs(lead: str = "claude", other: str = "codex", critic: str = "codex", judge: str = "claude",
                  code_tools: Optional[str] = "mcp") -> Dict[str, Dict[str, Any]]:
    """C0-C4 as {team, env}. Everything but the proposer is the same, so a difference is the proposer's.

    C3 puts the second persona on a different provider than the lead; C4 is C3 with the authors named to the Critics
    (GUARDIAN_DRAFT_NAMES=1). C1 needs its own persona id because a team's personas must be unique.
    """
    def member(persona: str, provider: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"persona": persona, "providers": [provider]}
        if code_tools:
            out["code_tools"] = code_tools
        return out

    def team(ident: str, name: str, proposers: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": ident, "name": name, "expert": member("domain_architect", lead),
                               "critic": [member("critic_redteam", critic)], "judge": member("jev_mcp_decision", judge),
                               "max_rounds": 2}
        if proposers:
            out["proposers"] = proposers
        return out

    return {
        "C0": {"team": team("eval-c0", "C0 baseline", []), "env": {}},
        "C1": {"team": team("eval-c1", "C1 same persona and provider", [member(CLONE_PERSONA, lead)]), "env": {}},
        "C2": {"team": team("eval-c2", "C2 other persona, same provider", [member(SECOND_PERSONA, lead)]), "env": {}},
        "C3": {"team": team("eval-c3", "C3 other persona and provider", [member(SECOND_PERSONA, other)]), "env": {}},
        "C4": {"team": team("eval-c4", "C4 C3 with names shown", [member(SECOND_PERSONA, other)]),
               "env": {"GUARDIAN_DRAFT_NAMES": "1"}},
    }


def clone_persona() -> Dict[str, Any]:
    """The lead's persona under a second id, for C1 (written to the evaluation's own personas directory)."""
    data = json.loads((builtin_personas_dir() / "domain_architect.json").read_text(encoding="utf-8"))
    data["id"] = CLONE_PERSONA
    data["name"] = f"{data['name']} (second draft)"
    return data


def prepare(dest: Path, lead: str = "claude", other: str = "codex", hard: bool = False) -> Dict[str, Any]:
    """Writes tasks/<id>/{files..., TASK.txt}, teams/<id>.json, personas/<id>.json, env.json and key.json below dest."""
    tasks = build_tasks(hard)
    for task in tasks:
        folder = dest / "tasks" / task["id"]
        for name, text in task["files"].items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        (folder / "TASK.txt").write_text(task["prompt"] + "\n", encoding="utf-8")
    configs = build_configs(lead, other)
    (dest / "teams").mkdir(parents=True, exist_ok=True)
    for cfg in configs.values():
        (dest / "teams" / f"{cfg['team']['id']}.json").write_text(json.dumps(cfg["team"], indent=2) + "\n", encoding="utf-8")
    (dest / "personas").mkdir(parents=True, exist_ok=True)
    (dest / "personas" / f"{CLONE_PERSONA}.json").write_text(json.dumps(clone_persona(), indent=2) + "\n", encoding="utf-8")
    (dest / "env.json").write_text(json.dumps({cid: cfg["env"] for cid, cfg in configs.items()}, indent=2) + "\n",
                                   encoding="utf-8")
    key = {t["id"]: t["defects"] for t in tasks}
    (dest / "key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return {"tasks": [t["id"] for t in tasks], "configs": list(configs)}


# ---- scoring ---------------------------------------------------------------------------------------------------

def review_text(result: Dict[str, Any]) -> str:
    """What counts for recall: the final decision and the critiques (not the drafts)."""
    parts = [json.dumps(result.get("final_decision") or "", ensure_ascii=False)]
    parts += [str(t.get("content") or "") for t in result.get("transcript") or [] if t.get("role") == "Critic Agent"]
    return "\n\n".join(parts)


def pregrade(text: str, defects: Sequence[Dict[str, Any]]) -> Dict[str, bool]:
    """A defect counts as found when the text names its file and matches its key (an upper bound, not the grade)."""
    return {d["ident"]: d["file"].lower() in text.lower() and bool(re.search(d["key"], text, re.IGNORECASE))
            for d in defects}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_facts(events_file: Path) -> Dict[str, Any]:
    """Wall seconds and the number of agents that fell back to the built-in answer."""
    started = finished = None
    fallbacks = 0
    if events_file.exists():
        for line in events_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind, data = event.get("type"), event.get("data") or {}
            if kind == "debate_started":
                started = event.get("ts")
            elif kind == "debate_finished":
                finished = event.get("ts")
            elif kind == "agent_finished" and data.get("fallback"):
                fallbacks += 1
    seconds = round((_ts(finished) - _ts(started)).total_seconds()) if started and finished else None
    return {"seconds": seconds, "fallback_agents": fallbacks}


def collect(runs: Path, tasks: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    by_id = {t["id"]: t for t in (tasks or build_tasks() + build_tasks(True))}
    rows: List[Dict[str, Any]] = []
    for result_file in sorted(runs.glob("*/*/r*/result.json")):
        run_dir = result_file.parent
        if not run_dir.name[1:].isdigit():  # r1.fallback-2: a discarded attempt
            continue
        config, tid = run_dir.parent.parent.name, run_dir.parent.name
        if tid not in by_id:
            continue
        result = json.loads(result_file.read_text(encoding="utf-8"))
        found = pregrade(review_text(result), by_id[tid]["defects"])
        rows.append({"config": config, "task": tid, "rep": run_dir.name, "found": found,
                     "recall": sum(found.values()) / len(found), "text": review_text(result),
                     **run_facts(run_dir / "events.jsonl")})
    return rows


def _stats(values: Sequence[float]) -> Dict[str, float]:
    return {"mean": statistics.mean(values), "sd": statistics.stdev(values) if len(values) > 1 else 0.0}


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per config: valid runs (no fallback) with mean/sd of recall and wall time; discarded runs are counted apart."""
    out: Dict[str, Dict[str, Any]] = {}
    for config in sorted({r["config"] for r in rows}):
        mine = [r for r in rows if r["config"] == config]
        valid = [r for r in mine if not r["fallback_agents"]]
        row: Dict[str, Any] = {"runs": len(valid), "discarded": len(mine) - len(valid)}
        if valid:
            row["recall"] = _stats([r["recall"] for r in valid])
            seconds = [r["seconds"] for r in valid if r["seconds"] is not None]
            if seconds:
                row["seconds"] = _stats(seconds)
        out[config] = row
    return out


def decide(summary: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Decision 10: C2 or C3 becomes the default only when it beats C0 and C1 in mean recall by more than one SD of C0
    and its mean wall time is at most 50 % above C0's. C4 only answers whether blinding matters."""
    base, control = summary.get("C0", {}), summary.get("C1", {})
    verdicts: Dict[str, Any] = {}
    if "recall" not in base or "recall" not in control:
        return {"verdict": "incomplete", "reason": "C0 and C1 need valid runs", "configs": verdicts}
    margin = THRESHOLD_SD * base["recall"]["sd"]
    for cid in ("C2", "C3"):
        row = summary.get(cid, {})
        if "recall" not in row:
            verdicts[cid] = {"adopt": False, "reason": "no valid runs"}
            continue
        gain_c0 = row["recall"]["mean"] - base["recall"]["mean"]
        gain_c1 = row["recall"]["mean"] - control["recall"]["mean"]
        slower = None
        if "seconds" in row and "seconds" in base and base["seconds"]["mean"]:
            slower = row["seconds"]["mean"] / base["seconds"]["mean"] - 1
        recall_ok = gain_c0 > margin and gain_c1 > margin
        time_ok = slower is not None and slower <= MAX_WALL_GROWTH
        verdicts[cid] = {"adopt": recall_ok and time_ok, "gain_vs_c0": gain_c0, "gain_vs_c1": gain_c1,
                         "margin": margin, "wall_growth": slower, "recall_ok": recall_ok, "time_ok": time_ok}
    adopt = [cid for cid, v in verdicts.items() if v["adopt"]]
    return {"verdict": "default" if adopt else "opt-in", "adopt": adopt, "configs": verdicts}


def blind_sheet(rows: Sequence[Dict[str, Any]], seed: int = 7) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """Valid runs under shuffled labels S01.. without config names, and the key that maps a label back."""
    valid = [r for r in rows if not r["fallback_agents"]]
    order = list(range(len(valid)))
    random.Random(seed).shuffle(order)
    sheet: List[Dict[str, Any]] = []
    key: Dict[str, Dict[str, str]] = {}
    for n, index in enumerate(order, start=1):
        row = valid[index]
        label = f"S{n:02d}"
        sheet.append({"label": label, "task": row["task"], "text": row["text"]})
        key[label] = {"config": row["config"], "task": row["task"], "rep": row["rep"]}
    return sheet, key


def render(summary: Dict[str, Dict[str, Any]], verdict: Dict[str, Any]) -> str:
    lines = ["config  runs  discarded  recall(mean±sd)  seconds(mean±sd)"]
    for cid, row in summary.items():
        recall = f"{row['recall']['mean']:.2f}±{row['recall']['sd']:.2f}" if "recall" in row else "-"
        secs = f"{row['seconds']['mean']:.0f}±{row['seconds']['sd']:.0f}" if "seconds" in row else "-"
        lines.append(f"{cid:<7} {row['runs']:>4}  {row['discarded']:>9}  {recall:<15}  {secs}")
    lines.append(f"verdict: {verdict['verdict']}" + (f" ({', '.join(verdict['adopt'])})" if verdict.get("adopt") else ""))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--list", action="store_true", help="print the tasks and configurations")
    parser.add_argument("--prepare", metavar="DIR", help="write tasks, teams, personas and the key below DIR")
    parser.add_argument("--score", metavar="RUNS_DIR", help="pre-grade finished runs and apply the decision rule")
    parser.add_argument("--sheet", metavar="DIR", help="with --score: write the blind grading sheet and its key")
    parser.add_argument("--hard", action="store_true", help="with --prepare: the hard tasks h1-h3 instead of t1-t5")
    parser.add_argument("--lead", default="claude")
    parser.add_argument("--other", default="codex")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    if args.list:
        for task in build_tasks() + build_tasks(True):
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
            sheet, key = blind_sheet(rows, args.seed)
            out = Path(args.sheet).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            (out / "grading-sheet.json").write_text(json.dumps(sheet, indent=2, ensure_ascii=False), encoding="utf-8")
            (out / "grading-key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

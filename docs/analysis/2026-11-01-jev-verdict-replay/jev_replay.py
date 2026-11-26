"""Replays past LLM judge turns through the jev verdict (docs/design/jev-verdict.md, J2 on earlier data).

Reads the hub's debate jobs read-only: for every judge turn, the exact judge material from the saved prompt
(prompts/<seq>.txt), else rebuilt from the job's transcript, and the LLM verdict of that round. Asks jev the same
verdict question the shadow mode asks and writes rows.json and summary.json to the output directory only.

    python3 jev_replay.py --jobs ~/.guardian/data/debate-jobs --out ~/guardian-eval/jev-replay [--dry-run]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import jev_verdict as jv

DECIDE = "Decide if consensus is reached"
JUDGE = "JEV-MCP Judge"


def events(path):
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def from_prompt(text):
    """(task, material) between the last 'Task: ' line before the decide question and that question."""
    k = text.rfind(DECIDE)
    if k < 0:
        return None
    starts = [m.start() for m in re.finditer(r"(?m)^Task: ", text[:k])]
    if not starts:
        return None
    body = text[starts[-1] + len("Task: "):k]
    task, _, material = body.partition("\n")
    return task.strip(), material.strip() + "\n"


def from_transcript(result, round_num):
    entries = [t for t in result.get("transcript") or [] if t.get("round") == round_num]
    experts = [t for t in entries if t.get("role") == "Domain Expert"]
    critics = [t for t in entries if t.get("role") == "Critic Agent"]
    if not experts:
        return None
    if len(experts) > 1:
        proposal = "Independent proposals:\n" + "\n\n".join(
            f"### Proposal {t.get('draft') or chr(65 + i)}\n{t.get('content', '')}" for i, t in enumerate(experts))
    else:
        proposal = f"Proposal:\n{experts[0].get('content', '')}"
    material = proposal + "\n\n"
    if critics:
        material += "Critique:\n" + "\n\n".join(f"### Critique from {t.get('agent')}\n{t.get('content', '')}" for t in critics) + "\n\n"
    return str(result.get("task") or ""), material


def turns(jobs):
    for job in sorted(p for p in jobs.iterdir() if (p / "events.jsonl").exists()):
        evs = events(job / "events.jsonl")
        started = next((e for e in evs if e.get("type") == "debate_started"), {})
        data = started.get("data") or {}
        result = {}
        if (job / "result.json").exists():
            try:
                result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            except ValueError:
                result = {}
        verdicts = {e.get("round"): e.get("data") or {} for e in evs if e.get("type") == "verdict"}
        gates = {e.get("round"): (e.get("data") or {}).get("gate") for e in evs if e.get("type") == "jev_gate"}
        for e in evs:
            if e.get("type") != "agent_started" or e.get("role") != JUDGE:
                continue
            r = e.get("round")
            verdict = verdicts.get(r)
            if verdict is None or verdict.get("unverified"):
                continue
            source, pair = "prompt", None
            if (e.get("data") or {}).get("prompt_saved"):
                prompt = job / "prompts" / f"{e['seq']}.txt"
                if prompt.exists():
                    pair = from_prompt(prompt.read_text(encoding="utf-8", errors="replace"))
            if pair is None:
                source, pair = "transcript", from_transcript(result, r)
            if pair is None:
                continue
            yield {"job": job.name, "project": data.get("project"), "team": data.get("team"), "flow": data.get("flow"),
                   "round": r, "source": source, "llm_approved": bool(verdict.get("approved")), "gate": gates.get(r),
                   "task": pair[0], "material": pair[1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    asker = jv.JevVerdict()
    for turn in turns(Path(args.jobs).expanduser()):
        row = {k: v for k, v in turn.items() if k not in ("task", "material")}
        row["material_chars"] = len(turn["material"])
        if not args.dry_run:
            try:
                rec = asker.ask(turn["task"], turn["material"])
                row.update(jv.compare(rec, turn["llm_approved"]))
            except jv.JevVerdictError as exc:
                row["error"] = str(exc)
            time.sleep(0.2)
        rows.append(row)
        print(json.dumps({k: row.get(k) for k in ("job", "round", "source", "llm_approved", "gate", "p_approve",
                                                   "verdict", "agree", "error", "material_chars")}, ensure_ascii=False))
    (out / "rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    if not args.dry_run:
        good = [dict(r, kind="code") for r in rows if "verdict" in r]
        (out / "summary.json").write_text(json.dumps(jv.summarize(good), indent=1, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(jv.summarize(good), indent=1, ensure_ascii=False))
    print(f"turns: {len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()

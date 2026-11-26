"""Second replay (J2 on earlier data): verdict questions rewritten for the debate's structure.

In a round the critique comes after the proposal, so the proposal never answers it; the judge settles the critique in
its decision. The questions now ask whether a decision can be written now, with the critique's valid points taken
into it, or whether the proposal must be redrafted. Three phrasings are asked in the same request (they cannot see
each other), so one call per turn compares them. Reuses jev_replay.turns; writes only to --out.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import jev_verdict as jv
from jev_replay import turns

QUESTIONS_V2 = [
    {"id": "decide_now", "type": "check",
     "question": "A judge reads this proposal and the critique of it, and writes the final decision itself, adopting what "
                 "is valid in the critique. Can that decision be written now from this material?",
     "yes_means": "The proposal is a sound base for the task; what the critique raises can be settled inside the decision "
                  "(a correction, a constraint, a check to run, a smaller scope) without a new draft.",
     "no_means": "The critique shows the proposal is wrong at its core, misses the task, or rests on claims the material "
                 "does not support, so the proposal must be redrafted before any decision."},
    {"id": "fundamental", "type": "check",
     "question": "Does the critique show a flaw that cannot be fixed within a decision and requires the proposal to be "
                 "redrafted?"},
    {"id": "next_step", "type": "classify",
     "question": "What should the judge of this debate round do next?",
     "options": {"approve": "Write the final decision now, taking the critique's valid points into it.",
                 "redraft": "Send it back: the proposal needs to be rewritten before a decision is possible."}},
    {"id": "evidence", "type": "score",
     "question": "How well do the proposal's claims rest on evidence quoted in the material?",
     "levels": jv.EVIDENCE_LEVELS},
]


def api_questions():
    out = {}
    for q in QUESTIONS_V2:
        if q["type"] == "check":
            e = {"type": "noul", "instructions": q["question"]}
            crit = {k: q[f] for k, f in (("true", "yes_means"), ("false", "no_means")) if q.get(f)}
            if crit:
                e["criteria"] = crit
        elif q["type"] == "classify":
            e = {"type": "choice", "instructions": q["question"], "criteria": q["options"]}
        else:
            e = {"type": "score", "instructions": q["question"], "criteria": list(q["levels"])}
        out[q["id"]] = e
    return out


def ask(task, material):
    import os, urllib.request
    from llm_runner import load_llm_env
    env = {**os.environ, **load_llm_env()}
    url = (env.get("TYPESAFE_BASE_URL", "").strip().rstrip("/") or jv.DEFAULT_BASE_URL) + jv.SYSTEM_ONE_PATH
    state = jv.state_text(task, material)
    body = {"model": env.get("TYPESAFE_DEFAULT_MODEL", "").strip() or jv.DEFAULT_MODEL, "state": state["text"],
            "questions": api_questions()}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + env["TYPESAFE_API_KEY"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode()), state["clipped"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for turn in turns(Path(args.jobs).expanduser()):
        row = {k: v for k, v in turn.items() if k not in ("task", "material")}
        try:
            result, clipped = ask(turn["task"], turn["material"])
            a = result.get("answers") or {}
            row.update({"p_decide": a["decide_now"]["noul"], "p_fundamental": a["fundamental"]["noul"],
                        "next_step": a["next_step"].get("choice"),
                        "p_next_approve": (a["next_step"].get("probabilities") or {}).get("approve"),
                        "evidence": a["evidence"].get("score"), "model": result.get("model"), "clipped": clipped})
        except Exception as exc:  # recorded, the run goes on
            row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        rows.append(row)
        time.sleep(0.2)
    (out / "rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"turns {len(rows)}, errors {sum('error' in r for r in rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()

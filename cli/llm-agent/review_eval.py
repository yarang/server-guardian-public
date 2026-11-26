"""Evaluation V1e of the quick review (docs/design/verification-layer.md). It never calls a model.

The material is the 16 defects seeded for audit run A4b (`docs/analysis/2026-10-19-audit-a4b/ground-truth.json`): each
holds the exact original line of a file of `cli/llm-agent` at the base commit `a3f5783` and the seeded line that breaks
it. `--prepare` turns them into changes: a scratch clone at the base and one commit per case, each case planting four
defects of four different kinds in four files (so a single diff asks for several findings). The same change then goes
to a quick review (`--review --range base..head`) and to a debate of the `default` team that gets the diff in its task
and reads the head commit, so the two see the same thing. `--score` compares them.

    python3 review_eval.py --prepare DIR --source REPO [--data-dir DATA] [--base a3f5783] [--per-case 4]
    bash DIR/run.sh                       # runs every case: review, then debate (written by --prepare)
    python3 review_eval.py --score DIR [--json]

A defect counts as found by the review when one of its findings (not its notes) points at the defect's file within
LINE_SLACK lines of the seeded line; notes that would have matched are counted apart. A debate has no structured
findings list, so it is pre-graded on its final decision and critiques: the file's name and either a line within
LINE_SLACK or the function's name. Precision of the review is the share of its findings that match a seeded defect,
a lower bound until the rest are graded by hand (`grading.json` lists them); the debate's precision needs that grade.

Decision rule (verification-layer.md, V1e): the quick review becomes the default first check when its recall is at
least RECALL_SHARE of the debate's and its mean time at most TIME_SHARE of the debate's; the precision condition is
reported as pending until the grading sheet is filled. Standard library only; nothing is written outside DIR (and the
eval project file in DATA when given).
"""
import argparse
import json
import re
import shlex
import shutil
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
TRUTH = ROOT / "docs/analysis/2026-10-19-audit-a4b/ground-truth.json"
DEFAULT_BASE = "a3f5783"
PROJECT_ID = "review-eval"
LINE_SLACK = 5
RECALL_SHARE = 0.8
TIME_SHARE = 0.25
GIT_ENV = ("-c", "user.email=eval@guardian.invalid", "-c", "user.name=review-eval")
DEBATE_TASK = ("Review this change of the repository (the diff below; the code after the change is at commit {head}). "
               "Report every defect the change introduces, each with file:line and a short failure scenario. Do not "
               "report older problems that the change does not touch.\n\n{diff}")


class EvalError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise EvalError(f"git {' '.join(args[:2])}: {done.stderr.strip()[-300:]}")
    return done.stdout


def load_truth(path: Path = TRUTH) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data:
        for key in ("id", "type", "path", "line", "function", "original", "seeded"):
            if key not in item:
                raise EvalError(f"ground truth {item.get('id', '?')}: missing {key}")
    return data


def bundle(defects: Sequence[Dict[str, Any]], per_case: int) -> List[List[Dict[str, Any]]]:
    """Cases of `per_case` defects, one kind each where possible: the kinds are dealt round-robin in id order."""
    kinds: Dict[str, List[Dict[str, Any]]] = {}
    for item in sorted(defects, key=lambda d: d["id"]):
        kinds.setdefault(item["type"], []).append(item)
    order: List[Dict[str, Any]] = []
    while any(kinds.values()):
        for kind in sorted(kinds):
            if kinds[kind]:
                order.append(kinds[kind].pop(0))
    return [order[i:i + per_case] for i in range(0, len(order), per_case)]


def plant(text: str, item: Dict[str, Any]) -> str:
    """`text` with the defect's original line replaced by its seeded line; the original must occur exactly once."""
    count = text.count(item["original"])
    if count != 1:
        raise EvalError(f"{item['id']}: the original line occurs {count} times in {item['path']} at the base")
    return text.replace(item["original"], item["seeded"], 1)


def prepare(dest: Path, source: Path, base: str = DEFAULT_BASE, per_case: int = 4,
            data_dir: Optional[Path] = None, truth: Path = TRUTH) -> Dict[str, Any]:
    """Clones `source` into DEST/ws, commits one branch per case on top of `base`, writes cases.json and run.sh."""
    defects = load_truth(truth)
    if dest.exists() and any(dest.iterdir()):
        raise EvalError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    ws = dest / "ws"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(ws)], check=True)
    base_sha = _git(ws, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    cases = []
    for number, items in enumerate(bundle(defects, per_case), start=1):
        name = f"case-{number}"
        _git(ws, "checkout", "--quiet", "-B", name, base_sha)
        for item in items:
            path = ws / item["path"]
            path.write_text(plant(path.read_text(encoding="utf-8"), item), encoding="utf-8")
        _git(ws, "add", "-A")
        _git(ws, *GIT_ENV, "commit", "--quiet", "-m", f"review-eval {name}")
        head = _git(ws, "rev-parse", "HEAD").strip()
        cases.append({"id": name, "base": base_sha, "head": head, "defects": [item["id"] for item in items]})
    _git(ws, "checkout", "--quiet", "--detach", base_sha)
    manifest = {"base": base_sha, "workspace": str(ws), "project": PROJECT_ID, "per_case": per_case,
                "truth": str(truth), "cases": cases, "created": datetime.now().astimezone().isoformat(timespec="seconds")}
    (dest / "cases.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    project = {"id": PROJECT_ID, "name": "Quick review evaluation (V1e)", "workspace": _home_path(ws)}
    (dest / "project.json").write_text(json.dumps(project, indent=2), encoding="utf-8")
    if data_dir is not None:
        (data_dir / "projects").mkdir(parents=True, exist_ok=True)
        (data_dir / "projects" / f"{PROJECT_ID}.json").write_text(json.dumps(project, indent=2), encoding="utf-8")
    (dest / "run.sh").write_text(run_script(dest, manifest), encoding="utf-8")
    return manifest


def _home_path(path: Path) -> str:
    """The workspace as ~/... (the project registry wants a path under the home directory)."""
    home = Path.home().resolve()
    resolved = path.resolve()
    return "~/" + str(resolved.relative_to(home)) if home in resolved.parents else str(resolved)


def run_script(dest: Path, manifest: Dict[str, Any]) -> str:
    """The runner: per case one quick review and one debate, sequential, results under DEST/runs/<case>/."""
    lines = [
        "#!/usr/bin/env bash",
        "# Written by review_eval.py --prepare. Needs GUARDIAN_DATA_DIR (the eval data dir that holds projects/"
        f"{PROJECT_ID}.json) and ENGINE (a checkout of the engine with review_agent.py). Never the production data dir.",
        "set -uo pipefail",
        ': "${GUARDIAN_DATA_DIR:?set GUARDIAN_DATA_DIR to the eval data dir}"',
        ': "${ENGINE:?set ENGINE to the engine checkout}"',
        'case "$GUARDIAN_DATA_DIR" in *"/.guardian/data"*) echo "refusing the production data dir" >&2; exit 2 ;; esac',
        "export GUARDIAN_AUTO_APPLY=off",
        f"out={shlex.quote(str(dest / 'runs'))}",
        f"ws={shlex.quote(manifest['workspace'])}",
        'cd "$ENGINE"',
    ]
    for case in manifest["cases"]:
        cid, base, head = case["id"], case["base"], case["head"]
        lines += [
            f'd="$out/{cid}"; mkdir -p "$d"',
            f'if [ ! -f "$d/review.json" ]; then',
            f'  echo "[{cid}] review $(date -u +%FT%TZ)"',
            f'  python3 cli/llm-agent/guardian.py --review --project {PROJECT_ID} --range {base}..{head} --json '
            f'> "$d/review.json.tmp" 2> "$d/review.log" &&',
            f'    mv "$d/review.json.tmp" "$d/review.json"',
            "fi",
            f'if [ ! -f "$d/debate-result.json" ]; then',
            f'  echo "[{cid}] debate $(date -u +%FT%TZ)"',
            f'  diff="$(git -C "$ws" diff --no-color {base} {head})"',
            f'  task="$(python3 -c \'import sys; print(sys.argv[1].format(head=sys.argv[2], diff=sys.argv[3]))\' '
            f'{shlex.quote(DEBATE_TASK)} {head[:12]} "$diff")"',
            f'  files="$(git -C "$ws" diff --name-only {base} {head} | sed "s/^/--context-path=/" | tr "\\n" " ")"',
            f'  timeout 7200 python3 cli/llm-agent/guardian.py --debate="$task" --project {PROJECT_ID} --team default '
            f'--rounds 1 $files --context-ref={head} --events-file "$d/debate-events.jsonl" '
            f'--result-file "$d/debate-result.json.tmp" > "$d/debate.log" 2>&1 &&',
            f'    mv "$d/debate-result.json.tmp" "$d/debate-result.json"',
            "fi",
        ]
    lines.append('echo "ALL DONE $(date -u +%FT%TZ)"')
    return "\n".join(lines) + "\n"


# --- scoring ----------------------------------------------------------------------------------------------------


def _near(path: str, line: Any, item: Dict[str, Any]) -> bool:
    return path == item["path"] and isinstance(line, int) and abs(line - int(item["line"])) <= LINE_SLACK


def score_review(review: Dict[str, Any], items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    findings = review.get("findings") or []
    notes = review.get("notes") or []
    found = {item["id"]: any(_near(f.get("path", ""), f.get("line"), item) for f in findings) for item in items}
    in_notes = {item["id"]: (not found[item["id"]]) and any(_near(n.get("path", ""), n.get("line"), item) for n in notes)
                for item in items}
    matched = sum(1 for f in findings if any(_near(f.get("path", ""), f.get("line"), item) for item in items))
    return {"found": found, "in_notes": in_notes, "findings": len(findings), "matched_findings": matched,
            "state": review.get("state"), "seconds": review.get("elapsed_s"), "degraded": bool(review.get("degraded"))}


def debate_text(result: Dict[str, Any]) -> str:
    """What counts for the debate: its final decision and the critiques (the evaluation 2 convention)."""
    parts = [json.dumps(result.get("final_decision") or "", ensure_ascii=False)]
    parts += [str(t.get("content") or "") for t in result.get("transcript") or [] if t.get("role") == "Critic Agent"]
    return "\n\n".join(parts)


def debate_mentions(text: str, item: Dict[str, Any]) -> bool:
    name = item["path"].rsplit("/", 1)[-1]
    if name not in text:
        return False
    for match in re.finditer(re.escape(name) + r"[:#L ]+(\d+)", text):
        if abs(int(match.group(1)) - int(item["line"])) <= LINE_SLACK:
            return True
    return bool(item.get("function")) and re.search(rf"\b{re.escape(item['function'])}\b", text) is not None


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def debate_seconds(events: Path) -> Tuple[Optional[float], int, bool]:
    """Seconds, fallback agents and whether a debate_finished event was seen; seconds is None when a ts is missing."""
    started = finished = None
    finished_seen = False
    fallbacks = 0
    if events.exists():
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "debate_started":
                started = event.get("ts")
            elif kind == "debate_finished":
                finished_seen = True
                finished = event.get("ts")
            elif kind == "agent_finished" and (event.get("data") or {}).get("fallback"):
                fallbacks += 1
    seconds = (_ts(finished) - _ts(started)).total_seconds() if started and finished else None
    return seconds, fallbacks, finished_seen


def score(dest: Path) -> Dict[str, Any]:
    manifest = json.loads((dest / "cases.json").read_text(encoding="utf-8"))
    truth = {item["id"]: item for item in load_truth(Path(manifest.get("truth") or TRUTH))}
    rows, grading = [], []
    for case in manifest["cases"]:
        run = dest / "runs" / case["id"]
        items = [truth[i] for i in case["defects"]]
        row: Dict[str, Any] = {"case": case["id"], "defects": case["defects"], "review": None, "debate": None}
        review_file = run / "review.json"
        if review_file.exists():
            review = json.loads(review_file.read_text(encoding="utf-8") or "{}")
            row["review"] = score_review(review, items)
            for f in review.get("findings") or []:
                if not any(_near(f.get("path", ""), f.get("line"), item) for item in items):
                    grading.append({"case": case["id"], "system": "review", "path": f.get("path"), "line": f.get("line"),
                                    "claim": f.get("claim"), "grade": None})
        debate_file = run / "debate-result.json"
        if debate_file.exists():
            result = json.loads(debate_file.read_text(encoding="utf-8") or "{}")
            text = debate_text(result)
            seconds, fallbacks, finished_seen = debate_seconds(run / "debate-events.jsonl")
            decision = result.get("final_decision")
            row["debate"] = {"found": {item["id"]: debate_mentions(text, item) for item in items},
                             "seconds": seconds, "fallback_agents": fallbacks,
                             "finished": finished_seen and isinstance(decision, dict) and bool(decision)}
            grading.append({"case": case["id"], "system": "debate", "decision": result.get("final_decision"),
                            "grade": None})
        rows.append(row)
    summary = summarize(rows)
    (dest / "grading.json").write_text(json.dumps(grading, indent=2, ensure_ascii=False), encoding="utf-8")
    report = {"rows": rows, "summary": summary, "verdict": decide(summary)}
    (dest / "score.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for system in ("review", "debate"):
        done = [r[system] for r in rows if r.get(system)]
        found = [v for r in done for v in r["found"].values()]
        seconds = [r["seconds"] for r in done if isinstance(r.get("seconds"), (int, float))]
        entry: Dict[str, Any] = {"cases": len(done), "defects": len(found), "found": sum(found),
                                 "recall": (sum(found) / len(found)) if found else None,
                                 "mean_seconds": statistics.mean(seconds) if seconds else None}
        if system == "review":
            total = sum(r["findings"] for r in done)
            entry.update({"findings": total, "matched_findings": sum(r["matched_findings"] for r in done),
                          "precision_lower_bound": (sum(r["matched_findings"] for r in done) / total) if total else None,
                          "in_notes": sum(v for r in done for v in r["in_notes"].values()),
                          "not_done": sum(1 for r in done if r.get("state") != "done")})
        else:
            entry["fallback_agents"] = sum(r["fallback_agents"] for r in done)
            entry["not_done"] = sum(1 for r in done if r.get("finished") is not True)
        out[system] = entry
    return out


def decide(summary: Dict[str, Any]) -> Dict[str, Any]:
    review, debate = summary["review"], summary["debate"]
    if review["recall"] is None or debate["recall"] is None or not review["cases"] or review["cases"] != debate["cases"]:
        return {"decision": "incomplete", "reason": "both systems must have finished the same cases"}
    review_left, debate_left = review.get("not_done", 0), debate.get("not_done", 0)
    if review_left or debate_left:
        return {"decision": "incomplete",
                "reason": f"{review_left} review and {debate_left} debate runs did not finish (state not done)"}
    recall_ok = review["recall"] >= RECALL_SHARE * debate["recall"]
    time_ok = (review["mean_seconds"] is not None and debate["mean_seconds"] is not None
               and review["mean_seconds"] <= TIME_SHARE * debate["mean_seconds"])
    return {"recall_ok": recall_ok, "time_ok": time_ok, "precision": "pending the grading sheet (grading.json)",
            "decision": "keep as default first check (precision pending)" if recall_ok and time_ok else "do not adopt yet"}


def render(report: Dict[str, Any]) -> str:
    s, v = report["summary"], report["verdict"]
    fmt = lambda x: "-" if x is None else (f"{x:.2f}" if isinstance(x, float) else str(x))  # noqa: E731
    lines = ["system   cases  found/defects  recall  mean s"]
    for system in ("review", "debate"):
        e = s[system]
        lines.append(f"{system:<8} {e['cases']:>5}  {e['found']:>5}/{e['defects']:<7} {fmt(e['recall']):>6}  {fmt(e['mean_seconds'])}")
    r = s["review"]
    lines.append(f"review findings {r['findings']}, matched {r['matched_findings']} (precision >= {fmt(r['precision_lower_bound'])}), "
                 f"seeded defects found only in notes {r['in_notes']}, runs not done {r['not_done']}")
    lines.append(f"debate agents that fell back: {s['debate']['fallback_agents']}, runs not done {s['debate']['not_done']}")
    lines.append(f"verdict: {v['decision']}" + ("" if "recall_ok" not in v else
                                                f" (recall_ok={v['recall_ok']}, time_ok={v['time_ok']}, precision {v['precision']})"))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prepare", metavar="DIR", type=Path)
    parser.add_argument("--source", metavar="REPO", type=Path, help="With --prepare: a full clone that holds the base commit")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--per-case", type=int, default=4)
    parser.add_argument("--data-dir", type=Path, help="With --prepare: the eval data dir that gets projects/review-eval.json")
    parser.add_argument("--score", metavar="DIR", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.prepare:
            if not args.source:
                raise EvalError("--prepare needs --source")
            if args.data_dir and "/.guardian/data" in str(args.data_dir.resolve()):
                raise EvalError("refusing the production data dir")
            manifest = prepare(args.prepare, args.source, args.base, args.per_case, args.data_dir)
            print(json.dumps(manifest, indent=2) if args.json else
                  f"{len(manifest['cases'])} cases at {manifest['base'][:12]} in {args.prepare}; run {args.prepare / 'run.sh'}")
        elif args.score:
            report = score(args.score)
            print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
        else:
            parser.error("give --prepare or --score")
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

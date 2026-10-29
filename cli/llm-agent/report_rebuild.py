"""Rewrite stored debate result documents from their `<thread>.json`, so old reports get the current layout.

`build_report` is a pure function of what `<thread>.json` keeps (task, team, context, transcript, decision), so a
report written by an older generator can be regenerated without running the debate again. Only `<thread>.md` and
`<thread>.assets/` of the given project directories are rewritten; `latest_debate_transcript.md`, the JSON files
and the thread index are never touched.

    python3 report_rebuild.py <decisions_dir> [--apply]     # dry run unless --apply
"""

import json
import sys
from pathlib import Path
from typing import List, Tuple

from debate_report import build_report, write_report


def rebuild_project(project_dir: Path, apply: bool) -> List[Tuple[str, str]]:
    """(thread id, outcome) per thread JSON in `project_dir`; outcome is changed, unchanged or error: ...."""
    results: List[Tuple[str, str]] = []
    for json_path in sorted(project_dir.glob("*.json")):
        if json_path.name == "threads_index.json":
            continue
        thread_id = json_path.stem
        try:
            from debate_engine import decision_status
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if data.get("thread_id") != thread_id or not isinstance(data.get("transcript"), list):
                continue
            decision = data.get("decision") if isinstance(data.get("decision"), dict) else None
            old_path = project_dir / f"{thread_id}.md"
            old = old_path.read_text(encoding="utf-8") if old_path.is_file() else ""
            # Keep the original timestamp: a rebuild is not a new save.
            updated = str(data.get("updated_at") or "")[:19]
            markdown, assets = build_report(
                data.get("project_id") or project_dir.name, thread_id, str(data.get("task") or ""), data["transcript"],
                decision, decision_status(decision), team=data.get("team"), context=data.get("context"), updated=updated,
            )
            if markdown == old:
                results.append((thread_id, "unchanged"))
                continue
            if apply:
                write_report(project_dir, thread_id, markdown, assets)
            results.append((thread_id, "changed"))
        except Exception as exc:  # one broken thread must not stop the others
            results.append((thread_id, f"error: {exc}"))
    return results


def main(argv: List[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    apply = "--apply" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    root = Path(args[0]) / "projects"
    failed = 0
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        for thread_id, outcome in rebuild_project(project_dir, apply):
            failed += outcome.startswith("error")
            print(f"{project_dir.name}/{thread_id}: {outcome}")
    print("applied" if apply else "dry run (use --apply to write)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

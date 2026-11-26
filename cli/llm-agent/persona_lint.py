"""Persona lint: the form guide of docs/design/agent-characters.md (section 3) as checks, warning only (phase C0).

Two kinds of finding:

- `warn`: wrong in any format: the text is longer than the guide's limit, claims autonomy, or carries facts that drift
  (host counts, version numbers, repository paths, environment variables).
- `move`: a seat rule, an evidence format of the project kind or a tool name. The guide puts these in engine role cards
  and kind protocols (phase C1); until then the builtin personas carry them on purpose, so they are reported as the
  baseline of what C1 will move, not as errors.

Sentences that several personas repeat word for word are listed too: they are the candidates for role cards.
The lint never fails CI and never blocks a save; `validate_persona` stays the only gate.

    python3 persona_lint.py              # builtin personas, one line per finding and a summary
    python3 persona_lint.py --json       # the same as JSON
    python3 persona_lint.py --custom     # builtin and custom personas (custom override builtin by id)
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Pattern, Tuple

import character

# The guide's limits for the persona text (section 3; decision 2 of chapter 6): the rendered block's bounds of
# character.py, which C2 made the home of the limits.
LENGTH_LIMIT = character.RENDER_LIMIT
LENGTH_NOTE = character.RENDER_NOTE
# A sentence repeated in this many personas is a role card candidate.
SHARED_MIN = 3

WARN, MOVE, NOTE = "warn", "move", "note"

# (level, rule, pattern, what). Patterns are matched against the persona's own text only (`character.body_text`:
# the system_prompt, or a character's rendered block): nothing else reaches the model.
RULES: Tuple[Tuple[str, str, Pattern[str], str], ...] = (
    (WARN, "autonomy", re.compile(r"자율|스스로 실행|집행하세요|autonomous|on your own", re.IGNORECASE),
     "claims to act on its own"),
    (WARN, "host-count", re.compile(r"\b\d+\s*대(?!상|안|응|비|기)|\b\d+\s+(?:hosts|servers)\b"), "counts hosts"),
    (WARN, "version", re.compile(r"\bv?\d+\.\d+\.\d+\b|\bp\d{2}\b"), "names a version"),
    (WARN, "path", re.compile(r"\b(?:apps|cli|scripts|configs|deploy|docs|packages|tools)/[A-Za-z0-9_./-]+"
                              r"|\b[A-Za-z0-9_-]+\.(?:py|go|tsx?|sh|json|md)(?![A-Za-z0-9])"), "names a repository path"),
    (WARN, "env", re.compile(r"\b(?:GUARDIAN|AGENT|ZAI|ANTHROPIC|OPENAI|GEMINI)_[A-Z0-9_]+(?![A-Za-z0-9_])"),
     "names an environment variable"),
    (MOVE, "tool", re.compile(r"\b(?:ops|jev_mcp):[a-z_]+"), "names a tool (engine adds tool rules)"),
    (MOVE, "grading", re.compile(r"\[차단\]|\[권고\]|심각도|높음/중간/낮음|high/medium/low"),
     "grades objections (critic role card)"),
    (MOVE, "verdict", re.compile(r"금지 조건|문제가 없으면 없다고|없다고 분명히|승인하지 마세요|판정의 일은"),
     "seat behaviour (role card)"),
    (MOVE, "citation", re.compile(r"파일:줄|file:line|\[#번호|조회 호스트"), "citation syntax (kind protocol)"),
    (MOVE, "protocol", re.compile(r"ops 블록|```(?:repo|ops)|repo 블록"), "evidence protocol (kind protocol)"),
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def check_text(text: str) -> List[Dict[str, str]]:
    """Findings for one persona text: level, rule, what, and the matched text."""
    findings: List[Dict[str, str]] = []
    length = len(text)
    if length > LENGTH_LIMIT:
        findings.append({"level": WARN, "rule": "length", "what": f"{length} characters, limit {LENGTH_LIMIT}",
                         "match": ""})
    elif length > LENGTH_NOTE:
        findings.append({"level": NOTE, "rule": "length", "what": f"{length} characters, note above {LENGTH_NOTE}",
                         "match": ""})
    for level, rule, pattern, what in RULES:
        match = pattern.search(text)
        if match:
            findings.append({"level": level, "rule": rule, "what": what, "match": match.group(0)})
    return findings


def warnings(text: str) -> List[str]:
    """The `warn` findings as short messages, for save and suggest responses."""
    return [f"system_prompt {f['what']}" + (f" ({f['match']!r})" if f["match"] else "")
            for f in check_text(text) if f["level"] == WARN]


def persona_warnings(persona: Dict[str, Any]) -> List[str]:
    """The guide's warnings for a persona file (what a save shows, never blocking): `warn` findings, plus the 501-700
    length note for a character, whose block an over-limit text already refuses at validation."""
    findings = check_text(character.body_text(persona))
    if character.is_character(persona):
        shown = [f for f in findings if f["level"] in (WARN, NOTE)]
        prefix = "persona"
    else:
        shown = [f for f in findings if f["level"] == WARN]
        prefix = "system_prompt"
    return [f"{prefix} {f['what']}" + (f" ({f['match']!r})" if f["match"] else "") for f in shown]


def sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE.split(" ".join(text.split())) if len(s.strip()) >= 12]


def shared_sentences(personas: List[Dict[str, Any]], minimum: int = SHARED_MIN) -> List[Dict[str, Any]]:
    """Sentences that `minimum` or more personas repeat word for word, most repeated first."""
    owners: Dict[str, set] = defaultdict(set)
    for persona in personas:
        for sentence in sentences(character.body_text(persona)):
            owners[sentence].add(persona.get("id"))
    rows = [{"sentence": s, "personas": sorted(ids)} for s, ids in owners.items() if len(ids) >= minimum]
    return sorted(rows, key=lambda r: (-len(r["personas"]), r["sentence"]))


def lint(personas: List[Dict[str, Any]]) -> Dict[str, Any]:
    entries = []
    counts = {WARN: 0, MOVE: 0, NOTE: 0}
    for persona in sorted(personas, key=lambda p: str(p.get("id"))):
        text = character.body_text(persona)
        findings = check_text(text)
        for f in findings:
            counts[f["level"]] += 1
        entries.append({"id": persona.get("id"), "source": persona.get("source", "builtin"), "length": len(text),
                        "findings": findings})
    lengths = sorted(e["length"] for e in entries)
    summary = {
        "personas": len(entries),
        "findings": counts,
        "over_limit": sum(1 for n in lengths if n > LENGTH_LIMIT),
        "median_length": lengths[len(lengths) // 2] if lengths else 0,
        "max_length": lengths[-1] if lengths else 0,
    }
    return {"summary": summary, "personas": entries, "shared": shared_sentences(personas)}


def render(report: Dict[str, Any]) -> str:
    lines = []
    for entry in report["personas"]:
        for f in entry["findings"]:
            match = f" {f['match']!r}" if f["match"] else ""
            lines.append(f"{f['level']:<4} {entry['id']:<28} {f['rule']:<10} {f['what']}{match}")
    for row in report["shared"]:
        lines.append(f"shared x{len(row['personas'])}: {row['sentence'][:90]}")
    s = report["summary"]
    lines.append(f"persona lint: {s['personas']} personas, {s['findings'][WARN]} warn, {s['findings'][MOVE]} move, "
                 f"{s['findings'][NOTE]} note, {len(report['shared'])} shared sentences; length median "
                 f"{s['median_length']}, max {s['max_length']}, {s['over_limit']} over {LENGTH_LIMIT} (warning only)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Lint personas against the form guide (warning only).")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--custom", action="store_true", help="include custom personas (custom override builtin)")
    args = parser.parse_args(argv)
    from persona_manager import PersonaManager

    manager = PersonaManager()
    personas = manager.list_personas() if args.custom else [
        {**p, "source": "builtin"} for p in PersonaManager._read_dir(manager.personas_dir)]
    report = lint(personas)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

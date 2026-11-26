"""Golden dump of the standing instructions every builtin team seat gets (docs/design/agent-characters.md, C1).

Each builtin team runs one debate under a deterministic fake LLM (the fixture project and runner of
test_flow_golden.py), and every turn's standing instructions (the claude system prompt: persona, role card, kind
protocol, rules) are recorded per seat. The retrieval and ops protocols are the same for every seat; they are kept as
hash and size so the dump shows the persona part in full. Each entry also keeps the hash of the exact text, so a byte
change shows even where the readable form (one sentence per line) hides it.

The expected dump lives in `golden/seat_prompts.txt`. A change of a persona or a role card must show up in its diff,
and nothing else may: review it sentence by sentence. Regenerate on purpose with
`GOLDEN_UPDATE=1 python3 -m unittest test_seat_golden`, then list what changed per seat (sentences gone and added; a
seat whose sentences only moved says so) with `python3 test_seat_golden.py --review <git revision>`.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Tuple
from unittest.mock import patch

import test_flow_golden as flow
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine
from debate_events import EventWriter
from llm_runner import LLMResult
from ops_checks import protocol_text as ops_protocol_text
from project_registry import ProjectManager
from team_manager import TeamManager
from test_ops_project import OPS, _FixedTargets
from test_team_flows import DESIGN

GOLDEN = Path(__file__).parent / "golden" / "seat_prompts.txt"
TEAMS_DIR = Path(__file__).resolve().parents[2] / "configs" / "teams"


def setUpModule() -> None:
    flow.setUpModule()


def tearDownModule() -> None:
    flow.tearDownModule()


def project_for(team_id: str) -> str:
    kind = json.loads((TEAMS_DIR / f"{team_id}.json").read_text(encoding="utf-8"))
    if team_id.startswith("fleet-"):
        return OPS["id"]
    return DESIGN["id"] if kind.get("category") == "design-docs" else flow.CODE_PROJECT


def claude_only(value):
    """The team with claude as every seat's only provider: the standing instructions (the claude system prompt) then
    exist for every seat. Other providers get the same persona text at the head of their one message."""
    if isinstance(value, dict):
        return {k: (["claude"] if k == "providers" else claude_only(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [claude_only(v) for v in value]
    return value


def _placeholder(name: str, text: str) -> str:
    return f"<<{name}: sha256 {hashlib.sha256(text.encode()).hexdigest()[:16]}, {len(text.encode())} bytes>>"


def readable(text: str) -> List[str]:
    """One sentence per line (display only: the entry's hash is of the exact text)."""
    out: List[str] = []
    for line in text.split("\n"):
        out += re.split(r"(?<=[.!?]) (?=\S)", line) if line else [""]
    return out


def seat_prompts(team_id: str) -> List[Tuple[str, str]]:
    """(seat label, standing text with the shared protocols replaced) for every distinct seat turn of one debate."""
    project = project_for(team_id)
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        repo = root / "repo"
        flow.make_repo(repo)
        builtin, custom = root / "builtin", root / "custom"
        builtin.mkdir()
        custom.mkdir()
        projects = {flow.CODE_PROJECT: {"id": flow.CODE_PROJECT, "name": "Golden", "workspace": "~/repo"},
                    OPS["id"]: OPS, DESIGN["id"]: DESIGN}
        for data in projects.values():
            (custom / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")
        manager = TeamManager(root / "teams")
        manager.save(claude_only(json.loads((TEAMS_DIR / f"{team_id}.json").read_text(encoding="utf-8"))))
        engine = DebateEngine(db_path=root / "d.db", events=EventWriter(str(root / "events.jsonl")), team_mgr=manager)
        engine.project_mgr = ProjectManager(directory=custom, builtin_dir=builtin)
        seen: Dict[Tuple[str, str], None] = {}
        real_call = engine._call

        def call(thread_id, key, prompt, context, session_id, provider, providers, failures, who, **kwargs):
            instructions = kwargs.get("instructions")
            text = instructions.static if instructions is not None else "(no standing instructions)"
            if engine._repo is not None and engine._retrieval_static:
                text = text.replace(engine._retrieval_static, _placeholder("retrieval protocol", engine._retrieval_static))
            if engine._ops is not None:
                protocol = ops_protocol_text(engine._ops.provisioning)
                text = text.replace(protocol, _placeholder("ops protocol", protocol))
            label = f"{key} ({who.get('agent_name')})"
            seen.setdefault((label, text), None)
            return real_call(thread_id, key, prompt, context, session_id, provider, providers, failures, who, **kwargs)

        def run(prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
            name = (providers or ["default"])[0]
            if JUDGE_VERDICT_RULE in prompt + (instructions.text() if instructions else ""):
                return LLMResult(f"{name}-cli", "Agreed.\nVERDICT: APPROVED", "s")
            return LLMResult(f"{name}-cli", f"text from {name}", "s")

        fixed = _FixedTargets() if project == OPS["id"] else None
        with patch.dict(os.environ, {"HOME": str(root)}), \
                patch.object(engine, "_call", side_effect=call), \
                patch.object(engine.llm, "run_session_prompt", side_effect=run), \
                patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or root / "l.md")):
            if fixed:
                fixed.__enter__()
            try:
                engine.run_debate("task", project_id=project, team=team_id, thread_id=flow.THREAD)
            finally:
                if fixed:
                    fixed.__exit__(None, None, None)
        engine.events.close()
        paths = tuple(sorted({str(root), str(repo), str(Path(tmp))}, key=len, reverse=True))
        return [(label, flow._scrub(text, paths)) for label, text in seen]


def dump() -> str:
    parts: List[str] = []
    for path in sorted(TEAMS_DIR.glob("*.json")):
        for label, text in seat_prompts(path.stem):
            parts.append(f"=== {path.stem} / {label}\nsha256 {hashlib.sha256(text.encode()).hexdigest()}\n")
            parts.append("\n".join(readable(text)) + "\n\n")
    return "".join(parts)


def parse(text: str) -> Dict[str, Tuple[str, List[str]]]:
    """seat header -> (sha line, sentence lines) of a dump."""
    out: Dict[str, Tuple[str, List[str]]] = {}
    seat = ""
    for line in text.split("\n"):
        if line.startswith("=== "):
            seat = line[4:]
            out[seat] = ("", [])
        elif seat and not out[seat][0] and line.startswith("sha256 "):
            out[seat] = (line, out[seat][1])
        elif seat:
            out[seat][1].append(line)
    return out


def review(before: str, after: str) -> str:
    """Per changed seat: the sentences that went and came (as multisets), or that only the order changed."""
    old, new = parse(before), parse(after)
    lines: List[str] = []
    for seat in sorted(old.keys() | new.keys()):
        if seat not in old or seat not in new:
            lines.append(f"## {seat}: {'added' if seat in new else 'gone'}")
            continue
        if old[seat][0] == new[seat][0]:
            continue
        gone, came = Counter(old[seat][1]) - Counter(new[seat][1]), Counter(new[seat][1]) - Counter(old[seat][1])
        lines.append(f"## {seat}" + ("" if gone or came else ": order only"))
        lines += [f"  - {s}" for s in gone.elements()] + [f"  + {s}" for s in came.elements()]
    unchanged = sum(1 for seat in old.keys() & new.keys() if old[seat][0] == new[seat][0])
    return "\n".join(lines + [f"unchanged: {unchanged} of {len(new)} seats"]) + "\n"


class SeatGoldenTests(unittest.TestCase):
    def test_dump_matches_golden(self) -> None:
        text = dump()
        if os.environ.get("GOLDEN_UPDATE") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(text, encoding="utf-8")
        self.assertTrue(GOLDEN.exists(), "run once with GOLDEN_UPDATE=1 to record the dump")
        expected = GOLDEN.read_text(encoding="utf-8")
        if text != expected:
            changed = sorted({line for line in text.splitlines() if line.startswith("=== ")}
                             ^ {line for line in expected.splitlines() if line.startswith("=== ")})
            self.fail("seat prompts changed; diff golden/seat_prompts.txt after GOLDEN_UPDATE=1"
                      + (f" (seats added or gone: {changed})" if changed else ""))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--review"] and len(sys.argv) == 3:
        rel = GOLDEN.relative_to(Path(__file__).resolve().parents[2]).as_posix()
        base = subprocess.run(["git", "show", f"{sys.argv[2]}:{rel}"], capture_output=True, text=True, check=True).stdout
        sys.stdout.write(review(base, GOLDEN.read_text(encoding="utf-8")))
    else:
        unittest.main()

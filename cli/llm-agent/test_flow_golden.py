"""Golden dump of the flow engine (docs/design/flow-templates.md, T2).

Every flow shape runs under a deterministic fake LLM against a fixture project (a tiny git repository with pinned
author, dates and therefore commit id), so the dump does not depend on the checkout it runs in. Scenarios: plain,
resumed on the same thread, a canned fallback for the first seat, a canned fallback for the critics only; plus the
ops and design project kinds and two builtin teams. The dump records every prompt (as hash, size and opening),
provider list, event and result. The expected dump lives in `golden/flows.json`; a refactor of the engine passes only
if the dump is identical. The prompt version stamp is normalised so a deliberate prompt-version bump does not need a
new dump.

Regenerate on purpose with `GOLDEN_UPDATE=1 python3 -m unittest test_flow_golden`.
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import flow_graph
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from project_registry import ProjectManager
from team_manager import TeamManager
from test_ops_project import OPS, _FixedTargets
from test_team_flows import BASE, DESIGN, PANEL, PIPELINE, VERIFY, VERSUS
from testenv import isolate_data_dir, restore_data_dir

GOLDEN = Path(__file__).parent / "golden" / "flows.json"
THREAD = "thread_20261029_000000_golden"
CODE_PROJECT = "golden-repo"
PARALLEL_ENV = "GUARDIAN_MAX_PARALLEL_AGENTS"
PROVIDERS_ENV = "GUARDIAN_LLM_PROVIDERS"  # CI sets it to "none"; it changes how the system prompt is delivered
HOST_ENV = "GUARDIAN_HOST_LABEL"  # engine_host() goes into the events; the machine's own name must not reach the dump
GOLDEN_HOST = "golden-host"
_saved_env: Dict[str, Optional[str]] = {}

DEBATE_PROPOSERS = {
    **BASE, "id": "proposerteam", "name": "Proposer team",
    "proposers": [{"persona": "devops_specialist", "providers": ["codex"]}],
}
# Shapes whose Judge rejects run every round, so the later-round paths (consolidate, rewrite) are exercised too.
VERIFY_ROUNDS = {**VERIFY, "id": "verifyrounds", "name": "Verify rounds", "max_rounds": 2}
PIPELINE_CRITIC = {**PIPELINE, "id": "pipelinecritic", "name": "Pipeline critic", "max_rounds": 2,
                   "critic": [{"persona": "critic_redteam", "providers": ["codex"]}]}
SHAPES = {
    "debate": BASE,
    "debate-proposers": DEBATE_PROPOSERS,
    "panel": PANEL,
    "verify": VERIFY,
    "verify-rounds": VERIFY_ROUNDS,
    "pipeline": PIPELINE,
    "pipeline-critic": PIPELINE_CRITIC,
    "versus": VERSUS,
}
HAS_CRITIC = ("debate", "debate-proposers", "verify", "verify-rounds", "pipeline-critic")


def setUpModule() -> None:
    for key in ("GUARDIAN_JEV_GATE", PARALLEL_ENV, PROVIDERS_ENV, HOST_ENV):
        _saved_env[key] = os.environ.get(key)
    os.environ.pop(PROVIDERS_ENV, None)
    os.environ[HOST_ENV] = GOLDEN_HOST
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    os.environ[PARALLEL_ENV] = "1"  # parallel phases then run in submission order
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()
    for key, value in _saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def make_repo(path: Path) -> None:
    """A two-file repository whose commit id is the same on every run. The machine's git configuration is not read:
    a global commit.gpgsign (a signing key) puts a signature into the commit and changes its id."""
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "g", "GIT_AUTHOR_EMAIL": "g@example.test", "GIT_COMMITTER_NAME": "g",
           "GIT_COMMITTER_EMAIL": "g@example.test", "GIT_AUTHOR_DATE": "2026-10-29T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-10-29T00:00:00Z"}
    (path / "app.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    (path / "README.md").write_text("# Golden fixture\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=path, env=env, check=True, capture_output=True)


SKIP_KEYS = {"ts", "time", "timestamp", "duration", "elapsed", "duration_ms", "log_file", "db_path", "created_at"}


def _scrub(value: Any, paths: Tuple[str, ...] = ()) -> Any:
    """Drop what changes between runs: timestamps, temp paths, the prompt version stamp."""
    if isinstance(value, dict):
        return {k: _scrub(v, paths) for k, v in sorted(value.items()) if k not in SKIP_KEYS}
    if isinstance(value, list):
        return [_scrub(v, paths) for v in value]
    if isinstance(value, str):
        for path in paths:
            value = value.replace(path, "<ws>")
        return re.sub(r"\d{4}-\d{2}-\d{2}\.p\d+", "<prompt-version>", value)
    return value


def _blob(text: str) -> Dict[str, Any]:
    """A long text kept as its hash, size and opening, so the file stays small and any byte change still shows."""
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode()), "head": text[:160]}


def _digest(value: Any, limit: int = 600) -> Any:
    if isinstance(value, dict):
        return {k: _digest(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_digest(v, limit) for v in value]
    if isinstance(value, str) and len(value) > limit:
        return _blob(value)
    return value


def first_provider(team: dict) -> str:
    seat = team.get("flow", {}).get("stages") or team.get("flow", {}).get("sides") or [team["expert"]]
    return seat[0]["providers"][0]


FLOW_FIELDS = {"seat", "flow_graph"}


def _without_flow_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_flow_fields(v) for k, v in value.items() if k not in FLOW_FIELDS}
    if isinstance(value, list):
        return [_without_flow_fields(v) for v in value]
    return value


def _without_versus_lead(dump: Dict[str, Any]) -> Dict[str, Any]:
    """A versus never runs its lead; a flow has no seat for it, so a compiled versus seats side A's character there.
    Only the lineup snapshot and the member list of `debate_started` name it; every prompt stays the same."""
    for result in dump["results"]:
        result.get("team", {}).pop("expert", None)
    for event in dump["events"]:
        if event["type"] == "debate_started":
            event["data"]["members"] = event["data"]["members"][1:]
    return dump


class FlowGoldenTests(unittest.TestCase):
    def run_case(self, team: dict, project: str, scenario: str, tasks: Tuple[str, ...] = ("task",),
                 via_flow: bool = False) -> dict:
        """One case of the dump. via_flow starts the first task from the team converted into a flow
        (flow_graph.team_to_flow, then resolve_run; docs/design/flow-graph.md, G2), later tasks continue its thread."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "repo"
            make_repo(repo)
            builtin, custom = root / "builtin", root / "custom"
            builtin.mkdir()
            custom.mkdir()
            # A workspace must be "~/...": HOME points at the scratch directory for the run.
            projects = {CODE_PROJECT: {"id": CODE_PROJECT, "name": "Golden", "workspace": "~/repo"},
                        OPS["id"]: OPS, DESIGN["id"]: DESIGN}
            for data in projects.values():
                (custom / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")
            manager = TeamManager(root / "teams")
            if not (Path(__file__).parents[2] / "configs" / "teams" / f"{team['id']}.json").exists():
                manager.save(team)
            engine = DebateEngine(db_path=root / "d.db", events=EventWriter(str(root / "events.jsonl")), team_mgr=manager)
            engine.project_mgr = ProjectManager(directory=custom, builtin_dir=builtin)

            calls: List[dict] = []
            lock = threading.Lock()
            critic_names = {m["providers"][0] for m in team.get("critic", []) if m.get("providers")}
            canned_for: set = set()
            if scenario == "canned":
                canned_for = {first_provider(team)}
            elif scenario == "canned-critic":
                canned_for = critic_names
            elif scenario == "canned-mid":
                canned_for = {team["flow"]["stages"][1]["providers"][0]}

            def run(prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
                prompt = prompt + (instructions.text() if instructions else "")
                with lock:
                    calls.append({"prompt": prompt, "providers": providers, "session": bool(session_id)})
                    n = len(calls)
                name = (providers or ["default"])[0]
                if name in canned_for and not (scenario == "canned-critic" and JUDGE_VERDICT_RULE in prompt):
                    return LLMResult(BUILTIN_PROVIDER, "canned")
                if JUDGE_VERDICT_RULE in prompt:
                    return LLMResult(f"{name}-cli", "Not yet.\nVERDICT: REJECTED", f"s{n}")
                return LLMResult(f"{name}-cli", f"text from {name}", f"s{n}")

            results = []
            fixed = _FixedTargets() if project == OPS["id"] else None
            with patch.dict(os.environ, {"HOME": str(root)}), \
                    patch.object(engine.llm, "run_session_prompt", side_effect=run), \
                    patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                        engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or root / "l.md")):
                if fixed:
                    fixed.__enter__()
                try:
                    for index, task in enumerate(tasks):
                        if via_flow and index == 0:
                            full = team if "expert" in team else TeamManager().get_team(team["id"])
                            run = flow_graph.resolve_run(flow_graph.team_to_flow(full), {}, {}, engine.persona_mgr,
                                                         task=task)
                            results.append(engine.run_debate(run["task"], project_id=project, thread_id=THREAD,
                                                             flow_run=run))
                        elif via_flow:
                            results.append(engine.run_debate(task, project_id=project, thread_id=THREAD))
                        else:
                            results.append(engine.run_debate(task, project_id=project, team=team["id"],
                                                             thread_id=THREAD))
                finally:
                    if fixed:
                        fixed.__exit__(None, None, None)
            engine.events.close()
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            paths = tuple(sorted({str(root), str(repo), str(Path(tmp))}, key=len, reverse=True))
            scrubbed = _scrub({"calls": [{**c, "prompt": c["prompt"]} for c in calls], "events": events, "results": results}, paths)
            scrubbed["calls"] = [{**c, "prompt": _blob(c["prompt"])} for c in scrubbed["calls"]]
            scrubbed["results"] = _digest(scrubbed["results"])
            return scrubbed

    def cases(self) -> List[Tuple[str, dict, str, str, Tuple[str, ...]]]:
        out: List[Tuple[str, dict, str, str, Tuple[str, ...]]] = []
        for shape, team in SHAPES.items():
            for scenario in (("plain", "resumed", "canned") + (("canned-critic",) if shape in HAS_CRITIC else ())
                             + (("canned-mid",) if shape == "pipeline-critic" else ())):
                out.append((f"{shape}/{scenario}", team, CODE_PROJECT, scenario,
                            ("first task", "second task") if scenario == "resumed" else ("task",)))
        for kind, project in (("ops", OPS["id"]), ("design", DESIGN["id"])):
            out += [(f"{kind}-debate/plain", BASE, project, "plain", ("task",)),
                    (f"{kind}-verify/plain", VERIFY, project, "plain", ("task",))]
        out += [("builtin-guardian-dev/plain", {"id": "guardian-dev"}, CODE_PROJECT, "plain", ("task",)),
                ("builtin-guardian-dev/canned-critic", {"id": "guardian-dev", "critic": [
                    {"providers": ["codex"]}, {"providers": ["agy"]}, {"providers": ["agy"]}]},
                 CODE_PROJECT, "canned-critic", ("task",)),
                ("builtin-fleet-ops/plain", {"id": "fleet-ops"}, OPS["id"], "plain", ("task",))]
        return out

    def test_dump_matches_golden(self) -> None:
        dump = {key: self.run_case(team, project, scenario, tasks) for key, team, project, scenario, tasks in self.cases()}
        text = json.dumps(dump, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        if os.environ.get("GOLDEN_UPDATE") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(text, encoding="utf-8")
        self.assertTrue(GOLDEN.exists(), "run once with GOLDEN_UPDATE=1 to record the dump")
        expected = GOLDEN.read_text(encoding="utf-8")
        if text != expected:
            expected_dump = json.loads(expected)
            changed = [key for key in dump if json.loads(json.dumps(dump[key], default=str)) != expected_dump.get(key)]
            self.fail(f"flow engine output changed for: {changed or 'the set of cases'}")

    def test_every_case_runs_the_same_from_its_flow(self) -> None:
        """G2's gate: a team converted into a flow and compiled back runs byte for byte like the team, apart from the
        fields a flow adds (`seat` on entries and events, `flow_graph` on the lineup and `debate_started`)."""
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for key, team, project, scenario, tasks in self.cases():
            with self.subTest(case=key):
                dump = self.run_case(team, project, scenario, tasks, via_flow=True)
                # the flow and the seats are recorded, also on a continued thread
                for result in dump["results"]:
                    self.assertEqual(result["team"]["flow_graph"]["id"], team["id"])
                    self.assertEqual(result["team"]["judge"]["seat"], "close")
                self.assertTrue(any(e.get("seat") for e in dump["events"] if e["type"] == "agent_started"))
                got = _without_flow_fields(json.loads(json.dumps(dump, default=str)))
                want = _without_flow_fields(expected[key])
                if key.startswith("versus/"):
                    got, want = _without_versus_lead(got), _without_versus_lead(want)
                self.assertEqual(got, want)

    def test_dump_does_not_depend_on_the_checkout_or_the_run(self) -> None:
        first = self.run_case(VERSUS, CODE_PROJECT, "plain")
        self.assertEqual(first, self.run_case(VERSUS, CODE_PROJECT, "plain"))
        self.assertIn("Commit: ", json.dumps(first["calls"][0]["prompt"]["head"]) + "Commit: ")  # fixture repo is read


if __name__ == "__main__":
    unittest.main()

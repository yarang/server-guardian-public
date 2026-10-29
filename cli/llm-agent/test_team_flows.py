"""Team flows (docs/design/team-flows.md): the `flow` field of a team file and the panel, verify, pipeline and
versus flows (F1-F3)."""

import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional
from unittest.mock import patch

from debate_engine import JUDGE_VERDICT_RULE, VERIFY_TASK, DebateEngine, versus_order
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from project_registry import ProjectManager
from team_manager import (
    EXPERT_ROLE,
    FLOW_KINDS,
    TeamManager,
    TeamValidationError,
    parse_lineup,
    validate_lineup,
)
from test_ops_project import OPS, _FixedTargets
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


BASE = {
    "id": "flowteam",
    "name": "Flow team",
    "expert": {"persona": "domain_architect", "providers": ["claude"]},
    "critic": [{"persona": "critic_redteam", "providers": ["codex"]}],
    "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]},
    "max_rounds": 2,
    "allowed_tools": [],
}
PANEL = {
    "id": "panelteam", "name": "Panel team", "flow": {"kind": "panel"},
    "expert": {"persona": "domain_architect", "providers": ["claude"]},
    "proposers": [{"persona": "devops_specialist", "providers": ["codex"]},
                  {"persona": "ops_sre", "providers": ["agy"]}],
    "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]},
    "allowed_tools": [],
}
VERIFY = {**{k: v for k, v in BASE.items() if k != "max_rounds"}, "id": "verifyteam", "name": "Verify team", "flow": {"kind": "verify"}}
PIPELINE = {
    "id": "pipelineteam", "name": "Pipeline team",
    "flow": {"kind": "pipeline", "stages": [{"persona": "devops_specialist", "providers": ["codex"]},
                                           {"persona": "ops_sre", "providers": ["agy"]},
                                           {"persona": "domain_architect", "providers": ["claude"]}]},
    "expert": {"persona": "domain_architect", "providers": ["claude"]},
    "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]},
    "allowed_tools": [],
}
VERSUS = {
    "id": "versusteam", "name": "Versus team",
    "flow": {"kind": "versus", "exchanges": 1,
             "sides": [{"persona": "ops_sre", "providers": ["claude"], "stance": "Keep the single hub."},
                       {"persona": "domain_architect", "providers": ["codex"], "stance": "Split into two hubs."}]},
    "expert": {"persona": "domain_architect", "providers": ["claude"]},
    "judge": {"persona": "jev_mcp_decision", "providers": ["agy"]},
    "allowed_tools": [],
}
DESIGN = {"id": "arch-docs", "kind": "design", "name": "Architecture docs"}


def lineup_problems(team: dict) -> str:
    try:
        validate_lineup(parse_lineup(team))
    except TeamValidationError as exc:
        return str(exc)
    return ""


class FlowSchemaTests(unittest.TestCase):
    def test_no_flow_is_a_debate_with_an_unchanged_snapshot(self) -> None:
        lineup = parse_lineup(BASE)
        self.assertEqual(lineup.flow_kind, "debate")
        self.assertNotIn("flow", lineup.snapshot())
        self.assertIn("debate", FLOW_KINDS)

    def test_panel_needs_proposers_and_no_critic(self) -> None:
        self.assertEqual(lineup_problems(PANEL), "")
        self.assertEqual(parse_lineup(PANEL).critics, [])  # `critic` is not required for a panel
        self.assertIn("critic", lineup_problems({**PANEL, "critic": BASE["critic"]}))
        self.assertIn("proposers", lineup_problems({**PANEL, "proposers": []}))
        self.assertIn("max_rounds", lineup_problems({**PANEL, "max_rounds": 2}))
        self.assertEqual(lineup_problems({**PANEL, "max_rounds": 1}), "")

    def test_verify_needs_a_verifier_and_no_proposers(self) -> None:
        self.assertEqual(lineup_problems(VERIFY), "")
        self.assertIn("proposers", lineup_problems({**VERIFY, "proposers": PANEL["proposers"]}))
        with self.assertRaisesRegex(TeamValidationError, "critic: missing"):
            parse_lineup({k: v for k, v in VERIFY.items() if k != "critic"})

    def test_unknown_kind_and_stray_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(TeamValidationError, "unknown flow 'tournament'"):
            parse_lineup({**BASE, "flow": {"kind": "tournament"}})
        with self.assertRaisesRegex(TeamValidationError, "unknown field"):
            parse_lineup({**BASE, "flow": {"kind": "verify", "stages": []}})
        with self.assertRaisesRegex(TeamValidationError, "flow"):
            parse_lineup({**BASE, "flow": "panel"})

    def test_pipeline_needs_two_to_four_stages_and_no_proposers(self) -> None:
        stages = PIPELINE["flow"]["stages"]
        self.assertEqual(lineup_problems(PIPELINE), "")
        self.assertEqual(parse_lineup(PIPELINE).critics, [])  # the critic is optional
        self.assertEqual(lineup_problems({**PIPELINE, "critic": BASE["critic"], "max_rounds": 2}), "")
        self.assertIn("2-4 stages", lineup_problems({**PIPELINE, "flow": {"kind": "pipeline", "stages": stages[:1]}}))
        self.assertIn("2-4 stages", lineup_problems(
            {**PIPELINE, "flow": {"kind": "pipeline", "stages": stages + stages[:2]}}))
        self.assertIn("proposers", lineup_problems({**PIPELINE, "proposers": PANEL["proposers"]}))
        self.assertIn("max_rounds", lineup_problems({**PIPELINE, "max_rounds": 2}))  # no critic: nothing to revise
        with self.assertRaisesRegex(TeamValidationError, "flow.stages"):
            parse_lineup({**PIPELINE, "flow": {"kind": "pipeline"}})
        with self.assertRaisesRegex(TeamValidationError, "flow.stages\\[1\\]"):
            parse_lineup({**PIPELINE, "flow": {"kind": "pipeline", "stages": [stages[0], 7]}})

    def test_stages_belong_to_the_pipeline_only(self) -> None:
        with self.assertRaisesRegex(TeamValidationError, "unknown field"):
            parse_lineup({**PANEL, "flow": {"kind": "panel", "stages": PIPELINE["flow"]["stages"]}})

    def test_stage_members_are_checked_like_the_others(self) -> None:
        bad = {**PIPELINE, "flow": {"kind": "pipeline", "stages": [
            {"persona": "devops_specialist", "providers": ["no_such_provider"]},
            {"persona": "no_such_persona"}]}}
        problems = lineup_problems(bad)
        self.assertIn("flow.stages[0]", problems)
        self.assertIn("flow.stages[1]", problems)

    def test_versus_needs_two_sides_with_a_stance_and_no_critic_or_proposers(self) -> None:
        sides = VERSUS["flow"]["sides"]

        def versus(**flow) -> dict:
            return {**VERSUS, "flow": {**VERSUS["flow"], **flow}}

        self.assertEqual(lineup_problems(VERSUS), "")
        self.assertEqual(parse_lineup(VERSUS).critics, [])  # the critic seat is not used
        self.assertEqual(parse_lineup(VERSUS).exchanges, 1)
        self.assertEqual(lineup_problems(versus(exchanges=2)), "")
        self.assertIn("exactly 2 sides", lineup_problems(versus(sides=sides[:1])))
        self.assertIn("exactly 2 sides", lineup_problems(versus(sides=sides + sides[:1])))
        self.assertIn("critic", lineup_problems({**VERSUS, "critic": BASE["critic"]}))
        self.assertIn("proposers", lineup_problems({**VERSUS, "proposers": PANEL["proposers"]}))
        self.assertIn("max_rounds", lineup_problems({**VERSUS, "max_rounds": 2}))
        self.assertEqual(lineup_problems({**VERSUS, "max_rounds": 1}), "")
        for bad in (0, 3, True, "1", 1.5):
            with self.subTest(exchanges=bad), self.assertRaisesRegex(TeamValidationError, "flow.exchanges"):
                parse_lineup({**VERSUS, "flow": {**VERSUS["flow"], "exchanges": bad}})
        with self.assertRaisesRegex(TeamValidationError, "flow.sides"):
            parse_lineup({**VERSUS, "flow": {"kind": "versus"}})
        with self.assertRaisesRegex(TeamValidationError, "flow.sides\\[1\\]"):
            parse_lineup(versus(sides=[sides[0], 7]))

    def test_a_stance_is_required_and_short(self) -> None:
        sides = VERSUS["flow"]["sides"]
        for stance in (None, "", "   ", 7, "x" * 401):
            with self.subTest(stance=stance):
                side = {k: v for k, v in sides[1].items() if k != "stance"}
                if stance is not None:
                    side["stance"] = stance
                with self.assertRaisesRegex(TeamValidationError, "flow.sides\\[1\\].stance"):
                    parse_lineup({**VERSUS, "flow": {**VERSUS["flow"], "sides": [sides[0], side]}})
        long_enough = {**sides[1], "stance": "x" * 400}
        self.assertEqual(lineup_problems({**VERSUS, "flow": {**VERSUS["flow"], "sides": [sides[0], long_enough]}}), "")

    def test_sides_and_exchanges_belong_to_versus_only(self) -> None:
        for kind in ("panel", "verify", "pipeline"):
            for field in ("sides", "exchanges"):
                with self.subTest(kind=kind, field=field), self.assertRaisesRegex(TeamValidationError, "unknown field"):
                    parse_lineup({**PANEL, "flow": {"kind": kind, field: VERSUS["flow"][field]}})
        with self.assertRaisesRegex(TeamValidationError, "unknown field"):
            parse_lineup({**VERSUS, "flow": {**VERSUS["flow"], "stages": PIPELINE["flow"]["stages"]}})

    def test_side_members_are_checked_like_the_others(self) -> None:
        sides = VERSUS["flow"]["sides"]
        bad = [{**sides[0], "providers": ["no_such_provider"]}, {**sides[1], "persona": "no_such_persona"}]
        problems = lineup_problems({**VERSUS, "flow": {**VERSUS["flow"], "sides": bad}})
        self.assertIn("flow.sides[0]", problems)
        self.assertIn("flow.sides[1]", problems)

    def test_one_persona_may_argue_both_sides_but_the_same_provider_draws_a_warning(self) -> None:
        sides = VERSUS["flow"]["sides"]
        same_persona = [sides[0], {**sides[1], "persona": sides[0]["persona"]}]
        self.assertEqual(lineup_problems({**VERSUS, "flow": {**VERSUS["flow"], "sides": same_persona}}), "")
        warnings = validate_lineup(parse_lineup(VERSUS))
        self.assertFalse([w for w in warnings if "flow.sides" in w])
        same_provider = [sides[0], {**sides[1], "providers": ["claude"]}]
        warnings = validate_lineup(parse_lineup({**VERSUS, "flow": {**VERSUS["flow"], "sides": same_provider}}))
        self.assertTrue([w for w in warnings if "flow.sides" in w])

    def test_snapshot_round_trips(self) -> None:
        for team in (PANEL, VERIFY, PIPELINE, {**PIPELINE, "critic": BASE["critic"]}, VERSUS,
                     {**VERSUS, "flow": {**VERSUS["flow"], "exchanges": 2}}):
            lineup = parse_lineup(team)
            snapshot = lineup.snapshot()
            self.assertEqual(snapshot["flow"]["kind"], lineup.flow_kind)
            self.assertEqual(len(snapshot["flow"].get("stages", [])), len(lineup.stages))
            self.assertEqual(len(snapshot["flow"].get("sides", [])), len(lineup.sides))
            if lineup.sides:
                self.assertEqual([s["stance"] for s in snapshot["flow"]["sides"]], [s.stance for s in lineup.sides])
                self.assertEqual(snapshot["flow"]["exchanges"], lineup.exchanges)
            again = parse_lineup(json.loads(json.dumps(snapshot)))
            self.assertEqual(again.snapshot(), snapshot)
            self.assertEqual(validate_lineup(again), validate_lineup(lineup))


class FlowRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls: list = []
        self.lock = threading.Lock()

    def engine(self, *teams: dict, project: Optional[dict] = None) -> DebateEngine:
        manager = TeamManager(self.root / "teams")
        for team in teams:
            manager.save(team)
        engine = DebateEngine(db_path=self.root / "d.db", events=EventWriter(str(self.root / "events.jsonl")),
                              team_mgr=manager)
        if project:
            (self.root / "builtin").mkdir(exist_ok=True)
            projects = ProjectManager(directory=self.root / "custom", builtin_dir=self.root / "builtin")
            projects.directory.mkdir(exist_ok=True)
            (projects.directory / f"{project['id']}.json").write_text(json.dumps(project))
            engine.project_mgr = projects
        return engine

    def fake(self, builtin_for=None, canned_if=None):
        def run(prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
            prompt = prompt + (instructions.text() if instructions else "")
            with self.lock:
                self.calls.append({"prompt": prompt, "providers": providers})
                n = len(self.calls)
            name = providers[0]
            if name == builtin_for or (canned_if and canned_if(name, prompt)):
                return LLMResult(BUILTIN_PROVIDER, "canned")
            if JUDGE_VERDICT_RULE in prompt:
                return LLMResult(f"{name}-cli", "Not yet.\nVERDICT: REJECTED", f"s{n}")
            return LLMResult(f"{name}-cli", f"text from {name}", f"s{n}")
        return run

    def debate(self, engine: DebateEngine, team: str, project_id: str = "server-guardian", builtin_for=None,
               canned_if=None, **kw):
        with patch.object(engine.llm, "run_session_prompt", side_effect=self.fake(builtin_for, canned_if)), \
                patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            result = engine.run_debate("task", project_id=project_id, team=team, **kw)
        engine.events.close()
        return result

    def events(self) -> list:
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def test_panel_answers_blind_without_critique_in_one_round(self) -> None:
        engine = self.engine(PANEL)
        result = self.debate(engine, "panelteam")

        self.assertEqual(len(self.calls), 4)  # three panelists and the Judge; nobody critiques
        self.assertEqual([t["role"] for t in result["transcript"]].count(EXPERT_ROLE), 3)
        self.assertNotIn("Critic Agent", [t["role"] for t in result["transcript"]])
        panelist = next(c for c in self.calls if c["providers"] == ["codex"])["prompt"]
        self.assertNotIn("Previous Critique", panelist)
        self.assertIn("you do not see their answers", panelist)
        judge = next(c for c in self.calls if JUDGE_VERDICT_RULE in c["prompt"])["prompt"]
        self.assertIn("### Proposal A", judge)
        self.assertIn("### Proposal C", judge)
        self.assertNotIn("Critique:", judge)
        self.assertNotIn("DevOps", judge)  # the authors' names stay out of the Judge's prompt
        started = next(e for e in self.events() if e["type"] == "debate_started")
        self.assertEqual(started["data"]["flow"], "panel")

    def test_a_canned_answer_makes_a_panel_unverified(self) -> None:
        engine = self.engine(PANEL)
        result = self.debate(engine, "panelteam", builtin_for="agy")
        verdict = next(e for e in self.events() if e["type"] == "verdict")
        self.assertTrue(verdict["data"]["unverified"])
        self.assertEqual(len(self.calls), 4)  # the canned answer ends the run after one round
        self.assertTrue(result["transcript"])

    def test_verify_asks_the_critic_to_check_not_to_argue(self) -> None:
        engine = self.engine(VERIFY)
        self.debate(engine, "verifyteam")
        critic = next(c for c in self.calls if c["providers"] == ["codex"])["prompt"]
        self.assertIn(VERIFY_TASK.split("{proposal}")[1].split(".")[0], critic)
        self.assertNotIn("Critique this proposal rigorously", critic)
        started = next(e for e in self.events() if e["type"] == "debate_started")
        self.assertEqual(started["data"]["flow"], "verify")
        self.assertEqual(started["data"].get("max_rounds"), 1)  # the default of a new flow

    def test_verify_rounds_follow_max_rounds(self) -> None:
        engine = self.engine({**VERIFY, "max_rounds": 2})
        result = self.debate(engine, "verifyteam")
        self.assertEqual(sum(1 for t in result["transcript"] if t["role"] == "JEV-MCP Judge"), 2)

    def test_a_debate_team_leaves_no_flow_in_its_events(self) -> None:
        engine = self.engine(BASE)
        self.debate(engine, "flowteam")
        started = next(e for e in self.events() if e["type"] == "debate_started")
        self.assertNotIn("flow", started["data"])

    def test_ops_and_design_projects_refuse_a_panel_before_any_call(self) -> None:
        for project in (OPS, DESIGN):
            for team, name in ((PANEL, "panel"), (PIPELINE, "pipeline"), (VERSUS, "versus")):
                with self.subTest(kind=project["kind"], flow=name):
                    self.calls.clear()
                    engine = self.engine(team, project=project)
                    with _FixedTargets(), self.assertRaisesRegex(TeamValidationError, f"{name} flow"):
                        self.debate(engine, team["id"], project_id=project["id"])
                    self.assertEqual(self.calls, [])

    def test_pipeline_stages_run_in_order_and_each_reads_the_one_before(self) -> None:
        engine = self.engine(PIPELINE)
        result = self.debate(engine, "pipelineteam")

        self.assertEqual(len(self.calls), 4)  # three stages and the Judge
        stage_calls = [c["prompt"] for c in self.calls[:3]]
        self.assertIn("You are stage 1 of 3", stage_calls[0])
        self.assertNotIn("Work of stage", stage_calls[0])
        self.assertIn("text from codex", stage_calls[1])  # the second stage was handed the first one's result
        self.assertIn("Work of stage 1", stage_calls[1])
        self.assertIn("Work of stage 2", stage_calls[2])
        self.assertIn("text from agy", stage_calls[2])
        judge = self.calls[3]["prompt"]
        self.assertIn("Proposal:\ntext from claude", judge)
        self.assertNotIn("Critique:", judge)  # no critic seat: nothing to show
        experts = [t for t in result["transcript"] if t["role"] == EXPERT_ROLE]
        self.assertEqual([t["stage"] for t in experts], [0, 1, 2])
        self.assertEqual(len([t for t in result["transcript"] if t["role"] == "JEV-MCP Judge"]), 1)

    def test_pipeline_events_carry_the_stage_and_each_stage_keeps_its_session(self) -> None:
        engine = self.engine(PIPELINE)
        self.debate(engine, "pipelineteam")
        started = [e for e in self.events() if e["type"] == "agent_started" and e.get("role") == EXPERT_ROLE]
        self.assertEqual([e["stage"] for e in started], [0, 1, 2])
        self.assertEqual([e["agent"] for e in started], [f"{EXPERT_ROLE}:stage{i}" for i in range(3)])
        judge = next(e for e in self.events() if e["type"] == "agent_started" and e.get("role") == "JEV-MCP Judge")
        self.assertNotIn("stage", judge)
        flow = next(e for e in self.events() if e["type"] == "debate_started")["data"]["flow"]
        self.assertEqual(flow, "pipeline")

    def test_a_canned_stage_ends_the_chain_and_makes_the_run_unverified(self) -> None:
        engine = self.engine({**PIPELINE, "critic": BASE["critic"]})
        result = self.debate(engine, "pipelineteam", builtin_for="agy")  # the second stage
        verdict = next(e for e in self.events() if e["type"] == "verdict")
        self.assertTrue(verdict["data"]["unverified"])
        self.assertEqual(len(self.calls), 3)  # stage 1, the canned stage 2, the Judge: no stage 3, no critic
        self.assertEqual([t.get("stage") for t in result["transcript"] if t["role"] == EXPERT_ROLE], [0, 1])

    def test_a_pipeline_with_a_critic_hands_it_the_last_result(self) -> None:
        engine = self.engine({**PIPELINE, "critic": BASE["critic"]})
        result = self.debate(engine, "pipelineteam")
        critic = next(c for c in self.calls if c["providers"] == ["codex"] and "Critique this proposal" in c["prompt"])
        self.assertIn("text from claude", critic["prompt"])
        self.assertIn("Critique:", self.calls[-1]["prompt"])
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(result["transcript"][3]["role"], "Critic Agent")

    def test_later_rounds_let_the_lead_rewrite_against_the_critique(self) -> None:
        engine = self.engine({**PIPELINE, "critic": BASE["critic"], "max_rounds": 2})
        result = self.debate(engine, "pipelineteam")
        round2 = [t for t in result["transcript"] if t.get("round") == 2 and t["role"] == EXPERT_ROLE]
        self.assertEqual(len(round2), 1)
        self.assertNotIn("stage", round2[0])
        rewrite = next(c for c in self.calls if "Proposal of the earlier round" in c["prompt"])["prompt"]
        self.assertIn("text from claude", rewrite)  # the previous round's proposal, carried over
        self.assertEqual(len(self.calls), 5 + 3)  # round 1: 3 stages, critic, Judge; round 2: lead, critic, Judge

    def test_versus_sides_open_blind_then_answer_each_other_once(self) -> None:
        engine = self.engine(VERSUS)
        result = self.debate(engine, "versusteam")

        self.assertEqual(len(self.calls), 5)  # two openings, two answers, the Judge; no critic, no lead
        by_provider = lambda phase: {c["providers"][0]: c["prompt"] for c in phase}  # noqa: E731
        openings, answers = by_provider(self.calls[:2]), by_provider(self.calls[2:4])
        self.assertIn("Keep the single hub.", openings["claude"])
        self.assertIn("Split into two hubs.", openings["codex"])
        for prompt in openings.values():
            self.assertNotIn("text from", prompt)  # blind: nobody has seen the other case yet
            self.assertNotIn("Previous Critique", prompt)
        self.assertIn("text from codex", answers["claude"])  # each side is handed the other's case...
        self.assertIn("text from claude", answers["codex"])
        self.assertIn("Keep the single hub.", answers["claude"])  # ...and keeps its own stance in view
        self.assertIn("Split into two hubs.", answers["codex"])
        self.assertIn("Answer it once", answers["claude"])
        experts = [t for t in result["transcript"] if t["role"] == EXPERT_ROLE]
        self.assertEqual([(t["side"], t["exchange"]) for t in experts], [("A", 0), ("B", 0), ("A", 1), ("B", 1)])
        self.assertNotIn("Critic Agent", [t["role"] for t in result["transcript"]])
        self.assertEqual(len([t for t in result["transcript"] if t["role"] == "JEV-MCP Judge"]), 1)

    def test_two_exchanges_pass_the_latest_answers_back_and_forth(self) -> None:
        engine = self.engine({**VERSUS, "flow": {**VERSUS["flow"], "exchanges": 2}})
        result = self.debate(engine, "versusteam")
        self.assertEqual(len(self.calls), 7)  # 2 + 2 + 2 + Judge
        experts = [t for t in result["transcript"] if t["role"] == EXPERT_ROLE]
        self.assertEqual([t["exchange"] for t in experts], [0, 0, 1, 1, 2, 2])
        self.assertIn("Answer it once", self.calls[4]["prompt"])

    def test_the_judge_sees_neutral_positions_with_their_stances_in_a_rotated_order(self) -> None:
        names = [engine_name for engine_name in ("Fleet Operations Engineer", "Domain Architect")]
        orders = {}
        for index in range(40):  # the order is a function of the thread, so look for one thread of each order
            orders.setdefault(tuple(versus_order(f"t{index}", 1)), f"t{index}")
        self.assertEqual(set(orders), {(0, 1), (1, 0)})
        self.assertEqual(versus_order("t1", 1), versus_order("t1", 1))
        self.assertNotEqual(versus_order("t1", 1), versus_order("t1", 2))  # a later round swaps the positions
        for order, thread in orders.items():
            with self.subTest(order=order):
                self.calls.clear()
                engine = self.engine(VERSUS)
                self.debate(engine, "versusteam", thread_id=thread)
                judge = next(c for c in self.calls if JUDGE_VERDICT_RULE in c["prompt"])["prompt"]
                stances = ["Keep the single hub.", "Split into two hubs."]
                first, second = (stances[i] for i in order)
                self.assertIn("Position A", judge)
                self.assertIn("Position B", judge)
                self.assertLess(judge.index(first), judge.index(second))
                self.assertLess(judge.index("Position A"), judge.index(first))
                self.assertLess(judge.index(first), judge.index("Position B"))
                for name in names:
                    self.assertNotIn(name, judge)  # the advocates' names stay out of the Judge's prompt
                self.assertNotIn("Critique:", judge)

    def test_versus_events_carry_the_side_and_each_side_keeps_its_session(self) -> None:
        engine = self.engine(VERSUS)
        self.debate(engine, "versusteam")
        started = [e for e in self.events() if e["type"] == "agent_started" and e.get("role") == EXPERT_ROLE]
        self.assertEqual(sorted((e["side"], e["agent"]) for e in started),
                         [("A", f"{EXPERT_ROLE}:side0")] * 2 + [("B", f"{EXPERT_ROLE}:side1")] * 2)
        judge = next(e for e in self.events() if e["type"] == "agent_started" and e.get("role") == "JEV-MCP Judge")
        self.assertNotIn("side", judge)
        self.assertEqual(next(e for e in self.events() if e["type"] == "debate_started")["data"]["flow"], "versus")

    def test_one_persona_on_both_sides_still_gets_two_sessions(self) -> None:
        sides = VERSUS["flow"]["sides"]
        team = {**VERSUS, "flow": {**VERSUS["flow"], "sides": [sides[0], {**sides[1], "persona": sides[0]["persona"]}]}}
        self.debate(self.engine(team), "versusteam")
        agents = {e["agent"] for e in self.events() if e["type"] == "agent_started" and e.get("role") == EXPERT_ROLE}
        self.assertEqual(agents, {f"{EXPERT_ROLE}:side0", f"{EXPERT_ROLE}:side1"})

    def test_a_canned_opening_skips_the_exchange_and_makes_the_run_unverified(self) -> None:
        engine = self.engine(VERSUS)
        result = self.debate(engine, "versusteam", builtin_for="codex")
        verdict = next(e for e in self.events() if e["type"] == "verdict")
        self.assertTrue(verdict["data"]["unverified"])
        self.assertEqual(len(self.calls), 3)  # two openings (one canned) and the Judge: nobody answers
        self.assertEqual([(t["side"], t["exchange"]) for t in result["transcript"] if t["role"] == EXPERT_ROLE],
                         [("A", 0), ("B", 0)])

    def test_a_canned_answer_makes_the_run_unverified_and_stops_before_a_second_exchange(self) -> None:
        engine = self.engine({**VERSUS, "flow": {**VERSUS["flow"], "exchanges": 2}})
        self.debate(engine, "versusteam", canned_if=lambda name, prompt: name == "codex" and "Answer it once" in prompt)
        self.assertTrue(next(e for e in self.events() if e["type"] == "verdict")["data"]["unverified"])
        self.assertEqual(len(self.calls), 5)  # 2 openings, 2 answers (one canned), the Judge: no second exchange

    def test_ops_and_design_projects_run_verify(self) -> None:
        for project in (OPS, DESIGN):
            with self.subTest(kind=project["kind"]):
                self.calls.clear()
                engine = self.engine(VERIFY, project=project)
                with _FixedTargets():
                    result = self.debate(engine, "verifyteam", project_id=project["id"])
                self.assertTrue(result["transcript"])
                self.assertTrue(self.calls)


if __name__ == "__main__":
    unittest.main()

"""Flow templates, phase T3a (docs/design/flow-templates.md): custom steps. The grammar, the translation into the engine's
step lists (parity with the five flow kinds), and runs under a fake LLM: the seats, the round limit, the snapshot and the
continuation of a thread. The golden dump (test_flow_golden) stays the gate for the flow kinds themselves."""

import json
import os
import threading
import types
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional
from unittest.mock import patch

import flow_templates as ft
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine
from debate_events import EventWriter
from llm_runner import LLMResult
from team_manager import TeamManager, TeamValidationError, parse_lineup
from test_team_flows import BASE, PANEL, VERIFY, VERSUS
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


PROPOSER_TEAM = {**BASE, "id": "proposerteam", "name": "Proposer team",
                 "proposers": [{"persona": "devops_specialist", "providers": ["agy"]}]}


def template(steps, **changes) -> dict:
    data = {
        "id": "custom-flow", "name": "Custom flow", "category": "development", "projects": ["code"],
        "team": "flowteam", "steps": steps,
        "params": {"TOPIC": {"type": "string", "label": "Topic", "max": 100}},
        "task": "Look at ${TOPIC}", "reviewed": date.today().isoformat(),
    }
    data.update(changes)
    return data


DRAFT_CRITIQUE = [{"kind": "draft"}, {"kind": "critique"}]
DRAFT_ONLY = [{"kind": "draft"}]
BLIND_CHECKLIST = [{"kind": "blind_draft"}, {"kind": "critique", "mode": "checklist"}]


class GrammarTests(unittest.TestCase):
    def refused(self, steps, text: str, **changes) -> None:
        with self.assertRaises(ft.TemplateValidationError) as caught:
            ft.validate_template(template(steps, **changes))
        self.assertIn(text, str(caught.exception))

    def test_the_accepted_shapes_are_custom_steps(self) -> None:
        for steps in (DRAFT_ONLY, DRAFT_CRITIQUE, BLIND_CHECKLIST, [{"kind": "blind_draft"}]):
            with self.subTest(steps=steps):
                self.assertEqual(ft.validate_template(template(steps)), [])
                self.assertEqual(ft.template_kind(template(steps)), ft.CUSTOM_KIND)

    def test_the_alias_form_is_unchanged(self) -> None:
        self.assertEqual(ft.template_kind(template([{"kind": "versus"}], stances={})), "versus")
        self.refused([{"kind": "versus"}, {"kind": "versus"}], "steps[0]")  # a flow kind is not a custom step
        self.refused([{"kind": "debate", "mode": "object"}], "steps[0]")

    def test_a_critique_needs_a_proposal_before_it(self) -> None:
        self.refused([{"kind": "critique"}, {"kind": "draft"}], "needs a proposal before it")
        self.refused([{"kind": "critique"}], "needs a proposal before it")

    def test_only_a_critique_may_follow_the_proposal_step(self) -> None:
        self.refused([{"kind": "draft"}, {"kind": "draft"}], "only a critique may follow")
        self.refused([{"kind": "blind_draft"}, {"kind": "blind_draft"}], "only a critique may follow")
        self.refused([{"kind": "draft"}, {"kind": "critique"}, {"kind": "critique"}], "at most 2 steps")

    def test_seats_are_implied_and_unknown_keys_or_kinds_are_refused(self) -> None:
        self.refused([{"kind": "draft", "seats": ["expert"]}], "steps[0].seats")
        self.refused([{"kind": "draft"}, {"kind": "critique", "seats": ["critic:1"]}], "steps[1].seats")
        self.refused([{"kind": "draft", "mode": "object"}], "steps[0].mode")
        self.refused([{"kind": "draft"}, {"kind": "critique", "mode": "loud"}], "steps[1].mode")
        self.refused([{"kind": "draft"}, {"kind": "rewrite"}], "unknown step kind")
        self.refused([{"kind": "exchange"}], "unknown step kind")
        self.refused([{"kind": 3}], "steps[0]")

    def test_custom_steps_run_on_code_projects_only(self) -> None:
        self.refused(DRAFT_CRITIQUE, "code projects only", projects=["code", "ops"])
        self.refused(DRAFT_CRITIQUE, "code projects only", projects=["design"])

    def test_custom_steps_have_no_sides_to_state_stances_for(self) -> None:
        self.refused(DRAFT_CRITIQUE, "only a versus flow has sides", stances={"side:A": "x", "side:B": "y"})

    def test_the_catalog_lists_the_custom_kinds_and_the_step_limit(self) -> None:
        catalog = ft.catalog()
        self.assertEqual(catalog["custom_step_kinds"], ["draft", "blind_draft", "critique"])
        self.assertEqual(catalog["critique_modes"], ["object", "checklist"])
        self.assertEqual(catalog["limits"]["steps"], 2)


class ParityTests(unittest.TestCase):
    """The translator writes the lists the engine builds for the flow kinds, so the pieces behind them are the ones the
    golden dump already pins."""

    @staticmethod
    def native(team: dict, round_num: int = 1, start_round: int = 1) -> list:
        lineup = parse_lineup(team)
        ctx = types.SimpleNamespace(lineup=lineup, start_round=start_round, stages=[],
                                    critics=[object()] if lineup.critics else [], proposers=list(lineup.proposers))
        return DebateEngine._steps_of(ctx, round_num)

    def test_draft_and_critique_is_the_debate_without_proposers(self) -> None:
        for round_num in (1, 2):
            self.assertEqual(ft.engine_steps(DRAFT_CRITIQUE, round_num == 1), self.native(BASE, round_num))

    def test_blind_draft_and_critique_is_the_debate_with_proposers_then_consolidates(self) -> None:
        for round_num in (1, 2):
            self.assertEqual(ft.engine_steps([{"kind": "blind_draft"}, {"kind": "critique"}], round_num == 1),
                             self.native(PROPOSER_TEAM, round_num))

    def test_draft_and_checklist_critique_is_the_verify_flow(self) -> None:
        for round_num in (1, 2):
            self.assertEqual(
                ft.engine_steps([{"kind": "draft"}, {"kind": "critique", "mode": "checklist"}], round_num == 1),
                self.native(VERIFY, round_num))

    def test_blind_drafts_without_a_critique_are_the_panel(self) -> None:
        self.assertEqual(ft.engine_steps([{"kind": "blind_draft"}], True), self.native(PANEL))

    def test_a_resumed_thread_starts_with_the_first_shape_again(self) -> None:
        self.assertEqual(ft.engine_steps([{"kind": "blind_draft"}, {"kind": "critique"}], True),
                         self.native(PROPOSER_TEAM, round_num=3, start_round=3))


class TeamFitTests(unittest.TestCase):
    def lineup(self, team: dict):
        return parse_lineup(team)

    def test_blind_draft_needs_proposers_and_critique_needs_critics(self) -> None:
        with self.assertRaises(ft.TemplateValidationError) as caught:
            ft.check_team([{"kind": "blind_draft"}], self.lineup(BASE))
        self.assertIn("needs proposers", str(caught.exception))
        bare = self.lineup(BASE)
        bare.critics = []  # a debate or verify team always has a critic; the check guards a lineup built by hand
        with self.assertRaises(ft.TemplateValidationError) as caught:
            ft.check_team(DRAFT_CRITIQUE, bare)
        self.assertIn("needs critics", str(caught.exception))
        ft.check_team(BLIND_CHECKLIST, self.lineup(PROPOSER_TEAM))

    def test_teams_of_the_other_flow_kinds_are_refused(self) -> None:
        for team in (PANEL, VERSUS):
            with self.subTest(team=team["id"]), self.assertRaises(ft.TemplateValidationError) as caught:
                ft.check_team(DRAFT_ONLY, self.lineup(team))
            self.assertIn("debate or verify team", str(caught.exception))
        ft.check_team(DRAFT_CRITIQUE, self.lineup(VERIFY))


class CustomRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls: list = []
        self.lock = threading.Lock()
        manager = TeamManager(self.root / "teams")
        for team in (BASE, PROPOSER_TEAM, PANEL):
            manager.save(team)
        self.engine = DebateEngine(db_path=self.root / "d.db", events=EventWriter(str(self.root / "events.jsonl")),
                                   team_mgr=manager)
        self.runs = 0

    def fake(self, prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
        prompt = prompt + (instructions.text() if instructions else "")
        with self.lock:
            self.calls.append({"prompt": prompt, "providers": providers})
            n = len(self.calls)
        if JUDGE_VERDICT_RULE in prompt:
            return LLMResult("claude-cli", "Not yet.\nVERDICT: REJECTED", f"s{n}")
        return LLMResult(f"{providers[0]}-cli", f"text from {providers[0]}", f"s{n}")

    def run_debate(self, steps=None, team=None, thread_id=None, **changes):
        """One run under the fake LLM; with `steps` it starts from a template of those steps."""
        self.runs += 1
        self.engine.events = EventWriter(str(self.root / f"events{self.runs}.jsonl"))
        kw = {}
        task = "task"
        if steps is not None:
            run, _ = ft.resolve(template(steps, **changes), {"TOPIC": "the cache"}, team=team)
            kw, task, team = {"template_run": run}, run["task"], run["team"]
        with patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake), \
                patch.object(self.engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            try:
                return self.engine.run_debate(task, project_id="server-guardian", team=team, thread_id=thread_id, **kw)
            finally:
                self.engine.events.close()

    def events(self, run: Optional[int] = None) -> list:
        path = self.root / f"events{run or self.runs}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def critic_calls(self) -> int:
        return sum(1 for c in self.calls if c["providers"] == ["codex"] and JUDGE_VERDICT_RULE not in c["prompt"])

    def test_a_template_without_a_critique_runs_one_round_and_never_asks_the_critics(self) -> None:
        result = self.run_debate(DRAFT_ONLY)
        self.assertEqual(self.critic_calls(), 0)
        started = next(e for e in self.events() if e["type"] == "debate_started")["data"]
        self.assertEqual((started["max_rounds"], started["template"]), (1, "custom-flow"))
        self.assertEqual({e["round"] for e in self.events() if e["type"] == "agent_started"} - {None}, {1})
        self.assertEqual(result["team"]["template"]["steps"], DRAFT_ONLY)
        self.assertEqual(result["team"]["template"]["kind"], "steps")

    def test_draft_and_critique_run_every_round_the_judge_rejects(self) -> None:
        self.run_debate(DRAFT_CRITIQUE)
        self.assertEqual(self.critic_calls(), 2)  # one critic, two rounds (the team allows two)
        steps = {e.get("step") for e in self.events() if e["type"] == "agent_started"}
        self.assertEqual(steps - {None}, {0, 1})  # the lead is step 0, the critic step 1; the close has no step

    def test_blind_drafts_then_a_checklist_critique_then_a_consolidation(self) -> None:
        self.run_debate(BLIND_CHECKLIST, team="proposerteam")
        drafters = [c for c in self.calls if c["providers"] == ["agy"] and JUDGE_VERDICT_RULE not in c["prompt"]]
        self.assertEqual(len(drafters), 1)  # the proposer drafts in round 1 only; the lead consolidates after it
        self.assertEqual(sum("Independent drafts written in an earlier round" in c["prompt"] for c in self.calls), 1)
        self.assertEqual(self.critic_calls(), 2)
        self.assertTrue(any("fail" in c["prompt"].lower() and c["providers"] == ["codex"] for c in self.calls))

    def test_a_team_without_the_seats_is_refused_before_any_agent_runs(self) -> None:
        with self.assertRaises(TeamValidationError) as caught:
            self.run_debate([{"kind": "blind_draft"}], team="flowteam")
        self.assertIn("needs proposers", str(caught.exception))
        with self.assertRaises(TeamValidationError) as caught:
            self.run_debate(DRAFT_CRITIQUE, team="panelteam")
        self.assertIn("debate or verify team", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_plain_continuation_keeps_the_steps_of_the_thread(self) -> None:
        first = self.run_debate(DRAFT_ONLY)
        self.calls.clear()
        again = self.run_debate(thread_id=first["thread_id"])
        self.assertEqual(self.critic_calls(), 0)
        self.assertEqual(again["team"]["template"]["steps"], DRAFT_ONLY)
        self.assertEqual(again["team"]["template"]["id"], "custom-flow")

    def test_a_continuation_that_names_the_team_keeps_the_steps_too(self) -> None:
        first = self.run_debate(DRAFT_ONLY)
        self.calls.clear()
        again = self.run_debate(thread_id=first["thread_id"], team="flowteam")
        self.assertEqual(self.critic_calls(), 0)  # flowteam has a critic, the stored steps do not ask for it
        self.assertEqual(again["team"]["template"]["steps"], DRAFT_ONLY)

    def test_a_continuation_on_a_team_without_the_seats_is_refused(self) -> None:
        first = self.run_debate(BLIND_CHECKLIST, team="proposerteam")
        self.calls.clear()
        with self.assertRaises(TeamValidationError) as caught:
            self.run_debate(thread_id=first["thread_id"], team="flowteam")  # no proposers
        self.assertIn("cannot continue", str(caught.exception))
        self.assertIn("needs proposers", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_an_alias_template_is_kept_on_continuation_too(self) -> None:
        first = self.run_debate([{"kind": "debate"}], team="flowteam", stances={})
        self.assertNotIn("steps", first["team"]["template"])
        again = self.run_debate(thread_id=first["thread_id"])
        self.assertEqual(again["team"]["template"]["id"], "custom-flow")

    def test_a_run_without_a_template_is_not_changed(self) -> None:
        result = self.run_debate(team="flowteam")
        self.assertNotIn("template", result["team"])
        self.assertEqual(self.critic_calls(), 2)


if __name__ == "__main__":
    unittest.main()

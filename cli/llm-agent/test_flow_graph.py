"""Flow graphs, phase G1 (docs/design/flow-graph.md): the flow file and its validation, the conversion of teams and
templates into flows, the store and the JSON commands the hub API will relay."""

import copy
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config_ops
import debate_engine as de
import flow_graph as fg
import flow_templates as ft
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from persona_manager import PersonaManager
from team_manager import TeamManager, parse_lineup
from test_flow_templates import DEBATE_TEMPLATE, VERSUS_TEMPLATE
from test_team_flows import BASE, PANEL, PIPELINE, VERIFY, VERSUS
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


TWO_STAGE = {
    "id": "review-two-stage",
    "name": "Two-stage review",
    "category": "development",
    "projects": ["code"],
    "params": {"TARGET": {"type": "string", "label": "Target", "max": 200}},
    "task": "Review ${TARGET}.",
    "seats": [
        {"id": "lead", "job": "write", "character": "domain_architect", "providers": ["claude"]},
        {"id": "sec", "job": "critique", "character": "critic_redteam", "providers": ["codex"]},
        {"id": "qa", "job": "critique", "mode": "checklist", "character": "devops_specialist", "providers": ["agy"]},
        {"id": "sum", "job": "summarise", "providers": ["claude"]},
    ],
    "bundles": [{"id": "review", "seats": ["sec", "qa"]}],
    "close": {"character": "jev_mcp_decision", "input": "condensed", "allowed_tools": []},
    "links": [
        {"from": "start", "to": "lead"},
        {"from": "review", "to": "lead", "look_back": True},
        {"from": "lead", "to": "review"},
        {"from": "review", "to": "sum"},
        {"from": "sum", "to": "close"},
        {"from": "close", "to": "lead", "exit": "rejected", "max_rounds": 3},
        {"from": "close", "to": "end", "exit": "approved"},
    ],
    "layout": {"lead": [240, 80], "review": [480, 40]},
}


class FakePersonas:
    """A persona manager with a format 2 character that may only critique."""

    def get_persona(self, persona_id):
        if persona_id == "only_critic":
            return {"id": persona_id, "format": 2, "seats": ["critic"]}
        return {"id": persona_id} if persona_id != "ghost" else None


def links(flow, **match):
    return [l for l in flow["links"] if all(l.get(k) == v for k, v in match.items())]


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.personas = PersonaManager()

    def check(self, flow, personas=None):
        return fg.validate_flow(flow, personas or self.personas)

    def broken(self, mutate, message, personas=None):
        flow = copy.deepcopy(TWO_STAGE)
        mutate(flow)
        with self.assertRaises(fg.FlowValidationError) as ctx:
            self.check(flow, personas)
        self.assertIn(message, str(ctx.exception))

    def test_two_stage_flow_is_valid_and_warns_about_the_condensed_close(self) -> None:
        warnings = self.check(TWO_STAGE)
        self.assertTrue(any("D4" in w for w in warnings), warnings)
        self.assertEqual(fg.calls_per_round(TWO_STAGE), 5)

    def test_unknown_keys_are_refused(self) -> None:
        self.broken(lambda f: f.update(color="red"), "color: unknown key")
        self.broken(lambda f: f["seats"][0].update(role="expert"), "seats[0].role: unknown key")
        self.broken(lambda f: f["links"][0].update(weight=1), "links[0].weight: unknown key")

    def test_fixed_ids_and_duplicates(self) -> None:
        self.broken(lambda f: f["seats"][0].update(id="close"), "not start, close or end")
        self.broken(lambda f: f["seats"][1].update(id="lead"), "used twice")
        self.broken(lambda f: f["bundles"][0].update(id="lead"), "used twice")

    def test_jobs_modes_and_characters(self) -> None:
        self.broken(lambda f: f["seats"][0].update(job="judge"), "seats.lead.job")
        self.broken(lambda f: f["seats"][0].update(mode="checklist"), "only a critique has a mode")
        self.broken(lambda f: f["seats"][1].update(character="ghost"), "unknown character 'ghost'", FakePersonas())
        self.broken(lambda f: f["seats"][0].update(character="only_critic"), "may take critic, not expert",
                    FakePersonas())
        self.broken(lambda f: f["seats"][0].update(providers=["gpt9"]), "unknown provider")
        self.broken(lambda f: f["seats"][0].update(instruction="Be brief."), "only a summariser")

    def test_a_format_two_critic_may_critique(self) -> None:
        flow = copy.deepcopy(TWO_STAGE)
        flow["seats"][1]["character"] = "only_critic"
        fg.validate_flow(flow, FakePersonas())

    def test_forward_loop_is_refused_and_look_back_is_not_a_loop(self) -> None:
        self.broken(lambda f: f["links"].__setitem__(1, {"from": "review", "to": "lead"}), "a loop of forward links")

    def test_a_seat_that_never_runs_or_never_reaches_the_close(self) -> None:
        def orphan(flow):
            flow["seats"].append({"id": "extra", "job": "critique", "character": "critic_redteam"})
            flow["links"].append({"from": "extra", "to": "close"})
        self.broken(orphan, "seats.extra: never runs")

        def dead_end(flow):
            flow["seats"].append({"id": "extra", "job": "critique", "character": "critic_redteam"})
            flow["links"].append({"from": "lead", "to": "extra"})
        self.broken(dead_end, "seats.extra: its work never reaches the close")

    def test_the_close_needs_both_exits(self) -> None:
        self.broken(lambda f: f["links"].pop(), "needs its approved exit")
        self.broken(lambda f: f["links"].pop(-2), "needs its rejected exit")
        self.broken(lambda f: f["links"].append({"from": "close", "to": "lead"}), "approved or rejected exit")
        self.broken(lambda f: f["links"].append({"from": "lead", "to": "end"}), "only the close's approved exit")
        self.broken(lambda f: f["links"][0].update(max_rounds=2), "belongs to the rejected exit")

    def test_condensed_needs_a_summariser_and_a_summariser_feeds_one_seat(self) -> None:
        self.broken(lambda f: f["seats"][0].update(input="condensed"), "needs a summariser linked in front")
        def two_receivers(flow):
            flow["seats"].append({"id": "extra", "job": "critique", "character": "critic_redteam"})
            flow["links"] += [{"from": "sum", "to": "extra"}, {"from": "extra", "to": "close"}]
        self.broken(two_receivers, "feeds exactly one seat")

    def test_members_of_a_bundle_do_not_read_each_other(self) -> None:
        self.broken(lambda f: f["links"].append({"from": "sec", "to": "qa"}), "stays inside bundle 'review'")

    def test_rebut_seats_come_in_a_bundle_of_two_with_a_stance(self) -> None:
        def lone(flow):
            flow["seats"][0].update(job="rebut", stance="Keep it.")
        self.broken(lone, "sits in a bundle of two sides")
        self.broken(lambda f: f["seats"][0].update(stance="Keep it."), "only a rebut seat defends a stance")

        def undeclared(flow):
            flow["seats"][1:3] = [{"id": "sec", "job": "rebut", "character": "critic_redteam", "stance": "${NOPE}"},
                                  {"id": "qa", "job": "rebut", "character": "devops_specialist", "stance": "No."}]
        self.broken(undeclared, "${NOPE} is not a declared parameter")

    def test_limits(self) -> None:
        def crowd(flow):
            flow["seats"] += [{"id": f"c{i}", "job": "critique", "character": "critic_redteam"} for i in range(9)]
        self.broken(crowd, "at most 12 seats")
        self.broken(lambda f: f["bundles"][0]["seats"].extend(["lead", "sum", "x", "y"]), "1 to 4 different seat ids")

    def test_parameters_reuse_the_template_rules(self) -> None:
        self.broken(lambda f: f["params"].update(bad={"type": "string"}), "is not a parameter name")
        self.broken(lambda f: f.update(task="Review ${OTHER}."), "${OTHER} is not a declared parameter")

    def test_layout_names_existing_elements(self) -> None:
        self.broken(lambda f: f["layout"].update(ghost=[1, 2]), "layout.ghost: no such element")
        self.broken(lambda f: f["layout"].update(lead=[1]), "expected [x, y]")


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.personas = PersonaManager()

    def convert(self, team):
        flow = fg.team_to_flow(copy.deepcopy(team))
        fg.validate_flow(flow, self.personas)
        return flow

    def test_every_builtin_team_converts_and_validates(self) -> None:
        teams = TeamManager(persona_mgr=self.personas).list_teams()
        self.assertTrue(teams)
        for team in teams:
            with self.subTest(team=team["id"]):
                flow = self.convert(team)
                self.assertEqual(flow["converted_from"], {"team": team["id"]})

    def test_debate(self) -> None:
        flow = self.convert(BASE)
        self.assertEqual([s["job"] for s in flow["seats"]], ["write", "critique"])
        self.assertEqual(links(flow, exit="rejected")[0], {"from": "close", "to": "lead", "exit": "rejected",
                                                           "max_rounds": 2})
        self.assertTrue(links(flow, **{"from": "critic-1", "to": "lead", "look_back": True}))
        self.assertEqual(flow["close"]["allowed_tools"], [])

    def test_debate_with_proposers_drafts_blind_then_the_lead_alone(self) -> None:
        team = {**BASE, "proposers": [{"persona": "devops_specialist", "providers": ["codex"]}]}
        flow = self.convert(team)
        self.assertEqual(flow["bundles"][0], {"id": "drafts", "seats": ["lead", "proposer-1"]})
        self.assertTrue(links(flow, **{"from": "start", "to": "drafts"}))
        self.assertTrue(links(flow, **{"from": "drafts", "to": "lead", "look_back": True}))
        self.assertEqual(links(flow, exit="rejected")[0]["to"], "lead")

    def test_verify_critics_use_the_checklist(self) -> None:
        flow = self.convert(VERIFY)
        self.assertEqual(flow["seats"][1]["mode"], "checklist")

    def test_panel_goes_straight_to_the_close_in_one_round(self) -> None:
        flow = self.convert(PANEL)
        self.assertNotIn("critique", [s["job"] for s in flow["seats"]])
        self.assertTrue(links(flow, **{"from": "drafts", "to": "close"}))
        self.assertNotIn("max_rounds", links(flow, exit="rejected")[0])   # one round anyway: nothing is critiqued

    def test_pipeline_lead_rewrites_only_after_a_rejection(self) -> None:
        flow = self.convert(PIPELINE)
        self.assertEqual([s["job"] for s in flow["seats"]], ["write", "rewrite", "rewrite", "rewrite"])
        self.assertFalse(links(flow, **{"from": "start", "to": "lead"}))
        self.assertEqual(links(flow, exit="rejected")[0]["to"], "lead")

    def test_versus_sides_form_one_bundle_with_their_stances(self) -> None:
        flow = self.convert(VERSUS)
        self.assertEqual(flow["bundles"][0]["id"], "sides")
        self.assertEqual(flow["bundles"][0]["exchanges"], VERSUS["flow"]["exchanges"])
        self.assertEqual([s["stance"] for s in flow["seats"]], [s["stance"] for s in VERSUS["flow"]["sides"]])

    def test_a_team_that_does_not_parse_does_not_convert(self) -> None:
        with self.assertRaises(fg.ConversionError):
            fg.team_to_flow({"id": "x"})


class TemplateConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.personas = PersonaManager()
        self.teams = TeamManager(directory=Path(self.tmp.name), persona_mgr=self.personas)
        for team in (BASE, VERSUS, {**BASE, "id": "propteam",
                                    "proposers": [{"persona": "devops_specialist", "providers": ["codex"]}]}):
            self.teams.save(copy.deepcopy(team))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def convert(self, template):
        flow = fg.template_to_flow(copy.deepcopy(template), self.teams)
        fg.validate_flow(flow, self.personas)
        return flow

    def test_alias_template_keeps_its_form_and_task(self) -> None:
        flow = self.convert(DEBATE_TEMPLATE)
        self.assertEqual(flow["params"], DEBATE_TEMPLATE["params"])
        self.assertEqual(flow["task"], DEBATE_TEMPLATE["task"])
        self.assertEqual(flow["converted_from"], {"template": "review-change", "team": "flowteam"})

    def test_versus_template_stances_replace_the_team_stances(self) -> None:
        flow = self.convert(VERSUS_TEMPLATE)
        self.assertEqual([s["stance"] for s in flow["seats"]], ["Defend ${STANCE_A}", "Defend ${STANCE_B}"])

    def test_custom_steps(self) -> None:
        base = {**DEBATE_TEMPLATE, "id": "custom", "team": "propteam"}
        draft_only = self.convert({**base, "steps": [{"kind": "draft"}]})
        self.assertEqual([s["id"] for s in draft_only["seats"]], ["lead"])
        self.assertEqual(fg.compile_flow(draft_only)[1], [{"kind": "draft"}])
        blind = self.convert({**base, "steps": [{"kind": "blind_draft"}, {"kind": "critique", "mode": "checklist"}]})
        self.assertEqual(blind["bundles"][0]["seats"], ["lead", "proposer-1"])
        self.assertEqual(blind["seats"][-1]["mode"], "checklist")

    def test_a_template_without_its_team_does_not_convert(self) -> None:
        for template, message in (({**DEBATE_TEMPLATE, "team": "nope"}, "does not exist"),
                                  ({k: v for k, v in DEBATE_TEMPLATE.items() if k != "team"}, "names no team"),
                                  ({**DEBATE_TEMPLATE, "team": "versusteam"}, "runs the versus flow")):
            with self.subTest(message=message), self.assertRaises(fg.ConversionError) as ctx:
                fg.template_to_flow(template, self.teams)
            self.assertIn(message, str(ctx.exception))


class CompileTests(unittest.TestCase):
    """G2: the shapes a flow compiles to, the ones it refuses, and the start of a run."""

    def setUp(self) -> None:
        self.personas = PersonaManager()

    def compile(self, team):
        return fg.compile_flow(fg.team_to_flow(copy.deepcopy(team)))

    def test_shapes_compile_to_their_flow_kind(self) -> None:
        team, steps, _ = self.compile(BASE)
        self.assertEqual((team.get("flow"), steps), (None, None))
        self.assertEqual(team["expert"]["seat"], "lead")
        self.assertEqual(team["judge"]["seat"], "close")
        self.assertEqual(self.compile(VERIFY)[0]["flow"], {"kind": "verify"})
        self.assertEqual(self.compile(PANEL)[0]["flow"], {"kind": "panel"})
        self.assertEqual(self.compile(PIPELINE)[0]["flow"]["kind"], "pipeline")
        versus = self.compile(VERSUS).team
        self.assertEqual(versus["flow"]["kind"], "versus")
        self.assertNotIn("seat", versus["expert"])   # the stand-in lead of a versus has no seat

    def test_shapes_without_a_kind_compile_to_custom_steps(self) -> None:
        lead_only = fg.team_to_flow(copy.deepcopy(BASE))
        lead_only["seats"] = lead_only["seats"][:1]
        lead_only["links"] = [{"from": "start", "to": "lead"}, {"from": "lead", "to": "close"},
                              {"from": "close", "to": "lead", "exit": "rejected"},
                              {"from": "close", "to": "end", "exit": "approved"}]
        self.assertEqual(fg.compile_flow(lead_only)[1], [{"kind": "draft"}])
        checklist = fg.team_to_flow(copy.deepcopy({**BASE, "proposers": [{"persona": "devops_specialist"}]}))
        for seat in checklist["seats"]:
            if seat["job"] == "critique":
                seat["mode"] = "checklist"
        self.assertEqual(fg.compile_flow(checklist)[1],
                         [{"kind": "blind_draft"}, {"kind": "critique", "mode": "checklist"}])

    def test_shapes_the_engine_does_not_run_yet_are_refused(self) -> None:
        with self.assertRaises(fg.FlowValidationError) as ctx:
            fg.compile_flow(copy.deepcopy(TWO_STAGE))
        self.assertIn("does not run yet", str(ctx.exception))
        two_reviews = fg.team_to_flow(copy.deepcopy(BASE))
        two_reviews["seats"].append({"id": "second", "job": "critique", "character": "devops_specialist"})
        two_reviews["links"] = [l for l in two_reviews["links"] if l.get("to") != "close" or "exit" in l]
        two_reviews["links"] += [{"from": "critic-1", "to": "second"}, {"from": "second", "to": "close"}]
        fg.validate_flow(two_reviews, self.personas)
        with self.assertRaises(fg.FlowValidationError):
            fg.compile_flow(two_reviews)

    def test_one_character_twice_in_a_panel_is_refused(self) -> None:
        flow = fg.team_to_flow(copy.deepcopy({**BASE, "critic": [{"persona": "critic_redteam"},
                                                                  {"persona": "devops_specialist"}]}))
        flow["seats"][2]["character"] = "critic_redteam"
        with self.assertRaises(fg.FlowValidationError) as ctx:
            fg.compile_flow(flow)
        self.assertIn("different characters", str(ctx.exception))

    def test_swaps_seat_another_character(self) -> None:
        flow = fg.team_to_flow(copy.deepcopy(BASE))
        swapped = fg.swap_characters(flow, {"critic-1": "devops_specialist", "close": "ops_judge"}, self.personas)
        self.assertEqual(swapped["seats"][1]["character"], "devops_specialist")
        self.assertEqual(swapped["close"]["character"], "ops_judge")
        self.assertEqual(flow["seats"][1]["character"], "critic_redteam")   # the stored flow is not changed
        with self.assertRaises(fg.FlowValidationError):
            fg.swap_characters(flow, {"nope": "ops_sre"}, self.personas)
        with self.assertRaises(fg.FlowValidationError):
            fg.swap_characters(flow, {"lead": "only_critic"}, FakePersonas())

    def test_resolve_run_renders_the_task_and_records_the_flow(self) -> None:
        flow = fg.team_to_flow(copy.deepcopy(BASE))
        flow.update(params={"TARGET": {"type": "string", "max": 50}}, task="Review ${TARGET}.")
        run = fg.resolve_run(flow, {"TARGET": "app.py"}, {"critic-1": "devops_specialist"}, self.personas)
        self.assertEqual(run["task"], "Review app.py.")
        self.assertEqual(run["record"]["seats"]["critic-1"], "devops_specialist")
        self.assertEqual(run["record"]["params"], {"TARGET": "app.py"})
        self.assertEqual(run["team"]["critic"][0]["persona"], "devops_specialist")
        with self.assertRaises(fg.FlowValidationError):
            fg.resolve_run(flow, {"TARGET": "app.py"}, {}, self.personas, task="my own task")
        with self.assertRaises(fg.FlowValidationError):
            fg.resolve_run(flow, {}, {}, self.personas)          # TARGET is required
        plain = fg.team_to_flow(copy.deepcopy(BASE))
        self.assertEqual(fg.resolve_run(plain, {}, {}, self.personas, task="t")["task"], "t")
        with self.assertRaises(fg.FlowValidationError):
            fg.resolve_run(plain, {}, {}, self.personas)          # a flow without a task text needs one
        with self.assertRaises(fg.FlowValidationError):
            fg.resolve_run({**plain, "projects": ["ops"]}, {}, {}, self.personas, project_kind="code", task="t")

    def test_seat_args(self) -> None:
        self.assertEqual(fg.parse_seat_args(["lead=ops_sre", "close=ops_judge"]),
                         {"lead": "ops_sre", "close": "ops_judge"})
        for bad in (["lead"], ["lead=a", "lead=b"], ["=x"]):
            with self.assertRaises(fg.FlowValidationError):
                fg.parse_seat_args(bad)


def condensing_flow(close_input: str = "condensed", summariser: bool = True, lead_input: str = "raw") -> dict:
    """The debate team BASE as a flow with a summariser between the critic and the close, one round."""
    flow = fg.team_to_flow(copy.deepcopy(BASE))
    flow["links"] = [l for l in flow["links"] if not (l.get("from") == "critic-1" and l.get("to") == "close")]
    if summariser:
        flow["seats"].append({"id": "sum", "job": "summarise", "providers": ["claude"]})
        flow["links"] += [{"from": "critic-1", "to": "sum"}, {"from": "sum", "to": "close"}]
    else:
        flow["links"].append({"from": "critic-1", "to": "close"})
    flow["close"]["input"] = close_input
    flow["seats"][0]["input"] = lead_input
    for link in flow["links"]:
        if link.get("exit") == "rejected":
            link["max_rounds"] = 1
    return flow


class G3CompileTests(unittest.TestCase):
    def test_a_summariser_in_front_of_the_close_compiles_around_the_debate(self) -> None:
        team, steps, condense = fg.compile_flow(condensing_flow())
        self.assertIsNone(steps)
        self.assertEqual([m["seat"] for m in team["critic"]], ["critic-1"])
        self.assertEqual(condense, {"summariser": {"seat": "sum", "providers": ["claude"]},
                                    "close_input": "condensed", "envelope": True})

    def test_structured_inputs_of_the_close_and_the_lead(self) -> None:
        condense = fg.compile_flow(condensing_flow("structured", summariser=False, lead_input="structured")).condense
        self.assertEqual(condense, {"close_input": "structured", "lead_input": "structured", "envelope": True})
        self.assertEqual(fg.compile_flow(fg.team_to_flow(copy.deepcopy(BASE))).condense, {})

    def test_what_g3_does_not_run_yet(self) -> None:
        latest = condensing_flow("raw", summariser=False)
        latest["seats"][1]["input"] = "latest"
        critic_structured = condensing_flow("raw", summariser=False)
        critic_structured["seats"][1]["input"] = "structured"
        history = condensing_flow("raw", summariser=False)
        history["seats"][0]["history"] = "none"
        for flow in (latest, critic_structured, history):
            with self.assertRaises(fg.FlowValidationError) as ctx:
                fg.compile_flow(flow)
            self.assertIn("does not run yet", str(ctx.exception))


class EnvelopeTextTests(unittest.TestCase):
    ANSWER = ("## Conclusion\nShip it.\n## Reasoning\nlong private reasoning\n## Claims\n- fine\n"
              "## Evidence\n- app.py:1 def hello\n## Open issues\n- none\n## Recommendation\nmerge")

    def test_keep_sections(self) -> None:
        kept = de.keep_sections("### Critique from A\n" + self.ANSWER)
        self.assertIn("### Critique from A", kept)
        self.assertIn("app.py:1", kept)
        self.assertNotIn("private reasoning", kept)
        self.assertNotIn("merge", kept)
        self.assertEqual(de.keep_sections("no envelope here"), "no envelope here")

    def test_invented_citations(self) -> None:
        self.assertEqual(de.invented_citations("see app.py:1 and [#3 disk node-1]", "app.py:1 ... [#3 disk node-1]"), [])
        self.assertEqual(de.invented_citations("see other.py:99 and app.py:1", "app.py:1"), ["other.py:99"])


class G3RunTests(unittest.TestCase):
    """The engine with a fake model: the summariser's place, the seeded invented citation, the structured close."""

    EXPERT = ("## Conclusion\nChange hello.\n## Reasoning\nprivate reasoning of the lead\n"
              "## Evidence\n- app.py:1 def hello\n## Open issues\n- none")

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls: list = []
        self.lock = threading.Lock()
        self.summary = "## Conclusion\nCondensed.\n## Evidence\n- app.py:1 def hello"
        self.summary_provider = "claude-cli"
        env = patch.dict(os.environ, {"GUARDIAN_JEV_GATE": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.engine = de.DebateEngine(db_path=self.root / "d.db", events=EventWriter(str(self.root / "events.jsonl")),
                                      team_mgr=TeamManager(self.root / "teams"))

    def fake(self, prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
        prompt = prompt + (instructions.text() if instructions else "")
        with self.lock:
            self.calls.append(prompt)
        if de.JUDGE_VERDICT_RULE in prompt:
            return LLMResult("claude-cli", "Not yet.\nVERDICT: REJECTED", "s")
        if "You condense work for the next step" in prompt:
            return LLMResult(self.summary_provider, self.summary, "s")
        return LLMResult(f"{providers[0]}-cli", self.EXPERT, "s")

    def run_flow(self, flow) -> dict:
        run = fg.resolve_run(flow, {}, {}, PersonaManager(), task="Improve hello")
        with patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake), \
                patch.object(self.engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            try:
                return self.engine.run_debate(run["task"], project_id="server-guardian", flow_run=run)
            finally:
                self.engine.events.close()

    def judge_prompt(self) -> str:
        return next(c for c in self.calls if de.JUDGE_VERDICT_RULE in c)

    def events(self) -> list:
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def test_the_judge_reads_the_summary_and_every_seat_gets_the_envelope(self) -> None:
        result = self.run_flow(condensing_flow())
        judge = self.judge_prompt()
        self.assertIn(de.SUMMARY_INTRO.strip(), judge)
        self.assertIn("Condensed.", judge)
        self.assertNotIn("private reasoning of the lead", judge)
        self.assertNotIn(de.ENVELOPE_RULE.strip(), judge)
        lead = next(c for c in self.calls if "Improve hello" in c and de.JUDGE_VERDICT_RULE not in c)
        self.assertIn(de.ENVELOPE_RULE.strip(), lead)
        summary_entries = [t for t in result["transcript"] if t["role"] == de.SUMMARISER_ROLE]
        self.assertEqual([t["seat"] for t in summary_entries], ["sum"])
        self.assertTrue(any(e.get("seat") == "sum" for e in self.events() if e["type"] == "agent_started"))
        self.assertFalse([e for e in self.events() if e["type"] == "summary_rejected"])

    def test_a_summary_that_invents_a_citation_is_dropped(self) -> None:
        self.summary = "## Conclusion\nCondensed.\n## Evidence\n- other.py:99 invented"
        self.run_flow(condensing_flow())
        judge = self.judge_prompt()
        self.assertNotIn(de.SUMMARY_INTRO.strip(), judge)
        self.assertIn("private reasoning of the lead", judge)        # the Judge reads the work as it is
        rejected = [e for e in self.events() if e["type"] == "summary_rejected"]
        self.assertEqual(rejected[0]["data"]["citations"], ["other.py:99"])
        self.assertEqual(rejected[0]["seat"], "sum")

    def test_a_canned_summary_is_dropped(self) -> None:
        self.summary_provider = BUILTIN_PROVIDER
        self.run_flow(condensing_flow())
        self.assertIn("private reasoning of the lead", self.judge_prompt())
        verdict = next(e for e in self.events() if e["type"] == "verdict")
        self.assertFalse(verdict["data"]["unverified"])   # a dropped summary does not make the round UNVERIFIED

    def test_the_summariser_reads_nothing_itself(self) -> None:
        """G5: a summariser that read files cited lines the work did not hold, and every summary was dropped."""
        seen = []
        real_ask = self.engine._ask

        def ask(thread_id, role, *args, **kwargs):
            if role == de.SUMMARISER_ROLE:
                seen.append((self.engine._repo, self.engine._ops, self.engine._context_text, self.engine._retrieval_forced))
            return real_ask(thread_id, role, *args, **kwargs)

        repo, ops = object(), object()
        run = fg.resolve_run(condensing_flow(), {}, {}, PersonaManager(), task="t")
        lineup = parse_lineup(run["team"])
        lineup.flow_graph = run["record"]
        self.engine._repo, self.engine._ops, self.engine._context_text = repo, ops, "pinned code"
        middle = de.Middle("proposal app.py:1", "critique", "", ["claude-cli"], "Proposal:\napp.py:1 def hello\n")
        with patch.object(self.engine, "_ask", side_effect=ask), \
                patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake):
            self.engine._close_material(lineup, "thread_x", 1, "task", middle, {}, [])
        self.assertEqual(seen, [(None, None, "", False)])
        self.assertIs(self.engine._repo, repo)       # restored after the turn
        self.assertIs(self.engine._ops, ops)
        self.assertEqual(self.engine._context_text, "pinned code")
        self.assertIn("no access to the repository", next(c for c in self.calls if "You condense work" in c))

    def test_a_structured_close_reads_the_kept_sections_only(self) -> None:
        self.run_flow(condensing_flow("structured", summariser=False))
        judge = self.judge_prompt()
        self.assertIn("app.py:1 def hello", judge)
        self.assertNotIn("private reasoning of the lead", judge)
        self.assertFalse(any("You condense work" in c for c in self.calls))


class StoreAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.builtin = root / "builtin"
        self.builtin.mkdir()
        self.personas = PersonaManager()
        self.store = fg.FlowStore(root / "custom", self.builtin, self.personas)
        self.teams = TeamManager(directory=root / "teams", persona_mgr=self.personas)
        self.templates = ft.FlowTemplateStore(root / "templates", root / "no-builtin-templates")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_stamps_and_delete_custom_only(self) -> None:
        self.store.save(copy.deepcopy(TWO_STAGE))
        saved = self.store.get("review-two-stage")
        self.assertEqual(saved["source"], "custom")
        self.assertIn("created", saved)
        (self.builtin / "builtin-flow.json").write_text(json.dumps({**TWO_STAGE, "id": "builtin-flow"}))
        self.assertFalse(self.store.delete("builtin-flow"))
        self.assertTrue(self.store.delete("review-two-stage"))
        self.assertIsNone(self.store.get("review-two-stage"))

    def test_save_refuses_an_invalid_flow(self) -> None:
        with self.assertRaises(fg.FlowValidationError):
            self.store.save({**TWO_STAGE, "links": []})
        self.assertEqual(self.store.list_flows(), [])

    def test_report_lists_teams_templates_and_flows_with_shadowing(self) -> None:
        self.teams.save(copy.deepcopy(BASE))
        self.templates.save(copy.deepcopy(DEBATE_TEMPLATE))
        self.store.save({**copy.deepcopy(TWO_STAGE), "id": "review-change"})
        out = fg.report(self.store, self.teams, self.templates)
        by_id = {f["id"]: f for f in out["flows"]}
        self.assertEqual(by_id["flowteam"]["source"], "team")
        self.assertEqual(by_id["review-change"]["source"], "custom")
        self.assertIn("shadows the template 'review-change'", " ".join(by_id["review-change"]["warnings"]))
        self.assertFalse(any(f["errors"] for f in out["flows"]), out["flows"])
        self.assertTrue(by_id["flowteam"]["runnable"])
        self.assertFalse(by_id["review-change"]["runnable"])        # TWO_STAGE mixes critique modes in one bundle
        self.assertIn("does not run yet", by_id["review-change"]["run_error"])
        self.assertIn("summarise", out["catalog"]["jobs"])

    def test_report_names_a_template_that_does_not_convert(self) -> None:
        self.templates.save({**copy.deepcopy(DEBATE_TEMPLATE), "team": "missing"})
        by_id = {f["id"]: f for f in fg.report(self.store, self.teams, self.templates)["flows"]}
        self.assertIn("does not convert", by_id["review-change"]["errors"][0])

    def test_json_commands(self) -> None:
        code, out = config_ops.save_flow(self.store, json.dumps(TWO_STAGE))
        self.assertEqual((code, out["ok"]), (0, True))
        code, out = config_ops.save_flow(self.store, "{")
        self.assertEqual(code, config_ops.INVALID)
        code, out = config_ops.delete_flow(self.store, "nope")
        self.assertEqual(code, config_ops.NOT_FOUND)
        code, out = config_ops.delete_flow(self.store, "review-two-stage")
        self.assertEqual((code, out["ok"]), (0, True))


if __name__ == "__main__":
    unittest.main()

"""Flow templates, phase T1 (docs/design/flow-templates.md): store, validation, parameters, the start of a debate from
a template, and the JSON commands the hub API relays."""

import json
import os
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config_ops
import flow_templates as ft
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine
from debate_events import EventWriter
from llm_runner import LLMResult
from team_manager import TeamManager, TeamValidationError
from test_team_flows import BASE, VERSUS
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


VERSUS_TEMPLATE = {
    "id": "design-decision",
    "name": "Design decision",
    "category": "development",
    "projects": ["code"],
    "team": "versusteam",
    "steps": [{"kind": "versus"}],
    "params": {
        "TOPIC": {"type": "text", "label": "Question", "max": 2000},
        "STANCE_A": {"type": "string", "label": "Position A", "max": 150},
        "STANCE_B": {"type": "string", "label": "Position B", "max": 150},
    },
    "task": "Decide: ${TOPIC}",
    "stances": {"side:A": "Defend ${STANCE_A}", "side:B": "Defend ${STANCE_B}"},
    "max_rounds": 1,
    "reviewed": date.today().isoformat(),
}
DEBATE_TEMPLATE = {
    "id": "review-change",
    "name": "Review a change",
    "category": "development",
    "projects": ["code"],
    "team": "flowteam",
    "steps": [{"kind": "debate"}],
    "params": {"CHANGE": {"type": "string", "label": "Change", "max": 100},
               "DEPTH": {"type": "enum", "label": "Depth", "values": ["quick", "deep"], "default": "quick"},
               "LINES": {"type": "int", "label": "Lines", "min": 1, "max": 3, "default": 2}},
    "task": "Review ${CHANGE} (${DEPTH}, ${LINES} lines)",
}
VALUES = {"TOPIC": "one hub or two", "STANCE_A": "the single hub", "STANCE_B": "two hubs"}


def valid(**changes) -> dict:
    data = json.loads(json.dumps(VERSUS_TEMPLATE))
    data.update(changes)
    return data


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "builtin").mkdir()
        self.store = ft.FlowTemplateStore(root / "custom", root / "builtin")

    def test_an_empty_or_missing_builtin_directory_is_fine(self) -> None:
        missing = ft.FlowTemplateStore(Path(self.tmp.name) / "none", Path(self.tmp.name) / "also-none")
        self.assertEqual(missing.list_templates(), [])
        self.assertIsNone(missing.get("design-decision"))

    def test_save_stamps_timestamps_and_keeps_created_on_edit(self) -> None:
        self.store.save(valid())
        first = self.store.get("design-decision")
        self.assertEqual(first["source"], "custom")
        self.assertTrue(first["created"] and first["modified"])
        self.store.save(valid(name="Renamed", created="1999-01-01T00:00:00+00:00"))
        second = self.store.get("design-decision")
        self.assertEqual(second["created"], first["created"])
        self.assertEqual(second["name"], "Renamed")

    def test_a_custom_template_shadows_a_builtin_one_and_deleting_it_shows_the_builtin_again(self) -> None:
        (Path(self.tmp.name) / "builtin" / "design-decision.json").write_text(json.dumps(valid(name="Builtin")))
        self.assertEqual(self.store.get("design-decision")["source"], "builtin")
        self.store.save(valid(name="Mine"))
        listed = self.store.list_templates()
        self.assertEqual([(t["name"], t["source"]) for t in listed], [("Mine", "custom")])
        self.assertTrue(self.store.delete("design-decision"))
        self.assertEqual(self.store.get("design-decision")["name"], "Builtin")

    def test_a_builtin_template_cannot_be_deleted_and_an_unknown_id_deletes_nothing(self) -> None:
        (Path(self.tmp.name) / "builtin" / "design-decision.json").write_text(json.dumps(valid()))
        self.assertFalse(self.store.delete("design-decision"))
        self.assertFalse(self.store.delete("nope"))
        self.assertFalse(self.store.delete("../etc"))

    def test_ids_that_could_leave_the_directory_are_not_found(self) -> None:
        self.assertIsNone(self.store.get("../teams/default"))
        self.assertFalse(self.store.is_custom("a/b"))
        self.store.save(valid())
        self.assertIsNone(self.store.get("design-decision\n"))
        self.assertFalse(self.store.is_custom("design-decision\n"))

    def test_an_unreadable_custom_file_shadows_the_builtin_instead_of_falling_back_to_it(self) -> None:
        (Path(self.tmp.name) / "builtin" / "design-decision.json").write_text(json.dumps(valid(name="Builtin")))
        self.store.directory.mkdir(parents=True)
        (self.store.directory / "design-decision.json").write_text("{not json")
        self.assertIsNone(self.store.get("design-decision"))
        self.assertEqual(self.store.list_templates(), [])

    def test_the_hash_ignores_the_stamps_but_follows_the_text(self) -> None:
        base = ft.content_hash(valid())
        self.assertEqual(base, ft.content_hash({**valid(), "created": "x", "modified": "y", "source": "custom"}))
        self.assertNotEqual(base, ft.content_hash(valid(task="Decide: ${TOPIC} now")))


class ValidationTests(unittest.TestCase):
    def refused(self, data, fragment: str) -> None:
        with self.assertRaises(ft.TemplateValidationError) as caught:
            ft.validate_template(data)
        self.assertIn(fragment, str(caught.exception))

    def test_the_examples_are_valid(self) -> None:
        self.assertEqual(ft.validate_template(valid()), [])
        self.assertEqual(ft.validate_template(DEBATE_TEMPLATE), [])

    def test_unknown_keys_and_bad_scalars_are_named(self) -> None:
        self.refused({**valid(), "color": "red"}, "color")
        self.refused(valid(id="Bad_Id"), "id")
        self.refused(valid(id="x" * 41), "id")
        self.refused(valid(name=" "), "name")
        self.refused(valid(category="fun"), "category")
        self.refused(valid(projects=[]), "projects")
        self.refused(valid(projects=["code", "code"]), "projects")
        self.refused(valid(projects=["mainframe"]), "projects")
        self.refused(valid(projects=[["code"]]), "projects")
        self.refused(valid(projects=[{"kind": "code"}]), "projects")
        self.refused(valid(id="design-decision\n"), "id")
        self.refused(valid(max_rounds=0), "max_rounds")
        self.refused(valid(max_rounds=True), "max_rounds")
        self.refused([], "JSON object")

    def test_steps_are_an_alias_of_a_flow_kind_or_custom_steps_of_t3a(self) -> None:
        self.refused(valid(steps=[]), "steps")
        self.refused(valid(steps="versus"), "steps")
        self.refused(valid(steps=[{"kind": "versus"}, {"kind": "versus"}]), "steps[0]")
        self.refused(valid(steps=[{"kind": "versus", "seats": ["side:A"]}]), "steps[0]")
        self.refused(valid(steps=[{"kind": "blind_draft"}]), "only a versus flow has sides")  # custom steps: no sides
        self.assertEqual(ft.template_kind(valid()), "versus")

    def test_placeholders_must_name_declared_parameters(self) -> None:
        self.refused(valid(task="Decide: ${MISSING}"), "MISSING")
        self.refused(valid(stances={"side:A": "${NOPE}", "side:B": "b"}), "NOPE")
        self.refused(valid(params={"lower": {"type": "string"}}), "parameter name")
        self.refused(valid(params={**VERSUS_TEMPLATE["params"], "TOPIC\n": {"type": "string"}}), "parameter name")

    def test_a_template_that_can_overrun_a_limit_after_filling_in_is_refused_at_save(self) -> None:
        wide = {**VERSUS_TEMPLATE["params"], "STANCE_A": {"type": "string", "max": 400}}
        self.refused(valid(params=wide), "stances.side:A")  # "Defend " + 400 characters is over 400
        huge = {**VERSUS_TEMPLATE["params"], "TOPIC": {"type": "text", "max": 4000}}
        self.refused(valid(params=huge, task="Decide: ${TOPIC}"), "task")

    def test_parameter_specs_are_checked(self) -> None:
        for spec, fragment in (
            ({"type": "float"}, "type"),
            ({"type": "string", "max": 0}, "max"),
            ({"type": "string", "max": 401}, "max"),
            ({"type": "int", "min": 5, "max": 1}, "min"),
            ({"type": "enum"}, "values"),
            ({"type": "enum", "values": ["a", "a"]}, "values"),
            ({"type": "string", "values": ["a"]}, "enum"),
            ({"type": "string", "extra": 1}, "extra"),
            ({"type": "int", "default": "x"}, "default"),
            ({"type": "enum", "values": ["a"], "default": "b"}, "default"),
            ({"type": "string", "label": "x" * 81}, "label"),
            ({"type": "int", "default": 1.5}, "default"),
            ({"type": "int", "default": 2.0}, "default"),
            ({"type": "int", "default": float("inf")}, "default"),
            ({"type": "int", "default": float("nan")}, "default"),
            ({"type": "int", "default": "１"}, "default"),
            ({"type": "int", "default": "1_0"}, "default"),
            ({"type": "string", "min": 1}, "min"),
            ({"type": "text", "max": 10, "min": 1}, "min"),
            ({"type": "enum", "values": ["a"], "min": 1}, "min"),
            ({"type": "enum", "values": ["a"], "max": 3}, "max"),
            ({"type": "enum", "values": ["a", " "]}, "values"),
        ):
            with self.subTest(spec=spec):
                params = {**VERSUS_TEMPLATE["params"], "EXTRA": spec}
                self.refused(valid(params=params, task="Decide: ${TOPIC} ${EXTRA}"), "EXTRA")

    def test_an_int_parameter_without_min_reaches_down_to_the_negative_floor(self) -> None:
        params = {**VERSUS_TEMPLATE["params"], "DELTA": {"type": "int", "max": -5, "default": -7}}
        self.assertEqual(ft.validate_template(valid(params=params, task="Decide: ${TOPIC} ${DELTA}")), [])
        self.assertEqual(ft._max_len({"type": "int"}), len(str(-10 ** 9)))
        self.refused(valid(params={**params, "DELTA": {"type": "int", "max": -10 ** 9 - 1}},
                           task="Decide: ${TOPIC} ${DELTA}"), "DELTA")

    def test_a_blank_stance_is_refused(self) -> None:
        self.refused(valid(stances={"side:A": "   ", "side:B": "b"}), "stances.side:A")

    def test_too_many_parameters_are_refused(self) -> None:
        params = {f"P{i}": {"type": "string"} for i in range(ft.MAX_PARAMS + 1)}
        self.refused(valid(params=params, task="x", stances={"side:A": "a", "side:B": "b"}), "params")

    def test_stances_belong_to_a_versus_flow_and_both_sides_are_required_there(self) -> None:
        self.refused(valid(stances={"side:A": "a"}), "stances")
        self.refused(valid(stances={"side:A": "a", "side:B": "b", "side:C": "c"}), "stances")
        self.refused({**DEBATE_TEMPLATE, "stances": {"side:A": "a", "side:B": "b"}}, "versus")

    def test_a_stale_reviewed_date_and_an_unused_parameter_are_warnings_not_errors(self) -> None:
        old = (date.today() - timedelta(days=ft.DEFAULT_MAX_AGE_DAYS + 5)).isoformat()
        warnings = ft.validate_template(valid(reviewed=old))
        self.assertTrue(any("reviewed" in w for w in warnings))
        spare = {**VERSUS_TEMPLATE["params"], "SPARE": {"type": "string"}}
        self.assertTrue(any("SPARE" in w for w in ft.validate_template(valid(params=spare))))
        self.refused(valid(reviewed="last week"), "reviewed")


class ParamTests(unittest.TestCase):
    def test_values_are_checked_against_their_specs_and_defaults_fill_in(self) -> None:
        checked = ft.check_params(DEBATE_TEMPLATE, {"CHANGE": "x", "LINES": "3"})
        self.assertEqual(checked, {"CHANGE": "x", "DEPTH": "quick", "LINES": 3})
        self.assertEqual(ft.check_params(DEBATE_TEMPLATE, {"CHANGE": "x"})["LINES"], 2)

    def test_bad_values_name_the_parameter(self) -> None:
        for values, fragment in (
            ({**VALUES, "TOPIC": "  "}, "TOPIC"),
            ({**VALUES, "STANCE_A": "x" * 151}, "STANCE_A"),
            ({**VALUES, "STANCE_A": "two\nlines"}, "STANCE_A"),
            ({**VALUES, "OTHER": "x"}, "OTHER"),
            ({"TOPIC": "t", "STANCE_A": "a"}, "STANCE_B"),  # required, no default
        ):
            with self.subTest(values=values):
                with self.assertRaises(ft.TemplateValidationError) as caught:
                    ft.check_params(valid(), values)
                self.assertIn(fragment, str(caught.exception))

    def test_an_enum_and_an_int_accept_only_what_they_declare(self) -> None:
        self.assertEqual(ft.check_params(DEBATE_TEMPLATE, {"CHANGE": "x"})["DEPTH"], "quick")
        self.assertEqual(ft.check_params(DEBATE_TEMPLATE, {"CHANGE": "x", "LINES": " +3 "})["LINES"], 3)
        for bad in ({"DEPTH": "bottomless"}, {"LINES": "9"}, {"LINES": "0"}, {"LINES": "many"}, {"LINES": "1.5"},
                    {"LINES": 2.9}, {"LINES": "２"}, {"LINES": "0_2"}, {"LINES": "9" * 5000}, {"LINES": float("inf")}):
            with self.subTest(bad=bad), self.assertRaises(ft.TemplateValidationError) as caught:
                ft.check_params(DEBATE_TEMPLATE, {"CHANGE": "x", **bad})
            self.assertIn(next(iter(bad)), str(caught.exception))

    def test_rendering_is_one_pass_so_a_value_cannot_inject_a_placeholder(self) -> None:
        values = ft.check_params(valid(), {**VALUES, "TOPIC": "${STANCE_A} and ${STANCE_B}"})
        self.assertEqual(ft.render("Decide: ${TOPIC}", values), "Decide: ${STANCE_A} and ${STANCE_B}")

    def test_param_arguments_split_on_the_first_equals_and_reject_repeats(self) -> None:
        self.assertEqual(ft.parse_param_args(["TOPIC=a=b", "X=1"]), {"TOPIC": "a=b", "X": "1"})
        for bad in (["topic=a"], ["TOPIC"], ["A=1", "A=2"], ["TOPIC\n=a"]):
            with self.subTest(bad=bad), self.assertRaises(ft.TemplateValidationError):
                ft.parse_param_args(bad)


class ResolveTests(unittest.TestCase):
    def test_resolve_renders_the_task_and_stances_and_names_the_hash(self) -> None:
        run, warnings = ft.resolve(valid(), VALUES)
        self.assertEqual(warnings, [])
        self.assertEqual(run["task"], "Decide: one hub or two")
        self.assertEqual(run["stances"], {"A": "Defend the single hub", "B": "Defend two hubs"})
        self.assertEqual((run["team"], run["kind"], run["max_rounds"]), ("versusteam", "versus", 1))
        self.assertEqual(run["hash"], ft.content_hash(valid()))

    def test_stances_are_stripped_like_a_team_stance(self) -> None:
        run, _ = ft.resolve(valid(stances={"side:A": "  Defend ${STANCE_A}\n", "side:B": "${STANCE_B} "}), VALUES)
        self.assertEqual(run["stances"], {"A": "Defend the single hub", "B": "two hubs"})

    def test_a_chosen_team_replaces_the_default_and_a_missing_team_is_refused(self) -> None:
        self.assertEqual(ft.resolve(valid(), VALUES, team="other")[0]["team"], "other")
        with self.assertRaises(ft.TemplateValidationError):
            ft.resolve({k: v for k, v in valid().items() if k != "team"}, VALUES)

    def test_a_project_kind_the_template_does_not_list_is_refused_with_the_list(self) -> None:
        with self.assertRaises(ft.TemplateValidationError) as caught:
            ft.resolve(valid(), VALUES, project_kind="ops")
        self.assertIn("code", str(caught.exception))
        self.assertIn("ops", str(caught.exception))


class ConfigOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "builtin").mkdir()
        self.store = ft.FlowTemplateStore(root / "custom", root / "builtin")
        self.teams = TeamManager(root / "teams")
        self.teams.save(VERSUS)

    def test_save_report_delete_round_trip(self) -> None:
        code, payload = config_ops.save_flow_template(self.store, json.dumps(valid()))
        self.assertEqual((code, payload["ok"], payload["id"]), (0, True, "design-decision"))
        code, report = config_ops.flow_templates_report(self.store, self.teams)
        entry = report["templates"][0]
        self.assertEqual((code, entry["id"], entry["source"], entry["errors"]), (0, "design-decision", "custom", []))
        self.assertEqual(entry["resolved"]["kind"], "versus")
        self.assertIn("versus", report["catalog"]["step_kinds"])
        self.assertEqual(config_ops.delete_flow_template(self.store, "design-decision")[0], 0)
        self.assertEqual(config_ops.delete_flow_template(self.store, "design-decision")[0], config_ops.NOT_FOUND)

    def test_invalid_input_is_exit_2_and_a_builtin_delete_is_refused(self) -> None:
        self.assertEqual(config_ops.save_flow_template(self.store, "{not json")[0], config_ops.INVALID)
        code, payload = config_ops.save_flow_template(self.store, json.dumps(valid(steps=[])))
        self.assertEqual(code, config_ops.INVALID)
        self.assertIn("steps", payload["error"])
        (Path(self.tmp.name) / "builtin" / "design-decision.json").write_text(json.dumps(valid()))
        self.assertEqual(config_ops.delete_flow_template(self.store, "design-decision")[0], config_ops.INVALID)

    def test_the_report_warns_about_a_missing_team_or_one_of_another_flow_kind(self) -> None:
        self.teams.save(BASE)  # a debate team
        self.store.save(valid(team="flowteam"))
        self.store.save(valid(id="lost", team="ghost"))
        report = config_ops.flow_templates_report(self.store, self.teams)[1]
        warnings = {t["id"]: t["warnings"] for t in report["templates"]}
        self.assertTrue(any("debate flow, not versus" in w for w in warnings["design-decision"]))
        self.assertTrue(any("ghost" in w for w in warnings["lost"]))

    def test_a_broken_stored_file_is_reported_not_raised(self) -> None:
        (self.store.directory).mkdir(parents=True)
        (self.store.directory / "broken.json").write_text(json.dumps({"id": "broken", "steps": []}))
        entry = config_ops.flow_templates_report(self.store, self.teams)[1]["templates"][0]
        self.assertTrue(entry["errors"])
        self.assertIsNone(entry["resolved"])


class TemplateRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls: list = []
        self.lock = threading.Lock()
        manager = TeamManager(self.root / "teams")
        manager.save(VERSUS)
        manager.save(BASE)
        self.engine = DebateEngine(db_path=self.root / "d.db", events=EventWriter(str(self.root / "events.jsonl")),
                                   team_mgr=manager)

    def fake(self, prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
        prompt = prompt + (instructions.text() if instructions else "")
        with self.lock:
            self.calls.append({"prompt": prompt, "providers": providers})
            n = len(self.calls)
        if JUDGE_VERDICT_RULE in prompt:
            return LLMResult("claude-cli", "Not yet.\nVERDICT: REJECTED", f"s{n}")
        return LLMResult(f"{providers[0]}-cli", f"text from {providers[0]}", f"s{n}")

    def start(self, template: dict, values: dict, team=None, **kw):
        run, _ = ft.resolve(template, values, team=team)
        with patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake), \
                patch.object(self.engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            try:
                return self.engine.run_debate(run["task"], project_id="server-guardian", team=run["team"],
                                              template_run=run, **kw)
            finally:
                self.engine.events.close()

    def events(self) -> list:
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def test_the_stances_of_the_template_replace_those_of_the_team(self) -> None:
        result = self.start(valid(), VALUES)
        openings = [c["prompt"] for c in self.calls[:2]]
        self.assertTrue(any("Defend the single hub" in p for p in openings))
        self.assertTrue(any("Defend two hubs" in p for p in openings))
        self.assertFalse(any("Keep the single hub." in c["prompt"] for c in self.calls))
        self.assertTrue(any("Decide: one hub or two" in c["prompt"] for c in self.calls))
        self.assertEqual(result["team"]["template"]["id"], "design-decision")
        started = next(e for e in self.events() if e["type"] == "debate_started")["data"]
        self.assertEqual((started["flow"], started["template"]), ("versus", "design-decision"))
        self.assertEqual([s["stance"] for s in started["sides"]], ["Defend the single hub", "Defend two hubs"])

    def test_the_snapshot_records_template_id_hash_and_values(self) -> None:
        snapshot = self.start(valid(), VALUES)["team"]["template"]
        self.assertEqual(snapshot["hash"], ft.content_hash(valid()))
        self.assertEqual(snapshot["params"]["TOPIC"], "one hub or two")
        self.assertEqual(set(snapshot["params"]), {"TOPIC", "STANCE_A", "STANCE_B"})
        self.assertEqual(snapshot["stances"]["A"], "Defend the single hub")

    def test_a_team_of_another_flow_kind_is_refused_before_any_agent_runs(self) -> None:
        with self.assertRaises(TeamValidationError) as caught:
            self.start(valid(), VALUES, team="flowteam")
        self.assertIn("versus flow but team 'flowteam' runs debate", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_project_kind_outside_the_template_is_refused(self) -> None:
        with self.assertRaises(TeamValidationError) as caught:
            self.start(valid(projects=["ops"]), VALUES)
        self.assertIn("not on code projects", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_template_cannot_continue_an_existing_thread(self) -> None:
        first = self.start(valid(), VALUES)
        self.calls.clear()
        self.engine.events = EventWriter(str(self.root / "events2.jsonl"))
        with self.assertRaises(TeamValidationError) as caught:
            self.start(valid(), VALUES, thread_id=first["thread_id"])
        self.assertIn("new thread", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_a_run_without_a_template_has_no_template_in_snapshot_or_event(self) -> None:
        with patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake), \
                patch.object(self.engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            result = self.engine.run_debate("task", project_id="server-guardian", team="versusteam")
        self.engine.events.close()
        self.assertNotIn("template", result["team"])
        started = next(e for e in self.events() if e["type"] == "debate_started")["data"]
        self.assertNotIn("template", started)


if __name__ == "__main__":
    unittest.main()

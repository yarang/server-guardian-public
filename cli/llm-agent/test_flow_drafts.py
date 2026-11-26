"""Session flow drafts (docs/design/session-flows.md, phase SF1): the short form and its refusals, the conversion
to a flow the engine runs, the per-workstation draft store, promotion into the operator flow store and the CLI
flags the hub API will relay."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import config_ops
import flow_drafts
import flow_graph as fg
import session_registry
from flow_drafts import DraftError
from persona_manager import PersonaManager
from test_session_registry import SessionRegistryTestCase
from testenv import isolate_data_dir, restore_data_dir

HERE = Path(__file__).resolve().parent
WS = "ws-mac-1"

# Drafts of every flow kind over builtin personas (no providers, hosts or tools: SD3).
DEBATE = {"id": "api-review", "name": "API review", "kind": "debate", "lead": "domain_architect",
          "critics": ["critic_redteam", "devops_specialist"], "proposers": [], "close": "jev_mcp_decision",
          "max_rounds": 2, "category": "development", "projects": ["code"]}
PANEL = {"id": "blind-panel", "name": "Blind panel", "kind": "panel", "lead": "domain_architect",
         "proposers": ["devops_specialist", "ops_sre"], "close": "jev_mcp_decision"}
VERIFY = {"id": "release-check", "name": "Release check", "kind": "verify", "lead": "domain_architect",
          "critics": ["guardian_qa_critic", "guardian_security_critic"], "close": "jev_mcp_decision"}
PIPELINE = {"id": "staged-fix", "name": "Staged fix", "kind": "pipeline",
            "stages": ["devops_specialist", "ops_sre", "domain_architect"], "lead": "domain_architect",
            "critics": ["critic_redteam"], "close": "jev_mcp_decision"}
VERSUS = {"id": "hub-split", "name": "Hub split", "kind": "versus",
          "sides": [{"character": "ops_sre", "stance": "Keep the single hub."},
                    {"character": "domain_architect", "stance": "Split into two hubs."}],
          "exchanges": 2, "close": "jev_mcp_decision"}
BY_KIND = {draft["kind"]: draft for draft in (DEBATE, PANEL, VERIFY, PIPELINE, VERSUS)}


class DraftTestCase(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()
        self.addCleanup(restore_data_dir)
        self.personas = PersonaManager()


class ConversionTests(DraftTestCase):
    def test_every_kind_converts_validates_and_runs(self) -> None:
        for kind, draft in BY_KIND.items():
            with self.subTest(kind=kind):
                result = flow_drafts.check(draft, self.personas)
                self.assertTrue(result["ok"], result.get("error"))
                self.assertNotIn("converted_from", result["flow"])   # a draft is not a team
                self.assertTrue(result["runnable"], result["run_error"])
                self.assertIsNone(result["run_error"])
                self.assertGreaterEqual(result["calls"], 3)

    def test_the_name_category_and_projects_are_carried_over(self) -> None:
        flow, _ = flow_drafts.convert(DEBATE, self.personas)
        self.assertEqual((flow["id"], flow["name"], flow["category"], flow["projects"]),
                         (DEBATE["id"], "API review", "development", ["code"]))
        bare, _ = flow_drafts.convert(PANEL, self.personas)
        self.assertEqual(bare["name"], "Blind panel")
        self.assertNotIn("category", bare)   # left out when the draft names none
        self.assertNotIn("projects", bare)

    def test_the_team_dict_uses_plain_members_and_the_flow_object(self) -> None:
        team = flow_drafts.to_team(DEBATE)
        self.assertEqual(team["expert"], "domain_architect")
        self.assertEqual(team["critic"], ["critic_redteam", "devops_specialist"])
        self.assertEqual(team["proposers"], [])
        self.assertEqual(team["judge"], "jev_mcp_decision")
        self.assertEqual(team["max_rounds"], 2)
        self.assertNotIn("flow", team)   # a debate needs no flow object
        self.assertEqual(flow_drafts.to_team(PANEL)["flow"], {"kind": "panel"})
        self.assertEqual(flow_drafts.to_team(VERIFY)["flow"], {"kind": "verify"})
        pipeline = flow_drafts.to_team(PIPELINE)
        self.assertEqual(pipeline["flow"]["kind"], "pipeline")
        self.assertEqual(pipeline["flow"]["stages"], PIPELINE["stages"])
        self.assertNotIn("proposers", pipeline)
        versus = flow_drafts.to_team(VERSUS)
        self.assertEqual(versus["expert"], "ops_sre")   # side A stands in; lineup_flow ignores it
        self.assertEqual(versus["flow"]["sides"], [{"persona": "ops_sre", "stance": "Keep the single hub."},
                                                   {"persona": "domain_architect", "stance": "Split into two hubs."}])
        self.assertEqual(versus["flow"]["exchanges"], 2)
        self.assertNotIn("critic", versus)

    def test_unknown_fields_are_refused_including_smuggled_member_fields(self) -> None:
        for draft, message in (({"foo": 1}, "foo: unknown field"),
                               ({**DEBATE, "providers": ["claude"]}, "providers: unknown field"),
                               ({**DEBATE, "host": "worker:any"}, "host: unknown field"),
                               ({**DEBATE, "allowed_tools": []}, "allowed_tools: unknown field"),
                               ("not an object", "a draft is a JSON object")):
            with self.subTest(message=message):
                result = flow_drafts.check(draft, self.personas)
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])

    def test_a_field_another_kind_takes_is_refused(self) -> None:
        for draft, message in (({**PANEL, "critics": ["critic_redteam"]},
                                "draft kind 'panel' does not take 'critics'"),
                               ({**VERSUS, "lead": "domain_architect"},
                                "draft kind 'versus' does not take 'lead'"),
                               ({**DEBATE, "stages": ["ops_sre", "ops_sre"]},
                                "draft kind 'debate' does not take 'stages'"),
                               ({**VERIFY, "proposers": ["ops_sre"]},
                                "draft kind 'verify' does not take 'proposers'")):
            with self.subTest(message=message):
                result = flow_drafts.check(draft, self.personas)
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])

    def test_a_seat_holds_a_plain_character_id_only(self) -> None:
        for draft, where in ((DEBATE, "lead"), (DEBATE, "close")):
            broken = {**draft, where: {"persona": "domain_architect", "providers": ["claude"]}}
            result = flow_drafts.check(broken, self.personas)
            self.assertFalse(result["ok"])
            self.assertIn(f"seat {where}: character id must be a string", result["error"])
        critic_dict = {**DEBATE, "critics": ["critic_redteam", {"persona": "ops_sre"}]}
        self.assertIn("seat critics[1]: character id must be a string",
                      flow_drafts.check(critic_dict, self.personas)["error"])
        self.assertIn("seat lead: a character id is missing",
                      flow_drafts.check({k: v for k, v in DEBATE.items() if k != "lead"}, self.personas)["error"])

    def test_wrong_counts_are_refused(self) -> None:
        cases = [
            ({**DEBATE, "critics": []}, "critics: 1-4 characters, got 0"),
            ({**DEBATE, "critics": ["a", "b", "c", "d", "e"]}, "critics: 1-4 characters, got 5"),
            ({**VERIFY, "critics": []}, "critics: 1-4 characters, got 0"),
            ({**PIPELINE, "stages": ["ops_sre"]}, "stages: 2-4 characters, got 1"),
            ({**PIPELINE, "stages": ["a", "b", "c", "d", "e"]}, "stages: 2-4 characters, got 5"),
            ({**VERSUS, "sides": VERSUS["sides"][:1]}, "sides: exactly 2 sides, got 1"),
            ({**VERSUS, "sides": VERSUS["sides"] + [VERSUS["sides"][0]]}, "sides: exactly 2 sides, got 3"),
            ({**VERSUS, "exchanges": 0}, "exchanges: a whole number from 1 to 2"),
            ({**VERSUS, "exchanges": 3}, "exchanges: a whole number from 1 to 2"),
            ({**DEBATE, "proposers": ["ops_sre", "devops_specialist", "ops_sre"]},
             "proposers: 0-2 characters, got 3"),
            ({**PANEL, "proposers": []}, "proposers: 1-2 characters, got 0"),
        ]
        for draft, message in cases:
            with self.subTest(message=message):
                result = flow_drafts.check(draft, self.personas)
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])

    def test_bad_sides_are_refused(self) -> None:
        empty = {**VERSUS, "sides": [{"character": "ops_sre", "stance": " "},
                                     {"character": "domain_architect", "stance": "Split."}]}
        self.assertIn("sides[0].stance: expected the position this side defends",
                      flow_drafts.check(empty, self.personas)["error"])
        long = {**VERSUS, "sides": [{"character": "ops_sre", "stance": "x" * 401},
                                    {"character": "domain_architect", "stance": "Split."}]}
        self.assertIn("sides[0].stance: at most 400 characters (got 401)",
                      flow_drafts.check(long, self.personas)["error"])
        extra = {**VERSUS, "sides": [{"character": "ops_sre", "stance": "Keep.", "providers": ["claude"]},
                                     {"character": "domain_architect", "stance": "Split."}]}
        self.assertIn('sides[0]: expected {"character": "<id>", "stance": "<text>"} and no other keys',
                      flow_drafts.check(extra, self.personas)["error"])

    def test_bad_header_fields_are_refused(self) -> None:
        cases = [
            ({**DEBATE, "id": "Bad/Id"}, "id: use 1-64"),
            ({**DEBATE, "id": ""}, "id: use 1-64"),
            ({**DEBATE, "name": "x" * 81}, "name: a short name"),
            ({**DEBATE, "name": " "}, "name: a short name"),
            ({**DEBATE, "kind": "roundtable"}, "kind: unknown flow"),
            ({**DEBATE, "max_rounds": True}, "max_rounds: a whole number from 1 to 10"),
            ({**DEBATE, "max_rounds": 0}, "max_rounds: a whole number from 1 to 10"),
            ({**DEBATE, "max_rounds": 11}, "max_rounds: a whole number from 1 to 10"),
            ({**DEBATE, "category": "fun"}, "category: use one of"),
            ({**DEBATE, "projects": ["server-guardian"]}, "projects: a list of project kinds"),
            ({**DEBATE, "projects": []}, "projects: a list of project kinds"),
            ({**DEBATE, "projects": ["code", "code"]}, "projects: a list of project kinds"),
        ]
        for draft, message in cases:
            with self.subTest(message=message):
                result = flow_drafts.check(draft, self.personas)
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])

    def test_an_unknown_character_is_refused_by_the_flow_check(self) -> None:
        result = flow_drafts.check({**DEBATE, "lead": "ghost"}, self.personas)
        self.assertFalse(result["ok"])
        self.assertIn("unknown character 'ghost'", result["error"])
        self.assertEqual(flow_drafts.check(DEBATE, None)["flow"]["seats"][0]["character"], "domain_architect")


class PromotedFromTests(DraftTestCase):
    def test_a_promoted_flow_passes_validate_flow(self) -> None:
        flow, _ = flow_drafts.convert(DEBATE, self.personas)
        flow["promoted_from"] = {"workstation": WS, "draft": "api-review", "at": "2026-11-26T10:00:00Z"}
        fg.validate_flow(flow, self.personas)

    def test_validate_flow_refuses_a_malformed_promoted_from(self) -> None:
        base = {"workstation": WS, "draft": "api-review", "at": "2026-11-26T10:00:00Z"}
        cases = [
            ({k: v for k, v in base.items() if k != "at"}, "promoted_from:"),
            ({**base, "extra": 1}, "promoted_from:"),
            ({**base, "workstation": "../x"}, "promoted_from.workstation"),
            ({**base, "draft": "Bad/Id"}, "promoted_from.draft"),
            ({**base, "at": "2026-11-26T10:00:00+00:00"}, "promoted_from.at"),
            ({**base, "at": 1234}, "promoted_from.at"),
        ]
        for promoted, message in cases:
            with self.subTest(promoted=promoted):
                flow, _ = flow_drafts.convert(DEBATE, self.personas)
                flow["promoted_from"] = promoted
                with self.assertRaises(fg.FlowValidationError) as ctx:
                    fg.validate_flow(flow, self.personas)
                self.assertIn(message, str(ctx.exception))


class StoreTests(DraftTestCase):
    def test_save_stamps_and_lists_by_workstation(self) -> None:
        record = flow_drafts.save_draft(WS, DEBATE, self.personas)
        self.assertEqual(record["workstation"], WS)
        self.assertEqual(record["draft"], DEBATE)   # the short form as given
        self.assertEqual(record["flow"]["id"], "api-review")
        self.assertTrue(record["runnable"])
        self.assertRegex(record["created"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(record["modified"], record["created"])
        path = flow_drafts.drafts_dir() / WS / "api-review.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)

        flow_drafts.save_draft("ws-other", {**PANEL, "id": "aaa-panel"}, self.personas)
        listed = flow_drafts.list_drafts()
        self.assertEqual([(r["workstation"], r["draft"]["id"]) for r in listed],
                         [(WS, "api-review"), ("ws-other", "aaa-panel")])
        self.assertEqual([r["draft"]["id"] for r in flow_drafts.list_drafts(WS)], ["api-review"])
        self.assertEqual(flow_drafts.get_draft(WS, "api-review")["draft"]["id"], "api-review")

    def test_a_re_save_keeps_created_and_an_invalid_draft_is_refused(self) -> None:
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        edited = flow_drafts.save_draft(WS, {**DEBATE, "name": "API review, again"}, self.personas)
        self.assertEqual(edited["created"], flow_drafts.get_draft(WS, "api-review")["created"])
        self.assertEqual(edited["draft"]["name"], "API review, again")
        with self.assertRaises(DraftError):
            flow_drafts.save_draft(WS, {**DEBATE, "kind": "nope"}, self.personas)
        self.assertEqual(len(flow_drafts.list_drafts(WS)), 1)

    def test_the_cap_is_twenty_new_ids_per_workstation(self) -> None:
        for index in range(flow_drafts.MAX_DRAFTS_PER_WORKSTATION):
            flow_drafts.save_draft(WS, {**DEBATE, "id": f"d{index:02d}"}, self.personas)
        with self.assertRaises(DraftError) as ctx:
            flow_drafts.save_draft(WS, {**DEBATE, "id": "one-too-many"}, self.personas)
        self.assertIn("20", str(ctx.exception))
        flow_drafts.save_draft(WS, {**DEBATE, "id": "d05", "name": "Edited"}, self.personas)   # a re-save passes
        flow_drafts.save_draft("ws-other", DEBATE, self.personas)   # another workstation starts empty

    def test_bad_workstation_and_draft_ids_are_refused_before_any_path(self) -> None:
        for bad_ws in ("../x", "", "operator", "hub", "ws one", None, 7):
            with self.subTest(ws=bad_ws):
                with self.assertRaises(DraftError):
                    flow_drafts.save_draft(bad_ws, DEBATE, self.personas)
        for bad_ws in ("../x", "", "operator", "hub", "ws one", 7):   # None means "every workstation"
            with self.subTest(ws=bad_ws):
                with self.assertRaises(DraftError):
                    flow_drafts.list_drafts(bad_ws)
        self.assertIsNone(flow_drafts.get_draft(WS, "Bad/Id"))
        self.assertFalse(flow_drafts.delete_draft(WS, "Bad/Id"))

    def test_delete_draft_and_delete_workstation(self) -> None:
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        flow_drafts.save_draft(WS, PANEL, self.personas)
        self.assertTrue(flow_drafts.delete_draft(WS, DEBATE["id"]))
        self.assertFalse(flow_drafts.delete_draft(WS, DEBATE["id"]))
        self.assertIsNone(flow_drafts.get_draft(WS, DEBATE["id"]))
        self.assertEqual(flow_drafts.delete_workstation(WS), 1)
        self.assertEqual(flow_drafts.list_drafts(WS), [])
        self.assertEqual(flow_drafts.delete_workstation(WS), 0)   # absent: no error
        self.assertFalse(flow_drafts.drafts_dir().joinpath(WS).exists())

    def test_unreadable_files_are_skipped(self) -> None:
        directory = flow_drafts.drafts_dir() / WS
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{", encoding="utf-8")
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        self.assertEqual([r["draft"]["id"] for r in flow_drafts.list_drafts(WS)], ["api-review"])


class PromotionTests(DraftTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.builtin = root / "builtin"
        self.builtin.mkdir()
        self.store = fg.FlowStore(root / "custom", self.builtin, self.personas)
        builtin_flow, _ = flow_drafts.convert(DEBATE, self.personas)
        builtin_flow["id"] = "builtin-flow"
        (self.builtin / "builtin-flow.json").write_text(json.dumps(builtin_flow), encoding="utf-8")

    def test_promote_saves_the_flow_and_removes_the_draft(self) -> None:
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        result = flow_drafts.promote(WS, DEBATE["id"], self.store, self.personas)
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "api-review")
        saved = self.store.get("api-review")
        self.assertEqual(saved["source"], "custom")
        self.assertEqual(saved["promoted_from"]["workstation"], WS)
        self.assertEqual(saved["promoted_from"]["draft"], "api-review")
        self.assertRegex(saved["promoted_from"]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("created", saved)
        self.assertIsNone(flow_drafts.get_draft(WS, DEBATE["id"]))
        with self.assertRaises(DraftError):   # gone: a second promotion is refused
            flow_drafts.promote(WS, DEBATE["id"], self.store, self.personas)

    def test_an_existing_custom_flow_is_refused_unless_overwrite(self) -> None:
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        flow_drafts.promote(WS, DEBATE["id"], self.store, self.personas)
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        with self.assertRaises(DraftError) as ctx:
            flow_drafts.promote(WS, DEBATE["id"], self.store, self.personas)
        self.assertIn("already exists", str(ctx.exception))
        again = flow_drafts.promote(WS, DEBATE["id"], self.store, self.personas, overwrite=True)
        self.assertTrue(again["ok"])

    def test_a_builtin_flow_id_is_always_refused(self) -> None:
        flow_drafts.save_draft(WS, {**DEBATE, "id": "builtin-flow"}, self.personas)
        for overwrite in (False, True):
            with self.assertRaises(DraftError) as ctx:
                flow_drafts.promote(WS, "builtin-flow", self.store, self.personas, overwrite=overwrite)
            self.assertIn("builtin", str(ctx.exception))
        self.assertEqual(self.store.get("builtin-flow")["source"], "builtin")
        self.assertIsNotNone(flow_drafts.get_draft(WS, "builtin-flow"))   # the draft stays

    def test_a_builtin_id_shadowed_by_a_custom_flow_is_still_refused(self) -> None:
        shadow, _ = flow_drafts.convert(DEBATE, self.personas)
        shadow["id"] = "builtin-flow"
        self.store.save(shadow)
        self.assertEqual(self.store.get("builtin-flow")["source"], "custom")
        flow_drafts.save_draft(WS, {**DEBATE, "id": "builtin-flow"}, self.personas)
        with self.assertRaises(DraftError) as ctx:
            flow_drafts.promote(WS, "builtin-flow", self.store, self.personas, overwrite=True)
        self.assertIn("builtin", str(ctx.exception))

    def test_promote_as_saves_under_another_id(self) -> None:
        flow_drafts.save_draft(WS, DEBATE, self.personas)
        result = flow_drafts.promote(WS, DEBATE["id"], self.store, self.personas, as_id="review-flow")
        self.assertEqual(result["id"], "review-flow")
        self.assertEqual(self.store.get("review-flow")["promoted_from"]["draft"], "api-review")
        self.assertIsNone(flow_drafts.get_draft(WS, DEBATE["id"]))
        with self.assertRaises(DraftError):   # a bad target id is refused
            flow_drafts.promote(WS, PANEL["id"], self.store, self.personas, as_id="Bad/Id")


class ConfigOpsTests(DraftTestCase):
    def test_check_save_list_delete_and_the_exit_codes(self) -> None:
        code, payload = config_ops.check_flow_draft(json.dumps(DEBATE), self.personas)
        self.assertEqual((code, payload["ok"], payload["runnable"]), (0, True, True))
        code, payload = config_ops.check_flow_draft(json.dumps({**DEBATE, "kind": "nope"}), self.personas)
        self.assertEqual(code, config_ops.INVALID)
        self.assertFalse(payload["ok"])
        self.assertIn("kind", payload["error"])
        self.assertEqual(config_ops.check_flow_draft("{", self.personas)[0], config_ops.INVALID)

        code, payload = config_ops.save_flow_draft(json.dumps(DEBATE), WS, self.personas)
        self.assertEqual((code, payload["ok"], payload["id"], payload["workstation"]), (0, True, "api-review", WS))
        code, payload = config_ops.list_flow_drafts()
        self.assertEqual(code, 0)
        self.assertEqual([(d["workstation"], d["draft"]["id"]) for d in payload["drafts"]], [(WS, "api-review")])
        self.assertEqual(config_ops.list_flow_drafts(WS)[1]["drafts"][0]["draft"]["id"], "api-review")
        self.assertEqual(config_ops.delete_flow_draft(WS, "api-review")[0], 0)
        self.assertEqual(config_ops.delete_flow_draft(WS, "api-review")[0], config_ops.NOT_FOUND)
        self.assertEqual(config_ops.delete_flow_draft("../x", "api-review")[0], config_ops.INVALID)

    def test_promote_exit_codes(self) -> None:
        store = fg.FlowStore()
        self.assertEqual(config_ops.promote_flow_draft(WS, "ghost", store, self.personas)[0], config_ops.NOT_FOUND)
        config_ops.save_flow_draft(json.dumps(DEBATE), WS, self.personas)
        code, payload = config_ops.promote_flow_draft(WS, DEBATE["id"], store, self.personas)
        self.assertEqual((code, payload["ok"], payload["id"]), (0, True, "api-review"))
        self.assertEqual(store.get("api-review")["source"], "custom")


class CliTests(DraftTestCase):
    """guardian.py the way the hub API runs it: JSON on stdout, exit 2 on a refused draft."""

    def run_cli(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], input=stdin,
                              env=dict(os.environ), capture_output=True, text=True, timeout=120)

    def test_check_ok_and_invalid(self) -> None:
        done = self.run_cli("--flow-draft-check", stdin=json.dumps(VERSUS))
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["runnable"])
        self.assertEqual(payload["flow"]["id"], "hub-split")
        self.assertEqual(payload["calls"], 7)   # two sides, 2 exchanges of two answers, the close

        done = self.run_cli("--flow-draft-check", stdin=json.dumps({**VERSUS, "exchanges": 3}))
        self.assertEqual(done.returncode, 2)
        self.assertFalse(json.loads(done.stdout)["ok"])

    def test_save_needs_workstation_and_lists(self) -> None:
        done = self.run_cli("--flow-draft-save", stdin=json.dumps(DEBATE))
        self.assertEqual(done.returncode, 2)
        self.assertIn("--workstation", json.loads(done.stdout)["error"])

        done = self.run_cli("--flow-draft-save", "--workstation", WS, stdin=json.dumps(DEBATE))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)["ok"])

        listed = json.loads(self.run_cli("--flow-drafts").stdout)
        self.assertEqual([(d["workstation"], d["draft"]["id"]) for d in listed["drafts"]], [(WS, "api-review")])
        mine = json.loads(self.run_cli("--flow-drafts", "--workstation", WS).stdout)
        self.assertEqual([d["draft"]["id"] for d in mine["drafts"]], ["api-review"])
        self.assertEqual(json.loads(self.run_cli("--flow-drafts", "--workstation", "ws-other").stdout)["drafts"], [])

        done = self.run_cli("--flow-draft-delete", DEBATE["id"], "--workstation", WS)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(self.run_cli("--flow-drafts", "--workstation", WS).stdout)["drafts"], [])

    def test_promote_over_the_cli(self) -> None:
        self.run_cli("--flow-draft-save", "--workstation", WS, stdin=json.dumps(DEBATE))
        done = self.run_cli("--flow-draft-promote", DEBATE["id"], "--workstation", WS, "--promote-as", "review-flow")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual((payload["ok"], payload["id"]), (True, "review-flow"))
        store = fg.FlowStore(persona_mgr=self.personas)
        self.assertEqual(store.get("review-flow")["promoted_from"]["workstation"], WS)
        self.assertEqual(json.loads(self.run_cli("--flow-drafts", "--workstation", WS).stdout)["drafts"], [])


class RemoveWorkstationDraftsTests(SessionRegistryTestCase):
    """SD6: session_registry.remove_workstation deletes the workstation's drafts with it."""

    def test_removal_deletes_the_workstation_drafts(self) -> None:
        self.add(ws_id=WS)
        personas = PersonaManager()
        flow_drafts.save_draft(WS, DEBATE, personas)
        flow_drafts.save_draft(WS, PANEL, personas)
        self.assertEqual(len(flow_drafts.list_drafts(WS)), 2)

        session_registry.remove_workstation(WS)

        self.assertEqual(flow_drafts.list_drafts(WS), [])
        self.assertFalse(flow_drafts.drafts_dir().joinpath(WS).exists())


if __name__ == "__main__":
    unittest.main()

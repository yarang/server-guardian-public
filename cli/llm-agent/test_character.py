"""Character files, persona format 2 (docs/design/agent-characters.md, C2): rendering, validation, seats, tools."""
import copy
import json
import os
import re
import unittest
from typing import Any, Dict
from unittest.mock import patch

import character
import config_ops
import persona_lint
from debate_engine import DebateEngine
from persona_manager import PersonaManager, PersonaValidationError, validate_persona
from role_cards import card, persona_text
from solo_agent import SoloError, resolve_persona
from team_manager import TeamValidationError, default_lineup, judge_tools, parse_lineup, validate_lineup
from testenv import isolate_data_dir, restore_data_dir

CHARACTER: Dict[str, Any] = {
    "id": "safety_reviewer",
    "format": 2,
    "name": "Safety reviewer",
    "profile": {"tagline": "Knows what an action deletes before it runs"},
    "seats": ["critic", "verifier", "solo"],
    "kinds": ["ops"],
    "role_card": "critic",
    "role_card_kind": "ops",
    "persona": {
        "identity": "데이터 손실 위험을 먼저 보는 운영 변경 검토자",
        "expertise": ["삭제와 되돌림 불가능한 조치", "감사 로그 보존"],
        "stance": "정리가 목적이고, 검토는 삭제와 중단이 의도적인지 확인하는 과정입니다.",
        "checks": ["각 조치가 무엇을 지우거나 멈추는지", "대상 호스트와 sudo"],
        "boundaries": ["보존 기간과 재발 방지는 다른 검토자의 일"],
    },
}


def character_variant(**changes: Any) -> Dict[str, Any]:
    """A copy of the fixture with top-level keys replaced (None removes the key)."""
    data = copy.deepcopy(CHARACTER)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def with_block(**fields: Any) -> Dict[str, Any]:
    """A copy of the fixture whose persona block carries `fields`."""
    data = copy.deepcopy(CHARACTER)
    data["persona"] = {**data["persona"], **fields}
    return data


class RenderTests(unittest.TestCase):
    def test_the_template_with_boundaries(self) -> None:
        self.assertEqual(
            character.render_persona(CHARACTER["persona"]),
            "당신은 데이터 손실 위험을 먼저 보는 운영 변경 검토자입니다.\n"
            "전문 분야: 삭제와 되돌림 불가능한 조치, 감사 로그 보존.\n"
            "관점: 정리가 목적이고, 검토는 삭제와 중단이 의도적인지 확인하는 과정입니다.\n"
            "먼저 확인할 것:\n"
            "- 각 조치가 무엇을 지우거나 멈추는지\n"
            "- 대상 호스트와 sudo\n"
            "맡지 않는 일:\n"
            "- 보존 기간과 재발 방지는 다른 검토자의 일")

    def test_without_boundaries_the_header_and_items_are_dropped(self) -> None:
        block = {**CHARACTER["persona"], "boundaries": []}
        rendered = character.render_persona(block)
        self.assertNotIn("맡지 않는 일", rendered)
        self.assertTrue(rendered.endswith("- 대상 호스트와 sudo"))  # no trailing newline
        self.assertEqual(character.render_persona({**block, "boundaries": None}), rendered)  # missing is the same

    def test_each_value_is_stripped(self) -> None:
        block = {**CHARACTER["persona"], "identity": "  검토자  ", "checks": ["  대상 확인  "]}
        lines = character.render_persona(block).splitlines()
        self.assertIn("당신은 검토자입니다.", lines)
        self.assertIn("- 대상 확인", lines)


class ValidationTests(unittest.TestCase):
    def test_a_valid_character_passes_and_keeps_known_fields(self) -> None:
        self.assertEqual(validate_persona(copy.deepcopy(CHARACTER)), CHARACTER)

    def test_an_id_with_a_trailing_newline_is_refused(self) -> None:
        # "$" also matches just before a trailing newline, so only fullmatch refuses "p\n"
        # (which would otherwise name a persona file "p\n.json").
        legacy = {"id": "p\n", "name": "P", "role": "Critic", "system_prompt": "x"}
        for data in (character_variant(id="safety_reviewer\n"), legacy):
            with self.subTest(format=data.get("format", 1)):
                with self.assertRaisesRegex(PersonaValidationError, "id:"):
                    validate_persona(data)

    def test_the_format_key_must_be_2_or_absent(self) -> None:
        for fmt in (1, 3, "2"):
            with self.subTest(format=fmt):
                with self.assertRaisesRegex(PersonaValidationError, "format: 2 or absent"):
                    validate_persona(character_variant(format=fmt))

    def test_format_1_is_unchanged_and_ignores_character_fields(self) -> None:
        clean = validate_persona({"id": "p", "name": "P", "role": "Critic", "system_prompt": "x",
                                  "seats": ["critic"], "tools": []})
        self.assertEqual(clean, {"id": "p", "name": "P", "role": "Critic", "system_prompt": "x"})

    def test_format_1_fields_are_refused_by_name(self) -> None:
        data = character_variant(system_prompt="x", role="Critic", domain="d", focus_areas=["f"],
                                 critique_focus=["c"], mcp_capabilities=["jev_mcp:run_tests"])
        with self.assertRaises(PersonaValidationError) as ctx:
            validate_persona(data)
        for field in ("system_prompt", "role", "domain", "focus_areas", "critique_focus", "mcp_capabilities"):
            self.assertIn(field, str(ctx.exception))

    def test_unknown_fields_are_refused(self) -> None:
        with self.assertRaisesRegex(PersonaValidationError, "unknown field"):
            validate_persona(character_variant(extra=1))

    def test_limits(self) -> None:
        cases = [
            ({"name": " "}, "name: required"),
            ({"name": "가" * 201}, "name: expected text"),
            ({"persona": {"identity": "가" * 121}}, "persona.identity"),
            ({"persona": {"identity": " "}}, "persona.identity: required"),
            ({"persona": {"stance": "가" * 201}}, "persona.stance"),
            ({"persona": {"stance": ""}}, "persona.stance: required"),
            ({"persona": {"expertise": []}}, "persona.expertise"),
            ({"persona": {"expertise": ["가"] * 5}}, "persona.expertise"),
            ({"persona": {"expertise": ["가" * 61]}}, "persona.expertise"),
            ({"persona": {"checks": []}}, "persona.checks"),
            ({"persona": {"checks": ["가"] * 6}}, "persona.checks"),
            ({"persona": {"checks": ["가" * 81]}}, "persona.checks"),
            ({"persona": {"boundaries": ["가"] * 4}}, "persona.boundaries"),
            ({"persona": {"boundaries": ["가" * 81]}}, "persona.boundaries"),
            ({"persona": {"identity": "x", "extra": 1}}, "persona: an object with"),
        ]
        for changes, fragment in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(PersonaValidationError, re.escape(fragment)):
                    validate_persona(character_variant(**changes))

    def test_seats(self) -> None:
        for seats in ([], ["critic", "critic"], ["wizard"], "critic"):
            with self.subTest(seats=seats):
                with self.assertRaisesRegex(PersonaValidationError, "seats:"):
                    validate_persona(character_variant(seats=seats))

    def test_an_empty_tools_list_is_refused(self) -> None:
        # `tools` is a ceiling, so an empty list is ambiguous: leaving the key out means the whole catalog.
        with self.assertRaisesRegex(PersonaValidationError, "tools: leave the key out for the whole catalog"):
            validate_persona(character_variant(tools=[]))
        self.assertNotIn("tools", validate_persona(character_variant(tools=None)))

    def test_kinds(self) -> None:
        with self.assertRaisesRegex(PersonaValidationError, "kinds:"):
            validate_persona(character_variant(kinds=["oper"]))
        self.assertEqual(validate_persona(character_variant(kinds=[]))["kinds"], [])  # empty means any kind

    def test_role_card_rules(self) -> None:
        with self.assertRaisesRegex(PersonaValidationError, "role_card: required"):
            validate_persona(character_variant(role_card=None))  # seats include critic
        with self.assertRaisesRegex(PersonaValidationError, "not one of the persona's seats"):
            validate_persona(character_variant(seats=["verifier", "solo"]))  # role_card critic is not among them
        with self.assertRaisesRegex(PersonaValidationError, "role_card: one of"):
            validate_persona(character_variant(role_card="verifier"))
        # a seat without a card of its own needs no card
        clean = validate_persona(character_variant(role_card=None, role_card_kind=None, seats=["verifier", "solo"]))
        self.assertNotIn("role_card", clean)
        with self.assertRaisesRegex(PersonaValidationError, "not one of the persona's kinds"):
            validate_persona(character_variant(kinds=["code"]))  # role_card_kind ops

    def test_role_card_kind_without_a_card(self) -> None:
        with self.assertRaisesRegex(PersonaValidationError, "role_card_kind: needs role_card"):
            validate_persona(character_variant(role_card=None, role_card_kind="ops", seats=["verifier", "solo"]))

    def test_seat_and_kind_rules_in_the_block_are_errors(self) -> None:
        with self.assertRaisesRegex(PersonaValidationError, "persona: grades objections"):
            validate_persona(with_block(checks=["이의는 [차단]로 표시하세요"]))
        with self.assertRaisesRegex(PersonaValidationError, "persona: names a tool"):
            validate_persona(with_block(stance="정리 후 ops:apt_clean 로 마무리합니다."))

    def test_autonomy_claims_are_errors(self) -> None:
        for claim in ("autonomous", "스스로 실행합니다"):
            with self.subTest(claim=claim):
                with self.assertRaisesRegex(PersonaValidationError, "persona: claims to act on its own"):
                    validate_persona(with_block(stance=f"검토가 끝나면 {claim}."))

    def test_the_rendered_length_is_an_error_above_the_limit(self) -> None:
        block = {"identity": "가" * 120, "expertise": ["가" * 60] * 4, "stance": "가" * 200,
                 "checks": ["가" * 80] * 5, "boundaries": ["가" * 80] * 3}
        with self.assertRaisesRegex(PersonaValidationError, "characters, limit 700"):
            validate_persona(character_variant(persona=block))


class ComposeTests(unittest.TestCase):
    def test_a_character_gets_its_card_on_its_own_line(self) -> None:
        self.assertEqual(persona_text(CHARACTER),
                         character.render_persona(CHARACTER["persona"]) + "\n" + card("critic", "ops"))

    def test_without_a_card_a_character_is_sent_as_rendered(self) -> None:
        persona = character_variant(role_card=None, role_card_kind=None, seats=["verifier", "solo"])
        self.assertEqual(persona_text(persona), character.render_persona(persona["persona"]))

    def test_format_1_keeps_the_space_join(self) -> None:
        persona = {"system_prompt": "당신은 검토자입니다.", "role_card": "critic"}
        self.assertEqual(persona_text(persona), f"당신은 검토자입니다. {card('critic')}")

    def test_body_text_tools_and_seats_switch_on_the_format(self) -> None:
        self.assertEqual(character.body_text(CHARACTER), character.render_persona(CHARACTER["persona"]))
        self.assertEqual(character.body_text({"system_prompt": "x"}), "x")
        self.assertEqual(character.persona_tools({**CHARACTER, "tools": ["ops:apt_clean"]}), ["ops:apt_clean"])
        self.assertEqual(character.persona_tools({"mcp_capabilities": ["jev_mcp:run_tests"]}),
                         ["jev_mcp:run_tests"])
        self.assertEqual(character.persona_seats(CHARACTER), ["critic", "verifier", "solo"])
        self.assertIsNone(character.persona_seats({"system_prompt": "x"}))


class JudgeToolsTests(unittest.TestCase):
    def test_the_personas_own_tools_narrow_the_judge(self) -> None:
        lineup = parse_lineup({"id": "t", "expert": "domain_architect", "critic": "critic_redteam",
                               "judge": "safety_reviewer",
                               "allowed_tools": ["jev_mcp:run_tests", "ops:apt_clean"]})
        narrow = character_variant(tools=["ops:apt_clean"], seats=["judge"], role_card="judge")
        wide = character_variant(tools=["ops:apt_clean", "jev_mcp:run_tests"], seats=["judge"], role_card="judge")
        self.assertEqual(judge_tools(lineup, narrow), ["ops:apt_clean"])  # the team list narrows further
        self.assertEqual(judge_tools(default_lineup(), narrow), ["ops:apt_clean"])  # no team list: the persona's own
        self.assertEqual(judge_tools(lineup, wide), ["jev_mcp:run_tests", "ops:apt_clean"])  # never widened
        self.assertIsNone(judge_tools(default_lineup(),
                                      character_variant(tools=[], seats=["judge"], role_card="judge")))


class LineupSeatTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()
        self.manager = PersonaManager()
        self.manager.save_persona(copy.deepcopy(CHARACTER))
        self.manager.save_persona(character_variant(id="plain_critic", name="Plain critic", seats=["critic"]))

    def tearDown(self) -> None:
        restore_data_dir()

    def team(self, **seats: Any) -> Dict[str, Any]:
        data: Dict[str, Any] = {"id": "t", "expert": seats.get("expert", "domain_architect"),
                                "judge": seats.get("judge", "jev_mcp_decision")}
        flow = seats.get("flow")
        if flow is not None:
            data["flow"] = flow
        if "proposers" in seats:
            data["proposers"] = seats["proposers"]
        # a debate and a verify flow need a critic; panel, pipeline and versus flows have none
        if "critic" in seats:
            data["critic"] = seats["critic"]
        elif flow is None or flow.get("kind") == "verify":
            data["critic"] = "critic_redteam"
        return data

    def test_a_character_seated_outside_its_seats_is_an_error(self) -> None:
        with self.assertRaisesRegex(TeamValidationError,
                                    re.escape("judge: persona 'safety_reviewer' may sit in critic, verifier, solo, "
                                              "not judge")):
            validate_lineup(parse_lineup(self.team(judge="safety_reviewer")))

    def test_seated_inside_its_seats_it_validates(self) -> None:
        self.assertEqual(validate_lineup(parse_lineup(self.team(critic="safety_reviewer"))), [])

    def test_a_verify_flow_critic_position_is_a_verifier_seat(self) -> None:
        flow = {"kind": "verify"}
        self.assertEqual(validate_lineup(parse_lineup(self.team(critic="safety_reviewer", flow=flow))), [])
        with self.assertRaisesRegex(TeamValidationError, "not verifier"):
            validate_lineup(parse_lineup(self.team(critic="plain_critic", flow=flow)))

    def test_the_home_seat_warning_still_applies_to_a_character(self) -> None:
        self.manager.save_persona(character_variant(id="both_seats", name="Both", seats=["critic", "expert"]))
        warnings = validate_lineup(parse_lineup(self.team(expert="both_seats")))
        self.assertTrue(any(w.startswith("expert: persona 'both_seats' has role_card critic") for w in warnings))

    def test_flow_positions_map_to_their_own_seats(self) -> None:
        self.manager.save_persona(character_variant(id="stage_char", name="Stage", seats=["stage"],
                                                    role_card=None, role_card_kind=None))
        self.manager.save_persona(character_variant(id="side_char", name="Side", seats=["side"],
                                                    role_card=None, role_card_kind=None))
        self.manager.save_persona(character_variant(id="prop_char", name="Proposer", seats=["proposer"],
                                                    role_card=None, role_card_kind=None))
        pipeline = self.team(flow={"kind": "pipeline", "stages": ["stage_char", "domain_architect"]})
        self.assertFalse([w for w in validate_lineup(parse_lineup(pipeline)) if "may sit" in w])
        versus = self.team(flow={"kind": "versus", "sides": [{"persona": "side_char", "stance": "a"},
                                                             {"persona": "domain_architect", "stance": "b"}]})
        self.assertFalse([w for w in validate_lineup(parse_lineup(versus)) if "may sit" in w])
        debate = self.team(proposers=[{"persona": "prop_char"}])
        self.assertFalse([w for w in validate_lineup(parse_lineup(debate)) if "may sit" in w])
        with self.assertRaisesRegex(TeamValidationError, "may sit in stage, not judge"):
            validate_lineup(parse_lineup(self.team(judge="stage_char")))
        with self.assertRaisesRegex(TeamValidationError, "may sit in proposer, not expert"):
            validate_lineup(parse_lineup(self.team(expert="prop_char")))
        with self.assertRaisesRegex(TeamValidationError, "may sit in side, not critic"):
            validate_lineup(parse_lineup(self.team(critic="side_char")))


class SoloTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()
        self.manager = PersonaManager()
        self.manager.save_persona(copy.deepcopy(CHARACTER))
        self.manager.save_persona(character_variant(id="plain_critic", name="Plain critic", seats=["critic"]))

    def tearDown(self) -> None:
        restore_data_dir()

    def test_a_character_without_a_solo_seat_is_refused(self) -> None:
        with self.assertRaisesRegex(SoloError, "has no solo seat"):
            resolve_persona(self.manager, "plain_critic")

    def test_a_character_with_a_solo_seat_is_accepted_without_the_markers(self) -> None:
        persona_id, persona = resolve_persona(self.manager, "safety_reviewer")
        self.assertEqual((persona_id, persona["format"]), ("safety_reviewer", 2))

    def test_format_1_markers_still_refuse_a_judge(self) -> None:
        with self.assertRaisesRegex(SoloError, "judges or proposes actions"):
            resolve_persona(self.manager, "jev_mcp_decision")


class LintTests(unittest.TestCase):
    def test_format_1_warnings_are_unchanged(self) -> None:
        persona = {"id": "p", "system_prompt": "당신은 서버 24대를 자율 관리합니다."}
        self.assertEqual(persona_lint.persona_warnings(persona), persona_lint.warnings(persona["system_prompt"]))

    def test_format_2_warnings_come_from_the_rendered_block(self) -> None:
        persona = character_variant(persona={**CHARACTER["persona"], "stance": "버전 v1.2.3 기준으로 검토합니다."})
        self.assertEqual(persona_lint.persona_warnings(persona), ["persona names a version ('v1.2.3')"])

    def test_format_2_also_reports_the_length_note(self) -> None:
        block = {"identity": "가" * 120, "expertise": ["나" * 60] * 3, "stance": "다" * 150,
                 "checks": ["라" * 20] * 2, "boundaries": []}
        warnings = persona_lint.persona_warnings(character_variant(persona=block))
        self.assertTrue(warnings and all(w.startswith("persona ") for w in warnings))
        self.assertTrue(any("note above 500" in w for w in warnings), warnings)

    def test_lint_measures_the_text_that_reaches_the_model(self) -> None:
        report = persona_lint.lint([{**CHARACTER, "source": "custom"}])
        self.assertEqual(report["personas"][0]["length"], len(character.render_persona(CHARACTER["persona"])))

    def test_shared_sentences_read_the_rendered_block(self) -> None:
        stance = "모든 주장은 읽은 근거에만 기대야 합니다."
        characters = [character_variant(id=f"c{i}", persona={**CHARACTER["persona"], "stance": stance})
                      for i in range(3)]
        rows = persona_lint.shared_sentences([*characters, {"id": "f1", "system_prompt": stance}])
        self.assertTrue(any(set(row["personas"]) == {"c0", "c1", "c2"} for row in rows))


class SaveTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()
        self.manager = PersonaManager()

    def tearDown(self) -> None:
        restore_data_dir()

    def test_a_character_saves_and_returns_its_warnings(self) -> None:
        data = character_variant(persona={**CHARACTER["persona"], "stance": "버전 v1.2.3 기준으로 검토합니다."})
        code, payload = config_ops.save_persona(self.manager, json.dumps(data, ensure_ascii=False))
        self.assertEqual(code, 0)
        self.assertEqual(payload["warnings"], ["persona names a version ('v1.2.3')"])
        stored = self.manager.get_persona("safety_reviewer")
        self.assertEqual(stored["format"], 2)
        self.assertEqual(character.body_text(stored), character.render_persona(data["persona"]))

    def test_a_character_the_guide_rejects_is_not_saved(self) -> None:
        data = with_block(checks=["이의는 [차단]로 표시하세요"])
        code, payload = config_ops.save_persona(self.manager, json.dumps(data, ensure_ascii=False))
        self.assertEqual(code, 2)
        self.assertIn("grades objections", payload["error"])
        self.assertIsNone(self.manager.get_persona("safety_reviewer"))

    def test_format_1_save_messages_are_unchanged(self) -> None:
        body = json.dumps({"id": "p1", "name": "P", "role": "Critic", "system_prompt": "서버 24대를 관리합니다."},
                          ensure_ascii=False)
        code, payload = config_ops.save_persona(self.manager, body)
        self.assertEqual((code, payload["warnings"]), (0, ["system_prompt counts hosts ('24대')"]))

    def test_the_list_shows_a_characters_rendered_text_without_storing_it(self) -> None:
        config_ops.save_persona(self.manager, json.dumps(CHARACTER, ensure_ascii=False))
        code, payload = config_ops.personas_report(self.manager)
        self.assertEqual(code, 0)
        entries = {p["id"]: p for p in payload["personas"]}
        self.assertEqual(entries["safety_reviewer"]["rendered"], character.render_persona(CHARACTER["persona"]))
        self.assertTrue(all("rendered" not in p for p in entries.values() if not character.is_character(p)))
        self.assertNotIn("rendered", self.manager.get_persona("safety_reviewer"))
        # Sent back as listed (source and rendered included), a persona of either format saves unchanged.
        code, payload = config_ops.save_persona(self.manager, json.dumps(entries["safety_reviewer"],
                                                                        ensure_ascii=False))
        self.assertEqual((code, payload["id"]), (0, "safety_reviewer"))
        self.assertEqual(self.manager.get_persona("safety_reviewer"), CHARACTER)
        body = json.dumps({"id": "p1", "name": "P", "role": "Critic", "system_prompt": "x", "source": "custom"})
        self.assertEqual(config_ops.save_persona(self.manager, body)[0], 0)
        self.assertEqual(self.manager.get_persona("p1"),
                         {"id": "p1", "name": "P", "role": "Critic", "system_prompt": "x"})

    def test_a_hand_edited_persona_block_that_is_not_an_object_renders_empty(self) -> None:
        self.assertEqual(character.body_text({**CHARACTER, "persona": "text"}),
                         character.render_persona({}))

    def test_a_hand_edited_block_of_any_shape_renders_without_raising(self) -> None:
        # every list field a text and every text field something else: each reads as empty
        block = {**CHARACTER["persona"], "identity": 3, "expertise": "검토", "stance": ["x"], "checks": "a",
                 "boundaries": 7}
        self.assertEqual(character.render_persona(block), "당신은 입니다.\n전문 분야: .\n관점: \n먼저 확인할 것:")
        self.assertEqual(character.render_persona(["list"]), character.render_persona({}))

    def test_a_format_1_save_does_not_replace_a_custom_character(self) -> None:
        self.manager.save_persona(copy.deepcopy(CHARACTER))
        plain = {"id": "safety_reviewer", "name": "P", "role": "Critic", "system_prompt": "x"}
        with self.assertRaisesRegex(PersonaValidationError,
                                    re.escape("safety_reviewer is a character (format 2); "
                                              "delete it first or pick another id")):
            self.manager.save_persona(plain)
        self.assertTrue(character.is_character(self.manager.get_persona("safety_reviewer")))

    def test_a_format_1_save_does_not_hide_a_builtin_character(self) -> None:
        builtin = self.manager.custom_dir / "builtin"
        builtin.mkdir(parents=True)
        (builtin / "safety_reviewer.json").write_text(json.dumps(CHARACTER, ensure_ascii=False), encoding="utf-8")
        manager = PersonaManager(personas_dir=builtin, custom_dir=self.manager.custom_dir)
        with self.assertRaisesRegex(PersonaValidationError, "is a character"):
            manager.save_persona({"id": "safety_reviewer", "name": "P", "role": "Critic", "system_prompt": "x"})
        self.assertTrue(character.is_character(manager.get_persona("safety_reviewer")))
        self.assertFalse(manager.is_custom("safety_reviewer"))  # nothing was written over the builtin one

    def test_a_character_over_the_character_and_format_1_over_format_1_stay_allowed(self) -> None:
        self.manager.save_persona(copy.deepcopy(CHARACTER))
        self.manager.save_persona(character_variant(profile={"tagline": "Updated"}))
        self.assertEqual(self.manager.get_persona("safety_reviewer")["profile"], {"tagline": "Updated"})
        plain = {"id": "p1", "name": "P", "role": "Critic", "system_prompt": "one"}
        self.manager.save_persona(plain)
        self.manager.save_persona({**plain, "system_prompt": "two"})
        self.assertEqual(self.manager.get_persona("p1")["system_prompt"], "two")


class OverrideSeatTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()
        self.manager = PersonaManager()
        self.manager.save_persona(character_variant(id="critic_char", name="Critic char", seats=["critic"]))
        self.manager.save_persona(character_variant(id="judge_char", name="Judge char", seats=["judge"],
                                                    role_card="judge"))

    def tearDown(self) -> None:
        restore_data_dir()

    def engine(self) -> DebateEngine:
        with patch.dict(os.environ, {"GUARDIAN_JEV_GATE": "off"}):
            return DebateEngine()

    def test_a_judge_override_outside_a_characters_seats_is_refused(self) -> None:
        # the override skips the team's validation, so the engine checks the lineup again and lets it raise
        with self.assertRaisesRegex(TeamValidationError, "may sit in critic, not judge"):
            self.engine()._resolve_lineup(None, None, None, None, judge_id="critic_char")

    def test_an_override_inside_the_seats_resolves(self) -> None:
        lineup = self.engine()._resolve_lineup(None, None, None, None, judge_id="judge_char")
        self.assertEqual(lineup.judge.persona, "judge_char")

    def test_a_critic_override_on_a_panel_team_is_not_refused(self) -> None:
        # a panel flow has no critic; the run drops the override with a warning, the re-validation must not refuse it
        engine = self.engine()
        engine.team_mgr.save({
            "id": "panelteam", "name": "Panel team", "flow": {"kind": "panel"},
            "expert": {"persona": "domain_architect", "providers": ["claude"]},
            "proposers": [{"persona": "devops_specialist", "providers": ["codex"]}],
            "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]},
            "allowed_tools": [],
        })
        lineup = engine._resolve_lineup("panelteam", None, None, "critic_redteam", None)
        self.assertEqual([m.persona for m in lineup.critics], ["critic_redteam"])


if __name__ == "__main__":
    unittest.main()

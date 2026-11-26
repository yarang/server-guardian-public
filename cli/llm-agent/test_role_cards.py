"""Role cards (docs/design/agent-characters.md, C1): the engine adds the seat rules to personas that opt in."""
import json
import unittest
from pathlib import Path

import persona_suggest
import role_cards
from persona_manager import PersonaManager, PersonaValidationError, builtin_personas_dir, validate_persona
from role_cards import CARD_SEATS, card, card_kind, home_seat, persona_text
from testenv import isolate_data_dir, restore_data_dir

TEAMS_DIR = Path(__file__).resolve().parents[2] / "configs" / "teams"
# C1 moved the seat rules of the code teams and of the ops teams; every persona of an ops team opts in with
# role_card_kind "ops" (the generic ops rules were approved by the operator on 2026-10-31, decisions C-G).
C1_TEAMS = ("default", "cross-review", "guardian-dev")
OPS_TEAMS = ("fleet-ops", "fleet-services", "fleet-mesh", "fleet-provision")


def builtin(persona_id: str) -> dict:
    return json.loads((builtin_personas_dir() / f"{persona_id}.json").read_text(encoding="utf-8"))


def team_personas(team_id: str) -> list:
    team = json.loads((TEAMS_DIR / f"{team_id}.json").read_text(encoding="utf-8"))
    seats = [team["expert"], team["judge"], *team.get("critic", []), *team.get("proposers", [])]
    return [seat["persona"] for seat in seats]


def sentences(text: str) -> list:
    return [s.strip() for s in text.replace(". ", ".\n").splitlines() if s.strip()]


class CardTests(unittest.TestCase):
    def test_a_persona_without_role_card_is_sent_as_written(self) -> None:
        persona = {"system_prompt": "당신은 검토자입니다."}
        self.assertEqual(persona_text(persona), "당신은 검토자입니다.")
        self.assertEqual(persona_text({"system_prompt": "x", "role_card": "nobody"}), "x")

    def test_the_card_is_the_home_seats(self) -> None:
        # the persona keeps its seat and its role (operator's decision of 2026-10-31): no seat argument to vary it
        persona = {"system_prompt": "당신은 검토자입니다.", "role_card": "critic"}
        self.assertEqual(persona_text(persona), f"당신은 검토자입니다. {card('critic')}")

    def test_kinds_without_their_own_card_use_the_code_card(self) -> None:
        for seat in CARD_SEATS:
            self.assertTrue(card(seat))
            self.assertTrue(card(seat, "ops"))
            self.assertNotEqual(card(seat, "ops"), card(seat))
            self.assertEqual(card(seat, "design"), card(seat))
        self.assertEqual(card("verifier"), "")
        self.assertEqual(set(role_cards.CARDS), {(seat, kind) for seat in CARD_SEATS for kind in (None, "ops")})

    def test_the_card_kind_is_the_personas_not_the_projects(self) -> None:
        persona = {"system_prompt": "당신은 검토자입니다.", "role_card": "critic", "role_card_kind": "ops"}
        self.assertEqual(card_kind(persona), "ops")
        self.assertEqual(persona_text(persona), f"당신은 검토자입니다. {card('critic', 'ops')}")
        self.assertIsNone(card_kind({**persona, "role_card_kind": "design"}))
        self.assertIsNone(card_kind({"role_card": "critic"}))

    def test_validation_keeps_a_known_seat_and_refuses_others(self) -> None:
        base = {"id": "p", "name": "P", "role": "Critic Agent", "system_prompt": "x"}
        self.assertEqual(validate_persona({**base, "role_card": "critic"})["role_card"], "critic")
        self.assertNotIn("role_card", validate_persona(base))
        with self.assertRaisesRegex(PersonaValidationError, "role_card: one of expert, critic, judge"):
            validate_persona({**base, "role_card": "verifier"})
        self.assertEqual(validate_persona({**base, "role_card": "critic", "role_card_kind": "ops"})["role_card_kind"], "ops")
        with self.assertRaisesRegex(PersonaValidationError, "role_card_kind: one of ops"):
            validate_persona({**base, "role_card": "critic", "role_card_kind": "design"})
        with self.assertRaisesRegex(PersonaValidationError, "role_card_kind: needs role_card"):
            validate_persona({**base, "role_card_kind": "ops"})


class SeatWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()

    def tearDown(self) -> None:
        restore_data_dir()

    def lineup(self, **seats):
        from team_manager import Lineup, Member
        member = lambda pid: Member(persona=pid, providers=[])
        return Lineup(id="t", expert=member(seats.get("expert", "server_guardian")),
                      critics=[member(pid) for pid in seats.get("critics", ["critic_redteam"])],
                      judge=member(seats.get("judge", "jev_mcp_decision")))

    def test_a_persona_outside_its_home_seat_is_warned_about(self) -> None:
        from team_manager import validate_lineup
        self.assertFalse([w for w in validate_lineup(self.lineup()) if "role_card" in w])
        warnings = validate_lineup(self.lineup(critics=["jev_mcp_decision"]))
        self.assertTrue(any(w.startswith("critic[0]: persona 'jev_mcp_decision' has role_card judge") for w in warnings))
        # a persona without role_card sits anywhere without a warning
        self.assertFalse([w for w in validate_lineup(self.lineup(critics=["design_critic"])) if "role_card" in w])


class BuiltinTests(unittest.TestCase):
    def test_exactly_the_personas_of_the_code_and_ops_teams_opted_in(self) -> None:
        wanted = {pid for team in C1_TEAMS + OPS_TEAMS for pid in team_personas(team)}
        opted = {path.stem for path in builtin_personas_dir().glob("*.json") if home_seat(builtin(path.stem))}
        self.assertEqual(opted, wanted)
        other = {pid for team in TEAMS_DIR.glob("*.json") if team.stem not in C1_TEAMS + OPS_TEAMS
                 for pid in team_personas(team.stem)}
        self.assertFalse(opted & other, "a persona of a design or general team got a role card")

    def test_each_home_seat_is_the_seat_its_teams_give_it(self) -> None:
        for team_id in C1_TEAMS + OPS_TEAMS:
            team = json.loads((TEAMS_DIR / f"{team_id}.json").read_text(encoding="utf-8"))
            seats = [("expert", team["expert"]), ("judge", team["judge"])] + [("critic", c) for c in team["critic"]]
            for seat, member in seats:
                with self.subTest(team=team_id, persona=member["persona"]):
                    self.assertEqual(builtin(member["persona"])["role_card"], seat)

    def test_the_opted_in_ops_personas_get_the_ops_cards(self) -> None:
        ops = {pid for team in OPS_TEAMS for pid in team_personas(team)}
        for path in builtin_personas_dir().glob("*.json"):
            persona = builtin(path.stem)
            if home_seat(persona):
                with self.subTest(persona=path.stem):
                    self.assertEqual(card_kind(persona), "ops" if path.stem in ops else None)

    def test_no_card_sentence_is_left_in_an_opted_in_persona(self) -> None:
        for path in builtin_personas_dir().glob("*.json"):
            persona = builtin(path.stem)
            if home_seat(persona):
                for sentence in sentences(card(persona["role_card"], card_kind(persona))):
                    with self.subTest(persona=path.stem, sentence=sentence):
                        self.assertNotIn(sentence, persona["system_prompt"])


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()

    def tearDown(self) -> None:
        restore_data_dir()

    def test_the_solo_agent_sends_the_home_seat_card(self) -> None:
        import solo_agent
        _pid, persona = solo_agent.resolve_persona(PersonaManager(), None)
        runner = solo_agent.SoloRunner.__new__(solo_agent.SoloRunner)
        runner.persona, runner.general = persona, False
        runner.engine = type("E", (), {"_language_rule": "rule"})()
        self.assertTrue(runner.prompt("q").persona.endswith(card("expert")))

    def test_the_solo_agent_sends_an_ops_persona_with_its_ops_card(self) -> None:
        import solo_agent
        _pid, persona = solo_agent.resolve_persona(PersonaManager(), "ops_sre")
        runner = solo_agent.SoloRunner.__new__(solo_agent.SoloRunner)
        runner.persona, runner.general = persona, False
        runner.engine = type("E", (), {"_language_rule": "rule"})()
        # the persona's own text, then the ops card of its home seat
        self.assertEqual(runner.prompt("q").persona, f"{persona['system_prompt']} {card('expert', 'ops')}")

    def test_suggest_examples_show_the_card(self) -> None:
        example = builtin("critic_redteam")
        prompt = persona_suggest.build_prompt("brief", "critic", "code", [example])
        self.assertIn(persona_text(example), prompt)
        ops_example = builtin("ops_safety_critic")
        prompt = persona_suggest.build_prompt("brief", "critic", "ops", [ops_example])
        self.assertIn(persona_text(ops_example), prompt)
        self.assertIn(card("critic", "ops"), prompt)
        self.assertNotIn(card("critic"), prompt)


if __name__ == "__main__":
    unittest.main()

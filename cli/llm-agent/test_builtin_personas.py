"""The builtin personas say what the engine does (prompt p21): evidence rules, no autonomy, no dead fields as prompt."""
import json
import unittest
import unittest.mock
from pathlib import Path

from multi_agent import MultiAgentSystem
from persona_manager import builtin_personas_dir
from role_cards import persona_text


def prompt(persona_id: str) -> str:
    """What reaches the model in the persona's home seat: its text plus the role card when it opted in (C1)."""
    data = json.loads((builtin_personas_dir() / f"{persona_id}.json").read_text(encoding="utf-8"))
    return persona_text(data)


class GenericPersonaTests(unittest.TestCase):
    def test_the_generic_lineup_cites_what_it_read(self) -> None:
        for pid in ("domain_architect", "critic_redteam", "devops_specialist"):
            with self.subTest(persona=pid):
                self.assertIn("파일:줄", prompt(pid))

    def test_the_default_expert_proposes_the_smallest_change(self) -> None:
        text = prompt("domain_architect")
        self.assertIn("가장 작은 변경", text)
        self.assertIn("되돌리는 방법", text)
        self.assertNotIn("모던하고 모듈화된", text)

    def test_the_reviewers_grade_and_may_find_nothing(self) -> None:
        for pid in ("critic_redteam", "devops_specialist"):
            with self.subTest(persona=pid):
                self.assertIn("심각도(높음/중간/낮음)", prompt(pid))
                self.assertIn("문제가 없으면 없다고", prompt(pid))
        # devops_specialist is a critic in cross-review and a proposer in proposer_eval: it covers both
        self.assertIn("제안을 쓸 때는", prompt("devops_specialist"))
        self.assertIn("검토할 때는", prompt("devops_specialist"))


class NoAutonomyTests(unittest.TestCase):
    def test_no_builtin_persona_claims_to_act_on_its_own_or_counts_hosts(self) -> None:
        for path in sorted(builtin_personas_dir().glob("*.json")):
            with self.subTest(persona=path.stem):
                text = json.loads(path.read_text(encoding="utf-8"))["system_prompt"]
                self.assertNotIn("자율", text)
                self.assertNotIn("24대", text)
                self.assertNotIn("집행하세요", text)

    def test_the_chat_fallback_is_the_builtin_text(self) -> None:
        system = MultiAgentSystem.__new__(MultiAgentSystem)
        with unittest.mock.patch("multi_agent.PersonaManager") as manager, \
                unittest.mock.patch("multi_agent.NodeAgentClient"), unittest.mock.patch("multi_agent.LLMRunner"):
            manager.return_value.get_persona.return_value = None
            system.__init__()
        self.assertEqual(system.active_persona["system_prompt"],
                         json.loads((builtin_personas_dir() / "server_guardian.json").read_text(encoding="utf-8"))["system_prompt"])


if __name__ == "__main__":
    unittest.main()

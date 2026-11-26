"""Persona suggestion: one model turn drafts persona and profile, the guide's checks, nothing saved."""
import json
import unittest
from pathlib import Path
from typing import List, Optional

import persona_suggest
from llm_runner import BUILTIN_PROVIDER
from persona_manager import PersonaManager, PersonaValidationError, validate_persona
from persona_suggest import SuggestError, suggest
from testenv import isolate_data_dir, restore_data_dir

ANSWER = {
    "id": "domain_architect",  # taken by a builtin: the draft gets a free id
    "name": "꼼꼼한 검토자",
    "domain": "코드 리뷰",
    "system_prompt": "당신은 변경이 깨뜨리는 것을 찾는 검토자입니다. 지적마다 파일:줄로 인용하세요.",
    "critique_focus": ["경계 조건", "테스트"],
    "profile": {"tagline": "깨지는 곳을 먼저 본다", "look": "안경을 쓴 30대 개발자, 회색 후드티", "extra": "dropped"},
    "temperature": 0.3,
}


class FakeRunner:
    def __init__(self, text: str, name: str = "claude-cli") -> None:
        self.text, self.name, self.prompts, self.last_error = text, name, [], "boom"

    def run_prompt(self, prompt: str, context=None, system: Optional[str] = None):
        self.prompts.append((prompt, system))
        return self.name, self.text


class SuggestTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()
        self.manager = PersonaManager()
        self.providers: List[Optional[List[str]]] = []

    def tearDown(self) -> None:
        restore_data_dir()

    def run_with(self, request, text: str = json.dumps(ANSWER, ensure_ascii=False), name: str = "claude-cli"):
        runner = FakeRunner(text, name)

        def factory(providers):
            self.providers.append(providers)
            return runner
        return suggest(request, self.manager, factory), runner

    def test_the_draft_is_a_valid_persona_with_a_free_id_and_nothing_is_saved(self) -> None:
        result, runner = self.run_with({"brief": "꼼꼼한 리뷰어", "seat": "critic", "kind": "code", "provider": "codex"})
        draft = result["suggestion"]
        self.assertEqual(validate_persona(draft), draft)
        self.assertEqual(draft["id"], "domain_architect_2")
        self.assertEqual(draft["role"], "Critic Agent")
        self.assertEqual(draft["profile"], {"tagline": "깨지는 곳을 먼저 본다", "look": "안경을 쓴 30대 개발자, 회색 후드티"})
        self.assertNotIn("temperature", draft)
        self.assertEqual(self.providers, [["codex"]])
        self.assertEqual(runner.prompts[0][1], "")  # no default role header
        self.assertIn("꼼꼼한 리뷰어", runner.prompts[0][0])
        self.assertFalse(list(self.manager.custom_dir.glob("*.json")))

    def test_examples_are_builtin_personas_of_the_seat_and_kind(self) -> None:
        result, runner = self.run_with({"brief": "x", "seat": "critic", "kind": "ops"})
        self.assertTrue(result["examples"])
        self.assertTrue(all("critic" in pid for pid in result["examples"]), result["examples"])
        self.assertTrue(all(pid.startswith("ops_") for pid in result["examples"]), result["examples"])
        self.assertLessEqual(len(result["examples"]), persona_suggest.MAX_EXAMPLES)

    def test_the_guide_warns_on_length_and_tool_names(self) -> None:
        long = dict(ANSWER, system_prompt="당신은 검토자입니다. " + "가" * 800 + " ops:apt_clean")
        result, _ = self.run_with({"brief": "x"}, json.dumps(long, ensure_ascii=False))
        text = " ".join(result["warnings"])
        self.assertIn("limit 700", text)
        self.assertIn("names a tool", text)

    def test_a_fenced_answer_parses(self) -> None:
        fenced = "초안입니다:\n```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"
        result, _ = self.run_with({"brief": "x"}, fenced)
        self.assertEqual(result["suggestion"]["name"], "꼼꼼한 검토자")

    def test_refusals(self) -> None:
        for request, needle in (({"brief": ""}, "brief"), ({"brief": "x" * 1001}, "brief"),
                                ({"brief": "x", "seat": "boss"}, "seat"), ({"brief": "x", "kind": "web"}, "kind"),
                                ({"brief": "x", "provider": "a b"}, "provider"), ([], "object")):
            with self.subTest(request=request), self.assertRaises(SuggestError) as caught:
                self.run_with(request)
            self.assertIn(needle, str(caught.exception))
            self.assertEqual(caught.exception.code, persona_suggest.USAGE)

    def test_a_character_current_is_refused_without_a_model_turn(self) -> None:
        # Suggestions fill the format 1 form; a character (format 2) has none of those fields.
        current = {"id": "safety_reviewer", "format": 2, "name": "Safety reviewer", "seats": ["critic"],
                   "persona": {"identity": "검토자", "expertise": ["삭제"], "stance": "신중하게.",
                               "checks": ["대상"]}}
        with self.assertRaises(SuggestError) as caught:
            self.run_with({"brief": "x", "current": current})
        self.assertIn("characters are edited from C4 on", str(caught.exception))
        self.assertEqual(caught.exception.code, persona_suggest.USAGE)
        self.assertEqual(self.providers, [])  # refused before any provider was asked

    def test_model_failures_exit_5(self) -> None:
        for text, name in (("no json here", "claude-cli"), ("{broken", "claude-cli"), ("{}", BUILTIN_PROVIDER)):
            with self.subTest(text=text), self.assertRaises(SuggestError) as caught:
                self.run_with({"brief": "x"}, text, name)
            self.assertEqual(caught.exception.code, persona_suggest.FAILED)

    def test_run_cli_reports_bad_json(self) -> None:
        code, payload = persona_suggest.run_cli("{not json")
        self.assertEqual(code, persona_suggest.USAGE)
        self.assertFalse(payload["ok"])


class ProfileFieldTests(unittest.TestCase):
    BASE = {"id": "p", "name": "n", "role": "r", "system_prompt": "s"}

    def test_profile_is_kept_and_checked(self) -> None:
        self.assertEqual(validate_persona({**self.BASE, "profile": {"tagline": "t"}})["profile"], {"tagline": "t"})
        for bad in ({"tagline": "x" * 121}, {"look": "x" * 201}, {"photo": "x"}, "text"):
            with self.subTest(bad=bad), self.assertRaises(PersonaValidationError):
                validate_persona({**self.BASE, "profile": bad})


if __name__ == "__main__":
    unittest.main()

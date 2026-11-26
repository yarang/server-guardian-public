"""Persona lint (docs/design/agent-characters.md, phase C0): the form guide as warning-only checks."""
import contextlib
import io
import json
import unittest

import config_ops
import persona_lint
from persona_lint import LENGTH_LIMIT, MOVE, NOTE, WARN, check_text, lint, shared_sentences
from persona_manager import PersonaManager
from testenv import isolate_data_dir, restore_data_dir


def rules(text: str, level: str) -> set:
    return {f["rule"] for f in check_text(text) if f["level"] == level}


class CheckTextTests(unittest.TestCase):
    def test_a_short_character_is_clean(self) -> None:
        self.assertEqual(check_text("당신은 되돌리는 방법부터 묻는 릴리스 검토자입니다. 배포 순서와 영향 범위를 먼저 봅니다."), [])

    def test_warn_rules(self) -> None:
        cases = {
            "autonomy": "당신은 24시간 자율 운영합니다.",
            "host-count": "서버 24대를 관리합니다.",
            "version": "프롬프트 p18 규칙을 따르세요. codex 0.160.0 기준.",
            "path": "apps/agent와 debate_engine.py를 보세요.",
            "env": "GUARDIAN_DATA_DIR를 확인하세요.",
        }
        for rule, text in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, rules(text, WARN))

    def test_move_rules_are_seat_and_kind_rules(self) -> None:
        cases = {
            "tool": "ops:run_script 단계로 넣으세요.",
            "grading": "이의는 [차단]과 [권고]로 나누세요.",
            "verdict": "사용자가 시키지 않은 금지 조건을 만들지 마세요.",
            "citation": "주장마다 파일:줄로 인용하세요.",
            "protocol": "먼저 ops 블록으로 점검하세요.",
        }
        for rule, text in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, rules(text, MOVE))
                self.assertEqual(rules(text, WARN), set())

    def test_length_note_and_limit(self) -> None:
        self.assertIn("length", rules("가" * 600, NOTE))
        self.assertIn("length", rules("가" * (LENGTH_LIMIT + 1), WARN))
        self.assertEqual(rules("가" * 500, NOTE) | rules("가" * 500, WARN), set())

    def test_a_path_match_stops_at_korean_particles(self) -> None:
        found = [f["match"] for f in check_text("scripts/ci.sh가 통과해야 합니다.") if f["rule"] == "path"]
        self.assertEqual(found, ["scripts/ci.sh"])

    def test_warnings_carry_only_warn_findings(self) -> None:
        text = "주장마다 파일:줄로 인용하세요. 서버 24대를 봅니다."
        self.assertEqual(len(persona_lint.warnings(text)), 1)
        self.assertIn("counts hosts", persona_lint.warnings(text)[0])


class ReportTests(unittest.TestCase):
    def test_shared_sentences_need_three_personas(self) -> None:
        line = "모든 주장에 근거를 인용하세요."
        personas = [{"id": f"p{i}", "system_prompt": f"당신은 검토자 {i}입니다. {line}"} for i in range(3)]
        self.assertEqual(shared_sentences(personas), [{"sentence": line, "personas": ["p0", "p1", "p2"]}])
        self.assertEqual(shared_sentences(personas[:2]), [])

    def test_the_builtin_baseline(self) -> None:
        manager = PersonaManager()
        report = lint([{**p, "source": "builtin"} for p in PersonaManager._read_dir(manager.personas_dir)])
        summary = report["summary"]
        self.assertGreaterEqual(summary["personas"], 29)
        # No builtin claims autonomy, counts hosts or names a version. The guardian-dev personas (guardian_*) are
        # written for this repository and name its paths and AGENT_TOKEN on purpose; every other builtin names none.
        for entry in report["personas"]:
            found = {f["rule"] for f in entry["findings"] if f["level"] == WARN}
            if str(entry["id"]).startswith("guardian_"):
                found -= {"env", "path"}
            with self.subTest(persona=entry["id"]):
                self.assertFalse(found & {"autonomy", "host-count", "env", "version", "path"}, found)

    def test_cli_prints_a_summary_and_exits_0(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(persona_lint.main([]), 0)
        self.assertIn("(warning only)", out.getvalue().splitlines()[-1])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            persona_lint.main(["--json"])
        self.assertIn("shared", json.loads(out.getvalue()))


class SaveWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_data_dir()

    def tearDown(self) -> None:
        restore_data_dir()

    def test_save_returns_the_guides_warnings_and_still_saves(self) -> None:
        manager = PersonaManager()
        body = json.dumps({"id": "lint_me", "name": "n", "role": "Critic Agent",
                           "system_prompt": "당신은 서버 24대를 자율 관리합니다."}, ensure_ascii=False)
        code, payload = config_ops.save_persona(manager, body)
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["warnings"]), 2)
        self.assertIsNotNone(manager.get_persona("lint_me"))


if __name__ == "__main__":
    unittest.main()

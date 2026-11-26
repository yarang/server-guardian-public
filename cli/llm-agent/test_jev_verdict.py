"""jev verdict, phase J1 (docs/design/jev-verdict.md): the verdict question, parsing jev's answer, the shadow record
next to the LLM judge in a real engine run with a fake jev, and the report."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import jev_verdict as jv
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from team_manager import TeamManager
from test_team_flows import BASE
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


# The shape jev_ask returned on 2026-11-01 (model jev-1.14.0).
ANSWER = {"answers": {"approve": {"type": "noul", "noul": 0.82}, "fundamental": {"type": "noul", "noul": 0.05},
                      "evidence": {"type": "score", "score": 1.43, "confidence": 0}},
          "model": "jev-1.14.0", "latency_ms": 276}
LIMITS = {"yes": 0.8, "no": 0.2}


class VerdictTests(unittest.TestCase):
    def test_parse_and_classify(self) -> None:
        record = jv.parse_answer(ANSWER, LIMITS)
        self.assertEqual((record["p_approve"], record["verdict"], record["p_fundamental"], record["evidence"]),
                         (0.82, jv.APPROVE, 0.05, 1.43))
        self.assertEqual(record["questions"], jv.QUESTION_VERSION)
        self.assertEqual(jv.classify(0.2, LIMITS), jv.REJECT)
        self.assertEqual(jv.classify(0.5, LIMITS), jv.UNCERTAIN)
        with self.assertRaises(jv.JevVerdictError):
            jv.parse_answer({"answers": {}}, LIMITS)

    def test_thresholds_from_the_environment(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_JEV_VERDICT_YES": "0.9", "GUARDIAN_JEV_VERDICT_NO": "0.1"}):
            self.assertEqual(jv.thresholds(), {"yes": 0.9, "no": 0.1})
        with patch.dict(os.environ, {"GUARDIAN_JEV_VERDICT_YES": "0.1", "GUARDIAN_JEV_VERDICT_NO": "0.9"}):
            self.assertEqual(jv.thresholds(), LIMITS)       # crossed limits fall back to the defaults
        with patch.dict(os.environ, {"GUARDIAN_JEV_VERDICT": "on"}):
            self.assertEqual(jv.mode(), jv.OFF)              # J1 knows only shadow

    def test_long_material_is_cut_in_the_middle(self) -> None:
        short = jv.state_text("t", "material")
        self.assertFalse(short["clipped"])
        long = jv.state_text("t", "head " + "x" * jv.MAX_STATE_CHARS + " tail")
        self.assertTrue(long["clipped"])
        self.assertTrue(long["text"].startswith("Task: t"))
        self.assertTrue(long["text"].endswith(" tail"))

    def test_compare(self) -> None:
        record = jv.parse_answer(ANSWER, LIMITS)
        self.assertTrue(jv.compare(record, True)["agree"])
        self.assertFalse(jv.compare(record, False)["agree"])
        unsure = {**record, "verdict": jv.UNCERTAIN}
        self.assertIsNone(jv.compare(unsure, False)["agree"])


class HttpTests(unittest.TestCase):
    """The System One request: URL, key header, question shape; errors never carry the key."""

    def response(self, payload):
        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Resp(json.dumps(payload).encode())

    def test_request_and_answer(self) -> None:
        seen = {}

        def urlopen(request, timeout):
            seen.update(url=request.full_url, auth=request.get_header("Authorization"), body=json.loads(request.data))
            return self.response(ANSWER)

        env = {"TYPESAFE_API_KEY": "test-key", "TYPESAFE_BASE_URL": "https://jev.test/", "TYPESAFE_DEFAULT_MODEL": "jev-preview"}
        with patch.dict(os.environ, env), patch("jev_verdict.load_llm_env", return_value={}), \
                patch("urllib.request.urlopen", side_effect=urlopen):
            record = jv.JevVerdict().ask("t", "material")
        self.assertEqual(seen["url"], "https://jev.test/v1/systemone")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["model"], "jev-preview")
        self.assertEqual(seen["body"]["questions"]["approve"]["type"], "noul")
        self.assertIn("true", seen["body"]["questions"]["approve"]["criteria"])
        self.assertEqual(seen["body"]["questions"]["evidence"]["criteria"], jv.EVIDENCE_LEVELS)
        self.assertEqual(record["verdict"], jv.APPROVE)

    def test_errors_are_named_without_the_key(self) -> None:
        import urllib.error
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "TYPESAFE_BASE_URL": ""}), \
                patch("jev_verdict.load_llm_env", return_value={}):
            with self.assertRaises(jv.JevVerdictError):
                jv.JevVerdict().ask("t", "m")
        failing = urllib.error.HTTPError("https://jev.test", 401, "unauthorized", {}, None)
        failing.read = lambda *a: b'{"error": "bad key"}'
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "secret-value"}), \
                patch("jev_verdict.load_llm_env", return_value={}), \
                patch("urllib.request.urlopen", side_effect=failing):
            with self.assertRaises(jv.JevVerdictError) as ctx:
                jv.JevVerdict().ask("t", "m")
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("secret-value", str(ctx.exception))


class FakeJev:
    def __init__(self, record=None, error=None):
        self.record, self.error, self.calls = record, error, []

    def ask(self, task, material, limits=None):
        self.calls.append((task, material))
        if self.error:
            raise self.error
        return self.record


class ShadowRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"GUARDIAN_JEV_GATE": "off", "GUARDIAN_JEV_VERDICT": "shadow"})
        env.start()
        self.addCleanup(env.stop)
        manager = TeamManager(self.root / "teams")
        manager.save(dict(BASE, max_rounds=1))
        self.engine = DebateEngine(db_path=self.root / "d.db", events=EventWriter(str(self.root / "events.jsonl")),
                                   team_mgr=manager)
        self.judge = "Not yet.\nVERDICT: REJECTED"

    def fake(self, prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
        prompt = prompt + (instructions.text() if instructions else "")
        if JUDGE_VERDICT_RULE in prompt:
            return LLMResult("claude-cli", self.judge, "s")
        return LLMResult(f"{providers[0]}-cli", "text with app.py:1", "s")

    def run_debate(self, jev) -> list:
        self.engine.jev_verdict = jev
        with patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake), \
                patch.object(self.engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            try:
                self.engine.run_debate("Improve hello", project_id="server-guardian", team="flowteam")
            finally:
                self.engine.events.close()
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def test_shadow_records_both_verdicts_and_a_disagreement(self) -> None:
        jev = FakeJev(jv.parse_answer(ANSWER, LIMITS))
        events = self.run_debate(jev)
        self.assertEqual(len(jev.calls), 1)
        task, material = jev.calls[0]
        self.assertIn("Improve hello", task)
        self.assertIn("Critique", material)          # the judge's material, not something else
        shadow = [e for e in events if e["type"] == "jev_verdict"]
        self.assertEqual(shadow[0]["data"]["verdict"], jv.APPROVE)
        self.assertEqual(shadow[0]["data"]["llm"], jv.REJECT)
        self.assertIs(shadow[0]["data"]["agree"], False)
        self.assertEqual(shadow[0]["seat"], "close")
        disagreement = [e for e in events if e["type"] == "verdict_disagreement"]
        self.assertEqual(disagreement[0]["data"]["kind"], "shadow")
        # the outcome is the LLM judge's: one verdict event, not approved
        verdicts = [e for e in events if e["type"] == "verdict"]
        self.assertEqual([v["data"]["approved"] for v in verdicts], [False])

    def test_agreement_emits_no_disagreement(self) -> None:
        self.judge = "Fine.\nVERDICT: APPROVED"
        events = self.run_debate(FakeJev(jv.parse_answer(ANSWER, LIMITS)))
        self.assertIs(next(e for e in events if e["type"] == "jev_verdict")["data"]["agree"], True)
        self.assertFalse([e for e in events if e["type"] == "verdict_disagreement"])

    def test_a_jev_failure_is_recorded_and_the_debate_goes_on(self) -> None:
        events = self.run_debate(FakeJev(error=jv.JevVerdictError("jev down")))
        shadow = next(e for e in events if e["type"] == "jev_verdict")
        self.assertIn("jev down", shadow["data"]["error"])
        self.assertTrue(any(e["type"] == "debate_finished" for e in events))

    def test_off_and_canned_judges_ask_nothing(self) -> None:
        jev = FakeJev(jv.parse_answer(ANSWER, LIMITS))
        with patch.dict(os.environ, {"GUARDIAN_JEV_VERDICT": "off"}):
            events = self.run_debate(jev)
        self.assertEqual(jev.calls, [])
        self.assertFalse([e for e in events if e["type"] == "jev_verdict"])

    def test_a_canned_judge_answer_is_not_compared(self) -> None:
        jev = FakeJev(jv.parse_answer(ANSWER, LIMITS))
        original = self.fake

        def canned(prompt, *args, **kwargs):
            full = prompt + (kwargs["instructions"].text() if kwargs.get("instructions") else "")
            if JUDGE_VERDICT_RULE in full:
                return LLMResult(BUILTIN_PROVIDER, "canned")
            return original(prompt, *args, **kwargs)

        self.fake = canned
        self.run_debate(jev)
        self.assertEqual(jev.calls, [])


class ReportTests(unittest.TestCase):
    def test_report_counts_agreement_where_jev_is_sure(self) -> None:
        with TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "debate-jobs"
            records = [(0.9, "approve"), (0.9, "reject"), (0.1, "reject"), (0.5, "approve"), (None, None)]
            for i, (p, llm) in enumerate(records):
                job = jobs / f"dbt_{i}"
                job.mkdir(parents=True)
                lines = [{"type": "debate_started", "thread_id": f"t{i}", "data": {"project": "fleet" if i == 2 else "server-guardian"}}]
                if p is None:
                    lines.append({"type": "jev_verdict", "data": {"mode": "shadow", "error": "down"}})
                else:
                    rec = jv.compare({"questions": jv.QUESTION_VERSION, "p_approve": p, "verdict": jv.classify(p, LIMITS),
                                      "p_fundamental": 0.1, "evidence": 2.0}, llm == "approve")
                    lines.append({"type": "jev_verdict", "thread_id": f"t{i}", "round": 1, "data": rec})
                (job / "events.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
            old = jobs / "dbt_old"   # a record of question set 1 is left out
            old.mkdir()
            (old / "events.jsonl").write_text(json.dumps({"type": "jev_verdict", "data": {"mode": "shadow", "p_approve": 0.9,
                                                          "verdict": "approve", "llm": "approve", "agree": True}}) + "\n")
            rows = jv.collect(jobs, {"fleet": "ops"})
            out = jv.summarize(rows)
        self.assertEqual((out["verdicts"], out["sure"]), (4, 3))
        self.assertAlmostEqual(out["agreement"], 2 / 3)
        self.assertEqual(out["confusion"]["approve"], {"approve": 1, "reject": 1})
        self.assertEqual(out["confusion"]["uncertain"], {"approve": 1, "reject": 0})
        self.assertEqual([c["job"] for c in out["jev_approves_llm_rejects"]], ["dbt_1"])
        self.assertEqual(out["by_kind"]["ops"]["sure"], 1)
        self.assertFalse(out["bar"]["met"])


if __name__ == "__main__":
    unittest.main()

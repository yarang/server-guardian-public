"""Verdict sources, phase S1 (docs/design/verdict-sources.md): source files and their validation, the shadow run with
fake sources, an engine run with an injected source, and the report over both event shapes."""

import datetime
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import jev_verdict as jv
import verdict_sources as vs
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine
from debate_events import EventWriter
from llm_runner import LLMResult
from team_manager import TeamManager
from test_jev_verdict import ANSWER, LIMITS
from test_team_flows import BASE
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


JEV = {"id": "jev", "name": "jev verdict", "kind": "jev", "reviewed": "2026-11-02", "roles": ["decide", "alarm"],
       "projects": ["code", "ops", "design"], "timeout_s": 60, "questions": jv.QUESTION_VERSION, "approve_at": 0.8,
       "reject_at": 0.2, "shadow_bar": {"min_verdicts": 30, "min_agreement": 0.9}}


class FakeSource:
    def __init__(self, sid, verdict=None, error=None, projects=("code", "ops", "design"), kind="fake", **extra):
        self.id, self.kind, self.projects, self.timeout_s = sid, kind, list(projects), 60
        self.verdict, self.error, self.extra, self.calls = verdict, error, extra, []

    def ask(self, task, material, context):
        self.calls.append((task, material, context))
        if self.error:
            raise self.error
        return {"verdict": self.verdict, "probability": 0.9, "reasons": ["looks fine"], **self.extra}


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, data):
        self.events.append((kind, data))

    def of(self, kind):
        return [d for k, d in self.events if k == kind]


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.builtin, self.data = Path(tmp.name) / "builtin", Path(tmp.name) / "data"
        self.builtin.mkdir()
        env = patch.dict(os.environ, {"GUARDIAN_VERDICT_SOURCES_DIR": str(self.builtin),
                                      "GUARDIAN_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, directory: Path, spec, name=None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name or spec['id']}.json").write_text(json.dumps(spec))

    def test_the_builtin_jev_file_validates(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_VERDICT_SOURCES_DIR": ""}):
            os.environ.pop("GUARDIAN_VERDICT_SOURCES_DIR")
            sources = vs.load_sources()
        self.assertEqual(sources["jev"]["kind"], "jev")
        self.assertEqual(sources["jev"]["source"], "builtin")

    def test_valid_file_loads_and_builds(self) -> None:
        self.write(self.builtin, JEV)
        sources = vs.load_sources()
        self.assertEqual(sources["jev"]["roles"], ["decide", "alarm"])
        built = vs.build_all()
        self.assertEqual([(s.id, s.kind) for s in built], [("jev", "jev")])

    def test_refusals(self) -> None:
        cases = {
            "bad id": dict(JEV, id="Jev"),
            "unknown key": dict(JEV, command="rm -rf /"),
            "unknown role": dict(JEV, roles=["boss"]),
            "no roles": dict(JEV, roles=[]),
            "unknown project kind": dict(JEV, projects=["web"]),
            "mcp not built": dict(JEV, kind="mcp"),
            "command not built": dict(JEV, kind="command"),
            "old question set": dict(JEV, questions=1),
            "crossed thresholds": dict(JEV, approve_at=0.2, reject_at=0.8),
            "future review": dict(JEV, reviewed=(datetime.date.today() + datetime.timedelta(days=2)).isoformat()),
            "bad bar": dict(JEV, shadow_bar={"min_verdicts": 0, "min_agreement": 0.9}),
            "bad timeout": dict(JEV, timeout_s=0),
        }
        for label, spec in cases.items():
            with self.subTest(label), self.assertRaises(vs.SourceError):
                vs.validate(spec)
        with self.assertRaisesRegex(vs.SourceError, "phase S3"):
            vs.validate(dict(JEV, kind="mcp"))

    def test_an_id_in_both_stores_is_an_error(self) -> None:
        self.write(self.builtin, JEV)
        self.write(self.data / "verdicts", JEV)
        with self.assertRaisesRegex(vs.SourceError, "builtin"):
            vs.load_sources()
        code, payload = vs.report(self.data)
        self.assertEqual((code, payload["ok"]), (2, False))

    def test_stale_is_a_warning(self) -> None:
        self.write(self.builtin, dict(JEV, reviewed="2020-01-01"))
        code, payload = vs.report(self.data)
        self.assertEqual(code, 0)
        self.assertEqual([s["id"] for s in payload["stale"]], ["jev"])

    def test_env_thresholds_override_the_file(self) -> None:
        source = vs.JevSource(vs.validate(dict(JEV, approve_at=0.7, reject_at=0.3)), asker=object())
        self.assertEqual(source.limits(), {"yes": 0.7, "no": 0.3})
        with patch.dict(os.environ, {"GUARDIAN_JEV_VERDICT_YES": "0.9", "GUARDIAN_JEV_VERDICT_NO": "0.1"}):
            self.assertEqual(source.limits(), {"yes": 0.9, "no": 0.1})

    def test_mode(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_VERDICT_SOURCES": "shadow", "GUARDIAN_JEV_VERDICT": "off"}):
            self.assertEqual(vs.mode(), vs.SHADOW)
        with patch.dict(os.environ, {"GUARDIAN_VERDICT_SOURCES": "", "GUARDIAN_JEV_VERDICT": "shadow"}):
            self.assertEqual(vs.mode(), vs.SHADOW)    # the J1 switch the hub has
        with patch.dict(os.environ, {"GUARDIAN_VERDICT_SOURCES": "on", "GUARDIAN_JEV_VERDICT": "off"}):
            self.assertEqual(vs.mode(), vs.OFF)


class ShadowTests(unittest.TestCase):
    def run_shadow(self, sources, approved=False, kind="code"):
        rec = Recorder()
        out = vs.run_shadow(sources, "task", "material", {"project": "p", "kind": kind}, approved, rec, say=lambda _: None)
        return rec, out

    def test_agreement_and_disagreement(self) -> None:
        rec, out = self.run_shadow([FakeSource("yes", vs.APPROVE), FakeSource("no", vs.REJECT),
                                    FakeSource("maybe", vs.UNCERTAIN)], approved=False)
        by_id = {d["source"]: d for d in rec.of("verdict_source")}
        self.assertEqual((by_id["yes"]["agree"], by_id["no"]["agree"], by_id["maybe"]["agree"]), (False, True, None))
        self.assertEqual(by_id["yes"]["role"], "shadow")
        self.assertEqual(by_id["yes"]["reasons"], ["looks fine"])
        self.assertEqual([d["source"] for d in rec.of("verdict_disagreement")], ["yes"])
        self.assertEqual(len(out), 3)

    def test_a_failing_source_is_uncertain_and_the_rest_still_run(self) -> None:
        rec, _ = self.run_shadow([FakeSource("boom", error=RuntimeError("down")), FakeSource("ok", vs.REJECT),
                                  FakeSource("empty", verdict="maybe")])
        by_id = {d["source"]: d for d in rec.of("verdict_source")}
        self.assertEqual(by_id["boom"]["verdict"], vs.UNCERTAIN)
        self.assertIn("down", by_id["boom"]["error"])
        self.assertIn("no verdict", by_id["empty"]["error"])
        self.assertTrue(by_id["ok"]["agree"])

    def test_project_kind_filter(self) -> None:
        only_ops = FakeSource("ops-only", vs.APPROVE, projects=("ops",))
        rec, _ = self.run_shadow([only_ops], kind="code")
        self.assertEqual((rec.events, only_ops.calls), ([], []))

    def test_jev_source_still_writes_the_j1_event(self) -> None:
        record = jv.parse_answer(ANSWER, LIMITS)
        jev = FakeSource("jev", kind="jev", **{k: v for k, v in record.items()})
        jev.verdict = record["verdict"]
        rec, _ = self.run_shadow([jev], approved=False)
        legacy = rec.of("jev_verdict")
        self.assertEqual(len(legacy), 1)
        self.assertEqual((legacy[0]["verdict"], legacy[0]["llm"], legacy[0]["agree"], legacy[0]["mode"]),
                         (vs.APPROVE, vs.REJECT, False, jv.SHADOW))
        self.assertNotIn("probability", legacy[0])
        self.assertEqual(rec.of("verdict_disagreement")[0]["jev"], vs.APPROVE)
        failing = FakeSource("jev", kind="jev", error=jv.JevVerdictError("jev down"))
        rec, _ = self.run_shadow([failing])
        self.assertIn("jev down", rec.of("jev_verdict")[0]["error"])

    def test_a_broken_event_writer_does_not_escape(self) -> None:
        def emit(kind, data):
            raise OSError("disk full")
        out = vs.run_shadow([FakeSource("x", vs.APPROVE)], "t", "m", {"kind": "code"}, True, emit, say=lambda _: None)
        self.assertEqual(out[0]["verdict"], vs.APPROVE)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"GUARDIAN_JEV_GATE": "off", "GUARDIAN_VERDICT_SOURCES": "shadow",
                                      "GUARDIAN_JEV_VERDICT": "off"})
        env.start()
        self.addCleanup(env.stop)
        manager = TeamManager(self.root / "teams")
        manager.save(dict(BASE, max_rounds=1))
        self.engine = DebateEngine(db_path=self.root / "d.db", events=EventWriter(str(self.root / "events.jsonl")),
                                   team_mgr=manager)

    def fake(self, prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_):
        prompt = prompt + (instructions.text() if instructions else "")
        if JUDGE_VERDICT_RULE in prompt:
            return LLMResult("claude-cli", "Fine.\nVERDICT: APPROVED", "s")
        return LLMResult(f"{providers[0]}-cli", "text with app.py:1", "s")

    def test_an_injected_source_runs_in_shadow_and_changes_nothing(self) -> None:
        source = FakeSource("scan", vs.REJECT)
        self.engine.verdict_sources = [source]
        with patch.object(self.engine.llm, "run_session_prompt", side_effect=self.fake), \
                patch.object(self.engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    self.engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or self.root / "l.md")):
            try:
                self.engine.run_debate("Improve hello", project_id="server-guardian", team="flowteam")
            finally:
                self.engine.events.close()
        events = [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]
        task, material, context = source.calls[0]
        self.assertIn("Critique", material)
        self.assertEqual((context["project"], context["kind"]), ("server-guardian", "code"))
        shadow = [e for e in events if e["type"] == "verdict_source"]
        self.assertEqual((shadow[0]["data"]["source"], shadow[0]["seat"], shadow[0]["data"]["agree"]),
                         ("scan", "close", False))
        self.assertTrue([e for e in events if e["type"] == "verdict_disagreement"])
        self.assertFalse([e for e in events if e["type"] == "jev_verdict"])
        self.assertEqual([e["data"]["approved"] for e in events if e["type"] == "verdict"], [True])


class ReportTests(unittest.TestCase):
    def test_both_event_shapes_without_double_counting(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "debate-jobs"
            record = jv.compare(jv.parse_answer(ANSWER, LIMITS), True)

            def job(name, events):
                (jobs / name).mkdir(parents=True)
                (jobs / name / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

            started = {"type": "debate_started", "data": {"project": "p"}}
            # an old job: only the J1 event
            job("a", [started, {"type": "jev_verdict", "round": 1, "data": record}])
            # a new job: both shapes for the same turn, counted once
            job("b", [started, {"type": "verdict_source", "round": 1,
                                "data": {**record, "source": "jev", "kind": "jev", "role": "shadow", "probability": 0.82}},
                      {"type": "jev_verdict", "round": 1, "data": record}])
            # another source, and a failure that does not count
            job("c", [started, {"type": "verdict_source", "round": 1,
                                "data": {"source": "scan", "kind": "fake", "role": "shadow", "verdict": vs.REJECT,
                                         "llm": vs.APPROVE, "agree": False}},
                      {"type": "verdict_source", "round": 2,
                       "data": {"source": "scan", "kind": "fake", "role": "shadow", "verdict": vs.UNCERTAIN,
                                "error": "down", "llm": vs.APPROVE, "agree": None}}])
            rows = vs.collect(jobs)
            self.assertEqual(len(rows["jev"]), 2)
            self.assertEqual(len(rows["scan"]), 1)
            self.assertEqual(rows["jev"][0]["kind"], "code")    # the project's kind, not the source's
            summary = vs.summarize(rows["jev"], {"min_verdicts": 2, "min_agreement": 0.9})
            self.assertEqual((summary["verdicts"], summary["sure"], summary["agreement"]), (2, 2, 1.0))
            self.assertTrue(summary["bar"]["met"])
            self.assertFalse(vs.summarize(rows["jev"], {"min_verdicts": 30, "min_agreement": 0.9})["bar"]["met"])
            code, payload = vs.report(root)
            self.assertEqual(code, 0)
            self.assertEqual(payload["shadow"]["jev"]["verdicts"], 2)
            self.assertIn("jev_approves_llm_rejects", payload["shadow"]["jev"])


if __name__ == "__main__":
    unittest.main()

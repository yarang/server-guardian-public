from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from debate_engine import DebateEngine
from db_manager import DecisionDBManager
from llm_runner import BUILTIN_PROVIDER, LLMResult
from testenv import isolate_data_dir, restore_data_dir



def setUpModule() -> None:
    # These tests cover the LLM debate flow; the jev gate has its own tests.
    import os
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # Keep the symbol index cache (and anything else) out of the real data directory.
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()

class VerdictParsingTests(unittest.TestCase):
    def test_explicit_approval(self) -> None:
        self.assertTrue(DebateEngine._parse_verdict("Looks good.\nVERDICT: APPROVED"))

    def test_last_verdict_line_wins(self) -> None:
        self.assertFalse(DebateEngine._parse_verdict("Earlier draft: VERDICT: APPROVED\nVERDICT: **REJECTED**"))

    def test_verdict_line_beats_body_mentions(self) -> None:
        self.assertTrue(DebateEngine._parse_verdict("The critic REJECTED option B.\nVERDICT: `APPROVED`"))

    def test_first_token_without_verdict_line(self) -> None:
        self.assertTrue(DebateEngine._parse_verdict("# 최종 판정: **APPROVED**\n인증 없이 기동하면 거부합니다. REJECTED 대안은 제외"))
        self.assertFalse(DebateEngine._parse_verdict("REJECTED: the proposal is not APPROVED"))

    def test_not_approved_is_rejection(self) -> None:
        self.assertFalse(DebateEngine._parse_verdict("Not approved yet, needs work"))

    def test_korean_consensus_words_alone_do_not_approve(self) -> None:
        self.assertFalse(DebateEngine._parse_verdict("최종 합의에 이르지 못했습니다"))

    def test_word_boundary(self) -> None:
        self.assertFalse(DebateEngine._parse_verdict("UNAPPROVEDCHANGES detected"))


class UnverifiedDebateTests(unittest.TestCase):
    def test_builtin_fallback_is_never_approved_and_stops_after_one_round(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=3, db_path=Path(tmp) / "decisions.db")
            engine.llm.last_error = "claude exited with code 1: 401"
            canned = LLMResult(BUILTIN_PROVIDER, "⚖️ CONSENSUS APPROVED")
            with patch.object(engine.llm, "run_session_prompt", return_value=canned):
                with patch.object(engine, "_save_debate_log", return_value=Path(tmp) / "log.md") as save:
                    result = engine.run_debate("test task", project_id="server-guardian")

        self.assertFalse(result["consensus_reached"])
        self.assertTrue(result["final_decision"]["unverified"])
        self.assertEqual(result["final_decision"]["llm_error"], "claude exited with code 1: 401")
        self.assertEqual({step["round"] for step in result["transcript"]}, {1})
        save.assert_called_once()

    def test_real_provider_approval_is_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=3, db_path=Path(tmp) / "decisions.db")
            approved = LLMResult("claude-cli", "Verdict: APPROVED", "sid-1")
            with patch.object(engine.llm, "run_session_prompt", return_value=approved):
                with patch.object(engine, "_save_debate_log", return_value=Path(tmp) / "log.md"):
                    result = engine.run_debate("test task", project_id="server-guardian")

        self.assertTrue(result["consensus_reached"])
        self.assertFalse(result["final_decision"]["unverified"])

    def test_decision_status(self) -> None:
        self.assertEqual(DebateEngine._decision_status({"approved": False, "unverified": True}), "UNVERIFIED")
        self.assertEqual(DebateEngine._decision_status({"approved": True, "unverified": False}), "APPROVED")
        self.assertEqual(DebateEngine._decision_status(None), "REVISION_REQUIRED")
        self.assertEqual(DebateEngine._decision_status({"approved": False}), "REVISION_REQUIRED")

    def test_decision_status_is_the_word_the_database_stores(self) -> None:
        decisions = [None, {"approved": False}, {"approved": True}, {"approved": False, "unverified": True},
                     {"approved": True, "jev_gate": "DISPUTED"}, {"approved": True, "jev_gate": "PENDING"},
                     {"approved": True, "jev_gate": "PASSED"}]
        with TemporaryDirectory() as tmp:
            db = DecisionDBManager(Path(tmp) / "d.db")
            for index, decision in enumerate(decisions):
                thread = f"thread_{index}"
                db.save_debate_session("p", thread, "task", [], decision)
                self.assertEqual(db.get_thread_status(thread), DebateEngine._decision_status(decision), decision)


class SessionResumeTests(unittest.TestCase):
    def test_sessions_are_saved_then_resumed_without_replaying_history(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "decisions.db")
            calls = []

            def fake(prompt, context=None, session_id=None, provider=None):
                calls.append({"prompt": prompt, "session_id": session_id, "provider": provider})
                sid = session_id or f"sid-{len(calls)}"
                return LLMResult("codex-cli", "Needs work.\nVERDICT: REJECTED", sid, resumed=session_id is not None)

            with patch.object(engine.llm, "run_session_prompt", side_effect=fake):
                with patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4]) or Path(tmp) / "log.md"
                )):
                    first = engine.run_debate("task", project_id="server-guardian")
                    engine.run_debate("task", project_id="server-guardian", thread_id=first["thread_id"])

            first_round, second_round = calls[:3], calls[3:]
            self.assertTrue(all(c["session_id"] is None for c in first_round))
            self.assertEqual([c["session_id"] for c in second_round], ["sid-1", "sid-2", "sid-3"])
            self.assertTrue(all(c["provider"] == "codex" for c in second_round))
            self.assertTrue(all("Earlier rounds in this thread" not in c["prompt"] for c in second_round))
            self.assertIn("VERDICT: REJECTED", second_round[0]["prompt"])  # previous critique carried over

            sessions = engine.db.list_agent_sessions(first["thread_id"])
            self.assertEqual({s["role"] for s in sessions}, {"Domain Expert", "Critic Agent", "JEV-MCP Judge"})

    def test_continuing_a_thread_keeps_its_original_task(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "decisions.db")
            prompts = []

            def fake(prompt, context=None, session_id=None, provider=None):
                prompts.append(prompt)
                return LLMResult("codex-cli", "VERDICT: REJECTED", session_id or f"sid-{len(prompts)}")

            with patch.object(engine.llm, "run_session_prompt", side_effect=fake):
                with patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4]) or Path(tmp) / "log.md"
                )):
                    first = engine.run_debate("Original topic", project_id="server-guardian")
                    second = engine.run_debate("Restate and finalize", project_id="server-guardian", thread_id=first["thread_id"])

            self.assertEqual(second["task"], "Original topic")
            self.assertEqual(second["follow_up"], "Restate and finalize")
            self.assertEqual(engine.db.get_thread_history(first["thread_id"])["task"], "Original topic")
            self.assertIn("Task: Original topic\nFollow-up instruction: Restate and finalize", prompts[3])
            self.assertIsNone(first["follow_up"])

    def test_a_stored_lineup_with_a_deleted_persona_falls_back_to_the_default_with_a_warning(self) -> None:
        import json
        import sqlite3

        from debate_events import EventWriter

        with TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "decisions.db", events=EventWriter(str(events_path)))
            reply = LLMResult("codex-cli", "VERDICT: REJECTED", "sid")
            with patch.object(engine.llm, "run_session_prompt", return_value=reply) as run:
                with patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or Path(tmp) / "log.md"
                )):
                    first = engine.run_debate("topic", project_id="server-guardian")
                    self.assertNotIn("warnings", first)
                    # The persona the thread's critic used is deleted afterwards.
                    snapshot = engine.db.get_thread_history(first["thread_id"])["team"]
                    snapshot["critic"][0]["persona"] = "gone_persona"
                    with sqlite3.connect(engine.db.db_path) as conn:
                        conn.execute("UPDATE threads SET team_snapshot = ? WHERE thread_id = ?",
                                     (json.dumps(snapshot), first["thread_id"]))
                    run.reset_mock()
                    second = engine.run_debate("more", project_id="server-guardian", thread_id=first["thread_id"])

            self.assertEqual(second["team"]["id"], "default")
            self.assertEqual(second["team"]["critic"][0]["persona"], "critic_redteam")
            self.assertEqual(len(second["warnings"]), 1)
            self.assertIn("gone_persona", second["warnings"][0])
            self.assertIn("default lineup", second["warnings"][0])
            started = [json.loads(line) for line in events_path.read_text().splitlines()]
            started = [e for e in started if e["type"] == "debate_started"]
            self.assertEqual(started[-1]["data"]["warnings"], second["warnings"])
            self.assertNotIn("warnings", started[0]["data"])
            engine.events.close()

    def test_a_thread_cannot_switch_projects(self) -> None:
        from project_registry import ProjectManager, ProjectValidationError

        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "decisions.db")
            engine.project_mgr = ProjectManager(directory=Path(tmp) / "projects")
            engine.project_mgr.save({"id": "other", "repo": "https://git.example.dev/me/other.git"})
            reply = LLMResult("codex-cli", "VERDICT: REJECTED", "sid")
            with patch.object(engine.llm, "run_session_prompt", return_value=reply):
                with patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4]) or Path(tmp) / "log.md"
                )):
                    first = engine.run_debate("topic", project_id="server-guardian")
                    with self.assertRaisesRegex(ProjectValidationError, "belongs to project 'server-guardian'"):
                        engine.run_debate("more", project_id="other", thread_id=first["thread_id"])

    def test_fresh_session_on_existing_thread_gets_history(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "decisions.db")
            with patch.object(engine.llm, "run_session_prompt", return_value=LLMResult("opencode-cli", "x")) as run:
                engine._ask("t1", "Critic Agent", "Critique this", {}, "[Round 1] Domain Expert: plan")

            self.assertIn("Earlier rounds in this thread", run.call_args.args[0])
            self.assertEqual(engine.db.list_agent_sessions("t1"), [])  # stateless provider stores nothing


class AgentSessionStoreTests(unittest.TestCase):
    def test_save_update_list_and_prune(self) -> None:
        with TemporaryDirectory() as tmp:
            db = DecisionDBManager(Path(tmp) / "d.db")
            db.save_agent_session("t1", "Critic Agent", "claude", "s1")
            db.save_agent_session("t1", "Critic Agent", "codex", "s2")

            row = db.get_agent_session("t1", "Critic Agent")
            self.assertEqual((row["provider"], row["session_id"]), ("codex", "s2"))
            self.assertEqual(len(db.list_agent_sessions()), 1)
            self.assertEqual(db.prune_agent_sessions(1), 0)

            with db._get_connection() as conn:
                conn.execute("UPDATE agent_sessions SET last_used_at = datetime('now', '-10 days');")
                conn.commit()
            self.assertEqual(db.prune_agent_sessions(7), 1)
            self.assertIsNone(db.get_agent_session("t1", "Critic Agent"))


if __name__ == "__main__":
    unittest.main()

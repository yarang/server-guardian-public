import io
import json
import os
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import guardian
from db_manager import DecisionDBManager
from session_ops import open_in_tmux, pick_session, tmux_open_commands, window_name

STORED = {"thread_id": "thread_20261003_070000_abc123", "role": "Critic Agent", "provider": "claude", "session_id": "s-1"}



def setUpModule() -> None:
    # These tests cover the LLM debate flow; the jev gate has its own tests.
    import os
    os.environ["GUARDIAN_JEV_GATE"] = "off"

class SessionOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._env = patch.dict(os.environ, {"GUARDIAN_LLM_WORKDIR": self._tmp.name, "GUARDIAN_TMUX_SESSION": "gtest", "GUARDIAN_LLM_ENV": "/tmp/gtest-llm.env"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_window_name(self) -> None:
        self.assertEqual(window_name(STORED["thread_id"], "JEV-MCP Judge"), "abc123-jev-mcp-judge")

    def test_new_tmux_session_when_none_exists(self) -> None:
        [command] = tmux_open_commands(STORED, has_session=False)
        self.assertEqual(command[:5], ["tmux", "new-session", "-d", "-s", "gtest"])
        self.assertEqual(command[command.index("-c") + 1], self._tmp.name)
        self.assertTrue(command[-1].endswith("; exec claude --resume s-1"))
        self.assertIn(". /tmp/gtest-llm.env", command[-1])

    def test_new_window_in_existing_session(self) -> None:
        [command] = tmux_open_commands({**STORED, "provider": "codex"}, has_session=True)
        self.assertEqual(command[:4], ["tmux", "new-window", "-t", "gtest:"])
        self.assertTrue(command[-1].endswith("exec codex resume s-1"))

    @patch("session_ops.subprocess.run")
    @patch("session_ops.shutil.which", return_value="/usr/bin/tmux")
    def test_open_in_tmux_runs_commands(self, _which: MagicMock, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(returncode=1)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            hint = open_in_tmux(STORED)

        self.assertEqual(hint, "tmux attach -t gtest")
        self.assertEqual(run.call_args_list[1].args[0][1], "new-session")

    @patch("session_ops.shutil.which", return_value=None)
    def test_open_in_tmux_requires_tmux(self, _which: MagicMock) -> None:
        with self.assertRaises(RuntimeError):
            open_in_tmux(STORED)

    def test_wrapped_command_sources_env_file_in_a_real_shell(self) -> None:
        import subprocess
        from session_ops import shell_with_llm_env

        env_file = Path(self._tmp.name) / "llm.env"
        env_file.write_text("export CLAUDE_CODE_OAUTH_TOKEN='tok-123'\n", encoding="utf-8")
        with patch.dict(os.environ, {"GUARDIAN_LLM_ENV": str(env_file)}):
            line = shell_with_llm_env(["sh", "-c", 'echo "$CLAUDE_CODE_OAUTH_TOKEN"'])
        self.assertNotIn("tok-123", line)
        out = subprocess.run(["sh", "-c", line], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(out.stdout.strip(), "tok-123")

    def test_pick_session(self) -> None:
        sessions = [{**STORED, "role": r} for r in ("Domain Expert", "Critic Agent", "JEV-MCP Judge")]
        self.assertEqual(pick_session(sessions, "critic")["role"], "Critic Agent")
        self.assertEqual(pick_session(sessions[:1], None)["role"], "Domain Expert")
        with self.assertRaises(LookupError):
            pick_session(sessions, None)
        with self.assertRaises(LookupError):
            pick_session(sessions, "nobody")


def _args(**overrides) -> Namespace:
    base = dict(debate=None, thread=None, project="p", rounds=1, sessions=None, attach=None, role=None,
                prune_sessions=None, json=False, team=None, teams=False, result_file=None, actor=None, context_path=None, context_ref=None,
                personas=False, persona_save=False, persona_delete=None, team_save=False, team_delete=None,
                actions=None, show_action=None, run_action=None, reject_action=None, yes=False,
                jev_status=None, jev_recheck=None, projects=False, project_save=False, project_delete=None,
                project_sync=None, groups=False, group_save=False, group_delete=None, group_preview=False)
    base.update(overrides)
    return Namespace(**base)


class GuardianSessionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = DecisionDBManager(Path(self._tmp.name) / "d.db")
        self._patch = patch("db_manager.DecisionDBManager", return_value=self.db)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _run(self, **overrides) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertTrue(guardian.handle_session_commands(_args(**overrides)))
        return out.getvalue()

    def test_not_handled_without_session_options(self) -> None:
        self.assertFalse(guardian.handle_session_commands(_args()))

    def test_list_sessions(self) -> None:
        self.assertIn("No persisted agent sessions", self._run(sessions=""))
        self.db.save_agent_session("t1", "Critic Agent", "claude", "s-1")
        self.assertIn("t1  Critic Agent", self._run(sessions="t1"))

    def test_prune(self) -> None:
        self.assertIn("Removed 0 session(s)", self._run(prune_sessions=7))

    def test_attach_opens_tmux(self) -> None:
        self.db.save_agent_session("t1", "Critic Agent", "claude", "s-1")
        with patch("session_ops.open_in_tmux", return_value="tmux attach -t guardian") as opened:
            output = self._run(attach="t1", role="crit")

        self.assertEqual(opened.call_args.args[0]["session_id"], "s-1")
        self.assertIn("tmux attach -t guardian", output)

    def test_debate_reports_status(self) -> None:
        engine = MagicMock()
        engine.run_debate.return_value = {
            "thread_id": "t9", "consensus_reached": False, "log_file": "x.md",
            "final_decision": {"unverified": True},
        }
        with patch("debate_engine.DebateEngine", return_value=engine):
            output = self._run(debate="task", thread="t9")

        engine.run_debate.assert_called_once_with("task", project_id="p", thread_id="t9", team=None)
        self.assertIn("Status: UNVERIFIED", output)

    def test_debate_writes_result_file(self) -> None:
        engine = MagicMock()
        engine.run_debate.return_value = {"thread_id": "t9", "log_file": "x.md", "final_decision": None}
        result_file = Path(self._tmp.name) / "result.json"
        with patch("debate_engine.DebateEngine", return_value=engine):
            self._run(debate="task", team="panel", result_file=str(result_file))

        self.assertEqual(json.loads(result_file.read_text())["thread_id"], "t9")

    def test_invalid_team_exits_2(self) -> None:
        from team_manager import TeamValidationError

        engine = MagicMock()
        engine.run_debate.side_effect = TeamValidationError("no team 'x'")
        with patch("debate_engine.DebateEngine", return_value=engine), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self._run(debate="task", team="x")
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

"""The zai provider: the claude binary run as a separate identity against z.ai's Anthropic-compatible endpoint."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

from env_policy import provider_env
from llm_runner import DEFAULT_PROVIDER_ORDER, LLMRunner, SessionInstructions, interactive_resume_command

KEY = "zai-key-123"
OAUTH = "oauth-tok-9"


def setUpModule() -> None:
    os.environ["GUARDIAN_LLM_ENV"] = "/nonexistent/guardian-llm.env"


def _ok(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _stream(text: str = "hi", session_id: str = "s1") -> str:
    events = [{"type": "system", "subtype": "init", "session_id": session_id},
              {"type": "assistant", "session_id": session_id,
               "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
              {"type": "result", "subtype": "success", "is_error": False, "session_id": session_id, "result": text}]
    return "\n".join(json.dumps(e) for e in events)


def _runner(config_dir: str, extra_env: Optional[dict] = None, binaries=("claude",), providers=("zai",)) -> LLMRunner:
    env = {"GUARDIAN_ZAI_CONFIG_DIR": config_dir, "ZAI_API_KEY": KEY, "CLAUDE_CODE_OAUTH_TOKEN": OAUTH,
           "ANTHROPIC_API_KEY": "sk-ant-1", **(extra_env or {})}
    with patch.dict(os.environ, env, clear=False), \
            patch("llm_runner.shutil.which", side_effect=lambda c, **_: f"/bin/{c}" if c in binaries else None):
        return LLMRunner(providers=list(providers))


class DetectionTests(unittest.TestCase):
    def test_zai_follows_agy_in_the_default_fallback_order(self) -> None:
        self.assertEqual(DEFAULT_PROVIDER_ORDER[:4], ["claude", "codex", "agy", "zai"])

    def test_installed_needs_the_claude_binary_and_a_key(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(_runner(tmp).installed_providers, ["claude", "zai"])
            self.assertEqual(_runner(tmp, binaries=()).installed_providers, [])
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ZAI_API_KEY", None)
                with patch("llm_runner.shutil.which", side_effect=lambda c, **_: "/bin/claude" if c == "claude" else None):
                    runner = LLMRunner(providers=["zai"])
            self.assertEqual(runner.installed_providers, ["claude"])
            self.assertEqual(runner.available_providers, [])

    def test_claude_alone_does_not_make_zai_available(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = _runner(tmp, extra_env={"ZAI_API_KEY": ""}, providers=("claude", "zai"))
        self.assertEqual(runner.available_providers, ["claude"])


class EnvIsolationTests(unittest.TestCase):
    def test_zai_env_holds_only_zai_credentials(self) -> None:
        source = {"PATH": "/bin", "ZAI_API_KEY": KEY, "CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "ANTHROPIC_API_KEY": "sk-ant-1",
                  "ANTHROPIC_AUTH_TOKEN": "other", "OPENAI_API_KEY": "sk-o"}
        env = provider_env("zai", source)
        self.assertEqual(env.get("ZAI_API_KEY"), KEY)
        for leaked in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
            self.assertNotIn(leaked, env)

    def test_claude_never_receives_the_zai_key(self) -> None:
        env = provider_env("claude", {"PATH": "/bin", "ZAI_API_KEY": KEY, "CLAUDE_CODE_OAUTH_TOKEN": OAUTH})
        self.assertNotIn("ZAI_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)

    def test_process_env_maps_the_key_and_drops_the_other_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            env, _ = _runner(tmp)._claude_family_config("zai")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], KEY)
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], tmp)
            self.assertEqual(env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
            for absent in ("ZAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
                self.assertNotIn(absent, env)

    def test_endpoint_and_models_can_be_overridden(self) -> None:
        extra = {"ZAI_BASE_URL": "https://example.test/anthropic", "ZAI_MODEL": "glm-x", "ZAI_HAIKU_MODEL": "glm-small"}
        with TemporaryDirectory() as tmp:
            env, _ = _runner(tmp, extra)._claude_family_config("zai")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://example.test/anthropic")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "glm-x")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "glm-x")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "glm-small")

    def test_models_are_left_to_the_server_when_unset(self) -> None:
        with TemporaryDirectory() as tmp:
            env, _ = _runner(tmp)._claude_family_config("zai")
        self.assertFalse([k for k in env if k.startswith("ANTHROPIC_DEFAULT_")])

    def test_config_dir_is_private_and_skips_onboarding(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "claude-zai"
            _runner(str(target))._claude_family_config("zai")
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o700")
            self.assertTrue(json.loads((target / ".claude.json").read_text())["hasCompletedOnboarding"])

    def test_a_missing_key_fails_the_call_instead_of_borrowing_claude_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = _runner(tmp)
            runner.base_env.pop("ZAI_API_KEY")
            with self.assertRaises(RuntimeError):
                runner._claude_family_config("zai")

    def test_claude_config_is_unchanged_by_the_zai_variables(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = _runner(tmp, binaries=("claude",), providers=("claude",))
            env, _ = runner._claude_family_config("claude")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ZAI_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)


class InvocationTests(unittest.TestCase):
    @patch("llm_runner.subprocess.run")
    def test_session_turn_runs_claude_with_the_zai_env_and_reports_zai(self, run: MagicMock) -> None:
        run.return_value = _ok(_stream("answer", "sid-7"))
        with TemporaryDirectory() as tmp:
            result = _runner(tmp).run_session_prompt("hello", providers=["zai"])
        self.assertEqual((result.provider, result.text, result.session_id), ("zai-cli", "answer", "sid-7"))
        args, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(args[0], "claude")
        self.assertEqual(kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], KEY)
        self.assertNotIn(KEY, " ".join(args))

    @patch("llm_runner.subprocess.run")
    def test_instructions_become_the_system_prompt_for_zai_too(self, run: MagicMock) -> None:
        run.side_effect = [_ok("claude 2.1.289"), _ok("--append-system-prompt-file"), _ok(_stream("ok"))]
        with TemporaryDirectory() as tmp, patch("llm_runner._CLAUDE_CAPS", None):
            _runner(tmp).run_session_prompt("task", providers=["zai"],
                                            instructions=SessionInstructions(static="You are a critic.", dynamic=""))
        self.assertIn("--append-system-prompt", run.call_args.args[0])

    @patch("llm_runner.subprocess.run")
    def test_resume_uses_the_zai_session_and_config_dir(self, run: MagicMock) -> None:
        run.return_value = _ok(_stream("again", "sid-7"))
        with TemporaryDirectory() as tmp:
            result = _runner(tmp).run_session_prompt("more", session_id="sid-7", provider="zai", providers=["zai"])
        self.assertIn("--resume", run.call_args.args[0])
        self.assertTrue(result.resumed)
        self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], tmp)

    @patch("llm_runner.subprocess.run")
    def test_stateless_run_prompt_reaches_zai_after_claude_fails(self, run: MagicMock) -> None:
        run.side_effect = [SimpleNamespace(returncode=1, stdout="", stderr="login required"), _ok("glm says hi")]
        with TemporaryDirectory() as tmp:
            runner = _runner(tmp, providers=("claude", "zai"))
            name, text = runner.run_prompt("ping")
        self.assertEqual((name, text), ("zai-cli", "glm says hi"))
        claude_env, zai_env = (c.kwargs["env"] for c in run.call_args_list)
        self.assertEqual(claude_env["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)
        self.assertNotIn("ZAI_API_KEY", claude_env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", zai_env)

    @patch("llm_runner.subprocess.run")
    def test_failure_is_labelled_zai(self, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="401 invalid key")
        with TemporaryDirectory() as tmp:
            result = _runner(tmp).run_session_prompt("hello", providers=["zai"])
        self.assertTrue(any(f.startswith("zai:") for f in result.failures))


class ResumeCommandTests(unittest.TestCase):
    def test_resume_runs_the_claude_binary(self) -> None:
        self.assertEqual(interactive_resume_command("zai", "sid-7"), ["claude", "--resume", "sid-7"])

    def test_tmux_line_selects_zai_without_putting_the_key_in_the_command(self) -> None:
        from session_ops import shell_with_llm_env
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"GUARDIAN_ZAI_CONFIG_DIR": tmp, "GUARDIAN_LLM_ENV": f"{tmp}/e"}):
            line = shell_with_llm_env(["claude", "--resume", "sid-7"], provider="zai")
        self.assertIn('ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY"', line)
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN", line)
        self.assertIn(f"CLAUDE_CONFIG_DIR={tmp}", line)
        self.assertNotIn(KEY, line)

    def test_other_providers_keep_the_plain_line(self) -> None:
        from session_ops import shell_with_llm_env
        self.assertNotIn("ZAI", shell_with_llm_env(["claude", "--resume", "x"]))


if __name__ == "__main__":
    unittest.main()

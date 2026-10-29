"""Phase-1 security hotfix: test command allowlist, risk level, and child-process environments."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from env_policy import is_secret_name, provider_env, tool_env
from llm_runner import LLMRunner
from mcp_client import ActionValidationError, MCPClient

SECRETS = {
    "AGENT_TOKEN": "agent-secret",
    "TYPESAFE_API_KEY": "jev-secret",
    "GUARDIAN_WORKSPACE": "/x",
    "OPENAI_API_KEY": "openai-secret",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
    "GEMINI_API_KEY": "gemini-secret",
    "DB_PASSWORD": "db-secret",
    "GOAUTH": "go-secret",
}
SAFE = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "GOPATH": "/go", "GIT_AUTHOR_NAME": "x"}


class TestCommandAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.mcp = MCPClient(workspace=Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ok(self, cmd: str) -> None:
        self.mcp.validate("jev_mcp:run_tests", {"test_cmd": cmd})

    def bad(self, cmd: str) -> None:
        with self.assertRaises(ActionValidationError, msg=cmd):
            self.mcp.validate("jev_mcp:run_tests", {"test_cmd": cmd})

    def test_bypasses_from_the_analysis_are_refused(self) -> None:
        for cmd in ("go test -exec /bin/sh ./...", "go test -toolexec=/tmp/x ./...", "go test -c -o /tmp/x ./...",
                    "go test -ldflags=-X ./...", "pytest -p evil_plugin", "pytest -c /etc/passwd",
                    "pytest --rootdir=/", "python3 -m pytest -p x", "python3 -m unittest -t /",
                    "npm test -- --exec", "npm run build --prefix /tmp", "go test ../outside/...",
                    "go test /abs/path", "python3 -m unittest discover -s ../..", "pytest -k"):
            self.bad(cmd)

    def test_normal_commands_still_pass(self) -> None:
        for cmd in ("go test ./...", "go test -race -count=1 -run TestX ./apps/agent/...", "go test -v .",
                    "pytest -q", "pytest -q -k 'auth and not slow' tests/test_x.py", "python3 -m pytest -x",
                    "python3 -m unittest discover -s cli/llm-agent -p test_db_backup.py",
                    "python3 -m unittest test_mcp_actions", "npm run build", "npm test"):
            self.ok(cmd)

    def test_run_tests_is_high_risk(self) -> None:
        self.assertEqual(self.mcp.tool("jev_mcp:run_tests")["risk"], "high")

    def test_tool_subprocesses_get_no_secrets(self) -> None:
        with patch.dict(os.environ, {**SECRETS, **SAFE}), patch("mcp_client.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "", ""
            self.mcp.call_tool("jev_mcp:run_tests", {"test_cmd": "pytest -q"})
        env = run.call_args.kwargs["env"]
        for name in SECRETS:
            self.assertNotIn(name, env)
        self.assertEqual(env["GOPATH"], "/go")
        self.assertEqual(env["GOFLAGS"], "-mod=readonly")


class EnvPolicyTests(unittest.TestCase):
    def test_tool_env(self) -> None:
        env = tool_env({**SECRETS, **SAFE})
        self.assertEqual(set(env), set(SAFE))

    def test_provider_env_keeps_only_own_credentials(self) -> None:
        source = {**SECRETS, **SAFE}
        claude = provider_env("claude", source)
        self.assertEqual(claude["CLAUDE_CODE_OAUTH_TOKEN"], "claude-secret")
        for name in ("AGENT_TOKEN", "TYPESAFE_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DB_PASSWORD"):
            self.assertNotIn(name, claude)
        codex = provider_env("codex", source)
        self.assertIn("OPENAI_API_KEY", codex)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", codex)
        self.assertNotIn("AGENT_TOKEN", provider_env("opencode", source))

    def test_secret_names(self) -> None:
        for name in ("AGENT_TOKEN", "X_API_KEY", "MY_SECRET", "GOAUTH", "SSH_PRIVATE_KEY"):
            self.assertTrue(is_secret_name(name), name)
        for name in ("GIT_AUTHOR_NAME", "PATH", "GOPATH"):
            self.assertFalse(is_secret_name(name), name)


class RunnerEnvTests(unittest.TestCase):
    def test_each_provider_cli_gets_a_minimal_env(self) -> None:
        with patch.dict(os.environ, {**SECRETS, **SAFE}), patch("llm_runner.load_llm_env", return_value={}):
            runner = LLMRunner(providers=["claude", "codex", "agy"])
        claude_env, _ = runner._claude_process_config()
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", claude_env)
        self.assertNotIn("AGENT_TOKEN", claude_env)
        self.assertNotIn("OPENAI_API_KEY", claude_env)
        self.assertTrue(claude_env["PATH"])
        for name in ("codex", "agy"):
            env = runner._provider_env(name)
            self.assertNotIn("AGENT_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn("TYPESAFE_API_KEY", env)


if __name__ == "__main__":
    unittest.main()

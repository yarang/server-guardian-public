import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from llm_runner import BUILTIN_PROVIDER, DEFAULT_PROVIDER_ORDER, LLMRunner, interactive_resume_command


def setUpModule() -> None:
    # Keep a real ~/.guardian/llm.env on the test host out of these tests.
    os.environ["GUARDIAN_LLM_ENV"] = "/nonexistent/guardian-llm.env"


def _ok(stdout: str = "Analysis result") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


def _runner(*providers: str, **kwargs) -> LLMRunner:
    with patch("llm_runner.shutil.which", side_effect=lambda c, **_: f"/bin/{c}" if c in providers else None):
        return LLMRunner(providers=list(providers), **kwargs)



def _claude_stream(*blocks, session_id: str = "abc", is_error: bool = False, result: str = None) -> str:
    """claude --output-format stream-json --verbose: init, one assistant event per content block, then a result event.

    A block is a text string or a (type, text) pair, e.g. ("thinking", "...").
    """
    events = [{"type": "system", "subtype": "init", "session_id": session_id}]
    for block in blocks:
        kind, text = block if isinstance(block, tuple) else ("text", block)
        content = {"type": kind, ("thinking" if kind == "thinking" else "text"): text}
        events.append({"type": "assistant", "session_id": session_id, "message": {"role": "assistant", "content": [content]}})
    texts = [b for b in blocks if not isinstance(b, tuple)]
    events.append({"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error,
                   "session_id": session_id, "result": result if result is not None else (texts[-1] if texts else "")})
    return "\n".join(json.dumps(e) for e in events)


def _codex_stream_for_mcp_test() -> str:
    return json.dumps({"type": "thread.started", "thread_id": "t1"})


def _agy_stream(response: str, conversation_id: str, status: str = "SUCCESS", error: str = "") -> str:
    """agy --output-format stream-json: an init event, progress, then one result event."""
    result = {"conversation_id": conversation_id, "status": status, "response": response}
    if error:
        result["error"] = error
    return "\n".join(
        json.dumps(e)
        for e in (
            {"event": "init", "conversation_id": conversation_id},
            {"event": "step_update", "step_update": {"text_delta": response}},
            {"event": "result", "result": result},
        )
    )

class ProviderDetectionTests(unittest.TestCase):
    @patch("llm_runner.shutil.which")
    def test_detects_installed_providers_in_default_order(self, which: MagicMock) -> None:
        which.side_effect = lambda command, **_: f"/bin/{command}" if command in {"agy", "claude", "codex"} else None
        with patch.dict(os.environ, {"GUARDIAN_LLM_PROVIDERS": ""}):
            runner = LLMRunner()

        self.assertEqual(runner.available_providers, ["claude", "codex", "agy"])
        self.assertEqual(runner.detected_cli, "claude")

    @patch("llm_runner.shutil.which", return_value="/bin/x")
    def test_provider_order_from_env(self, _which: MagicMock) -> None:
        with patch.dict(os.environ, {"GUARDIAN_LLM_PROVIDERS": "codex, agy"}):
            runner = LLMRunner()

        self.assertEqual(runner.available_providers, ["codex", "agy"])

    def test_default_order_prefers_subscription_clis(self) -> None:
        self.assertEqual(DEFAULT_PROVIDER_ORDER[:3], ["claude", "codex", "agy"])
        self.assertNotIn("gemini", DEFAULT_PROVIDER_ORDER)

    def test_local_bin_is_searched(self) -> None:
        with patch("llm_runner.shutil.which", return_value=None) as which:
            LLMRunner(providers=["agy"])
        self.assertEqual(which.call_args.kwargs["path"].split(os.pathsep)[0], str(Path.home() / ".local" / "bin"))


class ProviderInvocationTests(unittest.TestCase):
    @patch("llm_runner.subprocess.run")
    def test_claude_receives_prompt_on_stdin_without_tools(self, run: MagicMock) -> None:
        run.return_value = _ok()
        runner = _runner("claude")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUARDIAN_CLAUDE_CONFIG", None)
            provider, response = runner.run_prompt("Analyze metrics", {"cpu": 12})

        self.assertEqual((provider, response), ("claude-cli", "Analysis result"))
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            ["claude", "--print", "--output-format", "text", "--no-session-persistence", "--tools", "",
             "--effort", "medium"],
        )
        self.assertIn('"cpu": 12', run.call_args.kwargs["input"])

    @patch("llm_runner.subprocess.run")
    def test_codex_reads_last_message_file(self, run: MagicMock) -> None:
        def fake_run(args, **kwargs):
            Path(args[args.index("-o") + 1]).write_text("codex answer", encoding="utf-8")
            return _ok(stdout="progress noise")

        run.side_effect = fake_run
        provider, response = _runner("codex").run_prompt("Analyze")

        self.assertEqual((provider, response), ("codex-cli", "codex answer"))
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("read-only", command)
        self.assertEqual(command[-1], "-")
        self.assertIn("Analyze", run.call_args.kwargs["input"])

    @patch("llm_runner.subprocess.run")
    def test_agy_sends_prompt_on_stdin_not_argv(self, run: MagicMock) -> None:
        run.return_value = _ok(_agy_stream("agy answer", "c9"))
        provider, response = _runner("agy").run_prompt("Analyze SECRET-PROMPT")

        self.assertEqual((provider, response), ("agy-cli", "agy answer"))
        command = run.call_args.args[0]
        self.assertNotIn("SECRET-PROMPT", " ".join(command))
        self.assertIn("--print=", command)
        self.assertEqual(command[command.index("--input-format") + 1], "stream-json")
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent["event"], "user")
        self.assertIn("SECRET-PROMPT", sent["message"]["content"])

    @patch("llm_runner.subprocess.run")
    def test_agy_error_result_raises(self, run: MagicMock) -> None:
        run.return_value = _ok(_agy_stream("", "c9", status="ERROR", error="quota"))
        runner = _runner("agy")
        provider, _ = runner.run_prompt("Analyze")

        self.assertNotEqual(provider, "agy-cli")
        self.assertIn("agy status ERROR: quota", str(runner.last_error))

    @patch("llm_runner.subprocess.run")
    def test_runs_in_isolated_working_directory(self, run: MagicMock) -> None:
        run.return_value = _ok()
        _runner("claude").run_prompt("Analyze")

        workdir = run.call_args.kwargs["cwd"]
        self.assertIn("guardian_llm_", workdir)
        self.assertFalse(Path(workdir).exists())


class FailoverTests(unittest.TestCase):
    @patch("llm_runner.subprocess.run")
    def test_falls_over_to_next_provider(self, run: MagicMock) -> None:
        run.side_effect = [_fail("401 OAuth access token is invalid"), _ok(_agy_stream("agy answer", "c9"))]
        runner = _runner("claude", "agy")
        provider, response = runner.run_prompt("Analyze")

        self.assertEqual((provider, response), ("agy-cli", "agy answer"))
        self.assertIsNone(runner.last_error)

    @patch("llm_runner.subprocess.run")
    def test_all_failures_return_builtin_with_every_error(self, run: MagicMock) -> None:
        run.side_effect = [_fail("401"), subprocess.TimeoutExpired(cmd="agy", timeout=1)]
        runner = _runner("claude", "agy")
        with self.assertLogs("llm_runner", level="WARNING"):
            provider, _ = runner.run_prompt("Critique this proposal")

        self.assertEqual(provider, BUILTIN_PROVIDER)
        self.assertIn("claude: claude exited with code 1: 401", runner.last_error)
        self.assertIn("agy: timed out", runner.last_error)

    def test_no_providers_installed(self) -> None:
        runner = _runner()
        with self.assertLogs("llm_runner", level="WARNING"):
            provider, _ = runner.run_prompt("Analyze")

        self.assertEqual(provider, BUILTIN_PROVIDER)
        self.assertEqual(runner.last_error, "no supported LLM CLI found on PATH")


class ClaudeConfigTests(unittest.TestCase):
    def test_subscription_auth_by_default(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "keep"}):
            os.environ.pop("GUARDIAN_CLAUDE_CONFIG", None)
            runner = _runner("claude")
            process_env, timeout = runner._claude_process_config()
            # Only the minimal provider environment (env_policy), never the agent's own secrets.
            self.assertEqual(process_env["ANTHROPIC_API_KEY"], "keep")
            self.assertEqual(process_env["HOME"], os.environ["HOME"])
            self.assertNotIn("AGENT_TOKEN", process_env)

        self.assertIsNone(runner.claude_config_path)
        self.assertEqual(timeout, runner.timeout)

    def test_effort_flag(self) -> None:
        runner = _runner("claude")
        cases = [({}, ["--effort", "medium"]), ({"GUARDIAN_CLAUDE_EFFORT": "Low"}, ["--effort", "low"]),
                 ({"GUARDIAN_CLAUDE_EFFORT": "default"}, []), ({"GUARDIAN_CLAUDE_EFFORT": "bogus"}, [])]
        for extra, expected in cases:
            runner.base_env = {k: v for k, v in runner.base_env.items() if k != "GUARDIAN_CLAUDE_EFFORT"} | extra
            self.assertEqual(runner._claude_effort_args(), expected, extra)

    def test_loads_endpoint_override_only_when_configured(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "zai.json"
            config_path.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_AUTH_TOKEN": "test-token",
                            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                            "API_TIMEOUT_MS": "3000000",
                            "IGNORED_SECRET": "must-not-load",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GUARDIAN_CLAUDE_CONFIG": str(config_path)}):
                runner = _runner("claude")
                process_env, timeout = runner._claude_process_config()

        self.assertEqual(process_env["ANTHROPIC_AUTH_TOKEN"], "test-token")
        self.assertEqual(process_env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertNotIn("IGNORED_SECRET", process_env)
        self.assertEqual(timeout, 3000.0)

    def test_timeout_from_env(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_LLM_TIMEOUT": "42"}):
            self.assertEqual(_runner("claude").timeout, 42.0)


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        env = {"GUARDIAN_LLM_WORKDIR": self._tmp.name}
        self._env = patch.dict(os.environ, env)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    @patch("llm_runner.subprocess.run")
    def test_claude_new_then_resume(self, run: MagicMock) -> None:
        run.return_value = _ok(_claude_stream("hi"))
        runner = _runner("claude")

        first = runner.run_session_prompt("Analyze")
        self.assertEqual((first.provider, first.text, first.session_id, first.resumed), ("claude-cli", "hi", "abc", False))
        self.assertIn("--session-id", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["cwd"], self._tmp.name)

        second = runner.run_session_prompt("Again", session_id="abc", provider="claude")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--resume") + 1], "abc")
        self.assertTrue(second.resumed)

    @patch("llm_runner.subprocess.run")
    def test_default_header_is_kept_for_callers_without_a_system_line(self, run: MagicMock) -> None:
        run.return_value = _ok(_claude_stream("hi"))
        runner = _runner("claude")
        runner.run_session_prompt("Analyze")
        self.assertTrue(run.call_args.kwargs["input"].startswith("System: You are an expert Linux System & SSH Security Engineer."))
        runner.run_session_prompt("Again", session_id="abc", provider="claude")
        self.assertIn("SSH Security Engineer", run.call_args.kwargs["input"])  # unchanged for debates

    @patch("llm_runner.subprocess.run")
    def test_system_line_replaces_the_ssh_role_and_a_resumed_turn_is_sent_as_it_is(self, run: MagicMock) -> None:
        run.return_value = _ok(_claude_stream("hi"))
        runner = _runner("claude")
        runner.run_session_prompt("Task one", system="You are a documentation writer.")
        first = run.call_args.kwargs["input"]
        self.assertEqual(first, "System: You are a documentation writer.\n\nTask one")
        self.assertNotIn("SSH", first)
        runner.run_session_prompt("Results", session_id="abc", provider="claude", system="You are a documentation writer.")
        self.assertEqual(run.call_args.kwargs["input"], "Results")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--resume") + 1], "abc")

    @patch("llm_runner.subprocess.run")
    def test_claude_uses_stream_json_with_tools_off_and_prompt_on_stdin(self, run: MagicMock) -> None:
        run.return_value = _ok(_claude_stream("hi"))
        _runner("claude").run_session_prompt("Analyze")

        command = run.call_args.args[0]
        self.assertEqual(command[:7], ["claude", "--print", "--output-format", "stream-json", "--verbose", "--tools", ""])
        self.assertIn("--effort", command)
        self.assertNotIn("Analyze", " ".join(command))
        self.assertIn("Analyze", run.call_args.kwargs["input"])

    @patch("llm_runner.subprocess.run")
    def test_claude_turn_text_joins_all_text_blocks(self, run: MagicMock) -> None:
        # The model wrote a request, thought, then wrote more: `result` alone would only hold the last block.
        run.return_value = _ok(_claude_stream(
            "Let me look.\n```repo\n{\"requests\": []}\n```", ("thinking", "hmm"), "Now I have read enough."))
        result = _runner("claude").run_session_prompt("Analyze")

        self.assertEqual(result.text, "Let me look.\n```repo\n{\"requests\": []}\n```\n\nNow I have read enough.")
        self.assertNotIn("hmm", result.text)

    @patch("llm_runner.subprocess.run")
    def test_claude_ignores_sub_agent_messages_and_falls_back_to_result(self, run: MagicMock) -> None:
        lines = [
            {"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [{"type": "text", "text": "inner"}]}},
            {"type": "result", "is_error": False, "session_id": "abc", "result": "only the result"},
        ]
        run.return_value = _ok("\n".join(json.dumps(e) for e in lines))
        self.assertEqual(_runner("claude").run_session_prompt("Analyze").text, "only the result")

    @patch("llm_runner.subprocess.run")
    def test_claude_empty_or_unfinished_streams_fail_over(self, run: MagicMock) -> None:
        run.side_effect = [
            _ok(_claude_stream(result="")),  # no text at all
            _ok(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "cut off"}]}})),  # no result event
        ]
        with self.assertLogs("llm_runner", level="WARNING"):
            first = _runner("claude").run_session_prompt("Analyze")
        self.assertEqual(first.provider, BUILTIN_PROVIDER)
        with self.assertLogs("llm_runner", level="WARNING"):
            second = _runner("claude").run_session_prompt("Analyze")
        self.assertIn("no result event", second.failures[0])

    @patch("llm_runner.subprocess.run")
    def test_claude_error_payload_fails_over(self, run: MagicMock) -> None:
        run.side_effect = [
            _ok(_claude_stream(is_error=True, result="Credit balance too low")),
            _ok(_agy_stream("agy says hi", "c1")),
        ]
        result = _runner("claude", "agy").run_session_prompt("Analyze")

        self.assertEqual((result.provider, result.text, result.session_id), ("agy-cli", "agy says hi", "c1"))

    def _mcp_spec(self) -> dict:
        return {"workspace": "/ws", "sha": "a" * 40, "log": "/tmp/x/calls.jsonl",
                "limits": {"steps": 5, "step_bytes": None, "turn_bytes": 4096}}

    @patch("llm_runner.subprocess.run")
    def test_claude_with_repo_mcp_builds_config_and_flags(self, run: MagicMock) -> None:
        seen = {}

        def fake(command, **kwargs):
            config = Path(command[command.index("--mcp-config") + 1])
            seen["config"] = config
            seen["data"] = json.loads(config.read_text())
            seen["mode"] = config.stat().st_mode & 0o777
            seen["dir_mode"] = config.parent.stat().st_mode & 0o777
            return _ok(_claude_stream("answer"))

        run.side_effect = fake
        result = _runner("claude").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertEqual(result.text, "answer")
        command = run.call_args.args[0]
        self.assertEqual(command[:7], ["claude", "--print", "--output-format", "stream-json", "--verbose", "--tools", ""])
        self.assertIn("--strict-mcp-config", command)
        allowed = command[command.index("--allowedTools") + 1]
        self.assertEqual(allowed.split(","), ["mcp__repo__repo_list", "mcp__repo__repo_grep",
                                              "mcp__repo__repo_read", "mcp__repo__repo_outline",
                                              "mcp__repo__repo_defs", "mcp__repo__repo_refs", "mcp__repo__repo_map",
                                              "mcp__repo__repo_callers", "mcp__repo__repo_callees", "mcp__repo__repo_wiki"])
        self.assertNotIn("bypassPermissions", " ".join(command))
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("Analyze", " ".join(command))
        server = seen["data"]["mcpServers"]["repo"]
        self.assertEqual(server["type"], "stdio")
        self.assertTrue(server["args"][0].endswith("repo_mcp.py"))
        pairs = dict(zip(server["args"][1::2], server["args"][2::2]))
        self.assertEqual((pairs["--workspace"], pairs["--sha"], pairs["--log"]), ("/ws", "a" * 40, "/tmp/x/calls.jsonl"))
        self.assertEqual((pairs["--max-steps"], pairs["--max-turn-bytes"]), ("5", "4096"))
        self.assertNotIn("--max-step-bytes", pairs)
        self.assertEqual((seen["mode"], seen["dir_mode"]), (0o600, 0o700))
        self.assertFalse(seen["config"].exists())  # removed with its private dir

    @patch("llm_runner.subprocess.run")
    def test_claude_without_repo_mcp_has_no_mcp_flags(self, run: MagicMock) -> None:
        run.return_value = _ok(_claude_stream("hi"))
        _runner("claude").run_session_prompt("Analyze")
        command = run.call_args.args[0]
        for flag in ("--mcp-config", "--strict-mcp-config", "--allowedTools"):
            self.assertNotIn(flag, command)

    @patch("llm_runner.subprocess.run")
    def test_repo_mcp_is_ignored_by_providers_without_support(self, run: MagicMock) -> None:
        run.return_value = _ok(_agy_stream("agy says hi", "c1"))
        _runner("agy").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertNotIn("repo_mcp", " ".join(run.call_args.args[0]))

    @staticmethod
    def _codex_events(*items, thread: str = "t-9") -> str:
        events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"}]
        for item in items:
            events.append({"type": "item.completed", "item": item})
        return "\n".join(json.dumps(e) for e in events)

    @staticmethod
    def _codex_msg(text: str) -> dict:
        return {"id": "m", "type": "agent_message", "text": text}

    @staticmethod
    def _codex_tool_call(server: str = "repo", tool: str = "repo_read") -> dict:
        return {"id": "c", "type": "mcp_tool_call", "server": server, "tool": tool, "arguments": {}, "status": "completed"}

    def _codex_fake(self, answer: str, *items, seen: dict = None):
        def fake(command, **kwargs):
            if seen is not None:
                seen.setdefault("commands", []).append(list(command))
                seen["env"] = kwargs.get("env")
            Path(command[command.index("-o") + 1]).write_text(answer, encoding="utf-8")
            return _ok(self._codex_events(*items))
        return fake

    @patch("llm_runner.subprocess.run")
    def test_codex_with_repo_mcp_overrides_config_and_removes_builtin_tools(self, run: MagicMock) -> None:
        seen: dict = {}
        run.side_effect = self._codex_fake("done", self._codex_msg("done"), seen=seen)
        result = _runner("codex").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())

        self.assertEqual((result.provider, result.text), ("codex-cli", "done"))
        command = seen["commands"][0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--ignore-user-config", command)  # the user's own MCP servers, plugins and hooks stay out
        self.assertIn("--strict-config", command)       # an unknown flag or feature must fail, never be ignored
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        disabled = [command[i + 1] for i, a in enumerate(command) if a == "--disable"]
        for feature in ("shell_tool", "unified_exec", "view_image", "apps", "plugins", "multi_agent"):
            self.assertIn(feature, disabled)
        overrides = [command[i + 1] for i, a in enumerate(command) if a == "-c"]
        self.assertIn('web_search="disabled"', overrides)
        self.assertIn('mcp_servers.repo.default_tools_approval_mode="approve"', overrides)
        self.assertIn("mcp_servers.repo.args", " ".join(o.split("=")[0] for o in overrides))
        args = next(o for o in overrides if o.startswith("mcp_servers.repo.args="))
        server_args = json.loads(args.split("=", 1)[1])
        self.assertTrue(server_args[0].endswith("repo_mcp.py"))
        self.assertEqual(server_args[server_args.index("--max-steps") + 1], "5")
        self.assertEqual(server_args[server_args.index("--log") + 1], "/tmp/x/calls.jsonl")
        self.assertTrue(next(o for o in overrides if o.startswith("mcp_servers.repo.command=")).count('"') == 2)
        self.assertIn("mcp_servers.repo.env", " ".join(o.split("=")[0] for o in overrides))

    @patch("llm_runner.subprocess.run")
    def test_codex_repo_mcp_never_uses_bypass_flags(self, run: MagicMock) -> None:
        seen: dict = {}
        run.side_effect = self._codex_fake("done", self._codex_msg("done"), seen=seen)
        _runner("codex").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        joined = " ".join(seen["commands"][0])
        for banned in ("dangerously", "full-auto", "workspace-write", "danger-full-access"):
            self.assertNotIn(banned, joined)

    @patch("llm_runner.subprocess.run")
    def test_codex_without_repo_mcp_keeps_the_plain_command(self, run: MagicMock) -> None:
        seen: dict = {}
        run.side_effect = self._codex_fake("plain", self._codex_msg("plain"), seen=seen)
        _runner("codex").run_session_prompt("Analyze")
        command = seen["commands"][0]
        for flag in ("--ignore-user-config", "--strict-config", "--disable", "-c"):
            self.assertNotIn(flag, command)

    @patch("llm_runner.subprocess.run")
    def test_codex_resume_with_repo_mcp_passes_the_same_lockdown(self, run: MagicMock) -> None:
        seen: dict = {}
        run.side_effect = self._codex_fake("again", self._codex_msg("again"), seen=seen)
        _runner("codex").run_session_prompt("Again", session_id="t-9", provider="codex", repo_mcp=self._mcp_spec())
        command = seen["commands"][0]
        self.assertEqual(command[:3], ["codex", "exec", "resume"])
        self.assertEqual(command[-2:], ["t-9", "-"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("shell_tool", command)
        self.assertIn('sandbox_mode="read-only"', command)

    @patch("llm_runner.subprocess.run")
    def test_older_codex_without_the_flags_retries_without_mcp(self, run: MagicMock) -> None:
        seen: dict = {}
        good = self._codex_fake("fallback", self._codex_msg("fallback"), seen=seen)
        calls = []

        def fake(command, **kwargs):
            calls.append(list(command))
            if len(calls) == 1:
                return _fail("Error: Unknown feature flag: multi_agent_v2")
            return good(command, **kwargs)

        run.side_effect = fake
        with self.assertLogs("llm_runner", level="WARNING"):
            result = _runner("codex").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertEqual(result.text, "fallback")
        self.assertIn("--ignore-user-config", calls[0])
        self.assertNotIn("--ignore-user-config", calls[1])
        self.assertNotIn("--disable", calls[1])

    @patch("llm_runner.subprocess.run")
    def test_codex_real_failure_with_mcp_is_not_retried_as_old_cli(self, run: MagicMock) -> None:
        run.return_value = _fail("stream error: connection reset")
        result = _runner("codex").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result.provider, BUILTIN_PROVIDER)

    @patch("llm_runner.subprocess.run")
    def test_codex_mcp_turn_text_is_what_follows_the_last_repo_call(self, run: MagicMock) -> None:
        run.side_effect = self._codex_fake(
            "final answer",
            self._codex_msg("I will look at the file."), self._codex_tool_call(),
            self._codex_msg("now the next one"), self._codex_tool_call(tool="repo_grep"),
            self._codex_msg("final answer"))
        result = _runner("codex").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertEqual(result.text, "final answer")

    @patch("llm_runner.subprocess.run")
    def test_codex_without_mcp_still_joins_every_message(self, run: MagicMock) -> None:
        run.side_effect = self._codex_fake(
            "second", self._codex_msg("first"), self._codex_tool_call(), self._codex_msg("second"))
        result = _runner("codex").run_session_prompt("Analyze")
        self.assertEqual(result.text, "first\n\nsecond")

    @patch("llm_runner.subprocess.run")
    def test_codex_mcp_turn_with_a_builtin_command_is_rejected(self, run: MagicMock) -> None:
        run.side_effect = self._codex_fake(
            "leak", {"id": "x", "type": "command_execution", "command": "cat secret", "status": "completed"},
            self._codex_msg("leak"))
        result = _runner("codex").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertIn("built-in tool", result.failures[0])

    @patch("llm_runner.subprocess.run")
    def test_older_claude_without_the_flags_retries_without_mcp(self, run: MagicMock) -> None:
        run.side_effect = [_fail("error: unknown option '--strict-mcp-config'"), _ok(_claude_stream("fallback"))]
        with self.assertLogs("llm_runner", level="WARNING"):
            result = _runner("claude").run_session_prompt("Analyze", repo_mcp=self._mcp_spec())
        self.assertEqual(result.text, "fallback")
        self.assertIn("--mcp-config", run.call_args_list[0].args[0])
        self.assertNotIn("--mcp-config", run.call_args_list[1].args[0])

    def test_mcp_turn_text_is_what_follows_the_last_tool_call(self) -> None:
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me look."},
                                                          {"type": "tool_use", "name": "mcp__repo__repo_read", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "code"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Now more."},
                                                          {"type": "tool_use", "name": "mcp__repo__repo_list", "input": {}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL answer"}]}},
            {"type": "result", "is_error": False, "session_id": "abc", "result": "FINAL answer"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        self.assertEqual(LLMRunner._parse_claude_stream(raw, after_tools=True)[0], "FINAL answer")
        self.assertEqual(LLMRunner._parse_claude_stream(raw)[0], "Let me look.\n\nNow more.\n\nFINAL answer")

    @patch("llm_runner.subprocess.run")
    def test_codex_new_session_reads_thread_id(self, run: MagicMock) -> None:
        def fake_run(args, **kwargs):
            Path(args[args.index("-o") + 1]).write_text("codex answer", encoding="utf-8")
            events = [{"type": "thread.started", "thread_id": "t-123"}, {"type": "turn.started"}]
            return _ok("\n".join(json.dumps(e) for e in events))

        run.side_effect = fake_run
        result = _runner("codex").run_session_prompt("Analyze")

        self.assertEqual((result.text, result.session_id), ("codex answer", "t-123"))
        self.assertNotIn("resume", run.call_args.args[0])
        self.assertIn("read-only", run.call_args.args[0])

    @patch("llm_runner.subprocess.run")
    def test_codex_joins_every_agent_message_of_the_turn(self, run: MagicMock) -> None:
        def fake_run(args, **kwargs):
            Path(args[args.index("-o") + 1]).write_text("final words", encoding="utf-8")
            events = [
                {"type": "thread.started", "thread_id": "t-1"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "first ```repo block```"}},
                {"type": "item.completed", "item": {"type": "reasoning", "text": "secret thoughts"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "final words"}},
            ]
            return _ok("\n".join(json.dumps(e) for e in events))

        run.side_effect = fake_run
        result = _runner("codex").run_session_prompt("Analyze")

        self.assertEqual(result.text, "first ```repo block```\n\nfinal words")  # the -o file repeats the last message once
        self.assertEqual(result.session_id, "t-1")

    @patch("llm_runner.subprocess.run")
    def test_agy_joins_every_agent_response_step(self, run: MagicMock) -> None:
        events = [
            {"event": "init", "conversation_id": "c1"},
            {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "Reading "}},
            {"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "now.\n"}},
            {"event": "step_update", "step_update": {"step_index": 2, "step_type": "tool_call", "text_delta": "ignored"}},
            {"event": "step_update", "step_update": {"step_index": 3, "step_type": "agent_response", "text_delta": "All done."}},
            {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "All done."}},
        ]
        run.return_value = _ok("\n".join(json.dumps(e) for e in events))
        result = _runner("agy").run_session_prompt("Analyze")

        self.assertEqual((result.text, result.session_id), ("Reading now.\n\nAll done.", "c1"))

    @patch("llm_runner.subprocess.run")
    def test_codex_resume_passes_id_before_stdin_marker(self, run: MagicMock) -> None:
        def fake_run(args, **kwargs):
            Path(args[args.index("-o") + 1]).write_text("again", encoding="utf-8")
            return _ok('{"type":"thread.started","thread_id":"t-123"}')

        run.side_effect = fake_run
        result = _runner("codex").run_session_prompt("Again", session_id="t-123", provider="codex")

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "exec", "resume"])
        self.assertEqual(command[-2:], ["t-123", "-"])
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertNotIn("--color", command)
        self.assertNotIn("--sandbox", command)
        self.assertTrue(result.resumed)

    @patch("llm_runner.subprocess.run")
    def test_agy_resume_uses_conversation_flag(self, run: MagicMock) -> None:
        run.return_value = _ok(_agy_stream("ok", "c1"))
        _runner("agy").run_session_prompt("Again", session_id="c1", provider="agy")

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--conversation") + 1], "c1")
        self.assertNotIn("Again", " ".join(command))

    @patch("llm_runner.subprocess.run")
    def test_stored_provider_is_tried_first(self, run: MagicMock) -> None:
        run.return_value = _ok(_agy_stream("ok", "c1"))
        result = _runner("claude", "agy").run_session_prompt("Again", session_id="c1", provider="agy")

        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "agy")
        self.assertEqual(result.provider, "agy-cli")

    @patch("llm_runner.subprocess.run")
    def test_failed_resume_starts_new_session_on_next_provider(self, run: MagicMock) -> None:
        run.side_effect = [
            _fail("No conversation found"),
            _ok(_claude_stream("fresh", session_id="new")),
        ]
        result = _runner("claude", "agy").run_session_prompt("Again", session_id="gone", provider="agy")

        self.assertEqual((result.provider, result.session_id, result.resumed), ("claude-cli", "new", False))
        self.assertIn("--session-id", run.call_args.args[0])

    @patch("llm_runner.subprocess.run")
    def test_stateless_provider_returns_no_session(self, run: MagicMock) -> None:
        run.return_value = _ok("opencode answer")
        result = _runner("opencode").run_session_prompt("Analyze")

        self.assertEqual((result.provider, result.text, result.session_id), ("opencode-cli", "opencode answer", None))

    def test_all_fail_returns_builtin(self) -> None:
        runner = _runner()
        with self.assertLogs("llm_runner", level="WARNING"):
            result = runner.run_session_prompt("Analyze")

        self.assertEqual((result.provider, result.session_id), (BUILTIN_PROVIDER, None))

    def test_interactive_resume_commands(self) -> None:
        self.assertEqual(interactive_resume_command("claude", "s"), ["claude", "--resume", "s"])
        self.assertEqual(interactive_resume_command("codex", "s"), ["codex", "resume", "s"])
        self.assertEqual(interactive_resume_command("agy", "s"), ["agy", "--conversation", "s"])
        with self.assertRaises(ValueError):
            interactive_resume_command("opencode", "s")


class LLMEnvFileTests(unittest.TestCase):
    def test_parses_shell_style_env_file(self) -> None:
        from llm_runner import load_llm_env

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm.env"
            path.write_text(
                "# comment\n\nexport CLAUDE_CODE_OAUTH_TOKEN='tok-1'\nOPENAI_API_KEY=\"sk-2\"\nBAD LINE\n1X=no\nPLAIN=v=3\n",
                encoding="utf-8",
            )
            values = load_llm_env(path)

        self.assertEqual(values, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-1", "OPENAI_API_KEY": "sk-2", "PLAIN": "v=3"})

    def test_missing_file_is_empty(self) -> None:
        from llm_runner import load_llm_env

        self.assertEqual(load_llm_env(Path("/nonexistent/llm.env")), {})

    @patch("llm_runner.subprocess.run")
    def test_env_file_reaches_every_provider_without_argv(self, run: MagicMock) -> None:
        run.side_effect = [_fail("x"), _ok("opencode answer")]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm.env"
            path.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok-9\n", encoding="utf-8")
            with patch.dict(os.environ, {"GUARDIAN_LLM_ENV": str(path)}):
                runner = _runner("claude", "opencode")
                runner.run_prompt("Analyze")

        claude_call, opencode_call = run.call_args_list
        # llm.env reaches the CLI it belongs to, never another provider (env_policy), and never argv.
        self.assertEqual(claude_call.kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "tok-9")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", opencode_call.kwargs["env"])
        for call in run.call_args_list:
            self.assertNotIn("tok-9", " ".join(call.args[0]))
        self.assertIn(str(Path.home() / ".local" / "bin"), run.call_args_list[1].kwargs["env"]["PATH"].split(os.pathsep))


if __name__ == "__main__":
    unittest.main()


class BuiltinEngineTests(unittest.TestCase):
    """The built-in engine may only restate measured data; it must not invent numbers."""

    def _reason(self, context):
        return _runner()._expert_reasoning("summarize the fleet", context)

    def test_empty_fleet_says_there_is_no_data(self) -> None:
        text = self._reason({"fleet_summary": []})
        self.assertIn("진단 결과가 없습니다", text)
        self.assertNotIn("21", text)

    def test_fleet_counts_and_offline_hosts_come_from_the_probe(self) -> None:
        fleet = [
            {"id": "a", "name": "a", "group": "G", "status": "ONLINE"},
            {"id": "b", "name": "edge-mini", "group": "G", "status": "OFFLINE"},
        ]
        text = self._reason({"fleet_summary": fleet})
        self.assertIn("총 2대", text)
        self.assertIn("edge-mini", text)
        self.assertNotIn("매우 안정적", text)

    def test_missing_security_score_is_not_reported_as_50(self) -> None:
        text = _runner()._expert_reasoning("ssh security audit", {"security": {}})
        self.assertIn("점수 없음", text)
        self.assertNotIn("50/100", text)


class BuiltinNoticeTests(unittest.TestCase):
    """Debate roles and open questions get an explicit "no LLM" notice, never canned analysis."""

    def test_debate_roles_do_not_invent_verdicts_or_findings(self) -> None:
        r = _runner()
        for prompt in ("Decide if consensus is reached (APPROVED / REJECTED)", "Critique this proposal", "Write an architectural proposal"):
            text = r._expert_reasoning(prompt, {})
            self.assertNotIn("APPROVED", text)
            self.assertNotIn("⚠️ **", text)
            self.assertIn("만들 수 없습니다", text)

    def test_unknown_request_is_not_called_normal(self) -> None:
        text = _runner()._expert_reasoning("why is the build slow", {})
        self.assertNotIn("정상", text)

    def test_persona_prompt_is_not_echoed_as_an_answer(self) -> None:
        persona = {"name": "Red Teamer", "system_prompt": "SECRET SYSTEM PROMPT"}
        text = _runner()._expert_reasoning("review this design", {"persona": persona})
        self.assertNotIn("SECRET SYSTEM PROMPT", text)
        self.assertIn("Red Teamer", text)

    def test_metrics_summary_is_returned(self) -> None:
        m = {"hostname": "hub", "cpu_usage_pct": 12.5, "memory_pct": 40, "disk_pct": 50, "load_avg": [0.1, 0.2, 0.3], "issues": ["disk unavailable"]}
        text = _runner()._expert_reasoning("diagnose metrics", {"metrics": m})
        self.assertIn("호스트: hub", text)
        self.assertIn("12.5%", text)
        self.assertIn("disk unavailable", text)

    @patch("llm_runner.subprocess.run")
    def test_fallback_output_starts_with_the_banner(self, run: MagicMock) -> None:
        run.return_value = _fail("boom")
        provider, text = _runner("claude").run_prompt("Critique this proposal")
        self.assertEqual(provider, BUILTIN_PROVIDER)
        self.assertTrue(text.startswith("⚠️ [LLM 응답 없음]"), text[:80])

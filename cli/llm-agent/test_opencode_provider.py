"""The opencode provider: K-EXAONE through the opencode 2.x CLI (docs/design/opencode-exaone.md).

No real opencode binary and no network: a small fake executable answers --version with a canned version
and answers `run` with canned JSON event lines. Its behaviour comes from OPENCODE_FAKE_* variables, which
pass env_policy's OPENCODE_ prefix; it records argv, env and stdin of every run in a JSON file.
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List
from unittest.mock import patch

import testenv

import llm_runner
from llm_runner import BUILTIN_PROVIDER, DEFAULT_PROVIDER_ORDER, KNOWN_PROVIDERS, LLMRunner

KEY = "opencode-exaone-key-123"

# The fake opencode CLI. %(default_version)r is embedded at write time so a test can simulate a changed
# binary (the readiness cache is keyed by path and modification time); OPENCODE_FAKE_VERSION overrides it.
FAKE_OPENCODE = '''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time


def record(stdin_text):
    path = os.environ.get("OPENCODE_FAKE_DUMP")
    if not path:
        return
    try:
        with open(path) as handle:
            runs = json.load(handle)
    except (OSError, ValueError):
        runs = []
    runs.append({"argv": sys.argv, "env": dict(os.environ), "stdin": stdin_text})
    with open(path, "w") as handle:
        json.dump(runs, handle)


def step_start(number, message):
    return {"type": "step_start", "timestamp": number, "sessionID": "ses_a",
            "part": {"id": f"p{number}", "sessionID": "ses_a", "messageID": message, "type": "step-start"}}


def text_event(number, message, body):
    return {"type": "text", "timestamp": number, "sessionID": "ses_a",
            "part": {"id": f"p{number}", "sessionID": "ses_a", "messageID": message, "type": "text",
                     "text": body, "time": {"start": 1, "end": 2}}}


def tool_use(number, status="error", tool="skill"):
    error = None if status == "completed" else f"Permission denied: {tool}"
    return {"type": "tool_use", "timestamp": number, "sessionID": "ses_a",
            "part": {"partID": "p4", "sessionID": "ses_a", "messageID": "m1", "type": "tool", "id": "call_1",
                     "tool": tool,
                     "state": {"status": status, "input": {"path": "cli/llm-agent/guardian.py"},
                               "error": error,
                               "metadata": {"providerCall": {"executed": status == "completed"}},
                               "time": {"start": 1, "end": 2}}}}


def step_finish(number, message, reason):
    return {"type": "step_finish", "timestamp": number, "sessionID": "ses_a",
            "part": {"id": f"p{number}", "sessionID": "ses_a", "messageID": message, "type": "step-finish",
                     "reason": reason, "cost": 0,
                     "tokens": {"input": 664, "output": 4, "reasoning": 16, "cache": {"read": 0, "write": 0}}}}


def provider_error(etype, message, status, extra=None):
    detail = {"type": etype, "message": message, "status": status}
    if extra:
        detail["response"] = extra
    return detail


if "--version" in sys.argv:
    record("")
    sys.stdout.write(os.environ.get("OPENCODE_FAKE_VERSION", %(default_version)r) + "\\n")
    sys.exit(0)

record(sys.stdin.read())
mode = os.environ.get("OPENCODE_FAKE_MODE", "success")
lines = []
status = 0
stderr_text = ""
if mode == "success":
    lines = [step_start(1, "m1"), text_event(2, "m1", "\\n\\nPONG"), step_finish(3, "m1", "stop")]
elif mode == "two_steps":
    # A refused skill tool call (opencode denies it, asks again): the answer is the second step only.
    lines = [step_start(1, "m1"), tool_use(2), text_event(3, "m1", "FIRST STEP IS NOT THE ANSWER"),
             step_finish(4, "m1", "tool-calls"), step_start(5, "m2"), text_event(6, "m2", "PONG"),
             step_finish(7, "m2", "stop")]
elif mode == "served":
    # A repo tool call the MCP server answered, then the answer step: served, and the last step wins.
    lines = [step_start(1, "m1"), tool_use(2, status="completed", tool="repo_repo_read"),
             text_event(3, "m1", "READING IS NOT THE ANSWER"), step_finish(4, "m1", "tool-calls"),
             step_start(5, "m2"), text_event(6, "m2", "PONG"), step_finish(7, "m2", "stop")]
elif mode == "error500":
    error = provider_error("provider.internal", "fake 500", 500,
                           {"body": json.dumps({"error": {"message": "fake 500", "type": "server_error",
                                                          "code": "overloaded"}})})
    lines = [{"type": "retry", "timestamp": 1, "sessionID": "ses_a", "messageID": "m1", "attempt": 11,
              "nextRetryAt": 2, "error": error},
             {"type": "error", "timestamp": 3, "sessionID": "ses_a", "error": error}]
    status = 1
elif mode == "error_headers":
    error = provider_error("provider.rate", "fake 429", 429,
                           {"headers": {"X-Request-Id": "req-123"},
                            "body": json.dumps({"error": {"message": "fake 429", "code": "quota"}})})
    lines = [{"type": "error", "timestamp": 1, "sessionID": "ses_a", "error": error}]
    status = 1
elif mode == "rate_limited":
    # EXAONE overload: opencode's internal retries (the retry events) end in a 429 error event.
    error = provider_error("provider.rate-limit", "service is busy, too many concurrent requests(1024)", 429)
    lines = [{"type": "retry", "timestamp": 1, "sessionID": "ses_a", "messageID": "m1", "attempt": 11,
              "nextRetryAt": 2, "error": error},
             {"type": "error", "timestamp": 3, "sessionID": "ses_a", "error": error}]
    status = 1
elif mode == "rate_limited_once":
    # Rate-limited on the first `run` invocation only (the status arrives as a string), then the success shape.
    try:
        with open(os.environ["OPENCODE_FAKE_DUMP"]) as handle:
            recorded = json.load(handle)
    except (OSError, ValueError, KeyError):
        recorded = []
    runs_so_far = sum(1 for r in recorded if len(r.get("argv", [])) > 1 and r["argv"][1] == "run")
    if runs_so_far <= 1:
        error = provider_error("provider.rate-limit", "service is busy, too many concurrent requests(1024)", "429")
        lines = [{"type": "error", "timestamp": 1, "sessionID": "ses_a", "error": error}]
        status = 1
    else:
        lines = [step_start(1, "m1"), text_event(2, "m1", "PONG"), step_finish(3, "m1", "stop")]
elif mode == "length":
    # opencode continues a cut answer twice itself, then gives up with an output-limit error event.
    for number in range(3):
        lines += [step_start(number, "m1"), text_event(number, "m1", "PONG"), step_finish(number, "m1", "length")]
    lines.append({"type": "error", "timestamp": 9, "sessionID": "ses_a",
                  "error": provider_error("output-limit",
                                          "Response still truncated after two output token limit continuations",
                                          None)})
    status = 1
elif mode == "length_exit0":
    lines = [step_start(1, "m1"), text_event(2, "m1", "PONG"), step_finish(3, "m1", "length")]
elif mode == "empty":
    lines = [step_start(1, "m1"), step_finish(2, "m1", "stop")]
elif mode == "nonzero":
    lines = [step_start(1, "m1"), text_event(2, "m1", "PONG"), step_finish(3, "m1", "stop")]
    status = 3
    stderr_text = "boom: cannot reach the provider"
elif mode == "stall":
    # A stream that stops sending: the process keeps running (and holds a grandchild) until the caller kills it.
    sys.stdout.write(json.dumps(step_start(1, "m1")) + "\\n")
    sys.stdout.flush()
    child = subprocess.Popen(["sleep", "60"])
    pid_path = os.environ.get("OPENCODE_FAKE_CHILD_PID")
    if pid_path:
        with open(pid_path, "w") as handle:
            handle.write(str(child.pid))
    time.sleep(60)
    sys.exit(0)

for line in lines:
    sys.stdout.write(json.dumps(line) + "\\n")
if stderr_text:
    sys.stderr.write(stderr_text + "\\n")
sys.exit(status)
'''

DISABLE_FLAGS = (
    "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_FREE_MODELS",
)
GUARDIAN_SETTINGS = ("OPENCODE_BIN", "OPENCODE_TIMEOUT", "OPENCODE_EXAONE_EFFORT", "OPENCODE_EXAONE_MODEL")


def setUpModule() -> None:
    # Keep a real ~/.guardian/llm.env on the test host out of these tests, and the data dir isolated
    # (the team-validation test loads personas through the data dir).
    os.environ["GUARDIAN_LLM_ENV"] = "/nonexistent/guardian-llm.env"
    testenv.isolate_data_dir()


def tearDownModule() -> None:
    testenv.restore_data_dir()


def expected_config(model: str = "k-exaone_v2.5", effort: str = "high") -> Dict[str, Any]:
    """The exact config the invoker must hand over (the contract of docs/design/opencode-exaone.md)."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"exaone/{model}",
        "providers": {"exaone": {
            "package": "@opencode/ai/providers/openai-compatible",
            "name": "EXAONE",
            "settings": {"baseURL": "https://api.exaone.ai/v1", "apiKey": "{env:OPENCODE_EXAONE_API_KEY}"},
            "models": {model: {
                "name": "K-EXAONE 2.5",
                "capabilities": {"tools": False, "input": ["text"], "output": ["text"], "temperature": True,
                                 "reasoning": {"supported": True}},
                "compatibility": {"reasoningField": "reasoning_content"},
                "body": {"reasoning_effort": effort, "temperature": 1.0, "top_p": 0.95},
                "limit": {"context": 524288, "output": llm_runner.EXAONE_DEFAULT_MAX_TOKENS},
            }},
        }},
        "permission": {"*": "deny"},
    }


class OpencodeTestCase(unittest.TestCase):
    """Shared setup: a fake opencode binary, an isolated HOME (the cache dir stays out of the real one) and a
    fresh readiness cache per test."""

    def setUp(self) -> None:
        patcher = patch.object(llm_runner, "_OPENCODE_2X_CACHE", {})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A pace slot left over from an earlier test must never make this one wait (or eat its timeout).
        pacer = patch.object(llm_runner, "_EXAONE_NEXT_START", 0.0)
        pacer.start()
        self.addCleanup(pacer.stop)
        self.tmp = TemporaryDirectory(prefix="guardian-test-opencode-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.dump = Path(self.tmp.name) / "runs.json"
        self.bin = self._write_fake("opencode v2.2.7")  # the real v2 binary's output

    def _write_fake(self, version: str) -> Path:
        binary = Path(self.tmp.name) / "bin" / "opencode"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text(FAKE_OPENCODE % {"default_version": version}, encoding="utf-8")
        binary.chmod(0o755)
        return binary

    def _env(self, **extra: str) -> Dict[str, str]:
        env = {"OPENCODE_EXAONE_API_KEY": KEY, "OPENCODE_BIN": str(self.bin), "HOME": str(self.home),
               "OPENCODE_FAKE_DUMP": str(self.dump)}
        env.update(extra)
        return env

    def _runner(self, mode: str = "success", without_key: bool = False, **extra: str) -> LLMRunner:
        env = self._env(OPENCODE_FAKE_MODE=mode, **extra)
        with patch.dict(os.environ, env, clear=False):
            if without_key:
                os.environ.pop("OPENCODE_EXAONE_API_KEY", None)  # a developer shell value must not leak in
            return LLMRunner(providers=["opencode"])

    def _runs(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.dump.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _run_invocations(self) -> List[Dict[str, Any]]:
        """Every recorded `run` invocation: one entry per attempt (a --version probe may share the dump)."""
        return [r for r in self._runs() if len(r["argv"]) > 1 and r["argv"][1] == "run"]

    def _run_record(self) -> Dict[str, Any]:
        """The last recorded `run` invocation (a --version probe may share the dump file)."""
        runs = [r for r in self._runs() if len(r["argv"]) > 1 and r["argv"][1] == "run"]
        self.assertTrue(runs, f"the fake opencode was not run: {self._runs()}")
        return runs[-1]

    def _invoke(self, mode: str = "success", prompt: str = "hello prompt", **extra: str) -> str:
        return self._runner(mode=mode, **extra)._run_opencode(prompt, self.tmp.name)


class ProviderListTests(unittest.TestCase):
    def test_known_providers_and_the_default_order(self) -> None:
        self.assertEqual(KNOWN_PROVIDERS, ["claude", "codex", "agy", "zai", "exaone", "opencode", "ollama", "llm"])
        self.assertEqual(DEFAULT_PROVIDER_ORDER, ["claude", "codex", "agy", "zai", "opencode", "ollama", "llm"])
        self.assertEqual(DEFAULT_PROVIDER_ORDER[:5], ["claude", "codex", "agy", "zai", "opencode"])

    def test_a_team_may_still_pick_exaone_by_name(self) -> None:
        from team_manager import TeamValidationError, parse_lineup, validate_lineup

        team = {"id": "exa", "name": "Exa",
                "expert": {"persona": "domain_architect", "providers": ["exaone"]},
                "critic": [{"persona": "critic_redteam", "providers": ["opencode"]}],
                "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]}, "max_rounds": 2}
        self.assertEqual(validate_lineup(parse_lineup(team)), [])
        broken = {**team, "judge": {"persona": "jev_mcp_decision", "providers": ["gpt9"]}}
        with self.assertRaisesRegex(TeamValidationError, "unknown provider"):
            validate_lineup(parse_lineup(broken))


class ReadinessTests(OpencodeTestCase):
    def test_a_2x_binary_and_the_key_make_it_installed(self) -> None:
        runner = self._runner()
        self.assertEqual(runner.available_providers, ["opencode"])
        self.assertIn("opencode", runner.installed_providers)

    def test_a_1x_binary_is_not_installed(self) -> None:
        # The dev Mac's Homebrew opencode 1.19.5 must never pass.
        runner = self._runner(OPENCODE_FAKE_VERSION="1.19.5")
        self.assertEqual(runner.available_providers, [])
        self.assertNotIn("opencode", runner.installed_providers)

    def test_version_output_shapes(self) -> None:
        self.assertEqual(llm_runner._opencode_major("opencode v2.2.7\n"), 2)
        self.assertEqual(llm_runner._opencode_major("2.2.7"), 2)
        self.assertEqual(llm_runner._opencode_major("1.19.5\n"), 1)
        self.assertIsNone(llm_runner._opencode_major("opencode version unknown"))
        self.assertIsNone(llm_runner._opencode_major(""))

    def test_without_the_key_nothing_is_installed_and_no_binary_runs(self) -> None:
        runner = self._runner(without_key=True)
        self.assertNotIn("opencode", runner.installed_providers)
        self.assertEqual(runner.available_providers, [])
        self.assertEqual(self._runs(), [])  # the version probe never ran

    def test_the_version_probe_gets_throwaway_xdg_dirs(self) -> None:
        self._runner()
        probe = [r for r in self._runs() if "--version" in r["argv"]][0]
        for kind in ("CONFIG", "DATA", "STATE"):
            path = probe["env"][f"XDG_{kind}_HOME"]
            self.assertIn("guardian_opencode_", path)
            self.assertFalse(os.path.exists(path))

    def test_the_version_probe_runs_once_per_binary(self) -> None:
        self._runner()
        self._runner()
        versions = [r for r in self._runs() if "--version" in r["argv"]]
        self.assertEqual(len(versions), 1)

    def test_a_changed_binary_is_probed_again(self) -> None:
        self._runner()
        self.bin = self._write_fake("1.19.5")
        # The cache key holds (path, mtime): force a new mtime so the rewrite is seen deterministically.
        stamped = os.stat(self.bin).st_mtime + 1
        os.utime(self.bin, (stamped, stamped))
        runner = self._runner()
        self.assertNotIn("opencode", runner.installed_providers)
        versions = [r for r in self._runs() if "--version" in r["argv"]]
        self.assertEqual(len(versions), 2)

    def test_a_missing_key_fails_the_call(self) -> None:
        runner = self._runner(without_key=True)
        with self.assertRaisesRegex(RuntimeError, "OPENCODE_EXAONE_API_KEY is not set"):
            runner._run_opencode("hi", self.tmp.name)


class AnswerTests(OpencodeTestCase):
    def test_success_answers_from_the_last_step(self) -> None:
        self.assertEqual(self._invoke(prompt="hello prompt"), "PONG")
        record = self._run_record()
        # The prompt travels on stdin, never in argv (turns reach hundreds of KB).
        self.assertEqual(record["stdin"], "hello prompt")
        self.assertEqual(record["argv"], [str(self.bin), "run", "--standalone", "--format", "json",
                                          "--title", "guardian", "-m", "exaone/k-exaone_v2.5"])

    def test_a_refused_tool_call_is_counted_and_the_last_step_wins(self) -> None:
        with self.assertLogs("llm_runner", level="INFO") as logs:
            answer = self._invoke(mode="two_steps")
        self.assertEqual(answer, "PONG")  # the first step's text is not part of the answer
        self.assertIn("opencode: 0 tool call(s) served, 1 refused", "\n".join(logs.output))

    def test_a_served_repo_tool_call_is_counted_and_the_last_step_wins(self) -> None:
        with self.assertLogs("llm_runner", level="INFO") as logs:
            answer = self._invoke(mode="served")
        self.assertEqual(answer, "PONG")  # the tool-call step's text is not part of the answer
        self.assertIn("opencode: 1 tool call(s) served, 0 refused", "\n".join(logs.output))

    def test_run_prompt_labels_the_provider_opencode_cli(self) -> None:
        provider, text = self._runner().run_prompt("ping")
        self.assertEqual((provider, text), ("opencode-cli", "PONG"))


class TurnStatsTests(OpencodeTestCase):
    """Per-turn stats of a successful opencode turn (docs/design/exaone-provider.md "Deadline margin", option 1):
    the elapsed time and the timeout with its source. No token counts: the CLI reports none in the shape read."""

    def test_a_successful_turn_records_elapsed_timeout_and_its_source(self) -> None:
        result = self._runner(OPENCODE_TIMEOUT="90").run_session_prompt("Analyze", providers=["opencode"])
        self.assertEqual(result.provider, "opencode-cli")
        self.assertEqual(sorted(result.stats), ["elapsed_s", "timeout_s", "timeout_source"])
        self.assertEqual((result.stats["timeout_s"], result.stats["timeout_source"]), (90.0, "OPENCODE_TIMEOUT"))
        self.assertIsInstance(result.stats["elapsed_s"], float)
        self.assertGreaterEqual(result.stats["elapsed_s"], 0.0)

    def test_without_the_variable_the_timeout_source_is_the_default(self) -> None:
        result = self._runner().run_session_prompt("Analyze", providers=["opencode"])
        self.assertEqual((result.stats["timeout_s"], result.stats["timeout_source"]),
                         (llm_runner.OPENCODE_DEFAULT_TIMEOUT, "default"))

    def test_an_invalid_variable_reads_as_the_default(self) -> None:
        result = self._runner(OPENCODE_TIMEOUT="banana").run_session_prompt("Analyze", providers=["opencode"])
        self.assertEqual((result.stats["timeout_s"], result.stats["timeout_source"]),
                         (llm_runner.OPENCODE_DEFAULT_TIMEOUT, "default"))

    def test_a_failing_turn_falls_back_with_empty_stats(self) -> None:
        runner = self._runner(mode="error500")
        with self.assertLogs("llm_runner", level="WARNING"):
            result = runner.run_session_prompt("Analyze", providers=["opencode"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(result.stats, {})


class ParseTests(unittest.TestCase):
    """_opencode_parse counts tool calls by their part state; the last step still wins."""

    @staticmethod
    def _lines(*events: Dict[str, Any]) -> str:
        return "\n".join(json.dumps(e) for e in events)

    @staticmethod
    def _tool_use(status: Any) -> Dict[str, Any]:
        return {"type": "tool_use", "part": {"tool": "repo_repo_read", "state": {"status": status}}}

    def test_completed_counts_as_served_and_everything_else_as_refused(self) -> None:
        stdout = self._lines(self._tool_use("completed"), self._tool_use("error"), self._tool_use("pending"))
        self.assertEqual(llm_runner._opencode_parse(stdout), ("", None, 1, 2))

    def test_a_tool_use_without_a_state_counts_as_refused(self) -> None:
        unknown = {"type": "tool_use", "part": {"tool": "repo_repo_read"}}  # an unknown event shape
        self.assertEqual(llm_runner._opencode_parse(self._lines(unknown)), ("", None, 0, 1))

    def test_the_last_step_still_wins_around_tool_calls(self) -> None:
        stdout = self._lines(
            {"type": "step_start"}, {"type": "text", "part": {"text": "FIRST IS NOT THE ANSWER"}},
            self._tool_use("completed"),
            {"type": "step_start"}, {"type": "text", "part": {"text": "PONG"}},
        )
        self.assertEqual(llm_runner._opencode_parse(stdout), ("PONG", None, 1, 0))

    def test_the_counts_survive_a_failure(self) -> None:
        error = {"type": "error", "error": {"type": "provider.internal", "message": "boom", "status": 500}}
        stdout = self._lines(self._tool_use("completed"), error)
        answer, failure, served, refused = llm_runner._opencode_parse(stdout)
        self.assertEqual((answer, served, refused), ("", 1, 0))
        self.assertIn("provider.internal", failure)


class EnvironmentTests(OpencodeTestCase):
    def test_the_child_env_holds_the_config_the_flags_and_isolated_xdg_dirs(self) -> None:
        self._invoke()
        record = self._run_record()
        env = record["env"]
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), expected_config())
        for flag in DISABLE_FLAGS:
            self.assertEqual(env.get(flag), "1", flag)
        # Guardian-only settings are consumed by the runner, never handed to the CLI.
        for name in GUARDIAN_SETTINGS:
            self.assertNotIn(name, env, name)
        # No other provider's credentials reach the CLI (env_policy).
        self.assertNotIn("AGENT_TOKEN", env)
        self.assertNotIn("EXAONE_API_KEY", env)
        # The key itself rides in the env only: opencode expands {env:OPENCODE_EXAONE_API_KEY}.
        self.assertEqual(env.get("OPENCODE_EXAONE_API_KEY"), KEY)
        self.assertNotIn(KEY, env["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn(KEY, json.dumps(record["argv"]))

    def test_xdg_dirs_live_in_a_scratch_dir_removed_after_the_call(self) -> None:
        self._invoke()
        env = self._run_record()["env"]
        roots = {Path(env[name]).parent for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")}
        self.assertEqual(len(roots), 1)
        root = roots.pop()
        self.assertTrue(root.name.startswith("guardian_opencode_"))
        self.assertNotEqual(root, self.home)
        self.assertFalse(root.exists())  # no session or prompt copy outlives the turn
        # The package cache persists under HOME instead.
        self.assertEqual(env["XDG_CACHE_HOME"], str(self.home / ".cache" / "guardian-opencode"))
        self.assertTrue((self.home / ".cache" / "guardian-opencode").exists())

    def test_effort_and_model_settings_are_honoured(self) -> None:
        self._invoke(OPENCODE_EXAONE_EFFORT="max", OPENCODE_EXAONE_MODEL="k-exaone-test")
        record = self._run_record()
        self.assertEqual(json.loads(record["env"]["OPENCODE_CONFIG_CONTENT"]),
                         expected_config(model="k-exaone-test", effort="max"))
        self.assertEqual(record["argv"][-1], "exaone/k-exaone-test")

    def test_an_unknown_effort_falls_back_to_the_default(self) -> None:
        self._invoke(OPENCODE_EXAONE_EFFORT="banana")
        self.assertEqual(json.loads(self._run_record()["env"]["OPENCODE_CONFIG_CONTENT"]), expected_config())

    def test_an_invalid_timeout_falls_back_to_the_default(self) -> None:
        runner = self._runner(OPENCODE_TIMEOUT="banana")
        self.assertEqual(runner._opencode_settings()[1], llm_runner.OPENCODE_DEFAULT_TIMEOUT)
        runner = self._runner(OPENCODE_TIMEOUT="0")
        self.assertEqual(runner._opencode_settings()[1], llm_runner.OPENCODE_DEFAULT_TIMEOUT)

    def test_the_base_url_comes_from_the_module_constant(self) -> None:
        with patch.object(llm_runner, "OPENCODE_EXAONE_BASE_URL", "https://opencode.invalid/v1"):
            self._invoke()
        config = json.loads(self._run_record()["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["providers"]["exaone"]["settings"]["baseURL"], "https://opencode.invalid/v1")

    def test_exaone_pacing_runs_before_the_turn(self) -> None:
        runner = self._runner()
        with patch("llm_runner._exaone_pace") as pace:
            runner._run_opencode("hi", self.tmp.name)
        pace.assert_called_once()


class RepoMcpTests(OpencodeTestCase):
    """repo_mcp attaches the read-only repo tools: the server entry, the cleared environment and the permission."""

    SHA = "a" * 40

    def setUp(self) -> None:
        super().setUp()
        self.spec = {"workspace": "/ws", "sha": self.SHA, "log": "/tmp/x/calls.jsonl",
                     "limits": {"steps": 5, "step_bytes": None, "turn_bytes": 4096}}
        # An env like the one _opencode_invocation holds when it builds the config: the four base names plus
        # the key (opencode expands it) and the runner-only settings.
        self.env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.home), "LANG": "en_US.UTF-8",
                    "LC_ALL": "en_US.UTF-8", "OPENCODE_EXAONE_API_KEY": KEY, "OPENCODE_BIN": str(self.bin),
                    "OPENCODE_FAKE_DUMP": str(self.dump), "OPENCODE_FAKE_MODE": "success"}

    def _config(self, spec: Dict[str, Any] = None, env: Dict[str, str] = None) -> Dict[str, Any]:
        return llm_runner._opencode_config("k-exaone_v2.5", "high", repo_mcp=spec if spec is not None else self.spec,
                                           env=env if env is not None else self.env)

    def test_the_config_without_repo_mcp_is_unchanged(self) -> None:
        self.assertEqual(llm_runner._opencode_config("k-exaone_v2.5", "high"), expected_config())

    def test_the_server_entry_shape(self) -> None:
        entry = self._config()["mcp"]["repo"]
        self.assertEqual({name: entry[name] for name in ("type", "enabled", "codemode", "timeout")},
                         {"type": "local", "enabled": True, "codemode": False, "timeout": 60000})

    def test_the_command_clears_the_environment_and_starts_the_server(self) -> None:
        command = self._config()["mcp"]["repo"]["command"]
        self.assertEqual(command[:2], ["/usr/bin/env", "-i"])
        server = command[command.index(sys.executable):]
        self.assertEqual(server[0], sys.executable)
        self.assertTrue(server[1].endswith("repo_mcp.py"))
        pairs = dict(zip(server[2::2], server[3::2]))
        self.assertEqual((pairs["--workspace"], pairs["--sha"], pairs["--log"]),
                         ("/ws", self.SHA, "/tmp/x/calls.jsonl"))
        self.assertEqual((pairs["--max-steps"], pairs["--max-turn-bytes"]), ("5", "4096"))
        # The rebuilt environment holds NAME=value items for the four base names, in that order, and nothing
        # between them and the interpreter.
        prefixes = command[2:command.index(sys.executable)]
        self.assertEqual(prefixes, [f"PATH={self.env['PATH']}", f"HOME={self.env['HOME']}",
                                    f"LANG={self.env['LANG']}", f"LC_ALL={self.env['LC_ALL']}"])

    def test_the_four_base_names_enter_only_when_set(self) -> None:
        env = {"PATH": "/bin", "HOME": "/home/u", "OPENCODE_EXAONE_API_KEY": KEY}
        command = self._config(env=env)["mcp"]["repo"]["command"]
        self.assertEqual(command[2:command.index(sys.executable)], ["PATH=/bin", "HOME=/home/u"])

    def test_no_key_name_or_value_is_in_the_mcp_entry(self) -> None:
        # opencode passes its whole environment to the MCP child (the key included), so the entry itself must
        # carry none of it: neither the key's name and value nor any other env name.
        dumped = json.dumps(self._config()["mcp"])
        self.assertNotIn("OPENCODE_EXAONE_API_KEY", dumped)
        self.assertNotIn(KEY, dumped)
        for name in self.env:
            if name not in ("PATH", "HOME", "LANG", "LC_ALL"):
                self.assertNotIn(name, dumped, name)

    def test_tools_are_offered_and_only_the_repo_tools_allowed(self) -> None:
        config = self._config()
        model = config["providers"]["exaone"]["models"]["k-exaone_v2.5"]
        self.assertIs(model["capabilities"]["tools"], True)
        self.assertEqual(config["permission"], {
            "*": "deny",
            **{f"repo_{name}": "allow" for name in (
                "repo_list", "repo_grep", "repo_read", "repo_outline", "repo_defs", "repo_refs",
                "repo_map", "repo_callers", "repo_callees", "repo_wiki")},  # tool_names(), repo_wiki included
        })

    def test_no_index_allows_only_the_snapshot_tools(self) -> None:
        config = self._config(spec={**self.spec, "no_index": True})
        self.assertEqual(config["permission"], {
            "*": "deny",
            **{f"repo_{name}": "allow" for name in (
                "repo_list", "repo_grep", "repo_read", "repo_outline", "repo_map")},
        })
        command = config["mcp"]["repo"]["command"]
        self.assertIn("--no-index", command)

    def test_run_session_prompt_hands_the_mcp_entry_to_the_cli(self) -> None:
        result = self._runner().run_session_prompt("Analyze", providers=["opencode"], repo_mcp=self.spec)
        self.assertEqual((result.provider, result.text), ("opencode-cli", "PONG"))
        self.assertIsNone(result.session_id)  # stateless: no session to resume or store
        config = json.loads(self._run_record()["env"]["OPENCODE_CONFIG_CONTENT"])
        entry = config["mcp"]["repo"]
        self.assertEqual(entry["codemode"], False)
        # The key is in the child env but never in the config it expands from.
        self.assertNotIn(KEY, self._run_record()["env"]["OPENCODE_CONFIG_CONTENT"])

    def test_run_session_prompt_without_repo_mcp_has_no_mcp_entry(self) -> None:
        result = self._runner().run_session_prompt("Analyze", providers=["opencode"])
        self.assertEqual((result.provider, result.text), ("opencode-cli", "PONG"))
        config = json.loads(self._run_record()["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("mcp", config)
        self.assertEqual(config, expected_config())


class FailureTests(OpencodeTestCase):
    def _failure(self, mode: str, **extra: str) -> str:
        runner = self._runner(mode=mode, **extra)
        try:
            runner._run_opencode("hi", self.tmp.name)
        except RuntimeError as exc:
            return str(exc)
        self.fail(f"mode {mode} did not fail")

    def test_an_error_event_names_type_message_and_status_only(self) -> None:
        message = self._failure("error500")
        self.assertIn("error provider.internal: fake 500", message)
        self.assertIn("status 500", message)
        # The response body never reaches the failure text.
        for fragment in ("server_error", "overloaded", '"error"'):
            self.assertNotIn(fragment, message)

    def test_an_error_event_may_carry_a_request_id_from_its_headers(self) -> None:
        # Status 429 is rate-limited and would retry with real pauses (RetryTests covers that); pin them off.
        with patch.object(llm_runner, "OPENCODE_MAX_RETRIES", 0):
            message = self._failure("error_headers")
        self.assertIn("error provider.rate: fake 429 (status 429, request req-123)", message)
        self.assertNotIn("quota", message)

    def test_the_error_event_wins_over_the_exit_code(self) -> None:
        # The 500 fixture exits 1 AND emits an error event: the message is the event's, not "exited with code".
        message = self._failure("error500")
        self.assertTrue(message.startswith("error provider.internal"))
        self.assertNotIn("exited with code", message)

    def test_a_length_finish_with_exit_zero_fails_on_its_own(self) -> None:
        self.assertIn("length", self._failure("length_exit0"))
        self.assertIn("output limit", self._failure("length_exit0"))

    def test_the_output_limit_continuations_fail_with_the_error_event(self) -> None:
        # opencode continues twice itself and then errors: the error event names the truncation.
        message = self._failure("length")
        self.assertIn("error output-limit", message)
        self.assertIn("truncated", message)

    def test_an_empty_answer_fails(self) -> None:
        self.assertEqual(self._failure("empty"), "empty answer")

    def test_a_non_zero_exit_without_an_error_event_names_the_exit_and_stderr(self) -> None:
        message = self._failure("nonzero")
        self.assertIn("exited with code 3", message)
        self.assertIn("boom: cannot reach the provider", message)
        self.assertNotIn("PONG", message)  # stdout (the prompt echo path) never leaks into the failure

    def test_the_chain_prefixes_the_failure_and_moves_on(self) -> None:
        runner = self._runner(mode="error500")
        with self.assertLogs("llm_runner", level="WARNING"):
            result = runner.run_session_prompt("ping")
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertTrue(result.failures[0].startswith("opencode: error provider.internal: fake 500"))


class TimeoutTests(OpencodeTestCase):
    def _wait_for_file(self, path: Path, timeout: float = 5.0) -> str:
        end = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(path.exists(), "the fake never wrote the grandchild pid")
        return path.read_text(encoding="utf-8").strip()

    def test_a_stalled_stream_times_out_and_kills_the_whole_group(self) -> None:
        pid_file = Path(self.tmp.name) / "grandchild.pid"
        runner = self._runner(mode="stall", OPENCODE_TIMEOUT="1", OPENCODE_FAKE_CHILD_PID=str(pid_file))

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, r"timed out after 1s \(OPENCODE_TIMEOUT\)"):
            runner._run_opencode("hi", self.tmp.name)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)  # a few seconds, not the fake's 60 s sleep

        pid = int(self._wait_for_file(pid_file))
        # The whole process group dies: the grandchild (a live provider bridge holding stdout) included.
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return  # dead, as it must be
            time.sleep(0.05)
        self.fail(f"grandchild {pid} survived the group kill")


class RateLimitDetectionTests(unittest.TestCase):
    """_opencode_rate_limited reads the LAST error event; the English message text never matters."""

    @staticmethod
    def _stdout(*errors: Any) -> str:
        return "\n".join(json.dumps({"type": "error", "error": error}) for error in errors)

    def test_type_provider_rate_limit_counts_without_a_status(self) -> None:
        self.assertTrue(llm_runner._opencode_rate_limited(self._stdout({"type": "provider.rate-limit"})))

    def test_status_429_counts_as_int_or_numeric_string(self) -> None:
        for status in (429, 429.0, "429", " 429 "):
            with self.subTest(status=status):
                self.assertTrue(llm_runner._opencode_rate_limited(
                    self._stdout({"type": "provider.server-error", "status": status})))

    def test_no_error_event_or_another_failure_does_not(self) -> None:
        for stdout in ("", "not json\n", self._stdout("boom"), self._stdout({"type": "provider.rate"}),
                       self._stdout({"type": "provider.server-error", "status": 500}),
                       self._stdout({"type": "provider.internal", "status": "429.1"})):
            with self.subTest(stdout=stdout):
                self.assertFalse(llm_runner._opencode_rate_limited(stdout))

    def test_only_the_last_error_event_decides(self) -> None:
        then_server = self._stdout({"type": "provider.rate-limit", "status": 429},
                                   {"type": "provider.internal", "status": 500})
        self.assertFalse(llm_runner._opencode_rate_limited(then_server))
        server_then = self._stdout({"type": "provider.internal", "status": 500},
                                   {"type": "provider.rate-limit", "status": 429})
        self.assertTrue(llm_runner._opencode_rate_limited(server_then))


class RetryTests(OpencodeTestCase):
    """EXAONE 429 retries: one scratch dir and one pacing slot per attempt, all inside the one OPENCODE_TIMEOUT."""

    class FakeTime:
        """A monotonic clock that moves only when sleep is called, so backoff and deadline maths are exact."""

        def __init__(self) -> None:
            self.now = 1000.0
            self.slept: List[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

    def test_a_429_then_success_answers_after_one_retry(self) -> None:
        clock = self.FakeTime()
        with patch("llm_runner.time", clock), patch("llm_runner.random.uniform", return_value=2.5), \
                patch("llm_runner._exaone_pace") as pace, \
                self.assertLogs("llm_runner", level="INFO") as logs:
            answer = self._invoke(mode="rate_limited_once")  # the status arrives as the string "429"
        self.assertEqual(answer, "PONG")
        self.assertEqual(len(self._run_invocations()), 2)
        self.assertEqual(len(clock.slept), 1)  # 30 s backoff plus 2.5 s of jitter
        self.assertTrue(30.0 <= clock.slept[0] <= 35.0, clock.slept)
        self.assertEqual(pace.call_count, 2)  # one pacing slot per attempt
        logged = "\n".join(logs.output)
        self.assertIn("rate limit on attempt 1", logged)
        self.assertIn("retrying in", logged)
        self.assertNotIn("hello prompt", logged)  # no prompt text
        self.assertNotIn(KEY, logged)  # and no key

    def test_a_429_three_times_fails_after_two_retries(self) -> None:
        clock = self.FakeTime()
        with patch("llm_runner.time", clock), patch("llm_runner.random.uniform", return_value=0.0):
            with self.assertRaisesRegex(RuntimeError, "provider.rate-limit.*status 429"):
                self._invoke(mode="rate_limited")
        self.assertEqual(len(self._run_invocations()), 3)  # the first attempt plus two retries
        self.assertEqual(clock.slept, [30.0, 60.0])
        # Each attempt runs in its own throwaway XDG scratch dir.
        roots = {Path(r["env"]["XDG_CONFIG_HOME"]).parent for r in self._run_invocations()}
        self.assertEqual(len(roots), 3)

    def test_a_non_rate_limit_error_fails_after_one_attempt(self) -> None:
        clock = self.FakeTime()
        with patch("llm_runner.time", clock), patch("llm_runner.random.uniform", return_value=0.0):
            with self.assertRaisesRegex(RuntimeError, "provider.internal"):
                self._invoke(mode="error500")
        self.assertEqual(len(self._run_invocations()), 1)
        self.assertEqual(clock.slept, [])

    def test_a_non_zero_exit_without_an_error_event_fails_after_one_attempt(self) -> None:
        clock = self.FakeTime()
        with patch("llm_runner.time", clock), patch("llm_runner.random.uniform", return_value=0.0):
            with self.assertRaisesRegex(RuntimeError, "exited with code 3"):
                self._invoke(mode="nonzero")
        self.assertEqual(len(self._run_invocations()), 1)
        self.assertEqual(clock.slept, [])

    def test_each_attempt_gets_only_the_time_remaining_of_the_deadline(self) -> None:
        clock = self.FakeTime()
        real = LLMRunner._opencode_communicate
        timeouts: List[float] = []

        def spy(_self, proc, prompt, timeout):
            timeouts.append(timeout)
            return real(proc, prompt, timeout)

        with patch("llm_runner.time", clock), patch("llm_runner.random.uniform", return_value=0.0), \
                patch.object(LLMRunner, "_opencode_communicate", spy):
            answer = self._invoke(mode="rate_limited_once", OPENCODE_TIMEOUT="100")
        self.assertEqual(answer, "PONG")
        self.assertEqual(timeouts, [100.0, 70.0])  # the 30 s pause is part of the one 100 s budget

    def test_a_backoff_that_would_pass_the_deadline_fails_without_sleeping(self) -> None:
        clock = self.FakeTime()
        with patch("llm_runner.time", clock), patch("llm_runner.random.uniform", return_value=0.0):
            with self.assertRaises(RuntimeError) as error:  # the original 429 error text, not a timeout
                self._invoke(mode="rate_limited", OPENCODE_TIMEOUT="20")
        message = str(error.exception)
        self.assertIn("provider.rate-limit", message)
        self.assertIn("status 429", message)
        self.assertNotIn("timed out", message)
        self.assertEqual(len(self._run_invocations()), 1)
        self.assertEqual(clock.slept, [])  # the 30 s pause would pass the 20 s deadline: fail instead


if __name__ == "__main__":
    unittest.main()

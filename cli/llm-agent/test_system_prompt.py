"""The standing instructions of a claude session travel as its system prompt (docs/design/debate-agent-retrieval.md,
"시스템 프롬프트와 압축"): runner argv, other providers unchanged, the engine's split, and a simulated compaction."""
import json
import os
import stat
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import llm_runner
from debate_engine import DebateEngine, RolePrompt, RETRIEVAL_PROMPT_VERSION
from llm_runner import (
    CLAUDE_ARGV_PROMPT_LIMIT,
    CLAUDE_DISABLE_MDS_ENV,
    SYSTEM_PROMPT_BOUNDARY,
    SYSTEM_REMINDER_NOTE,
    ClaudeCaps,
    LLMResult,
    LLMRunner,
    SessionInstructions,
    parse_claude_version,
)
from team_manager import Lineup, Member
from test_code_wiki import BuildCase, PAGE_ANSWER, READ_BOTH
from test_retrieval_loop import READ_NOTES, EngineHarness, FakeRunner
from testenv import isolate_data_dir, restore_data_dir
import wiki_build


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    os.environ["GUARDIAN_LLM_ENV"] = "/nonexistent/guardian-llm.env"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def _runner(*providers: str) -> LLMRunner:
    with patch("llm_runner.shutil.which", side_effect=lambda c, **_: f"/bin/{c}" if c in providers else None):
        return LLMRunner(providers=list(providers))


def _stream(text: str = "hi", session_id: str = "abc") -> str:
    events = [{"type": "system", "subtype": "init", "session_id": session_id},
              {"type": "assistant", "session_id": session_id, "message": {"content": [{"type": "text", "text": text}]}},
              {"type": "result", "session_id": session_id, "is_error": False, "result": text}]
    return "\n".join(json.dumps(e) for e in events)


def _ok(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


INSTRUCTIONS = SessionInstructions("ROLE: be a reviewer.\n\nPROTOCOL: use repo blocks.", "STATE: commit abc123.")
NEW_CLAUDE = ClaudeCaps(version=(2, 1, 289), append_file=False, custom_file=False)


class VersionTests(unittest.TestCase):
    def test_versions_parse_from_the_cli_banner(self) -> None:
        self.assertEqual(parse_claude_version("2.1.289 (Claude Code)\n"), (2, 1, 289))
        self.assertEqual(parse_claude_version("claude 2.1.138"), (2, 1, 138))
        self.assertIsNone(parse_claude_version("no version here"))
        self.assertIsNone(parse_claude_version(""))

    def test_the_boundary_needs_2_1_275_and_a_recorded_prompt_2_1_265(self) -> None:
        self.assertFalse(ClaudeCaps(version=(2, 1, 274)).boundary)
        self.assertTrue(ClaudeCaps(version=(2, 1, 275)).boundary)
        self.assertTrue(ClaudeCaps(version=(2, 2, 0)).boundary)
        self.assertFalse(ClaudeCaps(version=None).boundary)
        self.assertFalse(ClaudeCaps(version=(2, 1, 138)).records_prompt)
        self.assertTrue(ClaudeCaps(version=(2, 1, 265)).records_prompt)
        self.assertFalse(ClaudeCaps().records_prompt)

    def test_instructions_join_static_before_dynamic(self) -> None:
        self.assertEqual(INSTRUCTIONS.text(), "ROLE: be a reviewer.\n\nPROTOCOL: use repo blocks.\n\nSTATE: commit abc123.")
        self.assertEqual(SessionInstructions("only").text(), "only")


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        env = patch.dict(os.environ, {"GUARDIAN_LLM_WORKDIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        reset = patch("llm_runner._CLAUDE_CAPS", None)
        reset.start()
        self.addCleanup(reset.stop)

    def test_version_and_file_flags_are_probed_once_per_process(self) -> None:
        help_text = "  --append-system-prompt <prompt>  Append\n  --append-system-prompt-file <path>  Append from file\n"

        def fake(args, **kwargs):
            return _ok("2.1.289 (Claude Code)\n" if args[1] == "--version" else help_text)

        runner = _runner("claude")
        with patch("llm_runner.subprocess.run", side_effect=fake) as run:
            first = runner.claude_caps()
            second = runner.claude_caps()
        self.assertIs(first, second)
        self.assertEqual(run.call_count, 2)  # --version and --help, once
        self.assertEqual((first.version, first.append_file, first.custom_file), ((2, 1, 289), True, False))

    def test_a_bracketed_help_line_and_a_failing_cli_are_handled(self) -> None:
        runner = _runner("claude")
        help_text = "  --system-prompt-file <path>\n  --append-system-prompt[-file] <path>\n"
        with patch("llm_runner.subprocess.run",
                   side_effect=lambda a, **k: _ok("2.1.300" if a[1] == "--version" else help_text)):
            caps = runner.claude_caps()
        self.assertTrue(caps.append_file and caps.custom_file)
        llm_runner._CLAUDE_CAPS = None
        with patch("llm_runner.subprocess.run", side_effect=OSError("no claude")):
            caps = runner.claude_caps()
        self.assertEqual(caps, ClaudeCaps())


class ClaudeArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = patch.dict(os.environ, {"GUARDIAN_LLM_WORKDIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.caps = patch("llm_runner._CLAUDE_CAPS", NEW_CLAUDE)
        self.caps.start()
        self.addCleanup(self.caps.stop)

    def run_claude(self, runner: LLMRunner, **kwargs: Any) -> List[Dict[str, Any]]:
        """Runs one session prompt and returns the claude invocations as {"args", "input", "env", "files"}."""
        seen: List[Dict[str, Any]] = []

        def fake(args, **kw):
            files = {}
            for flag in ("--append-system-prompt-file", "--system-prompt-file"):
                if flag in args:
                    path = Path(args[args.index(flag) + 1])
                    files[flag] = (path, path.read_text(encoding="utf-8"), stat.S_IMODE(path.stat().st_mode))
            seen.append({"args": list(args), "input": kw.get("input"), "env": kw.get("env"), "files": files})
            return _ok(_stream())

        with patch("llm_runner.subprocess.run", side_effect=fake):
            runner.run_session_prompt("Task one", **kwargs)
        return seen

    @staticmethod
    def system_of(args: List[str], flag: str = "--append-system-prompt") -> str:
        return args[args.index(flag) + 1]

    def test_first_and_resumed_turns_carry_the_same_system_text(self) -> None:
        runner = _runner("claude")
        first = self.run_claude(runner, instructions=INSTRUCTIONS)[0]
        resumed = self.run_claude(runner, instructions=INSTRUCTIONS, session_id="abc", provider="claude")[0]
        self.assertIn("--session-id", first["args"])
        self.assertEqual(resumed["args"][resumed["args"].index("--resume") + 1], "abc")
        text = INSTRUCTIONS.text()
        self.assertEqual(self.system_of(first["args"]), text)
        self.assertEqual(self.system_of(resumed["args"]), text)
        # Tools stay off and the flag comes after the variadic --tools value.
        self.assertEqual(first["args"][first["args"].index("--tools") + 1], "")

    def test_the_message_is_the_task_without_the_system_header_or_the_protocol(self) -> None:
        runner = _runner("claude")
        first = self.run_claude(runner, instructions=INSTRUCTIONS, plain_prompt="PLAIN with PROTOCOL: use repo blocks.")[0]
        self.assertEqual(first["input"], "Task one")
        self.assertNotIn("System:", first["input"])
        self.assertNotIn("SSH Security Engineer", first["input"])
        self.assertNotIn("PROTOCOL", first["input"])

    def test_context_data_is_a_first_turn_block_only_when_there_is_context(self) -> None:
        runner = _runner("claude")
        first = self.run_claude(runner, instructions=INSTRUCTIONS, context={"k": "v"})[0]
        self.assertTrue(first["input"].startswith("Context Data:\n{"))
        self.assertTrue(first["input"].endswith("Task one"))
        resumed = self.run_claude(runner, instructions=INSTRUCTIONS, context={"k": "v"}, session_id="abc",
                                  provider="claude")[0]
        self.assertEqual(resumed["input"], "Task one")

    def test_no_instructions_keeps_the_old_argv_and_header(self) -> None:
        run = self.run_claude(_runner("claude"))[0]
        self.assertNotIn("--append-system-prompt", run["args"])
        self.assertTrue(run["input"].startswith("System: You are an expert Linux System & SSH Security Engineer."))

    def test_claude_mds_are_switched_off_for_every_claude_process(self) -> None:
        runner = _runner("claude")
        for kwargs in ({}, {"instructions": INSTRUCTIONS}):
            run = self.run_claude(runner, **kwargs)[0]
            self.assertEqual(run["env"][CLAUDE_DISABLE_MDS_ENV], "1")
        with patch("llm_runner.subprocess.run", return_value=_ok("answer")) as one_shot:
            runner.run_prompt("hello")
        self.assertEqual(one_shot.call_args.kwargs["env"][CLAUDE_DISABLE_MDS_ENV], "1")

    def test_the_switch_does_not_reach_other_providers(self) -> None:
        self.assertNotIn(CLAUDE_DISABLE_MDS_ENV, _runner("codex")._provider_env("codex"))

    def test_large_text_goes_to_a_private_file_that_is_removed_afterwards(self) -> None:
        big = SessionInstructions("x" * (CLAUDE_ARGV_PROMPT_LIMIT + 10), "tail")
        with patch("llm_runner._CLAUDE_CAPS", ClaudeCaps(version=(2, 1, 289), append_file=True)):
            run = self.run_claude(_runner("claude"), instructions=big)[0]
        self.assertNotIn("--append-system-prompt", run["args"])
        path, content, mode = run["files"]["--append-system-prompt-file"]
        self.assertEqual(content, big.text())
        self.assertEqual(mode, 0o600)
        self.assertFalse(path.exists())
        self.assertNotIn("x" * 1000, " ".join(run["args"]))

    def test_large_text_stays_in_argv_when_this_claude_has_no_file_flag(self) -> None:
        big = SessionInstructions("x" * (CLAUDE_ARGV_PROMPT_LIMIT + 10))
        run = self.run_claude(_runner("claude"), instructions=big)[0]
        self.assertEqual(self.system_of(run["args"]), big.text())
        self.assertEqual(run["files"], {})

    def test_small_text_never_uses_the_file_even_when_supported(self) -> None:
        with patch("llm_runner._CLAUDE_CAPS", ClaudeCaps(version=(2, 1, 289), append_file=True)):
            run = self.run_claude(_runner("claude"), instructions=INSTRUCTIONS)[0]
        self.assertEqual(run["files"], {})
        self.assertEqual(self.system_of(run["args"]), INSTRUCTIONS.text())

    def test_append_mode_has_no_boundary_and_no_reminder_note(self) -> None:
        for version in ((2, 1, 289), (2, 1, 138)):
            with patch("llm_runner._CLAUDE_CAPS", ClaudeCaps(version=version)):
                run = self.run_claude(_runner("claude"), instructions=INSTRUCTIONS)[0]
            text = self.system_of(run["args"])
            self.assertNotIn(SYSTEM_PROMPT_BOUNDARY, text)
            self.assertNotIn(SYSTEM_REMINDER_NOTE, text)

    def test_custom_mode_puts_the_boundary_between_static_and_dynamic_from_2_1_275(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_CLAUDE_SYSTEM_PROMPT": "custom"}):
            runner = _runner("claude")
            cases = {(2, 1, 275): True, (2, 1, 274): False, None: False}
            for version, boundary in cases.items():
                with patch("llm_runner._CLAUDE_CAPS", ClaudeCaps(version=version)):
                    run = self.run_claude(runner, instructions=INSTRUCTIONS)[0]
                self.assertNotIn("--append-system-prompt", run["args"])
                text = self.system_of(run["args"], "--system-prompt")
                self.assertTrue(text.startswith(SYSTEM_REMINDER_NOTE), version)
                self.assertEqual(f"\n{SYSTEM_PROMPT_BOUNDARY}\n" in text, boundary, version)
                static, _, dynamic = text.partition(f"\n{SYSTEM_PROMPT_BOUNDARY}\n") if boundary else (text, "", "")
                if boundary:
                    self.assertIn("PROTOCOL", static)
                    self.assertEqual(dynamic, "STATE: commit abc123.")
                    self.assertNotIn("STATE", static)
                else:
                    self.assertIn("STATE: commit abc123.", text)

    def test_custom_mode_uses_the_system_prompt_file_for_large_text(self) -> None:
        big = SessionInstructions("x" * (CLAUDE_ARGV_PROMPT_LIMIT + 10), "tail")
        with patch.dict(os.environ, {"GUARDIAN_CLAUDE_SYSTEM_PROMPT": "custom"}), \
                patch("llm_runner._CLAUDE_CAPS", ClaudeCaps(version=(2, 1, 289), custom_file=True)):
            run = self.run_claude(_runner("claude"), instructions=big)[0]
        self.assertIn(SYSTEM_PROMPT_BOUNDARY, run["files"]["--system-prompt-file"][1])

    def test_an_unknown_mode_falls_back_to_append(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_CLAUDE_SYSTEM_PROMPT": "bogus"}):
            run = self.run_claude(_runner("claude"), instructions=INSTRUCTIONS)[0]
        self.assertIn("--append-system-prompt", run["args"])


class OtherProvidersUnchangedTests(unittest.TestCase):
    """codex, agy and the rest never see instructions: they get plain_prompt wrapped exactly as before."""

    GOLDEN_DEFAULT = ("System: You are an expert Linux System & SSH Security Engineer.\n"
                      "Context Data:\n{}\n\nTask: PLAIN FULL MESSAGE")

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {"GUARDIAN_LLM_WORKDIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_codex_and_agy_get_the_plain_prompt_byte_for_byte(self) -> None:
        for name in ("codex", "agy"):
            runner = _runner(name)
            with patch.object(runner, f"_session_{name}", return_value=("ok", "sid")) as session:
                runner.run_session_prompt("TASK ONLY", instructions=INSTRUCTIONS, plain_prompt="PLAIN FULL MESSAGE")
                self.assertEqual(session.call_args.args[0], self.GOLDEN_DEFAULT, name)
                self.assertEqual(session.call_args.args[1], None)
                runner.run_session_prompt("TASK ONLY", instructions=INSTRUCTIONS, plain_prompt="PLAIN FULL MESSAGE",
                                          session_id="sid", provider=name)
                # A resumed turn without a system line is sent in the old wrapping, as before.
                self.assertEqual(session.call_args.args[0], self.GOLDEN_DEFAULT, name)

    def test_a_system_line_keeps_its_header_on_the_first_turn_and_none_on_a_resumed_turn(self) -> None:
        runner = _runner("codex")
        with patch.object(runner, "_session_codex", return_value=("ok", "sid")) as session:
            runner.run_session_prompt("TASK ONLY", system="You are a writer.", instructions=INSTRUCTIONS,
                                      plain_prompt="PLAIN")
            self.assertEqual(session.call_args.args[0], "System: You are a writer.\n\nPLAIN")
            runner.run_session_prompt("TASK ONLY", system="You are a writer.", instructions=INSTRUCTIONS,
                                      plain_prompt="PLAIN", session_id="sid", provider="codex")
            self.assertEqual(session.call_args.args[0], "PLAIN")

    def test_without_instructions_nothing_changes_for_any_provider(self) -> None:
        runner = _runner("codex")
        with patch.object(runner, "_session_codex", return_value=("ok", "sid")) as session:
            runner.run_session_prompt("PLAIN FULL MESSAGE")
            self.assertEqual(session.call_args.args[0], self.GOLDEN_DEFAULT)

    def test_a_simple_provider_gets_the_plain_prompt(self) -> None:
        runner = _runner("ollama")
        with patch.object(runner, "_run_simple", return_value="x") as simple:
            runner.run_session_prompt("TASK ONLY", instructions=INSTRUCTIONS, plain_prompt="PLAIN FULL MESSAGE")
        self.assertEqual(simple.call_args.args[1][-1], self.GOLDEN_DEFAULT)

    def test_a_failed_claude_falls_back_to_codex_with_the_plain_message(self) -> None:
        runner = _runner("claude", "codex")
        with patch.object(runner, "_session_claude", side_effect=RuntimeError("down")) as claude, \
                patch.object(runner, "_session_codex", return_value=("ok", "sid")) as codex:
            result = runner.run_session_prompt("TASK ONLY", instructions=INSTRUCTIONS, plain_prompt="PLAIN FULL MESSAGE")
        self.assertEqual(claude.call_args.args[0], "TASK ONLY")
        self.assertEqual(claude.call_args.kwargs["instructions"], INSTRUCTIONS)
        self.assertEqual(codex.call_args.args[0], self.GOLDEN_DEFAULT)
        self.assertEqual(result.provider, "codex-cli")


# --- engine ---------------------------------------------------------------------------------------------------------

CLAUDE_LINEUP = Lineup(id="t", name="T", expert=Member("domain_architect", ["claude"], None),
                       critics=[Member("critic_redteam", ["claude"], None)],
                       judge=Member("jev_mcp_decision", ["claude"], None))
CODEX_LINEUP = Lineup(id="t", name="T", expert=Member("domain_architect", ["codex"], None),
                      critics=[Member("critic_redteam", ["codex"], None)],
                      judge=Member("jev_mcp_decision", ["codex"], None))
PROTOCOL_MARK = "RULES: write exactly ONE repo block per reply"


class SplitHarness(EngineHarness):
    def debate(self, runner: FakeRunner, lineup: Lineup, engine: Optional[DebateEngine] = None, **kwargs: Any) -> Dict[str, Any]:
        engine = engine or self.engine()
        self.last_engine = engine
        with patch.object(engine, "_resolve_lineup", return_value=lineup):
            return self.run_debate(engine, runner, **kwargs)

    def persona(self, name: str) -> str:
        return self.engine().persona_mgr.get_persona(name)["system_prompt"]

    def calls(self, runner: FakeRunner, role: str) -> List[Dict[str, Any]]:
        return [c for c in runner.calls if c["role"] == role]


class EngineSplitTests(SplitHarness):
    def scripts(self) -> Dict[str, Any]:
        return self.plain_scripts([READ_NOTES, "FINAL expert answer"], ["Fine.\nVERDICT: REJECTED"])

    def test_claude_roles_get_persona_protocol_and_rules_as_system_text(self) -> None:
        runner = FakeRunner(self.scripts())
        self.debate(runner, CLAUDE_LINEUP)
        rule = self.last_engine._language_rule
        for role, persona in (("expert", "domain_architect"), ("critic", "critic_redteam"), ("judge", "jev_mcp_decision")):
            call = self.calls(runner, role)[0]
            system = call["instructions"].text()
            self.assertIn(self.persona(persona), system, role)
            self.assertIn(PROTOCOL_MARK, system, role)
            self.assertIn("=== Repository state ===", system)
            self.assertIn(self.sha, system)
            self.assertIn("apps/agent/main.go", system)  # the file list (no map for this tiny repo)
            self.assertIn(rule, system, role)
            # The user message holds none of it.
            for standing in (self.persona(persona), PROTOCOL_MARK, "Repository retrieval", self.sha, rule):
                self.assertNotIn(standing, call["prompt"], (role, standing))
            self.assertIn("Task: task", call["prompt"])
        judge = self.calls(runner, "judge")[0]
        self.assertIn("VERDICT", judge["instructions"].text())
        self.assertIn("jev_mcp:apply_patch", judge["instructions"].text())
        self.assertNotIn("VERDICT", judge["prompt"])
        self.assertIn("Proposal:\nFINAL expert answer", judge["prompt"])
        self.assertTrue(judge["prompt"].rstrip().endswith("Decide if consensus is reached (APPROVED / REJECTED)."))

    def test_static_text_comes_before_the_commit_dependent_text(self) -> None:
        runner = FakeRunner(self.scripts())
        self.debate(runner, CLAUDE_LINEUP)
        instructions = self.calls(runner, "expert")[0]["instructions"]
        self.assertIn(PROTOCOL_MARK, instructions.static)
        self.assertNotIn(self.sha, instructions.static)
        self.assertIn(self.sha, instructions.dynamic)
        self.assertNotIn(PROTOCOL_MARK, instructions.dynamic)

    def test_every_call_of_a_session_carries_the_same_system_text(self) -> None:
        runner = FakeRunner(self.scripts())
        self.debate(runner, CLAUDE_LINEUP)
        expert = self.calls(runner, "expert")
        self.assertEqual(len(expert), 2)  # the task, then the retrieval results
        self.assertEqual(expert[0]["instructions"], expert[1]["instructions"])
        self.assertIsNotNone(expert[1]["session_id"])
        self.assertIn("=== REPO RESULTS", expert[1]["prompt"])
        self.assertNotIn(self.last_engine._language_rule, expert[1]["prompt"])  # the system text carries it

    def test_a_later_run_resumes_with_the_same_system_text_and_no_protocol_in_the_message(self) -> None:
        engine = self.engine()
        first = FakeRunner(self.plain_scripts(["FINAL"], ["Fine.\nVERDICT: REJECTED"]))
        result = self.debate(first, CLAUDE_LINEUP, engine)
        second = FakeRunner(self.plain_scripts(["FINAL again"], ["Fine.\nVERDICT: REJECTED"]))
        self.debate(second, CLAUDE_LINEUP, engine, thread_id=result["thread_id"])
        before, after = self.calls(first, "expert")[0], self.calls(second, "expert")[0]
        self.assertIsNotNone(after["session_id"])  # resumed
        self.assertEqual(before["instructions"], after["instructions"])
        self.assertNotIn(PROTOCOL_MARK, after["prompt"])
        self.assertNotIn("Repository retrieval", after["prompt"])

    def test_a_session_recorded_before_the_protocol_gets_it_as_a_message(self) -> None:
        engine = self.engine()
        self.debate(FakeRunner(self.scripts()), CLAUDE_LINEUP, engine)
        message = engine._compose_claude("TASK", "", False, False, resend_protocol=True)
        self.assertTrue(message.startswith("=== Repository retrieval (read-only) ==="))
        self.assertTrue(message.endswith("TASK"))
        self.assertEqual(engine._compose_claude("TASK", "", False, False), "TASK")

    def test_other_providers_get_the_whole_first_message_as_before(self) -> None:
        runner = FakeRunner(self.scripts(), provider="codex-cli")
        self.debate(runner, CODEX_LINEUP)
        for role, persona in (("expert", "domain_architect"), ("critic", "critic_redteam"), ("judge", "jev_mcp_decision")):
            call = self.calls(runner, role)[0]
            self.assertIsNone(call["instructions"])
            self.assertIsNone(call["plain_prompt"])
            prompt = call["prompt"]
            self.assertIn(self.persona(persona), prompt)
            self.assertIn("=== Repository retrieval (read-only) ===", prompt)
            self.assertIn(self.last_engine._language_rule, prompt)
        self.assertIn("VERDICT", self.calls(runner, "judge")[0]["prompt"])

    def test_claude_runs_also_hand_the_plain_message_to_the_runner_for_a_fallback_provider(self) -> None:
        runner = FakeRunner(self.scripts())
        first = self.debate(runner, CLAUDE_LINEUP)
        call = self.calls(runner, "expert")[0]
        plain = call["plain_prompt"].replace(first["thread_id"], "THREAD")
        self.assertIn(self.persona("domain_architect"), plain)
        self.assertIn("=== Repository retrieval (read-only) ===", plain)
        self.assertIn("Task: task", plain)
        # What the codex lineup sends as its message is exactly this plain message.
        codex = FakeRunner(self.scripts(), provider="codex-cli")
        second = self.debate(codex, CODEX_LINEUP)
        self.assertEqual(self.calls(codex, "expert")[0]["prompt"].replace(second["thread_id"], "THREAD"), plain)

    def test_agent_started_reports_the_standing_part(self) -> None:
        self.debate(FakeRunner(self.scripts()), CLAUDE_LINEUP)
        started = [e["data"] for e in self.events() if e["type"] == "agent_started"]
        self.assertTrue(all(d["system_bytes"] > 1000 for d in started))
        self.assertTrue(all(d["prompt_bytes"] > 1000 for d in started))  # the plain first turn, as before
        self.assertTrue(all(d["history_bytes"] == 0 for d in started))  # a new thread replays nothing

    def test_agent_started_has_zero_system_bytes_without_claude(self) -> None:
        self.debate(FakeRunner(self.scripts(), provider="codex-cli"), CODEX_LINEUP)
        started = [e["data"] for e in self.events() if e["type"] == "agent_started"]
        self.assertEqual({d["system_bytes"] for d in started}, {0})

    def test_a_replayed_turn_gets_a_task_only_message_for_claude_and_the_full_one_for_the_rest(self) -> None:
        runner = FakeRunner(self.scripts(), lose_session_for="expert")
        self.debate(runner, CLAUDE_LINEUP)
        replay = [c for c in self.calls(runner, "expert") if c["session_id"] is None][-1]
        self.assertIn("=== Earlier in this turn ===", replay["prompt"])
        self.assertNotIn(PROTOCOL_MARK, replay["prompt"])
        self.assertIn(PROTOCOL_MARK, replay["plain_prompt"])
        self.assertIsNotNone(replay["instructions"])

    def test_prompt_version_was_bumped(self) -> None:
        self.assertEqual(RETRIEVAL_PROMPT_VERSION, "2026-10-23.p20")
        self.assertEqual(wiki_build.WIKI_PROMPT_VERSION, "2026-10-16.wiki6")

    def test_role_prompt_plain_matches_the_old_concatenation(self) -> None:
        self.assertEqual(RolePrompt(task="T", persona="P", rules="R").plain(), "P\n\nTR")
        self.assertEqual(RolePrompt(task="T", persona="", rules="R").plain(), "\n\nTR")
        self.assertEqual(RolePrompt(task="only").plain(), "only")


class CompactingClaude(FakeRunner):
    """A claude session model: the system text is recorded at the first request and kept; after `compact_after`
    messages the earlier conversation is replaced by a summary. `views` holds what the agent sees on each call."""

    def __init__(self, scripts: Dict[str, Any], compact_after: int = 1, **kwargs: Any) -> None:
        super().__init__(scripts, **kwargs)
        self.compact_after = compact_after
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.views: List[Dict[str, Any]] = []
        self._view_lock = threading.Lock()

    def __call__(self, prompt, context=None, session_id=None, provider=None, providers=None, repo_mcp=None,
                 system=None, instructions=None, plain_prompt=None) -> LLMResult:
        result = super().__call__(prompt, context, session_id, provider, providers, repo_mcp, system, instructions,
                                  plain_prompt)
        sid = result.session_id
        with self._view_lock:
            session = self.sessions.setdefault(sid, {"system": instructions.text() if instructions else "", "messages": []})
            session["messages"].append(prompt)
            if len(session["messages"]) > self.compact_after + 1:
                session["messages"] = ["[summary of the earlier conversation]", prompt]
            role = self.role_of(prompt, session_id)
            self.views.append({"role": role, "sid": sid, "view": session["system"] + "\n" + "\n".join(session["messages"])})
        return result


class CompactionTests(SplitHarness):
    def run_expert(self, lineup: Lineup, provider: str) -> CompactingClaude:
        # Five retrieval steps, so the session is compacted several times before the final answer.
        script = [READ_NOTES] * 5 + ["FINAL"]
        runner = CompactingClaude(self.plain_scripts(script, ["Fine.\nVERDICT: REJECTED"]), compact_after=1,
                                  provider=provider)
        self.debate(runner, lineup)
        return runner

    def test_a_claude_session_keeps_role_protocol_and_language_after_compaction(self) -> None:
        runner = self.run_expert(CLAUDE_LINEUP, "claude-cli")
        views = [v["view"] for v in runner.views if v["role"] == "expert"]
        self.assertEqual(len(views), 6)
        persona = self.persona("domain_architect")
        for view in views:
            self.assertIn(persona, view)
            self.assertIn(PROTOCOL_MARK, view)
            self.assertIn(self.last_engine._language_rule, view)
            self.assertIn(self.sha, view)
        # The conversation really was compacted: the task of the first message is gone from the last view.
        self.assertIn("[summary of the earlier conversation]", views[-1])
        self.assertNotIn("Task: task", views[-1])
        self.assertIn("=== REPO RESULTS", views[-1])

    def test_without_a_system_prompt_the_same_compaction_loses_them(self) -> None:
        # This is the incident: for a provider that gets everything in its first message, compaction drops the role.
        runner = self.run_expert(CODEX_LINEUP, "codex-cli")
        views = [v["view"] for v in runner.views if v["role"] == "expert"]
        self.assertIn(PROTOCOL_MARK, views[0])
        self.assertNotIn(PROTOCOL_MARK, views[-1])
        self.assertNotIn(self.persona("domain_architect"), views[-1])


class WikiSplitTests(BuildCase):
    def build_with_claude(self, runner: Any) -> Dict[str, Any]:
        builder = self.builder()
        self.last_builder = builder
        builder.engine.llm.available_providers = ["claude"]
        return self.build(runner, builder)

    def test_the_writer_session_keeps_its_role_protocol_and_language_in_the_system_prompt(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        report = self.build_with_claude(runner)
        self.assertEqual(report["failed"], 0)
        first, second = [c for c in runner.calls if c["role"] == "expert"]
        system = first["instructions"].text()
        self.assertTrue(system.startswith(wiki_build.WRITER_SYSTEM))
        self.assertIn(PROTOCOL_MARK, system)
        self.assertIn(self.sha, system)
        self.assertIn("Write your whole reply in Korean", system)
        self.assertEqual(first["instructions"], second["instructions"])
        # The first message is the page task only.
        for standing in (wiki_build.WRITER_SYSTEM, PROTOCOL_MARK, "Repository retrieval", "System:"):
            self.assertNotIn(standing, first["prompt"])
        self.assertIn("Scope: svc/", first["prompt"])
        self.assertIn("Answer format: your whole final reply is the page", first["prompt"])
        # Providers without a system prompt still get the old text, headed by the runner's System line.
        self.assertEqual(first["system"], wiki_build.WRITER_SYSTEM)
        self.assertIn("=== Repository retrieval (read-only) ===", first["plain_prompt"])
        self.assertIn("Scope: svc/", first["plain_prompt"])

    def test_the_writer_protocol_uses_the_root_summary_in_the_system_text(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        self.build_with_claude(runner)
        system = [c for c in runner.calls if c["role"] == "expert"][0]["instructions"]
        self.assertIn("=== Repository state ===", system.dynamic)
        self.assertNotIn("Repository map of the whole", system.text())  # the task carries the scope map

    def test_without_claude_the_writer_prompt_is_unchanged(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        self.build(runner)
        first = [c for c in runner.calls if c["role"] == "expert"][0]
        self.assertIsNone(first["instructions"])
        self.assertIn("=== Repository retrieval (read-only) ===", first["prompt"])
        self.assertEqual(first["system"], wiki_build.WRITER_SYSTEM)


if __name__ == "__main__":
    unittest.main()

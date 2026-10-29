import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Union
from unittest.mock import patch

from debate_engine import RETRIEVAL_PROMPT_VERSION, DebateEngine, JUDGE_JSON_EXAMPLE, JUDGE_VERDICT_RULE, judge_missing_parts
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from mcp_client import MCPClient
from repo_map import MAP_VERSION
from repo_tools import RepoTools, find_pseudo_tool_calls
from team_manager import Lineup, Member
from test_repo_tools import COMMIT, git, make_repo
from testenv import isolate_data_dir, restore_data_dir, set_auto_apply

Script = Union[str, Callable[[str], str]]


def setUpModule() -> None:
    # These tests cover the retrieval loop and events; the jev gate has its own tests.
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # Keep the symbol index cache (and anything else) out of the real data directory.
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def block(*requests: Dict[str, Any]) -> str:
    return "Let me check the code.\n```repo\n" + json.dumps({"requests": list(requests)}) + "\n```"


READ_NOTES = block({"op": "read", "path": "apps/agent/notes.txt"})


class FakeRunner:
    """Stands in for LLMRunner.run_session_prompt. Replies come from per-role scripts.

    A script is a list of replies (the last one repeats) or a function of the prompt. Session ids
    are "<role>-s<n>"; a continuation call is recognized by its session id.
    """

    def __init__(self, scripts: Dict[str, Union[List[Script], Callable[[str], str]]], provider: str = "claude-cli",
                 stateless: bool = False, lose_session_for: Optional[str] = None, failures: Optional[List[str]] = None) -> None:
        self.scripts = scripts
        self.provider = provider
        self.stateless = stateless
        self.lose_session_for = lose_session_for
        self.failures = failures or []
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._lost = False

    @staticmethod
    def role_of(prompt: str, session_id: Optional[str]) -> str:
        if session_id:
            return session_id.split("-")[0]
        if "Decide if consensus is reached" in prompt:
            return "judge"
        if "Critique this proposal rigorously" in prompt:
            return "critic"
        return "expert"

    def __call__(self, prompt, context=None, session_id=None, provider=None, providers=None, repo_mcp=None,
                 system=None, instructions=None, plain_prompt=None) -> LLMResult:
        with self._lock:
            role = self.role_of(prompt, session_id)
            self.calls.append({"role": role, "prompt": prompt, "session_id": session_id, "provider": provider,
                               "system": system, "instructions": instructions, "plain_prompt": plain_prompt})
            if session_id and role == self.lose_session_for and not self._lost:
                self._lost = True  # another provider answered in a brand-new session
                return LLMResult("codex-cli", "GARBAGE from a session that saw nothing", f"{role}-s99", resumed=False)
            script = self.scripts[role]
            if callable(script):
                text: Any = script
            else:
                text = script[0] if len(script) == 1 else script.pop(0)
            n = len(self.calls)
        if callable(text):
            text = text(prompt)  # outside the lock, so scripts may block (a barrier for the panel test)
        if self.stateless:
            return LLMResult("opencode-cli", text, failures=list(self.failures))
        return LLMResult(self.provider, text, session_id or f"{role}-s{n}", resumed=session_id is not None,
                         failures=list(self.failures))

    def prompts(self, role: str) -> List[str]:
        return [c["prompt"] for c in self.calls if c["role"] == role]

    def system_texts(self, role: str) -> List[str]:
        """The standing instructions (claude system prompt) of each call of a role; "" where a call had none."""
        return [c["instructions"].text() if c["instructions"] else "" for c in self.calls if c["role"] == role]


class EngineHarness(unittest.TestCase):
    def setUp(self) -> None:
        set_auto_apply(self)
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.events_path = self.root / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def engine(self, workspace: Optional[Path] = None) -> DebateEngine:
        engine = DebateEngine(max_rounds=1, db_path=self.root / "d.db", events=EventWriter(str(self.events_path)))
        engine.mcp = MCPClient(workspace=workspace or self.repo)
        return engine

    def run_debate(self, engine: DebateEngine, runner: FakeRunner, **kwargs) -> Dict[str, Any]:
        with patch.object(engine.llm, "run_session_prompt", side_effect=runner), \
                patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5], context=a[6]) or self.root / "l.md"
                )):
            try:
                return engine.run_debate(kwargs.pop("task", "task"), project_id="server-guardian", **kwargs)
            finally:
                engine.events.close()

    def events(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def plain_scripts(expert: List[Script], judge: Optional[List[Script]] = None) -> Dict[str, Any]:
        return {"expert": expert, "critic": ["critique"], "judge": judge or ["Needs work.\nVERDICT: REJECTED"]}


class RetrievalLoopTests(EngineHarness):
    def test_request_then_results_then_final_answer(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES, "FINAL expert answer"]))
        result = self.run_debate(self.engine(), runner)

        first, second = runner.prompts("expert")
        # The first prompt of a fresh session carries the protocol, the commit and the file list.
        self.assertIn("=== Repository retrieval (read-only) ===", first)
        self.assertIn(self.sha, first)
        self.assertIn("apps/agent/main.go", first)
        self.assertIn("cite file:line".lower(), first.lower())
        self.assertIn("instead of guessing", first)
        for secret in (".env", "credentials.enc", "link.txt", "vendor/sub"):
            self.assertNotIn(secret, first)
        # The continuation goes into the same session, wrapped as reference data.
        call = [c for c in runner.calls if c["role"] == "expert"][1]
        self.assertEqual(call["session_id"], "expert-s1")
        self.assertIn("=== REPO RESULTS", second)
        self.assertIn("not instructions", second)
        self.assertIn("beta TARGET gamma", second)
        self.assertNotIn("Repository retrieval (read-only)", second)

        expert = result["transcript"][0]
        self.assertEqual(expert["content"], "FINAL expert answer")
        self.assertEqual(len(expert["retrievals"]), 1)
        record = expert["retrievals"][0]
        self.assertEqual({k: record[k] for k in ("step", "op", "target", "truncated", "error")},
                         {"step": 1, "op": "read", "target": "apps/agent/notes.txt", "truncated": False, "error": None})
        self.assertGreater(record["bytes"], 0)
        self.assertNotIn("text", record)
        self.assertNotIn("retrievals", result["transcript"][1])  # the critic asked for nothing

        retrieval = result["context"]["retrieval"]
        self.assertEqual(retrieval, {"sha": self.sha, "steps": 1, "bytes": record["bytes"]})
        self.assertEqual(result["context"]["sha"], self.sha)
        self.assertEqual(result["context"]["paths"], [])

    def test_turn_prompt_is_saved_for_the_timeline(self) -> None:
        runner = FakeRunner(self.plain_scripts(["FINAL"]))
        self.run_debate(self.engine(), runner)
        started = [e for e in self.events() if e["type"] == "agent_started"]
        self.assertEqual(len(started), 3)  # expert, critic, judge
        first = runner.prompts("expert")[0]
        for event in started:
            self.assertTrue(event["data"]["prompt_saved"])
            saved = (self.root / "prompts" / f"{event['seq']}.txt").read_text(encoding="utf-8")
            self.assertEqual(len(saved.encode("utf-8")), event["data"]["prompt_bytes"])
        expert = next(e for e in started if e["agent"] == "expert" or e.get("role") == "Domain Expert")
        self.assertEqual((self.root / "prompts" / f"{expert['seq']}.txt").read_text(encoding="utf-8"), first)

    def test_run_retrieval_totals_in_event_and_result(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES, READ_NOTES, "FINAL"]))
        result = self.run_debate(self.engine(), runner)
        served = [r for r in result["transcript"][0]["retrievals"]]
        total = sum(r["bytes"] for r in served)
        self.assertEqual(len(served), 2)
        self.assertGreater(total, 0)
        usage = {"answers": 3, "with_findings": 0, "checked": 0, "mismatched": 0}  # expert, critic, judge: no block
        expected = {"results": 2, "bytes": total, "by_agent": {"Domain Expert": {"results": 2, "bytes": total, "steps": 2}},
                    "findings": {"claude-cli": usage}}
        self.assertEqual(result["retrieval"], expected)
        finished = self.events()[-1]
        self.assertEqual(finished["type"], "debate_finished")
        self.assertEqual(finished["data"]["retrieval"], expected)
        json.dumps(result["retrieval"])

    def test_run_retrieval_totals_skip_failed_items(self) -> None:
        missing = block({"op": "read", "path": "apps/agent/does-not-exist.txt"})
        result = self.run_debate(self.engine(), FakeRunner(self.plain_scripts([missing, READ_NOTES, "FINAL"])))
        records = result["transcript"][0]["retrievals"]
        self.assertEqual([bool(r["error"]) for r in records], [True, False])
        good = records[1]["bytes"]
        self.assertEqual(result["retrieval"]["results"], 1)
        self.assertEqual(result["retrieval"]["bytes"], good)
        self.assertEqual(result["retrieval"]["by_agent"]["Domain Expert"], {"results": 1, "bytes": good, "steps": 2})

    def test_run_retrieval_totals_are_per_run_not_cumulative(self) -> None:
        engine = self.engine()
        first = self.run_debate(engine, FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"])), thread_id="thr_keep")
        self.events_path.unlink()
        engine2 = self.engine()
        second = self.run_debate(engine2, FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"])), thread_id="thr_keep")
        self.assertEqual(first["retrieval"]["results"], 1)
        self.assertEqual(second["retrieval"]["results"], 1)
        self.assertEqual(second["context"]["retrieval"]["steps"], 2)  # the thread snapshot stays cumulative

    def test_run_retrieval_totals_zero_without_requests(self) -> None:
        result = self.run_debate(self.engine(), FakeRunner(self.plain_scripts(["FINAL"])))
        self.assertEqual(result["retrieval"], {"results": 0, "bytes": 0, "by_agent": {}, "findings": {
            "claude-cli": {"answers": 3, "with_findings": 0, "checked": 0, "mismatched": 0}}})

    def test_event_order_and_shapes(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"]))
        self.run_debate(self.engine(), runner)
        events = self.events()
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual([e["type"] for e in events], [
            "debate_started", "round_started",
            "agent_started", "retrieval_requested", "retrieval_served", "agent_finished",  # expert
            "agent_started", "agent_finished",  # critic
            "agent_started", "agent_finished",  # judge
            "verdict", "debate_finished",
        ])
        started = events[0]
        self.assertNotIn("round", started)
        self.assertEqual(started["data"]["task"], "task")
        self.assertEqual(started["data"]["project"], "server-guardian")
        context = started["data"]["context"]
        self.assertEqual({k: context[k] for k in ("sha", "files")}, {"sha": self.sha, "files": 0})
        self.assertGreater(context["map_bytes"], 0)
        self.assertEqual((context["prompt_version"], context["map_version"]), (RETRIEVAL_PROMPT_VERSION, MAP_VERSION))
        self.assertEqual([m["role"] for m in started["data"]["members"]], ["Domain Expert", "Critic Agent", "JEV-MCP Judge"])
        self.assertEqual(set(started["data"]["members"][0]), {"role", "agent", "agent_name", "providers"})
        self.assertEqual(events[1]["round"], 1)
        self.assertNotIn("agent", events[1])
        expert_events = events[2:6]
        for e in expert_events:
            self.assertEqual((e["round"], e["role"], e["agent"]), (1, "Domain Expert", "Domain Expert"))
            self.assertTrue(e["agent_name"])
            self.assertTrue(e["ts"].endswith("Z") and len(e["ts"]) == 24)
        self.assertEqual(events[2]["data"]["resumed"], False)
        self.assertGreater(events[2]["data"]["prompt_bytes"], 1000)
        self.assertEqual(events[3]["data"], {"step": 1, "requests": [{"op": "read", "target": "apps/agent/notes.txt"}], "via": "block"})
        served = events[4]["data"]
        self.assertEqual(set(served), {"step", "results", "turn_bytes", "via"})
        self.assertEqual(served["via"], "block")
        self.assertEqual(set(served["results"][0]), {"op", "target", "ok", "bytes", "truncated", "error"})
        self.assertEqual(served["turn_bytes"], served["results"][0]["bytes"])
        self.assertNotIn("TARGET gamma", json.dumps(events))  # file content never goes into events
        finished = events[5]["data"]
        self.assertEqual(set(finished), {"provider", "duration_ms", "steps", "output_bytes", "excerpt", "fallback"})
        self.assertEqual((finished["provider"], finished["steps"], finished["fallback"], finished["excerpt"]),
                         ("claude-cli", 1, False, "FINAL"))
        self.assertEqual(events[10]["data"], {"approved": False, "unverified": False})
        done = dict(events[11]["data"])
        self.assertEqual(done.pop("retrieval")["results"], 1)
        self.assertEqual(done, {"status": "REVISION_REQUIRED", "consensus": False, "final_round": 1})
        self.assertTrue(all(e["thread_id"] == events[0]["thread_id"] and e["thread_id"].startswith("thread_") for e in events))

    def test_budget_exhaustion_strips_blocks(self) -> None:
        listing = block({"op": "list"})
        runner = FakeRunner(self.plain_scripts([listing, listing, listing, "Best effort.\n" + listing.split("\n", 1)[1]]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "2"}):
            result = self.run_debate(self.engine(), runner)

        expert_prompts = runner.prompts("expert")
        self.assertEqual(len(expert_prompts), 4)
        self.assertIn("REPO BUDGET EXHAUSTED", expert_prompts[3])
        self.assertEqual(result["transcript"][0]["content"], "Best effort.")
        self.assertEqual(len(result["transcript"][0]["retrievals"]), 2)
        types = [e["type"] for e in self.events()]
        self.assertEqual(types.count("retrieval_served"), 2)
        self.assertEqual(types.count("retrieval_exhausted"), 1)
        exhausted = next(e for e in self.events() if e["type"] == "retrieval_exhausted")
        self.assertEqual(exhausted["data"]["steps"], 2)
        self.assertEqual(result["context"]["retrieval"]["steps"], 2)

    def test_turn_byte_budget_clips_results_and_ends_retrieval(self) -> None:
        big = block({"op": "read", "path": "apps/agent/big.txt"})
        runner = FakeRunner(self.plain_scripts([big, big, big, "done after exhaustion"]))
        env = {"GUARDIAN_RETRIEVAL_STEP_BYTES": "2000", "GUARDIAN_RETRIEVAL_TURN_BYTES": "3000"}
        with patch.dict(os.environ, env):
            result = self.run_debate(self.engine(), runner)
        records = result["transcript"][0]["retrievals"]
        self.assertTrue(all(r["truncated"] for r in records))
        self.assertLessEqual(records[0]["bytes"], 2000 + 64)
        self.assertEqual(len(records), 2)  # the third request was refused: the turn budget was spent
        self.assertEqual(result["transcript"][0]["content"], "done after exhaustion")
        self.assertIn("REPO BUDGET EXHAUSTED", runner.prompts("expert")[-1])

    def test_malformed_block_costs_a_step_and_gets_an_error(self) -> None:
        runner = FakeRunner(self.plain_scripts(["```repo\n{oops\n```", block({"op": "frobnicate"}), "recovered"]))
        result = self.run_debate(self.engine(), runner)
        _, second, third = runner.prompts("expert")
        self.assertIn("invalid JSON in repo block", second)
        self.assertIn("unknown op", third)
        errors = [r["error"] for r in result["transcript"][0]["retrievals"]]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(errors))
        self.assertEqual(result["context"]["retrieval"]["steps"], 2)
        self.assertEqual(result["transcript"][0]["content"], "recovered")

    def test_excluded_files_are_not_served(self) -> None:
        runner = FakeRunner(self.plain_scripts([block({"op": "read", "path": "apps/agent/.env"}), "ok"]))
        result = self.run_debate(self.engine(), runner)
        self.assertNotIn("ENV_SECRET", runner.prompts("expert")[1])
        self.assertEqual(result["transcript"][0]["retrievals"][0]["error"], "excluded")

    def test_provider_change_resends_the_turn_and_exchange(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES, "FINAL after failover"]), lose_session_for="expert")
        result = self.run_debate(self.engine(), runner)
        expert_calls = [c for c in runner.calls if c["role"] == "expert"]
        self.assertEqual(len(expert_calls), 3)
        self.assertEqual(expert_calls[1]["session_id"], "expert-s1")  # tried to continue
        replay = expert_calls[2]
        self.assertIsNone(replay["session_id"])  # then started over
        self.assertIn("Formulate a complete architectural proposal", replay["prompt"])  # the turn's own prompt
        self.assertIn("Repository retrieval (read-only)", replay["prompt"])  # the protocol the new session never saw
        self.assertIn("Let me check the code.", replay["prompt"])  # the agent's earlier request
        self.assertIn("beta TARGET gamma", replay["prompt"])  # and its results
        self.assertNotIn("GARBAGE", replay["prompt"])
        self.assertEqual(result["transcript"][0]["content"], "FINAL after failover")
        self.assertEqual(len(result["transcript"][0]["retrievals"]), 1)

    def test_stateless_provider_gets_the_whole_exchange_every_step(self) -> None:
        listing = block({"op": "list", "path": "cli"})
        runner = FakeRunner(self.plain_scripts([READ_NOTES, listing, "stateless final"]), stateless=True)
        result = self.run_debate(self.engine(), runner)
        expert_calls = [c for c in runner.calls if c["role"] == "expert"]
        self.assertEqual(len(expert_calls), 3)
        self.assertTrue(all(c["session_id"] is None for c in expert_calls))
        third = expert_calls[2]["prompt"]
        self.assertEqual(third.count("=== REPO RESULTS"), 2)  # step 1 and step 2 results
        self.assertIn("cli/tool.py", third)
        self.assertIn("beta TARGET gamma", third)
        self.assertEqual(result["transcript"][0]["content"], "stateless final")

    def test_retrieval_can_be_switched_off(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL": "0"}):
            result = self.run_debate(self.engine(), runner)
        self.assertEqual(len(runner.prompts("expert")), 1)
        self.assertNotIn("Repository retrieval", runner.prompts("expert")[0])
        self.assertIn("```repo", result["transcript"][0]["content"])  # left as the final answer
        self.assertIsNone(result["context"])

    def test_workspace_without_git_repository_has_no_retrieval(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        runner = FakeRunner(self.plain_scripts([READ_NOTES]))
        result = self.run_debate(self.engine(workspace=plain), runner)
        self.assertEqual(len(runner.prompts("expert")), 1)
        self.assertIsNone(result["context"])

    def test_context_paths_still_work_and_keep_their_pack(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"]))
        result = self.run_debate(self.engine(), runner, context_paths=["cli/tool.py"])
        first = runner.prompts("expert")[0]
        self.assertIn("=== Repository code", first)
        self.assertIn("RUN_BODY", first)
        self.assertEqual(first.count("--- Repository file list"), 1)  # not repeated by the retrieval text
        self.assertIn("Repository retrieval (read-only)", first)
        self.assertEqual(result["context"]["paths"], ["cli/tool.py"])
        self.assertEqual(result["context"]["retrieval"]["steps"], 1)

    def test_continued_thread_pins_the_commit_and_accumulates_totals(self) -> None:
        engine = self.engine()
        first = self.run_debate(engine, FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"])))
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-qm", "later")
        runner = FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"]))
        engine.events = EventWriter(str(self.root / "e2.jsonl"))
        second = self.run_debate(engine, runner, thread_id=first["thread_id"])
        self.assertEqual(second["context"]["sha"], self.sha)  # still the first commit
        self.assertEqual(second["context"]["retrieval"]["steps"], 2)
        # Resumed sessions already know the protocol, so it is not sent again.
        self.assertNotIn("Repository retrieval (read-only)", runner.prompts("expert")[0])


class JudgeSeenTests(EngineHarness):
    PATCH = json.dumps({
        "decision": "Change the note.", "constraints": ["The note says CHANGED."], "checks": [],
        "actions": [{"tool": "jev_mcp:apply_patch", "reason": "r", "arguments": {
            "edits": [{"file_path": "apps/agent/notes.txt", "old": "beta TARGET gamma", "new": "beta CHANGED gamma"}]}}],
    })
    APPROVAL = f"Approved.\n```json\n{PATCH}\n```\nVERDICT: APPROVED"

    def test_code_the_judge_read_is_accepted_for_apply_patch(self) -> None:
        runner = FakeRunner(self.plain_scripts(["proposal"], judge=[READ_NOTES, self.APPROVAL]))
        result = self.run_debate(self.engine(), runner)
        action = result["final_decision"]["actions"][0]
        self.assertEqual((action["tool"], action["status"], action["error"]), ("jev_mcp:apply_patch", "PROPOSED", None))
        self.assertEqual(result["transcript"][2]["retrievals"][0]["op"], "read")
        types = [e["type"] for e in self.events()]
        self.assertIn("action_proposed", types)
        proposed = next(e for e in self.events() if e["type"] == "action_proposed")
        self.assertEqual(proposed["data"], {"id": action["id"], "tool": "jev_mcp:apply_patch", "status": "PROPOSED"})
        self.assertEqual(types[-2:], ["action_proposed", "debate_finished"])

    def test_code_never_read_is_still_refused(self) -> None:
        runner = FakeRunner(self.plain_scripts(["proposal"], judge=[self.APPROVAL]))
        result = self.run_debate(self.engine(), runner)
        action = result["final_decision"]["actions"][0]
        self.assertEqual(action["status"], "INVALID")
        self.assertIn("not shown", action["error"])

    def test_code_only_the_expert_read_does_not_count_for_the_judge(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES, "proposal"], judge=[self.APPROVAL]))
        result = self.run_debate(self.engine(), runner)
        self.assertEqual(result["final_decision"]["actions"][0]["status"], "INVALID")

    def test_a_truncated_read_does_not_cover_code_beyond_the_cut(self) -> None:
        # notes.txt is small, so cut the budget: the judge only saw a prefix of the file.
        runner = FakeRunner(self.plain_scripts(["proposal"], judge=[READ_NOTES, self.APPROVAL]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEP_BYTES": "20"}):
            result = self.run_debate(self.engine(), runner)
        self.assertEqual(result["final_decision"]["actions"][0]["status"], "INVALID")


class ProductionProblemTests(EngineHarness):
    """Countermeasures for the problems found in the 2026-10-09 production run."""

    LIMIT_VARS = ("GUARDIAN_RETRIEVAL_STEPS", "GUARDIAN_RETRIEVAL_REQUESTS", "GUARDIAN_RETRIEVAL_STEP_BYTES",
                  "GUARDIAN_RETRIEVAL_TURN_BYTES")

    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for name in self.LIMIT_VARS:
            os.environ.pop(name, None)

    def expert_entry(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return result["transcript"][0]

    def event(self, type_: str, nth: int = 0) -> Dict[str, Any]:
        return [e for e in self.events() if e["type"] == type_][nth]

    # --- limits are off unless configured ---------------------------------------------------------

    def test_limits_are_unlimited_by_default_and_apply_only_when_positive(self) -> None:
        self.assertEqual(DebateEngine._limits(), {"steps": None, "requests": None, "step_bytes": None, "turn_bytes": None})
        for value in ("", "0", "-3", "abc"):
            with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": value}):
                self.assertIsNone(DebateEngine._limits()["steps"])
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "5", "GUARDIAN_RETRIEVAL_REQUESTS": "3",
                                     "GUARDIAN_RETRIEVAL_STEP_BYTES": "160000", "GUARDIAN_RETRIEVAL_TURN_BYTES": "640000"}):
            self.assertEqual(DebateEngine._limits(),
                             {"steps": 5, "requests": 3, "step_bytes": 160000, "turn_bytes": 640000})

    def test_prompt_states_the_budget_only_when_one_is_configured(self) -> None:
        intro = DebateEngine._retrieval_intro(self.sha, ["a.go"], with_list=False)
        self.assertIn("no fixed budget", intro)
        self.assertNotIn("Budget per turn", intro)
        self.assertIn("outline", intro)
        self.assertIn("stop requesting", intro)
        # The symbol ops are documented in the example block and the operation list.
        self.assertIn('{"op": "defs", "symbol":', intro)
        self.assertIn('{"op": "refs", "symbol":', intro)
        self.assertIn("TypeScript/JavaScript", intro)
        self.assertIn("prefer defs/refs over grep", intro)
        self.assertIn("outline large files first", intro)
        self.assertIn("by name only", intro)
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "6", "GUARDIAN_RETRIEVAL_REQUESTS": "4",
                                     "GUARDIAN_RETRIEVAL_STEP_BYTES": str(160 * 1024),
                                     "GUARDIAN_RETRIEVAL_TURN_BYTES": str(640 * 1024)}):
            limited = DebateEngine._retrieval_intro(self.sha, ["a.go"], with_list=False)
        self.assertIn("Budget per turn: 6 request steps, 4 requests per step, 160 KB per step, 640 KB in total", limited)
        self.assertNotIn("no fixed budget", limited)

    def test_prompt_rules_one_block_last_no_conclusions_no_claims_of_unread_code(self) -> None:
        intro = DebateEngine._retrieval_intro(self.sha, [], with_list=False)
        self.assertIn("exactly ONE repo block", intro)
        self.assertIn("LAST", intro)
        self.assertIn("discarded", intro)
        self.assertIn("Do not write conclusions", intro)
        self.assertIn("never claim to have read code", intro)

    def test_many_steps_and_requests_run_without_any_limit(self) -> None:
        many = block(*[{"op": "read", "path": "apps/agent/notes.txt"}] * 12)
        runner = FakeRunner(self.plain_scripts([many] * 12 + ["FINAL"]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(entry["content"], "FINAL")
        self.assertEqual(len(entry["retrievals"]), 12 * 12)
        self.assertFalse(any(r.get("error") for r in entry["retrievals"]))
        self.assertEqual(self.events() and [e for e in self.events() if e["type"] == "retrieval_exhausted"], [])

    # --- a reply with a block is a request; text after the first block is dropped --------------------

    def test_text_after_the_block_is_discarded_and_reported(self) -> None:
        fabricated = "Now I have read enough code. The handler never validates input, so it is vulnerable."
        runner = FakeRunner(self.plain_scripts([READ_NOTES + "\n\n" + fabricated, "REAL analysis"]))
        result = self.run_debate(self.engine(), runner)

        self.assertEqual(self.expert_entry(result)["content"], "REAL analysis")
        self.assertNotIn("vulnerable", json.dumps(result["transcript"]))
        message = runner.prompts("expert")[1]
        self.assertIn(f"{len(fabricated)} characters you wrote after your repo block were discarded", message)
        self.assertIn("beta TARGET gamma", message)
        trimmed = self.event("reply_trimmed")
        self.assertEqual(trimmed["data"], {"step": 1, "discarded_chars": len(fabricated), "blocks": 1})
        self.assertEqual((trimmed["round"], trimmed["role"], trimmed["agent"]), (1, "Domain Expert", "Domain Expert"))
        types = [e["type"] for e in self.events()]
        self.assertLess(types.index("reply_trimmed"), types.index("retrieval_requested"))

    def test_clean_reply_is_not_reported_as_trimmed(self) -> None:
        self.run_debate(self.engine(), FakeRunner(self.plain_scripts([READ_NOTES, "FINAL"])))
        self.assertNotIn("reply_trimmed", [e["type"] for e in self.events()])

    def test_replay_after_provider_change_contains_only_the_trimmed_reply(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_NOTES + "\n\nFABRICATED conclusion", "FINAL"]), lose_session_for="expert")
        self.run_debate(self.engine(), runner)
        replay = [c for c in runner.calls if c["role"] == "expert" and c["session_id"] is None][-1]["prompt"]
        self.assertIn("[Your reply]", replay)
        self.assertNotIn("FABRICATED", replay)

    def test_requests_of_several_blocks_are_merged_into_one_step(self) -> None:
        reply = (block({"op": "read", "path": "apps/agent/notes.txt"}) + "\n\nI imagine the next step.\n"
                 + block({"op": "outline", "path": "cli/tool.py"}) + "\nAnd another.\n"
                 + block({"op": "list", "path": "cli"}))
        runner = FakeRunner(self.plain_scripts([reply, "FINAL"]))
        result = self.run_debate(self.engine(), runner)

        self.assertEqual(self.expert_entry(result)["content"], "FINAL")
        self.assertEqual([(r["step"], r["op"]) for r in self.expert_entry(result)["retrievals"]],
                         [(1, "read"), (1, "outline"), (1, "list")])
        self.assertEqual(len([e for e in self.events() if e["type"] == "retrieval_served"]), 1)
        self.assertEqual(self.event("reply_trimmed")["data"]["blocks"], 3)
        self.assertEqual([r["op"] for r in self.event("retrieval_requested")["data"]["requests"]],
                         ["read", "outline", "list"])
        self.assertNotIn("imagine", json.dumps(self.events()))

    # --- deferral -----------------------------------------------------------------------------------

    def test_requests_over_the_count_limit_are_deferred_with_a_copyable_block(self) -> None:
        reqs = [{"op": "read", "path": "apps/agent/notes.txt"}, {"op": "outline", "path": "cli/tool.py"},
                {"op": "grep", "pattern": "TARGET", "glob": "apps/agent/n*"}, {"op": "list", "path": "cli"}]
        runner = FakeRunner(self.plain_scripts([block(*reqs), "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_REQUESTS": "2"}):
            result = self.run_debate(self.engine(), runner)

        served = self.event("retrieval_served")["data"]["results"]
        self.assertEqual([r["op"] for r in served], ["read", "outline", "grep", "list"])  # each keeps its own op
        self.assertEqual([r.get("deferred", False) for r in served], [False, False, True, True])
        for deferred in served[2:]:
            self.assertEqual((deferred["ok"], deferred["error"], deferred["bytes"], deferred["truncated"]),
                             (False, "deferred", 0, False))
        self.assertEqual(served[3]["target"], "cli")
        self.assertNotIn("?", [r["op"] for r in served])
        self.assertEqual(set(served[0]), {"op", "target", "ok", "bytes", "truncated", "error"})  # unchanged unless deferred
        message = runner.prompts("expert")[1]
        self.assertIn("DEFERRED: 2 of your requests were not run", message)
        self.assertIn("deferred: not run, request it again", message)
        copy = message.split("```repo\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(json.loads(copy), {"requests": reqs[2:]})
        self.assertEqual([r.get("deferred") for r in self.expert_entry(result)["retrievals"]], [None, None, True, True])

    def test_deferred_requests_can_be_resent_and_then_run(self) -> None:
        reqs = [{"op": "read", "path": "apps/agent/notes.txt"}, {"op": "list", "path": "cli"}]

        def expert(prompt: str) -> str:
            if "DEFERRED" in prompt:
                copy = prompt.split("```repo\n", 1)[1].split("\n```", 1)[0]
                return "Resending.\n```repo\n" + copy + "\n```"
            if "REPO RESULTS" in prompt:
                return "FINAL"
            return block(*reqs)

        runner = FakeRunner({"expert": expert, "critic": ["critique"], "judge": ["VERDICT: REJECTED"]})
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_REQUESTS": "1"}):
            result = self.run_debate(self.engine(), runner)
        records = self.expert_entry(result)["retrievals"]
        self.assertEqual([(r["step"], r["op"], r.get("deferred")) for r in records],
                         [(1, "read", None), (1, "list", True), (2, "list", None)])
        self.assertTrue(all(r["error"] is None for r in records if not r.get("deferred")))

    def test_requests_that_do_not_fit_the_step_budget_are_deferred_not_failed(self) -> None:
        reqs = [{"op": "read", "path": "apps/agent/notes.txt"}, {"op": "read", "path": "apps/agent/big.txt"},
                {"op": "outline", "path": "cli/tool.py"}]
        runner = FakeRunner(self.plain_scripts([block(*reqs), "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEP_BYTES": "20000"}):
            self.run_debate(self.engine(), runner)

        served = self.event("retrieval_served")["data"]["results"]
        # big.txt would be cut to well under half: deferred whole. The small outline after it still fits.
        self.assertEqual([(r["op"], r["error"], r.get("deferred", False)) for r in served],
                         [("read", None, False), ("read", "deferred", True), ("outline", None, False)])
        self.assertNotIn("budget", [r["error"] for r in served])
        message = runner.prompts("expert")[1]
        copy = message.split("```repo\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(json.loads(copy), {"requests": [reqs[1]]})

    def test_step_budget_full_defers_the_rest_and_a_big_first_read_is_clipped_not_deferred(self) -> None:
        big = {"op": "read", "path": "apps/agent/big.txt"}
        runner = FakeRunner(self.plain_scripts([block(big, {"op": "list", "path": "cli"}), "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEP_BYTES": "3000"}):
            self.run_debate(self.engine(), runner)
        served = self.event("retrieval_served")["data"]["results"]
        self.assertEqual((served[0]["truncated"], served[0]["error"], served[0].get("deferred")), (True, None, None))
        self.assertEqual((served[1]["error"], served[1].get("deferred")), ("deferred", True))  # step full, turn unlimited

    def test_budget_error_only_when_the_turn_budget_is_spent(self) -> None:
        reqs = [{"op": "read", "path": "apps/agent/big.txt"}, {"op": "list", "path": "cli"}]
        runner = FakeRunner(self.plain_scripts([block(*reqs), "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEP_BYTES": "50000", "GUARDIAN_RETRIEVAL_TURN_BYTES": "3000"}):
            self.run_debate(self.engine(), runner)
        served = self.event("retrieval_served")["data"]["results"]
        self.assertEqual((served[1]["error"], served[1].get("deferred")), ("budget", None))

    def test_non_dict_items_are_errors_not_deferred(self) -> None:
        reply = "```repo\n" + json.dumps({"requests": [{"op": "list"}, "oops", {"op": "list", "path": "cli"}]}) + "\n```"
        runner = FakeRunner(self.plain_scripts([reply, "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_REQUESTS": "1"}):
            self.run_debate(self.engine(), runner)
        served = self.event("retrieval_served")["data"]["results"]
        self.assertEqual([(r["error"] is not None, r.get("deferred", False)) for r in served],
                         [(False, False), (True, False), (True, True)])

    # --- citation check -----------------------------------------------------------------------------

    def test_unbacked_citation_is_asked_about_once_and_the_answer_replaced(self) -> None:
        runner = FakeRunner(self.plain_scripts(["The bug is at cli/tool.py:3 and in apps/agent/main.go#Serve.",
                                                "Revised: I could not verify the claim."]))
        result = self.run_debate(self.engine(), runner)

        first, second = runner.prompts("expert")
        self.assertIn("CITATION CHECK", second)
        self.assertIn("never given: apps/agent/main.go, cli/tool.py", second.replace("cli/tool.py, apps/agent/main.go",
                                                                                      "apps/agent/main.go, cli/tool.py"))
        self.assertIn("repo block", second)
        entry = self.expert_entry(result)
        self.assertEqual(entry["content"], "Revised: I could not verify the claim.")
        self.assertNotIn("unverified_citations", entry)
        checks = [e for e in self.events() if e["type"] == "citation_check"]
        self.assertEqual(len(checks), 1)  # the revised answer cites nothing
        self.assertEqual(set(checks[0]["data"]), {"cited", "unverified", "asked", "mentioned", "short"})
        self.assertEqual(checks[0]["data"]["cited"], 2)
        self.assertEqual(sorted(checks[0]["data"]["unverified"]), ["apps/agent/main.go", "cli/tool.py"])
        self.assertTrue(checks[0]["data"]["asked"])
        self.assertEqual(checks[0]["role"], "Domain Expert")

    def test_agent_can_answer_the_citation_check_by_requesting_the_code(self) -> None:
        runner = FakeRunner(self.plain_scripts([
            "Claim about cli/tool.py:3.",
            block({"op": "read", "path": "cli/tool.py"}),
            "Confirmed in cli/tool.py:5, it returns RUN_BODY.",
        ]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(entry["content"], "Confirmed in cli/tool.py:5, it returns RUN_BODY.")
        self.assertNotIn("unverified_citations", entry)
        self.assertEqual(entry["retrievals"][0]["paths"], ["cli/tool.py"])
        checks = [e["data"] for e in self.events() if e["type"] == "citation_check"]
        self.assertEqual([c["asked"] for c in checks], [True, True])
        self.assertEqual([c["unverified"] for c in checks], [["cli/tool.py"], []])

    def test_still_unverified_after_asking_is_recorded_and_not_asked_again(self) -> None:
        runner = FakeRunner(self.plain_scripts(["See cli/tool.py:3.", "Still see cli/tool.py:3."]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(len(runner.prompts("expert")), 2)  # asked once only
        self.assertEqual(entry["content"], "Still see cli/tool.py:3.")
        self.assertEqual(entry["unverified_citations"], ["cli/tool.py"])
        checks = [e["data"] for e in self.events() if e["type"] == "citation_check"]
        self.assertEqual(checks, [{"cited": 1, "unverified": ["cli/tool.py"], "asked": True, "mentioned": 0, "short": 0}] * 2)

    def test_files_that_were_provided_are_not_questioned(self) -> None:
        # Read by request, shown by outline, found by grep, and in the context pack all count; unknown paths do not.
        reply = ("Sources: apps/agent/notes.txt:2, cli/tool.py#run, apps/agent/main.go, apps/agent/big.txt:1, "
                 "docs/nonexistent.md, apps/agent/.env.")
        runner = FakeRunner(self.plain_scripts([
            block({"op": "read", "path": "apps/agent/notes.txt"}, {"op": "outline", "path": "cli/tool.py"},
                  {"op": "grep", "pattern": "line 1 TARGET", "glob": "apps/agent/big*"}),
            reply,
        ]))
        result = self.run_debate(self.engine(), runner, context_paths=["apps/agent/main.go"])
        self.assertEqual(len(runner.prompts("expert")), 2)  # no citation question
        self.assertNotIn("unverified_citations", self.expert_entry(result))
        check = self.event("citation_check")["data"]
        self.assertEqual(check, {"cited": 4, "unverified": [], "asked": False, "mentioned": 0, "short": 0})

    def test_a_deferred_or_failed_request_does_not_count_as_provided(self) -> None:
        runner = FakeRunner(self.plain_scripts([
            block({"op": "read", "path": "apps/agent/notes.txt"}, {"op": "read", "path": "cli/tool.py"}),
            "Based on cli/tool.py:3 and apps/agent/notes.txt:2.",
            "Based on apps/agent/notes.txt:2 only.",
        ]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_REQUESTS": "1"}):
            self.run_debate(self.engine(), runner)
        self.assertIn("never given: cli/tool.py", runner.prompts("expert")[2])

    def test_earlier_runs_of_a_resumed_thread_count_as_provided(self) -> None:
        engine = self.engine()
        first = self.run_debate(engine, FakeRunner(self.plain_scripts([block({"op": "read", "path": "cli/tool.py"}), "Done."])))
        runner = FakeRunner(self.plain_scripts(["Still relying on cli/tool.py:3."]))
        second = self.run_debate(engine, runner, thread_id=first["thread_id"])
        self.assertEqual(len(runner.prompts("expert")), 1)  # no question: this agent read it in the first run
        self.assertNotIn("unverified_citations", second["transcript"][3])
        self.assertEqual(second["context"]["provided_files"]["Domain Expert"], ["cli/tool.py"])

    def test_citation_check_needs_no_step_budget_and_is_skipped_when_exhausted(self) -> None:
        listing = block({"op": "list"})
        runner = FakeRunner(self.plain_scripts([listing, listing, "Final with cli/tool.py:3 cited."]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "1"}):
            result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(entry["unverified_citations"], ["cli/tool.py"])  # no steps left to ask
        self.assertEqual(len(runner.prompts("expert")), 3)
        self.assertFalse(self.event("citation_check")["data"]["asked"])

    def test_builtin_fallback_text_is_not_citation_checked(self) -> None:
        runner = FakeRunner(self.plain_scripts(["canned cli/tool.py"]), provider=BUILTIN_PROVIDER)
        self.run_debate(self.engine(), runner)
        self.assertNotIn("citation_check", [e["type"] for e in self.events()])


def add_mention_files(harness: "EngineHarness") -> None:
    """Commits a Go file that only names cli/guardian.py in a string constant, plus two Python files."""
    repo = harness.repo
    (repo / "apps/agent/handler.go").write_text('package main\n\nconst defaultCLI = "cli/guardian.py"\n')
    (repo / "cli/guardian.py").write_text("print('guardian')\n")
    (repo / "cli/other.py").write_text("print('other')\n")
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", "mention files")
    harness.sha = git(repo, "rev-parse", "HEAD")


READ_HANDLER = block({"op": "read", "path": "apps/agent/handler.go"})


class BareMentionTests(EngineHarness):
    """A bare path counts when the file or content that names it was provided; a path:N citation needs the file."""

    def setUp(self) -> None:
        super().setUp()
        add_mention_files(self)

    @staticmethod
    def expert_entry(result: Dict[str, Any]) -> Dict[str, Any]:
        return next(t for t in result["transcript"] if t["role"] == "Domain Expert")

    def event_data(self, kind: str) -> Dict[str, Any]:
        return [e for e in self.events() if e["type"] == kind][0]["data"]

    def test_path_in_a_string_constant_of_a_read_file_is_accepted_without_a_reask(self) -> None:
        runner = FakeRunner(self.plain_scripts([
            READ_HANDLER, "apps/agent/handler.go:3 sets cli/guardian.py as the default CLI path."]))
        result = self.run_debate(self.engine(), runner)
        self.assertEqual(len(runner.prompts("expert")), 2)  # results only, no citation question
        self.assertNotIn("unverified_citations", self.expert_entry(result))
        self.assertEqual(self.event_data("citation_check"),
                         {"cited": 2, "unverified": [], "asked": False, "mentioned": 1, "short": 0})

    def test_bare_path_never_seen_anywhere_is_still_flagged(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_HANDLER, "It also uses cli/other.py.", "Dropped."]))
        self.run_debate(self.engine(), runner)
        self.assertIn("never given: cli/other.py", runner.prompts("expert")[2])
        self.assertEqual(self.event_data("citation_check"),
                         {"cited": 1, "unverified": ["cli/other.py"], "asked": True, "mentioned": 0, "short": 0})

    def test_strong_citation_needs_the_file_even_when_its_path_was_mentioned(self) -> None:
        for citation in ("cli/guardian.py:10", "cli/guardian.py:10-12", "cli/guardian.py#main",
                         "cli/guardian.py line 10", "`cli/guardian.py` (L10)"):
            with self.subTest(citation=citation):
                runner = FakeRunner(self.plain_scripts([READ_HANDLER, f"See {citation}.", "Dropped."]))
                self.run_debate(self.engine(), runner)
                self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[2])

    def test_a_bare_and_a_strong_mention_of_one_path_count_as_strong(self) -> None:
        runner = FakeRunner(self.plain_scripts([READ_HANDLER, "cli/guardian.py is the default; see cli/guardian.py:10.",
                                                "Dropped."]))
        self.run_debate(self.engine(), runner)
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[2])

    def test_the_repository_file_list_does_not_count(self) -> None:
        runner = FakeRunner(self.plain_scripts(["The default is cli/guardian.py.", "Dropped."]))
        with patch.dict(os.environ, {"GUARDIAN_REPO_MAP": "0"}):
            self.run_debate(self.engine(), runner)
        self.assertIn("cli/guardian.py", runner.prompts("expert")[0])  # it is in the file list the agent got
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[1])

    def test_the_repository_map_does_not_count_either(self) -> None:
        runner = FakeRunner(self.plain_scripts(["The default is cli/guardian.py.", "Dropped."]))
        self.run_debate(self.engine(), runner)
        self.assertIn("--- Repository map", runner.prompts("expert")[0])
        self.assertIn("guardian.py", runner.prompts("expert")[0])
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[1])

    def test_a_list_result_does_not_count(self) -> None:
        runner = FakeRunner(self.plain_scripts([block({"op": "list", "path": "cli"}), "Uses cli/guardian.py.", "Dropped."]))
        self.run_debate(self.engine(), runner)
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[2])

    def test_agents_own_reply_does_not_count(self) -> None:
        # The agent writes the path in an earlier step; that text is not provided content.
        runner = FakeRunner(self.plain_scripts([
            "I believe cli/guardian.py matters.\n" + READ_NOTES, "Final: cli/guardian.py.", "Dropped."]))
        self.run_debate(self.engine(), runner)
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[2])

    def test_grep_lines_and_outline_text_count(self) -> None:
        runner = FakeRunner(self.plain_scripts([
            block({"op": "grep", "pattern": "defaultCLI", "glob": "apps/**"}), "Default is cli/guardian.py."]))
        self.run_debate(self.engine(), runner)
        self.assertEqual(len(runner.prompts("expert")), 2)
        self.assertEqual(self.event_data("citation_check")["mentioned"], 1)

    def test_context_pack_file_text_counts_but_its_file_list_does_not(self) -> None:
        runner = FakeRunner(self.plain_scripts(["Default is cli/guardian.py."]))
        self.run_debate(self.engine(), runner, context_paths=["apps/agent/handler.go"])
        self.assertEqual(len(runner.prompts("expert")), 1)
        self.assertEqual(self.event_data("citation_check")["mentioned"], 1)
        # A pack of a file that does not name it lists it in the repository file list only.
        runner = FakeRunner(self.plain_scripts(["Default is cli/guardian.py.", "Dropped."]))
        self.run_debate(self.engine(), runner, context_paths=["apps/agent/notes.txt"])
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[1])

    def test_mentions_are_persisted_with_the_thread_and_bounded(self) -> None:
        first = self.run_debate(self.engine(), FakeRunner(self.plain_scripts([READ_HANDLER, "Done."])))
        self.assertIn("cli/guardian.py", first["context"]["mentioned_paths"]["Domain Expert"])
        runner = FakeRunner(self.plain_scripts(["The CLI default is cli/guardian.py."]))
        second = self.run_debate(self.engine(), runner, thread_id=first["thread_id"])
        self.assertEqual(len(runner.prompts("expert")), 1)  # nothing read in this run, still accepted
        self.assertNotIn("unverified_citations", second["transcript"][3])
        self.assertEqual(self.event_data("citation_check")["mentioned"], 1)
        self.assertIn("cli/guardian.py", second["context"]["mentioned_paths"]["Domain Expert"])

    def test_cited_refs_strength(self) -> None:
        tools = RepoTools(self.repo, self.sha)
        refs = tools.cited_refs(
            "a cli/tool.py:3, cli/guardian.py#Run and apps/agent/main.go line 4; bare cli/other.py. "
            "And apps/agent/notes.txt (L2) next to `apps/agent/handler.go`.")
        self.assertEqual(refs, {"cli/tool.py": True, "cli/guardian.py": True, "apps/agent/main.go": True,
                                "cli/other.py": False, "apps/agent/notes.txt": True, "apps/agent/handler.go": False})
        self.assertEqual(tools.cited_paths("cli/tool.py:3 and x"), ["cli/tool.py"])


DECISION_JSON = (
    '```json\n{"decision": "Use the retry wrapper.", "constraints": ["Retries are capped at 3."], '
    '"checks": [], "actions": []}\n```'
)
APPROVED_ANSWER = "The claim about cli/tool.py:3 holds.\n" + DECISION_JSON + "\nVERDICT: APPROVED"
PSEUDO_MARKUP = (
    '<function_calls>\n<invoke name="glob">\n<parameter name="path">/home/ubuntu/.guardian</parameter>\n</invoke>\n'
    '<invoke name="ls">\n<parameter name="path">/home/ubuntu</parameter>\n</invoke>\n'
    '<invoke name="glob">\n<parameter name="path">/home/ubuntu/.guardian/data</parameter>\n</invoke>\n</function_calls>'
)
PSEUDO_REPLY = "Let me check the current state of the analysis documents.\n\n" + PSEUDO_MARKUP + "\n\nInvented analysis."


class FormatGuardTests(EngineHarness):
    def expert_entry(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return result["transcript"][0]

    def event(self, type_: str, nth: int = 0) -> Dict[str, Any]:
        return [e for e in self.events() if e["type"] == type_][nth]

    def judge_entry(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return result["transcript"][2]

    def run_judge(self, judge: List[Script], expert: Optional[List[Script]] = None):
        runner = FakeRunner({"expert": expert or ["plan"], "critic": ["critique"], "judge": judge})
        return runner, self.run_debate(self.engine(), runner)

    # --- A: citation re-ask keeps the output contract -----------------------------------------------

    def test_citation_reask_restates_the_judge_contract_and_forbids_meta_commentary(self) -> None:
        runner, _ = self.run_judge([APPROVED_ANSWER, APPROVED_ANSWER.replace("cli/tool.py:3", "the tool")])
        ask = runner.prompts("judge")[1]
        self.assertIn("CITATION CHECK", ask)
        self.assertIn("COMPLETE final answer", ask)
        self.assertIn(JUDGE_VERDICT_RULE, ask)
        self.assertIn(JUDGE_JSON_EXAMPLE, ask)
        self.assertIn("Do not mention this correction", ask)
        # The restated contract is the one in the judge prompt itself.
        self.assertIn(JUDGE_VERDICT_RULE, runner.prompts("judge")[0])
        self.assertIn(JUDGE_JSON_EXAMPLE, runner.prompts("judge")[0])

    def test_expert_citation_reask_has_no_judge_contract(self) -> None:
        runner, _ = self.run_judge(["x\nVERDICT: REJECTED"], expert=["See cli/tool.py:3.", "Revised."])
        ask = runner.prompts("expert")[1]
        self.assertIn("COMPLETE final answer", ask)
        self.assertNotIn("VERDICT", ask)

    def test_judge_losing_its_verdict_in_the_reask_keeps_the_original_answer(self) -> None:
        rewritten = "The claim about `cli/tool.py` ... I'm removing the file path citation.\nRewritten analysis."
        runner, result = self.run_judge([APPROVED_ANSWER, rewritten])
        entry = self.judge_entry(result)
        self.assertEqual(entry["content"], APPROVED_ANSWER)
        self.assertTrue(entry["consensus"])
        self.assertEqual(entry["unverified_citations"], ["cli/tool.py"])
        self.assertEqual(len(runner.prompts("judge")), 2)  # the original had everything: no format retry
        self.assertNotIn("format_retry", [e["type"] for e in self.events()])

    def test_a_reask_that_keeps_the_contract_replaces_the_answer(self) -> None:
        better = APPROVED_ANSWER.replace("cli/tool.py:3", "the tool")
        _, result = self.run_judge([APPROVED_ANSWER, better])
        entry = self.judge_entry(result)
        self.assertEqual(entry["content"], better)
        self.assertNotIn("unverified_citations", entry)

    def test_reask_that_loses_the_verdict_is_recovered_by_a_format_retry_when_the_original_lacked_it_too(self) -> None:
        # Both answers lack a verdict: the newer one is kept, then the format retry supplies it.
        runner, result = self.run_judge(["Cites cli/tool.py:3. No verdict.", "Rewritten without the citation.",
                                         "VERDICT: REJECTED"])
        entry = self.judge_entry(result)
        self.assertEqual(entry["content"], "Rewritten without the citation.\n\nVERDICT: REJECTED")
        self.assertEqual(self.event("format_retry")["data"], {"missing": ["verdict"]})

    # --- B: judge verdict guard ---------------------------------------------------------------------

    def test_missing_verdict_is_asked_for_once_and_joined_to_the_analysis(self) -> None:
        runner, result = self.run_judge(["Long analysis without any verdict.", DECISION_JSON + "\nVERDICT: APPROVED"])
        entry = self.judge_entry(result)
        self.assertEqual(entry["content"], "Long analysis without any verdict.\n\n" + DECISION_JSON + "\nVERDICT: APPROVED")
        self.assertTrue(entry["consensus"])
        ask = runner.prompts("judge")[1]
        self.assertIn("FORMAT CHECK", ask)
        self.assertIn(JUDGE_VERDICT_RULE, ask)
        self.assertIn("Do not repeat your analysis", ask)
        events = [e for e in self.events() if e["type"] == "format_retry"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"], {"missing": ["verdict"]})
        self.assertEqual(events[0]["role"], "JEV-MCP Judge")

    def test_approved_without_json_asks_for_the_json_block(self) -> None:
        runner, result = self.run_judge(["Looks fine.\nVERDICT: APPROVED", DECISION_JSON])
        entry = self.judge_entry(result)
        self.assertIn(DECISION_JSON, entry["content"])
        self.assertTrue(entry["consensus"])
        self.assertEqual(self.event("format_retry")["data"], {"missing": ["json"]})
        self.assertIn("fenced json decision block", runner.prompts("judge")[1])

    def test_rejected_needs_no_json_and_no_retry(self) -> None:
        runner, _ = self.run_judge(["Not enough.\nVERDICT: REJECTED"])
        self.assertEqual(len(runner.prompts("judge")), 1)
        self.assertNotIn("format_retry", [e["type"] for e in self.events()])

    def test_format_retry_is_asked_once_and_falls_back_to_the_original(self) -> None:
        runner, result = self.run_judge(["Analysis only.", "Still no verdict here."])
        entry = self.judge_entry(result)
        self.assertEqual(len(runner.prompts("judge")), 2)
        self.assertEqual(entry["content"], "Analysis only.")
        self.assertFalse(entry["consensus"])
        self.assertEqual(len([e for e in self.events() if e["type"] == "format_retry"]), 1)

    def test_format_retry_needs_no_step_budget(self) -> None:
        listing = block({"op": "list"})
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "1"}):
            runner, result = self.run_judge([listing, listing, "Analysis only.", "VERDICT: REJECTED"])
        self.assertTrue(self.judge_entry(result)["content"].endswith("VERDICT: REJECTED"))

    def test_other_roles_get_no_format_retry(self) -> None:
        runner, _ = self.run_judge(["x\nVERDICT: REJECTED"], expert=["no verdict needed"])
        self.assertEqual(len(runner.prompts("expert")), 1)

    def test_judge_missing_parts(self) -> None:
        self.assertEqual(judge_missing_parts("x"), ["verdict"])
        self.assertEqual(judge_missing_parts("x\nVERDICT: REJECTED"), [])
        self.assertEqual(judge_missing_parts("x\nVERDICT: APPROVED"), ["json"])
        self.assertEqual(judge_missing_parts(APPROVED_ANSWER), [])
        self.assertEqual(judge_missing_parts("**VERDICT: REJECTED**"), [])

    # --- C: pseudo tool calls -----------------------------------------------------------------------

    def test_detects_the_markup_shapes_and_ignores_quotations(self) -> None:
        kept, discarded, tools = find_pseudo_tool_calls(PSEUDO_REPLY)
        self.assertEqual(kept, "Let me check the current state of the analysis documents.")
        self.assertEqual(tools, ["glob", "ls"])
        self.assertEqual(discarded, len(PSEUDO_REPLY) - len(kept) - 2)
        self.assertEqual(find_pseudo_tool_calls('x <invoke name="Read">y')[2], ["Read"])
        self.assertEqual(find_pseudo_tool_calls("a <tool_use name='bash'>")[2], ["bash"])
        self.assertEqual(find_pseudo_tool_calls("a <function_calls>")[2], [])
        self.assertIsNone(find_pseudo_tool_calls("Models emit `<function_calls>` markup; see\n```\n<invoke name=\"x\">\n```"))
        self.assertIsNone(find_pseudo_tool_calls("plain <b>html</b> and a < b"))

    def test_pseudo_tool_call_is_stripped_and_the_model_is_told_to_use_repo_blocks(self) -> None:
        runner = FakeRunner(self.plain_scripts([PSEUDO_REPLY, "FINAL answer"]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(entry["content"], "FINAL answer")
        self.assertNotIn("pseudo_tool_calls", entry)
        ask = runner.prompts("expert")[1]
        self.assertIn("nothing was executed", ask)
        self.assertIn("```repo block", ask)
        self.assertIn("never readable", ask)
        self.assertIn("glob, ls", ask)
        data = self.event("pseudo_tool_call")["data"]
        self.assertEqual(set(data), {"step", "tools", "discarded_chars"})
        self.assertEqual((data["step"], data["tools"]), (1, ["glob", "ls"]))
        self.assertEqual(data["discarded_chars"], len(PSEUDO_REPLY) - len("Let me check the current state of the analysis documents.") - 2)
        self.assertEqual(self.event("pseudo_tool_call")["role"], "Domain Expert")
        self.assertNotIn("Invented analysis", json.dumps(self.events()))

    def test_replay_after_provider_change_holds_only_the_stripped_reply(self) -> None:
        runner = FakeRunner(self.plain_scripts([PSEUDO_REPLY, "FINAL"]), lose_session_for="expert")
        self.run_debate(self.engine(), runner)
        replay = [c for c in runner.calls if c["role"] == "expert" and c["session_id"] is None][-1]["prompt"]
        self.assertNotIn("Invented analysis", replay)
        self.assertNotIn("/home/ubuntu", replay)

    def test_pseudo_replies_are_accepted_stripped_after_two_in_a_row(self) -> None:
        runner = FakeRunner(self.plain_scripts([PSEUDO_REPLY, PSEUDO_REPLY, PSEUDO_REPLY, "never reached"]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(len(runner.prompts("expert")), 3)
        self.assertEqual(entry["content"], "Let me check the current state of the analysis documents.")
        self.assertEqual(entry["pseudo_tool_calls"], 3)
        self.assertEqual([e["data"]["step"] for e in self.events() if e["type"] == "pseudo_tool_call"], [1, 2, 3])

    def test_a_normal_step_resets_the_consecutive_count(self) -> None:
        runner = FakeRunner(self.plain_scripts([PSEUDO_REPLY, READ_NOTES, PSEUDO_REPLY, PSEUDO_REPLY, "FINAL"]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(entry["content"], "FINAL")
        self.assertNotIn("pseudo_tool_calls", entry)

    def test_configured_step_limit_counts_pseudo_replies(self) -> None:
        runner = FakeRunner(self.plain_scripts([PSEUDO_REPLY, PSEUDO_REPLY, "never reached"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "1"}):
            result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(len(runner.prompts("expert")), 2)  # one step used, the next pseudo reply is accepted stripped
        self.assertEqual(entry["pseudo_tool_calls"], 2)
        self.assertEqual(entry["content"], "Let me check the current state of the analysis documents.")
        self.assertEqual([e["data"]["step"] for e in self.events() if e["type"] == "pseudo_tool_call"], [1, 1])

    def test_a_repo_block_before_the_markup_is_still_served(self) -> None:
        reply = block({"op": "read", "path": "apps/agent/notes.txt"}) + "\n" + PSEUDO_MARKUP
        runner = FakeRunner(self.plain_scripts([reply, "FINAL"]))
        result = self.run_debate(self.engine(), runner)
        entry = self.expert_entry(result)
        self.assertEqual(entry["content"], "FINAL")
        self.assertEqual(entry["retrievals"][0]["op"], "read")
        self.assertIn("nothing was executed", runner.prompts("expert")[1])
        self.assertIn("REPO RESULTS", runner.prompts("expert")[1])

    def test_judge_round_with_pseudo_calls_and_no_verdict_recovers_through_the_format_retry(self) -> None:
        runner, result = self.run_judge([PSEUDO_REPLY, "Analysis from the repository only.", "VERDICT: REJECTED"])
        entry = self.judge_entry(result)
        self.assertEqual(entry["content"], "Analysis from the repository only.\n\nVERDICT: REJECTED")
        types = [e["type"] for e in self.events() if e.get("role") == "JEV-MCP Judge" and e["type"] in ("pseudo_tool_call", "format_retry")]
        self.assertEqual(types, ["pseudo_tool_call", "format_retry"])

    def test_pseudo_markup_is_detected_when_retrieval_is_off(self) -> None:
        runner = FakeRunner(self.plain_scripts([PSEUDO_REPLY, "FINAL"]))
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL": "0"}):
            result = self.run_debate(self.engine(), runner)
        self.assertEqual(self.expert_entry(result)["content"], "FINAL")
        self.assertIn("No tools and no repository access", runner.prompts("expert")[1])


class PanelAndEventTests(EngineHarness):
    def test_critic_panel_retrieves_in_parallel_without_mixing_event_lines(self) -> None:
        lineup = Lineup(id="panel", name="Panel", expert=Member("domain_architect"),
                        critics=[Member("critic_redteam"), Member("devops_specialist")], judge=Member("jev_mcp_decision"))
        barrier = threading.Barrier(2, timeout=10)

        def critic(prompt: str) -> str:
            if "REPO RESULTS" in prompt:
                return "critique after reading"
            barrier.wait()  # both critics are mid-retrieval at the same time
            return block({"op": "grep", "pattern": "TARGET", "glob": "apps/agent/n*"}, {"op": "outline", "path": "cli/tool.py"})

        runner = FakeRunner({"expert": ["proposal"], "critic": critic, "judge": ["VERDICT: REJECTED"]})
        engine = self.engine()
        with patch.object(engine, "_resolve_lineup", return_value=lineup):
            result = self.run_debate(engine, runner)
        critics = [t for t in result["transcript"] if t["role"] == "Critic Agent"]
        self.assertEqual(len(critics), 2)
        for entry in critics:
            self.assertEqual([r["op"] for r in entry["retrievals"]], ["grep", "outline"])
        self.assertEqual(result["context"]["retrieval"]["steps"], 2)
        events = self.events()
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        agents = {e["agent"] for e in events if e["type"] == "retrieval_served"}
        self.assertEqual(agents, {"Critic Agent:critic_redteam", "Critic Agent:devops_specialist"})

    def test_provider_failures_become_events(self) -> None:
        runner = FakeRunner(self.plain_scripts(["proposal"]), failures=["claude: timed out after 300s"])
        self.run_debate(self.engine(), runner)
        failed = [e for e in self.events() if e["type"] == "provider_failed"]
        self.assertEqual(len(failed), 3)  # expert, critic, judge each skipped claude
        self.assertEqual(failed[0]["data"], {"provider": "claude", "error": "timed out after 300s"})
        self.assertEqual(failed[0]["role"], "Domain Expert")

    def test_builtin_fallback_is_flagged(self) -> None:
        def builtin(prompt, context=None, session_id=None, provider=None, providers=None):
            return LLMResult(BUILTIN_PROVIDER, "canned", failures=["claude: boom"])

        engine = self.engine()
        engine.llm.last_error = "claude: boom"
        with patch.object(engine.llm, "run_session_prompt", side_effect=builtin), \
                patch.object(engine, "_save_debate_log", return_value=self.root / "l.md"):
            engine.run_debate("task", project_id="server-guardian")
        engine.events.close()
        finished = [e for e in self.events() if e["type"] == "agent_finished"]
        self.assertTrue(all(e["data"]["fallback"] for e in finished))
        self.assertEqual([e["data"] for e in self.events() if e["type"] == "verdict"],
                         [{"approved": False, "unverified": True}])
        self.assertEqual(self.events()[-1]["data"]["status"], "UNVERIFIED")

    def test_exceptions_are_reported_as_error_events(self) -> None:
        def boom(*args, **kwargs):
            raise RuntimeError("provider exploded")

        engine = self.engine()
        with patch.object(engine.llm, "run_session_prompt", side_effect=boom):
            with self.assertRaises(RuntimeError):
                engine.run_debate("task", project_id="server-guardian")
        engine.events.close()
        last = self.events()[-1]
        self.assertEqual(last["type"], "error")
        self.assertIn("provider exploded", last["data"]["message"])

    def test_engine_without_a_events_file_writes_nothing(self) -> None:
        engine = DebateEngine(max_rounds=1, db_path=self.root / "d.db")
        engine.mcp = MCPClient(workspace=self.repo)
        runner = FakeRunner(self.plain_scripts(["proposal"]))
        with patch.object(engine.llm, "run_session_prompt", side_effect=runner), \
                patch.object(engine, "_save_debate_log", return_value=self.root / "l.md"):
            engine.run_debate("task", project_id="server-guardian")
        self.assertFalse(self.events_path.exists())


class EventWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_no_file_is_a_no_op(self) -> None:
        writer = EventWriter(None, "t")
        writer.emit("round_started", round=1)
        writer.close()
        self.assertFalse(writer.enabled)
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_line_shape_and_flush_per_line(self) -> None:
        writer = EventWriter(str(self.path), "thread_x")
        writer.emit("round_started", round=2)
        # Flushed already: readable before close.
        first = self.read()[0]
        self.assertEqual(set(first), {"seq", "ts", "thread_id", "type", "round", "data"})
        self.assertEqual((first["seq"], first["thread_id"], first["type"], first["round"], first["data"]),
                         (1, "thread_x", "round_started", 2, {}))
        self.assertRegex(first["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        writer.emit("agent_started", {"resumed": False, "prompt_bytes": 5}, round=2, role="Domain Expert",
                    agent="Domain Expert", agent_name="Architect")
        second = self.read()[1]
        self.assertEqual((second["seq"], second["role"], second["agent"], second["agent_name"]),
                         (2, "Domain Expert", "Domain Expert", "Architect"))
        writer.close()

    def test_unicode_is_kept(self) -> None:
        writer = EventWriter(str(self.path), "t")
        writer.emit("error", {"message": "토론 실패"})
        writer.close()
        self.assertIn("토론 실패", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.read()[0]["data"]["message"], "토론 실패")

    def test_size_cap_writes_one_truncation_marker_then_stops(self) -> None:
        writer = EventWriter(str(self.path), "t", max_bytes=1500)
        for i in range(50):
            writer.emit("agent_finished", {"excerpt": "x" * 100, "i": i})
        writer.close()
        events = self.read()
        self.assertEqual(events[-1]["type"], "events_truncated")
        self.assertEqual([e["type"] for e in events].count("events_truncated"), 1)
        self.assertLess(len(events), 50)
        self.assertLess(self.path.stat().st_size, 1500 + 200)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(isinstance(events[-1]["seq"], int))

    def test_concurrent_writers_never_interleave_lines(self) -> None:
        writer = EventWriter(str(self.path), "t")

        def work(n: int) -> None:
            for i in range(100):
                writer.emit("retrieval_served", {"worker": n, "i": i, "pad": "y" * 500}, agent=f"a{n}")

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        writer.close()
        events = self.read()  # every line parses
        self.assertEqual(len(events), 800)
        self.assertEqual(sorted(e["seq"] for e in events), list(range(1, 801)))
        self.assertEqual([e["seq"] for e in events], list(range(1, 801)))  # file order equals seq order

    def test_prompt_is_saved_beside_events_not_in_them(self) -> None:
        writer = EventWriter(str(self.path), "t")
        writer.emit("round_started", round=1)
        writer.emit("agent_started", {"resumed": False, "prompt_bytes": 9}, agent="a", prompt="프롬프트 본문")
        writer.close()
        started = self.read()[1]
        self.assertEqual(started["data"], {"resumed": False, "prompt_bytes": 9, "prompt_saved": True})
        self.assertNotIn("프롬프트 본문", self.path.read_text(encoding="utf-8"))
        saved = Path(self._tmp.name) / "prompts" / "2.txt"
        self.assertEqual(saved.read_text(encoding="utf-8"), "프롬프트 본문")

    def test_prompt_caps(self) -> None:
        with patch("debate_events.MAX_PROMPT_BYTES", 4), patch("debate_events.MAX_PROMPTS_BYTES", 6):
            writer = EventWriter(str(self.path), "t")
            writer.emit("agent_started", {}, prompt="가나다")  # 9 bytes: cut on a character boundary
            writer.emit("agent_started", {}, prompt="abcdef")  # 3 + 4 > 6: not saved
            writer.close()
        first, second = self.read()
        self.assertEqual(first["data"], {"prompt_saved": True, "prompt_truncated": True})
        self.assertEqual((Path(self._tmp.name) / "prompts" / "1.txt").read_text(encoding="utf-8"), "가")
        self.assertEqual(second["data"], {"prompt_saved": False})
        self.assertFalse((Path(self._tmp.name) / "prompts" / "2.txt").exists())

    def test_emit_does_not_mutate_caller_data(self) -> None:
        writer = EventWriter(str(self.path), "t")
        data = {"resumed": True}
        writer.emit("agent_started", data, prompt="p")
        writer.close()
        self.assertEqual(data, {"resumed": True})

    def test_unwritable_path_never_raises(self) -> None:
        writer = EventWriter(str(Path(self._tmp.name) / "missing-dir" / "events.jsonl"), "t")
        writer.emit("round_started", round=1)
        writer.emit("round_started", round=2)  # still fine after the first failure


class SymbolOpCitationTests(EngineHarness):
    """defs gives locations only, refs shows lines like grep (docs/design/debate-code-index-wiki.md)."""

    def run_with_data_dir(self, runner: FakeRunner) -> Dict[str, Any]:
        with patch.dict(os.environ, {"GUARDIAN_DATA_DIR": str(self.root / "data")}):
            return self.run_debate(self.engine(), runner)

    def test_citation_after_only_a_defs_result_is_unverified(self) -> None:
        defs = block({"op": "defs", "symbol": "run"})
        runner = FakeRunner(self.plain_scripts([defs, "The runner is cli/tool.py:4.", "Dropped."]))
        result = self.run_with_data_dir(runner)
        prompts = runner.prompts("expert")
        self.assertIn("cli/tool.py:4-5 function def run():", prompts[1])  # the agent did get the location
        self.assertIn("never given: cli/tool.py", prompts[2])
        self.assertEqual(self.event_data("citation_check")["unverified"], ["cli/tool.py"])
        self.assertEqual([r["op"] for r in result["transcript"][0]["retrievals"]], ["defs"])

    def test_a_bare_path_in_defs_text_is_no_mention_either(self) -> None:
        runner = FakeRunner(self.plain_scripts([block({"op": "defs", "symbol": "run"}), "Look at cli/tool.py:4.", "x"]))
        self.run_with_data_dir(runner)
        self.assertEqual(self.event_data("citation_check")["mentioned"], 0)

    def test_citation_after_a_refs_result_is_verified(self) -> None:
        refs = block({"op": "refs", "symbol": "run", "include_defs": True})
        runner = FakeRunner(self.plain_scripts([refs, "The runner is cli/tool.py:4."]))
        self.run_with_data_dir(runner)
        self.assertEqual(len(runner.prompts("expert")), 2)
        self.assertEqual(self.event_data("citation_check")["unverified"], [])

    def event_data(self, type_: str) -> Dict[str, Any]:
        return [e for e in self.events() if e["type"] == type_][0]["data"]


if __name__ == "__main__":
    unittest.main()

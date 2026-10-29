"""Engine side of the repo MCP tools: events, steps, provided files and judge tracking from the server log."""
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from llm_runner import LLMResult
from repo_mcp import RepoMcpServer
from repo_tools import RepoTools
from team_manager import Lineup, Member
from test_retrieval_loop import READ_NOTES, EngineHarness, FakeRunner, add_mention_files
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # Keep the symbol index cache (and anything else) out of the real data directory.
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


Call = Tuple[str, Dict[str, Any]]


class McpRunner(FakeRunner):
    """FakeRunner that, like claude with the repo MCP server attached, makes tool calls during a call.

    calls maps a role to the tool calls made in its first call; they run through the real RepoMcpServer,
    so the log has exactly the records a server would write.
    """

    def __init__(self, scripts: Dict[str, Any], calls: Optional[Dict[str, List[Call]]] = None,
                 provider: str = "claude-cli") -> None:
        super().__init__(scripts, provider=provider)
        self.mcp_calls = calls or {}
        self.specs: List[Optional[Dict[str, Any]]] = []

    def __call__(self, prompt, context=None, session_id=None, provider=None, providers=None, repo_mcp=None, **kw) -> LLMResult:
        self.specs.append(repo_mcp)
        role = self.role_of(prompt, session_id)
        if repo_mcp and self.provider == "claude-cli" and not session_id:
            limits = repo_mcp["limits"]
            server = RepoMcpServer(RepoTools(Path(repo_mcp["workspace"]), repo_mcp["sha"]), repo_mcp["log"],
                                   max_steps=limits["steps"], max_step_bytes=limits["step_bytes"],
                                   max_turn_bytes=limits["turn_bytes"])
            for name, arguments in self.mcp_calls.get(role, []):
                server.call_tool(name, arguments)
        return super().__call__(prompt, context, session_id, provider, providers, **kw)


class McpEngineTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        self._env = patch.dict(os.environ, {"GUARDIAN_REPO_MCP": "1"})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        super().tearDown()

    def run_mcp(self, runner: McpRunner, expert="mcp", critic=None, judge=None, engine=None) -> Dict[str, Any]:
        lineup = Lineup(id="t", name="T", expert=Member("domain_architect", ["claude"], expert),
                        critics=[Member("critic_redteam", ["claude"], critic)],
                        judge=Member("jev_mcp_decision", ["claude"], judge))
        engine = engine or self.engine()
        with patch.object(engine, "_resolve_lineup", return_value=lineup):
            return self.run_debate(engine, runner)

    def of(self, kind: str, role: str = "Domain Expert") -> List[Dict[str, Any]]:
        return [e for e in self.events() if e["type"] == kind and e.get("role") == role]

    def test_tool_calls_become_one_step_each_with_via_mcp(self) -> None:
        runner = McpRunner(self.plain_scripts(["FINAL see apps/agent/notes.txt:2"]), {"expert": [
            ("repo_list", {"path": "apps/agent"}),
            ("repo_read", {"path": "apps/agent/notes.txt"}),
            ("repo_read", {"path": "../outside.txt"}),
        ]})
        result = self.run_mcp(runner)
        self.assertIsNotNone(runner.specs[0])
        self.assertEqual(runner.specs[0]["sha"], self.sha)
        requested, served = self.of("retrieval_requested"), self.of("retrieval_served")
        self.assertEqual([e["data"]["step"] for e in requested], [1, 2, 3])
        self.assertEqual([e["data"]["step"] for e in served], [1, 2, 3])
        self.assertTrue(all(e["data"]["via"] == "mcp" for e in requested + served))
        self.assertEqual(requested[1]["data"]["requests"], [{"op": "read", "target": "apps/agent/notes.txt"}])
        self.assertEqual([len(e["data"]["results"]) for e in served], [1, 1, 1])
        self.assertEqual([e["data"]["results"][0]["ok"] for e in served], [True, True, False])
        self.assertEqual(served[2]["data"]["results"][0]["error"], "invalid path")
        self.assertEqual(served[2]["data"]["turn_bytes"], sum(e["data"]["results"][0]["bytes"] for e in served))
        self.assertEqual(self.of("agent_finished")[0]["data"]["steps"], 3)
        entry = next(t for t in result["transcript"] if t["role"] == "Domain Expert")
        self.assertEqual([r["step"] for r in entry["retrievals"]], [1, 2, 3])
        self.assertEqual(entry["retrievals"][1]["paths"], ["apps/agent/notes.txt"])
        self.assertEqual(result["context"]["retrieval"]["steps"], 3)
        self.assertGreater(result["context"]["retrieval"]["bytes"], 0)
        # Cited and read through MCP, so no citation problem.
        self.assertNotIn("unverified_citations", entry)
        self.assertEqual(self.of("citation_check")[0]["data"]["unverified"], [])
        self.assertIn("apps/agent/notes.txt", result["context"]["provided_files"]["Domain Expert"])
        self.assertNotIn("TARGET gamma", "\n".join(str(e) for e in self.events()))  # no contents in events

    def test_uncited_mcp_read_is_not_enough_for_other_files(self) -> None:
        runner = McpRunner(self.plain_scripts(["FINAL cites cli/tool.py"]), {"expert": [("repo_read", {"path": "apps/agent/notes.txt"})]})
        self.run_mcp(runner)
        self.assertEqual(self.of("citation_check")[0]["data"]["unverified"], ["cli/tool.py"])

    def test_path_named_inside_an_mcp_read_backs_a_bare_mention_only(self) -> None:
        # The server logs the tree paths named in each result (no contents); the engine accepts bare mentions of them.
        add_mention_files(self)
        runner = McpRunner(self.plain_scripts(["handler sets cli/guardian.py as the default."]),
                           {"expert": [("repo_read", {"path": "apps/agent/handler.go"})]})
        result = self.run_mcp(runner)
        self.assertEqual(len(runner.prompts("expert")), 1)  # no citation question
        self.assertEqual(self.of("citation_check")[0]["data"],
                         {"cited": 1, "unverified": [], "asked": False, "mentioned": 1, "short": 0})
        self.assertIn("cli/guardian.py", result["context"]["mentioned_paths"]["Domain Expert"])
        self.assertNotIn("cli/guardian.py", result["context"]["provided_files"]["Domain Expert"])

        # A strong citation of the same file still needs the file; so does a path the read never named.
        for answer, wanted in (("see cli/guardian.py:10", "cli/guardian.py"), ("also cli/other.py", "cli/other.py")):
            runner = McpRunner(self.plain_scripts([answer, "Dropped."]),
                               {"expert": [("repo_read", {"path": "apps/agent/handler.go"})]})
            self.run_mcp(runner)
            self.assertIn(f"never given: {wanted}", runner.prompts("expert")[1])

    def test_mcp_list_result_does_not_back_a_mention(self) -> None:
        add_mention_files(self)
        runner = McpRunner(self.plain_scripts(["uses cli/guardian.py", "Dropped."]),
                           {"expert": [("repo_list", {"path": "cli"})]})
        self.run_mcp(runner)
        self.assertIn("never given: cli/guardian.py", runner.prompts("expert")[1])

    def test_members_without_mcp_get_no_spec_or_prompt_section(self) -> None:
        for env, tools in (({"GUARDIAN_REPO_MCP": "0"}, "mcp"), ({"GUARDIAN_REPO_MCP": "1"}, None)):
            with patch.dict(os.environ, env):
                runner = McpRunner(self.plain_scripts(["FINAL"]))
                self.run_mcp(runner, expert=tools, critic=None, judge=None)
            self.assertTrue(all(s is None for s in runner.specs), (env, tools))
            self.assertFalse(any("mcp__repo__" in c["prompt"] + (c["instructions"].text() if c["instructions"] else "")
                             for c in runner.calls))
            self.assertFalse([e for e in self.events() if e.get("data", {}).get("via") == "mcp"])
        runner = McpRunner(self.plain_scripts(["FINAL"]))
        self.run_mcp(runner)
        # The MCP section is standing text: it is in the expert's system prompt, and the other roles' have none.
        self.assertEqual([c["role"] for c in runner.calls
                          if c["instructions"] and "mcp__repo__repo_read" in c["instructions"].text()], ["expert"])
        self.assertFalse(any("mcp__repo__repo_read" in c["prompt"] for c in runner.calls))
        self.assertIn("repo block protocol", runner.system_texts("expert")[0])  # blocks stay the fallback

    def test_other_provider_silently_uses_repo_blocks(self) -> None:
        # The runner got a spec but codex answered: no server ran, so no MCP events; blocks work as usual.
        runner = McpRunner(self.plain_scripts([READ_NOTES, "FINAL"]), {"expert": [("repo_list", {})]}, provider="codex-cli")
        self.run_mcp(runner)
        served = self.of("retrieval_served")
        self.assertEqual([e["data"]["via"] for e in served], ["block"])
        self.assertEqual([e["type"] for e in self.of("provider_failed")], [])

    def test_block_fallback_continues_the_step_numbering(self) -> None:
        runner = McpRunner(self.plain_scripts([READ_NOTES, "FINAL"]), {"expert": [("repo_list", {}), ("repo_list", {"path": "cli"})]})
        self.run_mcp(runner)
        served = self.of("retrieval_served")
        self.assertEqual([(e["data"]["step"], e["data"]["via"]) for e in served], [(1, "mcp"), (2, "mcp"), (3, "block")])
        # The block continuation is a session continuation call and carries no new MCP server run.
        self.assertEqual(self.of("agent_finished")[0]["data"]["steps"], 3)

    def test_judge_mcp_reads_count_as_seen_but_other_roles_do_not(self) -> None:
        runner = McpRunner(self.plain_scripts(["FINAL"], ["VERDICT: REJECTED"]), {
            "judge": [("repo_read", {"path": "apps/agent/notes.txt"}), ("repo_grep", {"pattern": "TARGET"}),
                      ("repo_read", {"path": "nope.txt"})],
            "expert": [("repo_read", {"path": "cli/tool.py"})],
        })
        engine = self.engine()
        self.run_mcp(runner, expert="mcp", judge="mcp", engine=engine)
        pack = engine._pack
        self.assertIn("apps/agent/notes.txt", pack.whole)
        self.assertNotIn("cli/tool.py", pack.whole)  # the expert's read is not the judge's
        self.assertEqual(set(pack.visible), {"apps/agent/notes.txt"})  # grep and failed reads show no code

    def test_configured_limits_are_enforced_by_the_server(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEPS": "2"}):
            runner = McpRunner(self.plain_scripts(["FINAL"]), {"expert": [("repo_list", {})] * 3})
            self.run_mcp(runner)
        self.assertEqual(runner.specs[0]["limits"]["steps"], 2)
        served = self.of("retrieval_served")
        self.assertEqual([e["data"]["results"][0]["error"] for e in served], [None, None, "budget"])
        self.assertEqual(len(self.of("retrieval_exhausted")), 1)

    def test_deferred_tool_result_is_flagged(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEP_BYTES": "500"}):
            runner = McpRunner(self.plain_scripts(["FINAL"]), {"expert": [("repo_read", {"path": "apps/agent/big.txt"})]})
            result = self.run_mcp(runner)
        entry = self.of("retrieval_served")[0]["data"]["results"][0]
        self.assertEqual((entry["ok"], entry["error"], entry["deferred"], entry["bytes"]), (False, "deferred", True, 0))
        record = next(t for t in result["transcript"] if t["role"] == "Domain Expert")["retrievals"][0]
        self.assertTrue(record["deferred"])


if __name__ == "__main__":
    unittest.main()

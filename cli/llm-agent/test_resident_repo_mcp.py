"""The resident repo MCP server of one agent (docs/design/resident-agents.md, M2e).

The daemon side is the same tiny unix-socket server as in test_mailbox_mcp.py: it records each
one-line request and answers from a settable reply. No network, no ssh, no real CLI.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from mcp_stdio import PROTOCOL_VERSION
import repo_mcp
from repo_mcp import SNAPSHOT_TOOLS
from resident_repo_mcp import SERVER_KEY, ResidentRepoMcpServer, allowed_tool_names
from test_mailbox_mcp import FakeDaemon

TOOL_ORDER = tuple(SNAPSHOT_TOOLS)  # repo_list, repo_grep, repo_read, repo_outline, repo_map

A_RESULT = {"isError": False, "content": [{"type": "text", "text": "1| alpha\n2| beta TARGET gamma"}]}


class RepoMcpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="sgrr", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.daemon = FakeDaemon(self.dir / "e1.sock")
        self.addCleanup(self.daemon.close)
        self.daemon.reply = {"ok": True, "result": A_RESULT}
        self.server = ResidentRepoMcpServer(str(self.daemon.path), timeout=10.0)

    def call(self, name: str, **arguments: Any) -> Dict[str, Any]:
        response = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
        assert response is not None
        return response["result"]

    def text(self, result: Dict[str, Any]) -> str:
        return result["content"][0]["text"]


class ProtocolTests(RepoMcpCase):
    def test_handshake_and_tools_list_each_require_a_task(self) -> None:
        init = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}})
        assert init is not None
        self.assertEqual(init["result"]["serverInfo"], {"name": "guardian-resident-repo", "version": "0.1.0"})
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listing = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listing is not None
        tools = listing["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(TOOL_ORDER))
        by_name = {tool["name"]: tool for tool in tools}
        for name, tool in by_name.items():
            schema = tool["inputSchema"]
            self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False))
            self.assertIn("task", schema["required"])
            self.assertIn("task", schema["properties"])
            self.assertEqual(tool["description"],
                             "Read-only view of the repository snapshot of a task you own; pass the "
                             "task id from task_claim.")
        self.assertEqual(by_name["repo_read"]["inputSchema"]["required"], ["path", "task"])
        self.assertEqual(by_name["repo_grep"]["inputSchema"]["required"], ["pattern", "task"])
        self.assertEqual(allowed_tool_names(), [f"mcp__{SERVER_KEY}__{name}" for name in TOOL_ORDER])
        self.assertEqual(len(allowed_tool_names()), 5)
        # the schemas are deep copies: adding task must not touch repo_mcp's own tools
        self.assertNotIn("task", repo_mcp.TOOLS["repo_read"]["inputSchema"]["properties"])

    def test_unknown_tools_and_methods(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "repo_defs", "arguments": {"task": "k-1"}}})
        assert response is not None
        self.assertEqual(response["error"]["code"], -32602)  # the symbol tools are not resident
        missing = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
        assert missing is not None
        self.assertEqual(missing["error"]["code"], -32601)


class ToolCallTests(RepoMcpCase):
    def test_a_call_is_forwarded_with_the_task_id_and_the_result_passes_through(self) -> None:
        result = self.call("repo_read", task="k-1", path="apps/agent/notes.txt")
        self.assertEqual(result, A_RESULT)  # exactly what repo_mcp would return
        self.assertEqual(self.daemon.requests,
                         [{"tool": "repo_read", "args": {"path": "apps/agent/notes.txt", "task": "k-1"}}])

    def test_every_tool_round_trips_without_an_operation_id(self) -> None:
        for name in TOOL_ORDER:
            self.assertFalse(self.call(name, task="k-1")["isError"], name)
        for request in self.daemon.requests:
            self.assertNotIn("op_id", request)  # answered on the node, like mail_inbox

    def test_a_missing_task_argument_never_touches_the_socket(self) -> None:
        result = self.call("repo_read", path="apps/agent/notes.txt")
        self.assertTrue(result["isError"])
        self.assertTrue(self.text(result).startswith("error: "))
        self.assertEqual(self.daemon.requests, [])

    def test_an_error_answer_becomes_the_error_text(self) -> None:
        self.daemon.reply = {"ok": False, "error": "snapshot_pending"}
        self.assertEqual(self.text(self.call("repo_read", task="k-1", path="x")), "snapshot_pending")
        self.daemon.reply = {"ok": False, "error": "no_task", "detail": "k-9"}
        self.assertEqual(self.text(self.call("repo_read", task="k-9", path="x")), "no_task: k-9")

    def test_a_missing_socket_is_an_error(self) -> None:
        server = ResidentRepoMcpServer(str(self.dir / "nowhere.sock"), timeout=2.0)
        response = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                  "params": {"name": "repo_list", "arguments": {"task": "k-1"}}})
        assert response is not None
        self.assertTrue(response["result"]["isError"])
        self.assertIn("repository tools not available", self.text(response["result"]))

    def test_an_ok_answer_without_a_result_is_an_internal_error(self) -> None:
        self.daemon.reply = {"ok": True}
        result = self.call("repo_read", task="k-1", path="x")
        self.assertTrue(result["isError"])
        self.assertIn("internal error", self.text(result))


if __name__ == "__main__":
    unittest.main()

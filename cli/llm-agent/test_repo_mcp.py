import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

from mcp_stdio import PROTOCOL_VERSION, StdioMCPClient
from repo_mcp import RepoMcpServer, allowed_tool_names, opencode_tool_names
from repo_tools import RepoTools
from test_repo_tools import git, make_repo

SERVER = str(Path(__file__).resolve().parent / "repo_mcp.py")


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.log = self.root / "calls.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def server(self, **limits: Optional[int]) -> RepoMcpServer:
        return RepoMcpServer(RepoTools(self.repo, self.sha), str(self.log), **limits)

    def call(self, server: RepoMcpServer, name: str, **arguments: Any) -> Dict[str, Any]:
        response = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
        assert response is not None and response["id"] == 7
        return response["result"]

    def records(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class ProtocolTests(ServerCase):
    def test_handshake_and_tools_list(self) -> None:
        server = self.server()
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}})
        self.assertEqual(init["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["repo_list", "repo_grep", "repo_read", "repo_outline", "repo_defs", "repo_refs", "repo_map",
                                                     "repo_callers", "repo_callees", "repo_wiki"])
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(next(t for t in tools if t["name"] == "repo_grep")["inputSchema"]["required"], ["pattern"])
        self.assertEqual(allowed_tool_names()[0], "mcp__repo__repo_list")
        self.assertEqual(opencode_tool_names()[0], "repo_repo_list")  # opencode prefixes one name only

    def test_unknown_method_tool_and_bad_json(self) -> None:
        server = self.server()
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)
        bad = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "rm", "arguments": {}}})
        self.assertEqual(bad["error"]["code"], -32602)
        out = io.StringIO()
        server.serve(io.StringIO("not json\n" + json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}) + "\n"), out)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})

    def test_real_subprocess_with_stdio_client(self) -> None:
        argv = [sys.executable, SERVER, "--workspace", str(self.repo), "--sha", self.sha, "--log", str(self.log)]
        with StdioMCPClient(argv[0], argv[1:], env={**os.environ}) as client:
            names = [t["name"] for t in client.list_tools()]
            self.assertEqual(names, ["repo_list", "repo_grep", "repo_read", "repo_outline", "repo_defs", "repo_refs", "repo_map",
                                                     "repo_callers", "repo_callees", "repo_wiki"])
            text = client.call_tool("repo_read", {"path": "apps/agent/notes.txt"})
            self.assertIn("2| beta TARGET gamma", text)
        self.assertEqual(self.records()[0]["paths"], ["apps/agent/notes.txt"])

    def test_bad_workspace_exits_with_error(self) -> None:
        proc = subprocess.run([sys.executable, SERVER, "--workspace", str(self.root), "--sha", "0" * 40],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot open", proc.stderr)


class ToolTests(ServerCase):
    def test_each_tool(self) -> None:
        server = self.server()
        listing = self.call(server, "repo_list", path="apps/agent", depth=1)
        self.assertFalse(listing["isError"])
        self.assertIn("apps/agent/main.go", listing["content"][0]["text"])
        grep = self.call(server, "repo_grep", pattern="SERVE_BODY", glob="apps/agent/*.go")
        self.assertIn("apps/agent/main.go:", grep["content"][0]["text"])
        read = self.call(server, "repo_read", path="apps/agent/main.go#Serve")
        self.assertIn("SERVE_BODY", read["content"][0]["text"])
        self.assertNotIn("OTHER_BODY", read["content"][0]["text"].split("[lines")[-1])
        outline = self.call(server, "repo_outline", path="cli/tool.py")
        self.assertIn("def run", outline["content"][0]["text"])
        self.assertEqual([r["op"] for r in self.records()], ["list", "grep", "read", "outline"])

    def test_traversal_absolute_and_secret_paths_are_rejected(self) -> None:
        server = self.server()
        outside = self.root / "outside.txt"
        for path in ("../outside.txt", "apps/../../outside.txt", "apps/agent/../../../outside.txt"):
            res = self.call(server, "repo_read", path=path)
            self.assertTrue(res["isError"], path)
            self.assertNotIn("OUTSIDE_SECRET", json.dumps(res))
        # An absolute path is read as a repository path, so it cannot reach the real file.
        res = self.call(server, "repo_read", path=str(outside))
        self.assertTrue(res["isError"])
        self.assertNotIn("OUTSIDE_SECRET", json.dumps(res))
        for path in ("apps/agent/.env", "server-guardian-credentials.enc", "apps/agent/link.txt", "apps/agent/blob.bin"):
            res = self.call(server, "repo_read", path=path)
            self.assertTrue(res["isError"], path)
            self.assertIn("excluded", res["content"][0]["text"])
        self.assertTrue(self.call(server, "repo_list", path="../")["isError"])
        grep = self.call(server, "repo_grep", pattern="TARGET")
        self.assertNotIn("ENV_SECRET", grep["content"][0]["text"])
        self.assertNotIn("ENC_SECRET", grep["content"][0]["text"])
        self.assertTrue(all(not r["ok"] for r in self.records()[:-1]))

    def test_uncommitted_changes_are_not_visible(self) -> None:
        (self.repo / "apps/agent/notes.txt").write_text("changed UNCOMMITTED\n")
        res = self.call(self.server(), "repo_read", path="apps/agent/notes.txt")
        self.assertNotIn("UNCOMMITTED", res["content"][0]["text"])

    def test_unknown_arguments_are_dropped_and_bad_ones_are_errors(self) -> None:
        server = self.server()
        self.assertTrue(self.call(server, "repo_read")["isError"])
        self.assertTrue(self.call(server, "repo_grep", pattern="(unclosed")["isError"])
        res = self.call(server, "repo_read", path="apps/agent/notes.txt", op="list", extra=1)
        self.assertFalse(res["isError"])  # "op" from the caller cannot change the operation


class LogTests(ServerCase):
    def test_record_shape_and_no_contents(self) -> None:
        server = self.server()
        self.call(server, "repo_read", path="apps/agent/notes.txt")
        self.call(server, "repo_read", path="nope.txt")
        ok, bad = self.records()
        self.assertEqual(set(ok), {"op", "target", "ok", "bytes", "truncated", "error", "paths", "spans", "mentions", "limit"})
        self.assertEqual((ok["op"], ok["target"], ok["ok"], ok["paths"]), ("read", "apps/agent/notes.txt", True, ["apps/agent/notes.txt"]))
        self.assertEqual(ok["spans"], {"apps/agent/notes.txt": [[1, 1_000_000_000]]})  # a whole file: numbers only
        self.assertGreater(ok["bytes"], 0)
        self.assertEqual((bad["ok"], bad["error"], bad["paths"], bad["spans"]), (False, "not found", [], {}))
        self.assertNotIn("TARGET gamma", self.log.read_text())
        self.assertEqual(oct(self.log.stat().st_mode & 0o777), "0o600")

    def test_big_read_is_marked_truncated(self) -> None:
        server = self.server()
        self.call(server, "repo_read", path="apps/agent/big.txt")
        self.assertTrue(self.records()[0]["truncated"])


class LimitTests(ServerCase):
    def test_step_limit_gives_budget_error(self) -> None:
        server = self.server(max_steps=2)
        self.call(server, "repo_list")
        self.call(server, "repo_list")
        res = self.call(server, "repo_read", path="apps/agent/notes.txt")
        self.assertTrue(res["isError"])
        self.assertTrue(res["content"][0]["text"].startswith("budget"))
        last = self.records()[-1]
        self.assertEqual((last["error"], last["ok"], last["bytes"], last["paths"]), ("budget", False, 0, []))

    def test_turn_bytes_limit_clips_then_refuses(self) -> None:
        server = self.server(max_turn_bytes=300)
        first = self.call(server, "repo_read", path="apps/agent/big.txt")
        self.assertFalse(first["isError"])
        self.assertLessEqual(len(first["content"][0]["text"].encode()), 300 + 60)
        self.assertTrue(self.records()[0]["truncated"])
        second = self.call(server, "repo_list")
        self.assertTrue(second["content"][0]["text"].startswith("budget"))

    def test_step_bytes_defers_a_read_that_would_be_cut_hard(self) -> None:
        server = self.server(max_step_bytes=500)
        res = self.call(server, "repo_read", path="apps/agent/big.txt")
        self.assertTrue(res["isError"])
        self.assertTrue(res["content"][0]["text"].startswith("deferred"))
        record = self.records()[0]
        self.assertEqual((record["error"], record["ok"], record["deferred"], record["bytes"]), ("deferred", False, True, 0))
        # A narrower request fits and is served.
        small = self.call(server, "repo_read", path="apps/agent/big.txt:1-5")
        self.assertFalse(small["isError"])

    def test_limits_continue_across_server_processes_of_one_turn(self) -> None:
        self.call(self.server(max_steps=2), "repo_list")
        second = self.server(max_steps=2)  # a new claude call of the same turn starts a new process
        self.call(second, "repo_list")
        self.assertTrue(self.call(second, "repo_list")["content"][0]["text"].startswith("budget"))

    def test_no_limits_by_default(self) -> None:
        server = self.server()
        for _ in range(30):
            self.assertFalse(self.call(server, "repo_list")["isError"])


if __name__ == "__main__":
    unittest.main()

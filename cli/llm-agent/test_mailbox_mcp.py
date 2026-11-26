"""The mailbox MCP server of one resident agent (docs/design/resident-agents.md, M2c).

The daemon side is a tiny unix-socket server in the test process: it records each one-line request
and answers from a settable reply. No network, no ssh, no real CLI.
"""

import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List

from mailbox_mcp import (HUB_TOOLS, MailboxMcpServer, SERVER_KEY, allowed_tool_names)
from mcp_stdio import PROTOCOL_VERSION

TOOL_ORDER = ("mail_inbox", "mail_send", "mail_agents", "task_list", "task_create", "task_claim",
              "task_update", "task_release")

GOOD_ARGS: Dict[str, Dict[str, Any]] = {
    "mail_inbox": {},
    "mail_send": {"to": "agy@node-1", "subject": "s", "body": "SECRET-BODY", "kind": "question",
                  "task": "k-1", "reply_to": "m-0", "thread": "t-1"},
    "mail_agents": {},
    "task_list": {},
    "task_create": {"title": "do it", "detail": "what done looks like", "assignee": "agy@node-1",
                    "blocked_by": ["k-0"], "project": "p-1"},
    "task_claim": {"task": "k-1"},
    "task_update": {"task": "k-1", "generation": 3, "status": "completed", "result": "done"},
    "task_release": {"task": "k-1", "generation": 3, "reason": "cannot continue"},
}


class FakeDaemon:
    """Serves the agent socket: one line in, one line out, and a record of every request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.requests: List[Dict[str, Any]] = []
        self.reply: Dict[str, Any] = {"ok": True, "messages": []}
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_UNIX)
        self._srv.bind(str(path))
        self._srv.listen(4)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        self._srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                with self._lock:
                    self.requests.append(json.loads(buffer.split(b"\n", 1)[0]))
                    reply = dict(self.reply)
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self.thread.join(2.0)
        self._srv.close()


class MailboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="sgm", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.daemon = FakeDaemon(self.dir / "e1.sock")
        self.addCleanup(self.daemon.close)
        self.log = self.dir / "calls.jsonl"
        self.server = MailboxMcpServer(str(self.daemon.path), str(self.log), timeout=10.0)

    def call(self, name: str, **arguments: Any) -> Dict[str, Any]:
        response = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
        assert response is not None
        return response["result"]

    def text(self, result: Dict[str, Any]) -> str:
        return result["content"][0]["text"]


class ProtocolTests(MailboxCase):
    def test_handshake_and_tools_list(self) -> None:
        init = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}})
        assert init is not None
        self.assertEqual(init["result"]["serverInfo"], {"name": "guardian-mailbox", "version": "0.1.0"})
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listing = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listing is not None
        tools = listing["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], list(TOOL_ORDER))
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False))
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(by_name["mail_send"]["inputSchema"]["required"], ["to", "subject", "body"])
        self.assertEqual(by_name["task_create"]["inputSchema"]["required"], ["title"])
        self.assertEqual(by_name["task_update"]["inputSchema"]["required"], ["task", "generation", "status"])
        self.assertEqual(by_name["task_release"]["inputSchema"]["required"], ["task", "generation", "reason"])
        self.assertEqual(by_name["task_claim"]["inputSchema"]["required"], ["task"])
        self.assertEqual(by_name["mail_send"]["inputSchema"]["properties"]["kind"]["enum"],
                         ["message", "question", "answer", "task_offer", "task_result"])
        self.assertEqual(by_name["task_update"]["inputSchema"]["properties"]["status"]["enum"],
                         ["completed", "failed"])
        self.assertEqual(allowed_tool_names(), [f"mcp__{SERVER_KEY}__{name}" for name in TOOL_ORDER])
        self.assertEqual(len(allowed_tool_names()), 8)

    def test_unknown_tools_and_methods(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "shell", "arguments": {}}})
        assert response is not None
        self.assertEqual(response["error"]["code"], -32602)
        missing = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
        assert missing is not None
        self.assertEqual(missing["error"]["code"], -32601)


class ToolCallTests(MailboxCase):
    def test_every_tool_round_trips_over_the_socket(self) -> None:
        for name in TOOL_ORDER:
            result = self.call(name, **GOOD_ARGS[name])
            self.assertFalse(result["isError"], (name, result))
            payload = json.loads(self.text(result))
            self.assertEqual(payload, {"messages": []})  # the daemon reply minus ok, as JSON text
        self.assertEqual([request["tool"] for request in self.daemon.requests], list(TOOL_ORDER))
        sent = {request["tool"]: request for request in self.daemon.requests}
        self.assertEqual(sent["mail_send"]["args"]["to"], "agy@node-1")
        self.assertEqual(sent["task_create"]["args"]["blocked_by"], ["k-0"])
        self.assertNotIn("op_id", sent["mail_inbox"])          # answered on the node, no operation id
        for name in HUB_TOOLS:
            self.assertIsInstance(sent[name]["op_id"], str)     # the hub tools carry one

    def test_operation_ids_are_fresh_per_call(self) -> None:
        self.call("task_claim", task="k-1")
        self.call("task_claim", task="k-1")
        ids = [request["op_id"] for request in self.daemon.requests]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_bad_input_never_touches_the_socket(self) -> None:
        for name, arguments in (("mail_send", {"to": "agy@node-1", "subject": "s"}),  # no body
                                ("task_update", {"task": "k-1", "generation": "three", "status": "completed"}),
                                ("task_update", {"task": "k-1", "generation": 1, "status": "lost"}),
                                ("task_release", {"task": "k-1", "generation": 1}),  # no reason
                                ("task_create", {"blocked_by": "k-0"}),              # not an array, no title
                                ("task_claim", {})):
            result = self.call(name, **arguments)
            self.assertTrue(result["isError"], (name, arguments))
            self.assertTrue(self.text(result).startswith("error: "), (name, arguments))
        self.assertEqual(self.daemon.requests, [])  # nothing was sent

    def test_a_missing_socket_is_an_error(self) -> None:
        server = MailboxMcpServer(str(self.dir / "nowhere.sock"), timeout=2.0)
        result = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                "params": {"name": "mail_agents", "arguments": {}}})
        assert result is not None
        self.assertTrue(result["result"]["isError"])
        self.assertIn("mailbox not available", self.text(result["result"]))

    def test_a_refusal_comes_back_with_its_code(self) -> None:
        self.daemon.reply = {"ok": False, "error": "limit_reached", "detail": "40 messages per thread"}
        result = self.call("mail_send", **GOOD_ARGS["mail_send"])
        self.assertTrue(result["isError"])
        self.assertEqual(self.text(result), "error: limit_reached: 40 messages per thread")


class LogTests(MailboxCase):
    def records(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_the_log_holds_no_body_text(self) -> None:
        self.call("mail_send", **GOOD_ARGS["mail_send"])
        self.daemon.reply = {"ok": False, "error": "peers", "detail": "not a peer"}
        self.call("mail_send", **GOOD_ARGS["mail_send"])
        raw = self.log.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-BODY", raw)  # the body text never reaches the log
        self.assertNotIn('"to"', raw)         # nor any argument names or values
        records = self.records()
        self.assertEqual([record["tool"] for record in records], ["mail_send", "mail_send"])
        self.assertEqual([record["ok"] for record in records], [True, False])
        self.assertEqual(records[1]["error"], "peers")
        for record in records:
            self.assertEqual(sorted(record), ["bytes", "error", "ok", "tool"])
            self.assertIsInstance(record["bytes"], int)


if __name__ == "__main__":
    unittest.main()

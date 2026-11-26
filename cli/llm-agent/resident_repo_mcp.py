"""The resident repository tools of one agent as a stdio MCP server (docs/design/resident-agents.md, M2e).

The resident supervisor starts one of these next to its CLI, pointed at that agent's incarnation
socket on the worker daemon (resident_node.py). The five snapshot tools of repo_mcp.py gain a required
`task` argument: the daemon answers from the snapshot of that task, only while the caller owns it at
the current generation, and appends every call to the per-task log that leaves with task_update. The
server holds no state, opens no other socket and never sees a hub credential.

    python3 resident_repo_mcp.py --socket <agents/<name>/e<epoch>.sock> [--timeout 40]

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout, protocol version as in mcp_stdio.py.
"""

import argparse
import copy
import json
import socket
import sys
from typing import Any, Dict, List, Optional, Tuple

from mcp_stdio import PROTOCOL_VERSION
from repo_mcp import SNAPSHOT_TOOLS
from repo_mcp import TOOLS as REPO_TOOL_SPECS

SERVER_NAME = "guardian-resident-repo"
SERVER_VERSION = "0.1.0"
SERVER_KEY = "repo"  # the name in the mcp config, so tools appear to claude as mcp__repo__<tool>
DEFAULT_TIMEOUT = 40.0  # the daemon waits at most 15 s for a pending snapshot; the agent socket times out at 30 s
MAX_LINE_BYTES = 1024 * 1024  # one line each way on the agent socket, as on the relay stream
DESCRIPTION = ("Read-only view of the repository snapshot of a task you own; pass the task id from "
               "task_claim.")
TASK_ARGUMENT = {"type": "string", "description": "Task id from task_claim; the snapshot is read only "
                                                  "while you own it."}


def _tools() -> Dict[str, Dict[str, Any]]:
    """The five snapshot tools with repo_mcp's input schemas plus a required `task` string."""
    tools: Dict[str, Dict[str, Any]] = {}
    for name in SNAPSHOT_TOOLS:
        schema = copy.deepcopy(REPO_TOOL_SPECS[name]["inputSchema"])
        schema["properties"]["task"] = dict(TASK_ARGUMENT)
        schema["required"] = list(schema.get("required", [])) + ["task"]
        tools[name] = {"description": DESCRIPTION, "inputSchema": schema,
                       "arguments": tuple(schema["properties"])}
    return tools


TOOLS = _tools()


def allowed_tool_names() -> List[str]:
    """The names claude sees (mcp__repo__repo_list, ...), for --allowedTools and the mcp config."""
    return [f"mcp__{SERVER_KEY}__{name}" for name in TOOLS]


def _input_error(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Why the arguments are not acceptable, or None. A bad call never touches the socket."""
    task = args.get("task")
    if not isinstance(task, str) or not task:
        return "task must be a non-empty string"
    return None


class ResidentRepoMcpServer:
    """JSON-RPC handler. handle() takes one decoded message and returns the response (None for
    notifications), the same shape as RepoMcpServer in repo_mcp.py."""

    def __init__(self, socket_path: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    # --- the agent socket ----------------------------------------------------------------------

    def _ask(self, request: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], int]:
        """One line to the daemon and one line back: (reply, bytes seen). reply is None on a
        transport failure (the daemon or the hub is not reachable)."""
        line = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            return None, 0
        conn = socket.socket(socket.AF_UNIX)
        try:
            conn.settimeout(self.timeout)
            conn.connect(self.socket_path)
            conn.sendall(line)
            buffer = b""
            while b"\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_LINE_BYTES:
                    break
        except OSError:
            return None, 0
        finally:
            conn.close()
        if len(buffer) > MAX_LINE_BYTES:
            return None, len(buffer)
        try:
            reply = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, len(buffer)
        return (reply, len(buffer)) if isinstance(reply, dict) else (None, len(buffer))

    def call_tool(self, name: str, arguments: Any) -> Dict[str, Any]:
        """One repo tool call: validate first (a bad call never touches the socket), then one round
        trip on the agent socket. An ok answer's result is returned exactly as repo_mcp would."""
        spec = TOOLS.get(name)
        if spec is None:
            return {"isError": True, "content": [{"type": "text", "text": f"error: unknown tool {name!r}"}]}
        args = arguments if isinstance(arguments, dict) else {}
        problem = _input_error(name, args)
        if problem is not None:
            return {"isError": True, "content": [{"type": "text", "text": f"error: {problem}"}]}
        reply, _size = self._ask({"tool": name, "args": {k: args[k] for k in spec["arguments"] if k in args}})
        if reply is None:
            return {"isError": True, "content": [{"type": "text", "text": "error: repository tools not available"}]}
        if reply.get("ok") is False:
            error = str(reply.get("error") or "refused")
            detail = reply.get("detail")
            text = f"{error}: {detail}" if detail else error
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        result = reply.get("result")
        if not isinstance(result, dict):
            return {"isError": True, "content": [{"type": "text", "text": "error: internal error"}]}
        return result

    # --- JSON-RPC ------------------------------------------------------------------------------

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        method = message.get("method")
        request_id = message.get("id")
        if "id" not in message:
            return None  # notification (notifications/initialized, notifications/cancelled, ...)
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            client_version = params.get("protocolVersion")
            return _ok(request_id, {
                "protocolVersion": client_version if isinstance(client_version, str) and client_version
                else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": [
                {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
                for name, spec in TOOLS.items()
            ]})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOLS:
                return _error(request_id, -32602, f"unknown tool {name!r}")
            try:
                return _ok(request_id, self.call_tool(name, params.get("arguments")))
            except Exception as exc:  # noqa: BLE001 - a broken call must not end the server
                print(f"resident_repo_mcp: {name} failed: {exc!r}", file=sys.stderr)
                return _ok(request_id, {"isError": True, "content": [{"type": "text", "text": "error: internal error"}]})
        return _error(request_id, -32601, f"method not found: {method}")

    def serve(self, stdin: Any = None, stdout: Any = None) -> None:
        """Reads requests until stdin closes."""
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response: Optional[Dict[str, Any]] = _error(None, -32700, "parse error")
            else:
                response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _ok(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--socket", required=True,
                        help="the agent's incarnation socket on the worker daemon")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds for one call (default 40)")
    args = parser.parse_args(argv)
    if len(args.socket.encode("utf-8")) > 103:  # sockaddr_un.sun_path minus the NUL on the smallest system
        print("resident_repo_mcp: the socket path is too long for a unix socket", file=sys.stderr)
        return 2
    ResidentRepoMcpServer(args.socket, timeout=args.timeout).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

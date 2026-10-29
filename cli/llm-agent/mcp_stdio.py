"""
Minimal MCP client over stdio (JSON-RPC 2.0, newline-delimited), standard library only.

Used to talk to real MCP servers such as jev-mcp from configs/mcp_servers.json:

    with StdioMCPClient.from_config("jev") as client:
        result = client.call_tool("jev_queue_status", {})
"""

import json
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"
CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "configs" / "mcp_servers.json"


class MCPError(RuntimeError):
    """Transport, protocol or tool-level failure from an MCP server."""


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def load_server_config(name: str, config_path: Optional[Path] = None) -> Dict[str, Any]:
    data = json.loads((config_path or CONFIG_PATH).read_text(encoding="utf-8"))
    servers = data.get("mcp_servers", {})
    if name not in servers:
        raise MCPError(f"MCP server '{name}' is not configured in {config_path or CONFIG_PATH}")
    return servers[name]


class StdioMCPClient:
    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.argv = [_expand(command), *[_expand(a) for a in (args or [])]]
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._stderr = None
        self._next_id = 0
        self._buffer = b""
        self.server_info: Dict[str, Any] = {}

    @classmethod
    def from_config(cls, name: str, base_env: Optional[Dict[str, str]] = None, timeout: float = 60.0,
                    config_path: Optional[Path] = None) -> "StdioMCPClient":
        """Builds a client from configs/mcp_servers.json. Only keys listed in pass_env are
        forwarded from base_env (plus PATH/HOME), so unrelated secrets stay out of the server."""
        cfg = load_server_config(name, config_path)
        source = base_env if base_env is not None else dict(os.environ)
        env = {k: source[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in source}
        env.update({k: source[k] for k in cfg.get("pass_env", []) if source.get(k)})
        env.update({k: _expand(str(v)) for k, v in cfg.get("env", {}).items()})
        cwd = _expand(cfg["cwd"]) if cfg.get("cwd") else None
        return cls(cfg["command"], cfg.get("args", []), env, cwd, timeout)

    # --- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> "StdioMCPClient":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> None:
        self._stderr = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                env=self.env, cwd=self.cwd,
            )
        except OSError as exc:
            raise MCPError(f"cannot start MCP server {self.argv[0]}: {exc}") from exc
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "server-guardian", "version": "0.1.0"},
        })
        self.server_info = result.get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
        if self._stderr:
            self._stderr.close()
        self._proc = None

    # --- API ---------------------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        return self._request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Calls a tool and returns its structured result (or parsed text content)."""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise MCPError(f"{name} failed: {text[:500]}")
        if "structuredContent" in result:
            structured = result["structuredContent"]
            # FastMCP wraps non-object returns as {"result": ...}
            return structured.get("result", structured) if set(structured) == {"result"} else structured
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # --- JSON-RPC plumbing ---------------------------------------------------------

    def _send(self, message: Dict[str, Any]) -> None:
        if not self._proc or self._proc.stdin is None:
            raise MCPError("MCP server is not running")
        try:
            self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as exc:
            raise MCPError(f"MCP server closed its input: {self._stderr_tail()}") from exc

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._read_message(deadline)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    err = message["error"]
                    raise MCPError(f"{method}: {err.get('message')} ({err.get('code')})")
                return message["result"]
            if "method" in message and "id" in message:
                # Server-initiated request (e.g. roots/list): we offer no client capabilities.
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "method not supported by client"}})
            # Notifications (logging, progress) are ignored.

    def _read_message(self, deadline: float) -> Dict[str, Any]:
        assert self._proc and self._proc.stdout
        selector = selectors.DefaultSelector()
        selector.register(self._proc.stdout, selectors.EVENT_READ)
        try:
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"timed out after {self.timeout:.0f}s waiting for the MCP server")
                if not selector.select(timeout=remaining):
                    continue
                chunk = os.read(self._proc.stdout.fileno(), 65536)
                if not chunk:
                    raise MCPError(f"MCP server exited: {self._stderr_tail()}")
                self._buffer += chunk
        finally:
            selector.close()
        line, self._buffer = self._buffer.split(b"\n", 1)
        if not line.strip():
            return {}
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPError(f"invalid JSON from MCP server: {line[:200]!r}") from exc

    def _stderr_tail(self) -> str:
        if not self._stderr:
            return ""
        self._stderr.seek(0)
        return self._stderr.read().decode("utf-8", "replace")[-500:].strip()

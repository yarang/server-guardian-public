"""
Read-only repository tools as a stdio MCP server (docs/design/debate-agent-retrieval.md, phase 2).

The debate engine starts it for a claude or codex team member whose "code_tools" is "mcp" (see
llm_runner._claude_mcp_config and _codex_mcp_args). It serves repo_list, repo_grep, repo_read, repo_outline, repo_defs, repo_refs, repo_map, repo_callers, repo_callees and repo_wiki over the
committed content of one workspace at one pinned sha, by calling RepoTools and nothing else: there is
no other way to reach the filesystem, so ../ traversal, absolute paths, secrets, symlinks and
binaries are refused by the same rules as the engine-mediated repo blocks.

    python repo_mcp.py --workspace <clone> --sha <sha> --log <calls.jsonl> [--max-steps N]
                       [--max-step-bytes N] [--max-turn-bytes N]
                       [--cache-dir <symbol index cache>] [--wiki-dir <code wiki pages of the project>]

Every tool call appends one JSON line to --log: {op, target, ok, bytes, truncated, error, paths, spans, mentions,
limit[, deferred][, resolved]} ("resolved": "types" or "name" for defs, refs, callers and callees). "spans" maps each provided path to the [start, end] line ranges the result showed (whole file =
[1, 1000000000]; numbers only, so strong citations can be checked against the lines the agent saw). "mentions"
names the repository paths that occur inside the returned text (names only). The
log never holds file contents; the engine reads it to emit retrieval events, to track which files the agent was
given and which paths appeared inside them (a bare path mention in an answer is backed by either). Limits count per agent turn, not per process: a
server started on a log that already has records (the engine reuses one log for all claude calls of
a turn) continues from those records.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout, protocol version as in mcp_stdio.py.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_stdio import PROTOCOL_VERSION
from repo_tools import RepoTools, describe_request, provided_paths, provided_spans

SERVER_NAME = "guardian-repo"
SERVER_VERSION = "0.1.0"
SERVER_KEY = "repo"  # the name in the mcp config, so tools appear to claude as mcp__repo__<tool>
# A read that the per-call byte limit would cut to less than this fraction is deferred instead.
DEFER_BELOW_FRACTION = 0.5
MAX_LOGGED_MENTIONS = 400  # tree paths named inside one tool result, logged for the engine's citation check

# tool name -> (RepoTools op, description, JSON schema of the arguments)
TOOLS: Dict[str, Dict[str, Any]] = {
    "repo_list": {
        "op": "list",
        "description": (
            "List files and directories of the repository at the pinned commit, with file sizes. "
            "Secret files, symlinks and binaries are not shown. At most 400 lines; list a subdirectory for more."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative directory (default: the root)."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Levels to show (default 2)."},
            },
            "additionalProperties": False,
        },
    },
    "repo_grep": {
        "op": "grep",
        "description": (
            "Regular-expression search (extended syntax, at most 200 characters) in the committed files. "
            "Returns 'path:line: text', at most 200 lines."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "maxLength": 200, "description": "Extended regular expression."},
                "glob": {"type": "string", "description": "Optional file filter such as apps/agent/*.go."},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search (default false)."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    "repo_read": {
        "op": "read",
        "description": (
            "Read a committed file with numbered lines ('N| code'; the numbers are not part of the code). "
            "path may be a whole file, 'path#Symbol' or 'path:START-END'. A file read is capped at 64 KB, so use "
            "line ranges or symbols for large files. Cite file:line for claims about the code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path, optionally with #Symbol or :START-END."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "repo_outline": {
        "op": "outline",
        "description": ("Top-level declaration signatures with line numbers of a Go, Python, TypeScript or "
                        "JavaScript file. Use it first on large files, then read a symbol or line range."),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repository-relative file path."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "repo_defs": {
        "op": "defs",
        "description": (
            "Where a symbol is declared, in Go, Python, TypeScript and JavaScript files: 'path:start-end kind "
            "signature', at most 100 lines. symbol is Name or Type.Method. To locate code prefer this over grep. "
            "The signature line is not code evidence: read the lines before citing them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "maxLength": 200, "description": "Name or Type.Method."},
                "glob": {"type": "string", "description": "Optional file filter such as apps/agent/*.go."},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    "repo_refs": {
        "op": "refs",
        "description": (
            "Lines where a symbol's name occurs, as 'path:line: text', at most 200 lines. The match is by name "
            "only (no type information), so it can include unrelated symbols with the same name: read the call "
            "sites before citing them. For Type.Method the method name is searched. For Go symbols the answer "
            "is type-checked when the Go index is ready (a header line says so) and lists only real references. "
            "To find callers prefer this over grep."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "maxLength": 200, "description": "Name or Type.Method."},
                "glob": {"type": "string", "description": "Optional file filter such as apps/agent/*.go."},
                "include_defs": {"type": "boolean", "description": "Also list the declaration lines (default false)."},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    "repo_map": {
        "op": "map",
        "description": (
            "Repository map of a directory: its files with line counts and their most referenced declarations "
            "(ranked by references from other files). Directories deeper than depth are folded into 'dir/ (N files, "
            "folded)' lines: call again with that path to expand them. A navigation aid, not code evidence: read the "
            "lines before citing them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative directory (default: the root)."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 4,
                          "description": "Directory levels shown in detail (default 2)."},
            },
            "additionalProperties": False,
        },
    },
    "repo_callers": {
        "op": "callers",
        "description": (
            "Call sites of a Go function or method as 'path:line: enclosing  text', at most 200 lines. Type-checked: "
            "a same-named symbol elsewhere is not listed, and a call through an interface is listed under the "
            "interface method. Go only (use repo_refs for other languages). The lines are locations: read the code "
            "before citing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "maxLength": 200, "description": "Name or Type.Method."},
                "glob": {"type": "string", "description": "Optional file filter such as apps/agent/*.go."},
                "include_tests": {"type": "boolean", "description": "Also list calls in _test.go files (default false)."},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    "repo_callees": {
        "op": "callees",
        "description": (
            "What the body of a Go function or method calls, with the definition location of each target "
            "(package.Name for the standard library and dependencies), at most 200 lines. Type-checked; Go only. "
            "The lines are locations: read the code before citing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "maxLength": 200, "description": "Name or Type.Method."}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    "repo_wiki": {
        "op": "wiki",
        "description": (
            "Code wiki: navigation notes about directories and features, written by earlier sessions or people. With "
            "page (a page id) returns the page: a generated overview of its scope plus the narrative and its state "
            "(fresh, stale or broken); with query lists matching pages with their state (without both, lists all "
            "pages). Notes only, never evidence: where a page and the code disagree the code is right, and every "
            "claim must cite code you read yourself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "maxLength": 64, "description": "Page id, e.g. cli-llm-agent."},
                "query": {"type": "string", "maxLength": 200, "description": "Case-insensitive text to find in page titles, scopes and narratives."},
            },
            "additionalProperties": False,
        },
    },
}
# Argument names forwarded to RepoTools per tool; anything else is dropped.
ARGUMENTS = {"repo_list": ("path", "depth"), "repo_grep": ("pattern", "glob", "ignore_case"),
             "repo_read": ("path",), "repo_outline": ("path",),
             "repo_defs": ("symbol", "glob"), "repo_refs": ("symbol", "glob", "include_defs"),
             "repo_map": ("path", "depth"), "repo_callers": ("symbol", "glob", "include_tests"),
             "repo_callees": ("symbol",), "repo_wiki": ("page", "query")}


# The tools a model worker serves from a repository snapshot (repo_snapshot.py): the ones that need no symbol index,
# no Go toolchain and no code wiki, none of which the worker has. See --no-index.
SNAPSHOT_TOOLS = ("repo_list", "repo_grep", "repo_read", "repo_outline", "repo_map")


def tool_names(no_index: bool = False) -> List[str]:
    return [name for name in TOOLS if not no_index or name in SNAPSHOT_TOOLS]


def allowed_tool_names(no_index: bool = False) -> List[str]:
    """The names claude sees (mcp__repo__repo_list, ...), for --allowedTools."""
    return [f"mcp__{SERVER_KEY}__{name}" for name in tool_names(no_index)]


def opencode_tool_names(no_index: bool = False) -> List[str]:
    """The names opencode sees (repo_repo_list, ...), for its permission config."""
    return [f"{SERVER_KEY}_{name}" for name in tool_names(no_index)]


def _env_limit(name: str) -> Optional[int]:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


class RepoMcpServer:
    """JSON-RPC handler. handle() takes one decoded message and returns the response (None for notifications)."""

    def __init__(self, tools: RepoTools, log_path: Optional[str], max_steps: Optional[int] = None,
                 max_step_bytes: Optional[int] = None, max_turn_bytes: Optional[int] = None,
                 no_index: bool = False, seed_steps: int = 0, seed_bytes: int = 0) -> None:
        self.tools = tools
        self.log_path = log_path
        self.max_steps = max_steps
        self.max_step_bytes = max_step_bytes
        self.max_turn_bytes = max_turn_bytes
        # no_index serves only SNAPSHOT_TOOLS. seed_* is what the turn used before this server's log began: a worker
        # job starts with an empty log while the hub counts the whole turn, so the hub passes the count so far.
        self.tool_names = tool_names(no_index)
        self.steps = max(0, seed_steps)
        self.turn_bytes = max(0, seed_bytes)
        # Records of earlier processes of the same turn count against the same limits.
        for record in self._read_log():
            self.steps += 1
            self.turn_bytes += int(record.get("bytes") or 0)

    # --- log -----------------------------------------------------------------------------------

    def _read_log(self) -> List[Dict[str, Any]]:
        if not self.log_path:
            return []
        records: List[Dict[str, Any]] = []
        try:
            with open(self.log_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        records.append(item)
        except OSError:
            pass
        return records

    def _append_log(self, record: Dict[str, Any]) -> None:
        """One line per call; a failing log never fails the tool call."""
        if not self.log_path:
            return
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as exc:
            print(f"repo_mcp: cannot write log: {exc}", file=sys.stderr)

    # --- tools ---------------------------------------------------------------------------------

    # @MX:ANCHOR: The only path from an MCP tool call to repository content.
    # @MX:REASON: Tests, the engine's log reader and claude all depend on it staying inside RepoTools at one sha.
    def call_tool(self, name: str, arguments: Any) -> Dict[str, Any]:
        """Runs one tool call under the turn limits; returns the MCP tool result and logs the call."""
        spec = TOOLS.get(name) if name in self.tool_names else None
        if spec is None:
            return {"isError": True, "content": [{"type": "text", "text": f"error: unknown tool {name!r}"}]}
        args = arguments if isinstance(arguments, dict) else {}
        request: Dict[str, Any] = {"op": spec["op"]}
        request.update({k: args[k] for k in ARGUMENTS[name] if k in args})

        step_left = None if self.max_steps is None else self.max_steps - self.steps
        turn_left = None if self.max_turn_bytes is None else self.max_turn_bytes - self.turn_bytes
        limit: Optional[int] = None
        if (step_left is not None and step_left <= 0) or (turn_left is not None and turn_left <= 0):
            result = {**_stub(request), "ok": False, "error": "budget"}
            message = ("budget: the retrieval budget for this turn is used up. Answer now with what you have seen "
                       "and say which claims could not be verified.")
        else:
            rooms = [r for r in (self.max_step_bytes, turn_left) if r is not None]
            limit = min(rooms) if rooms else None
            step_bound = self.max_step_bytes is not None and (turn_left is None or self.max_step_bytes <= turn_left)
            result = self.tools.run(request, max_bytes=limit, defer_below=DEFER_BELOW_FRACTION if step_bound else None)
            message = ""
            if result.get("deferred"):
                message = (f"deferred: this read is larger than the per-call limit of {limit // 1024} KB and was not "
                           "returned. Ask again for a line range (path:START-END), a symbol (path#Symbol) or the outline.")
        paths = [] if message else provided_paths(result)
        spans = {} if message else {p: [list(s) for s in v] for p, v in provided_spans(result).items()}
        self.steps += 1
        self.turn_bytes += 0 if message else result["bytes"]
        self._append_log({
            "op": result["op"], "target": result["target"], "ok": bool(result["ok"]) and not message,
            "bytes": 0 if message else result["bytes"], "truncated": bool(result["truncated"]),
            "error": result["error"], "paths": paths, "spans": spans, "limit": limit,
            "mentions": [] if message else self.tools.mentioned_in(result)[:MAX_LOGGED_MENTIONS],
            **({"deferred": True} if result.get("deferred") else {}),
            **({"resolved": result["resolved"]} if result.get("resolved") and result["ok"] and not message else {}),
        })
        if message:
            return {"isError": True, "content": [{"type": "text", "text": message}]}
        if not result["ok"]:
            return {"isError": True, "content": [{"type": "text", "text": f"error: {result['error']}"}]}
        return {"isError": False, "content": [{"type": "text", "text": result["text"]}]}

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
                "protocolVersion": client_version if isinstance(client_version, str) and client_version else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": [
                {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
                for name, spec in TOOLS.items() if name in self.tool_names
            ]})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tool_names:
                return _error(request_id, -32602, f"unknown tool {name!r}")
            try:
                return _ok(request_id, self.call_tool(name, params.get("arguments")))
            except Exception as exc:  # noqa: BLE001 - a broken call must not end the server
                print(f"repo_mcp: {name} failed: {exc!r}", file=sys.stderr)
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


def _stub(request: Dict[str, Any]) -> Dict[str, Any]:
    info = describe_request(request)
    return {"op": info["op"], "target": info["target"], "ok": False, "bytes": 0, "truncated": False,
            "error": None, "text": ""}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--log", default=None, help="JSONL file that receives one record per tool call")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-step-bytes", type=int, default=None)
    parser.add_argument("--max-turn-bytes", type=int, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="directory for the symbol index's per-blob cache (optional; memory only without it)")
    parser.add_argument("--wiki-dir", type=Path, default=None,
                        help="the project's code wiki directory (optional; the wiki tool answers 'not available' without it)")
    parser.add_argument("--no-index", action="store_true",
                        help="serve only repo_list, repo_grep, repo_read, repo_outline and repo_map (a repository snapshot on a worker)")
    parser.add_argument("--sha-label", default=None,
                        help="the commit shown to the agent when --sha is a snapshot's own commit (the hub's commit)")
    parser.add_argument("--seed-steps", type=int, default=0, help="tool calls this turn made before this log began")
    parser.add_argument("--seed-bytes", type=int, default=0, help="result bytes this turn served before this log began")
    args = parser.parse_args(argv)

    def limit(value: Optional[int], env: str) -> Optional[int]:
        return value if value and value > 0 else _env_limit(env)

    try:
        tools = RepoTools(args.workspace, args.sha, cache_dir=args.cache_dir, wiki_dir=args.wiki_dir,
                          sha_label=args.sha_label)
    except Exception as exc:  # noqa: BLE001 - ContextError or OSError: report once on stderr and exit
        print(f"repo_mcp: cannot open {args.workspace} at {args.sha}: {exc}", file=sys.stderr)
        return 2
    server = RepoMcpServer(
        tools, args.log,
        max_steps=limit(args.max_steps, "GUARDIAN_RETRIEVAL_STEPS"),
        max_step_bytes=limit(args.max_step_bytes, "GUARDIAN_RETRIEVAL_STEP_BYTES"),
        max_turn_bytes=limit(args.max_turn_bytes, "GUARDIAN_RETRIEVAL_TURN_BYTES"),
        no_index=args.no_index, seed_steps=args.seed_steps, seed_bytes=args.seed_bytes,
    )
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

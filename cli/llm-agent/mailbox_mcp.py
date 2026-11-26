"""The mailbox tools of one resident agent as a stdio MCP server (docs/design/resident-agents.md, M2c).

The resident supervisor starts one of these next to its CLI, pointed at that agent's incarnation
socket on the worker daemon (resident_node.py). Every tool is one JSON line to the daemon and one
line back: mail_inbox is answered from the node's spool, the rest are forwarded to the hub and
answered inside the same call (a claim, a refusal or an id comes back synchronously; there is no
offline outbox). The server holds no state, opens no other socket and never sees a hub credential.

    python3 mailbox_mcp.py --socket <agents/<name>/e<epoch>.sock> [--log PATH] [--timeout 25]
                           [--worktrees]

With --worktrees (open agents only, docs/design/resident-open-tools.md O3) the server also exposes
task_worktree: the daemon writes a git bundle of the owned task's snapshot into the sandbox socket
directory, and this server clones it into $HOME/work/tasks/<task> (kept between turns) and records
the snapshot commit as the diff base. A completed task_update then collects git's own diff of that
worktree - `git add -A` plus `git diff --cached --binary --no-renames <base>` - and attaches it as
patch/patch_base; the model never writes a diff itself. Without the flag the tool list and every
name stay exactly the closed set.

The optional log takes one JSON line per call: {tool, ok, error, bytes}. It never holds message
text. Standard library only.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout, protocol version as in mcp_stdio.py.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_stdio import PROTOCOL_VERSION

SERVER_NAME = "guardian-mailbox"
SERVER_VERSION = "0.1.0"
SERVER_KEY = "mailbox"  # the name in the mcp config, so tools appear to claude as mcp__mailbox__<tool>
DEFAULT_TIMEOUT = 25.0
MAX_LINE_BYTES = 1024 * 1024  # one line each way on the agent socket, as on the relay stream
MAX_PATCH_BYTES = 256 * 1024  # one task_update's patch (O3); above it the call answers patch_too_large
MAX_ARTIFACT_BYTES = 512 * 1024  # one artifact_publish payload (PD4); fits in 1 MiB NDJSON after base64
WORKTREE_GIT_TIMEOUT = 120.0
TASK_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # a task id becomes a directory name under work/tasks
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
UPDATE_SCOPES = ("index", "security", "upgrade")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Every git call of the worktree layer (O3): a fixed identity, no system config, never a prompt.
GIT_ARGS = ("-c", "user.name=guardian-agent", "-c", "user.email=agent@invalid")
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

KINDS = ("message", "question", "answer", "task_offer", "task_result")
STATUSES = ("completed", "failed")

# tool -> (description, JSON schema of the arguments, the argument names forwarded to the daemon)
TOOLS: Dict[str, Dict[str, Any]] = {
    "mail_inbox": {
        "description": "Unread messages for you, oldest first. Each message stays until your turn ends normally.",
        "arguments": (),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "mail_send": {
        "description": "Send one message to one address. One call per recipient; there is no broadcast.",
        "arguments": ("to", "subject", "body", "kind", "task", "reply_to", "thread"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient address, e.g. claude@node-1."},
                "subject": {"type": "string", "description": "One line."},
                "body": {"type": "string", "description": "The message text."},
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "Default message."},
                "task": {"type": "string", "description": "Task id this message is about."},
                "reply_to": {"type": "string", "description": "Message id this answers."},
                "thread": {"type": "string", "description": "Thread id to keep."},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    },
    "mail_agents": {
        "description": "The agents you may write to, with their last known status.",
        "arguments": (),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "task_list": {
        "description": "Open tasks you may see: your own, those assigned to you, unassigned ones you may "
                       "claim, and those you created.",
        "arguments": (),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "task_create": {
        "description": "Create a task with a title and a detail that says what done looks like.",
        "arguments": ("title", "detail", "assignee", "blocked_by", "project"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "One line."},
                "detail": {"type": "string", "description": "What to do and what done looks like."},
                "assignee": {"type": "string", "description": "Address of the agent to do it (optional)."},
                "blocked_by": {"type": "array", "items": {"type": "string"},
                               "description": "Task ids that must complete first (optional)."},
                "project": {"type": "string", "description": "Project id, when the task is about one."},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    "task_claim": {
        "description": "Claim a pending task assigned to you or unassigned. Returns the task's generation.",
        "arguments": ("task",),
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Task id."}},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "task_update": {
        "description": "With the generation from the claim: completed with a result, or failed with a reason.",
        "arguments": ("task", "generation", "status", "result", "reason"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task id."},
                "generation": {"type": "integer", "description": "The generation returned by task_claim."},
                "status": {"type": "string", "enum": list(STATUSES)},
                "result": {"type": "string", "description": "The result text (completed)."},
                "reason": {"type": "string", "description": "Why it failed (failed)."},
            },
            "required": ["task", "generation", "status"],
            "additionalProperties": False,
        },
    },
    "task_release": {
        "description": "With the generation from the claim: give the task back to pending, with a reason.",
        "arguments": ("task", "generation", "reason"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task id."},
                "generation": {"type": "integer", "description": "The generation returned by task_claim."},
                "reason": {"type": "string", "description": "Why you give it back."},
            },
            "required": ["task", "generation", "reason"],
            "additionalProperties": False,
        },
    },
}

# The tools whose calls reach the hub: they need an operation id (idempotence on resend). mail_inbox
# is answered on the node, so it needs none.
HUB_TOOLS = tuple(name for name in TOOLS if name != "mail_inbox")

# task_worktree (O3), artifact_publish (PD4) and ops_propose_* (PD3-a) exist only with --worktrees
# (open agents); the closed tool list above stays exactly as it is.
WORKTREE_TOOLS: Dict[str, Dict[str, Any]] = {
    "task_worktree": {
        "description": "Clone the snapshot of a task you own into your own work directory as a git "
                       "working copy you may edit, build and test in; the base commit comes back with it.",
        "arguments": ("task", "generation"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task id."},
                "generation": {"type": "integer", "description": "The generation returned by task_claim."},
            },
            "required": ["task", "generation"],
            "additionalProperties": False,
        },
    },
    "artifact_publish": {
        "description": "Publish one deliverable file from your $HOME/out or $HOME/work directory to "
                       "the hub's persistent artifact store so the operator and peers can retrieve it.",
        "arguments": ("path", "title", "task", "summary"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path inside $HOME/out or $HOME/work (relative or absolute)."},
                "title": {"type": "string", "description": "Short human-readable title for the artifact."},
                "task": {"type": "string", "description": "Optional task id this artifact belongs to."},
                "summary": {"type": "string", "description": "Optional short description of the deliverable."},
            },
            "required": ["path", "title"],
            "additionalProperties": False,
        },
    },
    "ops_propose_package": {
        "description": "Propose installing OS or tool packages on THIS node into the hub's Approvals "
                       "queue (PROPOSED ops:package_install; never runs until approved on the hub).",
        "arguments": ("manager", "packages", "reason", "task"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "manager": {"type": "string", "description": "Package manager: auto, apt, npm, pipx, uv, etc."},
                "packages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "version": {"type": "string"},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    "description": "List of packages to install, e.g. [{\"name\": \"ripgrep\"}].",
                },
                "reason": {"type": "string", "description": "Why these packages are needed."},
                "task": {"type": "string", "description": "Optional task id this proposal supports."},
            },
            "required": ["manager", "packages", "reason"],
            "additionalProperties": False,
        },
    },
    "ops_propose_update": {
        "description": "Propose an OS package index refresh or upgrade on THIS node into the hub's "
                       "Approvals queue (PROPOSED ops:system_update; never runs until approved on the hub).",
        "arguments": ("scope", "reason", "task"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": list(UPDATE_SCOPES),
                          "description": "index (apt-get update), security, or upgrade."},
                "reason": {"type": "string", "description": "Why this update is proposed."},
                "task": {"type": "string", "description": "Optional task id this proposal supports."},
            },
            "required": ["scope", "reason"],
            "additionalProperties": False,
        },
    },
}

# Open-agent tools whose calls reach the hub (need an op_id for idempotence on resend).
OPEN_HUB_TOOLS = ("artifact_publish", "ops_propose_package", "ops_propose_update")


def tool_specs(worktrees: bool = False) -> Dict[str, Dict[str, Any]]:
    """The tools this server offers: the closed set, plus task_worktree in --worktrees mode (O3)."""
    return {**TOOLS, **WORKTREE_TOOLS} if worktrees else TOOLS


def allowed_tool_names(worktrees: bool = False) -> List[str]:
    """The names claude sees (mcp__mailbox__mail_send, ...), for --allowedTools and the mcp config."""
    return [f"mcp__{SERVER_KEY}__{name}" for name in tool_specs(worktrees)]


def _input_error(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Why the arguments are not acceptable, or None. A bad call never touches the socket."""

    def text(field: str) -> bool:
        return isinstance(args.get(field), str) and bool(args[field])

    def whole(field: str) -> bool:
        return isinstance(args.get(field), int) and not isinstance(args.get(field), bool)

    if name == "mail_send":
        for field in ("to", "subject", "body"):
            if not text(field):
                return f"{field} must be a non-empty string"
        if args.get("kind") is not None and args.get("kind") not in KINDS:
            return "kind must be one of " + ", ".join(KINDS)
        for field in ("task", "reply_to", "thread"):
            if args.get(field) is not None and not isinstance(args.get(field), str):
                return f"{field} must be a string"
    elif name == "task_create":
        if not text("title"):
            return "title must be a non-empty string"
        for field in ("detail", "assignee", "project"):
            if args.get(field) is not None and not isinstance(args.get(field), str):
                return f"{field} must be a string"
        blocked = args.get("blocked_by")
        if blocked is not None and (not isinstance(blocked, list)
                                    or any(not isinstance(item, str) for item in blocked)):
            return "blocked_by must be an array of task ids"
    elif name == "task_claim":
        if not text("task"):
            return "task must be a non-empty string"
    elif name == "task_update":
        if not text("task"):
            return "task must be a non-empty string"
        if not whole("generation"):
            return "generation must be an integer"
        if args.get("status") not in STATUSES:
            return "status must be one of " + ", ".join(STATUSES)
        for field in ("result", "reason"):
            if args.get(field) is not None and not isinstance(args.get(field), str):
                return f"{field} must be a string"
    elif name == "task_release":
        if not text("task"):
            return "task must be a non-empty string"
        if not whole("generation"):
            return "generation must be an integer"
        if not text("reason"):
            return "reason must be a non-empty string"
    elif name == "task_worktree":
        task = args.get("task")
        if not isinstance(task, str) or not TASK_RE.fullmatch(task):
            return "task must be a task id"
        if not whole("generation"):
            return "generation must be an integer"
    elif name == "artifact_publish":
        if not text("path"):
            return "path must be a non-empty string"
        if not text("title"):
            return "title must be a non-empty string"
        for field in ("task", "summary"):
            if args.get(field) is not None and not isinstance(args.get(field), str):
                return f"{field} must be a string"
    elif name == "ops_propose_package":
        if not text("manager"):
            return "manager must be a non-empty string"
        pkgs = args.get("packages")
        if not isinstance(pkgs, list) or not pkgs or any(
                not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p.get("name")
                for p in pkgs):
            return "packages must be a non-empty list of objects with name"
        if not text("reason"):
            return "reason must be a non-empty string"
        if args.get("task") is not None and not isinstance(args.get("task"), str):
            return "task must be a string"
    elif name == "ops_propose_update":
        if args.get("scope") not in UPDATE_SCOPES:
            return "scope must be one of " + ", ".join(UPDATE_SCOPES)
        if not text("reason"):
            return "reason must be a non-empty string"
        if args.get("task") is not None and not isinstance(args.get("task"), str):
            return "task must be a string"
    return None


class MailboxMcpServer:
    """JSON-RPC handler. handle() takes one decoded message and returns the response (None for
    notifications), the same shape as RepoMcpServer in repo_mcp.py.

    worktrees=True turns on the O3 layer: task_worktree is offered and a completed task_update
    carries the collected patch. `home` overrides $HOME (the work directory root); tests inject it."""

    def __init__(self, socket_path: str, log_path: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT, worktrees: bool = False,
                 home: Optional[str] = None) -> None:
        self.socket_path = socket_path
        self.log_path = log_path
        self.timeout = timeout
        self.worktrees = worktrees
        self.tools = tool_specs(worktrees)
        self.home = home if home is not None else os.environ.get("HOME", "")

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

    def _fail(self, name: str, error: str, size: int = 0) -> Dict[str, Any]:
        """One error answer, logged, in the closed server's error shape."""
        self._log(name, ok=False, error=error, size=size)
        return {"isError": True, "content": [{"type": "text", "text": f"error: {error}"}]}

    def call_tool(self, name: str, arguments: Any) -> Dict[str, Any]:
        """One mailbox tool call: validate first (a bad call never touches the socket), then one
        round trip on the agent socket. In --worktrees mode task_worktree is served around its
        daemon call (clone), a completed task_update carries the collected patch, and an accepted
        task_update or task_release removes the task's worktree."""
        spec = self.tools.get(name)
        if spec is None:
            return {"isError": True, "content": [{"type": "text", "text": f"error: unknown tool {name!r}"}]}
        args = arguments if isinstance(arguments, dict) else {}
        problem = _input_error(name, args)
        if problem is not None:
            return self._fail(name, problem)
        request: Dict[str, Any] = {"tool": name, "args": {k: args[k] for k in spec["arguments"] if k in args}}
        if name in HUB_TOOLS or name in OPEN_HUB_TOOLS:
            # one operation id per call: the relay may resend this call after a reconnect, and the hub
            # applies it once; a second call by the agent is a new operation
            request["op_id"] = uuid.uuid4().hex
        if self.worktrees and name == "task_worktree":
            return self._task_worktree(request)
        if self.worktrees and name == "artifact_publish":
            payload_args, art_problem = self._collect_artifact(args)
            if art_problem is not None:
                return self._fail(name, art_problem)
            request["args"] = payload_args
        if self.worktrees and name == "task_update" and args.get("status") == "completed":
            extra, patch_problem = self._collect_patch(args.get("task"))
            if patch_problem is not None:
                return self._fail(name, patch_problem)
            request["args"].update(extra)
        reply, size = self._ask(request)
        if reply is None:
            return self._fail(name, "mailbox not available", size)
        if reply.get("ok") is False:
            error = str(reply.get("error") or "refused")
            detail = reply.get("detail")
            text = f"error: {error}: {detail}" if detail else f"error: {error}"
            self._log(name, ok=False, error=error, size=size)
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        self._log(name, ok=True, error=None, size=size)
        if self.worktrees and name in ("task_update", "task_release"):
            self._remove_worktree(args.get("task"))
        payload = json.dumps({k: v for k, v in reply.items() if k != "ok"}, ensure_ascii=False)
        return {"isError": False, "content": [{"type": "text", "text": payload}]}

    # --- the worktree layer (O3, --worktrees only) ----------------------------------------------

    def _git(self, args: List[str], cwd: Optional[Path] = None) -> Optional[subprocess.CompletedProcess]:
        """One git call of the worktree layer: the fixed identity flags, no system config, never a
        prompt, no shell, a timeout. None when git could not run at all."""
        env = {**os.environ, **GIT_ENV}
        try:
            return subprocess.run(["git", *GIT_ARGS, *args], cwd=str(cwd) if cwd is not None else None,
                                  capture_output=True, timeout=WORKTREE_GIT_TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError):
            return None

    def _workroot(self) -> Optional[Path]:
        """$HOME/work/tasks, the worktrees' root; None when there is no home to work under."""
        return Path(self.home) / "work" / "tasks" if self.home else None

    def _base_file(self, task: str) -> Path:
        root = self._workroot()
        assert root is not None
        return root / f".{task}.base"

    @staticmethod
    def _read_base(path: Path) -> Optional[str]:
        """The recorded base commit of a worktree, or None when it is missing or not a commit."""
        try:
            text = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return text if COMMIT_RE.fullmatch(text) else None

    def _task_worktree(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """task_worktree: the daemon writes the bundle, this server clones it into the agent's own
        work directory (kept between turns, never cloned over) and answers path and base commit."""
        name, task = "task_worktree", request["args"]["task"]
        reply, size = self._ask(request)
        if reply is None:
            return self._fail(name, "mailbox not available", size)
        if reply.get("ok") is False:
            error = str(reply.get("error") or "refused")
            detail = reply.get("detail")
            self._log(name, ok=False, error=error, size=size)
            text = f"error: {error}: {detail}" if detail else f"error: {error}"
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        workroot = self._workroot()
        if workroot is None:
            return self._fail(name, "no home directory to work in", size)
        work = workroot / task
        commit = str(reply.get("commit") or "")
        if work.is_dir() and not work.is_symlink():
            recorded = self._read_base(self._base_file(task))
            if recorded is None:
                return self._fail(name, "worktree base missing; remove the worktree and clone again", size)
            commit = recorded  # the agent's own work is never cloned over
        else:
            problem = self._clone_worktree(str(reply.get("bundle") or ""), work, commit)
            if problem is not None:
                return self._fail(name, problem, size)
        self._log(name, ok=True, error=None, size=size)
        payload = json.dumps({"path": str(work), "base": commit}, ensure_ascii=False)
        return {"isError": False, "content": [{"type": "text", "text": payload}]}

    def _clone_worktree(self, bundle: str, work: Path, commit: str) -> Optional[str]:
        """Clones the daemon's bundle into the work directory, checks the snapshot commit out and
        records it as the diff base. The problem name on failure, None on success."""
        if not bundle or not COMMIT_RE.fullmatch(commit):
            return "worktree bundle missing"
        try:
            work.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return "work directory not writable"
        if work.exists() or work.is_symlink():
            return "worktree already exists"
        done = self._git(["clone", "--quiet", "--no-hardlinks", bundle, str(work)])
        if done is None or done.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            return "worktree clone failed"
        checked = self._git(["checkout", "--quiet", commit], cwd=work)
        if checked is None or checked.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            return "worktree checkout failed"
        try:
            self._base_file(work.name).write_text(commit + "\n", encoding="ascii")
        except OSError:
            shutil.rmtree(work, ignore_errors=True)
            return "worktree base not writable"
        return None

    def _collect_patch(self, task: Any) -> Tuple[Dict[str, str], Optional[str]]:
        """(extra task_update arguments, problem) of a completed update in --worktrees mode: git's
        own diff of the task's worktree against its base, as patch/patch_base (both omitted when the
        diff is empty; no worktree means nothing to collect). The patch comes only from the
        worktree, never from the caller, and is capped at MAX_PATCH_BYTES."""
        if not isinstance(task, str) or not TASK_RE.fullmatch(task) or not self.home:
            return {}, None
        workroot = self._workroot()
        assert workroot is not None
        work = workroot / task
        if work.is_symlink() or not work.is_dir():
            return {}, None
        base = self._read_base(workroot / f".{task}.base")
        if base is None:
            return {}, "worktree base missing; remove the worktree and clone again"
        added = self._git(["add", "-A"], cwd=work)
        if added is None or added.returncode != 0:
            return {}, "worktree add failed"
        diff = self._git(["diff", "--cached", "--binary", "--no-renames", base], cwd=work)
        if diff is None or diff.returncode != 0:
            return {}, "worktree diff failed"
        if not diff.stdout.strip():
            return {}, None
        if len(diff.stdout) > MAX_PATCH_BYTES:
            return {}, "patch_too_large"
        try:
            return {"patch": diff.stdout.decode("utf-8"), "patch_base": base}, None
        except UnicodeDecodeError:
            return {}, "worktree diff not text"

    def _collect_artifact(self, args: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        """Read one regular file strictly inside $HOME/out or $HOME/work (no symlink traversal,
        O_NOFOLLOW, capped at MAX_ARTIFACT_BYTES) and build the hub artifact_publish args."""
        if not self.home:
            return {}, "no home directory"
        raw_path = str(args.get("path") or "").strip()
        if not raw_path or "\0" in raw_path:
            return {}, "bad_path"
        home = Path(self.home)
        allowed_roots = [home / "out", home / "work"]
        candidate = Path(raw_path) if os.path.isabs(raw_path) else (home / "out" / raw_path)
        if not os.path.isabs(raw_path) and not candidate.exists():
            alt = home / "work" / raw_path
            if alt.exists():
                candidate = alt
        # Refuse any symlink component between the allowed root and the file.
        matched_root: Optional[Path] = None
        for root in allowed_roots:
            try:
                rel = candidate.relative_to(root)
            except ValueError:
                continue
            if any(part in ("", "..") for part in rel.parts):
                return {}, "path_outside_allowed_dirs"
            cur = root
            for part in rel.parts:
                cur = cur / part
                try:
                    info = cur.lstat()
                except OSError:
                    return {}, "file_not_found"
                if stat.S_ISLNK(info.st_mode):
                    return {}, "symlink_not_allowed"
            matched_root = root
            rel_text = f"{root.name}/{'/'.join(rel.parts)}"
            break
        if matched_root is None:
            return {}, "path must be inside $HOME/out or $HOME/work"
        try:
            fd = os.open(str(candidate), os.O_RDONLY | _O_NOFOLLOW)
        except OSError:
            return {}, "file_not_found"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return {}, "not_a_regular_file"
            if info.st_size == 0:
                return {}, "empty_artifact"
            if info.st_size > MAX_ARTIFACT_BYTES:
                return {}, "artifact_too_large"
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    return {}, "artifact_too_large"
                chunks.append(chunk)
        except OSError:
            return {}, "read_failed"
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        filename = candidate.name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out: Dict[str, Any] = {
            "filename": filename,
            "rel_path": rel_text,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "mime": mime,
            "title": args["title"],
        }
        if args.get("task") is not None:
            out["task"] = args["task"]
        if args.get("summary") is not None:
            out["summary"] = args["summary"]
        return out, None

    def _remove_worktree(self, task: Any) -> None:
        """After an accepted task_update or task_release (O3): the task's worktree and its base file
        go, so a re-claimed task starts from a fresh clone. A symlink is unlinked, never followed,
        and nothing that resolves outside work/tasks is touched."""
        if not isinstance(task, str) or not TASK_RE.fullmatch(task) or not self.home:
            return
        workroot = self._workroot()
        assert workroot is not None
        work, base = workroot / task, workroot / f".{task}.base"
        try:
            resolved_root = str(workroot.resolve())
            if work.is_symlink():
                work.unlink()
            elif work.is_dir() and os.path.commonpath([str(work.resolve()), resolved_root]) == resolved_root:
                shutil.rmtree(work, ignore_errors=True)
            if base.is_symlink() or base.is_file():
                base.unlink()
        except (OSError, ValueError):
            pass

    def _log(self, tool: str, ok: bool, error: Optional[str], size: int) -> None:
        """One line per call; never the message text."""
        if not self.log_path:
            return
        record = json.dumps({"tool": tool, "ok": ok, "error": error, "bytes": size}) + "\n"
        try:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, record.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            print(f"mailbox_mcp: cannot write log: {exc}", file=sys.stderr)

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
                for name, spec in self.tools.items()
            ]})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in self.tools:
                return _error(request_id, -32602, f"unknown tool {name!r}")
            try:
                return _ok(request_id, self.call_tool(name, params.get("arguments")))
            except Exception as exc:  # noqa: BLE001 - a broken call must not end the server
                print(f"mailbox_mcp: {name} failed: {exc!r}", file=sys.stderr)
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
    parser.add_argument("--log", default=None, help="JSONL file that receives one record per call (never the message text)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds for one call (default 25)")
    parser.add_argument("--worktrees", action="store_true",
                        help="open agents (O3): offer task_worktree and collect the task's patch "
                             "from $HOME/work/tasks on task_update")
    args = parser.parse_args(argv)
    if len(args.socket.encode("utf-8")) > 103:  # sockaddr_un.sun_path minus the NUL on the smallest system
        print("mailbox_mcp: the socket path is too long for a unix socket", file=sys.stderr)
        return 2
    MailboxMcpServer(args.socket, args.log, timeout=args.timeout, worktrees=args.worktrees).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The resident tasks of one node (docs/design/resident-agents.md, M2e): the owned-task table and the repo router.

The hub pins a task that names a project to a W2 snapshot and tells the node through `task_state` and
`tasks` frames what each agent owns; the table lives in agents/<name>/tasks.json. The five read-only
snapshot tools are answered on the node from the snapshot of the task named in the call, only while the
caller owns it at the current generation, and every call appends to the per-task log
agents/<name>/tasks/<task id>.calls.jsonl, which the daemon attaches to task_update. O3's task_worktree
(open agents) goes through the same table: owned_entry checks the task and generation, and a task that
leaves the table loses its worktree bundle through the table's drop callback. Nothing here talks
to the hub: the frames arrive through resident_node.py, the agent calls on the agent socket.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

import repo_mcp
from repo_mcp import RepoMcpServer
from repo_snapshot import is_key, snapshot_commit, snapshot_path
from repo_tools import RepoTools

REPO_TOOLS = repo_mcp.SNAPSHOT_TOOLS
REPO_WAIT_SECONDS = 15.0  # a repo call waits this long for a snapshot the hub is still uploading
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # a task id becomes a file name (no traversal)
STATES = ("pending", "ready", "none")
TOOLS_CACHE = 4  # RepoTools instances held at once, one per snapshot key


def _write_json(path: Path, payload: Any) -> None:
    """Atomic write (temp file + rename, 0600); the parent directory must exist."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _valid_generation(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def tasks_path(agent_dir: Path) -> Path:
    return Path(agent_dir) / "tasks.json"


def calls_path(agent_dir: Path, task: str) -> Path:
    return Path(agent_dir) / "tasks" / f"{task}.calls.jsonl"


def parse_owned(owned: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """The `tasks` frame's `owned` map validated, or None when it is not well formed (resident_node
    refuses such a frame as bad_input)."""
    if not isinstance(owned, dict):
        return None
    parsed: Dict[str, Dict[str, Any]] = {}
    for task, spec in owned.items():
        if not isinstance(task, str) or not TASK_ID_RE.fullmatch(task) or not isinstance(spec, dict):
            return None
        generation, state, key = spec.get("generation"), spec.get("state"), spec.get("key")
        if not _valid_generation(generation) or state not in STATES:
            return None
        if state == "ready":
            if not is_key(key):
                return None
        elif key is not None and not is_key(key):
            return None
        parsed[task] = {"generation": generation, "state": state, "key": key}
    return parsed


def apply_frame(kind: str, table: "TaskTable", frame: Dict[str, Any]) -> Optional[str]:
    """One resident-task frame (task_state or tasks) applied to the table; None when accepted, else
    the refusal reason (resident_node answers the hub with it)."""
    if kind == "task_state":
        return apply_task_state(table, frame.get("task"), frame.get("generation"),
                                frame.get("state"), frame.get("key"))
    if kind == "tasks":
        return apply_tasks(table, frame.get("owned"))
    return "unknown_frame"


def apply_task_state(table: "TaskTable", task: Any, generation: Any, state: Any, key: Any) -> Optional[str]:
    """One `task_state` frame applied to the table; None when accepted, else the refusal reason."""
    if (not isinstance(task, str) or not TASK_ID_RE.fullmatch(task) or not _valid_generation(generation)
            or state not in ("ready", "released") or (state == "ready" and not is_key(key))):
        return "bad_input"
    table.set_state(task, generation, state, key if state == "ready" else None)
    return None


def apply_tasks(table: "TaskTable", owned: Any) -> Optional[str]:
    """One `tasks` frame applied to the table; None when accepted, else the refusal reason."""
    parsed = parse_owned(owned)
    if parsed is None:
        return "bad_input"
    table.replace(parsed)
    return None


def owned_entry(table: "TaskTable", task: Any, generation: Any,
                wait_seconds: float = REPO_WAIT_SECONDS) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(problem, entry) of a task the caller must own at exactly `generation` (O3's task_worktree).
    The entry never comes back pending: a pending snapshot is waited for like the repo router does
    (a wait that runs out answers snapshot_pending, a task that left the table task_released). An
    unknown task or another generation answers not_owner; a bad id or generation bad_input."""
    if not isinstance(task, str) or not TASK_ID_RE.fullmatch(task) or not _valid_generation(generation):
        return "bad_input", None
    entry = table.entry(task)
    if entry is None or entry["generation"] != generation:
        return "not_owner", None
    if entry["state"] == "pending":
        entry = table.wait_ready(task, wait_seconds)
        if entry["state"] == "gone":
            return "task_released", None
        if entry["generation"] != generation:
            return "not_owner", None
        if entry["state"] == "pending":
            return "snapshot_pending", None
    return None, entry


class TaskTable:
    """One agent's owned tasks, persisted to agents/<name>/tasks.json. The condition wakes the repo
    calls waiting for a pending snapshot. Frames may arrive in any order: the reader thread can hand
    over a `task_state ready` before the claim reply reaches the agent thread, so `ready` creates an
    entry and `claimed` never downgrades one of the same generation."""

    def __init__(self, agent_dir: Path, tasks: Optional[Dict[str, Dict[str, Any]]] = None,
                 on_drop: Optional[Callable[[str], None]] = None) -> None:
        self.agent_dir = Path(agent_dir)
        self._cond = threading.Condition(threading.Lock())
        self._tasks: Dict[str, Dict[str, Any]] = dict(tasks or {})
        self._on_drop = on_drop  # O3: called with every task that leaves the table

    @classmethod
    def load(cls, agent_dir: Path, on_drop: Optional[Callable[[str], None]] = None) -> "TaskTable":
        """Reads agents/<name>/tasks.json (creating nothing); a broken file reads as no tasks."""
        data = _read_json(tasks_path(agent_dir), {})
        if not isinstance(data, dict):
            data = {}
        tasks = {task: entry for task, entry in data.items() if isinstance(task, str)
                 and TASK_ID_RE.fullmatch(task) and parse_owned({task: entry}) is not None}
        return cls(agent_dir, tasks, on_drop=on_drop)

    def _save(self) -> None:
        try:
            _write_json(tasks_path(self.agent_dir), self._tasks)
        except OSError:
            pass  # a vanished agent directory takes the table with it

    def _dropped(self, task: str) -> None:
        """One task left the table; the drop callback (O3: the worktree bundle goes with the log)
        runs outside the condition and never raises over the table."""
        if self._on_drop is not None:
            try:
                self._on_drop(task)
            except Exception:  # noqa: BLE001 - a callback defect must not break the table
                pass

    def entry(self, task: str) -> Optional[Dict[str, Any]]:
        with self._cond:
            record = self._tasks.get(task)
            return dict(record) if record is not None else None

    def entries(self) -> Dict[str, Dict[str, Any]]:
        with self._cond:
            return {task: dict(entry) for task, entry in self._tasks.items()}

    def replace(self, owned: Dict[str, Dict[str, Any]]) -> None:
        """The `tasks` frame: the full set of the agent's in-progress tasks. Tasks absent from it are
        dropped together with their call logs."""
        with self._cond:
            gone = [task for task in self._tasks if task not in owned]
            self._tasks = dict(owned)
            if gone or owned:
                self._save()
            self._cond.notify_all()
        for task in gone:
            _unlink_quiet(calls_path(self.agent_dir, task))
            self._dropped(task)

    def set_state(self, task: str, generation: int, state: str, key: Optional[str]) -> None:
        """The `task_state` frame. `released` removes the entry and its log; a frame for an older
        generation than the table holds is ignored; `ready` for a task not yet in the table creates
        the entry."""
        with self._cond:
            current = self._tasks.get(task)
            if current is not None and generation < current["generation"]:
                return
            if state == "released":
                if current is not None:
                    del self._tasks[task]
                    self._save()
                self._cond.notify_all()
            else:  # ready (with the snapshot key) or pending
                self._tasks[task] = {"generation": generation, "state": state,
                                     "key": key if state == "ready" else None}
                self._save()
                self._cond.notify_all()
        if state == "released":
            _unlink_quiet(calls_path(self.agent_dir, task))
            if current is not None:
                self._dropped(task)

    def claimed(self, task: Any, generation: Any, snapshot: Any, key: Any) -> None:
        """An ok `task_claim` reply: snapshot is "none" (the task has no project), "ready" (with key)
        or "pending" (the hub is still ensuring the snapshot). Never downgrades a `ready` entry of the
        same generation to pending (the reader thread may have delivered task_state ready first)."""
        if not isinstance(task, str) or not TASK_ID_RE.fullmatch(task) or not _valid_generation(generation):
            return
        state = snapshot if snapshot in STATES else "pending"
        with self._cond:
            current = self._tasks.get(task)
            if current is not None and generation < current["generation"]:
                return
            if (current is not None and current["generation"] == generation
                    and state == "pending" and current["state"] == "ready"):
                return
            self._tasks[task] = {"generation": generation, "state": state,
                                 "key": key if state == "ready" and is_key(key) else None}
            self._save()
            self._cond.notify_all()

    def drop(self, task: str) -> None:
        """Removes the entry and its call log (task_update accepted, task_release accepted, release)."""
        with self._cond:
            present = task in self._tasks
            if present:
                del self._tasks[task]
                self._save()
            self._cond.notify_all()
        _unlink_quiet(calls_path(self.agent_dir, task))
        if present:
            self._dropped(task)

    def wait_ready(self, task: str, timeout: float, clock: Any = time.monotonic) -> Dict[str, Any]:
        """The task's entry once it is not pending, waiting at most `timeout`: {"state": "gone"} when
        the task left the table (released), {"state": "pending"} when the wait ran out, else the entry."""
        deadline = clock() + timeout
        with self._cond:
            while True:
                entry = self._tasks.get(task)
                if entry is None:
                    return {"state": "gone", "key": None}
                if entry["state"] != "pending":
                    return dict(entry)
                left = deadline - clock()
                if left <= 0:
                    return {"state": "pending", "key": None}
                self._cond.wait(left)


def pinned_keys(agents_root: Path) -> FrozenSet[str]:
    """Every snapshot key an owned task pins: every key in every agents/*/tasks.json. A broken file
    pins nothing and never raises (the prune must not fail over it); no agents root pins nothing."""
    try:
        children = [child for child in Path(agents_root).iterdir() if child.is_dir()]
    except OSError:
        return frozenset()
    found: set = set()
    for child in children:
        data = _read_json(tasks_path(child), None)
        if not isinstance(data, dict):
            continue
        for entry in data.values():
            if isinstance(entry, dict) and is_key(entry.get("key")):
                found.add(entry["key"])
    return frozenset(found)


class RepoRouter:
    """Answers the five snapshot repo tools from the snapshot of the task the caller owns. The router
    is stateless apart from a small RepoTools cache per snapshot key."""

    def __init__(self, snapshot_root: Optional[Path] = None, wait_seconds: float = REPO_WAIT_SECONDS) -> None:
        self._root = Path(snapshot_root) if snapshot_root is not None else None
        self.wait_seconds = wait_seconds
        self._tools: Dict[str, RepoTools] = {}
        self._tools_lock = threading.Lock()

    def _snapshot_root(self) -> Path:
        if self._root is None:
            from worker import snapshot_root  # lazy: worker imports resident_tasks lazily too
            self._root = Path(snapshot_root())
        return self._root

    def _repo_tools(self, key: str, commit: str) -> RepoTools:
        with self._tools_lock:
            tools = self._tools.get(key)
            if tools is None:
                tools = RepoTools(snapshot_path(self._snapshot_root(), key), commit, sha_label=commit)
                if len(self._tools) >= TOOLS_CACHE:
                    self._tools.pop(next(iter(self._tools)))
                self._tools[key] = tools
            return tools

    def stored_snapshot(self, key: Any) -> Optional[Tuple[Path, str]]:
        """(git dir, commit) of a stored snapshot, marked used now so pruning keeps it, or None when
        the key is not a key or this worker does not hold it. The worktree bundle of O3 is built
        from the same bare repository the repo tools read."""
        if not is_key(key):
            return None
        commit = snapshot_commit(self._snapshot_root(), key, touch=True)
        if commit is None:
            return None
        return snapshot_path(self._snapshot_root(), key), commit

    def call(self, agent_dir: Path, table: TaskTable, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """One repo tool call with a `task` argument. Everything but a ready snapshot is answered as
        {"ok": false, "error": ...}; the tool result comes back wrapped as {"ok": true, "result": ...}."""
        if tool not in REPO_TOOLS:
            return {"ok": False, "error": "unknown_tool"}
        task = args.get("task")
        if not isinstance(task, str) or not TASK_ID_RE.fullmatch(task):
            return {"ok": False, "error": "bad_input"}
        rest = {k: v for k, v in args.items() if k != "task"}
        entry = table.entry(task)
        if entry is None:
            return {"ok": False, "error": "no_task"}
        if entry["state"] == "pending":
            entry = table.wait_ready(task, self.wait_seconds)
            if entry["state"] == "gone":
                return {"ok": False, "error": "task_released"}
            if entry["state"] == "pending":
                return {"ok": False, "error": "snapshot_pending"}
        if entry["state"] == "none":
            return {"ok": False, "error": "no_project"}
        key = entry.get("key")
        if not is_key(key):
            return {"ok": False, "error": "snapshot_missing"}
        commit = snapshot_commit(self._snapshot_root(), key, touch=True)
        if commit is None:
            return {"ok": False, "error": "snapshot_missing"}
        log_dir = Path(agent_dir) / "tasks"
        try:
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        server = RepoMcpServer(self._repo_tools(key, commit), log_path=calls_path(agent_dir, task),
                               no_index=True)
        return {"ok": True, "result": server.call_tool(tool, rest)}


def read_calls(agent_dir: Path, task: str) -> Dict[str, Any]:
    """The call log of one task as the daemon attaches it to task_update: worker.read_calls semantics
    (too large or unreadable -> dropped; a missing file -> empty, not dropped)."""
    from worker import read_calls as worker_read_calls
    records = worker_read_calls(calls_path(agent_dir, task))
    if records is None:
        return {"records": [], "dropped": True}
    return {"records": records, "dropped": False}

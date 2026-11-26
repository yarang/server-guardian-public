"""Task snapshots for resident agents, hub side (docs/design/resident-agents.md, M2e).

A task that names a project is pinned to that project's commit at creation (resolve_project_sha).
The claim itself stays fast and atomic: the hub answers `snapshot: "pending"` and prepares the W2
snapshot in the background (SnapshotEnsurer: build on the hub, `snapshot_has`, then the upload over
the worker protocol's own ssh call, never over the relay stream); when it is ready the relay sends
the daemon a `task_state ready` frame, and when it failed the hub releases the task itself. The
call log the daemon attaches to `task_update` is checked against the snapshot's file list before it
is stored with the result (filter_calls), so a later filing can show which lines the agent saw.

Standard library only. remote_agents.py is imported, never modified: the upload path, its timeouts
and MAX_CALLS_BYTES are the debate workers' own.
"""

import json
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

import remote_agents

MiB = 1024 * 1024
# The fields a call record may keep (exactly what repo_mcp.RepoMcpServer writes to its log).
CALL_KEYS = ("op", "target", "ok", "bytes", "truncated", "error", "paths", "spans", "mentions", "limit")
# How many (project, sha) path sets the ensurer remembers before it drops the oldest.
ALLOWED_CACHE = 8


class TaskSnapshotError(RuntimeError):
    """A task project or snapshot cannot be resolved, built or uploaded; the message is one short line."""


def _short(text: Any, limit: int = 160) -> str:
    """One short line for a reason that travels to the node and into the store: no traceback."""
    return " ".join(str(text).split())[:limit] or "unknown error"


def _build_snapshot(tools: Any, directory: Path) -> Any:
    """The real snapshot builder (repo_snapshot.build_snapshot); SnapshotEnsurer(builder=...) replaces it."""
    from repo_snapshot import build_snapshot

    return build_snapshot(tools, directory)


def _load_project(project_id: Any):
    """The project a task names, or TaskSnapshotError when it is unknown."""
    from project_registry import ProjectManager, ProjectValidationError

    try:
        return ProjectManager().load(project_id)
    except ProjectValidationError as exc:
        raise TaskSnapshotError(_short(exc)) from None


def resolve_project_sha(project_id: Any, sha: Any = None) -> Tuple[str, str]:
    """(project_id, full_sha): the commit a task on this project is pinned to (HEAD when sha is None).

    Used at task creation, on the relay and in the CLI: the store then holds the full sha, never a
    moving ref. Refuses what is unknown, has no workspace, or has no commit."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise TaskSnapshotError("a task project must be a project id")
    if sha is not None and not isinstance(sha, str):
        raise TaskSnapshotError("a task sha must be a ref")
    project = _load_project(project_id)
    if not project.has_workspace:
        raise TaskSnapshotError(f"project {project_id} is {project.kind_label}: it has no git workspace")
    from code_context import ContextError, head_sha, resolve_sha

    try:
        full = resolve_sha(project.workspace, sha) if sha else head_sha(project.workspace)
    except ContextError as exc:
        raise TaskSnapshotError(_short(exc)) from None
    if not full:
        raise TaskSnapshotError(f"project {project_id} has no commit at HEAD")
    return project_id, full


def filter_calls(calls: Any, allowed: FrozenSet[str]) -> Dict[str, Any]:
    """Checks the daemon's task call log before the store keeps it: only dict records, only the known
    fields, and only paths the snapshot holds (the evidence rules trust what the log says the agent
    was shown, so a path outside the snapshot is dropped, not believed). Stops adding records once
    they would pass remote_agents.MAX_CALLS_BYTES."""
    if not (isinstance(calls, dict) and isinstance(calls.get("records"), list)):
        return {"records": [], "dropped": True, "filtered": 0}
    allowed = allowed or frozenset()
    records: List[Dict[str, Any]] = []
    dropped, removed, total = bool(calls.get("dropped")), 0, 0
    for record in calls["records"]:
        if not isinstance(record, dict):
            continue
        kept = {key: record[key] for key in CALL_KEYS if key in record}
        paths = record.get("paths") if isinstance(record.get("paths"), list) else []
        keep_paths = [p for p in paths if isinstance(p, str) and p in allowed]
        removed += len([p for p in paths if isinstance(p, str)]) - len(keep_paths)
        kept["paths"] = keep_paths
        spans = record.get("spans")
        if isinstance(spans, dict):
            keep_spans = {p: v for p, v in spans.items() if p in allowed}
            removed += len(spans) - len(keep_spans)
            kept["spans"] = keep_spans
        else:
            kept["spans"] = {}
        mentions = record.get("mentions")
        if isinstance(mentions, list):
            keep_mentions = [m for m in mentions if isinstance(m, str) and m in allowed]
            removed += len([m for m in mentions if isinstance(m, str)]) - len(keep_mentions)
            kept["mentions"] = keep_mentions
        else:
            kept["mentions"] = []
        line = json.dumps(kept, ensure_ascii=False)
        if total + len(line) > remote_agents.MAX_CALLS_BYTES:
            dropped = True
            break
        total += len(line)
        records.append(kept)
    return {"records": records, "dropped": dropped, "filtered": removed}


class SnapshotEnsurer:
    """The hub's background preparation of one relay node's task snapshots.

    ensure() blocks (a relay worker thread calls it) and is the only path that builds or uploads;
    known_key() and allowed_paths() never touch ssh and never build a snapshot, so the relay's pump
    thread may call them between two frames."""

    def __init__(self, node: str, transport: Any = None, protocol_info: Optional[Dict[str, Any]] = None,
                 clock: Callable[[], float] = time.monotonic, builder: Any = None):
        self.node = node
        self.transport = transport if transport is not None else remote_agents.SshTransport()
        self.protocol_info = protocol_info
        self.clock = clock
        self._builder = builder
        self._lock = threading.Lock()
        self._keys: Dict[Tuple[str, str], str] = {}  # (project, sha) -> key of the snapshot placed
        self._present: Set[str] = set()              # keys this node is known to hold
        self._allowed: "OrderedDict[Tuple[str, str], FrozenSet[str]]" = OrderedDict()

    # --- remembering ------------------------------------------------------------------------

    def _protocol(self) -> int:
        if self.protocol_info is None:
            return remote_agents.PROTOCOL
        return remote_agents.request_protocol(self.protocol_info)

    def _remember(self, pair: Tuple[str, str], key: Optional[str], allowed: FrozenSet[str]) -> None:
        """key None only fills the path cache (allowed_paths of a pair never ensured here)."""
        with self._lock:
            if key is not None:
                self._present.add(key)
                self._keys[pair] = key
            self._allowed[pair] = allowed
            self._allowed.move_to_end(pair)
            while len(self._allowed) > ALLOWED_CACHE:
                self._allowed.popitem(last=False)

    def known_key(self, project_id: str, sha: str) -> Optional[str]:
        """The key of a snapshot this ensurer already placed on the node in this process (no ssh, no build)."""
        with self._lock:
            return self._keys.get((project_id, sha))

    def allowed_paths(self, project_id: str, sha: str) -> FrozenSet[str]:
        """The set of paths the task's snapshot holds. Filled by ensure(); on a miss (a relay restart)
        it must not build one, so it returns RepoTools.readable_paths() alone: a safe superset, the
        size-cap filter only ever removes paths from it."""
        pair = (project_id, sha)
        with self._lock:
            cached = self._allowed.get(pair)
            if cached is not None:
                self._allowed.move_to_end(pair)
                return cached
        paths = frozenset(self._tools(project_id, sha).readable_paths())
        self._remember(pair, None, paths)  # no key known yet: only the paths are remembered
        return paths

    # --- preparing --------------------------------------------------------------------------

    def _tools(self, project_id: str, sha: str) -> Any:
        """RepoTools of the project's workspace at the pinned sha, as the W2 router builds it."""
        from repo_tools import RepoTools

        try:
            return RepoTools(_load_project(project_id).workspace, sha)
        except TaskSnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001 - ContextError, OSError: a short reason is enough
            raise TaskSnapshotError(f"cannot read the repository: {_short(exc)}") from None

    def _check_and_upload(self, snapshot: Any) -> None:
        """`snapshot_has`, then the upload when the node lacks the key (the W2 worker path)."""
        reply = self.transport.call(self.node, {"op": "snapshot_has", "protocol": self._protocol(),
                                                "key": snapshot.key}, timeout=15.0)
        if not reply.get("ok"):
            raise TaskSnapshotError(f"snapshot check refused: "
                                    f"{_short(reply.get('error') or 'unknown reason')}")
        if reply.get("present"):
            return
        timeout = remote_agents.UPLOAD_BASE_SECONDS + remote_agents.UPLOAD_SECONDS_PER_MIB * snapshot.size / MiB
        done = self.transport.upload(self.node, snapshot.key, snapshot.path, timeout=timeout)
        if not done.get("ok"):
            raise TaskSnapshotError(f"snapshot upload refused: "
                                    f"{_short(done.get('error') or 'unknown reason')}")

    def ensure(self, project_id: str, sha: str) -> Tuple[str, FrozenSet[str]]:
        """(key, allowed_paths); blocking. Builds the snapshot of the pinned commit, checks the node
        for it and uploads it when missing; the hub's temp copy always goes away afterwards."""
        pair = (project_id, sha)
        with self._lock:
            known = self._keys.get(pair)
            if known is not None and known in self._present and pair in self._allowed:
                return known, self._allowed[pair]
        tmpdir = tempfile.mkdtemp(prefix="guardian_tasksnap_")  # 0700
        try:
            tools = self._tools(project_id, sha)
            builder = self._builder or _build_snapshot
            with self._lock:
                present = set(self._present)
            try:
                snapshot = builder(tools, Path(tmpdir))
                allowed = frozenset(set(tools.readable_paths()) - set(snapshot.dropped))
                if snapshot.key not in present:
                    self._check_and_upload(snapshot)
            except TaskSnapshotError:
                raise
            except remote_agents.WorkerError as exc:
                raise TaskSnapshotError(_short(exc)) from None
            except Exception as exc:  # noqa: BLE001 - SnapshotError, ContextError, OSError
                raise TaskSnapshotError(f"cannot build the snapshot: {_short(exc)}") from None
            self._remember(pair, snapshot.key, allowed)
            return snapshot.key, allowed
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)  # the node keeps its copy, the hub does not

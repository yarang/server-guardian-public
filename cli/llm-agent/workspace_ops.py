"""
Workspace reset and publication listing for the hub API (docs/design/debate-teams-api.md, decisions D12/D13).

reset_plan_json reports what a reset would do; reset_apply_json does it. A reset discards stray changes in a
project's workspace clone but never the files that EXECUTED, not-yet-published file actions wrote (those are
waiting for publish). Only the listed paths are touched: never `git checkout -- .`, `git clean -fd` or `git add -A`.
"""

import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from action_ops import (ACTOR_PATTERN, CONFLICT, FILE_TOOLS, INVALID_INPUT, NOT_FOUND, THREAD_ID_PATTERN, Result,
                        _error)
from db_manager import DecisionDBManager
from mcp_client import GUARDIAN_ROOT
from project_registry import PROJECT_ID_PATTERN, ProjectValidationError, WorkspaceBusy, workspace_lock
from publish_ops import _Git


MAX_RELEASE = 50  # threads one reset may release


class ResetRefused(RuntimeError):
    """A precondition does not hold; nothing was changed."""


def _changed(git: _Git) -> Dict[str, str]:
    """Every changed or untracked path (rename sources included) -> 'modified' | 'untracked' | 'deleted'."""
    code, out, err = git.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code != 0:
        raise ResetRefused(f"git status failed: {err}")
    entries = out.split(b"\0")
    raw: List[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2].decode("ascii", "replace"), entry[3:].decode("utf-8", "surrogateescape")
        raw.append(path)
        if "R" in xy or "C" in xy:  # a rename or copy entry is followed by its source path
            if i < len(entries) and entries[i]:
                raw.append(entries[i].decode("utf-8", "surrogateescape"))
            i += 1
    paths = sorted({p for p in raw if p.split("/")[0] != ".git"})
    if not paths:
        return {}
    code, out, err = git.run("--literal-pathspecs", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *paths)
    if code != 0:
        raise ResetRefused(f"git ls-tree failed: {err}")
    in_head = {n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n}
    states: Dict[str, str] = {}
    for path in paths:
        if path not in in_head:
            states[path] = "untracked"
        elif not (git.cwd / path).exists():
            states[path] = "deleted"
        else:
            states[path] = "modified"
    return states


def _unpublished(db: DecisionDBManager, project_id: str) -> List[Dict[str, Any]]:
    """EXECUTED file actions of the project whose thread has no PUSHED publication and was not released,
    in the order they wrote their files (applied_at_ns, then id)."""
    published: Dict[str, bool] = {}
    done: List[Dict[str, Any]] = []
    for action in db.list_actions(status="EXECUTED", project_id=project_id):
        result = action.get("result")
        if action["tool"] not in FILE_TOOLS or not isinstance(result, dict) or not isinstance(result.get("file_hashes"), dict):
            continue
        if result.get("released_at"):  # the operator gave these files up (reset --release)
            continue
        thread = action["thread_id"]
        if thread not in published:
            published[thread] = any(p["status"] == "PUSHED" for p in db.list_publications(thread))
        if not published[thread]:
            done.append(action)
    return sorted(done, key=lambda a: (a["result"].get("applied_at_ns", 0), a["id"]))


def _owners(done: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """path -> the latest of the given actions that wrote it."""
    owners: Dict[str, Dict[str, Any]] = {}
    for action in done:
        for path in action["result"]["file_hashes"]:
            owners[path] = action
    return owners


def _context(workspaces: Any, project_id: str) -> Tuple[Any, _Git]:
    try:
        mcp = workspaces.for_project(project_id)
        project = workspaces.manager.load(project_id)
    except ValueError as exc:  # ProjectValidationError is a ValueError
        raise LookupError(str(exc)) from exc
    workspace = mcp.workspace
    if workspace is None:
        raise ResetRefused(f"project {project_id} is {project.kind_label} and has no workspace to reset")
    if workspace == GUARDIAN_ROOT.resolve():
        raise ResetRefused("the workspace is the running Server Guardian checkout, not a separate clone")
    if not (workspace / ".git").exists():
        raise ResetRefused(f"project {project_id}: the workspace {workspace} has no clone (run --project-sync)")
    git = _Git(workspace)
    top = git.text("rev-parse", "--show-toplevel")
    if top is None:
        raise ResetRefused(f"{workspace} is not a git repository")
    if Path(top).resolve() != workspace:
        raise ResetRefused(f"{workspace} is not the top of a git repository")
    current = git.text("symbolic-ref", "-q", "--short", "HEAD")
    if current is None:
        raise ResetRefused("the workspace is on a detached HEAD")
    if current != project.branch:
        raise ResetRefused(f"the workspace is on '{current}', not the tracked branch '{project.branch}'")
    return mcp, git


def _plan(db: DecisionDBManager, git: _Git, project_id: str) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    changed = _changed(git)
    owners = _owners(_unpublished(db, project_id))
    keep = [{"path": p, "action_id": owners[p]["id"], "thread_id": owners[p]["thread_id"]}
            for p in sorted(changed) if p in owners]
    discard = [{"path": p, "state": s} for p, s in sorted(changed.items()) if p not in owners]
    return discard, keep


def _release(db: DecisionDBManager, project_id: str, threads: List[str], actor: str) -> None:
    """Marks the EXECUTED file actions of each thread as released (status unchanged), so their files are no
    longer kept by a reset and the thread can never be published. Refuses a thread with nothing to release."""
    done = _unpublished(db, project_id)
    held = {a["thread_id"] for a in done}
    missing = [t for t in threads if t not in held]
    if missing:
        raise ResetRefused("nothing to release for " + ", ".join(missing)
                           + f": no executed, unpublished file changes of that thread in project {project_id}")
    released_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for action in done:
        if action["thread_id"] in threads:
            db.set_action_result(action["id"], {**action["result"], "released_by": actor, "released_at": released_at})


def _check_input(project_id: str) -> Result:
    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.match(project_id):
        return _error(INVALID_INPUT, "project: use 1-64 lowercase letters, digits, - or _")
    return 0, {}


def _refuse_running(db: DecisionDBManager, project_id: str) -> None:
    """A RUNNING action may be writing (or testing) a file right now; a reset must wait for it to settle."""
    running = db.list_actions(status="RUNNING", project_id=project_id)
    if running:
        raise ResetRefused("actions are running in this project: " + ", ".join(f"#{a['id']}" for a in running)
                           + " (wait for them, or mark a stuck one failed with --fail-stale-action)")


def reset_plan_json(db: DecisionDBManager, workspaces: Any, project_id: str) -> Result:
    """Exit 0 {"ok", "project_id", "workspace", "discard": [{path, state}], "keep": [{path, action_id, thread_id}]};
    2 bad project id, 3 unknown project, 4 a precondition refuses (including running actions)."""
    code, payload = _check_input(project_id)
    if code:
        return code, payload
    try:
        mcp, git = _context(workspaces, project_id)
        _refuse_running(db, project_id)
        discard, keep = _plan(db, git, project_id)
    except LookupError as exc:
        return _error(NOT_FOUND, str(exc))
    except ResetRefused as exc:
        return _error(CONFLICT, str(exc))
    return 0, {"ok": True, "project_id": project_id, "workspace": str(mcp.workspace), "discard": discard, "keep": keep}


def reset_apply_json(db: DecisionDBManager, workspaces: Any, project_id: str, actor: str,
                     release: Sequence[str] = ()) -> Result:
    """Discards exactly the plan's discard list under the workspace lock. Exit 0 {"ok": true, "discarded": [paths],
    "released": [threads]}; 2 bad input, 3 unknown project, 4 busy or a precondition refuses.

    release names threads whose kept files are given up too: each of their EXECUTED file actions records
    released_by / released_at in its result (status unchanged), and the thread can never be published.
    """
    code, payload = _check_input(project_id)
    if code:
        return code, payload
    if not ACTOR_PATTERN.match(actor):
        return _error(INVALID_INPUT, "actor: use 1-64 letters, digits or @:._-")
    if (isinstance(release, (str, bytes)) or not all(isinstance(t, str) and t for t in release)
            or len(set(release)) > MAX_RELEASE):
        return _error(INVALID_INPUT, f"release: a list of at most {MAX_RELEASE} thread ids")
    threads = sorted(set(release))
    try:
        mcp, git = _context(workspaces, project_id)
        _refuse_running(db, project_id)
        with workspace_lock(mcp.workspace, "another publish, project sync or action is running in this workspace"):
            _refuse_running(db, project_id)  # file actions hold this lock while RUNNING, so none can start now
            if threads:  # before any git step: whatever happens next, a released thread never publishes
                _release(db, project_id, threads, actor)
            discard, _ = _plan(db, git, project_id)  # recomputed under the lock
            tracked = [d["path"] for d in discard if d["state"] != "untracked"]
            fresh = [d["path"] for d in discard if d["state"] == "untracked"]
            steps: List[Tuple[str, ...]] = []
            if tracked:
                steps.append(("--literal-pathspecs", "checkout", "HEAD", "--", *tracked))
            if fresh:
                steps.append(("--literal-pathspecs", "rm", "-q", "-r", "-f", "--cached", "--ignore-unmatch", "--", *fresh))
                steps.append(("--literal-pathspecs", "clean", "-q", "-f", "--", *fresh))
            for step in steps:
                rc, _, err = git.run(*step)
                if rc != 0:
                    raise ResetRefused(f"git {step[1]} failed: {err}")
    except LookupError as exc:
        return _error(NOT_FOUND, str(exc))
    except WorkspaceBusy as exc:
        return _error(CONFLICT, str(exc))
    except ProjectValidationError as exc:
        return _error(CONFLICT, str(exc))
    except ResetRefused as exc:
        return _error(CONFLICT, str(exc))
    return 0, {"ok": True, "discarded": [d["path"] for d in discard], "released": threads}


PUBLICATION_FIELDS = ("id", "branch", "commit_sha", "remote", "status", "error", "actor", "created_at")


def publications_json(db: DecisionDBManager, thread_id: str) -> Result:
    """Exit 0 {"publications": [...]} newest first; 3 for an unknown thread."""
    if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.match(thread_id):
        return _error(INVALID_INPUT, "thread: invalid id")
    if db.get_thread_status(thread_id) is None:
        return _error(NOT_FOUND, f"thread {thread_id} not found")
    rows = sorted(db.list_publications(thread_id), key=lambda r: r["id"], reverse=True)
    return 0, {"publications": [{k: row.get(k) for k in PUBLICATION_FIELDS} for row in rows]}

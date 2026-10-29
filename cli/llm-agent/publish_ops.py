"""
Operator-triggered publish of a thread's executed file actions (docs/design/publish-thread.md).

Executed file actions leave UNCOMMITTED changes in the project's workspace clone. publish_thread
turns exactly those changes into one commit on a new branch and pushes that branch to the project's
repository, then puts the workspace back on the tracked branch, clean. Every precondition is checked
before the first git command that changes anything, so a refusal has no side effects.

Safety rules: only the files the actions wrote are added (never `git add -A`), the branch is new and
never the tracked or a protected branch, the push is never forced, git runs without a shell and with
the tool environment (no tokens), and credentials come from the hub's git credential helper.
"""

import hashlib
import os
import re
import subprocess
import textwrap
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import patch_ops
from action_ops import ACTOR_PATTERN, CONFLICT, FILE_TOOLS, INVALID_INPUT, NOT_FOUND, Result, last_file_hashes
from db_manager import DecisionDBManager
from mcp_client import GUARDIAN_ROOT, TEST_TIMEOUT, ActionValidationError, MCPClient
from project_registry import ProjectValidationError, workspace_lock

GIT_TIMEOUT = 60
PUSH_TIMEOUT = 300
SUBJECT_MAX = 72
BODY_MAX = 3000
OUTPUT_TAIL = 4000
# Action states that settle without a person's pending decision; anything else (PROPOSED, HELD, RUNNING) blocks.
SETTLED_STATUSES = ("EXECUTED", "FAILED", "REJECTED", "INVALID")
# Variables that would point git at a different repository than the workspace.
GIT_REDIRECT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")


class PublishRefused(RuntimeError):
    """A precondition does not hold; nothing was changed. details carries extra output (e.g. the test run)."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


class PublishInputError(ValueError):
    """The operator's input (the branch name) is not acceptable."""


class _Git:
    """git in the workspace: no shell, a timeout per call, the tool environment, never a prompt."""

    def __init__(self, workspace: Path) -> None:
        env = MCPClient.tool_environ()
        for name in GIT_REDIRECT_ENV:
            env.pop(name, None)
        self.env = {**env, "GIT_TERMINAL_PROMPT": "0"}
        self.cwd = workspace

    def run(self, *args: str, timeout: int = GIT_TIMEOUT) -> Tuple[int, bytes, str]:
        """(exit code, stdout bytes, stderr text); -1 when git cannot run or times out."""
        try:
            proc = subprocess.run(["git", *args], cwd=self.cwd, capture_output=True, timeout=timeout, env=self.env)
        except (OSError, subprocess.SubprocessError) as exc:
            return -1, b"", str(exc)
        return proc.returncode, proc.stdout, scrub(proc.stderr.decode("utf-8", errors="replace"))

    def text(self, *args: str, timeout: int = GIT_TIMEOUT) -> Optional[str]:
        """Stripped stdout of a command that succeeded, else None."""
        code, out, _ = self.run(*args, timeout=timeout)
        return out.decode("utf-8", errors="replace").strip() if code == 0 else None


def scrub(text: str) -> str:
    """Last OUTPUT_TAIL characters of git output with any credentials in a URL masked."""
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", text)[-OUTPUT_TAIL:].strip()


def commit_message(thread_id: str, summary: str) -> Tuple[str, str, str]:
    """(subject, body, trailer): the subject is `fix: ` plus the summary's first line, at most 72 characters."""
    clean = "".join(ch for ch in summary if ch == "\n" or (ch >= " " and ch != "\x7f"))
    first = next((" ".join(line.split()) for line in clean.splitlines() if line.strip()), "")
    first = first.rstrip(".:;, ")
    subject = f"fix: {first}" if first else f"fix: changes from debate {thread_id}"
    if len(subject) > SUBJECT_MAX:
        cut = subject[: SUBJECT_MAX - 3]
        if " " in cut[12:]:
            cut = cut.rsplit(" ", 1)[0]
        subject = cut.rstrip(" .,;:") + "..."
    paragraphs = [" ".join(p.split()) for p in clean[:BODY_MAX].split("\n\n") if p.strip()]
    body = "\n\n".join(textwrap.fill(p, width=72, break_long_words=False, break_on_hyphens=False) for p in paragraphs)
    return subject, body, f"Debate-Thread: {thread_id}"


def _changed_paths(git: _Git) -> Tuple[Set[str], List[str]]:
    """(paths git status reports including untracked files, paths with staged changes)."""
    code, out, err = git.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code != 0:
        raise PublishRefused(f"git status failed: {err}")
    entries = out.split(b"\0")
    paths: Set[str] = set()
    staged: List[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2].decode("ascii", "replace"), entry[3:].decode("utf-8", "surrogateescape")
        paths.add(path)
        if "R" in xy or "C" in xy:  # a rename or copy entry is followed by its source path
            if i < len(entries) and entries[i]:
                paths.add(entries[i].decode("utf-8", "surrogateescape"))
            i += 1
        if xy[0] not in " ?":
            staged.append(path)
    return paths, staged


def _check_worktree(git: _Git, workspace: Path, expected: Dict[str, str]) -> None:
    """The changed paths equal the files the actions wrote, unstaged, each with its last recorded hash."""
    changed, staged = _changed_paths(git)
    if staged:
        raise PublishRefused("changes are already staged: " + ", ".join(sorted(staged)))
    missing, extra = sorted(set(expected) - changed), sorted(changed - set(expected))
    if missing or extra:
        parts = []
        if extra:
            parts.append("changed in the workspace but not written by the executed actions: " + ", ".join(extra))
        if missing:
            parts.append("written by the executed actions but not changed in the workspace: " + ", ".join(missing))
        raise PublishRefused("; ".join(parts))
    stale = sorted(p for p, h in expected.items() if patch_ops.content_hash(workspace / p) != h)
    if stale:
        raise PublishRefused("files changed after the last action wrote them: " + ", ".join(stale))


def _check_existing_branch(git: _Git, tip: str, base: str, expected: Dict[str, str]) -> None:
    """A local branch from an earlier attempt holds exactly one commit on base with exactly the recorded files."""
    if git.text("rev-parse", f"{tip}^") != base or git.text("rev-list", "--count", f"{base}..{tip}") != "1":
        raise PublishRefused("the branch exists locally but is not a single commit on the base commit")
    code, out, err = git.run("diff-tree", "-r", "--no-commit-id", "--name-only", "--no-renames", "-z", base, tip)
    names = {n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n}
    if code != 0 or names != set(expected):
        raise PublishRefused("the branch exists locally but its commit does not hold exactly the files of the actions")
    for path, digest in expected.items():
        code, blob, _ = git.run("cat-file", "blob", f"{tip}:{path}")
        if code != 0 or hashlib.sha256(blob).hexdigest() != digest:
            raise PublishRefused(f"the branch exists locally but {path} differs from what the action wrote")


@contextmanager
def _publish_lock(git: _Git) -> Iterator[None]:
    """One publish (or project sync) per workspace at a time (flock on a file in the git dir)."""
    try:
        with workspace_lock(git.cwd, "another publish or project sync is running in this workspace"):
            yield
    except ProjectValidationError as exc:
        raise PublishRefused(str(exc)) from None


def _resolve(workspaces: Any, project_id: str, project: Any) -> Tuple[MCPClient, Any]:
    """The project's MCPClient and Project: from Workspaces, or a plain MCPClient plus an explicit project."""
    if hasattr(workspaces, "for_project"):
        try:
            mcp, proj = workspaces.for_project(project_id), workspaces.manager.load(project_id)
        except ValueError as exc:
            raise PublishRefused(f"project {project_id!r}: {exc}") from exc
        if not getattr(proj, "has_workspace", True):
            raise PublishRefused(f"project {project_id} is {proj.kind_label}: there is no repository to publish to")
        return mcp, proj
    if project is None:
        raise PublishRefused(f"project {project_id!r}: pass the Project together with a bare MCPClient")
    return workspaces, project


def _validate_branch(name: str, tracked: str) -> None:
    try:
        MCPClient._check_branch(name)
    except ActionValidationError as exc:
        raise PublishInputError(str(exc)) from exc
    if name == tracked:
        raise PublishInputError(f"refusing to publish to the tracked branch '{tracked}'")


# @MX:WARN: [AUTO] Creates a branch and a commit in the workspace and pushes it to the project's remote.
# @MX:REASON: Reached only through an operator's explicit publish; the push is never forced and the exact file set is verified first.
def _push(git: _Git, remote: str, branch: str) -> Tuple[bool, str]:
    """git push <remote> <branch>:refs/heads/<branch>; no force, no upstream, credentials from the helper."""
    code, _, err = git.run("push", "--", remote, f"{branch}:refs/heads/{branch}", timeout=PUSH_TIMEOUT)
    return code == 0, err or f"git push exited {code}"


# @MX:ANCHOR: [AUTO] Entry point of the publish step for the CLI and the hub route.
# @MX:REASON: Callers rely on it refusing without side effects and always leaving the workspace on the tracked branch.
def publish_thread(
    db: DecisionDBManager,
    workspaces: Any,
    thread_id: str,
    actor: str,
    branch: Optional[str] = None,
    *,
    project: Any = None,
    remote: Optional[str] = None,
    confirm: Optional[Callable[[Dict[str, Any]], bool]] = None,
    plan_only: bool = False,
) -> Dict[str, Any]:
    """Commits a thread's executed file changes to a new branch and pushes it.

    workspaces is project_registry.Workspaces (or one MCPClient plus project=). remote replaces the project's
    repo URL as the push target (tests only; the project must still have a repo). confirm(plan) is asked once
    all cheap preconditions hold, before the project's test runs.

    plan_only runs every precondition (all that come before the test gate) and returns {"status": "PLAN",
    **plan} without confirming, testing, or writing anything: no git command that changes state, no database
    row. An interrupted earlier publish (see below) is treated as already recovered.

    Under the workspace lock, a publish that was killed halfway is cleaned up first: the thread's RUNNING
    attempts become FAILED ("interrupted") and, if the workspace is still on a branch such an attempt created
    and the worktree is clean, the tracked branch is checked out again; a retry then pushes the kept commit.

    Returns {"status": "PUSHED" | "FAILED", "branch", "commit_sha", "remote", "files", "publication_id", ...};
    FAILED carries stderr and leaves the local branch for a retry. Raises LookupError (unknown thread),
    PublishInputError (bad branch) or PublishRefused (a precondition fails; nothing was changed).
    """
    if db.get_thread_status(thread_id) is None:
        raise LookupError(f"thread {thread_id} not found")
    attempts = db.list_publications(thread_id)
    done_before = [a for a in attempts if a["status"] == "PUSHED"]
    if done_before:
        raise PublishRefused(f"thread {thread_id} is already published as branch {done_before[-1]['branch']} "
                             f"({(done_before[-1]['commit_sha'] or '')[:12]})")

    actions = db.list_actions(thread_id)
    undecided = [a for a in actions if a["status"] not in SETTLED_STATUSES]
    if undecided:
        raise PublishRefused("actions are still undecided: " + ", ".join(f"#{a['id']} {a['status']}" for a in undecided))
    files = [a for a in actions if a["status"] == "EXECUTED" and a["tool"] in FILE_TOOLS]
    if not files:
        raise PublishRefused(f"thread {thread_id} has no executed file action to publish")
    released = [a for a in files if isinstance(a.get("result"), dict) and a["result"].get("released_at")]
    if released:
        raise PublishRefused(f"thread {thread_id} was released: {released[0]['result'].get('released_by')} gave up its "
                             f"workspace files at {released[0]['result']['released_at']} (workspace reset --release); "
                             "start a new thread")
    unrecorded = [a["id"] for a in files
                  if not (isinstance(a.get("result"), dict) and isinstance(a["result"].get("file_hashes"), dict)
                          and a["result"]["file_hashes"])]
    if unrecorded:
        raise PublishRefused("executed actions without recorded file hashes: " + ", ".join(f"#{i}" for i in unrecorded))
    projects = {a.get("project_id") or "" for a in files}
    bases = {a.get("base_sha") or "" for a in files}
    if len(projects) != 1 or "" in projects:
        raise PublishRefused("the executed file actions belong to different projects")
    if len(bases) != 1 or "" in bases:
        raise PublishRefused("the executed file actions do not share one base commit")
    base = bases.pop()
    mcp, proj = _resolve(workspaces, projects.pop(), project)
    if not getattr(proj, "repo", None):
        raise PublishRefused(f"project {proj.id} has no repo URL to push to")
    _check_gate_configured(mcp)
    target = remote or proj.repo

    name = branch or f"guardian/{thread_id}"
    _validate_branch(name, proj.branch)
    expected = last_file_hashes(files)

    workspace = mcp.workspace
    if workspace == GUARDIAN_ROOT.resolve():
        raise PublishRefused("the workspace is the running Server Guardian checkout, not a separate clone")
    git = _Git(workspace)
    top = git.text("rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != workspace:
        raise PublishRefused(f"{workspace} is not the top of a git repository")
    code, _, _ = git.run("check-ref-format", "--branch", name)
    if code != 0:
        raise PublishInputError(f"invalid branch name: {name}")

    # The read-only plan never takes the exclusive lock: opening the dashboard dialog must not make a real
    # publish or sync fail as busy. It only reads repository state.
    with (nullcontext() if plan_only else _publish_lock(git)):
        current = git.text("symbolic-ref", "-q", "--short", "HEAD")
        head: Optional[str] = None
        stuck = [a for a in attempts if a["status"] == "RUNNING"]  # live only in plan mode (a real publish holds the lock)
        if stuck:
            on_stuck_branch = (current is not None and current != proj.branch
                               and current in {a["branch"] for a in stuck} and not _changed_paths(git)[0])
            if plan_only:
                if on_stuck_branch:  # report what the real publish would do after its recovery
                    current, head = proj.branch, git.text("rev-parse", f"refs/heads/{proj.branch}")
            else:
                for attempt in stuck:
                    db.update_publication(attempt["id"], status="FAILED",
                                          error="interrupted: the process ended before this attempt finished")
                attempts = db.list_publications(thread_id)
                if on_stuck_branch:
                    code, _, err = git.run("checkout", "-q", proj.branch)
                    if code != 0:
                        raise PublishRefused(f"could not check out '{proj.branch}' after an interrupted publish: {err}")
                    current = proj.branch
        if current is None:
            raise PublishRefused("the workspace is on a detached HEAD")
        if current != proj.branch:
            raise PublishRefused(f"the workspace is on '{current}', not the tracked branch '{proj.branch}'")
        head = head or git.text("rev-parse", "HEAD")
        if head != base:
            raise PublishRefused(f"the workspace moved from {base[:12]} to {(head or 'unknown')[:12]} since the actions ran")

        tip = git.text("rev-parse", "--verify", "-q", f"refs/heads/{name}")
        if tip:
            # Retry of an attempt whose push failed: the commit exists, only the push is missing.
            record = [a for a in attempts if a["branch"] == name and a["status"] != "PUSHED"
                      and a["commit_sha"] in (None, tip)]
            if not record:
                raise PublishRefused(f"branch '{name}' already exists locally and is not from an earlier publish of this thread")
            _check_existing_branch(git, tip, base, expected)
            changed, _ = _changed_paths(git)
            if changed:
                raise PublishRefused("the workspace has uncommitted changes: " + ", ".join(sorted(changed)))
        else:
            _check_worktree(git, workspace, expected)

        subject, body, trailer = commit_message(thread_id, db.decision_text(thread_id))
        plan = {"thread_id": thread_id, "project_id": proj.id, "branch": name, "remote": target, "base_sha": base,
                "files": sorted(expected), "subject": subject, "retry": bool(tip)}
        if plan_only:
            if stuck:
                plan["note"] = ("a publish of this thread is recorded as RUNNING; it may be running now or may have "
                                "been interrupted. A real publish takes the workspace lock first")
            return {"status": "PLAN", **plan}
        if confirm is not None and not confirm(plan):
            return {"status": "SKIPPED", **plan}

        tests: Dict[str, Any] = {}
        if not tip:
            tests = _run_gate(mcp)
            _check_worktree(git, workspace, expected)  # the test run must not have changed the files

        publication = db.add_publication(thread_id, proj.id, name, tip, target, "RUNNING", actor)
        result = {**plan, "publication_id": publication, "commit_sha": tip, "tests": tests}
        try:
            error = _commit_and_push(git, db, publication, plan, result, (subject, body, trailer), proj.branch)
        finally:
            back = _return_to(git, proj.branch)
        if back:
            error = f"{error + '; ' if error else ''}{back}"
        if error:
            db.update_publication(publication, status="FAILED", error=error)
            return {**result, "status": "FAILED", "error": error, "stderr": error}
        db.update_publication(publication, status="PUSHED")
        return {**result, "status": "PUSHED"}


def _check_gate_configured(mcp: MCPClient) -> None:
    """Refuses before anything is touched when the project has no usable default test to gate on."""
    try:
        mcp.default_test_argv()
    except ActionValidationError as exc:
        raise PublishRefused(f"the project's test cannot run: {exc}") from exc


def _run_gate(mcp: MCPClient) -> Dict[str, Any]:
    """The project's default test (the one execute_code_action runs with run_test) in the workspace;
    refuses when it fails or cannot run."""
    try:
        res = mcp.run_default_test(TEST_TIMEOUT)
    except ActionValidationError as exc:
        raise PublishRefused(f"the project's test cannot run: {exc}") from exc
    summary = {"cmd": res["cmd"], "exit_code": res["exit_code"], "steps": res.get("steps", [])}
    if res["exit_code"] != 0:
        where = f"step {len(summary['steps'])} " if len(summary["steps"]) > 1 else ""
        raise PublishRefused(f"the project's test {where}failed (exit {res['exit_code']}): {summary['cmd']}",
                             {"tests": {**summary, "stdout": res["stdout"], "stderr": res["stderr"]}})
    return summary


def _commit_and_push(git: _Git, db: DecisionDBManager, publication: int, plan: Dict[str, Any],
                     result: Dict[str, Any], message: Tuple[str, str, str], tracked: str) -> str:
    """Creates the branch and commit when needed, then pushes. Returns an error text, '' on success."""
    subject, body, trailer = message
    name, files = plan["branch"], plan["files"]
    if not result["commit_sha"]:
        code, _, err = git.run("checkout", "-b", name)
        if code != 0:
            return f"git checkout -b {name} failed: {err}"
        steps = [("add", "--", *files), ("commit", "-m", subject, *(["-m", body] if body else []), "-m", trailer)]
        for step in steps:
            code, _, err = git.run(*step)
            if code != 0:
                # Nothing was committed: unstage, go back and drop the empty branch, so a retry starts clean.
                git.run("reset", "-q", "--", *files)
                git.run("checkout", "-q", tracked)
                git.run("branch", "-D", name)
                return f"git {step[0]} failed: {err}"
        sha = git.text("rev-parse", "HEAD")
        result["commit_sha"] = sha
        db.update_publication(publication, commit_sha=sha)
        code, out, _ = git.run("diff-tree", "-r", "--no-commit-id", "--name-only", "--no-renames", "-z", "HEAD")
        if {n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n} != set(files):
            return f"the commit {str(sha)[:12]} does not hold exactly the expected files; not pushed"
    ok, err = _push(git, plan["remote"], name)
    return "" if ok else f"git push failed: {err}"


def _return_to(git: _Git, tracked: str) -> str:
    """Checks out the tracked branch again; returns an error text when that fails."""
    if git.text("symbolic-ref", "-q", "--short", "HEAD") == tracked:
        return ""
    code, _, err = git.run("checkout", "-q", tracked)
    return "" if code == 0 else f"could not check out '{tracked}' again: {err}"


# --- JSON command for the hub API ----------------------------------------------------------------

def publish_thread_json(db: DecisionDBManager, workspaces: Any, thread_id: str, actor: str,
                        branch: Optional[str] = None, **kwargs: Any) -> Result:
    """Publishes a thread; the caller has already shown the plan to the person who confirmed it.

    Exit 0 with ok=true (PUSHED) or ok=false (FAILED: the commit stays local, stderr says why); 2 for a bad
    actor or branch, 3 for an unknown thread, 4 when a precondition refuses (extra fields such as `tests`).
    """
    if not ACTOR_PATTERN.match(actor):
        return INVALID_INPUT, {"ok": False, "error": "actor: use 1-64 letters, digits or @:._-"}
    try:
        outcome = publish_thread(db, workspaces, thread_id, actor, branch or None, **kwargs)
    except LookupError as exc:
        return NOT_FOUND, {"ok": False, "error": str(exc)}
    except PublishInputError as exc:
        return INVALID_INPUT, {"ok": False, "error": str(exc)}
    except PublishRefused as exc:
        return CONFLICT, {"ok": False, "error": str(exc), **exc.details}
    return 0, {"ok": outcome["status"] == "PUSHED", **outcome}


def publish_plan_json(db: DecisionDBManager, workspaces: Any, thread_id: str, branch: Optional[str] = None,
                      **kwargs: Any) -> Result:
    """Dry run of publish_thread: every precondition before the test gate, no confirmation, no write.

    Exit 0 with {"ok": true, "plan": {thread_id, project_id, branch, remote, base_sha, files, subject, retry}};
    2 for a bad branch, 3 for an unknown thread, 4 {"ok": false, "error"} when a precondition refuses.
    """
    try:
        outcome = publish_thread(db, workspaces, thread_id, "plan", branch or None, plan_only=True, **kwargs)
    except LookupError as exc:
        return NOT_FOUND, {"ok": False, "error": str(exc)}
    except PublishInputError as exc:
        return INVALID_INPUT, {"ok": False, "error": str(exc)}
    except PublishRefused as exc:
        return CONFLICT, {"ok": False, "error": str(exc)}
    return 0, {"ok": True, "plan": {k: v for k, v in outcome.items() if k != "status"}}

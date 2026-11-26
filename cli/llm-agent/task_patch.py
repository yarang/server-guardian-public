"""Patches an open resident agent attaches to task_update (resident-open-tools.md, O3, rule 5).

An open agent edits a git worktree built in its sandbox from the hub's W2 snapshot of the task's
pinned commit; the result leaves the node only as `git diff --cached --binary --no-renames <base>`
text attached to task_update, with the 40-hex commit the clone started at. The hub runs nothing:
convert() turns the patch into apply_patch edits, and the relay records them as ONE PROPOSED
jev_mcp:apply_patch action in the existing approvals path (db_manager.add_action). Approval stays
with a person: a task has no debate verdict, so the jev gate and `auto:engine` never apply (OD4;
the structural rule lives in action_ops).

Both fields are untrusted input from a sandboxed agent. convert() never touches the workspace's
working tree, HEAD, index or worktree list: the patch is applied against the pinned sha in the
object store through a temp index (GIT_INDEX_FILE seeded by `read-tree`, `git apply --cached`,
contents read back with `git show :<path>`). Deletes, renames, copies, mode changes, symlinks,
submodules, binary files and non-UTF-8 text are refused; a modified path must be inside the task
snapshot's allowed paths (SnapshotEnsurer.allowed_paths), an added path must not exist at the sha
(so a file the snapshot left out cannot be overwritten) and must not be secret. Every refusal
names the path.
"""

import difflib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

import patch_ops
from env_policy import tool_env

# codes of TaskPatchError: bad_patch (the patch is refused) and patch_too_large (it exceeds an
# apply_patch limit; the message names the limit).
BAD_PATCH = "bad_patch"
PATCH_TOO_LARGE = "patch_too_large"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT = 60
CREATE_RE = re.compile(r"^ create mode ([0-9]{6}) (.+)$")
PLAIN_MODES = ("100644", "100755")
# git quotes a path that holds quotes, backslashes or control characters; such a path never came
# from a diff of ordinary source files.
QUOTED_PATH = '"'
TOOL = "jev_mcp:apply_patch"
REASON_MAX = 300


class TaskPatchError(ValueError):
    """A task patch cannot become actions edits; `code` is bad_patch or patch_too_large."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _git_env(index: Optional[str] = None) -> Dict[str, str]:
    """The environment of every git call: the tool policy, no system config, and none of the
    GIT_* directory variables an inherited environment could use to redirect the repository."""
    env = {key: value for key, value in tool_env(os.environ).items()
           if not key.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                                  "GIT_COMMON_DIR", "GIT_ALTERNATE_OBJECT_DIRECTORIES"))}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    return env


def _git(workspace: Path, args: Sequence[str], *, index: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                              timeout=GIT_TIMEOUT, env=_git_env(index))
    except (OSError, subprocess.SubprocessError) as exc:  # a wedged git is a refusal, never a crash
        raise TaskPatchError(BAD_PATCH, f"git {args[0]} failed: {exc}") from exc


def _git_text(workspace: Path, args: Sequence[str], *, index: Optional[str] = None,
              what: str) -> str:
    proc = _git(workspace, args, index=index)
    if proc.returncode != 0:
        raise TaskPatchError(BAD_PATCH, f"{what}: {proc.stderr.decode(errors='replace').strip()[-200:]}")
    return proc.stdout.decode(errors="replace")


def _tail(line: str) -> str:
    """The path half of a `git apply --summary` line that is not a plain create."""
    return line.rstrip().rsplit(" ", 1)[-1]


def _parse_summary(text: str) -> List[str]:
    """The created paths of the patch; everything else git can say here is refused (deletes,
    renames, copies, mode changes, rewrites, symlinks, submodules)."""
    created: List[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = CREATE_RE.match(line)
        if match is not None:
            mode, path = match.groups()
            if mode == "120000":
                raise TaskPatchError(BAD_PATCH, f"{path} is a symlink")
            if mode == "160000":
                raise TaskPatchError(BAD_PATCH, f"{path} is a submodule")
            if mode not in PLAIN_MODES:
                raise TaskPatchError(BAD_PATCH, f"{path} has mode {mode}")
            created.append(path)
            continue
        if line.startswith(" delete mode "):
            raise TaskPatchError(BAD_PATCH, f"{_tail(line)}: deleting files is not allowed")
        if " mode change " in line:
            raise TaskPatchError(BAD_PATCH, f"{_tail(line)}: mode changes are not allowed")
        if " rename " in line:
            raise TaskPatchError(BAD_PATCH, f"{_tail(line)}: renames are not allowed")
        if " copy " in line:
            raise TaskPatchError(BAD_PATCH, f"{_tail(line)}: copies are not allowed")
        if " rewrite " in line:
            raise TaskPatchError(BAD_PATCH, f"{_tail(line)}: rewrites are not allowed")
        raise TaskPatchError(BAD_PATCH, f"unexpected patch summary: {line.strip()[:120]}")
    for path in created:
        _plain_path(path)
    return created


def _plain_path(path: str) -> str:
    """A path git printed unquoted, checked against the same rule the snapshot import uses."""
    if path.startswith(QUOTED_PATH):
        raise TaskPatchError(BAD_PATCH, f"a path with special characters cannot be patched: {path}")
    if path.startswith(":"):
        raise TaskPatchError(BAD_PATCH, f"a path starting with ':' cannot be patched: {path}")
    from repo_snapshot import unsafe_path

    problem = unsafe_path(path)
    if problem is not None:
        raise TaskPatchError(BAD_PATCH, f"{path} is not a repository-relative path: {problem}")
    return path


def _parse_numstat(text: str) -> List[str]:
    """The changed paths of the patch; a binary file shows as `-` and is refused."""
    paths: List[str] = []
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise TaskPatchError(BAD_PATCH, f"unexpected patch numstat: {line[:120]}")
        added, deleted, path = fields
        if added == "-" or deleted == "-":
            raise TaskPatchError(BAD_PATCH, f"{path} is a binary file")
        paths.append(_plain_path(path))
    if not paths:
        raise TaskPatchError(BAD_PATCH, "the patch changes no file")
    return paths


def _decode(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskPatchError(BAD_PATCH, f"{path} is not UTF-8 text") from exc


def _limits(edits: List[Dict[str, str]]) -> None:
    """The apply_patch size limits (patch_ops), as patch_too_large with the limit named."""
    total = 0
    for index, edit in enumerate(edits):
        for name in ("old", "new"):
            size = len(edit[name].encode("utf-8"))
            if size > patch_ops.MAX_TEXT_BYTES:
                raise TaskPatchError(PATCH_TOO_LARGE,
                                     f"edits[{index}].{name} is {size} bytes, the per-text limit is "
                                     f"{patch_ops.MAX_TEXT_BYTES} (MAX_TEXT_BYTES)")
        total += len(edit["old"].encode("utf-8")) + len(edit["new"].encode("utf-8"))
    if total > patch_ops.MAX_TOTAL_BYTES:
        raise TaskPatchError(PATCH_TOO_LARGE, f"the edits are {total} bytes in total, the limit is "
                                              f"{patch_ops.MAX_TOTAL_BYTES} (MAX_TOTAL_BYTES)")


def _file_edits(path: str, old_text: str, new_text: str) -> List[Dict[str, str]]:
    """The apply_patch edits of one modified file: each changed region of a line-wise diff becomes
    one edit, widened with equal context lines until its old text is non-empty and occurs exactly
    once where it applies (edits apply in order, so earlier edits count as applied). Regions whose
    widened spans touch merge; a file that is empty at the base cannot be edited (an empty old text
    means "create" in an edit)."""
    if old_text == new_text:
        return []
    if old_text == "":
        raise TaskPatchError(BAD_PATCH, f"{path} is empty at the base commit; an edit cannot express "
                                        "filling it")
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    # Maximal runs of non-equal opcodes, as [a_lo, a_hi, b_lo, b_hi] over old_lines / new_lines.
    regions: List[List[int]] = []
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if regions and regions[-1][1] == a1:
            regions[-1][1], regions[-1][3] = a2, b2
        else:
            regions.append([a1, a2, b1, b2])
    lines = list(old_lines)  # the file as the edits already emitted leave it
    edits: List[Dict[str, str]] = []
    shift = 0        # lines-index minus old-lines-index for material no edit touched yet
    applied_end = 0  # in lines coordinates: just past the chunk of the last emitted edit
    i = 0
    while i < len(regions):
        a_lo, a_hi, b_lo, b_hi = regions[i]
        lo, hi = a_lo + shift, a_hi + shift
        old_span = "".join(lines[lo:hi])
        new_span = "".join(new_lines[b_lo:b_hi])
        while True:
            content = "".join(lines)
            if old_span != "" and content.count(old_span) == 1:
                break
            right_limit = regions[i + 1][0] + shift if i + 1 < len(regions) else len(lines)
            if hi >= right_limit and i + 1 < len(regions):
                # widening right would touch the next region: the two merge into one edit
                nxt = regions.pop(i + 1)
                hi, b_hi = nxt[1] + shift, nxt[3]
                old_span = "".join(lines[lo:hi])
                new_span = "".join(new_lines[b_lo:b_hi])
                continue
            if lo > applied_end:
                lo -= 1
                line = lines[lo]
                old_span, new_span = line + old_span, line + new_span
                continue
            if hi < right_limit:
                line = lines[hi]
                hi += 1
                old_span, new_span = old_span + line, new_span + line
                continue
            raise TaskPatchError(BAD_PATCH, f"{path}: the change cannot be widened to a unique old text")
        edits.append({"file_path": path, "old": old_span, "new": new_span})
        lines[lo:hi] = [new_span]  # one chunk: later content counts it as applied
        applied_end = lo + 1
        shift += 1 - (hi - lo)
        i += 1
    return edits


def _default_client(project_id: str) -> Any:
    """The MCPClient of the project, the way the workspaces resolver builds it for actions."""
    from mcp_client import MCPClient
    from project_registry import ProjectManager, ProjectValidationError

    try:
        project = ProjectManager().load(project_id)
    except ProjectValidationError as exc:
        raise TaskPatchError(BAD_PATCH, f"project {project_id}: {exc}") from None
    return MCPClient(workspace=project.workspace if project.has_workspace else None, project=project)


def _fallback_allowed(workspace: Path, sha: str) -> FrozenSet[str]:
    """The path set SnapshotEnsurer.allowed_paths falls back to on a cache miss: every path an
    agent may read at the sha (a superset of the snapshot, which only removes paths from it)."""
    from repo_tools import RepoTools

    try:
        return frozenset(RepoTools(workspace, sha).readable_paths())
    except Exception as exc:  # noqa: BLE001 - ContextError, OSError: one short reason is enough
        raise TaskPatchError(BAD_PATCH, f"cannot read the repository at {sha[:12]}: {exc}") from None


def convert(project_id: str, sha: str, patch: Any, patch_base: Any, *,
            workspace: Optional[Path] = None, allowed: Optional[FrozenSet[str]] = None,
            ensurer: Any = None, client: Any = None) -> Dict[str, Any]:
    """Turns a task_update patch into apply_patch edits: {"edits", "files", "bytes"}.

    patch and patch_base are untrusted. patch_base is only checked (40 hex) and recorded by the
    caller in the task_patch event: the hub keeps no snapshot commit to compare it with. The
    patch is applied against `sha` in the project's workspace object store (a temp index; the
    working tree, HEAD, index and worktree list are never touched), every touched file must stay
    text the apply_patch format can express, and patch_ops.plan must reproduce the post-apply
    content exactly. Raises TaskPatchError (bad_patch / patch_too_large).
    """
    from code_context import is_secret

    if not isinstance(patch, str) or not patch.strip():
        raise TaskPatchError(BAD_PATCH, "patch must be the diff text of the worktree")
    if not (isinstance(patch_base, str) and SHA_RE.fullmatch(patch_base)):
        raise TaskPatchError(BAD_PATCH, "patch_base must be the 40-hex commit the worktree started at")
    if not (isinstance(sha, str) and SHA_RE.fullmatch(sha)):
        raise TaskPatchError(BAD_PATCH, "the task's pinned sha is not a 40-hex commit")
    client = client if client is not None else _default_client(project_id)
    if workspace is None:
        workspace = getattr(client, "workspace", None)
    if workspace is None:
        raise TaskPatchError(BAD_PATCH, f"project {project_id} has no git workspace")
    workspace = Path(workspace)
    if allowed is None:
        if ensurer is not None:
            allowed = frozenset(ensurer.allowed_paths(project_id, sha))
        else:
            allowed = _fallback_allowed(workspace, sha)

    tmp = tempfile.mkdtemp(prefix="guardian_taskpatch_")  # 0700
    try:
        patch_file = Path(tmp) / "task.diff"
        patch_file.write_text(patch, encoding="utf-8")
        created = set(_parse_summary(
            _git_text(workspace, ["apply", "--summary", str(patch_file)], what="cannot read the patch")))
        changed = _parse_numstat(
            _git_text(workspace, ["apply", "--numstat", str(patch_file)], what="cannot read the patch"))
        if not created <= set(changed):
            raise TaskPatchError(BAD_PATCH, "the patch summary and its numstat disagree")
        if len(set(changed)) > patch_ops.MAX_FILES:
            raise TaskPatchError(PATCH_TOO_LARGE, f"the patch touches {len(set(changed))} files, the "
                                                  f"limit is {patch_ops.MAX_FILES} (MAX_FILES)")

        index = str(Path(tmp) / "index")
        _git_text(workspace, ["read-tree", sha], index=index, what=f"cannot read the commit {sha[:12]}")
        proc = _git(workspace, ["apply", "--cached", str(patch_file)], index=index)
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()[-300:]
            raise TaskPatchError(BAD_PATCH, f"the patch does not apply to {sha[:12]}: {detail}")

        edits: List[Dict[str, str]] = []
        contents: Dict[str, str] = {}
        for path in sorted(set(changed)):
            new_data = _git(workspace, ["show", f":{path}"], index=index).stdout
            new_text = _decode(path, new_data)
            if path in created:
                if _git(workspace, ["cat-file", "-e", f"{sha}:{path}"]).returncode == 0:
                    raise TaskPatchError(BAD_PATCH, f"{path} exists at {sha[:12]}, so a file the "
                                                    f"snapshot left out cannot be added over it")
                if is_secret(path):
                    raise TaskPatchError(BAD_PATCH, f"{path} looks like secret material")
                if new_text == "":
                    raise TaskPatchError(BAD_PATCH, f"{path} would be an empty file")
                edits.append({"file_path": path, "old": "", "new": new_text})
                contents[path] = new_text
                continue
            if path not in allowed:
                raise TaskPatchError(BAD_PATCH, f"{path} is not in the task's snapshot")
            old_data = _git(workspace, ["cat-file", "blob", f"{sha}:{path}"]).stdout
            old_text = _decode(path, old_data)
            file_edits = _file_edits(path, old_text, new_text)
            edits.extend(file_edits)
            if file_edits:
                contents[path] = new_text
        if not edits:
            raise TaskPatchError(BAD_PATCH, "the patch changes nothing")
        if len(edits) > patch_ops.MAX_EDITS:
            raise TaskPatchError(PATCH_TOO_LARGE, f"the patch needs {len(edits)} edits, the limit is "
                                                  f"{patch_ops.MAX_EDITS} (MAX_EDITS)")
        _limits(edits)

        # the self-check: the edits, applied in order against the sha, must reproduce exactly what
        # the patch produced
        try:
            _original, updated = patch_ops.plan(edits, patch_ops.git_reader(workspace, sha))
        except patch_ops.PatchError as exc:
            raise TaskPatchError(BAD_PATCH, f"conversion: {exc}") from exc
        for path, new_text in contents.items():
            if updated.get(path) != new_text:
                raise TaskPatchError(BAD_PATCH, f"conversion: {path} would not be rebuilt exactly")

        try:
            validated = client.validate(TOOL, {"edits": edits})
        except Exception as exc:  # noqa: BLE001 - ActionValidationError: its message is the reason
            raise TaskPatchError(BAD_PATCH, str(exc)) from exc
        return {"edits": validated["edits"], "files": len(contents),
                "bytes": sum(len(text.encode("utf-8")) for text in contents.values())}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def action_thread_id(task_id: str) -> Optional[str]:
    """thread_task_<id with '-' as '_'>, or None when that cannot form a valid thread id
    (action_ops.THREAD_ID_PATTERN; the hub's Go threadPattern accepts exactly the same shape)."""
    from action_ops import THREAD_ID_PATTERN

    thread_id = "thread_task_" + str(task_id).replace("-", "_")
    return thread_id if THREAD_ID_PATTERN.fullmatch(thread_id) else None


def action_reason(task_id: str, agent: str, result: Any) -> str:
    """The stored action's reason: the task, its author and the first line of its result."""
    first = next((line for line in str(result or "").splitlines() if line.strip()), "")
    return f"resident task {task_id} by {agent}: {first}".strip()[:REASON_MAX]

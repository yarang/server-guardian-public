"""
Search-and-replace edits for judge actions (docs/design/debate-patch-tool.md).

The judge proposes {"file_path", "old", "new"} edits instead of whole files, so it can change code
it saw only in part. Each `old` must occur exactly once in the file at that point, which makes a
misplaced edit impossible, and must lie inside code the agents were shown. Edits are applied all
or nothing, optionally checked (py_compile, gofmt, go build) and reverted when a check fails.
"""

import difflib
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from env_policy import tool_env

MAX_FILES = 10
MAX_EDITS = 30
MAX_TEXT_BYTES = 20 * 1024
MAX_TOTAL_BYTES = 100 * 1024
CHECK_TIMEOUT = 300


class PatchError(ValueError):
    """An edit that cannot be applied as proposed.

    repairable marks errors the judge can fix by reading more code and resending the edit (the old text was
    not shown, was not found or was not unique), as opposed to a wrong path or a file that exists already.
    """

    def __init__(self, message: str, repairable: bool = False) -> None:
        super().__init__(message)
        self.repairable = repairable


def normalize_edits(edits: Any) -> List[Dict[str, str]]:
    """Structural checks shared by proposal validation and execution."""
    if not isinstance(edits, list) or not edits:
        raise PatchError("edits must be a non-empty list")
    if len(edits) > MAX_EDITS:
        raise PatchError(f"at most {MAX_EDITS} edits")
    cleaned, total = [], 0
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) - {"file_path", "old", "new"}:
            raise PatchError(f"edits[{i}] must be {{file_path, old, new}}")
        path, old, new = edit.get("file_path"), edit.get("old"), edit.get("new")
        if not isinstance(path, str) or not path.strip() or not isinstance(old, str) or not isinstance(new, str):
            raise PatchError(f"edits[{i}]: file_path, old and new must be strings")
        for name, text in (("old", old), ("new", new)):
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise PatchError(f"edits[{i}].{name} is larger than {MAX_TEXT_BYTES} bytes")
        if old == new:
            raise PatchError(f"edits[{i}]: old and new are identical")
        total += len(old.encode("utf-8")) + len(new.encode("utf-8"))
        path = path.strip()
        while path.startswith("./"):
            path = path[2:]
        cleaned.append({"file_path": path, "old": old, "new": new})
    if total > MAX_TOTAL_BYTES:
        raise PatchError(f"edits are larger than {MAX_TOTAL_BYTES} bytes in total")
    if len({e["file_path"] for e in cleaned}) > MAX_FILES:
        raise PatchError(f"at most {MAX_FILES} files")
    return cleaned


def plan(edits: List[Dict[str, str]], read: "callable") -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    """Applies edits in memory. read(path) returns the current text or None when the file is absent.

    Returns (original, updated) contents per path; original is None for files the edits create.
    """
    original: Dict[str, Optional[str]] = {}
    updated: Dict[str, str] = {}
    for i, edit in enumerate(edits):
        path = edit["file_path"]
        if path not in updated:
            original[path] = read(path)
        current = updated.get(path, original[path])
        if edit["old"] == "":
            if current is not None:
                raise PatchError(f"edits[{i}]: {path} already exists; an empty old text only creates new files")
            updated[path] = edit["new"]
            continue
        if current is None:
            raise PatchError(f"edits[{i}]: {path} does not exist")
        count = current.count(edit["old"])
        if count != 1:
            where = "not found" if count == 0 else f"found {count} times"
            raise PatchError(f"edits[{i}]: old text {where} in {path}; it must match exactly once", repairable=True)
        updated[path] = current.replace(edit["old"], edit["new"], 1)
    return original, updated


def unified_diff(original: Dict[str, Optional[str]], updated: Dict[str, str]) -> str:
    chunks = []
    for path, new in updated.items():
        old = original.get(path)
        chunks.extend(difflib.unified_diff(
            (old or "").splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}",
        ))
    return "".join(line if line.endswith("\n") else line + "\n" for line in chunks)


def content_hash(path: Path) -> Optional[str]:
    """sha256 of a file's bytes, or None when it is absent."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    except OSError:
        return None


def file_hashes(workspace: Path, paths: List[str]) -> Dict[str, str]:
    """Content hash per existing file; recorded in an executed action's result so siblings can build on it."""
    hashes = {p: content_hash(workspace / p) for p in paths}
    return {p: h for p, h in hashes.items() if h}


def uncommitted_conflicts(workspace: Path, paths: List[str], siblings: Optional[Dict[str, str]] = None) -> List[str]:
    """Files among paths with uncommitted changes that a sibling action of this decision did not make.

    siblings maps path -> hash of the content the last EXECUTED sibling action (same thread, same base commit)
    left in that file. A dirty file is acceptable only while its content still equals that hash; any other
    uncommitted change (a person's edit, another thread's action) is returned as a conflict.
    Returns messages naming the file; empty when the paths may be edited. A failing git is not a conflict.
    """
    siblings = siblings or {}
    found: List[str] = []
    for path in paths:
        proc = subprocess.run(["git", "status", "--porcelain", "--", path], cwd=workspace,
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        expected = siblings.get(path)
        if expected is None:
            found.append(f"{path} (the change was not made by a sibling action of this decision)")
        elif content_hash(workspace / path) != expected:
            found.append(f"{path} (changed after the last sibling action of this decision; "
                         "the change was not made by a sibling action)")
    return found


def git_reader(workspace: Path, sha: Optional[str]):
    """read(path) for the workspace at a commit (sha) or on disk (None)."""
    def read(path: str) -> Optional[str]:
        if sha:
            proc = subprocess.run(["git", "cat-file", "-p", f"{sha}:{path}"], cwd=workspace,
                                  capture_output=True, timeout=30)
            return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None
        target = workspace / path
        return target.read_text(encoding="utf-8") if target.is_file() else None
    return read


def check_seen(edits: List[Dict[str, str]], pack: Any) -> None:
    """Every replaced text must lie inside code the agents were shown (a code context pack)."""
    for i, edit in enumerate(edits):
        if edit["old"] == "":
            continue
        if pack is None or not pack.saw(edit["file_path"], edit["old"]):
            raise PatchError(f"edits[{i}]: the judge was not shown this code in {edit['file_path']}; "
                             "it can only change code from the debate's code context", repairable=True)


def _tool(name: str) -> Optional[str]:
    extra = [str(Path.home() / ".local" / "go" / "bin"), str(Path.home() / ".local" / "bin")]
    return shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", ""), *extra]))


def _go_module_dir(workspace: Path, path: str) -> Optional[Path]:
    directory = (workspace / path).parent
    while directory == workspace or workspace in directory.parents:
        if (directory / "go.mod").is_file():
            return directory
        if directory == workspace:
            break
        directory = directory.parent
    return None


def unformatted_go(workspace: Path, paths: List[str]) -> List[str]:
    """Go files among paths that gofmt would already change (checked before an edit)."""
    gofmt = _tool("gofmt")
    existing = [p for p in paths if p.endswith(".go") and (workspace / p).is_file()]
    if not gofmt or not existing:
        return []
    proc = subprocess.run([gofmt, "-l", *existing], cwd=workspace, capture_output=True, text=True, timeout=60)
    return proc.stdout.split()


def run_checks(workspace: Path, paths: List[str], unformatted_before: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """py_compile for Python files; gofmt and go build for each Go module touched.

    Files gofmt already flagged before the edit (unformatted_before) do not fail the gofmt check, so a
    correct edit to a file that was never formatted is not reverted; they are reported instead.
    """
    results: List[Dict[str, Any]] = []

    def run(argv: List[str], cwd: Path) -> None:
        env = {**tool_env(os.environ), "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly"}
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=CHECK_TIMEOUT, env=env)
            code, out, err = proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, out, err = -1, "", str(exc)
        rel = str(cwd.relative_to(workspace)) if cwd != workspace else "."
        results.append({"cmd": " ".join(Path(argv[0]).name if i == 0 else a for i, a in enumerate(argv)),
                        "cwd": rel, "exit_code": code, "stdout": out, "stderr": err})

    py = [p for p in paths if p.endswith(".py")]
    if py:
        run(["python3", "-m", "py_compile", *py], workspace)
    go_files = [p for p in paths if p.endswith(".go")]
    if go_files:
        go, gofmt = _tool("go"), _tool("gofmt")
        if not go or not gofmt:
            results.append({"cmd": "go build", "cwd": ".", "exit_code": -1, "stdout": "",
                            "stderr": "Go is not installed on this host; cannot check Go edits"})
            return results
        run([gofmt, "-l", *go_files], workspace)
        listed = results[-1]["stdout"].split()
        before = set(unformatted_before or [])
        new_problems = [p for p in listed if p not in before]
        if results[-1]["exit_code"] == 0 and new_problems:
            results[-1]["exit_code"] = 1  # gofmt -l lists files that are not formatted
            results[-1]["stderr"] = "not gofmt-formatted: " + " ".join(new_problems)
        elif results[-1]["exit_code"] == 0 and listed:
            results[-1]["stderr"] = "already not gofmt-formatted before this edit (ignored): " + " ".join(listed)
        for module in sorted({m for m in (_go_module_dir(workspace, p) for p in go_files) if m}):
            run([go, "build", "./..."], module)
    return results


def apply(workspace: Path, edits: List[Dict[str, str]], check: bool, resolve) -> Dict[str, Any]:
    """Applies edits to the working tree all or nothing; reverts when a check fails.

    resolve(path) maps a workspace-relative path to an absolute path inside the workspace (and
    raises for paths that escape it). Edits are matched against the working tree, which may hold the
    change of an earlier sibling action; a failed check restores exactly that state (not HEAD).
    """
    reader = git_reader(workspace, None)
    original, updated = plan(edits, reader)
    targets = {path: resolve(path) for path in updated}
    unformatted_before = unformatted_go(workspace, list(updated)) if check else []
    written: List[str] = []
    try:
        for path, content in updated.items():
            targets[path].parent.mkdir(parents=True, exist_ok=True)
            targets[path].write_text(content, encoding="utf-8")
            written.append(path)
    except OSError as exc:
        _restore(targets, original, written)
        return {"status": "FAILED", "error": f"write failed: {exc}", "reverted": True, "diff": ""}

    diff = unified_diff(original, updated)
    checks = run_checks(workspace, list(updated), unformatted_before) if check else []
    failed = [c for c in checks if c["exit_code"] != 0]
    if failed:
        _restore(targets, original, written)
        return {"status": "FAILED", "error": f"check failed: {failed[0]['cmd']}", "reverted": True,
                "diff": diff, "checks": checks}
    files = len(updated)
    return {"status": "SUCCESS", "message": f"applied {len(edits)} edits to {files} file{'s' if files != 1 else ''}",
            "reverted": False, "diff": diff, "checks": checks, "file_hashes": file_hashes(workspace, list(updated))}


def _restore(targets: Dict[str, Path], original: Dict[str, Optional[str]], written: List[str]) -> None:
    for path in written:
        if original[path] is None:
            targets[path].unlink(missing_ok=True)
        else:
            targets[path].write_text(original[path], encoding="utf-8")

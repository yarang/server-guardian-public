"""Quick review: one reviewer checks one change, read-only (docs/design/verification-layer.md, V1).

A quick review sits between a solo answer and a debate. One session of one persona (default: the default team's
critic) gets the diff of a change in a code project, reads the surrounding code through the usual repository tools
pinned to the head commit, and answers with one ```findings block. Deterministic checks then sort the findings:

- the quote of a finding must be on its line (within GUARDIAN_CITATION_SLACK) and that line must have been shown to
  the session, either by a repository tool or in a diff hunk (`evidence.check_findings`, the audit's rule);
- the change rule: a finding must point at a changed line (within the slack) or quote a symbol the change touches
  (a caller of changed code). Anything else is kept under `notes`, never dropped.

Like a solo run it is not a flow: no Judge, round, thread, approval or action. The label is REVIEW, never a verdict
word, and nothing in the answer is parsed for actions. It runs on the solo runner's footing: a scratch decision
database, no jev gate, its own slot (GUARDIAN_MAX_REVIEWS) and a record under `<GUARDIAN_DATA_DIR>/reviews/<id>/`
that expires after 30 days.

The change is a range `BASE..HEAD` of the workspace, a patch file, a branch of the project's origin remote, or the
edits of a PROPOSED `jev_mcp:apply_patch` action (V2). A patch is checked against its base in a
temporary index (the workspace's index and working tree are never touched) and turned into a commit object that no
ref points at, so the review reads the code after the change the same way as for a range. A branch is fetched from
the configured origin only, with explicit refspecs into `refs/guardian/review/<id>/`, so no tracking ref, local
branch, HEAD, the index or the working tree is ever touched; those refs live as long as the record (--review-prune
deletes them with it). An action's edits are replayed on their base commit in a temporary index, read from the
decision database opened read-only; the action row itself is never written.
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import character
import patch_ops
from audit_run import PRECISION_RULES, mask_secrets, parse_audit_findings
from code_context import ContextError, resolve_sha, validate_ref
from db_manager import data_dir, default_db_path
from debate_engine import DebateEngine, RolePrompt, answer_language_rule, repo_mcp_enabled, retrieval_enabled
from debate_events import EventWriter
from evidence import check_findings, citation_slack, coalesce, line_covered
from llm_runner import BUILTIN_PROVIDER
from mcp_client import MCPClient
from persona_manager import PersonaManager
from project_registry import ProjectManager, ProjectValidationError, workspace_lock
from role_cards import persona_text
from solo_agent import BUSY, USAGE, SoloError, solo_slot
from team_manager import EXPERT_ROLE, TeamManager

REVIEW_PROMPT_VERSION = 1
LABEL = "REVIEW"
NOTE = "advisory, no Judge"
SESSION_KEY = "review"
DEFAULT_MAX_REVIEWS = 2
DEFAULT_KEEP_DAYS = 30
DEFAULT_MAX_LINES = 1500  # changed lines (added + removed); a larger change is refused, never cut
DEFAULT_LANGUAGE = "Korean"
SEVERITIES = ("high", "medium", "low")
MAX_FINDINGS = 100
MAX_TEXT_CHARS = 800
MAX_PATCH_BYTES = 2 * 1024 * 1024
RUN_ID = re.compile(r"^review_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")
RANGE = re.compile(r"^([^\s.][^\s]*?)\.\.([^\s.][^\s]*)$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
FETCH_TIMEOUT = 120
APPLY_PATCH_TOOL = "jev_mcp:apply_patch"
# The private namespace a --ref review fetches into; the refs live as long as the record.
REVIEW_NS = "refs/guardian/review"
_GIT_ENV = {"GIT_AUTHOR_NAME": "guardian-review", "GIT_AUTHOR_EMAIL": "review@guardian.invalid",
            "GIT_COMMITTER_NAME": "guardian-review", "GIT_COMMITTER_EMAIL": "review@guardian.invalid"}

# A persona that judges or acts cannot review alone (the same markers as the solo runner).
_ID_MARKERS = ("judge", "jev_mcp")
_TEXT_MARKER = "jev_mcp"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# Names a change defines or touches: the hunk header's function context and declarations on changed lines.
_DECL = re.compile(r"\b(?:def|class|func|function|fn|interface|type|struct|enum|const|let|var)\s+(?:\([^)]*\)\s*)?"
                   r"([A-Za-z_][A-Za-z0-9_]{2,})")

REVIEW_RULES = (
    "You review one change alone: no other agent checks your findings and no judge decides on them. The diff is in "
    "the task; read the code around it with the repository tools (they show the commit after the change), and before "
    "you call something broken, read the callers and the callees of the code the change touches. Report only "
    "problems that the change introduces or makes reachable, not older problems elsewhere. This is advice only: do "
    "not propose patches, commands or actions, and do not write action blocks, a decision or a verdict line.")

FINDINGS_FORMAT = (
    "Answer with ONE ```findings block and nothing after it. The block is a JSON list; each item is an object:\n"
    '  {"path": "<file of the head commit>", "line": <line number in the head commit>, "quote": "<verbatim text of '
    'that line, at most 300 characters>", "claim": "<one or two sentences: what is wrong>", "scenario": "<the concrete '
    'failure: which input or state, through which path, gives which wrong result or harm>", "severity": "high" | '
    '"medium" | "low"}\n'
    "Severity: high = it goes wrong or can be abused in normal use; medium = it goes wrong under a condition that can "
    "really occur; low = real but harmless in practice. Put each finding at the line where the problem is, preferably "
    f"a changed line; at most {MAX_FINDINGS} items. An empty list `[]` says that you found nothing wrong. The block "
    "must be valid JSON: an unreadable block is asked for once more. Before the block you may add a short summary.")


class ReviewError(Exception):
    """A review that cannot start. `code` is the exit code (USAGE, or BUSY when every slot is taken)."""

    def __init__(self, message: str, code: int = USAGE) -> None:
        super().__init__(message)
        self.code = code


def review_root() -> Path:
    return data_dir() / "reviews"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def max_reviews() -> int:
    return _env_int("GUARDIAN_MAX_REVIEWS", DEFAULT_MAX_REVIEWS)


def max_lines() -> int:
    return _env_int("GUARDIAN_REVIEW_MAX_LINES", DEFAULT_MAX_LINES)


def resolve_persona(manager: PersonaManager, role: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """The persona id and record of the reviewer; default is the default team's first critic. A persona that judges
    or proposes actions is refused; a character needs a critic or solo seat."""
    if role:
        persona_id = role
    else:
        lineup = TeamManager(persona_mgr=manager).load(None)
        persona_id = lineup.critics[0].persona if lineup.critics else lineup.expert.persona
    persona = manager.get_persona(persona_id)
    if persona is None:
        raise ReviewError(f"unknown role {persona_id!r}")
    if character.is_character(persona):
        if not {"critic", "solo"} & set(persona.get("seats") or []):
            raise ReviewError(f"role {persona_id!r} has no critic or solo seat")
        return persona_id, persona
    text = str(persona.get("system_prompt") or "")
    if any(marker in persona_id.lower() for marker in _ID_MARKERS) or _TEXT_MARKER in text:
        raise ReviewError(f"role {persona_id!r} judges or proposes actions: a review only reports findings")
    return persona_id, persona


# --- the change -------------------------------------------------------------------------------------------------


def _git(workspace: Path, *args: str, stdin: Optional[bytes] = None, env: Optional[Dict[str, str]] = None) -> str:
    try:
        done = subprocess.run(["git", "-C", str(workspace), *args], input=stdin, capture_output=True, timeout=120,
                              env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError(f"git {args[0]}: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ReviewError(f"git {args[0]}: {(detail[-1] if detail else 'failed')[:300]}")
    return done.stdout.decode("utf-8", "replace")


def parse_range(text: str) -> Tuple[str, str]:
    match = RANGE.match((text or "").strip())
    if not match or "..." in text:
        raise ReviewError(f"range {text!r}: expected BASE..HEAD (two refs or commits)")
    return match.group(1), match.group(2)


def commit_patch(workspace: Path, base: str, patch: str) -> str:
    """The commit object of `patch` applied to `base`, built in a temporary index (no ref, no working tree change).
    Raises ReviewError when the patch does not apply to the base."""
    if not patch.strip():
        raise ReviewError("the patch is empty")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ReviewError(f"the patch is larger than {MAX_PATCH_BYTES // (1024 * 1024)} MiB")
    with tempfile.TemporaryDirectory(prefix="guardian-review-") as scratch:
        env = {**_GIT_ENV, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(workspace, "read-tree", base, env=env)
        try:
            _git(workspace, "apply", "--cached", "--whitespace=nowarn", "-", stdin=patch.encode("utf-8"), env=env)
        except ReviewError as exc:
            raise ReviewError(f"the patch does not apply to {base[:12]}: {exc}") from None
        tree = _git(workspace, "write-tree", env=env).strip()
        return _git(workspace, "commit-tree", tree, "-p", base, "-m", "guardian quick review (patch)", env=env).strip()


def _check_branch_name(workspace: Path, branch: str) -> None:
    """git's own branch-name rule, on top of validate_ref; refuses anything the refspec could misread."""
    try:
        done = subprocess.run(["git", "-C", str(workspace), "check-ref-format", "--branch", branch],
                              capture_output=True, timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError(f"git check-ref-format: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ReviewError(f"invalid branch name {branch!r}: {(detail[-1] if detail else 'refused by git')[:200]}")


def _fetch_origin(workspace: Path, refspecs: List[str]) -> None:
    """One `git fetch` from the workspace's configured origin with explicit refspecs (no URL from the caller).

    `--refmap=` turns off the opportunistic remote-tracking update, so only the given refspecs move.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def run(extra: List[str]) -> "subprocess.CompletedProcess[bytes]":
        try:
            return subprocess.run(["git", "-C", str(workspace), "fetch", *extra, "origin", *refspecs],
                                  capture_output=True, timeout=FETCH_TIMEOUT, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReviewError(f"git fetch origin: {exc}") from exc

    done = run(["--no-tags", "--refmap=", "--no-write-fetch-head"])
    if done.returncode != 0 and "no-write-fetch-head" in done.stderr.decode("utf-8", "replace"):
        done = run(["--no-tags", "--refmap="])  # an older git without the flag; a written FETCH_HEAD is metadata only
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ReviewError(f"git fetch origin: {(detail[-1] if detail else 'failed')[:300]}")


def load_action(action_id: int, project_id: str) -> Dict[str, Any]:
    """The PROPOSED apply_patch action of project_id, from the decision database opened read-only.

    Accepts only status PROPOSED, tool jev_mcp:apply_patch, the given project and a non-empty base_sha; everything
    else is refused. Returns {id, base, edits} (the edits normalized); the action row is never written.
    """
    if not isinstance(action_id, int) or isinstance(action_id, bool) or action_id <= 0:
        raise ReviewError(f"action id must be a positive integer, not {action_id!r}")
    path = Path(default_db_path())
    if not path.is_file():
        raise ReviewError(f"no decision database at {path}: nothing to review")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)  # read-only, never created
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT a.id, a.status, a.tool, a.arguments, a.base_sha, a.project_id, t.project_id AS thread_project "
                "FROM decision_actions a LEFT JOIN threads t ON t.thread_id = a.thread_id WHERE a.id = ?;",
                (action_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ReviewError(f"the decision database cannot be read: {exc}") from exc
    if row is None:
        raise ReviewError(f"action {action_id}: no such action in the decision database")
    if row["status"] != "PROPOSED":
        raise ReviewError(f"action {action_id} is {row['status']}, not PROPOSED: only a pending patch can be reviewed")
    if row["tool"] != APPLY_PATCH_TOOL:
        raise ReviewError(f"action {action_id} is a {row['tool']} action, not {APPLY_PATCH_TOOL}")
    belongs = row["project_id"] or row["thread_project"] or "server-guardian"
    if belongs != project_id:
        raise ReviewError(f"action {action_id} belongs to project {belongs}, not {project_id}")
    base = str(row["base_sha"] or "").strip()
    if not base:
        raise ReviewError(f"action {action_id} has no base sha: its edits cannot be replayed on a commit")
    try:
        arguments = json.loads(row["arguments"]) if row["arguments"] else {}
    except json.JSONDecodeError:
        raise ReviewError(f"action {action_id}: its arguments cannot be read") from None
    edits = arguments.get("edits") if isinstance(arguments, dict) else None
    try:
        edits = patch_ops.normalize_edits(edits)
    except patch_ops.PatchError as exc:
        raise ReviewError(f"action {action_id}: {exc}") from exc
    return {"id": int(row["id"]), "base": base, "edits": edits}


def _file_mode(workspace: Path, base: str, path: str) -> str:
    """The mode `path` carries in `base` (100644 for a file the edits create)."""
    try:
        fields = _git(workspace, "ls-tree", base, "--", path).strip().split()
    except ReviewError:
        return "100644"
    return fields[0] if fields and fields[0] in ("100644", "100755", "120000") else "100644"


def commit_edits(workspace: Path, base: str, edits: List[Dict[str, str]]) -> str:
    """The commit object of `edits` applied to `base`, built on a temporary index; the workspace is never touched."""
    try:
        _original, updated = patch_ops.plan(edits, patch_ops.git_reader(workspace, base))
    except patch_ops.PatchError as exc:
        raise ReviewError(f"the action's edits do not apply to {base[:12]}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="guardian-review-") as scratch:
        env = {**_GIT_ENV, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(workspace, "read-tree", base, env=env)
        for path, content in updated.items():
            blob = _git(workspace, "hash-object", "-w", "--stdin", stdin=content.encode("utf-8"), env=env).strip()
            _git(workspace, "update-index", "--add", "--cacheinfo", f"{_file_mode(workspace, base, path)},{blob},{path}",
                 env=env)
        tree = _git(workspace, "write-tree", env=env).strip()
        return _git(workspace, "commit-tree", tree, "-p", base, "-m", "guardian quick review (action)", env=env).strip()


# --- the review refs (--ref) -------------------------------------------------------------------------------------


def _delete_review_refs(workspace: Path, run_id: str) -> List[str]:
    """Best-effort deletion of refs/guardian/review/<run_id>/* in one workspace; returns the ref names found."""
    if not run_id:
        return []
    try:
        names = _git(workspace, "for-each-ref", "--format=%(refname)", f"{REVIEW_NS}/{run_id}/").split()
    except ReviewError:
        return []
    for name in names:
        try:
            _git(workspace, "update-ref", "-d", name)
        except ReviewError:
            pass
    return names


def _record_project(entry: Path) -> Optional[str]:
    """The project a review record belongs to (result.json, else request.json), for ref cleanup."""
    for name in ("result.json", "request.json"):
        try:
            data = json.loads((entry / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("project"):
            return str(data["project"])
    return None


def _workspace_of(project_id: str) -> Optional[Path]:
    """The project's workspace when it is a code project with a git clone on disk, else None."""
    try:
        project = ProjectManager().load(project_id)
    except ProjectValidationError:
        return None
    if not project.has_workspace:
        return None
    workspace = project.workspace
    return workspace if (workspace / ".git").exists() else None


def _code_workspaces() -> List[Path]:
    """Every code project's workspace on disk (deduplicated); ops and design projects have none."""
    found: Dict[Path, None] = {}
    manager = ProjectManager()
    for raw in manager.list_projects():
        project_id = str(raw.get("id") or "")
        if not project_id:
            continue
        try:
            project = manager.load(project_id)
        except ProjectValidationError:
            continue
        if project.has_workspace and (project.workspace / ".git").exists():
            found.setdefault(project.workspace, None)
    return list(found)


def _sweep_review_refs(root: Path) -> None:
    """Deletes review refs in every code project workspace whose record no longer exists."""
    keep = {entry.name for entry in root.iterdir() if entry.is_dir()} if root.is_dir() else set()
    for workspace in _code_workspaces():
        try:
            listing = _git(workspace, "for-each-ref", "--format=%(refname)", f"{REVIEW_NS}/").split()
        except ReviewError:
            continue
        stale: List[str] = []
        for name in listing:
            parts = name.split("/")  # refs/guardian/review/<id>/<tail>
            if len(parts) > 4 and parts[3] and parts[3] not in stale and parts[3] not in keep:
                stale.append(parts[3])
        for run_id in stale:
            _delete_review_refs(workspace, run_id)


def parse_diff(diff: str) -> Dict[str, Any]:
    """{files, added, removed, changed: path -> sorted new-side lines, hunks: path -> new-side spans, symbols}.

    `changed` holds the new-side number of every added line and, for a removal, the new-side line it sits before, so
    a finding about a deleted check can point at where it was. `hunks` are the new-side line spans a hunk shows (they
    count as shown to the session). Deleted files keep no new-side lines."""
    changed: Dict[str, Set[int]] = {}
    hunks: Dict[str, List[Tuple[int, int]]] = {}
    symbols: Set[str] = set()
    files: List[str] = []
    added = removed = 0
    path: Optional[str] = None
    new_line = 0
    header = False  # between "diff --git" and the first hunk: only there are ---/+++ file names
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path, header = None, True
            continue
        if header and raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            if path and path not in files:
                files.append(path)
            continue
        if header and raw.startswith("--- "):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            header = False
            new_line = int(hunk.group(3))
            count = int(hunk.group(4)) if hunk.group(4) is not None else 1
            if path and count:
                hunks.setdefault(path, []).append((new_line, new_line + count - 1))
            symbols.update(_DECL.findall(hunk.group(5) or ""))
            continue
        if header or not raw or raw.startswith("\\"):
            continue
        if path is None:  # a deleted file: its lines count toward the size, no new side to map
            removed += raw[0] == "-"
            continue
        if raw[0] == "+":
            added += 1
            changed.setdefault(path, set()).add(new_line)
            symbols.update(_DECL.findall(raw[1:]))
            new_line += 1
        elif raw[0] == "-":
            removed += 1
            changed.setdefault(path, set()).add(max(1, new_line))
            symbols.update(_DECL.findall(raw[1:]))
        elif raw[0] == " ":
            new_line += 1
    return {"files": files, "added": added, "removed": removed,
            "changed": {p: sorted(v) for p, v in changed.items()},
            "hunks": {p: coalesce(v) for p, v in hunks.items()}, "symbols": sorted(symbols)}


# --- the findings -----------------------------------------------------------------------------------------------


def on_change(finding: Dict[str, Any], change: Dict[str, Any], slack: int) -> bool:
    """The change rule: the finding points at a changed line (within slack) or quotes a symbol the change touches."""
    lines = change["changed"].get(finding.get("path") or "", [])
    line = int(finding.get("line") or 0)
    if any(abs(line - number) <= slack for number in lines):
        return True
    quote = str(finding.get("quote") or "")
    return any(re.search(rf"\b{re.escape(name)}\b", quote) for name in change["symbols"])


def sort_findings(repo: Any, items: List[Any], shown: Callable[[str], List[Tuple[int, int]]],
                  change: Dict[str, Any], slack: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(findings, notes): verified findings on the change, and everything else with the reason it was set aside."""
    findings: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    checked = check_findings(repo, items, slack)
    for item, check in zip(items[:MAX_FINDINGS], checked):
        if not isinstance(item, dict):
            notes.append({"path": "", "line": 0, "quote": "", "claim": "", "scenario": "", "severity": "medium",
                          "verified": False, "reason": "the finding is not an object"})
            continue
        path, line = check["path"], check["line"]
        ok, reason = bool(check["ok"]), str(check["reason"])
        if ok and not line_covered(shown(path), line, slack):
            ok, reason = False, "line was not shown to the session"
        text = {key: (str(item[key]) if isinstance(item.get(key), str) else "") for key in ("quote", "claim", "scenario")}
        entry = {"path": path, "line": line,
                 "quote": mask_secrets(text["quote"][:300]) if ok else "",  # an unchecked quote is the model's words
                 "claim": mask_secrets(text["claim"][:MAX_TEXT_CHARS]),
                 "scenario": mask_secrets(text["scenario"][:MAX_TEXT_CHARS]),
                 "severity": item.get("severity") if item.get("severity") in SEVERITIES else "medium",
                 "verified": ok, "reason": reason}
        if not ok:
            notes.append(entry)
        elif on_change({**entry, "quote": text["quote"]}, change, slack):
            findings.append(entry)
        else:
            notes.append({**entry, "reason": "not on a changed line and quotes no changed symbol"})
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: order[f["severity"]])
    return findings, notes


def summary_line(findings: List[Dict[str, Any]], notes: List[Dict[str, Any]]) -> str:
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    return (f"{len(findings)} findings ({counts['high']} high, {counts['medium']} medium, {counts['low']} low), "
            f"{len(notes)} notes")


def followup(problem: Optional[str]) -> str:
    """The single re-ask: the block is missing or cannot be read."""
    why = (f"Your ```findings block cannot be read: {problem}. Write it again as valid JSON."
           if problem else "Your answer has no ```findings block, so it cannot be used.")
    return "\n".join(["=== REVIEW CHECK ===", why,
                      "Reply with the complete ```findings block for the whole change, in the format given before."])


# --- the runner -------------------------------------------------------------------------------------------------


class ReviewRunner:
    """One quick review. run() returns the result dict (also stored as result.json)."""

    def __init__(self, project_id: str, change_range: Optional[str] = None, patch: Optional[str] = None,
                 patch_base: Optional[str] = None, ref: Optional[str] = None, base_ref: Optional[str] = None,
                 expect_sha: Optional[str] = None, action_id: Optional[int] = None, role: Optional[str] = None,
                 provider: Optional[str] = None, language_name: Optional[str] = None, root: Optional[Path] = None,
                 run_id: Optional[str] = None) -> None:
        if run_id is not None and not RUN_ID.match(run_id):
            raise ReviewError(f"invalid review id {run_id!r}")
        if sum(1 for source in (change_range, patch, ref, action_id) if source) != 1:
            raise ReviewError("give the change as --range BASE..HEAD, --patch FILE, --ref BRANCH or --action ID "
                              "(one of them)")
        if patch_base and patch is None:
            raise ReviewError("--patch-base goes with --patch")
        if (base_ref or expect_sha) and ref is None:
            raise ReviewError("--base and --expect-sha go with --ref")
        if expect_sha and not SHA40.match(expect_sha):
            raise ReviewError(f"--expect-sha must be 40 lowercase hex characters, not {expect_sha!r}")
        if action_id is not None and (not isinstance(action_id, int) or isinstance(action_id, bool) or action_id <= 0):
            raise ReviewError(f"--action must be a positive action id, not {action_id!r}")
        try:
            self.project = ProjectManager().load(project_id)
        except ProjectValidationError as exc:
            raise ReviewError(f"project: {exc}") from exc
        if self.project.is_ops or self.project.is_design:
            raise ReviewError(f"project {self.project.id} is {self.project.kind_label}: a quick review reads the "
                              "code of a code project")
        if not self.project.has_workspace:
            raise ReviewError(f"project {self.project.id} has no workspace to read")
        self.change_range, self.patch, self.patch_base = change_range, patch, patch_base
        self.ref, self.base_ref, self.expect_sha, self.action_id = ref, base_ref, expect_sha, action_id
        self.root = root or review_root()
        self.run_id = run_id or f"review_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.record = self.root / self.run_id
        self.language_name = language_name
        self._scratch = Path(tempfile.mkdtemp(prefix="guardian-review-"))
        self.events = EventWriter(str(self.record / "events.jsonl"), thread_id=self.run_id)
        try:
            self.engine = DebateEngine(max_rounds=1, db_path=self._scratch / "session.db", jev_gate=False,
                                       events=self.events)
            self.persona_id, self.persona = resolve_persona(self.engine.persona_mgr, role)
            available = self.engine.llm.available_providers
            if provider and provider not in available:
                raise ReviewError(f"provider {provider!r} is not available here ({', '.join(available) or 'none'})")
            self.providers: Optional[List[str]] = [provider] if provider else None
        except BaseException:
            self.close()
            raise

    def _fetch_ref(self) -> Tuple[str, str]:
        """(base sha, head sha): the branch fetched from the project's origin into this run's private refs.

        The only remote is the workspace's configured `origin`; no URL ever comes from the caller. The fetch uses
        explicit refspecs into refs/guardian/review/<run_id>/ under the workspace lock, so refs/remotes/origin/*,
        local branches, HEAD, the index and the working tree stay untouched.
        """
        workspace = self.project.workspace
        branch = self.ref or ""
        if not branch.strip() or branch.startswith("-"):
            raise ReviewError(f"invalid branch name {branch!r}")
        try:
            validate_ref(branch)
        except ContextError as exc:
            raise ReviewError(str(exc)) from exc
        _check_branch_name(workspace, branch)
        head_ref, base_tip = f"{REVIEW_NS}/{self.run_id}/head", f"{REVIEW_NS}/{self.run_id}/base-tip"
        refspecs = [f"+refs/heads/{branch}:{head_ref}", f"+refs/heads/{self.project.branch}:{base_tip}"]
        try:
            with workspace_lock(workspace, "a publish is running in this workspace; try again when it has finished"):
                _fetch_origin(workspace, refspecs)
        except ProjectValidationError as exc:
            raise ReviewError(str(exc)) from exc
        try:
            head = resolve_sha(workspace, head_ref)
            base = (resolve_sha(workspace, self.base_ref) if self.base_ref
                    else self._merge_base(workspace, head_ref, base_tip))
        except ContextError as exc:
            raise ReviewError(str(exc)) from exc
        if self.expect_sha and self.expect_sha != head:
            raise ReviewError(f"branch {branch} is at {head}, not the expected {self.expect_sha}: it moved since; "
                              "start the review again")
        return base, head

    @staticmethod
    def _merge_base(workspace: Path, head_ref: str, base_tip: str) -> str:
        """The merge-base of the fetched head and the project branch's tip."""
        try:
            found = _git(workspace, "merge-base", head_ref, base_tip).strip().splitlines()
        except ReviewError as exc:
            raise ReviewError(f"the branch and the project branch share no common commit ({exc})") from None
        if not found:
            raise ReviewError("the branch and the project branch share no common commit")
        return found[0].strip()

    def _review_action(self) -> Tuple[str, str]:
        """(base sha, head sha): a PROPOSED apply_patch action's edits, committed without touching the workspace."""
        action = load_action(self.action_id, self.project.id)
        return action["base"], commit_edits(self.project.workspace, action["base"], action["edits"])

    def _change(self) -> Tuple[str, str, str]:
        """(base sha, head sha, diff text) of the change; refuses a change above the line limit."""
        workspace = self.project.workspace
        try:
            if self.change_range:
                base_ref, head_ref = parse_range(self.change_range)
                base, head = resolve_sha(workspace, base_ref), resolve_sha(workspace, head_ref)
            elif self.patch is not None:
                base = resolve_sha(workspace, self.patch_base or "HEAD")
                head = commit_patch(workspace, base, self.patch or "")
            elif self.ref:
                base, head = self._fetch_ref()
            else:
                base, head = self._review_action()
        except ContextError as exc:
            raise ReviewError(str(exc)) from exc
        if base == head:
            raise ReviewError("the change is empty: base and head are the same commit")
        diff = _git(workspace, "diff", "--no-color", "--no-ext-diff", "--find-renames", "--unified=3", base, head)
        if not diff.strip():
            raise ReviewError("the change is empty: no file differs between base and head")
        return base, head, diff

    def _describe(self, base: str) -> str:
        if self.change_range:
            return f"range {self.change_range}"
        if self.patch is not None:
            return f"a patch on {base[:12]}"
        if self.ref:
            return f"branch {self.ref} of the project's origin remote"
        return f"the edits of action #{self.action_id}"

    def _source(self, base: str, head: str) -> Dict[str, Any]:
        """The `source` field of result.json: which change input this run reviewed."""
        if self.change_range:
            return {"kind": "range", "range": self.change_range}
        if self.patch is not None:
            return {"kind": "patch"}
        if self.ref:
            return {"kind": "ref", "ref": self.ref, "base": base, "head": head, "base_ref": self.base_ref or ""}
        return {"kind": "action", "action_id": self.action_id, "base": base, "head": head}

    def _task(self, base: str, head: str, diff: str, change: Dict[str, Any]) -> str:
        what = self._describe(base)
        return "\n".join([
            f"Project: {self.project.id}",
            f"Change: {what}, base {base[:12]}, head {head[:12]}: {len(change['files'])} files, "
            f"+{change['added']} -{change['removed']} lines.",
            "The repository tools show the head commit (the code after the change).",
            "",
            "--- Diff ---",
            diff.rstrip(),
            "--- End of diff ---",
            "",
            PRECISION_RULES,
            "",
            FINDINGS_FORMAT,
        ])

    def run(self) -> Dict[str, Any]:
        if not retrieval_enabled():
            self.close()
            raise ReviewError("retrieval is switched off (GUARDIAN_RETRIEVAL=0): a review cannot read code without it")
        try:
            with solo_slot(self.root, max_reviews()):
                return self._run()
        except SoloError as exc:  # the shared slot helper speaks of solo runs
            raise ReviewError(f"busy: {max_reviews()} review(s) already running (GUARDIAN_MAX_REVIEWS)",
                              exc.code) from None
        finally:
            self.close()

    def _run(self) -> Dict[str, Any]:
        record = self.record
        engine, started = self.engine, time.monotonic()
        try:
            try:
                base, head, diff = self._change()
                change = parse_diff(diff)
                limit = max_lines()
                if change["added"] + change["removed"] > limit:
                    raise ReviewError(f"the change has {change['added'] + change['removed']} changed lines, above the "
                                      f"limit of {limit} (GUARDIAN_REVIEW_MAX_LINES): review it in smaller parts")
            except ReviewError:
                if self.ref:  # a refused run leaves no record, so its fetched refs have nothing to outlive
                    _delete_review_refs(self.project.workspace, self.run_id)
                raise
            record.mkdir(parents=True, exist_ok=True)
            language = self.language_name or os.environ.get("GUARDIAN_REVIEW_LANGUAGE", "").strip() or DEFAULT_LANGUAGE
            engine._wiki_exposure = False
            engine._findings_rules = False  # FINDINGS_FORMAT replaces the debate's optional findings block
            engine._language_rule = answer_language_rule(language, debate=False)
            engine._context_changed = False
            engine._ops = None
            engine.mcp = MCPClient(workspace=self.project.workspace, project=self.project)
            # The changed files at the head commit go in as the context pack (the code pack's own size limits skip
            # the rest), so the session counts them as given and a citation of them needs no extra read. A change
            # that only deletes files has no pack; retrieval is then pinned to the head commit directly.
            try:
                pack = (engine._resolve_context(SimpleNamespace(context=None), None, change["files"], head)
                        if change["files"] else None)
            except ContextError as exc:
                raise ReviewError(f"context: {exc}") from exc
            pack = engine._setup_retrieval(pack, None if pack else {"sha": head}, self.project.id)
            engine._flush_compiler_events()
            engine._context_text = pack.text if pack and pack.snapshot.get("paths") else ""
            repo = engine._repo
            if repo is None:
                raise ReviewError("retrieval is not available for this workspace (not a git repository)")
            self.events.emit("review_started", {
                "project": self.project.id, "role": self.persona_id, "base": base, "head": head,
                "files": len(change["files"]), "added": change["added"], "removed": change["removed"],
                "prompt_version": REVIEW_PROMPT_VERSION, "source": self._source(base, head)})
            name = str(self.persona.get("name") or EXPERT_ROLE)
            prompt = RolePrompt(persona=persona_text(self.persona), task=self._task(base, head, diff, change),
                                rules=f"{REVIEW_RULES} {engine._language_rule} Keep the quotes in their original language.")
            code_tools = "mcp" if repo_mcp_enabled() else None

            def ask(message: Any, round_num: int, trace: Dict[str, Any]) -> Tuple[str, str, List[str]]:
                return engine._ask(self.run_id, EXPERT_ROLE, message, {}, "", SESSION_KEY, self.providers,
                                   round_num=round_num, agent_name=name, trace=trace, code_tools=code_tools)

            trace: Dict[str, Any] = {}
            provider, text, failures = ask(prompt, 1, trace)
            items, problem = None, None
            if provider != BUILTIN_PROVIDER:
                items, problem, _cut = parse_audit_findings(text)
                if items is None or problem:
                    again_trace: Dict[str, Any] = {}
                    provider_2, text_2, failures_2 = ask(followup(problem), 2, again_trace)
                    failures += failures_2
                    if provider_2 != BUILTIN_PROVIDER:
                        again, again_problem, _ = parse_audit_findings(text_2)
                        if again is not None and not again_problem:
                            provider, text, items, problem, trace = provider_2, text_2, again, None, again_trace
            degraded = provider == BUILTIN_PROVIDER
            with engine._retrieval_lock:
                engine._provided_set(SESSION_KEY, EXPERT_ROLE, name)
                hunks = change["hunks"]

                def shown(path: str) -> List[Tuple[int, int]]:
                    return coalesce([*engine._shown_spans(SESSION_KEY, path), *hunks.get(path, [])])

                findings, notes = ([], []) if degraded or items is None or problem else sort_findings(
                    repo, items, shown, change, citation_slack())
            state = "degraded" if degraded else ("bad_format" if items is None or problem else "done")
            # `change` keeps its V1 shape for range and patch runs; `source` names the input of every kind.
            change_field: Dict[str, Any] = ({"range": self.change_range} if self.change_range
                                            else ({"patch": True} if self.patch is not None else {}))
            result: Dict[str, Any] = {
                "id": self.run_id, "label": LABEL, "note": NOTE, "prompt_version": REVIEW_PROMPT_VERSION,
                "state": state, "project": self.project.id, "role": self.persona_id, "provider": provider,
                "change": change_field, "source": self._source(base, head),
                "base": base, "head": head, "files": change["files"], "added": change["added"],
                "removed": change["removed"], "findings": findings, "notes": notes,
                "summary": summary_line(findings, notes) if state == "done" else (
                    "no provider answered" if degraded else f"no readable findings block ({problem or 'missing'})"),
                "text": mask_secrets(text) if state != "done" and not degraded else "",
                "degraded": degraded, "steps": trace.get("steps"),
                "failures": [*failures, *([engine.llm.last_error] if degraded and engine.llm.last_error else [])],
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "elapsed_s": round(time.monotonic() - started, 1),
            }
            self.events.emit("review_finished", {"state": state, "summary": result["summary"]})
            self._store(record, result)
            return result
        finally:
            engine._close_go_index()
            self.events.close()

    @staticmethod
    def _store(record: Path, result: Dict[str, Any]) -> None:
        record.mkdir(parents=True, exist_ok=True)
        tmp = record / "result.json.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, record / "result.json")

    def close(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)


def render(result: Dict[str, Any]) -> str:
    """The plain-text form of a result: the label first, so it is never read as a verdict."""
    head = f"{LABEL} ({result.get('note') or NOTE})" + (" - DEGRADED: no provider answered" if result.get("degraded") else "")
    source = result.get("source") or {}
    kind = str(source.get("kind") or "")
    if kind == "ref":
        what = f"branch {source.get('ref')}"
    elif kind == "action":
        what = f"action #{source.get('action_id')}"
    else:
        change = result.get("change") or {}
        what = change.get("range") or source.get("range") or f"patch on {str(result.get('base') or '')[:12]}"
    lines = [head, f"project {result['project']} | {what} | role {result['role']} | provider {result['provider']}",
             f"{len(result.get('files') or [])} files, +{result.get('added', 0)} -{result.get('removed', 0)} | "
             f"{result['summary']}", ""]
    for finding in result.get("findings") or []:
        lines.append(f"[{finding['severity']}] {finding['path']}:{finding['line']}  {finding['claim']}")
        if finding.get("scenario"):
            lines.append(f"    scenario: {finding['scenario']}")
    if result.get("notes"):
        lines += ["", f"notes ({len(result['notes'])}):"]
        for note in result["notes"][:20]:
            lines.append(f"  {note['path']}:{note['line']}  {note['claim'][:160]}  ({note['reason']})")
    if result.get("text"):
        lines += ["", str(result["text"]).rstrip()]
    return "\n".join(lines).rstrip() + "\n"


def prune(root: Optional[Path] = None, days: Optional[int] = None, now: Optional[float] = None) -> List[str]:
    """Removes review records older than `days` (default 30, GUARDIAN_REVIEW_KEEP_DAYS) and their review refs
    (refs/guardian/review/<id>/*) in the record's project, then sweeps every code project workspace for review
    refs whose record no longer exists. Returns the removed ids."""
    root = root or review_root()
    if days is None:
        days = _env_int("GUARDIAN_REVIEW_KEEP_DAYS", DEFAULT_KEEP_DAYS, minimum=0)
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed: List[str] = []
    if root.is_dir():
        for entry in sorted(root.glob("review_*")):
            stamp = entry / "result.json"
            mtime = (stamp if stamp.exists() else entry).stat().st_mtime
            if entry.is_dir() and mtime < cutoff:
                project = _record_project(entry)
                shutil.rmtree(entry, ignore_errors=True)
                removed.append(entry.name)
                if project:
                    workspace = _workspace_of(project)
                    if workspace is not None:
                        _delete_review_refs(workspace, entry.name)
    _sweep_review_refs(root)
    return removed

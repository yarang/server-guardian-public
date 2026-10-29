"""
Whole-repository audit (docs/design/repo-audit-mode.md): plan and checkpoint store. No LLM, no actions.

An audit asks one question of every unit in a scope. This module owns the parts that must be deterministic and
independent of any model:

- build_plan(): the unit list (declarations of the code index plus whole files without declarations), the shards
  (consecutive files in directory order up to GUARDIAN_AUDIT_SHARD_BYTES) and the work key of each shard. The same
  commit, question and scope give the same plan; nothing in it comes from a model or from the clock.
- AuditStore: <GUARDIAN_DATA_DIR>/repo-audit/<project>/<audit id>/ with plan.json, audit.json (state), one record per
  shard under shards/ and (later) report.json. Writes go through a temporary file and a rename. A shard counts as
  done only when its record is `done` and carries the key the plan has for it today.

The audit is read-only: this module must not import the action machinery (a test checks it). A finding becomes a
change only when a person starts an ordinary debate from it.

Phase A2 adds the run lock, the report (build_report: a pure function of the plan and the shard records) and the
shard record format written by audit_run.py:
{state, key, provider, started_at, finished_at, coverage: "complete"|"incomplete", unseen: [unit ids], findings:
[{shard, unit, path, line, quote, claim, severity, verified, reason}], error}.
"""

import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

try:  # POSIX only; without it the run lock does not exclude a second run
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from code_context import is_secret, matches
from code_index import BLOB_BATCH, MAX_INDEXED_BYTES
from code_wiki import data_root

AUDIT_PLAN_VERSION = 1
# Part of every shard key: bump it when the audit prompt or the findings format changes (A2), so that finished shards
# of the old wording are not taken for finished work of the new one.
AUDIT_PROMPT_VERSION = 3  # 2: A2 shard prompt and findings block; 3: precision rules, scenario, re-check
REPORT_VERSION = 1
DEFAULT_SHARD_BYTES = 120 * 1024
MIN_SHARD_BYTES = 1024
SHARD_STATES = ("done", "deferred", "failed")
COVERAGE_STATES = ("complete", "incomplete")
AUDIT_ID = re.compile(r"^[a-z0-9_]{1,64}$")
SHARD_ID = re.compile(r"^s[0-9]{4,}$")
_PROJECT_ID = re.compile(r"[^A-Za-z0-9._-]")
_SYMLINK_MODE = "120000"
_SUBMODULE_MODE = "160000"
_NUL_WINDOW = 8192


class AuditError(Exception):
    """An unusable question, scope, audit id or shard record."""


class AuditBusy(AuditError):
    """Another run holds the audit's lock (exit code 4)."""


def shard_byte_limit() -> int:
    """GUARDIAN_AUDIT_SHARD_BYTES (default 120 KiB, at least 1 KiB)."""
    try:
        value = int(os.environ.get("GUARDIAN_AUDIT_SHARD_BYTES", DEFAULT_SHARD_BYTES))
    except ValueError:
        return DEFAULT_SHARD_BYTES
    return max(MIN_SHARD_BYTES, value)


def normalize_question(question: str) -> str:
    return " ".join(str(question or "").split())


def question_hash(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()[:16]


def default_audit_dir(project_id: Optional[str]) -> Optional[Path]:
    """<GUARDIAN_DATA_DIR>/repo-audit/<project>, or None when the project id is unusable. Creates nothing.

    Like code-wiki/ the directory stays in the nightly data tarball: an audit costs LLM time that cannot be recovered.
    """
    cleaned = _PROJECT_ID.sub("_", str(project_id or "")).strip(".")
    if not cleaned:
        return None
    return data_root() / "repo-audit" / cleaned[:80]


# --- plan ----------------------------------------------------------------------------------------------------------

def _in_scope(path: str, scope: Sequence[str]) -> bool:
    return not scope or any(matches(path, pattern) for pattern in scope)


def _line_count(data: bytes) -> int:
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _excluded_entries(tools: Any, scope: Sequence[str]) -> List[Dict[str, str]]:
    """Paths of the commit that an agent may not see, with the reason (never their content)."""
    found = []
    for path, (mode, _size, _blob) in tools.entries.items():
        if not _in_scope(path, scope):
            continue
        if mode == _SYMLINK_MODE:
            found.append({"path": path, "reason": "symlink"})
        elif mode == _SUBMODULE_MODE:
            found.append({"path": path, "reason": "submodule"})
        elif is_secret(path):
            found.append({"path": path, "reason": "secret"})
    return found


def _text_files(tools: Any, paths: List[str], excluded: List[Dict[str, str]]) -> Dict[str, int]:
    """path -> line count of the readable files the code index does not parse; empty, large and binary ones go to
    `excluded`. Blobs are read in batches only to look for NUL bytes and to count lines."""
    lines: Dict[str, int] = {}
    todo = []
    for path in paths:
        _mode, size, _blob = tools.entries[path]
        if size == 0:
            excluded.append({"path": path, "reason": "empty"})
        elif size > MAX_INDEXED_BYTES:
            excluded.append({"path": path, "reason": "large"})
        else:
            todo.append(path)
    for offset in range(0, len(todo), BLOB_BATCH):
        chunk = todo[offset:offset + BLOB_BATCH]
        blobs = tools._read_blobs(list(dict.fromkeys(tools.entries[p][2] for p in chunk)))
        for path in chunk:
            data = blobs.get(tools.entries[path][2])
            if data is None or b"\0" in data[:_NUL_WINDOW]:
                excluded.append({"path": path, "reason": "binary"})
            else:
                lines[path] = _line_count(data)
    return lines


def _unit(path: str, name: str, kind: str, start: int, end: int, blob: str, taken: set) -> Dict[str, Any]:
    base = f"{path}:{start}-{end}"
    unit_id, n = base, 1
    while unit_id in taken:  # two declarations on the same lines (grouped declarations)
        n += 1
        unit_id = f"{base}#{n}"
    taken.add(unit_id)
    return {"id": unit_id, "path": path, "name": name, "kind": kind, "start": start, "end": end, "blob": blob}


def _build_units(tools: Any, scope: Sequence[str], excluded: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    index = tools.code_index()
    readable = [p for p in tools.readable_paths() if _in_scope(p, scope)]
    large = set(index.skipped_large)
    for path in sorted(large.intersection(readable)):
        excluded.append({"path": path, "reason": "large"})
    decls: Dict[str, List[Dict[str, Any]]] = {}
    for decl in index.decls:
        decls.setdefault(decl["path"], []).append(decl)
    plain = [p for p in readable if p not in index.lines and p not in large]
    plain_lines = _text_files(tools, plain, excluded)

    taken: set = set()
    units: List[Dict[str, Any]] = []
    for path in readable:
        blob = tools.entries[path][2]
        if path in large:
            continue
        if path in plain_lines:
            units.append(_unit(path, path, "file", 1, plain_lines[path], blob, taken))
        elif path in index.lines:
            if decls.get(path):
                for decl in decls[path]:
                    units.append(_unit(path, decl["qualified"], decl["kind"], decl["start"], decl["end"], blob, taken))
            else:  # a source file without declarations (a parse error also lands here): the file is the unit
                units.append(_unit(path, path, "file", 1, max(1, index.lines[path]), blob, taken))
    units.sort(key=lambda u: (os.path.dirname(u["path"]), os.path.basename(u["path"]), u["start"], u["end"], u["id"]))
    return units


def _shards(tools: Any, units: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Consecutive files in (directory, name) order, a new shard when the next file would pass the limit or when a
    new directory starts and the current shard is at least half full. A file larger than the limit is a shard of its
    own (`oversized`): files are never split."""
    by_file: Dict[str, List[str]] = {}
    for unit in units:
        by_file.setdefault(unit["path"], []).append(unit["id"])
    ordered = sorted(by_file, key=lambda p: (os.path.dirname(p), os.path.basename(p)))
    groups: List[List[str]] = []
    current: List[str] = []
    used = 0
    for path in ordered:
        size = tools.entries[path][1]
        new_dir = bool(current) and os.path.dirname(path) != os.path.dirname(current[-1])
        if current and (used + size > limit or (new_dir and used * 2 >= limit)):
            groups.append(current)
            current, used = [], 0
        current.append(path)
        used += size
    if current:
        groups.append(current)
    shards = []
    for number, group in enumerate(groups, 1):
        files = [{"path": p, "blob": tools.entries[p][2], "size": tools.entries[p][1]} for p in group]
        total = sum(f["size"] for f in files)
        shards.append({
            "id": f"s{number:04d}", "dir": os.path.commonpath([os.path.dirname(p) or "." for p in group]),
            "files": files, "units": [uid for p in group for uid in by_file[p]],
            "bytes": total, "oversized": len(group) == 1 and total > limit})
    return shards


def shard_key(project: str, sha: str, qhash: str, shard: Dict[str, Any]) -> str:
    """Work key of a shard: project, commit, question, shard id, prompt version and the blobs it covers."""
    blobs = hashlib.sha256("\n".join(f"{f['path']}\0{f['blob']}" for f in shard["files"]).encode("utf-8")).hexdigest()
    raw = json.dumps([project, sha, qhash, shard["id"], AUDIT_PROMPT_VERSION, blobs], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_plan(tools: Any, project_id: str, sha: str, question: str, scope: Optional[Iterable[str]] = None,
               shard_limit: Optional[int] = None) -> Dict[str, Any]:
    """The audit plan of one commit: {version, project, sha, question, question_hash, scope, prompt_version,
    shard_limit, units, shards, excluded, totals}. tools is a repo_tools.RepoTools of that commit. No model is
    involved and no clock: the same inputs give the same dict."""
    text = normalize_question(question)
    if not text:
        raise AuditError("question: the audit needs a question")
    patterns = sorted({str(p).strip() for p in (scope or []) if str(p).strip()})
    limit = shard_limit if shard_limit is not None else shard_byte_limit()
    excluded = _excluded_entries(tools, patterns)
    units = _build_units(tools, patterns, excluded)
    shards = _shards(tools, units, limit)
    qhash = question_hash(text)
    for shard in shards:
        shard["key"] = shard_key(project_id, sha, qhash, shard)
    excluded.sort(key=lambda e: (e["path"], e["reason"]))
    return {
        "version": AUDIT_PLAN_VERSION, "project": project_id, "sha": sha, "question": text, "question_hash": qhash,
        "scope": patterns, "prompt_version": AUDIT_PROMPT_VERSION, "shard_limit": limit,
        "units": units, "shards": shards, "excluded": excluded,
        "totals": {"units": len(units), "files": sum(len(s["files"]) for s in shards),
                   "bytes": sum(s["bytes"] for s in shards), "shards": len(shards)},
    }


def plan_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    """The numbers a person confirms before an audit starts: units, files, bytes, shards, excluded paths by reason."""
    reasons: Dict[str, int] = {}
    for item in plan["excluded"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    return {"project": plan["project"], "sha": plan["sha"], "question": plan["question"], "scope": plan["scope"],
            "units": plan["totals"]["units"], "files": plan["totals"]["files"], "bytes": plan["totals"]["bytes"],
            "shards": plan["totals"]["shards"], "oversized": sum(1 for s in plan["shards"] if s["oversized"]),
            "excluded": reasons}


# --- store ---------------------------------------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class AuditStore:
    """The audits of one project under `root` (default_audit_dir(project))."""

    def __init__(self, root: Optional[Path]) -> None:
        if root is None:
            raise AuditError("project id is not usable for the audit directory")
        self.root = Path(root)

    def _dir(self, audit_id: str) -> Path:
        if not isinstance(audit_id, str) or not AUDIT_ID.fullmatch(audit_id):
            raise AuditError(f"audit id: use 1-64 lowercase letters, digits or _ ({audit_id!r})")
        return self.root / audit_id

    def create(self, plan: Dict[str, Any]) -> str:
        """Writes plan.json and audit.json of a new audit and returns its id."""
        for _attempt in range(5):
            audit_id = f"audit_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
            directory = self._dir(audit_id)
            try:
                directory.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            _write_json(directory / "plan.json", plan)
            _write_json(directory / "audit.json", {
                "id": audit_id, "state": "planned", "created_at": _utc_now(), "project": plan["project"],
                "sha": plan["sha"], "question": plan["question"], "question_hash": plan["question_hash"],
                "shards": plan["totals"]["shards"], "units": plan["totals"]["units"]})
            return audit_id
        raise AuditError("could not allocate an audit id")

    def load_plan(self, audit_id: str) -> Dict[str, Any]:
        plan = _read_json(self._dir(audit_id) / "plan.json")
        if not isinstance(plan, dict) or plan.get("version") != AUDIT_PLAN_VERSION:
            raise AuditError(f"audit {audit_id}: no readable plan")
        return plan

    def meta(self, audit_id: str) -> Dict[str, Any]:
        meta = _read_json(self._dir(audit_id) / "audit.json")
        if not isinstance(meta, dict):
            raise AuditError(f"audit {audit_id}: no readable metadata")
        return meta

    def set_state(self, audit_id: str, state: str, **extra: Any) -> Dict[str, Any]:
        meta = {**self.meta(audit_id), "state": state, "updated_at": _utc_now(), **extra}
        _write_json(self._dir(audit_id) / "audit.json", meta)
        return meta

    def list(self) -> List[Dict[str, Any]]:
        """Metadata of the project's audits, newest first (ids start with their creation time)."""
        found = []
        try:
            names = sorted((p.name for p in self.root.iterdir() if p.is_dir() and AUDIT_ID.fullmatch(p.name)), reverse=True)
        except OSError:
            return []
        for name in names:
            meta = _read_json(self.root / name / "audit.json")
            if isinstance(meta, dict):
                found.append(meta)
        return found

    def save_shard(self, audit_id: str, shard_id: str, record: Dict[str, Any]) -> None:
        plan = self.load_plan(audit_id)
        if not SHARD_ID.fullmatch(str(shard_id)) or shard_id not in {s["id"] for s in plan["shards"]}:
            raise AuditError(f"audit {audit_id}: unknown shard {shard_id!r}")
        if record.get("state") not in SHARD_STATES:
            raise AuditError(f"shard state: one of {', '.join(SHARD_STATES)} ({record.get('state')!r})")
        _write_json(self._dir(audit_id) / "shards" / f"{shard_id}.json", {**record, "shard": shard_id})

    def load_shard(self, audit_id: str, shard_id: str) -> Optional[Dict[str, Any]]:
        if not SHARD_ID.fullmatch(str(shard_id)):
            return None
        record = _read_json(self._dir(audit_id) / "shards" / f"{shard_id}.json")
        return record if isinstance(record, dict) else None

    def pending(self, audit_id: str) -> List[Dict[str, Any]]:
        """The plan's shards that are not done, in plan order. A shard is done only when its record says `done` and
        carries the key the plan holds for it; an unreadable record means the shard is run again."""
        plan = self.load_plan(audit_id)
        remaining = []
        for shard in plan["shards"]:
            record = self.load_shard(audit_id, shard["id"])
            if record and record.get("state") == "done" and record.get("key") == shard["key"]:
                continue
            remaining.append(shard)
        return remaining

    # --- run lock and report (A2) ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def run_lock(self, audit_id: str) -> Iterator[None]:
        """Held for a whole run of one audit; a second run of it raises AuditBusy. Another audit of the project is not
        excluded here (the hub's audit slot does that, phase A3)."""
        directory = self._dir(audit_id)
        if not directory.is_dir():
            raise AuditError(f"audit {audit_id}: unknown audit")
        fd = os.open(directory / "run.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)  # never through a link
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise AuditBusy(f"audit {audit_id} is already running") from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def records(self, audit_id: str) -> Dict[str, Optional[Dict[str, Any]]]:
        plan = self.load_plan(audit_id)
        return {s["id"]: self.load_shard(audit_id, s["id"]) for s in plan["shards"]}

    def save_report(self, audit_id: str, report: Dict[str, Any]) -> None:
        _write_json(self._dir(audit_id) / "report.json", report)

    def load_report(self, audit_id: str) -> Optional[Dict[str, Any]]:
        report = _read_json(self._dir(audit_id) / "report.json")
        return report if isinstance(report, dict) and report.get("version") == REPORT_VERSION else None


def build_report(plan: Dict[str, Any], records: Dict[str, Optional[Dict[str, Any]]], state: str) -> Dict[str, Any]:
    """The report of an audit from its plan and shard records, with no model and no clock besides `generated_at`:
    {version, state, header, totals, findings (verified), unverified, notes, unseen, unaudited}. `notes` are verified
    findings a shard set aside (below the severity floor, or not confirmed by its re-check), each with its `note`.

    A shard counts only when its record is `done` with the key the plan holds today. Every other shard is listed in
    `unaudited` with the reason (pending, deferred, failed, stale), so a reader sees what the findings do NOT cover.
    Within a counted shard, units the session was never shown completely are listed in `unseen`.
    """
    findings: List[Dict[str, Any]] = []
    unverified: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    unseen: List[Dict[str, Any]] = []
    unaudited: List[Dict[str, Any]] = []
    audited_units = unseen_units = unaudited_units = 0
    shard_counts = {"done": 0, "deferred": 0, "failed": 0, "pending": 0, "stale": 0}
    units = {u["id"]: u for u in plan["units"]}
    for shard in plan["shards"]:
        record = records.get(shard["id"])
        count = len(shard["units"])
        if not record:
            reason = "pending"
        elif record.get("key") != shard["key"]:
            reason = "stale"
        elif record.get("state") != "done":
            reason = str(record.get("state"))
        else:
            reason = ""
        if reason:
            shard_counts[reason if reason in shard_counts else "failed"] += 1
            unaudited_units += count
            entry: Dict[str, Any] = {"shard": shard["id"], "reason": reason, "units": count, "dir": shard["dir"]}
            if record and record.get("error"):
                entry["error"] = str(record["error"])[:300]
            unaudited.append(entry)
            continue
        shard_counts["done"] += 1
        missed = [u for u in (record.get("unseen") or []) if u in shard["units"]]
        unseen_units += len(missed)
        audited_units += count - len(missed)
        for unit_id in missed:
            unit = units.get(unit_id) or {}
            unseen.append({"shard": shard["id"], "unit": unit_id, "path": unit.get("path", ""),
                           "start": unit.get("start"), "end": unit.get("end")})
        for item in record.get("findings") or []:
            if isinstance(item, dict):
                (findings if item.get("verified") else unverified).append({**item, "shard": shard["id"]})
        notes += [{**item, "shard": shard["id"]} for item in record.get("notes") or [] if isinstance(item, dict)]
    order = lambda f: (str(f.get("path", "")), int(f.get("line") or 0), str(f.get("claim", "")))  # noqa: E731
    findings.sort(key=order)
    unverified.sort(key=order)
    notes.sort(key=order)
    return {
        "version": REPORT_VERSION, "state": state, "generated_at": _utc_now(), "project": plan["project"],
        "sha": plan["sha"], "question": plan["question"], "scope": plan["scope"],
        "prompt_version": plan["prompt_version"],
        "totals": {"units": plan["totals"]["units"], "units_audited": audited_units, "units_unseen": unseen_units,
                   "units_unaudited": unaudited_units, "shards": plan["totals"]["shards"],
                   "shards_by_state": shard_counts, "findings": len(findings), "unverified": len(unverified),
                   "notes": len(notes)},
        "findings": findings, "unverified": unverified, "notes": notes, "unseen": unseen, "unaudited": unaudited,
        "excluded": plan_summary(plan)["excluded"],
    }

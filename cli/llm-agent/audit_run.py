"""
Runs the shards of a planned repository audit (docs/design/repo-audit-mode.md, phase A2).

One shard is one LLM session of the Expert member of a team, run through the same retrieval loop as a debate answer
(repo blocks or MCP tools, the line-span citation check with its single re-ask, the findings check). There is no
Critic panel and no judge: the deterministic checks below take their place.

- A shard's answer must end in one ```findings block (a JSON list; an empty list says "nothing found"). Each finding is
  checked without a model: the quote must be in the file within GUARDIAN_CITATION_SLACK lines of `line`, and that line
  must have been shown to the session. Findings that fail stay in the report as `unverified`.
- Coverage is deterministic as well: a unit counts as audited only when every one of its lines was shown to the session
  (the engine's served spans). Units that were not are re-asked once; what is still missing is stored as `unseen`
  and the shard is `incomplete`, never silently complete.
- Precision step (A4 second run): verified findings below the severity floor (GUARDIAN_AUDIT_MIN_SEVERITY) and those
  that a second, fresh re-check session of the same member does not confirm with a concrete failure move to the
  shard's `notes`; only confirmed findings stay in the report's findings. GUARDIAN_AUDIT_RECHECK=off skips the re-check.
- A provider that is out of quota defers the shard (state `deferred`) and stops the run; the audit resumes later with
  the shards that are not done. Any other failure marks the shard `failed` and the run goes on.

The run never creates actions: this module calls only the engine's `_ask` (an agent turn) and never the debate flow
that parses actions; a test feeds an answer with an action block and checks that no action row exists.
"""

import contextlib
import datetime
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from code_context import ContextError, resolve_sha
from debate_engine import DebateEngine, answer_language_rule, retrieval_enabled
from debate_events import EventWriter
from evidence import (
    FINDINGS_BLOCK,
    MAX_QUOTE_CHARS,
    check_findings,
    citation_slack,
    describe_spans,
    is_whole,
    line_covered,
    lines_covered,
)
from llm_runner import BUILTIN_PROVIDER
from mcp_client import MCPClient
from project_registry import ProjectManager
from repo_audit import AUDIT_PROMPT_VERSION, AuditBusy, AuditError, AuditStore, build_report, default_audit_dir
from repo_map import root_summary
from team_manager import EXPERT_ROLE, TeamManager

# The auditor's role, the head of the session's system prompt (see wiki_build.WRITER_SYSTEM for how it is applied).
AUDIT_SYSTEM = (
    "You are a code auditor. You check one part of a software repository against one question given by a person and "
    "report only what you can prove from the code you have read in this session, with the exact line and a verbatim "
    "quote. You never invent files, symbols or behaviour, and you do not propose patches or changes. You have no "
    "tools other than the repository requests described in this conversation: you cannot open files, run commands "
    "or look for earlier conversations. Read in small batches: a few files or line ranges per repo block. One very "
    "large batch of results can push the earlier conversation out of your context."
)

# The re-check session's role: a second reader who has to be convinced, not the author defending its list.
RECHECK_SYSTEM = (
    "You are a reviewer of code audit findings. Another auditor reported the findings below; many findings of such an "
    "audit describe intended or harmless behaviour. You confirm a finding only when you can state a concrete failure "
    "from the code: which input or state, through which path, leads to which wrong result or harm. You never invent "
    "files, symbols or behaviour, and you do not propose patches or changes. You have no tools other than the "
    "repository requests described in this conversation."
)

DEFAULT_LANGUAGE = "Korean"
DEFAULT_AUDIT_STEP_BYTES = 512 * 1024
MAX_CLAIM_CHARS = 800
MAX_UNSEEN_LISTED = 40
MAX_EVENT_UNVERIFIED = 20
SEVERITIES = ("high", "medium", "low")
# Findings one shard answer may hold; the debate's 50 (evidence.MAX_FINDINGS) is too few for a part of a repository
# (the A4 second run averaged 22 per shard). The prompt states it, and a cut is recorded on the shard.
MAX_AUDIT_FINDINGS = 200
DEFAULT_MIN_SEVERITY = "medium"
RECHECK_SESSION_KEY = "audit-recheck"  # its own retrieval books, apart from the shard's coverage
RECHECK_EXCERPT_LINES = 80  # at most this many numbered lines of a finding's unit go into the re-check prompt
RECHECK_CONTEXT_LINES = 5  # lines before and after the unit
VERDICTS = ("confirmed", "rejected")
VERDICTS_BLOCK = re.compile(r"```verdicts[ \t]*\r?\n(?:(.*?)\r?\n)?[ \t]*```[ \t]*(?=\r?\n|$)", re.DOTALL)
# Providers of an Expert that names none. Only claude runs with every tool of its own switched off here; codex without
# the repo MCP tools and agy keep a shell that can read host files outside the repository, and an audit stores what the
# model writes. A team that lists providers itself is trusted to have chosen them.
AUDIT_PROVIDERS = ["claude"]
# Values of credential-looking assignments and private key blocks; masked in what the report stores (best effort).
_SECRET_VALUE = re.compile(
    r"(?i)((?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)s?[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s,;]+)")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL)
# Words of a provider's "out of quota" failure (llm_runner joins the CLI errors into engine.llm.last_error).
_RATE_LIMIT = re.compile(r"rate[ _-]?limit|usage limit|limit reached|quota|too many requests|\b429\b|overloaded|"
                         r"try again later|resets? (?:at|in)", re.IGNORECASE)


class RunError(Exception):
    """An audit that cannot run (unknown audit, bad project or team, retrieval off). `code` is the exit code."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def audit_language() -> str:
    return os.environ.get("GUARDIAN_AUDIT_LANGUAGE", "").strip() or DEFAULT_LANGUAGE


def audit_step_bytes() -> int:
    raw = os.environ.get("GUARDIAN_AUDIT_STEP_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_AUDIT_STEP_BYTES
    except ValueError:
        return DEFAULT_AUDIT_STEP_BYTES
    return value if value >= 0 else DEFAULT_AUDIT_STEP_BYTES


def audit_min_severity() -> str:
    """The lowest severity that stays in the report (GUARDIAN_AUDIT_MIN_SEVERITY: high, medium or low)."""
    value = os.environ.get("GUARDIAN_AUDIT_MIN_SEVERITY", "").strip().lower()
    return value if value in SEVERITIES else DEFAULT_MIN_SEVERITY


def recheck_enabled() -> bool:
    return os.environ.get("GUARDIAN_AUDIT_RECHECK", "").strip().lower() not in ("off", "0", "false", "no")


def below_floor(severity: str, floor: str) -> bool:
    return SEVERITIES.index(severity) > SEVERITIES.index(floor)


@contextlib.contextmanager
def step_limits() -> Iterator[None]:
    """Sets the engine's per-step byte limit for the sessions of one run (the smaller of the audit cap and an explicit
    GUARDIAN_RETRIEVAL_STEP_BYTES; 0 turns the cap off) and restores the environment afterwards."""
    cap = audit_step_bytes()
    key = "GUARDIAN_RETRIEVAL_STEP_BYTES"
    before = os.environ.get(key)
    try:
        if cap:
            current = int(before) if before and before.strip().isdigit() and int(before) > 0 else None
            os.environ[key] = str(min(cap, current) if current else cap)
        yield
    finally:
        if before is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before


def mask_secrets(text: str) -> str:
    """`text` with the values of credential-looking assignments and private key blocks replaced by ***."""
    return _SECRET_VALUE.sub(r"\1***", _PRIVATE_KEY.sub("***", text))


def is_rate_limited(error: Optional[str]) -> bool:
    return bool(error and _RATE_LIMIT.search(error))


# --- prompts and deterministic checks ------------------------------------------------------------------------------

FINDINGS_FORMAT = (
    "Answer with ONE ```findings block and nothing after it. The block is a JSON list; each item is an object:\n"
    '  {"path": "<file of this commit>", "line": <line number>, "quote": "<verbatim text of that line, at most 300 '
    'characters>", "claim": "<one or two sentences: how this code answers the question>", "scenario": "<the concrete '
    'failure: which input or state, through which path, gives which wrong result or harm>", "severity": "high" | '
    '"medium" | "low"}\n'
    "Severity: high = it goes wrong or can be abused in normal use; medium = it goes wrong under a condition that can "
    "really occur; low = it is a real instance but harmless in practice. Report one item per place, at the line where "
    f"the problem is, at most {MAX_AUDIT_FINDINGS} items; if you have more, keep the most severe. An empty list `[]` "
    "says that you checked every unit and found nothing. The block must be valid JSON: an unreadable block is asked "
    "for once more and then fails the part. Before the block you may add a short summary of what you checked."
)

# What made most of the false and debatable findings of the A4 second run (docs/analysis/2026-10-19-audit-a4b).
PRECISION_RULES = (
    "Report only real instances of the question, not everything that could be argued. Do NOT report:\n"
    "- behaviour that a comment, a docstring or a project document states as intended (\"on purpose\", \"by design\", "
    "\"best effort\", a documented default), unless the code contradicts that statement;\n"
    "- hardening suggestions such as a missing size cap, timeout or extra check, unless the code itself states that "
    "limit or rule and then breaks it;\n"
    "- values that only come from constants of the code, from the local operator's own command line or from a caller "
    "that already checked them (look at the callers before you call something unchecked);\n"
    "- anything for which you cannot write a concrete failure scenario."
)


def parse_audit_findings(text: str) -> Tuple[Optional[List[Any]], Optional[str], int]:
    """(items, problem, cut) of the last ```findings block of a shard answer.

    items is None without a block. A block that is there but cannot be read gives ([], problem): unlike a debate's
    optional block, an empty audit list means "checked and found nothing", so a broken one must not read as that.
    cut counts the items above MAX_AUDIT_FINDINGS that were left out."""
    blocks = FINDINGS_BLOCK.findall(text or "")
    if not blocks:
        return None, None, 0
    try:
        data = json.loads(blocks[-1])
    except ValueError as exc:
        return [], f"the findings block is not valid JSON ({exc.msg} at line {exc.lineno})", 0
    if not isinstance(data, list):
        return [], "the findings block is not a JSON list", 0
    return data[:MAX_AUDIT_FINDINGS], None, max(0, len(data) - MAX_AUDIT_FINDINGS)


def build_shard_prompt(plan: Dict[str, Any], shard: Dict[str, Any], language_rule: str) -> str:
    """The task of one shard session: the question, the files and units to check, and the answer format."""
    units: Dict[str, Dict[str, Any]] = {u["id"]: u for u in plan["units"]}
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for unit_id in shard["units"]:
        by_file.setdefault(units[unit_id]["path"], []).append(units[unit_id])
    lines = []
    for item in shard["files"]:
        lines.append(f"- {item['path']} ({item['size']} bytes)")
        for unit in by_file.get(item["path"], []):
            if unit["kind"] != "file":
                lines.append(f"    {unit['kind']} {unit['name']}: lines {unit['start']}-{unit['end']}")
    return "\n".join([
        f"Audit of project {plan['project']} at commit {plan['sha'][:12]}, part {shard['id']} "
        f"({len(shard['files'])} files, {len(shard['units'])} units).",
        "",
        "--- Question (apply it to every unit below) ---",
        plan["question"],
        "--- End of question ---",
        "",
        "Your part consists of these files (declarations are listed where the code index knows them):",
        *lines,
        "",
        "Work through it like this: read every file listed above completely (repo blocks, a few files per block), "
        "then judge each unit against the question. A unit counts as checked only when you were shown all of its "
        "lines, so do not skip any file and do not rely on outlines alone. Report only what the code itself shows; "
        "do not guess about code you did not read, and do not report style or other matters the question does not ask "
        "about. Do not propose patches, actions or tests.",
        "",
        PRECISION_RULES,
        "",
        FINDINGS_FORMAT,
        "",
        language_rule + " Keep the quotes in their original language.",
    ])


def build_followup(plan: Dict[str, Any], missing: List[Dict[str, Any]], no_block: bool,
                   problem: Optional[str] = None) -> str:
    """The single re-ask of a shard: the units not completely shown, the missing findings block and/or why the block
    could not be read."""
    parts = ["=== AUDIT CHECK ==="]
    if no_block:
        parts.append("Your answer has no ```findings block, so it cannot be used.")
    if problem:
        parts.append(f"Your ```findings block cannot be read: {problem}. Write it again as valid JSON.")
    if missing:
        parts.append("You were not shown all lines of these units, so they cannot count as checked:")
        for unit in missing[:MAX_UNSEEN_LISTED]:
            parts.append(f"  {unit['path']}:{unit['start']}-{unit['end']}")
        if len(missing) > MAX_UNSEEN_LISTED:
            parts.append(f"  (+{len(missing) - MAX_UNSEEN_LISTED} more units)")
        parts.append("Read them now (repo blocks).")
    parts.append("Then reply with the complete ```findings block for your whole part, including the findings you "
                 "already reported, in the format given before. The question remains: " + plan["question"])
    return "\n".join(parts)


def unseen_units(units: List[Dict[str, Any]], shown: Any, file_level: Set[str]) -> List[Dict[str, Any]]:
    """The units of which not every line was shown. `shown(path)` returns the shown spans of a path; files in
    `file_level` are backed as a whole (no line data exists, e.g. the MCP tools) and count as seen."""
    missed = []
    for unit in units:
        if unit["path"] in file_level:
            continue
        spans = shown(unit["path"])
        if is_whole(spans):
            continue
        if lines_covered(spans, unit["start"], unit["end"], 0) < unit["end"] - unit["start"] + 1:
            missed.append(unit)
    return missed


def unit_of(units: List[Dict[str, Any]], path: str, line: int) -> Optional[str]:
    """The smallest unit of the shard that holds path:line, or None (a finding outside every unit)."""
    best: Optional[Dict[str, Any]] = None
    for unit in units:
        if unit["path"] == path and unit["start"] <= line <= unit["end"]:
            if best is None or unit["end"] - unit["start"] < best["end"] - best["start"]:
                best = unit
    return best["id"] if best else None


def judge_findings(repo: Any, items: List[Any], units: List[Dict[str, Any]], shown: Any, file_level: Set[str],
                   slack: int) -> List[Dict[str, Any]]:
    """Findings of one shard answer with the verdict of the deterministic checks: `verified` needs the quote in the
    file within `slack` lines and the line among those shown to the session (any line of a file-level file)."""
    checked = check_findings(repo, items, slack)
    out = []
    for item, check in zip(items[:MAX_AUDIT_FINDINGS], checked):
        if not isinstance(item, dict):  # kept, never dropped silently
            out.append({"unit": None, "path": "", "line": 0, "quote": "", "claim": "", "severity": "medium",
                        "verified": False, "reason": "the finding is not an object"})
            continue
        path, line = check["path"], check["line"]
        ok, reason = bool(check["ok"]), str(check["reason"])
        if ok and path not in file_level and not line_covered(shown(path), line, slack):
            ok, reason = False, "line was not shown to the session"
        claim = str(item["claim"]) if isinstance(item.get("claim"), str) else ""
        scenario = str(item["scenario"]) if isinstance(item.get("scenario"), str) else ""
        quote = str(item["quote"]) if isinstance(item.get("quote"), str) else ""
        severity = item.get("severity") if item.get("severity") in SEVERITIES else "medium"
        if not ok:  # the quote of a finding that did not check out is the model's words, not the repository's
            quote = ""
        out.append({"unit": unit_of(units, path, line), "path": path, "line": line,
                    "quote": mask_secrets(quote[:MAX_QUOTE_CHARS]), "claim": mask_secrets(claim[:MAX_CLAIM_CHARS]),
                    "scenario": mask_secrets(scenario[:MAX_CLAIM_CHARS]),
                    "severity": severity, "verified": ok, "reason": reason})
    return out


def unit_excerpt(repo: Any, units_by_id: Dict[str, Dict[str, Any]], finding: Dict[str, Any]) -> str:
    """Numbered lines of the finding's unit plus RECHECK_CONTEXT_LINES around it, at most RECHECK_EXCERPT_LINES
    centred on the finding's line when the unit is longer; "" when the file cannot be read."""
    text = repo.source_text(finding["path"])
    if text is None:
        return ""
    lines = text.splitlines()
    line = int(finding["line"])
    unit = units_by_id.get(finding.get("unit") or "")
    start, end = (unit["start"], unit["end"]) if unit else (line, line)
    start, end = max(1, start - RECHECK_CONTEXT_LINES), min(len(lines), end + RECHECK_CONTEXT_LINES)
    if end - start + 1 > RECHECK_EXCERPT_LINES:
        half = RECHECK_EXCERPT_LINES // 2
        start = max(start, line - half)
        end = min(end, start + RECHECK_EXCERPT_LINES - 1)
    return "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))


def build_recheck_prompt(plan: Dict[str, Any], candidates: List[Dict[str, Any]], excerpts: List[str],
                         language_rule: str) -> str:
    """The task of a re-check session: the question, each finding with its unit's code, and the verdict format."""
    parts = [
        f"Re-check of audit findings in project {plan['project']} at commit {plan['sha'][:12]}.",
        "",
        "--- Question the auditor answered ---",
        plan["question"],
        "--- End of question ---",
        "",
        "For each finding below decide whether it is a real instance of the question. Confirm it only when you can "
        "state a concrete failure (input or state, path, wrong result or harm). Reject it when the code, a comment or "
        "a docstring shows the behaviour is intended, when the value cannot really reach that line (look at the callers "
        "and the arguments, not only the reported line), when it is a hardening suggestion without a stated limit, or "
        "when it is not about the question. You may read more code with repo blocks; the excerpt is a start.",
        "",
        PRECISION_RULES,
    ]
    for number, (finding, excerpt) in enumerate(zip(candidates, excerpts), 1):
        parts += ["", f"### Finding {number}: {finding['path']}:{finding['line']} (severity {finding['severity']})",
                  f"Claim: {finding['claim']}"]
        if finding.get("scenario"):
            parts.append(f"Auditor's scenario: {finding['scenario']}")
        parts.append("```\n" + excerpt + "\n```" if excerpt else "(the file could not be shown; read it yourself)")
    parts += [
        "",
        "Answer with ONE ```verdicts block and nothing after it. The block is a JSON list with one object per finding:",
        '  {"id": <finding number>, "verdict": "confirmed" | "rejected", "reason": "<for confirmed: the concrete '
        'failure; for rejected: why it is not one>"}',
        "",
        language_rule,
    ]
    return "\n".join(parts)


def parse_verdicts(text: str) -> Optional[Dict[int, Tuple[str, str]]]:
    """{finding number: (verdict, reason)} of the last ```verdicts block, None without a block. Items that are not
    objects with a known verdict and an integer id are left out (their findings count as not confirmed)."""
    blocks = VERDICTS_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except ValueError:
        return {}
    out: Dict[int, Tuple[str, str]] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("verdict") not in VERDICTS:
            continue
        number = item.get("id")
        if isinstance(number, int) and not isinstance(number, bool) and number not in out:
            reason = item.get("reason") if isinstance(item.get("reason"), str) else ""
            out[number] = (item["verdict"], mask_secrets(reason[:MAX_CLAIM_CHARS]))
    return out


def split_findings(findings: List[Dict[str, Any]], floor: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]],
                                                                          List[Dict[str, Any]]]:
    """(unverified, candidates for the re-check, notes): verified findings below the floor become notes."""
    unverified = [f for f in findings if not f["verified"]]
    candidates, notes = [], []
    for finding in findings:
        if not finding["verified"]:
            continue
        if below_floor(finding["severity"], floor):
            notes.append({**finding, "note": f"below the severity floor ({floor})"})
        else:
            candidates.append(finding)
    return unverified, candidates, notes


def apply_verdicts(candidates: List[Dict[str, Any]], verdicts: Dict[int, Tuple[str, str]]) -> Tuple[
        List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(confirmed, notes): a candidate stays a finding only with a `confirmed` verdict; the reason is kept either way."""
    confirmed, notes = [], []
    for number, finding in enumerate(candidates, 1):
        verdict, reason = verdicts.get(number, ("", ""))
        if verdict == "confirmed":
            confirmed.append({**finding, "recheck": "confirmed", "recheck_reason": reason})
        else:
            notes.append({**finding, "recheck": verdict or "no verdict", "recheck_reason": reason,
                          "note": "rejected by the re-check" if verdict else "the re-check gave no verdict"})
    return confirmed, notes


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the runner ----------------------------------------------------------------------------------------------------

class AuditRunner:
    """Runs the pending shards of one audit. run() returns the result dict (see its docstring)."""

    def __init__(self, project_id: Optional[str], audit_id: str, team: Optional[str] = None,
                 language_name: Optional[str] = None, events: Optional[EventWriter] = None,
                 db_path: Optional[Path] = None, audit_dir: Optional[Path] = None) -> None:
        self.events = events or EventWriter()
        try:
            self.project = ProjectManager().load(project_id)
        except Exception as exc:  # ProjectValidationError
            raise RunError(f"project: {exc}") from exc
        if self.project.is_ops:
            raise RunError(f"project {self.project.id} is an ops project: it has no repository to audit")
        directory = audit_dir or default_audit_dir(self.project.id)
        try:
            self.store = AuditStore(directory)
            self.plan = self.store.load_plan(audit_id)
        except AuditError as exc:
            raise RunError(str(exc), 3) from exc
        self.audit_id = audit_id
        if self.plan.get("prompt_version") != AUDIT_PROMPT_VERSION:
            raise RunError(f"audit {audit_id} was planned for prompt version {self.plan.get('prompt_version')}, this "
                           f"code is at {AUDIT_PROMPT_VERSION}: plan the audit again (--repo-audit)")
        self.engine = DebateEngine(max_rounds=1, db_path=db_path, events=self.events)
        try:
            self.lineup = TeamManager(persona_mgr=self.engine.persona_mgr).load(team)
        except Exception as exc:  # TeamValidationError
            raise RunError(f"team {team}: {exc}") from exc
        self.language_name = language_name or audit_language()
        try:
            self.sha = resolve_sha(self.project.workspace, self.plan["sha"])
        except ContextError as exc:
            raise RunError(f"commit {self.plan['sha'][:12]} of the plan is not in the workspace: {exc}") from exc
        self.engine.mcp = MCPClient(workspace=self.project.workspace, project=self.project)
        self.engine._wiki_exposure = False  # a shard is not judged on what a wiki page claims
        self.engine._findings_rules = False  # FINDINGS_FORMAT replaces the debate's optional findings block
        self.engine._language_rule = answer_language_rule(self.language_name, debate=False)
        self.engine._system_prompt = AUDIT_SYSTEM

    def run(self, max_shards: Optional[int] = None) -> Dict[str, Any]:
        """Runs the shards that are not done, in plan order, at most max_shards of them, and writes report.json.

        {audit, state, shards_run, done, deferred, failed, pending, report: totals}. state: `done` (every shard done),
        `paused` (stopped on a deferred shard or at max_shards; run again to resume) or `incomplete` (only failed
        shards remain). Raises RunError (exit 4: the audit is already running)."""
        if max_shards is not None and max_shards < 1:
            raise RunError("the maximum number of shards must be at least 1")
        if not retrieval_enabled():
            raise RunError("retrieval is switched off (GUARDIAN_RETRIEVAL=0): an audit cannot read code without it")
        try:
            with self.store.run_lock(self.audit_id), step_limits():
                return self._run(max_shards)
        except AuditBusy as exc:
            raise RunError(str(exc), 4) from exc

    def _run(self, max_shards: Optional[int]) -> Dict[str, Any]:
        pending = self.store.pending(self.audit_id)
        self.store.set_state(self.audit_id, "running", started_at=_utc_now(), team=self.lineup.id,
                             language=self.language_name)
        self.events.thread_id = f"audit_{self.audit_id}"
        self.events.emit("audit_started", {
            "audit": self.audit_id, "project": self.project.id, "sha": self.sha, "team": self.lineup.id,
            "language": self.language_name, "shards": len(self.plan["shards"]), "pending": len(pending)})
        counts = {"done": 0, "deferred": 0, "failed": 0}
        try:
            stopped = self._loop(pending, max_shards, counts)
        except BaseException as exc:  # an I/O error or an interrupt: leave a resumable audit, not a `running` one
            self._finish("paused", counts, interrupted=type(exc).__name__)
            raise
        finally:
            self.engine._close_go_index()
        remaining = self.store.pending(self.audit_id)
        return self._finish("done" if not remaining else ("paused" if stopped else "incomplete"), counts)

    def _loop(self, pending: List[Dict[str, Any]], max_shards: Optional[int], counts: Dict[str, int]) -> bool:
        """Runs the pending shards in order; True when it stopped early (a deferred shard or max_shards)."""
        for number, shard in enumerate(pending):
            if max_shards is not None and number >= max_shards:
                return True
            record = self._run_shard(shard)
            self.store.save_shard(self.audit_id, shard["id"], record)
            counts[record["state"]] += 1
            self.events.emit("audit_shard_finished", {
                "shard": shard["id"], "state": record["state"], "coverage": record.get("coverage"),
                "findings": len(record.get("findings") or []),
                "verified": sum(1 for f in record.get("findings") or [] if f["verified"]),
                "notes": len(record.get("notes") or []), "recheck": (record.get("recheck") or {}).get("state"),
                "unseen": len(record.get("unseen") or []), "error": record.get("error")})
            if record["state"] == "deferred":
                return True
        return False

    def _finish(self, state: str, counts: Dict[str, int], **extra: Any) -> Dict[str, Any]:
        """Writes the report and the final state of the audit, emits audit_finished and returns the result of run()."""
        remaining = self.store.pending(self.audit_id)
        report = build_report(self.plan, self.store.records(self.audit_id), state)
        self.store.save_report(self.audit_id, report)
        self.store.set_state(self.audit_id, state, finished_at=_utc_now(), totals=report["totals"], **extra)
        self.events.thread_id = f"audit_{self.audit_id}"
        self.events.emit("audit_finished", {"state": state, **counts, "pending": len(remaining),
                                            "findings": report["totals"]["findings"],
                                            "unverified": report["totals"]["unverified"],
                                            "notes": report["totals"]["notes"], **extra})
        return {"audit": self.audit_id, "state": state, "shards_run": sum(counts.values()), **counts,
                "pending": len(remaining), "report": report["totals"]}

    # --- one shard ---

    def _run_shard(self, shard: Dict[str, Any]) -> Dict[str, Any]:
        started = _utc_now()
        thread_id = f"audit_{self.audit_id}_{shard['id']}"
        self.events.thread_id = thread_id
        self.events.emit("audit_shard_started", {"shard": shard["id"], "files": len(shard["files"]),
                                                 "bytes": shard["bytes"], "oversized": shard["oversized"]})
        try:
            # A new thread id per attempt: a retried shard must not resume the CLI session of the failed one, whose
            # reads the new retrieval state knows nothing about.
            record = self._session(shard, f"{thread_id}_{uuid.uuid4().hex[:8]}")
        except Exception as exc:  # noqa: BLE001 - one broken shard must not stop the audit
            record = {"state": "failed", "error": f"error: {type(exc).__name__}: {str(exc)[:200]}"}
        return {**record, "key": shard["key"], "started_at": started, "finished_at": _utc_now()}

    def _prepare(self, shard: Dict[str, Any]) -> Optional[Tuple[Any, str]]:
        """(the repo tools, the shard task) of a session, with the retrieval protocol set up for it; None without
        retrieval. The guide is the root summary: the task names the files to read."""
        self.engine._setup_retrieval(None, {"sha": self.sha}, self.project.id)
        self.engine._flush_compiler_events()
        repo = self.engine._repo
        if repo is None:
            return None
        if self.engine._map_version is not None:
            guide = root_summary(repo)
            self.engine._map_bytes = len(guide.encode("utf-8"))
            self.engine._set_retrieval_text(self.sha, repo.readable_paths(), False, guide)
        return repo, build_shard_prompt(self.plan, shard, self.engine._language_rule)

    def _failure(self, error: Optional[str]) -> Dict[str, Any]:
        """The record of a session that got no answer from any provider."""
        text = str(error or "no provider answered")[:300]
        if is_rate_limited(text):
            return {"state": "deferred", "error": f"rate_limited: {text}"}
        return {"state": "failed", "error": f"llm_fallback: no provider answered ({text})"}

    def _session(self, shard: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
        prepared = self._prepare(shard)
        if prepared is None:
            return {"state": "failed", "error": "retrieval is not available for this workspace"}
        repo, prompt = prepared
        engine, lineup = self.engine, self.lineup
        key = lineup.session_key(EXPERT_ROLE, lineup.expert)
        agent_name = (engine.persona_mgr.get_persona(lineup.expert.persona) or {}).get("name", EXPERT_ROLE)
        units_by_id = {u["id"]: u for u in self.plan["units"]}
        units = [units_by_id[uid] for uid in shard["units"]]
        slack = citation_slack()

        def ask(message: str, round_num: int, trace: Dict[str, Any]) -> Tuple[str, str, str]:
            """(provider, answer, why no provider answered): the failures of the CLIs that were tried."""
            provider, text, failures = engine._ask(
                thread_id, EXPERT_ROLE, message, {}, "", key, lineup.expert.providers or AUDIT_PROVIDERS,
                round_num=round_num,
                agent_name=agent_name, trace=trace, code_tools=lineup.expert.code_tools)
            return provider, text, " | ".join([*failures, engine.llm.last_error or ""]).strip(" |")

        def shown_state() -> Tuple[Any, Set[str]]:
            """(path -> spans shown to the session, files backed as a whole) once its last answer is in."""
            with engine._retrieval_lock:
                engine._provided_set(key, EXPERT_ROLE, agent_name)
                return (lambda path: engine._shown_spans(key, path)), set(engine._file_level[key])

        trace: Dict[str, Any] = {}
        provider, text, why = ask(prompt, 1, trace)
        if provider == BUILTIN_PROVIDER:
            return self._failure(why)
        if not text.strip() or text.lstrip().startswith("(no answer"):
            return {"state": "failed", "error": "no_answer: the session ended without an answer"}
        items, problem, cut = parse_audit_findings(text)
        shown, file_level = shown_state()
        missing = unseen_units(units, shown, file_level)
        if items is None or problem or missing:  # one re-ask for the block and the units not completely shown
            again_trace: Dict[str, Any] = {}
            provider_2, text_2, why = ask(build_followup(self.plan, missing, items is None, problem), 2, again_trace)
            again, again_problem, again_cut = (parse_audit_findings(text_2) if provider_2 != BUILTIN_PROVIDER
                                               else (None, None, 0))
            if provider_2 == BUILTIN_PROVIDER and (items is None or problem or is_rate_limited(why)):
                return self._failure(why)  # a quota failure here defers the shard, it does not end it as incomplete
            # take the second block unless it is broken where the first was readable
            if again is not None and not (again_problem and items is not None and not problem):
                provider, items, problem, cut, trace = provider_2, again, again_problem, again_cut, again_trace
            shown, file_level = shown_state()
            missing = unseen_units(units, shown, file_level)
        if items is None:
            return {"state": "failed", "error": "bad_format: the answer has no findings block"}
        if problem:
            return {"state": "failed", "error": f"bad_format: {problem}"[:300]}
        findings = judge_findings(repo, items, units, shown, file_level, slack)
        unverified, candidates, notes = split_findings(findings, audit_min_severity())
        checked = self._recheck(shard, thread_id, repo, units_by_id, candidates, agent_name)
        if "state" in checked:  # out of quota: the shard is deferred and its first session is run again later
            return checked
        notes += checked["notes"]
        record = self._record(shard, provider, checked["confirmed"] + unverified, notes, checked["summary"], missing,
                              trace, shown)
        if cut:  # items the answer held above MAX_AUDIT_FINDINGS: the shard is not reported as if it had no more
            record["findings_cut"] = cut
            self.events.emit("audit_findings_cut", {"shard": shard["id"], "cut": cut, "kept": MAX_AUDIT_FINDINGS})
        return record

    def _recheck(self, shard: Dict[str, Any], thread_id: str, repo: Any, units_by_id: Dict[str, Dict[str, Any]],
                 candidates: List[Dict[str, Any]], agent_name: str) -> Dict[str, Any]:
        """{confirmed, notes, summary} of the shard's re-check session, or the record of a deferred shard.

        One fresh session of the Expert member (own thread, own retrieval key, the reviewer system prompt) sees every
        candidate with its unit's code. Without a candidate, with the re-check switched off or when the session fails
        for another reason than quota, nothing is asked or the candidates move to the notes with that reason."""
        if not candidates:
            return {"confirmed": [], "notes": [], "summary": {"state": "skipped", "candidates": 0}}
        if not recheck_enabled():
            return {"confirmed": candidates, "notes": [], "summary": {"state": "off", "candidates": len(candidates)}}
        engine, member = self.engine, self.lineup.expert
        excerpts = [unit_excerpt(repo, units_by_id, f) for f in candidates]
        prompt = build_recheck_prompt(self.plan, candidates, excerpts, engine._language_rule)
        self.events.emit("audit_recheck_started", {"shard": shard["id"], "candidates": len(candidates)})
        system, engine._system_prompt = engine._system_prompt, RECHECK_SYSTEM
        try:
            provider, text, failures = engine._ask(
                f"{thread_id}_recheck", EXPERT_ROLE, prompt, {}, "", RECHECK_SESSION_KEY,
                member.providers or AUDIT_PROVIDERS, round_num=1, agent_name=agent_name, trace={},
                code_tools=member.code_tools)
        finally:
            engine._system_prompt = system
        why = " | ".join([*failures, engine.llm.last_error or ""]).strip(" |")
        verdicts = parse_verdicts(text) if provider != BUILTIN_PROVIDER else None
        if provider == BUILTIN_PROVIDER and is_rate_limited(why):
            return self._failure(why)
        if verdicts is None:
            error = (f"llm_fallback: {why[:200]}" if provider == BUILTIN_PROVIDER
                     else "bad_format: the answer has no verdicts block")
            notes = [{**f, "recheck": "failed", "note": f"the re-check failed ({error})"} for f in candidates]
            return {"confirmed": [], "notes": notes,
                    "summary": {"state": "failed", "candidates": len(candidates), "error": error}}
        confirmed, notes = apply_verdicts(candidates, verdicts)
        return {"confirmed": confirmed, "notes": notes,
                "summary": {"state": "done", "provider": provider.removesuffix("-cli"), "candidates": len(candidates),
                            "confirmed": len(confirmed), "rejected": sum(1 for n in notes if n["recheck"] == "rejected")}}

    def _record(self, shard: Dict[str, Any], provider: str, findings: List[Dict[str, Any]],
                notes: List[Dict[str, Any]], recheck: Dict[str, Any], missing: List[Dict[str, Any]],
                trace: Dict[str, Any], shown: Any) -> Dict[str, Any]:
        """The `done` record of a shard, and the audit_unseen event when units were not completely shown."""
        record: Dict[str, Any] = {
            "state": "done", "provider": provider.removesuffix("-cli"),
            "coverage": "incomplete" if missing else "complete", "unseen": [u["id"] for u in missing],
            "findings": findings, "notes": notes, "recheck": recheck}
        unverified_citations = [str(u)[:300] for u in (trace.get("unverified_citations") or [])][:MAX_EVENT_UNVERIFIED]
        if unverified_citations:
            record["unverified_citations"] = unverified_citations
        if missing:
            self.events.emit("audit_unseen", {"shard": shard["id"], "units": len(missing),
                                              "shown": describe_spans(shown(missing[0]["path"]))})
        return record

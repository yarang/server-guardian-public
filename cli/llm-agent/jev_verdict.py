"""
jev verdict (docs/design/jev-verdict.md): asks jev whether the round's proposal should be approved, from the same
material the LLM judge reads.

Phase J1 is shadow mode only: with GUARDIAN_JEV_VERDICT=shadow the engine asks after every LLM judge turn and records
the answer (`jev_verdict`, and `verdict_disagreement` when the two differ); the outcome never changes. jev judges, it
does not write: one System One request (`POST <TYPESAFE_BASE_URL>/v1/systemone`) with three questions, of which only
`approve` would decide (J3). The hub's jev-mcp offers no such call, so the engine sends the request itself with the
settings jev-mcp reads (`TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL`, from the environment or
llm.env). The key stays with the engine (`TYPESAFE_*` never reaches an agent) and never enters a message or an event.

`report` summarises the recorded events of the hub's debate jobs for J2: agreement where jev is sure, the confusion
table and the cases a person has to read.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from llm_runner import load_llm_env

OFF, SHADOW = "off", "shadow"
APPROVE, REJECT, UNCERTAIN = "approve", "reject", "uncertain"
DEFAULT_YES, DEFAULT_NO = 0.8, 0.2
DEFAULT_BASE_URL = "https://api.typesafe.ai"   # jev-mcp's defaults (src/jev_mcp/config.py)
DEFAULT_MODEL = "jev-latest"
SYSTEM_ONE_PATH = "/v1/systemone"
MAX_STATE_CHARS = 24000   # the material jev reads: the head and the tail of a longer one, marked as cut
EVIDENCE_LEVELS = ["none", "some", "most", "all"]
# Version 2 (2026-11-01): in a round the critique comes after the proposal, so the proposal never answers it; the judge
# settles it in the decision. Version 1 asked whether the proposal answered the critique and was "no" for 56 of 60 past
# rounds (docs/analysis/2026-11-01-jev-verdict-replay). `approve` is the deciding question; `fundamental` explains.
QUESTION_VERSION = 2
QUESTIONS = [
    {"id": "approve", "type": "check",
     "question": "A judge reads this proposal and the critique of it, and writes the final decision itself, adopting "
                 "what is valid in the critique. Can that decision be written now from this material?",
     "yes_means": "The proposal is a sound base for the task; what the critique raises can be settled inside the "
                  "decision (a correction, a constraint, a check to run, a smaller scope) without a new draft.",
     "no_means": "The critique shows the proposal is wrong at its core, misses the task, or rests on claims the "
                 "material does not support, so the proposal must be redrafted before any decision."},
    {"id": "fundamental", "type": "check",
     "question": "Does the critique show a flaw that cannot be fixed within a decision and requires the proposal to "
                 "be redrafted?"},
    {"id": "evidence", "type": "score",
     "question": "How well do the proposal's claims rest on evidence quoted in the material?",
     "levels": EVIDENCE_LEVELS},
]


class JevVerdictError(RuntimeError):
    """jev gave no usable verdict (no key, an HTTP or network error, an answer without the approve probability)."""


def api_questions() -> Dict[str, Dict[str, Any]]:
    """QUESTIONS in the System One request shape: named questions with instructions and criteria."""
    out: Dict[str, Dict[str, Any]] = {}
    for q in QUESTIONS:
        if q["type"] == "check":
            entry: Dict[str, Any] = {"type": "noul", "instructions": q["question"]}
            criteria = {k: q[f] for k, f in (("true", "yes_means"), ("false", "no_means")) if q.get(f)}
            if criteria:
                entry["criteria"] = criteria
        else:
            entry = {"type": "score", "instructions": q["question"], "criteria": list(q["levels"])}
        out[q["id"]] = entry
    return out


MIN_SHADOW = 30          # the J2 bar (design, proposed): at least this many shadow verdicts,
MIN_AGREEMENT = 0.9      # and this agreement where jev is sure


def mode() -> str:
    """GUARDIAN_JEV_VERDICT: `shadow` records jev's verdict next to the LLM judge's; anything else is off (J1)."""
    return SHADOW if os.environ.get("GUARDIAN_JEV_VERDICT", OFF).strip().lower() == SHADOW else OFF


def thresholds() -> Dict[str, float]:
    def read(name: str, default: float) -> float:
        try:
            value = float(os.environ.get(name, default))
        except ValueError:
            return default
        return value if 0.0 <= value <= 1.0 else default
    yes, no = read("GUARDIAN_JEV_VERDICT_YES", DEFAULT_YES), read("GUARDIAN_JEV_VERDICT_NO", DEFAULT_NO)
    return {"yes": yes, "no": no} if no < yes else {"yes": DEFAULT_YES, "no": DEFAULT_NO}


def classify(p_approve: float, limits: Dict[str, float]) -> str:
    if p_approve >= limits["yes"]:
        return APPROVE
    if p_approve <= limits["no"]:
        return REJECT
    return UNCERTAIN


def state_text(task: str, material: str) -> Dict[str, Any]:
    """The text jev reads: the task and the judge's material, cut in the middle when it is longer than the limit."""
    text = f"Task: {task}\n\n{material}".strip()
    if len(text) <= MAX_STATE_CHARS:
        return {"text": text, "clipped": False}
    half = MAX_STATE_CHARS // 2
    return {"text": f"{text[:half]}\n\n[... {len(text) - MAX_STATE_CHARS} characters left out ...]\n\n{text[-half:]}",
            "clipped": True}


def parse_answer(result: Dict[str, Any], limits: Dict[str, float]) -> Dict[str, Any]:
    """The jev_ask result as a verdict record; raises MCPError when `approve` has no probability."""
    answers = (result or {}).get("answers") or {}
    approve = answers.get("approve") or {}
    if not isinstance(approve.get("noul"), (int, float)):
        raise JevVerdictError(f"jev returned no probability for approve: {json.dumps(result)[:200]}")
    p_approve = float(approve["noul"])
    fundamental = answers.get("fundamental") or {}
    evidence = answers.get("evidence") or {}
    return {
        "questions": QUESTION_VERSION,
        "p_approve": round(p_approve, 3),
        "verdict": classify(p_approve, limits),
        "p_fundamental": (round(float(fundamental["noul"]), 3)
                          if isinstance(fundamental.get("noul"), (int, float)) else None),
        "evidence": round(float(evidence["score"]), 2) if isinstance(evidence.get("score"), (int, float)) else None,
        "yes_at": limits["yes"],
        "no_at": limits["no"],
        "model": result.get("model"),
        "latency_ms": result.get("latency_ms"),
    }


class JevVerdict:
    """Asks TypeSafe's System One API for a verdict, with the jev gate's key and settings."""

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def ask(self, task: str, material: str, limits: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """{p_approve, verdict, p_fundamental, evidence, ...}; raises JevVerdictError when jev cannot answer. `limits`
        are the yes/no thresholds (default: thresholds(); the verdict source passes its file's)."""
        env = {**os.environ, **load_llm_env()}   # the jev gate's precedence: llm.env over the environment
        key = env.get("TYPESAFE_API_KEY", "").strip()
        if not key:
            raise JevVerdictError("TYPESAFE_API_KEY is not set")
        url = env.get("TYPESAFE_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
        state = state_text(task, material)
        body = {"model": env.get("TYPESAFE_DEFAULT_MODEL", "").strip() or DEFAULT_MODEL, "state": state["text"],
                "questions": api_questions()}
        request = urllib.request.Request(url + SYSTEM_ONE_PATH, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {key}",
                                                  "Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:   # the body names the problem; the key is only in the request header
            raise JevVerdictError(f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise JevVerdictError(f"{type(exc).__name__}: {exc}") from None
        record = parse_answer(result, limits or thresholds())
        record["latency_ms"] = record.get("latency_ms") or round((time.monotonic() - started) * 1000)
        record["clipped"] = state["clipped"]
        return record


def compare(record: Dict[str, Any], llm_approved: bool) -> Dict[str, Any]:
    """The shadow event's data: jev's record plus the LLM judge's verdict and whether they agree (None: jev unsure)."""
    jev = record["verdict"]
    agree = None if jev == UNCERTAIN else (jev == APPROVE) == llm_approved
    return {**record, "llm": APPROVE if llm_approved else REJECT, "agree": agree, "decided_by": "llm", "mode": SHADOW}


# --- the report for J2 ---------------------------------------------------------------------------------------------


def _events(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def collect(jobs_dir: Path, kinds: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """One row per shadow verdict in the jobs' events: job, thread, project (and its kind), round and the record."""
    rows: List[Dict[str, Any]] = []
    for events_file in sorted(jobs_dir.glob("*/events.jsonl")):
        project = None
        for event in _events(events_file):
            if event.get("type") == "debate_started":
                project = (event.get("data") or {}).get("project")
            elif event.get("type") == "jev_verdict":
                data = event.get("data") or {}
                if data.get("error") or data.get("mode") != SHADOW or data.get("questions") != QUESTION_VERSION:
                    continue   # failures, and records of an earlier question set, do not mix into the measurement
                rows.append({"job": events_file.parent.name, "thread": event.get("thread_id"), "project": project,
                             "kind": (kinds or {}).get(project or "", "code"), "round": event.get("round"), **data})
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    confusion: Dict[str, Dict[str, int]] = {v: {APPROVE: 0, REJECT: 0} for v in (APPROVE, REJECT, UNCERTAIN)}
    for row in rows:
        confusion[row["verdict"]][row["llm"]] += 1
    sure = [r for r in rows if r["verdict"] != UNCERTAIN]
    agreed = sum(1 for r in sure if r["agree"])
    agreement = agreed / len(sure) if sure else None
    to_read = [{k: r.get(k) for k in ("job", "thread", "project", "round", "p_approve", "p_fundamental", "evidence")}
               for r in rows if r["verdict"] == APPROVE and r["llm"] == REJECT]
    by_kind: Dict[str, Dict[str, Any]] = {}
    for kind in sorted({r["kind"] for r in rows}):
        mine = [r for r in rows if r["kind"] == kind and r["verdict"] != UNCERTAIN]
        by_kind[kind] = {"verdicts": sum(1 for r in rows if r["kind"] == kind), "sure": len(mine),
                         "agreement": (sum(1 for r in mine if r["agree"]) / len(mine)) if mine else None}
    return {
        "verdicts": len(rows),
        "sure": len(sure),
        "agreement": agreement,
        "confusion": confusion,
        "by_kind": by_kind,
        "jev_approves_llm_rejects": to_read,
        "bar": {"min_verdicts": MIN_SHADOW, "min_agreement": MIN_AGREEMENT,
                "met": len(rows) >= MIN_SHADOW and agreement is not None and agreement >= MIN_AGREEMENT,
                "note": "and every case in jev_approves_llm_rejects read by a person and found harmless"},
    }


def report(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = data_dir or Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()
    kinds: Dict[str, str] = {}
    try:
        from project_registry import ProjectManager

        for entry in ProjectManager().list_projects():
            kinds[str(entry.get("id"))] = str(entry.get("kind") or "code")
    except Exception:  # the report still counts without kinds
        pass
    rows = collect(root / "debate-jobs", kinds)
    return {"mode": mode(), "thresholds": thresholds(), **summarize(rows)}

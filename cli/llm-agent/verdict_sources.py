"""
Verdict sources (docs/design/verdict-sources.md), phase S1: the registry and the shadow run.

A verdict source answers one question about a round, from the material the LLM judge reads: approve, reject or
uncertain, with an optional probability and short reasons. It never writes the decision and never changes anything.
The operator registers sources on the hub; a flow only picks them by id (S2 onwards).

- Source files: builtin configs/verdicts/*.json (data of the repository, validated by scripts/ci.sh config), the
  operator's in <GUARDIAN_DATA_DIR>/verdicts/*.json. One source per file; an id in both stores is an error. Each file
  lists the roles the source may take (decide, alarm, gate), the project kinds it serves, its time limit, a `reviewed`
  date (a warning after 180 days, as for command sets) and `shadow_bar`, the numbers it must reach before its shadow
  may be lifted.
- Kinds: `jev` (one System One request, jev_verdict.py) runs; `mcp` (S3) and `command` (S4) are refused until they
  are built. The LLM judge is the implicit `llm` source and has no file.
- Shadow run (S1 has no roles): with GUARDIAN_VERDICT_SOURCES=shadow (or the J1 switch GUARDIAN_JEV_VERDICT=shadow) the
  engine asks every registered source that serves the project's kind after each LLM judge turn and records a
  `verdict_source` event per source, plus `verdict_disagreement` when a sure source differs from the LLM judge. The
  outcome is always the LLM judge's. A failure or a timeout counts as uncertain and is recorded. For the jev source
  the J1 event `jev_verdict` is still written, so the J2 measurement goes on without a break.

`report` gives the registry and, per source, the shadow numbers and whether its bar is met.
"""

import datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import jev_verdict

OFF, SHADOW = "off", "shadow"
APPROVE, REJECT, UNCERTAIN = jev_verdict.APPROVE, jev_verdict.REJECT, jev_verdict.UNCERTAIN
ROLES = ("decide", "alarm", "gate")
PROJECT_KINDS = ("code", "ops", "design")
KINDS = ("jev", "mcp", "command")
NOT_BUILT = {"mcp": "S3", "command": "S4"}
SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
COMMON_KEYS = {"id", "name", "kind", "description", "reviewed", "roles", "projects", "timeout_s", "shadow_bar"}
KIND_KEYS = {"jev": {"questions", "approve_at", "reject_at"}}
MAX_FILE_BYTES = 65536
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_AGE_DAYS = 180
MAX_REASONS, MAX_REASON_CHARS = 8, 300


class SourceError(ValueError):
    """A source file that does not validate (the message names the file or the id)."""


# --- the store ----------------------------------------------------------------------------------------------------


def builtin_dir() -> Path:
    override = os.environ.get("GUARDIAN_VERDICT_SOURCES_DIR")
    return Path(override) if override else Path(__file__).resolve().parent.parent.parent / "configs" / "verdicts"


def operator_dir() -> Path:
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "verdicts"


def max_age_days() -> int:
    """How old a source's `reviewed` date may get before it is reported as stale (GUARDIAN_VERDICT_SOURCE_MAX_AGE_DAYS)."""
    try:
        return max(1, int(os.environ.get("GUARDIAN_VERDICT_SOURCE_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def mode() -> str:
    """GUARDIAN_VERDICT_SOURCES=shadow runs every registered source in shadow; the J1 switch GUARDIAN_JEV_VERDICT=shadow
    (what the hub was deployed with) does the same. Anything else is off."""
    for name in ("GUARDIAN_VERDICT_SOURCES", "GUARDIAN_JEV_VERDICT"):
        if os.environ.get(name, OFF).strip().lower() == SHADOW:
            return SHADOW
    return OFF


def _probability(sid: str, name: str, raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0.0 <= raw <= 1.0:
        raise SourceError(f"{sid}: {name} must be a number from 0 to 1")
    return float(raw)


def validate(raw: Any) -> Dict[str, Any]:
    """One source file's content, checked and normalised; raises SourceError."""
    if not isinstance(raw, dict):
        raise SourceError("a verdict source is a JSON object")
    sid = raw.get("id")
    if not isinstance(sid, str) or not SOURCE_ID.fullmatch(sid):
        raise SourceError(f"id {sid!r}: lowercase letters, digits and -, starting with a letter, at most 40 characters")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise SourceError(f"{sid}: kind must be one of {', '.join(KINDS)}")
    if kind in NOT_BUILT:
        raise SourceError(f"{sid}: kind {kind} is not built yet (phase {NOT_BUILT[kind]})")
    unknown = set(raw) - COMMON_KEYS - KIND_KEYS.get(kind, set())
    if unknown:
        raise SourceError(f"{sid}: unknown keys {', '.join(sorted(unknown))}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise SourceError(f"{sid}: name must be a text of 1 to 80 characters")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 400:
        raise SourceError(f"{sid}: description must be a text of at most 400 characters")
    try:
        reviewed = datetime.date.fromisoformat(str(raw.get("reviewed")))
    except ValueError:
        raise SourceError(f"{sid}: reviewed must be a date such as 2026-11-02") from None
    if reviewed > datetime.date.today():
        raise SourceError(f"{sid}: reviewed is in the future")
    out: Dict[str, Any] = {"id": sid, "name": name.strip(), "kind": kind, "description": description.strip(),
                           "reviewed": reviewed.isoformat()}
    for key, allowed in (("roles", ROLES), ("projects", PROJECT_KINDS)):
        values = raw.get(key)
        if not isinstance(values, list) or not values or any(v not in allowed for v in values):
            raise SourceError(f"{sid}: {key} must be a non-empty list of {', '.join(allowed)}")
        out[key] = [v for v in allowed if v in values]
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        raise SourceError(f"{sid}: timeout_s must be whole seconds from 1 to {MAX_TIMEOUT}")
    out["timeout_s"] = timeout
    bar = raw.get("shadow_bar")
    if not isinstance(bar, dict) or set(bar) != {"min_verdicts", "min_agreement"}:
        raise SourceError(f"{sid}: shadow_bar must be {{\"min_verdicts\": ..., \"min_agreement\": ...}}")
    count = bar["min_verdicts"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise SourceError(f"{sid}: shadow_bar.min_verdicts must be a whole number of at least 1")
    out["shadow_bar"] = {"min_verdicts": count,
                         "min_agreement": _probability(sid, "shadow_bar.min_agreement", bar["min_agreement"])}
    if kind == "jev":
        if raw.get("questions") != jev_verdict.QUESTION_VERSION:
            raise SourceError(f"{sid}: questions must be {jev_verdict.QUESTION_VERSION}, the question set the engine has")
        approve_at = _probability(sid, "approve_at", raw.get("approve_at"))
        reject_at = _probability(sid, "reject_at", raw.get("reject_at"))
        if reject_at >= approve_at:
            raise SourceError(f"{sid}: reject_at must be below approve_at")
        out.update(questions=jev_verdict.QUESTION_VERSION, approve_at=approve_at, reject_at=reject_at)
    return out


def _read_dir(directory: Path, source: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("[a-z]*.json")):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise SourceError(f"larger than {MAX_FILE_BYTES} bytes")
            spec = validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise SourceError(f"{path}: {exc}") from None
        if spec["id"] in out:
            raise SourceError(f"{path}: id {spec['id']} is used by another file in {directory}")
        out[spec["id"]] = {**spec, "source": source}
    return out


def _signature(directory: Path) -> Tuple[Any, ...]:
    if not directory.is_dir():
        return (str(directory),)
    files = []
    for path in sorted(directory.glob("[a-z]*.json")):
        try:
            st = path.stat()
        except OSError:
            continue
        files.append((path.name, st.st_mtime_ns, st.st_size, st.st_ino))
    return (str(directory), *files)


_LOADED: Dict[Tuple[Any, ...], Dict[str, Dict[str, Any]]] = {}


def load_sources() -> Dict[str, Dict[str, Any]]:
    """Every validated source, builtin and operator's, by id. An id in both stores is an error (nothing overrides a
    reviewed builtin silently). Raises SourceError naming the file. Remembered until a file changes."""
    key = (_signature(builtin_dir()), _signature(operator_dir()))
    cached = _LOADED.get(key)
    if cached is None:
        sources = _read_dir(builtin_dir(), "builtin")
        for sid, spec in _read_dir(operator_dir(), "operator").items():
            if sid in sources:
                raise SourceError(f"verdict source {sid} exists as a builtin and in {operator_dir()}; rename the operator's")
            sources[sid] = spec
        _LOADED.clear()
        _LOADED[key] = cached = sources
    return {sid: dict(spec) for sid, spec in cached.items()}


def stale(sources: Dict[str, Dict[str, Any]], today: Optional[datetime.date] = None,
          limit_days: Optional[int] = None) -> List[Dict[str, Any]]:
    today = today or datetime.date.today()
    limit = limit_days if limit_days is not None else max_age_days()
    old = [{"id": i, "reviewed": s["reviewed"], "age_days": (today - datetime.date.fromisoformat(s["reviewed"])).days}
           for i, s in sources.items()]
    return sorted((o for o in old if o["age_days"] > limit), key=lambda o: -o["age_days"])


# --- the sources --------------------------------------------------------------------------------------------------


class JevSource:
    """The jev verdict as a source: the file's thresholds, unless GUARDIAN_JEV_VERDICT_YES / _NO are set (the operator
    tunes them without a deploy)."""

    def __init__(self, spec: Dict[str, Any], asker: Optional[Any] = None):
        self.id, self.kind = spec["id"], spec["kind"]
        self.projects, self.timeout_s = list(spec["projects"]), spec["timeout_s"]
        self.spec = spec
        self.asker = asker or jev_verdict.JevVerdict(timeout=float(self.timeout_s))

    def limits(self) -> Dict[str, float]:
        if os.environ.get("GUARDIAN_JEV_VERDICT_YES") or os.environ.get("GUARDIAN_JEV_VERDICT_NO"):
            return jev_verdict.thresholds()
        return {"yes": self.spec["approve_at"], "no": self.spec["reject_at"]}

    def ask(self, task: str, material: str, context: Dict[str, Any]) -> Dict[str, Any]:
        record = self.asker.ask(task, material, limits=self.limits())
        return {**record, "probability": record["p_approve"]}


BUILDERS: Dict[str, Callable[[Dict[str, Any]], Any]] = {"jev": JevSource}


def build(spec: Dict[str, Any]) -> Any:
    return BUILDERS[spec["kind"]](spec)


def build_all() -> List[Any]:
    """Every registered source, built; raises SourceError when the registry is invalid."""
    return [build(spec) for _, spec in sorted(load_sources().items())]


# --- the shadow run -----------------------------------------------------------------------------------------------


def _reasons(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(r)[:MAX_REASON_CHARS] for r in raw[:MAX_REASONS] if str(r).strip()]


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_shadow(sources: Iterable[Any], task: str, material: str, context: Dict[str, Any], llm_approved: bool,
               emit: Callable[..., None], say: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    """Asks each source that serves the context's project kind and records what it said (`verdict_source`, and
    `verdict_disagreement` when a sure source differs from the LLM judge). Returns the event data. Nothing raised by
    a source escapes: the debate goes on whatever a source does."""
    llm = APPROVE if llm_approved else REJECT
    kind = context.get("kind") or "code"
    out: List[Dict[str, Any]] = []
    for source in sources:
        if kind not in getattr(source, "projects", PROJECT_KINDS):
            continue
        sid, skind = getattr(source, "id", "?"), getattr(source, "kind", "?")
        started = time.monotonic()
        error, record = None, {}
        try:
            record = dict(source.ask(task, material, context) or {})
            if record.get("verdict") not in (APPROVE, REJECT, UNCERTAIN):
                raise ValueError(f"no verdict in the answer: {json.dumps(record)[:120]}")
        except Exception as exc:   # never let the shadow measurement touch the debate
            error, record = _short(f"{type(exc).__name__}: {exc}"), {}
        elapsed = round((time.monotonic() - started) * 1000)
        timeout_s = getattr(source, "timeout_s", None)
        if error is None and timeout_s and elapsed > timeout_s * 1000:
            error = f"took {elapsed} ms, over the {timeout_s} s limit"
        verdict = UNCERTAIN if error else record["verdict"]
        probability = record.get("probability")
        data = {**{k: v for k, v in record.items() if k not in ("verdict", "probability", "reasons")},
                "source": sid, "kind": skind, "role": "shadow", "verdict": verdict,
                "probability": probability if isinstance(probability, (int, float)) else None,
                "reasons": _reasons(record.get("reasons")), "latency_ms": record.get("latency_ms") or elapsed,
                "error": error, "llm": llm, "agree": None if verdict == UNCERTAIN else (verdict == APPROVE) == llm_approved}
        out.append(data)
        _emit(emit, "verdict_source", data)
        if skind == "jev":   # the J1 event, so the J2 measurement and its readers go on unchanged
            legacy = ({"mode": jev_verdict.SHADOW, "error": error} if error
                      else jev_verdict.compare({k: v for k, v in record.items() if k != "probability"}, llm_approved))
            _emit(emit, "jev_verdict", legacy)
        if data["agree"] is False:
            _emit(emit, "verdict_disagreement", {"kind": "shadow", "source": sid, "verdict": verdict, "llm": llm,
                                                 "probability": data["probability"],
                                                 **({"jev": verdict, "p_approve": record.get("p_approve")}
                                                    if skind == "jev" else {})})
        if error:
            say(f"⚠️  [verdict source {sid}, shadow] uncertain: {error}")
        else:
            p = f" (p {data['probability']})" if data["probability"] is not None else ""
            say(f"🧮 [verdict source {sid}, shadow] {verdict}{p} · LLM judge: {llm}")
    return out


def _emit(emit: Callable[..., None], kind: str, data: Dict[str, Any]) -> None:
    try:
        emit(kind, data)
    except Exception as exc:   # a broken event writer must not stop the debate either
        print(f"⚠️  [verdict source] event {kind} not written: {exc}")


# --- the report ---------------------------------------------------------------------------------------------------


def collect(jobs_dir: Path, kinds: Optional[Dict[str, str]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Shadow rows per source id from the jobs' events. For jev, the J1 `jev_verdict` events count too, in the jobs
    that recorded no `verdict_source` for jev (no double count); failures and other jev question sets are left out."""
    rows: Dict[str, List[Dict[str, Any]]] = {}
    for events_file in sorted(jobs_dir.glob("*/events.jsonl")):
        project, mine, legacy = None, [], []
        for event in jev_verdict._events(events_file):
            etype, data = event.get("type"), event.get("data") or {}
            if etype == "debate_started":
                project = data.get("project")
                continue
            if etype not in ("verdict_source", "jev_verdict") or data.get("error"):
                continue
            # `kind` of a row is the project's kind (what jev_verdict.summarize groups by); the source's is source_kind.
            row = {**data, "source_kind": data.get("kind"), "job": events_file.parent.name,
                   "thread": event.get("thread_id"), "project": project,
                   "kind": (kinds or {}).get(project or "", "code"), "round": event.get("round")}
            if etype == "verdict_source" and data.get("role") == "shadow" and data.get("source"):
                if row["source_kind"] == "jev" and data.get("questions") != jev_verdict.QUESTION_VERSION:
                    continue
                mine.append(row)
            elif etype == "jev_verdict" and data.get("mode") == jev_verdict.SHADOW \
                    and data.get("questions") == jev_verdict.QUESTION_VERSION:
                legacy.append({**row, "source": "jev"})
        if any(r["source"] == "jev" for r in mine):
            legacy = []
        for row in mine + legacy:
            rows.setdefault(row["source"], []).append(row)
    return rows


def summarize(rows: List[Dict[str, Any]], bar: Dict[str, Any]) -> Dict[str, Any]:
    confusion: Dict[str, Dict[str, int]] = {v: {APPROVE: 0, REJECT: 0} for v in (APPROVE, REJECT, UNCERTAIN)}
    for row in rows:
        if row.get("verdict") in confusion and row.get("llm") in (APPROVE, REJECT):
            confusion[row["verdict"]][row["llm"]] += 1
    sure = [r for r in rows if r.get("verdict") in (APPROVE, REJECT)]
    agreement = sum(1 for r in sure if r.get("agree")) / len(sure) if sure else None
    return {"verdicts": len(rows), "sure": len(sure), "agreement": agreement, "confusion": confusion,
            "bar": {**bar, "met": len(rows) >= bar["min_verdicts"] and agreement is not None
                    and agreement >= bar["min_agreement"]}}


def _project_kinds() -> Dict[str, str]:
    kinds: Dict[str, str] = {}
    try:
        from project_registry import ProjectManager

        for entry in ProjectManager().list_projects():
            kinds[str(entry.get("id"))] = str(entry.get("kind") or "code")
    except Exception:  # the report still counts without kinds
        pass
    return kinds


def report(data_dir: Optional[Path] = None) -> Tuple[int, Dict[str, Any]]:
    """(exit code, JSON) of `guardian.py --verdict-sources`: 2 when a source file is invalid."""
    base = {"mode": mode(), "max_age_days": max_age_days()}
    try:
        sources = load_sources()
    except SourceError as exc:
        return 2, {**base, "ok": False, "error": str(exc), "sources": [], "stale": [], "shadow": {}}
    root = data_dir or Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()
    rows = collect(root / "debate-jobs", _project_kinds())
    shadow: Dict[str, Any] = {}
    for sid, spec in sorted(sources.items()):
        mine = rows.get(sid, [])
        shadow[sid] = summarize(mine, spec["shadow_bar"])
        if spec["kind"] == "jev":   # the J2 detail: by project kind and the cases a person has to read
            detail = jev_verdict.summarize(mine)
            shadow[sid].update(by_kind=detail["by_kind"], jev_approves_llm_rejects=detail["jev_approves_llm_rejects"])
    keys = ("id", "name", "kind", "description", "source", "roles", "projects", "timeout_s", "reviewed", "shadow_bar")
    return 0, {**base, "ok": True, "stale": stale(sources),
               "sources": [{k: s[k] for k in keys} for _, s in sorted(sources.items())], "shadow": shadow}

"""Session flow drafts (docs/design/session-flows.md, phase SF1).

A draft is the short, team-shaped lineup a workstation session sends (SD3): a flow kind and the character ids in
its seats, never providers, hosts or tools (constraint 4: a flow never widens tools). The hub builds the team dict
the conversion path already knows, runs it through team_to_flow (parse_lineup then lineup_flow) and checks the
result with validate_flow and compile_flow, so a session drafts, checks, fixes and checks again without touching
the operator flow store:

    {"id": "api-review", "name": "API review", "kind": "debate", "lead": "domain_architect",
     "critics": ["critic_redteam", "devops_specialist"], "proposers": [], "close": "jev_mcp_decision",
     "max_rounds": 2, "category": "development", "projects": ["code"]}

Drafts live one JSON file per id under <GUARDIAN_DATA_DIR>/flow-drafts/<ws>/ (SD1: their own directory, never in
flows/), at most MAX_DRAFTS_PER_WORKSTATION per workstation (SD6), and reach the operator store only when a person
promotes one (promote() here, through FlowStore.save, never over a builtin flow). A removed workstation's drafts
are deleted with it (SD6, session_registry.remove_workstation).
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import flow_graph
import mail_store
from flow_graph import MAX_NAME_CHARS
from persona_manager import PersonaManager
from project_registry import KINDS as PROJECT_KINDS
from session_registry import RESERVED_IDS
from team_manager import (FLOW_DEBATE, FLOW_PANEL, FLOW_PIPELINE, FLOW_VERSUS, FLOW_VERIFY, MAX_CRITICS,
                          MAX_EXCHANGES, MAX_PROPOSERS, MAX_ROUNDS_LIMIT, MAX_STAGES, MAX_STANCE_CHARS, MIN_STAGES,
                          SIDES, TEAM_CATEGORIES, TEAM_ID_PATTERN)

# The seat fields each flow kind takes (section 3 of the design); every other key of a draft is refused.
KIND_FIELDS = {
    FLOW_DEBATE: {"lead", "critics", "proposers", "close"},
    FLOW_PANEL: {"lead", "proposers", "close"},
    FLOW_VERIFY: {"lead", "critics", "close"},
    FLOW_PIPELINE: {"stages", "lead", "critics", "close"},
    FLOW_VERSUS: {"sides", "exchanges", "close"},
}
SEAT_FIELDS = set().union(*KIND_FIELDS.values())
DRAFT_KEYS = {"id", "name", "kind", "max_rounds", "category", "projects"}
MAX_DRAFTS_PER_WORKSTATION = 20  # SD6


class DraftError(ValueError):
    """A draft (or a draft store request) that cannot be used; the message names the field."""


def _now_iso() -> str:
    return mail_store.format_ts(datetime.now(timezone.utc))


# --- the short form ------------------------------------------------------------------------------------------------


def _character(value: Any, where: str) -> str:
    """A seat holds a plain character id: a dict here ({"persona": ..., "providers": [...]}) would smuggle member
    fields a draft must never carry."""
    if value is None:
        raise DraftError(f"seat {where}: a character id is missing")
    if not isinstance(value, str):
        raise DraftError(f"seat {where}: character id must be a string")
    if not TEAM_ID_PATTERN.fullmatch(value):
        raise DraftError(f"seat {where}: use a character id of 1-64 lowercase letters, digits, '-' or '_'")
    return value


def _characters(value: Any, where: str, low: int, high: int) -> List[str]:
    """A seat list of `low`-`high` plain character ids; a missing list counts as empty, so a required seat reports
    its count and not its absence."""
    values: Any = [] if value is None else value
    if not isinstance(values, list):
        raise DraftError(f"{where}: expected a list of character ids")
    if not low <= len(values) <= high:
        raise DraftError(f"{where}: {low}-{high} characters, got {len(values)}")
    return [_character(c, f"{where}[{i}]") for i, c in enumerate(values)]


def _sides(value: Any) -> List[Dict[str, str]]:
    """The two advocates of a versus draft: {"character": "<id>", "stance": "<text>"} and no other keys."""
    if not isinstance(value, list):
        raise DraftError('sides: expected a list of two {"character": ..., "stance": ...} objects')
    if len(value) != SIDES:
        raise DraftError(f"sides: exactly {SIDES} sides, got {len(value)}")
    sides: List[Dict[str, str]] = []
    for index, raw in enumerate(value):
        where = f"sides[{index}]"
        if not isinstance(raw, dict) or set(raw) != {"character", "stance"}:
            raise DraftError(f'{where}: expected {{"character": "<id>", "stance": "<text>"}} and no other keys')
        stance = raw["stance"]
        if not isinstance(stance, str) or not stance.strip():
            raise DraftError(f"{where}.stance: expected the position this side defends (a short text)")
        if len(stance) > MAX_STANCE_CHARS:
            raise DraftError(f"{where}.stance: at most {MAX_STANCE_CHARS} characters (got {len(stance)})")
        sides.append({"persona": _character(raw["character"], f"{where}.character"), "stance": stance.strip()})
    return sides


def to_team(draft: Any) -> Dict[str, Any]:
    """Validates the short form and builds the team dict parse_lineup accepts: the seats as plain persona strings,
    so no member carries providers (an empty list means the hub's default order, SD5) or a host or tools. Raises
    DraftError naming the field."""
    if not isinstance(draft, dict):
        raise DraftError("a draft is a JSON object")
    unknown = sorted(set(draft) - DRAFT_KEYS - SEAT_FIELDS)
    if unknown:
        raise DraftError(f"{unknown[0]}: unknown field")
    draft_id = draft.get("id")
    if not isinstance(draft_id, str) or not TEAM_ID_PATTERN.fullmatch(draft_id):
        raise DraftError("id: use 1-64 lowercase letters, digits, '-' or '_'")
    name = draft.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise DraftError(f"name: a short name (at most {MAX_NAME_CHARS} characters)")
    kind = draft.get("kind")
    if kind not in KIND_FIELDS:
        raise DraftError(f"kind: unknown flow {kind!r}; known: {sorted(KIND_FIELDS)}")
    for other in sorted(SEAT_FIELDS - KIND_FIELDS[kind]):
        if other in draft:
            raise DraftError(f"draft kind {kind!r} does not take {other!r}")
    rounds = draft.get("max_rounds")
    if rounds is not None and (not isinstance(rounds, int) or isinstance(rounds, bool)
                               or not 1 <= rounds <= MAX_ROUNDS_LIMIT):
        raise DraftError(f"max_rounds: a whole number from 1 to {MAX_ROUNDS_LIMIT}")
    category = draft.get("category")
    if category is not None and category not in TEAM_CATEGORIES:
        raise DraftError(f"category: use one of {', '.join(TEAM_CATEGORIES)}")
    projects = draft.get("projects")
    if projects is not None and (not isinstance(projects, list) or not projects
                                 or not all(isinstance(p, str) and p in PROJECT_KINDS for p in projects)
                                 or len(set(projects)) != len(projects)):
        raise DraftError(f"projects: a list of project kinds from {', '.join(PROJECT_KINDS)}")

    team: Dict[str, Any] = {"id": draft_id, "name": name}
    if rounds is not None:
        team["max_rounds"] = rounds
    if kind == FLOW_VERSUS:
        exchanges = draft.get("exchanges", 1)
        if not isinstance(exchanges, int) or isinstance(exchanges, bool) or not 1 <= exchanges <= MAX_EXCHANGES:
            raise DraftError(f"exchanges: a whole number from 1 to {MAX_EXCHANGES}")
        sides = _sides(draft.get("sides"))
        # lineup_flow ignores the expert for a versus; side A's character stands in so parse_lineup has a lead.
        team.update(expert=sides[0]["persona"], judge=_character(draft.get("close"), "close"),
                    flow={"kind": FLOW_VERSUS, "sides": sides, "exchanges": exchanges})
        return team
    team.update(expert=_character(draft.get("lead"), "lead"), judge=_character(draft.get("close"), "close"))
    if kind == FLOW_PIPELINE:
        team["flow"] = {"kind": FLOW_PIPELINE,
                        "stages": _characters(draft.get("stages"), "stages", MIN_STAGES, MAX_STAGES)}
        if draft.get("critics") is not None:   # a pipeline's critics are optional
            team["critic"] = _characters(draft.get("critics"), "critics", 0, MAX_CRITICS)
    elif kind == FLOW_PANEL:
        team.update(proposers=_characters(draft.get("proposers"), "proposers", 1, MAX_PROPOSERS),
                    flow={"kind": FLOW_PANEL})
    else:
        team["critic"] = _characters(draft.get("critics"), "critics", 1, MAX_CRITICS)
        if kind == FLOW_VERIFY:
            team["flow"] = {"kind": FLOW_VERIFY}
        elif draft.get("proposers") is not None:   # a debate's proposers are optional
            team["proposers"] = _characters(draft.get("proposers"), "proposers", 0, MAX_PROPOSERS)
    return team


def convert(draft: Any, persona_mgr: Optional[PersonaManager] = None) -> Tuple[Dict[str, Any], List[str]]:
    """The draft as a flow the engine already knows: to_team, then team_to_flow's path (parse_lineup and
    lineup_flow), without "converted_from" (a draft is not a team) but with the draft's own name, category and
    projects, checked by validate_flow. Raises DraftError; returns its warnings."""
    team = to_team(draft)
    try:
        flow = flow_graph.team_to_flow(team)
    except flow_graph.ConversionError as exc:
        raise DraftError(str(exc)) from None
    flow.pop("converted_from", None)
    flow["name"] = team["name"]
    if draft.get("category") is not None:
        flow["category"] = draft["category"]
    if draft.get("projects") is not None:
        flow["projects"] = list(draft["projects"])
    try:
        warnings = flow_graph.validate_flow(flow, persona_mgr)
    except flow_graph.FlowValidationError as exc:
        raise DraftError(str(exc)) from None
    return flow, warnings


def check(draft: Any, persona_mgr: Optional[PersonaManager] = None) -> Dict[str, Any]:
    """Converts and validates a draft, never raising on user input: {"ok": False, "error"} when it does not parse,
    else the flow, its warnings, the model calls of a first round and whether the engine runs the shape."""
    try:
        flow, warnings = convert(draft, persona_mgr)
    except DraftError as exc:
        return {"ok": False, "error": str(exc)}
    runnable, run_error = True, None
    try:
        flow_graph.compile_flow(flow)
    except flow_graph.FlowValidationError as exc:
        runnable, run_error = False, str(exc)
    return {"ok": True, "flow": flow, "warnings": warnings, "calls": flow_graph.calls_per_round(flow),
            "runnable": runnable, "run_error": run_error}


# --- the store -----------------------------------------------------------------------------------------------------


def drafts_dir() -> Path:
    """The draft store root, resolved per call (like FlowStore.operator_dir) so an isolated GUARDIAN_DATA_DIR
    works."""
    return mail_store.data_dir() / "flow-drafts"


def _check_ws(ws: Any) -> str:
    """A workstation id the registry would accept, checked before any path is built."""
    if not isinstance(ws, str) or not mail_store.NODE_RE.fullmatch(ws):
        raise DraftError(f"bad workstation id {ws!r}")
    if ws in RESERVED_IDS:
        raise DraftError(f"workstation id {ws} is reserved")
    return ws


def _ws_dir(ws: str) -> Path:
    return drafts_dir() / ws


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _records(directory: Path) -> List[Dict[str, Any]]:
    if not directory.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read(path)
        if data is not None:
            records.append(data)
    return records


def save_draft(ws: Any, draft: Any, persona_mgr: Optional[PersonaManager] = None) -> Dict[str, Any]:
    """Checks a draft (nothing invalid is stored) and writes its record under the workstation: the short form as
    given, the converted flow, the warnings and whether it runs. A re-save keeps "created"; a new id past
    MAX_DRAFTS_PER_WORKSTATION is refused (SD6)."""
    ws = _check_ws(ws)
    checked = check(draft, persona_mgr)
    if not checked["ok"]:
        raise DraftError(checked["error"])
    draft_id = draft["id"]
    previous = _read(_ws_dir(ws) / f"{draft_id}.json")
    if previous is None and len(_records(_ws_dir(ws))) >= MAX_DRAFTS_PER_WORKSTATION:
        raise DraftError(f"workstation {ws!r} already holds {MAX_DRAFTS_PER_WORKSTATION} drafts; "
                         "delete one before saving a new one (SD6)")
    now = _now_iso()
    record = {"workstation": ws, "draft": draft, "flow": checked["flow"], "warnings": checked["warnings"],
              "runnable": checked["runnable"], "created": (previous or {}).get("created") or now, "modified": now}
    directory = _ws_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{draft_id}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return record


def list_drafts(ws: Optional[str] = None) -> List[Dict[str, Any]]:
    """Stored draft records sorted by workstation then draft id; unreadable files are skipped. Without a ws, every
    workstation's drafts (only directory names the registry would accept are read)."""
    root = drafts_dir()
    if ws is not None:
        directories = [_ws_dir(_check_ws(ws))]
    elif root.is_dir():
        directories = [p for p in sorted(root.iterdir()) if p.is_dir() and mail_store.NODE_RE.fullmatch(p.name)]
    else:
        directories = []
    records = [record for directory in directories for record in _records(directory)]
    records.sort(key=lambda r: (str(r.get("workstation", "")), str(r.get("draft", {}).get("id", ""))))
    return records


def get_draft(ws: Any, draft_id: Any) -> Optional[Dict[str, Any]]:
    """One stored record; a bad draft id answers None (no path is built from it)."""
    _check_ws(ws)
    if not isinstance(draft_id, str) or not TEAM_ID_PATTERN.fullmatch(draft_id):
        return None
    return _read(_ws_dir(ws) / f"{draft_id}.json")


def delete_draft(ws: Any, draft_id: Any) -> bool:
    """Deletes one draft; False when it was not there."""
    _check_ws(ws)
    if not isinstance(draft_id, str) or not TEAM_ID_PATTERN.fullmatch(draft_id):
        return False
    path = _ws_dir(ws) / f"{draft_id}.json"
    try:
        path.unlink()
        return True
    except OSError:
        return False


def delete_workstation(ws: Any) -> int:
    """Removes the workstation's whole draft directory (SD6) and returns how many drafts went with it; 0 when the
    workstation held none."""
    _check_ws(ws)
    directory = _ws_dir(ws)
    if not directory.is_dir():
        return 0
    removed = len(list(directory.glob("*.json")))
    shutil.rmtree(directory)
    return removed


def promote(ws: Any, draft_id: Any, flow_store: flow_graph.FlowStore, persona_mgr: Optional[PersonaManager] = None,
            as_id: Optional[str] = None, overwrite: bool = False) -> Dict[str, Any]:
    """Saves a stored draft as a custom operator flow (SD1: a person's action) through FlowStore.save, records where
    it came from and removes the draft. The flow is re-converted from the stored short form, never trusted from the
    record; a builtin flow id is always refused, an existing custom one only with overwrite."""
    _check_ws(ws)
    record = get_draft(ws, draft_id)
    if record is None:
        raise DraftError(f"no draft {draft_id!r} for workstation {ws!r}")
    target = as_id or draft_id
    if not isinstance(target, str) or not TEAM_ID_PATTERN.fullmatch(target):
        raise DraftError("bad flow id: use 1-64 lowercase letters, digits, '-' or '_'")
    if (Path(flow_store.builtin_dir) / f"{target}.json").exists():
        # Refused by file, not by the stored source: a custom flow shadowing a builtin id must not become a way
        # for a session draft to take that id.
        raise DraftError(f"{target!r} is a builtin flow id; a draft never takes one")
    existing = flow_store.get(target)
    if existing is not None and not overwrite:
        raise DraftError(f"flow {target!r} already exists; pass overwrite to replace it")
    flow, _ = convert(record.get("draft"), persona_mgr)
    flow["id"] = target
    flow["promoted_from"] = {"workstation": ws, "draft": draft_id, "at": _now_iso()}
    warnings = flow_store.save(flow)
    delete_draft(ws, draft_id)
    return {"ok": True, "id": target, "warnings": warnings}

"""
Flow graphs (docs/design/flow-graph.md, phase G1): the flow file, its validation, the store, and the conversion of
teams and flow templates into flows.

A flow is a diagram: seats (자리; a character with a job), bundles (동시 묶음; seats that run in parallel), one close
(마감; the shared close: Judge, jev gate, action checks) and links (연결) between them, plus the start form
(parameters, task text) of a flow template. `start`, `close` and `end` are fixed ids.

    {
      "id": "review-two-stage", "name": "Two-stage review", "category": "development", "projects": ["code"],
      "params": {"TARGET": {"type": "string", "max": 200}}, "task": "Review ${TARGET}.",
      "seats": [{"id": "lead", "job": "write", "character": "guardian_lead_dev", "providers": ["claude"]},
                {"id": "sec", "job": "critique", "character": "guardian_security_critic"},
                {"id": "qa", "job": "critique", "mode": "checklist", "character": "guardian_qa_critic"}],
      "bundles": [{"id": "review", "seats": ["sec", "qa"]}],
      "close": {"character": "guardian_dev_judge", "allowed_tools": ["jev_mcp:run_tests"]},
      "links": [{"from": "start", "to": "lead"}, {"from": "lead", "to": "review"},
                {"from": "review", "to": "lead", "look_back": true}, {"from": "review", "to": "close"},
                {"from": "close", "to": "lead", "exit": "rejected", "max_rounds": 3},
                {"from": "close", "to": "end", "exit": "approved"}]
    }

G1 only defines, validates, stores and converts; the engine still runs teams and templates (G2 turns a flow into the
engine's step lists). Teams and templates stay readable: `team_to_flow` and `template_to_flow` give the flow each one
is, so the report lists every way to start work as a flow. Builtin flows live in configs/flows (GUARDIAN_FLOWS
overrides the directory), the operator's own in <GUARDIAN_DATA_DIR>/flows; a custom file shadows a builtin one.
Validation lives here and nowhere else: the hub only forwards.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import character
import flow_templates
import mail_store
from llm_runner import KNOWN_PROVIDERS
from mcp_client import RETIRED_TOOLS, TOOLS
from persona_manager import PersonaManager
from project_registry import KINDS as PROJECT_KINDS
from remote_agents import host_syntax_error
from team_manager import (CODE_TOOLS, FLOW_PANEL, FLOW_PIPELINE, FLOW_VERIFY, FLOW_VERSUS, MAX_EXCHANGES,
                          MAX_ROUNDS_LIMIT, MAX_STANCE_CHARS, TEAM_CATEGORIES, TEAM_ID_PATTERN, Lineup, Member,
                          TeamManager, TeamValidationError, parse_lineup)

START, CLOSE, END = "start", "close", "end"
FIXED_IDS = (START, CLOSE, END)
SEAT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

JOBS = ("write", "critique", "rewrite", "rebut", "summarise")
CRITIQUE_MODES = flow_templates.CRITIQUE_MODES
INPUT_MODES = ("raw", "condensed", "structured", "latest")
HISTORY_MODES = ("digest", "full", "none")
EXITS = ("approved", "rejected")

# The engine seats a job may use; a character (format 2) must list one of them in its `seats`.
JOB_SEATS = {
    "write": ("expert", "proposer", "stage"),
    "rewrite": ("stage", "expert"),
    "rebut": ("side",),
    ("critique", "object"): ("critic",),
    ("critique", "checklist"): ("verifier",),
}

FLOW_KEYS = {"id", "name", "category", "projects", "params", "task", "seats", "bundles", "close", "links", "layout",
             "context", "converted_from", "promoted_from", "reviewed", "created", "modified"}
SEAT_KEYS = {"id", "job", "character", "providers", "code_tools", "host", "focus", "mode", "stance", "input",
             "history", "instruction"}
BUNDLE_KEYS = {"id", "seats", "exchanges"}
CLOSE_KEYS = {"character", "providers", "code_tools", "host", "allowed_tools", "input", "history"}
LINK_KEYS = {"from", "to", "look_back", "blocked", "exit", "max_rounds"}

MAX_SEATS = 12
MAX_BUNDLE_SEATS = 4
MAX_CALLS_PER_ROUND = 16       # seats, the extra answers of rebut bundles and the close
MAX_FOCUS_CHARS = 200
MAX_INSTRUCTION_CHARS = 2000
MAX_NAME_CHARS = flow_templates.MAX_LABEL_CHARS


class FlowValidationError(ValueError):
    """A flow file that cannot be used; the message names the field."""


def _fail(where: str, message: str) -> FlowValidationError:
    return FlowValidationError(f"{where}: {message}")


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(where, "expected true or false")
    return value


# --- validation ---------------------------------------------------------------------------------------------------


def _check_member_fields(where: str, raw: Dict[str, Any]) -> None:
    """providers, code_tools and host: the member fields of a team, with the same rules."""
    providers = raw.get("providers", [])
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        raise _fail(f"{where}.providers", "expected a list of provider names")
    unknown = [p for p in providers if p not in KNOWN_PROVIDERS]
    if unknown:
        raise _fail(f"{where}.providers", f"unknown provider(s) {unknown}; known: {KNOWN_PROVIDERS}")
    code_tools = raw.get("code_tools")
    if code_tools is not None and code_tools not in CODE_TOOLS:
        raise _fail(f"{where}.code_tools", f"expected null or one of {list(CODE_TOOLS)}")
    host = raw.get("host")
    if host is not None:
        problem = host_syntax_error(host)
        if problem:
            raise _fail(f"{where}.host", problem)


def _check_modes(where: str, raw: Dict[str, Any]) -> None:
    if raw.get("input", "raw") not in INPUT_MODES:
        raise _fail(f"{where}.input", f"use one of {', '.join(INPUT_MODES)}")
    if raw.get("history", "digest") not in HISTORY_MODES:
        raise _fail(f"{where}.history", f"use one of {', '.join(HISTORY_MODES)}")


def _check_character(where: str, persona_id: Any, seats: Iterable[str], persona_mgr: Optional[PersonaManager]) -> None:
    """The character exists and (for a format 2 character) may take one of the engine seats of its job."""
    if not isinstance(persona_id, str) or not persona_id:
        raise _fail(f"{where}.character", "expected a character id")
    if persona_mgr is None:
        return
    persona = persona_mgr.get_persona(persona_id)
    if persona is None:
        raise _fail(f"{where}.character", f"unknown character {persona_id!r}")
    allowed = character.persona_seats(persona)
    seats = tuple(seats)
    if allowed is not None and not set(seats) & set(allowed):
        raise _fail(f"{where}.character", f"{persona_id!r} may take {', '.join(allowed) or 'no job'}, not "
                                          f"{' or '.join(seats)}")


def _job_seats(seat: Dict[str, Any]) -> Tuple[str, ...]:
    if seat["job"] == "critique":
        return JOB_SEATS[("critique", seat.get("mode", "object"))]
    return JOB_SEATS[seat["job"]]


def _check_seat(index: int, raw: Any, params: Dict[str, Any], persona_mgr: Optional[PersonaManager]) -> Dict[str, Any]:
    where = f"seats[{index}]"
    if not isinstance(raw, dict):
        raise _fail(where, 'expected {"id": ..., "job": ..., "character": ...}')
    unknown = sorted(set(raw) - SEAT_KEYS)
    if unknown:
        raise _fail(f"{where}.{unknown[0]}", "unknown key")
    seat_id = raw.get("id")
    if not isinstance(seat_id, str) or not SEAT_ID.fullmatch(seat_id) or seat_id in FIXED_IDS:
        raise _fail(f"{where}.id", "use 1-32 lowercase letters, digits, '-' or '_' (not start, close or end)")
    where = f"seats.{seat_id}"
    job = raw.get("job")
    if job not in JOBS:
        raise _fail(f"{where}.job", f"use one of {', '.join(JOBS)}")
    if "mode" in raw and job != "critique":
        raise _fail(f"{where}.mode", "only a critique has a mode")
    if raw.get("mode", "object") not in CRITIQUE_MODES:
        raise _fail(f"{where}.mode", f"use one of {', '.join(CRITIQUE_MODES)}")
    if "instruction" in raw:
        if job != "summarise":
            raise _fail(f"{where}.instruction", "only a summariser takes its own instruction (the others get a role "
                                                "card from their job)")
        text = raw["instruction"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INSTRUCTION_CHARS:
            raise _fail(f"{where}.instruction", f"a text of at most {MAX_INSTRUCTION_CHARS} characters")
    if job == "summarise":
        if "character" in raw:
            _check_character(where, raw["character"], (), None)
    else:
        _check_character(where, raw.get("character"), _job_seats(raw), persona_mgr)
    _check_member_fields(where, raw)
    focus = raw.get("focus")
    if focus is not None and (not isinstance(focus, str) or not focus.strip() or len(focus) > MAX_FOCUS_CHARS
                              or "\n" in focus):
        raise _fail(f"{where}.focus", f"one sentence of at most {MAX_FOCUS_CHARS} characters")
    if job == "rebut":
        stance = raw.get("stance")
        flow_templates._check_text_uses(f"{where}.stance", stance, params)
        if not stance.strip():
            raise _fail(f"{where}.stance", "expected the position this side defends (from a person, never a model)")
        longest = len(flow_templates.PLACEHOLDER.sub(lambda m: "x" * flow_templates._max_len(params[m.group(1)]),
                                                     stance))
        if longest > MAX_STANCE_CHARS:
            raise _fail(f"{where}.stance", f"can reach {longest} characters after filling in; at most "
                                           f"{MAX_STANCE_CHARS}")
    elif "stance" in raw:
        raise _fail(f"{where}.stance", "only a rebut seat defends a stance")
    _check_modes(where, raw)
    return raw


def _check_close(raw: Any, persona_mgr: Optional[PersonaManager]) -> Tuple[Dict[str, Any], List[str]]:
    if not isinstance(raw, dict):
        raise _fail(CLOSE, 'expected {"character": "<judge>", ...}')
    unknown = sorted(set(raw) - CLOSE_KEYS)
    if unknown:
        raise _fail(f"{CLOSE}.{unknown[0]}", "unknown key")
    _check_character(CLOSE, raw.get("character"), ("judge",), persona_mgr)
    _check_member_fields(CLOSE, raw)
    _check_modes(CLOSE, raw)
    warnings: List[str] = []
    tools = raw.get("allowed_tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise _fail(f"{CLOSE}.allowed_tools", "expected a list of tool names")
        risk = {t["name"]: t["risk"] for t in TOOLS}
        for tool in tools:
            if tool in RETIRED_TOOLS:
                warnings.append(f"close.allowed_tools: {tool} is retired and ignored ({RETIRED_TOOLS[tool]})")
            elif tool not in risk:
                raise _fail(f"{CLOSE}.allowed_tools", f"unknown tool {tool!r}; known: {sorted(risk)}")
            elif risk[tool] == "high":
                warnings.append(f"close.allowed_tools: {tool} is high risk; every proposal still needs a person's "
                                "approval")
    if raw.get("input", "raw") != "raw":
        warnings.append("close.input: the Judge reads a shortened view; critiques no longer reach it verbatim "
                        "(decision D4 of flow-graph.md)")
    return raw, warnings


def _check_bundles(raw: Any, seats: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise _fail("bundles", 'expected a list of {"id": ..., "seats": [...]}')
    bundles: Dict[str, Dict[str, Any]] = {}
    owner: Dict[str, str] = {}
    for index, bundle in enumerate(raw):
        where = f"bundles[{index}]"
        if not isinstance(bundle, dict):
            raise _fail(where, 'expected {"id": ..., "seats": [...]}')
        unknown = sorted(set(bundle) - BUNDLE_KEYS)
        if unknown:
            raise _fail(f"{where}.{unknown[0]}", "unknown key")
        bundle_id = bundle.get("id")
        if not isinstance(bundle_id, str) or not SEAT_ID.fullmatch(bundle_id) or bundle_id in FIXED_IDS:
            raise _fail(f"{where}.id", "use 1-32 lowercase letters, digits, '-' or '_' (not start, close or end)")
        if bundle_id in seats or bundle_id in bundles:
            raise _fail(f"{where}.id", f"{bundle_id!r} is used twice")
        where = f"bundles.{bundle_id}"
        members = bundle.get("seats")
        if (not isinstance(members, list) or not 1 <= len(members) <= MAX_BUNDLE_SEATS
                or len(set(map(str, members))) != len(members)):
            raise _fail(f"{where}.seats", f"1 to {MAX_BUNDLE_SEATS} different seat ids")
        for member in members:
            if member not in seats:
                raise _fail(f"{where}.seats", f"no seat {member!r}")
            if member in owner:
                raise _fail(f"{where}.seats", f"seat {member!r} is already in bundle {owner[member]!r}")
            if seats[member]["job"] == "summarise":
                raise _fail(f"{where}.seats", f"{member!r} is a summariser; it feeds one seat and runs alone")
            owner[member] = bundle_id
        rebut = [m for m in members if seats[m]["job"] == "rebut"]
        if rebut and (len(rebut) != 2 or len(members) != 2):
            raise _fail(f"{where}.seats", "rebut seats come as one bundle of exactly two sides")
        if "exchanges" in bundle:
            exchanges = bundle["exchanges"]
            if not rebut:
                raise _fail(f"{where}.exchanges", "only a bundle of two rebut seats answers back and forth")
            if not isinstance(exchanges, int) or isinstance(exchanges, bool) or not 1 <= exchanges <= MAX_EXCHANGES:
                raise _fail(f"{where}.exchanges", f"a whole number from 1 to {MAX_EXCHANGES}")
        bundles[bundle_id] = bundle
    lone_rebut = [s for s, data in seats.items() if data["job"] == "rebut" and s not in owner]
    if lone_rebut:
        raise _fail(f"seats.{lone_rebut[0]}", "a rebut seat sits in a bundle of two sides")
    return bundles


def _check_links(raw: Any, seats: Dict[str, Dict[str, Any]], bundles: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Shape of every link and the rules of the close's exits; the graph rules follow in _check_graph."""
    if not isinstance(raw, list) or not raw:
        raise _fail("links", 'expected a list of {"from": ..., "to": ...}')
    targets = set(seats) | set(bundles)
    links: List[Dict[str, Any]] = []
    exits: Dict[str, Dict[str, Any]] = {}
    for index, link in enumerate(raw):
        where = f"links[{index}]"
        if not isinstance(link, dict):
            raise _fail(where, 'expected {"from": ..., "to": ...}')
        unknown = sorted(set(link) - LINK_KEYS)
        if unknown:
            raise _fail(f"{where}.{unknown[0]}", "unknown key")
        source, target = link.get("from"), link.get("to")
        if source not in targets | {START, CLOSE}:
            raise _fail(f"{where}.from", f"no seat or bundle {source!r} (or start, close)")
        if target not in targets | {CLOSE, END}:
            raise _fail(f"{where}.to", f"no seat or bundle {target!r} (or close, end)")
        if source == target:
            raise _fail(where, "a seat does not read itself")
        look_back = _flag(link.get("look_back", False), f"{where}.look_back")
        _flag(link.get("blocked", False), f"{where}.blocked")
        exit_kind = link.get("exit")
        if source == CLOSE:
            if exit_kind not in EXITS:
                raise _fail(f"{where}.exit", "a link from the close is its approved or rejected exit")
            if exit_kind in exits:
                raise _fail(f"{where}.exit", f"the close has one {exit_kind} exit")
            if exit_kind == "approved" and target != END:
                raise _fail(where, "the approved exit goes to the end")
            if exit_kind == "rejected" and target not in targets:
                raise _fail(where, "the rejected exit goes to the seat or bundle where the next round starts")
            if look_back or link.get("blocked"):
                raise _fail(where, "an exit of the close is neither look-back nor blocked")
            exits[exit_kind] = link
        elif exit_kind is not None:
            raise _fail(f"{where}.exit", "only the close has exits")
        elif target == END:
            raise _fail(where, "only the close's approved exit reaches the end")
        if "max_rounds" in link:
            rounds = link["max_rounds"]
            if exit_kind != "rejected":
                raise _fail(f"{where}.max_rounds", "belongs to the rejected exit")
            if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= MAX_ROUNDS_LIMIT:
                raise _fail(f"{where}.max_rounds", f"a whole number from 1 to {MAX_ROUNDS_LIMIT}")
        if look_back and START in (source, target):
            raise _fail(where, "the start is read in every round; a look-back link from it means nothing")
        links.append(link)
    for exit_kind in EXITS:
        if exit_kind not in exits:
            raise _fail("links", f"the close needs its {exit_kind} exit")
    return links


def _check_graph(seats: Dict[str, Dict[str, Any]], bundles: Dict[str, Dict[str, Any]],
                 links: List[Dict[str, Any]], close: Dict[str, Any]) -> List[str]:
    """Order and reachability: forward links acyclic, every seat runs in some round and reaches the close."""
    owner = {m: b for b, bundle in bundles.items() for m in bundle["seats"]}

    def unit(element: str) -> str:   # a bundle member is ordered with its bundle
        return owner.get(element, element)

    forward = [l for l in links if not l.get("look_back") and l.get("from") != CLOSE]
    edges: Dict[str, Set[str]] = {}
    for link in forward:
        a, b = unit(link["from"]), unit(link["to"])
        if a == b:
            raise _fail("links", f"{link['from']} -> {link['to']} stays inside bundle {a!r}; members of a bundle "
                                 "run together and do not read each other")
        edges.setdefault(a, set()).add(b)

    state: Dict[str, int] = {}

    def visit(node: str, path: List[str]) -> None:
        state[node] = 1
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt):] + [nxt] if nxt in path else [node, nxt]
                raise _fail("links", f"a loop of forward links ({' -> '.join(cycle)}); mark the backward one "
                                     "look_back, or let the close's rejected exit start the next round")
            if state.get(nxt) is None:
                visit(nxt, path + [nxt])
        state[node] = 2

    for node in sorted(edges):
        if state.get(node) is None:
            visit(node, [node])

    def reach(starts: Iterable[str]) -> Set[str]:
        seen, todo = set(), list(starts)
        while todo:
            node = todo.pop()
            if node in seen:
                continue
            seen.add(node)
            todo.extend(edges.get(node, ()))
        return seen

    rejected = next(l for l in links if l.get("exit") == "rejected")
    runs = reach([START]) | reach([unit(rejected["to"])])
    to_close = {n for n in set(map(unit, seats)) | set(bundles) if CLOSE in reach([n])}
    for seat_id in seats:
        if unit(seat_id) not in runs:
            raise _fail(f"seats.{seat_id}", "never runs: link it from the start, from a seat that runs, or make it "
                                            "the target of the rejected exit")
        if unit(seat_id) not in to_close:
            raise _fail(f"seats.{seat_id}", "its work never reaches the close; link it onwards")
    if CLOSE not in reach([START]):
        raise _fail("links", "nothing links the start to the close")

    warnings: List[str] = []
    feeds: Dict[str, List[str]] = {}
    for link in forward:
        feeds.setdefault(link["from"], []).append(link["to"])
    for seat_id, seat in seats.items():
        if seat["job"] != "summarise":
            continue
        out = feeds.get(seat_id, [])
        if len(out) != 1:
            raise _fail(f"seats.{seat_id}", "a summariser feeds exactly one seat (or the close)")
        receiver = seats.get(out[0], close if out[0] == CLOSE else {})
        if receiver.get("input", "raw") != "condensed":
            warnings.append(f"seats.{seat_id}: it feeds {out[0]!r}, which does not read condensed input")
    for name, element in [*seats.items(), (CLOSE, close)]:
        if element.get("input", "raw") == "condensed":
            readers = [l["from"] for l in forward if l["to"] == name]
            if not any(seats.get(r, {}).get("job") == "summarise" for r in readers):
                raise _fail(f"{name}.input" if name == CLOSE else f"seats.{name}.input",
                            "condensed input needs a summariser linked in front of it")
    return warnings


def calls_per_round(data: Dict[str, Any]) -> int:
    """Model calls of a first round: every seat once, each answer of a rebut bundle twice more, the close once."""
    extra = sum(2 * b.get("exchanges", 1) for b in data.get("bundles") or []
                if any(s.get("job") == "rebut" for s in data.get("seats", []) if s.get("id") in b.get("seats", [])))
    return len(data.get("seats", [])) + extra + 1


def validate_flow(data: Any, persona_mgr: Optional[PersonaManager] = None) -> List[str]:
    """Raises FlowValidationError naming the field; returns warnings. Without a persona manager the characters are
    not looked up (shape only)."""
    if not isinstance(data, dict):
        raise FlowValidationError("a flow is a JSON object")
    unknown = sorted(set(data) - FLOW_KEYS)
    if unknown:
        raise _fail(unknown[0], "unknown key")
    flow_id = data.get("id")
    if not isinstance(flow_id, str) or not TEAM_ID_PATTERN.fullmatch(flow_id):
        raise _fail("id", "use 1-64 lowercase letters, digits, '-' or '_'")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise _fail("name", f"a short name (at most {MAX_NAME_CHARS} characters)")
    category = data.get("category")
    if category is not None and category not in TEAM_CATEGORIES:
        raise _fail("category", f"use one of {', '.join(TEAM_CATEGORIES)}")
    projects = data.get("projects")
    if projects is not None and (not isinstance(projects, list) or not projects
                                 or not all(isinstance(p, str) and p in PROJECT_KINDS for p in projects)
                                 or len(set(projects)) != len(projects)):
        raise _fail("projects", f"a list of project kinds from {', '.join(PROJECT_KINDS)} (leave it out for any)")

    params = data.get("params", {})
    if not isinstance(params, dict) or len(params) > flow_templates.MAX_PARAMS:
        raise _fail("params", f"an object with at most {flow_templates.MAX_PARAMS} parameters")
    try:
        for pname, spec in params.items():
            flow_templates._check_param(pname, spec)
        task = data.get("task")
        if task is not None or params:
            flow_templates._check_text_uses("task", task, params)
            if not str(task).strip():
                raise _fail("task", "expected the task text the work starts with")
            longest = len(flow_templates.PLACEHOLDER.sub(
                lambda m: "x" * flow_templates._max_len(params[m.group(1)]), task))
            if longest > flow_templates.MAX_TASK_CHARS:
                raise _fail("task", f"can reach {longest} characters after filling in; at most "
                                    f"{flow_templates.MAX_TASK_CHARS}")
    except flow_templates.TemplateValidationError as exc:
        raise FlowValidationError(str(exc)) from None

    seats_raw = data.get("seats")
    if not isinstance(seats_raw, list) or not seats_raw:
        raise _fail("seats", "expected a list of seats (a character with a job each)")
    if len(seats_raw) > MAX_SEATS:
        raise _fail("seats", f"at most {MAX_SEATS} seats")
    seats: Dict[str, Dict[str, Any]] = {}
    try:
        for index, raw in enumerate(seats_raw):
            seat = _check_seat(index, raw, params, persona_mgr)
            if seat["id"] in seats:
                raise _fail(f"seats[{index}].id", f"{seat['id']!r} is used twice")
            seats[seat["id"]] = seat
    except flow_templates.TemplateValidationError as exc:
        raise FlowValidationError(str(exc)) from None
    if not any(s["job"] in ("write", "rewrite", "rebut") for s in seats.values()):
        raise _fail("seats", "at least one seat writes (write, rewrite or rebut); the close needs something to decide")
    bundles = _check_bundles(data.get("bundles"), seats)
    close, warnings = _check_close(data.get("close"), persona_mgr)
    links = _check_links(data.get("links"), seats, bundles)
    warnings += _check_graph(seats, bundles, links, close)
    calls = calls_per_round(data)
    if calls > MAX_CALLS_PER_ROUND:
        raise _fail("seats", f"{calls} model calls per round; at most {MAX_CALLS_PER_ROUND}")

    layout = data.get("layout")
    if layout is not None:
        known = set(seats) | set(bundles) | set(FIXED_IDS)
        if not isinstance(layout, dict):
            raise _fail("layout", "expected {\"<id>\": [x, y]}")
        for key, point in layout.items():
            if key not in known:
                raise _fail(f"layout.{key}", "no such element")
            if (not isinstance(point, list) or len(point) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in point)):
                raise _fail(f"layout.{key}", "expected [x, y]")
    context = data.get("context")
    if context is not None:
        from code_context import ContextError, validate_patterns, validate_ref

        if not isinstance(context, dict) or set(context) - {"paths", "ref"}:
            raise _fail("context", 'expected {"paths": [glob...], "ref": "HEAD"}')
        try:
            validate_patterns(context.get("paths", []))
            validate_ref(str(context.get("ref") or "HEAD"))
        except ContextError as exc:
            raise _fail("context", str(exc)) from None
    if "reviewed" in data:
        try:
            datetime.fromisoformat(str(data["reviewed"]))
        except ValueError:
            raise _fail("reviewed", "use a date like 2026-11-01") from None
    promoted = data.get("promoted_from")
    if promoted is not None:   # set by flow_drafts.promote when a session draft became this flow
        if not isinstance(promoted, dict) or set(promoted) != {"workstation", "draft", "at"}:
            raise _fail("promoted_from", 'expected {"workstation": "<id>", "draft": "<id>", "at": "<UTC timestamp>"}')
        if (not isinstance(promoted["workstation"], str)
                or not mail_store.NODE_RE.fullmatch(promoted["workstation"])):
            raise _fail("promoted_from.workstation", "a workstation id")
        if not isinstance(promoted["draft"], str) or not TEAM_ID_PATTERN.fullmatch(promoted["draft"]):
            raise _fail("promoted_from.draft", "a draft id")
        if not isinstance(promoted["at"], str) or not promoted["at"].endswith("Z"):
            raise _fail("promoted_from.at", "an ISO 8601 UTC timestamp ending in Z")
    used = {n for text in [data.get("task") or "", *(s.get("stance", "") for s in seats.values())]
            for n in flow_templates._placeholders(text)}
    warnings += [f"params.{p} is declared but neither the task nor a stance uses it" for p in params if p not in used]
    return warnings


# --- conversion of teams and templates ----------------------------------------------------------------------------


class ConversionError(ValueError):
    """A team or template that cannot be written as a flow (it does not resolve as it is either)."""


def _seat(seat_id: str, job: str, member: Member, **extra: Any) -> Dict[str, Any]:
    seat: Dict[str, Any] = {"id": seat_id, "job": job, "character": member.persona, "providers": list(member.providers)}
    for key in ("code_tools", "host"):
        if getattr(member, key):
            seat[key] = getattr(member, key)
    seat.update(extra)
    return seat


def _close(lineup: Lineup) -> Dict[str, Any]:
    close: Dict[str, Any] = {"character": lineup.judge.persona, "providers": list(lineup.judge.providers)}
    for key in ("code_tools", "host"):
        if getattr(lineup.judge, key):
            close[key] = getattr(lineup.judge, key)
    if lineup.allowed_tools is not None:
        close["allowed_tools"] = list(lineup.allowed_tools)
    return close


def _rejected(target: str, rounds: Optional[int]) -> Dict[str, Any]:
    link: Dict[str, Any] = {"from": CLOSE, "to": target, "exit": "rejected"}
    if rounds is not None:
        link["max_rounds"] = rounds
    return link


def _critics(lineup: Lineup, mode: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    """The critic seats, the bundle around a panel (none for one critic) and the id the links use."""
    extra = {"mode": mode} if mode != "object" else {}
    seats = [_seat(f"critic-{i + 1}", "critique", m, **extra) for i, m in enumerate(lineup.critics)]
    if len(seats) == 1:
        return seats, [], seats[0]["id"]
    return seats, [{"id": "critics", "seats": [s["id"] for s in seats]}], "critics"


def lineup_flow(lineup: Lineup, steps: Optional[List[Dict[str, Any]]] = None,
                rounds: Optional[int] = None) -> Dict[str, Any]:
    """The seats, bundles, close and links of a lineup: its flow kind, or a template's custom steps on it.

    The shapes follow debate_engine._steps_of: the lead alone (or the lead with blind proposers in the first round,
    then the lead alone), the critics after it; a panel's drafts go straight to the close; a pipeline's stages in
    order, the lead rewriting alone after a rejection; a versus' two sides in one bundle. The rejected exit starts the
    next round at the lead, which reads the last round through look-back links."""
    rounds = lineup.max_rounds if rounds is None else rounds
    kind = lineup.flow_kind
    seats: List[Dict[str, Any]] = []
    bundles: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []

    if kind == FLOW_VERSUS:
        seats = [_seat(f"side-{label}", "rebut", m, stance=m.stance) for label, m in zip("ab", lineup.sides)]
        bundles = [{"id": "sides", "seats": [s["id"] for s in seats], "exchanges": lineup.exchanges}]
        links = [{"from": START, "to": "sides"}, {"from": "sides", "to": CLOSE}, _rejected("sides", rounds)]
    elif kind == FLOW_PIPELINE:
        stages = [_seat(f"stage-{i + 1}", "write" if i == 0 else "rewrite", m) for i, m in enumerate(lineup.stages)]
        lead = _seat("lead", "rewrite", lineup.expert)
        crit, crit_bundles, crit_id = _critics(lineup, "object") if lineup.critics else ([], [], CLOSE)
        seats = [*stages, lead, *crit]
        bundles = crit_bundles
        links = [{"from": START, "to": stages[0]["id"]}]
        links += [{"from": a["id"], "to": b["id"]} for a, b in zip(stages, stages[1:])]
        links += [{"from": stages[-1]["id"], "to": crit_id}, {"from": stages[-1]["id"], "to": "lead",
                                                              "look_back": True},
                  {"from": "lead", "to": crit_id}]
        if crit:
            links += [{"from": crit_id, "to": "lead", "look_back": True}, {"from": crit_id, "to": CLOSE}]
        links.append(_rejected("lead", rounds))
    else:
        custom = steps or []
        critiqued = (any(s["kind"] == "critique" for s in custom) if custom else kind != FLOW_PANEL)
        blind = (any(s["kind"] == "blind_draft" for s in custom) if custom
                 else kind == FLOW_PANEL or bool(lineup.proposers))
        mode = next((s.get("mode", "object") for s in custom if s["kind"] == "critique"),
                    "checklist" if kind == FLOW_VERIFY and not custom else "object")
        lead = _seat("lead", "write", lineup.expert)
        drafters = [lead]
        if blind:
            drafters += [_seat(f"proposer-{i + 1}", "write", m) for i, m in enumerate(lineup.proposers)]
            bundles.append({"id": "drafts", "seats": [s["id"] for s in drafters]})
        draft_id = "drafts" if blind else "lead"
        seats = list(drafters)
        links = [{"from": START, "to": draft_id}]
        if critiqued:
            crit, crit_bundles, crit_id = _critics(lineup, mode)
            seats += crit
            bundles += crit_bundles
            links += [{"from": draft_id, "to": crit_id}, {"from": crit_id, "to": "lead", "look_back": True},
                      {"from": crit_id, "to": CLOSE}]
            if blind:
                links.append({"from": "drafts", "to": "lead", "look_back": True})
        else:
            links.append({"from": draft_id, "to": CLOSE})   # one round: nothing is critiqued (the engine clamps it)
        links.append(_rejected("lead", rounds))
    links.append({"from": CLOSE, "to": END, "exit": "approved"})
    flow: Dict[str, Any] = {"seats": seats, "close": _close(lineup), "links": links}
    if bundles:
        flow["bundles"] = bundles
    if lineup.context:
        flow["context"] = lineup.context
    return flow


def _drop_none(seat: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in seat.items() if v is not None}


def team_to_flow(team: Dict[str, Any]) -> Dict[str, Any]:
    """The flow a team file is: its lineup drawn as seats, with its name and category."""
    try:
        lineup = parse_lineup(team)
    except TeamValidationError as exc:
        raise ConversionError(str(exc)) from None
    flow = {"id": lineup.id, "name": (lineup.name or lineup.id)[:MAX_NAME_CHARS],
            **({"category": team["category"]} if team.get("category") in TEAM_CATEGORIES else {}),
            **lineup_flow(lineup), "converted_from": {"team": lineup.id}}
    flow["seats"] = [_drop_none(s) for s in flow["seats"]]
    return flow


def template_to_flow(template: Dict[str, Any], teams: TeamManager) -> Dict[str, Any]:
    """The flow a template is: its team's seats in the template's shape, with its parameters, task and stances."""
    try:
        flow_templates.validate_template(template)
    except flow_templates.TemplateValidationError as exc:
        raise ConversionError(str(exc)) from None
    team_id = template.get("team")
    if not team_id:
        raise ConversionError("the template names no team; a flow seats its characters (choose a default team)")
    team = teams.get_team(team_id)
    if team is None:
        raise ConversionError(f"team {team_id!r} does not exist here")
    try:
        lineup = parse_lineup(team)
    except TeamValidationError as exc:
        raise ConversionError(f"team {team_id!r}: {exc}") from None
    kind = flow_templates.template_kind(template)
    steps = None
    if kind == flow_templates.CUSTOM_KIND:
        steps = template["steps"]
        try:
            flow_templates.check_team(steps, lineup)
        except flow_templates.TemplateValidationError as exc:
            raise ConversionError(str(exc)) from None
    elif kind != lineup.flow_kind:
        raise ConversionError(f"team {team_id!r} runs the {lineup.flow_kind} flow, not {kind}")
    if kind == FLOW_VERSUS:
        for member, seat in zip(lineup.sides, flow_templates.STANCE_SEATS):
            member.stance = template.get("stances", {}).get(seat, member.stance)
    flow = {"id": template["id"], "name": template["name"], "category": template["category"],
            "projects": list(template["projects"]), "params": template.get("params", {}), "task": template["task"],
            **lineup_flow(lineup, steps, template.get("max_rounds")),
            "converted_from": {"template": template["id"], "team": team_id}}
    flow["seats"] = [_drop_none(s) for s in flow["seats"]]
    if "reviewed" in template:
        flow["reviewed"] = template["reviewed"]
    return flow


# --- compiling a flow for the engine (G2) ---------------------------------------------------------------------------


# What runs: the shapes the T2 interpreter already has (G2), plus (G3) one summariser in front of the close, the
# close reading condensed or structured input and the lead reading the carried critique structured. Not yet: input
# modes elsewhere, `latest`, history settings, blocked links and seat focus.
LATER_FEATURES = ("input modes other than raw outside the close and the lead, the latest mode, history settings, "
                  "blocked links and seat focus are not built yet")


class Compiled(NamedTuple):
    """A flow compiled for the engine: the team (lineup), the T3a custom steps where the five kinds have no name, and
    what G3 adds around them (`condense`: summariser, close_input, lead_input, envelope; empty when nothing)."""

    team: Dict[str, Any]
    steps: Optional[List[Dict[str, Any]]]
    condense: Dict[str, Any]


def _not_runnable(message: str) -> FlowValidationError:
    return FlowValidationError(f"this flow does not run yet: {message}")


def swap_characters(flow: Dict[str, Any], swaps: Dict[str, str],
                    persona_mgr: Optional[PersonaManager]) -> Dict[str, Any]:
    """A copy of the flow with other characters seated (D7): `swaps` maps a seat id (or "close") to a character id.
    The character must be able to take the seat's job; a summariser has no character to swap."""
    flow = json.loads(json.dumps(flow))
    seats = {s["id"]: s for s in flow.get("seats", [])}
    for seat_id, persona_id in swaps.items():
        if seat_id == CLOSE:
            _check_character(CLOSE, persona_id, ("judge",), persona_mgr)
            flow["close"]["character"] = persona_id
            continue
        seat = seats.get(seat_id)
        if seat is None:
            raise _fail(f"seats.{seat_id}", "no such seat to seat another character in")
        if seat["job"] == "summarise":
            raise _fail(f"seats.{seat_id}", "a summariser has no character")
        _check_character(f"seats.{seat_id}", persona_id, _job_seats(seat), persona_mgr)
        seat["character"] = persona_id
    return flow


def _member_dict(seat: Dict[str, Any]) -> Dict[str, Any]:
    member: Dict[str, Any] = {"persona": seat["character"], "providers": list(seat.get("providers", [])),
                              "seat": seat.get("id", CLOSE)}
    for key in ("code_tools", "host", "stance"):
        if seat.get(key) is not None:
            member[key] = seat[key]
    return member


def compile_flow(flow: Dict[str, Any]) -> Compiled:
    """The team (lineup), the custom steps when the five flow kinds have no name for the shape, and the G3 settings.

    The flow must already be valid (validate_flow). Recognised shapes, with L the seat the rejected exit returns to:
    - versus: one bundle of two rebut seats between the start and the close;
    - pipeline: write -> rewrite ... (2-4 stages) -> [critics] -> close, and L (rewrite) after a rejection only;
    - debate, verify, panel and the custom steps of T3a: L (write; in a bundle with the proposers) -> [critics] ->
      close, with the look-back links of the critics (and the drafts) to L.
    A summariser may stand between the unit that feeds the close and the close (which then reads condensed); it is
    taken out of the shape and recorded in `condense`. Anything else is refused by name; the engine then never runs a
    shape it would read differently."""
    seats = {s["id"]: s for s in flow["seats"]}
    bundles = {b["id"]: b for b in flow.get("bundles") or []}
    close = flow["close"]
    for element in [*seats.values(), close]:
        if element.get("history", "digest") != "digest" or element.get("focus") or element.get("input") == "latest":
            raise _not_runnable(LATER_FEATURES)
    if any(l.get("blocked") for l in flow["links"]):
        raise _not_runnable(LATER_FEATURES)
    owner = {m: b for b, bundle in bundles.items() for m in bundle["seats"]}
    forward = {(l["from"], l["to"]) for l in flow["links"] if "exit" not in l and not l.get("look_back")}
    back = {(l["from"], l["to"]) for l in flow["links"] if l.get("look_back")}
    rejected = next(l for l in flow["links"] if l.get("exit") == "rejected")
    target, rounds = rejected["to"], rejected.get("max_rounds")

    condense: Dict[str, Any] = {}
    summarisers = [s for s in seats.values() if s["job"] == "summarise"]
    if summarisers:
        summariser = summarisers[0]
        sid = summariser["id"]
        into = [(a, b) for (a, b) in forward if b == sid]
        if (len(summarisers) != 1 or [(a, b) for (a, b) in forward if a == sid] != [(sid, CLOSE)] or len(into) != 1
                or any(sid in pair for pair in back) or target == sid):
            raise _not_runnable("a summariser runs in one place for now: between what feeds the close and the close")
        forward = (forward - {into[0], (sid, CLOSE)}) | {(into[0][0], CLOSE)}
        del seats[sid]
        condense["summariser"] = {"seat": sid, "providers": list(summariser.get("providers", [])),
                                  **{k: summariser[k] for k in ("character", "instruction") if summariser.get(k)}}
    if close.get("input", "raw") != "raw":
        condense["close_input"] = close["input"]
    for seat_id, seat in seats.items():
        if seat.get("input", "raw") != "raw" and not (seat_id == target and seat["input"] == "structured"):
            raise _not_runnable("only the close (condensed or structured) and the lead (structured) read anything "
                                "but raw input for now")
    if seats.get(target, {}).get("input") == "structured":
        condense["lead_input"] = "structured"
    if condense.get("close_input") or condense.get("lead_input"):
        condense["envelope"] = True

    def members(unit: str) -> List[Dict[str, Any]]:
        return [seats[m] for m in bundles[unit]["seats"]] if unit in bundles else [seats[unit]]

    def distinct(group: List[Dict[str, Any]], what: str) -> None:
        names = [s["character"] for s in group]
        if len(set(names)) != len(names):
            raise _not_runnable(f"{what} need different characters (their sessions are keyed by character)")

    team: Dict[str, Any] = {"id": flow["id"], "name": flow["name"], "judge": _member_dict(close)}
    if close.get("allowed_tools") is not None:
        team["allowed_tools"] = list(close["allowed_tools"])
    if flow.get("context"):
        team["context"] = flow["context"]
    if rounds is not None:
        team["max_rounds"] = rounds
    jobs = {s["job"] for s in seats.values()}

    if "rebut" in jobs:
        sides = next((b for b in bundles if all(seats[m]["job"] == "rebut" for m in bundles[b]["seats"])), None)
        if (sides is None or len(seats) != 2 or forward != {(START, sides), (sides, CLOSE)} or back
                or target != sides):
            raise _not_runnable("rebut seats run as one bundle of two between the start and the close, and the "
                                "rejected exit returns to that bundle")
        pair = members(sides)
        # the engine needs a lead even where it never runs: side A's character stands in (a versus runs one round)
        stand_in = {k: v for k, v in _member_dict(pair[0]).items() if k not in ("seat", "stance")}
        team.update(expert=stand_in, critic=[],
                    flow={"kind": FLOW_VERSUS, "exchanges": bundles[sides].get("exchanges", 1),
                          "sides": [_member_dict(s) for s in pair]})
        return Compiled(team, None, condense)

    if target in bundles and len(bundles[target]["seats"]) > 1:
        raise _not_runnable("the rejected exit returns to one seat (the lead), not to a whole bundle")
    lead = seats.get(target)
    if lead is None or lead["job"] not in ("write", "rewrite") or (target in owner and lead["job"] != "write"):
        raise _not_runnable("the rejected exit returns to the lead seat (write, or rewrite in a pipeline)")

    def critics_after(unit: str) -> Tuple[List[Dict[str, Any]], str]:
        """The critic seats the unit feeds (none when it feeds the close) and the unit id of the critics."""
        nexts = [b for (a, b) in forward if a == unit]
        if nexts == [CLOSE]:
            return [], CLOSE
        if len(nexts) != 1 or nexts[0] == CLOSE:
            raise _not_runnable(f"{unit} feeds one critique step or the close")
        crit = members(nexts[0])
        if not all(s["job"] == "critique" for s in crit) or len({s.get("mode", "object") for s in crit}) != 1:
            raise _not_runnable("the critics of one step share the job critique and one mode")
        return crit, nexts[0]

    if lead["job"] == "rewrite":   # a pipeline: its stages run from the start, the lead only after a rejection
        stages, unit = [], START
        while True:
            nexts = [b for (a, b) in forward if a == unit and b != "lead" and seats.get(b, {}).get("job") in
                     ("write", "rewrite")]
            if not nexts:
                break
            if len(nexts) != 1 or nexts[0] in bundles or nexts[0] in owner:
                raise _not_runnable("a pipeline's stages run one after the other, not in a bundle")
            unit = nexts[0]
            stages.append(seats[unit])
        if (not 2 <= len(stages) <= 4 or stages[0]["job"] != "write"
                or any(s["job"] != "rewrite" for s in stages[1:])):
            raise _not_runnable("a pipeline has 2-4 stages: one write, then rewrites")
        crit, crit_unit = critics_after(stages[-1]["id"])
        expected = {(START, stages[0]["id"]), *((a["id"], b["id"]) for a, b in zip(stages, stages[1:])),
                    (stages[-1]["id"], crit_unit), (target, crit_unit)}
        if crit:
            expected.add((crit_unit, CLOSE))
        expected_back = {(stages[-1]["id"], target), *({(crit_unit, target)} if crit else set())}
        if (forward != expected or back != expected_back or len(seats) != len(stages) + 1 + len(crit)
                or (crit and crit[0].get("mode", "object") != "object")):
            raise _not_runnable("this pipeline shape is not one the engine runs (stages, the lead after a rejection, "
                                "optional critics with objections)")
        team.update(expert=_member_dict(lead), critic=[_member_dict(s) for s in crit],
                    flow={"kind": FLOW_PIPELINE, "stages": [_member_dict(s) for s in stages]})
        return Compiled(team, None, condense)

    drafts = owner.get(target, target)
    drafters = members(drafts)
    if not all(s["job"] == "write" for s in drafters):
        raise _not_runnable("the drafts bundle holds write seats only")
    proposers = [s for s in drafters if s["id"] != target]
    distinct(drafters, "the lead and its proposers")
    crit, crit_unit = critics_after(drafts)
    distinct(crit, "the critics of one step")
    expected = {(START, drafts), (drafts, crit_unit), *({(crit_unit, CLOSE)} if crit else set())}
    expected_back = ({(crit_unit, target)} | ({(drafts, target)} if proposers else set())) if crit else set()
    if forward != expected or back != expected_back or len(seats) != len(drafters) + len(crit):
        raise _not_runnable("this shape is not one the engine runs yet (the lead with optional proposers, optional "
                            "critics, the close)")
    mode = crit[0].get("mode", "object") if crit else None
    team.update(expert=_member_dict(lead), critic=[_member_dict(s) for s in crit],
                proposers=[_member_dict(s) for s in proposers])
    if not crit and proposers:
        team["flow"] = {"kind": FLOW_PANEL}
        return Compiled(team, None, condense)
    if not crit:
        return Compiled(team, [{"kind": "draft"}], condense)
    if mode == "checklist":
        if proposers:
            return Compiled(team, [{"kind": "blind_draft"}, {"kind": "critique", "mode": "checklist"}], condense)
        team["flow"] = {"kind": FLOW_VERIFY}
    return Compiled(team, None, condense)


def flow_record(flow: Dict[str, Any], params: Dict[str, Any], steps: Optional[List[Dict[str, Any]]],
                condense: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What a thread stores about the flow it started from (Lineup.flow_graph)."""
    record: Dict[str, Any] = {
        "id": flow["id"], "hash": flow_templates.content_hash(flow), "params": params,
        "seats": {**{s["id"]: s.get("character") for s in flow["seats"]}, CLOSE: flow["close"]["character"]},
    }
    if steps:
        record["steps"] = [dict(step) for step in steps]
    if condense:
        record["condense"] = json.loads(json.dumps(condense))
    return record


def resolve_run(flow: Dict[str, Any], values: Dict[str, Any], swaps: Dict[str, str],
                persona_mgr: Optional[PersonaManager], project_kind: Optional[str] = None,
                task: Optional[str] = None) -> Dict[str, Any]:
    """The start of a flow: validated parameter values, the task, the characters swapped in, the compiled team.

    A flow with a task text renders it from the values; a flow without one (a converted team) takes the task the
    person wrote. Stances of rebut seats are rendered the same way. Raises FlowValidationError."""
    data = {k: v for k, v in flow.items() if k != "source"}
    validate_flow(data, persona_mgr)
    projects = data.get("projects")
    if project_kind is not None and projects and project_kind not in projects:
        raise _fail("projects", f"flow {data['id']!r} runs on {', '.join(projects)} projects, not on {project_kind}")
    try:
        checked = flow_templates.check_params(data, values)
    except flow_templates.TemplateValidationError as exc:
        raise FlowValidationError(str(exc)) from None
    if data.get("task"):
        if task:
            raise _fail("task", "the task comes from the flow's own text and parameters")
        task = flow_templates.render(data["task"], checked)
    elif not task:
        raise _fail("task", "this flow has no task text; write the task")
    seated = swap_characters(data, swaps, persona_mgr)
    for seat in seated["seats"]:
        if seat.get("stance"):
            seat["stance"] = flow_templates.render(seat["stance"], checked).strip()
    team, steps, condense = compile_flow(seated)
    return {"id": data["id"], "task": task, "team": team, "steps": steps,
            "record": flow_record(seated, checked, steps, condense), "max_rounds": team.get("max_rounds")}


def parse_seat_args(pairs: Optional[List[str]]) -> Dict[str, str]:
    """`--seat SEAT=CHARACTER` (repeatable) into a dict; a repeated seat is refused."""
    swaps: Dict[str, str] = {}
    for pair in pairs or []:
        seat, sep, persona_id = pair.partition("=")
        if not sep or not seat or not persona_id:
            raise FlowValidationError(f"--seat {pair!r}: expected SEAT=CHARACTER")
        if seat in swaps:
            raise FlowValidationError(f"--seat {seat}: given twice")
        swaps[seat] = persona_id
    return swaps


# --- the store ----------------------------------------------------------------------------------------------------


def builtin_dir() -> Path:
    override = os.environ.get("GUARDIAN_FLOWS")
    return Path(override) if override else Path(__file__).resolve().parent.parent.parent / "configs" / "flows"


def operator_dir() -> Path:
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "flows"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class FlowStore:
    """Loads and saves flow files. Saving validates first, so a stored flow always resolves."""

    def __init__(self, directory: Optional[Path] = None, builtin: Optional[Path] = None,
                 persona_mgr: Optional[PersonaManager] = None):
        self.directory = directory or operator_dir()
        self.builtin_dir = builtin or builtin_dir()
        self.persona_mgr = persona_mgr or PersonaManager()

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_flows(self) -> List[Dict[str, Any]]:
        """Flow files, custom overriding builtin by file name. Each carries "source": builtin|custom."""
        merged: Dict[str, Dict[str, Any]] = {}
        for source, directory in (("builtin", self.builtin_dir), ("custom", self.directory)):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                data = self._read(path)
                if data is not None:
                    merged[path.stem] = {**data, "source": source}
                elif source == "custom":
                    merged.pop(path.stem, None)   # an unreadable custom file still shadows the builtin (see get)
        return list(merged.values())

    def get(self, flow_id: str) -> Optional[Dict[str, Any]]:
        if not TEAM_ID_PATTERN.fullmatch(flow_id or ""):
            return None
        for source, directory in (("custom", self.directory), ("builtin", self.builtin_dir)):
            path = directory / f"{flow_id}.json"
            if path.exists():
                data = self._read(path)
                return None if data is None else {**data, "source": source}
        return None

    def is_custom(self, flow_id: str) -> bool:
        return TEAM_ID_PATTERN.fullmatch(flow_id or "") is not None and (self.directory / f"{flow_id}.json").exists()

    def save(self, data: Any) -> List[str]:
        """Validates and writes a flow to the custom directory. Returns warnings."""
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "source"}
        warnings = validate_flow(data, self.persona_mgr)
        previous = self.get(data["id"]) or {}
        now = _now_iso()
        data = {**data, "created": previous.get("created") or data.get("created") or now, "modified": now}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{data['id']}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return warnings

    def delete(self, flow_id: str) -> bool:
        """Deletes a custom flow. Builtin flows cannot be deleted."""
        if not self.is_custom(flow_id):
            return False
        (self.directory / f"{flow_id}.json").unlink()
        return True


def find(flow_id: str, store: FlowStore, teams: TeamManager,
         templates: flow_templates.FlowTemplateStore) -> Optional[Dict[str, Any]]:
    """The flow an id starts, by the precedence of the report: a stored flow, else a template, else a team, converted.
    Raises FlowValidationError when the template or team exists but does not convert."""
    stored = store.get(flow_id)
    if stored is not None:
        return {k: v for k, v in stored.items() if k != "source"}
    try:
        template = templates.get(flow_id)
        if template is not None:
            return template_to_flow({k: v for k, v in template.items() if k != "source"}, teams)
        team = teams.get_team(flow_id)
        if team is not None:
            return team_to_flow(team)
    except ConversionError as exc:
        raise FlowValidationError(f"{flow_id}: {exc}") from None
    return None


# --- the report for the API ---------------------------------------------------------------------------------------


def _entry(data: Dict[str, Any], source: str, persona_mgr: PersonaManager,
           error: Optional[str] = None) -> Dict[str, Any]:
    raw = {k: v for k, v in data.items() if k != "source"}
    entry: Dict[str, Any] = {"id": data.get("id"), "name": data.get("name", ""), "source": source, "data": raw,
                             "hash": flow_templates.content_hash(raw), "calls": None, "runnable": False,
                             "run_error": None, "errors": [], "warnings": []}
    if error:
        entry["errors"].append(error)
        return entry
    try:
        entry["warnings"] = validate_flow(raw, persona_mgr)
        entry["calls"] = calls_per_round(raw)
    except FlowValidationError as exc:
        entry["errors"].append(str(exc))
        return entry
    try:   # valid, but does the engine run this shape yet? (the start form offers only flows that run)
        compile_flow(raw)
        entry["runnable"] = True
    except FlowValidationError as exc:
        entry["run_error"] = str(exc)
    return entry


def report(store: FlowStore, teams: TeamManager, templates: flow_templates.FlowTemplateStore) -> Dict[str, Any]:
    """Every way to start work, as flows: stored flows, then templates and teams converted (sources "template" and
    "team"). A stored flow shadows a template or team of the same id, a template shadows a team."""
    entries: Dict[str, Dict[str, Any]] = {}
    for team in teams.list_teams():
        team_id = str(team.get("id") or "")
        try:
            entries[team_id] = _entry(team_to_flow(team), "team", store.persona_mgr)
        except ConversionError as exc:
            entries[team_id] = _entry({"id": team_id, "name": team.get("name", team_id)}, "team", store.persona_mgr,
                                      f"team {team_id!r} does not convert: {exc}")
    for template in templates.list_templates():
        template_id = str(template.get("id") or "")
        raw = {k: v for k, v in template.items() if k != "source"}
        shadowed = entries.get(template_id)
        try:
            entries[template_id] = _entry(template_to_flow(raw, teams), "template", store.persona_mgr)
        except ConversionError as exc:
            entries[template_id] = _entry({"id": template_id, "name": template.get("name", template_id)}, "template",
                                          store.persona_mgr, f"template {template_id!r} does not convert: {exc}")
        if shadowed is not None:
            entries[template_id]["warnings"].append(f"shadows the team {template_id!r} of the same id")
    for data in store.list_flows():
        flow_id = str(data.get("id") or "")
        shadowed = entries.get(flow_id)
        entries[flow_id] = _entry(data, data.get("source", "custom"), store.persona_mgr)
        if shadowed is not None:
            entries[flow_id]["warnings"].append(f"shadows the {shadowed['source']} {flow_id!r} of the same id")
    flows = sorted(entries.values(), key=lambda e: str(e["id"]))
    return {"custom_dir": str(store.directory), "flows": flows, "catalog": catalog()}


def catalog() -> Dict[str, Any]:
    return {
        "jobs": list(JOBS),
        "critique_modes": list(CRITIQUE_MODES),
        "input_modes": list(INPUT_MODES),
        "history_modes": list(HISTORY_MODES),
        "limits": {"seats": MAX_SEATS, "bundle_seats": MAX_BUNDLE_SEATS, "calls_per_round": MAX_CALLS_PER_ROUND,
                   "max_rounds": MAX_ROUNDS_LIMIT, "exchanges": MAX_EXCHANGES, "params": flow_templates.MAX_PARAMS,
                   "task_chars": flow_templates.MAX_TASK_CHARS, "stance_chars": MAX_STANCE_CHARS,
                   "focus_chars": MAX_FOCUS_CHARS},
        "categories": list(TEAM_CATEGORIES),
        "project_kinds": list(PROJECT_KINDS),
        "param_types": list(flow_templates.PARAM_TYPES),
    }

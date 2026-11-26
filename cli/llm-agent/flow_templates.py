"""
Flow templates (docs/design/flow-templates.md, phase T1): a stored, parameterised way to start a flow.

A template names the steps of a flow (T1: exactly one step that aliases one of the five team flow kinds), a default
team, the input form of the start screen (`params`), the task text and the stances of a versus flow. Values the person
types go into the task text and the stances only; they never reach a shell. The engine is unchanged: the template
resolves to a task, a team, a round count and stance overrides before the debate starts.

Builtin templates live in configs/flow-templates (GUARDIAN_FLOW_TEMPLATES overrides the directory), the operator's
own in <GUARDIAN_DATA_DIR>/flow-templates. A custom file with a builtin id shadows it (the rule of TeamManager).
Validation lives here and nowhere else: the hub only forwards.
"""

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from command_sets import PARAM_NAME, PLACEHOLDER
from project_registry import KINDS as PROJECT_KINDS
from team_manager import (FLOW_DEBATE, FLOW_KINDS, FLOW_VERIFY, FLOW_VERSUS, MAX_STANCE_CHARS, MAX_ROUNDS_LIMIT,
                          TEAM_CATEGORIES, Lineup)

TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PARAM_TYPES = ("string", "text", "int", "enum")
TEMPLATE_KEYS = {"id", "name", "category", "projects", "team", "steps", "params", "task", "stances", "max_rounds",
                 "reviewed", "created", "modified"}
PARAM_KEYS = {"type", "label", "max", "min", "values", "default"}
STEP_KEYS = {"kind"}
CUSTOM_KIND = "steps"       # what a template of custom steps reports as its kind (the alias form reports the flow kind)
STEP_KINDS = ("draft", "blind_draft", "critique")  # the custom steps of T3a; the rest of the catalog follows (see below)
CRITIQUE_MODES = ("object", "checklist")
STANCE_SEATS = ("side:A", "side:B")
MAX_PARAMS = 8
MAX_STEPS = 2               # one proposal-producing step and an optional critique; the design's 6 needs the in-round
                            # rewrite and consolidate steps (flow-templates.md, T3 notes)
MAX_TASK_CHARS = 4000       # the hub's task limit (debate_handler.go maxTaskChars)
MAX_STRING_CHARS = 400
MAX_TEXT_CHARS = 4000
MAX_LABEL_CHARS = 80
MAX_ENUM_VALUES = 12
INT_MIN, INT_MAX = -10 ** 9, 10 ** 9   # the bounds of an int parameter that states none
# ASCII only (int() also takes "１" and "1_0", the dashboard does not), at most the digits of INT_MAX, so int() never
# meets its 4300-digit limit (a ValueError the callers do not catch)
WHOLE_NUMBER = re.compile(r"[+-]?[0-9]{1,10}")
DEFAULT_MAX_AGE_DAYS = 180
DEFAULT_STEP_LIMITS = {"steps": MAX_STEPS}


class TemplateValidationError(ValueError):
    """A template file or a start request that cannot be used; the message names the field."""


# --- the store ----------------------------------------------------------------------------------------------------


def builtin_dir() -> Path:
    override = os.environ.get("GUARDIAN_FLOW_TEMPLATES")
    return Path(override) if override else Path(__file__).resolve().parent.parent.parent / "configs" / "flow-templates"


def operator_dir() -> Path:
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "flow-templates"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def content_hash(data: Dict[str, Any]) -> str:
    """Hash of the template text without server-stamped fields, so a re-save without changes keeps it."""
    body = {k: v for k, v in data.items() if k not in ("source", "created", "modified")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


class FlowTemplateStore:
    """Loads and saves template files. Saving validates first, so a stored template always resolves."""

    def __init__(self, directory: Optional[Path] = None, builtin: Optional[Path] = None):
        self.directory = directory or operator_dir()
        self.builtin_dir = builtin or builtin_dir()

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_templates(self) -> List[Dict[str, Any]]:
        """Template files, custom overriding builtin by file name. Each carries "source": builtin|custom."""
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

    def get(self, template_id: str) -> Optional[Dict[str, Any]]:
        if not TEMPLATE_ID.fullmatch(template_id or ""):
            return None
        for source, directory in (("custom", self.directory), ("builtin", self.builtin_dir)):
            path = directory / f"{template_id}.json"
            if path.exists():
                data = self._read(path)
                return None if data is None else {**data, "source": source}
        return None

    def is_custom(self, template_id: str) -> bool:
        return TEMPLATE_ID.fullmatch(template_id or "") is not None and (self.directory / f"{template_id}.json").exists()

    def save(self, data: Any) -> List[str]:
        """Validates and writes a template to the custom directory. Returns warnings."""
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "source"}
        warnings = validate_template(data)
        previous = self.get(data["id"]) or {}
        now = _now_iso()
        data = {**data, "created": previous.get("created") or data.get("created") or now, "modified": now}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{data['id']}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return warnings

    def delete(self, template_id: str) -> bool:
        """Deletes a custom template. Builtin templates cannot be deleted."""
        if not self.is_custom(template_id):
            return False
        (self.directory / f"{template_id}.json").unlink()
        return True


# --- validation ---------------------------------------------------------------------------------------------------


def _fail(where: str, message: str) -> TemplateValidationError:
    return TemplateValidationError(f"{where}: {message}")


def _check_param(name: Any, spec: Any) -> None:
    where = f"params.{name}"
    if not isinstance(name, str) or not PARAM_NAME.fullmatch(name):
        raise _fail("params", f"{name!r} is not a parameter name (uppercase letters, digits and _, at most 32)")
    if not isinstance(spec, dict):
        raise _fail(where, "expected an object with a type")
    unknown = sorted(set(spec) - PARAM_KEYS)
    if unknown:
        raise _fail(where, f"unknown key {unknown[0]!r}")
    kind = spec.get("type")
    if kind not in PARAM_TYPES:
        raise _fail(where, f"type must be one of {', '.join(PARAM_TYPES)}")
    label = spec.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS):
        raise _fail(where, f"label must be a short text (at most {MAX_LABEL_CHARS} characters)")
    if kind in ("string", "text"):
        ceiling = MAX_STRING_CHARS if kind == "string" else MAX_TEXT_CHARS
        limit = spec.get("max", ceiling)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= ceiling:
            raise _fail(where, f"max must be a whole number from 1 to {ceiling}")
    if "min" in spec and kind != "int":
        raise _fail(where, "min belongs to int parameters only")
    if "max" in spec and kind == "enum":
        raise _fail(where, "max does not apply to enum parameters (the values bound them)")
    if kind == "int":
        for bound in ("min", "max"):
            if bound in spec and (not isinstance(spec[bound], int) or isinstance(spec[bound], bool)):
                raise _fail(where, f"{bound} must be a whole number")
        if spec.get("min", INT_MIN) > spec.get("max", INT_MAX):
            raise _fail(where, "min is above max")
    if kind == "enum":
        values = spec.get("values")
        if (not isinstance(values, list) or not 1 <= len(values) <= MAX_ENUM_VALUES
                or not all(isinstance(v, str) and v.strip() and len(v) <= MAX_STRING_CHARS for v in values)
                or len(set(values)) != len(values)):
            raise _fail(where, f"values must be 1 to {MAX_ENUM_VALUES} different texts")
    elif "values" in spec:
        raise _fail(where, "values belong to enum parameters only")
    if "default" in spec:
        try:
            _coerce(name, spec, spec["default"])
        except TemplateValidationError as exc:
            raise _fail(where, f"default is not valid: {exc}") from None


def _placeholders(text: str) -> List[str]:
    return PLACEHOLDER.findall(text)


def _check_text_uses(where: str, text: Any, params: Dict[str, Any]) -> None:
    if not isinstance(text, str):
        raise _fail(where, "expected a text")
    for name in _placeholders(text):
        if name not in params:
            raise _fail(where, f"${{{name}}} is not a declared parameter")


def validate_steps(steps: Any) -> str:
    """Returns the kind of a template: a flow kind for the alias form (one step naming a flow kind), or CUSTOM_KIND.

    Custom steps (T3a) are `draft` or `blind_draft`, then an optional `critique` (`mode` object or checklist). Seats
    are implied and always a whole group: the lead (`draft`), the lead with all proposers (`blind_draft`), all critics
    (`critique`). A step that would run without its input (a critique before any proposal) is refused here, so a
    stored template never produces a round the Judge reads wrongly."""
    if not isinstance(steps, list) or not steps:
        raise _fail("steps", f'expected a list of steps, e.g. [{{"kind": "{FLOW_KINDS[0]}"}}] or '
                             f'[{{"kind": "draft"}}, {{"kind": "critique"}}]')
    if len(steps) > MAX_STEPS:
        raise _fail("steps", f"at most {MAX_STEPS} steps for now: one of {', '.join(STEP_KINDS[:2])}, then a critique")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("kind"), str):
            raise _fail(f"steps[{index}]", 'expected {"kind": "<step kind>"}')
    if len(steps) == 1 and steps[0]["kind"] in FLOW_KINDS:
        if set(steps[0]) - STEP_KEYS:
            raise _fail("steps[0]", 'expected {"kind": "<flow kind>"}')
        return steps[0]["kind"]
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if step["kind"] not in STEP_KINDS:
            raise _fail(where, f"unknown step kind {step['kind']!r}; use {', '.join(STEP_KINDS)}, or one step naming "
                               f"a flow kind ({', '.join(FLOW_KINDS)})")
        allowed = STEP_KEYS | ({"mode"} if step["kind"] == "critique" else set())
        extra = sorted(set(step) - allowed)
        if extra:
            raise _fail(f"{where}.{extra[0]}", "not a key of this step (seats are implied: whole groups only)")
        if step["kind"] == "critique" and step.get("mode", "object") not in CRITIQUE_MODES:
            raise _fail(f"{where}.mode", f"use one of {', '.join(CRITIQUE_MODES)}")
    if steps[0]["kind"] == "critique":
        raise _fail("steps[0]", "a critique needs a proposal before it: start with draft or blind_draft")
    if len(steps) == 2 and steps[1]["kind"] != "critique":
        raise _fail("steps[1]", "only a critique may follow the first step (one proposal-producing step for now)")
    return CUSTOM_KIND


def has_critique(steps: List[Dict[str, Any]]) -> bool:
    return any(step["kind"] == "critique" for step in steps)


def check_team(steps: List[Dict[str, Any]], lineup: Lineup) -> None:
    """A custom run needs the seats its steps ask for in the team: raises TemplateValidationError naming the gap."""
    if lineup.flow_kind not in (FLOW_DEBATE, FLOW_VERIFY):
        raise _fail("team", f"custom steps run on a {FLOW_DEBATE} or {FLOW_VERIFY} team (a lead, optional proposers "
                            f"and critics), not on a {lineup.flow_kind} team")
    for step in steps:
        if step["kind"] == "blind_draft" and not lineup.proposers:
            raise _fail("team", f"blind_draft needs proposers; team {lineup.id!r} has none (use draft, or pick "
                                "a team with proposers)")
        if step["kind"] == "critique" and not lineup.critics:
            raise _fail("team", f"critique needs critics; team {lineup.id!r} has none")


def engine_steps(steps: List[Dict[str, Any]], first: bool) -> List[Dict[str, Any]]:
    """The engine's step list of one round for custom steps (debate_engine._steps_of reads it).

    Same shapes the five flow kinds use, so no piece changed: `draft` is the lead alone, `blind_draft` the lead with
    the proposers in the first round of a run (a `consolidate` over those drafts after it), `critique` the critics.
    Without a critique step the drafts are independent answers (the panel shape), so the lead does not read a
    critique. A template without a critique runs one round (the engine clamps `max_rounds`)."""
    critiqued = has_critique(steps)
    out: List[Dict[str, Any]] = []
    for step in steps:
        if step["kind"] == "draft":
            out.append({"kind": "draft", "seat": "expert"})
        elif step["kind"] == "blind_draft":
            out.append({"kind": "blind_draft", "panel": not critiqued} if first else {"kind": "consolidate"})
        else:
            out.append({"kind": "critique", "mode": step.get("mode", "object")})
    return out


def validate_template(data: Any) -> List[str]:
    """Raises TemplateValidationError naming the field; returns warnings (e.g. a stale `reviewed` date)."""
    if not isinstance(data, dict):
        raise TemplateValidationError("a template is a JSON object")
    unknown = sorted(set(data) - TEMPLATE_KEYS)
    if unknown:
        raise _fail(unknown[0], "unknown key")
    template_id = data.get("id")
    if not isinstance(template_id, str) or not TEMPLATE_ID.fullmatch(template_id):
        raise _fail("id", "use 1-40 lowercase letters, digits and hyphens")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_LABEL_CHARS:
        raise _fail("name", f"a short name (at most {MAX_LABEL_CHARS} characters)")
    if data.get("category") not in TEAM_CATEGORIES:
        raise _fail("category", f"use one of {', '.join(TEAM_CATEGORIES)}")
    projects = data.get("projects")
    if (not isinstance(projects, list) or not projects or not all(isinstance(p, str) for p in projects)
            or len(set(projects)) != len(projects) or not all(p in PROJECT_KINDS for p in projects)):
        raise _fail("projects", f"a list of project kinds from {', '.join(PROJECT_KINDS)}")
    team = data.get("team")
    if team is not None and (not isinstance(team, str) or not team):
        raise _fail("team", "a team id")
    kind = validate_steps(data.get("steps"))
    if kind == CUSTOM_KIND and projects != ["code"]:
        raise _fail("projects", "custom steps run on code projects only for now (ops and design projects keep the "
                                "debate and verify flows)")
    params = data.get("params", {})
    if not isinstance(params, dict) or len(params) > MAX_PARAMS:
        raise _fail("params", f"an object with at most {MAX_PARAMS} parameters")
    for pname, spec in params.items():
        _check_param(pname, spec)
    task = data.get("task")
    _check_text_uses("task", task, params)
    if not str(task).strip():
        raise _fail("task", "expected the task text the debate starts with")
    stances = data.get("stances", {})
    if not isinstance(stances, dict) or set(stances) - set(STANCE_SEATS):
        raise _fail("stances", f"keys must be among {', '.join(STANCE_SEATS)}")
    if stances and kind != FLOW_VERSUS:
        raise _fail("stances", f"only a {FLOW_VERSUS} flow has sides")
    if kind == FLOW_VERSUS and set(stances) != set(STANCE_SEATS):
        raise _fail("stances", f"a {FLOW_VERSUS} template states both {' and '.join(STANCE_SEATS)}")
    for seat, text in stances.items():
        _check_text_uses(f"stances.{seat}", text, params)
        if not text.strip():
            raise _fail(f"stances.{seat}", "expected the position this side defends")
        longest = len(PLACEHOLDER.sub(lambda m: "x" * _max_len(params[m.group(1)]), text))
        if longest > MAX_STANCE_CHARS:
            raise _fail(f"stances.{seat}", f"can reach {longest} characters after filling in; at most {MAX_STANCE_CHARS}")
    longest_task = len(PLACEHOLDER.sub(lambda m: "x" * _max_len(params[m.group(1)]), task))
    if longest_task > MAX_TASK_CHARS:
        raise _fail("task", f"can reach {longest_task} characters after filling in; at most {MAX_TASK_CHARS}")
    rounds = data.get("max_rounds")
    if rounds is not None and (not isinstance(rounds, int) or isinstance(rounds, bool)
                               or not 1 <= rounds <= MAX_ROUNDS_LIMIT):
        raise _fail("max_rounds", f"a whole number from 1 to {MAX_ROUNDS_LIMIT}")
    warnings: List[str] = []
    reviewed = data.get("reviewed")
    if reviewed is not None:
        try:
            age = (date.today() - date.fromisoformat(reviewed)).days
        except (TypeError, ValueError):
            raise _fail("reviewed", "use a date like 2026-10-29") from None
        if age > max_age_days():
            warnings.append(f"reviewed {age} days ago (more than {max_age_days()}); check it again")
    used = {n for text in [task, *stances.values()] for n in _placeholders(text)}
    for pname in params:
        if pname not in used:
            warnings.append(f"params.{pname} is declared but neither the task nor a stance uses it")
    return warnings


def max_age_days() -> int:
    try:
        return max(1, int(os.environ.get("GUARDIAN_FLOW_TEMPLATE_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def template_kind(data: Dict[str, Any]) -> str:
    return validate_steps(data.get("steps"))


# --- parameters and rendering -------------------------------------------------------------------------------------


def _max_len(spec: Dict[str, Any]) -> int:
    """The longest text a parameter can render to (an int or enum is bounded by its own shape)."""
    kind = spec.get("type")
    if kind == "enum":
        return max(len(v) for v in spec["values"])
    if kind == "int":
        return max(len(str(spec.get("min", INT_MIN))), len(str(spec.get("max", INT_MAX))))
    return int(spec.get("max", MAX_STRING_CHARS if kind == "string" else MAX_TEXT_CHARS))


def _coerce(name: str, spec: Dict[str, Any], raw: Any) -> Any:
    kind = spec["type"]
    if kind == "int":
        if isinstance(raw, int) and not isinstance(raw, bool):
            value = raw
        elif isinstance(raw, str) and WHOLE_NUMBER.fullmatch(raw.strip()):
            value = int(raw.strip())
        else:   # a float is refused, not truncated (2.9 is not 2; inf and nan are not numbers here)
            raise _fail(name, "expected a whole number")
        if value < spec.get("min", INT_MIN) or value > spec.get("max", INT_MAX):
            raise _fail(name, f"outside {spec.get('min', '-')}..{spec.get('max', '-')}")
        return value
    if not isinstance(raw, str):
        raise _fail(name, "expected a text")
    if kind == "enum":
        if raw not in spec["values"]:
            raise _fail(name, f"use one of {', '.join(spec['values'])}")
        return raw
    text = raw.strip()
    if not text:
        raise _fail(name, "must not be empty")
    if len(text) > spec.get("max", MAX_STRING_CHARS if kind == "string" else MAX_TEXT_CHARS):
        raise _fail(name, f"at most {_max_len(spec)} characters")
    if kind == "string" and ("\n" in text or "\r" in text):
        raise _fail(name, "one line only")
    return text


def check_params(data: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Validated values by name: declared parameters only, defaults filled in, a missing required one refused."""
    specs = data.get("params") or {}
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise _fail(unknown[0], "not a parameter of this template")
    result: Dict[str, Any] = {}
    for name, spec in specs.items():
        if name in values:
            result[name] = _coerce(name, spec, values[name])
        elif "default" in spec:
            result[name] = _coerce(name, spec, spec["default"])
        else:
            raise _fail(name, "required")
    return result


def render(text: str, values: Dict[str, Any]) -> str:
    """One pass: a value that itself contains `${X}` is not expanded again."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)


class TemplateRun(Dict[str, Any]):
    """The resolved start of a template: what the CLI hands to the engine. A plain dict so it serialises as is."""


def resolve(data: Dict[str, Any], values: Dict[str, Any], team: Optional[str] = None,
            project_kind: Optional[str] = None) -> Tuple[TemplateRun, List[str]]:
    """Validates the template and the values and returns (run, warnings).

    run: id, hash, kind, team, task, max_rounds, stances ({"A": ..., "B": ...} for versus), params (the values).
    A template that does not list the project kind is refused here, with the list, never ignored.
    """
    warnings = validate_template({k: v for k, v in data.items() if k != "source"})
    if project_kind is not None and project_kind not in data["projects"]:
        raise _fail("projects", f"template {data['id']!r} runs on {', '.join(data['projects'])} projects, "
                                f"not on {project_kind} projects")
    checked = check_params(data, values)
    chosen_team = team or data.get("team")
    if not chosen_team:
        raise _fail("team", "this template names no team; choose one")
    run = TemplateRun(
        id=data["id"], hash=content_hash(data), kind=template_kind(data), team=chosen_team,
        projects=list(data["projects"]),
        task=render(data["task"], checked), max_rounds=data.get("max_rounds"),
        stances={seat.split(":")[1]: render(text, checked).strip() for seat, text in (data.get("stances") or {}).items()},
        params=checked)
    if run["kind"] == CUSTOM_KIND:
        run["steps"] = [dict(step) for step in data["steps"]]
    return run, warnings


def apply_to_lineup(run: Dict[str, Any], lineup: Lineup, project_kind: Optional[str] = None) -> None:
    """Checks project kind and resolved team against the template, sets the stances. Raises TemplateValidationError."""
    if project_kind is not None and project_kind not in run["projects"]:
        raise _fail("projects", f"template {run['id']!r} runs on {', '.join(run['projects'])} projects, "
                                f"not on {project_kind} projects")
    if run["kind"] == CUSTOM_KIND:
        check_team(run["steps"], lineup)
        lineup.template = {"id": run["id"], "hash": run["hash"], "kind": CUSTOM_KIND, "params": run["params"],
                           "steps": [dict(step) for step in run["steps"]]}
        return
    if lineup.flow_kind != run["kind"]:
        raise _fail("team", f"template {run['id']!r} is a {run['kind']} flow but team {lineup.id!r} runs "
                            f"{lineup.flow_kind}; pick a {run['kind']} team")
    for index, seat in enumerate(("A", "B")):
        if seat in run["stances"]:
            lineup.sides[index].stance = run["stances"][seat]
    lineup.template = {"id": run["id"], "hash": run["hash"], "kind": run["kind"], "params": run["params"]}
    if run["stances"]:
        lineup.template["stances"] = dict(run["stances"])


def restore(stored: Dict[str, Any], lineup: Lineup) -> None:
    """Puts the template record of a thread snapshot back on the lineup of a run that continues the thread.

    A thread keeps the steps it started with: the team may have been edited or replaced since, and a plain continue
    loads the lineup without the template. Custom steps are checked against the lineup again, so a team that lost its
    critics refuses the continuation instead of running a different flow. Raises TemplateValidationError."""
    if not isinstance(stored, dict):
        return
    steps = stored.get("steps")
    if steps is not None:
        if validate_steps(steps) != CUSTOM_KIND:
            raise _fail("steps", "the thread's stored steps are not custom steps")
        check_team(steps, lineup)
    lineup.template = dict(stored)


def parse_param_args(pairs: Optional[List[str]]) -> Dict[str, str]:
    """`--param KEY=VALUE` (repeatable) into a dict; a repeated key is refused, the value may hold '='."""
    values: Dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not PARAM_NAME.fullmatch(key):
            raise TemplateValidationError(f"--param {pair!r}: expected KEY=VALUE with an uppercase KEY")
        if key in values:
            raise TemplateValidationError(f"--param {key}: given twice")
        values[key] = value
    return values


# --- the report for the API ---------------------------------------------------------------------------------------


def catalog() -> Dict[str, Any]:
    return {
        "step_kinds": list(FLOW_KINDS),
        "custom_step_kinds": list(STEP_KINDS),
        "critique_modes": list(CRITIQUE_MODES),
        "limits": {**DEFAULT_STEP_LIMITS, "params": MAX_PARAMS, "task_chars": MAX_TASK_CHARS,
                   "stance_chars": MAX_STANCE_CHARS, "max_rounds": MAX_ROUNDS_LIMIT},
        "categories": list(TEAM_CATEGORIES),
        "project_kinds": list(PROJECT_KINDS),
        "param_types": list(PARAM_TYPES),
    }

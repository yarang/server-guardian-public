"""
JSON commands for editing personas and teams, called by the hub API (apps/agent) and the CLI.

Each command returns (exit_code, payload). Exit 0 is success, exit 2 a validation error whose
payload is {"ok": false, "error": "..."}; the hub maps exit 2 to HTTP 400 and exit 3 (not found, e.g. deleting an
unknown id) to 404. Deleting a builtin persona, team or project stays exit 2.
See docs/design/debate-teams-api.md for the contract.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from llm_runner import DEFAULT_PROVIDER_ORDER
from mcp_client import TOOLS
from persona_manager import PersonaManager, PersonaValidationError
from remote_agents import LABELS, worker_choices
from team_manager import (
    MAX_CRITICS,
    MAX_PROPOSERS,
    MAX_ROUNDS_LIMIT,
    TEAM_CATEGORIES,
    TeamManager,
    TeamValidationError,
    parse_lineup,
    validate_lineup,
    validate_team_meta,
)

INVALID = 2
NOT_FOUND = 3
CONFLICT = 4
MAX_INPUT_BYTES = 64 * 1024

Result = Tuple[int, Dict[str, Any]]


def _invalid(message: str) -> Result:
    return INVALID, {"ok": False, "error": message}


def _parse_input(text: str) -> Tuple[Optional[Any], Optional[str]]:
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        return None, f"input larger than {MAX_INPUT_BYTES} bytes"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def personas_report(manager: PersonaManager) -> Result:
    personas = sorted(manager.list_personas(), key=lambda p: p.get("id", ""))
    return 0, {"personas": personas, "custom_dir": str(manager.custom_dir)}


def save_persona(manager: PersonaManager, text: str) -> Result:
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        path = manager.save_persona(data)
    except PersonaValidationError as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": path.stem, "warnings": []}


def _not_deleted(kind: str, item_id: str, exists: bool) -> Result:
    """Why a delete removed nothing: an unknown id is NOT_FOUND, a builtin one is invalid input."""
    if not exists:
        return NOT_FOUND, {"ok": False, "error": f"no {kind} {item_id!r}"}
    return _invalid(f"{item_id!r} is not a custom {kind} (builtin {kind}s cannot be deleted)")


def delete_persona(manager: PersonaManager, persona_id: str) -> Result:
    if not manager.delete_persona(persona_id):
        return _not_deleted("persona", persona_id, manager.get_persona(persona_id) is not None)
    return 0, {"ok": True, "id": persona_id}


def _team_entry(data: Dict[str, Any], manager: TeamManager, installed: List[str]) -> Dict[str, Any]:
    raw = {k: v for k, v in data.items() if k != "source"}
    entry: Dict[str, Any] = {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "source": data.get("source", "custom"),
        "data": raw,
        "lineup": None,
        "errors": [],
        "warnings": [],
    }
    try:
        lineup = parse_lineup(raw)
        entry["warnings"] = validate_lineup(lineup, manager.persona_mgr)
        try:
            validate_team_meta(raw)
        except TeamValidationError as exc:  # a list-only field never makes the team unusable
            entry["warnings"].append(str(exc))
        entry["lineup"] = lineup.snapshot()
        for member in (lineup.expert, *lineup.proposers, *lineup.stages, *lineup.sides, *lineup.critics, lineup.judge):
            if member.providers and not any(p in installed for p in member.providers):
                entry["warnings"].append(f"{member.persona}: none of {member.providers} is installed here")
    except TeamValidationError as exc:
        entry["errors"].append(str(exc))
    return entry


def teams_report(manager: TeamManager, installed: List[str], workers: Optional[List[str]] = None) -> Result:
    """`workers` is the list of enabled worker ids for the editor's host choice (read from the inventory when None)."""
    teams = [_team_entry(data, manager, installed) for data in manager.list_teams()]
    teams.sort(key=lambda t: str(t["id"]))
    catalog = {
        "providers": list(DEFAULT_PROVIDER_ORDER),
        "installed": list(installed),
        "tools": [{"name": t["name"], "risk": t["risk"], "description": t["description"]} for t in TOOLS],
        "max_critics": MAX_CRITICS,
        "max_proposers": MAX_PROPOSERS,
        "max_rounds": MAX_ROUNDS_LIMIT,
        "categories": list(TEAM_CATEGORIES),
        "host_labels": list(LABELS),
        "workers": worker_choices() if workers is None else list(workers),
    }
    return 0, {"custom_dir": str(manager.directory), "teams": teams, "catalog": catalog}


def save_team(manager: TeamManager, text: str) -> Result:
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        warnings = manager.save(data)
    except TeamValidationError as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": data["id"], "warnings": warnings}


def delete_team(manager: TeamManager, team_id: str) -> Result:
    if not manager.delete(team_id):
        return _not_deleted("team", team_id, manager.get_team(team_id) is not None)
    return 0, {"ok": True, "id": team_id}


# --- command sets (docs/design/batch-execution.md, B4c) -----------------------------------


def save_command_set(text: str) -> Result:
    import command_sets

    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        stored = command_sets.save_set(data)
    except (ValueError, OSError) as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": stored["id"], "reviewed": stored["reviewed"], "warnings": []}


def delete_command_set(set_id: str) -> Result:
    import command_sets

    if command_sets.delete_set(set_id):
        return 0, {"ok": True, "id": set_id}
    return _not_deleted("command set", set_id, command_sets.is_builtin(set_id))


def confirm_command_set(set_id: str) -> Result:
    import command_sets

    try:
        command_sets.confirm_set(set_id)
    except ValueError as exc:
        return (_invalid(str(exc)) if command_sets.is_builtin(set_id)
                else (3, {"ok": False, "error": f"unknown command set {set_id!r}"}))
    except OSError as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": set_id}


# --- projects -----------------------------------------------------------------------------


def _project_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A project file with its validation errors and workspace state."""
    from project_registry import ProjectValidationError, parse_project, workspace_status

    data = {k: v for k, v in raw.items() if k != "source"}
    entry: Dict[str, Any] = {"id": raw.get("id"), "source": raw.get("source"), "data": data, "errors": []}
    try:
        project = parse_project(data)
        entry["status"] = workspace_status(project)
        if project.is_ops:
            entry["targets"] = _targets_summary(project)
    except ProjectValidationError as exc:
        entry["errors"].append(str(exc))
    return entry


def _targets_summary(project: Any) -> Dict[str, Any]:
    """Resolved hosts of an ops project for the project list (ids, unknown names, why it could not resolve)."""
    import ops_targets

    resolved = ops_targets.resolve(project)
    return {k: resolved[k] for k in ("ids", "hub", "include_hub", "missing", "error", "groups_unknown")}


def projects_report(manager: Any) -> Result:
    projects = [_project_entry(p) for p in sorted(manager.list_projects(), key=lambda p: str(p.get("id", "")))]
    return 0, {"projects": projects, "custom_dir": str(manager.directory)}


def save_project(manager: Any, text: str) -> Result:
    from project_registry import ProjectValidationError

    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        project = manager.save(data)
    except ProjectValidationError as exc:
        return _invalid(str(exc))
    warnings = []
    if project.is_ops:
        summary = _targets_summary(project)
        if summary["error"]:
            warnings.append(f"targets not checked: {summary['error']}")
        elif summary["groups_unknown"]:
            warnings.append("node groups not checked: this host has no hub database")
        elif not summary["ids"]:
            warnings.append("the targets match no node now; actions can run only once they do")
    return 0, {"ok": True, "id": project.id, "warnings": warnings}


def delete_project(manager: Any, project_id: str) -> Result:
    if not manager.delete(project_id):
        return _not_deleted("project", project_id, manager.get_raw(project_id) is not None)
    return 0, {"ok": True, "id": project_id, "note": "the workspace clone was left on disk"}


def sync_projects(manager: Any, project_id: str) -> Result:
    """Clones or fast-forwards one project's workspace, or every project's with 'all'.

    'all' reports each project and fails (exit 2) only when every sync failed."""
    from project_registry import ProjectValidationError, WorkspaceBusy

    from project_registry import NO_WORKSPACE_KINDS, NoWorkspace

    # 'all' leaves ops and design projects out: they have no repository to sync.
    ids = ([str(p.get("id")) for p in manager.list_projects() if p.get("kind") not in NO_WORKSPACE_KINDS]
           if project_id == "all" else [project_id])
    results, failures, busy = [], 0, False
    for pid in ids:
        try:
            results.append({"id": pid, "ok": True, **manager.sync(manager.load(pid))})
        except (ProjectValidationError, OSError) as exc:
            failures += 1
            busy = isinstance(exc, (WorkspaceBusy, NoWorkspace))
            results.append({"id": pid, "ok": False, "error": str(exc)})
    if project_id != "all" and failures:
        # a publish holds the workspace, or the project has none: a state conflict, not bad input
        if busy:
            return CONFLICT, {"ok": False, "error": results[0]["error"]}
        return _invalid(results[0]["error"])
    code = INVALID if ids and failures == len(ids) else 0
    return code, {"ok": code == 0, "results": results}


# --- node groups --------------------------------------------------------------------------


def groups_report(manager: Any) -> Result:
    return 0, manager.report()


def save_group(manager: Any, text: str) -> Result:
    from group_manager import GroupValidationError

    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        return 0, manager.save(data)
    except GroupValidationError as exc:
        return _invalid(str(exc))


def preview_group(manager: Any, text: str) -> Result:
    from group_manager import GroupValidationError

    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        return 0, manager.preview(data)
    except GroupValidationError as exc:
        return _invalid(str(exc))


def delete_group(manager: Any, group_id: str) -> Result:
    from group_manager import GroupNotFoundError

    try:
        manager.delete(group_id)
    except GroupNotFoundError as exc:
        return NOT_FOUND, {"ok": False, "error": str(exc)}
    return 0, {"ok": True, "id": group_id}

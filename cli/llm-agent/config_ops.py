"""
JSON commands for editing personas and teams, called by the hub API (apps/agent) and the CLI.

Each command returns (exit_code, payload). Exit 0 is success, exit 2 a validation error whose
payload is {"ok": false, "error": "..."}; the hub maps exit 2 to HTTP 400 and exit 3 (not found, e.g. deleting an
unknown id) to 404. Deleting a builtin persona, team or project stays exit 2.
See docs/design/debate-teams-api.md for the contract.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import character
import flow_drafts
import flow_graph
import flow_templates
import persona_lint
from llm_runner import KNOWN_PROVIDERS
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
    # A character's text as the engine sends it, for display only: never stored. save_persona drops it (and the
    # list's "source") when a client sends a listed persona back.
    for persona in personas:
        if character.is_character(persona):
            persona["rendered"] = character.body_text(persona)
    return 0, {"personas": personas, "custom_dir": str(manager.custom_dir)}


def save_persona(manager: PersonaManager, text: str) -> Result:
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        path = manager.save_persona(data)
    except PersonaValidationError as exc:
        return _invalid(str(exc))
    # The form guide's warn findings (docs/design/agent-characters.md, section 3): shown, never blocking. For a
    # character (format 2) they come from the rendered block; a format 1 persona keeps its system_prompt messages.
    return 0, {"ok": True, "id": path.stem, "warnings": persona_lint.persona_warnings(data)}


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
        "providers": list(KNOWN_PROVIDERS),
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


def _template_entry(data: Dict[str, Any], teams: TeamManager) -> Dict[str, Any]:
    raw = {k: v for k, v in data.items() if k != "source"}
    entry: Dict[str, Any] = {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "source": data.get("source", "custom"),
        "data": raw,
        "resolved": None,
        "errors": [],
        "warnings": [],
    }
    try:
        entry["warnings"] = flow_templates.validate_template(raw)
        kind = flow_templates.template_kind(raw)
        entry["resolved"] = {"kind": kind, "hash": flow_templates.content_hash(raw), "team": raw.get("team")}
        team = raw.get("team")
        if team:
            team_data = teams.get_team(team)
            if team_data is None:
                entry["warnings"].append(f"team {team!r} does not exist here; choose another at start")
            else:
                try:
                    team_kind = parse_lineup(team_data).flow_kind
                except TeamValidationError:
                    team_kind = None
                if kind == flow_templates.CUSTOM_KIND:
                    try:
                        flow_templates.check_team(raw["steps"], parse_lineup(team_data))
                    except (flow_templates.TemplateValidationError, TeamValidationError) as exc:
                        entry["warnings"].append(f"team {team!r}: {exc}")
                elif team_kind is not None and team_kind != kind:
                    entry["warnings"].append(f"team {team!r} runs the {team_kind} flow, not {kind}")
    except flow_templates.TemplateValidationError as exc:
        entry["errors"].append(str(exc))
    return entry


def flow_templates_report(store: flow_templates.FlowTemplateStore, teams: TeamManager) -> Result:
    templates = [_template_entry(data, teams) for data in store.list_templates()]
    templates.sort(key=lambda t: str(t["id"]))
    return 0, {"custom_dir": str(store.directory), "templates": templates, "catalog": flow_templates.catalog()}


def save_flow_template(store: flow_templates.FlowTemplateStore, text: str) -> Result:
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        warnings = store.save(data)
    except flow_templates.TemplateValidationError as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": data["id"], "warnings": warnings}


def delete_flow_template(store: flow_templates.FlowTemplateStore, template_id: str) -> Result:
    if not store.delete(template_id):
        return _not_deleted("flow template", template_id, store.get(template_id) is not None)
    return 0, {"ok": True, "id": template_id}


def flows_report(store: flow_graph.FlowStore, teams: TeamManager,
                 templates: flow_templates.FlowTemplateStore) -> Result:
    return 0, flow_graph.report(store, teams, templates)


def save_flow(store: flow_graph.FlowStore, text: str) -> Result:
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        warnings = store.save(data)
    except flow_graph.FlowValidationError as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": data["id"], "warnings": warnings}


def delete_flow(store: flow_graph.FlowStore, flow_id: str) -> Result:
    if not store.delete(flow_id):
        return _not_deleted("flow", flow_id, store.get(flow_id) is not None)
    return 0, {"ok": True, "id": flow_id}


# --- session flow drafts (docs/design/session-flows.md, SF1) --------------------------------------------------------


def check_flow_draft(text: str, persona_mgr: PersonaManager) -> Result:
    """Converts and validates a draft without storing it; an invalid draft is INVALID with its error."""
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    result = flow_drafts.check(data, persona_mgr)
    if not result.get("ok"):
        return _invalid(result.get("error", "invalid draft"))
    return 0, result


def save_flow_draft(text: str, ws: str, persona_mgr: PersonaManager) -> Result:
    data, error = _parse_input(text)
    if error:
        return _invalid(error)
    try:
        record = flow_drafts.save_draft(ws, data, persona_mgr)
    except flow_drafts.DraftError as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "id": record["draft"]["id"], "workstation": ws, "warnings": record["warnings"],
               "runnable": record["runnable"]}


def list_flow_drafts(ws: Optional[str] = None) -> Result:
    try:
        drafts = flow_drafts.list_drafts(ws)
    except flow_drafts.DraftError as exc:
        return _invalid(str(exc))
    return 0, {"drafts": drafts, "drafts_dir": str(flow_drafts.drafts_dir())}


def promote_flow_draft(ws: str, draft_id: str, store: flow_graph.FlowStore, persona_mgr: PersonaManager,
                       as_id: Optional[str] = None, overwrite: bool = False) -> Result:
    try:
        if flow_drafts.get_draft(ws, draft_id) is None:
            return NOT_FOUND, {"ok": False, "error": f"no draft {draft_id!r} for workstation {ws!r}"}
        payload = flow_drafts.promote(ws, draft_id, store, persona_mgr, as_id=as_id, overwrite=overwrite)
    except flow_drafts.DraftError as exc:
        return _invalid(str(exc))
    return 0, payload


def delete_flow_draft(ws: str, draft_id: str) -> Result:
    try:
        removed = flow_drafts.delete_draft(ws, draft_id)
    except flow_drafts.DraftError as exc:
        return _invalid(str(exc))
    if not removed:
        return NOT_FOUND, {"ok": False, "error": f"no draft {draft_id!r} for workstation {ws!r}"}
    return 0, {"ok": True, "id": draft_id, "workstation": ws}

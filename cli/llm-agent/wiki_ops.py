"""
JSON commands for the code wiki, called by guardian.py (and, from P4, by the hub API and the dashboard).

Each command returns (exit_code, payload) like config_ops: 0 success, 2 invalid input or an unusable project/commit
(payload {"ok": false, "error": "..."}), 3 not found (an unknown page), 4 conflict (a build is running). Pages live in
<GUARDIAN_DATA_DIR>/code-wiki/<project>/pages; states are computed against a commit of the project's workspace
(default: its HEAD), see code_wiki.page_state. The build command is wiki_build.WikiBuilder.
"""

import json
from typing import Any, Dict, Optional, Tuple

from code_context import ContextError, head_sha, resolve_sha
from code_index import default_cache_dir
from code_wiki import (
    MAX_BODY_BYTES,
    PageNotFound,
    WikiError,
    WikiStore,
    WikiView,
    default_wiki_dir,
    generated_layer,
    save_human_page,
)
from repo_tools import RepoTools

INVALID = 2
NOT_FOUND = 3
MAX_INPUT_BYTES = MAX_BODY_BYTES + 64 * 1024

Result = Tuple[int, Dict[str, Any]]


def _invalid(message: str) -> Result:
    return INVALID, {"ok": False, "error": message}


class OpenError(Exception):
    """The project or commit cannot be opened (exit 2)."""


def open_store(project_id: Optional[str]) -> Tuple[Any, WikiStore]:
    """(project, store) for a project id (None: the default project)."""
    from project_registry import ProjectManager, ProjectValidationError

    try:
        project = ProjectManager().load(project_id)
    except ProjectValidationError as exc:
        raise OpenError(f"project: {exc}") from exc
    if not project.has_workspace:
        raise OpenError(f"project {project.id} is {project.kind_label}: it has no repository, files or code wiki")
    directory = default_wiki_dir(project.id)
    if directory is None:
        raise OpenError("project id is not usable for the wiki directory")
    return project, WikiStore(directory)


def open_tools(project: Any, sha: Optional[str]) -> RepoTools:
    """RepoTools of the project's workspace at sha (default HEAD)."""
    workspace = project.workspace
    try:
        resolved = resolve_sha(workspace, sha) if sha else head_sha(workspace)
    except ContextError as exc:
        raise OpenError(str(exc)) from exc
    if not resolved:
        raise OpenError(f"{workspace} is not a git repository with a commit")
    try:
        return RepoTools(workspace, resolved, cache_dir=default_cache_dir(project.id),
                         wiki_dir=default_wiki_dir(project.id))
    except (ContextError, OSError) as exc:
        raise OpenError(f"cannot read {workspace} at {resolved[:7]}: {exc}") from exc


def list_pages(project_id: Optional[str], sha: Optional[str] = None) -> Result:
    """{"ok", "project", "sha", "pages": [{id, title, scope, author, updated, state, kind, summary, links}], "errors": [{id, error}]}"""
    try:
        project, store = open_store(project_id)
        tools = open_tools(project, sha)
    except OpenError as exc:
        return _invalid(str(exc))
    view = WikiView(store, tools)
    return 0, {"ok": True, "project": project.id, "sha": tools.sha, "pages": view.listing(),
               "errors": view.load_errors}


def show_page(project_id: Optional[str], page_id: str, sha: Optional[str] = None) -> Result:
    """{"ok", "project", "sha", "page": {id, title, scope, author, updated, written_sha, prompt_version, basis,
    unverified}, "narrative", "state", "staleness": {state, sha, written_sha, changed, removed, new, broken,
    unverified, notes}, "generated"}. The generated layer is the scope's repository map at sha, made now."""
    try:
        project, store = open_store(project_id)
        tools = open_tools(project, sha)
        page = store.load(page_id)
    except OpenError as exc:
        return _invalid(str(exc))
    except PageNotFound as exc:
        return NOT_FOUND, {"ok": False, "error": str(exc)}
    except WikiError as exc:
        return _invalid(str(exc))
    details = WikiView(store, tools).details(page)
    return 0, {"ok": True, "project": project.id, "sha": tools.sha, "page": page.meta(), "narrative": page.body,
               "state": details["state"], "staleness": details, "generated": generated_layer(page, tools)}


def save_page(project_id: Optional[str], text: str, user: str, sha: Optional[str] = None) -> Result:
    """Saves a person's edit. text is JSON {id, title, scope, body}; author, written_sha and basis are written by the
    server. Answers {"ok", "project", "sha", "page", "state", "staleness"}."""
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        return _invalid(f"input larger than {MAX_INPUT_BYTES} bytes")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return _invalid(f"invalid JSON: {exc}")
    try:
        project, store = open_store(project_id)
        tools = open_tools(project, sha)
        page = save_human_page(store, tools, payload, user)
    except OpenError as exc:
        return _invalid(str(exc))
    except WikiError as exc:
        return _invalid(str(exc))
    details = WikiView(store, tools).details(page)
    return 0, {"ok": True, "project": project.id, "sha": tools.sha, "page": page.meta(), "state": details["state"],
               "staleness": details}


def delete_page(project_id: Optional[str], page_id: str) -> Result:
    try:
        project, store = open_store(project_id)
        deleted = store.delete(page_id)
    except OpenError as exc:
        return _invalid(str(exc))
    except WikiError as exc:
        return _invalid(str(exc))
    if not deleted:
        return NOT_FOUND, {"ok": False, "error": f"no wiki page {page_id!r}"}
    return 0, {"ok": True, "project": project.id, "deleted": page_id}

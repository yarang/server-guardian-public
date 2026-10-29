"""
Target hosts of an ops project (docs/design/server-ops-project.md, 3.3 and 3.4).

An ops project names node groups, single nodes and exclusions (or every node). The set is resolved
against the current servers.json and node groups each time it is needed: when a debate starts (the
agents get that list) and again when an action is validated or run, so a host that left the group
since the debate cannot be touched.

The hub (guardian_role "hub") is never a target unless the project's policy sets include_hub. When it
is, the hub is addressed as target_host "local" (the agent's own sandbox), never over fleet SSH.
"""

from typing import Any, Dict, List, Optional, Sequence

LOCAL_TARGETS = ("local", "localhost")


class TargetScopeError(ValueError):
    """A target host outside the project's resolved targets; the message is shown as is."""


def hub_id(servers: Sequence[Dict[str, Any]]) -> Optional[str]:
    return next((str(s["id"]) for s in servers if s.get("guardian_role") == "hub"), None)


def _load(servers: Optional[Sequence[Dict[str, Any]]], groups: Optional[Sequence[Dict[str, Any]]]):
    """(servers, groups, error, groups_known): servers.json and the node groups with resolved members, unless the
    caller passes them. groups_known is False where there is no hub database to read the groups from."""
    error = None
    known = True
    if servers is None:
        from group_manager import load_servers

        servers, error, _ = load_servers()
    if groups is None:
        from db_manager import default_db_path
        from group_manager import GroupManager

        # Only the hub has the database; elsewhere GroupManager would create an empty one.
        known = default_db_path().exists()
        groups = GroupManager().list_groups() if not error and known else []
    return list(servers), list(groups), error, known


def resolve(project: Any, servers: Optional[Sequence[Dict[str, Any]]] = None,
            groups: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{"ids": [...], "hosts": [{id, name, host, groups}], "hub": id|None, "include_hub": bool,
    "missing": {"groups": [...], "nodes": [...]}, "error": str|None} for an ops project.

    ids keeps servers.json order and never contains the hub; include_hub says whether "local" may be used.
    Unknown group or node ids are reported in missing, not raised: a group rule may match nodes later.
    """
    servers, groups, error, groups_known = _load(servers, groups)
    targets = project.targets or {}
    by_id = {str(s["id"]): s for s in servers}
    group_by_id = {str(g.get("id")): g for g in groups}
    wanted: set = set(by_id) if targets.get("all") else set()
    missing: Dict[str, List[str]] = {"groups": [], "nodes": []}
    for gid in targets.get("groups") or []:
        group = group_by_id.get(gid)
        if group is None:
            if groups_known:  # without the hub database a group cannot be told apart from a missing one
                missing["groups"].append(gid)
        else:
            wanted.update(str(m) for m in group.get("members") or [])
    for nid in targets.get("nodes") or []:
        if nid in by_id:
            wanted.add(nid)
        else:
            missing["nodes"].append(nid)
    wanted -= set(targets.get("exclude") or [])
    hub = hub_id(servers)
    wanted.discard(hub)
    member_of: Dict[str, List[str]] = {}
    for g in groups:
        for m in g.get("members") or []:
            member_of.setdefault(str(m), []).append(str(g.get("name") or g.get("id")))
    hosts = [{"id": sid, "name": s.get("name") or sid, "host": s.get("hostname") or s.get("tailscale_ip") or "",
              "groups": member_of.get(sid, [])}
             for sid, s in by_id.items() if sid in wanted]
    return {
        "ids": [h["id"] for h in hosts],
        "hosts": hosts,
        "hub": hub,
        # ops_roles of every inventory entry (the hub included, it is reached as "local"): package protection reads them
        "roles": {sid: [str(r) for r in (s.get("ops_roles") or [])] for sid, s in by_id.items()},
        "include_hub": bool((project.policy or {}).get("include_hub")),
        "missing": missing,
        "error": error,
        "groups_unknown": not groups_known and bool(targets.get("groups")),
    }


def group_hosts(project: Any, group_ids: Sequence[str], servers: Optional[Sequence[Dict[str, Any]]] = None,
                groups: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{"ids": [...], "missing": [...], "error": str|None}: the target hosts of the project that belong to one of
    the groups. It resolves the groups afresh and fails closed: when servers.json or the group database cannot be
    read, ids is empty and error says why. The hub is never in it."""
    servers, groups, error, groups_known = _load(servers, groups)
    if not error and not groups_known:
        error = "the node groups cannot be read here (no hub database)"
    if error:
        return {"ids": [], "missing": [], "error": error}
    by_id = {str(g.get("id")): g for g in groups}
    missing = [g for g in group_ids if g not in by_id]
    members = {str(m) for g in group_ids if g in by_id for m in by_id[g].get("members") or []}
    resolved = resolve(project, servers, groups)
    return {"ids": [i for i in resolved["ids"] if i in members], "missing": missing, "error": resolved["error"]}


def check_target(project: Any, target_host: Optional[str], resolved: Optional[Dict[str, Any]] = None) -> str:
    """The target an ops action may run on ("local" for the hub), or TargetScopeError."""
    resolved = resolved or resolve(project)
    target = (target_host or "").strip() or "local"
    if target in LOCAL_TARGETS:
        if not resolved["include_hub"]:
            raise TargetScopeError(f"project {project.id} does not include the hub; name one of its target hosts")
        return "local"
    if resolved["hub"] and target == resolved["hub"]:
        raise TargetScopeError("the hub is reached as target_host \"local\", never over fleet SSH")
    if target not in resolved["ids"]:
        if resolved.get("error"):
            raise TargetScopeError(f"cannot resolve the targets of project {project.id}: {resolved['error']}")
        raise TargetScopeError(f"{target} is not a target of project {project.id} "
                               f"(targets: {', '.join(resolved['ids']) or 'none'})")
    return target


def host_roles(project: Any, target: str, resolved: Optional[Dict[str, Any]] = None) -> List[str]:
    """The ops_roles of a checked target ("local" is the hub's entry). Empty when the inventory has none."""
    resolved = resolved or resolve(project)
    sid = resolved["hub"] if target in LOCAL_TARGETS else target
    return list((resolved.get("roles") or {}).get(sid or "", []))


def prompt_text(project: Any, resolved: Dict[str, Any]) -> str:
    """The target list the agents of an ops debate get with the task."""
    lines = [f"Ops project {project.id}: the only hosts actions may target (resolved now; "
             "use the id as target_host):"]
    lines += [f"- {h['id']} ({h['name']}{', ' + h['host'] if h['host'] else ''})"
              + (f" groups: {', '.join(h['groups'])}" if h["groups"] else "") for h in resolved["hosts"]]
    if not resolved["hosts"]:
        lines.append("- none: no action can run until the project's targets match a node")
    if resolved.get("groups_unknown"):
        lines.append("Node groups could not be read here, so hosts named only through a group are missing.")
    if resolved["include_hub"]:
        lines.append(f"- local: the hub {resolved['hub'] or ''} itself, inside the agent's sandbox".rstrip())
    else:
        lines.append("The hub is not a target of this project.")
    return "\n".join(lines)

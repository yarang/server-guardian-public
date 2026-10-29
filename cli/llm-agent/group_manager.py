"""
User-defined node groups: validation, rule evaluation and storage in the hub database.

A group is a named, many-to-many set of servers: explicit ids (`include`), dynamic `rules`
(each an AND of conditions; the group matches when ANY rule matches) and `exclude` ids removed
last. Members are always computed against the current servers.json, so a new server that
matches a rule joins automatically. See docs/design/node-groups.md.

Pure functions (parse_group, resolve_members, ...) take plain dicts; GroupManager adds the
DecisionDBManager storage and the one-time seed from the legacy servers.json `group` field.
"""

import difflib
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from db_manager import DecisionDBManager

GROUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_NAME = 64
MAX_DESCRIPTION = 500
MAX_IDS = 500
MAX_RULES = 20
MAX_CONDITIONS = 10
MAX_VALUE = 128

TONES = ("ok", "warn", "crit", "info", "idle")
GROUP_KEYS = {"id", "name", "description", "tone", "include", "rules", "exclude"}
SEED_MARKER = "node_groups_seeded"
SEED_DESCRIPTION = "Seeded from servers.json group"
UNGROUPED = "Ungrouped"

SCALAR_OPS = ("eq", "ne")
LIST_OPS = ("contains", "not_contains")
# Rule fields: server JSON key -> (kind, allowed operators).
FIELDS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "tier": ("scalar", SCALAR_OPS),
    "arch": ("scalar", SCALAR_OPS),
    "guardian_role": ("scalar", SCALAR_OPS),
    "role": ("scalar", SCALAR_OPS),
    "tailscale_tags": ("list", LIST_OPS),
    "cli_support": ("list", LIST_OPS),
    "name": ("scalar", ("glob", "eq", "ne")),
}

Server = Dict[str, Any]
Group = Dict[str, Any]


class GroupValidationError(ValueError):
    """Invalid group definition; the message is shown to the user as is."""


class GroupNotFoundError(LookupError):
    pass


# --- servers.json -----------------------------------------------------------------------


def servers_file() -> Optional[Path]:
    """servers.json the hub agent uses. When GUARDIAN_SERVERS_FILE is set (the Go agent sets it) that path is
    returned as is, even if missing, so loading fails naming it (same rule as the Go agent). Only when it is
    unset: the repository's configs/servers.json, then ~/.config/agentthread/servers.json."""
    configured = os.environ.get("GUARDIAN_SERVERS_FILE")
    if configured:
        return Path(configured).expanduser()
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "configs" / "servers.json",
        Path("~/.config/agentthread/servers.json").expanduser(),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_servers(path: Optional[Path] = None) -> Tuple[List[Server], Optional[str], Optional[Path]]:
    """Returns (servers, error, path). On failure servers is empty and error says why."""
    path = path or servers_file()
    if path is None:
        return [], "servers.json not found (set GUARDIAN_SERVERS_FILE)", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"cannot read {path}: {exc}", path
    if not isinstance(data, list):
        return [], f"{path}: expected a JSON array of servers", path
    return [s for s in data if isinstance(s, dict) and s.get("id")], None, path


# --- validation -------------------------------------------------------------------------


def name_key(name: str) -> str:
    """Case-insensitive uniqueness key for group names."""
    return " ".join(name.split()).casefold()


def slugify(name: str) -> str:
    """Group id derived from a name: lowercase [a-z0-9] runs joined by '-', else a hash."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56].strip("-")
    if not slug:
        slug = "group-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return slug


def _text(data: Dict[str, Any], key: str, limit: int, required: bool = False) -> str:
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise GroupValidationError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise GroupValidationError(f"{key} is required")
    if len(value) > limit:
        raise GroupValidationError(f"{key} is longer than {limit} characters")
    if CONTROL_RE.search(value):
        raise GroupValidationError(f"{key} must not contain control characters")
    return value


def _ids(data: Dict[str, Any], key: str) -> List[str]:
    raw = data.get(key) or []
    if not isinstance(raw, list):
        raise GroupValidationError(f"{key} must be a list of server ids")
    if len(raw) > MAX_IDS:
        raise GroupValidationError(f"{key} has more than {MAX_IDS} ids")
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str) or not SERVER_ID_RE.match(item):
            raise GroupValidationError(f"{key}: invalid server id {item!r}")
        if item not in out:
            out.append(item)
    return out


def _condition(raw: Any, where: str) -> Dict[str, str]:
    if not isinstance(raw, dict) or set(raw) - {"field", "op", "value"}:
        raise GroupValidationError(f"{where}: a condition is {{field, op, value}}")
    field, op, value = raw.get("field"), raw.get("op"), raw.get("value")
    if field not in FIELDS:
        raise GroupValidationError(f"{where}: unknown field {field!r} (one of {', '.join(FIELDS)})")
    ops = FIELDS[field][1]
    if op not in ops:
        raise GroupValidationError(f"{where}: {field} supports {', '.join(ops)}, not {op!r}")
    if not isinstance(value, str) or not value.strip():
        raise GroupValidationError(f"{where}: value must be a non-empty string")
    value = value.strip()
    if len(value) > MAX_VALUE or CONTROL_RE.search(value):
        raise GroupValidationError(f"{where}: value must be at most {MAX_VALUE} printable characters")
    return {"field": field, "op": op, "value": value}


def parse_rules(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GroupValidationError("rules must be a list")
    if len(raw) > MAX_RULES:
        raise GroupValidationError(f"at most {MAX_RULES} rules")
    rules = []
    for i, rule in enumerate(raw, 1):
        if not isinstance(rule, dict) or set(rule) != {"conditions"}:
            raise GroupValidationError(f"rule {i}: expected {{\"conditions\": [...]}}")
        conds = rule["conditions"]
        if not isinstance(conds, list) or not conds:
            raise GroupValidationError(f"rule {i}: needs at least one condition (an empty rule would match every server)")
        if len(conds) > MAX_CONDITIONS:
            raise GroupValidationError(f"rule {i}: at most {MAX_CONDITIONS} conditions")
        rules.append({"conditions": [_condition(c, f"rule {i}") for c in conds]})
    return rules


def parse_group(data: Any, require_name: bool = True) -> Group:
    """Validates a group definition and returns its normalized form (id may be missing)."""
    if not isinstance(data, dict):
        raise GroupValidationError("group must be a JSON object")
    unknown = set(data) - GROUP_KEYS
    if unknown:
        raise GroupValidationError(f"unknown keys: {', '.join(sorted(unknown))}")
    group: Group = {
        "name": _text(data, "name", MAX_NAME, required=require_name),
        "description": _text(data, "description", MAX_DESCRIPTION),
        "include": _ids(data, "include"),
        "rules": parse_rules(data.get("rules")),
        "exclude": _ids(data, "exclude"),
        "tone": None,
    }
    group["name"] = " ".join(group["name"].split())
    tone = data.get("tone")
    if tone not in (None, ""):
        if tone not in TONES:
            raise GroupValidationError(f"tone must be one of {', '.join(TONES)}")
        group["tone"] = tone
    gid = data.get("id")
    if gid not in (None, ""):
        if not isinstance(gid, str) or not GROUP_ID_RE.match(gid):
            raise GroupValidationError("id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        group["id"] = gid
    return group


# --- rule evaluation --------------------------------------------------------------------


def _field_values(server: Server, field: str) -> List[str]:
    value = server.get(field)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).casefold() for v in value]
    return [str(value).casefold()]


def condition_matches(server: Server, cond: Dict[str, str]) -> bool:
    values = _field_values(server, cond["field"])
    want = cond["value"].casefold()
    op = cond["op"]
    if op == "eq":
        return want in values
    if op == "ne":
        return want not in values
    if op == "contains":
        return want in values
    if op == "not_contains":
        return want not in values
    if op == "glob":
        return any(fnmatch.fnmatchcase(v, want) for v in values)
    return False


def rule_matches(server: Server, rule: Dict[str, Any]) -> bool:
    return all(condition_matches(server, c) for c in rule["conditions"])


def resolve_members(group: Group, servers: Sequence[Server]) -> List[str]:
    """(include ∪ servers matching any rule) − exclude, in servers.json order."""
    include = set(group.get("include") or [])
    exclude = set(group.get("exclude") or [])
    rules = group.get("rules") or []
    members = []
    for s in servers:
        sid = s.get("id")
        if sid in exclude:
            continue
        if sid in include or any(rule_matches(s, r) for r in rules):
            members.append(sid)
    return members


def group_warnings(group: Group, servers: Sequence[Server], members: Sequence[str]) -> List[str]:
    known = {s.get("id") for s in servers}
    warnings = [f"include: {i} is not in servers.json" for i in group.get("include") or [] if i not in known]
    warnings += [f"exclude: {i} is not in servers.json" for i in group.get("exclude") or [] if i not in known]
    if servers and not members:
        warnings.append("no servers match this group")
    return warnings


def catalog(servers: Sequence[Server]) -> Dict[str, Any]:
    """Rule fields with their operators and the distinct values seen in servers.json."""
    fields = []
    for field, (kind, ops) in FIELDS.items():
        seen: List[str] = []
        for s in servers:
            raw = s.get(field)
            for v in raw if isinstance(raw, list) else ([raw] if raw is not None else []):
                v = str(v)
                if v not in seen:
                    seen.append(v)
        fields.append({"field": field, "kind": kind, "ops": list(ops), "values": sorted(seen)})
    return {"fields": fields, "tones": list(TONES)}


def seed_definitions(servers: Sequence[Server]) -> List[Group]:
    """One static group per distinct legacy servers.json `group` value."""
    by_name: Dict[str, Group] = {}
    used: set = set()
    for s in servers:
        legacy = " ".join(str(s.get("group") or "").split())
        if not legacy:
            continue
        key = name_key(legacy)
        if key not in by_name:
            gid = base = slugify(legacy)
            n = 2
            while gid in used:
                gid, n = f"{base}-{n}", n + 1
            used.add(gid)
            by_name[key] = {"id": gid, "name": legacy, "name_key": key, "description": SEED_DESCRIPTION, "include": []}
        by_name[key]["include"].append(s["id"])
    return list(by_name.values())


def memberships(groups: Iterable[Dict[str, Any]], server_ids: Iterable[str]) -> Dict[str, List[str]]:
    """Group name -> member ids for aggregation; ids in no group go under "Ungrouped".

    A server in several groups is counted in each of them."""
    out: Dict[str, List[str]] = {}
    grouped = set()
    ids = list(server_ids)
    wanted = set(ids)
    for g in groups:
        members = [m for m in g.get("members") or [] if m in wanted]
        out[g.get("name") or g.get("id")] = members
        grouped.update(members)
    rest = [i for i in ids if i not in grouped]
    if rest:
        out[UNGROUPED if UNGROUPED not in out else f"{UNGROUPED} (no group)"] = rest
    return out


# --- --target resolution ----------------------------------------------------------------


class TargetError(ValueError):
    pass


def resolve_target(target: str, servers: Sequence[Server], groups: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolves a CLI --target: a server (exact id/name/hostname) wins, then a group by id or
    case-insensitive name. Returns {"kind": "server"|"group", "id", "name", "ids": [...]}.

    Raises TargetError with close matches when nothing matches or the group is empty."""
    t = target.strip()
    for s in servers:
        if t and t in (s.get("id"), s.get("name"), s.get("hostname")):
            return {"kind": "server", "id": s["id"], "name": s.get("name") or s["id"], "ids": [s["id"]]}
    key = name_key(t)
    for g in groups:
        if t == g.get("id") or key == name_key(str(g.get("name") or "")):
            ids = list(g.get("members") or [])
            if not ids:
                raise TargetError(f"group {g.get('name')!r} has no members")
            return {"kind": "group", "id": g["id"], "name": g.get("name") or g["id"], "ids": ids}
    choices = [str(s.get("id")) for s in servers] + [str(g.get("name")) for g in groups] + [str(g.get("id")) for g in groups]
    close = difflib.get_close_matches(t, sorted(set(choices)), n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else " Use --groups to list groups."
    raise TargetError(f"unknown target {target!r}: not a server id/name or group.{hint}")


# --- storage ----------------------------------------------------------------------------


class GroupManager:
    """Group CRUD on the hub database plus the one-time seed from servers.json."""

    def __init__(self, db: Optional[DecisionDBManager] = None, servers_path: Optional[Path] = None):
        self.db = db or DecisionDBManager()
        self.servers, self.servers_error, self.servers_path = load_servers(servers_path)
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        # Without servers.json there is nothing to migrate yet; try again on the next call.
        if self.servers_error or self.db.get_meta(SEED_MARKER):
            return
        self.db.seed_node_groups(seed_definitions(self.servers), SEED_MARKER)

    def _entry(self, group: Group) -> Dict[str, Any]:
        members = resolve_members(group, self.servers)
        return {**group, "members": members, "warnings": group_warnings(group, self.servers, members)}

    def list_groups(self) -> List[Dict[str, Any]]:
        return [self._entry(g) for g in self.db.list_node_groups()]

    def report(self) -> Dict[str, Any]:
        groups = self.list_groups()
        grouped = {m for g in groups for m in g["members"]}
        return {
            "groups": groups,
            "ungrouped": [s["id"] for s in self.servers if s["id"] not in grouped],
            "catalog": catalog(self.servers),
            "servers_file": str(self.servers_path) if self.servers_path else None,
            "warnings": [self.servers_error] if self.servers_error else [],
        }

    def preview(self, data: Any) -> Dict[str, Any]:
        group = parse_group(data, require_name=False)
        members = resolve_members(group, self.servers)
        warnings = group_warnings(group, self.servers, members)
        if self.servers_error:
            warnings.insert(0, self.servers_error)
        return {"ok": True, "members": members, "warnings": warnings}

    def _unique_id(self, base: str) -> str:
        gid, n = base, 2
        while self.db.get_node_group(gid) is not None:
            suffix = f"-{n}"
            gid, n = base[: 64 - len(suffix)] + suffix, n + 1
        return gid

    def save(self, data: Any) -> Dict[str, Any]:
        """Creates (no id, or a new id) or updates (existing id) a group."""
        group = parse_group(data)
        key = name_key(group["name"])
        if "id" not in group:
            group["id"] = self._unique_id(slugify(group["name"]))
        for other in self.db.list_node_groups():
            if other["id"] != group["id"] and name_key(other["name"]) == key:
                raise GroupValidationError(f"group name {group['name']!r} is already used by {other['id']}")
        try:
            self.db.save_node_group(group, key)
        except sqlite3.IntegrityError as exc:
            raise GroupValidationError(f"group name {group['name']!r} is already used") from exc
        entry = self._entry(self.db.get_node_group(group["id"]) or group)
        return {"ok": True, "id": group["id"], "members": entry["members"], "warnings": entry["warnings"]}

    def delete(self, group_id: str) -> None:
        if not GROUP_ID_RE.match(group_id or "") or not self.db.delete_node_group(group_id):
            raise GroupNotFoundError(f"no group {group_id!r}")

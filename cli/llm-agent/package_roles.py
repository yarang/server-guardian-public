"""
Role-based and derived protection for package actions (docs/design/fleet-provisioning.md, chapter 6.2, P4).

Four sources of protected packages are added together by ops_packages.check and ops_packages.assess:

- the built-in list of ops_packages.py (cannot be lifted),
- the roles of the host: `ops_roles` on the inventory entry maps through configs/package-roles.json to patterns per
  manager (this module),
- what runs on the host: the package that owns every running service and the process name of every listening
  port, from the host facts (derived(); a package installed and started an hour ago is protected the moment it runs),
- the project's `policy.protected_packages` (it can only add).

A role the file does not define is not ignored: the validator refuses package actions on that host until the
inventory is fixed, because a misspelt role would silently protect nothing.

The file is data of the repository: validate_roles() is run by scripts/ci.sh config through `guardian.py
--package-roles` and again whenever the roles are loaded. Standard library only.
"""

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import ops_packages

ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A fnmatch pattern over package names: letters, digits and . _ + @ / - plus * and ?, at least one literal character
# (a lone * would protect everything). No brackets and no whitespace.
PATTERN = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9@*?][A-Za-z0-9+._@/*?-]{0,99}$")
MAX_ROLES = 64
MAX_PATTERNS_PER_MANAGER = 40
MAX_PROJECT_PATTERNS = 100
MAX_HOST_ROLES = 16
EVERY_MANAGER = "*"  # key of the patterns that apply to every manager (the project's additions)


def roles_path() -> Path:
    override = os.environ.get("GUARDIAN_PACKAGE_ROLES")
    return Path(override) if override else Path(__file__).resolve().parent.parent.parent / "configs" / "package-roles.json"


def check_patterns(raw: object, field: str = "protected_packages", limit: int = MAX_PROJECT_PATTERNS) -> List[str]:
    """Validates a list of package name patterns (policy.protected_packages, or the patterns of one role and
    manager). Raises ValueError with the field named."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > limit or not all(isinstance(p, str) and PATTERN.fullmatch(p) for p in raw):
        raise ValueError(f"{field} must list at most {limit} package name patterns (letters, digits and . _ + @ / -, "
                         "with * or ? as wildcards, no brackets or spaces), e.g. nginx*")
    return list(dict.fromkeys(raw))


def validate_roles(data: Any) -> Dict[str, Dict[str, Any]]:
    """The roles of a parsed package-roles.json as {name: {"description", "protected": {manager: [patterns]}}}.
    Raises ValueError with the first problem."""
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("roles"), dict):
        raise ValueError("package-roles.json must be an object {\"version\": 1, \"roles\": {...}}")
    roles = data["roles"]
    if not 1 <= len(roles) <= MAX_ROLES:
        raise ValueError(f"package-roles.json must define 1 to {MAX_ROLES} roles")
    out: Dict[str, Dict[str, Any]] = {}
    for name, body in roles.items():
        if not isinstance(name, str) or not ROLE_NAME.fullmatch(name):
            raise ValueError(f"role name {name!r} must be lowercase letters, digits and _ (1-32 characters, starting with a letter)")
        if not isinstance(body, dict) or set(body) - {"description", "protected"}:
            raise ValueError(f"role {name}: expected only \"description\" and \"protected\"")
        description = body.get("description", "")
        if not isinstance(description, str) or len(description) > 300 or "\n" in description:
            raise ValueError(f"role {name}: description must be one line of at most 300 characters")
        protected = body.get("protected")
        if not isinstance(protected, dict) or not protected:
            raise ValueError(f"role {name}: protected must map a package manager to a list of patterns")
        per_manager: Dict[str, List[str]] = {}
        for manager, patterns in protected.items():
            if manager not in ops_packages.MANAGERS:
                raise ValueError(f"role {name}: {manager!r} is not a package manager ({', '.join(ops_packages.MANAGERS)})")
            checked = check_patterns(patterns, f"role {name}: protected.{manager}", MAX_PATTERNS_PER_MANAGER)
            if not checked:
                raise ValueError(f"role {name}: protected.{manager} is empty")
            per_manager[manager] = checked
        out[name] = {"description": description, "protected": per_manager}
    return out


def load_roles(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The validated roles of configs/package-roles.json (GUARDIAN_PACKAGE_ROLES points elsewhere). Raises ValueError
    when the file is missing, unreadable or invalid."""
    path = path or roles_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    return validate_roles(data)


def report(path: Optional[Path] = None) -> Tuple[int, Dict[str, Any]]:
    """(exit code, JSON) of `guardian.py --package-roles`: the roles for the dashboard, 2 when the file is invalid."""
    try:
        roles = load_roles(path)
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc), "roles": []}
    return 0, {"ok": True, "managers": list(ops_packages.MANAGERS),
               "roles": [{"name": n, "description": r["description"], "protected": r["protected"]}
                         for n, r in sorted(roles.items())]}


class Protection:
    """The protected patterns of one host: where each came from, per manager. Built by protection()."""

    def __init__(self, roles: Sequence[str], unknown_roles: Sequence[str]) -> None:
        self.roles = list(roles)
        self.unknown_roles = list(unknown_roles)
        self.blind: List[str] = []  # blind_spots() of the host, see blind_for()
        self.system: Tuple[str, ...] = ()  # the managers that install what runs on the host
        self._patterns: Dict[str, Dict[str, str]] = {}

    def blind_for(self, manager: str) -> List[str]:
        """The blind spots that matter for one manager: derived protection only speaks for the system managers of the
        host's OS family, so a user-level manager (npm, pipx, ...) has none."""
        return list(self.blind) if manager in self.system else []

    def add(self, manager: str, pattern: str, source: str) -> None:
        self._patterns.setdefault(manager, {}).setdefault(pattern, source)

    def for_manager(self, manager: str) -> List[str]:
        merged = dict(self._patterns.get(EVERY_MANAGER, {}))
        merged.update(self._patterns.get(manager, {}))
        return list(merged)

    def as_mapping(self) -> Dict[str, List[str]]:
        return {m: list(p) for m, p in self._patterns.items()}

    def describe(self, manager: str) -> List[str]:
        """"pattern (source)" lines for the approval screen: the role, the running service or the project."""
        merged = dict(self._patterns.get(EVERY_MANAGER, {}))
        merged.update(self._patterns.get(manager, {}))
        return [f"{pattern} ({source})" for pattern, source in sorted(merged.items())]

    def source_of(self, manager: str, name: str) -> Optional[Tuple[str, str]]:
        """(pattern, source) of the first pattern here that covers a package name, else None."""
        base = name.split(":", 1)[0]
        merged = dict(self._patterns.get(EVERY_MANAGER, {}))
        merged.update(self._patterns.get(manager, {}))
        for pattern, source in merged.items():
            if fnmatch.fnmatchcase(base, pattern):
                return pattern, source
        return None


def _literal(name: str) -> bool:
    """A package name that is safe to use as its own pattern (no wildcard, bracket or odd character)."""
    return bool(PATTERN.fullmatch(name)) and not any(c in name for c in "*?/@")


def derived(facts: Optional[Mapping[str, Any]]) -> List[Tuple[str, str]]:
    """(package name, source) for what runs on the host: the owner of every running service and the process of every
    listening port. Process names only match when a package carries the same name (nginx, redis-server); others
    protect nothing, and a name that is not a plain package name is skipped."""
    found: Dict[str, str] = {}
    for service in (facts or {}).get("services") or []:
        package = str(service.get("package") or "")
        if package and package != "unknown" and _literal(package):
            found.setdefault(package, f"running {service.get('unit', 'service')}")
    for port in (facts or {}).get("ports") or []:
        # The package that owns the listening binary (postgres -> postgresql-16) when the facts carry it, else the
        # process name, which only matches when a package happens to carry the same name.
        package = str(port.get("package") or "")
        process = str(port.get("process") or "")
        name = package if package and package != "unknown" else process
        if name and name != "unknown" and _literal(name):
            found.setdefault(name, f"listening on {port.get('listen', 'a port')}")
    return sorted(found.items())


def blind_spots(facts: Optional[Mapping[str, Any]]) -> List[str]:
    """Why the derived protection of a host may be incomplete, as short English reasons (empty when it is not).

    A listening port whose owner the host could not name (a socket-activated service, a process of another user seen
    without root) and a host where the owners of other users' sockets are hidden mean that a package which serves a
    port may not be in derived(). A removal on such a host cannot be ruled safe by the protected list alone.
    A running service whose package is unknown is not a blind spot: the service is listed by name, and a unit without
    a package file belongs to no package that could be removed."""
    reasons: List[str] = []
    visibility = (facts or {}).get("ports_owner_visibility")
    if visibility is not None and visibility != "full":
        reasons.append("the owners of other users' listening ports are hidden (no root or passwordless sudo)")
    hidden = sorted({str(p.get("listen", "?")) for p in (facts or {}).get("ports") or []
                     if str(p.get("process") or "unknown") == "unknown"})
    if hidden:
        shown = ", ".join(hidden[:3]) + (f" and {len(hidden) - 3} more" if len(hidden) > 3 else "")
        reasons.append(f"no owner found for listening port(s) {shown}")
    return reasons


def protection(roles: Iterable[str], facts: Optional[Mapping[str, Any]], project_extra: Iterable[str] = (),
               catalog: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Protection:
    """The role, derived and project protection of one host. catalog defaults to configs/package-roles.json (a file
    that cannot be loaded raises ValueError: no package action without it)."""
    catalog = catalog if catalog is not None else load_roles()
    names = list(dict.fromkeys(roles))
    result = Protection(names, [r for r in names if r not in catalog])
    for role in names:
        for manager, patterns in (catalog.get(role) or {}).get("protected", {}).items():
            for pattern in patterns:
                result.add(manager, pattern, f"role {role}")
    system = ops_packages.SYSTEM_FAMILY.get(str((facts or {}).get("os_family", "")), ())
    result.system = tuple(system)
    result.blind = blind_spots(facts)
    for package, source in derived(facts):
        for manager in system:
            result.add(manager, package, source)
    for pattern in project_extra:
        result.add(EVERY_MANAGER, pattern, "project policy")
    return result

"""
Install bundles for package actions (docs/design/fleet-provisioning.md, chapter 6.3).

A bundle is a named list of package names per manager, split into `required` (the thing does not work without it) and
`optional` (extras the operator or the judge may leave out). It is a *suggestion*, not a permission: expanding a bundle
yields plain package names for `ops:package_install`, and every one of them goes through the same validator, simulation,
protected-list check and drift check as a package typed by hand. Bundles never touch the protection of package_roles.py
(roles are a veto and stay wide, bundles are a proposal and stay exact), and a role never installs anything.

The file is data of the repository: validate_bundles() is run by scripts/ci.sh config through `guardian.py
--package-bundles` and again whenever the bundles are loaded. Standard library only.
"""

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ops_packages

BUNDLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_BUNDLES = 64
MAX_NOTES = 5
MAX_NOTE_LENGTH = 200
DEFAULT_MAX_AGE_DAYS = 180


def max_age_days() -> int:
    """How old a bundle's `reviewed` date may get before it is reported as stale (GUARDIAN_BUNDLE_MAX_AGE_DAYS)."""
    try:
        return max(1, int(os.environ.get("GUARDIAN_BUNDLE_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def bundles_path() -> Path:
    override = os.environ.get("GUARDIAN_PACKAGE_BUNDLES")
    return Path(override) if override else Path(__file__).resolve().parent.parent.parent / "configs" / "package-bundles.json"


def _names(raw: Any, manager: str, field: str) -> List[str]:
    """A list of package names valid for one manager, no duplicates. Raises ValueError with the field named."""
    pattern = ops_packages.NAME_PATTERNS[manager]
    if not isinstance(raw, list) or len(raw) > ops_packages.MAX_PACKAGES:
        raise ValueError(f"{field} must be a list of at most {ops_packages.MAX_PACKAGES} package names")
    for name in raw:
        if not isinstance(name, str) or not pattern.fullmatch(name):
            raise ValueError(f"{field}: {name!r} is not a valid {manager} package name")
    if len(set(raw)) != len(raw):
        raise ValueError(f"{field} lists a package twice")
    return list(raw)


def validate_bundles(data: Any) -> Dict[str, Dict[str, Any]]:
    """The bundles of a parsed package-bundles.json as {name: {"description", "notes", "packages": {manager:
    {"required": [...], "optional": [...]}}}}. Raises ValueError with the first problem."""
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("bundles"), dict):
        raise ValueError("package-bundles.json must be an object {\"version\": 1, \"bundles\": {...}}")
    bundles = data["bundles"]
    if not 1 <= len(bundles) <= MAX_BUNDLES:
        raise ValueError(f"package-bundles.json must define 1 to {MAX_BUNDLES} bundles")
    out: Dict[str, Dict[str, Any]] = {}
    for name, body in bundles.items():
        if not isinstance(name, str) or not BUNDLE_NAME.fullmatch(name):
            raise ValueError(f"bundle name {name!r} must be lowercase letters, digits and _ (1-32 characters, starting with a letter)")
        if not isinstance(body, dict) or set(body) - {"description", "reviewed", "notes", "packages"}:
            raise ValueError(f"bundle {name}: expected only \"description\", \"reviewed\", \"notes\" and \"packages\"")
        reviewed = body.get("reviewed")
        try:
            if not isinstance(reviewed, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reviewed):
                raise ValueError
            datetime.date.fromisoformat(reviewed)
        except ValueError:
            raise ValueError(f"bundle {name}: reviewed must be the date a person last checked the package names and "
                             f"versions, as YYYY-MM-DD") from None
        description = body.get("description", "")
        if not isinstance(description, str) or len(description) > 300 or "\n" in description:
            raise ValueError(f"bundle {name}: description must be one line of at most 300 characters")
        notes = body.get("notes", [])
        if (not isinstance(notes, list) or len(notes) > MAX_NOTES
                or not all(isinstance(n, str) and 0 < len(n) <= MAX_NOTE_LENGTH and "\n" not in n for n in notes)):
            raise ValueError(f"bundle {name}: notes must be at most {MAX_NOTES} one-line strings of up to {MAX_NOTE_LENGTH} characters")
        packages = body.get("packages")
        if not isinstance(packages, dict) or not packages:
            raise ValueError(f"bundle {name}: packages must map a package manager to its package lists")
        per_manager: Dict[str, Dict[str, List[str]]] = {}
        for manager, lists in packages.items():
            if manager not in ops_packages.MANAGERS:
                raise ValueError(f"bundle {name}: {manager!r} is not a package manager ({', '.join(ops_packages.MANAGERS)})")
            if not isinstance(lists, dict) or set(lists) - {"required", "optional"}:
                raise ValueError(f"bundle {name}: {manager} expects only \"required\" and \"optional\"")
            required = _names(lists.get("required"), manager, f"bundle {name}: {manager}.required")
            optional = _names(lists.get("optional", []), manager, f"bundle {name}: {manager}.optional")
            if not required:
                raise ValueError(f"bundle {name}: {manager}.required is empty")
            if set(required) & set(optional):
                raise ValueError(f"bundle {name}: {manager} lists {sorted(set(required) & set(optional))[0]} as required and optional")
            if len(required) + len(optional) > ops_packages.MAX_PACKAGES:
                raise ValueError(f"bundle {name}: {manager} has more than {ops_packages.MAX_PACKAGES} packages")
            per_manager[manager] = {"required": required, "optional": optional}
        out[name] = {"description": description, "reviewed": reviewed, "notes": list(notes), "packages": per_manager}
    return out


def stale(catalog: Dict[str, Dict[str, Any]], today: Optional[datetime.date] = None,
          limit_days: Optional[int] = None) -> List[Dict[str, Any]]:
    """The bundles whose `reviewed` date is older than the limit, oldest first, as {"name", "reviewed", "age_days"}.
    A stale bundle is a warning, not an error: it still expands, but a person should look at the names and versions."""
    today = today or datetime.date.today()
    limit = limit_days if limit_days is not None else max_age_days()
    old = [{"name": n, "reviewed": b["reviewed"], "age_days": (today - datetime.date.fromisoformat(b["reviewed"])).days}
           for n, b in catalog.items()]
    return sorted((o for o in old if o["age_days"] > limit), key=lambda o: -o["age_days"])


def load_bundles(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The validated bundles of configs/package-bundles.json (GUARDIAN_PACKAGE_BUNDLES points elsewhere). Raises
    ValueError when the file is missing, unreadable or invalid."""
    path = path or bundles_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    return validate_bundles(data)


def report(path: Optional[Path] = None) -> Tuple[int, Dict[str, Any]]:
    """(exit code, JSON) of `guardian.py --package-bundles`: the bundles for the judge and the dashboard, 2 when the
    file is invalid."""
    try:
        bundles = load_bundles(path)
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc), "bundles": []}
    return 0, {"ok": True, "managers": list(ops_packages.MANAGERS), "max_age_days": max_age_days(),
               "stale": stale(bundles),
               "bundles": [{"name": n, "description": b["description"], "reviewed": b["reviewed"], "notes": b["notes"],
                            "packages": b["packages"]} for n, b in sorted(bundles.items())]}


def expand(name: str, manager: str, optional: Iterable[str] = (), skip: Iterable[str] = (),
           installed: Iterable[str] = (), catalog: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The package names to hand to `ops:package_install` for one bundle on one manager.

    `optional` names the optional packages to include (none by default: a partial install is the default, the extras
    are asked for); `skip` drops required packages the operator does not want, which is allowed but reported, because a
    bundle without one of its required packages is not the bundle any more; `installed` removes what the host already
    has. Raises ValueError for an unknown bundle or manager, or a package the bundle does not offer.

    Returns {"bundle", "manager", "install": [...], "already_installed": [...], "skipped_required": [...],
    "optional_available": [...], "notes": [...], "reviewed": "YYYY-MM-DD"}."""
    catalog = catalog if catalog is not None else load_bundles()
    if name not in catalog:
        raise ValueError(f"unknown bundle {name!r} (known: {', '.join(sorted(catalog))})")
    lists = catalog[name]["packages"].get(manager)
    if lists is None:
        raise ValueError(f"bundle {name} has no package list for {manager} (it has: {', '.join(sorted(catalog[name]['packages']))})")
    wanted_optional, skipped = list(dict.fromkeys(optional)), list(dict.fromkeys(skip))
    for pkg in wanted_optional:
        if pkg not in lists["optional"]:
            raise ValueError(f"{pkg} is not an optional package of bundle {name} for {manager}")
    for pkg in skipped:
        if pkg not in lists["required"]:
            raise ValueError(f"{pkg} is not a required package of bundle {name} for {manager} (leave optional ones out instead)")
    have = set(installed)
    wanted = [p for p in lists["required"] if p not in skipped] + wanted_optional
    return {
        "bundle": name, "manager": manager,
        "install": [p for p in wanted if p not in have],
        "already_installed": [p for p in wanted if p in have],
        "skipped_required": skipped,
        "optional_available": [p for p in lists["optional"] if p not in wanted_optional],
        "notes": list(catalog[name]["notes"]),
        "reviewed": catalog[name]["reviewed"],
    }

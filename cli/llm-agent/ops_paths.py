"""
Paths that ops cleanup actions may never delete or truncate (docs/design/server-ops-project.md, 6.3, P4).

The default rules always apply; a project's policy.deny_paths can only add to them. A path is refused when:

- it is not a plain absolute path (no globs, spaces, `.` or `..` components, trailing slash);
- it is one of the top-level system directories itself (/, /home, /var, /var/log, /tmp, ...), whose removal would
  take everything below with it;
- it lies inside a protected tree (/etc, /boot, /usr, /var/lib, /proc, ...);
- it is a home directory itself (/root, /home/<user>);
- any component names a credential or Guardian directory (.ssh, .gnupg, .claude, .codex, .guardian, .config,
  server-guardian, guardian-workspace, guardian-eval, ...), or the path looks like secret material (is_secret);
- it is inside or an ancestor of a path matched by policy.deny_paths (fnmatch patterns, such as /srv/data/*).

Removing a directory removes what is below it, so a path that contains a protected place is refused too: that is
what the "itself" list, the home rule and the ancestor check against deny_paths cover.
"""

import fnmatch
import re
from typing import Iterable, List, Optional

from code_context import is_secret

PLAIN_PATH = re.compile(r"^(/[A-Za-z0-9._@+-]+)+$")
MAX_DENY_PATTERNS = 50
# Removing these would remove everything below them.
SYSTEM_DIRS = frozenset({
    "/", "/home", "/root", "/var", "/var/lib", "/var/log", "/var/cache", "/var/tmp", "/var/spool", "/var/mail",
    "/tmp", "/opt", "/srv", "/mnt", "/media", "/snap",
})
# Nothing at or below these is ever touched.
PROTECTED_TREES = ("/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/var/lib",
                   "/proc", "/sys", "/dev", "/run", "/snap", "/var/backups")
# A component with one of these names protects the path (credentials, Guardian state and checkouts).
PROTECTED_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".docker", ".kube", ".claude", ".codex", ".gemini", ".guardian", ".config",
    ".local", "server-guardian", "guardian-workspace", "guardian-workspaces", "guardian-eval", "gitea",
})


def _within(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree.rstrip("/") + "/")


def check_deny_patterns(raw: object) -> List[str]:
    """Validates policy.deny_paths: absolute fnmatch patterns. Raises ValueError."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_DENY_PATTERNS or not all(
            isinstance(p, str) and p.startswith("/") and 1 < len(p) <= 256 and ".." not in p.split("/")
            and not any(c in p for c in "\r\n\0") for p in raw):
        raise ValueError(f"deny_paths must list at most {MAX_DENY_PATTERNS} absolute path patterns such as /srv/data/*")
    return list(dict.fromkeys(raw))


def _pattern_prefix(pattern: str) -> str:
    """The literal leading directories of a pattern (/srv/data/* -> /srv/data)."""
    parts = []
    for part in pattern.split("/")[1:]:
        if any(c in part for c in "*?["):
            break
        parts.append(part)
    return "/" + "/".join(parts)


def refusal(path: str, deny_patterns: Iterable[str] = ()) -> Optional[str]:
    """Why an action may not delete or truncate path, or None when it may."""
    if not isinstance(path, str) or not PLAIN_PATH.fullmatch(path) or any(p in (".", "..") for p in path.split("/")[1:]):
        return f"{path!r} is not a plain absolute path (letters, digits and ._@+- only, no globs or ..)"
    if path in SYSTEM_DIRS:
        return f"{path} is a system directory"
    for tree in PROTECTED_TREES:
        if _within(path, tree):
            return f"{path} is inside the protected {tree}"
    parts = path.split("/")[1:]
    if (len(parts) == 2 and parts[0] == "home") or path == "/root":
        return f"{path} is a home directory"
    named = [p for p in parts if p.lower() in PROTECTED_NAMES]
    if named:
        return f"{path} is inside a protected directory ({named[0]})"
    if is_secret(path.lstrip("/")):
        return f"{path} looks like secret material"
    for pattern in deny_patterns:
        if fnmatch.fnmatchcase(path, pattern) or any(
                fnmatch.fnmatchcase("/" + "/".join(parts[:i]), pattern) for i in range(1, len(parts))):
            return f"{path} is denied by the project policy ({pattern})"
        if _within(_pattern_prefix(pattern), path) and _pattern_prefix(pattern) != "/":
            return f"{path} contains a path the project policy denies ({pattern})"
    return None

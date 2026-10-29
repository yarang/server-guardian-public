"""
Host facts and package information for ops projects (docs/design/fleet-provisioning.md, chapter 5, P1).

Two read-only checks of ops_checks.py are built here:

- host_facts: one shell script that prints plain `key: value` lines (no python3, no jq: macOS may not have them).
  parse_host_facts() turns the text into a dict on the hub. A run that did not reach the last line is not complete
  and is never cached.
- package_info: what a package manager knows about one name (does it exist, candidate and installed version,
  dependencies, installed packages that depend on it). Fixed commands, a validated manager and name.

The facts are cached per host in <data dir>/ops-facts/<host>.json so the validator of a plan (P3) can reuse a recent
result. The cache is production state; tests call testenv.isolate_data_dir().
"""

import json
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Non-login ssh sessions on macOS lack /opt/homebrew/bin (same trap as Go on the Mac), so the usual install
# locations are added to PATH before anything is probed. Appended, so the system tools still win.
PATH_PREFIX = ('export LC_ALL=C; PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/home/linuxbrew/.linuxbrew/bin:'
               '$HOME/.bun/bin:$HOME/.local/bin:$HOME/.cargo/bin"; '
               'for d in "$HOME"/.nvm/versions/node/*/bin; do [ -d "$d" ] && PATH="$PATH:$d"; done; export PATH; ')

# Package managers the facts look for (the ones of docs/design/fleet-provisioning.md chapter 4).
MANAGER_PROGRAMS = ("apt-get", "dnf", "yum", "zypper", "apk", "pacman", "brew", "npm", "bun", "pnpm", "yarn", "pipx", "uv")
MAX_LISTED = 60  # running services and listening ports are cut here
FACTS_TIMEOUT = 60  # dpkg -S / rpm -qf for each service can take longer than the 20 s of a plain check
PACKAGE_INFO_TIMEOUT = 60  # npm view and brew info go over the network
FACTS_MAX_AGE = 900  # seconds a cached result may be used (15 minutes)
FACTS_END = "facts_complete: yes"
REQUIRED_FACTS = ("os_family", "arch", "init", "sudo_nopasswd")

# The managers package_info knows today. The other managers of ops_packages (bun, zypper, apk, pacman, pnpm, yarn,
# pipx, uv) have adapters but no package_info yet; a read-only query is added once it is verified on a host.
INFO_MANAGERS = ("apt", "dnf", "brew", "npm")
NAME_PATTERNS = {
    "apt": re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}$"),
    "dnf": re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,99}$"),
    # No "/": a tap name (owner/repo/formula) would add third-party code.
    "brew": re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._@-]{0,99}$"),
    # Registry names only, optionally scoped. No ":" so URLs, git+ and file: specs cannot match; no version suffix.
    "npm": re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,99}/)?[a-z0-9][a-z0-9._-]{0,99}$"),
}
HOST_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def case_by_os(darwin: str, other: str) -> str:
    """A command that runs `darwin` on macOS and `other` elsewhere. The builders only see their arguments, and the
    facts of a host may not exist yet, so the branch is taken on the host."""
    return f'case "$(uname -s)" in Darwin) {darwin};; *) {other};; esac'


def host_facts_command() -> str:
    managers = " ".join(MANAGER_PROGRAMS)
    # One owner lookup per running service: dpkg -S, rpm -qf (Linux), brew services (macOS).
    linux_services = (
        "if command -v systemctl >/dev/null 2>&1; then "
        "for u in $(systemctl list-units --type=service --state=running --no-legend --plain --no-pager 2>/dev/null "
        f"| awk '{{print $1}}' | head -{MAX_LISTED}); do "
        'f=$(systemctl show -p FragmentPath --value "$u" 2>/dev/null); pkg=""; '
        'if [ -n "$f" ] && command -v dpkg >/dev/null 2>&1; then '
        "pkg=$(dpkg -S \"$f\" 2>/dev/null | head -1 | sed 's/:.*//; s/,.*//'); "
        'elif [ -n "$f" ] && command -v rpm >/dev/null 2>&1; then '
        'pkg=$(rpm -qf --qf "%{NAME}\\n" "$f" 2>/dev/null | head -1); fi; '
        'echo "service: $u pkg=${pkg:-unknown}"; done; fi')
    macos_services = (
        "if command -v brew >/dev/null 2>&1; then "
        "brew services list 2>/dev/null | awk 'NR>1 && $2==\"started\" {print \"service: \" $1 \" pkg=\" $1}' "
        f"| head -{MAX_LISTED}; fi")
    # The package that owns the listening binary, so that a process called "postgres" can protect "postgresql-16".
    # Reading /proc/<pid>/exe of another user's process needs the same privilege as the owners themselves ($S).
    # Two traps seen on a real host: a socket-activated service (sshd, rpcbind) is listed under pid 1, the init system,
    # whose package says nothing about the service, so such a port stays "unknown"; and /proc/<pid>/exe of a binary
    # replaced by an upgrade ends in " (deleted)", which dpkg and rpm cannot find.
    linux_ports = (
        "$S ss -H -ltnp 2>/dev/null | while read -r st rq sq la pa rest; do "
        "p=$(printf '%s' \"$rest\" | sed -n 's/.*users:((\"\\([^\"]*\\)\".*/\\1/p'); "
        "pid=$(printf '%s' \"$rest\" | sed -n 's/.*pid=\\([0-9][0-9]*\\).*/\\1/p'); pkg=\"\"; "
        'if [ "$pid" = 1 ]; then p=""; pid=""; fi; '
        'if [ -n "$pid" ]; then exe=$($S readlink -f "/proc/$pid/exe" 2>/dev/null); exe=${exe% (deleted)}; '
        'if [ -n "$exe" ] && command -v dpkg >/dev/null 2>&1; then '
        "pkg=$(dpkg -S \"$exe\" 2>/dev/null | head -1 | sed 's/:.*//; s/,.*//'); "
        'elif [ -n "$exe" ] && command -v rpm >/dev/null 2>&1; then '
        'pkg=$(rpm -qf --qf "%{NAME}\\n" "$exe" 2>/dev/null | grep -v " " | head -1); fi; fi; '
        f'echo "port: $la proc=${{p:-unknown}} pkg=${{pkg:-unknown}}"; done | head -{MAX_LISTED}')
    macos_ports = (
        "$S lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print \"port: \" $9 \" proc=\" $1}' "
        f"| sort -u | head -{MAX_LISTED}")
    return "\n".join([
        PATH_PREFIX,
        'kernel=$(uname -s); case "$kernel" in Darwin) fam=macos;; Linux) fam=linux;; *) fam=other;; esac',
        'echo "os_family: $fam"',
        'echo "kernel: $(uname -sr)"',
        'echo "arch: $(uname -m)"',
        'if [ "$fam" = macos ]; then echo "os_id: macos"; echo "os_version: $(sw_vers -productVersion 2>/dev/null)"; '
        'else for k in ID VERSION_ID; do v=$(sed -n "s/^$k=//p" /etc/os-release 2>/dev/null | head -1 | tr -d \'"\'); '
        'case $k in ID) echo "os_id: ${v:-unknown}";; *) echo "os_version: ${v:-unknown}";; esac; done; fi',
        'if [ "$fam" = macos ]; then echo "init: launchd"; elif [ -d /run/systemd/system ]; then echo "init: systemd"; '
        'else echo "init: other"; fi',
        'echo "user: $(id -un)"',
        'if [ "$(id -u)" = 0 ]; then echo "root: true"; else echo "root: false"; fi',
        'S=""; if sudo -n true 2>/dev/null; then echo "sudo_nopasswd: true"; S="sudo -n"; '
        'else echo "sudo_nopasswd: false"; fi',
        # Without root or sudo, ss and lsof hide the owners of other users' sockets (lsof also the sockets).
        'if [ "$(id -u)" = 0 ] || [ -n "$S" ]; then echo "ports_owner_visibility: full"; '
        'else echo "ports_owner_visibility: partial"; fi',
        f"for m in {managers}; do p=$(command -v \"$m\" 2>/dev/null) && echo \"manager: $m $p\"; done",
        "echo \"logged_in: $(who 2>/dev/null | awk '{print $1}' | sort -u | tr '\\n' ' ')\"",
        case_by_os(macos_services, linux_services),
        case_by_os(macos_ports, linux_ports),
        f"echo '{FACTS_END}'",
    ])


def parse_host_facts(text: str) -> Dict[str, Any]:
    """The facts of one host_facts run as a dict. `complete` is True only when the last line was reached; the keys of
    REQUIRED_FACTS are present in a complete result. Lines that do not fit the format are ignored."""
    facts: Dict[str, Any] = {"managers": {}, "services": [], "ports": [], "logged_in": [], "complete": False}
    for raw in (text or "").splitlines():
        key, sep, value = raw.partition(": ")
        value = value.strip()
        if not sep and raw.endswith(":"):
            key, value = raw[:-1], ""
        elif not sep:
            continue
        if key == "manager":
            name, _, path = value.partition(" ")
            if name:
                facts["managers"][name] = path
        elif key == "service":
            unit, _, rest = value.partition(" pkg=")
            if unit:
                facts["services"].append({"unit": unit, "package": rest or "unknown"})
        elif key == "port":
            listen, _, rest = value.partition(" proc=")
            proc, _, pkg = rest.partition(" pkg=")
            if listen:
                facts["ports"].append({"listen": listen, "process": proc or "unknown", "package": pkg or "unknown"})
        elif key == "logged_in":
            facts["logged_in"] = value.split()
        elif key in ("sudo_nopasswd", "root"):
            facts[key] = value == "true"
        elif key == "facts_complete":
            facts["complete"] = value == "yes"
        elif key in ("os_family", "os_id", "os_version", "kernel", "arch", "init", "user", "ports_owner_visibility"):
            facts[key] = value
    if facts["complete"] and any(k not in facts for k in REQUIRED_FACTS):
        facts["complete"] = False
    return facts


def facts_dir() -> Path:
    """<data dir>/ops-facts. Creates nothing."""
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "ops-facts"


def _facts_file(host: str) -> Optional[Path]:
    return facts_dir() / f"{host}.json" if isinstance(host, str) and HOST_FILE.fullmatch(host) else None


def store_facts(host: str, facts: Dict[str, Any], now: Optional[float] = None) -> bool:
    """Writes a complete result atomically; False when it is incomplete or the host id is not a safe file name."""
    path = _facts_file(host)
    if path is None or not facts.get("complete"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"host": host, "fetched_at": time.time() if now is None else now, "facts": facts}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".facts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def load_facts(host: str, max_age: float = FACTS_MAX_AGE, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The cached facts of a host if they are complete and at most max_age seconds old, else None."""
    path = _facts_file(host)
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        fetched = float(record["fetched_at"])
        facts = record["facts"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (time.time() if now is None else now) - fetched
    if not isinstance(facts, dict) or not facts.get("complete") or age < 0 or age > max_age:
        return None
    return facts


def package_info_command(args: Dict[str, Any]) -> str:
    """The read-only query of one manager for one name, run on the host."""
    manager, name = args.get("manager"), args.get("name")
    if manager not in INFO_MANAGERS:
        raise ValueError(f"manager must be one of: {', '.join(INFO_MANAGERS)}")
    if not isinstance(name, str) or not NAME_PATTERNS[manager].fullmatch(name):
        raise ValueError(f"name is not a valid {manager} package name (a plain name: no version, URL, path or option)")
    n = shlex.quote(name)
    if manager == "apt":
        body = (f"echo '# policy (installed and candidate)'; apt-cache policy {n} 2>&1 | head -12; "
                f"echo '# depends'; apt-cache depends {n} 2>&1 | head -40; "
                f"echo '# installed packages that depend on it'; apt-cache rdepends --installed {n} 2>&1 | head -40")
        program = "apt-cache"
    elif manager == "dnf":
        # -C: cache only, so the check cannot start a metadata download that outlives its time limit.
        body = (f"echo '# installed'; rpm -q {n} 2>&1 | head -5; "
                f"echo '# info (cached repository data)'; dnf -C -q info {n} 2>&1 | head -30; "
                f"echo '# installed packages that depend on it'; rpm -q --whatrequires {n} 2>&1 | head -40")
        program = "dnf"
    elif manager == "brew":
        body = ("export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1; "
                f"echo '# info'; brew info {n} 2>&1 | head -14; "
                f"echo '# dependencies'; brew deps {n} 2>&1 | head -40; "
                f"echo '# installed formulae that depend on it'; brew uses --installed {n} 2>&1 | head -40")
        program = "brew"
    else:
        body = ("export NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false; "
                f"echo '# registry'; npm view {n} name version deprecated dependencies 2>&1 | head -40; "
                f"echo '# installed globally'; npm ls -g --depth=0 {n} 2>&1 | head -10")
        program = "npm"
    return (PATH_PREFIX + f"command -v {program} >/dev/null 2>&1 || {{ echo '{manager} is not available on this host'; "
            f"exit 3; }}; echo '# manager: {manager}'; " + body)


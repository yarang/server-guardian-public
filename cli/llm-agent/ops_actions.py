"""
Typed cleanup actions of ops projects (docs/design/server-ops-project.md, chapter 6, P3 and P4).

Next to the free-form jev_mcp:server_remediation, an ops judge can propose actions from this catalog. Each one
is a fixed command with validated arguments and comes with a measurement:

- preview: measured when the action is proposed (stored with it) and again right before it runs. When what it
  would delete grew a lot since the proposal, or names something the proposal did not, the action is not run
  and becomes STALE (drift check).
- verify: the same measurement after the run, so the result shows before, after and what was freed (or, for a
  service restart, whether the unit is active again).

A measurement is {"bytes", "items", "text", "error"}: bytes is the amount the action would delete (None when it
does not apply or cannot be read), items the things it would act on (paths, packages; None when not listed).

Low risk (P3): dangling docker images and build cache, the journal down to a size, the apt cache.
Medium risk (P4): stopped containers and unused images, removing paths (ops_paths deny rules), emptying log files,
purging old kernels, restarting a service. All run through the hub agent API like every other server command.
"""

import re
import shlex
from typing import Any, Dict, List, Optional, Sequence, Tuple

import command_sets
import ops_facts
import ops_packages
import ops_paths

PREFIX = "ops:"
LOW_SCOPES = ("dangling_images", "build_cache")
SCOPES = LOW_SCOPES + ("stopped_containers", "unused_images")
MAX_SIZE = re.compile(r"^([1-9][0-9]{0,5})([MG])$")
MIN_JOURNAL_BYTES = 100 * 1024 ** 2
MAX_PATHS = 50
MEASURE_TIMEOUT = 60
PACKAGE_MEASURE_TIMEOUT = 120  # a simulation may query a registry or refresh repository metadata
RUN_TIMEOUT = 600
# Drift: the amount to delete may grow by half since the proposal, and at least by this much, before the run is refused.
DRIFT_FACTOR = 1.5
DRIFT_FLOOR = 256 * 1024 ** 2
APT_ARCHIVES = "/var/cache/apt/archives"
UNIT = re.compile(r"^[A-Za-z0-9@._:-]{1,96}\.service$")
OTHER_UNIT_TYPE = re.compile(r"\.(socket|timer|mount|automount|target|path|slice|scope|device|swap)$")
# Units whose restart can cut the hub off from the host or take the host's own management down.
PROTECTED_UNITS = ("ssh.service", "sshd.service", "tailscaled.service", "dbus.service", "dbus-broker.service",
                   "systemd-*", "network*", "NetworkManager*", "wpa_supplicant*", "server-guardian*", "guardian-*",
                   "getty@*", "serial-getty@*", "cloud-init*", "polkit.service", "user@*",
                   # the tailnet control server: the hub reaches the fleet over the mesh it coordinates
                   "headscale*")
KERNEL_PACKAGE = re.compile(r"^linux-(image|image-unsigned|headers|modules|modules-extra)-([0-9][A-Za-z0-9.+~-]*)$")

# Passwordless sudo when the remote user has it, else plain (the command then fails where root is needed).
SUDO = 'S=""; if sudo -n true 2>/dev/null; then S="sudo -n"; fi; '
# docker directly when the remote user may, else passwordless sudo. A host without docker answers NO_DOCKER, which
# measure() turns into an error, so the action cannot run there.
NO_DOCKER = "docker is not installed on this host"
DOCKER = (f"command -v docker >/dev/null 2>&1 || {{ echo '{NO_DOCKER}'; exit 3; }}; "
          'if docker info >/dev/null 2>&1; then D=docker; else D="sudo -n docker"; fi; ')

PATH_LIST = "list"

# tool -> (parameters as in mcp_client.TOOLS, description for the judge)
ACTIONS: Dict[str, Tuple[Dict[str, str], str]] = {
    "ops:docker_prune": (
        {"target_host": "string", "scope": "string"},
        "Remove unused docker data on one host. scope \"dangling_images\" (untagged images no container uses) and "
        "\"build_cache\" (dangling build cache) are low risk; \"stopped_containers\" (all exited or created containers) "
        "and \"unused_images\" (every image no container uses, tagged ones too) are medium risk. Volumes are never touched",
    ),
    "ops:journal_vacuum": (
        {"target_host": "string", "max_size": "string"},
        "Shrink the systemd journal on one host to max_size (such as \"500M\" or \"2G\", at least 100M) by deleting "
        "its oldest archived files (low risk)",
    ),
    "ops:apt_clean": (
        {"target_host": "string"},
        "Empty the apt package cache (/var/cache/apt/archives) on one host; installed packages are not affected (low risk)",
    ),
    "ops:remove_paths": (
        {"target_host": "string", "paths": "list", "older_than_days": "integer?"},
        "Delete up to 50 absolute paths (files or directories, recursively, never across a mount point) on one host; "
        "with older_than_days only the files below them older than that many days. System directories, /etc, /usr, "
        "/var/lib, home directories, credential and Guardian directories and anything the project denies are refused. "
        "Name each path exactly as a server check showed it; globs are not accepted (medium risk)",
    ),
    "ops:truncate_logs": (
        {"target_host": "string", "paths": "list"},
        "Empty up to 50 regular files under /var/log on one host (truncate to 0 bytes, the file stays for the process "
        "writing it) (medium risk)",
    ),
    "ops:remove_old_kernels": (
        {"target_host": "string"},
        "Purge the installed kernel packages other than the running kernel and the newest one (medium risk)",
    ),
    "ops:restart_service": (
        {"target_host": "string", "unit": "string"},
        "Restart one systemd service unit (name.service) on one host; ssh, tailscale, headscale, networking, systemd "
        "and Guardian units are refused (medium risk)",
    ),
    "ops:package_install": (
        {"target_host": "string", "manager": "string", "packages": "list", "ignore_scripts": "boolean?"},
        "Install packages on one host with its package manager. manager is \"auto\" (the host's system manager: apt, "
        "dnf, zypper, apk, pacman or brew) or one of apt, dnf, zypper, apk, pacman, brew, npm, bun, pnpm, yarn (global "
        "installs), pipx, uv (Python command-line tools; one package per action). packages is a list of {\"name\": ..., "
        "\"version\": ...?}; names are plain registry or repository names (no URL, git, tarball, tap or option), "
        "versions are exact (pacman cannot pin). System managers need root or passwordless sudo (the host_facts check "
        "says). npm, bun, pnpm and yarn run package scripts as usual; ignore_scripts true installs without them. "
        "Medium risk; high for bun, pnpm, yarn, pipx and uv (their preview is only the request) or when the "
        "simulation would also remove packages. Read host_facts and package_info first",
    ),
    "ops:package_remove": (
        {"target_host": "string", "manager": "string", "packages": "list", "purge": "boolean?"},
        "Remove packages from one host (apt purge with purge true, which also deletes configuration). Same manager and "
        "name rules as ops:package_install. Packages that keep the host reachable or running (ssh, sudo, systemd, libc, "
        "the kernel, the package manager, tailscale, Guardian) are protected and refused, also when a simulation shows "
        "they would be removed as dependents. Medium risk; high for more than five packages or a simulation that "
        "removes more than five. No automatic rollback",
    ),
    "ops:run_command_set": (
        {"target_host": "string", "set": "string", "params": "object"},
        "Run a stored command set (a reviewed script with typed parameters) on one host; params is an object of the "
        "set's declared parameter values. The set declares its own risk, a read-only check shown at proposal and "
        "whether the run can be undone; a set that cannot be undone, or is high risk, always needs a person",
    ),
    "ops:run_script": (
        {"target_host": "string", "script": "string", "reason": "string"},
        "Run a bash script of at most 4 KiB on one host (high risk, never undoable: a person approves it, except on "
        "the host groups where the project lets the critics' vote release scripts). Use a stored command set when one "
        "fits. A best-effort check refuses the obvious disasters (formatting disks, deleting the root or a home "
        "directory, stopping ssh, tailscale or Guardian, running downloaded code or text built and run on the spot, "
        "shutting the host down) and the commands the operator listed; it is not a sandbox, the critics and the "
        "person who approves read the script",
    ),
}
# Batch steps (docs/design/batch-execution.md): offered only when a project lists them in policy.allowed_actions.
BATCH_TOOLS = command_sets.TOOLS


def is_catalog(tool: str) -> bool:
    return tool in ACTIONS


def risk(tool: str, args: Optional[Dict[str, Any]] = None, preview: Optional[Dict[str, Any]] = None) -> str:
    """The risk of one action: docker_prune depends on its scope, package actions on the manager, the flags and
    the stored simulation (preview), the P4 actions are medium, a command set has the risk it declares and a script
    is high."""
    if tool in BATCH_TOOLS:
        return command_sets.risk(tool, args)
    if tool in ops_packages.TOOLS:
        return ops_packages.risk(tool, args or {}, preview)
    if tool in ("ops:journal_vacuum", "ops:apt_clean"):
        return "low"
    if tool == "ops:docker_prune":
        return "low" if (args or {}).get("scope") in LOW_SCOPES else "medium"
    return "medium"


def _journal_bytes(max_size: str) -> int:
    number, unit = MAX_SIZE.match(max_size).groups()
    return int(number) * 1024 ** (2 if unit == "M" else 3)


def _paths(args: Dict[str, Any], deny: Sequence[str], under: Optional[str] = None) -> List[str]:
    paths = args.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
        raise ValueError(f"paths must list 1 to {MAX_PATHS} absolute paths")
    out = []
    for path in paths:
        reason = ops_paths.refusal(path, deny)
        if reason:
            raise ValueError(reason)
        if under and not path.startswith(under):
            raise ValueError(f"{path} is not under {under}")
        out.append(path)
    return list(dict.fromkeys(out))


def validate(tool: str, args: Dict[str, Any], deny: Sequence[str] = (),
             deny_commands: Sequence[str] = ()) -> Dict[str, Any]:
    """The tool's own argument rules (types are checked by mcp_client); deny is the project's deny_paths and
    deny_commands its command patterns (batch tools only). Raises ValueError."""
    if tool in BATCH_TOOLS:
        return command_sets.validate(tool, args, deny, deny_commands)
    if tool in ops_packages.TOOLS:
        return ops_packages.shape(tool, args)
    if tool == "ops:docker_prune" and args.get("scope") not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    if tool == "ops:journal_vacuum":
        size = str(args.get("max_size", "")).strip().upper()
        if not MAX_SIZE.fullmatch(size):
            raise ValueError("max_size must be a whole number of megabytes or gigabytes, such as 500M or 2G")
        if _journal_bytes(size) < MIN_JOURNAL_BYTES:
            raise ValueError("max_size must be at least 100M")
        return {**args, "max_size": size}
    if tool == "ops:remove_paths":
        days = args.get("older_than_days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650):
            raise ValueError("older_than_days must be an integer from 1 to 3650")
        return {**args, "paths": _paths(args, deny)}
    if tool == "ops:truncate_logs":
        paths = _paths(args, deny, under="/var/log/")
        if any(p.count("/") < 3 for p in paths):
            raise ValueError("name log files below /var/log, such as /var/log/syslog.1")
        return {**args, "paths": paths}
    if tool == "ops:restart_service":
        unit = str(args.get("unit", "")).strip()
        if OTHER_UNIT_TYPE.search(unit):
            raise ValueError("only service units can be restarted (name.service)")
        if UNIT.fullmatch(unit + ".service") and not unit.endswith(".service"):
            unit += ".service"
        if not UNIT.fullmatch(unit) or unit.startswith("-"):
            raise ValueError("unit must be a service unit name such as nginx.service")
        import fnmatch

        if any(fnmatch.fnmatchcase(unit, pattern) for pattern in PROTECTED_UNITS):
            raise ValueError(f"{unit} may not be restarted by an action (it keeps the host reachable or managed)")
        return {**args, "unit": unit}
    return dict(args)


def _quoted(paths: Sequence[str]) -> str:
    return " ".join(shlex.quote(p) for p in paths)


def _find_filter(args: Dict[str, Any]) -> str:
    days = args.get("older_than_days")
    return f" -type f -mtime +{int(days)}" if days else ""


def measure_command(tool: str, args: Dict[str, Any]) -> str:
    """A read-only command whose output parse_measure() turns into bytes and items."""
    if tool in ops_packages.TOOLS:
        return ops_packages.measure_command(tool, args)
    if tool == "ops:docker_prune":
        scope = args["scope"]
        if scope == "dangling_images":
            return DOCKER + "$D images -f dangling=true --format '{{.ID}} {{.Size}}' 2>&1"
        if scope == "stopped_containers":
            return DOCKER + ("$D ps -a --filter status=exited --filter status=created "
                             "--format '{{.ID}} {{.Size}} {{.Names}}' 2>&1")
        return DOCKER + "$D system df --format '{{.Type}}|{{.Reclaimable}}' 2>&1"
    if tool == "ops:journal_vacuum":
        return "sudo -n journalctl --disk-usage 2>/dev/null || journalctl --disk-usage 2>&1"
    if tool == "ops:apt_clean":
        # partial/ is root-only: measure with sudo when possible and drop du's permission warnings.
        return f"sudo -n du -sk {APT_ARCHIVES} 2>/dev/null || du -sk {APT_ARCHIVES} 2>/dev/null"
    if tool == "ops:remove_paths":
        # One line per path: bytes, entries, newest mtime, path ("missing" when it does not exist).
        return SUDO + "for p in " + _quoted(args["paths"]) + "; do if [ -e \"$p\" ] || [ -L \"$p\" ]; then " \
            "$S find \"$p\" -xdev" + _find_filter(args) + " -printf '%s %T@\\n' 2>/dev/null | " \
            "awk -v p=\"$p\" '{s+=$1; n++; if ($2>m) m=$2} END {printf \"%d\\t%d\\t%d\\t%s\\n\", s, n, m, p}'; " \
            "else printf 'missing\\t%s\\n' \"$p\"; fi; done"
    if tool == "ops:truncate_logs":
        return SUDO + "for p in " + _quoted(args["paths"]) + "; do if [ -f \"$p\" ] && [ ! -L \"$p\" ]; then " \
            "printf '%s\\t%s\\n' \"$($S stat -c %s -- \"$p\")\" \"$p\"; else printf 'skip\\t%s\\n' \"$p\"; fi; done"
    if tool == "ops:remove_old_kernels":
        return ("echo \"running $(uname -r)\"; dpkg-query -W -f '${Package} ${Status}\\n' 'linux-image-*' "
                "'linux-headers-*' 'linux-modules-*' 2>/dev/null | awk '$NF == \"installed\" {print $1}'")
    if tool == "ops:restart_service":
        unit = shlex.quote(args["unit"])
        return (f"systemctl is-active -- {unit} 2>&1; systemctl show -p ActiveEnterTimestamp -p NRestarts -- {unit} "
                f"2>&1; systemctl status --no-pager -n 5 -- {unit} 2>&1 | tail -6")
    raise ValueError(f"unknown action {tool}")


def run_command(tool: str, args: Dict[str, Any], before: Dict[str, Any]) -> str:
    """The command that acts; before is the measurement just taken (remove_old_kernels purges exactly its items)."""
    if tool in ops_packages.TOOLS:
        return ops_packages.run_command(tool, args)
    if tool == "ops:docker_prune":
        prune = {"dangling_images": "image prune -f", "build_cache": "builder prune -f",
                 "stopped_containers": "container prune -f", "unused_images": "image prune -a -f"}[args["scope"]]
        return DOCKER + f"$D {prune}"
    if tool == "ops:journal_vacuum":
        return f"sudo -n journalctl --vacuum-size={args['max_size']}"
    if tool == "ops:apt_clean":
        return "sudo -n apt-get clean"
    if tool == "ops:remove_paths":
        refuse_mounts = ("for p in " + _quoted(args["paths"]) + "; do if mountpoint -q -- \"$p\" 2>/dev/null; then "
                         "echo \"refusing: $p is a mount point\"; exit 4; fi; done; ")
        if args.get("older_than_days"):
            return SUDO + refuse_mounts + "for p in " + _quoted(args["paths"]) + "; do [ -e \"$p\" ] && " \
                "$S find \"$p\" -xdev" + _find_filter(args) + " -delete; done; true"
        return SUDO + refuse_mounts + "$S rm -rf --one-file-system -- " + _quoted(args["paths"])
    if tool == "ops:truncate_logs":
        return SUDO + "for p in " + _quoted(args["paths"]) + "; do if [ -f \"$p\" ] && [ ! -L \"$p\" ]; then " \
            "$S truncate -s 0 -- \"$p\"; else echo \"skipped (not a regular file): $p\"; fi; done"
    if tool == "ops:remove_old_kernels":
        packages = [p for p in (before.get("items") or []) if KERNEL_PACKAGE.match(p)]
        if not packages:
            return "echo 'no old kernel packages to remove'"
        return "sudo -n apt-get purge -y -- " + _quoted(packages)
    if tool == "ops:restart_service":
        return f"sudo -n systemctl restart -- {shlex.quote(args['unit'])}"
    raise ValueError(f"unknown action {tool}")


_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([kKMGT]?i?B|[KMGT])\b")
_DECIMAL = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}
_BINARY = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4,
           "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(text: str) -> Optional[int]:
    """Bytes of the first size in text: docker's decimal units (12.5MB, 1.2GB, 0B) and journalctl's binary ones
    (512.0M, 1.2G)."""
    match = _SIZE.search(text or "")
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2).lower()
    factor = _DECIMAL.get(unit) or _BINARY.get(unit)
    return int(number * factor) if factor else None


def _kernel_version_key(version: str) -> Tuple:
    """Numeric parts compare as numbers; a word part (the flavour) sorts after any number at the same position."""
    return tuple((0, int(n), "") if n.isdigit() else (1, 0, n) for n in re.split(r"[.+~-]", version))


def old_kernel_packages(text: str) -> Optional[List[str]]:
    """Installed kernel packages of versions other than the running one and the newest one."""
    lines = (text or "").splitlines()
    if not lines or not lines[0].startswith("running "):
        return None
    running = lines[0].split(None, 1)[1].strip()
    by_version: Dict[str, List[str]] = {}
    for line in lines[1:]:
        match = KERNEL_PACKAGE.match(line.strip())
        if match:
            by_version.setdefault(match.group(2), []).append(match.group(0))
    if not by_version:
        return []
    newest = max(by_version, key=_kernel_version_key)
    keep = {running, newest}
    # A headers package may be named without the flavour suffix (linux-headers-6.8.0-1015 next to -oracle).
    keep |= {v for v in by_version if any(k.startswith(v + "-") for k in keep)}
    return sorted(p for v, ps in by_version.items() if v not in keep for p in ps)


def parse_measure(tool: str, args: Dict[str, Any], stdout: str) -> Tuple[Optional[int], Optional[List[str]]]:
    """(bytes, items) from the measure command's output; None where it does not apply or cannot be read."""
    text = stdout or ""
    if tool == "ops:docker_prune" and args["scope"] in ("dangling_images", "stopped_containers"):
        lines = [line.split() for line in text.splitlines() if line.strip()]
        if any(len(parts) < 2 for parts in lines):
            return None, None  # an error message, not the list
        sizes = [parse_size(parts[1]) for parts in lines]
        if any(s is None for s in sizes):
            return None, None
        return sum(sizes), [parts[0] for parts in lines]
    if tool == "ops:docker_prune":
        wanted = "build cache" if args["scope"] == "build_cache" else "images"
        for line in text.splitlines():
            kind, sep, reclaimable = line.partition("|")
            if sep and kind.strip().lower() == wanted:
                return parse_size(reclaimable), None
        return None, None
    if tool == "ops:journal_vacuum":
        usage = parse_size(text.split("take up", 1)[1]) if "take up" in text else None
        return (None if usage is None else max(0, usage - _journal_bytes(args["max_size"]))), None
    if tool == "ops:apt_clean":
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == APT_ARCHIVES and parts[0].isdigit():
                return int(parts[0]) * 1024, None
        return None, None
    if tool in ("ops:remove_paths", "ops:truncate_logs"):
        total, present = 0, []
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[0].isdigit() and fields[-1].startswith("/"):
                total += int(fields[0])
                present.append(fields[-1])
        return total, present
    if tool == "ops:remove_old_kernels":
        return None, old_kernel_packages(text)
    return None, None


def measure_bytes(tool: str, args: Dict[str, Any], stdout: str) -> Optional[int]:
    return parse_measure(tool, args, stdout)[0]


def drift(stored: Optional[Dict[str, Any]], now: Dict[str, Any]) -> Optional[str]:
    """Why the action must not run because what it would delete grew too much, or names something the proposal did
    not, else None. Without a stored measurement (it failed at proposal time) there is nothing to compare."""
    stored = stored or {}
    if stored.get("script_sha") and now.get("script_sha") and stored["script_sha"] != now["script_sha"]:
        return "the command set was edited since the proposal; debate again on the current text"
    if now.get("refusal"):
        return f"{now['refusal']}; debate again on the current state"
    before, after = stored.get("bytes"), now.get("bytes")
    if before is not None and after is not None and after > max(before * DRIFT_FACTOR, before + DRIFT_FLOOR):
        return (f"it would now delete {human(after)}, {human(before)} when it was proposed; "
                "debate again on the current state")
    if isinstance(stored.get("items"), list) and isinstance(now.get("items"), list):
        new = [i for i in now["items"] if i not in stored["items"]]
        if new:
            return (f"it would now also act on {', '.join(new[:5])}{' …' if len(new) > 5 else ''}, which the proposal "
                    "did not show; debate again on the current state")
    return None


def human(size: Optional[int]) -> str:
    if size is None:
        return "unknown"
    for unit, factor in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} B"


def _send(agent: Any, target: str, command: str, timeout: int, labels: Dict[str, str]) -> Dict[str, Any]:
    if target == "local":
        return agent.execute_command(command, timeout_sec=timeout, labels=labels)
    return agent.execute_remote_ssh(target, command, timeout_sec=timeout, labels=labels)


def _labels(tool: str, project: str, action_id: Optional[int], preview: bool = False,
            op: Optional[str] = None) -> Dict[str, str]:
    labels = {"source": "action", "op": (op or tool[len(PREFIX):]) + (":preview" if preview else ""), "project": project}
    if action_id is not None:
        labels["action"] = str(action_id)
    return labels


def host_facts(agent: Any, target: str, project: str) -> Optional[Dict[str, Any]]:
    """The facts of a host: the cached result when recent, else read live (and cached). None when the host did not
    answer completely. The validator of package actions uses them."""
    cached = ops_facts.load_facts(target)
    if cached:
        return cached
    labels = {"source": "ops_check", "op": "host_facts", "project": project}
    try:
        res = _send(agent, target, ops_facts.host_facts_command(), ops_facts.FACTS_TIMEOUT, labels)
    except (RuntimeError, ConnectionError, OSError):
        return None
    if res.get("exit_code", 1) != 0:
        return None
    facts = ops_facts.parse_host_facts(str(res.get("stdout") or ""))
    if not facts.get("complete"):
        return None
    ops_facts.store_facts(target, facts)
    return facts


def measure(agent: Any, target: str, tool: str, args: Dict[str, Any], project: str,
            action_id: Optional[int] = None, protection: Any = None) -> Dict[str, Any]:
    """{"bytes", "items", "text", "error"}: the measurement on the host (error when it could not be taken).
    protection (package_roles.Protection) adds the role, running-service and project patterns to the refusals of a
    package simulation and is recorded as "protected" for the approval screen."""
    if tool in BATCH_TOOLS:
        return _measure_batch(agent, target, tool, args, project, action_id)
    labels = _labels(tool, project, action_id, preview=True)
    packages = tool in ops_packages.TOOLS
    try:
        res = _send(agent, target, measure_command(tool, args),
                    PACKAGE_MEASURE_TIMEOUT if packages else MEASURE_TIMEOUT, labels)
    except (RuntimeError, ConnectionError, OSError) as exc:
        return {"bytes": None, "items": None, "text": "", "error": f"could not measure: {exc}"[:500]}
    code = res.get("exit_code", 1)
    out = str(res.get("stdout") or "")
    if code in (255, -1):
        return {"bytes": None, "items": None, "text": out[-4000:], "error": f"host not reached (exit {code})"}
    if packages:
        # Parsed before the text is cut: a long simulation must not lose its items.
        found = ops_packages.assess(tool, args, out, protection or ())
        if protection is not None:
            found["protected"] = protection.describe(args["manager"])
            blind = protection.blind_for(args["manager"])
            if blind:
                found["blind"] = blind
        return {"bytes": None, "text": out[-4000:], **found}
    if NO_DOCKER in out:
        return {"bytes": None, "items": None, "text": out[-4000:], "error": NO_DOCKER}
    if tool == "ops:restart_service" and "could not be found" in out:
        # Seen on a fleet node: restarting a unit the host does not have would only fail after approval.
        return {"bytes": None, "items": None, "text": out[-4000:], "error": f"{args['unit']} does not exist on this host"}
    size, items = parse_measure(tool, args, out)
    return {"bytes": size, "items": items, "text": out[-4000:], "error": None}


def _measure_batch(agent: Any, target: str, tool: str, args: Dict[str, Any], project: str,
                   action_id: Optional[int], plan: Optional["command_sets.Plan"] = None) -> Dict[str, Any]:
    """The preview of a batch step: the hash of what would run (compared at the run, so an edited set is STALE) and,
    for a set that has a check, the check's output from the host. A script is never run to preview it."""
    try:
        plan = plan or command_sets.Plan(tool, args)
    except ValueError as exc:
        return {"bytes": None, "items": None, "text": "", "error": str(exc)[:500]}
    found: Dict[str, Any] = {"bytes": None, "items": None, "text": "", "error": None, "script_sha": plan.sha,
                             "script": plan.script, "check": plan.check}  # what would run, kept after the set is edited
    if tool == command_sets.TOOL_SCRIPT:
        found["text"] = f"script of {len(plan.script.encode())} bytes; it is not run to preview it"
        return found
    if not plan.check:
        found["text"] = "this command set has no check"
        return found
    try:
        res = _send(agent, target, plan.check_command(), command_sets.CHECK_TIMEOUT,
                    _labels(tool, project, action_id, preview=True, op=plan.label))
    except (RuntimeError, ConnectionError, OSError) as exc:
        return {**found, "error": f"could not run the check: {exc}"[:500]}
    code, out = res.get("exit_code", 1), str(res.get("stdout") or "")
    found["text"] = out[-4000:]
    if code in (255, -1):
        found["error"] = f"host not reached (exit {code})"
    elif code != 0:
        found["error"] = f"the check failed (exit {code}): {str(res.get('stderr') or '')[-300:]}".strip()
    return found


def _execute_batch(agent: Any, target: str, tool: str, args: Dict[str, Any], project: str,
                   action_id: Optional[int], expected_sha: Optional[str] = None) -> Dict[str, Any]:
    """Check, run, check again (a script has no check, so it only runs). One Plan serves all three, so an edit of the
    set while the check runs cannot change what is run."""
    try:
        plan = command_sets.Plan(tool, args)
    except ValueError as exc:
        return {"status": "FAILED", "target_host": target, "error": f"cannot measure before running: {exc}"[:500]}
    if expected_sha and plan.sha != expected_sha:
        # The set was edited after the step was checked: what would run is not what was approved or voted on.
        return {"status": "FAILED", "target_host": target,
                "error": "the command set changed after the step was approved; it was not run"}
    before = _measure_batch(agent, target, tool, args, project, action_id, plan)
    if before.get("error"):
        return {"status": "FAILED", "target_host": target, "error": f"cannot measure before running: {before['error']}"}
    res = _send(agent, target, plan.run_command(), plan.timeout, _labels(tool, project, action_id, op=plan.label))
    code = res.get("exit_code", 1)
    verify: Dict[str, Any] = {"ok": code == 0, "script_sha": plan.sha, "script": plan.script, "check": plan.check}
    if plan.check:
        after = _measure_batch(agent, target, tool, args, project, action_id, plan)
        verify.update(before=before["text"], after=after["text"])
    return {"status": "SUCCESS" if code == 0 else "FAILED", "target_host": target, "exit_code": code,
            "stdout": str(res.get("stdout") or "")[-4000:], "stderr": str(res.get("stderr") or "")[-4000:],
            "verify": verify}


def _verify(tool: str, before: Dict[str, Any], after: Dict[str, Any], code: int) -> Dict[str, Any]:
    if tool in ops_packages.TOOLS:
        raise ValueError("package actions are verified by execute() with their arguments")
    b, a = before.get("bytes"), after.get("bytes")
    verify: Dict[str, Any] = {"before": b, "after": a, "freed": (b - a) if b is not None and a is not None else None}
    if tool == "ops:restart_service":
        active = (after.get("text") or "").split("\n", 1)[0].strip()
        verify.update(active=active, ok=code == 0 and active == "active")
    elif tool == "ops:remove_old_kernels":
        left = [p for p in (before.get("items") or []) if p in (after.get("items") or [])]
        verify.update(removed=[p for p in (before.get("items") or []) if p not in left], left=left,
                      ok=code == 0 and not left and after.get("items") is not None)
    else:
        verify["ok"] = code == 0 and a is not None and (b is None or a <= b)
    return verify


def execute(agent: Any, target: str, tool: str, args: Dict[str, Any], project: str,
            action_id: Optional[int] = None, protection: Any = None,
            expected_sha: Optional[str] = None) -> Dict[str, Any]:
    """Measures, runs and measures again; the result carries the run's output and verify. action_id labels the
    hub's audit lines of all three commands. expected_sha (batch tools): the script_sha the step was approved with."""
    if tool in BATCH_TOOLS:
        return _execute_batch(agent, target, tool, args, project, action_id, expected_sha)
    before = measure(agent, target, tool, args, project, action_id, protection)
    if before.get("error"):
        return {"status": "FAILED", "target_host": target, "error": f"cannot measure before running: {before['error']}"}
    if before.get("refusal"):
        return {"status": "FAILED", "target_host": target, "error": before["refusal"]}
    res = _send(agent, target, run_command(tool, args, before), RUN_TIMEOUT, _labels(tool, project, action_id))
    code = res.get("exit_code", 1)
    after = measure(agent, target, tool, args, project, action_id, protection)
    if tool in ops_packages.TOOLS:
        verify = ops_packages.verify(tool, args, before, after, code)
        return {"status": "SUCCESS" if code == 0 and verify.get("ok") else "FAILED", "target_host": target,
                "exit_code": code, "stdout": str(res.get("stdout") or "")[-4000:],
                "stderr": str(res.get("stderr") or "")[-4000:], "verify": verify}
    return {"status": "SUCCESS" if code == 0 else "FAILED", "target_host": target, "exit_code": code,
            "stdout": str(res.get("stdout") or "")[-4000:], "stderr": str(res.get("stderr") or "")[-4000:],
            "verify": _verify(tool, before, after, code)}

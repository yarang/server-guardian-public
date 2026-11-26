"""The sandbox layer of open resident agents (docs/design/resident-open-tools.md, O2): node helpers.

An open agent's turns run inside the O1 sandbox: the daemon starts them through the fixed sudo
wrapper as gsa-<name> in a transient systemd unit, and the root-installed launcher (C2) runs the
pinned provider CLI copy inside it. This module owns everything of that contract the node touches:

- capability(): the hello fields of C5 (open_tools, sandbox_version);
- open_problem(): the ordered reasons a turn of an open agent must not run at all (C6/C7); a
  refused turn never falls back to the closed or unsandboxed path;
- stop() and turn_command(): the two wrapper invocations (C4 and the turn start);
- launch_input(): the launcher's stdin JSON (C2);
- install_mcp_copy(), remove_stale_sockets() and clear_sock_dir(): the daemon's own files in the
  agent's socket directory (C7), each written or removed only after an lstat check - never rmtree;
  every written directory and file carries the agent's group explicitly (the setgid parent alone
  can lose the bit) and a wrong group is repaired;
- write_task_bundle() and remove_task_bundle(): the task worktree bundles of O3, git bundles of a
  stored snapshot under tasks/ in the same socket directory, written the same careful way.

Nothing here runs a provider CLI or reads a credential. Every environment touchpoint (the paths,
the root uid, the gsa-<name> user and group lookups, the daemon's supplementary groups) lives in
SandboxPaths so tests can inject it; production uses DEFAULT_PATHS and never an environment
variable. Standard library only; shutil is deliberately not imported because no path here may
ever be removed with rmtree.
"""

import grp
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

WRAPPER = "/usr/local/sbin/guardian-sandbox-run"
LAUNCHER = "/usr/local/lib/guardian-sandbox/launch.py"
INSTALLED = "/etc/guardian-sandbox/installed.json"
SOCK_ROOT = "/var/lib/guardian-sandbox/sock"
HOME_ROOT = "/home"

OPEN_PROVIDERS = ("claude", "agy", "codex")  # C8 + PD1: codex allowed under tools: "open"
# The wrapper's own agent-name rule (deploy/sandbox/guardian-sandbox-run.sh); the resident name is
# the part before the '@' of the address.
OPEN_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
# The CLI version shape of C2 (the launcher refuses anything else).
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")
# A worker code version (remote_agents.code_version()) names an MCP copy directory.
WORKER_VERSION_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
STOP_TIMEOUT = 30.0  # C4: the wrapper waits for every unit of the agent to end
WRAPPER_SLACK = 30   # the wrapper's RuntimeMaxSec rides this far above the turn timeout

# O3: a task's worktree bundle file name inside the agent's socket directory; the task id becomes a
# file name, so nothing but this shape may ever reach task_bundle_path / remove_task_bundle.
BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.bundle$")
TASK_BUNDLE_TIMEOUT = 120.0  # building one bundle of a stored snapshot (repo_snapshot.IMPORT_TIMEOUT's shape)
BUNDLE_REF = "refs/heads/snapshot"  # the one ref a task bundle carries (repo_snapshot.SNAPSHOT_REF)

# The MCP import closure the sandbox needs (C7): the two servers plus every repository module they
# import, directly or lazily. test_resident_open keeps this list closed against the real imports.
MCP_COPY_MODULES = ("mailbox_mcp", "resident_repo_mcp", "mcp_stdio", "repo_mcp", "repo_tools",
                    "code_context", "code_index", "code_select", "code_wiki", "env_policy",
                    "evidence", "go_facts", "go_ops", "repo_map")

# The M0 table's provider versions (configs/resident-providers.json): a worker ships no configs/,
# so the constants are the pin there; expected_cli_versions() prefers the table when this tree has
# one (test_resident_open keeps the two equal).
CLAUDE_VERSION = "2.1.301"
AGY_VERSION = "1.5.12"
CODEX_VERSION = "0.163.0"

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)



def _grp_gid(name: str) -> Optional[int]:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


@dataclass
class SandboxPaths:
    """Every place the sandbox contract touches the node. The defaults are the production paths
    and the real user/group database; tests inject a temp layout and their own uid."""

    wrapper: Path = Path(WRAPPER)
    launcher: Path = Path(LAUNCHER)
    installed: Path = Path(INSTALLED)
    sock_root: Path = Path(SOCK_ROOT)
    home_root: Path = Path(HOME_ROOT)
    root_uid: int = 0
    daemon_uid: Callable[[], int] = os.getuid
    agent_gid: Callable[[str], Optional[int]] = _grp_gid
    member_gids: Callable[[], List[int]] = os.getgroups

    def agent_user(self, name: str) -> str:
        return f"gsa-{name}"

    def agent_group(self, name: str) -> str:
        return f"gsa-{name}"

    def sock_dir(self, name: str) -> Path:
        return self.sock_root / name

    def home(self, name: str) -> Path:
        return self.home_root / f"gsa-{name}"

    def workdir(self, name: str) -> Path:
        return self.home(name) / "work"


DEFAULT_PATHS = SandboxPaths()


def expected_cli_versions() -> Dict[str, str]:
    """provider -> the M0 table's version the sandbox CLI copy must match. The repository table
    wins when this tree has configs/ (the hub checkout); a worker tree ships none, so the module
    constants stand in there (test_resident_open keeps the two equal)."""
    versions = {"claude": CLAUDE_VERSION, "agy": AGY_VERSION, "codex": CODEX_VERSION}
    path = Path(__file__).resolve().parent.parent.parent / "configs" / "resident-providers.json"
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
        providers = table.get("providers") if isinstance(table, dict) else None
        if isinstance(providers, dict):
            for provider in versions:
                entry = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
                version = entry.get("version")
                if isinstance(version, str) and version:
                    versions[provider] = version
    except (OSError, ValueError):
        pass
    return versions


# --- small stat helpers ---------------------------------------------------------------------------


def _lstat(path: Path) -> Optional["os.stat_result"]:
    try:
        return path.lstat()
    except OSError:
        return None


def _root_regular_file(path: Path, root_uid: int) -> bool:
    """True only for an existing regular file (a symlink is not one) owned by root."""
    info = _lstat(path)
    return info is not None and stat.S_ISREG(info.st_mode) and info.st_uid == root_uid


def _read_installed(paths: SandboxPaths) -> Optional[Dict[str, Any]]:
    """installed.json as a dict, or None when it is unreadable or not a JSON object."""
    try:
        with open(paths.installed, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unlink_after_lstat(path: Path) -> None:
    """Unlinks one entry, but only after lstat saw it (a symlink is unlinked, never followed)."""
    if _lstat(path) is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _remove_dir_by_hand(path: Path) -> None:
    """Removes a directory tree the slow way: every entry is unlinked only after an lstat check,
    subdirectories recurse first and an empty directory is rmdir'd. Whatever cannot be removed
    stays behind; nothing is ever followed or forced (no rmtree anywhere in this module)."""
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return
    for entry in entries:
        info = _lstat(entry)
        if info is None:
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            _remove_dir_by_hand(entry)
        else:
            _unlink_after_lstat(entry)
    try:
        path.rmdir()
    except OSError:
        pass


def _chown_group(path: Path, gid: int) -> None:
    """Sets one path's group to `gid`, owner untouched. The path was just lstat-ed as a real
    directory of this layout, so lchown semantics are not needed, but the no-follow form is used
    where the platform has it (a few raise NotImplementedError for the flag and take the plain
    call). Raises OSError - PermissionError included - when the process may not set the group."""
    try:
        os.chown(path, -1, gid, follow_symlinks=False)
    except NotImplementedError:
        os.chown(path, -1, gid)


def _read_file_checked(path: Path) -> Optional[Tuple[bytes, int, int]]:
    """(bytes, mode, gid) of one regular file through an O_NOFOLLOW fd, or None when it is
    missing, a symlink or unreadable."""
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks: List[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(info.st_mode), info.st_gid
    except OSError:
        return None
    finally:
        os.close(fd)


def _write_file_checked(path: Path, data: bytes, mode: int, gid: Optional[int] = None) -> None:
    """Atomically-ish writes one file with exactly `mode` through an O_NOFOLLOW fd (a symlink in
    its place is refused, never followed). With `gid` the file's group is set explicitly on the
    same fd - fchown first, fchmod after, because a chown may clear permission bits."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        if gid is not None:
            os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _fix_file_group(path: Path, mode: int, gid: int) -> None:
    """Repairs the group of one file whose bytes and mode are already right but whose group is
    not the agent's (the setgid parent did not carry it down): fchown to (-1, gid) through an
    O_NOFOLLOW fd, then fchmod, because a chown may clear permission bits. The bytes are never
    rewritten for this. Raises OSError when the file cannot be opened or changed."""
    fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW)
    try:
        os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _ensure_dir(path: Path, mode: int, gid: Optional[int] = None) -> None:
    """The directory must exist as a real directory (a symlink never counts) with exactly `mode`
    and, with `gid`, exactly the agent's group. No parents are created: the agent's socket
    directory must already exist, and the chmod runs only right after an lstat said the path is a
    real directory of this layout. The group is never left to the setgid parent alone: that bit
    can be lost (a Linux chmod by a process outside the file's group strips S_ISGID), so a wrong
    group is chowned back and the mode re-applied (a chown may clear S_ISGID too). A final lstat
    must then confirm both, else the layout is refused."""
    info = _lstat(path)
    created = False
    if info is None:
        path.mkdir(mode=mode)  # raises when a symlink or a file is in its place
        created = True
        info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError(f"not a real directory: {path}")
    changed = created or stat.S_IMODE(info.st_mode) != mode
    if gid is not None and info.st_gid != gid:
        _chown_group(path, gid)
        changed = True  # the mode is re-applied right after: a chown may clear S_ISGID
    if changed:
        os.chmod(path, mode)
    if gid is None:
        return
    check = _lstat(path)
    found_gid = check.st_gid if check is not None else None
    found_mode = stat.S_IMODE(check.st_mode) if check is not None else None
    if found_gid != gid or found_mode != mode:
        found_mode_text = oct(found_mode) if found_mode is not None else "none"
        raise OSError(f"directory group or mode not repaired: {path} (want gid {gid} mode "
                      f"{oct(mode)}, found gid {found_gid} mode {found_mode_text})")


# --- C5: the hello capability ---------------------------------------------------------------------


def capability(paths: SandboxPaths = DEFAULT_PATHS) -> Dict[str, Any]:
    """The hello fields of C5: open_tools is true only when both sandbox entry points exist as
    root-owned regular files and installed.json parses; sandbox_version is its version or None."""
    installed = _read_installed(paths)
    ready = (_root_regular_file(paths.wrapper, paths.root_uid)
             and _root_regular_file(paths.launcher, paths.root_uid)
             and installed is not None)
    version = installed.get("version") if installed is not None else None
    return {"open_tools": bool(ready),
            "sandbox_version": version if isinstance(version, str) and version else None}


# --- C6/C7: why an open turn must not run ---------------------------------------------------------


def open_problem(name: str, spec: Dict[str, Any], paths: SandboxPaths = DEFAULT_PATHS) -> Optional[str]:
    """Why a turn of this open agent must not run at all, or None when it may. The checks run in
    the contract's order and the first reason wins; the caller refuses the turn (never a fallback
    to the closed or unsandboxed path) and reports the reason through agent_exit."""
    spec = spec if isinstance(spec, dict) else {}
    blocked = spec.get("blocked")
    if isinstance(blocked, str) and blocked:
        return blocked
    provider = spec.get("provider")
    if provider not in OPEN_PROVIDERS:
        return "open_provider"
    if not OPEN_NAME_RE.fullmatch(name if isinstance(name, str) else ""):
        return "open_name"
    if not (_root_regular_file(paths.wrapper, paths.root_uid)
            and _root_regular_file(paths.launcher, paths.root_uid)):
        return "sandbox_missing"
    installed = _read_installed(paths)
    if installed is None:
        return "sandbox_missing"
    wanted = spec.get("sandbox") if isinstance(spec.get("sandbox"), dict) else {}
    wanted_version = wanted.get("version") if isinstance(wanted.get("version"), str) else None
    if wanted_version is None or installed.get("version") != wanted_version:
        return "sandbox_version_mismatch"
    agents = installed.get("agents")
    if not isinstance(agents, list) or name not in agents:
        return "sandbox_agent_missing"
    cli = installed.get("cli")
    if not isinstance(cli, dict) or cli.get(provider) != expected_cli_versions().get(provider):
        return "sandbox_cli_missing"
    # The socket directory belongs to the daemon (node user), group gsa-<name>, mode 2750: the agent
    # may connect to the 0660 sockets and read the MCP copy but never rename or replace anything in
    # it, so no path the daemon later chmods, writes or unlinks there can be swapped under it.
    sock = _lstat(paths.sock_dir(name))
    if (sock is None or not stat.S_ISDIR(sock.st_mode) or sock.st_uid != paths.daemon_uid()
            or sock.st_mode & 0o022):
        return "sandbox_sock_dir"
    gid = paths.agent_gid(paths.agent_group(name))
    if gid is None or gid not in paths.member_gids():
        return "sandbox_group"  # the daemon must be restarted after the installer joined the group
    if sock.st_gid != gid:
        return "sandbox_sock_dir"
    return None


# --- C4/C2: the wrapper invocations ----------------------------------------------------------------


def stop(name: str, paths: SandboxPaths = DEFAULT_PATHS, run: Any = subprocess.run) -> bool:
    """Stops every sandbox unit of the agent and waits for them to end (C4). True when none ran
    or they all ended; False when the wrapper failed or could not be run at all."""
    try:
        done = run(["sudo", "-n", str(paths.wrapper), "--stop", name],
                   capture_output=True, timeout=STOP_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def turn_command(name: str, timeout_s: int, paths: SandboxPaths = DEFAULT_PATHS) -> List[str]:
    """The argv that starts one sandboxed turn: sudo (the sudoers rule allows any wrapper
    arguments), the wrapper with the turn's hard deadline and the agent's work directory, then
    the launcher as C2 fixed it (-I: the launcher trusts nothing from its environment)."""
    return ["sudo", "-n", str(paths.wrapper), "--timeout", str(int(timeout_s)),
            "--chdir", str(paths.workdir(name)), name, "--",
            "/usr/bin/python3", "-I", str(paths.launcher)]


def launch_input(provider: str, version: str, argv: List[str], files: Dict[str, str],
                 stdin_text: str) -> bytes:
    """The launcher's stdin JSON (C2). Validates everything the launcher would refuse, so a
    driver bug surfaces here as a ValueError instead of as a lost turn."""
    if provider not in OPEN_PROVIDERS:
        raise ValueError(f"provider must be claude or agy: {provider!r}")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError(f"version must match {VERSION_RE.pattern}: {version!r}")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if argv[0] != provider:
        raise ValueError(f"argv[0] must equal the provider: {argv[0]!r}")
    if not isinstance(stdin_text, str):
        raise ValueError("stdin must be text")
    if not isinstance(files, dict):
        raise ValueError("files must map paths relative to $HOME to text")
    for rel, text in files.items():
        if (not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel
                or any(part == ".." for part in rel.split("/"))):
            raise ValueError(f"bad file path (relative to $HOME, no .. or NUL): {rel!r}")
        if not isinstance(text, str):
            raise ValueError(f"file content must be text: {rel!r}")
    payload = {"format": 1, "provider": provider, "version": version, "argv": list(argv),
               "files": dict(files), "stdin": stdin_text}
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


# --- C7: the daemon's own files in the agent's socket directory ------------------------------------


def remove_stale_sockets(directory: Path, keep: Optional[Path] = None) -> None:
    """Unlinks old incarnation sockets in one agent socket directory, but only what lstat says
    is a socket: a regular file or a symlink named e*.sock stays (the agent owns the directory
    too, so a name alone proves nothing)."""
    try:
        candidates = sorted(directory.glob("e*.sock"))
    except OSError:
        return
    for candidate in candidates:
        if keep is not None and candidate == keep:
            continue
        info = _lstat(candidate)
        if info is not None and stat.S_ISSOCK(info.st_mode):
            _unlink_after_lstat(candidate)


def clear_sock_dir(name: str, paths: SandboxPaths = DEFAULT_PATHS) -> None:
    """Retirement (C7): removes the daemon's own files in the agent's socket directory - the
    incarnation sockets and the MCP copies - with every entry unlinked only after an lstat check.
    The directory itself stays (the installer owns it) and nothing under /home/gsa-<name> is ever
    touched."""
    root = paths.sock_dir(name)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        info = _lstat(entry)
        if info is None:
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            _remove_dir_by_hand(entry)
        else:
            _unlink_after_lstat(entry)


def install_mcp_copy(name: str, worker_version: str, source_dir: Path,
                     paths: SandboxPaths = DEFAULT_PATHS) -> Path:
    """Copies the MCP import closure (MCP_COPY_MODULES) from the worker tree into the agent's
    socket directory under mcp/<worker_version>/, files 0640 and directories 2750. Every
    directory and file carries the agent's group explicitly, not only through the setgid parent:
    that bit can be lost (a Linux chmod by a process outside the file's group strips S_ISGID)
    and a wrong group is repaired here, so the sandbox user may always read but never write the
    copy. Skips files that already hold the same bytes, group and mode, and removes other version
    directories it finds there only by unlinking entries after an lstat check (no rmtree; a
    symlink is unlinked, never followed). Returns the copy directory; raises OSError when the
    source or the layout is wrong or the agent's group does not exist (the caller refuses the
    bind)."""
    if not isinstance(worker_version, str) or not WORKER_VERSION_RE.fullmatch(worker_version):
        raise ValueError(f"bad worker version: {worker_version!r}")
    gid = paths.agent_gid(paths.agent_group(name))
    if gid is None:
        raise OSError(f"no group {paths.agent_group(name)}")
    mcp_root = paths.sock_dir(name) / "mcp"
    dest = mcp_root / worker_version
    _ensure_dir(mcp_root, 0o2750, gid)
    _ensure_dir(dest, 0o2750, gid)
    for module in MCP_COPY_MODULES:
        source = Path(source_dir) / f"{module}.py"
        data = source.read_bytes()  # a missing module is the caller's error (OSError raises)
        target = dest / f"{module}.py"
        current = _read_file_checked(target)
        if current is not None and current[0] == data and current[1] & 0o777 == 0o640:
            if current[2] != gid:
                _fix_file_group(target, 0o640, gid)  # right bytes and mode, wrong group
            continue  # the bytes are never rewritten for a group repair
        _write_file_checked(target, data, 0o640, gid)
    try:
        others = sorted(mcp_root.iterdir())
    except OSError:
        others = []
    for entry in others:
        if entry == dest:
            continue
        info = _lstat(entry)
        if info is None:
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            _remove_dir_by_hand(entry)
        else:
            _unlink_after_lstat(entry)
    return dest


# --- O3: the task worktree bundles ---------------------------------------------------------------


def task_bundle_path(name: str, task: str, paths: SandboxPaths = DEFAULT_PATHS) -> Path:
    """The bundle file of one task (O3): tasks/<task>.bundle inside the agent's sandbox socket
    directory, where the daemon writes it and the sandboxed agent reads it."""
    return paths.sock_dir(name) / "tasks" / f"{task}.bundle"


def _bundle_ref(path: Path) -> Optional[str]:
    """The commit at a bundle file's refs/heads/snapshot line, read from the file header alone (no
    git runs for this); None when the file is missing, a symlink, unreadable or holds no such line."""
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        try:
            head = os.read(fd, 4096)
        except OSError:
            return None
    finally:
        os.close(fd)
    for line in head.split(b"\n"):
        sha, _sep, ref = line.partition(b" ")
        if (ref == BUNDLE_REF.encode("ascii") and len(sha) == 40
                and all(c in b"0123456789abcdef" for c in sha)):
            return sha.decode("ascii")
    return None


def write_task_bundle(name: str, task: str, git_dir: Path, commit: str,
                      paths: SandboxPaths = DEFAULT_PATHS, run: Any = subprocess.run) -> Optional[Path]:
    """Writes a git bundle of refs/heads/snapshot from a stored snapshot (O3) into the agent's
    sandbox socket directory: the tasks directory 2750 like the socket directory itself, the file
    0640, both with the agent's group set explicitly rather than inherited from the setgid parent
    (that bit can be lost, and a wrong group is repaired), so the agent may read the bundle but
    never write there. The file is written under a temp name, chowned and chmodded, fsynced and
    renamed into place. A bundle that already holds this commit is returned untouched. None when
    the task id is not a file name here or git failed; raises OSError when the agent's group does
    not exist (the caller refuses the call either way)."""
    if not isinstance(task, str) or not BUNDLE_NAME_RE.fullmatch(f"{task}.bundle"):
        return None
    gid = paths.agent_gid(paths.agent_group(name))
    if gid is None:
        raise OSError(f"no group {paths.agent_group(name)}")
    tasks = paths.sock_dir(name) / "tasks"
    _ensure_dir(tasks, 0o2750, gid)
    final = tasks / f"{task}.bundle"
    if _bundle_ref(final) == commit:
        info = _lstat(final)
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_gid != gid:
            try:
                _fix_file_group(final, 0o640, gid)  # an older bundle written with the wrong group
            except OSError:
                return None
        return final
    tmp = tasks / f".{task}.bundle.tmp"
    _unlink_after_lstat(tmp)
    try:
        done = run(["git", "--git-dir", str(git_dir), "bundle", "create", str(tmp), BUNDLE_REF],
                   capture_output=True, timeout=TASK_BUNDLE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or _bundle_ref(tmp) != commit:
        _unlink_after_lstat(tmp)
        return None
    try:
        _chown_group(tmp, gid)   # chown first, chmod after: a chown may clear permission bits
        os.chmod(str(tmp), 0o640)
        fd = os.open(str(tmp), os.O_RDONLY | _O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(final))
    except OSError:
        _unlink_after_lstat(tmp)
        return None
    return final


def remove_task_bundle(name: str, task: str, paths: SandboxPaths = DEFAULT_PATHS) -> None:
    """Unlinks the task's bundle (O3) after an lstat check; the tasks directory and everything else
    around it stays. A task that is not a file name here is ignored, never mapped to another path."""
    if isinstance(task, str) and BUNDLE_NAME_RE.fullmatch(f"{task}.bundle"):
        _unlink_after_lstat(task_bundle_path(name, task, paths))

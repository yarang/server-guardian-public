"""Workstation registry and the Guardian-owned authorized_keys2 renderer (operator-sessions.md S1b).

The declaration file <GUARDIAN_DATA_DIR>/session-workstations.json names the workstations whose
link keys the hub accepts: {"id", "address", "key", "node", "added"}, written atomically with mode
0600. From those entries Guardian renders the hub's authorized_keys2 whole (SD10): a fixed
two-line header, then one `from=`, `command=`, `restrict` line per workstation (SD9 binds the key
to the workstation's tailnet address), written to a temp file in the same directory and renamed
into place. The operator's authorized_keys is never written: keys_path() refuses that name.

keys_path() honours GUARDIAN_SESSION_KEYS_FILE so tests can point it at an isolated directory,
and every write refuses a target whose first line is not Guardian's header, so a hand-written
authorized_keys2 is never overwritten (SD10). Reads create nothing.
"""

import base64
import ipaddress
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import mail_store

RESERVED_IDS = ("operator", "hub")
KEY_TYPE = "ssh-ed25519"
# An ed25519 public key blob on the wire: the string "ssh-ed25519" + a string of 32 key bytes.
KEY_BLOB_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY_BLOB_LENGTH = 51
TAILNET_V4 = ipaddress.IPv4Network("100.64.0.0/10")
KEYS2_NAME = "authorized_keys2"
OPERATOR_KEYS_NAME = "authorized_keys"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEADER_LINES = (
    "# Guardian-owned file: written by guardian.py --session-add/--session-remove. Do not edit by hand.",
    "# docs/design/operator-sessions.md (SD10). One line per workstation link key.",
)
SSHD_TIMEOUT_S = 30


class RegistrationError(ValueError):
    """A registry check refused the change; the message is the reason shown to the operator."""


def declaration_path() -> Path:
    return mail_store.data_dir() / "session-workstations.json"


def keys_path() -> Path:
    """Where the hub's link lines live. GUARDIAN_SESSION_KEYS_FILE is the injection point for
    tests; authorized_keys (the operator's file) is refused whichever way the path was built."""
    configured = os.environ.get("GUARDIAN_SESSION_KEYS_FILE")
    path = Path(configured) if configured else Path.home() / ".ssh" / KEYS2_NAME
    if path.name == OPERATOR_KEYS_NAME:
        raise RegistrationError(f"{path}: authorized_keys is the operator's file; "
                                "Guardian never writes it")
    return path


# --- entry checks ------------------------------------------------------------------------


def _check_id(value: Any) -> str:
    if not isinstance(value, str) or not mail_store.NODE_RE.fullmatch(value):
        raise RegistrationError(f"bad workstation id {value!r}")
    if value in RESERVED_IDS:
        raise RegistrationError(f"workstation id {value} is reserved")
    return value


def _check_address(value: Any) -> str:
    """The workstation's tailnet IPv4, and nothing else (no IPv6, no CIDR, no hostnames)."""
    if not isinstance(value, str):
        raise RegistrationError(f"bad address {value!r}: expected the workstation's tailnet IPv4")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise RegistrationError(f"bad address {value!r}: {exc}") from exc
    if address not in TAILNET_V4:
        raise RegistrationError(f"bad address {value}: not inside the tailnet range 100.64.0.0/10")
    return str(address)


def _check_key(value: Any) -> str:
    """Validates `ssh-ed25519 <base64> [comment]` and returns the stored form without the comment."""
    if not isinstance(value, str):
        raise RegistrationError(f"bad key {value!r}: expected 'ssh-ed25519 <base64> [comment]'")
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise RegistrationError("bad key: newlines, CR and NUL are not allowed in a key")
    parts = value.strip().split()
    if not parts:
        raise RegistrationError("bad key: it is empty")
    if parts[0] != KEY_TYPE:
        if KEY_TYPE in parts[1:]:
            raise RegistrationError(f"bad key: options before the key type are not allowed "
                                    f"({parts[0]!r})")
        raise RegistrationError(f"bad key: only {KEY_TYPE} keys are accepted, got {parts[0]!r}")
    if len(parts) < 2:
        raise RegistrationError("bad key: the base64 part is missing")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError
        raise RegistrationError(f"bad key: the base64 part does not decode ({exc})") from exc
    if len(blob) != KEY_BLOB_LENGTH or not blob.startswith(KEY_BLOB_PREFIX):
        raise RegistrationError(f"bad key: the blob is not an {KEY_TYPE} key "
                                "(the type string or the 32-byte key length is wrong)")
    return f"{KEY_TYPE} {parts[1]}"


def _check_node(value: Any) -> Optional[str]:
    """The optional tailnet node name; display only."""
    if value is None:
        return None
    if not isinstance(value, str) or not mail_store.NODE_RE.fullmatch(value):
        raise RegistrationError(f"bad tailnet node name {value!r}")
    return value


def _check_added(value: Any) -> str:
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise RegistrationError(f"bad added timestamp {value!r}")
    try:
        mail_store.parse_ts(value)
    except ValueError as exc:
        raise RegistrationError(f"bad added timestamp {value!r} ({exc})") from exc
    return value


def _entry_problem(entry: Any) -> Optional[str]:
    """The static rules a stored entry must pass (the inventory check runs at registration)."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    try:
        _check_id(entry.get("id"))
        _check_address(entry.get("address"))
        if _check_key(entry.get("key")) != entry.get("key"):
            raise RegistrationError("key is not in stored form: 'ssh-ed25519 <base64>', no comment")
        _check_node(entry.get("node"))
        _check_added(entry.get("added"))
    except RegistrationError as exc:
        return str(exc)
    return None


# --- declaration file --------------------------------------------------------------------


def load_workstations(path: Optional[Path] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The valid entries and the problems of the skipped ones; a missing file reads as empty."""
    path = path or declaration_path()
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"{path.name}: cannot read ({exc})"]
    if not isinstance(data, list):
        return [], [f"{path.name}: expected a JSON list of workstations"]
    entries: List[Dict[str, Any]] = []
    problems: List[str] = []
    seen_ids: Set[str] = set()
    seen_keys: Set[str] = set()
    for raw in data:
        problem = _entry_problem(raw)
        if problem is None and raw["id"] in seen_ids:
            problem = f"duplicate workstation id {raw['id']}"
        if problem is None and raw["key"] in seen_keys:
            problem = f"key already used by another workstation (id {raw['id']})"
        if problem:
            problems.append(f"{path.name}: {problem}")
            continue
        entries.append(dict(raw))
        seen_ids.add(raw["id"])
        seen_keys.add(raw["key"])
    return entries, problems


def _write_atomic(path: Path, text: str) -> Path:
    """Writes through a temp file in the same directory (mode 0600, fsync) and os.replace."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _write_declarations(entries: List[Dict[str, Any]], path: Optional[Path] = None) -> Path:
    path = path or declaration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    return _write_atomic(path, payload)


def _inventory_ids(servers: Optional[Sequence[Dict[str, Any]]]) -> Set[str]:
    """The inventory node ids. An unreadable inventory refuses the registration instead of
    skipping the check: a workstation id that shadows a fleet node would blur mail addresses."""
    if servers is None:
        import group_manager
        servers, error, _ = group_manager.load_servers()
        if error:
            raise RegistrationError(error)
    return {entry.get("id") for entry in (servers or [])
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)}


def _find(entries: List[Dict[str, Any]], ws_id: str) -> Optional[Dict[str, Any]]:
    return next((entry for entry in entries if entry["id"] == ws_id), None)


# --- registry changes --------------------------------------------------------------------


def add_workstation(ws_id: str, address: str, key: str, *, node: Optional[str] = None,
                    servers: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Checks the id (including the inventory), the address and the key, writes the declaration
    and then the keys file. `servers` is injectable for tests; the default is the hub inventory."""
    _check_id(ws_id)
    if ws_id in _inventory_ids(servers):
        raise RegistrationError(f"workstation id {ws_id} is an inventory node id")
    entries, _ = load_workstations()
    if _find(entries, ws_id) is not None:
        raise RegistrationError(f"workstation {ws_id} is already registered")
    checked_address = _check_address(address)
    checked_key = _check_key(key)
    if any(entry["key"] == checked_key for entry in entries):
        raise RegistrationError("that key is already used by another workstation")
    entry = {"id": ws_id, "address": checked_address, "key": checked_key,
             "node": _check_node(node),
             "added": mail_store.format_ts(datetime.now(timezone.utc))}
    _refuse_foreign_keys_file()
    entries.append(entry)
    _write_declarations(entries)
    write_keys(entries)
    return entry


def set_address(ws_id: str, address: str) -> Dict[str, Any]:
    """SD9: the line's `from=` follows the node's new tailnet address."""
    entries, _ = load_workstations()
    entry = _find(entries, ws_id)
    if entry is None:
        raise RegistrationError(f"no workstation {ws_id}")
    entry["address"] = _check_address(address)
    _refuse_foreign_keys_file()
    _write_declarations(entries)
    write_keys(entries)
    return entry


def remove_workstation(ws_id: str) -> Dict[str, Any]:
    """Removes the declaration and its line; the last removal still leaves a header-only file."""
    entries, _ = load_workstations()
    entry = _find(entries, ws_id)
    if entry is None:
        raise RegistrationError(f"no workstation {ws_id}")
    remaining = [other for other in entries if other["id"] != ws_id]
    _refuse_foreign_keys_file()
    _write_declarations(remaining)
    write_keys(remaining)
    try:  # SD6: the workstation's flow drafts go with it; a failure here never undoes the removal
        import flow_drafts

        flow_drafts.delete_workstation(ws_id)
    except OSError:
        pass
    return entry


def get_workstation(ws_id: str) -> Optional[Dict[str, Any]]:
    """Read only; the link process polls this to notice removal."""
    entries, _ = load_workstations()
    return _find(entries, ws_id)


def list_workstations() -> Tuple[List[Dict[str, Any]], List[str]]:
    """The entries, each with `keys_line`: whether the current keys file holds this workstation's
    exact line (a missing file leaves every entry False)."""
    entries, problems = load_workstations()
    try:
        current = keys_path().read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        current = []
    for entry in entries:
        entry["keys_line"] = _keys_line(entry) in current
    return entries, problems


# --- keys file ---------------------------------------------------------------------------


def link_command(ws_id: str) -> str:
    """The forced command of the link entry. The `current` symlink stays unresolved so a new
    release takes effect without rewriting the file."""
    return (f"/usr/bin/python3 {Path.home()}/.guardian/current/cli/llm-agent/guardian.py "
            f"--session-link --workstation {ws_id}")


def _keys_line(entry: Dict[str, Any]) -> str:
    return (f'from="{entry["address"]}",command="{link_command(entry["id"])}",restrict '
            f'{entry["key"]} guardian-session:{entry["id"]}')


def render_keys(entries: Sequence[Dict[str, Any]]) -> str:
    """The header, then one line per entry sorted by id. `restrict` alone already turns off the
    pty, forwarding, agent and X11; no other option is ever written."""
    lines = list(HEADER_LINES)
    lines.extend(_keys_line(entry) for entry in sorted(entries, key=lambda e: e["id"]))
    return "\n".join(lines) + "\n"


def _first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readline().rstrip("\r\n")
    except (OSError, ValueError):
        return ""


def _refuse_foreign_keys_file() -> None:
    """SD10: a registry change refuses when authorized_keys2 exists without Guardian's header,
    before anything is written (otherwise the declaration and the file would drift apart)."""
    path = keys_path()
    if path.exists() and _first_line(path) != HEADER_LINES[0]:
        raise RegistrationError(f"{path}: not Guardian-owned; refusing to overwrite")


def write_keys(entries: Sequence[Dict[str, Any]], path: Optional[Path] = None) -> Path:
    """Writes the rendered file atomically: a foreign target is refused unchanged, otherwise a
    temp file in the same directory (mode 0600, fsync) replaces it by rename. The parent is
    created with mode 0700 only when missing; an empty registry writes the header-only file."""
    path = path or keys_path()
    if path.exists() and _first_line(path) != HEADER_LINES[0]:
        raise RegistrationError(f"{path}: not Guardian-owned; refusing to overwrite")
    parent = path.parent
    if not parent.is_dir():
        parent.mkdir(mode=0o700, parents=True)
        os.chmod(parent, 0o700)
    return _write_atomic(path, render_keys(entries))


# --- sshd check --------------------------------------------------------------------------


def _run(argv: List[str]) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=SSHD_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _lists_keys2(value: str) -> bool:
    for item in value.split():
        if item in (f".ssh/{KEYS2_NAME}", f"%h/.ssh/{KEYS2_NAME}"):
            return True
        if item.startswith("/") and item.endswith(f"/.ssh/{KEYS2_NAME}"):
            return True
    return False


def _authorized_keys_file(stdout: str) -> Dict[str, Any]:
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "authorizedkeysfile":
            value = " ".join(parts[1].split())
            listed = _lists_keys2(value)
            state = "includes" if listed else "does not include"
            return {"ok": listed,
                    "detail": f"authorizedkeysfile {state} .ssh/{KEYS2_NAME}: {value}"}
    return {"ok": False,
            "detail": f"sshd -T printed no authorizedkeysfile line "
                      f"(the compiled default has no .ssh/{KEYS2_NAME})"}


def sshd_reads_keys2(runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None) \
        -> Dict[str, Any]:
    """Whether the hub's sshd still reads authorized_keys2 (SD10): `sshd -T` plain first, then
    `sudo -n sshd -T`. ok None means the check could not run; it never reports ok silently."""
    run = runner or _run
    failures: List[str] = []
    for argv in (["sshd", "-T"], ["sudo", "-n", "sshd", "-T"]):
        code, out, err = run(argv)
        if code == 0:
            return _authorized_keys_file(out)
        failures.append(f"{' '.join(argv)}: rc {code}, {(err or '').strip() or 'no stderr'}")
    return {"ok": None, "detail": "the check could not run: " + "; ".join(failures)}

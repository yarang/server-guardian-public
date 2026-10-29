"""
Command sets and ad-hoc scripts of ops projects (docs/design/batch-execution.md, chapter 4, phase B1).

Two plan step kinds run a shell script on a host, both through the same typed-action path as the rest of the catalog
(validate, measure at proposal, drift check, run, audit label):

- ops:run_command_set {target_host, set, params}: a stored, reviewed set. The script, its read-only `check`, the risk
  and the reversibility are declared in the set's file, never by the agent; the agent only picks the set and fills its
  typed parameters. Builtin sets live in configs/command-sets/*.json (data of the repository, validated by
  scripts/ci.sh config), the operator's own in <GUARDIAN_DATA_DIR>/command-sets/*.json.
- ops:run_script {target_host, script, reason}: a script written by the judge. Always high risk and irreversible, so
  a person approves it (batch_gate, B2), except on the host groups of policy.batch.script_groups when the project set
  policy.batch.scripts to "vote" (F1); it is refused when it contains an obvious disaster (script_refusal), a bypass
  shape or a command of policy.deny_commands (ad_hoc_refusal).

A parameter value is rendered into the script only after it matched its declared type, and every accepted value is
a plain token (letters, digits and ._:/@+=,- , never starting with a dash), so the rendering cannot add a shell
word. The script itself travels base64-encoded in a fixed wrapper (wrap), so no quoting of the script text can break
out of the command line either. Standard library only; this module imports ops_paths and, lazily, ops_actions.

Placeholders are uppercase ${NAME}; a script uses lowercase names for its own shell variables.
"""

import base64
import datetime
import functools
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ops_paths

TOOL_SET = "ops:run_command_set"
TOOL_SCRIPT = "ops:run_script"
TOOLS = (TOOL_SET, TOOL_SCRIPT)

SET_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")  # run_command_set:<id>:preview must fit the hub's 64 character label
PARAM_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
PLACEHOLDER = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]*)\}")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/@+=,][A-Za-z0-9._:/@+=,-]{0,199}$")
NESTED_QUANTIFIER = re.compile(r"[+*}][^()]*\)[+*{?]|\)[+*{][^()]*\)[+*{]")  # (a+)+ and friends: catastrophic backtracking
MAX_STRING_VALUE = 64
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,99}$")
PARAM_TYPES = ("int", "enum", "unit", "package", "path", "string")
SET_KEYS = {"id", "description", "os", "needs_sudo", "params", "check", "script", "reversible", "undo", "risk",
            "timeout", "reviewed", "check_sudo"}
PARAM_KEYS = {"type", "min", "max", "values", "default", "pattern"}
RISKS = ("low", "medium", "high")
OSES = ("linux", "macos")
MAX_SCRIPT_BYTES = 8192   # a stored set
MAX_CHECK_BYTES = 4096
MAX_AD_HOC_BYTES = 4096   # a judge's script
MAX_FILE_BYTES = 65536
MAX_PARAMS = 12
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 600
CHECK_TIMEOUT = 60
DEFAULT_MAX_AGE_DAYS = 180


# --- the store ----------------------------------------------------------------------------------------------------


def builtin_dir() -> Path:
    override = os.environ.get("GUARDIAN_COMMAND_SETS")
    return Path(override) if override else Path(__file__).resolve().parent.parent.parent / "configs" / "command-sets"


def operator_dir() -> Path:
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "command-sets"


def max_age_days() -> int:
    """How old a set's `reviewed` date may get before it is reported as stale (GUARDIAN_COMMAND_SET_MAX_AGE_DAYS)."""
    try:
        return max(1, int(os.environ.get("GUARDIAN_COMMAND_SET_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


@functools.lru_cache(maxsize=512)
def _bash_syntax_error(script: str) -> Optional[str]:
    """The message of `bash -n` for a script, or None when it parses (or bash is not installed here)."""
    try:
        done = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "bash -n did not finish"
    if done.returncode != 0:
        return (done.stderr or "syntax error").strip().splitlines()[-1][:200]
    return None


def _check_param(name: str, raw: Any) -> Dict[str, Any]:
    if not PARAM_NAME.fullmatch(name):
        raise ValueError(f"param {name}: names are uppercase letters, digits and _ (such as AGE_DAYS)")
    if not isinstance(raw, dict) or set(raw) - PARAM_KEYS:
        raise ValueError(f"param {name}: an object with the keys {sorted(PARAM_KEYS)}")
    kind = raw.get("type")
    if kind not in PARAM_TYPES:
        raise ValueError(f"param {name}: type must be one of {', '.join(PARAM_TYPES)}")
    out: Dict[str, Any] = {"type": kind}
    if kind == "int":
        low, high = raw.get("min"), raw.get("max")
        if isinstance(low, bool) or not isinstance(low, int) or isinstance(high, bool) or not isinstance(high, int) \
                or low > high:
            raise ValueError(f"param {name}: an int needs integer min and max (min <= max)")
        if low < 0:
            raise ValueError(f"param {name}: min must not be negative (a value starting with - reads as an option)")
        out.update(min=low, max=high)
    elif kind == "enum":
        values = raw.get("values")
        if not isinstance(values, list) or not 1 <= len(values) <= 32 or not all(
                isinstance(v, str) and SAFE_VALUE.fullmatch(v) for v in values):
            raise ValueError(f"param {name}: an enum needs 1 to 32 plain values")
        out["values"] = list(dict.fromkeys(values))
    elif kind == "string":
        pattern = raw.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
            raise ValueError(f"param {name}: a string needs a pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"param {name}: bad pattern ({exc})") from exc
        if NESTED_QUANTIFIER.search(pattern):
            raise ValueError(f"param {name}: a repeated group inside a repeated group can stall the matcher; "
                             "write the pattern without nesting")
        out["pattern"] = pattern
    elif any(k in raw for k in ("min", "max", "values", "pattern")):
        raise ValueError(f"param {name}: min, max, values and pattern do not apply to {kind}")
    if "default" in raw:
        out["default"] = raw["default"]
        try:
            _check_value(name, out, raw["default"], ())
        except ValueError as exc:
            raise ValueError(f"param {name}: default: {exc}") from exc
    return out


def validate_set(spec: Any) -> Dict[str, Any]:
    """The normalized form of one command set file. Raises ValueError naming the field."""
    if not isinstance(spec, dict):
        raise ValueError("a command set is a JSON object")
    unknown = sorted(set(spec) - SET_KEYS)
    if unknown:
        raise ValueError(f"unknown keys {unknown}; known: {sorted(SET_KEYS)}")
    sid = spec.get("id")
    if not isinstance(sid, str) or not SET_ID.fullmatch(sid):
        raise ValueError("id must be lowercase letters, digits and - (at most 40, starting with a letter)")
    description = spec.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 300:
        raise ValueError(f"{sid}: description is required (at most 300 characters)")
    oses = spec.get("os", list(OSES))
    if not isinstance(oses, list) or not oses or any(o not in OSES for o in oses):
        raise ValueError(f"{sid}: os must list {' and/or '.join(OSES)}")
    risk_level = spec.get("risk", "medium")
    if risk_level not in RISKS:
        raise ValueError(f"{sid}: risk must be one of {', '.join(RISKS)}")
    timeout = spec.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        raise ValueError(f"{sid}: timeout must be 1 to {MAX_TIMEOUT} seconds")
    try:
        reviewed = datetime.date.fromisoformat(str(spec.get("reviewed")))
    except ValueError as exc:
        raise ValueError(f"{sid}: reviewed must be a date such as 2026-10-20") from exc
    if reviewed > datetime.date.today():
        raise ValueError(f"{sid}: reviewed is in the future")
    needs_sudo = spec.get("needs_sudo", False)
    reversible = spec.get("reversible", False)
    check_sudo = spec.get("check_sudo", False)
    if not isinstance(needs_sudo, bool) or not isinstance(reversible, bool) or not isinstance(check_sudo, bool):
        raise ValueError(f"{sid}: needs_sudo, check_sudo and reversible are true or false")
    if check_sudo and not needs_sudo:
        raise ValueError(f"{sid}: check_sudo needs needs_sudo")
    undo = spec.get("undo", "")
    if not isinstance(undo, str) or len(undo) > 500:
        raise ValueError(f"{sid}: undo is a text of at most 500 characters")
    if risk_level == "high":
        reversible = False  # a high risk set never runs without a person, whatever it says about undoing
    if reversible and not undo.strip():
        raise ValueError(f"{sid}: a reversible set must say in `undo` how to undo it")
    script, check = spec.get("script"), spec.get("check", "")
    if not isinstance(script, str) or not script.strip():
        raise ValueError(f"{sid}: script is required")
    if not isinstance(check, str):
        raise ValueError(f"{sid}: check is a script text")
    if len(script.encode()) > MAX_SCRIPT_BYTES or len(check.encode()) > MAX_CHECK_BYTES:
        raise ValueError(f"{sid}: script is at most {MAX_SCRIPT_BYTES} bytes and check {MAX_CHECK_BYTES}")
    raw_params = spec.get("params", {})
    if not isinstance(raw_params, dict) or len(raw_params) > MAX_PARAMS:
        raise ValueError(f"{sid}: params is an object of at most {MAX_PARAMS} parameters")
    params = {name: _check_param(name, raw) for name, raw in raw_params.items()}
    used = set(PLACEHOLDER.findall(script)) | set(PLACEHOLDER.findall(check))
    unnamed = sorted(n for n in used if n not in params and n.isupper())
    if unnamed:
        raise ValueError(f"{sid}: ${{{unnamed[0]}}} is not a declared param (uppercase ${{NAME}} is reserved for params; "
                         "use lowercase names for shell variables)")
    unused = sorted(set(params) - used)
    if unused:
        raise ValueError(f"{sid}: param {unused[0]} is not used by the script or the check")
    for label, text in (("script", script), ("check", check)):
        refusal = script_refusal(text) if text else None
        if refusal:
            raise ValueError(f"{sid}: {label}: {refusal}")
        if text:
            error = _bash_syntax_error(_syntax_probe(text, params))
            if error:
                raise ValueError(f"{sid}: {label} does not parse: {error}")
    return {"id": sid, "description": description, "os": list(dict.fromkeys(oses)), "needs_sudo": needs_sudo,
            "check_sudo": check_sudo, "params": params, "check": check, "script": script, "reversible": reversible, "undo": undo.strip(),
            "risk": risk_level, "timeout": timeout, "reviewed": reviewed.isoformat()}


def _syntax_probe(text: str, params: Dict[str, Dict[str, Any]]) -> str:
    """The text with every placeholder replaced by a plain word, to let bash -n judge the structure."""
    return PLACEHOLDER.sub(lambda m: "x" if m.group(1) in params else m.group(0), text)


def _read_dir(directory: Path, source: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("[a-z]*.json")):
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path.name}: must be a regular file of at most {MAX_FILE_BYTES} bytes")
        try:
            spec = validate_set(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path.name}: cannot read: {exc}") from exc
        except ValueError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
        if path.stem != spec["id"]:
            raise ValueError(f"{path.name}: the file name must be the set id ({spec['id']}.json)")
        out[spec["id"]] = {**spec, "source": source}
    return out


def _signature(directory: Path) -> Tuple[Any, ...]:
    if not directory.is_dir():
        return (str(directory),)
    files = []
    for path in sorted(directory.glob("[a-z]*.json")):
        try:
            st = path.stat()
        except OSError:
            continue
        files.append((path.name, st.st_mtime_ns, st.st_size, st.st_ino))
    return (str(directory), *files)


_LOADED: Dict[Tuple[Any, ...], Dict[str, Dict[str, Any]]] = {}


def load_sets() -> Dict[str, Dict[str, Any]]:
    """Every validated set, builtin and operator's, by id. An id in both stores is an error (nothing overrides a
    reviewed builtin silently). Raises ValueError naming the file. The result is remembered until a file changes."""
    key = (_signature(builtin_dir()), _signature(operator_dir()))
    cached = _LOADED.get(key)
    if cached is None:
        sets = _read_dir(builtin_dir(), "builtin")
        for sid, spec in _read_dir(operator_dir(), "operator").items():
            if sid in sets:
                raise ValueError(f"command set {sid} exists as a builtin and in {operator_dir()}; rename the operator's")
            sets[sid] = spec
        _LOADED.clear()
        _LOADED[key] = cached = sets
    return {sid: dict(spec) for sid, spec in cached.items()}


def get_set(set_id: Any) -> Dict[str, Any]:
    sets = load_sets()
    if not isinstance(set_id, str) or set_id not in sets:
        raise ValueError(f"unknown command set {set_id!r}; known: {', '.join(sorted(sets)) or 'none'}")
    return sets[set_id]


def stale(sets: Dict[str, Dict[str, Any]], today: Optional[datetime.date] = None,
          limit_days: Optional[int] = None) -> List[Dict[str, Any]]:
    today = today or datetime.date.today()
    limit = limit_days if limit_days is not None else max_age_days()
    old = [{"id": i, "reviewed": s["reviewed"], "age_days": (today - datetime.date.fromisoformat(s["reviewed"])).days}
           for i, s in sets.items()]
    return sorted((o for o in old if o["age_days"] > limit), key=lambda o: -o["age_days"])


def report() -> Tuple[int, Dict[str, Any]]:
    """(exit code, JSON) of `guardian.py --command-sets`: 2 when a file is invalid."""
    try:
        sets = load_sets()
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc), "sets": [], "stale": []}
    keys = ("id", "description", "source", "os", "needs_sudo", "check_sudo", "params", "reversible", "undo", "risk", "timeout",
            "reviewed", "check", "script")
    return 0, {"ok": True, "max_age_days": max_age_days(), "stale": stale(sets),
               "sets": [{**{k: s[k] for k in keys}, "confirmed": is_confirmed(s)} for _, s in sorted(sets.items())]}


CONFIRM_FILE = ".confirmed.json"  # next to the operator's sets; a leading dot keeps the loaders' [a-z]*.json glob off it


def content_digest(spec: Dict[str, Any]) -> str:
    """sha256 of what a set does: every field but the review date, the store it came from and the confirmation."""
    body = {k: v for k, v in spec.items() if k not in ("reviewed", "source", "confirmed")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _confirmations() -> Dict[str, str]:
    """set id -> content digest the operator confirmed. A missing, unreadable or malformed file confirms nothing."""
    path = operator_dir() / CONFIRM_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write_confirmations(data: Dict[str, str]) -> None:
    directory = operator_dir()
    if directory.is_symlink():
        raise ValueError(f"{directory} must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CONFIRM_FILE
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{path.name}: must be a regular file")
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_confirmed(spec: Dict[str, Any]) -> bool:
    """May this set be released by the batch gate without a person? A builtin set went through review and CI. An
    operator's set only after the operator confirmed this exact text (any change takes the confirmation back)."""
    if spec.get("source") == "builtin":
        return True
    return _confirmations().get(spec.get("id", "")) == content_digest(spec)


def confirm_set(set_id: Any) -> Dict[str, Any]:
    """Confirms the text of an operator's set as it is now, so the batch gate may release plans that use it.
    Raises ValueError for an unknown id or a builtin set (those need none)."""
    spec = get_set(set_id)
    if spec.get("source") != "operator":
        raise ValueError(f"{set_id} is a builtin command set and needs no confirmation")
    confirmed = _confirmations()
    confirmed[spec["id"]] = content_digest(spec)
    _write_confirmations(confirmed)
    return spec


def is_builtin(set_id: Any) -> bool:
    return isinstance(set_id, str) and bool(SET_ID.fullmatch(set_id)) and (builtin_dir() / f"{set_id}.json").is_file()


def save_set(spec: Any) -> Dict[str, Any]:
    """Validates and stores one set of the operator (B4c). `reviewed` is stamped here with today's date, whatever the
    caller sent: the date says when this host accepted the text, not when somebody claims to have read it. A builtin
    id is refused (nothing overrides a reviewed set), and so is a path that is a symlink. The file is written
    atomically. Raises ValueError naming the field; returns the normalized set."""
    if not isinstance(spec, dict):
        raise ValueError("a command set is a JSON object")
    stored = validate_set({**spec, "reviewed": datetime.date.today().isoformat()})
    if is_builtin(stored["id"]):
        raise ValueError(f"{stored['id']} is a builtin command set; save the operator's set under another id")
    directory = operator_dir()
    if directory.is_symlink():
        raise ValueError(f"{directory} must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stored['id']}.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{path.name}: must be a regular file")
    text = json.dumps(stored, indent=2, ensure_ascii=False) + "\n"
    if len(text.encode()) > MAX_FILE_BYTES:
        raise ValueError(f"{stored['id']}: the stored file would exceed {MAX_FILE_BYTES} bytes")
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)  # the loaders glob [a-z]*.json only
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return stored


def delete_set(set_id: Any) -> bool:
    """Removes an operator's set; False for an unknown id, a builtin set or anything that is not a set id."""
    if not isinstance(set_id, str) or not SET_ID.fullmatch(set_id):
        return False
    path = operator_dir() / f"{set_id}.json"
    if path.is_symlink() or not path.is_file():
        return False
    path.unlink()
    try:
        confirmed = _confirmations()
        if confirmed.pop(set_id, None) is not None:
            _write_confirmations(confirmed)
    except (OSError, ValueError):
        pass  # a stale entry is harmless: it only matches the exact text it was made for
    return True


# --- parameters ---------------------------------------------------------------------------------------------------


def _check_value(name: str, decl: Dict[str, Any], value: Any, deny: Sequence[str]) -> str:
    """The string a value renders as, or ValueError. Every result is a plain token."""
    kind = decl["type"]
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str)) or (
                isinstance(value, str) and not re.fullmatch(r"-?[0-9]{1,9}", value)):
            raise ValueError(f"{name} must be a whole number")
        number = int(value)
        if not decl["min"] <= number <= decl["max"]:
            raise ValueError(f"{name} must be {decl['min']} to {decl['max']}")
        return str(number)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a text")
    if not SAFE_VALUE.fullmatch(value):
        raise ValueError(f"{name}: {value!r} is not a plain token (letters, digits and ._:/@+=,- , not starting with -)")
    if re.search(r"(^|/)\.\.(/|$)", value):
        raise ValueError(f"{name}: {value!r} climbs out of its directory (.. segment)")
    if kind == "enum":
        if value not in decl["values"]:
            raise ValueError(f"{name} must be one of {', '.join(decl['values'])}")
    elif kind == "unit":
        import ops_actions

        try:
            return ops_actions.validate("ops:restart_service", {"unit": value})["unit"]
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    elif kind == "package":
        if not PACKAGE_NAME.fullmatch(value):
            raise ValueError(f"{name} must be a package name")
    elif kind == "path":
        reason = ops_paths.refusal(value, deny)
        if reason:
            raise ValueError(f"{name}: {reason}")
    elif kind == "string":
        if len(value) > MAX_STRING_VALUE:
            raise ValueError(f"{name} is longer than {MAX_STRING_VALUE} characters")
        if not re.fullmatch(decl["pattern"], value):
            raise ValueError(f"{name} does not match {decl['pattern']}")
    return value


def check_params(spec: Dict[str, Any], values: Any, deny: Sequence[str]) -> Dict[str, str]:
    """The set's parameters as strings, defaults filled in. Raises ValueError for a missing, unknown or misfit value."""
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("params must be an object")
    extra = sorted(set(values) - set(spec["params"]))
    if extra:
        raise ValueError(f"unknown params {extra}; the set takes {sorted(spec['params']) or 'none'}")
    out: Dict[str, str] = {}
    for name, decl in spec["params"].items():
        if name in values:
            out[name] = _check_value(name, decl, values[name], deny)
        elif "default" in decl:
            out[name] = _check_value(name, decl, decl["default"], deny)
        else:
            raise ValueError(f"missing param {name}")
    return out


def render(text: str, params: Dict[str, str]) -> str:
    """The text with each ${NAME} of a declared param replaced; other ${...} are the script's own shell variables."""
    return PLACEHOLDER.sub(lambda m: params.get(m.group(1), m.group(0)), text)


# --- transport ----------------------------------------------------------------------------------------------------


def wrap(script: str, sudo: bool, oses: Sequence[str]) -> str:
    """The command line that runs `script` on a host: the OS guard, the optional root requirement, then the script
    decoded from base64 into `bash -c`. The script text never appears on the command line itself. The line is plain
    POSIX shell without word splitting of variables, so it runs the same under the login shell of any host (zsh too),
    and a script that cannot be decoded fails with exit 7 instead of running nothing and reporting success."""
    payload = base64.b64encode(script.encode()).decode()
    guard = ('case "$(uname -s)" in Linux) o=linux;; Darwin) o=macos;; *) o=other;; esac; '
             f'case " {" ".join(oses)} " in *" $o "*) ;; *) echo "this command set does not support $o" >&2; exit 5;; esac; ')
    decode = (f"d=$(printf %s '{payload}' | {{ base64 -d 2>/dev/null || base64 -D; }}) && [ -n \"$d\" ] || "
              '{ echo "cannot decode the script" >&2; exit 7; }; ')
    if not sudo:
        return f'{guard}{decode}bash -c "$d"'
    root = ('if [ "$(id -u)" = 0 ]; then r=0; elif sudo -n true 2>/dev/null; then r=1; '
            'else echo "needs root or passwordless sudo" >&2; exit 6; fi; ')
    return f'{guard}{root}{decode}if [ "$r" = 1 ]; then sudo -n bash -c "$d"; else bash -c "$d"; fi'


def script_sha(*texts: str) -> str:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


# --- ad-hoc scripts: the tripwires --------------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[;&|\n]+|\$\(|`")
_SYSTEM_HOME = ("~", "~/", "~/*", "~/.", "~/..", "~root", "$HOME", "$HOME/", "$HOME/*", "$HOME/.", "${HOME}", "${HOME}/",
                "${HOME}/*", "${HOME:?}", "/", "/*", "/.", "/..")
_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = tuple((re.compile(p), why) for p, why in (
    (r"--no-preserve-root", "rm --no-preserve-root"),
    (r"\bmkfs(\.[a-z0-9]+)?\b|\bwipefs\b|\bshred\b|\b(s?fdisk|parted)\b", "it formats or partitions a disk"),
    (r"\bdd\b[^\n;|&]*\bof=[\"']?/dev/", "dd writes to a device"),
    (r">>?\s*/dev/(sd|nvme|vd|xvd|hd|mmcblk)", "it writes to a block device"),
    (r"\.guardian\b|\.ssh\b|\.claude\b|\.codex\b|\.gnupg\b|authorized_keys|\bid_(rsa|ed25519|ecdsa|dsa)\b|\.enc\b",
     "it touches credentials or Guardian state"),
    (r"/etc/(shadow|gshadow|sudoers)", "it touches the password or sudo files"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*((sudo|env)\s+(-\S+\s+)*)?(/\S*/)?((ba|z|da|k)?sh|python[0-9.]*|perl|ruby|node)\b|\b(ba|z|da)?sh\s+<\(\s*(curl|wget)\b|"
     r"\beval\s+[\"']?\$\(\s*(curl|wget)\b|\bsource\s+<\(\s*(curl|wget)\b", "it runs downloaded code"),
    (r"\btailscale\s+(down|logout|set)\b", "it takes the host off the tailnet"),
    (r"\b(pkill|killall)\b[^\n;|&]*(sshd|tailscaled|headscale)", "it kills a service that keeps the host reachable"),
    (r"\bfind\s+(/|/home|/root|/etc|/var|/usr)(\s|$)[^\n;|&]*-delete", "find -delete on a system directory"),
    (r"/proc/sysrq-trigger", "it shuts the host down"),
    (r"\b(apt|apt-get|dnf|yum|zypper)\b[^\n;|&]*\b(remove|purge|erase)\b[^\n;|&]*\b(openssh-server|openssh|sshd|tailscale)\b",
     "it removes the package that keeps the host reachable"),
    (r"\b(iptables|ip6tables)\b[^\n;|&]*(\s-F\b|--flush|\s-P\s+\w+\s+DROP)|\bnft\s+flush\b|\bufw\s+(--force\s+)?(disable|reset)\b",
     "it opens or cuts the firewall"),
))
_PREFIX_WORDS = ("sudo", "doas", "$S", "-n", "nohup", "env", "time", "command", "exec", "nice", "timeout", "stdbuf",
                 "xargs", "then", "do", "else", "if", "while", "!", "{", "(")
_SERVICE_VERBS = ("stop", "disable", "mask", "kill", "restart", "reload", "isolate")

# `sh -c '<text>'` and `sh <<< '<text>'` run text the segment checks would see only as the arguments of `sh`. The
# text is taken out and every check runs on it again. At most six words may sit between the shell and its flag
# (`bash -o pipefail -c`), which also keeps the scan linear.
_SHELL = r"\b(?:ba|z|da|k|a)?sh\s+(?:[^\s'\";&|]+\s+){0,6}?"
_SHELL_TEXT = (r"""(?:'(?P<single>[^']*)'|"(?P<double>(?:[^"\\]|\\.)*)"|\$'(?P<ansi>(?:[^'\\]|\\.)*)'|"""
               r"""(?P<bare>[^\s;&|()<>]+))""")
_SHELL_INNER = re.compile(rf"{_SHELL}(?:-[A-Za-z]*c[A-Za-z]*\s+|<<<\s*){_SHELL_TEXT}")
_MAX_SHELL_DEPTH = 4
_MAX_SHELL_LAYERS = 64


def _shell_layers(script: str) -> Tuple[List[str], bool]:
    """The script and the text of every shell it starts with -c or a here-string, to any depth up to
    _MAX_SHELL_DEPTH; the flag is True when more is nested below that (the caller refuses: it cannot be read)."""
    layers, frontier = [script], [script]
    for _ in range(_MAX_SHELL_DEPTH + 1):
        found: List[str] = []
        for text in frontier:
            for match in _SHELL_INNER.finditer(text):
                inner = next(match.group(g) for g in ("single", "double", "ansi", "bare") if match.group(g) is not None)
                if match.group("double") is not None:
                    inner = re.sub(r'\\(["\\$`])', r"\1", inner)
                if inner.strip():
                    found.append(inner)
        if not found:
            return layers, False
        if len(layers) + len(found) > _MAX_SHELL_LAYERS:
            return layers, True
        layers += found
        frontier = found
    return layers, True


def _tokens(segment: str) -> List[str]:
    """The words of one command, quotes removed anywhere in a word ("$HOME"/ reads as $HOME/)."""
    return [re.sub(r"[\"'\\]", "", t) for t in segment.split()]


def _command_word(words: List[str]) -> List[str]:
    """The words from the command itself on: prefix commands (sudo -u root, timeout 5, nice -n 5, VAR=x) and flag
    values are skipped, and /bin/rm reads as rm."""
    i = 0
    while i < len(words):
        word = words[i]
        if word in _PREFIX_WORDS or "=" in word or re.fullmatch(r"[0-9]+[smhd]?", word):
            i += 1
        elif word.startswith("-") and i > 0 and words[i - 1] in _PREFIX_WORDS:
            i += 2 if word in ("-u", "-g", "-n", "-H", "-C") else 1  # sudo -u root, nice -n 5
        else:
            break
    words = words[i:]
    if words:
        words = [words[0].rsplit("/", 1)[-1], *words[1:]]
    return words


_WILDCARDS = "*?[$"
# A glob below one of these reaches far more than its name suggests.
_BROAD_DIRS = ("/", "/home", "/root", "/var", "/srv", "/opt", "/mnt", "/media")


def _target_refusal(token: str) -> Optional[str]:
    """Why a path argument of rm/chmod/chown is out of bounds. A token with a glob or a variable counts by its
    literal leading directory (/tmp/* is fine, /home/* and /etc/* are not)."""
    if token in _SYSTEM_HOME:
        return f"{token} (the root or a home directory)"
    if not token.startswith("/"):
        return None
    cut = [i for i, c in enumerate(token) if c in _WILDCARDS]
    if not cut:
        return ops_paths.refusal(token.rstrip("/") or "/", ())
    prefix = token[: cut[0]].rsplit("/", 1)[0] or "/"
    if prefix in _BROAD_DIRS or any(ops_paths._within(prefix, tree) for tree in ops_paths.PROTECTED_TREES):
        return f"{token} (it reaches a system directory)"
    return None


# Shapes that hide what a script runs from the checks above: the text is built or decoded first and executed after.
# Only ad-hoc scripts get this check (a stored set is reviewed by a person when it is saved).
_BYPASS_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = tuple((re.compile(p), why) for p, why in (
    (r"(^|[\s;&|(`{])eval\b", "eval runs text the checks cannot read"),
    (r"\|\s*((sudo|env)\s+(-\S+\s+)*)?(/\S*/)?(ba|z|da|k)?sh(\s*$|\s*[;&|)]|\s+-s\b|\s+-)", "it pipes generated text into a shell"),
    (r"\b(ba|z|da|k)?sh\s+-[a-z]*c\s+[\"']?\$\(|\b(ba|z|da|k)?sh\s+-[a-z]*c\s+[\"']?`", "it runs the output of another command as code"),
    (r"\b(source|\.)\s+<\(|(\$\(|`)\s*base64\s+(-d|-D|--decode)\b", "it builds or decodes text and runs it"),
))
MAX_DENY_COMMANDS = 100
MAX_DENY_COMMAND_LEN = 200


def check_deny_commands(raw: object) -> List[str]:
    """Validates policy.deny_commands: fnmatch patterns matched against one command (after sudo, env and the like)
    written with single spaces, such as "docker system prune*" or "dd *". Raises ValueError."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_DENY_COMMANDS or not all(
            isinstance(p, str) and p.strip() and len(p) <= MAX_DENY_COMMAND_LEN and not any(c in p for c in "\r\n\0")
            for p in raw):
        raise ValueError(f"deny_commands must list at most {MAX_DENY_COMMANDS} patterns such as 'docker system prune*'")
    return list(dict.fromkeys(" ".join(p.split()) for p in raw))


_TOO_DEEP = "refused: shells are nested too deeply to read what they run"


def deny_refusal(script: str, deny_commands: Sequence[str]) -> Optional[str]:
    """Why a script runs a command the operator listed in policy.deny_commands, or None. Every command of the script
    (split at ; & | newlines and substitutions) is matched whole, so "rm *" also catches `sudo -n rm -rf x`. The
    text of `sh -c '...'` is read the same way, at any depth up to _MAX_SHELL_DEPTH."""
    if not deny_commands:
        return None
    layers, too_deep = _shell_layers(script)
    for text in layers:
        refusal = _deny_refusal_layer(text, deny_commands)
        if refusal:
            return refusal
    return _TOO_DEEP if too_deep else None


def _deny_refusal_layer(script: str, deny_commands: Sequence[str]) -> Optional[str]:
    import fnmatch

    for segment in _SEPARATORS.split(script):
        words = _command_word(_tokens(segment))
        if not words:
            continue
        line = " ".join(words)
        for pattern in deny_commands:
            if fnmatch.fnmatchcase(line, pattern) or fnmatch.fnmatchcase(" ".join(_tokens(segment)), pattern):
                return f"refused: the command {line[:80]!r} matches policy.deny_commands {pattern!r}"
    return None


def ad_hoc_refusal(script: str, deny_commands: Sequence[str] = ()) -> Optional[str]:
    """Everything an ad-hoc script is refused for: the tripwires of any script, the bypass shapes and the operator's
    own deny list."""
    refusal = script_refusal(script)
    if refusal:
        return refusal
    for pattern, why in _BYPASS_PATTERNS:
        if pattern.search(script):
            return f"refused: {why}"
    return deny_refusal(script, deny_commands)


def script_refusal(script: str) -> Optional[str]:
    """Why a script may not run at all: an obvious disaster. A tripwire for mistakes and prompt-injected scripts, not a
    sandbox: the critics, and a person where the project has not opted in to free scripts, read the script. The text
    of `sh -c '...'` is checked like the script itself, at any depth up to _MAX_SHELL_DEPTH."""
    layers, too_deep = _shell_layers(script)
    for text in layers:
        refusal = _script_refusal_layer(text)
        if refusal:
            return refusal
    return _TOO_DEEP if too_deep else None


def _script_refusal_layer(script: str) -> Optional[str]:
    for pattern, why in _PATTERNS:
        if pattern.search(script):
            return f"refused: {why}"
    import fnmatch

    import ops_actions

    for segment in _SEPARATORS.split(script):
        words = _command_word(_tokens(segment))
        if not words:
            continue
        command, args = words[0], words[1:]
        if command in ("shutdown", "reboot", "poweroff", "halt") or (command == "init" and args[:1] in (["0"], ["6"])):
            return "refused: it shuts the host down"
        if command == "systemctl":  # the values of -H/-M/-t belong to the flag, not to the verb
            args = [a for i, a in enumerate(args) if not (i and args[i - 1] in ("-H", "-M", "-t", "--host", "--machine", "--type"))]
        flags = [a for a in args if a.startswith("-")]
        plain = [a for a in args if not a.startswith("-")]
        recursive = any(f in ("--recursive",) or (not f.startswith("--") and ("r" in f or "R" in f)) for f in flags)
        if command == "rm":
            targets = plain
        elif command in ("chmod", "chown", "chgrp") and recursive:
            targets = plain[1:]
        else:
            targets = []
        for target in targets:
            reason = _target_refusal(target)
            if reason:
                return f"refused: {command} on {reason}"
        if command == "systemctl" and plain and plain[0] in ("reboot", "poweroff", "halt", "kexec"):
            return "refused: it shuts the host down"
        if command == "systemctl" and plain and plain[0] in _SERVICE_VERBS:
            units = [u if "." in u else u + ".service" for u in plain[1:]]
        elif command == "service" and len(plain) >= 2 and plain[1] in _SERVICE_VERBS:
            units = [plain[0] + ".service"]
        else:
            units = []
        for unit in units:
            unit = re.sub(r"\.(socket|path)$", ".service", unit)  # ssh.socket brings sshd back as surely as ssh.service
            if any(fnmatch.fnmatchcase(unit, p) for p in ops_actions.PROTECTED_UNITS):
                return f"refused: {unit} keeps the host reachable or managed"
    return None


# --- the two tools --------------------------------------------------------------------------------------------


def _known_set(args: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    try:
        return get_set((args or {}).get("set"))
    except ValueError:
        return None


def risk(tool: str, args: Optional[Dict[str, Any]] = None) -> str:
    """A script is always high; a set has the risk it declares, high when it cannot be found."""
    if tool == TOOL_SCRIPT or not args:
        return "high"
    spec = _known_set(args)
    return spec["risk"] if spec else "high"


def reversible(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the step can be undone: only a set that says so, and never a high risk one."""
    if tool == TOOL_SCRIPT or not args:
        return False
    spec = _known_set(args)
    return bool(spec and spec["reversible"] and spec["risk"] != "high")


def validate(tool: str, args: Dict[str, Any], deny: Sequence[str] = (),
             deny_commands: Sequence[str] = ()) -> Dict[str, Any]:
    """The tool's own argument rules (types are checked by mcp_client); deny is the project's deny_paths and
    deny_commands its command patterns. Raises ValueError."""
    if tool == TOOL_SET:
        extra = sorted(set(args) - {"target_host", "set", "params"})
        if extra:
            raise ValueError(f"unknown arguments {extra}: the script, check, risk and undo come from the set, not the agent")
        spec = get_set(args.get("set"))
        params = check_params(spec, args.get("params"), deny)
        _refuse_rendered(spec["id"], render(spec["script"], params), render(spec["check"], params),
                         deny_commands=deny_commands)
        return {**args, "params": params}
    if tool == TOOL_SCRIPT:
        script, reason = args.get("script"), args.get("reason")
        if not isinstance(script, str) or not script.strip():
            raise ValueError("script is empty")
        if len(script.encode()) > MAX_AD_HOC_BYTES:
            raise ValueError(f"script is longer than {MAX_AD_HOC_BYTES} bytes; store it as a command set")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required: say what the script is for")
        refusal = ad_hoc_refusal(script, deny_commands)
        if refusal:
            raise ValueError(refusal)
        error = _bash_syntax_error(script)
        if error:
            raise ValueError(f"script does not parse: {error}")
        return {**args, "reason": reason.strip()[:500]}
    raise ValueError(f"unknown action {tool}")


def _refuse_rendered(set_id: str, *texts: str, deny_commands: Sequence[str] = ()) -> None:
    """The tripwires also run on the text a set renders to: a parameter value can complete a destructive command
    (`rm -rf /srv/${T}` with T=..), which no check of the template alone would see."""
    for text in texts:
        refusal = (script_refusal(text) or deny_refusal(text, deny_commands)) if text else None
        if refusal:
            raise ValueError(f"{set_id}: with these params the script is refused: {refusal}")


class Plan:
    """What one validated step runs: the rendered script and check, how to wrap them and the audit label."""

    def __init__(self, tool: str, args: Dict[str, Any]) -> None:
        self.tool = tool
        if tool == TOOL_SCRIPT:
            self.label, self.script, self.check = "run_script", args["script"], ""
            self.sudo, self.check_sudo, self.oses, self.timeout = False, False, OSES, DEFAULT_TIMEOUT
            declared: Tuple[Any, ...] = ("high", False)
        else:
            spec = get_set(args.get("set"))
            params = check_params(spec, args.get("params"), ())
            self.label = f"run_command_set:{spec['id']}"
            self.script, self.check = render(spec["script"], params), render(spec["check"], params)
            self.sudo, self.oses, self.timeout = spec["needs_sudo"], tuple(spec["os"]), spec["timeout"]
            self.check_sudo = spec["check_sudo"]
            declared = (spec["risk"], spec["reversible"])
            _refuse_rendered(spec["id"], self.script, self.check)
        # Everything that decides how the step runs or how it was rated, so an edit to any of it is STALE.
        self.sha = script_sha(self.script, self.check, json.dumps(
            [self.sudo, self.check_sudo, list(self.oses), self.timeout, *declared], separators=(",", ":")))

    def run_command(self) -> str:
        return wrap(self.script, self.sudo, self.oses)

    def check_command(self) -> str:
        # The check runs when a step is proposed, before any person has seen it: it gets root only if the set says so.
        return wrap(self.check, self.check_sudo, self.oses) if self.check else ""

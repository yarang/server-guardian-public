#!/usr/bin/env python3
"""Capability probe decisions and the worker flags of the hub inventory (docs/design/remote-agents.md).

  worker_probe.py evaluate --requirements FILE [--max-jobs N]   facts on stdin -> verdict JSON on stdout
  worker_probe.py table                                        verdict JSON on stdin -> table, exit 1 when not capable
  worker_probe.py record-probe INVENTORY HOST VERDICT_B64      store the verdict as `worker_capable`
  worker_probe.py set-worker INVENTORY HOST on|off [INFO_B64]  `worker: true|false`; `on` needs a passed probe

scripts/probe-worker.sh gathers the facts over ssh and calls `evaluate` on the admin machine; the two inventory
commands run on the hub, where the script feeds this file to `python3 -` over ssh (so the hub needs no release of
this version). That is why the module imports nothing from the repository. Standard library only.
"""

import base64
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

REQUIREMENT_KEYS = {
    "min_total_memory_mib": (int, 1),
    "free_memory_mib_per_job": (int, 1),
    "default_max_jobs": (int, 1),
    "python_min": (str, None),
    "min_free_disk_mib": (int, 1),
    "min_working_cli": (int, 1),
}
VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")
HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FACT_LINE = re.compile(r"^([a-z][a-z0-9_.]*)=(.*)$")
CLI_OK = re.compile(r"^\s*✅\s+(\S+)")
CLI_FAIL = re.compile(r"^\s*❌\s+([^:\s]+)")
LLM_CHECK_MARK = "---llm-check---"


# --- requirements ---------------------------------------------------------------------------------

def validate_requirements(data: Any) -> List[str]:
    """Problems of a requirements object (empty = usable). Unknown keys are refused so a typo cannot go unnoticed."""
    if not isinstance(data, dict):
        return ["requirements must be a JSON object"]
    problems = [f"unknown key {key!r}" for key in data if key not in REQUIREMENT_KEYS]
    for key, (kind, floor) in REQUIREMENT_KEYS.items():
        value = data.get(key)
        if value is None:
            problems.append(f"{key} is missing")
        elif isinstance(value, bool) or not isinstance(value, kind):
            problems.append(f"{key} must be {'a whole number' if kind is int else 'a string'}")
        elif floor is not None and value < floor:
            problems.append(f"{key} must be at least {floor}")
        elif kind is str and not VERSION.match(value):
            problems.append(f"{key} must look like 3.9 or 3.9.1")
    return problems


def load_requirements(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read the requirements {path}: {exc}")
    problems = validate_requirements(data)
    if problems:
        raise SystemExit(f"{path}: " + "; ".join(problems))
    return data


# --- facts and verdict ----------------------------------------------------------------------------

def parse_facts(text: str) -> Dict[str, Any]:
    """`key=value` lines, then (after the llm-check marker) the lines of scripts/lib/llm-check.sh.

    `clis_ok` lists the CLIs that answered the PONG test, `clis_failed` those that did not."""
    facts: Dict[str, Any] = {"clis_ok": [], "clis_failed": []}
    in_check = False
    for line in text.splitlines():
        if line.strip() == LLM_CHECK_MARK:
            in_check = True
            continue
        if in_check:
            ok, bad = CLI_OK.match(line), CLI_FAIL.match(line)
            if ok:
                facts["clis_ok"].append(ok.group(1))
            elif bad:
                facts["clis_failed"].append(bad.group(1))
            continue
        match = FACT_LINE.match(line.strip())
        if match:
            facts[match.group(1)] = match.group(2).strip()
    return facts


def _number(facts: Dict[str, Any], key: str) -> Optional[int]:
    try:
        return int(str(facts.get(key, "")).strip())
    except ValueError:
        return None


def _version(text: str) -> Optional[Tuple[int, ...]]:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(text).strip())
    return tuple(int(p) for p in match.groups() if p is not None) if match else None


def evaluate(facts: Dict[str, Any], req: Dict[str, Any], max_jobs: Optional[int] = None) -> Dict[str, Any]:
    """The verdict: {passed, max_jobs, conditions: [{name, ok, detail}]}. A fact that is missing fails its condition."""
    jobs = max_jobs or req["default_max_jobs"]
    conditions: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        conditions.append({"name": name, "ok": bool(ok), "detail": detail})

    total_kib, free_kib, disk_kib = (_number(facts, k) for k in ("mem_total_kib", "mem_available_kib", "disk_free_kib"))
    total = None if total_kib is None else total_kib // 1024
    free = None if free_kib is None else free_kib // 1024
    disk = None if disk_kib is None else disk_kib // 1024
    need_free = req["free_memory_mib_per_job"] * jobs

    add("memory_total", total is not None and total >= req["min_total_memory_mib"],
        "unknown" if total is None else f"{total} MiB, needs {req['min_total_memory_mib']} MiB")
    add("memory_free", free is not None and free >= need_free,
        "unknown" if free is None else f"{free} MiB available, needs {need_free} MiB for {jobs} job(s)")

    have = _version(facts.get("python3_version", ""))
    want = _version(req["python_min"])
    add("python", have is not None and want is not None and have >= want,
        f"python3 {facts['python3_version']}, needs {req['python_min']}" if have else "python3 not found")

    # W2: the hub ships repository snapshots as git fast-import streams, so the worker needs git to import them.
    git = str(facts.get("git_version") or "").strip()
    add("git", bool(git), f"git {git}" if git else "git not found (repository snapshots need it)")

    working = list(facts.get("clis_ok") or [])
    add("llm_cli", len(working) >= req["min_working_cli"],
        ", ".join(working) + " answered PONG" if working else
        "no LLM CLI answered the PONG test" + (f" (failed: {', '.join(facts['clis_failed'])})" if facts.get("clis_failed") else ""))

    add("disk", disk is not None and disk >= req["min_free_disk_mib"],
        "unknown" if disk is None else f"{disk} MiB free, needs {req['min_free_disk_mib']} MiB")

    path = str(facts.get("hub_path", "unknown"))
    add("hub_path", path == "ok", {"ok": "the hub reaches the host with the fleet key"}.get(path, f"hub path: {path}"))

    return {"passed": all(c["ok"] for c in conditions), "max_jobs": jobs, "clis": working, "conditions": conditions}


def format_verdict(verdict: Dict[str, Any]) -> str:
    """The per-condition table scripts/probe-worker.sh prints."""
    lines = [f"  {'✅' if c['ok'] else '❌'} {c['name']:<13} {c['detail']}" for c in verdict["conditions"]]
    lines.append(f"  → {'capable' if verdict['passed'] else 'not capable'} (max_jobs {verdict.get('max_jobs')})")
    return "\n".join(lines)


# --- inventory flags ------------------------------------------------------------------------------

def _lock(lock_path: str, timeout: float = 30.0) -> Any:
    """The advisory flock the agent and setup-hub-ssh.sh take for inventory and fleet file writes."""
    try:
        import fcntl
        os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
        handle = open(lock_path, "a")
    except (ImportError, OSError) as exc:
        print(f"note: inventory lock unavailable ({exc}); continuing without it", file=sys.stderr)
        return None
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.time() > deadline:
                raise SystemExit("the inventory is locked by another process (the agent registering a node?); retry in a moment")
            time.sleep(0.1)


def default_lock_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "guardian_fleet.conf.lock")


def edit_inventory(path: str, host: str, change: Callable[[Dict[str, Any]], None],
                   lock_path: Optional[str] = None) -> Dict[str, Any]:
    """Read-modify-write one entry of servers.json under the shared lock; atomic, mode and key order kept, `.bak` left."""
    if not HOST_ID.match(host):
        raise SystemExit(f"invalid host id {host!r}")
    handle = _lock(lock_path or default_lock_path())
    try:
        with open(path, encoding="utf-8") as src:
            raw = src.read()
        data = json.loads(raw)
        if not isinstance(data, list):
            raise SystemExit(f"{path} is not a JSON array")
        entry = next((e for e in data if isinstance(e, dict) and e.get("id") == host), None)
        if entry is None:
            raise SystemExit(f"{host!r} is not in {path}")
        if entry.get("guardian_role") == "hub":
            raise SystemExit("the hub runs debates itself and is never a worker")
        change(entry)
        text = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")
        mode = os.stat(path).st_mode & 0o777
        with open(path + ".bak", "w", encoding="utf-8") as bak:
            os.chmod(path + ".bak", mode)
            bak.write(raw)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=os.path.dirname(os.path.abspath(path)))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(text)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return entry
    finally:
        if handle is not None:
            handle.close()


def record_probe(path: str, host: str, verdict: Dict[str, Any], lock_path: Optional[str] = None) -> Dict[str, Any]:
    reasons = [f"{c['name']}: {c['detail']}" for c in verdict["conditions"] if not c["ok"]]

    def change(entry: Dict[str, Any]) -> None:
        entry["worker_capable"] = {"passed": bool(verdict["passed"]), "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "max_jobs": verdict.get("max_jobs"), "clis": list(verdict.get("clis") or []),
                                   "reasons": reasons}
        if not verdict["passed"]:
            entry["worker"] = False  # a host that no longer passes stops receiving work

    return edit_inventory(path, host, change, lock_path)


def set_worker(path: str, host: str, enabled: bool, info: Optional[Dict[str, Any]] = None,
               lock_path: Optional[str] = None) -> Dict[str, Any]:
    """`worker: true` only for a host whose last probe passed; `false` always allowed (a rollback)."""
    def change(entry: Dict[str, Any]) -> None:
        if enabled and not (isinstance(entry.get("worker_capable"), dict) and entry["worker_capable"].get("passed") is True):
            raise SystemExit(f"{host}: the last capability probe did not pass (run scripts/probe-worker.sh {host} --record)")
        entry["worker"] = enabled
        if info:
            entry["worker_install"] = info
        elif not enabled:
            entry.pop("worker_install", None)

    return edit_inventory(path, host, change, lock_path)


# --- command line ---------------------------------------------------------------------------------

def _decode(arg: str) -> Dict[str, Any]:
    try:
        value = json.loads(base64.b64decode(arg.encode("ascii"), validate=True))
    except ValueError:
        raise SystemExit("argument is not base64 JSON")
    if not isinstance(value, dict):
        raise SystemExit("argument must be a JSON object")
    return value


def main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[0] == "evaluate":
        args = argv[1:]
        try:
            requirements = args[args.index("--requirements") + 1]
            max_jobs = int(args[args.index("--max-jobs") + 1]) if "--max-jobs" in args else None
        except (ValueError, IndexError):
            raise SystemExit("usage: worker_probe.py evaluate --requirements FILE [--max-jobs N]")
        verdict = evaluate(parse_facts(sys.stdin.read()), load_requirements(requirements), max_jobs)
        print(json.dumps(verdict, ensure_ascii=False))
        return 0
    if argv == ["table"]:
        verdict = json.loads(sys.stdin.read())
        print(format_verdict(verdict))
        return 0 if verdict.get("passed") else 1
    if len(argv) == 4 and argv[0] == "record-probe":
        record_probe(argv[1], argv[2], _decode(argv[3]))
        print(f"{argv[2]}: capability result recorded")
        return 0
    if len(argv) in (4, 5) and argv[0] == "set-worker" and argv[3] in ("on", "off"):
        set_worker(argv[1], argv[2], argv[3] == "on", _decode(argv[4]) if len(argv) == 5 else None)
        print(f"{argv[2]}: worker {argv[3]}")
        return 0
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Hub side of the remote model workers (docs/design/remote-agents.md).

A fleet host that passed the capability probe and is opted in (`"worker": true` in the inventory) runs a resident
model worker (worker.py). The hub asks it to run a provider CLI call there, so a debate member can think on another
machine. The worker is a model runner, not an agent: it holds no hub credentials and gets nothing but the request.

Transport: `ssh <host> python3 ~/.guardian/worker/worker.py client`, the request as one JSON line on stdin, the reply
as one JSON line on stdout. The prompt never appears in argv or in the hub's audit log. Any failure (unreachable,
protocol mismatch, busy, timeout) makes the call run on the hub instead, with a line in `LLMResult.failures`; the
session then lives on the hub and `LLMResult.host` says so.

A member with repository tools (a `repo_mcp` spec) reads a snapshot of the pinned commit (repo_snapshot.py): the router
builds it once per commit, uploads it over a second ssh command (`worker.py put-snapshot KEY`) when the worker does not
have it, and names it in the request. The worker serves the five snapshot tools from it and returns its tool call log,
which the router checks against the snapshot's file list and appends to the hub's own log, so the engine sees the same
records as for a member on the hub (W2).

Standard library only; llm_runner is imported lazily so that team_manager can use the host syntax helpers.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

# Bump when the request or reply shape changes; a worker with another protocol is refused (the call runs on the hub).
PROTOCOL = 2
# Placement labels of builtin teams (no real host names in git); resolved among the usable workers once per thread.
LABEL_ANY = "worker:any"
LABEL_OTHER = "worker:other"
LABELS = (LABEL_ANY, LABEL_OTHER)
HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Files whose content makes the worker's code version (informational: a stale worker is reported, not refused).
# This is the file set installed on a worker (deploy/worker/files.txt; test_worker_install.py keeps the two equal).
VERSION_FILES = (
    "code_context.py", "code_index.py", "code_select.py", "code_wiki.py", "env_policy.py", "evidence.py", "go_facts.py",
    "go_ops.py", "llm_runner.py", "mcp_stdio.py", "remote_agents.py", "repo_map.py", "repo_mcp.py", "repo_snapshot.py",
    "repo_tools.py", "worker.py",
)
WORKER_REMOTE_COMMAND = "python3 ~/.guardian/worker/worker.py client"
# Largest request or reply line (a prompt with its history is a few hundred KiB).
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
# Largest tool call log a worker returns with a job (records hold paths and counts, never file contents).
MAX_CALLS_BYTES = 8 * 1024 * 1024
# A snapshot upload may take longer than a call: a floor plus time per MiB of the compressed stream.
UPLOAD_BASE_SECONDS = 120.0
UPLOAD_SECONDS_PER_MIB = 6.0
PING_TTL_SECONDS = 30.0
DOWN_TTL_SECONDS = 60.0
# How long the hub waits beyond the job's own timeout (queue wait at the worker, ssh start, reply).
TRANSPORT_MARGIN_SECONDS = 20.0


class WorkerError(RuntimeError):
    """The remote call cannot be used; the message says why and is shown as the reason for the hub fallback."""


class WorkerBusy(WorkerError):
    """The worker has no free job slot (it stays usable for later calls)."""


class HubSideError(WorkerError):
    """The call cannot be sent because of the hub (a repository snapshot that cannot be built); the worker is fine."""


def is_label(host: Optional[str]) -> bool:
    return host in LABELS


def host_syntax_error(host: Any) -> Optional[str]:
    """Why a member's `host` is not acceptable by syntax alone (a label or an inventory-id shaped string)."""
    if not isinstance(host, str) or not host:
        return "expected null, a label (" + ", ".join(LABELS) + ") or an inventory host id"
    if host.startswith("worker:") and host not in LABELS:
        return f"unknown label {host!r}; known: {', '.join(LABELS)}"
    if host not in LABELS and not HOST_ID.match(host):
        return "use an inventory host id (letters, digits, '.', '_', '-') or a label"
    return None


def code_version() -> str:
    """Short digest of the worker's own files, for the Settings page (hub and worker compute it the same way)."""
    import hashlib
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in VERSION_FILES:
        digest.update(name.encode())
        try:
            digest.update((here / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:12]


def worker_hosts() -> List[Dict[str, Any]]:
    """Inventory entries with `"worker": true`; empty when the inventory cannot be read."""
    from group_manager import load_servers  # lazy: keeps this module cheap to import
    servers, _error, _path = load_servers()
    return [s for s in servers if s.get("worker") is True and HOST_ID.match(str(s.get("id", "")))]


def inventory_host_error(host: str) -> Optional[str]:
    """For the team save path: why an inventory id cannot hold a member, or None. A label is always fine; an
    unreadable inventory checks nothing (git-ignored, absent in CI)."""
    if is_label(host):
        return None
    from group_manager import load_servers
    servers, error, _path = load_servers()
    if error is not None:
        return None
    entry = next((s for s in servers if s.get("id") == host), None)
    if entry is None:
        return f"host {host!r} is not in the inventory"
    if entry.get("worker") is not True:
        return f"host {host!r} is not an enabled worker (inventory \"worker\": true; run the capability probe first)"
    return None


# --- transport ----------------------------------------------------------------------------------


class Transport:
    """Sends one request to a host's worker and returns its reply object. Raises WorkerError."""

    def call(self, host: str, request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        raise NotImplementedError

    def upload(self, host: str, key: str, stream: Path, timeout: float) -> Dict[str, Any]:
        """Sends the compressed snapshot stream in the file `stream` to be stored as snapshot `key`."""
        raise NotImplementedError


class SshTransport(Transport):
    """ssh with the fleet configuration of the hub user (key, known hosts and host key policy come from the fleet
    ssh blocks, so no StrictHostKeyChecking override here)."""

    def argv(self, host: str) -> List[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "--", host, WORKER_REMOTE_COMMAND]

    def call(self, host: str, request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        payload = json.dumps(request, ensure_ascii=False)
        try:
            proc = subprocess.run(self.argv(host), input=payload, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise WorkerError(f"no reply within {timeout:.0f}s") from None
        except OSError as exc:
            raise WorkerError(f"cannot start ssh: {exc}") from None
        return parse_reply(proc.returncode, proc.stdout, proc.stderr)

    def upload(self, host: str, key: str, stream: Path, timeout: float) -> Dict[str, Any]:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "--", host,
                f"python3 ~/.guardian/worker/worker.py put-snapshot {key}"]
        try:
            with open(stream, "rb") as source:
                proc = subprocess.run(argv, stdin=source, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise WorkerError(f"the snapshot upload took longer than {timeout:.0f}s") from None
        except OSError as exc:
            raise WorkerError(f"cannot upload the snapshot: {exc}") from None
        return parse_reply(proc.returncode, proc.stdout, proc.stderr)


def parse_reply(returncode: int, stdout: str, stderr: str) -> Dict[str, Any]:
    """The one JSON line a `worker.py client` prints, or WorkerError naming what went wrong."""
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    if returncode != 0 and not line:
        tail = " ".join(stderr.split())[-160:] or f"exit {returncode}"
        raise WorkerError(f"unreachable ({tail})")
    try:
        reply = json.loads(line)
    except json.JSONDecodeError:
        raise WorkerError("unreadable reply from the worker") from None
    if not isinstance(reply, dict):
        raise WorkerError("unreadable reply from the worker")
    return reply


# --- router -------------------------------------------------------------------------------------


def log_totals(path: str) -> Tuple[int, int]:
    """(tool calls, result bytes) already in a turn's log: what a worker job continues under the same limits."""
    steps = served = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    steps += 1
                    served += int(record.get("bytes") or 0)
    except OSError:
        pass
    return steps, served


REPORT_PING_SECONDS = 10.0


def _worker_state(host: str, transport: Transport, current: str) -> Dict[str, Any]:
    """One fresh ping for the report: {state, error, version, current, protocol, free_slots, max_jobs, providers, git}."""
    try:
        reply = transport.call(host, {"op": "ping", "protocol": PROTOCOL}, timeout=REPORT_PING_SECONDS)
    except WorkerError as exc:
        return {"state": "down", "error": str(exc)[:300]}
    if not reply.get("ok") or reply.get("protocol") != PROTOCOL:
        problem = reply.get("error") or f"worker speaks protocol {reply.get('protocol')}, the hub {PROTOCOL}; update the worker"
        return {"state": "down", "error": str(problem)[:300]}
    return {"state": "up", "error": None, "version": reply.get("version"), "current": reply.get("version") == current,
            "protocol": reply.get("protocol"), "free_slots": reply.get("free_slots"), "max_jobs": reply.get("max_jobs"),
            "providers": [str(p) for p in reply.get("providers") or []], "git": reply.get("git")}


def worker_report(servers: Optional[List[Dict[str, Any]]] = None, transport: Optional[Transport] = None) -> Dict[str, Any]:
    """The Settings view of the model workers (W4b): every inventory host that passed the probe or is enabled, with its
    recorded probe result and, for an enabled host, a fresh ping (all hosts in parallel). Read-only; a host that does not
    answer is reported `down`, never an error of the report. `servers` is the inventory (read from the hub when None)."""
    from concurrent.futures import ThreadPoolExecutor
    if servers is None:
        from group_manager import load_servers
        servers, error, _path = load_servers()
        if error is not None:
            return {"protocol": PROTOCOL, "code_version": code_version(), "workers": [], "error": error}
    transport = transport or SshTransport()
    current = code_version()
    rows: List[Dict[str, Any]] = []
    for entry in servers:
        host = str(entry.get("id", ""))
        probe = entry.get("worker_capable") if isinstance(entry.get("worker_capable"), dict) else None
        enabled = entry.get("worker") is True
        if not HOST_ID.match(host) or not (enabled or probe is not None):
            continue
        rows.append({"id": host, "name": str(entry.get("name") or ""), "enabled": enabled,
                     "capable": None if probe is None else probe.get("passed") is True,
                     "checked": None if probe is None else probe.get("checked"),
                     "max_jobs": None if probe is None else probe.get("max_jobs"), "state": "off", "error": None})
    live = [row for row in rows if row["enabled"]]
    if live:
        with ThreadPoolExecutor(max_workers=min(8, len(live))) as pool:
            states = list(pool.map(lambda row: _worker_state(row["id"], transport, current), live))
        for row, state in zip(live, states):
            row.update(state)
    rows.sort(key=lambda row: row["id"])
    return {"protocol": PROTOCOL, "code_version": current, "workers": rows}


def worker_choices(servers: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Inventory ids a team member may name as `host` (enabled workers), for the team editor; no network."""
    if servers is None:
        return sorted(str(s["id"]) for s in worker_hosts())
    return sorted(str(s["id"]) for s in servers if s.get("worker") is True and HOST_ID.match(str(s.get("id", ""))))


class RemoteRouter:
    """Runs session prompts on a worker and falls back to the hub's own runner when the worker cannot be used.

    A router belongs to one engine; the lock only guards its small caches (parallel critics share it)."""

    def __init__(self, local: Any, transport: Optional[Transport] = None) -> None:
        self.local = local
        self.transport = transport or SshTransport()
        self._lock = threading.Lock()
        self._pings: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._down: Dict[str, Tuple[float, str]] = {}
        # Repository snapshots: built once per (workspace, commit) in a private temp dir that goes with the router;
        # _present remembers which worker holds which key, so the next member of the thread skips the round trip.
        self._build_lock = threading.Lock()
        self._snapshots: Dict[Tuple[str, str], Tuple[Any, FrozenSet[str]]] = {}
        self._snapshot_dir: Optional[Path] = None
        self._present: Set[Tuple[str, str]] = set()

    # -- availability ------------------------------------------------------------------------

    def _mark_down(self, host: str, reason: str) -> None:
        with self._lock:
            self._down[host] = (time.monotonic() + DOWN_TTL_SECONDS, reason)
            self._pings.pop(host, None)
            self._present = {item for item in self._present if item[0] != host}

    def _down_reason(self, host: str) -> Optional[str]:
        with self._lock:
            entry = self._down.get(host)
            if entry and entry[0] > time.monotonic():
                return entry[1]
            self._down.pop(host, None)
        return None

    def ping(self, host: str) -> Dict[str, Any]:
        """The worker's ping reply (protocol, code version, free slots, installed providers), cached briefly.
        Raises WorkerError when the host is down, speaks another protocol or does not answer."""
        reason = self._down_reason(host)
        if reason is not None:
            raise WorkerError(reason)
        with self._lock:
            cached = self._pings.get(host)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        try:
            reply = self.transport.call(host, {"op": "ping", "protocol": PROTOCOL}, timeout=15.0)
            if not reply.get("ok"):
                raise WorkerError(str(reply.get("error") or "ping refused"))
            if reply.get("protocol") != PROTOCOL:
                raise WorkerError(f"worker speaks protocol {reply.get('protocol')}, the hub {PROTOCOL}; update the worker")
        except WorkerError as exc:
            self._mark_down(host, str(exc))
            raise
        with self._lock:
            self._pings[host] = (time.monotonic() + PING_TTL_SECONDS, reply)
        return reply

    def pick(self, label: str, in_use: Optional[Set[str]] = None) -> Optional[str]:
        """Resolves a placement label to a usable worker id, or None (the role then runs on the hub).

        Usable = `"worker": true` in the inventory and a ping that works. More free job slots first, then hosts that
        hold fewer roles of this thread, then the id. worker:other leaves hosts that already hold a role of the thread
        out as long as another one is usable."""
        in_use = in_use or set()
        usable: List[Tuple[int, int, str]] = []
        for entry in worker_hosts():
            host = str(entry["id"])
            try:
                info = self.ping(host)
            except WorkerError:
                continue
            usable.append((-int(info.get("free_slots") or 0), 1 if host in in_use else 0, host))
        if label == LABEL_OTHER:
            unused = [u for u in usable if u[1] == 0]
            usable = unused or usable
        usable.sort(key=lambda u: (u[1], u[0], u[2]))
        return usable[0][2] if usable else None

    # -- calls -------------------------------------------------------------------------------

    def run(self, host: str, prompt: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        """run_session_prompt on `host`. The keyword arguments are those of LLMRunner.run_session_prompt; a repo_mcp
        spec is never sent as it is: the worker gets a snapshot of the commit and builds its own spec. On any
        WorkerError the call runs on the hub, with the original spec."""
        from llm_runner import LLMResult
        spec = kwargs.pop("repo_mcp", None)
        fallback = kwargs.pop("fallback", None)  # the same turn for a session that has not seen the thread
        job = uuid.uuid4().hex[:16]
        try:
            self._check_host(host)
            info = self.ping(host)
            request = self._request(job, prompt, context, kwargs, info)
            allowed: FrozenSet[str] = frozenset()
            if spec:
                request["repo"], allowed = self._prepare_repo(host, spec, info)
            timeout = float(request["timeout"]) + float(info.get("queue_seconds") or 60) + TRANSPORT_MARGIN_SECONDS
            try:
                reply = self.transport.call(host, request, timeout=timeout)
            except WorkerError:
                self._cancel(host, job)
                raise
            if not reply.get("ok"):
                message = str(reply.get("error") or "the worker refused the call")
                raise WorkerBusy(message) if reply.get("busy") else WorkerError(message)
            data = reply.get("result")
            if not isinstance(data, dict) or not isinstance(data.get("provider"), str) or not isinstance(data.get("text"), str):
                raise WorkerError("the worker's reply has no result")
            if spec:
                self._append_calls(spec, reply.get("calls"), allowed)
            return LLMResult(
                provider=data["provider"], text=data["text"], session_id=data.get("session_id") or None,
                resumed=bool(data.get("resumed")), failures=[str(f) for f in data.get("failures") or []], host=host)
        except WorkerError as exc:
            if not isinstance(exc, (WorkerBusy, HubSideError)):
                self._mark_down(host, str(exc))
            if fallback:
                prompt = fallback.get("prompt", prompt)
                kwargs = {**kwargs, **({"plain_prompt": fallback["plain_prompt"]} if "plain_prompt" in fallback else {})}
            if spec:
                kwargs = {**kwargs, "repo_mcp": spec}
            return self._fallback(host, exc, prompt, context, kwargs)

    # -- repository snapshot -----------------------------------------------------------------

    def _snapshot_for(self, spec: Dict[str, Any]) -> Tuple[Any, FrozenSet[str]]:
        """The snapshot of the spec's commit and the set of paths it holds; built on first use."""
        from repo_snapshot import SnapshotError, build_snapshot
        from repo_tools import RepoTools
        key = (str(spec["workspace"]), str(spec["sha"]))
        with self._build_lock:
            cached = self._snapshots.get(key)
            if cached is not None:
                return cached
            try:
                if self._snapshot_dir is None:
                    self._snapshot_dir = Path(tempfile.mkdtemp(prefix="guardian_snapshots_"))  # 0700
                    weakref.finalize(self, shutil.rmtree, str(self._snapshot_dir), True)
                tools = RepoTools(Path(key[0]), key[1])
                snapshot = build_snapshot(tools, self._snapshot_dir)
                paths = frozenset(set(tools.readable_paths()) - set(snapshot.dropped))
            except SnapshotError as exc:
                raise HubSideError(f"cannot build the repository snapshot: {exc}") from None
            except Exception as exc:  # noqa: BLE001 - ContextError, OSError: the hub can still answer
                raise HubSideError(f"cannot read the repository: {exc}") from None
            self._snapshots[key] = (snapshot, paths)
            return self._snapshots[key]

    def _prepare_repo(self, host: str, spec: Dict[str, Any], info: Dict[str, Any]) -> Tuple[Dict[str, Any], FrozenSet[str]]:
        """The request's `repo` field for the spec, with the snapshot uploaded when the worker lacks it."""
        if info.get("git") is False:
            raise WorkerError("git is not installed on the worker")
        snapshot, paths = self._snapshot_for(spec)
        with self._lock:
            known = (host, snapshot.key) in self._present
        if not known:
            reply = self.transport.call(host, {"op": "snapshot_has", "protocol": PROTOCOL, "key": snapshot.key},
                                        timeout=15.0)
            if not reply.get("ok"):
                raise WorkerError(str(reply.get("error") or "the worker refused the snapshot check"))
            if not reply.get("present"):
                timeout = UPLOAD_BASE_SECONDS + UPLOAD_SECONDS_PER_MIB * snapshot.size / (1024 * 1024)
                done = self.transport.upload(host, snapshot.key, snapshot.path, timeout=timeout)
                if not done.get("ok"):
                    raise WorkerError("snapshot upload refused: " + str(done.get("error") or "unknown reason"))
            with self._lock:
                self._present.add((host, snapshot.key))
        used_steps, used_bytes = log_totals(str(spec.get("log") or ""))
        limits = spec.get("limits") or {}
        return ({"key": snapshot.key, "sha_label": str(spec["sha"]),
                 "limits": {k: limits.get(k) for k in ("steps", "step_bytes", "turn_bytes")},
                 "seed_steps": used_steps, "seed_bytes": used_bytes}, paths)

    @staticmethod
    def _append_calls(spec: Dict[str, Any], calls: Any, allowed: FrozenSet[str]) -> None:
        """Appends the worker's tool call records to the hub's log of the turn, after checking them: the evidence rules
        trust what the log says the agent was shown, so a path outside the snapshot is dropped, not believed."""
        if not isinstance(calls, list):
            raise WorkerError("the worker's reply has no tool call log")
        lines = []
        for record in calls:
            if not isinstance(record, dict):
                continue
            record = dict(record)
            record["paths"] = [p for p in record.get("paths") or [] if isinstance(p, str) and p in allowed]
            spans = record.get("spans")
            record["spans"] = {p: v for p, v in spans.items() if p in allowed} if isinstance(spans, dict) else {}
            lines.append(json.dumps(record, ensure_ascii=False) + "\n")
        if not lines or not spec.get("log"):
            return
        try:
            fd = os.open(str(spec["log"]), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, "".join(lines).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            raise WorkerError(f"cannot record the worker's tool calls: {exc}") from None

    def _check_host(self, host: str) -> None:
        if not HOST_ID.match(host):
            raise WorkerError("not a valid host id")
        if not any(str(s["id"]) == host for s in worker_hosts()):
            raise WorkerError("not an enabled worker in the inventory")

    @staticmethod
    def _request(job: str, prompt: str, context: Optional[Dict[str, Any]], kwargs: Dict[str, Any],
                 info: Dict[str, Any]) -> Dict[str, Any]:
        instructions = kwargs.get("instructions")
        providers = kwargs.get("providers") or []
        per_call = float(info.get("provider_timeout") or 300.0)
        return {
            "op": "run", "protocol": PROTOCOL, "job": job, "prompt": prompt, "context": context or {},
            "session_id": kwargs.get("session_id"), "provider": kwargs.get("provider"), "providers": list(providers),
            "system": kwargs.get("system"), "plain_prompt": kwargs.get("plain_prompt"),
            "instructions": ({"static": instructions.static, "dynamic": instructions.dynamic} if instructions else None),
            # Every provider of the order may take its own timeout when the one before failed.
            "timeout": per_call * max(1, len(providers)) + 30.0,
        }

    def _cancel(self, host: str, job: str) -> None:
        try:
            self.transport.call(host, {"op": "cancel", "protocol": PROTOCOL, "job": job}, timeout=10.0)
        except WorkerError:
            pass  # unreachable: the worker notices the closed connection or its own timeout

    def _fallback(self, host: str, exc: WorkerError, prompt: str, context: Optional[Dict[str, Any]],
                  kwargs: Dict[str, Any]) -> Any:
        """The hub answers instead, in a new session (a worker session id means nothing here)."""
        local_kwargs = {k: v for k, v in kwargs.items() if v is not None and k not in ("session_id", "provider")}
        result = self.local.run_session_prompt(prompt, context, **local_kwargs)
        result.failures.insert(0, f"{host}: {exc}; the call ran on the hub")
        result.host = ""
        return result

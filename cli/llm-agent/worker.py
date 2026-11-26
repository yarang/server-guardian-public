#!/usr/bin/env python3
"""Model worker: runs provider CLI calls for the hub on this host (docs/design/remote-agents.md).

  worker.py serve   the resident daemon (systemd user unit): a 0600 unix socket, a job table, timeouts, cancellation
  worker.py client  what the hub runs over ssh: one JSON line on stdin -> the socket -> one JSON line on stdout
  worker.py relay   what the hub's mail relay runs over ssh (docs/design/resident-agents.md, protocol 3): one relay
                    line to the socket, then a raw byte pipe stdin -> socket and socket -> stdout until either side
                    closes; the resident duties on the node are in resident_node.py
  worker.py exec    one job in its own process group (the daemon starts it, so a cancel can kill the whole tree)
  worker.py put-snapshot KEY
                    what the hub runs over ssh to upload a repository snapshot (repo_snapshot.py): the compressed
                    stream on stdin, imported into ~/.guardian/worker/snapshots/KEY

The worker is a model runner, not an agent: it never receives AGENT_TOKEN or any hub key, serves no HTTP and answers
only the hub's ssh session. It uses this host's own provider logins (~/.guardian/llm.env, deployed by
deploy-llm-env.sh); credential files are never copied here. Standard library only.
"""

import json
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from remote_agents import COMPATIBLE_PROTOCOLS, MAX_CALLS_BYTES, MAX_MESSAGE_BYTES, PROTOCOL, code_version
from repo_snapshot import SnapshotError, import_stream, is_key, prune_snapshots, snapshot_commit

# The version of the code this process loaded, taken once at start. A ping must not re-read the files: after an
# update whose restart did not happen, the old process would then report the new version (scripts/install-worker.sh
# compares this value with the files it installed).
STARTED_VERSION = code_version()
DEFAULT_MAX_JOBS = 2
DEFAULT_QUEUE_SECONDS = 60.0
# A request may ask for less than this, never for more (a runaway provider call must end).
MAX_JOB_SECONDS = 3600.0
JOB_ID = re.compile(r"^[0-9a-f]{8,32}$")
# Fields of a run request that reach the exec process; everything else is ignored.
PAYLOAD_KEYS = ("prompt", "context", "session_id", "provider", "providers", "system", "plain_prompt", "instructions")
# The repo field of a run request: the snapshot to read and the limits the hub's turn continues under.
REPO_LABEL = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
REPO_LIMITS = ("steps", "step_bytes", "turn_bytes")
REPO_SEEDS = ("seed_steps", "seed_bytes")


def worker_dir() -> Path:
    return Path(os.environ.get("GUARDIAN_WORKER_DIR", "~/.guardian/worker")).expanduser()


def snapshot_root() -> Path:
    return worker_dir() / "snapshots"


def jobs_root() -> Path:
    return worker_dir() / "jobs"


def snapshot_policy() -> Dict[str, float]:
    """How long unused snapshots stay and how many are kept (GUARDIAN_WORKER_SNAPSHOT_TTL seconds / _KEEP)."""
    return {"ttl_seconds": _env_number("GUARDIAN_WORKER_SNAPSHOT_TTL", 24 * 3600.0),
            "keep": int(_env_number("GUARDIAN_WORKER_SNAPSHOT_KEEP", 8))}


def pinned_snapshot_keys() -> frozenset:
    """Snapshot keys an owned resident task pins (M2e): every key in every agents/*/tasks.json, read
    from disk so the separate put-snapshot process sees them too. An import or read error pins
    nothing; the W2 prune path never breaks over the resident layer."""
    try:
        from resident_tasks import pinned_keys
        return pinned_keys(worker_dir() / "agents")
    except Exception:  # noqa: BLE001 - a worker without the resident layer prunes exactly as before
        return frozenset()


def socket_path() -> Path:
    return Path(os.environ.get("GUARDIAN_WORKER_SOCKET") or worker_dir() / "worker.sock")


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def exec_argv() -> List[str]:
    """Command of one job. GUARDIAN_WORKER_EXEC_CMD (a JSON list) replaces it for tests of the job table."""
    override = os.environ.get("GUARDIAN_WORKER_EXEC_CMD")
    if override:
        value = json.loads(override)
        if isinstance(value, list) and all(isinstance(v, str) for v in value) and value:
            return value
    return [sys.executable, str(Path(__file__).resolve()), "exec"]


class Job:
    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.proc: Optional[subprocess.Popen] = None
        self.cancelled = False
        self.started = time.monotonic()

    def kill(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the job is its own process group leader
            except (ProcessLookupError, PermissionError):
                proc.kill()


class Worker:
    """The job table of one daemon."""

    def __init__(self, max_jobs: Optional[int] = None, queue_seconds: Optional[float] = None) -> None:
        self.max_jobs = max_jobs or int(_env_number("GUARDIAN_WORKER_MAX_JOBS", DEFAULT_MAX_JOBS))
        self.queue_seconds = queue_seconds if queue_seconds is not None else _env_number(
            "GUARDIAN_WORKER_QUEUE_SECONDS", DEFAULT_QUEUE_SECONDS)
        self._slots = threading.Condition()
        self._jobs: Dict[str, Job] = {}
        # The dedicated resident slot of D5 (docs/design/resident-agents.md): one per node, on top of
        # the job slots, held by the address of the agent whose turn is running.
        self._resident_slot: Optional[str] = None
        # Lazily a resident_node.ResidentManager on the first `relay` op, so a daemon that never sees
        # a relay creates nothing (imported lazily: resident_node imports this module's worker_dir).
        self.resident: Optional[Any] = None

    # -- requests ----------------------------------------------------------------------------

    def handle(self, request: Any, conn: Optional[socket.socket] = None) -> Dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        if request.get("protocol") not in COMPATIBLE_PROTOCOLS:
            # An old hub still speaks 2 (COMPATIBLE_PROTOCOLS); the answer always names the worker's own protocol.
            return {"ok": False, "error": f"protocol mismatch: worker speaks {PROTOCOL}", "protocol": PROTOCOL}
        op = request.get("op")
        if op == "ping":
            return self._ping()
        if op == "cancel":
            return self._cancel(request.get("job"))
        if op == "run":
            return self._run(request, conn)
        if op == "snapshot_has":
            key = request.get("key")
            commit = snapshot_commit(snapshot_root(), key) if is_key(key) else None
            return {"ok": True, "present": commit is not None}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def _ping(self) -> Dict[str, Any]:
        from llm_runner import LLMRunner
        runner = LLMRunner()
        with self._slots:
            free = max(0, self.max_jobs - len(self._jobs))
            resident_free = self._resident_slot is None
        return {"ok": True, "protocol": PROTOCOL, "version": STARTED_VERSION, "free_slots": free,
                "max_jobs": self.max_jobs, "queue_seconds": self.queue_seconds,
                "provider_timeout": runner.timeout, "providers": runner.installed_providers,
                "git": shutil.which("git") is not None, "resident_slot_free": resident_free}

    def take_resident_slot(self, owner: str) -> bool:
        """The one resident slot (D5), non-blocking; False when another agent's turn holds it."""
        with self._slots:
            if self._resident_slot is not None:
                return False
            self._resident_slot = owner
            return True

    def release_resident_slot(self, owner: str) -> None:
        """Gives the resident slot back; only the agent that holds it can free it."""
        with self._slots:
            if self._resident_slot == owner:
                self._resident_slot = None

    def _cancel(self, job_id: Any) -> Dict[str, Any]:
        with self._slots:
            job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            return {"ok": True, "cancelled": False}  # finished already, or never started
        job.cancelled = True
        job.kill()
        return {"ok": True, "cancelled": True}

    def _acquire(self, job: Job) -> bool:
        deadline = time.monotonic() + self.queue_seconds
        with self._slots:
            while len(self._jobs) >= self.max_jobs:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._slots.wait(left)
            self._jobs[job.id] = job
        return True

    def _release(self, job: Job) -> None:
        with self._slots:
            self._jobs.pop(job.id, None)
            self._slots.notify()

    def _run(self, request: Dict[str, Any], conn: Optional[socket.socket]) -> Dict[str, Any]:
        job_id = request.get("job")
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            return {"ok": False, "error": "run needs a job id (8-32 hex characters)"}
        error = self._invalid(request)
        if error:
            return {"ok": False, "error": error}
        job = Job(job_id)
        if not self._acquire(job):
            return {"ok": False, "busy": True, "error": f"busy: {self.max_jobs} jobs running"}
        try:
            return self._execute(job, request, conn)
        finally:
            self._release(job)

    @staticmethod
    def _invalid(request: Dict[str, Any]) -> Optional[str]:
        from llm_runner import KNOWN_PROVIDERS
        if not isinstance(request.get("prompt"), str):
            return "run needs a prompt"
        providers = request.get("providers") or []
        if not isinstance(providers, list) or any(p not in KNOWN_PROVIDERS for p in providers):
            return "providers must be known provider names"
        instructions = request.get("instructions")
        if instructions is not None and not (isinstance(instructions, dict) and isinstance(instructions.get("static"), str)):
            return "instructions must be {static, dynamic}"
        repo = request.get("repo")
        if repo is not None:
            return Worker._invalid_repo(repo)
        return None

    @staticmethod
    def _invalid_repo(repo: Any) -> Optional[str]:
        if not isinstance(repo, dict) or not is_key(repo.get("key")):
            return "repo must name a snapshot key"
        if not isinstance(repo.get("sha_label"), str) or not REPO_LABEL.match(repo["sha_label"]):
            return "repo.sha_label must be a short commit label"
        limits = repo.get("limits") or {}
        if not isinstance(limits, dict) or any(
                k not in REPO_LIMITS or not (v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0))
                for k, v in limits.items()):
            return "repo.limits must be non-negative integers"
        for name in REPO_SEEDS:
            value = repo.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return f"repo.{name} must be a non-negative integer"
        if snapshot_commit(snapshot_root(), repo["key"]) is None:
            return "snapshot missing"
        return None

    @staticmethod
    def _repo_spec(repo: Dict[str, Any], log: Path) -> Dict[str, Any]:
        """The repo_mcp spec of a job, built here from the validated field: the hub never sends a path or a command."""
        key = repo["key"]
        commit = snapshot_commit(snapshot_root(), key, touch=True)
        if commit is None:
            raise SnapshotError("snapshot missing")
        limits = repo.get("limits") or {}
        return {"workspace": str(snapshot_root() / key), "sha": commit, "log": str(log),
                "limits": {k: limits.get(k) for k in REPO_LIMITS}, "no_index": True, "sha_label": repo["sha_label"],
                "seed_steps": int(repo.get("seed_steps") or 0), "seed_bytes": int(repo.get("seed_bytes") or 0)}

    def _execute(self, job: Job, request: Dict[str, Any], conn: Optional[socket.socket]) -> Dict[str, Any]:
        try:
            requested = float(request.get("timeout") or 330.0)
        except (TypeError, ValueError):
            requested = 330.0
        timeout = max(1.0, min(requested, MAX_JOB_SECONDS))
        fields: Dict[str, Any] = {k: request.get(k) for k in PAYLOAD_KEYS}
        job_dir: Optional[Path] = None
        log: Optional[Path] = None
        if request.get("repo") is not None:
            try:
                jobs_root().mkdir(parents=True, exist_ok=True, mode=0o700)
                job_dir = Path(tempfile.mkdtemp(prefix=f"{job.id}-", dir=jobs_root()))  # 0700
                log = job_dir / "calls.jsonl"
                log.touch(mode=0o600)
                fields["repo_mcp"] = self._repo_spec(request["repo"], log)
            except (OSError, SnapshotError) as exc:
                if job_dir is not None:
                    shutil.rmtree(job_dir, ignore_errors=True)
                return {"ok": False, "error": f"cannot prepare the repository: {exc}"}
        try:
            return self._run_exec(job, fields, timeout, conn, log)
        finally:
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)

    def _run_exec(self, job: Job, fields: Dict[str, Any], timeout: float, conn: Optional[socket.socket],
                  log: Optional[Path]) -> Dict[str, Any]:
        payload = json.dumps(fields, ensure_ascii=False)
        try:
            job.proc = subprocess.Popen(exec_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
        except OSError as exc:
            return {"ok": False, "error": f"cannot start the job: {exc}"}
        captured: Dict[str, Any] = {}

        def communicate() -> None:
            try:
                captured["out"], captured["err"] = job.proc.communicate(payload)  # type: ignore[union-attr]
            except (OSError, ValueError) as exc:
                captured["out"], captured["err"] = "", str(exc)

        thread = threading.Thread(target=communicate, name=f"job-{job.id}", daemon=True)
        thread.start()
        stop = ""
        deadline = time.monotonic() + timeout
        while thread.is_alive():
            thread.join(0.5)
            if not thread.is_alive():
                break
            if job.cancelled:
                stop = "cancelled"
            elif time.monotonic() > deadline:
                stop = f"timed out after {timeout:.0f}s"
            elif conn is not None and _peer_closed(conn):
                stop = "the hub closed the connection"
            if stop:
                job.kill()
                thread.join(10)
                break
        if job.cancelled and not stop:
            stop = "cancelled"  # the kill landed between two polls: the process is already gone
        if stop:
            return {"ok": False, "error": stop}
        return _job_reply(job.proc.returncode if job.proc else -1, captured.get("out", ""), captured.get("err", ""), log)

    def shutdown(self) -> None:
        with self._slots:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancelled = True
            job.kill()


def _peer_closed(conn: socket.socket) -> bool:
    """True when the other end hung up (the ssh session behind the client died); the request was fully read."""
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        return bool(readable) and conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _job_reply(returncode: int, out: str, err: str, log: Optional[Path] = None) -> Dict[str, Any]:
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        result = json.loads(line)
    except json.JSONDecodeError:
        result = None
    if returncode != 0 or not isinstance(result, dict):
        tail = " ".join(err.split())[-200:] or f"exit {returncode}"
        return {"ok": False, "error": f"the job failed ({tail})"}
    if log is None:
        return {"ok": True, "result": result}
    calls = read_calls(log)
    if calls is None:
        return {"ok": False, "error": f"the tool call log is larger than {MAX_CALLS_BYTES} bytes"}
    return {"ok": True, "result": result, "calls": calls}


def read_calls(log: Path) -> Optional[List[Dict[str, Any]]]:
    """The records of a job's tool call log, or None when the log is larger than MAX_CALLS_BYTES (a partial log would
    make the hub undercount what the agent was shown). The log never holds file contents."""
    try:
        with open(log, "rb") as handle:
            raw = handle.read(MAX_CALLS_BYTES + 1)
    except OSError:
        return []
    if len(raw) > MAX_CALLS_BYTES:
        return None
    calls: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            calls.append(record)
    return calls


# --- daemon -------------------------------------------------------------------------------------


def read_line(stream: Any) -> bytes:
    line = stream.readline(MAX_MESSAGE_BYTES + 1)
    if len(line) > MAX_MESSAGE_BYTES:
        raise ValueError("message too large")
    return line


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        worker: Worker = self.server.worker  # type: ignore[attr-defined]
        try:
            line = read_line(self.rfile)
            if not line.strip():
                return
            request = json.loads(line)
        except (ValueError, OSError):
            reply: Dict[str, Any] = {"ok": False, "error": "unreadable request"}
        else:
            if isinstance(request, dict) and request.get("op") == "relay":
                return self._relay(worker, request)
            reply = worker.handle(request, self.request)
        try:
            self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass  # the client is gone; a job it started was already stopped

    def _relay(self, worker: Worker, request: Dict[str, Any]) -> None:
        """The hub's resident stream (protocol 3; resident_node.py owns the frames). This connection
        writes no ordinary reply: the daemon's `hello` is the first line on it. The relay op line above
        is the only line read through the buffered reader: the peer sends nothing after it until
        `hello` is answered, so nothing of the stream can sit in that reader's buffer."""
        if request.get("protocol") != 3:
            reply = {"ok": False, "error": "protocol mismatch: relay speaks 3", "protocol": 3}
            try:
                self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                pass
            return
        if worker.resident is None:
            from resident_driver import HeadlessDriver
            from resident_node import ResidentManager
            driver = HeadlessDriver()
            worker.resident = ResidentManager(worker, driver=driver)
            driver.attach(worker.resident)
        worker.resident.attach(self.rfile, self.wfile)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, worker: Worker) -> None:
        self.worker = worker
        super().__init__(path, _Handler)


def _prepare_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()  # a stale socket of a dead daemon
        else:
            raise SystemExit(f"another worker is already serving {path}")
        finally:
            probe.close()


def make_server(path: Path, worker: Worker) -> _Server:
    _prepare_socket(path)
    previous = os.umask(0o177)  # the socket is created 0600
    try:
        return _Server(str(path), worker)
    finally:
        os.umask(previous)


def serve() -> int:
    worker = Worker()
    server = make_server(socket_path(), worker)

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    shutil.rmtree(jobs_root(), ignore_errors=True)  # the logs of jobs a restart cut off
    prune_snapshots(snapshot_root(), pinned=pinned_snapshot_keys(), **snapshot_policy())
    print(f"worker {code_version()} protocol {PROTOCOL} listening on {socket_path()} (max {worker.max_jobs} jobs)",
          flush=True)
    try:
        server.serve_forever()
    finally:
        if worker.resident is not None:
            worker.resident.shutdown()
        worker.shutdown()
        server.server_close()
        try:
            socket_path().unlink()
        except OSError:
            pass
    return 0


# --- client and exec modes ------------------------------------------------------------------------


def client() -> int:
    """Relays the single request on stdin to the daemon. Failures are printed as an {"ok": false} reply."""
    def fail(message: str) -> int:
        print(json.dumps({"ok": False, "error": message}))
        return 0

    raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
    try:
        if len(raw) > MAX_MESSAGE_BYTES:
            return fail("request too large")
        request = json.loads(raw)
    except ValueError:
        return fail("unreadable request")
    conn = socket.socket(socket.AF_UNIX)
    try:
        conn.connect(str(socket_path()))
        conn.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        line = read_line(conn.makefile("rb"))
    except (OSError, ValueError):
        return fail("worker daemon not running")
    finally:
        conn.close()
    sys.stdout.write(line.decode("utf-8").strip() + "\n" if line else json.dumps({"ok": False, "error": "no reply"}) + "\n")
    return 0


def run_job() -> int:
    """One provider call; prints the LLMResult as one JSON line."""
    from llm_runner import LLMRunner, SessionInstructions
    payload = json.loads(sys.stdin.read())
    kwargs: Dict[str, Any] = {k: payload[k] for k in ("session_id", "provider", "providers") if payload.get(k)}
    # An empty system string is meaningful (no role header), so these two only drop null.
    kwargs.update({k: payload[k] for k in ("system", "plain_prompt") if payload.get(k) is not None})
    if payload.get("instructions"):
        kwargs["instructions"] = SessionInstructions(payload["instructions"]["static"],
                                                     payload["instructions"].get("dynamic") or "")
    if payload.get("repo_mcp"):
        kwargs["repo_mcp"] = payload["repo_mcp"]
    result = LLMRunner().run_session_prompt(payload["prompt"], payload.get("context") or {}, **kwargs)
    print(json.dumps({"provider": result.provider, "text": result.text, "session_id": result.session_id,
                      "resumed": result.resumed, "failures": result.failures}, ensure_ascii=False))
    return 0


def relay_client() -> int:
    """What the hub's mail relay runs over ssh (docs/design/resident-agents.md): one relay line to the
    daemon, then a raw byte pipe until either side closes. Holds no state; the frames on the stream
    belong to the daemon (resident_node.py) and the hub."""
    conn = socket.socket(socket.AF_UNIX)
    try:
        conn.connect(str(socket_path()))
    except OSError:
        print(json.dumps({"t": "error", "error": "worker daemon not running"}))
        return 1
    try:
        conn.sendall(b'{"op": "relay", "protocol": 3}\n')
        _copy_until_closed(conn)
    except OSError:
        pass  # one side is gone; the stream is over either way
    finally:
        conn.close()
    return 0


def _copy_until_closed(conn: socket.socket) -> None:
    """stdin -> socket and socket -> stdout, binary, flushed per chunk, until either side closes."""
    stdin_fd = sys.stdin.buffer.fileno()
    stdout = sys.stdout.buffer
    while True:
        readable, _w, _e = select.select([stdin_fd, conn], [], [])
        if stdin_fd in readable:
            chunk = os.read(stdin_fd, 65536)
            if not chunk:
                return  # the hub closed its side (the ssh session ended)
            conn.sendall(chunk)
        if conn in readable:
            chunk = conn.recv(65536)
            if not chunk:
                return  # the daemon closed the stream
            stdout.write(chunk)
            stdout.flush()


def put_snapshot(key: str) -> int:
    """Imports the compressed snapshot stream on stdin as snapshot `key`; prints one JSON line like the client does."""
    if not is_key(key):
        print(json.dumps({"ok": False, "error": "not a snapshot key"}))
        return 0
    try:
        commit = import_stream(snapshot_root(), key, sys.stdin.buffer)
        prune_snapshots(snapshot_root(), pinned=pinned_snapshot_keys(), **snapshot_policy())
    except (SnapshotError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:300]}))
        return 0
    print(json.dumps({"ok": True, "commit": commit}))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    modes = {"serve": serve, "client": client, "relay": relay_client, "exec": run_job}
    if len(args) == 2 and args[0] == "put-snapshot":
        return put_snapshot(args[1])
    if len(args) != 1 or args[0] not in modes:
        print("usage: worker.py serve|client|relay|exec|put-snapshot KEY", file=sys.stderr)
        return 2
    return modes[args[0]]()


if __name__ == "__main__":
    sys.exit(main())

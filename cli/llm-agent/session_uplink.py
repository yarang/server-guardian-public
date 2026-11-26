"""The uplink half of the workstation link daemon (S2 of docs/design/operator-sessions.md).

Installed on an operator workstation next to session_local.py, this module owns everything
below the daemon itself: dialling the hub (SD12), running one link stream against the hub's
session_link.LinkSession, and the durable mail spool. It imports only the standard library
and session_local, because no hub module exists on a workstation.

The dial: ssh -T to the hub's tailnet IPv4 with the workstation's own key, the hub's host
key pinned through one HostKeyAlias entry, no agent, no accept-new. A connect-class failure
before any greeting falls back to the link name once (SD12); any other failure never does,
and a host key mismatch is terminal ("host_key_mismatch"). Backoff doubles from 1 s to 60 s.
LinkClient mirrors the hub's LinkSession (greeting, hello, welcome, pings, a "liveness" end
after 90 s of silence: sshd sends no keepalive). Mail is stored durably before the reply
that acks it; nothing here ever logs a mail body or subject.
"""

import calendar
import json
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import session_local

# The ssh environment: SSH_AUTH_SOCK is not passed, so no agent offers keys (SD2).
PASS_ENV = ("PATH", "HOME", "LANG", "USER", "LOGNAME")
_HOST_KEY_MARKERS = ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED",
                     "No ED25519 host key is known", "No ECDSA host key is known",
                     "No RSA host key is known", "host key for")
_CONNECT_MARKERS = ("Connection refused", "Connection timed out", "Operation timed out",
                    "No route to host", "Network is unreachable", "Could not resolve hostname",
                    "Name or service not known", "nodename nor servname provided")
_AUTH_MARKERS = ("Permission denied",)
_MAIL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAIL_REQUIRED = ("id", "to", "from", "kind", "body")


def log(message: str) -> None:
    """One line on stderr (the daemon redirects this to its log); never mail content."""
    print(f"session-uplink: {message}", file=sys.stderr, flush=True)


def _call_quietly(callback: Callable, *args: Any) -> None:
    """Runs a callback; a broken callback is logged and never ends a stream or the loop."""
    try:
        callback(*args)
    except Exception as exc:  # noqa: BLE001 - whose callback this is must not matter here
        log(f"callback failed: {exc}")


def _close_stream(stream: Any) -> None:
    """Closes a stream object that may be a plain fake without close()."""
    try:
        if getattr(stream, "close", None) is not None:
            stream.close()
    except (OSError, ValueError, AttributeError):
        pass


def _kill(proc: Any) -> None:
    try:
        if getattr(proc, "kill", None) is not None:
            proc.kill()
    except OSError:
        pass


def ssh_argv(config: Dict[str, Any], paths: Any, target: str) -> List[str]:
    """The argument vector of one dial: always a list, never a shell string, no remote
    command (the hub's authorized_keys2 line forces it, SD10). BatchMode, IdentitiesOnly
    with our key, the host key pinned through one HostKeyAlias over the private
    known_hosts file (strict, never accept-new, SD12), ServerAliveInterval (sshd sends no
    keepalive of its own)."""
    return ["ssh", "-T", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-i", str(paths.key), "-o", f"HostKeyAlias={config['hub_alias']}", "-o",
            f"UserKnownHostsFile={paths.known_hosts}", "-o", "StrictHostKeyChecking=yes",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ServerAliveInterval=30", "-o",
            "ServerAliveCountMax=3", "-o", "ConnectTimeout=15", "-p", str(config["ssh_port"]),
            f"{config['hub_user']}@{target}"]


def classify_ssh_failure(returncode: Any, stderr_text: str) -> str:
    """The class of one failed dial: "host_key" (checked first, so a mismatch is never a
    connect failure), "connect", "auth" or "other" (anything else, including empty stderr;
    "Connection closed by remote host" is other). The returncode adds nothing."""
    text = stderr_text or ""
    for markers, answer in ((_HOST_KEY_MARKERS, "host_key"), (_CONNECT_MARKERS, "connect"),
                            (_AUTH_MARKERS, "auth")):
        if any(marker in text for marker in markers):
            return answer
    return "other"


def _child_env(base: Dict[str, str]) -> Dict[str, str]:
    """Only the PASS_ENV keys of base; SSH_AUTH_SOCK and everything else never reach ssh."""
    return {key: base[key] for key in PASS_ENV if base.get(key) is not None}


class SshChild:
    """One ssh child: proc.stdin/stdout are the link stream, stderr is drained by a thread
    into a bounded tail the failure classification reads after the exit."""

    STDERR_TAIL_BYTES = 8192
    WAIT_GRACE = 5.0

    def __init__(self, proc: Any):
        self.proc = proc
        self._tail = bytearray()
        self._drained = threading.Event()
        self._done = False
        self._thread = threading.Thread(target=self._drain_stderr, daemon=True,
                                        name="uplink-ssh-stderr")
        self._thread.start()

    def _drain_stderr(self) -> None:
        stream = getattr(self.proc, "stderr", None)
        try:
            while stream is not None:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self._tail += chunk
                del self._tail[:-self.STDERR_TAIL_BYTES]
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            self._drained.set()

    def shutdown(self) -> None:
        """Closes our end and reaps the child; idempotent."""
        if self._done:
            return
        self._done = True
        _close_stream(self.proc.stdin)
        if self._wait(self.WAIT_GRACE) is not None:
            return
        _kill(self.proc)
        self._wait(2.0)

    def finish(self) -> Tuple[Any, str]:
        """(returncode, stderr text) once the child is gone; safe after shutdown."""
        self.shutdown()
        self._drained.wait(timeout=1.0)
        return self._wait(2.0), bytes(self._tail).decode("utf-8", "replace")

    def _wait(self, timeout: float) -> Any:
        try:
            return self.proc.wait(timeout=timeout)
        except TypeError:  # a fake without the keyword
            return self.proc.wait()
        except subprocess.TimeoutExpired:
            return None


class Dialer:
    """Dials the hub (SD12) and owns the retry policy: one cycle serves one stream per
    target, a pre-greeting connect failure falls back to the name once, and a host key
    mismatch makes the Dialer terminal."""

    BACKOFF_MAX = 60.0
    BACKOFF_JITTER = 0.2
    STEADY_SECONDS = 60.0   # a stream up at least this long resets the backoff sequence
    SLICE = 0.5             # sleep slices, so a stop is noticed at once

    def __init__(self, config: Dict[str, Any], paths: Any, *,
                 spawn: Callable = subprocess.Popen, clock: Callable = time.monotonic,
                 sleep: Callable = time.sleep, rng: Callable = random.random,
                 env: Optional[Dict[str, str]] = None):
        self.config, self.paths = config, paths
        self._spawn, self._clock, self._sleep, self._rng = spawn, clock, sleep, rng
        self._env = env
        self.state, self.last_error = "idle", None   # "idle" or "host_key_mismatch"
        self._failures = 0
        self._last_outcome, self._last_up_for = "other", 0.0

    def targets(self) -> List[str]:
        """SD12's dial order: the tailnet IPv4, then the link name when set."""
        targets = [self.config["hub_address"]]
        if self.config.get("hub_name"):
            targets.append(self.config["hub_name"])
        return targets

    def dial_cycle(self, run_stream: Callable[[SshChild], bool]) -> str:
        """One cycle: run_stream(child) serves one stream and reports whether a greeting
        arrived: "connected", or the failure class; "host_key" also turns terminal."""
        if self.state == "host_key_mismatch":
            self._last_outcome = "host_key"
            return "host_key"
        outcome = "other"
        for target in self.targets():
            child = self._start(target)
            started = self._clock()
            if child is None:
                outcome = "other"            # ssh itself missing: never a name fallback
            else:
                greeted = run_stream(child)
                self._last_up_for = self._clock() - started
                outcome = self._finish(child, greeted)
                if greeted:
                    self._last_outcome = "connected"
                    return "connected"
            if outcome == "host_key":
                self.state = "host_key_mismatch"
                break
            if outcome != "connect":
                break                        # auth, other, a spawn error: no fallback (SD12)
        self._last_outcome = outcome
        return outcome

    def _start(self, target: str) -> Optional[SshChild]:
        base = os.environ if self._env is None else self._env
        try:
            proc = self._spawn(ssh_argv(self.config, self.paths, target),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=_child_env(base))
        except OSError as exc:
            self.last_error = f"cannot start ssh: {exc}"
            return None
        return SshChild(proc)

    def _finish(self, child: SshChild, greeted: bool) -> str:
        code, stderr = child.finish()
        if greeted:
            return "connected"               # the stream ran; ssh's exit adds nothing
        failure = classify_ssh_failure(code, stderr)
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        self.last_error = f"{failure}: {lines[-1] if lines else f'exit {code}'}"
        return failure

    def backoff_delay(self, outcome: str, up_for: float) -> float:
        """1, 2, 4, ... seconds capped at BACKOFF_MAX with +-20 % jitter from rng; a stream
        up at least STEADY_SECONDS resets the sequence."""
        if outcome == "connected" and up_for >= self.STEADY_SECONDS:
            self._failures = 0
        self._failures = min(self._failures + 1, 8)   # 2**7 already passes the cap
        base = min(2.0 ** (self._failures - 1), self.BACKOFF_MAX)
        return base * (1.0 + (self._rng() * 2.0 - 1.0) * self.BACKOFF_JITTER)

    def wait_before_retry(self, should_stop: Optional[Callable[[], bool]] = None) -> None:
        """Sleeps the last cycle's backoff delay in slices through the injected sleep, so
        a stop is honoured at once; the delay is not restarted."""
        end = self._clock() + self.backoff_delay(self._last_outcome, self._last_up_for)
        while end > self._clock() and not (should_stop and should_stop()):
            self._sleep(min(end - self._clock(), self.SLICE))


def mail_problems(mail: Any) -> Optional[str]:
    """The one-line problem with a delivered envelope, None when storable; an invalid one
    raises out of store(), is answered spool_error and stays queued on the hub."""
    if not isinstance(mail, dict):
        return "mail must be a JSON object"
    for key in _MAIL_REQUIRED:
        if key not in mail:
            return f"mail is missing {key!r}"
    if not isinstance(mail["id"], str) or _MAIL_ID_RE.fullmatch(mail["id"]) is None:
        return "mail id must match ^[A-Za-z0-9_-]{1,80}$"
    return None


def _utc_now() -> str:
    """An ISO-UTC stamp in the hub's format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(stamp: Any) -> Optional[float]:
    """An ISO-UTC stamp as epoch seconds, None when missing or unparsable."""
    if not isinstance(stamp, str):
        return None
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of the directory fd (macOS refuses it); the rename stays atomic."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _silent_unlink(path: Path) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


class Spool:
    """Durable per-mail records under SessionPaths.spool: one <mail id>.json per delivered
    mail, mode 0600, written through a temp file with fsync and rename; store() returns
    only after the record is on disk. Unreadable files are skipped and logged."""

    RETRIEVED_TTL = 24 * 3600

    def __init__(self, paths: Any):
        self.dir = Path(paths.spool)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def store(self, mail: Dict[str, Any]) -> bool:
        """Writes one unread record; False when this id is already spooled (untouched)."""
        problem = mail_problems(mail)
        if problem is not None:
            raise ValueError(problem)
        path = self._path(mail["id"])
        if path.exists():
            return False
        record = {"mail": mail, "session": str(mail["to"]).split("@", 1)[0],
                  "state": "unread", "stored": _utc_now()}
        self._write(record, path)
        return True

    def unread(self, session_name: str) -> List[Dict[str, Any]]:
        """The unread mail of one session, oldest first by created then id."""
        mails = [record["mail"] for record, _ in self._scan()
                 if record.get("session") == session_name
                 and record.get("state") == "unread" and isinstance(record.get("mail"), dict)]
        return sorted(mails, key=lambda m: (str(m.get("created", "")), str(m.get("id", ""))))

    def unread_count(self, session_name: str) -> int:
        return len(self.unread(session_name))

    def mark_retrieved(self, ids: List[str]) -> List[str]:
        """Durably flips records to "retrieved"; returns the ids it actually marked."""
        marked: List[str] = []
        for mail_id in ids:
            if not isinstance(mail_id, str) or _MAIL_ID_RE.fullmatch(mail_id) is None:
                continue
            record = self._read(self._path(mail_id))
            if record is None or record.get("state") != "unread":
                continue
            record["state"] = "retrieved"
            record["retrieved"] = _utc_now()
            self._write(record, self._path(mail_id))
            marked.append(mail_id)
        return marked

    def prune(self, now: float) -> int:
        """Deletes retrieved records past RETRIEVED_TTL and expired mail's records; now is
        epoch seconds. Returns the files removed."""
        removed = 0
        for record, path in self._scan():
            mail = record.get("mail") if isinstance(record.get("mail"), dict) else {}
            retrieved, expires = _epoch(record.get("retrieved")), _epoch(mail.get("expires"))
            stale = (retrieved is not None and now - retrieved >= self.RETRIEVED_TTL)
            if stale or (expires is not None and expires <= now):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    log(f"spool: cannot remove {path.name}: {exc}")
        return removed

    def _scan(self):
        """Every readable record with its path; corrupt or foreign files are skipped."""
        try:
            names = sorted(os.listdir(str(self.dir)))
        except OSError as exc:
            log(f"spool: cannot list {self.dir}: {exc}")
            return
        for name in names:
            mail_id = name[:-len(".json")] if name.endswith(".json") else ""
            if mail_id and _MAIL_ID_RE.fullmatch(mail_id) is not None:
                record = self._read(self.dir / name)
                if record is not None:
                    yield record, self.dir / name

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"spool: skipping unreadable record {path.name}: {exc}")
            return None
        return record if isinstance(record, dict) else None

    def _path(self, mail_id: str) -> Path:
        return self.dir / f"{mail_id}.json"

    def _write(self, record: Dict[str, Any], path: Path) -> None:
        """Durably replaces one record: a 0600 temp file, flush, fsync, rename."""
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            _silent_unlink(Path(tmp))
            raise
        _fsync_dir(self.dir)


class _Pending:
    """One request waiting for the hub's reply (the event carries the answer)."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: Optional[Dict[str, Any]] = None


class LinkClient:
    """The daemon's end of one link stream, the peer of the hub's session_link.LinkSession
    (a reader thread parses NDJSON into a queue, a writer thread does every write, run()
    owns the state). request() is safe from other threads: that is how the daemon serves
    the session MCP calls through the uplink."""

    HANDSHAKE_TIMEOUT = 20.0    # the greeting and the welcome each wait this long
    PING_INTERVAL = 30.0
    LIVENESS_TIMEOUT = 90.0     # no frame this long ends the stream (S0 part 3)
    WRITER_QUEUE_MAX = 256
    WRITER_GRACE = 5.0
    OPS = frozenset({"bind", "unbind", "state", "send", "ack", "ping", "list",
                     "query_submit", "query_get", "review_submit", "review_get"})

    def __init__(self, reader: Any, writer: Any, workstation: str, spool: Spool, *,
                 clock: Callable = time.monotonic, on_deliver: Optional[Callable] = None,
                 on_state: Optional[Callable] = None, wake: float = 0.2):
        self.reader, self.writer = reader, writer
        self.workstation, self.spool = workstation, spool
        self.clock, self.on_deliver, self.on_state, self.wake = clock, on_deliver, on_state, wake
        self._frames: "queue.Queue" = queue.Queue()
        self._out: "queue.Queue" = queue.Queue(maxsize=self.WRITER_QUEUE_MAX)
        self._pending: Dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self.lease, self.close_reason = None, None
        self.greeted = False      # any greeting marks the dial as connected for the Dialer
        self._closed, self._last_recv, self._last_ping = False, 0.0, 0.0
        self._reader_thread = self._writer_thread = None

    @property
    def up(self) -> bool:
        """True between the welcome and the end of the stream."""
        return self.lease is not None and not self._closed

    def run(self) -> str:
        """Serves the stream until it ends; returns the close reason."""
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True,
                                               name=f"uplink-writer-{self.workstation}")
        self._writer_thread.start()
        self._reader_thread = threading.Thread(target=self._read_frames, daemon=True,
                                               name=f"uplink-reader-{self.workstation}")
        self._reader_thread.start()
        try:
            if self._handshake():
                self._last_ping = self.clock()
                while not self._closed:
                    self._pump()
                    if not self._closed:
                        self._tick(self.clock())
        except Exception as exc:  # noqa: BLE001 - a crash still ends the stream cleanly
            self._close(f"crash: {exc}")
        finally:
            self._shutdown()
        return self.close_reason or "stopped"

    def _handshake(self) -> bool:
        """greeting -> hello -> welcome: a first frame other than the hub's greeting for
        this workstation ends as bad_greeting with no hello sent; a greeting at all, even
        a mismatched one, still marks the dial as connected."""
        frame = self._next_frame(self.HANDSHAKE_TIMEOUT, "greeting_timeout")
        if frame is None:
            return False
        self._last_recv = self.clock()
        if frame.get("t") == "greeting":
            self.greeted = True
        problem = self._greeting_problem(frame)
        if problem is not None:
            log(f"{self.workstation}: greeting refused: {problem}")
            self._close("bad_greeting")
            return False
        self._send({"t": "hello", "proto": session_local.LINK_PROTO,
                    "version": session_local.LINK_VERSION})
        frame = self._next_frame(self.HANDSHAKE_TIMEOUT, "welcome_timeout")
        if frame is None:
            return False
        self._last_recv = self.clock()
        if frame.get("t") == "bye":
            self._close(str(frame.get("reason") or "bye"))
        elif frame.get("t") != "welcome" or not isinstance(frame.get("lease"), int):
            log(f"{self.workstation}: expected welcome, got {frame.get('t')!r}")
            self._close("bad_welcome")
        else:
            self.lease = frame["lease"]
            if self.on_state is not None:
                _call_quietly(self.on_state, "up", "")
            return True
        return False

    def _greeting_problem(self, frame: Dict[str, Any]) -> Optional[str]:
        if frame.get("t") != "greeting":
            return f"the first frame must be greeting, not {frame.get('t')!r}"
        for key, want in (("proto", session_local.LINK_PROTO),
                          ("version", session_local.LINK_VERSION),
                          ("workstation", self.workstation)):
            if frame.get(key) != want:
                return f"greeting {key} is {frame.get(key)!r}, not {want!r}"
        return None

    def _pump(self) -> None:
        try:
            item = self._frames.get(timeout=self.wake)
        except queue.Empty:
            return
        if isinstance(item, tuple):   # ("_close", reason) from the reader or writer thread
            self._close(item[1])
            return
        self._last_recv = self.clock()
        self._handle(item)

    def _tick(self, now: float) -> None:
        """Pings on the interval (fire-and-forget) and ends the stream on silence."""
        if now - self._last_ping >= self.PING_INTERVAL:
            self._last_ping = now
            with self._lock:
                self._next_id += 1
                rid = f"d{self._next_id}"
            self._send({"t": "ping", "id": rid})
        if now - self._last_recv > self.LIVENESS_TIMEOUT:
            self._close("liveness")

    def _next_frame(self, seconds: float, timeout_reason: str) -> Optional[Dict[str, Any]]:
        """The next frame within seconds; None once the stream closed (the reader's or
        writer's reason, a stop's reason or timeout_reason becomes the close reason)."""
        deadline = self.clock() + seconds
        while True:
            if self._closed:
                return None
            remaining = deadline - self.clock()
            if remaining <= 0:
                self._close(timeout_reason)
                return None
            try:
                item = self._frames.get(timeout=min(remaining, self.wake))
            except queue.Empty:
                continue
            if isinstance(item, tuple):
                self._close(item[1])
                return None
            return item

    def _handle(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "reply":
            self._on_reply(frame)
        elif kind == "ping":
            self._reply(frame.get("id"), True)
        elif kind == "bye":
            self._close(str(frame.get("reason") or "bye"))
        elif kind == "deliver":
            self._on_deliver(frame)
        else:
            self._reply(frame.get("id"), False, "unknown_frame", f"no frame {kind!r}")

    def _on_deliver(self, frame: Dict[str, Any]) -> None:
        """One mail from the hub: stored durably first, acked after; a dedupe is ok too."""
        rid, mail = frame.get("id"), frame.get("mail")
        try:
            fresh = self.spool.store(mail)
        except Exception as exc:  # noqa: BLE001 - the mail stays queued on the hub
            log(f"{self.workstation}: spool refused a mail: {exc}")
            self._reply(rid, False, "spool_error")
            return
        if self.on_deliver is not None:
            _call_quietly(self.on_deliver, mail, fresh)
        self._reply(rid, True)

    def request(self, op: str, payload: Optional[Dict[str, Any]] = None,
                timeout: float = session_local.HUB_CALL_TIMEOUT) -> Dict[str, Any]:
        """One round trip, safe from any thread: sends {"t": op, "id": "d<n>", **payload}
        and blocks for the hub's reply to it, or answers timeout / link_down."""
        if op not in self.OPS:
            raise ValueError(f"unknown link op {op!r}")
        payload = payload or {}
        with self._lock:
            if not self.up:
                return {"ok": False, "error": "link_down"}
            self._next_id += 1
            rid = f"d{self._next_id}"
            pending = _Pending()
            self._pending[rid] = pending
        self._send({"t": op, "id": rid, **payload})
        deadline = self.clock() + timeout
        while not pending.event.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            pending.event.wait(min(remaining, self.wake))
        with self._lock:
            entry = self._pending.pop(rid, None)
        if entry is not None:
            return {"ok": False, "error": "timeout"}
        if pending.reply is None:  # the reply is being placed right after its pop
            pending.event.wait(0.1)
        return pending.reply or {"ok": False, "error": "timeout"}

    def _on_reply(self, frame: Dict[str, Any]) -> None:
        with self._lock:
            pending = self._pending.pop(frame.get("re"), None)
        if pending is not None:
            pending.reply = frame
            pending.event.set()

    def _read_frames(self) -> None:
        while True:
            try:
                line = self.reader.readline()
            except (OSError, ValueError) as exc:
                self._frames.put(("_close", f"read_error: {exc}"))
                return
            if not line:
                self._frames.put(("_close", "stream_end"))
                return
            if len(line) > session_local.MAX_LINE_BYTES + 1:
                self._frames.put(("_close", "line_too_long"))
                return
            try:
                frame = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._frames.put(("_close", "bad_json"))
                return
            if not isinstance(frame, dict):
                self._frames.put(("_close", "bad_frame"))
                return
            self._frames.put(frame)

    def _writer_loop(self) -> None:
        while True:
            item = self._out.get()
            if item is None:    # the shutdown sentinel behind the bye frame
                return
            try:
                self.writer.write(item)
                self.writer.flush()
            except (OSError, ValueError, TypeError) as exc:
                self._frames.put(("_close", f"write_error: {exc}"))
                return

    def _queue_frame(self, frame: Dict[str, Any]) -> None:
        try:
            self._out.put_nowait((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        except queue.Full:
            self._close("writer_backlog")

    def _send(self, frame: Dict[str, Any]) -> None:
        if not self._closed:
            self._queue_frame(frame)

    def _reply(self, rid: Any, ok: bool, error: str = "", detail: Any = "") -> None:
        frame: Dict[str, Any] = {"t": "reply", "re": rid, "ok": ok}
        if not ok:
            frame["error"] = error
            if detail:
                frame["detail"] = str(detail)
        self._send(frame)

    def stop(self, reason: str = "stopped") -> None:
        """Ends the stream from outside (shutdown runs in the run() thread)."""
        self._close(reason)

    def _close(self, reason: str) -> None:
        if not self._closed:
            self._closed = True
            self.close_reason = reason

    def _shutdown(self) -> None:
        """State down, pendings failed with link_down, the bye, then the transports."""
        if self.close_reason is None:
            self.close_reason = "stopped"
        self._closed = True
        reason = self.close_reason
        if self.on_state is not None:
            _call_quietly(self.on_state, "down", reason)
        with self._lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.reply = {"ok": False, "error": "link_down"}
            pending.event.set()
        self._queue_frame({"t": "bye", "reason": reason})
        try:
            self._out.put(None, timeout=self.WRITER_GRACE)   # behind the bye frame
        except queue.Full:
            pass
        writer, reader = self._writer_thread, self._reader_thread
        if writer is not None:
            writer.join(timeout=self.WRITER_GRACE)
            if not writer.is_alive():
                _close_stream(self.writer)
        if reader is not None:
            reader.join(timeout=0.5)
        if reader is None or not reader.is_alive():
            _close_stream(self.reader)


class Uplink:
    """Dialer and LinkClient on one background thread, run forever until stop(): the
    daemon's whole hub connection. Exposes state ("connecting", "up", "down",
    "host_key_mismatch", "stopped"), last_error, request() (link_down while no stream is
    up) and the on_up callbacks, which fire after every reconnect: a new lease marks the
    hub's session rows offline and the daemon re-binds its live sessions."""

    def __init__(self, config: Dict[str, Any], paths: Any, spool: Spool, *,
                 dialer: Optional[Dialer] = None, clock: Callable = time.monotonic,
                 on_up: Optional[List[Callable]] = None,
                 on_deliver: Optional[Callable] = None, wake: float = 0.2):
        self.config, self.paths, self.spool = config, paths, spool
        self._clock, self._wake = clock, wake
        self._on_up = list(on_up) if on_up else []
        self._on_deliver = on_deliver
        self._dialer = dialer if dialer is not None else Dialer(config, paths)
        self.state, self.last_error = "connecting", None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._client, self._child, self._thread = None, None, None

    def start(self) -> "Uplink":
        self._thread = threading.Thread(target=self._run, daemon=True, name="session-uplink")
        self._thread.start()
        return self

    def stop(self) -> None:
        """Ends the current stream (the child sees EOF, both ends end cleanly) and joins
        the loop thread."""
        self._stop.set()
        with self._lock:
            child = self._child
        if child is not None:
            child.shutdown()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15.0)

    def request(self, op: str, payload: Optional[Dict[str, Any]] = None,
                timeout: float = session_local.HUB_CALL_TIMEOUT) -> Dict[str, Any]:
        """Delegates to the current stream's client; link_down while none is up."""
        with self._lock:
            client = self._client
        if client is None:
            return {"ok": False, "error": "link_down"}
        return client.request(op, payload, timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._dialer.state == "host_key_mismatch":
                self._set_state("host_key_mismatch", self._dialer.last_error)
                self._stop.wait(self._wake)   # terminal: no dial until the process restarts
                continue
            self._set_state("connecting", None)
            self._dialer.dial_cycle(self._serve_child)   # outcome kept inside the Dialer
            if self._stop.is_set():
                break
            self._set_state("down", self._dialer.last_error)
            self._dialer.wait_before_retry(self._stop.is_set)
        self._set_state("stopped", None)

    def _serve_child(self, child: SshChild) -> bool:
        """One stream on one dialed child; returns whether a greeting arrived."""
        client = LinkClient(child.proc.stdout, child.proc.stdin, self.config["workstation"],
                            self.spool, clock=self._clock, on_deliver=self._on_deliver,
                            on_state=self._on_client_state, wake=self._wake)
        with self._lock:
            self._client, self._child = client, child
        try:
            client.run()
        finally:
            with self._lock:
                if self._client is client:
                    self._client, self._child = None, None
        return client.greeted

    def _on_client_state(self, state: str, reason: str) -> None:
        if state == "up":
            self._set_state("up", None)
            for callback in self._on_up:
                _call_quietly(callback)
        else:
            self._set_state("down", reason or None)

    def _set_state(self, state: str, error: Optional[str]) -> None:
        if state != self.state or error != self.last_error:
            log(f"{self.config['workstation']}: {state}" + (f" ({error})" if error else ""))
        self.state = state
        self.last_error = error

"""The hub's end of one workstation session link (S1c of docs/design/operator-sessions.md).

sshd starts this module as the forced command of one authorized_keys2 line per workstation
(`guardian.py --session-link --workstation <id>`); stdin/stdout are the SSH stream of the
workstation's link daemon (built in S2), which dialed the hub (SD1). entry() refuses, in order and
before it writes anything, a non-empty SSH_ORIGINAL_COMMAND (silently: not one word, S0 part 2), a
terminal on stdin, the kill switch and an unknown workstation; it loads the hub's agent.env for
the GUARDIAN_* keys (a forced command gets no systemd EnvironmentFile) and pins LANG and PATH.
What follows is one NDJSON frame stream (one JSON object per line, at most
mail_relay.MAX_LINE_BYTES):

    hub    greeting {proto, version, workstation}   sent first, before reading anything
    daemon hello {proto, version}                   within HANDSHAKE_TIMEOUT
    hub    welcome {lease}                          after store.link_claim
    daemon bind / unbind / state / send / ack / list / query_submit / query_get /
           review_submit / review_get / ping requests, answered {"t":"reply","re":id}
    hub    deliver {mail} and ping requests (ids h<n>), pending until the reply or 20 s

A dial that never completes the handshake claims nothing, so it cannot cut a healthy link
(Architecture rule 2); only a completed handshake takes the link over, and the old stream notices
at its next poll (SQLite has no notifications). Every store call is fenced by the lease: a
`superseded` answer is replied and then ends the stream without link_end, because another stream
owns the link now. The 2 s poll also watches the workstation registry (removing a workstation
ends its stream), the kill switch and queued session mail; no frame for 90 s ends the stream with
`liveness` and the mail stays queued. Writes go through a bounded writer queue, so the main loop
never blocks on a stuck stream; the end path flushes the bye frame within WRITER_GRACE and
returns even when the writer is still blocked (every thread is a daemon).

The review ops (V2d of docs/design/verification-layer.md) spawn nothing: the link entry calls the
hub's own POST /api/reviews on loopback through ReviewAPI with the hub's AGENT_TOKEN, which is
read on the hub and never crosses the stream. started_by is this link's own workstation, never a
client value; review_get answers only reviews with that started_by, exactly as for an unknown id
otherwise. The workstation is untrusted, so every field is re-validated here and extra fields are
refused outright.
"""

import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import agent_client
import code_context
import llm_runner
import mail_relay
import mail_store
import session_registry

LINK_PROTO = "guardian-session-link"
LINK_VERSION = 1
WRITER_QUEUE_MAX = 256
# The review tools: the frame shapes the daemon may send and the trimmed answer's caps.
# REVIEW_ID_RE mirrors the hub's review ids (review_agent.RUN_ID); test_session_link.py pins it.
REVIEW_ID_RE = re.compile(r"^review_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$", re.ASCII)
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)
REVIEW_API_BASE = "http://127.0.0.1:9870"
REVIEW_API_TIMEOUT = 30.0     # a short ceiling; a POST answers within the solo start grace
REVIEW_RESULT_MAX_BYTES = 64 * 1024   # the largest trimmed answer a workstation session may see
REVIEW_TEXT_CHARS = 800       # one summary, title or detail, like the runner's MAX_TEXT_CHARS
_REVIEW_SUBMIT_FIELDS = frozenset({"t", "id", "name", "project", "ref", "base", "expect_sha",
                                    "provider"})
_REVIEW_GET_FIELDS = frozenset({"t", "id", "name", "review_id"})
# The reasons a stream ends as expected: every other end (and every pre-claim end) is an error.
NORMAL_END_REASONS = frozenset({"stream_end", "superseded", "removed", "sessions_off"})


def log(message: str) -> None:
    """One line per link start and end on stderr; no secrets and never mail bodies."""
    print(f"session-link: {message}", file=sys.stderr, flush=True)


def _strip_quotes(value: str) -> str:
    """One pair of surrounding single or double quotes, as in a systemd EnvironmentFile."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _load_agent_env(env: Dict[str, str], path: Path) -> None:
    """Copies the GUARDIAN_* keys of the hub's agent.env into env; other keys (AGENT_TOKEN and
    the like) never cross, and a key already set in env always wins. A missing file is fine."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("GUARDIAN_") and key not in env:
            env[key] = _strip_quotes(value.strip())


def _sessions_off(env: Dict[str, str]) -> bool:
    """The kill switch as this process sees it: the env entry() built. In production env is
    os.environ (entry is called with it), so this is mail_store.sessions_off() over the merged
    view of agent.env and the environment sshd passed."""
    return str(env.get("GUARDIAN_SESSIONS", "")).strip().lower() == "off"


def _is_terminal(stdin: Any) -> bool:
    """A refused -tt still starts the forced command (S0 part 3): the entry refuses a terminal on
    stdin itself. Objects without a usable fileno count as no terminal."""
    try:
        return bool(os.isatty(stdin.fileno()))
    except (AttributeError, OSError, ValueError):
        return False


def _refused(stderr: Any, message: str) -> int:
    """The one-line refusal of a pre-stream check; exit code 2."""
    print(f"session-link: {message}", file=stderr, flush=True)
    return 2


def _missing(frame: Dict[str, Any], keys: Sequence[str]) -> List[str]:
    return [key for key in keys if key not in frame]


def _superseded(answer: Dict[str, Any]) -> bool:
    """Whether a store answer says another stream took this workstation's link."""
    return not answer.get("ok") and answer.get("reason") == "superseded"


def entry(workstation: str, *, env=os.environ, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer,
          stderr=sys.stderr, agent_env_path: Optional[Path] = None) -> int:
    """The forced command of one authorized_keys2 line: the refusals in order (each touches
    neither the store nor any stream frame), then the session on the SSH stream. Returns the
    process exit code: 0 a normal end, 1 an error end, 2 a refusal."""
    if env.get("SSH_ORIGINAL_COMMAND"):
        return 2  # a client command or subsystem: refused with no output at all
    _load_agent_env(env, Path(agent_env_path) if agent_env_path is not None
                    else Path.home() / ".guardian" / "agent.env")
    env["LANG"], env["PATH"] = "C.UTF-8", "/usr/bin:/bin"  # the login shell's PATH is not ours
    if _is_terminal(stdin):
        return _refused(stderr, "stdin is a terminal: the link daemon dials with ssh -T")
    if _sessions_off(env):
        return _refused(stderr, "sessions off")
    if (not isinstance(workstation, str) or not mail_store.NODE_RE.fullmatch(workstation)
            or session_registry.get_workstation(workstation) is None):
        return _refused(stderr, f"unknown workstation {workstation!r}")
    return LinkSession(workstation, stdin, stdout).run()


class ReviewAPIError(Exception):
    """One failed call to the hub's own review API; message is the hub's own error text (never a
    token, never a header), status the HTTP code, 0 when the API was unreachable."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _hub_api_error_text(exc: "urllib.error.HTTPError") -> str:
    """The hub's own message of one HTTP error (the hub's errJSON answers {"error": msg})."""
    try:
        answer = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - the body is best effort
        answer = None
    if isinstance(answer, dict) and isinstance(answer.get("error"), str) and answer["error"]:
        return answer["error"]
    return f"HTTP {exc.code}"


class ReviewAPI:
    """The hub's review API on loopback, as the link handlers call it: submit (POST /api/reviews)
    and get (GET /api/reviews/{id}) with the hub's own AGENT_TOKEN from agent_client (read here on
    the hub, never sent to a workstation, and no workstation value ever reaches a URL or header).
    Injected as LinkSession(review_api=...) in tests; the default talks to 127.0.0.1:9870."""

    def __init__(self, base_url: str = REVIEW_API_BASE, timeout: float = REVIEW_API_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def _fetch(self, endpoint: str, method: str,
               body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{endpoint}", method=method)
        request.add_header("Content-Type", "application/json")
        token = agent_client.agent_token()
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            with urllib.request.urlopen(request, data=payload, timeout=self.timeout) as res:
                text = res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ReviewAPIError(_hub_api_error_text(exc), status=exc.code) from None
        except Exception as exc:  # noqa: BLE001 - any transport failure is one error line
            raise ReviewAPIError(f"cannot reach the hub's review API: {exc}") from None
        try:
            answer = json.loads(text)
        except ValueError:
            raise ReviewAPIError("the hub's review API did not answer JSON") from None
        if not isinstance(answer, dict):
            raise ReviewAPIError("the hub's review API did not answer an object") from None
        return answer

    def submit(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/reviews: starts one review; the answer is {id, status, label, note}."""
        return self._fetch("/api/reviews", "POST", body)

    def get(self, review_id: str) -> Dict[str, Any]:
        """GET /api/reviews/{id}: the review view, `request` included when the record has it."""
        return self._fetch(f"/api/reviews/{review_id}", "GET")


def _trim_review(view: Dict[str, Any]) -> Dict[str, Any]:
    """The trimmed answer of review_get: the view's head fields, and for a completed review the
    result's state, summary, counts by severity and findings (severity, title, file, line,
    detail), capped at REVIEW_RESULT_MAX_BYTES by dropping findings from the end, with truncated
    set when any were dropped (the counts keep the real totals)."""
    out: Dict[str, Any] = {"id": view.get("id"), "status": view.get("status"),
                           "label": view.get("label"), "note": view.get("note")}
    if view.get("error"):
        out["error"] = view["error"]
    result = view.get("result")
    if view.get("status") != "completed" or not isinstance(result, dict):
        return out
    findings: List[Dict[str, Any]] = []
    counts = {"high": 0, "medium": 0, "low": 0}
    items = result.get("findings") if isinstance(result.get("findings"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity") if item.get("severity") in counts else "medium"
        counts[severity] += 1
        line = item.get("line")
        findings.append({
            "severity": severity,
            "title": str(item.get("claim") or "")[:REVIEW_TEXT_CHARS],
            "file": str(item.get("path") or "")[:512],
            "line": line if isinstance(line, int) and not isinstance(line, bool) else 0,
            "detail": str(item.get("scenario") or "")[:REVIEW_TEXT_CHARS],
        })
    out["state"] = result.get("state")
    out["summary"] = str(result.get("summary") or "")[:REVIEW_TEXT_CHARS]
    out["counts"] = counts
    while findings and len(json.dumps({**out, "findings": findings},
                                      ensure_ascii=False).encode("utf-8")) > REVIEW_RESULT_MAX_BYTES:
        findings.pop()
        out["truncated"] = True
    out.setdefault("truncated", False)
    out["findings"] = findings
    return out


def _review_submit_problems(frame: Dict[str, Any]) -> List[str]:
    """Every problem with one review_submit frame; the workstation is untrusted, so the hub
    re-validates each field and refuses extra fields outright, identity keys included."""
    problems: List[str] = []
    extra = sorted(set(frame) - _REVIEW_SUBMIT_FIELDS)
    if extra:
        problems.append(f"unknown field(s): {', '.join(extra)}")
    for key in ("name", "project", "ref"):
        if not isinstance(frame.get(key), str) or not frame[key]:
            problems.append(f"missing field {key!r}")
    project = frame.get("project")
    if isinstance(project, str) and project and _PROJECT_ID_RE.fullmatch(project) is None:
        problems.append("'project' must be a project id like server-guardian")
    for key in ("ref", "base"):
        value = frame.get(key)
        if isinstance(value, str) and value:
            try:
                code_context.validate_ref(value)
            except code_context.ContextError as exc:
                problems.append(str(exc))
    sha = frame.get("expect_sha")
    if sha is not None and (not isinstance(sha, str) or _SHA1_RE.fullmatch(sha) is None):
        problems.append("'expect_sha' must be 40 lower-case hex characters")
    provider = frame.get("provider")
    if provider is not None and (not isinstance(provider, str)
                                 or (provider != "" and provider not in llm_runner.KNOWN_PROVIDERS)):
        problems.append("'provider' must be a known provider name or empty")
    return problems


def _review_get_problems(frame: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    extra = sorted(set(frame) - _REVIEW_GET_FIELDS)
    if extra:
        problems.append(f"unknown field(s): {', '.join(extra)}")
    for key in ("name", "review_id"):
        if not isinstance(frame.get(key), str) or not frame[key]:
            problems.append(f"missing field {key!r}")
    review_id = frame.get("review_id")
    if isinstance(review_id, str) and review_id and REVIEW_ID_RE.fullmatch(review_id) is None:
        problems.append("'review_id' must match review_YYYYMMDD_HHMMSS_hex")
    return problems


class LinkSession:
    """One stream to one workstation. A reader thread only parses lines into a queue and a writer
    thread does every write; run() is the single thread that touches the store (one MailStore per
    session: sqlite connections are per thread). Tests run it over fake streams with an injected
    clock."""

    HANDSHAKE_TIMEOUT = 20.0
    POLL_INTERVAL = 2.0
    PING_INTERVAL = 30.0
    LIVENESS_TIMEOUT = 90.0
    REQUEST_TIMEOUT = 20.0
    WRITER_GRACE = 5.0

    def __init__(self, workstation: str, reader: Any, writer: Any, *,
                 store: Optional[mail_store.MailStore] = None,
                 clock: Callable[[], float] = time.monotonic,
                 registry_get: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                 wake: float = 0.2, review_api: Optional[Any] = None):
        self.workstation = workstation
        self.reader, self.writer = reader, writer
        self.store = store if store is not None else mail_store.MailStore()
        self.clock = clock
        self.wake = wake  # the wall-clock wait for the next frame or tick, never the clock()
        self._registry_get = registry_get or session_registry.get_workstation
        # the hub's own review API on loopback; injectable so the tests need no real hub
        self.review_api = review_api if review_api is not None else ReviewAPI()
        self._frames: "queue.Queue" = queue.Queue()
        self._out: "queue.Queue" = queue.Queue(maxsize=WRITER_QUEUE_MAX)
        self._pending: Dict[str, Tuple[str, Dict[str, Any], float]] = {}
        self._inflight: set = set()
        self._next_id = 0
        self.lease: Optional[int] = None
        self.close_reason: Optional[str] = None
        self._closed = False
        self._link_ended = False
        self._last_recv = 0.0
        self._last_ping = self._last_poll = 0.0
        self._reader_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None

    # --- the session loop ----------------------------------------------------------------

    def run(self) -> int:
        """Serves the stream until it ends: 0 a normal end, 1 an error end."""
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True,
                                               name=f"link-writer-{self.workstation}")
        self._writer_thread.start()
        self._send({"t": "greeting", "proto": LINK_PROTO, "version": LINK_VERSION,
                    "workstation": self.workstation})  # out before reading anything
        self._reader_thread = threading.Thread(target=self._read_frames, daemon=True,
                                               name=f"link-reader-{self.workstation}")
        self._reader_thread.start()
        try:
            if self._handshake():
                self._serve()
        except Exception as exc:  # noqa: BLE001 - a crash still ends the link with a code
            self._close(f"crash: {exc}")
        finally:
            self._shutdown()
        return 0 if (self.lease is not None
                     and self.close_reason in NORMAL_END_REASONS) else 1

    def _handshake(self) -> bool:
        """Waits for the daemon's hello and claims the link. A dial that fails here claims
        nothing: it must not cut a healthy link (Architecture rule 2)."""
        deadline = self.clock() + self.HANDSHAKE_TIMEOUT
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                self._close("handshake_timeout")
                return False
            try:
                item = self._frames.get(timeout=min(remaining, self.wake))
            except queue.Empty:
                continue
            if isinstance(item, tuple):  # the stream ended before any hello
                self._close(item[1])
                return False
            self._last_recv = self.clock()
            problem = self._hello_problem(item)
            if problem is not None:
                log(f"{self.workstation}: handshake refused: {problem}")
                self._close("protocol_mismatch")
                return False
            return self._claim()

    def _hello_problem(self, frame: Dict[str, Any]) -> Optional[str]:
        if frame.get("t") != "hello":
            return f"the first frame must be hello, not {frame.get('t')!r}"
        if frame.get("proto") != LINK_PROTO:
            return f"the link protocol is {LINK_PROTO!r}, not {frame.get('proto')!r}"
        if frame.get("version") != LINK_VERSION:
            return f"the link protocol version is {LINK_VERSION}, not {frame.get('version')!r}"
        return None

    def _claim(self) -> bool:
        """link_claim -> lease -> welcome. A refused claim ends the dial with no lease, so
        nothing is ever written for it (no link_end, no sessions offline)."""
        try:
            answer = self.store.link_claim(self.workstation)
        except Exception as exc:  # noqa: BLE001 - the short reason is all the log needs
            self._close(f"claim_error: {exc}")
            return False
        if not answer.get("ok"):
            self._close(answer.get("reason", "refused"))
            return False
        self.lease = answer["lease"]
        self._send({"t": "welcome", "lease": self.lease})
        return True

    def _serve(self) -> None:
        log(f"{self.workstation}: link up (lease {self.lease})")
        now = self.clock()
        self._last_ping, self._last_poll = now, now
        self._poll()  # mail queued while the link was down leaves at once, and an immediate
        # takeover is caught here rather than at the first tick
        while not self._closed:
            self._pump()
            if self._closed:
                break
            self._tick(self.clock())

    def _pump(self) -> None:
        try:
            item = self._frames.get(timeout=self.wake)
        except queue.Empty:
            return
        if isinstance(item, tuple):  # ("_close", reason) from the reader or the writer thread
            self._close(item[1])
            return
        self._last_recv = self.clock()
        self._handle(item)

    def _tick(self, now: float) -> None:
        self._drop_timed_out(now)
        if now - self._last_poll >= self.POLL_INTERVAL:
            self._last_poll = now
            self._poll()
        if now - self._last_ping >= self.PING_INTERVAL:
            self._last_ping = now
            self._request("ping", {})
        if now - self._last_recv > self.LIVENESS_TIMEOUT:
            self._close("liveness")

    def _poll(self) -> None:
        """The 2 s poll (SQLite has no notifications): registry removal, the kill switch, the
        lease, queued mail and the offline release, in that order."""
        if self._registry_get(self.workstation) is None:
            self._close("removed")
            return
        if mail_store.sessions_off():
            self._close("sessions_off")
            return
        if not self.store.link_current(self.workstation, self.lease):
            self._close("superseded")  # another stream holds the link: no link_end for this one
            return
        self._deliver_queued()
        self.store.release_offline_sessions()

    # --- reading and writing -------------------------------------------------------------

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
            if len(line) > mail_relay.MAX_LINE_BYTES + 1:
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
            if item is None:  # the shutdown sentinel behind the bye frame
                return
            try:
                self.writer.write(item)
                self.writer.flush()
            except (OSError, ValueError, TypeError) as exc:
                self._frames.put(("_close", f"write_error: {exc}"))
                return

    def _queue_frame(self, frame: Dict[str, Any]) -> None:
        """Hands one frame to the writer thread without ever blocking the caller; a full queue
        means the stream is stuck and ends the session (the bye of an already closed session is
        simply abandoned)."""
        try:
            self._out.put_nowait((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        except queue.Full:
            self._close("writer_backlog")

    def _send(self, frame: Dict[str, Any]) -> None:
        if self._closed:
            return
        self._queue_frame(frame)

    def _request(self, kind: str, payload: Dict[str, Any],
                 context: Optional[Dict[str, Any]] = None) -> str:
        """Sends a hub request and holds it as pending until its reply or the 20 s timeout."""
        self._next_id += 1
        rid = f"h{self._next_id}"
        self._pending[rid] = (kind, context or {}, self.clock())
        self._send({"t": kind, "id": rid, **payload})
        return rid

    def _reply_ok(self, rid: Any, **payload: Any) -> None:
        self._send({"t": "reply", "re": rid, "ok": True, **payload})

    def _reply_error(self, rid: Any, error: str, detail: Any = "") -> None:
        frame: Dict[str, Any] = {"t": "reply", "re": rid, "ok": False, "error": error}
        if detail:
            frame["detail"] = str(detail)
        self._send(frame)

    def _reply_result(self, rid: Any, answer: Dict[str, Any]) -> None:
        """A store answer {"ok": false, reason, detail} becomes error + detail; an ok answer is
        spread into the reply."""
        if answer.get("ok"):
            self._reply_ok(rid, **{key: value for key, value in answer.items() if key != "ok"})
        else:
            self._reply_error(rid, answer.get("reason", "error"), answer.get("detail", ""))

    # --- frames from the daemon ----------------------------------------------------------

    def _handle(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "reply":
            self._on_reply(frame)
            return
        rid = frame.get("id")
        handler = self._DAEMON_FRAMES.get(kind)
        if handler is None:
            self._reply_error(rid, "unknown_frame", f"no frame {kind!r}")
            return
        try:
            handler(self, frame)
        except Exception as exc:  # noqa: BLE001 - one poison frame never ends the stream
            self._reply_error(rid, "internal_error", str(exc) or exc.__class__.__name__)

    def _bad_frame(self, rid: Any, problems: List[str]) -> None:
        self._reply_error(rid, "bad_frame", "; ".join(problems))

    def _store_call(self, rid: Any, call: Callable[[], Dict[str, Any]]) -> None:
        """One fenced store call whose answer is the reply. `superseded` is replied first, then
        it ends the stream: this stream is fenced out from here on."""
        try:
            answer = call()
        except Exception as exc:  # noqa: BLE001 - a broken call must not end the stream
            self._reply_error(rid, "store_error", str(exc) or exc.__class__.__name__)
            return
        self._reply_result(rid, answer)
        if _superseded(answer):
            self._close("superseded")

    def _on_bind(self, frame: Dict[str, Any]) -> None:
        problems = _missing(frame, ("name", "cli_session"))
        peers = frame.get("peers")
        if peers is not None and not isinstance(peers, list):
            problems.append("peers must be a list of addresses")
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_bind(
            self.workstation, self.lease, frame["name"], frame.get("cli_session"),
            away=bool(frame.get("away")), peers=peers))

    def _on_unbind(self, frame: Dict[str, Any]) -> None:
        problems = _missing(frame, ("name",))
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_unbind(
            self.workstation, self.lease, frame["name"]))

    def _on_state(self, frame: Dict[str, Any]) -> None:
        problems = _missing(frame, ("name", "state"))
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_state(
            self.workstation, self.lease, frame["name"], frame["state"]))

    def _on_send(self, frame: Dict[str, Any]) -> None:
        # The daemon never chooses the caller fields: identity comes from the stream's
        # workstation and lease, so mail_relay.TOOL_DROP_KEYS never reach the store.
        args = {key: value for key, value in frame.items()
                if key not in mail_relay.TOOL_DROP_KEYS}
        problems = _missing(args, ("name", "to", "kind", "subject", "body", "op_id"))
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_send(
            self.workstation, self.lease, args["name"], args["to"], args["kind"],
            args["subject"], args["body"], op_id=args["op_id"], thread=args.get("thread"),
            reply_to=args.get("reply_to")))

    def _on_ack(self, frame: Dict[str, Any]) -> None:
        problems = _missing(frame, ("name", "ids"))
        if not isinstance(frame.get("ids"), list):
            problems.append("ids must be a list of mail ids")
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_ack(
            self.workstation, self.lease, frame["name"], frame["ids"]))

    def _on_list(self, frame: Dict[str, Any]) -> None:
        problems = _missing(frame, ("name",))
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_peers_list(
            self.workstation, self.lease, frame["name"]))

    def _on_query_submit(self, frame: Dict[str, Any]) -> None:
        args = {key: value for key, value in frame.items()
                if key not in mail_relay.TOOL_DROP_KEYS}
        problems = _missing(args, ("name", "to", "project", "prompt", "op_id"))
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        timeout_sec = args.get("timeout_sec")
        self._store_call(frame.get("id"), lambda: self.store.session_resident_query(
            self.workstation, self.lease, args["name"], args["to"], args["project"], args["prompt"],
            op_id=args["op_id"], task=args.get("task"),
            session_key=args.get("session_key"), mode=args.get("mode") or "in_place",
            timeout_sec=int(timeout_sec) if timeout_sec is not None else 180))

    def _on_query_get(self, frame: Dict[str, Any]) -> None:
        problems = _missing(frame, ("name", "query_id"))
        if problems:
            self._bad_frame(frame.get("id"), problems)
            return
        self._store_call(frame.get("id"), lambda: self.store.session_resident_query_get(
            self.workstation, self.lease, frame["name"], frame["query_id"]))

    def _review_session_live(self, rid: Any, name: str) -> bool:
        """The liveness check both review handlers share: the calling session must be bound and
        live under this stream's workstation and lease, checked exactly as the store-backed
        handlers check it (a superseded answer is replied, then the stream ends)."""
        answer = self.store.session_live(self.workstation, self.lease, name)
        if answer.get("ok"):
            return True
        self._reply_result(rid, answer)
        if _superseded(answer):
            self._close("superseded")
        return False

    def _on_review_submit(self, frame: Dict[str, Any]) -> None:
        rid = frame.get("id")
        problems = _review_submit_problems(frame)
        if problems:
            self._bad_frame(rid, problems)
            return
        if not self._review_session_live(rid, frame["name"]):
            return
        # started_by is this link's own workstation (the authorized_keys2 line), never a value
        # the workstation sent; only the documented fields cross, and no patch or range does.
        body: Dict[str, Any] = {"project": frame["project"], "ref": frame["ref"],
                                "started_by": f"session:{self.workstation}"}
        for key in ("base", "expect_sha", "provider"):
            if frame.get(key):
                body[key] = frame[key]
        try:
            answer = self.review_api.submit(body)
        except ReviewAPIError as exc:
            self._reply_error(rid, "hub_error", str(exc))
            return
        self._reply_ok(rid, **{key: value for key, value in answer.items()
                               if key in ("id", "status", "label", "note")})

    def _on_review_get(self, frame: Dict[str, Any]) -> None:
        rid = frame.get("id")
        problems = _review_get_problems(frame)
        if problems:
            self._bad_frame(rid, problems)
            return
        if not self._review_session_live(rid, frame["name"]):
            return
        review_id = frame["review_id"]
        try:
            view = self.review_api.get(review_id)
        except ReviewAPIError as exc:
            # an unknown id answers not_found with the hub's own message; anything else is a
            # plain error reply with the hub's text (no token, no headers)
            error = "not_found" if exc.status == 404 else "hub_error"
            self._reply_error(rid, error, str(exc))
            return
        request = view.get("request")
        if not isinstance(request, dict) \
                or request.get("started_by") != f"session:{self.workstation}":
            # a review another workstation (or the hub itself) started answers exactly as an
            # unknown id, so the id's existence never leaks across workstations
            self._reply_error(rid, "not_found", "review not found")
            return
        self._reply_ok(rid, review=_trim_review(view))

    def _on_ping(self, frame: Dict[str, Any]) -> None:
        self._reply_ok(frame.get("id"))

    # --- hub requests --------------------------------------------------------------------

    def _on_reply(self, frame: Dict[str, Any]) -> None:
        entry = self._pending.pop(frame.get("re"), None)
        if entry is None:  # unknown or already timed out
            return
        kind, context, _sent = entry
        if kind != "deliver":  # ping replies need nothing
            return
        mail_id = context.get("mail_id")
        self._inflight.discard(mail_id)
        if frame.get("ok"):
            answer = self.store.session_mark_delivered(self.workstation, self.lease, [mail_id])
            if _superseded(answer):
                self._close("superseded")
        # an error reply (or a timeout, dropped below) leaves the mail queued; the next poll
        # sends it again

    def _deliver_queued(self) -> None:
        """One deliver request per queued mail of the workstation's live sessions, oldest first;
        a mail whose deliver is still in flight is never sent twice."""
        answer = self.store.session_inbox(self.workstation, self.lease)
        if not answer.get("ok"):
            if _superseded(answer):
                self._close("superseded")
            return  # any other refusal: the next poll tries again
        for row in answer["mail"]:
            if row["id"] in self._inflight:
                continue
            self._inflight.add(row["id"])
            self._request("deliver", {"mail": mail_relay.envelope(row)},
                          context={"mail_id": row["id"]})

    def _drop_timed_out(self, now: float) -> None:
        for rid, (kind, context, sent) in list(self._pending.items()):
            if now - sent <= self.REQUEST_TIMEOUT:
                continue
            del self._pending[rid]
            if kind == "deliver":  # the mail stays queued and is sent again on the next poll
                self._inflight.discard(context.get("mail_id"))

    # --- ending --------------------------------------------------------------------------

    def _close(self, reason: str) -> None:
        if not self._closed:
            self._closed = True
            self.close_reason = reason

    def _shutdown(self) -> None:
        """Bye where the stream still takes it, link_end once (never for `superseded`, whose
        link another stream owns), then the transports: the writer gets WRITER_GRACE to flush,
        and entry returns even when it is still blocked on a stuck stream."""
        if self.close_reason is None:
            self.close_reason = "stopped"
        self._closed = True
        reason = self.close_reason
        self._queue_frame({"t": "bye", "reason": reason})
        self._link_end(reason)
        self._stop_writer()
        self._close_reader_when_done()
        lease = self.lease if self.lease is not None else "none"
        log(f"{self.workstation}: link ended (lease {lease}, reason {reason})")

    def _link_end(self, reason: str) -> None:
        if self.lease is None or reason == "superseded" or self._link_ended:
            return  # before the claim nothing was written; a superseded link is not ours
        self._link_ended = True
        try:
            self.store.link_end(self.workstation, self.lease, reason)
        except Exception as exc:  # noqa: BLE001 - a broken store must not mask the exit code
            log(f"{self.workstation}: link_end failed: {exc}")

    def _stop_writer(self) -> None:
        try:
            self._out.put(None, timeout=self.WRITER_GRACE)  # the sentinel behind the bye frame
        except queue.Full:
            pass  # the queue is full and the writer stuck: abandoned, entry still returns
        thread = self._writer_thread
        if thread is None:
            return
        thread.join(timeout=self.WRITER_GRACE)
        if not thread.is_alive():
            _close_stream(self.writer)  # only once the writer has let go of the buffer

    def _close_reader_when_done(self) -> None:
        thread = self._reader_thread
        if thread is not None:
            thread.join(timeout=0.5)
        if thread is None or not thread.is_alive():
            _close_stream(self.reader)  # closing inside readline() would block; the daemon
            # thread and the process exit end the stream instead

    _DAEMON_FRAMES = {"bind": _on_bind, "unbind": _on_unbind, "state": _on_state,
                      "send": _on_send, "ack": _on_ack, "list": _on_list,
                      "query_submit": _on_query_submit, "query_get": _on_query_get,
                      "review_submit": _on_review_submit, "review_get": _on_review_get,
                      "ping": _on_ping}


def _close_stream(stream: Any) -> None:
    """Closes a stream object that may be a plain fake without close()."""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except (OSError, ValueError, AttributeError):
        pass

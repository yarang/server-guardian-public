"""
Structured event stream of a debate run (docs/design/debate-agent-retrieval.md).

One JSON object per line is appended to the --events-file the hub passes for each job, so the
dashboard can follow the run. The critic panel runs in parallel threads, so writes are serialized
by a lock. Events never carry prompts or file contents: an agent's turn prompt is saved next to the
events file as prompts/<seq>.txt (seq of its agent_started event) and the event only says so.
"""

import datetime
import json
import os
import socket
import threading
from typing import Any, Dict, Optional

MAX_EVENTS_BYTES = 8 * 1024 * 1024
PROMPTS_DIR = "prompts"
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MAX_PROMPTS_BYTES = 64 * 1024 * 1024


def engine_host() -> str:
    """Where this process, and so every LLM CLI an agent runs, executes: GUARDIAN_HOST_LABEL or the hostname.

    Agents are subprocesses of the engine and never run on the fleet hosts they inspect over SSH, so one value
    describes the whole run. It is runtime data in the events file, never written to git.
    """
    label = os.environ.get("GUARDIAN_HOST_LABEL", "").strip()
    if not label:
        try:
            label = socket.gethostname()
        except OSError:
            label = ""
    return label[:80]


def _timestamp() -> str:
    """UTC ISO-8601 with milliseconds, like 2026-10-08T05:21:03.120Z."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class EventWriter:
    """Thread-safe JSONL writer. With no path every call is a no-op, so callers never branch on it."""

    # @MX:WARN: emit() is called concurrently by the critic panel threads.
    # @MX:REASON: seq assignment, the size check and the append must happen under one lock or lines interleave.
    def __init__(self, path: Optional[str] = None, thread_id: str = "", max_bytes: int = MAX_EVENTS_BYTES) -> None:
        self.path = path or None
        self.thread_id = thread_id
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._seq = 0
        self._size = 0
        self._closed = False
        self._fh: Any = None
        self._prompt_bytes = 0
        if self.path:
            # The hub gives each job a fresh file; an existing one still counts toward the size cap.
            try:
                self._size = os.path.getsize(self.path)
            except OSError:
                self._size = 0

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def emit(
        self,
        type: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        round: Optional[int] = None,
        role: Optional[str] = None,
        agent: Optional[str] = None,
        agent_name: Optional[str] = None,
        prompt: Optional[str] = None,
        stage: Optional[int] = None,
        side: Optional[str] = None,
    ) -> None:
        """Appends one event and flushes. Never raises: a broken events file must not stop a debate.

        stage (optional, 0-based) is the pipeline stage and side (optional, "A" or "B") the versus advocate of an agent
        event (docs/design/team-flows.md).

        prompt (optional) is saved as prompts/<seq>.txt before the event is written, so a reader that sees
        "prompt_saved" can always fetch it. Over MAX_PROMPT_BYTES it is cut; past MAX_PROMPTS_BYTES per job
        prompts are no longer saved and the event says "prompt_saved": false.
        """
        if not self.path:
            return
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            event: Dict[str, Any] = {"seq": self._seq, "ts": _timestamp(), "thread_id": self.thread_id, "type": type}
            for key, value in (("round", round), ("role", role), ("agent", agent), ("agent_name", agent_name),
                               ("stage", stage), ("side", side)):
                if value is not None:
                    event[key] = value
            event["data"] = dict(data or {})
            if prompt is not None:
                event["data"].update(self._save_prompt(self._seq, prompt))
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
            size = len(line.encode("utf-8"))
            if self._size + size > self.max_bytes:
                # One marker (a few bytes over the cap at most), then silence.
                event = {"seq": self._seq, "ts": event["ts"], "thread_id": self.thread_id,
                         "type": "events_truncated", "data": {}}
                line = json.dumps(event) + "\n"
                self._closed = True
            self._append(line)

    def _save_prompt(self, seq: int, prompt: str) -> Dict[str, Any]:
        """Writes prompts/<seq>.txt (caller holds the lock). Returns the fields to merge into the event data."""
        raw = prompt.encode("utf-8")
        truncated = len(raw) > MAX_PROMPT_BYTES
        if truncated:
            raw = raw[:MAX_PROMPT_BYTES].decode("utf-8", "ignore").encode("utf-8")
        if self._prompt_bytes + len(raw) > MAX_PROMPTS_BYTES:
            return {"prompt_saved": False}
        try:
            folder = os.path.join(os.path.dirname(os.path.abspath(self.path)), PROMPTS_DIR)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            with open(os.path.join(folder, f"{seq}.txt"), "wb") as fh:
                fh.write(raw)
        except OSError:
            return {"prompt_saved": False}
        self._prompt_bytes += len(raw)
        out: Dict[str, Any] = {"prompt_saved": True}
        if truncated:
            out["prompt_truncated"] = True
        return out

    def _append(self, line: str) -> None:
        try:
            if self._fh is None:
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(line)
            self._fh.flush()
            self._size += len(line.encode("utf-8"))
        except OSError:
            self._closed = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

"""Hub storage and lifecycle for direct resident session queries and multi-session pools
(docs/design/resident-session-query.md, Q1-Q4; docs/design/resident-multi-session.md, MS1-MS4).

Mixed into mail_store.MailStore (schema 8: `agent_sessions` table and `session_queries.session_key`).
Every direct query is strictly anchored to a registered hub project (`code`, `ops`, or `design`),
fenced to an active incarnation (`state == 'ready'`, `incarnations.session_ready == 1`), and bounded
by a dedicated per-agent hourly budget (QD4: 20/hour, separate from the 12-turn mailbox budget),
queue cap (5 pending per agent), and active session cap (MD4: 8 active sessions per agent including
`main`, 14-day idle archival). Prompts and answers are secret-masked and never logged.
"""

import json
import re
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import mail_artifacts

QUERY_ID_RE = re.compile(r"^q-\d{8}-\d{4,}$", re.ASCII)
SESSION_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$", re.ASCII)
SESSION_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", re.ASCII)
QUERY_MODES = ("in_place", "fork")
QUERY_ACTIVE_STATES = ("queued", "dispatched", "running")
QUERY_UNSTARTED_STATES = ("queued", "dispatched")
QUERY_TERMINAL_STATES = ("completed", "failed", "expired", "cancelled")
QUERIES_PER_HOUR = 20          # QD4: separate from TURNS_PER_HOUR (12)
MAX_PENDING_QUERIES = 5
MAX_SESSIONS_PER_AGENT = 8     # MD4: includes 'main'
SESSION_IDLE_TTL = timedelta(days=14)
MAX_PROMPT_BYTES = 32 * 1024
MAX_ANSWER_BYTES = 64 * 1024
DEFAULT_QUERY_TIMEOUT = 180
MIN_QUERY_TIMEOUT = 10
MAX_QUERY_TIMEOUT = 600
QUERY_UNSTARTED_TTL = timedelta(minutes=10)
QUERY_RETENTION = timedelta(days=30)  # QD5: matches D7 mail/task retention
FORK_PROVIDERS = frozenset({"claude"})


def provider_supports_fork(provider: str) -> bool:
    """Whether `provider` supports native read-only `--fork-session` queries (MD3)."""
    if not isinstance(provider, str) or not provider:
        return False
    try:
        import resident_registry
        table = resident_registry.load_table()
        entry = (table.get("providers") or {}).get(provider)
        if isinstance(entry, dict) and "supports_fork" in entry:
            return bool(entry["supports_fork"])
    except Exception:  # noqa: BLE001 - fallback when table is absent in isolated unit tests
        pass
    return provider in FORK_PROVIDERS


def parse_session_key(session_key: Optional[str], project_id: str
                      ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[Tuple[str, str]]]:
    """Validate and normalize `session_key` against `project_id` (MD1, MD2).
    Returns `(normalized_key, key_project, label, None)` or `(None, None, None, (reason, detail))`."""
    if session_key is None or (isinstance(session_key, str) and not session_key.strip()):
        return f"project:{project_id}", project_id, None, None
    if not isinstance(session_key, str):
        return None, None, None, ("bad_input", "session_key must be a string")
    clean = session_key.strip()
    if clean == "main":
        return "main", None, None, None
    parts = clean.split(":")
    if parts[0] != "project" or len(parts) not in (2, 3):
        return None, None, None, (
            "bad_input",
            "session_key must be 'main', 'project:<project_id>', or 'project:<project_id>:<label>'",
        )
    key_proj = parts[1]
    if not SESSION_PROJECT_RE.fullmatch(key_proj):
        return None, None, None, ("bad_input", f"invalid project in session_key {clean!r}")
    if key_proj != project_id:
        return None, None, None, (
            "session_key_project_mismatch",
            f"session_key {clean!r} does not match project {project_id!r}",
        )
    label: Optional[str] = None
    if len(parts) == 3:
        label = parts[2]
        if not SESSION_LABEL_RE.fullmatch(label):
            return None, None, None, (
                "bad_input",
                f"session_key label {label!r} must match ^[a-z0-9][a-z0-9-]{{0,23}}$",
            )
    return clean, key_proj, label, None


def valid_session_key_syntax(session_key: Any) -> bool:
    """Syntax-only check for reset/list endpoints where project is not passed separately."""
    if not isinstance(session_key, str):
        return False
    clean = session_key.strip()
    if clean == "main":
        return True
    parts = clean.split(":")
    if parts[0] != "project" or len(parts) not in (2, 3):
        return False
    if not SESSION_PROJECT_RE.fullmatch(parts[1]):
        return False
    if len(parts) == 3 and not SESSION_LABEL_RE.fullmatch(parts[2]):
        return False
    return True


def mask_query_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return mail_artifacts._mask_text(text)


def truncate_utf8(text: str, max_bytes: int = MAX_ANSWER_BYTES) -> Tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[:max_bytes].decode("utf-8", errors="ignore")
    return cut, True


def _resolve_project(project_id: str) -> Tuple[Optional[Any], Optional[str]]:
    """Returns (Project, None) or (None, error_detail)."""
    if not isinstance(project_id, str) or not project_id.strip():
        return None, "project is required"
    from project_registry import ProjectManager
    try:
        proj = ProjectManager().load(project_id.strip())
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    if proj is None:
        return None, f"unknown hub project {project_id!r}"
    return proj, None


class SessionQueryMixin:
    """Direct active-session queries and multi-session pools; mixed into mail_store.MailStore."""

    def _query_id(self, conn: sqlite3.Connection) -> str:
        day = self.now().strftime("%Y%m%d")
        prefix = f"q-{day}-"
        rows = conn.execute("SELECT id FROM session_queries WHERE id LIKE ?",
                            (prefix + "%",)).fetchall()
        highest = max((int(row["id"][len(prefix):]) for row in rows), default=0)
        return f"{prefix}{highest + 1:04d}"

    def _query_dict(self, row: sqlite3.Row, *, include_bodies: bool = False) -> Dict[str, Any]:
        keys = set(row.keys())
        item: Dict[str, Any] = {
            "id": row["id"],
            "address": row["address"],
            "project": row["project"],
            "project_kind": row["project_kind"],
            "session_key": row["session_key"] if "session_key" in keys else "main",
            "task": row["task"],
            "epoch": row["epoch"],
            "session_id": row["session_id"],
            "caller": row["caller"],
            "caller_kind": row["caller_kind"],
            "mode": row["mode"],
            "status": row["status"],
            "turn_id": row["turn_id"],
            "answer_truncated": bool(row["answer_truncated"]),
            "error": row["error"],
            "timeout_sec": row["timeout_sec"],
            "created": row["created"],
            "started": row["started"],
            "completed": row["completed"],
            "expires": row["expires"],
        }
        if include_bodies:
            item["prompt"] = mask_query_text(row["prompt"])
            item["answer"] = mask_query_text(row["answer"])
        return item

    @staticmethod
    def _agent_session_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "address": row["address"],
            "epoch": row["epoch"],
            "session_key": row["session_key"],
            "project": row["project"],
            "label": row["label"],
            "provider_session_id": row["provider_session_id"],
            "session_ready": bool(row["session_ready"]),
            "status": row["status"],
            "turns": row["turns"],
            "created": row["created"],
            "last_used": row["last_used"],
            "closed": row["closed"],
        }

    # --- agent_sessions lifecycle helpers (MS1) ---------------------------------------------

    def _sync_main_session(self, conn: sqlite3.Connection, address: str, epoch: int,
                           session_id: Optional[str], *, session_ready: Optional[int] = None,
                           bump_turns: bool = False) -> None:
        """Keep the `main` row of `agent_sessions` aligned with `incarnations`."""
        import mail_store
        now = mail_store.format_ts(self.now())
        existing = conn.execute(
            "SELECT * FROM agent_sessions WHERE address=? AND epoch=? AND session_key='main'",
            (address, epoch),
        ).fetchone()
        if existing is None:
            ready_val = int(session_ready) if session_ready is not None else 0
            conn.execute(
                "INSERT INTO agent_sessions(address, epoch, session_key, project, label, "
                "provider_session_id, session_ready, status, turns, created, last_used) "
                "VALUES(?, ?, 'main', NULL, NULL, ?, ?, 'active', ?, ?, ?)",
                (address, epoch, session_id, ready_val, 1 if bump_turns else 0,
                 now, now if bump_turns else None),
            )
            return
        new_sid = session_id if session_id else existing["provider_session_id"]
        new_ready = int(session_ready) if session_ready is not None else existing["session_ready"]
        new_turns = existing["turns"] + (1 if bump_turns else 0)
        new_last = now if bump_turns else existing["last_used"]
        conn.execute(
            "UPDATE agent_sessions SET provider_session_id=?, session_ready=?, status='active', "
            "turns=?, last_used=?, closed=NULL WHERE address=? AND epoch=? AND session_key='main'",
            (new_sid, new_ready, new_turns, new_last, address, epoch),
        )

    def _close_epoch_sessions(self, conn: sqlite3.Connection, address: str,
                              max_epoch: Optional[int] = None) -> None:
        import mail_store
        now = mail_store.format_ts(self.now())
        if max_epoch is None:
            conn.execute(
                "UPDATE agent_sessions SET status='closed', closed=? "
                "WHERE address=? AND status='active'",
                (now, address),
            )
        else:
            conn.execute(
                "UPDATE agent_sessions SET status='closed', closed=? "
                "WHERE address=? AND epoch<=? AND status='active'",
                (now, address, max_epoch),
            )

    def _archive_idle_sessions(self, conn: sqlite3.Connection, now: str) -> None:
        import mail_store
        cutoff = mail_store.format_ts(self.now() - SESSION_IDLE_TTL)
        stale = conn.execute(
            "SELECT address, epoch, session_key FROM agent_sessions "
            "WHERE session_key!='main' AND status='active' AND COALESCE(last_used, created)<=?",
            (cutoff,),
        ).fetchall()
        for row in stale:
            conn.execute(
                "UPDATE agent_sessions SET status='archived', closed=? "
                "WHERE address=? AND epoch=? AND session_key=? AND status='active'",
                (now, row["address"], row["epoch"], row["session_key"]),
            )
            self._event("session_archived", agent=row["address"],
                        detail={"epoch": row["epoch"], "session_key": row["session_key"], "reason": "idle"},
                        conn=conn)

    def _record_scoped_session(self, conn: sqlite3.Connection, address: str, epoch: int,
                               session_key: str, session_id: Optional[str], end_reason: str,
                               query_id: Optional[str],
                               query_reply: Optional[Dict[str, Any]]) -> bool:
        """Update one scoped (non-main) row in `agent_sessions` at `turn_end`.
        Returns True if the scoped session experienced `resume_new_session` (scoped-only loss)."""
        import mail_store
        now = mail_store.format_ts(self.now())
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE address=? AND epoch=? AND session_key=?",
            (address, epoch, session_key),
        ).fetchone()
        reply = query_reply if isinstance(query_reply, dict) else {}
        if end_reason != "normal" or reply.get("status") == "failed":
            if reply.get("error") == "session_lost" and row is not None:
                conn.execute(
                    "UPDATE agent_sessions SET status='lost', closed=? "
                    "WHERE address=? AND epoch=? AND session_key=?",
                    (now, address, epoch, session_key),
                )
                self._event("scoped_session_lost", agent=address,
                            detail={"epoch": epoch, "session_key": session_key}, conn=conn)
            return False
        if row is None or not session_id:
            return False
        stored_sid = row["provider_session_id"]
        if not stored_sid:
            conn.execute(
                "UPDATE agent_sessions SET provider_session_id=?, session_ready=1, status='active', "
                "turns=turns+1, last_used=?, closed=NULL "
                "WHERE address=? AND epoch=? AND session_key=?",
                (session_id, now, address, epoch, session_key),
            )
            if query_id:
                conn.execute("UPDATE session_queries SET session_id=? WHERE id=?",
                             (session_id, query_id))
            return False
        if stored_sid == session_id:
            conn.execute(
                "UPDATE agent_sessions SET session_ready=1, turns=turns+1, last_used=? "
                "WHERE address=? AND epoch=? AND session_key=?",
                (now, address, epoch, session_key),
            )
            return False
        # Scoped resume_new_session: isolate loss to this session_key only; never tear down main.
        conn.execute(
            "UPDATE agent_sessions SET status='lost', closed=? "
            "WHERE address=? AND epoch=? AND session_key=?",
            (now, address, epoch, session_key),
        )
        self._event("scoped_session_lost", agent=address,
                    detail={"epoch": epoch, "session_key": session_key, "reason": "resume_new_session"},
                    conn=conn)
        return True

    def list_agent_sessions(self, *, agent: Optional[str] = None, project: Optional[str] = None,
                            include_inactive: bool = False) -> List[Dict[str, Any]]:
        """List active (or all) sessions in `agent_sessions` for open incarnations."""
        self._sweep()
        conn = self._ready()
        if conn is None:
            return []
        clauses: List[str] = ["s.epoch=a.epoch"]
        params: List[Any] = []
        if not include_inactive:
            clauses.append("s.status='active'")
        if agent:
            clauses.append("s.address=?")
            params.append(agent)
        if project:
            clauses.append("(s.project=? OR s.session_key='main')")
            params.append(project)
        where = " WHERE " + " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT s.* FROM agent_sessions s JOIN agents a ON a.address=s.address{where} "
            "ORDER BY s.address ASC, CASE WHEN s.session_key='main' THEN 0 ELSE 1 END, s.session_key ASC",
            tuple(params),
        ).fetchall()
        return [self._agent_session_dict(row) for row in rows]

    def reset_agent_session(self, address: str, session_key: str) -> Dict[str, Any]:
        """Archive/reset a scoped session for `address` without disturbing `main` or sibling sessions."""
        import mail_store
        if not mail_store.valid_address(address):
            return self._refuse("bad_input", f"invalid resident address {address!r}")
        if not valid_session_key_syntax(session_key):
            return self._refuse("bad_input", f"invalid session_key {session_key!r}", agent=address)
        clean_key = session_key.strip()
        if clean_key == "main":
            return self._refuse("cannot_reset_main",
                                "cannot reset the main session; rebind or restart the agent instead",
                                agent=address)
        self._sweep()
        conn = self._ready()
        if conn is None:
            return self._refuse("unknown_agent", f"no agent {address}")
        now = mail_store.format_ts(self.now())
        with self._write() as wconn:
            agent = self._agent(wconn, address)
            if agent is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=wconn)
            epoch = agent["epoch"]
            row = wconn.execute(
                "SELECT * FROM agent_sessions WHERE address=? AND epoch=? AND session_key=?",
                (address, epoch, clean_key),
            ).fetchone()
            if row is None or row["status"] != "active":
                return self._refuse("not_found",
                                    f"no active session {clean_key!r} for {address} at epoch {epoch}",
                                    agent=address, conn=wconn)
            running = wconn.execute(
                "SELECT id FROM session_queries WHERE address=? AND epoch=? AND session_key=? "
                "AND status='running'",
                (address, epoch, clean_key),
            ).fetchone()
            if running is not None:
                return self._refuse("session_busy",
                                    f"session {clean_key!r} has a running query ({running['id']})",
                                    agent=address, conn=wconn)
            wconn.execute(
                "UPDATE agent_sessions SET status='archived', closed=? "
                "WHERE address=? AND epoch=? AND session_key=?",
                (now, address, epoch, clean_key),
            )
            pending = wconn.execute(
                "SELECT id FROM session_queries WHERE address=? AND epoch=? AND session_key=? "
                "AND status IN ('queued','dispatched')",
                (address, epoch, clean_key),
            ).fetchall()
            for qrow in pending:
                wconn.execute(
                    "UPDATE session_queries SET status='failed', error='session_reset', completed=? "
                    "WHERE id=? AND status IN ('queued','dispatched')",
                    (now, qrow["id"]),
                )
            self._event("session_reset", agent=address,
                        detail={"epoch": epoch, "session_key": clean_key,
                                "failed_queries": [r["id"] for r in pending]}, conn=wconn)
            return {"ok": True, "address": address, "epoch": epoch,
                    "session_key": clean_key, "status": "archived"}

    # --- direct session queries (Q1-Q4 + MS1) -----------------------------------------------

    def submit_query(self, address: str, project: str, prompt: str, *,
                     caller: str = "operator", caller_kind: str = "operator",
                     task: Optional[str] = None, mode: str = "in_place",
                     session_key: Optional[str] = None,
                     timeout_sec: int = DEFAULT_QUERY_TIMEOUT,
                     op_id: Optional[str] = None) -> Dict[str, Any]:
        """Queue one project-scoped query against a resident agent's active session (QD1-QD4, MD1-MD4)."""
        import mail_store

        self._sweep()
        if mail_store.resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=address)
        if not mail_store.valid_address(address):
            return self._refuse("bad_input", f"invalid resident address {address!r}")
        if caller_kind not in ("operator", "session"):
            return self._refuse("bad_input", "caller_kind must be operator or session", agent=address)
        if mode not in QUERY_MODES:
            return self._refuse("bad_input", f"mode must be one of {', '.join(QUERY_MODES)}",
                                agent=address)
        if not isinstance(prompt, str) or not prompt.strip():
            return self._refuse("bad_input", "prompt must be a non-empty string", agent=address)
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            return self._refuse("prompt_too_large",
                                f"prompt exceeds {MAX_PROMPT_BYTES} bytes", agent=address)
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int) \
                or not MIN_QUERY_TIMEOUT <= timeout_sec <= MAX_QUERY_TIMEOUT:
            return self._refuse("bad_input",
                                f"timeout_sec must be an integer from {MIN_QUERY_TIMEOUT} to {MAX_QUERY_TIMEOUT}",
                                agent=address)

        proj, proj_err = _resolve_project(project)
        if proj is None:
            return self._refuse("unknown_project", proj_err or f"unknown project {project!r}",
                                agent=address)

        norm_key, key_proj, key_label, key_err = parse_session_key(session_key, proj.id)
        if key_err is not None or norm_key is None:
            reason, detail = key_err or ("bad_input", "invalid session_key")
            return self._refuse(reason, detail, agent=address)

        policy = proj.policy if isinstance(getattr(proj, "policy", None), dict) else {}
        allowed_agents = policy.get("resident_query_agents")
        if allowed_agents is not None:
            if not allowed_agents:
                return self._refuse("queries_disabled_for_project",
                                    f"direct resident session queries are disabled for project {proj.id}",
                                    agent=address)
            if address not in allowed_agents:
                return self._refuse("agent_not_allowed_for_project",
                                    f"{address} is not in policy.resident_query_agents for project {proj.id}",
                                    agent=address)

        clean_task = task.strip() if isinstance(task, str) and task.strip() else None

        with self._write() as conn:
            cached = self._cached_op(conn, caller, op_id)
            if cached is not None:
                return cached
            agent = self._agent(conn, address)
            if agent is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            if agent["state"] == "retiring":
                return self._refuse("agent_retired", f"{address} is being removed",
                                    agent=address, conn=conn)
            if agent["state"] != "ready":
                return self._refuse("agent_not_ready",
                                    f"{address} is {agent['state']}, not ready",
                                    agent=address, conn=conn)
            epoch = agent["epoch"]
            main_sid = agent["session_id"]
            inc = conn.execute(
                "SELECT session_id, session_ready, ended FROM incarnations WHERE address=? AND epoch=?",
                (address, epoch),
            ).fetchone()
            if not main_sid or inc is None or inc["ended"] is not None or not inc["session_ready"]:
                return self._refuse("no_active_session",
                                    f"{address} has no active session at epoch {epoch} yet",
                                    agent=address, conn=conn)
            # Ensure main is mirrored in agent_sessions (handles stores upgraded from schema 7).
            self._sync_main_session(conn, address, epoch, main_sid, session_ready=1)

            if mode == "fork":
                if not provider_supports_fork(agent["provider"]):
                    return self._refuse(
                        "fork_unsupported",
                        f"provider {agent['provider']!r} does not support read-only fork queries",
                        agent=address, conn=conn,
                    )

            if clean_task is not None:
                trow = conn.execute("SELECT id, project FROM tasks WHERE id=?", (clean_task,)).fetchone()
                if trow is None or trow["project"] != proj.id:
                    return self._refuse("bad_task",
                                        f"task {clean_task!r} does not belong to project {proj.id}",
                                        agent=address, conn=conn)

            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM session_queries WHERE address=? AND status IN ('queued','dispatched','running')",
                (address,),
            ).fetchone()["n"]
            if pending >= MAX_PENDING_QUERIES:
                return self._refuse("queue_full",
                                    f"{address} already has {pending} pending queries (limit {MAX_PENDING_QUERIES})",
                                    agent=address, conn=conn)

            hour_ago = mail_store.format_ts(self.now() - timedelta(hours=1))
            recent = conn.execute(
                "SELECT COUNT(*) AS n FROM turns WHERE address=? AND kind='query' AND started>=?",
                (address, hour_ago),
            ).fetchone()["n"]
            if recent >= QUERIES_PER_HOUR:
                return self._refuse("query_budget",
                                    f"{recent} direct queries in the last hour (limit {QUERIES_PER_HOUR})",
                                    agent=address, conn=conn)

            now_dt = self.now()
            created = mail_store.format_ts(now_dt)
            expires = mail_store.format_ts(now_dt + QUERY_UNSTARTED_TTL)

            # Resolve target provider session id for `norm_key`.
            if norm_key == "main":
                target_sid = main_sid
            else:
                srow = conn.execute(
                    "SELECT * FROM agent_sessions WHERE address=? AND epoch=? AND session_key=?",
                    (address, epoch, norm_key),
                ).fetchone()
                if mode == "fork":
                    if (srow is None or srow["status"] != "active"
                            or not srow["session_ready"] or not srow["provider_session_id"]):
                        return self._refuse(
                            "no_active_session",
                            f"session {norm_key!r} is not ready to fork on {address} at epoch {epoch}",
                            agent=address, conn=conn,
                        )
                    target_sid = srow["provider_session_id"]
                elif srow is not None and srow["status"] == "active":
                    target_sid = srow["provider_session_id"] or ""
                else:
                    active_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM agent_sessions "
                        "WHERE address=? AND epoch=? AND status='active'",
                        (address, epoch),
                    ).fetchone()["n"]
                    if active_count >= MAX_SESSIONS_PER_AGENT:
                        return self._refuse(
                            "session_limit_reached",
                            f"{address} already has {active_count} active sessions "
                            f"(limit {MAX_SESSIONS_PER_AGENT}); reset an unused session first",
                            agent=address, conn=conn,
                        )
                    if srow is None:
                        conn.execute(
                            "INSERT INTO agent_sessions(address, epoch, session_key, project, label, "
                            "provider_session_id, session_ready, status, turns, created) "
                            "VALUES(?, ?, ?, ?, ?, NULL, 0, 'active', 0, ?)",
                            (address, epoch, norm_key, key_proj, key_label, created),
                        )
                    else:
                        conn.execute(
                            "UPDATE agent_sessions SET project=?, label=?, provider_session_id=NULL, "
                            "session_ready=0, status='active', turns=0, created=?, last_used=NULL, closed=NULL "
                            "WHERE address=? AND epoch=? AND session_key=?",
                            (key_proj, key_label, created, address, epoch, norm_key),
                        )
                    self._event("session_created", agent=address,
                                detail={"epoch": epoch, "session_key": norm_key, "project": proj.id},
                                conn=conn)
                    target_sid = ""

            qid = self._query_id(conn)
            stored_prompt = mask_query_text(prompt) or prompt
            conn.execute(
                "INSERT INTO session_queries (id, address, project, project_kind, session_key, task, "
                "epoch, session_id, caller, caller_kind, mode, prompt, status, timeout_sec, created, expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (qid, address, proj.id, proj.kind, norm_key, clean_task, epoch, target_sid, caller,
                 caller_kind, mode, stored_prompt, timeout_sec, created, expires),
            )
            answer: Dict[str, Any] = {
                "ok": True,
                "id": qid,
                "address": address,
                "project": proj.id,
                "project_kind": proj.kind,
                "session_key": norm_key,
                "task": clean_task,
                "epoch": epoch,
                "session_id": target_sid,
                "mode": mode,
                "status": "queued",
                "timeout_sec": timeout_sec,
                "created": created,
                "expires": expires,
            }
            self._store_op(conn, caller, op_id, answer)
            self._event("query_queued", agent=address,
                        detail={"id": qid, "project": proj.id, "session_key": norm_key,
                                "mode": mode, "epoch": epoch, "caller": caller},
                        conn=conn)
            return answer

    def queued_queries(self, node: str) -> List[Dict[str, Any]]:
        """Unstarted queued queries for agents on `node`, oldest first (relay poll)."""
        self._sweep()
        conn = self._ready()
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT q.* FROM session_queries q JOIN agents a ON a.address=q.address "
            "WHERE a.node=? AND q.status='queued' ORDER BY q.created ASC, q.id ASC",
            (node,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_query_dispatched(self, query_id: str) -> bool:
        conn = self._ready()
        if conn is None:
            return False
        with self._write() as wconn:
            cur = wconn.execute(
                "UPDATE session_queries SET status='dispatched' WHERE id=? AND status='queued'",
                (query_id,),
            )
            return cur.rowcount > 0

    def fail_query(self, query_id: str, error: str) -> bool:
        import mail_store
        conn = self._ready()
        if conn is None:
            return False
        now = mail_store.format_ts(self.now())
        with self._write() as wconn:
            row = wconn.execute("SELECT address, status FROM session_queries WHERE id=?",
                                (query_id,)).fetchone()
            if row is None or row["status"] not in QUERY_ACTIVE_STATES:
                return False
            wconn.execute(
                "UPDATE session_queries SET status='failed', error=?, completed=? "
                "WHERE id=? AND status IN ('queued','dispatched','running')",
                (str(error)[:500], now, query_id),
            )
            self._event("query_failed", agent=row["address"],
                        detail={"id": query_id, "error": str(error)[:200]}, conn=wconn)
            return True

    def cancel_query(self, query_id: str) -> Dict[str, Any]:
        """Cancel a queued or dispatched query (never kills a running turn)."""
        import mail_store
        if not isinstance(query_id, str) or not QUERY_ID_RE.fullmatch(query_id):
            return self._refuse("bad_input", f"invalid query id {query_id!r}")
        self._sweep()
        conn = self._ready()
        if conn is None:
            return self._refuse("not_found", f"no query {query_id}")
        now = mail_store.format_ts(self.now())
        with self._write() as wconn:
            row = wconn.execute("SELECT * FROM session_queries WHERE id=?", (query_id,)).fetchone()
            if row is None:
                return self._refuse("not_found", f"no query {query_id}", conn=wconn)
            if row["status"] == "running":
                return self._refuse("already_running",
                                    f"query {query_id} is already running and cannot be cancelled",
                                    agent=row["address"], conn=wconn)
            if row["status"] not in QUERY_UNSTARTED_STATES:
                return self._refuse("already_finished",
                                    f"query {query_id} is already {row['status']}",
                                    agent=row["address"], conn=wconn)
            wconn.execute(
                "UPDATE session_queries SET status='cancelled', completed=? WHERE id=? AND status IN ('queued','dispatched')",
                (now, query_id),
            )
            self._event("query_cancelled", agent=row["address"], detail={"id": query_id}, conn=wconn)
            return {"ok": True, "id": query_id, "address": row["address"], "status": "cancelled"}

    def list_queries(self, *, project: Optional[str] = None, agent: Optional[str] = None,
                     session_key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        self._sweep()
        conn = self._ready()
        if conn is None:
            return []
        cap = max(1, min(int(limit), 200))
        clauses: List[str] = []
        params: List[Any] = []
        if project:
            clauses.append("project=?")
            params.append(project)
        if agent:
            clauses.append("address=?")
            params.append(agent)
        if session_key:
            clauses.append("session_key=?")
            params.append(session_key)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(cap)
        rows = conn.execute(
            f"SELECT * FROM session_queries{where} ORDER BY created DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._query_dict(row, include_bodies=False) for row in rows]

    def get_query(self, query_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(query_id, str) or not QUERY_ID_RE.fullmatch(query_id):
            raise ValueError(f"bad query id {query_id!r}")
        self._sweep()
        conn = self._ready()
        if conn is None:
            return None
        row = conn.execute("SELECT * FROM session_queries WHERE id=?", (query_id,)).fetchone()
        return self._query_dict(row, include_bodies=True) if row is not None else None

    def _fail_epoch_queries(self, conn: sqlite3.Connection, address: str,
                            error: str, *, max_epoch: Optional[int] = None) -> int:
        import mail_store
        now = mail_store.format_ts(self.now())
        if max_epoch is None:
            rows = conn.execute(
                "SELECT id FROM session_queries WHERE address=? AND status IN ('queued','dispatched')",
                (address,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM session_queries WHERE address=? AND epoch<=? AND status IN ('queued','dispatched')",
                (address, max_epoch),
            ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE session_queries SET status='failed', error=?, completed=? "
                "WHERE id=? AND status IN ('queued','dispatched')",
                (error, now, row["id"]),
            )
            self._event("query_failed", agent=address,
                        detail={"id": row["id"], "error": error}, conn=conn)
        return len(rows)

    def _expire_queries(self, conn: sqlite3.Connection, now: str) -> None:
        import mail_store
        stale = conn.execute(
            "SELECT id, address FROM session_queries WHERE status IN ('queued','dispatched') AND expires<=? "
            "ORDER BY id",
            (now,),
        ).fetchall()
        for row in stale:
            conn.execute(
                "UPDATE session_queries SET status='expired', error='expired', completed=? "
                "WHERE id=? AND status IN ('queued','dispatched')",
                (now, row["id"]),
            )
            self._event("query_expired", agent=row["address"], detail={"id": row["id"]}, conn=conn)
        retention_cutoff = mail_store.format_ts(self.now() - QUERY_RETENTION)
        conn.execute(
            "DELETE FROM session_queries WHERE status IN ('completed','failed','expired','cancelled') "
            "AND COALESCE(completed, created)<=?",
            (retention_cutoff,),
        )
        self._archive_idle_sessions(conn, now)

    def _settle_query_on_turn_end(self, conn: sqlite3.Connection, address: str, turn_id: str,
                                  end_reason: str, query_id: Optional[str],
                                  query_reply: Optional[Dict[str, Any]],
                                  resume_new: bool) -> None:
        import mail_store
        row = None
        if query_id:
            row = conn.execute("SELECT * FROM session_queries WHERE id=? AND address=?",
                               (query_id, address)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM session_queries WHERE turn_id=? AND address=?",
                               (turn_id, address)).fetchone()
        if row is None or row["status"] in QUERY_TERMINAL_STATES:
            return
        now = mail_store.format_ts(self.now())
        reply = query_reply if isinstance(query_reply, dict) else {}
        if resume_new:
            status = "failed"
            answer_text = None
            truncated = 0
            err = "session_lost"
        elif end_reason != "normal" or reply.get("status") == "failed":
            status = "failed"
            answer_text = None
            truncated = 0
            err = str(reply.get("error") or end_reason or "turn_lost")[:500]
        else:
            status = "completed"
            raw_answer = str(reply.get("answer") or "")
            masked = mask_query_text(raw_answer) or ""
            cut_text, was_cut = truncate_utf8(masked, MAX_ANSWER_BYTES)
            answer_text = cut_text
            truncated = 1 if (was_cut or bool(reply.get("truncated"))) else 0
            err = None
        conn.execute(
            "UPDATE session_queries SET status=?, answer=?, answer_truncated=?, error=?, completed=? "
            "WHERE id=?",
            (status, answer_text, truncated, err, now, row["id"]),
        )
        self._event("query_completed" if status == "completed" else "query_failed",
                    agent=address,
                    detail={"id": row["id"], "turn": turn_id, "status": status,
                            "truncated": bool(truncated), **({"error": err} if err else {})},
                    conn=conn)

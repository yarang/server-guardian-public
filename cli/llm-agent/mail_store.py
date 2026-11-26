"""Hub mail store for resident agents: agents, incarnations, turns, mail and events.

SQLite (stdlib) at <GUARDIAN_DATA_DIR>/mailbox/mail.db, phases M1 and M2 of
docs/design/resident-agents.md. Every model turn of a resident agent has a turn record here; mail
hop, thread and body limits, the hourly turn budget and the kill switch (GUARDIAN_RESIDENT=off) are
enforced at this level. The task board lives in mail_tasks.py (a mixin of MailStore) because the two
together would make one file too large; they share this module's connection, clock and event
helpers. Schema version 2 adds the bind timeout columns and the M2 relay calls (delivery marks,
node view, the `sync` reconcile); version 3 adds tasks.calls_json (the M2e task call log); version 4
adds tasks.patch and tasks.patch_action (the O3 task patch: the raw patch an open agent attached to
task_update, and the id of the PROPOSED apply_patch action the relay recorded for it); version 5
adds the links and sessions tables of operator sessions (mail_sessions.py, S1a of
docs/design/operator-sessions.md: interactive sessions on workstations, a second address kind that
never enters the agents table).

No side effect on the no-agent path: read-only calls on a data dir without a store create neither
the database file nor the mailbox directory; only a write (registration, send, task create, admit)
creates them. Refusals never raise: they return {"ok": False, "reason": <code>, "detail": <text>}
and are written to the events table.
"""

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import mail_artifacts
import mail_queries
import mail_relay_store
import mail_sessions
import mail_tasks

# Limits (docs/design/resident-agents.md, "Authority, loops and limits").
HOP_MAX = 3
THREAD_MAX = 40
OPEN_TASKS_MAX = 5
TURNS_PER_HOUR = 12
BODY_MAX = 32 * 1024
LIFETIME = timedelta(hours=24)
RELEASE_AFTER = timedelta(minutes=10)
BIND_TIMEOUT = timedelta(seconds=60)
BIND_RETRIES = 3

SCHEMA_VERSION = 8
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
SPECIAL_SENDERS = ("operator", "hub")

MAIL_KINDS = ("message", "question", "answer", "task_offer", "task_result", "status", "shutdown")
OPERATOR_KINDS = ("message", "question", "answer", "task_offer", "shutdown")
HUB_STATUS_TYPES = ("idle", "busy", "question", "down", "peers_changed", "undeliverable")
RESERVED_KINDS = ("status", "shutdown")
TURN_KINDS = ("wake", "continuation", "sweep", "resume", "operator", "notice", "query")
OPEN_MAIL_STATES = ("queued", "delivered", "retrieved")


def data_dir() -> Path:
    """The hub data directory, without creating it (reads on an empty dir must stay empty)."""
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()


def default_store_path() -> Path:
    return data_dir() / "mailbox" / "mail.db"


def resident_off() -> bool:
    """The kill switch, read per call: GUARDIAN_RESIDENT=off admits no turn and accepts no mail."""
    return os.environ.get("GUARDIAN_RESIDENT", "").strip().lower() == "off"


def sessions_off() -> bool:
    """The session kill switch, read per call: GUARDIAN_SESSIONS=off refuses every session
    operation (GUARDIAN_RESIDENT=off does not apply to sessions)."""
    return os.environ.get("GUARDIAN_SESSIONS", "").strip().lower() == "off"


def valid_address(address: str) -> bool:
    """name@node with the inventory's id pattern for the node part."""
    if not isinstance(address, str) or address.count("@") != 1:
        return False
    name, node = address.split("@")
    return bool(NAME_RE.fullmatch(name) and NODE_RE.fullmatch(node))


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class MailStore(mail_tasks.TaskBoardMixin, mail_relay_store.RelayStoreMixin,
                mail_sessions.SessionStoreMixin, mail_artifacts.ArtifactStoreMixin,
                mail_queries.SessionQueryMixin):
    """One connection per instance; write operations that count before they insert or touch several
    rows run inside BEGIN IMMEDIATE ... COMMIT (rollback on exception)."""

    def __init__(self, path: Optional[Path] = None, now: Optional[Any] = None):
        self.path = Path(path) if path is not None else default_store_path()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._conn: Optional[sqlite3.Connection] = None

    def close(self) -> None:
        """Closes the connection; a later call opens it again."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "MailStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # interpreter shutdown: sqlite3 may already be gone
            pass

    # --- connection and events ---------------------------------------------------------

    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _open(self) -> sqlite3.Connection:
        """Opens (creating directories, file and schema) the store. Only write paths may call this."""
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._connect(self.path)
            self._init_schema(self._conn)
        return self._conn

    def _ready(self) -> Optional[sqlite3.Connection]:
        """The connection when the store exists; None (creating nothing) when it does not."""
        if self._conn is None and not self.path.exists():
            return None
        return self._open()

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agents (
            address TEXT PRIMARY KEY, name TEXT NOT NULL, node TEXT NOT NULL, provider TEXT NOT NULL,
            driver TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'down', epoch INTEGER NOT NULL DEFAULT 0,
            session_id TEXT, not_ready_since TEXT, peers_json TEXT, bind_started TEXT,
            bind_failures INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
            modified TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS incarnations (
            address TEXT NOT NULL, epoch INTEGER NOT NULL, session_id TEXT, pane TEXT, pid TEXT,
            session_ready INTEGER NOT NULL DEFAULT 0,
            started TEXT NOT NULL, ended TEXT, end_reason TEXT, PRIMARY KEY(address, epoch));
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, address TEXT NOT NULL, epoch INTEGER NOT NULL, kind TEXT NOT NULL,
            started TEXT NOT NULL, ended TEXT, end_reason TEXT, hop_in INTEGER,
            FOREIGN KEY(address) REFERENCES agents(address));
        CREATE TABLE IF NOT EXISTS turn_inputs (
            turn TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, hop INTEGER NOT NULL,
            PRIMARY KEY(turn, kind, ref), FOREIGN KEY(turn) REFERENCES turns(id));
        CREATE TABLE IF NOT EXISTS mail (
            id TEXT PRIMARY KEY, thread TEXT NOT NULL, sender TEXT NOT NULL, from_kind TEXT NOT NULL,
            recipient TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT, body TEXT, task TEXT,
            reply_to TEXT, hop INTEGER NOT NULL, turn TEXT, created TEXT NOT NULL, expires TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued', retrieved_turn TEXT, redelivered INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(turn) REFERENCES turns(id));
        CREATE INDEX IF NOT EXISTS mail_recipient ON mail(recipient, state);
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT, creator TEXT NOT NULL,
            creator_kind TEXT NOT NULL, assignee TEXT, owner TEXT, status TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0, project TEXT, sha TEXT, blocked_by_json TEXT,
            hop INTEGER NOT NULL, result TEXT, reason TEXT, calls_json TEXT,
            patch TEXT, patch_action INTEGER,
            created TEXT NOT NULL, modified TEXT NOT NULL, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations (
            sender TEXT NOT NULL, op_id TEXT NOT NULL, answer_json TEXT NOT NULL, created TEXT NOT NULL,
            PRIMARY KEY(sender, op_id));
        CREATE TABLE IF NOT EXISTS links (
            workstation TEXT PRIMARY KEY, lease INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'down', started TEXT, ended TEXT, end_reason TEXT);
        CREATE TABLE IF NOT EXISTS sessions (
            address TEXT PRIMARY KEY, name TEXT NOT NULL, workstation TEXT NOT NULL,
            cli_session TEXT, state TEXT NOT NULL, away INTEGER NOT NULL DEFAULT 0,
            peers_json TEXT, last_turn TEXT, linked TEXT, offline_since TEXT,
            created TEXT NOT NULL, modified TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resident_artifacts (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, task TEXT, title TEXT NOT NULL,
            filename TEXT NOT NULL, rel_path TEXT NOT NULL, mime TEXT NOT NULL,
            sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, summary TEXT,
            pinned INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, expires TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS resident_artifacts_agent ON resident_artifacts(agent, created);
        CREATE TABLE IF NOT EXISTS session_queries (
            id TEXT PRIMARY KEY, address TEXT NOT NULL, project TEXT NOT NULL,
            project_kind TEXT NOT NULL, session_key TEXT NOT NULL DEFAULT 'main',
            task TEXT, epoch INTEGER NOT NULL,
            session_id TEXT NOT NULL, caller TEXT NOT NULL, caller_kind TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'in_place', prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', turn_id TEXT, answer TEXT,
            answer_truncated INTEGER NOT NULL DEFAULT 0, error TEXT,
            timeout_sec INTEGER NOT NULL DEFAULT 180, created TEXT NOT NULL,
            started TEXT, completed TEXT, expires TEXT NOT NULL,
            FOREIGN KEY(address) REFERENCES agents(address),
            FOREIGN KEY(turn_id) REFERENCES turns(id));
        CREATE INDEX IF NOT EXISTS idx_session_queries_dispatch
            ON session_queries(address, status, created);
        CREATE TABLE IF NOT EXISTS agent_sessions (
            address TEXT NOT NULL, epoch INTEGER NOT NULL, session_key TEXT NOT NULL,
            project TEXT, label TEXT, provider_session_id TEXT,
            session_ready INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active', turns INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL, last_used TEXT, closed TEXT,
            PRIMARY KEY(address, epoch, session_key),
            FOREIGN KEY(address) REFERENCES agents(address));
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_active
            ON agent_sessions(address, epoch, status);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT,
            detail_json TEXT);
        """)
        # A version-1 store (M1) passes CREATE TABLE IF NOT EXISTS unchanged: add the missing
        # agent columns by hand, then stamp the new version. A fresh store already has them.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
        for column, decl in (("bind_started", "TEXT"),
                             ("bind_failures", "INTEGER NOT NULL DEFAULT 0")):
            if column not in columns:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {column} {decl}")
        # A version-2 store gains the task call log column the same way (version 3).
        if "calls_json" not in {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}:
            conn.execute("ALTER TABLE tasks ADD COLUMN calls_json TEXT")
        # A version-3 store gains the task patch columns the same way (version 4, O3).
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "patch" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN patch TEXT")
        if "patch_action" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN patch_action INTEGER")
        # Version 7 (docs/design/resident-session-query.md): active-session readiness on incarnations.
        inc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(incarnations)")}
        if "session_ready" not in inc_columns:
            conn.execute("ALTER TABLE incarnations ADD COLUMN session_ready INTEGER NOT NULL DEFAULT 0")
        # Version 8 (docs/design/resident-multi-session.md): scoped session_key on session_queries.
        sq_columns = {row["name"] for row in conn.execute("PRAGMA table_info(session_queries)")}
        if "session_key" not in sq_columns:
            conn.execute("ALTER TABLE session_queries ADD COLUMN session_key TEXT NOT NULL DEFAULT 'main'")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _event(self, kind: str, agent: Optional[str] = None, detail: Any = None,
               conn: Optional[sqlite3.Connection] = None) -> None:
        """Appends an event row when the store exists; read paths never create it for an event."""
        conn = conn or self._ready()
        if conn is None:
            return
        conn.execute("INSERT INTO events(at, kind, agent, detail_json) VALUES(?, ?, ?, ?)",
                     (format_ts(self.now()), kind, agent,
                      json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    def _refuse(self, reason: str, detail: str, agent: Optional[str] = None,
                conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        self._event("refused", agent=agent, detail={"reason": reason, "detail": detail}, conn=conn)
        return {"ok": False, "reason": reason, "detail": detail}

    def events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Newest first; an empty list when the store does not exist."""
        conn = self._ready()
        if conn is None:
            return []
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "null")
            out.append(item)
        return out

    # --- operation ids (idempotence) ---------------------------------------------------

    def _cached_op(self, conn: sqlite3.Connection, sender: str, op_id: Optional[str]
                   ) -> Optional[Dict[str, Any]]:
        if not op_id:
            return None
        row = conn.execute("SELECT answer_json FROM operations WHERE sender=? AND op_id=?",
                           (sender, op_id)).fetchone()
        return json.loads(row["answer_json"]) if row else None

    def _store_op(self, conn: sqlite3.Connection, sender: str, op_id: Optional[str],
                  answer: Dict[str, Any]) -> None:
        """Caches only accepted answers: a refused call may be retried with the same op id."""
        if op_id and answer.get("ok"):
            conn.execute("INSERT OR REPLACE INTO operations(sender, op_id, answer_json, created) "
                         "VALUES(?, ?, ?, ?)", (sender, op_id, json.dumps(answer, ensure_ascii=False),
                                                format_ts(self.now())))

    # --- lazy expiry and the 10-minute release ------------------------------------------

    def _sweep(self) -> None:
        """Runs at the start of every public operation: lazy expiry, then the 10-minute releases."""
        self._expire()
        self.release_stale()
        self.release_offline_sessions()

    def _expire(self) -> None:
        conn = self._ready()
        if conn is None:
            return
        now = format_ts(self.now())
        stale = conn.execute(
            f"SELECT id FROM mail WHERE state IN ({','.join('?' * len(OPEN_MAIL_STATES))}) "
            "AND expires<=? ORDER BY id", (*OPEN_MAIL_STATES, now)).fetchall()
        expired_tasks = conn.execute(
            "SELECT id FROM tasks WHERE status IN ('pending','blocked','in_progress') AND expires<=? "
            "ORDER BY id", (now,)).fetchall()
        expired_queries = conn.execute(
            "SELECT id FROM session_queries WHERE status IN ('queued','dispatched') AND expires<=? "
            "ORDER BY id", (now,)).fetchall()
        old_queries = conn.execute(
            "SELECT id FROM session_queries WHERE status IN ('completed','failed','expired','cancelled') "
            "AND COALESCE(completed, created)<=? LIMIT 1",
            (format_ts(self.now() - mail_queries.QUERY_RETENTION),)).fetchall()
        idle_sessions = conn.execute(
            "SELECT session_key FROM agent_sessions WHERE session_key!='main' AND status='active' "
            "AND COALESCE(last_used, created)<=? LIMIT 1",
            (format_ts(self.now() - mail_queries.SESSION_IDLE_TTL),)).fetchall()
        if not stale and not expired_tasks and not expired_queries and not old_queries and not idle_sessions:
            return
        with self._write() as wconn:
            for row in stale:
                wconn.execute("UPDATE mail SET state='expired' WHERE id=? AND state IN "
                              f"({','.join('?' * len(OPEN_MAIL_STATES))})", (row["id"], *OPEN_MAIL_STATES))
                self._event("mail_expired", detail={"id": row["id"]}, conn=wconn)
            for row in expired_tasks:
                wconn.execute("UPDATE tasks SET status='expired', modified=? WHERE id=? AND status IN "
                              "('pending','blocked','in_progress')", (now, row["id"]))
                self._event("task_expired", detail={"id": row["id"]}, conn=wconn)
                self._propagate_blockers(wconn, row["id"], "dead")
            if expired_queries or old_queries or idle_sessions:
                self._expire_queries(wconn, now)

    # --- agents and incarnations --------------------------------------------------------

    def _agent(self, conn: sqlite3.Connection, address: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM agents WHERE address=?", (address,)).fetchone()

    def _recipient(self, conn: sqlite3.Connection, address: str) -> Tuple[str, Optional[sqlite3.Row]]:
        """Resolves an address to ("agent", row), ("session", row) or (None, None); agents win a
        collision (a session may never bind an address an agent holds)."""
        agent = self._agent(conn, address)
        if agent is not None:
            return "agent", agent
        session = conn.execute("SELECT * FROM sessions WHERE address=?", (address,)).fetchone()
        if session is not None:
            return "session", session
        return None, None

    def upsert_agent(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Inserts or updates name/node/provider/driver/peers from a declaration entry; keeps the
        state and epoch of an existing row. A new agent starts `down` at epoch 0."""
        now = format_ts(self.now())
        peers = entry.get("peers")
        peers_json = json.dumps(sorted(peers), ensure_ascii=False) if peers is not None else None
        with self._write() as conn:
            stored = self._agent(conn, entry["address"])
            if stored is not None and any(stored[key] != entry[key] for key in ("node", "provider",
                                                                               "driver")):
                # A changed declaration ends a bind_retries_exhausted refusal.
                conn.execute("UPDATE agents SET bind_failures=0 WHERE address=?",
                             (entry["address"],))
            conn.execute(
                "INSERT INTO agents(address, name, node, provider, driver, state, epoch, not_ready_since,"
                " peers_json, created, modified) VALUES(?,?,?,?,?,'down',0,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET name=excluded.name, node=excluded.node,"
                " provider=excluded.provider, driver=excluded.driver, peers_json=excluded.peers_json,"
                " modified=excluded.modified",
                (entry["address"], entry["name"], entry["node"], entry["provider"], entry["driver"],
                 now, peers_json, now, now))
            self._event("agent_upserted", agent=entry["address"],
                        detail={"provider": entry["provider"], "driver": entry["driver"]}, conn=conn)
        return {"ok": True, "address": entry["address"]}

    def agent_rows(self) -> List[Dict[str, Any]]:
        conn = self._ready()
        if conn is None:
            return []
        return [self._agent_dict(row) for row in
                conn.execute("SELECT * FROM agents ORDER BY address").fetchall()]

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["peers"] = json.loads(item.pop("peers_json") or "null")
        return item

    def _peers(self, conn: sqlite3.Connection, address: str) -> List[str]:
        """The agent's effective peer list: its declared list as stored (it may name itself, which
        the rotation's hand-over note needs), or every other declared agent plus every non-ended
        session when it declares none (SD4: one address space, default peers of each other)."""
        row = self._agent(conn, address)
        if row is None:
            return []
        if row["peers_json"]:
            return list(json.loads(row["peers_json"]))
        peers = [r["address"] for r in conn.execute(
            "SELECT address FROM agents WHERE address!=?", (address,)).fetchall()]
        peers += [r["address"] for r in conn.execute(
            "SELECT address FROM sessions WHERE state!='ended' AND address!=?",
            (address,)).fetchall()]
        return peers

    def note(self, kind: str, agent: Optional[str] = None, detail: Any = None) -> None:
        """Writes one event row from outside the store (the registry's checks and removal steps);
        creates no store, so an empty data dir stays empty."""
        self._event(kind, agent=agent, detail=detail)

    def bind_begin(self, address: str) -> Dict[str, Any]:
        self._sweep()
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            if row["state"] == "retiring":
                return self._refuse("agent_retired", f"{address} is being removed", agent=address, conn=conn)
            if row["bind_failures"] >= BIND_RETRIES:
                return self._refuse("bind_retries_exhausted", f"{address} failed "
                                    f"{row['bind_failures']} binds in a row", agent=address, conn=conn)
            now, epoch = format_ts(self.now()), row["epoch"] + 1
            not_ready = format_ts(self.now()) if (row["state"] == "ready" or not row["not_ready_since"]) \
                else row["not_ready_since"]
            conn.execute("UPDATE agents SET epoch=?, state='binding', not_ready_since=?, bind_started=?, "
                         "modified=? WHERE address=?", (epoch, not_ready, now, now, address))
            conn.execute("UPDATE incarnations SET ended=?, end_reason='rebind' WHERE address=? AND "
                         "epoch=? AND ended IS NULL", (now, address, row["epoch"]))
            conn.execute("INSERT INTO incarnations(address, epoch, started) VALUES(?, ?, ?)",
                         (address, epoch, now))
            open_turns = conn.execute(
                "SELECT id FROM turns WHERE address=? AND ended IS NULL AND epoch<?",
                (address, epoch)).fetchall()
            for turn in open_turns:
                self._end_turn_row(conn, address, turn["id"], "lost")
                self._settle_query_on_turn_end(conn, address, turn["id"], "lost", None, None, False)
            self._fail_epoch_queries(conn, address, "epoch_changed", max_epoch=row["epoch"])
            self._close_epoch_sessions(conn, address, max_epoch=row["epoch"])
            self._event("bind_begin", agent=address, detail={"epoch": epoch}, conn=conn)
            return {"ok": True, "epoch": epoch}

    def bind_complete(self, address: str, epoch: int, session_id: Optional[str] = None,
                      pane: Optional[str] = None, pid: Optional[str] = None) -> Dict[str, Any]:
        """session_id may be empty (agy chooses its id on the first turn; the first turn_end then
        stores it); a successful bind clears the failure count of the retry rule."""
        self._sweep()
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            if row["epoch"] != epoch:
                return self._refuse("wrong_epoch", f"agent is at epoch {row['epoch']}, not {epoch}",
                                    agent=address, conn=conn)
            now = format_ts(self.now())
            conn.execute("UPDATE agents SET state='ready', not_ready_since=NULL, session_id=?, "
                         "bind_started=NULL, bind_failures=0, modified=? WHERE address=?",
                         (session_id, now, address))
            conn.execute("UPDATE incarnations SET session_id=?, pane=?, pid=? WHERE address=? AND "
                         "epoch=?", (session_id, pane, pid, address, epoch))
            self._sync_main_session(conn, address, epoch, session_id, session_ready=0)
            self._event("bind_complete", agent=address,
                        detail={"epoch": epoch, "session_id": session_id}, conn=conn)
            return {"ok": True, "epoch": epoch, "state": "ready"}

    def check_bind_timeouts(self) -> List[str]:
        """Every `binding` older than BIND_TIMEOUT becomes `bind_failed` (failures + 1, event);
        returns the addresses. Creates no store (the relay polls this every tick)."""
        conn = self._ready()
        if conn is None:
            return []
        cutoff = format_ts(self.now() - BIND_TIMEOUT)
        rows = conn.execute("SELECT address FROM agents WHERE state='binding' AND bind_started IS "
                            "NOT NULL AND bind_started<=?", (cutoff,)).fetchall()
        failed: List[str] = []
        with self._write() as wconn:
            for row in rows:
                cursor = wconn.execute(
                    "UPDATE agents SET state='bind_failed', bind_started=NULL, "
                    "bind_failures=bind_failures+1, modified=? WHERE address=? AND state='binding'",
                    (format_ts(self.now()), row["address"]))
                if cursor.rowcount:
                    self._event("bind_failed", agent=row["address"], conn=wconn)
                    failed.append(row["address"])
        return failed

    def _session_changed(self, conn: sqlite3.Connection, address: str, epoch: int,
                         session_id: str) -> None:
        """A CLI reported a session id other than the bound one: the conversation started fresh,
        so the incarnation ends (`resume_new_session`); the next bind opens a new one."""
        conn.execute("UPDATE incarnations SET ended=?, end_reason='resume_new_session' WHERE "
                     "address=? AND epoch=? AND ended IS NULL", (format_ts(self.now()), address, epoch))
        self._event("resume_new_session", agent=address,
                    detail={"epoch": epoch, "session_id": session_id}, conn=conn)

    def report_resume(self, address: str, epoch: int, session_id: str) -> Dict[str, Any]:
        self._sweep()
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            incarnation = conn.execute("SELECT * FROM incarnations WHERE address=? AND epoch=?",
                                       (address, epoch)).fetchone()
            if incarnation is None or row["epoch"] != epoch:
                return self._refuse("wrong_epoch", f"agent is at epoch {row['epoch']}, not {epoch}",
                                    agent=address, conn=conn)
            if incarnation["session_id"] == session_id and session_id is not None:
                return {"ok": True, "new_session": False}
            self._session_changed(conn, address, epoch, session_id)
            return {"ok": True, "new_session": True}

    def set_down(self, address: str, reason: str) -> Dict[str, Any]:
        self._sweep()
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            now = format_ts(self.now())
            not_ready = now if row["state"] == "ready" else row["not_ready_since"] or now
            conn.execute("UPDATE agents SET state='down', not_ready_since=?, modified=? WHERE address=?",
                         (not_ready, now, address))
            self._event("agent_down", agent=address, detail={"reason": reason}, conn=conn)
            return {"ok": True, "state": "down"}

    def set_retiring(self, address: str) -> Dict[str, Any]:
        """Removal step 1: new mail to the agent is refused with `agent_retired`."""
        self._sweep()
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            conn.execute("UPDATE agents SET state='retiring', modified=? WHERE address=?",
                         (format_ts(self.now()), address))
            self._fail_epoch_queries(conn, address, "agent_retired")
            self._close_epoch_sessions(conn, address)
            self._event("agent_retiring", agent=address, conn=conn)
            return {"ok": True, "state": "retiring"}

    def release_stale(self) -> List[str]:
        """Every agent not `ready` for RELEASE_AFTER or more gives up its in_progress tasks."""
        conn = self._ready()
        if conn is None:
            return []
        cutoff = format_ts(self.now() - RELEASE_AFTER)
        rows = conn.execute("SELECT address FROM agents WHERE state!='ready' AND not_ready_since IS "
                            "NOT NULL AND not_ready_since<=?", (cutoff,)).fetchall()
        released: List[str] = []
        for row in rows:
            released.extend(self.release_agent_tasks(row["address"], "agent_not_ready"))
        return released

    # --- turns ---------------------------------------------------------------------------

    def _open_turn(self, conn: sqlite3.Connection, address: str, epoch: Optional[int],
                   turn: Optional[str]) -> Optional[sqlite3.Row]:
        """The agent's open turn at its current epoch, or None (`no_turn` / `wrong_epoch`)."""
        if turn is None or epoch is None:
            return None
        row = conn.execute("SELECT t.* FROM turns t JOIN agents a ON a.address=t.address "
                           "WHERE t.id=? AND t.address=? AND t.ended IS NULL AND t.epoch=a.epoch "
                           "AND a.epoch=?", (turn, address, epoch)).fetchone()
        return row

    def admit_turn(self, address: str, epoch: int, kind: str,
                   query_id: Optional[str] = None) -> Dict[str, Any]:
        self._sweep()
        if resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=address)
        if kind not in TURN_KINDS:
            return self._refuse("bad_input", f"turn kind must be one of {', '.join(TURN_KINDS)}",
                                agent=address)
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            if row["state"] == "retiring":
                return self._refuse("agent_retired", f"{address} is being removed", agent=address, conn=conn)
            if row["epoch"] != epoch or row["state"] != "ready":
                return self._refuse("wrong_epoch", f"agent is {row['state']} at epoch {row['epoch']}, "
                                                   f"not ready at {epoch}", agent=address, conn=conn)
            hour_ago = format_ts(self.now() - timedelta(hours=1))
            if kind == "query":
                if not query_id:
                    return self._refuse("bad_input", "query turn requires query_id",
                                        agent=address, conn=conn)
                qrow = conn.execute(
                    "SELECT * FROM session_queries WHERE id=? AND address=?",
                    (query_id, address),
                ).fetchone()
                if qrow is None or qrow["status"] not in mail_queries.QUERY_UNSTARTED_STATES:
                    return self._refuse("query_not_active",
                                        f"query {query_id!r} is not queued/dispatched for {address}",
                                        agent=address, conn=conn)
                if qrow["epoch"] != epoch:
                    return self._refuse("wrong_epoch",
                                        f"query {query_id} belongs to epoch {qrow['epoch']}, not {epoch}",
                                        agent=address, conn=conn)
                recent = conn.execute(
                    "SELECT COUNT(*) AS n FROM turns WHERE address=? AND kind='query' AND started>=?",
                    (address, hour_ago),
                ).fetchone()["n"]
                if recent >= mail_queries.QUERIES_PER_HOUR:
                    return self._refuse("query_budget",
                                        f"{recent} direct queries in the last hour "
                                        f"(limit {mail_queries.QUERIES_PER_HOUR})",
                                        agent=address, conn=conn)
            else:
                recent = conn.execute(
                    "SELECT COUNT(*) AS n FROM turns WHERE address=? AND kind!='query' AND started>=?",
                    (address, hour_ago),
                ).fetchone()["n"]
                if recent >= TURNS_PER_HOUR:
                    return self._refuse("turn_budget", f"{recent} turns in the last hour "
                                                       f"(limit {TURNS_PER_HOUR})", agent=address, conn=conn)
            turn_id, now = new_id("u-"), format_ts(self.now())
            conn.execute("INSERT INTO turns(id, address, epoch, kind, started) VALUES(?,?,?,?,?)",
                         (turn_id, address, epoch, kind, now))
            if kind == "query":
                conn.execute(
                    "UPDATE session_queries SET status='running', turn_id=?, started=? WHERE id=?",
                    (turn_id, now, query_id),
                )
            owned = conn.execute("SELECT id, hop FROM tasks WHERE owner=? AND status='in_progress'",
                                 (address,)).fetchall()
            for task in owned:
                conn.execute("INSERT OR IGNORE INTO turn_inputs(turn, kind, ref, hop) VALUES(?,?,?,?)",
                             (turn_id, "task", task["id"], task["hop"]))
            detail: Dict[str, Any] = {"turn": turn_id, "kind": kind, "epoch": epoch}
            if query_id:
                detail["query_id"] = query_id
            self._event("turn_admitted", agent=address, detail=detail, conn=conn)
            return {"ok": True, "turn": turn_id}

    def record_retrieved(self, address: str, epoch: int, turn: str,
                         mail_ids: Sequence[str]) -> Dict[str, Any]:
        """What the daemon calls when mail_inbox returns: records the envelopes this turn read."""
        self._sweep()
        with self._write() as conn:
            agent = self._agent(conn, address)
            if agent is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            if self._open_turn(conn, address, epoch, turn) is None:
                return self._refuse("wrong_epoch", f"turn {turn} is not {address}'s open turn at "
                                                   f"epoch {epoch}", agent=address, conn=conn)
            for mail_id in mail_ids:
                row = conn.execute("SELECT id, hop FROM mail WHERE id=? AND recipient=? AND state IN "
                                   "('queued','delivered','retrieved')", (mail_id, address)).fetchone()
                if row is None:
                    return self._refuse("bad_input", f"{mail_id} is not unread mail to {address}",
                                        agent=address, conn=conn)
            for mail_id in mail_ids:
                conn.execute("UPDATE mail SET state='retrieved', retrieved_turn=? WHERE id=?",
                             (turn, mail_id))
                hop = conn.execute("SELECT hop FROM mail WHERE id=?", (mail_id,)).fetchone()["hop"]
                conn.execute("INSERT OR IGNORE INTO turn_inputs(turn, kind, ref, hop) VALUES(?,?,?,?)",
                             (turn, "mail", mail_id, hop))
            self._event("mail_retrieved", agent=address,
                        detail={"turn": turn, "ids": list(mail_ids)}, conn=conn)
            return {"ok": True, "ids": list(mail_ids)}

    def _end_turn_row(self, conn: sqlite3.Connection, address: str, turn: str, end_reason: str,
                      acked: Sequence[str] = ()) -> None:
        """Ends the turn and settles its mail: listed ids acked on `normal`, the rest back to queued."""
        turn_row = conn.execute("SELECT * FROM turns WHERE id=? AND address=? AND ended IS NULL",
                                (turn, address)).fetchone()
        if turn_row is None:
            return
        ended = format_ts(self.now())
        conn.execute("UPDATE turns SET ended=?, end_reason=? WHERE id=?", (ended, end_reason, turn))
        if end_reason == "normal":
            for mail_id in acked:
                conn.execute("UPDATE mail SET state='acked' WHERE id=? AND retrieved_turn=? AND "
                             "state='retrieved'", (mail_id, turn))
        conn.execute("UPDATE mail SET state='queued', redelivered=1 WHERE retrieved_turn=? AND "
                     "state='retrieved'", (turn,))

    def end_turn(self, address: str, epoch: int, turn: str, end_reason: str = "normal",
                 acked: Sequence[str] = (), session_id: Optional[str] = None,
                 query_id: Optional[str] = None,
                 query_reply: Optional[Dict[str, Any]] = None,
                 session_key: Optional[str] = None) -> Dict[str, Any]:
        """Only the current epoch's open turn matches; any other call changes no mail, so it is
        redelivered when the epoch's open turns are ended by bind_begin. A session_id other than
        the stored one ends the incarnation (`resume_new_session`) for `main`, or isolates the loss
        to `session_key` for scoped sessions."""
        self._sweep()
        with self._write() as conn:
            agent = self._agent(conn, address)
            if agent is None:
                return self._refuse("unknown_agent", f"no agent {address}", conn=conn)
            open_turn = self._open_turn(conn, address, epoch, turn)
            if open_turn is None:
                return self._refuse("wrong_epoch", f"turn {turn} is not {address}'s open turn at "
                                                   f"epoch {epoch}", agent=address, conn=conn)
            qrow = None
            if query_id:
                qrow = conn.execute("SELECT * FROM session_queries WHERE id=? AND address=?",
                                    (query_id, address)).fetchone()
            if qrow is None:
                qrow = conn.execute("SELECT * FROM session_queries WHERE turn_id=? AND address=?",
                                    (turn, address)).fetchone()
            effective_key = session_key or (qrow["session_key"] if qrow is not None else "main")
            qmode = qrow["mode"] if qrow is not None else "in_place"

            resumed = False
            scoped_lost = False
            if qmode == "fork":
                # Read-only fork: never mutate main or scoped provider_session_id.
                pass
            elif effective_key == "main":
                resumed = self._record_session(conn, agent, session_id)
                effective_sid = session_id or agent["session_id"]
                if end_reason == "normal" and effective_sid and not resumed:
                    conn.execute(
                        "UPDATE incarnations SET session_ready=1 WHERE address=? AND epoch=? AND ended IS NULL",
                        (address, epoch),
                    )
                    self._sync_main_session(conn, address, epoch, effective_sid,
                                            session_ready=1, bump_turns=True)
            else:
                scoped_lost = self._record_scoped_session(
                    conn, address, epoch, effective_key, session_id, end_reason,
                    qrow["id"] if qrow is not None else query_id, query_reply,
                )

            self._end_turn_row(conn, address, turn, end_reason, acked=acked)
            self._settle_query_on_turn_end(conn, address, turn, end_reason, query_id, query_reply,
                                           resumed or scoped_lost)
            self._event("turn_ended", agent=address,
                        detail={"turn": turn, "end_reason": end_reason,
                                "acked": list(acked),
                                **({"session_key": effective_key} if effective_key != "main" else {})},
                        conn=conn)
            answer: Dict[str, Any] = {"ok": True, "turn": turn, "end_reason": end_reason}
            if resumed:
                answer["resume_new_session"] = True
            return answer

    def _record_session(self, conn: sqlite3.Connection, agent: sqlite3.Row,
                        session_id: Optional[str]) -> bool:
        """The first session id of an incarnation is stored on the agent and the incarnation; a
        later different one ends the incarnation. Returns True for the resume_new_session case."""
        if not session_id:
            return False
        address, epoch = agent["address"], agent["epoch"]
        if not agent["session_id"]:
            conn.execute("UPDATE agents SET session_id=?, modified=? WHERE address=?",
                         (session_id, format_ts(self.now()), address))
            conn.execute("UPDATE incarnations SET session_id=? WHERE address=? AND epoch=? AND "
                         "ended IS NULL", (session_id, address, epoch))
            return False
        if session_id == agent["session_id"]:
            return False
        self._session_changed(conn, address, epoch, session_id)
        return True

    def turn_hop(self, turn: str) -> int:
        """1 + the highest hop among the turn's recorded inputs (0 when it has none)."""
        conn = self._ready()
        if conn is None:
            return 1
        row = conn.execute("SELECT MAX(hop) AS hop FROM turn_inputs WHERE turn=?", (turn,)).fetchone()
        return 1 + (row["hop"] or 0)

    # --- mail ---------------------------------------------------------------------------

    def _mail_id(self, conn: sqlite3.Connection) -> str:
        day = self.now().strftime("%Y%m%d")
        prefix = f"m-{day}-"
        rows = conn.execute("SELECT id FROM mail WHERE id LIKE ?", (prefix + "%",)).fetchall()
        highest = max((int(row["id"][len(prefix):]) for row in rows), default=0)
        return f"{prefix}{highest + 1:04d}"

    def _insert_mail(self, conn: sqlite3.Connection, sender: str, from_kind: str, to: str, kind: str,
                     subject: str, body: str, thread: str, hop: int, turn: Optional[str],
                     task: Optional[str], reply_to: Optional[str]) -> str:
        now = format_ts(self.now())
        mail_id = self._mail_id(conn)
        # The operator has no node and no spool, so nothing will ever push this mail: it starts
        # delivered. Every other recipient waits for the relay as before.
        state = "delivered" if to == "operator" else "queued"
        conn.execute(
            "INSERT INTO mail(id, thread, sender, from_kind, recipient, kind, subject, body, task, "
            "reply_to, hop, turn, created, expires, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mail_id, thread, sender, from_kind, to, kind, subject, body, task, reply_to, hop, turn,
             now, format_ts(self.now() + LIFETIME), state))
        return mail_id

    def _hub_notice(self, conn: sqlite3.Connection, to: str, subject: str, body: str) -> str:
        """A hub status notice, bypassing the public send checks (system traffic, hop 0)."""
        return self._insert_mail(conn, "hub", "hub", to, "status", subject, body, new_id("t-"), 0,
                                 None, None, None)

    def send(self, sender: str, to: str, kind: str, subject: str, body: str, *, op_id: Optional[str],
             thread: Optional[str] = None, task: Optional[str] = None, reply_to: Optional[str] = None,
             epoch: Optional[int] = None, turn: Optional[str] = None) -> Dict[str, Any]:
        self._sweep()
        if resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=sender)
        if self._ready() is None:
            # No store means no agent was ever declared: refuse without creating the file.
            missing = to if sender in SPECIAL_SENDERS else sender
            return {"ok": False, "reason": "unknown_agent", "detail": f"no agent {missing}"}
        with self._write() as conn:
            cached = self._cached_op(conn, sender, op_id)
            if cached is not None:
                return cached
            answer = self._send_checked(conn, sender, to, kind, subject, body, thread=thread, task=task,
                                        reply_to=reply_to, epoch=epoch, turn=turn)
            self._store_op(conn, sender, op_id, answer)
            return answer

    def _send_checked(self, conn: sqlite3.Connection, sender: str, to: str, kind: str, subject: str,
                      body: str, *, thread: Optional[str], task: Optional[str],
                      reply_to: Optional[str], epoch: Optional[int], turn: Optional[str]
                      ) -> Dict[str, Any]:
        if not isinstance(subject, str) or not isinstance(body, str):
            return self._refuse("bad_input", "subject and body must be text", agent=sender, conn=conn)
        if sender in SPECIAL_SENDERS:
            return self._send_special(conn, sender, to, kind, subject, body, thread=thread, task=task,
                                      reply_to=reply_to)
        if not valid_address(sender):
            return self._refuse("bad_input", f"bad sender {sender!r}", conn=conn)
        agent = self._agent(conn, sender)
        if agent is None:
            return self._refuse("unknown_agent", f"no agent {sender}", agent=sender, conn=conn)
        if agent["state"] == "retiring":
            return self._refuse("agent_retired", f"{sender} is being removed", agent=sender, conn=conn)
        if turn is None or epoch is None:
            return self._refuse("no_turn", "an agent send needs the open turn (epoch and turn)",
                                agent=sender, conn=conn)
        if self._open_turn(conn, sender, epoch, turn) is None:
            return self._refuse("wrong_epoch", f"turn {turn} is not {sender}'s open turn at epoch "
                                               f"{epoch}", agent=sender, conn=conn)
        if kind in RESERVED_KINDS:
            return self._refuse("reserved_kind", f"{kind} is reserved for the hub and the operator",
                                agent=sender, conn=conn)
        if kind not in MAIL_KINDS:
            return self._refuse("bad_kind", f"kind must be one of {', '.join(MAIL_KINDS)}",
                                agent=sender, conn=conn)
        recipient_kind, recipient = self._recipient(conn, to) if valid_address(to) else (None, None)
        if to != "operator" and (recipient is None or to not in self._peers(conn, sender)):
            return self._refuse("not_a_peer", f"{to} is not operator or a peer of {sender}",
                                agent=sender, conn=conn)
        if recipient_kind == "agent" and recipient["state"] == "retiring":
            return self._refuse("agent_retired", f"{to} is being removed", agent=sender, conn=conn)
        return self._send_common(conn, sender, "agent", to, kind, subject, body, thread=thread,
                                 task=task, reply_to=reply_to, turn=turn)

    def _send_special(self, conn: sqlite3.Connection, sender: str, to: str, kind: str, subject: str,
                      body: str, *, thread: Optional[str], task: Optional[str],
                      reply_to: Optional[str]) -> Dict[str, Any]:
        if sender == "hub":
            if kind != "status":
                return self._refuse("bad_kind", "the hub sends status only", agent=sender, conn=conn)
            if subject not in HUB_STATUS_TYPES:
                return self._refuse("bad_input", f"status subtype must be one of "
                                                 f"{', '.join(HUB_STATUS_TYPES)}", agent=sender, conn=conn)
        elif kind not in OPERATOR_KINDS:
            return self._refuse("bad_kind", f"the operator sends one of {', '.join(OPERATOR_KINDS)}",
                                agent=sender, conn=conn)
        if to in SPECIAL_SENDERS or not valid_address(to):
            return self._refuse("bad_address", f"bad recipient {to!r}", agent=sender, conn=conn)
        recipient_kind, recipient = self._recipient(conn, to)
        if recipient is None:
            return self._refuse("unknown_agent", f"no agent {to}", agent=sender, conn=conn)
        if recipient_kind == "agent" and recipient["state"] == "retiring":
            return self._refuse("agent_retired", f"{to} is being removed", agent=sender, conn=conn)
        # a session row still accepts mail whatever its state: it stays queued until the session
        # is linked again or the mail expires
        return self._send_common(conn, sender, sender, to, kind, subject, body, thread=thread,
                                 task=task, reply_to=reply_to, turn=None)

    def _send_common(self, conn: sqlite3.Connection, sender: str, from_kind: str, to: str, kind: str,
                     subject: str, body: str, *, thread: Optional[str], task: Optional[str],
                     reply_to: Optional[str], turn: Optional[str],
                     session_sender: bool = False) -> Dict[str, Any]:
        """The checks after identity: reply_to, task, thread, hop, thread size, body size, insert.
        A task the sender cannot see is accepted on one path only: a reply whose answered mail,
        received by the sender, carries that task. Agents and sessions share the reply_to and
        thread visibility rules; a session sender has no turn, so its hop comes from the thread
        (mail_sessions._session_hop) instead of turn_hop."""
        reply_row = None
        if reply_to is not None:
            query, params = "SELECT id, thread, task, recipient, hop FROM mail WHERE id=?", [reply_to]
            if from_kind in ("agent", "session"):  # mail the sender sent or received
                query += " AND (sender=? OR recipient=?)"
                params += [sender, sender]
            reply_row = conn.execute(query, params).fetchone()
            if reply_row is None:
                return self._refuse("invisible_reply_to", f"{reply_to} is not mail {sender} sent or "
                                                          f"received", agent=sender, conn=conn)
            thread = thread or reply_row["thread"]
        if task is not None and not self._task_visible(conn, self._task_row(conn, task), sender):
            # the exception above: the sender received the mail it answers with this task tag
            inherits_task = (reply_row is not None and reply_row["task"] == task
                             and reply_row["recipient"] == sender)
            if not inherits_task:
                return self._refuse("invisible_task", f"task {task} is not visible to {sender}",
                                    agent=sender, conn=conn)
        if thread is not None and from_kind in ("agent", "session"):
            in_thread = conn.execute("SELECT 1 FROM mail WHERE thread=? AND (sender=? OR recipient=?) "
                                     "LIMIT 1", (thread, sender, sender)).fetchone()
            if in_thread is None:
                return self._refuse("not_in_thread", f"{sender} is not part of thread {thread}",
                                    agent=sender, conn=conn)
        hop = self._session_hop(conn, sender, thread, reply_row) if session_sender \
            else (self.turn_hop(turn) if turn else 0)
        if hop > HOP_MAX:
            return self._refuse("hop_limit", f"hop {hop} is above {HOP_MAX}", agent=sender, conn=conn)
        thread = thread or new_id("t-")
        count = conn.execute("SELECT COUNT(*) AS n FROM mail WHERE thread=?", (thread,)).fetchone()["n"]
        if count >= THREAD_MAX:
            return self._refuse("thread_limit", f"thread {thread} already has {count} messages "
                                                f"(limit {THREAD_MAX})", agent=sender, conn=conn)
        if len(subject.encode("utf-8")) + len(body.encode("utf-8")) > BODY_MAX:
            return self._refuse("too_large", f"subject and body exceed {BODY_MAX} bytes", agent=sender,
                                conn=conn)
        mail_id = self._insert_mail(conn, sender, from_kind, to, kind, subject, body, thread, hop,
                                    turn, task, reply_to)
        answer: Dict[str, Any] = {"ok": True, "id": mail_id, "thread": thread, "hop": hop}
        recipient_kind, recipient = self._recipient(conn, to)
        if recipient_kind == "agent" and recipient["state"] != "ready":
            answer["note"] = "recipient down, queued"
        elif recipient_kind == "session" and recipient["state"] not in ("linked", "busy"):
            answer["note"] = "recipient down, queued"
        self._event("mail_sent", agent=sender,
                    detail={"id": mail_id, "to": to, "kind": kind, "hop": hop}, conn=conn)
        return answer

    def inbox_queued(self, address: str) -> List[Dict[str, Any]]:
        """Queued (including redelivered) mail of an agent, oldest first: what the relay will push."""
        self._sweep()
        conn = self._ready()
        if conn is None:
            return []
        rows = conn.execute("SELECT * FROM mail WHERE recipient=? AND state='queued' ORDER BY created, id",
                            (address,)).fetchall()
        return [dict(row) for row in rows]

    def list_mail(self, address: Optional[str] = None, thread: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        self._sweep()
        conn = self._ready()
        if conn is None:
            return []
        query, params = "SELECT * FROM mail", []
        clauses = []
        if address is not None:
            clauses.append("(sender=? OR recipient=?)")
            params.extend((address, address))
        if thread is not None:
            clauses.append("thread=?")
            params.append(thread)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in conn.execute(query, params).fetchall()]

    def mark_undeliverable(self, address: str) -> List[str]:
        """Unacked mail to the agent becomes undeliverable; each agent sender gets one hub notice."""
        self._sweep()
        with self._write() as conn:
            rows = conn.execute("SELECT id, sender, from_kind FROM mail WHERE recipient=? AND state IN "
                                "('queued','delivered','retrieved') ORDER BY id",
                                (address,)).fetchall()
            for row in rows:
                conn.execute("UPDATE mail SET state='undeliverable' WHERE id=?", (row["id"],))
                self._event("mail_undeliverable", agent=address, detail={"id": row["id"]}, conn=conn)
            by_sender: Dict[str, List[str]] = {}
            for row in rows:
                if row["from_kind"] == "agent":
                    by_sender.setdefault(row["sender"], []).append(row["id"])
                else:
                    self._event("mail_undeliverable_sender", agent=row["sender"],
                                detail={"ids": [row["id"]], "recipient": address}, conn=conn)
            for sender, ids in by_sender.items():
                self._hub_notice(conn, sender, "undeliverable",
                                 f"mail to {address} became undeliverable: {', '.join(ids)}")
            return [row["id"] for row in rows]

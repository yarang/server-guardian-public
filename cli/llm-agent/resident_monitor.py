"""Read-only snapshot of the resident mail store for the dashboard's monitor tab.

docs/design/resident-monitor.md, RM1: one bundled answer to "which resident agents exist and what
are they doing", polled as one Python spawn. The read path never writes: it opens the store itself
with the SQLite URI `file:<path>?mode=ro` and calls neither MailStore (whose _ready() would run
the schema migrations and could create or migrate the file from a dashboard poll) nor
resident_registry.load_declarations() (which writes a declaration_problem event); the declaration
file is read as plain JSON. A store older or newer than this module still shows: every SELECT is
the intersection of the wanted columns with PRAGMA table_info, a missing column reads as None and
a missing table gives an empty section plus a problems line. Mail bodies, task patches and call
logs never leave as text through the snapshot: it carries metadata, sizes and counts, and
mail_body() is the one body route, masked through audit_run.mask_secrets.
"""

import base64
import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import mail_artifacts
import mail_queries
import mail_store
import resident_registry

DEFAULT_LIMITS = {"turns": 20, "mail": 100, "closed_tasks": 50, "events": 100, "artifacts": 50, "queries": 50}
MAIL_ID_RE = re.compile(r"^m-\d{8}-\d{4,}$", re.ASCII)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Copied on purpose instead of read through the store: the monitor may not call the mail helpers
# or the task board, and the constants keep this read path explicit about what "open" means.
OPEN_MAIL_STATES = ("queued", "delivered", "retrieved")               # mail_store.OPEN_MAIL_STATES
TASK_OPEN_STATES = ("pending", "blocked", "in_progress")              # mail_tasks.TASK_OPEN_STATES
TASK_CLOSED_STATES = ("completed", "failed", "cancelled", "expired")  # mail_tasks.TASK_FINISHED_STATES


def _col(name: str) -> Tuple[str, str, Optional[str]]:
    """A plain table column: selected under its own name; a missing column reads as None."""
    return name, name, name


# (key, SQL expression, required column; None = no column requirement). Names and expressions
# come from these constants only, never from input.
_AGENTS_WANTED = tuple(_col(c) for c in (
    "address", "name", "node", "provider", "driver", "state", "epoch", "not_ready_since",
    "bind_failures", "bind_started", "modified"))
_INCARNATION_WANTED = tuple(_col(c) for c in ("epoch", "started", "pane", "pid"))
_OPEN_TURN_WANTED = tuple(_col(c) for c in ("id", "kind", "started", "hop_in"))
_TURNS_WANTED = tuple(_col(c) for c in (
    "id", "epoch", "kind", "started", "ended", "end_reason", "hop_in"))
_INPUTS_EXPR = "(SELECT COUNT(*) FROM turn_inputs AS i WHERE i.turn = turns.id)"
_MAIL_WANTED = (*(_col(c) for c in (
    "id", "thread", "sender", "recipient", "kind", "subject", "task", "reply_to", "hop", "state",
    "created", "expires", "redelivered")), ("bytes", "length(CAST(body AS BLOB))", "body"))
_TASK_WANTED = (*(_col(c) for c in (
    "id", "title", "creator", "assignee", "owner", "status", "project", "sha", "hop", "generation",
    "reason", "created", "modified", "expires")),
    ("blocked_by_json", "blocked_by_json", "blocked_by_json"),
    ("patch_bytes", "length(CAST(patch AS BLOB))", "patch"),
    ("patch_action", "patch_action", "patch_action"),
    ("calls_json", "calls_json", "calls_json"))
_EVENTS_WANTED = (*(_col(c) for c in ("id", "at", "kind", "agent")),
                  ("detail_json", "detail_json", "detail_json"))
_ARTIFACTS_WANTED = tuple(_col(c) for c in (
    "id", "agent", "task", "title", "filename", "rel_path", "mime", "sha256",
    "size_bytes", "summary", "pinned", "created", "expires"))
_QUERIES_WANTED = tuple(_col(c) for c in (
    "id", "address", "project", "project_kind", "session_key", "task", "epoch", "session_id", "caller",
    "caller_kind", "mode", "status", "turn_id", "answer_truncated", "error", "timeout_sec",
    "created", "started", "completed", "expires"))
_QUERY_DETAIL_WANTED = (*_QUERIES_WANTED, _col("prompt"), _col("answer"))
_SESSIONS_WANTED = tuple(_col(c) for c in (
    "address", "epoch", "session_key", "project", "label", "provider_session_id",
    "session_ready", "status", "turns", "created", "last_used", "closed"))


def open_readonly(path: Path) -> Optional[sqlite3.Connection]:
    """The store for reading only; None when the file is missing (nothing is ever created)."""
    if not Path(path).exists():
        return None
    uri = "file:" + urllib.parse.quote(str(path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def snapshot(limits: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """The monitor view of docs/design/resident-monitor.md section 3: agents, turns, mail, tasks
    and events of the resident world in one read-only pass. A missing store is an empty snapshot
    (store.exists False) that still lists the declared agents, never an exception."""
    lim = _limits(limits)
    problems: List[str] = []
    declared, declaration_problems = _declared_agents()
    problems.extend(declaration_problems)
    empty_mail = {state: 0 for state in OPEN_MAIL_STATES}
    snap: Dict[str, Any] = {
        "generated": mail_store.format_ts(datetime.now(timezone.utc)),
        "store": {"exists": False, "schema": None, "path_kind": _path_kind()},
        "agents": [_agent_item(address, declared.get(address), None, None, None, empty_mail, 0, False, 0)
                   for address in sorted(declared)],
        "turns": {}, "mail": [], "tasks": [], "events": [], "artifacts": [], "queries": [], "sessions": [],
        "problems": problems,
    }
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return snap
    try:
        snap["store"] = _store_block(conn, problems)
        snap["agents"] = _agents_section(conn, declared, problems)
        snap["turns"] = _turns_section(conn, lim["turns"], problems)
        snap["mail"] = _mail_section(conn, lim["mail"], problems)
        snap["tasks"] = _tasks_section(conn, lim["closed_tasks"], problems)
        snap["events"] = _events_section(conn, lim["events"], problems)
        snap["artifacts"] = _artifacts_section(conn, lim["artifacts"])
        snap["queries"] = _queries_section(conn, lim["queries"])
        snap["sessions"] = _sessions_section(conn)
    finally:
        conn.close()
    return snap


def _sessions_section(conn: sqlite3.Connection, *, agent: Optional[str] = None,
                      project: Optional[str] = None,
                      include_inactive: bool = False) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "agent_sessions"):
        return []
    epochs: Dict[str, int] = {}
    if _table_exists(conn, "agents"):
        for arow in conn.execute("SELECT address, epoch FROM agents").fetchall():
            epochs[str(arow["address"])] = int(arow["epoch"] or 0)
    clauses: List[str] = []
    params: List[Any] = []
    if not include_inactive:
        clauses.append("status='active'")
    if agent:
        clauses.append("address=?")
        params.append(agent)
    if project:
        clauses.append("(project=? OR session_key='main')")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
    rows, _, _ = _select(
        conn, "agent_sessions", _SESSIONS_WANTED,
        f"{where}ORDER BY address ASC, CASE WHEN session_key='main' THEN 0 ELSE 1 END, session_key ASC",
        tuple(params),
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        addr = row.get("address")
        if not include_inactive and addr in epochs and row.get("epoch") != epochs[addr]:
            continue
        out.append({**row, "session_ready": bool(row.get("session_ready"))})
    return out


def list_sessions(agent: Optional[str] = None, project: Optional[str] = None,
                  include_inactive: bool = False) -> Dict[str, Any]:
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return {"sessions": []}
    try:
        return {"sessions": _sessions_section(conn, agent=agent, project=project,
                                              include_inactive=include_inactive)}
    finally:
        conn.close()


def cli_sessions(agent: Optional[str], project: Optional[str], json_out: bool,
                 include_inactive: bool = False) -> int:
    try:
        data = list_sessions(agent=agent, project=project, include_inactive=include_inactive)
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    items = data["sessions"]
    if not items:
        print("no resident sessions")
        return 0
    for item in items:
        ready = "ready" if item.get("session_ready") else "pending"
        print(f"{item['address']:<24} epoch {item['epoch']}  {item['session_key']:<28}  "
              f"{item['status']:<8}  {ready:<7}  turns={item.get('turns') or 0}")
    return 0


def _queries_section(conn: sqlite3.Connection, limit: int, *,
                     project: Optional[str] = None,
                     agent: Optional[str] = None,
                     session_key: Optional[str] = None) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "session_queries") or limit <= 0:
        return []
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
    where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
    params.append(limit)
    rows, _, _ = _select(conn, "session_queries", _QUERIES_WANTED,
                         f"{where}ORDER BY created DESC, id DESC LIMIT ?", tuple(params))
    return [{**row, "session_key": row.get("session_key") or "main",
             "answer_truncated": bool(row.get("answer_truncated"))} for row in rows]


def list_queries(project: Optional[str] = None, agent: Optional[str] = None,
                 session_key: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return {"queries": []}
    try:
        cap = max(1, min(int(limit), 200))
        return {"queries": _queries_section(conn, cap, project=project, agent=agent,
                                            session_key=session_key)}
    finally:
        conn.close()


def query_get(query_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(query_id, str) or not mail_queries.QUERY_ID_RE.fullmatch(query_id):
        raise ValueError(f"bad query id {query_id!r}")
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return None
    try:
        if not _table_exists(conn, "session_queries"):
            return None
        rows, _, _ = _select(conn, "session_queries", _QUERY_DETAIL_WANTED,
                             "WHERE id=?", (query_id,))
    finally:
        conn.close()
    if not rows:
        return None
    row = dict(rows[0])
    row["session_key"] = row.get("session_key") or "main"
    row["answer_truncated"] = bool(row.get("answer_truncated"))
    row["prompt"] = mail_queries.mask_query_text(row.get("prompt"))
    row["answer"] = mail_queries.mask_query_text(row.get("answer"))
    return row


def cli_queries(project: Optional[str], agent: Optional[str], json_out: bool,
                session_key: Optional[str] = None, limit: int = 50) -> int:
    try:
        data = list_queries(project=project, agent=agent, session_key=session_key, limit=limit)
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    items = data["queries"]
    if not items:
        print("no resident session queries")
        return 0
    for item in items:
        print(f"{item['id']}  {item['created']}  {item['status']:<10}  {item['project']:<18}  "
              f"{item['address']}  epoch {item['epoch']}")
    return 0


def cli_query_get(query_id: str, json_out: bool) -> int:
    try:
        data = query_get(query_id)
    except ValueError:
        print(json.dumps({"error": "invalid query id"}, ensure_ascii=False))
        return 2
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if data is None:
        print(json.dumps({"error": "query not found"}, ensure_ascii=False))
        return 3
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"{data['id']}  {data['address']}  project={data['project']}  status={data['status']}")
        if data.get("error"):
            print(f"error: {data['error']}")
        if data.get("answer") is not None:
            print()
            print(data["answer"])
    return 0


def _artifacts_section(conn: sqlite3.Connection, limit: int, agent: Optional[str] = None) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "resident_artifacts") or limit <= 0:
        return []
    if agent:
        rows, _, _ = _select(conn, "resident_artifacts", _ARTIFACTS_WANTED,
                             "WHERE agent=? ORDER BY created DESC, id DESC LIMIT ?", (agent, limit))
    else:
        rows, _, _ = _select(conn, "resident_artifacts", _ARTIFACTS_WANTED,
                             "ORDER BY created DESC, id DESC LIMIT ?", (limit,))
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append({
            **row,
            "summary": _mask(row["summary"]) if isinstance(row.get("summary"), str) else "",
            "pinned": bool(row.get("pinned")),
        })
    return out


def list_artifacts(agent: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return {"artifacts": []}
    try:
        return {"artifacts": _artifacts_section(conn, limit, agent=agent)}
    finally:
        conn.close()


def artifact_body(artifact_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(artifact_id, str) or not mail_artifacts.ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise ValueError(f"bad artifact id {artifact_id!r}")
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return None
    try:
        if not _table_exists(conn, "resident_artifacts"):
            return None
        rows, _, _ = _select(conn, "resident_artifacts", _ARTIFACTS_WANTED,
                             "WHERE id=?", (artifact_id,))
    finally:
        conn.close()
    if not rows:
        return None
    meta = {**rows[0], "pinned": bool(rows[0].get("pinned"))}
    path = mail_artifacts.artifact_file_path(mail_store.data_dir(), meta["agent"], meta["id"], meta["filename"])
    raw = mail_artifacts._read_checked(path)
    if raw is None:
        return {**meta, "missing": True, "content_b64": None, "text": None}
    text: Optional[str] = None
    if mail_artifacts.is_text_artifact(meta["filename"], meta["mime"]):
        try:
            text = _mask(raw.decode("utf-8"))
        except UnicodeDecodeError:
            text = None
    return {
        **meta,
        "missing": False,
        "content_b64": base64.b64encode(raw).decode("ascii"),
        "text": text,
    }


def cli_artifacts(agent: Optional[str], json_out: bool) -> int:
    try:
        data = list_artifacts(agent=agent)
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    items = data["artifacts"]
    if not items:
        print("no resident artifacts")
        return 0
    for item in items:
        pin = "*" if item["pinned"] else " "
        print(f"{pin} {item['id']}  {item['created']}  {item['agent']}  {item['filename']} ({item['size_bytes']} B)  {item['title']}")
    return 0


def cli_artifact_get(artifact_id: str, json_out: bool, out_path: Optional[str] = None) -> int:
    try:
        data = artifact_body(artifact_id)
    except ValueError:
        print(json.dumps({"error": "invalid artifact id"}, ensure_ascii=False))
        return 2
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if data is None:
        print(json.dumps({"error": "artifact not found"}, ensure_ascii=False))
        return 3
    if out_path and data.get("content_b64") is not None:
        raw = base64.b64decode(data["content_b64"].encode("ascii"))
        Path(out_path).write_bytes(raw)
        if not json_out:
            print(f"wrote {len(raw)} bytes to {out_path}")
            return 0
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
    elif data.get("text") is not None:
        print(data["text"])
    else:
        print(f"{data['id']} {data['filename']} ({data['size_bytes']} B, binary; pass --resident-artifact-out PATH)")
    return 0


def mail_body(mail_id: str) -> Optional[Dict[str, Any]]:
    """The one mail with its body, masked; ValueError on a malformed id, None when there is no
    store or no such mail. `bytes` counts the unmasked body as stored."""
    if not isinstance(mail_id, str) or not MAIL_ID_RE.fullmatch(mail_id):
        raise ValueError(f"bad mail id {mail_id!r}")
    conn = open_readonly(mail_store.default_store_path())
    if conn is None:
        return None
    try:
        rows, _, _ = _select(conn, "mail", (_col("id"), _col("body")), "WHERE id=?", (mail_id,))
    finally:
        conn.close()
    if not rows:
        return None
    body = rows[0]["body"] or ""
    return {"id": mail_id, "body": _mask(body), "bytes": len(body.encode("utf-8")), "masked": True}


def cli_monitor(json_out: bool) -> int:
    """`--resident-monitor`: the snapshot as JSON, or one line per agent. Exit 0 (also with no
    store); 1 when the store cannot be read."""
    try:
        data = snapshot()
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    for agent in data["agents"]:
        turn = agent["turn"]["kind"] if agent["turn"] else "-"
        print(f"{agent['address']:<24} {(agent['node'] or '-'):<10} {(agent['provider'] or '-'):<8} "
              f"{(agent['driver'] or '-'):<9} tools {(agent['tools'] or 'closed'):<7} "
              f"{(agent['state'] or 'no-store'):<9} epoch "
              f"{agent['epoch'] if agent['epoch'] is not None else '-'} turn {turn} "
              f"mail {sum(agent['mail_open'].values())} tasks {agent['tasks_held']}")
    for problem in data["problems"]:
        print(f"problem: {problem}")
    if not data["agents"]:
        print("no resident agents")
    return 0


def cli_mail_body(mail_id: str, json_out: bool) -> int:
    """`--resident-mail-body`: one masked mail body. Exit 0 ok; 1 store error; 2 bad id;
    3 not found."""
    try:
        data = mail_body(mail_id)
    except ValueError:
        print(json.dumps({"error": "invalid mail id"}, ensure_ascii=False))
        return 2
    except sqlite3.Error as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    if data is None:
        print(json.dumps({"error": "mail not found"}, ensure_ascii=False))
        return 3
    if json_out:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(data["body"])
    return 0


# --- schema-tolerant SELECT and small helpers -----------------------------------------


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(f"PRAGMA table_info({table})").fetchall())


def _problem(problems: List[str], line: str) -> None:
    if line not in problems:  # per-agent queries would otherwise repeat one missing table
        problems.append(line)


def _select(conn: sqlite3.Connection, table: str,
            wanted: Sequence[Tuple[str, str, Optional[str]]], tail_sql: str = "",
            params: Sequence[Any] = ()) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    """One SELECT over `table` built from the wanted fields, tolerating missing columns.

    Each wanted item is (name, expr, requires): the output key, its SQL expression and the column
    that must exist for the expression to hold (None: no requirement). A missing column comes back
    as None and a missing table as no rows, so a store of another schema version still shows.
    Returns (rows, missing_columns, table_present)."""
    if not _IDENT_RE.fullmatch(table):
        raise ValueError(f"bad table name {table!r}")
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not columns:
        return [], [name for name, _, _ in wanted], False
    picks: List[str] = []
    missing: List[str] = []
    for name, expr, requires in wanted:
        if requires is None or requires in columns:
            picks.append(f"{expr} AS {name}")
        else:
            picks.append(f"NULL AS {name}")
            missing.append(name)
    query = f"SELECT {', '.join(picks)} FROM {table} {tail_sql}".strip()
    rows = [dict(row) for row in conn.execute(query, params)]
    return rows, missing, True


def _limits(overrides: Optional[Dict[str, int]]) -> Dict[str, int]:
    lim = dict(DEFAULT_LIMITS)
    for key, value in (overrides or {}).items():  # unknown keys are ignored
        if key in lim:
            try:
                lim[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    return lim


def _path_kind() -> str:
    """How default_store_path() resolved: an overridden GUARDIAN_DATA_DIR or the default place.
    The snapshot never carries the full path."""
    return "override" if os.environ.get("GUARDIAN_DATA_DIR") else "default"


def _mask(text: str) -> str:
    from audit_run import mask_secrets  # late import: audit_run pulls in the debate stack
    return mask_secrets(text)


def _mask_detail(value: Any) -> Any:
    """mask_secrets over every string of a parsed event detail (ids and counts today, but the
    writers are many, so the monitor does not trust them to keep bodies out)."""
    if isinstance(value, str):
        return _mask(value)
    if isinstance(value, list):
        return [_mask_detail(item) for item in value]
    if isinstance(value, dict):
        return {key: _mask_detail(item) for key, item in value.items()}
    return value


# --- store block and the declaration file ----------------------------------------------


def _store_block(conn: sqlite3.Connection, problems: List[str]) -> Dict[str, Any]:
    """exists, the schema stamp from meta (never PRAGMA user_version) and the path kind."""
    block: Dict[str, Any] = {"exists": True, "schema": None, "path_kind": _path_kind()}
    rows, _, present = _select(conn, "meta", (_col("value"),), "WHERE key=?", ("schema_version",))
    if not present:
        _problem(problems, "table meta is missing: no schema stamp")
        return block
    if not rows:
        _problem(problems, "meta has no schema_version stamp")
        return block
    stamp = rows[0]["value"]
    block["schema"] = int(stamp) if isinstance(stamp, str) and stamp.isdigit() else stamp
    if stamp != str(mail_store.SCHEMA_VERSION):
        _problem(problems, f"store schema version {stamp} is not {mail_store.SCHEMA_VERSION}")
    return block


def _declared_agents() -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """The declaration file read as plain JSON (never load_declarations, which writes an event):
    {address: {name, node, provider, driver, tools}} and the problems of unreadable parts."""
    path = resident_registry.declaration_path()
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"{path.name}: cannot read ({exc})"]
    if not isinstance(data, dict) or data.get("format") != resident_registry.DECLARATION_FORMAT \
            or not isinstance(data.get("agents"), list):
        return {}, [f"{path.name}: expected format {resident_registry.DECLARATION_FORMAT} with "
                    f"an agents list"]
    agents: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for index, raw in enumerate(data["agents"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) \
                or not isinstance(raw.get("node"), str):
            problems.append(f"{path.name}: entry {index + 1} is not a usable declaration")
            continue
        address = f"{raw['name']}@{raw['node']}"
        agents[address] = {key: raw.get(key)
                           for key in ("name", "node", "provider", "driver", "tools")}
    return agents, problems


# --- the sections ----------------------------------------------------------------------


def _agents_section(conn: sqlite3.Connection, declared: Dict[str, Dict[str, Any]],
                    problems: List[str]) -> List[Dict[str, Any]]:
    """The union of declared and stored agents, by address: declaration fields when declared,
    store fields when stored, live state for both."""
    rows, _, present = _select(conn, "agents", _AGENTS_WANTED, "ORDER BY address")
    if not present:
        _problem(problems, "table agents is missing")
    stored = {row["address"]: row for row in rows if row.get("address")}
    addresses = sorted(set(declared) | set(stored))
    open_mail = _open_mail_counts(conn, addresses)
    held = _tasks_held_counts(conn, addresses)
    pending_q = _queries_pending_counts(conn, addresses)
    ready_map = _session_ready_map(conn, addresses)
    empty_mail = {state: 0 for state in OPEN_MAIL_STATES}
    return [_agent_item(address, declared.get(address), stored.get(address),
                        _incarnation(conn, address, problems), _open_turn(conn, address, problems),
                        open_mail.get(address) or empty_mail, held.get(address, 0),
                        ready_map.get(address, False), pending_q.get(address, 0))
            for address in addresses]


def _agent_item(address: str, declared: Optional[Dict[str, Any]],
                stored: Optional[Dict[str, Any]], incarnation: Optional[Dict[str, Any]],
                turn: Optional[Dict[str, Any]], mail_open: Dict[str, int],
                tasks_held: int, session_ready: bool = False,
                queries_pending: int = 0) -> Dict[str, Any]:
    """One agent: identity from the declaration when it has one (tools is declaration-only),
    state and counters from the store, None/0 where the side is absent."""
    dec, row = declared or {}, stored or {}
    return {
        "address": address,
        "name": dec.get("name") or row.get("name"),
        "node": dec.get("node") or row.get("node"),
        "provider": dec.get("provider") or row.get("provider"),
        "driver": dec.get("driver") or row.get("driver"),
        "tools": dec.get("tools"),
        "state": row.get("state"),
        "epoch": row.get("epoch"),
        "not_ready_since": row.get("not_ready_since"),
        "bind_failures": row.get("bind_failures") if stored is not None else 0,
        "bind_started": row.get("bind_started"),
        "modified": row.get("modified"),
        "incarnation": incarnation,
        "turn": turn,
        "mail_open": mail_open,
        "tasks_held": tasks_held,
        "session_ready": bool(session_ready),
        "queries_pending": int(queries_pending),
        "declared": declared is not None,
        "in_store": stored is not None,
    }


def _session_ready_map(conn: sqlite3.Connection, addresses: List[str]) -> Dict[str, bool]:
    out = {address: False for address in addresses}
    if not addresses or not _table_exists(conn, "incarnations") or not _table_exists(conn, "agents"):
        return out
    inc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(incarnations)")}
    if "session_ready" not in inc_cols:
        return out
    placeholders = ",".join("?" * len(addresses))
    rows = conn.execute(
        f"SELECT a.address, a.session_id, i.session_ready FROM agents a "
        f"JOIN incarnations i ON i.address=a.address AND i.epoch=a.epoch AND i.ended IS NULL "
        f"WHERE a.address IN ({placeholders})",
        tuple(addresses),
    ).fetchall()
    for row in rows:
        if row["address"] in out:
            out[row["address"]] = bool(row["session_id"]) and bool(row["session_ready"])
    return out


def _queries_pending_counts(conn: sqlite3.Connection, addresses: List[str]) -> Dict[str, int]:
    counts = {address: 0 for address in addresses}
    if not addresses or not _table_exists(conn, "session_queries"):
        return counts
    placeholders = ",".join("?" * len(addresses))
    rows = conn.execute(
        f"SELECT address, COUNT(*) AS n FROM session_queries "
        f"WHERE address IN ({placeholders}) AND status IN ('queued','dispatched','running') "
        f"GROUP BY address",
        tuple(addresses),
    ).fetchall()
    for row in rows:
        if row["address"] in counts:
            counts[row["address"]] = row["n"]
    return counts


def _incarnation(conn: sqlite3.Connection, address: str, problems: List[str]) \
        -> Optional[Dict[str, Any]]:
    """The address's open incarnation (highest epoch without an end), or None."""
    rows, _, present = _select(conn, "incarnations", _INCARNATION_WANTED,
                               "WHERE address=? AND ended IS NULL ORDER BY epoch DESC LIMIT 1",
                               (address,))
    if not present:
        _problem(problems, "table incarnations is missing")
        return None
    return rows[0] if rows else None


def _open_turn(conn: sqlite3.Connection, address: str, problems: List[str]) \
        -> Optional[Dict[str, Any]]:
    """The address's newest turn with no end, or None."""
    rows, _, present = _select(conn, "turns", _OPEN_TURN_WANTED,
                               "WHERE address=? AND ended IS NULL "
                               "ORDER BY started DESC, id DESC LIMIT 1", (address,))
    if not present:
        _problem(problems, "table turns is missing")
        return None
    return rows[0] if rows else None


def _open_mail_counts(conn: sqlite3.Connection, addresses: List[str]) -> Dict[str, Dict[str, int]]:
    """Open mail per recipient; a missing mail table is reported by the mail section."""
    counts = {address: {state: 0 for state in OPEN_MAIL_STATES} for address in addresses}
    if not addresses or not _table_exists(conn, "mail"):
        return counts
    recipients = ",".join("?" * len(addresses))
    states = ",".join("?" * len(OPEN_MAIL_STATES))
    rows = conn.execute(
        f"SELECT recipient, state, COUNT(*) AS n FROM mail "
        f"WHERE recipient IN ({recipients}) AND state IN ({states}) GROUP BY recipient, state",
        (*addresses, *OPEN_MAIL_STATES)).fetchall()
    for row in rows:
        if row["recipient"] in counts and row["state"] in counts[row["recipient"]]:
            counts[row["recipient"]][row["state"]] = row["n"]
    return counts


def _tasks_held_counts(conn: sqlite3.Connection, addresses: List[str]) -> Dict[str, int]:
    """Open tasks per owner (an agent holds what it owns while the status stays open); a missing
    tasks table is reported by the tasks section."""
    held = {address: 0 for address in addresses}
    if not addresses or not _table_exists(conn, "tasks"):
        return held
    owners = ",".join("?" * len(addresses))
    states = ",".join("?" * len(TASK_OPEN_STATES))
    rows = conn.execute(
        f"SELECT owner, COUNT(*) AS n FROM tasks WHERE owner IN ({owners}) "
        f"AND status IN ({states}) GROUP BY owner", (*addresses, *TASK_OPEN_STATES)).fetchall()
    for row in rows:
        if row["owner"] in held:
            held[row["owner"]] = row["n"]
    return held


def _turns_section(conn: sqlite3.Connection, limit: int, problems: List[str]) \
        -> Dict[str, List[Dict[str, Any]]]:
    """The newest turns per address (ended and open alike), newest first, `limit` per address."""
    turns: Dict[str, List[Dict[str, Any]]] = {}
    if not _table_exists(conn, "turns"):
        _problem(problems, "table turns is missing")
        return turns
    has_inputs = _table_exists(conn, "turn_inputs")
    if not has_inputs:
        _problem(problems, "table turn_inputs is missing: turn input counts are unknown")
    wanted = (*_TURNS_WANTED, ("inputs", _INPUTS_EXPR if has_inputs else "NULL", None))
    addresses = [row["address"] for row in
                 conn.execute("SELECT DISTINCT address FROM turns ORDER BY address")]
    for address in addresses:
        rows, _, _ = _select(conn, "turns", wanted,
                             "WHERE address=? ORDER BY started DESC, id DESC LIMIT ?",
                             (address, limit))
        turns[address] = rows
    return turns


def _mail_section(conn: sqlite3.Connection, limit: int, problems: List[str]) \
        -> List[Dict[str, Any]]:
    """The newest mail, metadata and body size only (never the body), newest first."""
    rows, _, present = _select(conn, "mail", _MAIL_WANTED,
                               "ORDER BY created DESC, id DESC LIMIT ?", (limit,))
    if not present:
        _problem(problems, "table mail is missing")
    return rows


def _tasks_section(conn: sqlite3.Connection, closed_limit: int, problems: List[str]) \
        -> List[Dict[str, Any]]:
    """Every open task (oldest first, the board order) plus the newest closed ones."""
    open_states = ",".join("?" * len(TASK_OPEN_STATES))
    open_rows, _, present = _select(conn, "tasks", _TASK_WANTED,
                                    f"WHERE status IN ({open_states}) ORDER BY created, id",
                                    TASK_OPEN_STATES)
    if not present:
        _problem(problems, "table tasks is missing")
        return []
    closed_states = ",".join("?" * len(TASK_CLOSED_STATES))
    closed_rows, _, _ = _select(conn, "tasks", _TASK_WANTED,
                                f"WHERE status IN ({closed_states}) "
                                f"ORDER BY created DESC, id DESC LIMIT ?",
                                (*TASK_CLOSED_STATES, closed_limit))
    return [_task_item(row) for row in open_rows + closed_rows]


def _task_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Sizes and counts only: detail, result and the patch text never enter the snapshot."""
    item = dict(row)
    item["blocked_by"] = _json_list(item.pop("blocked_by_json", None))
    item["calls"] = _call_count(item.pop("calls_json", None))
    return item


def _json_list(text: Optional[str]) -> List[Any]:
    try:
        data = json.loads(text) if text else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _call_count(text: Optional[str]) -> int:
    """Entries in the stored call log ({"records": [...], "dropped": bool}); 0 when empty or bad."""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return 0
    records = data.get("records") if isinstance(data, dict) else None
    return len(records) if isinstance(records, list) else 0


def _events_section(conn: sqlite3.Connection, limit: int, problems: List[str]) \
        -> List[Dict[str, Any]]:
    """The newest events by id, their detail parsed and masked."""
    rows, _, present = _select(conn, "events", _EVENTS_WANTED, "ORDER BY id DESC LIMIT ?",
                               (limit,))
    if not present:
        _problem(problems, "table events is missing")
        return []
    return [_event_item(row) for row in rows]


def _event_item(row: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    raw = item.pop("detail_json", None)
    try:
        detail = json.loads(raw) if raw else {}
    except ValueError:
        detail = {}
    item["detail"] = _mask_detail(detail if isinstance(detail, dict) else {})
    return item

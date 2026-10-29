"""
SQLite Database Manager for Server Guardian Multi-Agent Decision System.
Manages Projects, Discussion Threads, Debate Rounds, and Consensus Decisions.
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

# A thread the user closed: hidden from default dashboard lists, all records kept. A new debate
# on the thread overwrites the status (save_debate_session), which reopens it.
CLOSED_STATUS = "CLOSED"
# final_decision keys stored next to the decision (JSON in decisions.decision_trace): the judge's rationale and the
# constraints and requirement ids (refs) that tie constraints and checks to the decision.
DECISION_TRACE_KEYS = ("rationale", "constraints", "constraint_refs", "check_refs")


def data_dir() -> Path:
    """Runtime data directory on the hub, outside the git checkout (GUARDIAN_DATA_DIR)."""
    path = Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db_path() -> Path:
    """The single decision database. Only the hub writes it, so it is never committed to git."""
    configured = os.environ.get("GUARDIAN_DB_PATH")
    return Path(configured).expanduser() if configured else data_dir() / "guardian_decisions.db"


def decisions_dir() -> Path:
    """Markdown/JSON exports of debate threads, next to the database."""
    path = data_dir() / "decisions"
    path.mkdir(parents=True, exist_ok=True)
    return path


class DecisionDBManager:
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = default_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes database schema for projects, threads, and debate messages."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Projects Table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 2. Threads Table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                consensus_reached INTEGER DEFAULT 0,
                final_round INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(project_id)
            );
            """)

            # 3. Debate Messages (Rounds) Table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS debate_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                agent_name TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                provider TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
            );
            """)

            # 4. Decisions Table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL UNIQUE,
                judgment_summary TEXT NOT NULL,
                proposal_content TEXT NOT NULL,
                critique_content TEXT NOT NULL,
                mcp_actions TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
            );
            """)

            # 5. Agent Sessions Table: persisted provider CLI conversation per thread and role
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                provider TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (thread_id, role)
            );
            """)

            # 6. Decision Actions: tool actions proposed by the judge, executed only after approval
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS decision_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                tool TEXT NOT NULL,
                arguments TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'PROPOSED',
                result TEXT,
                decided_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 6b. Plans: ordered, multi-host sets of catalog actions decided together (plan_ops.py,
            # docs/design/fleet-provisioning.md, chapter 7.2). The steps are decision_actions rows with a plan_id.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS decision_plans (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                project_id TEXT,
                goal TEXT,
                stop_plan INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_decision_plans_thread ON decision_plans(thread_id);")
            # The critics' vote on the stored plan (plan_hash names what they voted on; votes and gate are JSON,
            # docs/design/batch-execution.md chapters 6-7). NULL for a plan nobody voted on.
            plan_columns = {row[1] for row in cursor.execute("PRAGMA table_info(decision_plans);")}
            for column in ("plan_hash", "votes", "gate"):
                if column not in plan_columns:
                    cursor.execute(f"ALTER TABLE decision_plans ADD COLUMN {column} TEXT;")

            # 7. jev reviews: independent jev-mcp judgment of an approved debate (the gate for actions)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS jev_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                jev_decision_id INTEGER,
                gate TEXT NOT NULL,
                reasons TEXT,
                evaluation TEXT,
                request TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 8. Node groups: user-defined, many-to-many server groups (group_manager.py,
            # docs/design/node-groups.md). Members are computed from include/rules/exclude
            # against servers.json on every read, so only the definition is stored.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS node_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                tone TEXT,
                include_ids TEXT NOT NULL DEFAULT '[]',
                rules TEXT NOT NULL DEFAULT '[]',
                exclude_ids TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 9. Small key/value store for one-time migrations (e.g. node_groups_seeded).
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS guardian_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 10. Publish attempts: judge changes committed to a branch and pushed (publish_ops.py,
            # docs/design/publish-thread.md). One row per attempt; RUNNING until it ends PUSHED or FAILED.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS thread_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                project_id TEXT,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                remote TEXT,
                status TEXT NOT NULL,
                error TEXT,
                actor TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_thread_publications_thread ON thread_publications(thread_id);")

            # Lineup that ran the thread (team_manager.Lineup.snapshot), added after the threads table shipped.
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(threads);")}
            if "team_snapshot" not in columns:
                cursor.execute("ALTER TABLE threads ADD COLUMN team_snapshot TEXT;")
            # Code the agents read (code_context.ContextPack.snapshot).
            if "context_snapshot" not in columns:
                cursor.execute("ALTER TABLE threads ADD COLUMN context_snapshot TEXT;")
            # When the thread was closed (status CLOSED); cleared when a new debate continues it.
            if "closed_at" not in columns:
                cursor.execute("ALTER TABLE threads ADD COLUMN closed_at TIMESTAMP;")
            # Commit a judge action was proposed against; write tools refuse to run once the workspace moves.
            # The judge's final approach and the implementation checks to confirm after its actions run.
            decision_columns = {row[1] for row in cursor.execute("PRAGMA table_info(decisions);")}
            if "decision_summary" not in decision_columns:
                cursor.execute("ALTER TABLE decisions ADD COLUMN decision_summary TEXT;")
            if "implementation_checks" not in decision_columns:
                cursor.execute("ALTER TABLE decisions ADD COLUMN implementation_checks TEXT;")
            # Rationale, constraints and the requirement ids (refs) that tie constraints and checks to the decision.
            if "decision_trace" not in decision_columns:
                cursor.execute("ALTER TABLE decisions ADD COLUMN decision_trace TEXT;")
            # Host a session lives on: '' is the hub, anything else the id of a fleet host running the model worker
            # (remote_agents.py). A session id is only valid on the host that created it.
            session_columns = {row[1] for row in cursor.execute("PRAGMA table_info(agent_sessions);")}
            if "host" not in session_columns:
                cursor.execute("ALTER TABLE agent_sessions ADD COLUMN host TEXT NOT NULL DEFAULT '';")
            action_columns = {row[1] for row in cursor.execute("PRAGMA table_info(decision_actions);")}
            if "base_sha" not in action_columns:
                cursor.execute("ALTER TABLE decision_actions ADD COLUMN base_sha TEXT;")
            # Project whose workspace the action runs in; older rows fall back to their thread's project.
            if "project_id" not in action_columns:
                cursor.execute("ALTER TABLE decision_actions ADD COLUMN project_id TEXT;")
            # The measurement of an ops cleanup action when it was proposed (ops_actions.measure, JSON).
            if "preview" not in action_columns:
                cursor.execute("ALTER TABLE decision_actions ADD COLUMN preview TEXT;")
            # Plan steps: the plan they belong to, their id inside it, the step ids they wait for (JSON list) and
            # what happens to the rest when they fail ('stop' or 'continue'); all NULL for a single action.
            for column in ("plan_id", "step", "requires", "on_failure"):
                if column not in action_columns:
                    cursor.execute(f"ALTER TABLE decision_actions ADD COLUMN {column} TEXT;")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_decision_actions_plan ON decision_actions(plan_id);")

            # git_commit_push was retired: a stored PROPOSED/HELD row naming it could never run but would keep
            # blocking its thread's publish, so settle it as INVALID (idempotent; only touches undecided rows).
            cursor.execute(
                "UPDATE decision_actions SET status = 'INVALID', result = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE tool = 'jev_mcp:git_commit_push' AND status IN ('PROPOSED', 'HELD');",
                (json.dumps({"error": "git_commit_push was retired; use publish"}),))

            # Insert default project if not exists
            cursor.execute("""
            INSERT OR IGNORE INTO projects (project_id, name, description)
            VALUES ('server-guardian', 'Server Guardian Main Cluster', 'Fleet management and autonomous multi-agent decision hub');
            """)

            conn.commit()

    def save_debate_session(
        self,
        project_id: str,
        thread_id: str,
        task_prompt: str,
        transcript: List[Dict[str, Any]],
        decision: Optional[Dict[str, Any]],
        team: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Saves or updates a debate session in SQLite database.

        team is the lineup snapshot, context the code context snapshot (both kept when None).
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Ensure project exists
            cursor.execute("""
            INSERT OR IGNORE INTO projects (project_id, name, description)
            VALUES (?, ?, ?);
            """, (project_id, project_id.replace("-", " ").title(), f"Project {project_id} decisions"))

            is_approved = decision.get("approved", False) if decision else False
            if decision and decision.get("unverified"):
                status_str = "UNVERIFIED"
            elif decision and decision.get("jev_gate") in ("DISPUTED", "PENDING"):
                status_str = "DISPUTED" if decision["jev_gate"] == "DISPUTED" else "PENDING_JEV"
            else:
                status_str = "APPROVED" if is_approved else "REVISION_REQUIRED"
            final_round = decision.get("final_round", 0) if decision else 0

            # 2. Upsert Thread
            cursor.execute("""
            INSERT INTO threads (thread_id, project_id, task_prompt, status, consensus_reached, final_round,
                                 team_snapshot, context_snapshot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(thread_id) DO UPDATE SET
                status = excluded.status,
                consensus_reached = excluded.consensus_reached,
                final_round = excluded.final_round,
                team_snapshot = COALESCE(excluded.team_snapshot, threads.team_snapshot),
                context_snapshot = COALESCE(excluded.context_snapshot, threads.context_snapshot),
                closed_at = NULL,
                updated_at = CURRENT_TIMESTAMP;
            """, (thread_id, project_id, task_prompt, status_str, 1 if is_approved else 0, final_round,
                  json.dumps(team, ensure_ascii=False) if team else None,
                  json.dumps(context, ensure_ascii=False) if context else None))

            # 3. Clear existing debate messages for thread to avoid duplicates during re-runs
            cursor.execute("DELETE FROM debate_messages WHERE thread_id = ?;", (thread_id,))

            # 4. Insert Messages
            for step in transcript:
                cursor.execute("""
                INSERT INTO debate_messages (thread_id, round_num, agent_name, role, content, provider)
                VALUES (?, ?, ?, ?, ?, ?);
                """, (
                    thread_id,
                    step.get("round", 1),
                    step.get("agent", "Agent"),
                    step.get("role", "Participant"),
                    step.get("content", ""),
                    step.get("provider", "unknown")
                ))

            # 5. Insert Decision if available
            if decision:
                trace = {k: decision[k] for k in DECISION_TRACE_KEYS if decision.get(k)}
                cursor.execute("DELETE FROM decisions WHERE thread_id = ?;", (thread_id,))
                cursor.execute("""
                INSERT INTO decisions (thread_id, judgment_summary, proposal_content, critique_content,
                                       decision_summary, implementation_checks, decision_trace)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (
                    thread_id,
                    decision.get("judgment", ""),
                    decision.get("proposal", ""),
                    decision.get("critique", ""),
                    decision.get("decision") or None,
                    json.dumps(decision["checks"], ensure_ascii=False) if decision.get("checks") else None,
                    json.dumps(trace, ensure_ascii=False) if trace else None,
                ))

            conn.commit()

    def get_thread_history(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a thread and its messages from SQLite."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM threads WHERE thread_id = ?;", (thread_id,))
            thread_row = cursor.fetchone()
            if not thread_row:
                return None

            cursor.execute(
                "SELECT * FROM debate_messages WHERE thread_id = ? ORDER BY id ASC;",
                (thread_id,)
            )
            msg_rows = cursor.fetchall()

            transcript = []
            for row in msg_rows:
                transcript.append({
                    "round": row["round_num"],
                    "agent": row["agent_name"],
                    "role": row["role"],
                    "content": row["content"],
                    "provider": row["provider"],
                    "created_at": row["created_at"]
                })

            cursor.execute("SELECT * FROM decisions WHERE thread_id = ?;", (thread_id,))
            decision_row = cursor.fetchone()
            decision = None
            if decision_row:
                decision = {
                    "approved": thread_row["consensus_reached"] == 1,
                    "final_round": thread_row["final_round"],
                    "judgment": decision_row["judgment_summary"],
                    "proposal": decision_row["proposal_content"],
                    "critique": decision_row["critique_content"],
                    "decision": decision_row["decision_summary"] or "",
                    "checks": json.loads(decision_row["implementation_checks"]) if decision_row["implementation_checks"] else [],
                }
                if decision_row["decision_trace"]:
                    decision.update(json.loads(decision_row["decision_trace"]))

            return {
                "project_id": thread_row["project_id"],
                "thread_id": thread_row["thread_id"],
                "task": thread_row["task_prompt"],
                "status": thread_row["status"],
                "closed_at": thread_row["closed_at"],
                "team": json.loads(thread_row["team_snapshot"]) if thread_row["team_snapshot"] else None,
                "context": json.loads(thread_row["context_snapshot"]) if thread_row["context_snapshot"] else None,
                "decision": decision,
                "transcript": transcript,
            }

    def list_project_threads(self, project_id: str, include_closed: bool = True) -> List[Dict[str, Any]]:
        """Lists the discussion threads under a project; include_closed=False hides CLOSED ones."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT thread_id, task_prompt, status, consensus_reached, final_round, updated_at, closed_at
            FROM threads
            WHERE project_id = ? AND (? OR status != ?)
            ORDER BY updated_at DESC;
            """, (project_id, 1 if include_closed else 0, CLOSED_STATUS))

            rows = cursor.fetchall()
            threads = []
            for r in rows:
                threads.append({
                    "thread_id": r["thread_id"],
                    "task": r["task_prompt"],
                    "status": r["status"],
                    "consensus_reached": bool(r["consensus_reached"]),
                    "final_round": r["final_round"],
                    "updated_at": r["updated_at"],
                    "closed_at": r["closed_at"],
                })
            return threads

    def get_agent_session(self, thread_id: str, role: str) -> Optional[Dict[str, Any]]:
        """Returns the persisted CLI session for a thread role, if any."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE thread_id = ? AND role = ?;", (thread_id, role)
            ).fetchone()
            return dict(row) if row else None

    def save_agent_session(self, thread_id: str, role: str, provider: str, session_id: str, host: str = "") -> None:
        """Creates or replaces the CLI session for a thread role and marks it as used now.

        host is where the session lives ('' = the hub); a session that moved to another host replaces the row."""
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO agent_sessions (thread_id, role, provider, session_id, host)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, role) DO UPDATE SET
                provider = excluded.provider,
                session_id = excluded.session_id,
                host = excluded.host,
                created_at = CASE
                    WHEN agent_sessions.session_id = excluded.session_id THEN agent_sessions.created_at
                    ELSE CURRENT_TIMESTAMP
                END,
                last_used_at = CURRENT_TIMESTAMP;
            """, (thread_id, role, provider, session_id, host or ""))
            conn.commit()

    def list_agent_sessions(self, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists persisted CLI sessions, most recently used first."""
        query = "SELECT * FROM agent_sessions"
        params: tuple = ()
        if thread_id:
            query += " WHERE thread_id = ?"
            params = (thread_id,)
        with self._get_connection() as conn:
            rows = conn.execute(query + " ORDER BY last_used_at DESC, thread_id, role;", params).fetchall()
            return [dict(r) for r in rows]

    def prune_agent_sessions(self, older_than_days: int) -> int:
        """Forgets sessions unused for the given number of days. Returns the number removed.

        Only the mapping is removed; the provider CLI keeps its own conversation files.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_sessions WHERE last_used_at < datetime('now', ?);",
                (f"-{int(older_than_days)} days",),
            )
            conn.commit()
            return cursor.rowcount

    # --- Decision actions -------------------------------------------------------------

    # HELD: waiting for the jev gate; becomes PROPOSED only when jev passes the decision.
    # STALE: an ops cleanup action whose target grew too much since it was proposed; it never runs (debate again).
    # SKIPPED: a plan step that was never started because an earlier step failed or one it requires did not succeed.
    ACTION_STATUSES = ("HELD", "PROPOSED", "INVALID", "REJECTED", "RUNNING", "EXECUTED", "FAILED", "STALE", "SKIPPED")

    def add_action(
        self, thread_id: str, round_num: int, tool: str, arguments: Dict[str, Any], reason: str, status: str = "PROPOSED",
        base_sha: Optional[str] = None, project_id: Optional[str] = None,
        plan_id: Optional[str] = None, step: Optional[str] = None, requires: Optional[List[str]] = None,
        on_failure: Optional[str] = None,
    ) -> int:
        """Stores a judge-proposed action and returns its id. base_sha is the commit the judge read.

        plan_id, step, requires and on_failure are set for a step of a plan (plan_ops.record_plan)."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO decision_actions (thread_id, round_num, tool, arguments, reason, status, base_sha, project_id, "
                "plan_id, step, requires, on_failure) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (thread_id, round_num, tool, json.dumps(arguments, ensure_ascii=False), reason, status, base_sha,
                 project_id, plan_id, step, json.dumps(requires) if requires is not None else None, on_failure),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def add_plan(self, plan_id: str, thread_id: str, round_num: int, project_id: Optional[str], goal: str,
                 stop_plan: bool = False) -> None:
        with self._get_connection() as conn:
            conn.execute("INSERT INTO decision_plans (id, thread_id, round_num, project_id, goal, stop_plan) "
                         "VALUES (?, ?, ?, ?, ?, ?);",
                         (plan_id, thread_id, round_num, project_id, goal, 1 if stop_plan else 0))
            conn.commit()

    def set_plan_gate(self, plan_id: str, plan_hash: str, votes: List[Dict[str, Any]], gate: Dict[str, Any]) -> bool:
        """Stores the votes on a plan and what the batch gate made of them. False when the plan does not exist or the
        engine already started it (its gate then holds the start record the daily cap counts)."""
        with self._get_connection() as conn:
            cursor = conn.execute("UPDATE decision_plans SET plan_hash = ?, votes = ?, gate = ? WHERE id = ? "
                                  "AND (gate IS NULL OR json_extract(gate, '$.auto_run') IS NULL);",
                                  (plan_hash, json.dumps(votes, ensure_ascii=False),
                                   json.dumps(gate, ensure_ascii=False), plan_id))
            conn.commit()
            return cursor.rowcount == 1

    def claim_auto_plan(self, plan_id: str, project_id: str, cap: int, actor: str, day: str, at: str,
                        plan_hash: Optional[str] = None) -> Optional[str]:
        """Marks a released plan as started by the engine, unless it already was or the project's cap of plans for
        `day` (YYYY-MM-DD, UTC) is used up. Counting and marking happen in one write transaction, so two debates
        finishing at once cannot both take the last slot. With plan_hash, the stored gate must still say auto for that
        plan. Returns None on success, else why not."""
        conn = self._get_connection()
        try:
            conn.isolation_level = None  # explicit transaction
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute("SELECT gate, plan_hash FROM decision_plans WHERE id = ?;", (plan_id,)).fetchone()
                gate = json.loads(row["gate"]) if row and row["gate"] else None
                if not isinstance(gate, dict):
                    conn.execute("ROLLBACK;")
                    return "the plan has no stored gate record"
                if plan_hash is not None and not (gate.get("auto") is True and gate.get("plan_hash") == plan_hash
                                                  and row["plan_hash"] == plan_hash):
                    conn.execute("ROLLBACK;")
                    return "the stored gate no longer releases this plan"
                if gate.get("auto_run"):
                    conn.execute("ROLLBACK;")
                    return "the plan was already started"
                used = conn.execute(
                    "SELECT COUNT(*) FROM decision_plans WHERE project_id = ? "
                    "AND json_extract(gate, '$.auto_run.started_at') LIKE ?;", (project_id, day + "%")).fetchone()[0]
                if used >= cap:
                    conn.execute("ROLLBACK;")
                    return f"{used} plans were started today, at most {cap} per day"
                gate["auto_run"] = {"actor": actor, "started_at": at}
                conn.execute("UPDATE decision_plans SET gate = ? WHERE id = ?;",
                             (json.dumps(gate, ensure_ascii=False), plan_id))
                conn.execute("COMMIT;")
                return None
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

    def finish_auto_plan(self, plan_id: str, outcome: Dict[str, Any]) -> bool:
        """Adds what became of an auto-started plan to its gate record (merged into gate["auto_run"]). The merge is
        one UPDATE, so a concurrent writer of the gate cannot lose the start record the daily cap counts."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE decision_plans SET gate = json_set(gate, '$.auto_run', "
                "json_patch(json_extract(gate, '$.auto_run'), json(?))) "
                "WHERE id = ? AND json_extract(gate, '$.auto_run') IS NOT NULL;",
                (json.dumps(outcome, ensure_ascii=False), plan_id))
            conn.commit()
            return cur.rowcount > 0

    def note_release_refusal(self, plan_id: str, reasons: List[str], at: str) -> bool:
        """Keeps why the engine did not run a plan the gate had released (gate["release_refused"]), so the dashboard can
        tell a person what holds it. Only a plan that has a gate and was never started takes it."""
        record = {"reasons": [str(r)[:300] for r in reasons[:20]], "at": at}
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE decision_plans SET gate = json_set(gate, '$.release_refused', json(?)) "
                "WHERE id = ? AND gate IS NOT NULL AND json_extract(gate, '$.auto_run') IS NULL;",
                (json.dumps(record, ensure_ascii=False), plan_id))
            conn.commit()
            return cur.rowcount > 0

    def count_auto_plans(self, project_id: str, day: str) -> int:
        """Plans of the project the engine started on `day` (YYYY-MM-DD, UTC), whatever their outcome."""
        with self._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM decision_plans WHERE project_id = ? "
                                "AND json_extract(gate, '$.auto_run.started_at') LIKE ?;",
                                (project_id, day + "%")).fetchone()[0]

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM decision_plans WHERE id = ?;", (plan_id,)).fetchone()
        return self._plan_row(row) if row else None

    def list_plans(self, thread_id: Optional[str] = None, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query, params = "SELECT * FROM decision_plans WHERE 1=1", []
        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        with self._get_connection() as conn:
            rows = conn.execute(query + " ORDER BY created_at, id;", params).fetchall()
        return [self._plan_row(r) for r in rows]

    def plan_steps(self, plan_id: str) -> List[Dict[str, Any]]:
        """The steps of a plan in plan order (the order they were stored in)."""
        with self._get_connection() as conn:
            rows = conn.execute(self.ACTION_SELECT + " WHERE a.plan_id = ? ORDER BY a.id;", (plan_id,)).fetchall()
        return [self._action_row(r) for r in rows]

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["stop_plan"] = bool(item.get("stop_plan"))
        for column in ("votes", "gate"):
            if column in item:
                item[column] = json.loads(item[column]) if item[column] else None
        return item

    def get_action(self, action_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(self.ACTION_SELECT + " WHERE a.id = ?;", (action_id,)).fetchone()
        return self._action_row(row) if row else None

    # Actions carry their project: their own column, else their thread's, else the default project.
    ACTION_SELECT = (
        "SELECT a.*, COALESCE(a.project_id, t.project_id, 'server-guardian') AS effective_project "
        "FROM decision_actions a LEFT JOIN threads t ON t.thread_id = a.thread_id"
    )

    def list_actions(
        self, thread_id: Optional[str] = None, status: Optional[str] = None, project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query, params = self.ACTION_SELECT + " WHERE 1=1", []
        if thread_id:
            query += " AND a.thread_id = ?"
            params.append(thread_id)
        if status:
            query += " AND a.status = ?"
            params.append(status)
        if project_id:
            query += " AND COALESCE(a.project_id, t.project_id, 'server-guardian') = ?"
            params.append(project_id)
        with self._get_connection() as conn:
            rows = conn.execute(query + " ORDER BY a.id;", params).fetchall()
        return [self._action_row(r) for r in rows]

    def update_action(
        self, action_id: int, status: str, result: Optional[Dict[str, Any]] = None, decided_by: Optional[str] = None,
        expected_status: Optional[str] = None,
    ) -> bool:
        """Sets the status (and result). With expected_status, only transitions from that status (returns False otherwise)."""
        if status not in self.ACTION_STATUSES:
            raise ValueError(f"unknown action status {status}")
        query = "UPDATE decision_actions SET status = ?, result = COALESCE(?, result), decided_by = COALESCE(?, decided_by), updated_at = CURRENT_TIMESTAMP WHERE id = ?"
        params: List[Any] = [status, json.dumps(result, ensure_ascii=False) if result is not None else None, decided_by, action_id]
        if expected_status:
            query += " AND status = ?"
            params.append(expected_status)
        with self._get_connection() as conn:
            cursor = conn.execute(query + ";", params)
            conn.commit()
            return cursor.rowcount == 1

    def set_action_preview(self, action_id: int, preview: Dict[str, Any]) -> None:
        """Stores the proposal-time measurement of an ops cleanup action."""
        with self._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET preview = ? WHERE id = ?;",
                         (json.dumps(preview, ensure_ascii=False), action_id))
            conn.commit()

    def set_action_result(self, action_id: int, result: Dict[str, Any]) -> None:
        """Replaces the stored result of an action without touching its status."""
        with self._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET result = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                         (json.dumps(result, ensure_ascii=False), action_id))
            conn.commit()

    def thread_project(self, thread_id: str) -> Optional[str]:
        """The project a thread was debated in, or None for an unknown thread."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT project_id FROM threads WHERE thread_id = ?;", (thread_id,)).fetchone()
        return row[0] if row else None

    def decision_text(self, thread_id: str) -> str:
        """The thread's decision in words (the judge's final approach, else its judgment); '' when none."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT decision_summary, judgment_summary FROM decisions WHERE thread_id = ?;",
                               (thread_id,)).fetchone()
        return ((row["decision_summary"] or row["judgment_summary"] or "") if row else "").strip()

    # --- publications ---------------------------------------------------------------------

    PUBLICATION_STATUSES = ("RUNNING", "PUSHED", "FAILED")

    def add_publication(self, thread_id: str, project_id: Optional[str], branch: str, commit_sha: Optional[str],
                        remote: Optional[str], status: str, actor: Optional[str], error: Optional[str] = None) -> int:
        """Records a publish attempt and returns its id."""
        if status not in self.PUBLICATION_STATUSES:
            raise ValueError(f"unknown publication status {status}")
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO thread_publications (thread_id, project_id, branch, commit_sha, remote, status, error, actor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (thread_id, project_id, branch, commit_sha, remote, status, error, actor))
            conn.commit()
            return int(cursor.lastrowid)

    def update_publication(self, publication_id: int, status: Optional[str] = None, commit_sha: Optional[str] = None,
                           error: Optional[str] = None) -> None:
        """Sets the outcome of an attempt; None leaves a field as it was."""
        if status is not None and status not in self.PUBLICATION_STATUSES:
            raise ValueError(f"unknown publication status {status}")
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE thread_publications SET status = COALESCE(?, status), commit_sha = COALESCE(?, commit_sha), "
                "error = COALESCE(?, error) WHERE id = ?;", (status, commit_sha, error, publication_id))
            conn.commit()

    def list_publications(self, thread_id: str) -> List[Dict[str, Any]]:
        """A thread's publish attempts, oldest first."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM thread_publications WHERE thread_id = ? ORDER BY id;",
                                (thread_id,)).fetchall()
        return [dict(r) for r in rows]

    def published_thread_ids(self) -> List[str]:
        """Ids of every thread with a PUSHED publication (published threads are final)."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT DISTINCT thread_id FROM thread_publications WHERE status = 'PUSHED' "
                                "ORDER BY thread_id;").fetchall()
        return [r["thread_id"] for r in rows]

    @staticmethod
    def _action_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if "effective_project" in item:
            item["project_id"] = item.pop("effective_project")
        item["arguments"] = json.loads(item["arguments"]) if item.get("arguments") else {}
        item["result"] = json.loads(item["result"]) if item.get("result") else None
        if "preview" in item:
            item["preview"] = json.loads(item["preview"]) if item.get("preview") else None
        if "requires" in item:
            item["requires"] = json.loads(item["requires"]) if item.get("requires") else []
        return item

    # --- jev reviews --------------------------------------------------------------------

    def save_jev_review(self, thread_id: str, round_num: int, review: Dict[str, Any], request: Dict[str, Any],
                        review_id: Optional[int] = None) -> int:
        """Inserts a jev review, or updates review_id in place. Returns the review id."""
        values = (
            review.get("jev_decision_id"),
            review["gate"],
            json.dumps(review.get("reasons", []), ensure_ascii=False),
            json.dumps(review.get("evaluation"), ensure_ascii=False),
        )
        with self._get_connection() as conn:
            if review_id:
                conn.execute(
                    "UPDATE jev_reviews SET jev_decision_id = COALESCE(?, jev_decision_id), gate = ?, reasons = ?, "
                    "evaluation = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                    (*values, review_id),
                )
                conn.commit()
                return review_id
            cursor = conn.execute(
                "INSERT INTO jev_reviews (thread_id, round_num, jev_decision_id, gate, reasons, evaluation, request) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (thread_id, round_num, *values, json.dumps(request, ensure_ascii=False)),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def latest_jev_review(self, thread_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM jev_reviews WHERE thread_id = ? ORDER BY id DESC LIMIT 1;", (thread_id,)
            ).fetchone()
        return self._review_row(row) if row else None

    def list_jev_reviews(self, gate: Optional[str] = None) -> List[Dict[str, Any]]:
        query, params = "SELECT * FROM jev_reviews", []
        if gate:
            query += " WHERE gate = ?"
            params.append(gate)
        with self._get_connection() as conn:
            rows = conn.execute(query + " ORDER BY id;", params).fetchall()
        return [self._review_row(r) for r in rows]

    def set_thread_status(self, thread_id: str, status: str) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE threads SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE thread_id = ?;",
                         (status, thread_id))
            conn.commit()

    def get_thread_status(self, thread_id: str) -> Optional[str]:
        """The thread's status, or None when the thread does not exist."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT status FROM threads WHERE thread_id = ?;", (thread_id,)).fetchone()
        return row["status"] if row else None

    def thread_final_round(self, thread_id: str) -> Optional[int]:
        """The round the thread's latest debate ended in, or None when the thread does not exist."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT final_round FROM threads WHERE thread_id = ?;", (thread_id,)).fetchone()
        return int(row["final_round"] or 0) if row else None

    def closed_thread_ids(self) -> List[str]:
        """Ids of every CLOSED thread. Only close_thread writes CLOSED (the dashboard route and --close-thread
        both call it); no rule closes inconclusive threads on its own."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT thread_id FROM threads WHERE status = ? ORDER BY thread_id;",
                                (CLOSED_STATUS,)).fetchall()
        return [r["thread_id"] for r in rows]

    def close_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Marks a thread CLOSED; messages, decisions, reviews, actions and sessions are kept.

        Returns None for an unknown thread, else {"thread_id", "status", "previous_status",
        "already_closed", "closed_at"}. Closing a CLOSED thread changes nothing.
        """
        with self._get_connection() as conn:
            row = conn.execute("SELECT status, closed_at FROM threads WHERE thread_id = ?;", (thread_id,)).fetchone()
            if row is None:
                return None
            previous = row["status"]
            if previous == CLOSED_STATUS:
                return {"thread_id": thread_id, "status": CLOSED_STATUS, "previous_status": previous,
                        "already_closed": True, "closed_at": row["closed_at"]}
            conn.execute(
                "UPDATE threads SET status = ?, closed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE thread_id = ?;", (CLOSED_STATUS, thread_id))
            conn.commit()
            closed_at = conn.execute("SELECT closed_at FROM threads WHERE thread_id = ?;", (thread_id,)).fetchone()[0]
        return {"thread_id": thread_id, "status": CLOSED_STATUS, "previous_status": previous,
                "already_closed": False, "closed_at": closed_at}

    @staticmethod
    def _review_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("reasons", "evaluation", "request"):
            item[key] = json.loads(item[key]) if item.get(key) else None
        return item

    # --- node groups (group_manager.py owns validation and rule evaluation) ---------------

    @staticmethod
    def _group_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item.pop("name_key", None)
        item["include"] = json.loads(item.pop("include_ids") or "[]")
        item["rules"] = json.loads(item.get("rules") or "[]")
        item["exclude"] = json.loads(item.pop("exclude_ids") or "[]")
        return item

    def list_node_groups(self) -> List[Dict[str, Any]]:
        """Every stored group definition, ordered by name."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM node_groups ORDER BY name_key, id;").fetchall()
        return [self._group_row(r) for r in rows]

    def get_node_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM node_groups WHERE id = ?;", (group_id,)).fetchone()
        return self._group_row(row) if row else None

    def save_node_group(self, group: Dict[str, Any], name_key: str) -> None:
        """Creates or replaces a group definition (validated by the caller); keeps created_at.

        Raises sqlite3.IntegrityError when another group already uses name_key."""
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO node_groups (id, name, name_key, description, tone, include_ids, rules, exclude_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                name_key = excluded.name_key,
                description = excluded.description,
                tone = excluded.tone,
                include_ids = excluded.include_ids,
                rules = excluded.rules,
                exclude_ids = excluded.exclude_ids,
                updated_at = CURRENT_TIMESTAMP;
            """, (
                group["id"], group["name"], name_key, group.get("description") or "", group.get("tone"),
                json.dumps(group.get("include") or []), json.dumps(group.get("rules") or []),
                json.dumps(group.get("exclude") or []),
            ))
            conn.commit()

    def delete_node_group(self, group_id: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM node_groups WHERE id = ?;", (group_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_meta(self, key: str) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM guardian_meta WHERE key = ?;", (key,)).fetchone()
        return row["value"] if row else None

    def seed_node_groups(self, groups: List[Dict[str, Any]], marker: str) -> bool:
        """One-time seed: inserts groups only when the marker is unset and the table is empty,
        then sets the marker, all in one transaction. Returns True when groups were inserted."""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            if conn.execute("SELECT 1 FROM guardian_meta WHERE key = ?;", (marker,)).fetchone():
                conn.rollback()
                return False
            empty = conn.execute("SELECT COUNT(*) FROM node_groups;").fetchone()[0] == 0
            if empty:
                for g in groups:
                    conn.execute(
                        "INSERT OR IGNORE INTO node_groups (id, name, name_key, description, include_ids) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (g["id"], g["name"], g["name_key"], g.get("description") or "",
                         json.dumps(g.get("include") or [])),
                    )
            conn.execute("INSERT OR REPLACE INTO guardian_meta (key, value) VALUES (?, ?);", (marker, "1"))
            conn.commit()
            return empty and bool(groups)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

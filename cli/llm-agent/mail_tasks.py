"""The task board of the resident mail store (docs/design/resident-agents.md, "Task board").

A mixin of mail_store.MailStore: it shares that module's connection, injected clock, events,
operation-id cache and refusal helper, so the two together are one store. Claims are fenced by a
generation: the claim sets owner, status and generation in one UPDATE, and a late update after a
release matches no row (`task_released`). Blockers are fixed at creation, may only name existing
visible tasks (so no cycle can form) and propagate on completion, failure, cancellation and expiry.

A result is stored text and applies nothing: filing it through an approval, a debate or an artifact
keeps its own gate (phase M5). The raw patch an open agent attaches to task_update (O3) is the same:
stored text on the row, while the PROPOSED apply_patch action it becomes waits for a person.
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

TASK_OPEN_STATES = ("pending", "blocked", "in_progress")
TASK_FINISHED_STATES = ("completed", "failed", "cancelled", "expired")
DEAD_BLOCKER_STATES = ("failed", "cancelled", "expired")
# Subject of the hub notice a dependent's creator receives when a blocker failed or was cancelled
# or expired; the send-side subtype catalog (mail_store.HUB_STATUS_TYPES) governs the public API.
BLOCKER_NOTICE = "blocker_failed"
# Subject of the hub notice an assignee receives when a task is assigned to it (its wake).
ASSIGN_NOTICE = "task_assigned"


class TaskBoardMixin:
    """Needs the host contract of MailStore: _open/_ready/_write/_event/_refuse/_cached_op/_store_op,
    _agent, _peers, _open_turn, turn_hop, _insert_mail, _hub_notice, now, _refuse codes."""

    # --- helpers ----------------------------------------------------------------------

    def _task_row(self, conn, task_id: Optional[str]):
        if not task_id:
            return None
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    @staticmethod
    def _task_dict(row) -> Dict[str, Any]:
        item = dict(row)
        item["blocked_by"] = json.loads(item.pop("blocked_by_json") or "[]")
        calls_json = item.pop("calls_json", None)
        if calls_json:
            try:
                calls = json.loads(calls_json)
            except ValueError:
                calls = None
            if isinstance(calls, dict):
                records = calls.get("records")
                # Counts only: the records themselves leave the store through task_calls alone.
                item["calls"] = {"records": len(records) if isinstance(records, list) else 0,
                                 "dropped": bool(calls.get("dropped"))}
        # The raw patch stays in the store (task_calls is the pattern); the board shows that there
        # is one and which action it became.
        item["patch"] = bool(item.pop("patch", None))
        return item

    def _task_visible(self, conn, row, viewer: str) -> bool:
        """Operator sees all; an agent sees tasks it owns, is assigned, created, or may claim
        (unassigned, created by the operator, itself or a peer of it)."""
        if row is None:
            return False
        if viewer == "operator":
            return True
        if row["creator"] == viewer or row["owner"] == viewer or row["assignee"] == viewer:
            return True
        if row["assignee"] is not None:
            return False
        return row["creator"] == "operator" or row["creator"] in self._peers(conn, viewer)

    def _open_task_count(self, conn, address: str) -> int:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE ((assignee=? AND status IN "
                           "('pending','blocked')) OR (owner=? AND status='in_progress'))",
                           (address, address)).fetchone()
        return row["n"]

    @staticmethod
    def _blocked_ids(row) -> List[str]:
        return json.loads(row["blocked_by_json"] or "[]") if row else []

    def _dependents(self, conn, blocker_id: str, states: Sequence[str]) -> List[str]:
        """Open tasks whose stored blockers name blocker_id (JSON containment, verified by parsing)."""
        out: List[str] = []
        rows = conn.execute("SELECT id, blocked_by_json FROM tasks WHERE status IN "
                            f"({','.join('?' * len(states))}) AND blocked_by_json LIKE ?",
                            (*states, f'%"{blocker_id}"%')).fetchall()
        return [row["id"] for row in rows if blocker_id in json.loads(row["blocked_by_json"] or "[]")]

    def _propagate_blockers(self, conn, blocker_id: str, outcome: str) -> None:
        """`completed`: dependents whose last open blocker this was become pending. `dead`: they are
        cancelled (reason blocker_failed) and their creators are told; the cancellation cascades."""
        if outcome == "completed":
            for task_id in self._dependents(conn, blocker_id, ("blocked",)):
                row = self._task_row(conn, task_id)
                if row is None:
                    continue
                statuses = {b: (self._task_row(conn, b) or {"status": None})["status"]
                            for b in self._blocked_ids(row)}
                if not [b for b, status in statuses.items() if status in TASK_OPEN_STATES]:
                    conn.execute("UPDATE tasks SET status='pending', modified=? WHERE id=?",
                                 (self._now_ts(), task_id))
                    self._event("task_unblocked", detail={"id": task_id, "blocker": blocker_id},
                                conn=conn)
                    self._notify_assignee(conn, task_id, row["title"], row["assignee"], row["hop"],
                                          None)
            return
        for task_id in self._dependents(conn, blocker_id, ("pending", "blocked")):
            row = self._task_row(conn, task_id)
            if row is None or row["status"] not in ("pending", "blocked"):
                continue
            conn.execute("UPDATE tasks SET status='cancelled', reason='blocker_failed', modified=? "
                         "WHERE id=?", (self._now_ts(), task_id))
            self._event("task_cancelled", detail={"id": task_id, "reason": "blocker_failed",
                                                  "blocker": blocker_id}, conn=conn)
            self._notify_creator(conn, row, f"task {task_id} cancelled: its blocker {blocker_id} "
                                            f"failed, was cancelled or expired")
            self._propagate_blockers(conn, task_id, "dead")

    def _notify_creator(self, conn, row, body: str) -> None:
        if row["creator_kind"] == "agent":
            self._hub_notice(conn, row["creator"], BLOCKER_NOTICE, body)
        else:
            self._event("task_notice", agent=row["creator"], detail={"body": body}, conn=conn)

    def _notify_assignee(self, conn, task_id: str, title: str, assignee: Optional[str], hop: int,
                         actor: Optional[str]) -> None:
        """Queues one hub status notice to the assignee in the same transaction: the relay's next
        poll pushes it, and queued mail wakes the agent. Nothing for a self-assignment (that agent
        is mid-turn already), an unknown or a retiring assignee. Unlike `_hub_notice` the hop is
        the task's, so an agent-to-agent assignment chain keeps counting hops; the mail's task
        column stays NULL (the id is in the body, so the mail names no task the reader cannot see)."""
        import mail_store

        if assignee is None or assignee == actor:
            return
        agent = self._agent(conn, assignee)
        if agent is None or agent["state"] == "retiring":
            return
        body = f"task {task_id} is assigned to you: {title}. Check task_list and claim it with task_claim."
        self._insert_mail(conn, "hub", "hub", assignee, "status", ASSIGN_NOTICE, body,
                          mail_store.new_id("t-"), hop, None, None, None)

    def _now_ts(self) -> str:
        from mail_store import format_ts
        return format_ts(self.now())

    # --- task board -------------------------------------------------------------------

    def task_create(self, creator: str, title: str, detail: str = "", *, op_id: Optional[str],
                    assignee: Optional[str] = None, blocked_by: Sequence[str] = (),
                    project: Optional[str] = None, sha: Optional[str] = None,
                    epoch: Optional[int] = None, turn: Optional[str] = None) -> Dict[str, Any]:
        import mail_store

        self._sweep()
        if creator != "operator":
            if mail_store.resident_off():
                return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=creator)
            if not mail_store.valid_address(creator):
                return self._refuse("bad_input", f"bad creator {creator!r}")
        with self._write() as conn:
            cached = self._cached_op(conn, creator, op_id)
            if cached is not None:
                return cached
            answer = self._task_create_checked(conn, creator, title, detail, assignee, blocked_by,
                                               project, sha, epoch, turn)
            self._store_op(conn, creator, op_id, answer)
            return answer

    def _task_create_checked(self, conn, creator, title, detail, assignee, blocked_by, project, sha,
                             epoch, turn) -> Dict[str, Any]:
        import mail_store
        if not isinstance(title, str) or not title.strip():
            return self._refuse("bad_input", "a task needs a title", agent=creator, conn=conn)
        hop = 0
        if creator != "operator":
            agent = self._agent(conn, creator)
            if agent is None:
                return self._refuse("unknown_agent", f"no agent {creator}", agent=creator, conn=conn)
            if agent["state"] == "retiring":
                return self._refuse("agent_retired", f"{creator} is being removed", agent=creator, conn=conn)
            if turn is None or epoch is None:
                return self._refuse("no_turn", "an agent task create needs the open turn (epoch and "
                                               "turn)", agent=creator, conn=conn)
            if self._open_turn(conn, creator, epoch, turn) is None:
                return self._refuse("wrong_epoch", f"turn {turn} is not {creator}'s open turn at "
                                                   f"epoch {epoch}", agent=creator, conn=conn)
            hop = self.turn_hop(turn)
            if hop > mail_store.HOP_MAX:
                return self._refuse("hop_limit", f"hop {hop} is above {mail_store.HOP_MAX}",
                                    agent=creator, conn=conn)
        if assignee is not None:
            row = self._agent(conn, assignee) if mail_store.valid_address(assignee) else None
            if row is None:
                return self._refuse("unknown_agent", f"no agent {assignee}", agent=creator, conn=conn)
            if row["state"] == "retiring":
                return self._refuse("agent_retired", f"{assignee} is being removed", agent=creator, conn=conn)
            if creator != "operator" and assignee != creator \
                    and assignee not in self._peers(conn, creator):
                return self._refuse("not_a_peer", f"{assignee} is not a peer of {creator}",
                                    agent=creator, conn=conn)
        checked, stored_blockers = self._check_blockers(conn, creator, blocked_by)
        if checked is not None:
            return checked
        if project is not None:
            # The caller (relay or CLI) resolves the project to its commit; the store keeps full shas
            # only, so a project without one is a caller defect, not a task.
            if not isinstance(project, str) or not project.strip():
                return self._refuse("bad_project", "a task project must be a project id",
                                    agent=creator, conn=conn)
            if not (isinstance(sha, str) and sha.strip()):
                return self._refuse("bad_project", f"project {project} needs the sha it is pinned to "
                                                   f"(resolve it when the task is created)",
                                    agent=creator, conn=conn)
        status = "blocked" if stored_blockers else "pending"
        if assignee is not None and self._open_task_count(conn, assignee) >= mail_store.OPEN_TASKS_MAX:
            return self._refuse("open_task_limit", f"{assignee} already has "
                                                   f"{mail_store.OPEN_TASKS_MAX} open tasks",
                                agent=creator, conn=conn)
        now = self._now_ts()
        task_id = mail_store.new_id("k-")
        conn.execute(
            "INSERT INTO tasks(id, title, detail, creator, creator_kind, assignee, owner, status, "
            "generation, project, sha, blocked_by_json, hop, created, modified, expires) "
            "VALUES(?,?,?,?,?,?,NULL,?,0,?,?,?,?,?,?,?)",
            (task_id, title, detail, creator, "agent" if creator != "operator" else "operator",
             assignee, status, project, sha, json.dumps(stored_blockers), hop, now, now,
             mail_store.format_ts(self.now() + mail_store.LIFETIME)))
        self._event("task_created", agent=creator,
                    detail={"id": task_id, "title": title, "assignee": assignee, "status": status},
                    conn=conn)
        if status == "pending":  # a blocked task is announced when it unblocks
            self._notify_assignee(conn, task_id, title, assignee, hop, creator)
        return {"ok": True, "id": task_id, "status": status, "hop": hop}

    def _check_blockers(self, conn, creator, blocked_by) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """Each blocker must exist, be visible to the creator and not be dead; completed are dropped."""
        stored: List[str] = []
        for blocker_id in blocked_by or ():
            row = self._task_row(conn, blocker_id)
            if row is None:
                return self._refuse("unknown_blocker", f"no task {blocker_id}", agent=creator,
                                    conn=conn), []
            if not self._task_visible(conn, row, creator):
                return self._refuse("invisible_blocker", f"task {blocker_id} is not visible to "
                                                         f"{creator}", agent=creator, conn=conn), []
            if row["status"] in DEAD_BLOCKER_STATES:
                return self._refuse("dead_blocker", f"task {blocker_id} is {row['status']}",
                                    agent=creator, conn=conn), []
            if row["status"] != "completed" and blocker_id not in stored:
                stored.append(blocker_id)
        return None, stored

    def task_assign(self, caller: str, task_id: str, assignee: str) -> Dict[str, Any]:
        from mail_store import resident_off, valid_address
        self._sweep()
        if caller != "operator" and resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=caller)
        with self._write() as conn:
            row = self._task_row(conn, task_id)
            if row is None:
                return self._refuse("bad_input", f"no task {task_id}", agent=caller, conn=conn)
            if caller != "operator" and caller != row["creator"]:
                return self._refuse("not_creator", f"{caller} did not create {task_id}", agent=caller,
                                    conn=conn)
            if row["status"] not in ("pending", "blocked"):
                return self._refuse("bad_state", f"task {task_id} is {row['status']}", agent=caller,
                                    conn=conn)
            target = self._agent(conn, assignee) if valid_address(assignee) else None
            if target is None:
                return self._refuse("unknown_agent", f"no agent {assignee}", agent=caller, conn=conn)
            if target["state"] == "retiring":
                return self._refuse("agent_retired", f"{assignee} is being removed", agent=caller,
                                    conn=conn)
            conn.execute("UPDATE tasks SET assignee=?, modified=? WHERE id=?",
                         (assignee, self._now_ts(), task_id))
            self._event("task_assigned", agent=caller,
                        detail={"id": task_id, "assignee": assignee}, conn=conn)
            if row["status"] == "pending":  # a blocked task is announced when it unblocks
                self._notify_assignee(conn, task_id, row["title"], assignee, row["hop"], caller)
            return {"ok": True, "id": task_id, "assignee": assignee}

    def task_claim(self, agent: str, task_id: str, *, op_id: Optional[str], epoch: int,
                   turn: str) -> Dict[str, Any]:
        from mail_store import resident_off
        self._sweep()
        if resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=agent)
        with self._write() as conn:
            cached = self._cached_op(conn, agent, op_id)
            if cached is not None:
                return cached
            answer = self._task_claim_checked(conn, agent, task_id, epoch, turn)
            self._store_op(conn, agent, op_id, answer)
            return answer

    def _task_claim_checked(self, conn, agent: str, task_id: str, epoch: int,
                            turn: str) -> Dict[str, Any]:
        import mail_store

        row_agent = self._agent(conn, agent)
        if row_agent is None:
            return self._refuse("unknown_agent", f"no agent {agent}", agent=agent, conn=conn)
        if row_agent["state"] == "retiring":
            return self._refuse("agent_retired", f"{agent} is being removed", agent=agent, conn=conn)
        if turn is None or epoch is None:
            return self._refuse("no_turn", "a claim needs the open turn (epoch and turn)", agent=agent,
                                conn=conn)
        if self._open_turn(conn, agent, epoch, turn) is None:
            return self._refuse("wrong_epoch", f"turn {turn} is not {agent}'s open turn at epoch "
                                               f"{epoch}", agent=agent, conn=conn)
        task = self._task_row(conn, task_id)
        if task is None:
            return self._refuse("not_claimable", f"no task {task_id}", agent=agent, conn=conn)
        if not self._task_visible(conn, task, agent):
            return self._refuse("invisible_task", f"task {task_id} is not visible to {agent}",
                                agent=agent, conn=conn)
        if self._open_task_count(conn, agent) >= mail_store.OPEN_TASKS_MAX:
            return self._refuse("open_task_limit", f"{agent} already has "
                                                   f"{mail_store.OPEN_TASKS_MAX} open tasks",
                                agent=agent, conn=conn)
        cursor = conn.execute(
            "UPDATE tasks SET owner=:agent, status='in_progress', generation=generation+1, "
            "modified=:now WHERE id=:id AND status='pending' AND owner IS NULL AND "
            "(assignee IS NULL OR assignee=:agent)",
            {"agent": agent, "now": self._now_ts(), "id": task_id})
        if cursor.rowcount != 1:
            return self._refuse("not_claimable", f"task {task_id} cannot be claimed by {agent}",
                                agent=agent, conn=conn)
        generation = self._task_row(conn, task_id)["generation"]
        self._event("task_claimed", agent=agent, detail={"id": task_id, "generation": generation},
                    conn=conn)
        return {"ok": True, "id": task_id, "generation": generation}

    def task_update(self, agent: str, task_id: str, generation: int, status: str, *, op_id: Optional[str],
                    result: Optional[str] = None, reason: Optional[str] = None,
                    epoch: Optional[int] = None, turn: Optional[str] = None,
                    calls: Optional[Dict[str, Any]] = None,
                    patch: Optional[str] = None) -> Dict[str, Any]:
        from mail_store import resident_off
        self._sweep()
        if resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=agent)
        if status not in ("completed", "failed"):
            return self._refuse("bad_input", "status must be completed or failed", agent=agent)
        if status == "completed" and not (isinstance(result, str) and result.strip()):
            return self._refuse("bad_input", "a completed task needs a result", agent=agent)
        if status == "failed" and not (isinstance(reason, str) and reason.strip()):
            return self._refuse("bad_input", "a failed task needs a reason", agent=agent)
        if patch is not None and not isinstance(patch, str):
            return self._refuse("bad_input", "a task patch must be text", agent=agent)
        with self._write() as conn:
            cached = self._cached_op(conn, agent, op_id)
            if cached is not None:
                return cached
            row_agent = self._agent(conn, agent)
            if row_agent is None:
                return self._refuse("unknown_agent", f"no agent {agent}", agent=agent, conn=conn)
            if self._open_turn(conn, agent, epoch, turn) is None:
                return self._refuse("wrong_epoch", f"turn {turn} is not {agent}'s open turn at "
                                                   f"epoch {epoch}", agent=agent, conn=conn)
            answer = self._task_update_checked(conn, agent, task_id, generation, status, result,
                                               reason, calls, patch)
            self._store_op(conn, agent, op_id, answer)
            return answer

    def _task_update_checked(self, conn, agent, task_id, generation, status, result, reason, calls,
                             patch) -> Dict[str, Any]:
        # The relay filters the daemon's call log (mail_snapshots.filter_calls) and converts the
        # open agent's patch before it gets here; both are stored in the same fenced UPDATE as the
        # result, so neither ever outlives its claim.
        cursor = conn.execute(
            "UPDATE tasks SET status=?, result=?, reason=?, calls_json=?, patch=?, modified=? "
            "WHERE id=? AND owner=? AND generation=? AND status='in_progress'",
            (status, result, reason,
             json.dumps(calls, ensure_ascii=False) if calls is not None else None,
             patch, self._now_ts(), task_id, agent, generation))
        task = self._task_row(conn, task_id)
        if cursor.rowcount != 1:
            if task is not None and task["generation"] != generation:
                return self._refuse("task_released", f"task {task_id} moved on (generation "
                                                     f"{task['generation']}, not {generation})",
                                    agent=agent, conn=conn)
            return self._refuse("not_owner", f"task {task_id} is not owned by {agent} at generation "
                                             f"{generation}", agent=agent, conn=conn)
        self._event("task_updated", agent=agent,
                    detail={"id": task_id, "status": status}, conn=conn)
        self._propagate_blockers(conn, task_id, "completed" if status == "completed" else "dead")
        return {"ok": True, "id": task_id, "status": status}

    def task_release(self, caller: str, task_id: str, generation: Optional[int], reason: str, *,
                     op_id: Optional[str] = None) -> Dict[str, Any]:
        from mail_store import resident_off
        self._sweep()
        if caller != "hub" and resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=caller)
        if not (isinstance(reason, str) and reason.strip()):
            return self._refuse("bad_input", "a release needs a reason", agent=caller)
        with self._write() as conn:
            cached = self._cached_op(conn, caller, op_id)
            if cached is not None:
                return cached
            row = self._task_row(conn, task_id)
            if row is None:
                return self._refuse("not_owner", f"no task {task_id}", agent=caller, conn=conn)
            if row["status"] != "in_progress":
                return self._refuse("bad_state", f"task {task_id} is {row['status']}", agent=caller,
                                    conn=conn)
            if caller != "hub" and (row["owner"] != caller or row["generation"] != generation):
                return self._refuse("not_owner", f"task {task_id} is not owned by {caller} at "
                                                 f"generation {generation}", agent=caller, conn=conn)
            if caller == "hub" and generation is not None and row["generation"] != generation:
                # The snapshot pump re-checks the generation before it releases: a late ensure
                # failure must never release a newer claim of the same task.
                return self._refuse("task_released", f"task {task_id} moved on (generation "
                                                     f"{row['generation']}, not {generation})",
                                    agent=caller, conn=conn)
            conn.execute("UPDATE tasks SET owner=NULL, status='pending', generation=generation+1, "
                         "reason=?, modified=? WHERE id=?", (reason, self._now_ts(), task_id))
            self._event("task_released", agent=caller,
                        detail={"id": task_id, "reason": reason}, conn=conn)
            return {"ok": True, "id": task_id, "generation": row["generation"] + 1}

    def task_cancel(self, caller: str, task_id: str, reason: str) -> Dict[str, Any]:
        from mail_store import resident_off
        self._sweep()
        if caller != "operator" and resident_off():
            return self._refuse("resident_off", "GUARDIAN_RESIDENT is off", agent=caller)
        if not (isinstance(reason, str) and reason.strip()):
            return self._refuse("bad_input", "a cancellation needs a reason", agent=caller)
        with self._write() as conn:
            row = self._task_row(conn, task_id)
            if row is None:
                return self._refuse("bad_input", f"no task {task_id}", agent=caller, conn=conn)
            if caller != "operator" and caller != row["creator"]:
                return self._refuse("not_creator", f"{caller} did not create {task_id}", agent=caller,
                                    conn=conn)
            if row["status"] not in TASK_OPEN_STATES:
                return self._refuse("bad_state", f"task {task_id} is {row['status']}", agent=caller,
                                    conn=conn)
            conn.execute("UPDATE tasks SET status='cancelled', reason=?, modified=? WHERE id=?",
                         (reason, self._now_ts(), task_id))
            self._event("task_cancelled", agent=caller,
                        detail={"id": task_id, "reason": reason}, conn=conn)
            self._propagate_blockers(conn, task_id, "dead")
            return {"ok": True, "id": task_id, "status": "cancelled"}

    def task_list(self, viewer: str) -> List[Dict[str, Any]]:
        self._sweep()
        conn = self._ready()
        if conn is None:
            return []
        if viewer == "operator":
            rows = conn.execute("SELECT * FROM tasks ORDER BY created, id").fetchall()
            return [self._task_dict(row) for row in rows]
        placeholders = ",".join("?" * len(TASK_OPEN_STATES))
        rows = conn.execute(f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
                            "ORDER BY created, id", TASK_OPEN_STATES).fetchall()
        out = [self._task_dict(row) for row in rows if self._task_visible(conn, row, viewer)]
        finished = conn.execute(f"SELECT * FROM tasks WHERE status IN "
                                f"({','.join('?' * len(TASK_FINISHED_STATES))}) AND creator=? "
                                "ORDER BY created, id", (*TASK_FINISHED_STATES, viewer)).fetchall()
        out.extend(self._task_dict(row) for row in finished)
        return out

    def task_calls(self, viewer: str, task_id: str) -> Dict[str, Any]:
        """The full stored call log of a task the viewer can see (the operator CLI); task_list and the
        daemon's board show counts only."""
        self._sweep()
        conn = self._ready()
        if conn is None:
            return self._refuse("bad_input", f"no task {task_id}", agent=viewer)
        row = self._task_row(conn, task_id)
        if row is None:
            return self._refuse("bad_input", f"no task {task_id}", agent=viewer)
        if not self._task_visible(conn, row, viewer):
            return self._refuse("invisible_task", f"task {task_id} is not visible to {viewer}",
                                agent=viewer)
        calls = None
        if row["calls_json"]:
            try:
                calls = json.loads(row["calls_json"])
            except ValueError:
                calls = None
        if not isinstance(calls, dict):
            calls = {"records": [], "dropped": False}
        return {"ok": True, "id": task_id, "calls": calls}

    def task_claim_info(self, task_id: str) -> Optional[Dict[str, str]]:
        """The project and sha a task is pinned to, or None when it names none (the relay's claim
        path; read-only, creates no store)."""
        conn = self._ready()
        if conn is None:
            return None
        row = self._task_row(conn, task_id)
        if row is None or not row["project"]:
            return None
        return {"project": row["project"], "sha": row["sha"]}

    # --- task patches (resident-open-tools.md O3) ------------------------------------------

    def task_patch_action(self, task_id: str) -> Optional[int]:
        """The id of the action a task's patch became, or None when it has none yet (read-only,
        creates no store)."""
        conn = self._ready()
        if conn is None:
            return None
        row = self._task_row(conn, task_id)
        return row["patch_action"] if row is not None else None

    def record_task_patch(self, task_id: str, action_id: int, files: int, text_bytes: int,
                          base: Optional[str] = None) -> bool:
        """Links a task's patch to its action and writes the `task_patch` event in one write; only a
        row that carries a patch and has no action yet takes it (the idempotence of the O3 path).
        base, when known, is the patch_base the agent sent (the hub stores no snapshot commit)."""
        with self._write() as conn:
            row = self._task_row(conn, task_id)
            if row is None or not row["patch"] or row["patch_action"] is not None:
                return False
            conn.execute("UPDATE tasks SET patch_action=? WHERE id=? AND patch_action IS NULL",
                         (action_id, task_id))
            detail = {"task": task_id, "action": action_id, "files": files, "bytes": text_bytes}
            if base:
                detail["base"] = base
            self._event("task_patch", agent=row["owner"], detail=detail, conn=conn)
            return True

    def pending_patch_tasks(self) -> List[Dict[str, Any]]:
        """Completed tasks that carry a patch but no action yet (the O3 crash window between the
        fenced task UPDATE and the action record): what the relay's start reconcile turns into
        actions. Read-only, creates no store."""
        conn = self._ready()
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT id, owner, project, sha, result, patch FROM tasks "
            "WHERE status='completed' AND patch IS NOT NULL AND patch_action IS NULL "
            "ORDER BY created, id").fetchall()
        return [dict(row) for row in rows]

    def in_progress_tasks(self, owners: Sequence[str]) -> List[Dict[str, Any]]:
        """The in_progress tasks of the given owners (id, owner, generation, project, sha): what the
        relay's snapshot pump and the post-sync `tasks` frames cover."""
        owners = [owner for owner in owners if isinstance(owner, str)]
        conn = self._ready()
        if conn is None or not owners:
            return []
        placeholders = ",".join("?" * len(owners))
        rows = conn.execute(f"SELECT id, owner, generation, project, sha FROM tasks "
                            f"WHERE status='in_progress' AND owner IN ({placeholders}) "
                            "ORDER BY created, id", tuple(owners)).fetchall()
        return [dict(row) for row in rows]

    def release_agent_tasks(self, address: str, reason: str) -> List[str]:
        """Its in_progress tasks go back to pending (generation+1, owner NULL, reason); tasks
        assigned to it lose the assignee. Used by removal and the 10-minute release."""
        released: List[str] = []
        with self._write() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE owner=? AND status='in_progress'",
                                (address,)).fetchall()
            for row in rows:
                conn.execute("UPDATE tasks SET owner=NULL, status='pending', generation=generation+1, "
                             "reason=?, modified=? WHERE id=?", (reason, self._now_ts(), row["id"]))
                self._event("task_released", agent=address,
                            detail={"id": row["id"], "reason": reason}, conn=conn)
                released.append(row["id"])
            assigned = conn.execute("SELECT id FROM tasks WHERE assignee=?", (address,)).fetchall()
            for row in assigned:
                conn.execute("UPDATE tasks SET assignee=NULL, modified=? WHERE id=?",
                             (self._now_ts(), row["id"]))
                self._event("task_unassigned", agent=address, detail={"id": row["id"]}, conn=conn)
        return released

"""The session part of the mail store (S1a of docs/design/operator-sessions.md).

A mixin of mail_store.MailStore, like mail_tasks and mail_relay_store: interactive CLI sessions on
operator workstations as a new address kind, <name>@<workstation>. Sessions never enter the agents
table, so the node views, the relay's down marks and the task board never see them. Every operation
is fenced by the workstation's link lease: inside the same BEGIN IMMEDIATE transaction the stored
lease must equal the caller's and the link must be `up`, so a superseded stream fails closed on
every write, delivery marks included. GUARDIAN_SESSIONS=off refuses every session operation;
GUARDIAN_RESIDENT=off does not apply to sessions (it governs agents).
"""

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

# A session sends no task kinds (the task board is not offered to sessions) and none reserved for
# the hub and the operator.
SESSION_KINDS = ("message", "question", "answer")
# Mails a session may send per hour (docs/design/operator-sessions.md, "Authority and limits").
SESSION_MAIL_MAX = 30
# The derived `idle`: linked, no turn for this long.
SESSION_IDLE_AFTER = timedelta(minutes=10)


class SessionStoreMixin:
    """Session-side store operations; mixed into mail_store.MailStore. Needs the host contract of
    MailStore: _open/_ready/_write/_event/_refuse/_cached_op/_store_op/_sweep, _agent, _recipient,
    _send_common, now, and the mail_store constants."""

    # --- links and fencing ---------------------------------------------------------------

    def _fence(self, conn, workstation: str, lease: Any) -> Optional[Dict[str, Any]]:
        """None when the caller holds the workstation's current link, a `superseded` refusal
        otherwise. Runs inside the caller's write transaction, so a superseded caller changes
        nothing, whatever it came to do."""
        row = conn.execute("SELECT lease, state FROM links WHERE workstation=?",
                           (workstation,)).fetchone()
        current = (row is not None and row["state"] == "up" and isinstance(lease, int)
                   and not isinstance(lease, bool) and row["lease"] == lease)
        if not current:
            return self._refuse("superseded", f"lease {lease!r} is not workstation "
                                              f"{workstation}'s current link",
                                agent=workstation, conn=conn)
        return None

    def _sessions_offline(self, conn, workstation: str, now: str) -> None:
        conn.execute("UPDATE sessions SET state='offline', offline_since=?, modified=? "
                     "WHERE workstation=? AND state!='ended'", (now, now, workstation))

    def link_claim(self, workstation: str) -> Dict[str, Any]:
        """A new stream for the workstation: lease + 1, state `up`, and every non-ended session of
        it `offline` until it binds again. Creates the store when it does not exist."""
        import mail_store

        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        if not isinstance(workstation, str) or not mail_store.NODE_RE.fullmatch(workstation):
            return self._refuse("bad_input", f"bad workstation {workstation!r}")
        self._sweep()
        with self._write() as conn:
            row = conn.execute("SELECT lease FROM links WHERE workstation=?",
                               (workstation,)).fetchone()
            lease = (row["lease"] + 1) if row is not None else 1
            now = mail_store.format_ts(self.now())
            conn.execute(
                "INSERT INTO links(workstation, lease, state, started) VALUES(?, ?, 'up', ?) "
                "ON CONFLICT(workstation) DO UPDATE SET lease=excluded.lease, state='up', "
                "started=excluded.started, ended=NULL, end_reason=NULL",
                (workstation, lease, now))
            self._sessions_offline(conn, workstation, now)
            self._event("link_claimed", agent=workstation, detail={"lease": lease}, conn=conn)
            return {"ok": True, "lease": lease}

    def link_current(self, workstation: str, lease: Any) -> bool:
        """Whether the lease is the workstation's current live link; read-only, creates nothing."""
        import mail_store

        if mail_store.sessions_off():
            return False
        conn = self._ready()
        if conn is None:
            return False
        row = conn.execute("SELECT lease, state FROM links WHERE workstation=?",
                           (workstation,)).fetchone()
        return (row is not None and row["state"] == "up" and isinstance(lease, int)
                and not isinstance(lease, bool) and row["lease"] == lease)

    def link_end(self, workstation: str, lease: Any, reason: str) -> Dict[str, Any]:
        """Ends the link while it is still current: state `down` with a reason, its non-ended
        sessions `offline`. A stale lease changes nothing (`superseded`)."""
        import mail_store

        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        if not (isinstance(reason, str) and reason.strip()):
            return self._refuse("bad_input", "a link end needs a reason")
        self._sweep()
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            now = mail_store.format_ts(self.now())
            conn.execute("UPDATE links SET state='down', ended=?, end_reason=? WHERE workstation=?",
                         (now, reason, workstation))
            self._sessions_offline(conn, workstation, now)
            self._event("link_ended", agent=workstation, detail={"reason": reason}, conn=conn)
            return {"ok": True, "state": "down"}

    # --- sessions -------------------------------------------------------------------------

    def _session(self, conn, workstation: str, name: str):
        return conn.execute("SELECT * FROM sessions WHERE address=?",
                            (f"{name}@{workstation}",)).fetchone()

    def session_bind(self, workstation: str, lease: Any, name: str,
                     cli_session: Optional[str], *, away: bool = False,
                     peers: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Binds the name to a CLI session on the workstation. A row that is `ended`, or that still
        holds the same cli_session, may rebind (a reconnect keeps its address); a live row bound to
        another CLI session refuses `name_in_use`. Sends no peers_changed notices."""
        import mail_store

        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        if not isinstance(workstation, str) or not mail_store.NODE_RE.fullmatch(workstation):
            return self._refuse("bad_input", f"bad workstation {workstation!r}")
        if not isinstance(name, str) or not mail_store.NAME_RE.fullmatch(name):
            return self._refuse("bad_input", f"bad session name {name!r}")
        if peers is not None and any(not isinstance(peer, str) for peer in peers):
            return self._refuse("bad_input", "peers must be addresses (text)")
        self._sweep()
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            return self._session_bind_checked(conn, workstation, name, cli_session, away, peers)

    def _session_bind_checked(self, conn, workstation, name, cli_session, away,
                              peers) -> Dict[str, Any]:
        import mail_store

        address = f"{name}@{workstation}"
        row = conn.execute("SELECT * FROM sessions WHERE address=?", (address,)).fetchone()
        if name in mail_store.SPECIAL_SENDERS or self._agent(conn, address) is not None:
            return self._refuse("bad_address", f"{address} cannot be a session address",
                                agent=address, conn=conn)
        if row is not None and row["state"] != "ended" and row["cli_session"] != cli_session:
            return self._refuse("name_in_use", f"{name} is in use on {workstation}",
                                agent=address, conn=conn)
        now = mail_store.format_ts(self.now())
        peers_json = json.dumps(sorted(set(peers)), ensure_ascii=False) if peers is not None \
            else (row["peers_json"] if row is not None else None)
        conn.execute(
            "INSERT INTO sessions(address, name, workstation, cli_session, state, away, peers_json,"
            " linked, created, modified) VALUES(?,?,?,?, 'linked', ?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET cli_session=excluded.cli_session, state='linked', "
            "away=excluded.away, peers_json=excluded.peers_json, linked=excluded.linked, "
            "last_turn=NULL, offline_since=NULL, modified=excluded.modified",
            (address, name, workstation, cli_session, int(bool(away)), peers_json, now, now, now))
        self._event("session_bound", agent=address,
                    detail={"workstation": workstation, "rebind": row is not None}, conn=conn)
        return {"ok": True, "address": address}

    def session_unbind(self, workstation: str, lease: Any, name: str) -> Dict[str, Any]:
        """SessionEnd: the session ends and releases its address. Mail sent to it stays `queued`
        until it expires or the name binds again."""
        import mail_store

        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        self._sweep()
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            row = self._session(conn, workstation, name)
            if row is None or row["state"] not in ("linked", "busy"):
                return self._refuse("not_bound", f"{name} is not a bound session of {workstation}",
                                    agent=row["address"] if row is not None else None, conn=conn)
            conn.execute("UPDATE sessions SET state='ended', modified=? WHERE address=?",
                         (mail_store.format_ts(self.now()), row["address"]))
            self._event("session_unbound", agent=row["address"], conn=conn)
            return {"ok": True, "address": row["address"], "state": "ended"}

    def session_state(self, workstation: str, lease: Any, name: str, state: str) -> Dict[str, Any]:
        """`busy` (a turn started) and `linked` (a turn ended) stamp last_turn; `away` and `back`
        set and clear the away flag."""
        import mail_store

        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        if state not in ("busy", "linked", "away", "back"):
            return self._refuse("bad_input", "state must be one of busy, linked, away, back")
        self._sweep()
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            row = self._session(conn, workstation, name)
            if row is None or row["state"] not in ("linked", "busy"):
                return self._refuse("not_bound", f"{name} is not a bound session of {workstation}",
                                    agent=row["address"] if row is not None else None, conn=conn)
            now = mail_store.format_ts(self.now())
            if state in ("busy", "linked"):
                conn.execute("UPDATE sessions SET state=?, last_turn=?, modified=? WHERE address=?",
                             (state, now, now, row["address"]))
                return {"ok": True, "address": row["address"], "state": state}
            away = 1 if state == "away" else 0
            conn.execute("UPDATE sessions SET away=?, modified=? WHERE address=?",
                         (away, now, row["address"]))
            return {"ok": True, "address": row["address"], "away": bool(away)}

    def release_offline_sessions(self) -> List[str]:
        """Every session `offline` for RELEASE_AFTER or more becomes `ended`; the sweep calls this.
        Returns the addresses; creates no store and opens no write when nothing is due."""
        import mail_store

        conn = self._ready()
        if conn is None:
            return []
        cutoff = mail_store.format_ts(self.now() - mail_store.RELEASE_AFTER)
        rows = conn.execute("SELECT address FROM sessions WHERE state='offline' AND offline_since "
                            "IS NOT NULL AND offline_since<=?", (cutoff,)).fetchall()
        released: List[str] = []
        if not rows:
            return released
        with self._write() as wconn:
            for row in rows:
                cursor = wconn.execute("UPDATE sessions SET state='ended', modified=? WHERE "
                                       "address=? AND state='offline'",
                                       (mail_store.format_ts(self.now()), row["address"]))
                if cursor.rowcount:
                    self._event("session_released", agent=row["address"], conn=wconn)
                    released.append(row["address"])
        return released

    # --- session mail ----------------------------------------------------------------------

    def _session_hop(self, conn, sender: str, thread: Optional[str], reply_row) -> int:
        """A session has no turn: its hop is 1 + the highest hop it saw in the thread (the mail
        addressed to it, and the mail it answers), or 1 for a fresh thread."""
        if reply_row is not None:
            thread = thread or reply_row["thread"]
        if not thread:
            return 1
        row = conn.execute("SELECT MAX(hop) AS hop FROM mail WHERE thread=? AND recipient=?",
                           (thread, sender)).fetchone()
        highest = row["hop"] or 0
        if reply_row is not None:
            highest = max(highest, reply_row["hop"])
        return 1 + highest

    def _session_peers(self, conn, row) -> List[str]:
        """The session's effective peers: its stored list, or every agent, every non-ended session
        except itself, and operator when it narrowed nothing."""
        if row["peers_json"]:
            return list(json.loads(row["peers_json"]))
        peers = ["operator"]
        peers += [r["address"] for r in conn.execute("SELECT address FROM agents").fetchall()]
        peers += [r["address"] for r in conn.execute(
            "SELECT address FROM sessions WHERE state!='ended' AND address!=?",
            (row["address"],)).fetchall()]
        return peers

    def _session_peer_entry(self, conn, address: str) -> Dict[str, Any]:
        """One `session_list` row for a peer address: operator, an agent, a session (with the
        derived state and the away flag), or an address no row matches anymore, which stays in
        the answer as `unknown` instead of being dropped silently."""
        if address == "operator":
            return {"address": address, "kind": "operator", "state": "operator", "last_seen": None}
        agent = self._agent(conn, address)
        if agent is not None:
            return {"address": address, "kind": "agent", "state": agent["state"],
                    "last_seen": agent["modified"]}
        row = conn.execute("SELECT * FROM sessions WHERE address=?", (address,)).fetchone()
        if row is None:
            return {"address": address, "kind": "unknown", "state": "unknown", "last_seen": None}
        return {"address": address, "kind": "session", "state": self._session_state(row),
                "away": bool(row["away"]), "last_seen": row["last_turn"] or row["linked"]}

    def session_peers_list(self, workstation: str, lease: Any, name: str) -> Dict[str, Any]:
        """The `session_list` answer: every address this session may write to, one row each with
        kind, state and last-seen, sorted by address and de-duplicated. The effective peers plus
        `operator`, which session_send always allows even when a stored list omits it. Writes
        nothing itself (the fence transaction is the only write it joins)."""
        import mail_store

        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        self._sweep()
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            address = f"{name}@{workstation}"
            row = self._session(conn, workstation, name)
            if row is None or row["state"] not in ("linked", "busy"):
                return self._refuse("not_bound", f"{address} is not a bound session of "
                                                  f"{workstation}", agent=address, conn=conn)
            peers = set(self._session_peers(conn, row))
            peers.add("operator")
            return {"ok": True, "peers": [self._session_peer_entry(conn, peer)
                                           for peer in sorted(peers)]}

    def session_send(self, workstation: str, lease: Any, name: str, to: str, kind: str,
                     subject: str, body: str, *, op_id: Optional[str],
                     thread: Optional[str] = None, reply_to: Optional[str] = None) -> Dict[str, Any]:
        """One mail from a bound session of the workstation: from_kind `session`, no task tag, the
        session hop rule, and the common thread, body and rate limits."""
        self._sweep()
        with self._write() as conn:
            import mail_store

            if mail_store.sessions_off():
                return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off",
                                    agent=f"{name}@{workstation}")
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            address = f"{name}@{workstation}"
            cached = self._cached_op(conn, address, op_id)
            if cached is not None:
                return cached
            answer = self._session_send_checked(conn, workstation, address, to, kind, subject,
                                                body, thread=thread, reply_to=reply_to)
            self._store_op(conn, address, op_id, answer)
            return answer

    def _session_send_checked(self, conn, workstation, address, to, kind, subject, body, *,
                              thread, reply_to) -> Dict[str, Any]:
        import mail_store

        row = conn.execute("SELECT * FROM sessions WHERE address=?", (address,)).fetchone()
        if row is None or row["state"] not in ("linked", "busy"):
            return self._refuse("not_bound", f"{address} is not a bound session of {workstation}",
                                agent=address, conn=conn)
        if not isinstance(subject, str) or not isinstance(body, str):
            return self._refuse("bad_input", "subject and body must be text", agent=address,
                                conn=conn)
        if kind in mail_store.RESERVED_KINDS:
            return self._refuse("reserved_kind", f"{kind} is reserved for the hub and the operator",
                                agent=address, conn=conn)
        if kind not in SESSION_KINDS:
            return self._refuse("bad_kind", f"a session sends one of {', '.join(SESSION_KINDS)}",
                                agent=address, conn=conn)
        if to != "operator" and to not in self._session_peers(conn, row):
            return self._refuse("not_a_peer", f"{to} is not operator or a peer of {address}",
                                agent=address, conn=conn)
        rkind, recipient = self._recipient(conn, to) if mail_store.valid_address(to) else (None, None)
        if rkind == "agent" and recipient["state"] == "retiring":
            return self._refuse("agent_retired", f"{to} is being removed", agent=address, conn=conn)
        hour_ago = mail_store.format_ts(self.now() - timedelta(hours=1))
        recent = conn.execute("SELECT COUNT(*) AS n FROM mail WHERE sender=? AND from_kind='session'"
                              " AND created>=?", (address, hour_ago)).fetchone()["n"]
        if recent >= SESSION_MAIL_MAX:
            return self._refuse("rate_limit", f"{address} sent {recent} mails in the last hour "
                                              f"(limit {SESSION_MAIL_MAX})", agent=address, conn=conn)
        return self._send_common(conn, address, "session", to, kind, subject, body, thread=thread,
                                 task=None, reply_to=reply_to, turn=None, session_sender=True)

    def session_inbox(self, workstation: str, lease: Any) -> Dict[str, Any]:
        """Queued mail of every linked or busy session of the workstation, oldest first."""
        import mail_store

        self._sweep()
        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            rows = conn.execute(
                "SELECT m.* FROM mail m JOIN sessions s ON s.address=m.recipient "
                "WHERE s.workstation=? AND s.state IN ('linked','busy') AND m.state='queued' "
                "ORDER BY m.created, m.id", (workstation,)).fetchall()
            return {"ok": True, "mail": [dict(row) for row in rows]}

    def session_mark_delivered(self, workstation: str, lease: Any,
                               ids: Sequence[str]) -> Dict[str, Any]:
        """queued -> delivered for mail whose recipient is a session of this workstation, in one
        fenced write."""
        import mail_store

        self._sweep()
        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            changed: List[str] = []
            for mail_id in ids or ():
                cursor = conn.execute(
                    "UPDATE mail SET state='delivered' WHERE id=? AND state='queued' AND recipient "
                    "IN (SELECT address FROM sessions WHERE workstation=?)", (mail_id, workstation))
                if cursor.rowcount:
                    changed.append(mail_id)
            return {"ok": True, "ids": changed}

    def session_ack(self, workstation: str, lease: Any, name: str,
                    ids: Sequence[str]) -> Dict[str, Any]:
        """delivered or queued -> acked for mail to that one session."""
        import mail_store

        self._sweep()
        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off")
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            address = f"{name}@{workstation}"
            changed: List[str] = []
            for mail_id in ids or ():
                cursor = conn.execute("UPDATE mail SET state='acked' WHERE id=? AND recipient=? AND "
                                      "state IN ('queued','delivered')", (mail_id, address))
                if cursor.rowcount:
                    changed.append(mail_id)
            return {"ok": True, "ids": changed}

    def session_resident_query(self, workstation: str, lease: Any, name: str, to: str,
                               project: str, prompt: str, *, op_id: Optional[str] = None,
                               task: Optional[str] = None, session_key: Optional[str] = None,
                               mode: str = "in_place",
                               timeout_sec: int = 180) -> Dict[str, Any]:
        """Q4/MS4 (docs/design/resident-session-query.md, resident-multi-session.md): a bound
        workstation session submits a hub-project-scoped direct query to an active resident agent
        (`caller_kind='session'`). Fenced by (workstation, lease) and checked against peers."""
        import mail_store

        self._sweep()
        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off",
                                agent=f"{name}@{workstation}")
        address = f"{name}@{workstation}"
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            row = conn.execute("SELECT * FROM sessions WHERE address=?", (address,)).fetchone()
            if row is None or row["state"] not in ("linked", "busy"):
                return self._refuse("not_bound", f"{address} is not a bound session of {workstation}",
                                    agent=address, conn=conn)
            if to not in self._session_peers(conn, row):
                return self._refuse("not_a_peer", f"{to} is not a peer of {address}",
                                    agent=address, conn=conn)
        return self.submit_query(
            to, project, prompt,
            caller=address, caller_kind="session",
            task=task, mode=mode, session_key=session_key,
            timeout_sec=timeout_sec, op_id=op_id,
        )

    def session_resident_query_get(self, workstation: str, lease: Any, name: str,
                                   query_id: str) -> Dict[str, Any]:
        """Q4: read one direct query submitted by this workstation session (caller must match)."""
        import mail_queries
        import mail_store

        self._sweep()
        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off",
                                agent=f"{name}@{workstation}")
        address = f"{name}@{workstation}"
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            row = conn.execute("SELECT * FROM sessions WHERE address=?", (address,)).fetchone()
            if row is None or row["state"] not in ("linked", "busy"):
                return self._refuse("not_bound", f"{address} is not a bound session of {workstation}",
                                    agent=address, conn=conn)
        if not isinstance(query_id, str) or not mail_queries.QUERY_ID_RE.fullmatch(query_id):
            return self._refuse("bad_input", f"invalid query id {query_id!r}", agent=address)
        item = self.get_query(query_id)
        if item is None or item.get("caller") != address:
            return self._refuse("not_found", f"no query {query_id} for {address}", agent=address)
        return {"ok": True, "query": item}

    def session_live(self, workstation: str, lease: Any, name: str) -> Dict[str, Any]:
        """V2d of docs/design/verification-layer.md: whether one session of this workstation's
        current link is bound and live — the liveness half of session_resident_query, for link
        handlers whose effect happens outside the store (a review runs through the hub's own
        /api/reviews). Fenced by (workstation, lease): a superseded stream fails closed."""
        import mail_store

        self._sweep()
        if mail_store.sessions_off():
            return self._refuse("sessions_off", "GUARDIAN_SESSIONS is off",
                                agent=f"{name}@{workstation}")
        address = f"{name}@{workstation}"
        with self._write() as conn:
            refusal = self._fence(conn, workstation, lease)
            if refusal is not None:
                return refusal
            row = conn.execute("SELECT state FROM sessions WHERE address=?", (address,)).fetchone()
            if row is None or row["state"] not in ("linked", "busy"):
                return self._refuse("not_bound", f"{address} is not a bound session of {workstation}",
                                    agent=address, conn=conn)
        return {"ok": True}

    def _session_state(self, row) -> str:
        """The stored state with `idle` derived: linked, last_turn (or the bind) older than
        SESSION_IDLE_AFTER."""
        import mail_store

        if row["state"] != "linked":
            return row["state"]
        reference = row["last_turn"] or row["linked"]
        if reference and mail_store.parse_ts(reference) <= self.now() - SESSION_IDLE_AFTER:
            return "idle"
        return "linked"

    def session_rows(self, workstation: Optional[str] = None) -> List[Dict[str, Any]]:
        """The session rows of one workstation or all of them, with the derived state, the away
        flag as a boolean and peers parsed; read-only, creates no store."""
        conn = self._ready()
        if conn is None:
            return []
        if workstation is None:
            rows = conn.execute("SELECT * FROM sessions ORDER BY address").fetchall()
        else:
            rows = conn.execute("SELECT * FROM sessions WHERE workstation=? ORDER BY address",
                                (workstation,)).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["state"] = self._session_state(row)
            item["away"] = bool(item["away"])
            item["peers"] = json.loads(item.pop("peers_json") or "null")
            out.append(item)
        return out

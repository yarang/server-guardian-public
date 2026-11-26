"""mail_relay_store: the hub relay's part of the mail store (M2 of docs/design/resident-agents.md).

A mixin of mail_store.MailStore, like mail_tasks: the operations the relay session runs for one
node's stream (delivery and tombstone marks, the node's agent rows, the `mail_agents` view, the
reconcile of the daemon's `sync` frame). Every method uses the store's own connection and helpers;
none creates the store."""

import json
from typing import Any, Dict, List, Sequence

# the `down` reason a lost relay stream writes; the only one a resumed stream may undo
STREAM_LOST = "stream_lost"


class RelayStoreMixin:
    """Relay-side store operations; mixed into mail_store.MailStore."""

    def mark_delivered(self, address: str, ids: Sequence[str]) -> List[str]:
        """The node spooled this mail (its `deliver` answer spooled or duplicate): queued ->
        delivered. Returns the changed ids; creates no store."""
        conn = self._ready()
        if conn is None or not ids:
            return []
        changed: List[str] = []
        with self._write() as wconn:
            for mail_id in ids:
                cursor = wconn.execute("UPDATE mail SET state='delivered' WHERE id=? AND recipient=? "
                                       "AND state='queued'", (mail_id, address))
                if cursor.rowcount:
                    changed.append(mail_id)
        return changed

    def mark_tombstoned(self, address: str, ids: Sequence[str]) -> List[str]:
        """Mail the node reports as already acknowledged (its tombstones): queued or delivered ->
        acked. `retrieved` rows are never touched; their turn settles them."""
        conn = self._ready()
        if conn is None or not ids:
            return []
        changed: List[str] = []
        with self._write() as wconn:
            for mail_id in ids:
                cursor = wconn.execute("UPDATE mail SET state='acked' WHERE id=? AND recipient=? AND "
                                       "state IN ('queued','delivered')", (mail_id, address))
                if cursor.rowcount:
                    self._event("mail_tombstoned", agent=address, detail={"id": mail_id}, conn=wconn)
                    changed.append(mail_id)
        return changed

    def node_agents(self, node: str) -> List[Dict[str, Any]]:
        """The agent rows declared on one node; creates no store."""
        conn = self._ready()
        if conn is None:
            return []
        return [self._agent_dict(row) for row in
                conn.execute("SELECT * FROM agents WHERE node=? ORDER BY address", (node,)).fetchall()]

    def set_node_down(self, node: str, reason: str) -> List[str]:
        """`down` for every agent of the node that is not retiring and not already down (a lost
        relay stream); returns the addresses."""
        rows = [row["address"] for row in self.node_agents(node)
                if row["state"] not in ("retiring", "down")]
        down: List[str] = []
        for address in rows:
            if self.set_down(address, reason).get("ok"):
                down.append(address)
        return down

    def peer_view(self, address: str) -> List[Dict[str, Any]]:
        """The `mail_agents` answer: each effective peer's address, name, node, provider and
        state. No session ids, no epochs (they never leave the hub). A session peer carries the
        derived session state and provider `session`."""
        conn = self._ready()
        if conn is None:
            return []
        out: List[Dict[str, Any]] = []
        for peer in self._peers(conn, address):
            row = self._agent(conn, peer)
            if row is not None:
                out.append({"address": peer, "name": row["name"], "node": row["node"],
                            "provider": row["provider"], "state": row["state"]})
                continue
            session = conn.execute("SELECT * FROM sessions WHERE address=?", (peer,)).fetchone()
            if session is not None:
                out.append({"address": peer, "name": session["name"],
                            "node": session["workstation"], "provider": "session",
                            "state": self._session_state(session)})
        return out

    def reconcile(self, node: str, sync_agents: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """The hub half of the daemon's `sync` frame (resend on reconnect): pending
        acknowledgements first (a wrong_epoch counts as applied; the daemon drops it), then
        tombstones, then the spool against the node's open mail, and last an agent the daemon
        reports bound at the store's epoch goes back to `ready` (a lost relay stream had marked
        it `down` while its incarnation never ended). Addresses unknown to this node are
        returned in `unknown` for the daemon to retire; restored addresses in `restored`."""
        self._sweep()  # expired mail is never put back
        known = {row["address"] for row in self.node_agents(node)}
        applied: List[Dict[str, Any]] = []
        unknown: List[str] = []
        restored: List[str] = []
        for address, report in (sync_agents or {}).items():
            if address not in known:
                unknown.append(address)
                continue
            for ack in report.get("pending_acks") or ():
                result = self.end_turn(address, ack.get("epoch"), ack.get("turn"),
                                       ack.get("end_reason") or "normal",
                                       acked=ack.get("acked") or (),
                                       session_id=ack.get("session_id"),
                                       query_id=ack.get("query_id"),
                                       query_reply=ack.get("query_reply"),
                                       session_key=ack.get("session_key"))
                applied.append({"turn": ack.get("turn"), "ok": bool(result.get("ok"))})
            tombstones = report.get("tombstones") or ()
            self.mark_tombstoned(address, tombstones)
            self._reconcile_spool(address, report.get("spool") or (), tombstones)
            if self._restore_ready(address, report):
                restored.append(address)
        return {"ok": True, "applied": applied, "unknown": unknown, "restored": restored}

    def _restore_ready(self, address: str, report: Dict[str, Any]) -> bool:
        """A `down` agent whose daemon reports itself bound (ready) at the store's exact epoch goes
        back to `ready`: the relay stream was lost and resumed while the incarnation lived on (a
        hub-side restart). A missing or false `ready` (an older daemon, an unbound agent) or an
        epoch mismatch restores nothing; no state but `down` is ever touched."""
        if report.get("ready") is not True:
            return False
        epoch = report.get("epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool):
            return False
        import mail_store  # late: this mixin is part of mail_store itself
        with self._write() as conn:
            row = self._agent(conn, address)
            if row is None or row["state"] != "down" or row["epoch"] != epoch:
                return False
            # only a down the lost stream caused: an exit the daemon reported, or any later
            # reason the hub may have (a lost sandbox), is never undone by the daemon's word
            last = conn.execute("SELECT detail_json FROM events WHERE kind='agent_down' AND agent=? "
                                "ORDER BY id DESC LIMIT 1", (address,)).fetchone()
            reason = (json.loads(last["detail_json"] or "{}") if last else {}).get("reason")
            if reason != STREAM_LOST:
                return False
            cursor = conn.execute("UPDATE agents SET state='ready', not_ready_since=NULL, "
                                  "modified=? WHERE address=? AND state='down' AND epoch=?",
                                  (mail_store.format_ts(self.now()), address, epoch))
            if not cursor.rowcount:
                return False
            self._event("agent_restored", agent=address,
                        detail={"epoch": epoch, "reason": "stream_resumed"}, conn=conn)
            return True

    def _reconcile_spool(self, address: str, spool: Sequence[str], tombstones: Sequence[str]) -> None:
        """Spool ids still queued are delivered (the node already holds them); delivered mail the
        node no longer lists went back to queued as redelivered (the node lost it)."""
        with self._write() as conn:
            for mail_id in spool:
                conn.execute("UPDATE mail SET state='delivered' WHERE id=? AND recipient=? AND "
                             "state='queued'", (mail_id, address))
            kept = set(spool) | set(tombstones)
            rows = conn.execute("SELECT id FROM mail WHERE recipient=? AND state='delivered'",
                                (address,)).fetchall()
            for row in rows:
                if row["id"] not in kept:
                    conn.execute("UPDATE mail SET state='queued', redelivered=1 WHERE id=?",
                                 (row["id"],))

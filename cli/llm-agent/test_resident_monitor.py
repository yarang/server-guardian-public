"""resident_monitor: the read-only snapshot of the resident mail store (resident-monitor.md, RM1).

The fixtures write through the real MailStore (the TEST may write; the module under test may not)
and, where the public API would be heavier than the case needs, through direct SQL on a separate
connection. Addresses use the design's neutral names only: node-1, claude@node-1, agy@node-1,
coder@node-1.
"""

import io
import json
import os
import re
import sqlite3
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

import mail_store
import resident_monitor
import resident_registry
import testenv

# A version-3 tasks table: no patch, no patch_action (the O3 columns of version 4).
OLDER_TASKS_DDL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT, creator TEXT NOT NULL,
    creator_kind TEXT NOT NULL, assignee TEXT, owner TEXT, status TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0, project TEXT, sha TEXT, blocked_by_json TEXT,
    hop INTEGER NOT NULL, result TEXT, reason TEXT, calls_json TEXT,
    created TEXT NOT NULL, modified TEXT NOT NULL, expires TEXT NOT NULL);
"""
OLDER_TASKS_COLUMNS = ("id", "title", "detail", "creator", "creator_kind", "assignee", "owner",
                       "status", "generation", "project", "sha", "blocked_by_json", "hop",
                       "result", "reason", "calls_json", "created", "modified", "expires")

AGENT_KEYS = {"address", "name", "node", "provider", "driver", "tools", "state", "epoch",
              "not_ready_since", "bind_failures", "bind_started", "modified", "incarnation",
              "turn", "mail_open", "tasks_held", "session_ready", "queries_pending",
              "declared", "in_store"}
TURN_KEYS = {"id", "epoch", "kind", "started", "ended", "end_reason", "hop_in", "inputs"}
MAIL_KEYS = {"id", "thread", "sender", "recipient", "kind", "subject", "task", "reply_to", "hop",
             "state", "created", "expires", "redelivered", "bytes"}
TASK_KEYS = {"id", "title", "creator", "assignee", "owner", "status", "project", "sha", "hop",
             "generation", "blocked_by", "patch_bytes", "patch_action", "calls", "reason",
             "created", "modified", "expires"}
EVENT_KEYS = {"id", "at", "kind", "agent", "detail"}


class SimpleClock:
    """A fake now() the test advances by hand."""

    def __init__(self, start="2026-11-21T10:00:00"):
        self.now = datetime.fromisoformat(start + "+00:00")

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def write_declarations(entries):
    path = resident_registry.declaration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": resident_registry.DECLARATION_FORMAT,
                                "modified": "2026-11-21T10:00:00Z", "agents": entries},
                               ensure_ascii=False), encoding="utf-8")
    return path


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = SimpleClock()
        self.store = mail_store.MailStore(now=self.clock)
        self.path = mail_store.default_store_path()

    def sql(self, query, params=()):
        """One write on the store file from a separate connection (the fixture may write)."""
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(query, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def build_world(self):
        """claude@node-1 ready mid-turn with mail and tasks, agy@node-1 stored only,
        coder@node-1 declared only."""
        write_declarations([
            {"name": "claude", "node": "node-1", "provider": "claude", "driver": "headless"},
            {"name": "coder", "node": "node-1", "provider": "claude", "driver": "headless",
             "tools": "open"},
        ])
        for entry in (
            {"address": "claude@node-1", "name": "claude", "node": "node-1", "provider": "claude",
             "driver": "headless"},
            {"address": "agy@node-1", "name": "agy", "node": "node-1", "provider": "agy",
             "driver": "headless"},
        ):
            self.assertTrue(self.store.upsert_agent(entry)["ok"])
        bind = self.store.bind_begin("claude@node-1")
        self.assertTrue(bind["ok"], bind)
        self.assertTrue(self.store.bind_complete("claude@node-1", bind["epoch"], "sess-1")["ok"])
        got = self.store.admit_turn("claude@node-1", bind["epoch"], "wake")
        self.assertTrue(got["ok"], got)
        turn = got["turn"]
        self.clock.advance(5)
        m1 = self.store.send("operator", "claude@node-1", "question", "first", "question body one",
                             op_id="w1")
        self.assertTrue(m1["ok"], m1)
        self.clock.advance(5)
        m2 = self.store.send("operator", "claude@node-1", "message", "second", "second body",
                             op_id="w2")
        self.assertTrue(m2["ok"], m2)
        self.assertTrue(self.store.record_retrieved("claude@node-1", bind["epoch"], turn,
                                                    [m1["id"]])["ok"])
        self.clock.advance(5)
        m3 = self.store.send("claude@node-1", "operator", "answer", "re: first", "answer body",
                             op_id="w3", epoch=bind["epoch"], turn=turn)
        self.assertTrue(m3["ok"], m3)
        self.clock.advance(5)  # distinct created stamps: the task list order must not hang on ids
        task_a = self.store.task_create("operator", "check the thing", "detail",
                                        assignee="claude@node-1", op_id=None)
        self.clock.advance(5)
        task_b = self.store.task_create("operator", "write the fix", "detail", op_id=None)
        for task in (task_a, task_b):
            self.assertTrue(self.store.task_claim("claude@node-1", task["id"], op_id=None,
                                                  epoch=bind["epoch"], turn=turn)["ok"])
        patch_text = "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        self.assertTrue(self.store.task_update(
            "claude@node-1", task_b["id"], 1, "completed", op_id=None, result="done",
            epoch=bind["epoch"], turn=turn,
            calls={"records": [{"tool": "read"}, {"tool": "grep"}], "dropped": False},
            patch=patch_text)["ok"])
        self.assertTrue(self.store.record_task_patch(task_b["id"], 77, 1,
                                                     len(patch_text.encode("utf-8"))))
        self.clock.advance(5)
        task_c = self.store.task_create("operator", "follow-up", "detail",
                                        blocked_by=(task_a["id"],), op_id=None)
        self.assertTrue(task_c["ok"], task_c)
        return {"epoch": bind["epoch"], "turn": turn, "mail": (m1["id"], m2["id"], m3["id"]),
                "tasks": (task_a["id"], task_b["id"], task_c["id"]), "patch": patch_text}

    def find_task(self, snap, task_id):
        return next(task for task in snap["tasks"] if task["id"] == task_id)


class ReadOnly(MonitorTestCase):
    """The read path writes nothing, not even through the helpers it is forbidden to call."""

    def fingerprint(self):
        stat = os.stat(self.path)
        conn = resident_monitor.open_readonly(self.path)
        try:
            stamp = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        return stamp, stat.st_size, stat.st_mtime_ns

    def test_snapshot_and_mail_body_never_write_or_call_the_store(self):
        world = self.build_world()
        before = self.fingerprint()
        with patch.object(mail_store.MailStore, "_open", side_effect=AssertionError("write path")), \
                patch.object(mail_store.MailStore, "_ready", side_effect=AssertionError("write path")), \
                patch("resident_registry.load_declarations",
                      side_effect=AssertionError("write path")):
            snap = resident_monitor.snapshot()
            body = resident_monitor.mail_body(world["mail"][0])
        self.assertTrue(snap["store"]["exists"])
        self.assertEqual([agent["address"] for agent in snap["agents"]],
                         ["agy@node-1", "claude@node-1", "coder@node-1"])
        self.assertEqual(body["id"], world["mail"][0])
        self.assertTrue(body["masked"])
        self.assertEqual(self.fingerprint(), before)

    def test_connection_is_read_only(self):
        self.build_world()
        conn = resident_monitor.open_readonly(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO events(at, kind) VALUES('2026-11-21T00:00:00Z', 'x')")
        finally:
            conn.close()

    def test_missing_store_is_not_created(self):
        write_declarations([{"name": "claude", "node": "node-1", "provider": "claude",
                             "driver": "headless"}])
        snap = resident_monitor.snapshot()
        self.assertFalse(snap["store"]["exists"])
        self.assertIsNone(snap["store"]["schema"])
        self.assertEqual(snap["store"]["path_kind"], "override")  # testenv sets GUARDIAN_DATA_DIR
        self.assertEqual([agent["address"] for agent in snap["agents"]], ["claude@node-1"])
        self.assertEqual((snap["turns"], snap["mail"], snap["tasks"], snap["events"]),
                         ({}, [], [], []))
        self.assertEqual(snap["problems"], [])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())  # not even the mailbox directory
        self.assertIsNone(resident_monitor.mail_body("m-20261121-0001"))

    def test_open_readonly_returns_none_when_missing(self):
        self.assertIsNone(resident_monitor.open_readonly(self.path))


class Shape(MonitorTestCase):
    def test_documented_keys_everywhere(self):
        world = self.build_world()
        snap = resident_monitor.snapshot()
        self.assertEqual(set(snap), {"generated", "store", "agents", "turns", "mail", "tasks",
                                     "events", "artifacts", "queries", "sessions", "problems"})
        self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", snap["generated"]))
        self.assertEqual(set(snap["store"]), {"exists", "schema", "path_kind"})
        self.assertTrue(snap["store"]["exists"])
        self.assertEqual(snap["store"]["schema"], mail_store.SCHEMA_VERSION)
        self.assertEqual(snap["problems"], [])
        by_address = {agent["address"]: agent for agent in snap["agents"]}
        self.assertEqual(sorted(by_address), ["agy@node-1", "claude@node-1", "coder@node-1"])
        for agent in snap["agents"]:
            self.assertEqual(set(agent), AGENT_KEYS)
        claude = by_address["claude@node-1"]
        self.assertTrue(claude["declared"] and claude["in_store"])
        self.assertEqual(claude["state"], "ready")
        self.assertEqual(claude["epoch"], world["epoch"])
        self.assertEqual(claude["bind_failures"], 0)
        self.assertEqual(claude["incarnation"]["epoch"], world["epoch"])
        self.assertIsNone(claude["incarnation"]["pane"])
        self.assertEqual(claude["turn"], {"id": world["turn"], "kind": "wake",
                                          "started": claude["turn"]["started"], "hop_in": None})
        # queued: m2 plus the hub's task_assigned notice to claude (task_a's assignee)
        self.assertEqual(claude["mail_open"], {"queued": 2, "delivered": 0, "retrieved": 1})
        self.assertEqual(claude["tasks_held"], 1)
        self.assertEqual(set(snap["turns"]), {"claude@node-1"})
        turn = snap["turns"]["claude@node-1"][0]
        self.assertEqual(set(turn), TURN_KEYS)
        self.assertEqual((turn["id"], turn["epoch"], turn["kind"], turn["inputs"]),
                         (world["turn"], world["epoch"], "wake", 1))
        self.assertIsNone(turn["ended"])
        notice = snap["mail"][0]  # the task_assigned notice is the newest mail
        self.assertEqual((notice["sender"], notice["kind"], notice["subject"]),
                         ("hub", "status", "task_assigned"))
        self.assertEqual((notice["recipient"], notice["state"]), ("claude@node-1", "queued"))
        self.assertEqual([mail["id"] for mail in snap["mail"][1:]],
                         [world["mail"][2], world["mail"][1], world["mail"][0]])  # newest first
        for mail in snap["mail"]:
            self.assertEqual(set(mail), MAIL_KEYS)
        answer = snap["mail"][1]
        self.assertEqual((answer["recipient"], answer["state"]), ("operator", "delivered"))
        self.assertEqual(answer["bytes"], len("answer body".encode("utf-8")))
        self.assertEqual([task["id"] for task in snap["tasks"]],
                         [world["tasks"][0], world["tasks"][2], world["tasks"][1]])
        for task in snap["tasks"]:
            self.assertEqual(set(task), TASK_KEYS)
        done = self.find_task(snap, world["tasks"][1])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["patch_bytes"], len(world["patch"].encode("utf-8")))
        self.assertEqual(done["patch_action"], 77)
        self.assertEqual(done["calls"], 2)
        self.assertEqual(done["blocked_by"], [])
        held = self.find_task(snap, world["tasks"][0])
        self.assertEqual((held["status"], held["owner"]), ("in_progress", "claude@node-1"))
        self.assertIsNone(held["patch_bytes"])
        blocked = self.find_task(snap, world["tasks"][2])
        self.assertEqual((blocked["status"], blocked["blocked_by"]),
                         ("blocked", [world["tasks"][0]]))
        ids = [event["id"] for event in snap["events"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        for event in snap["events"]:
            self.assertEqual(set(event), EVENT_KEYS)
        patch_event = next(event for event in snap["events"] if event["kind"] == "task_patch")
        self.assertEqual(patch_event["detail"],
                         {"task": world["tasks"][1], "action": 77, "files": 1,
                          "bytes": len(world["patch"].encode("utf-8"))})

    def test_declared_only_and_store_only_agents(self):
        self.build_world()
        snap = resident_monitor.snapshot()
        by_address = {agent["address"]: agent for agent in snap["agents"]}
        coder = by_address["coder@node-1"]  # in the declaration file, never registered
        self.assertTrue(coder["declared"])
        self.assertFalse(coder["in_store"])
        self.assertEqual((coder["name"], coder["node"], coder["provider"], coder["driver"],
                          coder["tools"]), ("coder", "node-1", "claude", "headless", "open"))
        for key in ("state", "epoch", "not_ready_since", "bind_started", "modified",
                    "incarnation", "turn"):
            self.assertIsNone(coder[key], key)
        self.assertEqual(coder["bind_failures"], 0)
        self.assertEqual(coder["mail_open"], {"queued": 0, "delivered": 0, "retrieved": 0})
        self.assertEqual(coder["tasks_held"], 0)
        agy = by_address["agy@node-1"]  # in the store, not in the declaration file
        self.assertFalse(agy["declared"])
        self.assertTrue(agy["in_store"])
        self.assertEqual((agy["name"], agy["provider"], agy["state"]), ("agy", "agy", "down"))
        self.assertIsNone(agy["tools"])

    def test_bad_declaration_file_is_one_problem_line(self):
        self.build_world()
        path = resident_registry.declaration_path()
        path.write_text("{not json", encoding="utf-8")
        snap = resident_monitor.snapshot()
        self.assertEqual(len(snap["problems"]), 1)
        self.assertTrue(snap["problems"][0].startswith(f"{path.name}: cannot read ("))
        self.assertEqual([agent["address"] for agent in snap["agents"]],
                         ["agy@node-1", "claude@node-1"])  # the store's agents still show

    def test_bad_declaration_entry_is_skipped(self):
        self.build_world()
        write_declarations([
            {"name": "claude", "node": "node-1", "provider": "claude", "driver": "headless"},
            {"node": "node-1"},  # no name: not a usable declaration
        ])
        snap = resident_monitor.snapshot()
        self.assertEqual(snap["problems"], ["resident-agents.json: entry 2 is not a usable "
                                            "declaration"])
        self.assertEqual([agent["address"] for agent in snap["agents"]],
                         ["agy@node-1", "claude@node-1"])


class Secrets(MonitorTestCase):
    def test_bodies_patch_text_and_event_values_never_leave_raw(self):
        body = "plain PLAINWORD note and password=SECRETMARKER-hunter2 also api_key=SECRETMARKER-sk1"
        self.assertTrue(self.store.upsert_agent(
            {"address": "claude@node-1", "name": "claude", "node": "node-1", "provider": "claude",
             "driver": "headless"})["ok"])
        sent = self.store.send("operator", "claude@node-1", "message", "hello", body, op_id="s1")
        self.assertTrue(sent["ok"], sent)
        self.sql("INSERT INTO tasks(id, title, creator, creator_kind, status, hop, created, "
                 "modified, expires, patch) VALUES('k-sec', 't', 'operator', 'operator', "
                 "'completed', 0, '2026-11-21T10:00:00Z', '2026-11-21T10:00:00Z', "
                 "'2026-11-22T10:00:00Z', 'SECRETMARKER-in-patch')")
        self.store.note("secret_note", agent="claude@node-1",
                        detail={"note": "token=SECRETMARKER-tok"})
        dump = json.dumps(resident_monitor.snapshot(), ensure_ascii=False)
        self.assertNotIn("SECRETMARKER", dump)   # the masked value is nowhere, events included
        self.assertNotIn("PLAINWORD", dump)      # and no mail body leaves at all
        got = resident_monitor.mail_body(sent["id"])
        self.assertIn("PLAINWORD", got["body"])
        self.assertNotIn("SECRETMARKER", got["body"])
        self.assertIn("password=***", got["body"])
        self.assertEqual(got["bytes"], len(body.encode("utf-8")))
        self.assertTrue(got["masked"])
        snap = resident_monitor.snapshot()
        event = next(item for item in snap["events"] if item["kind"] == "secret_note")
        self.assertEqual(event["detail"], {"note": "token=***"})


class Sizes(MonitorTestCase):
    def test_mail_bytes_are_utf8_length_not_characters(self):
        text = "가나다body"  # 7 characters, 13 UTF-8 bytes
        self.assertEqual(len(text), 7)
        self.assertTrue(self.store.upsert_agent(
            {"address": "claude@node-1", "name": "claude", "node": "node-1", "provider": "claude",
             "driver": "headless"})["ok"])
        sent = self.store.send("operator", "claude@node-1", "message", "제목", text, op_id="k1")
        self.assertTrue(sent["ok"], sent)
        mail = next(item for item in resident_monitor.snapshot()["mail"]
                    if item["id"] == sent["id"])
        self.assertEqual(mail["bytes"], 13)
        self.assertEqual(resident_monitor.mail_body(sent["id"])["bytes"], 13)


class OlderSchema(MonitorTestCase):
    def test_version3_tasks_still_list_without_patch_columns(self):
        world = self.build_world()
        keep = ", ".join(OLDER_TASKS_COLUMNS)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("ALTER TABLE tasks RENAME TO tasks_v5")
            conn.execute(OLDER_TASKS_DDL)
            conn.execute(f"INSERT INTO tasks({keep}) SELECT {keep} FROM tasks_v5")
            conn.execute("DROP TABLE tasks_v5")
            conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        snap = resident_monitor.snapshot()
        self.assertEqual(snap["store"]["schema"], 3)
        done = self.find_task(snap, world["tasks"][1])
        self.assertEqual(done["status"], "completed")
        self.assertIsNone(done["patch_bytes"])
        self.assertIsNone(done["patch_action"])
        self.assertEqual(done["calls"], 2)  # calls_json survived the rewrite
        blocked = self.find_task(snap, world["tasks"][2])
        self.assertEqual(blocked["blocked_by"], [world["tasks"][0]])
        self.assertIn(f"store schema version 3 is not {mail_store.SCHEMA_VERSION}", snap["problems"])


class MissingTable(MonitorTestCase):
    def test_dropped_events_table_gives_empty_section_and_problem(self):
        self.build_world()
        self.sql("DROP TABLE events")
        snap = resident_monitor.snapshot()
        self.assertEqual(snap["events"], [])
        self.assertIn("table events is missing", snap["problems"])
        self.assertTrue(snap["agents"])  # the rest of the snapshot still shows


class Limits(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.store.upsert_agent(
            {"address": "claude@node-1", "name": "claude", "node": "node-1", "provider": "claude",
             "driver": "headless"})["ok"])

    def insert_turns(self, count):
        for i in range(1, count + 1):
            ended = f"'2026-11-21T10:00:{i:02d}Z'" if i <= count - 5 else "NULL"
            reason = "'normal'" if i <= count - 5 else "NULL"
            self.sql(f"INSERT INTO turns(id, address, epoch, kind, started, ended, end_reason) "
                     f"VALUES('u-{i:04d}', 'claude@node-1', 1, 'wake', "
                     f"'2026-11-21T10:00:{i:02d}Z', {ended}, {reason})")

    def insert_mail(self, count):
        for i in range(1, count + 1):
            created = f"2026-11-21T{10 + i // 60:02d}:{i % 60:02d}:00Z"
            self.sql("INSERT INTO mail(id, thread, sender, from_kind, recipient, kind, subject, "
                     "body, hop, created, expires, state) VALUES(?, 't-1', 'operator', 'operator', "
                     "'claude@node-1', 'message', 's', 'b', 0, ?, '2026-11-22T10:00:00Z', "
                     "'queued')", (f"m-20261121-{i:04d}", created))

    def test_turns_and_mail_are_cut_newest_first(self):
        self.insert_turns(25)
        self.insert_mail(120)
        snap = resident_monitor.snapshot()
        turns = snap["turns"]["claude@node-1"]
        self.assertEqual(len(turns), 20)
        self.assertEqual(turns[0]["id"], "u-0025")  # newest first
        self.assertEqual(turns[0]["inputs"], 0)
        self.assertEqual(len(snap["mail"]), 100)
        self.assertEqual(snap["mail"][0]["id"], "m-20261121-0120")
        self.assertEqual(snap["problems"], [])

    def test_overrides_and_unknown_keys(self):
        self.insert_turns(25)
        self.insert_mail(120)
        snap = resident_monitor.snapshot({"turns": 5, "mail": 7})
        self.assertEqual(len(snap["turns"]["claude@node-1"]), 5)
        self.assertEqual(len(snap["mail"]), 7)
        snap = resident_monitor.snapshot({"nonsense": 1})
        self.assertEqual(len(snap["mail"]), 100)
        self.assertEqual(len(snap["turns"]["claude@node-1"]), 20)


class MailBodyCli(MonitorTestCase):
    def test_bad_id_raises_and_cli_returns_2(self):
        for bad in ("", "x", "m-123", "m-20261121-12", "m-20261121-0012x", "m-20261121-0012 ",
                    "M-20261121-0012"):
            with self.assertRaises(ValueError):
                resident_monitor.mail_body(bad)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_mail_body("oops", json_out=True), 2)
        self.assertEqual(json.loads(out.getvalue()), {"error": "invalid mail id"})

    def test_unknown_id_returns_none_and_cli_returns_3(self):
        self.build_world()
        self.assertIsNone(resident_monitor.mail_body("m-20261121-9999"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_mail_body("m-20261121-9999", json_out=True), 3)
        self.assertEqual(json.loads(out.getvalue()), {"error": "mail not found"})

    def test_found_body_json_and_text(self):
        world = self.build_world()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_mail_body(world["mail"][0], json_out=True), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["id"], world["mail"][0])
        self.assertEqual(data["body"], "question body one")
        self.assertTrue(data["masked"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_mail_body(world["mail"][0], json_out=False), 0)
        self.assertEqual(out.getvalue().strip(), "question body one")


class CliMonitor(MonitorTestCase):
    def test_json_and_human_output(self):
        self.build_world()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_monitor(json_out=True), 0)
        snap = json.loads(out.getvalue())
        self.assertEqual({agent["address"] for agent in snap["agents"]},
                         {"agy@node-1", "claude@node-1", "coder@node-1"})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_monitor(json_out=False), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)  # one line per agent, no problems
        self.assertTrue(lines[0].startswith("agy@node-1"))
        self.assertIn("claude@node-1", lines[1])
        self.assertIn("coder@node-1", lines[2])

    def test_missing_store_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_monitor(json_out=True), 0)
        snap = json.loads(out.getvalue())
        self.assertFalse(snap["store"]["exists"])
        self.assertEqual(snap["agents"], [])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(resident_monitor.cli_monitor(json_out=False), 0)
        self.assertIn("no resident agents", out.getvalue())  # human, no store: still not an error


class Cost(MonitorTestCase):
    def test_snapshot_of_a_loaded_store_under_one_second(self):
        for name, provider in (("claude", "claude"), ("agy", "agy"), ("coder", "claude")):
            self.assertTrue(self.store.upsert_agent(
                {"address": f"{name}@node-1", "name": name, "node": "node-1",
                 "provider": provider, "driver": "headless"})["ok"])
        self.store.close()
        conn = sqlite3.connect(self.path)
        conn.execute("BEGIN")
        for agent in ("claude", "agy", "coder"):
            for i in range(30):
                conn.execute("INSERT INTO turns(id, address, epoch, kind, started) "
                             "VALUES(?, ?, 1, 'wake', ?)",
                             (f"u-{agent}-{i:03d}", f"{agent}@node-1",
                              f"2026-11-21T09:00:{i:02d}Z"))
        for i in range(1, 201):
            conn.execute("INSERT INTO mail(id, thread, sender, from_kind, recipient, kind, "
                         "subject, body, hop, created, expires, state) VALUES(?, 't', 'operator', "
                         "'operator', 'claude@node-1', 'message', 's', 'b', 0, ?, "
                         "'2026-11-22T10:00:00Z', 'queued')",
                         (f"m-20261121-{i:04d}", f"2026-11-21T{10 + i // 60:02d}:{i % 60:02d}:00Z"))
        for i in range(1, 61):
            status, owner = ("in_progress", "claude@node-1") if i <= 20 else ("completed", None)
            conn.execute("INSERT INTO tasks(id, title, creator, creator_kind, owner, status, hop, "
                         "created, modified, expires) VALUES(?, 't', 'operator', 'operator', ?, ?, "
                         "0, '2026-11-21T10:00:00Z', '2026-11-21T10:00:00Z', "
                         "'2026-11-22T10:00:00Z')", (f"k-{i:03d}", owner, status))
        for i in range(300):
            conn.execute("INSERT INTO events(at, kind, agent, detail_json) VALUES(?, 'note', "
                         "'claude@node-1', ?)",
                         ("2026-11-21T10:00:00Z", json.dumps({"i": i, "note": f"event {i}"})))
        conn.commit()
        conn.close()
        resident_monitor._mask("warm")  # the one-time audit_run import is not a per-poll cost
        start = time.monotonic()
        snap = resident_monitor.snapshot()
        elapsed = time.monotonic() - start
        self.assertEqual([agent["address"] for agent in snap["agents"]],
                         ["agy@node-1", "claude@node-1", "coder@node-1"])
        self.assertEqual(len(snap["mail"]), 100)
        self.assertEqual(len(snap["events"]), 100)
        self.assertEqual(len(snap["tasks"]), 60)  # 20 open + 40 newest closed
        self.assertEqual([len(rows) for rows in snap["turns"].values()], [20, 20, 20])
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()

"""mail_snapshots: sha pinning at task creation, the background snapshot ensure against a fake
transport, the call-log filter, the store's schema 3 (tasks.calls_json) and the operator CLI views.
No ssh and no model: git workspaces are built in a temp dir (make_repo of test_repo_tools) and
ProjectManager.load is patched onto them."""

import io
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import mail_snapshots
import mail_store
import remote_agents
import testenv
from project_registry import Project, ProjectManager, ProjectValidationError
from repo_snapshot import SnapshotError, build_snapshot
from test_mail_store import make_clock, make_ready, open_turn, refuse, register
from test_repo_tools import git, make_repo


class FakeTransport:
    """Records the ssh-shaped calls of SnapshotEnsurer; snapshot_has answers from a set of keys."""

    def __init__(self, present=(), fail_upload=False):
        self.calls = []
        self.uploads = []
        self.present = set(present)
        self.fail_upload = fail_upload

    def call(self, host, request, timeout):
        self.calls.append((host, dict(request), timeout))
        if request.get("op") != "snapshot_has":
            return {"ok": True}
        return {"ok": True, "present": request.get("key") in self.present}

    def upload(self, host, key, stream, timeout):
        self.uploads.append((host, key, Path(stream), timeout))
        if self.fail_upload:
            return {"ok": False, "error": "no space left on device"}
        self.present.add(key)
        return {"ok": True}

    def ops(self):
        return [request.get("op") for _host, request, _timeout in self.calls]


class SnapshotCase(unittest.TestCase):
    """A git workspace standing in for project projx, with ProjectManager.load patched onto it."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(Path(self._tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        patcher = patch.object(ProjectManager, "load",
                               return_value=Project(id="projx", workspace_raw=str(self.repo)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ensurer(self, transport=None, **kwargs):
        return mail_snapshots.SnapshotEnsurer("node-1", transport=transport or FakeTransport(),
                                              **kwargs)


class ResolveProjectSha(SnapshotCase):
    def test_pins_the_full_head_sha(self):
        self.assertEqual(mail_snapshots.resolve_project_sha("projx"), ("projx", self.sha))

    def test_a_ref_resolves_to_the_same_commit(self):
        self.assertEqual(mail_snapshots.resolve_project_sha("projx", "main"), ("projx", self.sha))
        self.assertEqual(mail_snapshots.resolve_project_sha("projx", self.sha[:8])[1], self.sha)

    def test_unknown_project_refuses(self):
        with patch.object(ProjectManager, "load",
                          side_effect=ProjectValidationError("no project 'ghost'")):
            with self.assertRaises(mail_snapshots.TaskSnapshotError) as caught:
                mail_snapshots.resolve_project_sha("ghost")
        self.assertIn("ghost", str(caught.exception))

    def test_project_without_workspace_refuses(self):
        ops = Project(id="opsx", kind="ops")
        with patch.object(ProjectManager, "load", return_value=ops):
            with self.assertRaises(mail_snapshots.TaskSnapshotError) as caught:
                mail_snapshots.resolve_project_sha("opsx")
        self.assertIn("no git workspace", str(caught.exception))

    def test_unknown_ref_and_headless_repository_refuse(self):
        with self.assertRaises(mail_snapshots.TaskSnapshotError):
            mail_snapshots.resolve_project_sha("projx", "no-such-ref")
        empty = Project(id="emptyx", workspace_raw=str(Path(self._tmp.name) / "not-a-repo"))
        with patch.object(ProjectManager, "load", return_value=empty):
            with self.assertRaises(mail_snapshots.TaskSnapshotError):
                mail_snapshots.resolve_project_sha("emptyx")


class Ensure(SnapshotCase):
    def test_builds_uploads_and_removes_the_temp_dir(self):
        transport = FakeTransport()
        seen = []

        def builder(tools, directory):
            seen.append(Path(directory))
            return build_snapshot(tools, directory)

        ensurer = self.ensurer(transport, builder=builder)
        key, allowed = ensurer.ensure("projx", self.sha)
        self.assertEqual(transport.ops(), ["snapshot_has"])
        host, uploaded_key, _stream, timeout = transport.uploads[0]
        self.assertEqual((host, uploaded_key), ("node-1", key))
        self.assertGreater(timeout, remote_agents.UPLOAD_BASE_SECONDS)
        self.assertEqual(ensurer.known_key("projx", self.sha), key)
        self.assertEqual(ensurer.allowed_paths("projx", self.sha), allowed)  # from the cache now
        self.assertIn("cli/tool.py", allowed)
        self.assertNotIn("apps/agent/.env", allowed)
        self.assertFalse(seen[0].exists())  # the hub's temp copy always goes away

    def test_a_second_ensure_of_the_same_pair_skips_ssh(self):
        transport = FakeTransport()
        ensurer = self.ensurer(transport)
        first = ensurer.ensure("projx", self.sha)
        again = ensurer.ensure("projx", self.sha)
        self.assertEqual(first, again)
        self.assertEqual(transport.ops(), ["snapshot_has"])  # one check, one upload, nothing since

    def test_a_snapshot_the_node_already_holds_needs_no_upload(self):
        key, _allowed = self.ensurer().ensure("projx", self.sha)
        transport = FakeTransport(present=[key])  # a relay restart forgot the memory, the node not
        again = self.ensurer(transport)
        self.assertEqual(again.ensure("projx", self.sha)[0], key)
        self.assertEqual(transport.ops(), ["snapshot_has"])
        self.assertEqual(transport.uploads, [])

    def test_a_failed_upload_raises_and_removes_the_temp_dir(self):
        transport = FakeTransport(fail_upload=True)
        seen = []

        def builder(tools, directory):
            seen.append(Path(directory))
            return build_snapshot(tools, directory)

        ensurer = self.ensurer(transport, builder=builder)
        with self.assertRaises(mail_snapshots.TaskSnapshotError) as caught:
            ensurer.ensure("projx", self.sha)
        self.assertIn("upload", str(caught.exception))
        self.assertFalse(seen[0].exists())
        self.assertIsNone(ensurer.known_key("projx", self.sha))

    def test_a_builder_failure_raises_and_removes_the_temp_dir(self):
        seen = []

        def builder(tools, directory):
            seen.append(Path(directory))
            raise SnapshotError("cannot write the snapshot stream: disk full")

        with self.assertRaises(mail_snapshots.TaskSnapshotError) as caught:
            self.ensurer(builder=builder).ensure("projx", self.sha)
        self.assertIn("snapshot", str(caught.exception))
        self.assertFalse(seen[0].exists())

    def test_allowed_paths_on_a_miss_builds_nothing_and_touches_no_ssh(self):
        transport = FakeTransport()
        paths = self.ensurer(transport).allowed_paths("projx", self.sha)
        self.assertEqual(transport.calls, [])
        self.assertIn("cli/tool.py", paths)  # a safe superset of what the snapshot holds
        self.assertNotIn("apps/agent/.env", paths)


class FilterCalls(unittest.TestCase):
    RECORD = {"op": "read", "target": "cli/tool.py", "ok": True, "bytes": 9, "truncated": False,
              "error": None, "limit": None}

    def test_keeps_known_fields_and_filters_paths_to_the_snapshot(self):
        calls = {"records": [dict(self.RECORD, paths=["cli/tool.py", "/etc/shadow"],
                                  spans={"cli/tool.py": [[1, 9]], "gone.py": [[2, 4]]},
                                  mentions=["cli/tool.py", "outside/secret.py"], spoof=True)],
                 "dropped": False}
        out = mail_snapshots.filter_calls(calls, frozenset({"cli/tool.py"}))
        record = out["records"][0]
        self.assertEqual(sorted(record), sorted(mail_snapshots.CALL_KEYS))
        self.assertEqual(record["paths"], ["cli/tool.py"])
        self.assertEqual(record["spans"], {"cli/tool.py": [[1, 9]]})
        self.assertEqual(record["mentions"], ["cli/tool.py"])
        self.assertFalse(out["dropped"])
        self.assertEqual(out["filtered"], 3)  # /etc/shadow, gone.py, outside/secret.py

    def test_anything_else_is_empty_and_dropped(self):
        for calls in (None, ["x"], {"records": "no"}, {"dropped": False}):
            self.assertEqual(mail_snapshots.filter_calls(calls, frozenset()),
                             {"records": [], "dropped": True, "filtered": 0})
        out = mail_snapshots.filter_calls({"records": ["x", 3]}, frozenset())
        self.assertEqual(out["records"], [])

    def test_the_daemons_dropped_flag_travels(self):
        out = mail_snapshots.filter_calls({"records": [self.RECORD], "dropped": True}, frozenset())
        self.assertTrue(out["dropped"])
        self.assertEqual(len(out["records"]), 1)

    def test_oversize_stops_adding_records(self):
        calls = {"records": [dict(self.RECORD), dict(self.RECORD)], "dropped": False}
        with patch.object(remote_agents, "MAX_CALLS_BYTES", 1):
            out = mail_snapshots.filter_calls(calls, frozenset())
        self.assertEqual(out["records"], [])
        self.assertTrue(out["dropped"])


class StoreCalls(unittest.TestCase):
    """Schema 3 and the task call log in the store (mail_tasks through MailStore)."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = make_clock()
        self.store = mail_store.MailStore(now=self.clock)

    def open_claim(self, project="projx", sha="f" * 40):
        """A ready agent with an open turn that claimed a (project) task; (address, turn, task, gen)."""
        address = register(self.store)
        epoch = make_ready(self.store, address)
        turn = open_turn(self.store, address, epoch)
        task = self.store.task_create("operator", "work", op_id="op-create", project=project,
                                      sha=sha)["id"]
        claimed = self.store.task_claim(address, task, op_id="op-claim", epoch=epoch, turn=turn)
        self.assertTrue(claimed["ok"], claimed)
        return address, epoch, turn, task, claimed["generation"]

    def test_version_2_store_is_migrated(self):
        path = mail_store.default_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(path)
        old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE agents (
            address TEXT PRIMARY KEY, name TEXT NOT NULL, node TEXT NOT NULL, provider TEXT NOT NULL,
            driver TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'down', epoch INTEGER NOT NULL DEFAULT 0,
            session_id TEXT, not_ready_since TEXT, peers_json TEXT, bind_started TEXT,
            bind_failures INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, modified TEXT NOT NULL);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT, creator TEXT NOT NULL,
            creator_kind TEXT NOT NULL, assignee TEXT, owner TEXT, status TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0, project TEXT, sha TEXT, blocked_by_json TEXT,
            hop INTEGER NOT NULL, result TEXT, reason TEXT, created TEXT NOT NULL, modified TEXT NOT NULL,
            expires TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '2');
        INSERT INTO agents VALUES('claude@node-1', 'claude', 'node-1', 'claude', 'headless', 'ready',
                                  1, 's', NULL, NULL, NULL, 0, '2026-11-15T09:00:00Z',
                                  '2026-11-15T09:00:00Z');
        INSERT INTO tasks VALUES('k-1', 'work', '', 'operator', 'operator', NULL, 'claude@node-1',
                                 'in_progress', 3, NULL, NULL, '[]', 0, NULL, NULL,
                                 '2026-11-15T09:00:00Z', '2026-11-15T09:00:00Z',
                                 '2026-11-16T09:00:00Z');
        CREATE TABLE turns (
            id TEXT PRIMARY KEY, address TEXT NOT NULL, epoch INTEGER NOT NULL, kind TEXT NOT NULL,
            started TEXT NOT NULL, ended TEXT, end_reason TEXT, hop_in INTEGER);
        INSERT INTO turns VALUES('u-1', 'claude@node-1', 1, 'wake', '2026-11-15T09:30:00Z', NULL,
                                 NULL, NULL);
        """)
        old.commit()
        old.close()
        store = mail_store.MailStore(now=self.clock)
        rows = store.task_list("operator")
        self.assertEqual([row["id"] for row in rows], ["k-1"])
        self.assertEqual(rows[0]["generation"], 3)  # the row survived the migration
        conn = store._ready()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("calls_json", columns)
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(version["value"], str(mail_store.SCHEMA_VERSION))
        # the migrated column works: an update fills it
        done = store.task_update("claude@node-1", "k-1", 3, "completed", op_id="op-mig",
                                 result="done", epoch=1, turn="u-1",
                                 calls={"records": [{"op": "read"}], "dropped": False})
        self.assertTrue(done["ok"], done)
        self.assertEqual(store.task_list("operator")[0]["calls"], {"records": 1, "dropped": False})

    def test_task_create_pins_the_given_sha_and_refuses_one_without(self):
        register(self.store)
        kept = self.store.task_create("operator", "pinned", op_id="op-1", project="projx",
                                      sha="a" * 40)
        self.assertTrue(kept["ok"], kept)
        row = self.store.task_list("operator")[0]
        self.assertEqual((row["project"], row["sha"]), ("projx", "a" * 40))
        self.assertTrue(refuse(self.store.task_create("operator", "loose", op_id="op-2",
                                                      project="projx"), "bad_project"))
        self.assertTrue(refuse(self.store.task_create("operator", "odd", op_id="op-3", project=7,
                                                      sha="a" * 40), "bad_project"))
        self.assertEqual([row["id"] for row in self.store.task_list("operator")], [kept["id"]])

    def test_task_update_stores_calls_with_the_result(self):
        address, epoch, turn, task, generation = self.open_claim()
        self.assertEqual(self.store.task_claim_info(task), {"project": "projx", "sha": "f" * 40})
        self.assertIsNone(self.store.task_claim_info("k-none"))
        calls = {"records": [dict(FilterCalls.RECORD)], "dropped": False, "filtered": 0}
        done = self.store.task_update(address, task, generation, "completed", op_id="op-done",
                                      result="two findings", epoch=epoch, turn=turn, calls=calls)
        self.assertTrue(done["ok"], done)
        row = self.store.task_list("operator")[0]
        self.assertEqual(row["calls"], {"records": 1, "dropped": False})  # counts only
        full = self.store.task_calls("operator", task)
        self.assertTrue(full["ok"])
        self.assertEqual(full["calls"]["records"][0]["target"], "cli/tool.py")

    def test_task_update_without_calls_leaves_no_log(self):
        address, epoch, turn, task, generation = self.open_claim()
        done = self.store.task_update(address, task, generation, "completed", op_id="op-done",
                                      result="done", epoch=epoch, turn=turn)
        self.assertTrue(done["ok"], done)
        row = self.store.task_list("operator")[0]
        self.assertNotIn("calls", row)
        self.assertEqual(self.store.task_calls("operator", task)["calls"],
                         {"records": [], "dropped": False})

    def test_task_calls_visibility(self):
        address, _epoch, _turn, task, _generation = self.open_claim()
        self.assertTrue(self.store.task_calls("operator", task)["ok"])
        self.assertTrue(refuse(self.store.task_calls("claude@node-1", "k-none"), "bad_input"))
        other = register(self.store, "agy")
        assigned = self.store.task_create("operator", "theirs", op_id="op-theirs",
                                          assignee=other)["id"]
        self.assertTrue(refuse(self.store.task_calls(address, assigned), "invisible_task"))

    def test_in_progress_tasks_lists_only_the_owners_rows(self):
        first = register(self.store, "claude")
        second = register(self.store, "agy", node="node-2")
        epoch = make_ready(self.store, first)
        turn = open_turn(self.store, first, epoch)
        mine = self.store.task_create("operator", "mine", op_id="op-a", assignee=first)["id"]
        open_one = self.store.task_create("operator", "open", op_id="op-b")["id"]
        for task in (mine, open_one):
            self.assertTrue(self.store.task_claim(first, task, op_id=f"op-c{task}", epoch=epoch,
                                                  turn=turn)["ok"])
        rows = self.store.in_progress_tasks([first])
        self.assertEqual(sorted(row["id"] for row in rows), sorted([mine, open_one]))
        self.assertEqual({row["owner"] for row in rows}, {first})
        self.assertEqual({row["project"] for row in rows}, {None})
        self.assertEqual(self.store.in_progress_tasks([second]), [])
        self.assertEqual(self.store.in_progress_tasks([]), [])

    def test_a_hub_release_with_a_generation_fences(self):
        address, epoch, turn, task, generation = self.open_claim()
        self.assertTrue(refuse(self.store.task_release("hub", task, generation - 1,
                                                       "snapshot_failed: late"), "task_released"))
        row = self.store.task_list("operator")[0]
        self.assertEqual(row["status"], "in_progress")  # nothing was released
        released = self.store.task_release("hub", task, generation, "snapshot_failed: now")
        self.assertTrue(released["ok"], released)
        row = self.store.task_list("operator")[0]
        self.assertEqual((row["status"], row["generation"]), ("pending", generation + 1))
        self.assertTrue(row["reason"].startswith("snapshot_failed"))


class GuardianTasks(SnapshotCase):
    """The operator CLI: --task-create pins the project, --task-calls prints the stored log."""

    def namespace(self, **kwargs):
        fields = {"resident_add": None, "resident_remove": None, "residents": False,
                  "mail_send": None, "mail_list": None, "tasks": False, "mail_relay": False,
                  "task_create": None, "task_detail": None, "task_assignee": None,
                  "task_blocked_by": None, "task_project": None, "task_sha": None,
                  "task_calls": None, "task_cancel": None, "task_reason": None, "op_id": None,
                  "json": False}
        fields.update(kwargs)
        return SimpleNamespace(**fields)

    def test_task_create_pins_the_project_head(self):
        import guardian

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertTrue(guardian.handle_resident_commands(
                self.namespace(task_create="review the mail store", task_project="projx",
                               op_id="cli-1")))
        self.assertIn("Created", out.getvalue())
        row = mail_store.MailStore().task_list("operator")[0]
        self.assertEqual((row["project"], row["sha"]), ("projx", self.sha))

    def test_task_create_with_a_bad_project_exits_two(self):
        import guardian

        with patch("sys.stderr", new_callable=io.StringIO) as err, \
                patch.object(ProjectManager, "load",
                             side_effect=ProjectValidationError("no project 'ghost'")):
            with self.assertRaises(SystemExit) as caught:
                guardian.handle_resident_commands(self.namespace(task_create="x",
                                                                 task_project="ghost"))
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("bad_project", err.getvalue())
        self.assertEqual(mail_store.MailStore().task_list("operator"), [])  # nothing was stored

    def test_task_calls_prints_the_stored_log(self):
        import guardian

        store = mail_store.MailStore()
        address = register(store)
        epoch = make_ready(store, address)
        turn = open_turn(store, address, epoch)
        task = store.task_create("operator", "work", op_id="op-1", project="projx",
                                 sha=self.sha)["id"]
        generation = store.task_claim(address, task, op_id="op-2", epoch=epoch, turn=turn)["generation"]
        self.assertTrue(store.task_update(address, task, generation, "completed", op_id="op-3",
                                          result="done", epoch=epoch, turn=turn,
                                          calls={"records": [dict(FilterCalls.RECORD)],
                                                 "dropped": False})["ok"])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertTrue(guardian.handle_resident_commands(self.namespace(task_calls=task)))
        printed = json.loads(out.getvalue())
        self.assertEqual(printed["records"][0]["target"], "cli/tool.py")


if __name__ == "__main__":
    unittest.main()

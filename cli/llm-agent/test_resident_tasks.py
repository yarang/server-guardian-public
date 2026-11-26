"""The resident task table and repo router of one node (docs/design/resident-agents.md, M2e).

A real W2 snapshot is built from a temp git repository and imported into a temp snapshot root, so the
router reads real committed files. No ssh, no model, no network; the hub side of the frames is not
here (test_resident_node.py drives those through the daemon).
"""

import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict
from unittest.mock import patch

import worker
from repo_snapshot import build_snapshot, import_stream
from repo_tools import RepoTools
from resident_tasks import (RepoRouter, TaskTable, calls_path, parse_owned, pinned_keys, read_calls)
from test_repo_tools import git, make_repo
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class ResidentTasksCase(unittest.TestCase):
    """A temp agent directory, a task table and a real imported snapshot."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agents = self.root / "agents"
        self.agent = self.agents / "claude"
        self.agent.mkdir(parents=True, mode=0o700)
        self.repo = make_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        streams = self.root / "streams"
        streams.mkdir(mode=0o700)
        self.snap = build_snapshot(RepoTools(self.repo, self.sha), streams)
        self.store = self.root / "worker" / "snapshots"
        with open(self.snap.path, "rb") as source:
            self.commit = import_stream(self.store, self.snap.key, source)
        self.table = TaskTable.load(self.agent)

    def router(self, wait_seconds: float = 0.05) -> RepoRouter:
        return RepoRouter(self.store, wait_seconds=wait_seconds)

    def read(self, table: TaskTable, task: str = "k-1", **extra: Any) -> Dict[str, Any]:
        return self.router(2.0).call(self.agent, table, "repo_read",
                                     {"task": task, "path": "apps/agent/notes.txt", **extra})


class RouterTests(ResidentTasksCase):
    def test_a_call_for_a_task_the_table_does_not_hold(self) -> None:
        self.assertEqual(self.router().call(self.agent, self.table, "repo_read",
                                            {"task": "k-1", "path": "apps/agent/notes.txt"}),
                         {"ok": False, "error": "no_task"})

    def test_a_task_without_a_project_has_no_repo(self) -> None:
        self.table.claimed("k-1", 1, "none", None)
        self.assertEqual(self.router().call(self.agent, self.table, "repo_list", {"task": "k-1"}),
                         {"ok": False, "error": "no_project"})

    def test_a_snapshot_the_node_does_not_hold_is_missing(self) -> None:
        self.table.set_state("k-1", 1, "ready", "b" * 64)
        self.assertEqual(self.router().call(self.agent, self.table, "repo_list", {"task": "k-1"}),
                         {"ok": False, "error": "snapshot_missing"})

    def test_bad_task_ids_never_become_file_names(self) -> None:
        for bad in ("../x", "a/b", "", "a b", "x" * 65):
            self.assertEqual(self.router().call(self.agent, self.table, "repo_read",
                                                {"task": bad, "path": "apps/agent/notes.txt"}),
                             {"ok": False, "error": "bad_input"}, bad)
        self.assertEqual(self.router().call(self.agent, self.table, "repo_read",
                                            {"path": "apps/agent/notes.txt"}),
                         {"ok": False, "error": "bad_input"})  # no task at all
        self.assertFalse((self.agent / "tasks").exists())

    def test_a_ready_call_reads_the_snapshot_and_appends_to_the_task_log(self) -> None:
        self.table.set_state("k-1", 2, "ready", self.snap.key)
        reply = self.read(self.table)
        self.assertTrue(reply["ok"], reply)
        self.assertIn("alpha", reply["result"]["content"][0]["text"])
        self.assertFalse(reply["result"]["isError"])
        log = calls_path(self.agent, "k-1")
        self.assertTrue(log.exists())
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        calls = read_calls(self.agent, "k-1")
        self.assertFalse(calls["dropped"])
        self.assertEqual(calls["records"][0]["op"], "read")
        self.assertEqual(calls["records"][0]["paths"], ["apps/agent/notes.txt"])

    def test_the_log_is_reused_across_calls_of_the_same_task(self) -> None:
        self.table.set_state("k-1", 1, "ready", self.snap.key)
        router = self.router(2.0)
        for _ in range(2):
            self.assertTrue(router.call(self.agent, self.table, "repo_grep",
                                        {"task": "k-1", "pattern": "TARGET"})["ok"])
        records = read_calls(self.agent, "k-1")["records"]
        self.assertEqual([record["op"] for record in records], ["grep", "grep"])


class WaitTests(ResidentTasksCase):
    def call_in_a_thread(self, router: RepoRouter, table: TaskTable) -> Dict[str, Any]:
        box: Dict[str, Any] = {}

        def run() -> None:
            box["reply"] = router.call(self.agent, table, "repo_read",
                                       {"task": "k-1", "path": "apps/agent/notes.txt"})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5.0)
        time.sleep(0.2)  # the call is inside wait_ready by now
        return box

    def await_reply(self, box: Dict[str, Any]) -> Dict[str, Any]:
        deadline = time.monotonic() + 8.0
        while "reply" not in box:
            if time.monotonic() > deadline:
                self.fail("the waiting call never answered")
            time.sleep(0.02)
        return box["reply"]

    def test_a_pending_call_wakes_when_the_snapshot_becomes_ready(self) -> None:
        self.table.claimed("k-1", 1, "pending", None)
        box = self.call_in_a_thread(self.router(2.0), self.table)
        self.table.set_state("k-1", 1, "ready", self.snap.key)
        reply = self.await_reply(box)
        self.assertTrue(reply["ok"], reply)
        self.assertIn("alpha", reply["result"]["content"][0]["text"])

    def test_a_pending_call_without_a_ready_snapshot_times_out(self) -> None:
        self.table.claimed("k-1", 1, "pending", None)
        self.assertEqual(self.router(0.05).call(self.agent, self.table, "repo_list", {"task": "k-1"}),
                         {"ok": False, "error": "snapshot_pending"})

    def test_a_release_while_waiting_ends_the_call(self) -> None:
        self.table.claimed("k-1", 1, "pending", None)
        box = self.call_in_a_thread(self.router(2.0), self.table)
        self.table.set_state("k-1", 1, "released", None)
        self.assertEqual(self.await_reply(box), {"ok": False, "error": "task_released"})


class TableTests(ResidentTasksCase):
    def test_a_task_state_for_an_older_generation_is_ignored(self) -> None:
        self.table.set_state("k-1", 3, "ready", self.snap.key)
        self.table.set_state("k-1", 2, "released", None)
        self.assertEqual(self.table.entry("k-1"),
                         {"generation": 3, "state": "ready", "key": self.snap.key})
        self.table.set_state("k-1", 4, "pending", None)  # a newer generation replaces the entry
        self.assertEqual(self.table.entry("k-1")["state"], "pending")

    def test_released_removes_the_entry_and_its_log(self) -> None:
        self.table.set_state("k-1", 1, "ready", self.snap.key)
        self.read(self.table)
        self.assertTrue(calls_path(self.agent, "k-1").exists())
        self.table.set_state("k-1", 1, "released", None)
        self.assertIsNone(self.table.entry("k-1"))
        self.assertFalse(calls_path(self.agent, "k-1").exists())
        self.assertEqual(self.router().call(self.agent, self.table, "repo_read",
                                            {"task": "k-1", "path": "apps/agent/notes.txt"}),
                         {"ok": False, "error": "no_task"})

    def test_a_ready_frame_before_the_claim_reply_stays_ready(self) -> None:
        # the reader thread may process task_state ready while the claim reply is still in flight
        self.table.set_state("k-1", 4, "ready", self.snap.key)
        self.table.claimed("k-1", 4, "pending", None)
        self.assertEqual(self.table.entry("k-1"),
                         {"generation": 4, "state": "ready", "key": self.snap.key})
        reply = self.read(self.table)  # and the router answers from the snapshot
        self.assertTrue(reply["ok"], reply)

    def test_a_newer_generation_still_replaces_a_ready_entry(self) -> None:
        self.table.set_state("k-1", 2, "ready", self.snap.key)
        self.table.claimed("k-1", 3, "pending", None)
        self.assertEqual(self.table.entry("k-1"),
                         {"generation": 3, "state": "pending", "key": None})

    def test_the_tasks_frame_drops_absent_tasks_and_their_logs(self) -> None:
        self.table.set_state("k-1", 1, "ready", self.snap.key)
        self.table.claimed("k-2", 1, "pending", None)
        self.read(self.table)
        self.assertTrue(calls_path(self.agent, "k-1").exists())
        self.table.replace({"k-2": {"generation": 1, "state": "pending", "key": None}})
        self.assertEqual(sorted(self.table.entries()), ["k-2"])
        self.assertFalse(calls_path(self.agent, "k-1").exists())
        self.assertEqual(json.loads((self.agent / "tasks.json").read_text(encoding="utf-8")),
                         {"k-2": {"generation": 1, "state": "pending", "key": None}})

    def test_drop_removes_the_entry_and_the_log(self) -> None:
        self.table.set_state("k-1", 1, "ready", self.snap.key)
        self.read(self.table)
        self.table.drop("k-1")
        self.assertIsNone(self.table.entry("k-1"))
        self.assertFalse(calls_path(self.agent, "k-1").exists())

    def test_the_table_survives_a_reload_and_a_broken_file(self) -> None:
        self.table.set_state("k-1", 7, "ready", self.snap.key)
        again = TaskTable.load(self.agent)
        self.assertEqual(again.entry("k-1"),
                         {"generation": 7, "state": "ready", "key": self.snap.key})
        (self.agent / "tasks.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(TaskTable.load(self.agent).entries(), {})

    def test_parse_owned_accepts_only_well_formed_maps(self) -> None:
        good = {"k-1": {"generation": 2, "state": "ready", "key": self.snap.key}}
        self.assertEqual(parse_owned(good), good)
        for bad in ("nope", None, {"k-1": "x"},
                    {"../x": {"generation": 1, "state": "none", "key": None}},
                    {"k-1": {"generation": "two", "state": "none", "key": None}},
                    {"k-1": {"generation": 1, "state": "lost", "key": None}},
                    {"k-1": {"generation": 1, "state": "ready", "key": None}},
                    {"k-1": {"generation": 1, "state": "ready", "key": "../x"}}):
            self.assertIsNone(parse_owned(bad), bad)


class ReadCallsTests(ResidentTasksCase):
    def test_a_missing_log_is_empty_not_dropped(self) -> None:
        self.assertEqual(read_calls(self.agent, "k-1"), {"records": [], "dropped": False})

    def test_a_log_over_the_limit_is_dropped(self) -> None:
        self.table.set_state("k-1", 1, "ready", self.snap.key)
        self.read(self.table)
        with patch.object(worker, "MAX_CALLS_BYTES", 10):
            self.assertEqual(read_calls(self.agent, "k-1"), {"records": [], "dropped": True})


class PinnedKeysTests(unittest.TestCase):
    def write(self, agent: str, payload: Any) -> None:
        path = self.root / "agents" / agent
        path.mkdir(parents=True, mode=0o700)
        (path / "tasks.json").write_text(json.dumps(payload), encoding="utf-8")

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.first = "a" * 64
        self.second = "b" * 64

    def test_every_agents_keys_pin_and_a_broken_file_pins_nothing(self) -> None:
        self.write("claude", {"k-1": {"generation": 1, "state": "ready", "key": self.first},
                              "k-2": {"generation": 1, "state": "pending", "key": None}})
        self.write("agy", {"k-9": {"generation": 1, "state": "ready", "key": self.second}})
        broken = self.root / "agents" / "codex"
        broken.mkdir(mode=0o700)
        (broken / "tasks.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(pinned_keys(self.root / "agents"), {self.first, self.second})

    def test_no_agents_root_pins_nothing_and_creates_nothing(self) -> None:
        self.assertEqual(pinned_keys(self.root / "agents"), frozenset())
        self.assertFalse((self.root / "agents").exists())
        with patch.dict(os.environ, {"GUARDIAN_WORKER_DIR": str(self.root)}):
            self.assertEqual(worker.pinned_snapshot_keys(), frozenset())
        self.assertFalse((self.root / "agents").exists())

    def test_the_worker_wrapper_reads_the_pinned_worker_dir(self) -> None:
        self.write("claude", {"k-1": {"generation": 1, "state": "ready", "key": self.first}})
        with patch.dict(os.environ, {"GUARDIAN_WORKER_DIR": str(self.root)}):
            self.assertEqual(worker.pinned_snapshot_keys(), {self.first})


if __name__ == "__main__":
    unittest.main()

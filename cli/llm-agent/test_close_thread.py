import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import action_ops
from db_manager import CLOSED_STATUS, DecisionDBManager
from jev_gate import PASSED

HERE = Path(__file__).resolve().parent
TRANSCRIPT = [{"round": 1, "agent": "A", "role": "Domain Expert", "content": "hello", "provider": "x"}]


class CloseThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = DecisionDBManager(self.root / "d.db")
        self.db.save_debate_session("p", "t1", "task one", TRANSCRIPT, {"approved": False})
        self.db.save_debate_session("p", "t2", "task two", TRANSCRIPT, {"approved": True})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_close_keeps_records_and_is_idempotent(self) -> None:
        self.db.add_action("t1", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "r", "PROPOSED")
        self.db.save_agent_session("t1", "Critic", "claude", "sess-1")
        first = self.db.close_thread("t1")
        self.assertEqual((first["status"], first["already_closed"], first["previous_status"]),
                         (CLOSED_STATUS, False, "REVISION_REQUIRED"))
        self.assertTrue(first["closed_at"])
        again = self.db.close_thread("t1")
        self.assertTrue(again["already_closed"])
        self.assertEqual(again["closed_at"], first["closed_at"])
        history = self.db.get_thread_history("t1")
        self.assertEqual(history["status"], CLOSED_STATUS)
        self.assertEqual(len(history["transcript"]), 1)
        self.assertEqual(len(self.db.list_actions("t1")), 1)
        self.assertEqual(self.db.get_agent_session("t1", "Critic")["session_id"], "sess-1")

    def test_unknown_thread(self) -> None:
        self.assertIsNone(self.db.close_thread("nope"))
        code, out = action_ops.close_thread_json(self.db, "nope")
        self.assertEqual((code, out["ok"]), (action_ops.NOT_FOUND, False))
        self.assertIn("not found", out["error"])

    def test_listing_marks_status_and_can_exclude_closed(self) -> None:
        self.db.close_thread("t1")
        everything = {t["thread_id"]: t for t in self.db.list_project_threads("p")}
        self.assertEqual(everything["t1"]["status"], CLOSED_STATUS)
        self.assertEqual(everything["t2"]["status"], "APPROVED")
        open_only = self.db.list_project_threads("p", include_closed=False)
        self.assertEqual([t["thread_id"] for t in open_only], ["t2"])

    def test_new_debate_reopens_the_thread(self) -> None:
        self.db.close_thread("t1")
        self.db.save_debate_session("p", "t1", "task one", TRANSCRIPT, {"approved": True})
        history = self.db.get_thread_history("t1")
        self.assertEqual(history["status"], "APPROVED")
        self.assertIsNone(history["closed_at"])

    def test_closed_thread_ids_follow_the_database(self) -> None:
        self.assertEqual(action_ops.closed_threads_json(self.db), (0, {"ok": True, "threads": []}))
        self.db.close_thread("t1")
        self.db.close_thread("t2")
        self.assertEqual(self.db.closed_thread_ids(), ["t1", "t2"])
        self.db.save_debate_session("p", "t1", "task one", TRANSCRIPT, {"approved": True})
        self.assertEqual(action_ops.closed_threads_json(self.db), (0, {"ok": True, "threads": ["t2"]}))

    def test_recheck_does_not_reopen(self) -> None:
        self.db.save_debate_session("p", "t3", "task", [], {"approved": False, "jev_gate": "PENDING"})
        self.db.save_jev_review("t3", 1, {"gate": "PENDING", "reasons": [], "jev_decision_id": 3}, {"title": "x"})
        self.db.close_thread("t3")

        class Gate:
            def recheck(self, decision_id, request):
                return {"gate": PASSED, "reasons": [], "jev_decision_id": 3, "eval_status": "done", "evaluation": {}}

        outcome = action_ops.recheck_thread(self.db, "t3", Gate())
        self.assertEqual(outcome["gate"], PASSED)
        self.assertEqual(self.db.get_thread_status("t3"), CLOSED_STATUS)

    def test_existing_database_gains_closed_at(self) -> None:
        path = self.root / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_prompt TEXT NOT NULL,"
                     " status TEXT NOT NULL DEFAULT 'IN_PROGRESS', consensus_reached INTEGER DEFAULT 0,"
                     " final_round INTEGER DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO threads (thread_id, project_id, task_prompt) VALUES ('t9', 'p', 'old')")
        conn.commit()
        conn.close()
        db = DecisionDBManager(path)
        self.assertEqual(db.close_thread("t9")["previous_status"], "IN_PROGRESS")


class CloseThreadCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.env = {**os.environ, "GUARDIAN_DATA_DIR": self._tmp.name, "GUARDIAN_WORKSPACE": self._tmp.name}
        self.db = DecisionDBManager(Path(self._tmp.name) / "guardian_decisions.db")
        self.db.save_debate_session("server-guardian", "t1", "task", [], {"approved": False})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_json_contract(self) -> None:
        done = self._run("--close-thread", "t1", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        body = json.loads(done.stdout)
        self.assertEqual((body["ok"], body["status"], body["already_closed"]), (True, "CLOSED", False))
        again = json.loads(self._run("--close-thread", "t1", "--json").stdout)
        self.assertTrue(again["already_closed"])
        missing = self._run("--close-thread", "t_missing", "--json")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("not found", json.loads(missing.stdout)["error"])

    def test_plain_output(self) -> None:
        done = self._run("--close-thread", "t1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("closed", done.stdout)
        missing = self._run("--close-thread", "t_missing")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("not found", missing.stderr)


if __name__ == "__main__":
    unittest.main()

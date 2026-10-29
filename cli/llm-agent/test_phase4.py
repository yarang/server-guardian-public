"""Phase-4 fixes: proposal base commit, workspace lock vs actions, published threads, release, retired tool."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import action_ops
import publish_ops
import workspace_ops
from mcp_client import MCPClient
from project_registry import WorkspaceBusy, workspace_lock
from db_manager import DecisionDBManager
from test_publish_ops import WRITE, PublishCase, git

HERE = Path(__file__).resolve().parent


class ProposalBaseTests(PublishCase):
    def test_file_proposal_without_code_context_records_head(self) -> None:
        mcp = self.workspaces.for_project("demo")
        stored = action_ops.record_proposals(
            self.db, mcp, "t1", 1,
            [{"tool": WRITE, "arguments": {"file_path": "docs/new.md", "content": "# new\n"}, "reason": "r"}])
        self.assertEqual(stored[0]["status"], "PROPOSED", stored)
        action = self.db.get_action(stored[0]["id"])
        self.assertEqual(action["base_sha"], self.base)
        result = action_ops.run_action(self.db, self.workspaces, action["id"], lambda a: True, actor="op")
        self.assertEqual(result["status"], "SUCCESS", result)
        plan = publish_ops.publish_plan_json(self.db, self.workspaces, "t1")
        self.assertEqual(plan[0], 0, plan)

    def test_stale_base_still_blocks(self) -> None:
        mcp = self.workspaces.for_project("demo")
        stored = action_ops.record_proposals(
            self.db, mcp, "t1", 1,
            [{"tool": WRITE, "arguments": {"file_path": "docs/new.md", "content": "# new\n"}, "reason": "r"}])
        (self.ws / "other.txt").write_text("x")
        git(self.ws, "add", "other.txt")
        git(self.ws, "commit", "-q", "-m", "move head")
        with self.assertRaises(RuntimeError) as ctx:
            action_ops.run_action(self.db, self.workspaces, stored[0]["id"], lambda a: True, actor="op")
        self.assertIn("workspace moved", str(ctx.exception))


class ActionWorkspaceLockTests(PublishCase):
    def test_running_actions_block_reset_plan_and_apply(self) -> None:
        running = self.db.add_action("t1", 1, WRITE, {"file_path": "docs/new.md", "content": "x\n"}, "r", "RUNNING",
                                     base_sha=self.base, project_id="demo")
        (self.ws / "junk.txt").write_text("x")
        for result in (workspace_ops.reset_plan_json(self.db, self.workspaces, "demo"),
                       workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op")):
            self.assertEqual(result[0], 4, result)
            self.assertIn(f"actions are running in this project: #{running}", result[1]["error"])
        self.assertTrue((self.ws / "junk.txt").exists())
        self.db.update_action(running, "FAILED")
        self.assertEqual(workspace_ops.reset_plan_json(self.db, self.workspaces, "demo")[0], 0)

    def test_file_action_holds_the_workspace_lock_while_it_runs(self) -> None:
        seen = {}
        real = MCPClient.call_tool

        def probe(client, name, arguments, **kwargs):
            try:
                with workspace_lock(client.workspace, "busy"):
                    seen["free"] = True
            except WorkspaceBusy:
                seen["free"] = False
            return real(client, name, arguments, **kwargs)

        action = self.db.add_action("t1", 1, WRITE, {"file_path": "docs/new.md", "content": "# new\n"}, "r",
                                    "PROPOSED", base_sha=self.base, project_id="demo")
        with patch.object(MCPClient, "call_tool", probe):
            result = action_ops.run_action(self.db, self.workspaces, action, lambda a: True, actor="op")
        self.assertEqual((result["status"], seen["free"]), ("SUCCESS", False))
        # Released afterwards: publish and reset can take it again.
        with workspace_lock(self.ws, "busy"):
            pass
        self.assertEqual(publish_ops.publish_plan_json(self.db, self.workspaces, "t1")[0], 0)

    def test_busy_workspace_refuses_the_action_before_it_runs(self) -> None:
        action = self.db.add_action("t1", 1, WRITE, {"file_path": "docs/new.md", "content": "# new\n"}, "r",
                                    "PROPOSED", base_sha=self.base, project_id="demo")
        with workspace_lock(self.ws, "busy"):
            code, out = action_ops.run_action_json(self.db, self.workspaces, action, "op")
        self.assertEqual(code, 4, out)
        self.assertIn("another publish, project sync or workspace reset", out["error"])
        self.assertEqual(self.db.get_action(action)["status"], "PROPOSED")
        self.assertFalse((self.ws / "docs" / "new.md").exists())

    def test_non_file_tool_does_not_take_the_lock(self) -> None:
        action = self.db.add_action("t1", 1, "jev_mcp:run_tests", {"command": "python3 -m unittest discover -s tests -q"},
                                    "r", "PROPOSED", base_sha=self.base, project_id="demo")
        with workspace_lock(self.ws, "busy"):
            code, out = action_ops.run_action_json(self.db, self.workspaces, action, "op")
        self.assertNotIn("another publish", str(out.get("error", "")), out)


class PublishedThreadTests(PublishCase):
    def cli(self, *args: str):
        proc = subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], env=dict(os.environ),
                              capture_output=True, text=True, timeout=120)
        return proc

    def test_published_threads_list_and_refusal_text(self) -> None:
        self.db.add_publication("t1", "demo", "guardian/t1", "abc", "r", "FAILED", "op", error="push failed")
        self.assertEqual(action_ops.published_threads_json(self.db), (0, {"ok": True, "threads": []}))
        self.assertIsNone(action_ops.published_refusal(self.db, "t1"))
        self.db.add_publication("t1", "demo", "guardian/t1", "abc", "r", "PUSHED", "op")
        self.assertEqual(action_ops.published_threads_json(self.db)[1]["threads"], ["t1"])
        self.assertEqual(action_ops.published_refusal(self.db, "t1"),
                         "thread t1 was published as branch guardian/t1; start a new thread")

    def test_cli_lists_published_threads_and_refuses_to_continue_one(self) -> None:
        db = DecisionDBManager()  # the default database under GUARDIAN_DATA_DIR, which the CLI uses too
        db.save_debate_session("demo", "thread_pub", "task", [], {"approved": True, "decision": "d"})
        db.save_debate_session("demo", "thread_open", "task", [], {"approved": True, "decision": "d"})
        db.add_publication("thread_pub", "demo", "guardian/thread_pub", "abc", "r", "PUSHED", "op")
        proc = self.cli("--published-threads", "--json")
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["threads"]), (0, ["thread_pub"]), proc.stderr)
        proc = self.cli("--debate", "more work", "--thread", "thread_pub")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("thread thread_pub was published as branch guardian/thread_pub; start a new thread", proc.stderr)


class ReleaseTests(PublishCase):
    def two_threads(self) -> tuple:
        self.db.save_debate_session("demo", "t2", "other work", [], {"approved": True, "decision": "other"})
        a = self.act(WRITE, {"file_path": "docs/one.md", "content": "# one\n"}, thread="t1")
        b = self.act(WRITE, {"file_path": "docs/two.md", "content": "# two\n"}, thread="t2")
        return a, b

    def test_plan_groups_keep_by_thread(self) -> None:
        a, b = self.two_threads()
        code, plan = workspace_ops.reset_plan_json(self.db, self.workspaces, "demo")
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["keep"], [{"path": "docs/one.md", "action_id": a, "thread_id": "t1"},
                                        {"path": "docs/two.md", "action_id": b, "thread_id": "t2"}])

    def test_release_discards_that_threads_files_and_records_it(self) -> None:
        a, b = self.two_threads()
        code, out = workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op", release=["t2"])
        self.assertEqual(code, 0, out)
        self.assertEqual((out["discarded"], out["released"]), (["docs/two.md"], ["t2"]))
        self.assertTrue((self.ws / "docs" / "one.md").exists())
        self.assertFalse((self.ws / "docs" / "two.md").exists())
        released = self.db.get_action(b)
        self.assertEqual(released["status"], "EXECUTED")
        self.assertEqual(released["result"]["released_by"], "op")
        self.assertRegex(released["result"]["released_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("file_hashes", released["result"])
        self.assertNotIn("released_at", self.db.get_action(a)["result"])
        # The released thread's files are not kept any more, and it can never be published.
        self.assertEqual(workspace_ops.reset_plan_json(self.db, self.workspaces, "demo")[1]["keep"],
                         [{"path": "docs/one.md", "action_id": a, "thread_id": "t1"}])
        with self.assertRaises(publish_ops.PublishRefused) as ctx:
            publish_ops.publish_thread(self.db, self.workspaces, "t2", "op", remote=str(self.remote))
        self.assertIn("was released", str(ctx.exception))
        self.assertEqual(publish_ops.publish_plan_json(self.db, self.workspaces, "t2")[0], 4)

    def test_release_refuses_a_thread_without_kept_files(self) -> None:
        self.two_threads()
        (self.ws / "junk.txt").write_text("x")
        code, out = workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op", release=["t2", "nope"])
        self.assertEqual(code, 4, out)
        self.assertIn("nothing to release for nope", out["error"])
        self.assertTrue((self.ws / "junk.txt").exists())  # nothing was discarded or marked
        self.assertNotIn("released_at", self.db.list_actions("t2")[0]["result"])
        self.assertEqual(workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op", release="t2")[0], 2)

    def test_published_thread_cannot_be_released(self) -> None:
        self.two_threads()
        self.db.add_publication("t2", "demo", "guardian/t2", "abc", "r", "PUSHED", "op")
        code, out = workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op", release=["t2"])
        self.assertEqual(code, 4, out)

    def test_cli_release_flag(self) -> None:
        proc = subprocess.run([sys.executable, str(HERE / "guardian.py"), "--workspace-reset", "demo", "--plan",
                               "--release", "t1", "--json"], env=dict(os.environ), capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--release applies to --yes", proc.stdout)


class RetiredToolMigrationTests(PublishCase):
    def test_undecided_git_commit_push_rows_become_invalid_once(self) -> None:
        retired = "jev_mcp:git_commit_push"
        rows = {status: self.db.add_action("t1", 1, retired, {"branch": "x/y", "message": "m"}, "r", status,
                                           base_sha=self.base, project_id="demo")
                for status in ("PROPOSED", "HELD", "REJECTED")}
        live = self.db.add_action("t1", 1, WRITE, {"file_path": "a.md", "content": "x"}, "r", "PROPOSED",
                                  base_sha=self.base, project_id="demo")
        db = DecisionDBManager(self.db.db_path)  # opening the database runs the migration
        self.assertEqual(db.get_action(rows["PROPOSED"])["status"], "INVALID")
        self.assertEqual(db.get_action(rows["HELD"])["status"], "INVALID")
        self.assertEqual(db.get_action(rows["PROPOSED"])["result"], {"error": "git_commit_push was retired; use publish"})
        self.assertEqual(db.get_action(rows["REJECTED"])["status"], "REJECTED")
        self.assertEqual(db.get_action(live)["status"], "PROPOSED")


if __name__ == "__main__":
    unittest.main()

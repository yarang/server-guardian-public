import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import workspace_ops
from test_publish_ops import APPLY, WRITE, PublishCase, git

HERE = Path(__file__).resolve().parent


class WorkspaceResetTests(PublishCase):
    def stray(self) -> None:
        (self.ws / "app.py").write_text("VALUE = 99\n")  # modified tracked file
        (self.ws / "tests" / "test_app.py").unlink()  # deleted tracked file
        (self.ws / "junk.txt").write_text("x")  # untracked

    def test_plan_splits_keep_and_discard(self) -> None:
        action = self.act(WRITE, {"file_path": "docs/new.md", "content": "# new\n"})
        self.stray()
        code, plan = workspace_ops.reset_plan_json(self.db, self.workspaces, "demo")
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["keep"], [{"path": "docs/new.md", "action_id": action, "thread_id": "t1"}])
        self.assertEqual(plan["discard"], [{"path": "app.py", "state": "modified"},
                                           {"path": "junk.txt", "state": "untracked"},
                                           {"path": "tests/test_app.py", "state": "deleted"}])
        self.assertEqual((plan["project_id"], plan["workspace"]), ("demo", str(self.ws)))

    def test_apply_discards_only_the_discard_list(self) -> None:
        self.act(WRITE, {"file_path": "docs/new.md", "content": "# new\n"})
        self.stray()
        (self.ws / "keepdir").mkdir()
        code, out = workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["discarded"], ["app.py", "junk.txt", "tests/test_app.py"])
        self.assertEqual(git(self.ws, "status", "--porcelain", "--untracked-files=all"), "?? docs/new.md")
        self.assertEqual((self.ws / "docs" / "new.md").read_text(), "# new\n")
        self.assertEqual((self.ws / "app.py").read_text(), "VALUE = 1\n")

    def test_published_thread_files_are_discardable(self) -> None:
        self.act(WRITE, {"file_path": "docs/new.md", "content": "# new\n"})
        self.db.add_publication("t1", "demo", "guardian/t1", "abc", "r", "PUSHED", "op")
        code, plan = workspace_ops.reset_plan_json(self.db, self.workspaces, "demo")
        self.assertEqual((code, plan["keep"], plan["discard"]),
                         (0, [], [{"path": "docs/new.md", "state": "untracked"}]))

    def test_staged_new_file_is_unstaged_and_removed(self) -> None:
        (self.ws / "staged.txt").write_text("s")
        git(self.ws, "add", "staged.txt")
        code, out = workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op")
        self.assertEqual((code, out["discarded"]), (0, ["staged.txt"]))
        self.assertEqual(git(self.ws, "status", "--porcelain", "--untracked-files=all"), "")

    def test_refusals(self) -> None:
        self.assertEqual(workspace_ops.reset_plan_json(self.db, self.workspaces, "nope")[0], 3)
        self.assertEqual(workspace_ops.reset_plan_json(self.db, self.workspaces, "Bad Id")[0], 2)
        self.assertEqual(workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "bad actor!")[0], 2)
        git(self.ws, "checkout", "-q", "-b", "other")
        (self.ws / "junk.txt").write_text("x")
        for result in (workspace_ops.reset_plan_json(self.db, self.workspaces, "demo"),
                       workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op")):
            self.assertEqual(result[0], 4)
            self.assertIn("not the tracked branch", result[1]["error"])
        self.assertTrue((self.ws / "junk.txt").exists())

    def test_a_trailing_newline_in_an_id_or_actor_is_refused(self) -> None:
        # "$" also matches just before a trailing newline, so only fullmatch refuses "demo\n" / "op\n".
        self.assertEqual(workspace_ops.reset_plan_json(self.db, self.workspaces, "demo\n")[0], 2)
        self.assertEqual(workspace_ops.reset_apply_json(self.db, self.workspaces, "demo\n", "op")[0], 2)
        self.assertEqual(workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op\n")[0], 2)

    def test_busy_workspace_is_a_conflict(self) -> None:
        from project_registry import workspace_lock

        (self.ws / "junk.txt").write_text("x")
        with workspace_lock(self.ws, "busy"):
            code, out = workspace_ops.reset_apply_json(self.db, self.workspaces, "demo", "op")
        self.assertEqual(code, 4, out)
        self.assertTrue((self.ws / "junk.txt").exists())


class RetiredToolTests(PublishCase):
    def test_stored_git_commit_push_lists_but_cannot_run(self) -> None:
        import action_ops

        aid = self.db.add_action("t1", 1, "jev_mcp:git_commit_push", {"branch": "x/y", "message": "m"}, "r",
                                 "PROPOSED", base_sha=self.base, project_id="demo")
        self.assertEqual(action_ops.list_actions_json(self.db, self.workspaces, "t1")[0], 0)
        self.assertEqual(action_ops.show_action_json(self.db, self.workspaces, aid)[0], 0)
        code, out = action_ops.run_action_json(self.db, self.workspaces, aid, "op")
        self.assertNotEqual(code, 0)
        self.assertIn("git_commit_push was retired; use publish", out["error"])
        self.assertEqual(self.db.get_action(aid)["status"], "INVALID")
        self.assertEqual(git(self.ws, "branch", "--list", "x/y"), "")


class PublicationsTests(PublishCase):
    def test_newest_first_with_contract_fields(self) -> None:
        self.db.save_debate_session("demo", "thread_pub", "task", [], {"approved": True, "decision": "d"})
        first = self.db.add_publication("thread_pub", "demo", "guardian/a", None, "r", "RUNNING", "op")
        self.db.update_publication(first, status="FAILED", error="push failed")
        second = self.db.add_publication("thread_pub", "demo", "guardian/b", "abc", "r", "PUSHED", "op")
        code, out = workspace_ops.publications_json(self.db, "thread_pub")
        self.assertEqual(code, 0)
        rows = out["publications"]
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual(set(rows[0]), {"id", "branch", "commit_sha", "remote", "status", "error", "actor", "created_at"})
        self.assertEqual(rows[1]["error"], "push failed")
        self.assertEqual(workspace_ops.publications_json(self.db, "thread_missing")[0], 3)
        # The same id pattern as the hub route: anything else is a bad input, not a lookup.
        for bad in ("t1", "missing", "thread_../x", "thread_" + "a" * 81):
            self.assertEqual(workspace_ops.publications_json(self.db, bad)[0], 2, bad)

    def test_a_thread_id_with_a_trailing_newline_is_refused(self) -> None:
        # "thread_x\n" is bad input (2), not an unknown thread (3): "$" also matches before a trailing newline.
        self.assertEqual(workspace_ops.publications_json(self.db, "thread_x")[0], 3)
        self.assertEqual(workspace_ops.publications_json(self.db, "thread_x\n")[0], 2)

    def test_cli_exit_codes(self) -> None:
        env = {**os.environ, "GUARDIAN_DATA_DIR": str(self.root / "cli-data")}
        for args, code in ((["--publications", "thread_nope", "--json"], 3), (["--workspace-reset", "nope", "--plan", "--json"], 3),
                           (["--workspace-reset", "nope", "--json"], 2)):
            proc = subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, code, proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()

import os
import sqlite3
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import action_ops
import publish_ops
from db_manager import DecisionDBManager
from project_registry import Project, Workspaces
from publish_ops import PublishInputError, PublishRefused

APPLY, WRITE = "jev_mcp:apply_patch", "jev_mcp:execute_code_action"
TEST_CMD = "python3 -m unittest discover -s tests -q"
TEST_SOURCE = (
    "import unittest\nfrom pathlib import Path\n\n\nclass AppTest(unittest.TestCase):\n"
    "    def test_app_is_not_broken(self):\n"
    "        self.assertNotIn('BROKEN', Path('app.py').read_text())\n"
)


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.rstrip("\n")


class FixedProjects:
    """Stands in for ProjectManager: the test project, without the home-directory rules of parse_project."""

    def __init__(self, project: Project) -> None:
        self.project = project

    def load(self, project_id):
        if project_id != self.project.id:
            raise ValueError(f"no project {project_id!r}")
        return self.project


class PublishCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # No user or system git configuration (signing, hooks, credential helpers) leaks into the tests.
        env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                      "GUARDIAN_DATA_DIR": str(self.root / "data"),
                                      "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                                      "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
        env.start()
        self.addCleanup(env.stop)

        self.remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.ws = (self.root / "work").resolve()
        self.ws.mkdir()
        git(self.ws, "init", "-q", "-b", "main")
        (self.ws / "app.py").write_text("VALUE = 1\n")
        (self.ws / "tests").mkdir()
        (self.ws / "tests" / "test_app.py").write_text(TEST_SOURCE)
        (self.ws / ".gitignore").write_text("__pycache__/\n")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-q", "-m", "initial")
        git(self.ws, "push", "-q", str(self.remote), "main")
        self.base = git(self.ws, "rev-parse", "HEAD")

        self.project = Project(id="demo", repo="https://git.example.invalid/me/demo.git", branch="main",
                               workspace_raw=str(self.ws), default_test=TEST_CMD)
        self.db = DecisionDBManager(self.root / "d.db")
        self.workspaces = Workspaces(self.db, manager=FixedProjects(self.project))
        self.db.save_debate_session("demo", "t1", "cap the request body", [], {
            "approved": True, "decision": "Cap the request body of the command endpoints at 4 KiB so oversize input "
            "fails before it is read", "judgment": "ignored when a decision exists"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- helpers ---

    def act(self, tool: str, args: dict, thread: str = "t1", run: bool = True, base=None, status: str = "PROPOSED") -> int:
        action_id = self.db.add_action(thread, 1, tool, args, "r", status, base_sha=base or self.base, project_id="demo")
        if run:
            result = action_ops.run_action(self.db, self.workspaces, action_id, lambda a: True, actor="op")
            self.assertEqual(result["status"], "SUCCESS", result)
        return action_id

    def two_actions(self) -> None:
        self.act(APPLY, {"edits": [{"file_path": "app.py", "old": "VALUE = 1", "new": "VALUE = 2"}]})
        self.act(WRITE, {"file_path": "docs/new.md", "content": "# new\n"})

    def publish(self, **kwargs):
        kwargs.setdefault("remote", str(self.remote))
        return publish_ops.publish_thread(self.db, self.workspaces, "t1", "op", **kwargs)

    def assertUntouched(self) -> None:
        """A refusal changed nothing: no branch, no publication row, workspace still on main."""
        self.assertEqual(git(self.ws, "branch", "--list", "guardian/*"), "")
        self.assertEqual(self.db.list_publications("t1"), [])
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD") if self.on_branch() else "", "main" if self.on_branch() else "")
        self.assertEqual(git(self.remote, "branch", "--list"), "* main")

    def on_branch(self) -> bool:
        return subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=self.ws, capture_output=True).returncode == 0

    def refused(self, contains: str, **kwargs) -> PublishRefused:
        with self.assertRaises(PublishRefused) as ctx:
            self.publish(**kwargs)
        self.assertIn(contains, str(ctx.exception))
        return ctx.exception


class PublishSuccessTests(PublishCase):
    def test_pushes_exactly_the_action_files_and_restores_the_workspace(self) -> None:
        self.two_actions()
        result = self.publish()
        self.assertEqual(result["status"], "PUSHED", result)
        self.assertEqual((result["branch"], result["files"]), ("guardian/t1", ["app.py", "docs/new.md"]))
        self.assertEqual(result["tests"]["exit_code"], 0)
        # The remote branch differs from main by exactly the two files, in one commit on the base.
        self.assertEqual(git(self.remote, "diff", "--name-only", "main", "guardian/t1").split(), ["app.py", "docs/new.md"])
        self.assertEqual(git(self.remote, "rev-parse", "guardian/t1"), result["commit_sha"])
        self.assertEqual(git(self.remote, "rev-list", "--count", f"{self.base}..guardian/t1"), "1")
        self.assertEqual(git(self.remote, "show", "guardian/t1:app.py"), "VALUE = 2")
        message = git(self.remote, "log", "-1", "--format=%B", "guardian/t1")
        subject = message.splitlines()[0]
        self.assertTrue(subject.startswith("fix: Cap the request body"), subject)
        self.assertLessEqual(len(subject), 72)
        self.assertIn("fails before it is read", message)
        self.assertTrue(message.endswith("Debate-Thread: t1"), message)
        # The workspace is back on the tracked branch at the base commit, clean (the change lives on the branch).
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.ws, "status", "--porcelain", "--untracked-files=all"), "")
        rows = self.db.list_publications("t1")
        self.assertEqual([(r["status"], r["branch"], r["commit_sha"], r["actor"], r["project_id"]) for r in rows],
                         [("PUSHED", "guardian/t1", result["commit_sha"], "op", "demo")])
        self.assertEqual(rows[0]["remote"], str(self.remote))

    def test_a_published_thread_is_not_published_twice(self) -> None:
        self.two_actions()
        self.publish()
        self.refused("already published")

    def test_operator_branch_name_and_declined_confirmation(self) -> None:
        self.two_actions()
        seen = []
        result = self.publish(branch="feature/cap-body", confirm=lambda plan: seen.append(plan) is None and False)
        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual((seen[0]["branch"], seen[0]["files"]), ("feature/cap-body", ["app.py", "docs/new.md"]))
        self.assertEqual(self.db.list_publications("t1"), [])
        self.assertEqual(git(self.ws, "branch", "--list", "feature/*"), "")
        result = self.publish(branch="feature/cap-body", confirm=lambda plan: True)
        self.assertEqual((result["status"], result["branch"]), ("PUSHED", "feature/cap-body"))
        self.assertEqual(git(self.remote, "rev-parse", "feature/cap-body"), result["commit_sha"])

    def test_sibling_actions_on_one_file_publish_the_final_content(self) -> None:
        self.act(WRITE, {"file_path": "docs/new.md", "content": "# one\n"})
        self.act(APPLY, {"edits": [{"file_path": "docs/new.md", "old": "# one", "new": "# two"}]})
        result = self.publish()
        self.assertEqual((result["status"], result["files"]), ("PUSHED", ["docs/new.md"]))
        self.assertEqual(git(self.remote, "show", "guardian/t1:docs/new.md"), "# two")

    def test_failed_and_rejected_actions_do_not_block(self) -> None:
        self.two_actions()
        self.act(APPLY, {"edits": [{"file_path": "app.py", "old": "x", "new": "y"}]}, run=False, status="REJECTED")
        self.act(APPLY, {"edits": [{"file_path": "app.py", "old": "x", "new": "y"}]}, run=False, status="INVALID")
        self.assertEqual(self.publish()["status"], "PUSHED")


class PublishRefusalTests(PublishCase):
    def test_unknown_thread(self) -> None:
        with self.assertRaises(LookupError):
            publish_ops.publish_thread(self.db, self.workspaces, "nope", "op")

    def test_needs_an_executed_file_action(self) -> None:
        self.refused("no executed file action")
        self.act("jev_mcp:run_tests", {"test_cmd": "pytest"}, run=False, status="EXECUTED")
        self.refused("no executed file action")
        self.assertUntouched()

    def test_undecided_actions_block(self) -> None:
        for status in ("PROPOSED", "HELD", "RUNNING"):
            with self.subTest(status=status):
                self.db.save_debate_session("demo", "t1", "task", [], {"approved": True})
                self.act(WRITE, {"file_path": "docs/a.md", "content": "a\n"}, run=False, status=status)
                self.refused(f"{status}")
                self.assertUntouched()
                for action in self.db.list_actions("t1"):
                    self.db.update_action(action["id"], "REJECTED")

    def test_actions_must_share_one_base_commit(self) -> None:
        self.two_actions()
        self.act(WRITE, {"file_path": "docs/b.md", "content": "b\n"}, run=False, base="f" * 40, status="EXECUTED")
        self.db.update_action(self.db.list_actions("t1")[-1]["id"], "EXECUTED",
                              result={"status": "SUCCESS", "file_hashes": {"docs/b.md": "00"}})
        self.refused("one base commit")
        self.assertUntouched()

    def test_actions_must_share_one_project(self) -> None:
        self.two_actions()
        other = self.db.add_action("t1", 1, WRITE, {"file_path": "x", "content": "x"}, "r", "EXECUTED",
                                   base_sha=self.base, project_id="other")
        self.db.update_action(other, "EXECUTED", result={"status": "SUCCESS", "file_hashes": {"x": "00"}})
        self.refused("different projects")

    def test_executed_action_without_recorded_hashes(self) -> None:
        self.act(WRITE, {"file_path": "docs/a.md", "content": "a\n"}, run=False, status="EXECUTED")
        self.refused("without recorded file hashes")

    def test_project_needs_a_repo(self) -> None:
        self.two_actions()
        self.project.repo = None
        self.refused("no repo URL")
        self.assertUntouched()

    def test_a_project_without_a_default_test_cannot_publish(self) -> None:
        self.two_actions()
        self.project.default_test = None
        self.workspaces._clients.clear()
        self.refused("set tests.default for project demo")
        self.assertUntouched()
        self.assertEqual(publish_ops.publish_thread_json(self.db, self.workspaces, "t1", "op")[0], publish_ops.CONFLICT)

    def test_publish_and_run_test_share_the_default_test(self) -> None:
        self.two_actions()
        seen = []
        real = self.workspaces.for_project("demo").run_default_test

        def spy(*args, **kwargs):
            seen.append((args, kwargs))
            return real(*args, **kwargs)

        with patch.object(self.workspaces.for_project("demo"), "run_default_test", side_effect=spy):
            self.assertEqual(self.publish()["tests"]["cmd"], TEST_CMD)
        self.assertEqual(len(seen), 1)

    def test_branch_names(self) -> None:
        self.two_actions()
        for bad in ("main", "release", "develop", "bad name", "../escape", "-x", "a..b", "x.lock", "dir/"):
            with self.subTest(branch=bad):
                with self.assertRaises(PublishInputError):
                    self.publish(branch=bad)
        self.project.branch = "trunk"
        git(self.ws, "branch", "-m", "trunk")
        with self.assertRaises(PublishInputError):
            self.publish(branch="trunk")
        with self.assertRaises(PublishInputError):  # shaped like a valid name, rejected by git itself
            self.publish(branch="a//b")
        self.assertEqual(self.db.list_publications("t1"), [])
        self.assertEqual(git(self.ws, "branch", "--list", "a*"), "")

    def test_workspace_must_be_on_the_tracked_branch(self) -> None:
        self.two_actions()
        git(self.ws, "checkout", "-q", "-b", "scratch")
        self.refused("not the tracked branch")
        git(self.ws, "checkout", "-q", "--detach")
        self.refused("detached HEAD")
        git(self.ws, "checkout", "-q", "main")
        self.assertEqual(git(self.ws, "branch", "--list", "guardian/*"), "")

    def test_head_must_equal_the_base(self) -> None:
        self.two_actions()
        git(self.ws, "stash", "-u", "-q")
        (self.ws / "other.txt").write_text("o\n")
        git(self.ws, "add", "other.txt")
        git(self.ws, "commit", "-q", "-m", "moved")
        git(self.ws, "stash", "pop", "-q")
        self.refused("moved from")

    def test_changed_paths_must_equal_the_action_files(self) -> None:
        self.two_actions()
        (self.ws / "stray.txt").write_text("s\n")
        error = self.refused("not written by the executed actions: stray.txt")
        (self.ws / "stray.txt").unlink()
        (self.ws / "tests" / "test_app.py").write_text(TEST_SOURCE + "\n")
        self.refused("tests/test_app.py")
        git(self.ws, "checkout", "--", "tests/test_app.py")
        git(self.ws, "checkout", "--", "app.py")  # an action file reverted by hand
        self.refused("not changed in the workspace: app.py")
        self.assertIsNotNone(error)

    def test_a_file_changed_after_its_action_is_refused(self) -> None:
        self.two_actions()
        with open(self.ws / "docs" / "new.md", "a") as handle:
            handle.write("hand edit\n")
        self.refused("changed after the last action wrote them: docs/new.md")

    def test_staged_changes_are_refused(self) -> None:
        self.two_actions()
        git(self.ws, "add", "app.py")
        self.refused("already staged")

    def test_failing_test_gate_refuses_with_output_and_no_side_effects(self) -> None:
        self.act(APPLY, {"edits": [{"file_path": "app.py", "old": "VALUE = 1", "new": "VALUE = 'BROKEN'"}]})
        error = self.refused("test failed")
        tests = error.details["tests"]
        self.assertNotEqual(tests["exit_code"], 0)
        self.assertIn("BROKEN", tests["stdout"] + tests["stderr"])
        self.assertEqual(self.db.list_publications("t1"), [])
        self.assertEqual(git(self.ws, "branch", "--list", "guardian/*"), "")
        self.assertEqual(git(self.ws, "status", "--porcelain"), " M app.py")

    def test_gate_that_changes_files_is_refused(self) -> None:
        self.two_actions()
        original = publish_ops._run_gate

        def noisy(mcp):
            (mcp.workspace / "leftover.txt").write_text("x\n")
            return original(mcp)

        with patch.object(publish_ops, "_run_gate", noisy):
            self.refused("leftover.txt")
        self.assertEqual(git(self.ws, "branch", "--list", "guardian/*"), "")

    def test_one_publish_at_a_time(self) -> None:
        import fcntl

        self.two_actions()
        with open(self.ws / ".git" / "guardian-publish.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.refused("another publish")


class PublishPlanTests(PublishCase):
    def snapshot(self):
        return (git(self.ws, "branch", "--list"), git(self.ws, "status", "--porcelain", "--untracked-files=all"),
                self.db.list_publications("t1"), git(self.remote, "branch", "--list"))

    def test_plan_matches_the_publish_and_writes_nothing(self) -> None:
        self.two_actions()
        before = self.snapshot()
        with patch.object(publish_ops, "_run_gate", side_effect=AssertionError("gate must not run")):
            code, out = publish_ops.publish_plan_json(self.db, self.workspaces, "t1", None, remote=str(self.remote))
        self.assertEqual(code, 0)
        plan = out["plan"]
        self.assertEqual(set(plan), {"thread_id", "project_id", "branch", "remote", "base_sha", "files", "subject", "retry"})
        self.assertEqual((plan["branch"], plan["files"], plan["retry"], plan["base_sha"]),
                         ("guardian/t1", ["app.py", "docs/new.md"], False, self.base))
        self.assertEqual(self.snapshot(), before)
        done = self.publish()
        self.assertEqual({k: done[k] for k in plan}, plan)

    def test_plan_does_not_take_the_workspace_lock(self) -> None:
        import fcntl
        self.two_actions()
        lock_dir = git(self.ws, "rev-parse", "--absolute-git-dir")
        with open(os.path.join(lock_dir, "guardian-publish.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a real publish or sync is running
            code, out = publish_ops.publish_plan_json(self.db, self.workspaces, "t1", None, remote=str(self.remote))
        self.assertEqual((code, out["plan"]["files"]), (0, ["app.py", "docs/new.md"]))
        # nothing of the plan holds the lock afterwards
        with open(os.path.join(lock_dir, "guardian-publish.lock"), "w") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_plan_notes_a_recorded_running_publish(self) -> None:
        self.two_actions()
        self.db.add_publication("t1", "demo", "guardian/t1", None, str(self.remote), "RUNNING", "op")
        code, out = publish_ops.publish_plan_json(self.db, self.workspaces, "t1")
        self.assertEqual(code, 0)
        self.assertIn("RUNNING", out["plan"]["note"])

    def test_plan_codes(self) -> None:
        self.assertEqual(publish_ops.publish_plan_json(self.db, self.workspaces, "nope")[0], publish_ops.NOT_FOUND)
        code, out = publish_ops.publish_plan_json(self.db, self.workspaces, "t1")
        self.assertEqual((code, out["ok"]), (publish_ops.CONFLICT, False))  # no executed file action
        self.two_actions()
        self.assertEqual(publish_ops.publish_plan_json(self.db, self.workspaces, "t1", "main")[0], publish_ops.INVALID_INPUT)
        (self.ws / "stray.txt").write_text("x")
        code, out = publish_ops.publish_plan_json(self.db, self.workspaces, "t1")
        self.assertEqual(code, publish_ops.CONFLICT)
        self.assertIn("stray.txt", out["error"])

    def test_plan_after_an_interrupted_publish_does_not_recover_it(self) -> None:
        self.two_actions()
        self.db.add_publication("t1", "demo", "guardian/t1", None, str(self.remote), "RUNNING", "op")
        git(self.ws, "checkout", "-q", "-b", "guardian/t1")
        git(self.ws, "add", "--", "app.py", "docs/new.md")
        git(self.ws, "commit", "-q", "-m", "fix: x")
        code, out = publish_ops.publish_plan_json(self.db, self.workspaces, "t1")
        self.assertEqual((code, out["plan"]["retry"]), (0, True))
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "guardian/t1")
        self.assertEqual(self.db.list_publications("t1")[0]["status"], "RUNNING")


class InterruptedPublishTests(PublishCase):
    """A publish killed halfway (hub timeout, restart) leaves a RUNNING row and maybe the workspace on the branch."""

    def kill_after(self, steps: str) -> int:
        """Reproduces the state of a publish killed after `checkout` or after `commit`; returns its RUNNING row."""
        row = self.db.add_publication("t1", "demo", "guardian/t1", None, str(self.remote), "RUNNING", "op")
        git(self.ws, "checkout", "-q", "-b", "guardian/t1")
        if steps == "commit":
            git(self.ws, "add", "--", "app.py", "docs/new.md")
            git(self.ws, "commit", "-q", "-m", "fix: killed before the push")
        return row

    def test_killed_after_the_commit_is_recovered_and_pushed(self) -> None:
        self.two_actions()
        stale = self.kill_after("commit")
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "guardian/t1")
        result = self.publish()
        self.assertEqual(result["status"], "PUSHED", result)
        self.assertTrue(result["retry"])  # the kept commit was pushed, not rebuilt
        self.assertEqual(git(self.remote, "rev-parse", "guardian/t1"), result["commit_sha"])
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "main")
        rows = {r["id"]: r for r in self.db.list_publications("t1")}
        self.assertEqual(rows[stale]["status"], "FAILED")
        self.assertIn("interrupted", rows[stale]["error"])
        self.assertEqual(rows[result["publication_id"]]["status"], "PUSHED")

    def test_a_running_row_without_a_stuck_branch_is_marked_failed_and_publish_goes_on(self) -> None:
        self.two_actions()
        stale = self.db.add_publication("t1", "demo", "guardian/t1", None, str(self.remote), "RUNNING", "op")
        self.assertEqual(self.publish()["status"], "PUSHED")
        self.assertEqual(self.db.list_publications("t1")[0]["id"], stale)
        self.assertEqual(self.db.list_publications("t1")[0]["status"], "FAILED")

    def test_a_dirty_worktree_on_the_stuck_branch_is_left_alone(self) -> None:
        self.two_actions()
        stale = self.kill_after("checkout")  # the action files are still uncommitted
        self.refused("not the tracked branch")
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "guardian/t1")
        self.assertEqual((self.ws / "docs" / "new.md").read_text(), "# new\n")
        self.assertEqual(self.db.list_publications("t1")[0]["status"], "FAILED")
        self.assertEqual(self.db.list_publications("t1")[0]["id"], stale)

    def test_a_branch_the_publish_did_not_create_is_not_recovered(self) -> None:
        self.two_actions()
        self.db.add_publication("t1", "demo", "guardian/t1", None, str(self.remote), "RUNNING", "op")
        git(self.ws, "checkout", "-q", "-b", "somebody-elses")
        self.refused("'somebody-elses'")
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "somebody-elses")


class PublishPushFailureTests(PublishCase):
    def reject_pushes(self, on: bool) -> None:
        hook = self.remote / "hooks" / "pre-receive"
        if on:
            hook.write_text("#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        else:
            hook.unlink()

    def test_failed_push_keeps_the_branch_and_a_retry_only_pushes(self) -> None:
        self.two_actions()
        self.reject_pushes(True)
        failed = self.publish()
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("rejected by test hook", failed["stderr"])
        sha = failed["commit_sha"]
        self.assertEqual(git(self.ws, "rev-parse", "guardian/t1"), sha)
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(self.ws, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(git(self.remote, "branch", "--list"), "* main")
        row = self.db.list_publications("t1")[0]
        self.assertEqual((row["status"], row["commit_sha"]), ("FAILED", sha))
        self.assertIn("rejected by test hook", row["error"])

        self.reject_pushes(False)
        with patch.object(publish_ops, "_run_gate", side_effect=AssertionError("a retry must not re-run the tests")):
            retried = self.publish()
        self.assertEqual((retried["status"], retried["commit_sha"]), ("PUSHED", sha))
        self.assertEqual(git(self.remote, "rev-parse", "guardian/t1"), sha)
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual([r["status"] for r in self.db.list_publications("t1")], ["FAILED", "PUSHED"])

    def test_retry_refuses_a_branch_whose_content_moved(self) -> None:
        self.two_actions()
        self.reject_pushes(True)
        self.assertEqual(self.publish()["status"], "FAILED")
        self.reject_pushes(False)
        git(self.ws, "checkout", "-q", "guardian/t1")
        (self.ws / "extra.txt").write_text("e\n")
        git(self.ws, "add", "extra.txt")
        git(self.ws, "commit", "-q", "-m", "extra")
        git(self.ws, "checkout", "-q", "main")
        self.refused("not from an earlier publish")  # the tip is no longer the commit that was recorded
        with sqlite3.connect(self.db.db_path) as conn:  # a crash before the commit was recorded
            conn.execute("UPDATE thread_publications SET commit_sha = NULL")
        self.refused("single commit on the base")
        self.assertEqual(git(self.remote, "branch", "--list"), "* main")

    def test_existing_branch_from_elsewhere_is_not_adopted(self) -> None:
        self.two_actions()
        git(self.ws, "branch", "guardian/t1")
        self.refused("not from an earlier publish")

    def test_push_is_never_forced(self) -> None:
        self.two_actions()
        # The remote already has a branch of that name with other history.
        git(self.ws, "checkout", "-q", "-b", "elsewhere")
        (self.ws / "elsewhere.txt").write_text("e\n")
        git(self.ws, "add", "elsewhere.txt")
        git(self.ws, "commit", "-q", "-m", "elsewhere")
        git(self.ws, "push", "-q", str(self.remote), "elsewhere:refs/heads/guardian/t1")
        remote_tip = git(self.remote, "rev-parse", "guardian/t1")
        git(self.ws, "checkout", "-q", "main")
        git(self.ws, "branch", "-q", "-D", "elsewhere")
        failed = self.publish()
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("rejected", failed["stderr"])
        self.assertEqual(git(self.remote, "rev-parse", "guardian/t1"), remote_tip)
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "main")

    def test_failed_commit_leaves_no_branch(self) -> None:
        self.two_actions()
        hook = self.ws / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'no commits today' >&2\nexit 1\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        failed = self.publish()
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("no commits today", failed["stderr"])
        self.assertEqual(git(self.ws, "branch", "--list", "guardian/*"), "")
        self.assertEqual(git(self.ws, "symbolic-ref", "--short", "HEAD"), "main")
        # The changes are back as plain uncommitted files, unstaged, so the publish can be tried again.
        self.assertEqual(git(self.ws, "status", "--porcelain", "--untracked-files=all").splitlines(),
                         [" M app.py", "?? docs/new.md"])
        hook.unlink()
        self.assertEqual(self.publish()["status"], "PUSHED")


class CommitMessageTests(unittest.TestCase):
    def test_subject_is_prefixed_and_bounded(self) -> None:
        subject, body, trailer = publish_ops.commit_message("t9", "Cap request bodies.\n\nSecond paragraph here.")
        self.assertEqual(subject, "fix: Cap request bodies")
        self.assertEqual(body, "Cap request bodies.\n\nSecond paragraph here.")
        self.assertEqual(trailer, "Debate-Thread: t9")
        long_subject, long_body, _ = publish_ops.commit_message("t9", "word " * 60)
        self.assertLessEqual(len(long_subject), 72)
        self.assertTrue(long_subject.endswith("..."))
        self.assertTrue(all(len(line) <= 72 for line in long_body.splitlines()))

    def test_empty_summary_and_control_characters(self) -> None:
        subject, body, _ = publish_ops.commit_message("t9", "")
        self.assertEqual((subject, body), ("fix: changes from debate t9", ""))
        subject, _, _ = publish_ops.commit_message("t9", "bell\x07 and\x00 nul")
        self.assertEqual(subject, "fix: bell and nul")

    def test_scrub_masks_url_credentials(self) -> None:
        self.assertEqual(publish_ops.scrub("fatal: https://user:pw@host/x.git failed"), "fatal: https://***@host/x.git failed")


class PublishJsonTests(PublishCase):
    def call(self, thread="t1", actor="dashboard@100.64.0.9", branch=None, **kwargs):
        return publish_ops.publish_thread_json(self.db, self.workspaces, thread, actor, branch, **kwargs)

    def test_codes(self) -> None:
        self.assertEqual(self.call(actor="bad actor!")[0], action_ops.INVALID_INPUT)
        code, out = self.call(thread="nope")
        self.assertEqual((code, out["ok"]), (action_ops.NOT_FOUND, False))
        code, out = self.call(remote=str(self.remote))
        self.assertEqual((code, out["ok"]), (action_ops.CONFLICT, False))
        self.assertIn("no executed file action", out["error"])
        self.two_actions()
        code, out = self.call(branch="main", remote=str(self.remote))
        self.assertEqual((code, out["ok"]), (action_ops.INVALID_INPUT, False))
        code, out = self.call(branch="", remote=str(self.remote))
        self.assertEqual((code, out["ok"], out["status"], out["branch"]), (0, True, "PUSHED", "guardian/t1"))

    def test_test_failure_is_a_conflict_with_the_output(self) -> None:
        self.act(APPLY, {"edits": [{"file_path": "app.py", "old": "VALUE = 1", "new": "VALUE = 'BROKEN'"}]})
        code, out = self.call(remote=str(self.remote))
        self.assertEqual(code, action_ops.CONFLICT)
        self.assertNotEqual(out["tests"]["exit_code"], 0)

    def test_failed_push_is_ok_false_with_exit_0(self) -> None:
        self.two_actions()
        code, out = self.call(remote=str(self.root / "no-such-remote.git"))
        self.assertEqual((code, out["ok"], out["status"]), (0, False, "FAILED"))
        self.assertTrue(out["stderr"])


class PublicationTableTests(unittest.TestCase):
    def test_existing_database_gains_the_table(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            DecisionDBManager(path)
            with sqlite3.connect(path) as conn:
                conn.execute("DROP TABLE thread_publications;")
            db = DecisionDBManager(path)
            self.assertEqual(db.list_publications("t1"), [])
            pid = db.add_publication("t1", "demo", "guardian/t1", None, "https://example.invalid/r.git", "RUNNING", "op")
            db.update_publication(pid, status="FAILED", commit_sha="abc", error="boom")
            row = db.list_publications("t1")[0]
            self.assertEqual((row["status"], row["commit_sha"], row["error"], row["created_at"] is not None),
                             ("FAILED", "abc", "boom", True))
            with self.assertRaises(ValueError):
                db.add_publication("t1", "demo", "b", None, None, "WEIRD", "op")


if __name__ == "__main__":
    unittest.main()

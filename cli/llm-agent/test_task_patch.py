"""task_patch and the relay's patch path (docs/design/resident-open-tools.md, O3): the conversion
of an untrusted git patch into apply_patch edits against the workspace object store, the ONE
PROPOSED action on a thread_task_* thread, the row-keyed idempotence, the start reconcile, the
schema-4 store and the structural no-auto rule. Real git in temp dirs, no ssh, no model."""

import io
import os
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import action_ops
import mail_relay
import mail_store
import patch_ops
import task_patch
import testenv
from db_manager import DecisionDBManager
from project_registry import Project, ProjectManager, Workspaces
from test_mail_store import make_clock, make_ready, open_turn, register
from test_repo_tools import git

COMMIT = ("-c", "user.email=t@t", "-c", "user.name=t")
NODE = "node-1"
TOOL_PY = "import os\n\n\ndef run():\n    return 'RUN_BODY'\n"


def make_project_repo(root: Path) -> Tuple[Path, str]:
    """A small code project: a tool, a file with repeated lines, a secret, twelve filler files."""
    repo = root / "ws"
    (repo / "cli").mkdir(parents=True)
    (repo / "cli" / "tool.py").write_text(TOOL_PY)
    (repo / "cli" / "repeat.txt").write_text("a\nx\nb\nx\nc\n")
    (repo / "secret.enc").write_text("enc\n")
    for index in range(12):
        (repo / "cli" / f"many{index:02d}.txt").write_text(f"file {index}\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", "one")
    return repo, git(repo, "rev-parse", "HEAD")


def clone_of(repo: Path, root: Path, name: str = "clone") -> Path:
    clone = root / name
    subprocess.run(["git", "clone", "-q", "--shared", str(repo), str(clone)], check=True)
    return clone


def git_raw(repo: Path, *args: str) -> str:
    """git output verbatim: a diff must keep its trailing newline to stay a valid patch."""
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout


def diff_of(clone: Path, base: str, *flags: str) -> str:
    """The node's own command: what the agent's worktree attaches to task_update."""
    git(clone, "add", "-A")
    return git_raw(clone, "diff", "--cached", "--binary", *flags, base)


def workspace_fingerprint(repo: Path) -> Dict[str, str]:
    """Everything a task patch conversion must leave alone."""
    return {"head": git(repo, "rev-parse", "HEAD"),
            "status": git(repo, "status", "--porcelain"),
            "index": git(repo, "hash-object", ".git/index"),
            "worktrees": git(repo, "worktree", "list")}


class ProjectCase(unittest.TestCase):
    """A temp repo the ProjectManager resolves as project projx."""

    def setUp(self) -> None:
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo, self.sha = make_project_repo(self.repo_root())
        loader = patch.object(ProjectManager, "load",
                              return_value=Project(id="projx", workspace_raw=str(self.repo)))
        loader.start()
        self.addCleanup(loader.stop)

    def repo_root(self) -> Path:
        return self.root


class ConvertTests(ProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.clone = clone_of(self.repo, self.root)
        (self.clone / "cli" / "tool.py").write_text(TOOL_PY.replace("RUN_BODY", "CHANGED"))
        (self.clone / "cli" / "new.txt").write_text("a new file\n")
        self.good_patch = diff_of(self.clone, self.sha, "--no-renames")

    def convert(self, patch_text: Any, patch_base: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return task_patch.convert("projx", self.sha, patch_text,
                                  self.sha if patch_base is None else patch_base, **kwargs)

    def refused(self, patch_text: Any, code: str, *in_message: str, **kwargs: Any) -> str:
        with self.assertRaises(task_patch.TaskPatchError) as caught:
            self.convert(patch_text, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        for part in in_message:
            self.assertIn(part, caught.exception.message)
        return caught.exception.message

    # -- the happy path -----------------------------------------------------------------------

    def test_modify_and_add_reproduce_content_and_leave_the_workspace_untouched(self) -> None:
        before = workspace_fingerprint(self.repo)
        converted = self.convert(self.good_patch)
        after = workspace_fingerprint(self.repo)
        self.assertEqual(after, before)
        self.assertEqual(converted["files"], 2)
        self.assertEqual(converted["bytes"],
                         len(TOOL_PY.replace("RUN_BODY", "CHANGED").encode()) + len(b"a new file\n"))
        original, updated = patch_ops.plan(converted["edits"],
                                           patch_ops.git_reader(self.repo, self.sha))
        self.assertEqual(original["cli/tool.py"], TOOL_PY)
        self.assertIsNone(original["cli/new.txt"])
        self.assertEqual(updated["cli/tool.py"], (self.clone / "cli" / "tool.py").read_text())
        self.assertEqual(updated["cli/new.txt"], "a new file\n")
        # the created file is one edit with an empty old text
        created = [e for e in converted["edits"] if e["file_path"] == "cli/new.txt"]
        self.assertEqual(created, [{"file_path": "cli/new.txt", "old": "", "new": "a new file\n"}])
        task_patch.convert("projx", self.sha, self.good_patch, self.sha)  # twice: no leftover state

    # -- refusals -----------------------------------------------------------------------------

    def test_bad_patch_base_format(self) -> None:
        self.refused(self.good_patch, "bad_patch", patch_base="not-a-sha")
        self.refused(self.good_patch, "bad_patch", patch_base=self.sha[:39])

    def test_delete_rename_mode_binary_and_symlink_are_refused(self) -> None:
        clone = clone_of(self.repo, self.root, "scratch")
        git(clone, "rm", "-q", "cli/repeat.txt")
        self.assertIn("cli/repeat.txt", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                                     "bad_patch", "deleting"))
        git(clone, "reset", "-q", "--hard")
        git(clone, "mv", "cli/tool.py", "cli/tool2.py")
        self.assertIn("rename", self.refused(diff_of(clone, self.sha), "bad_patch"))
        git(clone, "reset", "-q", "--hard")
        git(clone, "add", "-A")
        git(clone, "update-index", "--chmod=+x", "cli/tool.py")
        self.assertIn("mode change", self.refused(
            git_raw(clone, "diff", "--cached", "--binary", self.sha), "bad_patch", "mode"))
        git(clone, "reset", "-q", "--hard")
        (clone / "cli" / "tool.py").write_bytes(b"a\x00b\x01c")
        self.assertIn("cli/tool.py", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                                  "bad_patch", "binary"))
        git(clone, "reset", "-q", "--hard")
        os.symlink("repeat.txt", clone / "cli" / "newlink")
        self.assertIn("symlink", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                              "bad_patch", "cli/newlink"))

    def test_a_modified_path_outside_the_allowed_set_is_refused(self) -> None:
        # the snapshot of this task held only cli/repeat.txt: modifying cli/tool.py is refused
        self.assertIn("cli/tool.py", self.refused(self.good_patch, "bad_patch",
                                                  "not in the task's snapshot",
                                                  allowed=frozenset({"cli/repeat.txt"})))

    def test_a_secret_path_is_refused(self) -> None:
        clone = clone_of(self.repo, self.root, "scratch")
        (clone / "secret.enc").write_text("changed\n")
        self.assertIn("secret.enc", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                                 "bad_patch", "not in the task's snapshot"))
        (clone / "new.enc").write_text("a new secret\n")
        self.assertIn("new.enc", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                              "bad_patch", "secret"))

    def test_an_added_path_that_exists_at_the_sha_is_refused(self) -> None:
        # a forged patch that "creates" a file the pinned commit already holds
        forged = self.good_patch.replace("cli/new.txt", "cli/repeat.txt")
        self.assertIn("cli/repeat.txt", self.refused(forged, "bad_patch"))

    def test_too_many_files(self) -> None:
        clone = clone_of(self.repo, self.root, "scratch")
        for index in range(patch_ops.MAX_FILES + 1):
            (clone / "cli" / f"many{index:02d}.txt").write_text(f"file {index} changed\n")
        self.assertIn("MAX_FILES", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                                "patch_too_large"))

    def test_oversized_text(self) -> None:
        clone = clone_of(self.repo, self.root, "scratch")
        (clone / "big.txt").write_text("x" * (patch_ops.MAX_TEXT_BYTES + 1) + "\n")
        self.assertIn("MAX_TEXT_BYTES", self.refused(diff_of(clone, self.sha, "--no-renames"),
                                                     "patch_too_large"))

    def test_garbage_is_refused(self) -> None:
        self.refused("not a diff at all", "bad_patch")
        self.refused("", "bad_patch")

    # -- the widening rule --------------------------------------------------------------------

    def test_uniqueness_widening_with_a_repeated_line(self) -> None:
        clone = clone_of(self.repo, self.root, "scratch")
        # both repeated "x" lines change, differently: the first edit must widen to "a\nx\n" to be
        # unique, the second is unique only because edits apply in order
        (clone / "cli" / "repeat.txt").write_text("a\ny\nb\nz\nc\n")
        converted = self.convert(diff_of(clone, self.sha, "--no-renames"))
        self.assertEqual(converted["edits"], [
            {"file_path": "cli/repeat.txt", "old": "a\nx\n", "new": "a\ny\n"},
            {"file_path": "cli/repeat.txt", "old": "x\n", "new": "z\n"},
        ])
        original, updated = patch_ops.plan(converted["edits"],
                                           patch_ops.git_reader(self.repo, self.sha))
        self.assertEqual(updated["cli/repeat.txt"], "a\ny\nb\nz\nc\n")


class ThreadIdTests(unittest.TestCase):
    def test_the_thread_id_follows_both_patterns(self) -> None:
        self.assertEqual(task_patch.action_thread_id("k-1a2b3c4d5e6f"),
                         "thread_task_k_1a2b3c4d5e6f")
        self.assertTrue(action_ops.THREAD_ID_PATTERN.fullmatch("thread_task_k_1a2b3c4d5e6f"))
        self.assertIsNone(task_patch.action_thread_id("k/bad"))
        self.assertIsNone(task_patch.action_thread_id("k-" + "a" * 80))

    def test_the_reason_names_the_task_its_author_and_the_first_line(self) -> None:
        self.assertEqual(task_patch.action_reason("k-1", "claude@node-1", "first\nsecond"),
                         "resident task k-1 by claude@node-1: first")
        self.assertEqual(task_patch.action_reason("k-1", "claude@node-1", None),
                         "resident task k-1 by claude@node-1:")

    def test_the_reason_skips_leading_blank_lines_and_is_truncated(self) -> None:
        self.assertEqual(task_patch.action_reason("k-1", "claude@node-1", "\n  \t\n\nfirst\nsecond"),
                         "resident task k-1 by claude@node-1: first")
        long_reason = task_patch.action_reason("k-1", "claude@node-1", "\n" + "x" * 5000 + "\nsecond")
        self.assertEqual(len(long_reason), task_patch.REASON_MAX)
        self.assertTrue(long_reason.startswith("resident task k-1 by claude@node-1: xxx"))


class RelayPatchCase(ProjectCase):
    """A store with a bound agent, its open declaration and a session that never runs (the tool
    handler is called directly; nothing goes over a socket)."""

    def setUp(self) -> None:
        super().setUp()
        self.store = mail_store.MailStore(now=make_clock())
        self.address = register(self.store)
        make_ready(self.store, self.address)
        self.entry: Dict[str, Any] = {"name": "claude", "node": NODE, "provider": "claude",
                                      "driver": "headless", "address": self.address}
        self.entries: List[Dict[str, Any]] = [self.entry]
        self.session = mail_relay.RelaySession(NODE, io.BytesIO(), io.BytesIO(), store=self.store,
                                               declarations=lambda: self.entries)
        self.clone = clone_of(self.repo, self.root)
        (self.clone / "cli" / "tool.py").write_text(TOOL_PY.replace("RUN_BODY", "CHANGED"))
        self.patch_text = diff_of(self.clone, self.sha, "--no-renames")

    def set_tools(self, tools: Optional[str]) -> None:
        if tools is None:
            self.entry.pop("tools", None)
        else:
            self.entry["tools"] = tools

    def project_task(self, *, project: Optional[str] = "projx", sha: Optional[str] = None) -> str:
        created = self.store.task_create("operator", "change the tool", op_id="op-task",
                                         assignee=self.address, project=project,
                                         sha=sha if sha is not None else self.sha)
        self.assertTrue(created["ok"], created)
        return created["id"]

    def claimed(self, task_id: str) -> Tuple[str, int]:
        turn = open_turn(self.store, self.address, 1)
        claimed = self.store.task_claim(self.address, task_id, op_id=f"op-claim-{task_id}",
                                        epoch=1, turn=turn)
        self.assertTrue(claimed["ok"], claimed)
        return turn, claimed["generation"]

    def update(self, task_id: str, generation: int, turn: str, *, patch: Optional[str] = None,
               base: Any = None, status: str = "completed", result: Any = "fixed the tool\nsecond",
               reason: Any = None, op_id: str = "op-done") -> Dict[str, Any]:
        args: Dict[str, Any] = {"task": task_id, "generation": generation, "status": status,
                                "result": result, "reason": reason}
        if patch is not None:
            args["patch"], args["patch_base"] = patch, self.sha if base is None else base
        return self.session._tool_task_update(self.address, args,
                                              {"op_id": op_id, "epoch": 1, "turn": turn})

    def task_row(self, task_id: str) -> Dict[str, Any]:
        return next(row for row in self.store.task_list("operator") if row["id"] == task_id)

    def db(self) -> DecisionDBManager:
        return DecisionDBManager()


class RelayPatchTests(RelayPatchCase):
    def test_a_closed_agent_is_refused_and_the_task_stays_in_progress(self) -> None:
        self.set_tools(None)
        task = self.project_task()
        turn, generation = self.claimed(task)
        answer = self.update(task, generation, turn, patch=self.patch_text)
        self.assertEqual((answer["ok"], answer["reason"]), (False, "patch_not_allowed"))
        self.assertEqual(self.task_row(task)["status"], "in_progress")
        self.assertEqual(self.store.task_patch_action(task), None)

    def test_a_failed_status_is_refused_even_for_an_open_agent(self) -> None:
        self.set_tools("open")
        task = self.project_task()
        turn, generation = self.claimed(task)
        answer = self.update(task, generation, turn, patch=self.patch_text, status="failed",
                             reason="not done")
        self.assertEqual((answer["ok"], answer["reason"]), (False, "patch_not_allowed"))
        self.assertEqual(self.task_row(task)["status"], "in_progress")

    def test_a_task_without_a_project_is_refused(self) -> None:
        self.set_tools("open")
        task = self.project_task(project=None)
        turn, generation = self.claimed(task)
        answer = self.update(task, generation, turn, patch=self.patch_text)
        self.assertEqual((answer["ok"], answer["reason"]), (False, "patch_not_allowed"))

    def test_a_bad_patch_is_refused_and_leaves_the_task_in_progress(self) -> None:
        self.set_tools("open")
        task = self.project_task()
        turn, generation = self.claimed(task)
        for bad in ({"patch": "garbage", "base": self.sha}, {"patch": self.patch_text, "base": "x"}):
            answer = self.update(task, generation, turn, op_id=f"op-{bad['base']}", **bad)
            self.assertFalse(answer["ok"], answer)
            self.assertEqual(answer["reason"], "bad_patch")
        row = self.task_row(task)
        self.assertEqual(row["status"], "in_progress")
        self.assertFalse(row["patch"])
        self.assertEqual(self.db().list_actions(), [])

    def test_a_good_patch_becomes_one_proposed_action(self) -> None:
        self.set_tools("open")
        task = self.project_task()
        turn, generation = self.claimed(task)
        answer = self.update(task, generation, turn, patch=self.patch_text)
        self.assertTrue(answer["ok"], answer)
        action_id = answer["action_id"]
        thread_id = "thread_task_" + task.replace("-", "_")
        self.assertEqual(self.store.task_patch_action(task), action_id)
        row = self.task_row(task)
        self.assertEqual((row["status"], row["patch"], row["patch_action"]),
                         ("completed", True, action_id))
        db = self.db()
        self.assertEqual(db.list_actions(thread_id), [db.get_action(action_id)])
        action = db.get_action(action_id)
        self.assertEqual((action["tool"], action["status"], action["round_num"], action["base_sha"],
                          action["project_id"]),
                         ("jev_mcp:apply_patch", "PROPOSED", 0, self.sha, "projx"))
        self.assertEqual(action["reason"],
                         f"resident task {task} by {self.address}: fixed the tool")
        original, updated = patch_ops.plan(action["arguments"]["edits"],
                                           patch_ops.git_reader(self.repo, self.sha))
        self.assertEqual(updated["cli/tool.py"], (self.clone / "cli" / "tool.py").read_text())
        events = [e for e in self.store.events() if e["kind"] == "task_patch"]
        self.assertEqual(events[0]["detail"]["task"], task)
        self.assertEqual(events[0]["detail"]["action"], action_id)
        self.assertEqual(events[0]["detail"]["files"], 1)
        self.assertIn("base", events[0]["detail"])

    def test_an_op_id_replay_creates_no_second_action(self) -> None:
        self.set_tools("open")
        task = self.project_task()
        turn, generation = self.claimed(task)
        first = self.update(task, generation, turn, patch=self.patch_text, op_id="op-done")
        self.assertTrue(first["ok"], first)
        replay = self.update(task, generation, turn, patch=self.patch_text, op_id="op-done")
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["action_id"], first["action_id"])  # the row's id, row-keyed
        thread_id = "thread_task_" + task.replace("-", "_")
        self.assertEqual(len(self.db().list_actions(thread_id)), 1)
        # a retry with a fresh op id finds the task completed and creates nothing either
        late = self.update(task, generation, turn, patch=self.patch_text, op_id="op-late")
        self.assertEqual((late["ok"], late["reason"]), (False, "not_owner"))
        self.assertEqual(len(self.db().list_actions(thread_id)), 1)


class ReconcileTests(RelayPatchCase):
    def test_the_reconcile_creates_the_missing_action_once(self) -> None:
        task = self.project_task()
        turn, generation = self.claimed(task)
        # the crash window: the fenced update landed, the action did not
        self.assertTrue(self.store.task_update(self.address, task, generation, "completed",
                                               op_id="op-1", result="fixed", epoch=1, turn=turn,
                                               patch=self.patch_text)["ok"])
        pending = self.store.pending_patch_tasks()
        self.assertEqual([row["id"] for row in pending], [task])
        db = self.db()
        created = mail_relay.reconcile_task_patches(self.store, lambda: db)
        action_id = self.store.task_patch_action(task)
        self.assertEqual(created, [action_id])
        self.assertIsNotNone(action_id)
        action = db.get_action(action_id)
        self.assertEqual((action["status"], action["base_sha"], action["project_id"]),
                         ("PROPOSED", self.sha, "projx"))
        self.assertEqual(action["reason"], f"resident task {task} by {self.address}: fixed")
        # the row settled: a second reconcile run creates nothing
        self.assertEqual(mail_relay.reconcile_task_patches(self.store, lambda: db), [])
        thread_id = "thread_task_" + task.replace("-", "_")
        self.assertEqual(len(db.list_actions(thread_id)), 1)

    def test_a_conversion_that_no_longer_passes_is_logged_and_left_alone(self) -> None:
        task = self.project_task()
        turn, generation = self.claimed(task)
        # a patch built against a different commit: it no longer applies to the pinned sha
        scratch = clone_of(self.repo, self.root, "scratch")
        (scratch / "cli" / "tool.py").write_text(TOOL_PY.replace("RUN_BODY", "OTHER"))
        git(scratch, *COMMIT, "commit", "-q", "-am", "two")
        other = git(scratch, "rev-parse", "HEAD")
        (scratch / "cli" / "tool.py").write_text(TOOL_PY.replace("RUN_BODY", "CHANGED"))
        stale_patch = diff_of(scratch, other, "--no-renames")
        self.assertTrue(self.store.task_update(self.address, task, generation, "completed",
                                               op_id="op-1", result="fixed", epoch=1, turn=turn,
                                               patch=stale_patch)["ok"])
        db = self.db()
        self.assertEqual(mail_relay.reconcile_task_patches(self.store, lambda: db), [])
        self.assertIsNone(self.store.task_patch_action(task))
        self.assertEqual(self.db().list_actions(), [])
        events = [e for e in self.store.events() if e["kind"] == "task_patch_failed"]
        self.assertEqual(events[0]["detail"]["task"], task)
        self.assertIn("bad_patch", events[0]["detail"]["error"])


class SchemaV4(unittest.TestCase):
    def setUp(self) -> None:
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.clock = make_clock()  # before the seeded task's expiry, which is fixed in the past
        self.store = mail_store.MailStore(now=self.clock)

    def test_a_fresh_store_is_version_4(self) -> None:
        address = register(self.store)
        make_ready(self.store, address)
        conn = self.store._ready()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("patch", columns)
        self.assertIn("patch_action", columns)
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(version["value"], str(mail_store.SCHEMA_VERSION))

    def test_a_version_3_store_is_migrated_to_4(self) -> None:
        path = mail_store.default_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(path)
        old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT, creator TEXT NOT NULL,
            creator_kind TEXT NOT NULL, assignee TEXT, owner TEXT, status TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0, project TEXT, sha TEXT, blocked_by_json TEXT,
            hop INTEGER NOT NULL, result TEXT, reason TEXT, calls_json TEXT,
            created TEXT NOT NULL, modified TEXT NOT NULL, expires TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '3');
        INSERT INTO tasks VALUES('k-1', 'work', '', 'operator', 'operator', NULL, NULL, 'pending',
                                 0, NULL, NULL, '[]', 0, NULL, NULL, NULL,
                                 '2026-11-15T09:00:00Z', '2026-11-15T09:00:00Z',
                                 '2026-11-16T09:00:00Z');
        """)
        old.commit()
        old.close()
        store = mail_store.MailStore(now=self.clock)
        row = store.task_list("operator")[0]
        self.assertEqual((row["id"], row["patch"], row["patch_action"]), ("k-1", False, None))
        conn = store._ready()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("patch", columns)
        self.assertIn("patch_action", columns)
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(version["value"], str(mail_store.SCHEMA_VERSION))
        # the migrated columns work: a claimed task completes with a patch and links its action
        address = register(store)
        make_ready(store, address)
        turn = open_turn(store, address, 1)
        self.assertTrue(store.task_claim(address, "k-1", op_id="op-claim", epoch=1, turn=turn)["ok"])
        self.assertTrue(store.task_update(address, "k-1", 1, "completed", op_id="op-done",
                                          result="done", epoch=1, turn=turn,
                                          patch="diff --git a/x b/x")["ok"])
        self.assertTrue(store.record_task_patch("k-1", 7, 1, 10))
        self.assertEqual(store.task_patch_action("k-1"), 7)
        self.assertFalse(store.record_task_patch("k-1", 8, 1, 10))  # the row already settled


class NoAutoTests(ProjectCase):
    """The structural rule (rule 5 / OD4): a task patch action never runs itself."""

    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"GUARDIAN_AUTO_APPLY": "on"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_task_patch_action_never_runs_even_with_auto_apply_on(self) -> None:
        db = DecisionDBManager()
        mcp = Workspaces(db)
        edits = [{"file_path": "cli/tool.py", "old": "'RUN_BODY'", "new": "'AUTO_RUN'"}]
        control = db.add_action("thread_review_1", 1, "jev_mcp:apply_patch", {"edits": edits},
                                "a debate action", "PROPOSED", base_sha=self.sha, project_id="projx")
        thread_id = task_patch.action_thread_id("k-abc123def456")
        task_action = db.add_action(thread_id, 0, "jev_mcp:apply_patch",
                                    {"edits": [{**edits[0], "new": "'TASK_PATCH'"}]},
                                    "a task patch", "PROPOSED", base_sha=self.sha,
                                    project_id="projx")
        self.assertTrue(action_ops.is_auto_approvable(db.get_action(control)))
        self.assertFalse(action_ops.is_auto_approvable(db.get_action(task_action)))
        outcomes = action_ops.auto_run_actions(db, mcp, [control, task_action])
        self.assertEqual(db.get_action(control)["status"], "EXECUTED")  # the switch was on
        self.assertEqual(db.get_action(task_action)["status"], "PROPOSED")  # the rule held anyway
        self.assertEqual([outcome["id"] for outcome in outcomes], [control])


if __name__ == "__main__":
    unittest.main()

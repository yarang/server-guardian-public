import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config_ops
from db_manager import DecisionDBManager
from mcp_client import ActionValidationError, MCPClient
from project_registry import (
    DEFAULT_PROJECT_ID,
    Project,
    ProjectManager,
    ProjectValidationError,
    WorkspaceBusy,
    Workspaces,
    parse_project,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("one\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "one")
    return path


VALID = {"id": "svc", "repo": "https://git.example.com/me/svc.git"}


class ParseProjectTests(unittest.TestCase):
    def test_minimal_project_gets_defaults(self) -> None:
        p = parse_project(VALID)
        self.assertEqual((p.name, p.branch), ("svc", "main"))
        self.assertEqual(p.workspace, (Path.home() / "guardian-workspaces" / "svc").resolve())

    def test_rejects_unsafe_values(self) -> None:
        bad = [
            {**VALID, "id": "Bad Id"},
            {**VALID, "repo": "http://git.example/x.git"},
            {**VALID, "repo": "https://user:pw@git.example/x.git"},
            {**VALID, "repo": "git@github.com:me/x.git"},
            {**VALID, "branch": "-x"},
            {**VALID, "workspace": "/etc/x"},
            {**VALID, "workspace": "~/../x"},
            {"id": "svc"},  # neither repo nor workspace
            {**VALID, "tests": {"commands": [{"prefix": "bash -c"}]}},
            {**VALID, "tests": {"commands": [{"prefix": "npx"}]}},
            {**VALID, "tests": {"commands": [{"prefix": "npm exec"}]}},
            {**VALID, "tests": {"commands": [{"prefix": "/usr/bin/make"}]}},
            {**VALID, "tests": {"commands": [{"prefix": "cargo test", "flags": {"--x; rm": True}}]}},
            {**VALID, "tests": {"default": "cargo test"}},  # not in the allow-list
        ]
        for data in bad:
            with self.assertRaises(ProjectValidationError, msg=json.dumps(data)):
                parse_project(data)

    def test_a_trailing_newline_is_refused_in_every_checked_field(self) -> None:
        # "$" also matches just before a trailing newline, so only fullmatch refuses "svc\n"
        # (which would otherwise name a file "svc\n.json").
        bad = [
            {**VALID, "id": "svc\n"},
            {**VALID, "branch": "main\n"},
            {**VALID, "repo": "https://git.example.com/me/x.git\n"},
            {**VALID, "tests": {"commands": [{"prefix": "cargo test", "flags": {"--release\n": True}}]}},
        ]
        for data in bad:
            with self.assertRaises(ProjectValidationError, msg=json.dumps(data)):
                parse_project(data)

    def test_project_test_commands_extend_the_allow_list(self) -> None:
        p = parse_project({**VALID, "tests": {"default": "cargo test --release",
                                              "commands": [{"prefix": "cargo test", "flags": {"--release": False, "-p": True}}]}})
        mcp = MCPClient(workspace=Path.home(), project=p)
        self.assertEqual(mcp._test_argv("cargo test -p core src/lib"), ["cargo", "test", "-p", "core", "src/lib"])
        self.assertEqual(mcp.default_test, "cargo test --release")
        with self.assertRaises(ActionValidationError):
            mcp._test_argv("cargo test --config evil")
        # Another project's client does not get these commands.
        with self.assertRaises(ActionValidationError):
            MCPClient(workspace=Path.home())._test_argv("cargo test")


class EnvRepoTests(unittest.TestCase):
    def test_repo_from_the_environment(self) -> None:
        data = {"id": "self", "repo": "${GUARDIAN_SELF_REPO}", "workspace": "~/guardian-workspaces/self"}
        with patch.dict(os.environ, {"GUARDIAN_SELF_REPO": "https://git.example.com/me/self.git"}):
            self.assertEqual(parse_project(data).repo, "https://git.example.com/me/self.git")
        with patch.dict(os.environ, {"GUARDIAN_SELF_REPO": ""}):
            self.assertIsNone(parse_project(data).repo)
        with patch.dict(os.environ, {"GUARDIAN_SELF_REPO": "http://insecure.example/x.git"}), \
                self.assertRaises(ProjectValidationError):
            parse_project(data)
        with self.assertRaises(ProjectValidationError):
            parse_project({**data, "repo": "${lower}"})

    def test_builtin_project_names_no_account(self) -> None:
        raw = ProjectManager(directory=Path("/nonexistent")).get_raw(DEFAULT_PROJECT_ID)
        self.assertEqual(raw["repo"], "${GUARDIAN_SELF_REPO}")


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.builtin = root / "builtin"
        self.builtin.mkdir()
        (self.builtin / "svc.json").write_text(json.dumps({**VALID, "name": "builtin"}))
        self.mgr = ProjectManager(directory=root / "custom", builtin_dir=self.builtin)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_custom_overrides_builtin_and_only_custom_deletes(self) -> None:
        self.assertFalse(self.mgr.delete("svc"))
        self.mgr.save({**VALID, "name": "custom"})
        self.assertEqual(self.mgr.load("svc").name, "custom")
        self.assertTrue(self.mgr.delete("svc"))
        self.assertEqual(self.mgr.load("svc").name, "builtin")

    def test_delete_of_an_unknown_project_is_not_found_and_of_a_builtin_is_invalid(self) -> None:
        code, out = config_ops.delete_project(self.mgr, "nope")
        self.assertEqual((code, out["ok"]), (config_ops.NOT_FOUND, False))
        code, out = config_ops.delete_project(self.mgr, "svc")  # only the builtin file exists
        self.assertEqual(code, config_ops.INVALID)
        self.assertIn("builtin", out["error"])
        self.mgr.save({**VALID, "name": "custom"})
        self.assertEqual(config_ops.delete_project(self.mgr, "svc")[0], 0)

    def test_unknown_project_is_an_error(self) -> None:
        with self.assertRaises(ProjectValidationError):
            self.mgr.load("nope")

    def test_an_id_with_a_trailing_newline_is_not_a_lookup(self) -> None:
        self.mgr.save({**VALID, "name": "custom"})
        self.assertEqual(self.mgr.load("svc").name, "custom")
        self.assertIsNone(self.mgr.get_raw("svc\n"))
        self.assertFalse(self.mgr.is_custom("svc\n"))

    def test_config_ops_report_and_save(self) -> None:
        code, payload = config_ops.save_project(self.mgr, json.dumps({**VALID, "id": "other"}))
        self.assertEqual((code, payload["id"]), (0, "other"))
        code, payload = config_ops.save_project(self.mgr, json.dumps({"id": "x", "repo": "ftp://x"}))
        self.assertEqual(code, 2)
        code, payload = config_ops.projects_report(self.mgr)
        ids = [p["id"] for p in payload["projects"]]
        self.assertEqual(ids, ["other", "svc"])
        self.assertFalse(payload["projects"][0]["status"]["present"])


class SyncTests(unittest.TestCase):
    """sync() clones a missing workspace, fast-forwards a clean one and never touches local work."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = _repo(root / "origin")
        # Built directly: parse_project only accepts https URLs and home-relative workspaces.
        self.project = Project(id="svc", repo=str(self.origin), branch="main", workspace_raw=str(root / "ws"))
        self.mgr = ProjectManager(directory=root / "custom", builtin_dir=root / "none")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clone_then_fast_forward_then_skip_when_modified(self) -> None:
        first = self.mgr.sync(self.project)
        self.assertEqual(first["action"], "cloned")
        ws = self.project.workspace
        self.assertTrue(_git(ws, "config", "user.name"))  # local identity set when no global one exists

        (self.origin / "a.txt").write_text("two\n")
        _git(self.origin, "commit", "-qam", "two")
        second = self.mgr.sync(self.project)
        self.assertEqual(second["action"], "updated")
        self.assertEqual(second["head"], _git(self.origin, "rev-parse", "HEAD"))

        (ws / "a.txt").write_text("local edit\n")
        third = self.mgr.sync(self.project)
        self.assertEqual(third["action"], "skipped")
        self.assertEqual((ws / "a.txt").read_text(), "local edit\n")

    def test_untracked_files_block_the_fast_forward(self) -> None:
        self.mgr.sync(self.project)
        ws = self.project.workspace
        (ws / "new.txt").write_text("written by an executed action\n")
        (self.origin / "a.txt").write_text("two\n")
        _git(self.origin, "commit", "-qam", "two")
        before = _git(ws, "rev-parse", "HEAD")
        result = self.mgr.sync(self.project)
        self.assertEqual(result["action"], "skipped")
        self.assertIn("untracked", result["reason"])
        self.assertEqual(_git(ws, "rev-parse", "HEAD"), before)
        (ws / "new.txt").unlink()
        self.assertEqual(self.mgr.sync(self.project)["action"], "updated")

    def test_sync_refuses_while_a_publish_holds_the_workspace_lock(self) -> None:
        import fcntl

        self.mgr.sync(self.project)
        ws = self.project.workspace
        with open(ws / ".git" / "guardian-publish.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(WorkspaceBusy) as ctx:
                self.mgr.sync(self.project)
            self.assertIn("publish is running", str(ctx.exception))
            with patch.object(self.mgr, "load", return_value=self.project):
                code, payload = config_ops.sync_projects(self.mgr, "svc")
        self.assertEqual((code, payload["ok"]), (config_ops.CONFLICT, False))
        self.assertIn("publish is running", payload["error"])
        self.assertEqual(self.mgr.sync(self.project)["action"], "updated")  # the lock is free again

    def test_refuses_a_non_git_directory(self) -> None:
        self.project.workspace.mkdir(parents=True)
        (self.project.workspace / "x").write_text("x")
        with self.assertRaises(ProjectValidationError):
            self.mgr.sync(self.project)


class WorkspacesTests(unittest.TestCase):
    def test_actions_resolve_to_their_project_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = DecisionDBManager(db_path=root / "d.db")
            a = db.add_action("thread_a", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "r", project_id="svc")
            b = db.add_action("thread_b", 1, "jev_mcp:run_tests", {"test_cmd": "pytest"}, "r")
            mgr = ProjectManager(directory=root / "custom", builtin_dir=root / "none")
            mgr.save({**VALID})
            ws = Workspaces(db, mgr)
            with patch("project_registry.ProjectManager.load", side_effect=lambda pid: (
                    Project(id=pid, workspace_raw=str(root / pid)))):
                self.assertEqual(ws.for_action(db.get_action(a)).workspace, (root / "svc").resolve())
                self.assertEqual(db.get_action(b)["project_id"], DEFAULT_PROJECT_ID)
            self.assertEqual([x["id"] for x in db.list_actions(project_id="svc")], [a])


class DefaultProjectTests(unittest.TestCase):
    def test_guardian_workspace_env_wins_for_the_default_project(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"GUARDIAN_WORKSPACE": tmp}):
            p = ProjectManager().load(None)
            self.assertEqual((p.id, p.workspace), (DEFAULT_PROJECT_ID, Path(tmp).resolve()))


if __name__ == "__main__":
    unittest.main()

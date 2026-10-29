import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import action_ops
import patch_ops
from code_context import build_pack
from db_manager import DecisionDBManager
from debate_engine import DebateEngine
from llm_runner import LLMResult
from mcp_client import ActionValidationError, MCPClient
from testenv import isolate_data_dir, restore_data_dir, set_auto_apply

HAVE_GO = shutil.which("go") is not None and shutil.which("gofmt") is not None

MAIN_GO = '''package main

import "fmt"

func greet(name string) string {
\treturn "hello " + name
}

func farewell() string {
\treturn "bye"
}

func main() {
\tfmt.Println(greet("x"), farewell())
}
'''


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # Keep the symbol index cache (and anything else) out of the real data directory.
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "ws"
    (repo / "app").mkdir(parents=True)
    (repo / "app/go.mod").write_text("module example.com/app\n\ngo 1.21\n")
    (repo / "app/main.go").write_text(MAIN_GO)
    (repo / "tool.py").write_text("def f():\n    return 1\n")
    (repo / "dup.txt").write_text("x = 1\nx = 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one")
    return repo


class NormalizeAndPlanTests(unittest.TestCase):
    def test_normalize(self) -> None:
        for bad in ([], "x", [{"file_path": "a"}], [{"file_path": "a", "old": "x", "new": "x"}],
                    [{"file_path": "a", "old": "x", "new": "y", "extra": 1}],
                    [{"file_path": "a", "old": "x" * 30000, "new": "y"}]):
            with self.assertRaises(patch_ops.PatchError, msg=repr(bad)[:60]):
                patch_ops.normalize_edits(bad)
        self.assertEqual(patch_ops.normalize_edits([{"file_path": "./a/b.go", "old": "x", "new": "y"}])[0]["file_path"],
                         "a/b.go")

    def test_plan(self) -> None:
        files = {"a.txt": "one two three\n", "dup.txt": "x\nx\n"}
        read = files.get
        original, updated = patch_ops.plan([
            {"file_path": "a.txt", "old": "one", "new": "1"},
            {"file_path": "a.txt", "old": "1 two", "new": "1 2"},  # sees the previous edit
            {"file_path": "new.txt", "old": "", "new": "fresh\n"},
        ], read)
        self.assertEqual(updated["a.txt"], "1 2 three\n")
        self.assertIsNone(original["new.txt"])
        for edits, message in (([{"file_path": "dup.txt", "old": "x", "new": "y"}], "found 2 times"),
                               ([{"file_path": "a.txt", "old": "zzz", "new": "y"}], "not found"),
                               ([{"file_path": "a.txt", "old": "", "new": "y"}], "already exists"),
                               ([{"file_path": "missing.txt", "old": "a", "new": "b"}], "does not exist")):
            with self.assertRaisesRegex(patch_ops.PatchError, message):
                patch_ops.plan(edits, read)
        diff = patch_ops.unified_diff(*patch_ops.plan([{"file_path": "a.txt", "old": "two", "new": "2"}], read))
        self.assertIn("-one two three", diff)
        self.assertIn("+one 2 three", diff)


class ProposalCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))
        self.mcp = MCPClient(workspace=self.repo)
        self.sha = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check(self, tool, args, pack):
        args = self.mcp.validate(tool, args)
        action_ops.proposal_checks(self.mcp, tool, args, self.sha, pack)

    def test_only_shown_code_can_change(self) -> None:
        pack = build_pack(self.repo, ["app/main.go#greet"])
        edit = {"file_path": "app/main.go", "old": 'return "hello " + name', "new": 'return "hi " + name'}
        self.check("jev_mcp:apply_patch", {"edits": [edit]}, pack)
        unseen = {"file_path": "app/main.go", "old": 'return "bye"', "new": 'return "ciao"'}
        with self.assertRaisesRegex(ActionValidationError, "not shown this code"):
            self.check("jev_mcp:apply_patch", {"edits": [unseen]}, pack)
        with self.assertRaisesRegex(ActionValidationError, "not shown this code"):
            self.check("jev_mcp:apply_patch", {"edits": [edit]}, None)
        # New files need no context.
        self.check("jev_mcp:apply_patch", {"edits": [{"file_path": "app/extra.go", "old": "", "new": "package main\n"}]},
                   None)

    def test_imports_of_partial_file_are_editable(self) -> None:
        pack = build_pack(self.repo, ["app/main.go#greet"])
        edit = {"file_path": "app/main.go", "old": 'import "fmt"', "new": 'import (\n\t"fmt"\n\t"os"\n)'}
        self.check("jev_mcp:apply_patch", {"edits": [edit]}, pack)

    def test_verbatim_declaration_lines_are_anchors_but_signatures_are_not(self) -> None:
        (self.repo / "app/main.go").write_text(MAIN_GO.replace('import "fmt"\n', 'import "fmt"\n\nconst Version = "1"\n'))
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "const")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        pack = build_pack(self.repo, ["app/main.go#greet"])
        anchor = {"file_path": "app/main.go", "old": 'const Version = "1"', "new": 'const Version = "1"\n\nconst Max = 60'}
        self.check("jev_mcp:apply_patch", {"edits": [anchor]}, pack)
        signature = {"file_path": "app/main.go", "old": "func farewell() string", "new": "func farewell(x int) string"}
        with self.assertRaisesRegex(ActionValidationError, "not shown this code"):
            self.check("jev_mcp:apply_patch", {"edits": [signature]}, pack)

    def test_match_must_be_unique_at_base(self) -> None:
        pack = build_pack(self.repo, ["dup.txt"])
        with self.assertRaisesRegex(ActionValidationError, "found 2 times"):
            self.check("jev_mcp:apply_patch", {"edits": [{"file_path": "dup.txt", "old": "x = 1", "new": "x = 2"}]}, pack)

    def test_overwrite_requires_whole_file(self) -> None:
        args = {"file_path": "app/main.go", "content": "package main\n"}
        with self.assertRaisesRegex(ActionValidationError, "was not shown in full"):
            self.check("jev_mcp:execute_code_action", args, build_pack(self.repo, ["app/main.go#greet"]))
        self.check("jev_mcp:execute_code_action", args, build_pack(self.repo, ["app/main.go"]))
        self.check("jev_mcp:execute_code_action", {"file_path": "app/new.go", "content": "package main\n"}, None)

    def test_secret_and_escape_paths_rejected(self) -> None:
        for path in (".env", "../outside.go", ".git/config"):
            with self.assertRaises(ActionValidationError, msg=path):
                self.mcp.validate("jev_mcp:apply_patch", {"edits": [{"file_path": path, "old": "", "new": "x"}]})

    def test_secret_directories_and_components_block_edits(self) -> None:
        inside = str(self.repo / "config" / "Secrets" / "x.yaml")
        for path in ("config/secrets/x.yaml", "Secrets/a.txt", "deploy/.ssh/config", ".aws/credentials",
                     "app/../.kube/config", "a/.DOCKER/config.json", inside):
            with self.assertRaises(ActionValidationError, msg=path) as caught:
                self.mcp.validate("jev_mcp:apply_patch", {"edits": [{"file_path": path, "old": "", "new": "x"}]})
            self.assertIn("secret", str(caught.exception))

    def test_ordinary_paths_are_not_mistaken_for_secrets(self) -> None:
        # An absolute path is judged by its workspace-relative part, so the directories above the workspace
        # (a temporary directory, a user's "secrets-projects" folder) cannot make every file look secret.
        edit = {"file_path": str(self.repo / "app" / "main.go"), "old": "", "new": "x"}
        try:
            self.mcp.validate("jev_mcp:apply_patch", {"edits": [edit]})
        except ActionValidationError as exc:
            self.assertNotIn("secret", str(exc))


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))
        self.mcp = MCPClient(workspace=self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, edits, check=True):
        return self.mcp.call_tool("jev_mcp:apply_patch", {"edits": edits, "check": check})

    def test_python_check_and_revert(self) -> None:
        ok = self.call([{"file_path": "tool.py", "old": "return 1", "new": "return 2"}])
        self.assertEqual(ok["status"], "SUCCESS", ok)
        self.assertIn("+    return 2", ok["diff"])
        self.assertEqual(ok["checks"][0]["exit_code"], 0)
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two")
        bad = self.call([{"file_path": "tool.py", "old": "return 2", "new": "return ("},
                         {"file_path": "added.py", "old": "", "new": "X = 1\n"}])
        self.assertEqual((bad["status"], bad["reverted"]), ("FAILED", True))
        self.assertEqual((self.repo / "tool.py").read_text(), "def f():\n    return 2\n")
        self.assertFalse((self.repo / "added.py").exists())

    def test_all_or_nothing_on_bad_edit(self) -> None:
        result = self.call([{"file_path": "tool.py", "old": "return 1", "new": "return 2"},
                            {"file_path": "dup.txt", "old": "x = 1", "new": "x = 3"}], check=False)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("found 2 times", result["error"])
        self.assertEqual((self.repo / "tool.py").read_text(), "def f():\n    return 1\n")

    def test_uncommitted_target_refused(self) -> None:
        (self.repo / "tool.py").write_text("def f():\n    return 1\n# local\n")
        result = self.call([{"file_path": "tool.py", "old": "return 1", "new": "return 2"}])
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("uncommitted changes", result["error"])

    @unittest.skipUnless(HAVE_GO, "Go is not installed")
    def test_go_build_and_gofmt_checks(self) -> None:
        ok = self.call([{"file_path": "app/main.go", "old": 'return "bye"', "new": 'return "ciao"'}])
        self.assertEqual(ok["status"], "SUCCESS", ok)
        self.assertEqual([c["cmd"].split()[0] for c in ok["checks"]], ["gofmt", "go"])
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two")
        broken = self.call([{"file_path": "app/main.go", "old": 'return "ciao"', "new": "return undefinedName"}])
        self.assertTrue(broken["reverted"])
        self.assertIn('return "ciao"', (self.repo / "app/main.go").read_text())
        unformatted = self.call([{"file_path": "app/main.go", "old": 'return "ciao"', "new": 'return    "ciao2"'}])
        self.assertEqual(unformatted["status"], "FAILED")
        self.assertIn("not gofmt-formatted", unformatted["checks"][0]["stderr"])

    @unittest.skipUnless(HAVE_GO, "Go is not installed")
    def test_file_unformatted_before_edit_is_not_reverted(self) -> None:
        (self.repo / "app/main.go").write_text(MAIN_GO.replace('return "bye"', 'return    "bye"'))
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "unformatted")
        ok = self.call([{"file_path": "app/main.go", "old": 'return "hello " + name', "new": 'return "hi " + name'}])
        self.assertEqual(ok["status"], "SUCCESS", ok)
        self.assertIn("already not gofmt-formatted before this edit", ok["checks"][0]["stderr"])

    def test_missing_go_fails_closed(self) -> None:
        with patch("patch_ops._tool", return_value=None):
            result = self.call([{"file_path": "app/main.go", "old": 'return "bye"', "new": 'return "ciao"'}])
        self.assertTrue(result["reverted"])
        self.assertIn("Go is not installed", result["checks"][0]["stderr"])
        self.assertIn('return "bye"', (self.repo / "app/main.go").read_text())


class RunTestsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))
        self.mcp = MCPClient(workspace=self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_go_commands_run_from_their_module(self) -> None:
        rewrite = self.mcp._go_module_argv
        self.assertEqual(rewrite(["go", "test", "./app/..."]), (["go", "test", "./..."], self.mcp.workspace / "app"))
        self.assertEqual(rewrite(["go", "vet", "./app"]), (["go", "vet", "."], self.mcp.workspace / "app"))
        self.assertEqual(rewrite(["pytest", "-q"]), (["pytest", "-q"], self.mcp.workspace))

    def test_dot_dot_dot_uses_the_only_module(self) -> None:
        self.assertEqual(self.mcp._go_module_argv(["go", "test", "./..."]),
                         (["go", "test", "./..."], self.mcp.workspace / "app"))
        (self.repo / "other").mkdir()
        (self.repo / "other/go.mod").write_text("module example.com/other\n")
        with self.assertRaisesRegex(ActionValidationError, "several Go modules"):
            self.mcp.validate("jev_mcp:run_tests", {"test_cmd": "go test ./..."})

    def test_tool_path_and_toolchain_env(self) -> None:
        with patch("mcp_client.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "", ""
            self.mcp.call_tool("jev_mcp:run_tests", {"test_cmd": "go test ./app/..."})
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertTrue(env["PATH"].startswith(str(Path.home() / ".local" / "go" / "bin")))
        self.assertEqual(run.call_args.kwargs["cwd"], self.mcp.workspace / "app")

    def test_default_test_runs_like_run_tests_from_the_go_module_with_one_timeout(self) -> None:
        from types import SimpleNamespace

        from mcp_client import TEST_TIMEOUT

        project = SimpleNamespace(id="demo", default_test="go test ./app/...", test_commands=[])
        mcp = MCPClient(workspace=self.repo, project=project)
        with patch("mcp_client.subprocess.run") as run:
            run.return_value.returncode, run.return_value.stdout, run.return_value.stderr = 0, "", ""
            default = mcp.run_default_test()
            tool = mcp.call_tool("jev_mcp:run_tests", {"test_cmd": "go test ./app/..."})
            written = mcp.call_tool("jev_mcp:execute_code_action",
                                    {"file_path": "docs/x.md", "content": "x", "run_test": True})
        self.assertEqual(default["cmd"], "go test ./...")
        self.assertEqual(tool["status"], "SUCCESS")
        self.assertEqual(written["tests"]["cmd"], "go test ./...")
        calls = [c for c in run.call_args_list if c.args[0][0] == "go"]
        self.assertEqual(len(calls), 3)
        for call in calls:  # the same argv, module directory and timeout for all three
            self.assertEqual(call.args[0], ["go", "test", "./..."])
            self.assertEqual(call.kwargs["cwd"], mcp.workspace / "app")
            self.assertEqual(call.kwargs["timeout"], TEST_TIMEOUT)

    @unittest.skipUnless(HAVE_GO, "Go is not installed")
    def test_real_go_test_from_repo_root_path(self) -> None:
        (self.repo / "app/main_test.go").write_text(
            'package main\n\nimport "testing"\n\nfunc TestGreet(t *testing.T) {\n'
            '\tif greet("x") != "hello x" {\n\t\tt.Fatal("bad")\n\t}\n}\n')
        result = self.mcp.call_tool("jev_mcp:run_tests", {"test_cmd": "go test ./app/..."})
        self.assertEqual(result["status"], "SUCCESS", result)


class SiblingActionTests(unittest.TestCase):
    """Judge actions of one decision build on each other's uncommitted changes (docs/design/debate-patch-tool.md)."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = make_repo(root)
        self.mcp = MCPClient(workspace=self.repo)
        self.db = DecisionDBManager(root / "d.db")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.greet = {"file_path": "app/main.go", "old": 'return "hello " + name', "new": 'return "hi " + name'}
        self.bye = {"file_path": "app/main.go", "old": 'return "bye"', "new": 'return "see you"'}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def propose(self, edit, thread="t1", base=None, check=False, tool="jev_mcp:apply_patch", args=None) -> int:
        base = self.base if base is None else base
        args = args or {"edits": [edit], "check": check}
        return self.db.add_action(thread, 1, tool, args, "r", "PROPOSED", base_sha=base)

    def run_it(self, action_id):
        return action_ops.run_action(self.db, self.mcp, action_id, confirm=lambda _a: True, actor="t")

    def main_go(self) -> str:
        return (self.repo / "app/main.go").read_text()

    def test_siblings_apply_in_sequence(self) -> None:
        first, second = self.propose(self.greet), self.propose(self.bye)
        self.assertEqual(self.run_it(first)["status"], "SUCCESS")
        recorded = self.db.get_action(first)["result"]["file_hashes"]
        self.assertEqual(recorded, {"app/main.go": patch_ops.content_hash(self.repo / "app/main.go")})
        self.assertEqual(self.run_it(second)["status"], "SUCCESS")
        self.assertIn("hi ", self.main_go())
        self.assertIn("see you", self.main_go())
        self.assertEqual(self.db.get_action(second)["status"], "EXECUTED")

    def test_manual_edit_between_siblings_blocks(self) -> None:
        first, second = self.propose(self.greet), self.propose(self.bye)
        self.run_it(first)
        (self.repo / "app/main.go").write_text(self.main_go() + "// by hand\n")
        result = self.run_it(second)
        self.assertEqual(self.db.get_action(second)["status"], "FAILED")
        self.assertIn("uncommitted changes", result["error"])
        self.assertIn("app/main.go", result["error"])
        self.assertIn("not made by a sibling action", result["error"])
        self.assertNotIn("see you", self.main_go())

    def test_other_thread_is_not_a_sibling(self) -> None:
        first, second = self.propose(self.greet, thread="t1"), self.propose(self.bye, thread="t2")
        self.run_it(first)
        result = self.run_it(second)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("not made by a sibling action", result["error"])

    def test_other_base_sha_is_not_a_sibling(self) -> None:
        first = self.propose(self.greet)
        self.run_it(first)
        second = self.propose(self.bye, base="0" * 40)
        with self.assertRaises(RuntimeError):  # the workspace is not at that commit either
            self.run_it(second)
        # Same HEAD check passes only for the real base; a sibling record at another base must not count.
        self.db.update_action(first, "EXECUTED", result={**self.db.get_action(first)["result"]})
        with self.db._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET base_sha = ? WHERE id = ?", ("1" * 40, first))
            conn.commit()
        third = self.propose(self.bye)
        result = self.run_it(third)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("not made by a sibling action", result["error"])

    def test_failed_sibling_does_not_count(self) -> None:
        first = self.propose(self.greet)
        self.run_it(first)
        with self.db._get_connection() as conn:
            conn.execute("UPDATE decision_actions SET status = 'FAILED' WHERE id = ?", (first,))
            conn.commit()
        self.assertEqual(self.run_it(self.propose(self.bye))["status"], "FAILED")

    def test_revert_restores_sibling_state(self) -> None:
        self.run_it(self.propose(self.greet))
        after_first = self.main_go()
        bad = self.propose({"file_path": "app/main.go", "old": 'return "bye"', "new": 'return 42 +'}, check=True)
        result = self.run_it(bad)
        self.assertEqual(result["status"], "FAILED")
        if HAVE_GO:
            self.assertTrue(result["reverted"])
            self.assertEqual(self.main_go(), after_first)
        # Direct apply: a failed check restores the working-tree content before this action, not HEAD.
        python_first = self.propose({"file_path": "tool.py", "old": "return 1", "new": "return 2"}, check=True)
        self.run_it(python_first)
        before = (self.repo / "tool.py").read_text()
        broken = self.propose({"file_path": "tool.py", "old": "return 2", "new": "return ("}, check=True)
        result = self.run_it(broken)
        self.assertEqual((result["status"], result["reverted"]), ("FAILED", True))
        self.assertEqual((self.repo / "tool.py").read_text(), before)
        self.assertIn("return 2", before)

    def test_execute_code_action_follows_the_rule(self) -> None:
        write = lambda text: dict(args={"file_path": "tool.py", "content": text}, tool="jev_mcp:execute_code_action")
        first = self.propose(None, **write("def f():\n    return 5\n"))
        self.run_it(first)
        self.assertIn("file_hashes", self.db.get_action(first)["result"])
        second = self.propose(None, **write("def f():\n    return 6\n"))
        self.assertEqual(self.run_it(second)["status"], "SUCCESS")
        # A patch builds on the written file too; a hand edit blocks the next sibling.
        patch_action = self.propose({"file_path": "tool.py", "old": "return 6", "new": "return 7"})
        self.assertEqual(self.run_it(patch_action)["status"], "SUCCESS")
        (self.repo / "tool.py").write_text("# hand\n")
        blocked = self.propose(None, **write("def f():\n    return 8\n"))
        result = self.run_it(blocked)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("tool.py", result["error"])
        self.assertEqual((self.repo / "tool.py").read_text(), "# hand\n")

    def test_execute_code_action_refuses_foreign_uncommitted_file(self) -> None:
        (self.repo / "tool.py").write_text("# local\n")
        action = self.propose(None, tool="jev_mcp:execute_code_action",
                              args={"file_path": "tool.py", "content": "x = 1\n"})
        result = self.run_it(action)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("uncommitted changes", result["error"])

    def test_clean_files_and_commit_are_unaffected(self) -> None:
        first = self.propose(self.greet)
        self.run_it(first)
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "manual")
        # HEAD moved: the base rule still applies and stops the next action.
        with self.assertRaises(RuntimeError):
            self.run_it(self.propose(self.bye))


class EndToEndTests(unittest.TestCase):
    def _debate(self, root: Path, repo: Path):
        engine = DebateEngine(max_rounds=1, db_path=root / "d.db")
        engine.mcp = MCPClient(workspace=repo)
        judge = ('```json\n{"decision": "Say hi.", "constraints": ["greet says hi"], "checks": [], "actions": ['
                 '{"tool": "jev_mcp:apply_patch", "arguments": {"edits": [{"file_path": "app/main.go", '
                 '"old": "return \\"hello \\" + name", "new": "return \\"hi \\" + name"}], "check": false}, '
                 '"reason": "r"}, '
                 '{"tool": "jev_mcp:apply_patch", "arguments": {"edits": [{"file_path": "app/main.go", '
                 '"old": "return \\"bye\\"", "new": "return \\"ciao\\""}]}, "reason": "unseen"}]}\n```\n'
                 "VERDICT: APPROVED")

        def fake(prompt, context=None, session_id=None, provider=None):
            return LLMResult("claude-cli", judge if "VERDICT" in prompt else "text", None)

        with patch.object(engine.llm, "run_session_prompt", side_effect=fake), \
                patch.object(engine, "_save_debate_log", return_value=root / "l.md"):
            result = engine.run_debate("task", project_id="server-guardian", context_paths=["app/main.go#greet"])
        return engine, result

    def test_judge_patch_flows_to_execution(self) -> None:
        set_auto_apply(self, "off")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            engine, result = self._debate(root, repo)

            seen, unseen = result["final_decision"]["actions"]
            self.assertEqual(seen["status"], "PROPOSED")
            self.assertEqual(unseen["status"], "INVALID")
            self.assertIn("not shown this code", unseen["error"])

            code, shown = action_ops.show_action_json(engine.db, engine.mcp, seen["id"])
            self.assertIn('+\treturn "hi " + name', shown["action"]["diff"])
            code, out = action_ops.run_action_json(engine.db, engine.mcp, seen["id"], "dashboard")
            self.assertEqual((code, out["action"]["status"]), (0, "EXECUTED"))
            self.assertIn('return "hi " + name', (repo / "app/main.go").read_text())

    def test_judge_patch_is_applied_without_the_approval_tab(self) -> None:
        set_auto_apply(self, "on")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            engine, result = self._debate(root, repo)

            seen, unseen = result["final_decision"]["actions"]
            self.assertEqual(seen["status"], "EXECUTED")
            self.assertEqual(unseen["status"], "INVALID")  # a refused proposal is never run
            self.assertIn('return "hi " + name', (repo / "app/main.go").read_text())
            stored = engine.db.get_action(seen["id"])
            self.assertEqual((stored["status"], stored["decided_by"]), ("EXECUTED", action_ops.AUTO_ACTOR))


if __name__ == "__main__":
    unittest.main()

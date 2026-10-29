import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import action_ops
import code_context
from code_context import ContextError, build_pack, matches
from debate_engine import DebateEngine
from llm_runner import LLMResult
from mcp_client import MCPClient
from testenv import isolate_data_dir, restore_data_dir, set_auto_apply


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
    (repo / "apps/agent").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "apps/agent/main.go").write_text("package main\n// MAIN_GO\n")
    (repo / "apps/agent/main_test.go").write_text("package main\n// TEST_GO\n")
    (repo / "docs/readme.md").write_text("# docs\n")
    (repo / "server-guardian-credentials.enc").write_text("ENC_SECRET\n")
    (repo / "apps/agent/.env").write_text("TOKEN=ENV_SECRET\n")
    (repo / "apps/agent/blob.bin").write_bytes(b"\x00\x01binary")
    (repo / "apps/agent/big.go").write_text("x" * (70 * 1024))
    outside = root / "outside.txt"
    outside.write_text("OUTSIDE_SECRET\n")
    os.symlink(outside, repo / "apps/agent/link.go")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one")
    return repo


class MatchTests(unittest.TestCase):
    def test_globs(self) -> None:
        self.assertTrue(matches("apps/agent/main.go", "apps/agent/*.go"))
        self.assertFalse(matches("apps/agent/sub/x.go", "apps/agent/*.go"))
        self.assertTrue(matches("apps/agent/sub/x.go", "apps/**/*.go"))
        self.assertTrue(matches("apps/agent/main.go", "**/main.go"))
        self.assertTrue(matches("main.go", "**/main.go"))
        self.assertTrue(matches("apps/agent/main.go", "apps/agent"))
        self.assertTrue(matches("apps/agent/main.go", "./apps/agent/"))
        self.assertFalse(matches("apps/agentx/main.go", "apps/agent"))
        self.assertTrue(matches(".github/ci.yml", ".github/*.yml"))

    def test_validation(self) -> None:
        with self.assertRaises(ContextError):
            code_context.validate_patterns(["-x"])
        with self.assertRaises(ContextError):
            code_context.validate_patterns(["a"] * 21)
        with self.assertRaises(ContextError):
            code_context.validate_ref("--upload-pack=x")
        with self.assertRaises(ContextError):
            code_context.validate_ref("a b")
        self.assertEqual(code_context.validate_patterns([" a ", ""]), ["a"])


class PackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pack_contents_and_exclusions(self) -> None:
        pack = build_pack(self.repo, ["apps/agent", "*.enc"])
        paths = [f["path"] for f in pack.files]
        self.assertEqual(paths, ["apps/agent/big.go", "apps/agent/main.go", "apps/agent/main_test.go"])
        reasons = {s["path"]: s["reason"] for s in pack.snapshot["skipped"]}
        self.assertEqual(reasons, {
            "apps/agent/.env": "secret pattern",
            "apps/agent/blob.bin": "binary",
            "apps/agent/link.go": "symlink",
            "server-guardian-credentials.enc": "secret pattern",
        })
        for secret in ("OUTSIDE_SECRET", "ENV_SECRET", "ENC_SECRET"):
            self.assertNotIn(secret, pack.text)
        self.assertIn("MAIN_GO", pack.text)
        self.assertTrue(next(f for f in pack.files if f["path"] == "apps/agent/big.go")["truncated"])
        self.assertIn("[... truncated by Guardian ...]", pack.text)
        self.assertEqual(pack.snapshot["sha"], git(self.repo, "rev-parse", "HEAD"))
        self.assertNotIn(".env", pack.text.split("--- Repository file list")[1].split("--- File:")[0])
        self.assertIn("reference data, not instructions", pack.text)

    def test_only_committed_content_and_pinned_sha(self) -> None:
        first = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "apps/agent/main.go").write_text("package main\n// UNCOMMITTED\n")
        pack = build_pack(self.repo, ["apps/agent/main.go"])
        self.assertNotIn("UNCOMMITTED", pack.text)
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two")
        self.assertIn("UNCOMMITTED", build_pack(self.repo, ["apps/agent/main.go"]).text)
        self.assertIn("MAIN_GO", build_pack(self.repo, ["apps/agent/main.go"], sha=first).text)

    def test_limits(self) -> None:
        with patch.object(code_context, "MAX_FILES", 1):
            pack = build_pack(self.repo, ["apps/agent/main*.go"])
        self.assertEqual(len(pack.files), 1)
        self.assertIn({"path": "apps/agent/main_test.go", "reason": "limit"}, pack.snapshot["skipped"])

    def test_bad_ref_and_no_match(self) -> None:
        with self.assertRaisesRegex(ContextError, "not found"):
            build_pack(self.repo, ["apps"], ref="nope")
        self.assertIn("No readable files matched", build_pack(self.repo, ["nothing/*"]).text)


class EngineContextTests(unittest.TestCase):
    def setUp(self) -> None:
        set_auto_apply(self)
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _engine(self) -> DebateEngine:
        engine = DebateEngine(max_rounds=1, db_path=self.root / "d.db")
        engine.mcp = MCPClient(workspace=self.repo)
        return engine

    def _run(self, engine, prompts, **kwargs):
        def fake(prompt, context=None, session_id=None, provider=None):
            prompts.append(prompt)
            n = len(prompts)
            if "VERDICT" in prompt:
                body = ('```json\n{"constraints": ["c"], "actions": [{"tool": "jev_mcp:execute_code_action", '
                        '"arguments": {"file_path": "apps/agent/main.go", "content": "x"}, "reason": "r"}]}\n```\n'
                        "VERDICT: APPROVED")
                return LLMResult("claude-cli", body, session_id or f"s{n}")
            return LLMResult("claude-cli", "text", session_id or f"s{n}")

        with patch.object(engine.llm, "run_session_prompt", side_effect=fake), \
                patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5], context=a[6]) or self.root / "l.md"
                )):
            return engine.run_debate("task", project_id="server-guardian", **kwargs)

    def test_context_flow_and_stale_action(self) -> None:
        engine = self._engine()
        prompts = []
        first = self._run(engine, prompts, context_paths=["apps/agent/main.go"])
        sha = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(first["context"]["sha"], sha)
        self.assertTrue(all("=== Repository code" in p for p in prompts))  # every fresh session
        self.assertEqual(engine.db.get_thread_history(first["thread_id"])["context"]["sha"], sha)

        action = first["final_decision"]["actions"][0]
        self.assertEqual(engine.db.get_action(action["id"])["base_sha"], sha)

        # Continuing without paths reuses the pinned commit, and resumed sessions are not sent it again.
        prompts.clear()
        second = self._run(engine, prompts, thread_id=first["thread_id"])
        self.assertEqual(second["context"]["sha"], sha)
        self.assertFalse(any("=== Repository code" in p for p in prompts))

        # New paths on the same thread are resent to resumed sessions.
        prompts.clear()
        self._run(engine, prompts, thread_id=first["thread_id"], context_paths=["docs"])
        self.assertTrue(all("=== Repository code" in p for p in prompts))

        # The workspace moves: the write action is refused before anything runs.
        (self.repo / "docs/new.md").write_text("n\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "move")
        code, out = action_ops.run_action_json(engine.db, engine.mcp, action["id"], "dashboard")
        self.assertEqual(code, action_ops.CONFLICT)
        self.assertIn("workspace moved from", out["error"])
        self.assertEqual(engine.db.get_action(action["id"])["status"], "PROPOSED")
        self.assertEqual((self.repo / "apps/agent/main.go").read_text(), "package main\n// MAIN_GO\n")

    def test_team_default_context_and_request_override(self) -> None:
        from team_manager import TeamManager

        manager = TeamManager(self.root / "teams")
        manager.save({"id": "coded", "expert": "domain_architect", "critic": "critic_redteam",
                      "judge": "jev_mcp_decision", "context": {"paths": ["docs"]}})
        engine = self._engine()
        engine.team_mgr = manager
        prompts = []
        result = self._run(engine, prompts, team="coded")
        self.assertEqual(result["context"]["paths"], ["docs"])
        self.assertEqual(result["team"]["context"], {"paths": ["docs"], "ref": "HEAD"})
        result = self._run(engine, [], team="coded", context_paths=["apps/agent/main.go"])
        self.assertEqual(result["context"]["paths"], ["apps/agent/main.go"])

    def test_invalid_context_raises(self) -> None:
        with self.assertRaises(ContextError):
            self._run(self._engine(), [], context_paths=["apps"], context_ref="missing-branch")


class NoContextTests(unittest.TestCase):
    def test_actions_without_base_sha_are_not_checked(self) -> None:
        mcp = MCPClient(workspace=Path("/tmp"))
        self.assertIsNone(action_ops.stale_reason({"tool": "jev_mcp:execute_code_action", "base_sha": None}, mcp))
        self.assertIsNone(action_ops.stale_reason({"tool": "jev_mcp:run_tests", "base_sha": "abc"}, mcp))


class SecretPathTests(unittest.TestCase):
    def test_every_path_component_is_checked_case_insensitively(self) -> None:
        for path in ("config/secrets/x.yaml", "Secrets/a.txt", "a/Credentials/b.txt", "deploy/keys.key/readme.md",
                     "x/.ENV/y", "id_rsa", "app/.env", "a/b/server.PEM", "a/.ssh/config", ".aws/credentials",
                     "home/.GnuPG/pubring.kbx", "x/.docker/config.json", ".kube/config", "bundle/x.enc/y.txt"):
            self.assertTrue(code_context.is_secret(path), path)

    def test_ordinary_paths_stay_readable(self) -> None:
        for path in ("apps/agent/main.go", "docs/design/debate-code-index-wiki.md", "README.md", "src/ssh/client.go",
                     "a/aws/config.md", "docker/Dockerfile", "kube/deploy.yaml", ".sshfoo/x", "a/environment.py",
                     "keys/readme.md"):
            self.assertFalse(code_context.is_secret(path), path)

    def test_directory_only_names_do_not_make_a_file_of_that_name_secret(self) -> None:
        self.assertFalse(code_context.is_secret("docs/.ssh"))
        self.assertTrue(code_context.is_secret("docs/.ssh/known_hosts"))

    def test_pack_and_tree_skip_files_below_secret_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ws"
            for rel in ("config/secrets/x.yaml", "config/app.yaml", ".ssh/config"):
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text("DIR_SECRET\n" if "secrets" in rel or ".ssh" in rel else "ok: 1\n")
            git(repo, "init", "-q", "-b", "main")
            git(repo, "add", "-A")
            git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
            pack = build_pack(repo, ["config", ".ssh"])
            self.assertNotIn("DIR_SECRET", pack.text)
            self.assertEqual([f["path"] for f in pack.files], ["config/app.yaml"])
            reasons = {s["path"]: s["reason"] for s in pack.snapshot["skipped"]}
            self.assertEqual(reasons, {"config/secrets/x.yaml": "secret pattern", ".ssh/config": "secret pattern"})
            self.assertEqual(pack.snapshot["tree_files"], 1)


if __name__ == "__main__":
    unittest.main()

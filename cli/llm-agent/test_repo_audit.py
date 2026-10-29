import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tempfile import TemporaryDirectory

import testenv
import audit_ops
import repo_audit
from repo_audit import AuditError, AuditStore, build_plan, plan_summary
from repo_tools import RepoTools

COMMIT = ("-c", "user.email=t@t", "-c", "user.name=t")
QUESTION = "Which handlers ignore an error?"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "ws"
    (repo / "apps/agent").mkdir(parents=True)
    (repo / "cli").mkdir()
    (repo / "conf").mkdir()
    (repo / "apps/agent/main.go").write_text(
        'package main\n\nfunc Serve() {\n\t_ = 1\n}\n\nfunc Other() {\n\t_ = 2\n}\n')
    (repo / "apps/agent/notes.txt").write_text("alpha\nbeta\ngamma\n")
    (repo / "apps/agent/blob.bin").write_bytes(b"\x00\x01binary")
    (repo / "apps/agent/.env").write_text("TOKEN=secret\n")
    (repo / "cli/tool.py").write_text("def run():\n    return 1\n\n\nclass Box:\n    def open(self):\n        return 2\n")
    (repo / "conf/app.yaml").write_text("name: guardian\nport: 9870\n")
    (repo / "conf/empty.txt").write_text("")
    (repo / "server-guardian-credentials.enc").write_text("ENC_SECRET\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", "one")
    return repo


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.tools = RepoTools(self.repo, self.sha)

    def plan(self, **kwargs):
        return build_plan(self.tools, "demo", self.sha, QUESTION, **kwargs)

    def test_same_sha_gives_identical_plan_file(self) -> None:
        first = json.dumps(self.plan(), sort_keys=True, indent=2)
        second = json.dumps(build_plan(RepoTools(self.repo, self.sha), "demo", self.sha, QUESTION),
                            sort_keys=True, indent=2)
        self.assertEqual(first, second)

    def test_units_are_declarations_and_whole_files_without_declarations(self) -> None:
        plan = self.plan()
        by_path = {}
        for unit in plan["units"]:
            by_path.setdefault(unit["path"], []).append(unit)
        names = {u["name"] for u in by_path["cli/tool.py"]}
        self.assertEqual(names, {"run", "Box", "Box.open"})
        self.assertEqual({u["name"] for u in by_path["apps/agent/main.go"]}, {"Serve", "Other"})
        notes = by_path["apps/agent/notes.txt"]
        self.assertEqual([(u["kind"], u["start"], u["end"]) for u in notes], [("file", 1, 3)])
        self.assertEqual(len({u["id"] for u in plan["units"]}), len(plan["units"]))

    def test_secret_binary_and_empty_files_are_excluded_with_a_reason(self) -> None:
        plan = self.plan()
        in_units = {u["path"] for u in plan["units"]}
        excluded = {e["path"]: e["reason"] for e in plan["excluded"]}
        self.assertNotIn("apps/agent/.env", in_units)
        self.assertNotIn("server-guardian-credentials.enc", in_units)
        self.assertEqual(excluded["apps/agent/.env"], "secret")
        self.assertEqual(excluded["server-guardian-credentials.enc"], "secret")
        self.assertEqual(excluded["apps/agent/blob.bin"], "binary")
        self.assertEqual(excluded["conf/empty.txt"], "empty")
        self.assertNotIn("ENC_SECRET", json.dumps(plan))
        self.assertNotIn("TOKEN=secret", json.dumps(plan))

    def test_scope_globs_narrow_the_plan(self) -> None:
        plan = self.plan(scope=["cli/**"])
        self.assertEqual({u["path"] for u in plan["units"]}, {"cli/tool.py"})
        self.assertEqual(plan["scope"], ["cli/**"])
        self.assertEqual(plan["excluded"], [])  # paths outside the scope are not "excluded", they are out of scope

    def test_every_unit_is_in_exactly_one_shard(self) -> None:
        plan = self.plan()
        listed = [uid for shard in plan["shards"] for uid in shard["units"]]
        self.assertEqual(sorted(listed), sorted(u["id"] for u in plan["units"]))
        self.assertEqual(len(listed), len(set(listed)))

    def test_shards_respect_the_byte_limit_and_keep_files_whole(self) -> None:
        plan = self.plan(shard_limit=60)
        self.assertGreater(len(plan["shards"]), 1)
        for shard in plan["shards"]:
            paths = [f["path"] for f in shard["files"]]
            if len(paths) > 1:
                self.assertLessEqual(shard["bytes"], 60)
            else:
                self.assertEqual(shard["bytes"], shard["files"][0]["size"])
        files = [f["path"] for shard in plan["shards"] for f in shard["files"]]
        self.assertEqual(len(files), len(set(files)))
        self.assertEqual(files, sorted(files, key=lambda p: (os.path.dirname(p), os.path.basename(p))))

    def test_oversized_file_gets_its_own_shard(self) -> None:
        plan = self.plan(shard_limit=20)
        big = [s for s in plan["shards"] if s["oversized"]]
        self.assertTrue(big)
        for shard in big:
            self.assertEqual(len(shard["files"]), 1)

    def test_shard_key_depends_on_question_prompt_version_and_blobs(self) -> None:
        base = self.plan()
        other_question = build_plan(self.tools, "demo", self.sha, "Which paths skip auth?")
        self.assertNotEqual([s["key"] for s in base["shards"]], [s["key"] for s in other_question["shards"]])
        self.assertEqual([s["id"] for s in base["shards"]], [s["id"] for s in other_question["shards"]])
        # Same wording with other spacing is the same question.
        spaced = build_plan(self.tools, "demo", self.sha, "  Which   handlers ignore\tan error? ")
        self.assertEqual(base["question_hash"], spaced["question_hash"])
        old = repo_audit.AUDIT_PROMPT_VERSION
        self.addCleanup(setattr, repo_audit, "AUDIT_PROMPT_VERSION", old)
        repo_audit.AUDIT_PROMPT_VERSION = old + 1
        bumped = self.plan()
        self.assertNotEqual([s["key"] for s in base["shards"]], [s["key"] for s in bumped["shards"]])

    def test_new_commit_keeps_the_layout_but_changes_every_key(self) -> None:
        before = self.plan(shard_limit=60)
        (self.repo / "conf/app.yaml").write_text("name: guardian\nport: 9871\n")
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-q", "-m", "two")
        sha2 = git(self.repo, "rev-parse", "HEAD")
        after = build_plan(RepoTools(self.repo, sha2), "demo", sha2, QUESTION, shard_limit=60)
        # The sha is part of every key (the plan is per commit); the shard layout itself stays the same.
        self.assertEqual([s["id"] for s in before["shards"]], [s["id"] for s in after["shards"]])
        self.assertTrue(all(a != b for a, b in zip((s["key"] for s in before["shards"]),
                                                   (s["key"] for s in after["shards"]))))

    def test_empty_question_is_refused(self) -> None:
        with self.assertRaises(AuditError):
            build_plan(self.tools, "demo", self.sha, "   ")

    def test_summary_counts(self) -> None:
        plan = self.plan()
        summary = plan_summary(plan)
        self.assertEqual(summary["units"], len(plan["units"]))
        self.assertEqual(summary["shards"], len(plan["shards"]))
        self.assertEqual(summary["excluded"], {"secret": 2, "binary": 1, "empty": 1})


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.plan = build_plan(RepoTools(self.repo, self.sha), "demo", self.sha, QUESTION, shard_limit=60)
        self.store = AuditStore(repo_audit.default_audit_dir("demo"))

    def test_store_lives_under_the_data_dir(self) -> None:
        self.assertTrue(str(self.store.root).startswith(self.data))
        self.assertEqual(self.store.root.parent.name, "repo-audit")

    def test_create_writes_plan_and_metadata_once(self) -> None:
        audit_id = self.store.create(self.plan)
        self.assertRegex(audit_id, r"^[a-z0-9_]{1,64}$")
        saved = json.loads((self.store.root / audit_id / "plan.json").read_text())
        self.assertEqual(saved, self.plan)
        meta = self.store.meta(audit_id)
        self.assertEqual((meta["id"], meta["state"], meta["sha"]), (audit_id, "planned", self.sha))
        self.assertEqual(self.store.load_plan(audit_id), self.plan)
        self.assertEqual([m["id"] for m in self.store.list()], [audit_id])

    def test_two_audits_of_the_same_question_get_distinct_ids(self) -> None:
        first, second = self.store.create(self.plan), self.store.create(self.plan)
        self.assertNotEqual(first, second)

    def test_resume_skips_done_shards_only(self) -> None:
        audit_id = self.store.create(self.plan)
        shards = self.plan["shards"]
        self.assertGreater(len(shards), 2)
        self.assertEqual([s["id"] for s in self.store.pending(audit_id)], [s["id"] for s in shards])
        self.store.save_shard(audit_id, shards[0]["id"], {"state": "done", "key": shards[0]["key"], "findings": []})
        self.store.save_shard(audit_id, shards[1]["id"], {"state": "deferred", "key": shards[1]["key"], "findings": []})
        self.assertEqual([s["id"] for s in self.store.pending(audit_id)], [s["id"] for s in shards[1:]])

    def test_done_record_with_a_stale_key_does_not_count(self) -> None:
        audit_id = self.store.create(self.plan)
        first = self.plan["shards"][0]
        self.store.save_shard(audit_id, first["id"], {"state": "done", "key": "old-prompt-version", "findings": []})
        self.assertEqual(self.store.pending(audit_id)[0]["id"], first["id"])

    def test_corrupt_shard_file_counts_as_pending(self) -> None:
        audit_id = self.store.create(self.plan)
        first = self.plan["shards"][0]
        path = self.store.root / audit_id / "shards" / f"{first['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(self.store.pending(audit_id)[0]["id"], first["id"])

    def test_save_shard_rejects_unknown_state_and_shard(self) -> None:
        audit_id = self.store.create(self.plan)
        first = self.plan["shards"][0]
        with self.assertRaises(AuditError):
            self.store.save_shard(audit_id, first["id"], {"state": "great", "key": first["key"]})
        with self.assertRaises(AuditError):
            self.store.save_shard(audit_id, "s9999", {"state": "done", "key": "x"})

    def test_writes_leave_no_temp_files(self) -> None:
        audit_id = self.store.create(self.plan)
        first = self.plan["shards"][0]
        self.store.save_shard(audit_id, first["id"], {"state": "done", "key": first["key"]})
        names = [p.name for p in (self.store.root / audit_id).rglob("*") if p.is_file()]
        self.assertFalse([n for n in names if n.endswith(".tmp")])

    def test_audit_id_cannot_escape_the_store(self) -> None:
        for bad in ("../x", "a/b", "", "A_B", "x" * 65, "..", "a.b"):
            with self.assertRaises(AuditError, msg=bad):
                self.store.load_plan(bad)

    def test_unknown_audit_is_an_error(self) -> None:
        with self.assertRaises(AuditError):
            self.store.load_plan("audit_20260101_000000_abcdef")

    def test_project_id_is_cleaned_for_the_directory(self) -> None:
        self.assertIsNone(repo_audit.default_audit_dir(""))
        self.assertIsNone(repo_audit.default_audit_dir(None))
        self.assertEqual(repo_audit.default_audit_dir("../evil").parent.name, "repo-audit")
        self.assertEqual(repo_audit.default_audit_dir("../evil").parent.parent, repo_audit.data_root())


class OpsTests(unittest.TestCase):
    def setUp(self) -> None:
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = make_repo(Path(tmp.name))
        sha = git(repo, "rev-parse", "HEAD")
        project = SimpleNamespace(id="demo")
        for target, value in (("audit_ops.open_store", (project, None)),
                              ("audit_ops.open_tools", RepoTools(repo, sha))):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dry_run_writes_nothing(self) -> None:
        code, payload = audit_ops.plan_audit("demo", QUESTION, dry_run=True)
        self.assertEqual(code, 0)
        self.assertIsNone(payload["audit"])
        self.assertFalse(repo_audit.default_audit_dir("demo").exists())

    def test_plan_list_show_round_trip(self) -> None:
        code, payload = audit_ops.plan_audit("demo", QUESTION, scope=["cli/**"])
        self.assertEqual(code, 0)
        audit_id = payload["audit"]
        self.assertEqual(payload["summary"]["scope"], ["cli/**"])
        self.assertEqual([a["id"] for a in audit_ops.list_audits("demo")[1]["audits"]], [audit_id])
        code, shown = audit_ops.show_audit("demo", audit_id)
        self.assertEqual(code, 0)
        self.assertEqual(shown["pending_shards"], ["s0001"])

    def test_bad_question_and_unknown_audit_use_the_exit_codes(self) -> None:
        self.assertEqual(audit_ops.plan_audit("demo", "  ")[0], 2)
        self.assertEqual(audit_ops.show_audit("demo", "audit_20260101_000000_abcdef")[0], 3)
        self.assertEqual(audit_ops.show_audit("demo", "../x")[0], 3)


class NoActionsTests(unittest.TestCase):
    """The audit is read-only: it never creates actions (docs/design/repo-audit-mode.md, "자동 적용과의 관계")."""

    def test_module_does_not_import_the_action_machinery(self) -> None:
        tree = ast.parse(Path(str(repo_audit.__file__)).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("action_ops", "patch_ops", "publish_ops", "workspace_ops", "debate_engine", "db"):
            self.assertNotIn(forbidden, imported)

    def test_cli_path_never_loads_the_action_machinery(self) -> None:
        # A fresh interpreter, so imports of other tests do not leak in.
        code = ("import sys, audit_ops; "
                "print(','.join(m for m in ('action_ops','patch_ops','publish_ops','workspace_ops','debate_engine',"
                "'db_manager') if m in sys.modules))")
        here = str(Path(str(repo_audit.__file__)).parent)
        out = subprocess.run([sys.executable, "-c", code], cwd=here, capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": here}, check=True).stdout.strip()
        self.assertEqual(out, "")

    def test_planning_and_checkpointing_create_no_decision_database(self) -> None:
        data = testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = make_repo(Path(tmp.name))
        sha = git(repo, "rev-parse", "HEAD")
        plan = build_plan(RepoTools(repo, sha), "demo", sha, QUESTION)
        store = AuditStore(repo_audit.default_audit_dir("demo"))
        audit_id = store.create(plan)
        first = plan["shards"][0]
        store.save_shard(audit_id, first["id"], {"state": "done", "key": first["key"], "findings": []})
        files = [p for p in Path(data).rglob("*") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertEqual(path.suffix, ".json", path)
            self.assertIn("repo-audit", path.parts)
        # The workspace is untouched too.
        self.assertEqual(git(repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()

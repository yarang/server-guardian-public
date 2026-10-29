"""Report snapshots: a debate report kept as a document version (office-export.md, D1 option (c))."""
import contextlib
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Tuple
from unittest.mock import patch

import artifact_ops
import report_snapshot
from artifact_ops import ArtifactError, ArtifactStore
from project_registry import parse_project
from testenv import isolate_data_dir, restore_data_dir

CODE = {"id": "code-demo", "workspace": "~/ws/code-demo"}
OPS = {"id": "ops-demo", "kind": "ops", "name": "Ops demo", "targets": {"all": True}}
DESIGN = {"id": "arch-docs", "kind": "design", "name": "Architecture docs"}
THREAD = "thread_20261028_101500_abc123"
REPORT = "# Report\n\nThe disk is full.\n\n![Summary](thread_20261028_101500_abc123.assets/summary.svg)\n"


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def put_report(project: str, text: str, thread: str = THREAD) -> Path:
    from db_manager import decisions_dir

    folder = decisions_dir() / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{thread}.md"
    path.write_text(text, encoding="utf-8")
    return path


class KeepReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = parse_project(CODE)
        put_report(self.project.id, REPORT)

    def tearDown(self) -> None:
        for root in (artifact_ops.artifacts_root(), Path(os.environ["GUARDIAN_DATA_DIR"]) / "decisions"):
            for path in sorted(root.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()

    def test_the_report_becomes_version_one_unchanged(self) -> None:
        manifest = report_snapshot.keep_report(self.project, THREAD)
        store = ArtifactStore(self.project.id, THREAD)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(store.read(1, "report.md"), REPORT)
        entry = manifest["files"][0]
        self.assertEqual(entry["sha256"], hashlib.sha256(REPORT.encode()).hexdigest())
        self.assertEqual((entry["agent"], entry["title"]), ("report-snapshot", "Debate report"))

    def test_images_of_the_report_assets_only_warn(self) -> None:
        manifest = report_snapshot.keep_report(self.project, THREAD)
        self.assertEqual(len(manifest["warnings"]), 1)
        self.assertIn("not an artifact of this thread", manifest["warnings"][0])

    def test_the_same_text_is_not_stored_twice_but_a_changed_one_is(self) -> None:
        report_snapshot.keep_report(self.project, THREAD)
        with self.assertRaises(report_snapshot.AlreadyKept):
            report_snapshot.keep_report(self.project, THREAD)
        put_report(self.project.id, REPORT + "\nRound two.\n")
        self.assertEqual(report_snapshot.keep_report(self.project, THREAD)["version"], 2)
        store = ArtifactStore(self.project.id, THREAD)
        self.assertEqual(store.read(1, "report.md"), REPORT)  # the older version is untouched

    def test_a_converted_file_does_not_make_the_next_keep_look_new(self) -> None:
        report_snapshot.keep_report(self.project, THREAD)
        ArtifactStore(self.project.id, THREAD).convert(1, "report.md", "docx")
        with self.assertRaises(report_snapshot.AlreadyKept):
            report_snapshot.keep_report(self.project, THREAD)

    def test_the_word_and_powerpoint_conversion_work_on_the_kept_report(self) -> None:
        report_snapshot.keep_report(self.project, THREAD)
        store = ArtifactStore(self.project.id, THREAD)
        for target in ("docx", "pptx"):
            self.assertEqual(store.convert(1, "report.md", target)["path"], f"report.{target}")

    def test_ops_projects_are_kept_too(self) -> None:
        ops = parse_project(OPS)
        put_report(ops.id, REPORT)
        self.assertEqual(report_snapshot.keep_report(ops, THREAD)["project"], ops.id)

    def test_design_projects_are_refused(self) -> None:
        put_report(DESIGN["id"], REPORT)
        with self.assertRaisesRegex(ArtifactError, "design project"):
            report_snapshot.keep_report(parse_project(DESIGN), THREAD)

    def test_a_report_the_validators_refuse_is_refused_with_their_message(self) -> None:
        cases = {
            "raw HTML": "# R\n\nSet <TZ> first.\n",
            "a credential": "# R\n\npassword = hunter2hunter2hunter2\nAKIAABCDEFGHIJKLMNOP\n",
            "control characters": "# R\n\x07\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                put_report(self.project.id, text)
                with self.assertRaises(ArtifactError):
                    report_snapshot.keep_report(self.project, THREAD)
                self.assertIsNone(ArtifactStore(self.project.id, THREAD).latest())

    def test_a_report_over_the_project_limit_is_refused(self) -> None:
        small = parse_project({**CODE, "id": "code-small"})
        put_report(small.id, "# R\n\n" + "x" * 300000)
        with self.assertRaisesRegex(ArtifactError, "max_bytes"):
            report_snapshot.keep_report(small, THREAD)

    def test_a_missing_report_a_symlink_and_odd_ids_are_refused(self) -> None:
        with self.assertRaisesRegex(ArtifactError, "no report"):
            report_snapshot.keep_report(self.project, "thread_20261028_000000_zzzzzz")
        path = put_report(self.project.id, REPORT, "linked")
        target = path.with_name("target.txt")
        target.write_text("# Secret\n")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ArtifactError, "no report"):
            report_snapshot.keep_report(self.project, "linked")
        with self.assertRaises(ArtifactError):
            report_snapshot.keep_report(self.project, "../escape")


class KeepCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        put_report("code-demo", REPORT)

    def tearDown(self) -> None:
        for root in (artifact_ops.artifacts_root(), Path(os.environ["GUARDIAN_DATA_DIR"]) / "decisions"):
            for path in sorted(root.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()

    def run_command(self, project: str = "code-demo", thread: str = THREAD) -> Tuple[int, Dict[str, Any]]:
        import guardian

        manager = SimpleNamespace(load=lambda pid: parse_project({**CODE, "id": pid}) if pid != "nope" else self._unknown())
        args = SimpleNamespace(report_keep=True, project=project, thread=thread)
        out = io.StringIO()
        code = 0
        with patch("project_registry.ProjectManager", return_value=manager), contextlib.redirect_stdout(out):
            try:
                self.assertTrue(guardian.handle_report_keep(args))
            except SystemExit as exc:
                code = int(exc.code)
        return code, json.loads(out.getvalue())

    @staticmethod
    def _unknown() -> None:
        from project_registry import ProjectValidationError

        raise ProjectValidationError("no project 'nope'")

    def test_exit_codes_follow_the_other_artifact_commands(self) -> None:
        code, answer = self.run_command()
        self.assertEqual((code, answer["ok"], answer["version"]), (0, True, 1))
        self.assertEqual(answer["file"]["path"], "report.md")
        self.assertEqual(self.run_command()[0], 4)  # the same text again
        self.assertEqual(self.run_command(project="nope")[0], 3)
        self.assertEqual(self.run_command(thread="thread_20261028_000000_zzzzzz")[0], 3)
        put_report("code-demo", "# R\n\n<TZ>\n", "thread_20261028_111111_bbbbbb")
        code, answer = self.run_command(thread="thread_20261028_111111_bbbbbb")
        self.assertEqual(code, 2)
        self.assertIn("HTML", answer["error"])


if __name__ == "__main__":
    unittest.main()

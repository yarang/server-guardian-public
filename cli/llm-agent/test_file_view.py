"""File view (docs/design/code-wiki-coverage.md, C): list, show and read of one repository file without an LLM."""
import json
import unittest
from typing import Any
from unittest.mock import patch

import file_view
from code_wiki import basis_for
from project_registry import Project, ProjectManager
from repo_tools import RepoTools
import test_code_wiki
from test_code_wiki import BuildCase, WikiCase, add_svc, commit, make_page, wiki_store, write
from test_repo_tools import make_repo
from testenv import isolate_data_dir, restore_data_dir

TOTAL_PY = "def compute_total(items):\n    return sum(items)\n\n\nclass Ledger:\n    def balance(self):\n        return 0\n"
USER_PY = "from svc.total import compute_total\n\n\ndef report():\n    return compute_total([1, 2])\n"
STRANGER_PY = "def other():\n    compute_total = 3\n    return compute_total\n"
TEST_PY = "import total\n\n\ndef test_it():\n    assert total.compute_total([]) == 0\n"


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class FileViewCase(WikiCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.root)
        add_svc(self.repo)
        write(self.repo, "svc/total.py", TOTAL_PY)
        write(self.repo, "svc/report.py", USER_PY)
        write(self.repo, "svc/stranger.py", STRANGER_PY)
        write(self.repo, "svc/test_total.py", TEST_PY)
        write(self.repo, "svc/secret_keys.txt", "token\n")
        write(self.repo, "docs/design/ledger.md",
              "# Ledger design\n\nTotals live in `svc/total.py:1`.\n\nSee total.py for the rest.\n")
        write(self.repo, "notes.md", "Unrelated: total sums.\n")
        write(self.repo, "svc/big.txt", "".join(f"line {i} " + "x" * 300 + "\n" for i in range(1, 401)))
        self.sha = commit(self.repo, "ledger")
        self.project = Project(id="fileview", workspace_raw=str(self.repo))
        patcher = patch.object(ProjectManager, "load", return_value=self.project)
        patcher.start()
        self.addCleanup(patcher.stop)

    def show(self, path: str) -> Any:
        code, payload = file_view.show_file("fileview", path)
        self.assertEqual(code, 0, payload)
        return payload


class ListTests(FileViewCase):
    def test_lists_readable_files_only(self) -> None:
        code, payload = file_view.list_files("fileview")
        self.assertEqual(code, 0)
        paths = [f["path"] for f in payload["files"]]
        self.assertIn("svc/total.py", paths)
        self.assertNotIn("svc/secret_keys.txt", paths)
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(payload["sha"], self.sha)
        self.assertFalse(payload["truncated"])
        size = next(f["size"] for f in payload["files"] if f["path"] == "svc/total.py")
        self.assertEqual(size, len(TOTAL_PY.encode()))


class ShowTests(FileViewCase):
    def test_outline_in_line_order(self) -> None:
        payload = self.show("svc/total.py")
        self.assertEqual(payload["language"], "python")
        self.assertTrue(payload["indexed"])
        self.assertEqual([(d["qualified"], d["start"]) for d in payload["outline"]],
                         [("compute_total", 1), ("Ledger", 5), ("Ledger.balance", 6)])

    def test_references_need_an_import(self) -> None:
        incoming = {r["path"]: r for r in self.show("svc/total.py")["incoming"]}
        # report.py imports it; stranger.py only reuses the name; the test imports it and is marked.
        self.assertEqual(set(incoming), {"svc/report.py", "svc/test_total.py"})
        self.assertFalse(incoming["svc/report.py"]["test"])
        self.assertTrue(incoming["svc/test_total.py"]["test"])
        self.assertEqual(self.show("svc/report.py")["outgoing"], [{"path": "svc/total.py", "count": 2, "test": False}])
        self.assertEqual(self.show("svc/stranger.py")["outgoing"], [])

    def test_documents_that_mention_the_file(self) -> None:
        docs = self.show("svc/total.py")["docs"]
        self.assertEqual(docs, [{"path": "docs/design/ledger.md", "title": "Ledger design", "lines": [3, 5]}])

    def test_pages_by_scope_basis_and_citation(self) -> None:
        tools = RepoTools(self.repo, self.sha)
        store = wiki_store("fileview")
        store.save(make_page(tools))  # scope svc/
        store.save(make_page(tools, id="other", scope=["docs/"], basis=basis_for(tools, ["svc/server.go"]),
                             body="## 역할\nsee `svc/auth.go:6`.\n"))
        pages = {p["id"]: p for p in self.show("svc/auth.go")["pages"]}
        self.assertEqual({k: v["via"] for k, v in pages.items()}, {"svc": "scope", "other": "cited"})
        self.assertEqual(pages["svc"]["state"], "fresh")
        via = {p["id"]: p["via"] for p in self.show("svc/server.go")["pages"]}
        self.assertEqual(via, {"svc": "scope", "other": "basis"})

    def test_unreadable_paths_are_not_found(self) -> None:
        for path in ("svc/secret_keys.txt", "svc/missing.py", "../etc/passwd", ""):
            code, payload = file_view.show_file("fileview", path)
            self.assertEqual((code, payload["ok"]), (3, False), path)


class ReadTests(FileViewCase):
    def test_reads_a_range(self) -> None:
        code, payload = file_view.read_lines("fileview", "svc/total.py", 5, 6)
        self.assertEqual(code, 0)
        self.assertEqual(payload["lines"], ["class Ledger:", "    def balance(self):"])
        self.assertEqual((payload["start"], payload["end"], payload["total"], payload["truncated"]), (5, 6, 7, False))

    def test_end_past_the_file_is_clipped(self) -> None:
        _code, payload = file_view.read_lines("fileview", "svc/total.py", 6, 50)
        self.assertEqual((payload["end"], len(payload["lines"])), (7, 2))

    def test_limits(self) -> None:
        self.assertEqual(file_view.read_lines("fileview", "svc/total.py", 0, 3)[0], 2)
        self.assertEqual(file_view.read_lines("fileview", "svc/total.py", 4, 3)[0], 2)
        self.assertEqual(file_view.read_lines("fileview", "svc/total.py", 1, 401)[0], 2)
        self.assertEqual(file_view.read_lines("fileview", "svc/total.py", 99, 100)[0], 2)
        self.assertEqual(file_view.read_lines("fileview", "svc/secret_keys.txt", 1, 1)[0], 3)
        code, payload = file_view.read_lines("fileview", "svc/big.txt", 1, 400)
        self.assertEqual(code, 0)
        self.assertTrue(payload["truncated"])
        self.assertLess(len(payload["lines"]), 400)
        self.assertEqual(payload["end"], len(payload["lines"]))


class ImportFormTests(unittest.TestCase):
    def test_forms(self) -> None:
        imports = file_view.imports
        self.assertTrue(imports("a/b.ts", "import { x } from '../lib/report';", "lib/report.ts"))
        self.assertTrue(imports("a/b.tsx", 'const m = await import("./report.js");', "a/report.ts"))
        self.assertTrue(imports("a/b.ts", "import x from './ui';", "a/ui/index.ts"))
        self.assertFalse(imports("a/b.ts", "import { x } from './reporter';", "a/report.ts"))
        self.assertTrue(imports("cmd/main.go", 'import "example.com/m/svc"', "svc/auth.go"))
        self.assertTrue(imports("svc/a.go", "package svc", "svc/auth.go"))
        self.assertFalse(imports("cmd/main.go", 'import "example.com/m/other"', "svc/auth.go"))
        self.assertTrue(imports("x.py", "import os, wiki_build\n", "cli/wiki_build.py"))
        self.assertTrue(imports("x.py", "from .wiki_build import run\n", "cli/wiki_build.py"))
        self.assertFalse(imports("x.py", "# wiki_build is used elsewhere\n", "cli/wiki_build.py"))


class FileCliTests(BuildCase):
    run_cli = test_code_wiki.CliTests.run_cli

    def test_file_commands(self) -> None:
        code, payload, _t, _e = self.run_cli("--code-file-list", "--project", "wikitest")
        self.assertEqual(code, 0)
        self.assertIn("svc/auth.go", [f["path"] for f in payload["files"]])
        code, payload, _t, _e = self.run_cli("--code-file-show", "svc/auth.go", "--project", "wikitest")
        self.assertEqual((code, payload["path"]), (0, "svc/auth.go"))
        self.assertEqual([d["name"] for d in payload["outline"]], ["Check"])
        code, payload, _t, _e = self.run_cli("--code-file-read", "svc/auth.go", "--start", "6", "--end", "6",
                                             "--project", "wikitest")
        self.assertEqual((code, payload["lines"]), (0, ["func Check(token string) bool {"]))
        code, payload, _t, _e = self.run_cli("--code-file-read", "svc/auth.go", "--project", "wikitest")
        self.assertEqual((code, payload["start"], payload["end"]), (0, 1, 8))
        code, payload, _t, _e = self.run_cli("--code-file-show", "nope.go", "--project", "wikitest")
        self.assertEqual((code, payload["ok"]), (3, False))
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()

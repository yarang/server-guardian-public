import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from code_context import ContextError
from repo_mcp import RepoMcpServer
from repo_tools import REFS_HEADER, RepoTools, clip_result, describe_request, provided_paths
from test_code_index import TSX_SOURCE
from test_repo_tools import COMMIT, git

HANDLER_GO = (
    "package api\n\n"
    "// Handle serves the API.\n"
    "func Handle(name string) string {\n"
    "\treturn \"hello \" + name\n"
    "}\n"
)
HANDLER_TS = (
    "export function Handle(event: string): string {\n"
    "  return event;\n"
    "}\n"
)
MAIN_GO = (
    "package main\n\n"
    "import \"example.com/api\"\n\n"
    "func main() {\n"
    "\tapi.Handle(\"x\") // Handle is called here\n"
    "}\n"
)
WIDGETS_GO = (
    "package ui\n\n"
    "type Widget struct{}\n\n"
    "func (w *Widget) Draw() {}\n\n"
    "func (w *Widget) Close() {\n\tw.Draw()\n}\n"
)
OTHER_GO = (
    "package ui\n\n"
    "type Panel struct{}\n\n"
    "func (p *Panel) Draw() {}\n"
)


def make_index_repo(root: Path) -> Path:
    repo = root / "ws"
    files = {
        "api/handler.go": HANDLER_GO,
        "web/src/handler.ts": HANDLER_TS,
        "cmd/main.go": MAIN_GO,
        "ui/widget.go": WIDGETS_GO,
        "ui/panel.go": OTHER_GO,
        "web/src/Counter.tsx": TSX_SOURCE,
        "tools/run.py": "import os\n\n\ndef run_tool(x):\n    return os.path.join(x)\n\n\nclass Runner:\n    def go(self):\n        return run_tool(1)\n",
        "web/src/credentials.ts": "export function Handle() {}\n",
        "docs/notes.md": "Handle appears in docs only.\n",
        "many/big.go": "package many\n\n" + "".join(f"func Caller{i}() {{\n\tShared()\n}}\n\n" for i in range(250)),
        "many/blob.go": "package many\n\nfunc Binary() {}\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    (repo / "many/blob.go").write_bytes(b"package many\x00\x01\nfunc Binary() {}\n")
    for i in range(120):
        (repo / f"many/shared{i}.go").write_text("package many\n\nfunc Shared() {}\n")
    outside = root / "outside.ts"
    outside.write_text("export function Handle() {}\n")
    os.symlink(outside, repo / "web/src/link.ts")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", "index fixture")
    return repo


class IndexOpsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "ws").mkdir()
        self.repo = make_index_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.cache = self.root / "cache"
        self.tools = RepoTools(self.repo, self.sha, cache_dir=self.cache)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_op(self, **request: Any) -> Dict[str, Any]:
        return self.tools.run(request)

    def commit_more(self, files: Dict[str, str]) -> RepoTools:
        """Commits extra files on top of the fixture and returns tools for the new commit (same cache dir)."""
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-qm", "more")
        return RepoTools(self.repo, git(self.repo, "rev-parse", "HEAD"), cache_dir=self.cache)


class EntriesTests(IndexOpsCase):
    def test_entries_carry_the_blob_id(self) -> None:
        mode, size, blob_id = self.tools.entries["api/handler.go"]
        self.assertEqual(mode, "100644")
        self.assertEqual(size, len(HANDLER_GO))
        self.assertEqual(blob_id, git(self.repo, "rev-parse", f"{self.sha}:api/handler.go"))


class DefsTests(IndexOpsCase):
    def test_lists_declarations_of_every_language(self) -> None:
        res = self.run_op(op="defs", symbol="Handle")
        self.assertTrue(res["ok"])
        self.assertEqual(set(res), {"op", "target", "ok", "bytes", "truncated", "error", "text", "resolved"})
        self.assertEqual(res["resolved"], "name")
        self.assertEqual(res["text"].splitlines(), [
            "api/handler.go:4-6 function func Handle(name string) string {",
            "web/src/handler.ts:1-3 function export function Handle(event: string): string {",
        ])
        self.assertEqual(res["target"], "Handle")

    def test_secret_symlinked_and_non_code_files_are_not_indexed(self) -> None:
        text = self.run_op(op="defs", symbol="Handle")["text"]
        self.assertNotIn("credentials", text)
        self.assertNotIn("link.ts", text)
        self.assertNotIn("notes.md", text)
        self.assertNotIn("many/blob.go", self.run_op(op="defs", symbol="Binary")["text"])
        self.assertEqual(self.run_op(op="defs", symbol="Binary")["text"], "(no definitions found)")

    def test_qualified_methods_and_python_symbols(self) -> None:
        self.assertEqual(self.run_op(op="defs", symbol="Widget.Draw")["text"],
                         "ui/widget.go:5-5 method func (w *Widget) Draw() {}")
        both = self.run_op(op="defs", symbol="Draw")["text"].splitlines()
        self.assertEqual([line.split(" ")[0] for line in both], ["ui/panel.go:5-5", "ui/widget.go:5-5"])
        self.assertIn("tools/run.py:8-10 class class Runner:", self.run_op(op="defs", symbol="Runner")["text"])
        self.assertIn("method def go(self):", self.run_op(op="defs", symbol="Runner.go")["text"])
        self.assertIn("web/src/Counter.tsx:24-33 const export const Counter",
                      self.run_op(op="defs", symbol="Counter")["text"])

    def test_glob_uses_the_grep_rules(self) -> None:
        self.assertEqual(self.run_op(op="defs", symbol="Handle", glob="web/**")["text"].count("\n"), 0)
        self.assertTrue(self.run_op(op="defs", symbol="Handle", glob="web/**")["text"].startswith("web/src/handler.ts:"))
        self.assertTrue(self.run_op(op="defs", symbol="Handle", glob="api")["text"].startswith("api/handler.go:"))
        self.assertEqual(self.run_op(op="defs", symbol="Handle", glob="docs/*")["text"], "(no definitions found)")
        self.assertEqual(self.run_op(op="defs", symbol="Handle", glob="web/**")["target"], "Handle in web/**")

    def test_limit_of_100_lines_marks_truncation(self) -> None:
        res = self.run_op(op="defs", symbol="Shared")
        self.assertTrue(res["truncated"])
        lines = res["text"].splitlines()
        self.assertEqual(len(lines), 101)
        self.assertIn("more definitions", lines[-1])
        self.assertEqual(lines[0].split(" ")[0], "many/shared0.go:3-3")

    def test_invalid_requests(self) -> None:
        for symbol in (None, "", "a b", "a.b.c", "x;rm", "../x", 5, "1abc"):
            res = self.run_op(op="defs", symbol=symbol)
            self.assertFalse(res["ok"], symbol)
            self.assertIn("invalid symbol", res["error"])
        self.assertEqual(self.run_op(op="defs", symbol="Handle", glob="--bad")["error"], "invalid glob")
        self.assertEqual(self.run_op(op="defs", symbol="Handle", glob=5)["error"], "invalid glob")
        self.assertFalse(self.run_op(op="defs")["ok"])

    def test_parse_failures_do_not_fail_the_index(self) -> None:
        self.tools = self.commit_more({"tools/broken.py": "def broken(:\n"})
        self.assertTrue(self.run_op(op="defs", symbol="run_tool")["ok"])
        self.assertEqual(self.tools.code_index().errors, ["tools/broken.py"])
        # The unparsed file is named in the coverage footer: "no definitions" may be a false negative.
        self.assertEqual(self.run_op(op="defs", symbol="broken")["text"].splitlines()[0], "(no definitions found)")
        self.assertIn("tools/broken.py", self.run_op(op="defs", symbol="broken")["text"].splitlines()[1])

    def test_budget_clipping_applies(self) -> None:
        res = self.tools.run({"op": "defs", "symbol": "Shared"}, max_bytes=100)
        self.assertTrue(res["truncated"])
        self.assertIn("truncated by Guardian", res["text"])
        self.assertEqual(self.tools.run({"op": "defs", "symbol": "Shared"}, max_bytes=0)["error"], "budget")


class RefsTests(IndexOpsCase):
    def test_header_and_grep_format(self) -> None:
        res = self.run_op(op="refs", symbol="Handle")
        self.assertTrue(res["ok"])
        lines = res["text"].splitlines()
        self.assertEqual(lines[0], REFS_HEADER.format(symbol="Handle"))
        self.assertIn("name-based", lines[0])
        self.assertIn("unrelated symbols with the same name", lines[0])
        self.assertEqual(lines[1:], ['cmd/main.go:6: \tapi.Handle("x") // Handle is called here'])

    def test_include_defs_adds_the_declaration_lines(self) -> None:
        lines = self.run_op(op="refs", symbol="Handle", include_defs=True)["text"].splitlines()[1:]
        self.assertEqual([line.split(": ")[0] for line in lines],
                         ["api/handler.go:4", "cmd/main.go:6", "web/src/handler.ts:1"])
        # Only a real True counts: a string does not switch it on.
        self.assertEqual(len(self.run_op(op="refs", symbol="Handle", include_defs="true")["text"].splitlines()), 2)

    def test_type_method_uses_the_method_name(self) -> None:
        text = self.run_op(op="refs", symbol="Widget.Draw")["text"]
        self.assertIn("ui/widget.go:8: \tw.Draw()", text)
        self.assertNotIn("ui/widget.go:5:", text)  # the declaration of Widget.Draw
        self.assertIn("ui/panel.go:5:", text)  # Panel.Draw has the same name: name-based
        self.assertTrue(text.startswith(REFS_HEADER.format(symbol="Widget.Draw")))

    def test_python_and_tsx_references(self) -> None:
        text = self.run_op(op="refs", symbol="run_tool")["text"]
        self.assertIn("tools/run.py:10:         return run_tool(1)", text)
        self.assertNotIn("tools/run.py:4:", text)
        jsx = self.run_op(op="refs", symbol="Counter", glob="web/**")["text"]
        self.assertIn('web/src/Counter.tsx:38:   return <Counter label="x" />;', jsx)
        self.assertNotIn("Counter.tsx:24:", jsx)

    def test_glob_and_no_matches(self) -> None:
        self.assertEqual(self.run_op(op="refs", symbol="Handle", glob="web/**")["text"].splitlines()[1:],
                         ["(no references found)"])
        self.assertTrue(self.run_op(op="refs", symbol="Nothing")["ok"])
        self.assertEqual(self.run_op(op="refs", symbol="Handle", glob="--x")["error"], "invalid glob")
        self.assertIn("invalid symbol", self.run_op(op="refs", symbol="a b")["error"])
        self.assertEqual(self.run_op(op="refs", symbol="Handle", glob="cmd")["target"], "Handle in cmd")

    def test_limit_of_200_lines_marks_truncation(self) -> None:
        res = self.run_op(op="refs", symbol="Shared")
        self.assertTrue(res["truncated"])
        lines = res["text"].splitlines()
        self.assertEqual(len(lines), 1 + 200 + 1)
        self.assertIn("more references", lines[-1])
        self.assertEqual(lines[1], "many/big.go:4: \tShared()")

    def test_long_lines_are_cut(self) -> None:
        (self.repo / "wide.js").write_text("Wide(" + "a" * 600 + ");\n")
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-qm", "wide")
        tools = RepoTools(self.repo, git(self.repo, "rev-parse", "HEAD"))
        row = tools.run({"op": "refs", "symbol": "Wide"})["text"].splitlines()[1]
        self.assertEqual(len(row), len("wide.js:1: ") + 301)
        self.assertTrue(row.endswith("…"))


class OutlineAndReadTests(IndexOpsCase):
    def test_outline_supports_tsx_and_js(self) -> None:
        res = self.run_op(op="outline", path="web/src/Counter.tsx")
        self.assertTrue(res["ok"])
        lines = res["text"].splitlines()
        self.assertEqual(lines[0], "[outline of web/src/Counter.tsx: line: declaration]")
        self.assertIn("24: export const Counter = ({ label }: ButtonProps): JSX.Element => {", lines)
        self.assertIn("5: export interface ButtonProps {", lines)
        self.assertEqual(provided_paths(res), ["web/src/Counter.tsx"])
        self.assertFalse(self.run_op(op="outline", path="docs/notes.md")["ok"])
        self.assertIn("TypeScript", self.run_op(op="outline", path="docs/notes.md")["error"])
        self.assertEqual(self.run_op(op="outline", path="web/src/credentials.ts")["error"], "excluded")

    def test_read_symbol_of_a_tsx_file(self) -> None:
        res = self.run_op(op="read", path="web/src/Counter.tsx#App")
        self.assertTrue(res["ok"], res["error"])
        self.assertIn("35|", res["text"])
        self.assertIn("39| }", res["text"])
        self.assertIn('import React, { useState } from "react";', res["text"])  # the import block comes along
        self.assertNotIn("function plain", res["text"].split("[lines")[-1])
        self.assertEqual(self.run_op(op="read", path="web/src/Counter.tsx#Missing")["error"], "symbol not found")

    def test_go_and_python_outline_are_unchanged(self) -> None:
        self.assertEqual(self.run_op(op="outline", path="api/handler.go")["text"],
                         "[outline of api/handler.go: line: declaration]\n1: package api\n4: func Handle(name string) string")
        self.assertEqual(self.run_op(op="outline", path="tools/run.py")["text"].splitlines()[1:],
                         ["4: def run_tool(x):", "8: class Runner:", "9:     def go(self):"])


class CitationRuleTests(IndexOpsCase):
    def test_defs_provides_and_mentions_nothing(self) -> None:
        res = self.run_op(op="defs", symbol="Handle")
        self.assertIn("api/handler.go", res["text"])
        self.assertEqual(provided_paths(res), [])
        self.assertEqual(self.tools.mentioned_in(res), [])

    def test_refs_behaves_like_grep(self) -> None:
        res = self.run_op(op="refs", symbol="Handle", include_defs=True)
        self.assertEqual(provided_paths(res), ["api/handler.go", "cmd/main.go", "web/src/handler.ts"])
        self.assertEqual(self.tools.mentioned_in(res), self.tools.cited_paths(res["text"]))
        none = self.run_op(op="refs", symbol="Nothing")
        self.assertEqual(provided_paths(none), [])  # the header alone provides no file
        self.assertEqual(provided_paths(self.run_op(op="refs", symbol="Handle", glob="web/**")), [])
        # A refs header never reads as a "path:N: " line.
        self.assertEqual(provided_paths({"ok": True, "op": "refs", "text": REFS_HEADER.format(symbol="a.b")}), [])

    def test_describe_request(self) -> None:
        self.assertEqual(describe_request({"op": "defs", "symbol": "Handle"}), {"op": "defs", "target": "Handle"})
        self.assertEqual(describe_request({"op": "refs", "symbol": "A.b", "glob": "x/*"}),
                         {"op": "refs", "target": "A.b in x/*"})
        self.assertEqual(clip_result(self.run_op(op="defs", symbol="Handle"), 10)["truncated"], True)


class BaitTests(IndexOpsCase):
    """Bait 1 (docs/design/debate-code-index-wiki.md): the same name in a Go and a TS file, one real call."""

    def test_same_name_in_two_files_only_one_is_called(self) -> None:
        defs = self.run_op(op="defs", symbol="Handle", glob="api/**")["text"] + "\n" + \
            self.run_op(op="defs", symbol="Handle", glob="web/**")["text"]
        both = self.run_op(op="defs", symbol="Handle")["text"]
        self.assertIn("api/handler.go:", both)
        self.assertIn("web/src/handler.ts:", both)
        self.assertIn("api/handler.go:", defs)
        refs = self.run_op(op="refs", symbol="Handle")["text"].splitlines()
        self.assertIn("name-based", refs[0])
        self.assertIn("may include unrelated symbols", refs[0])
        # The one real call site is listed; neither declaration is mistaken for a caller.
        self.assertEqual([line.split(": ")[0] for line in refs[1:]], ["cmd/main.go:6"])
        self.assertFalse(any("handler.ts" in line for line in refs[1:]))
        # defs alone provides nothing a citation could lean on, so the agent must read the call site.
        self.assertEqual(provided_paths(self.run_op(op="defs", symbol="Handle")), [])


class McpToolTests(IndexOpsCase):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.root / "calls.jsonl"
        self.server = RepoMcpServer(self.tools, str(self.log))

    def call(self, name: str, **arguments: Any) -> Dict[str, Any]:
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
        assert response is not None
        return response["result"]

    def records(self) -> List[Dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_tools_are_listed_with_schemas(self) -> None:
        tools = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        self.assertEqual(by_name["repo_defs"]["inputSchema"]["required"], ["symbol"])
        self.assertEqual(by_name["repo_refs"]["inputSchema"]["properties"]["include_defs"]["type"], "boolean")
        self.assertIn("TypeScript", by_name["repo_outline"]["description"])
        self.assertIn("by name", by_name["repo_refs"]["description"])

    def test_defs_and_refs_calls_are_served_and_logged(self) -> None:
        defs = self.call("repo_defs", symbol="Handle")
        self.assertFalse(defs["isError"])
        self.assertIn("api/handler.go:4-6", defs["content"][0]["text"])
        refs = self.call("repo_refs", symbol="Handle", include_defs=True, glob="cmd/**")
        self.assertFalse(refs["isError"])
        self.assertIn("cmd/main.go:6:", refs["content"][0]["text"])
        first, second = self.records()
        self.assertEqual((first["op"], first["target"], first["paths"], first["mentions"]), ("defs", "Handle", [], []))
        self.assertEqual((second["op"], second["target"], second["paths"]), ("refs", "Handle in cmd/**", ["cmd/main.go"]))
        self.assertNotIn("hello", json.dumps(self.records()))  # the log never holds file contents

    def test_errors_and_unknown_arguments(self) -> None:
        bad = self.call("repo_defs", symbol="a b")
        self.assertTrue(bad["isError"])
        self.assertIn("invalid symbol", bad["content"][0]["text"])
        ok = self.call("repo_defs", symbol="Handle", glob="api", bogus="x")  # unknown arguments are dropped
        self.assertFalse(ok["isError"])
        self.assertTrue(self.call("repo_refs")["isError"])

    def test_budget_applies_to_the_new_tools(self) -> None:
        server = RepoMcpServer(self.tools, str(self.log), max_steps=1)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "repo_defs", "arguments": {"symbol": "Handle"}}})
        res = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "repo_refs", "arguments": {"symbol": "Handle"}}})["result"]
        self.assertTrue(res["isError"])
        self.assertIn("budget", res["content"][0]["text"])
        self.assertEqual(self.records()[1]["error"], "budget")

    def test_cache_dir_flag_reaches_the_index(self) -> None:
        import sys
        import mcp_stdio
        argv = [sys.executable, str(Path(__file__).resolve().parent / "repo_mcp.py"), "--workspace", str(self.repo),
                "--sha", self.sha, "--cache-dir", str(self.root / "mcp-cache")]
        with mcp_stdio.StdioMCPClient(argv[0], argv[1:], env={**os.environ}) as client:
            self.assertIn("api/handler.go:4-6", client.call_tool("repo_defs", {"symbol": "Handle"}))
        self.assertTrue(list((self.root / "mcp-cache").glob("blobs/*/*.json")))


class ThreadSafetyTests(IndexOpsCase):
    def test_parallel_requests_share_one_index(self) -> None:
        import threading
        results: List[Optional[str]] = []

        def worker() -> None:
            results.append(self.tools.run({"op": "defs", "symbol": "Handle"})["text"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(results), 6)
        self.assertIs(self.tools.code_index(), self.tools.code_index())

    def test_works_without_any_cache(self) -> None:
        tools = RepoTools(self.repo, self.sha)
        self.assertIsNone(tools.cache_dir)
        self.assertIn("api/handler.go:4-6", tools.run({"op": "defs", "symbol": "Handle"})["text"])

    def test_second_instance_reuses_the_blob_cache(self) -> None:
        self.tools.run({"op": "defs", "symbol": "Handle"})
        again = RepoTools(self.repo, self.sha, cache_dir=self.cache)
        again.code_index()
        self.assertEqual(again.code_index().stats["parsed"], 0)
        self.assertGreater(again.code_index().stats["cache_hits"], 0)


class CoverageFooterTests(IndexOpsCase):
    """W2: defs/refs say which files could hide a result (failed parse, over 1 MB, identifier cap)."""

    def footer(self, res: Dict[str, Any]) -> List[str]:
        return [line for line in res["text"].splitlines() if line.startswith("[index coverage: ")]

    def test_no_footer_when_everything_was_indexed(self) -> None:
        for request in ({"op": "defs", "symbol": "Handle"}, {"op": "refs", "symbol": "Handle"},
                        {"op": "refs", "symbol": "Nothing"}, {"op": "defs", "symbol": "Nothing"}):
            self.assertEqual(self.footer(self.run_op(**request)), [], request)

    def test_parse_failures_large_files_and_the_cap_are_counted(self) -> None:
        self.tools = self.commit_more({
            "tools/broken.py": "def broken(:\n",
            "gen/huge.go": "package gen\n// " + "x" * (1024 * 1024) + "\nfunc Huge() {}\n",
            "gen/loop.py": "tick = 0\n" + "tick\n" * 5200,
        })
        refs = self.run_op(op="refs", symbol="tick")
        self.assertEqual(self.footer(refs), [
            "[index coverage: 1 file failed to parse, 1 file over 1 MB not indexed, "
            "1 file with this identifier capped at 5000 occurrences — results may be incomplete; "
            "use grep for these: gen/huge.go, gen/loop.py, tools/broken.py]"])
        self.assertEqual(refs["text"].splitlines()[-1], self.footer(refs)[0])  # the footer ends the result
        defs = self.run_op(op="defs", symbol="Huge")
        self.assertEqual(defs["text"].splitlines()[0], "(no definitions found)")
        self.assertEqual(self.footer(defs), [
            "[index coverage: 1 file failed to parse, 1 file over 1 MB not indexed — results may be incomplete; "
            "use grep for these: gen/huge.go, tools/broken.py]"])  # the cap concerns refs only
        found = self.run_op(op="defs", symbol="run_tool")
        self.assertEqual(found["text"].splitlines()[0].split(" ")[0], "tools/run.py:4-5")
        self.assertEqual(len(self.footer(found)), 1)

    def test_no_references_still_carries_the_footer(self) -> None:
        self.tools = self.commit_more({"tools/broken.py": "def broken(:\n"})
        res = self.run_op(op="refs", symbol="Nothing")
        lines = res["text"].splitlines()
        self.assertEqual(lines[1], "(no references found)")
        self.assertIn("1 file failed to parse", lines[2])

    def test_only_files_matching_the_glob_count(self) -> None:
        self.tools = self.commit_more({"tools/broken.py": "def broken(:\n", "gen/loop.py": "tick = 0\n" + "tick\n" * 5200})
        self.assertEqual(self.footer(self.run_op(op="refs", symbol="Handle", glob="api/**")), [])
        self.assertEqual(self.footer(self.run_op(op="defs", symbol="Handle", glob="web/**")), [])
        scoped = self.footer(self.run_op(op="refs", symbol="tick", glob="gen/**"))
        self.assertEqual(len(scoped), 1)
        self.assertIn("1 file with this identifier capped", scoped[0])
        self.assertNotIn("failed to parse", scoped[0])
        self.assertIn("1 file failed to parse", self.footer(self.run_op(op="defs", symbol="Handle", glob="tools"))[0])

    def test_at_most_ten_paths_are_named(self) -> None:
        self.tools = self.commit_more({f"bad/b{i:02d}.py": "def f(:\n" for i in range(13)})
        line = self.footer(self.run_op(op="defs", symbol="Handle"))[0]
        self.assertIn("13 files failed to parse", line)
        self.assertIn("bad/b09.py", line)
        self.assertNotIn("bad/b10.py", line)
        self.assertTrue(line.endswith("(+3 more)]"))

    def test_footer_paths_are_neither_provided_nor_mentioned(self) -> None:
        self.tools = self.commit_more({"tools/broken.py": "def broken(:\n"})
        res = self.run_op(op="refs", symbol="Handle", include_defs=True)
        self.assertIn("tools/broken.py", res["text"])
        self.assertEqual(provided_paths(res), ["api/handler.go", "cmd/main.go", "web/src/handler.ts"])
        self.assertNotIn("tools/broken.py", self.tools.mentioned_in(res))
        self.assertEqual(self.tools.mentioned_in(self.run_op(op="defs", symbol="Handle")), [])

    def test_coverage_comes_from_the_cached_index_too(self) -> None:
        self.tools = self.commit_more({"tools/broken.py": "def broken(:\n"})
        self.run_op(op="defs", symbol="Handle")
        warm = RepoTools(self.repo, self.tools.sha, cache_dir=self.cache)
        res = warm.run({"op": "defs", "symbol": "Handle"})
        self.assertEqual(warm.code_index().stats["parsed"], 0)
        self.assertIn("tools/broken.py", res["text"])


class BatchedRefsTests(IndexOpsCase):
    """W5: refs reads line texts through the batched blob reader, not one git process per file."""

    def test_many_files_cost_batches_not_one_process_per_file(self) -> None:
        self.tools = self.commit_more({f"callers/c{i:02d}.go": f"package c\n\nfunc Call{i}() {{\n\tPing({i})\n}}\n"
                                       for i in range(30)})
        self.tools.code_index()  # building the index is not what is measured
        calls: List[List[str]] = []
        original = self.tools._read_blobs

        def counting(ids: List[str]) -> Dict[str, bytes]:
            calls.append(list(ids))
            return original(ids)

        with patch.object(self.tools, "_read_blobs", side_effect=counting), \
                patch.object(self.tools, "_blob", side_effect=AssertionError("per-file read")):
            res = self.run_op(op="refs", symbol="Ping")
        rows = res["text"].splitlines()[1:]
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0], "callers/c00.go:4: \tPing(0)")
        self.assertEqual(rows[-1], "callers/c29.go:4: \tPing(29)")
        self.assertEqual(sum(len(ids) for ids in calls), 30)
        self.assertEqual(len(calls), 2)  # chunks of REFS_BLOB_CHUNK (25) files, not one call per file

    def test_output_is_identical_to_the_per_file_reads(self) -> None:
        res = self.run_op(op="refs", symbol="Draw", include_defs=True)
        self.assertEqual(res["text"].splitlines()[1:], ["ui/panel.go:5: func (p *Panel) Draw() {}",
                                                       "ui/widget.go:5: func (w *Widget) Draw() {}",
                                                       "ui/widget.go:8: \tw.Draw()"])

    def test_batch_failure_is_an_error_result_not_an_exception(self) -> None:
        self.tools.code_index()
        with patch.object(self.tools, "_read_blobs", side_effect=ContextError("git cat-file failed: boom")):
            res = self.run_op(op="refs", symbol="Handle")
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["error"])


if __name__ == "__main__":
    unittest.main()

"""P3 code wiki: front matter, storage, staleness, the wiki op, the first prompt, the writer and the CLI commands."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import code_wiki
import wiki_build
from code_wiki import (
    MAX_BODY_BYTES,
    BuildBusy,
    FrontMatterError,
    Page,
    PageNotFound,
    WikiError,
    WikiStore,
    basis_for,
    default_wiki_dir,
    dump_front_matter,
    page_state,
    parse_front_matter,
    wiki_enabled,
)
from debate_engine import DebateEngine
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER, LLMResult
from project_registry import Project, ProjectManager
from repo_mcp import RepoMcpServer
from repo_tools import RepoTools, provided_paths, provided_spans
from test_repo_tools import COMMIT, git, make_repo
from test_retrieval_loop import EngineHarness, FakeRunner, block
from testenv import isolate_data_dir, restore_data_dir
from wiki_build import BuildError, WikiBuilder, parse_answer, select_targets

PROJECT = "server-guardian"
AUTH_GO = ('package svc\n\nimport "strings"\n\n// Check validates a bearer token.\n'
           'func Check(token string) bool {\n\treturn strings.HasPrefix(token, "Bearer ")\n}\n')
AUTH_MOVED = 'package svc\n\n// Login starts a session.\nfunc Login(user string) string {\n\treturn user\n}\n'
GUARD_GO = 'package svc\n\nfunc Check(token string) bool {\n\treturn token != ""\n}\n'
SERVER_GO = 'package svc\n\nfunc Serve() string {\n\treturn "ok"\n}\n'
HELPER_PY = 'def help_me():\n    return 1\n\n\nclass Box:\n    def open(self):\n        return 2\n'
BODY = ("## 역할\n인증은 `svc/auth.go#Check` 에서 한다 (`svc/auth.go:6`).\n"
        "## 흐름\n`svc/server.go:3` 이 응답한다.\n")
GOLDEN_INTRO_HASHES = {  # sha256 of DebateEngine._retrieval_intro(...) as of p4 (full-path citation rule), without the wiki section
    ("a" * 40, ("a.py", "b/c.go"), True, None): "776fbf6eaf1df8ecbf2efe3dcf9e3f5570d23627a8dc3209c87ad28c05a7d554",
    ("a" * 40, ("a.py",), False, "GUIDE TEXT"): "b2f3e5bd7e19acf0e6939ed58661a57c132bdbde8bc777c6eca054c35a215dae",
    ("a" * 40, ("a.py", "b/c.go"), True, "GUIDE"): "4ef2c849f01d2be1a186e11b4827be978b57e4732fb46158d3105c5e5fc6ae0a",
}


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def add_svc(repo: Path) -> str:
    """The service package the pages talk about: Check lives in svc/auth.go line 6."""
    write(repo, "svc/auth.go", AUTH_GO)
    write(repo, "svc/server.go", SERVER_GO)
    write(repo, "svc/helper.py", HELPER_PY)
    write(repo, "docs/guide.md", "# Guide\n\n## Install\nrun it\n")
    return commit(repo, "svc")


def make_page(tools: Any, **over: Any) -> Page:
    fields: Dict[str, Any] = dict(
        id="svc", title="Service", scope=["svc/"], written_sha=tools.sha,
        basis=basis_for(tools, ["svc/auth.go", "svc/server.go"]),
        author={"kind": "llm", "provider": "claude", "job": "wiki_1"}, updated="2026-10-12T09:00:00Z", body=BODY,
        prompt_version=wiki_build.WIKI_PROMPT_VERSION)
    fields.update(over)
    return Page(**fields)


def wiki_store(project: str = PROJECT) -> WikiStore:
    directory = default_wiki_dir(project)
    assert directory is not None
    return WikiStore(directory)


def clean_wiki() -> None:
    shutil.rmtree(Path(os.environ["GUARDIAN_DATA_DIR"]) / "code-wiki", ignore_errors=True)


class WikiCase(unittest.TestCase):
    def setUp(self) -> None:
        clean_wiki()
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        clean_wiki()
        self._tmp.cleanup()


# --- front matter --------------------------------------------------------------------------------

class FrontMatterTests(unittest.TestCase):
    def test_page_roundtrips_with_unicode_quotes_and_dashes_in_the_body(self) -> None:
        page = Page(id="a-1", title='제목 "quoted" \\ back', scope=["cli/llm-agent/", "setup.sh"], written_sha="a" * 40,
                    basis={"cli/x y.py": "b" * 40, "z:colon.py": "c" * 40}, author={"kind": "human", "user": "owner"},
                    updated="2026-10-12T09:00:00Z", prompt_version="v1", unverified=["a.py:3", "b.go#X"],
                    body="## 역할\n---\n본문   줄 구분자와 --- 구분선\n\n```\n---\n```\n")
        text = page.to_text()
        self.assertTrue(text.startswith("---\nid: \"a-1\"\n"))
        self.assertEqual(Page.from_text(text), page)
        self.assertEqual(text.count("\n---\n"), 1 + 2)  # the closing line, and the two `---` lines of the body

    def test_basis_is_a_block_mapping_and_everything_else_one_line(self) -> None:
        text = Page(id="b", title="T", scope=["a/"], written_sha="d" * 40, basis={"a.py": "e" * 40},
                    author={"kind": "llm", "provider": "p", "job": "j"}, updated="2026-10-12T09:00:00Z", body="x").to_text()
        head = text.split("\n---\n", 1)[0].split("\n")
        self.assertIn("basis:", head)
        self.assertEqual(head[head.index("basis:") + 1], '  "a.py": "' + "e" * 40 + '"')

    def test_reader_accepts_bare_tokens_comments_and_a_json_flow_basis(self) -> None:
        text = ("---\n# a comment\nid: my-page\ntitle: \"T\"\nscope: [\"a/\"]\nwritten_sha: abcdef1\nbasis: {}\n"
                "author: {\"kind\": \"llm\", \"provider\": \"claude\", \"job\": \"j\"}\nupdated: 2026-10-12T09:00:00Z\n"
                "prompt_version: v1\nunverified: []\n---\nbody")
        page = Page.from_text(text)
        self.assertEqual((page.id, page.written_sha, page.basis, page.body), ("my-page", "abcdef1", {}, "body"))

    def test_malformed_front_matter_is_rejected(self) -> None:
        for text, fragment in [
            ("no front matter", "must start"),
            ("---\nid: \"a\"\n", "no closing"),
            ("---\nid: \"a\"\nid: \"b\"\n---\n", "duplicate key"),
            ("---\n  \"a\": \"b\"\n---\n", "unexpected indented"),
            ("---\nid: 'single quotes'\n---\n", "unsupported value"),
            ("---\nid: [1, 2\n---\n", "not a JSON value"),
            ("---\nBad Key: 1\n---\n", "expected `key: value`"),
            ("---\nbasis:\n  \"a\" \"b\"\n---\n", "expected `key: value`"),
            ("---\nbasis:\n  \"a\": \"b\"\n  \"a\": \"c\"\n---\n", "duplicate key"),
            ("---\nid:x\n---\n", "expected `key: value`"),
        ]:
            with self.subTest(text=text), self.assertRaises(FrontMatterError) as ctx:
                parse_front_matter(text)
            self.assertIn(fragment, str(ctx.exception))

    def test_invalid_fields_fail_validation(self) -> None:
        good = dict(id="ok", title="T", scope=["a/"], written_sha="a" * 40, basis={}, author={"kind": "llm", "provider": "p", "job": "j"},
                    updated="2026-10-12T09:00:00Z", body="x")
        Page(**good).validate()
        for key, value in [
            ("id", "../x"), ("id", "A"), ("id", "-a"), ("id", "a" * 65), ("id", ""), ("title", "two\nlines"), ("title", " "),
            ("scope", []), ("scope", "a/"), ("written_sha", "zzz"), ("basis", {"a": "short"}),
            ("author", {"kind": "robot"}), ("author", {"kind": "human", "user": "bad user"}),
            ("updated", "yesterday"), ("unverified", ["a\nb"]), ("body", "x" * (MAX_BODY_BYTES + 1)),
        ]:
            with self.subTest(key=key, value=str(value)[:20]), self.assertRaises(WikiError):
                Page(**{**good, key: value}).validate()
        Page(**{**good, "body": "x" * MAX_BODY_BYTES}).validate()  # the cap itself is allowed

    def test_dump_rejects_bad_keys(self) -> None:
        with self.assertRaises(FrontMatterError):
            dump_front_matter({"Bad Key": 1})


# --- storage -------------------------------------------------------------------------------------

class StoreTests(WikiCase):
    def store(self) -> WikiStore:
        return WikiStore(self.root / "code-wiki" / "proj")

    def page(self, page_id: str = "one", **over: Any) -> Page:
        fields: Dict[str, Any] = dict(id=page_id, title="T", scope=["a/"], written_sha="a" * 40, basis={},
                                      author={"kind": "llm", "provider": "p", "job": "j"},
                                      updated="2026-10-12T09:00:00Z", body="## x\ny")
        fields.update(over)
        return Page(**fields)

    def test_save_load_modes_and_atomic_replace(self) -> None:
        store = self.store()
        store.save(self.page())
        store.save(self.page(title="Second"))
        self.assertEqual(store.load("one").title, "Second")
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
        self.assertEqual(mode(store.pages_dir / "one.md"), 0o600)
        for directory in (store.directory.parent, store.directory, store.pages_dir):
            self.assertEqual(mode(directory), 0o700, directory)
        self.assertEqual(os.listdir(store.pages_dir), ["one.md"])  # no temp file is left behind

    def test_a_failed_save_keeps_the_old_page_and_leaves_no_temp_file(self) -> None:
        store = self.store()
        store.save(self.page(title="Old"))
        with patch("code_wiki.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            store.save(self.page(title="New"))
        self.assertEqual(store.load("one").title, "Old")
        self.assertEqual(os.listdir(store.pages_dir), ["one.md"])
        with self.assertRaises(WikiError):
            store.save(self.page(body="x" * (MAX_BODY_BYTES + 1)))
        self.assertEqual(os.listdir(store.pages_dir), ["one.md"])

    def test_ids_cannot_leave_the_pages_directory(self) -> None:
        store = self.store()
        for bad in ("../one", "a/b", ".hidden", "ONE", "one.md", ""):
            with self.subTest(bad=bad), self.assertRaises(WikiError):
                store.load(bad)
            with self.assertRaises(WikiError):
                store.delete(bad)
        with self.assertRaises(WikiError):
            store.save(self.page("../evil"))

    def test_missing_symlinked_oversized_and_mismatched_files(self) -> None:
        store = self.store()
        store.save(self.page())
        with self.assertRaises(PageNotFound):
            store.load("nope")
        outside = self.root / "outside.md"
        outside.write_text(self.page("link").to_text())
        os.symlink(outside, store.pages_dir / "link.md")
        with self.assertRaises(WikiError):
            store.load("link")
        (store.pages_dir / "big.md").write_text("x" * (code_wiki.MAX_PAGE_FILE_BYTES + 1))
        with self.assertRaises(WikiError):
            store.load("big")
        (store.pages_dir / "other.md").write_text(self.page("one").to_text())
        with self.assertRaisesRegex(WikiError, "declares id"):
            store.load("other")
        (store.pages_dir / "broken.md").write_text("not a page")
        pages, errors = store.load_all()
        self.assertEqual([p.id for p in pages], ["one"])
        self.assertEqual(sorted(e["id"] for e in errors), ["big", "broken", "link", "other"])
        self.assertEqual(store.ids(), ["big", "broken", "link", "one", "other"])

    def test_delete(self) -> None:
        store = self.store()
        store.save(self.page())
        self.assertTrue(store.delete("one"))
        self.assertFalse(store.delete("one"))

    def test_second_build_lock_is_refused_until_the_first_ends(self) -> None:
        store = self.store()
        with store.build_lock():
            with self.assertRaises(BuildBusy), self.store().build_lock():
                pass
        with store.build_lock():
            pass
        self.assertEqual(stat.S_IMODE(os.stat(store.directory / "build.lock").st_mode), 0o600)

    def test_wiki_dir_is_derived_from_the_data_dir_and_the_project(self) -> None:
        self.assertEqual(default_wiki_dir("my.proj"), Path(os.environ["GUARDIAN_DATA_DIR"]) / "code-wiki" / "my.proj")
        self.assertEqual(default_wiki_dir("a/../b").name, "a_.._b")
        self.assertIsNone(default_wiki_dir(""))
        self.assertIsNone(default_wiki_dir(".."))


# --- staleness -----------------------------------------------------------------------------------

class StalenessTests(WikiCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.root)
        self.sha1 = add_svc(self.repo)
        self.tools1 = RepoTools(self.repo, self.sha1)
        self.page = make_page(self.tools1)

    def at(self, sha: str, page: Optional[Page] = None) -> Dict[str, Any]:
        return page_state(page or self.page, RepoTools(self.repo, sha))

    def test_fresh_at_the_commit_it_was_written_for(self) -> None:
        details = self.at(self.sha1)
        self.assertEqual(details["state"], "fresh")
        self.assertEqual((details["changed"], details["removed"], details["new"], details["broken"]), ([], [], [], []))

    def test_unrelated_commits_keep_it_fresh(self) -> None:
        write(self.repo, "cli/other.py", "x = 1\n")
        self.assertEqual(self.at(commit(self.repo, "unrelated"))["state"], "fresh")

    def test_a_changed_basis_blob_makes_it_stale(self) -> None:
        write(self.repo, "svc/auth.go", AUTH_GO.replace("Bearer ", "Token "))  # Check is still there
        details = self.at(commit(self.repo, "edit"))
        self.assertEqual((details["state"], details["changed"], details["broken"]), ("stale", ["svc/auth.go"], []))

    def test_a_new_readable_file_in_scope_makes_it_stale_but_old_unbased_and_foreign_files_do_not(self) -> None:
        page = make_page(self.tools1, basis=basis_for(self.tools1, ["svc/auth.go"]))  # server.go, helper.py not in basis
        self.assertEqual(self.at(self.sha1, page)["state"], "fresh")
        write(self.repo, "svc/extra.go", "package svc\n")
        write(self.repo, "svc/.env", "SECRET=1\n")  # not readable: never counts
        write(self.repo, "cli/new_tool.py", "y = 2\n")  # outside the scope
        sha2 = commit(self.repo, "new files")
        details = self.at(sha2, page)
        self.assertEqual((details["state"], details["new"]), ("stale", ["svc/extra.go"]))

    def test_new_file_detection_is_skipped_when_the_written_commit_is_gone(self) -> None:
        page = make_page(self.tools1, written_sha="f" * 40, basis=basis_for(self.tools1, ["svc/auth.go"]))
        details = self.at(self.sha1, page)
        self.assertEqual((details["state"], details["new"]), ("fresh", []))
        self.assertIn("not available", details["notes"][0])

    def test_a_removed_basis_file_and_its_citation_break_the_page(self) -> None:
        git(self.repo, "rm", "-q", "svc/server.go")
        details = self.at(commit(self.repo, "rm"))
        self.assertEqual((details["state"], details["removed"]), ("broken", ["svc/server.go"]))
        self.assertEqual(details["broken"], [{"citation": "svc/server.go:3", "reason": "path not found"}])

    def test_a_plain_mention_relative_to_the_scope_is_not_broken_but_a_located_one_is(self) -> None:
        write(self.repo, "apps/web/scripts/build-ui.mjs", "x\n")
        write(self.repo, "scripts/other.sh", "y\n")  # makes `scripts` a known top-level directory
        tools = RepoTools(self.repo, commit(self.repo, "web"))
        mention = make_page(tools, scope=["apps/web/"], basis={}, body="## 역할\n`scripts/build-ui.mjs` builds.\n")
        self.assertEqual(page_state(mention, tools)["broken"], [])
        located = make_page(tools, scope=["apps/web/"], basis={}, body="## 역할\n`scripts/build-ui.mjs:1` builds.\n")
        self.assertEqual(page_state(located, tools)["broken"],
                         [{"citation": "scripts/build-ui.mjs:1", "reason": "path not found"}])
        elsewhere = make_page(tools, scope=["svc/"], basis={}, body="## 역할\n`scripts/build-ui.mjs` builds.\n")
        self.assertEqual(page_state(elsewhere, tools)["broken"][0]["reason"], "path not found")

    def test_bait_the_cited_symbol_moved_to_another_file(self) -> None:
        """The page says authentication is `svc/auth.go#Check`; the code now has it in svc/guard.go."""
        write(self.repo, "svc/auth.go", AUTH_MOVED)
        write(self.repo, "svc/guard.go", GUARD_GO)
        details = self.at(commit(self.repo, "move Check"))
        self.assertEqual(details["state"], "broken")
        self.assertEqual(details["broken"], [{"citation": "svc/auth.go#Check", "reason": "symbol not found"}])
        self.assertEqual(details["changed"], ["svc/auth.go"])
        self.assertEqual(details["new"], ["svc/guard.go"])

    def test_unverified_citations_recorded_at_write_time_make_it_broken(self) -> None:
        page = make_page(self.tools1, unverified=["svc/helper.py:3"])
        details = self.at(self.sha1, page)
        self.assertEqual((details["state"], details["unverified"], details["broken"]), ("broken", ["svc/helper.py:3"], []))

    def test_symbol_citations_python_methods_and_languages_without_a_parser(self) -> None:
        body = ("`cli/tool.py#run` `cli/tool.py#Box.open` `svc/helper.py#Box.open` `svc/helper.py#Gone` "
                "`docs/guide.md#Anything` `cli/tool.py#Box.close`")
        details = self.at(self.sha1, make_page(self.tools1, body=body))
        self.assertEqual([b["citation"] for b in details["broken"]], ["svc/helper.py#Gone", "cli/tool.py#Box.close"])
        self.assertTrue(all(b["reason"] == "symbol not found" for b in details["broken"]))

    def test_what_counts_as_a_citation_of_a_missing_path(self) -> None:
        body = "\n".join([
            "see https://example.com/a.html and api.example.com:443 and v1.2:3 and e.g. 3.5:1",  # not paths
            "bare name that exists elsewhere in the tree: server.go:3",  # resolvable, lenient
            "gone: svc/gone.go:12 and svc/gone2.go#Run and missing.go#X and svc/old.go",
            "unknown top-level directory is not ours: foo/bar.go and foo/baz.go:4-6 (the strong one counts)",
            "lines: svc/gone3.go line 9, and ./svc/gone4.go:2.",
        ])
        details = self.at(self.sha1, make_page(self.tools1, body=body))
        self.assertEqual([b["citation"] for b in details["broken"]], [
            "svc/gone.go:12", "svc/gone2.go#Run", "missing.go#X", "svc/old.go", "foo/baz.go:4-6",
            "svc/gone3.go:9", "svc/gone4.go:2"])

    def test_go_facts_can_vouch_for_a_symbol_the_index_does_not_have(self) -> None:
        class Facts:
            def find_defs(self, symbol: str) -> List[Dict[str, Any]]:
                return [{"path": "svc/auth.go"}] if symbol == "Ghost" else []

        class Provider:
            def has_go(self) -> bool:
                return True

            def peek(self) -> Any:
                return Facts()

        tools = RepoTools(self.repo, self.sha1)
        page = make_page(tools, body="`svc/auth.go#Ghost` and `svc/auth.go#Phantom`")
        self.assertEqual([b["citation"] for b in page_state(page, tools)["broken"]],
                         ["svc/auth.go#Ghost", "svc/auth.go#Phantom"])
        tools = RepoTools(self.repo, self.sha1, go_provider=Provider())  # type: ignore[arg-type]
        self.assertEqual([b["citation"] for b in page_state(page, tools)["broken"]], ["svc/auth.go#Phantom"])


# --- generated layer and the wiki op -------------------------------------------------------------

class WikiOpTests(WikiCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.root)
        self.sha = add_svc(self.repo)
        self.store = WikiStore(self.root / "wiki")
        base = RepoTools(self.repo, self.sha)
        self.store.save(make_page(base))
        self.store.save(make_page(base, id="tool", title="CLI tool", scope=["cli/tool.py"], basis=basis_for(base, ["cli/tool.py"]),
                                  body="## 역할\n`cli/tool.py:4` 는 run 을 정의한다. 검색어: Zebra\n"))
        self.tools = RepoTools(self.repo, self.sha, wiki_dir=self.store.directory)

    def test_page_result_has_the_header_the_generated_overview_and_then_the_narrative(self) -> None:
        res = self.tools.run({"op": "wiki", "page": "svc"})
        self.assertTrue(res["ok"], res)
        lines = res["text"].split("\n")
        self.assertEqual(lines[0], "[wiki page svc — navigation notes, not evidence; state: fresh]")
        overview = next(i for i, line in enumerate(lines) if line.startswith("--- Generated overview of svc/"))
        narrative = next(i for i, line in enumerate(lines) if line.startswith("--- Narrative: Service"))
        self.assertLess(overview, narrative)
        self.assertTrue(any("--- Repository map of svc" in line for line in lines[overview:narrative]))
        self.assertIn("not evidence", lines[narrative])
        self.assertIn("인증은 `svc/auth.go#Check`", "\n".join(lines[narrative:]))
        self.assertEqual((res["op"], res["target"]), ("wiki", "svc"))

    def test_file_scope_uses_the_outline_as_the_generated_layer(self) -> None:
        res = self.tools.run({"op": "wiki", "page": "tool"})
        self.assertIn("[outline of cli/tool.py: line: declaration]", res["text"])

    def test_stale_and_broken_pages_carry_a_warning_block(self) -> None:
        write(self.repo, "svc/auth.go", AUTH_MOVED)
        sha2 = commit(self.repo, "move")
        tools = RepoTools(self.repo, sha2, wiki_dir=self.store.directory)
        text = tools.run({"op": "wiki", "page": "svc"})["text"]
        self.assertTrue(text.startswith("[wiki page svc — navigation notes, not evidence; state: broken]\nWARNING:"))
        self.assertIn("These files changed after this narrative was written: svc/auth.go", text)
        self.assertIn("Broken citations: svc/auth.go#Check (symbol not found)", text)

    def test_query_lists_matching_pages_with_their_state(self) -> None:
        res = self.tools.run({"op": "wiki", "query": "zebra"})
        self.assertEqual(res["text"].split("\n")[0], '[wiki search "zebra": 1 of 2 pages — navigation notes, not evidence]')
        self.assertEqual(res["text"].split("\n")[1], "tool  [fresh]  CLI tool  (cli/tool.py)")
        everything = self.tools.run({"op": "wiki"})["text"].split("\n")
        self.assertEqual(everything[0], "[wiki pages: 2 of 2 pages — navigation notes, not evidence]")
        self.assertEqual(len(everything), 3)
        by_scope = self.tools.run({"op": "wiki", "query": "SVC/"})["text"]
        self.assertIn("svc  [fresh]  Service  (svc/)", by_scope)

    def test_query_lists_at_most_twenty_pages(self) -> None:
        base = RepoTools(self.repo, self.sha)
        for n in range(25):
            self.store.save(make_page(base, id=f"extra-{n:02d}", title=f"Extra {n}"))
        lines = self.tools.run({"op": "wiki", "query": "extra"})["text"].split("\n")
        self.assertEqual(len(lines), 1 + 20 + 1)
        self.assertEqual(lines[-1], "... 5 more (narrow the query)")

    def test_errors(self) -> None:
        self.assertEqual(self.tools.run({"op": "wiki", "page": "nope"})["error"], "page not found (search with query)")
        self.assertEqual(self.tools.run({"op": "wiki", "page": "../x"})["error"], "page id must match ^[a-z0-9][a-z0-9-]{0,63}$")
        self.assertEqual(self.tools.run({"op": "wiki", "page": 5})["error"], "page must be a page id")
        self.assertIn("query must be text", self.tools.run({"op": "wiki", "query": "x" * 500})["error"])
        none = RepoTools(self.repo, self.sha)
        self.assertEqual(none.run({"op": "wiki", "page": "svc"})["error"], "the code wiki is not available")
        empty = RepoTools(self.repo, self.sha, wiki_dir=self.root / "no-such-dir")
        self.assertIn("no pages", empty.run({"op": "wiki"})["text"])

    def test_a_wiki_result_provides_no_file_span_or_mention(self) -> None:
        """Nothing a page says is shown code: the citation check gets no file, no span and no mention from it."""
        page = self.tools.run({"op": "wiki", "page": "svc"})
        self.assertIn("svc/auth.go", page["text"])  # the page does name the path ...
        for request in ({"op": "wiki", "page": "svc"}, {"op": "wiki", "query": "svc"}, {"op": "wiki"}):
            res = self.tools.run(request)
            self.assertTrue(res["ok"])
            self.assertEqual(provided_paths(res), [])
            self.assertEqual(provided_spans(res), {})
            self.assertEqual(self.tools.mentioned_in(res), [])  # ... but that is not even a mention

    def test_oversized_narratives_are_cut_and_flagged(self) -> None:
        base = RepoTools(self.repo, self.sha)
        self.store.save(make_page(base, id="huge", body="## x\n" + ("line of text\n" * 8000)))
        res = self.tools.run({"op": "wiki", "page": "huge"})
        self.assertTrue(res["truncated"])
        self.assertIn("page narrative cut by Guardian", res["text"])
        self.assertLess(res["bytes"], 60 * 1024)

    def test_describe_request(self) -> None:
        from repo_tools import describe_request

        self.assertEqual(describe_request({"op": "wiki", "page": "svc"}), {"op": "wiki", "target": "svc"})
        self.assertEqual(describe_request({"op": "wiki", "query": "auth"}), {"op": "wiki", "target": "auth"})

    def test_mcp_tool_serves_the_op_and_logs_no_paths(self) -> None:
        log = self.root / "calls.jsonl"
        server = RepoMcpServer(self.tools, str(log))
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "repo_wiki", "arguments": {"page": "svc", "bogus": 1}}})
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertTrue(result["content"][0]["text"].startswith("[wiki page svc — navigation notes, not evidence; state: fresh]"))
        record = json.loads(log.read_text().splitlines()[0])
        self.assertEqual((record["op"], record["target"], record["ok"]), ("wiki", "svc", True))
        self.assertEqual((record["paths"], record["spans"], record["mentions"]), ([], {}, []))
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(listed[-1]["name"], "repo_wiki")
        self.assertEqual(listed[-1]["inputSchema"]["properties"].keys(), {"page", "query"})

    def test_the_engine_hands_the_wiki_dir_to_the_mcp_server_command(self) -> None:
        from debate_engine import McpTurn
        from llm_runner import LLMRunner

        turn = McpTurn(self.tools, {})
        self.addCleanup(turn.close)
        self.assertEqual(turn.spec["wiki_dir"], str(self.store.directory))
        self.assertNotIn("wiki_dir", McpTurn(RepoTools(self.repo, self.sha), {}).spec)
        with TemporaryDirectory() as scratch:
            path = LLMRunner()._claude_mcp_config(turn.spec, {"PATH": "/usr/bin"}, Path(scratch))
            args = json.loads(path.read_text())["mcpServers"]["repo"]["args"]
        self.assertEqual(args[args.index("--wiki-dir") + 1], str(self.store.directory))

    def test_mcp_server_process_takes_the_wiki_dir(self) -> None:
        from mcp_stdio import StdioMCPClient

        argv = [sys.executable, str(Path(__file__).with_name("repo_mcp.py")), "--workspace", str(self.repo), "--sha", self.sha,
                "--wiki-dir", str(self.store.directory)]
        with StdioMCPClient(argv[0], argv[1:], env={**os.environ}) as client:
            self.assertIn("state: fresh", client.call_tool("repo_wiki", {"page": "svc"}))


# --- the first prompt, snapshot and events -------------------------------------------------------

class WikiPromptTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        # The debate exposure is off by default since 2026-10-18; these tests cover it switched on.
        wiki_on = patch.dict(os.environ, {"GUARDIAN_CODE_WIKI": "1"})
        wiki_on.start()
        self.addCleanup(wiki_on.stop)
        clean_wiki()
        self.sha = add_svc(self.repo)
        self.tools = RepoTools(self.repo, self.sha)
        self.store = wiki_store()

    def tearDown(self) -> None:
        clean_wiki()
        super().tearDown()

    def first_prompt(self, **kwargs: Any) -> str:
        runner = FakeRunner(self.plain_scripts(["final"]))
        self.last_result = self.run_debate(self.engine(), runner, **kwargs)
        return runner.prompts("expert")[0]

    def started(self) -> Dict[str, Any]:
        return next(e for e in self.events() if e["type"] == "debate_started")["data"]["context"]

    def test_the_intro_without_a_fresh_page_is_byte_identical_to_p4(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GUARDIAN_RETRIEVAL_")}
        with patch.dict(os.environ, env, clear=True):
            for (sha, listed, with_list, guide), digest in GOLDEN_INTRO_HASHES.items():
                text = DebateEngine._retrieval_intro(sha, list(listed), with_list, guide)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
                self.assertEqual(DebateEngine._retrieval_intro(sha, list(listed), with_list, guide, wiki=""), text)

    def test_no_pages_means_no_wiki_words_in_the_prompt_and_zero_counts(self) -> None:
        prompt = self.first_prompt()
        self.assertNotIn("wiki", prompt.lower())
        context = self.started()
        self.assertEqual((context["wiki_pages"], context["wiki_stale"]), (0, 0))
        self.assertNotIn("wiki", self.last_result["context"])

    def test_only_stale_pages_are_counted_but_not_listed(self) -> None:
        self.store.save(make_page(self.tools, scope=["svc/"], basis=basis_for(self.tools, ["svc/auth.go"]), unverified=["x.py:1"]))
        prompt = self.first_prompt()
        self.assertNotIn("Code wiki", prompt)
        self.assertEqual((self.started()["wiki_pages"], self.started()["wiki_stale"]), (1, 1))
        self.assertEqual(self.last_result["context"]["wiki"], {"shown": [], "states": {"svc": "broken"}})

    def test_fresh_pages_are_listed_after_the_map_with_the_guidance(self) -> None:
        self.store.save(make_page(self.tools))
        self.store.save(make_page(self.tools, id="old", title="Old page", unverified=["a.py:1"]))
        self.store.save(make_page(self.tools, id="tool", title="Tool " + "x" * 180, scope=["cli/tool.py"]))
        prompt = self.first_prompt()
        start = prompt.index("=== Code wiki (navigation notes) ===")
        end = prompt.index("=== End of code wiki ===")
        self.assertLess(prompt.index("--- Repository map"), start)
        self.assertLess(end, prompt.index("=== End of repository retrieval ==="))
        section = prompt[start:end]
        self.assertIn("not evidence", section)
        self.assertIn("the code is right", section)
        self.assertIn('{"op": "wiki", "page": "<id>"}', section)
        self.assertIn("svc — Service (svc/)", section)
        self.assertIn("tool — Tool " + "x" * 114 + "… (cli/tool.py)", section)  # titles are cut at 120 characters
        self.assertNotIn("old — ", section)  # broken: not in the first prompt
        self.assertIn("1 more pages are stale or broken", section)
        self.assertNotIn("인증은", prompt)  # bodies are never in the prompt
        self.assertEqual((self.started()["wiki_pages"], self.started()["wiki_stale"]), (3, 1))
        self.assertEqual(self.last_result["context"]["wiki"], {"shown": ["svc", "tool"],
                                                              "states": {"old": "broken", "svc": "fresh", "tool": "fresh"}})
        self.assertEqual(self.started()["prompt_version"], "2026-11-11.p29")

    def test_at_most_forty_fresh_pages_are_listed(self) -> None:
        for n in range(45):
            self.store.save(make_page(self.tools, id=f"p{n:02d}", title=f"Page {n}"))
        prompt = self.first_prompt()
        section = prompt[prompt.index("=== Code wiki"):prompt.index("=== End of code wiki")]
        self.assertEqual(sum(1 for line in section.split("\n") if line.startswith("p") and " — Page " in line), 40)
        self.assertIn("... 5 more fresh pages", section)
        self.assertEqual(len(self.last_result["context"]["wiki"]["shown"]), 40)

    def test_switching_the_wiki_off_restores_the_prompt_and_the_op(self) -> None:
        self.store.save(make_page(self.tools))
        with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI": "0"}):
            prompt = self.first_prompt()
        self.assertNotIn("wiki", prompt.lower())
        self.assertEqual(self.started()["wiki_pages"], 0)
        engine = self.engine()
        with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI": "0"}):
            engine._setup_retrieval(None, None, PROJECT)
        self.assertEqual(engine._repo.run({"op": "wiki", "page": "svc"})["error"], "the code wiki is not available")

    def test_the_wiki_is_hidden_from_agents_by_default(self) -> None:
        self.store.save(make_page(self.tools))
        env = {k: v for k, v in os.environ.items() if k != "GUARDIAN_CODE_WIKI"}
        with patch.dict(os.environ, env, clear=True):
            prompt = self.first_prompt()
            engine = self.engine()
            engine._setup_retrieval(None, None, PROJECT)
            self.assertEqual(engine._repo.run({"op": "wiki", "page": "svc"})["error"], "the code wiki is not available")
        self.assertNotIn("wiki", prompt.lower())
        self.assertEqual(self.started()["wiki_pages"], 0)
        for value in ("1", "true", "On", "yes"):
            with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI": value}):
                self.assertTrue(wiki_enabled(), value)
        for value in ("0", "", "off", "maybe"):
            with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI": value}):
                self.assertFalse(wiki_enabled(), value)

    def test_a_broken_wiki_directory_never_stops_the_debate(self) -> None:
        self.store.save(make_page(self.tools))
        with patch("debate_engine.WikiView", side_effect=RuntimeError("boom")):
            prompt = self.first_prompt()
        self.assertNotIn("Code wiki", prompt)

    def test_bait_a_stale_page_the_agent_cites_through_the_wiki_is_not_evidence(self) -> None:
        """The page says authentication is `svc/auth.go#Check`; the code moved it. An answer that cites svc/auth.go:6 after
        reading only the wiki page is caught by the citation check, and the wiki op shows no file as provided."""
        self.store.save(make_page(self.tools))
        write(self.repo, "svc/auth.go", AUTH_MOVED)
        write(self.repo, "svc/guard.go", GUARD_GO)
        self.sha = commit(self.repo, "move Check")
        wiki_read = block({"op": "wiki", "page": "svc"})
        runner = FakeRunner(self.plain_scripts([wiki_read, "인증은 svc/auth.go:6 에서 한다.", "Check는 옮겨졌고 확인하지 못했다."]))
        result = self.run_debate(self.engine(), runner)
        prompts = runner.prompts("expert")
        # Broken: not offered in the first prompt, but counted and reachable.
        self.assertNotIn("Code wiki", prompts[0])
        self.assertEqual((self.started()["wiki_pages"], self.started()["wiki_stale"]), (1, 1))
        # The wiki op answered, with the state and the broken citation, and no word of it counts as code.
        self.assertIn("state: broken", prompts[1])
        self.assertIn("Broken citations: svc/auth.go#Check (symbol not found)", prompts[1])
        served = next(e for e in self.events() if e["type"] == "retrieval_served")["data"]["results"][0]
        self.assertEqual((served["op"], served["target"], served["ok"]), ("wiki", "svc", True))
        expert = next(t for t in result["transcript"] if t["role"] == "Domain Expert")
        self.assertEqual(expert["retrievals"][0]["paths"], [])
        # Citing the path from the page alone is unverified and asked about once.
        self.assertIn("CITATION CHECK", prompts[2])
        self.assertIn("never given: svc/auth.go.", prompts[2])
        check = next(e for e in self.events() if e["type"] == "citation_check")["data"]
        self.assertEqual((check["unverified"], check["asked"], check["mentioned"]), (["svc/auth.go"], True, 0))
        self.assertNotIn("provided_files", result["context"])
        self.assertNotIn("provided_spans", result["context"])

    def test_after_reading_the_code_the_same_citation_passes(self) -> None:
        self.store.save(make_page(self.tools))
        runner = FakeRunner(self.plain_scripts([block({"op": "wiki", "page": "svc"}), block({"op": "read", "path": "svc/auth.go"}),
                                                "Check is at svc/auth.go:6."]))
        self.run_debate(self.engine(), runner)
        self.assertEqual(len(runner.prompts("expert")), 3)  # no citation question
        check = next(e for e in self.events() if e["type"] == "citation_check")["data"]
        self.assertEqual(check["unverified"], [])


# --- the writer ----------------------------------------------------------------------------------

PAGE_ANSWER = ("# 서비스\n## 역할\n`svc/auth.go:6` 에서 토큰을 확인한다.\n## 흐름\n`svc/server.go:3` 이 응답한다.\n"
               "## 주의점\n없음\n## 관련 페이지\n없음\n")
READ_BOTH = block({"op": "read", "path": "svc/auth.go"}, {"op": "read", "path": "svc/server.go"})


class BuildCase(WikiCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.root)
        self.sha = add_svc(self.repo)
        self.project = Project(id="wikitest", workspace_raw=str(self.repo))
        patcher = patch.object(ProjectManager, "load", return_value=self.project)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events_path = self.root / "events.jsonl"
        self.store = wiki_store("wikitest")

    def builder(self, **kwargs: Any) -> WikiBuilder:
        return WikiBuilder("wikitest", events=EventWriter(str(self.events_path)), db_path=self.root / "d.db", **kwargs)

    def build(self, runner: Any, builder: Optional[WikiBuilder] = None, **run: Any) -> Dict[str, Any]:
        builder = builder or self.builder()
        with patch.object(builder.engine.llm, "run_session_prompt", side_effect=runner):
            try:
                return builder.run(**run)
            finally:
                builder.events.close()

    def events(self) -> List[Dict[str, Any]]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()]

    def by_type(self, kind: str) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events() if e["type"] == kind]

    @staticmethod
    def scripts(*replies: str) -> FakeRunner:
        return FakeRunner({"expert": list(replies), "critic": ["c"], "judge": ["j"]})


class SelectionTests(BuildCase):
    def tools(self) -> RepoTools:
        return RepoTools(self.repo, self.sha)

    def test_default_set_covers_top_level_and_code_directories_below_them(self) -> None:
        for rel in ("apps/web-desktop/a.tsx", "apps/web-desktop/b.ts", "tools/goindex/main.go", "tools/goindex/x.go",
                    "scripts/ci.sh", "scripts/lib/a.sh", "scripts/x.sh", ".design-sync/previews/p.tsx",
                    ".design-sync/previews/q.tsx", "lone/only.py"):
            write(self.repo, rel, "x\n")
        write(self.repo, "apps/agent/extra.go", "package a\n")
        sha = commit(self.repo, "more")
        found = {c["id"]: c["scope"] for c in wiki_build.default_candidates(RepoTools(self.repo, sha))}
        self.assertEqual(found, {
            "apps-agent": ["apps/agent/"], "apps-web-desktop": ["apps/web-desktop/"], "scripts": ["scripts/"],
            "svc": ["svc/"], "tools-goindex": ["tools/goindex/"]})  # apps/agent: main.go + extra.go; lone/ has one file

    def test_default_set_is_capped_at_twenty_keeping_the_biggest_directories(self) -> None:
        for n in range(25):
            for k in range(2 + n % 3):
                write(self.repo, f"pkg/d{n:02d}/f{k}.py", "x = 1\n")
        sha = commit(self.repo, "many")
        chosen = wiki_build.default_page_set(RepoTools(self.repo, sha))
        self.assertEqual(len(chosen), 20)
        self.assertEqual([c["scope"][0] for c in chosen], sorted(c["scope"][0] for c in chosen))
        scopes = {c["scope"][0] for c in chosen}
        self.assertTrue(all(f"pkg/d{n:02d}/" in scopes for n in range(2, 25, 3)))  # the 8 directories with 4 files

    def test_ids_are_derived_from_paths_and_unique(self) -> None:
        self.assertEqual(wiki_build.derive_id("cli/llm-agent/"), "cli-llm-agent")
        self.assertEqual(wiki_build.derive_id("A b/ü"), "a-b")
        self.assertEqual(wiki_build.derive_id("/"), "root")
        write(self.repo, "a/b-c/x.py", "1\n")
        write(self.repo, "a/b-c/y.py", "1\n")
        write(self.repo, "a-b/c/x.py", "1\n")
        write(self.repo, "a-b/c/y.py", "1\n")
        ids = [c["id"] for c in wiki_build.default_candidates(RepoTools(self.repo, commit(self.repo, "clash")))]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({"a-b-c", "a-b-c-2"} <= set(ids), True)

    def test_selection_rules(self) -> None:
        tools = self.tools()
        self.assertEqual([t.id for t in select_targets(self.store, tools, [], False, False)], ["svc"])  # no pages yet
        self.store.save(make_page(tools))
        self.store.save(make_page(tools, id="odd", title="Odd", unverified=["x.py:1"]))
        with self.assertRaisesRegex(BuildError, "pass --page"):
            select_targets(self.store, tools, [], False, False)
        self.assertEqual([t.id for t in select_targets(self.store, tools, [], False, True)], ["odd", "svc"])
        self.assertEqual([t.id for t in select_targets(self.store, tools, [], True, False)], ["odd"])
        self.assertEqual([t.id for t in select_targets(self.store, tools, ["svc", "svc"], False, False)], ["svc"])
        with self.assertRaises(BuildError) as ctx:
            select_targets(self.store, tools, ["ghost"], False, False)
        self.assertEqual(ctx.exception.code, 3)
        with self.assertRaisesRegex(BuildError, "only one of"):
            select_targets(self.store, tools, ["svc"], True, False)
        with self.assertRaisesRegex(BuildError, "invalid page id"):
            select_targets(self.store, tools, ["../x"], False, False)

    def test_page_ids_of_default_directories_can_be_created_by_name(self) -> None:
        targets = select_targets(self.store, self.tools(), ["svc"], False, False)
        self.assertEqual((targets[0].id, targets[0].scope, targets[0].existing), ("svc", ["svc/"], None))

    def test_stale_also_picks_llm_pages_written_with_another_prompt_version(self) -> None:
        tools = self.tools()
        self.store.save(make_page(tools, id="old-prompt", prompt_version="ancient"))
        self.store.save(make_page(tools, id="by-hand", prompt_version="", author={"kind": "human", "user": "me"}))
        self.assertEqual([t.id for t in select_targets(self.store, tools, [], True, False)], ["old-prompt"])


class AnswerParsingTests(unittest.TestCase):
    def test_title_sections_and_findings(self) -> None:
        title, body = parse_answer("preface\n# 제목 **x**\n\n## 역할\nA\n## 흐름\nB\n```findings\n[{\"claim\": \"c\"}]\n```\n")
        self.assertEqual(title, "제목 **x")
        self.assertEqual(body, "## 역할\nA\n## 흐름\nB\n")

    def test_fenced_answers_are_unwrapped(self) -> None:
        title, body = parse_answer("```markdown\n# T\n## a\nx\n## b\ny\n```")
        self.assertEqual((title, body), ("T", "## a\nx\n## b\ny\n"))

    def test_answers_without_sections_are_rejected(self) -> None:
        for text in ("just prose", "# T\n## only one\nx"):
            with self.subTest(text=text), self.assertRaises(WikiError):
                parse_answer(text)


class WriterTests(BuildCase):
    def test_writes_a_fresh_page_from_a_checked_session(self) -> None:
        report = self.build(self.scripts(READ_BOTH, PAGE_ANSWER))
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["pages"], ["svc"])
        self.assertEqual(report["written"], [{"page": "svc", "state": "fresh", "basis_files": 2, "citations": 2, "unverified": []}])
        page = self.store.load("svc")
        self.assertEqual((page.title, page.scope, page.written_sha, page.unverified), ("서비스", ["svc/"], self.sha, []))
        self.assertEqual(page.basis, basis_for(RepoTools(self.repo, self.sha), ["svc/auth.go", "svc/server.go"]))
        self.assertEqual((page.author["kind"], page.author["provider"]), ("llm", "claude"))
        self.assertTrue(page.author["job"].startswith("wiki_"))
        self.assertEqual(page.prompt_version, wiki_build.WIKI_PROMPT_VERSION)
        self.assertTrue(page.body.startswith("## 역할\n"))
        self.assertEqual(stat.S_IMODE(os.stat(self.store.pages_dir / "svc.md").st_mode), 0o600)
        self.assertEqual(page_state(page, RepoTools(self.repo, self.sha))["state"], "fresh")

    def test_events(self) -> None:
        self.build(self.scripts(READ_BOTH, PAGE_ANSWER))
        kinds = [e["type"] for e in self.events()]
        self.assertEqual(kinds[0], "wiki_build_started")
        self.assertEqual(kinds[-2:], ["wiki_page_written", "wiki_build_finished"])
        self.assertIn("retrieval_served", kinds)
        self.assertEqual(self.by_type("wiki_build_started")[0]["pages"], ["svc"])
        self.assertEqual(self.by_type("wiki_page_written"), [
            {"page": "svc", "state": "fresh", "basis_files": 2, "citations": 2, "unverified": []}])
        self.assertEqual(self.by_type("wiki_build_finished"), [{"written": 1, "skipped": 0, "failed": 0}])
        self.assertTrue(all(e["thread_id"].startswith("wiki_") for e in self.events()))

    def test_the_prompt_asks_for_korean_sections_and_hides_the_other_pages(self) -> None:
        self.store.save(make_page(RepoTools(self.repo, self.sha), id="other", title="Zebra title", unverified=["a.py:1"]))
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        self.build(runner, page_ids=["svc"])
        first = runner.prompts("expert")[0]
        for needle in ("## 역할", "## 흐름", "## 주의점", "## 관련 페이지", "Write your whole reply in Korean",
                       "Scope: svc/", "--- Overview of the scope", "Repository map of svc", "[[id]]", "do not invent ids): other",
                       "Cite the code for EVERY claim", "=== Repository retrieval (read-only) ==="):
            self.assertIn(needle, first)
        self.assertNotIn("Code wiki (navigation notes)", first)  # the writer never sees other pages' text
        self.assertNotIn("Zebra title", first)
        # wiki7: no debate words in the language rule, one size, the findings block named
        self.assertNotIn("decision, rationale, constraints", first)
        self.assertIn("Keep the page under about 6000 characters", first)
        self.assertIn("A ```findings block after the page is allowed", first)

    def test_link_candidates_put_the_family_first_and_reach_past_sixty(self) -> None:
        known = [f"a{i:03}" for i in range(100)] + ["zz", "zz--one", "zz--two"]
        offered = wiki_build.link_candidates("zz--one", known)
        self.assertEqual(offered[:2], ["zz", "zz--two"])
        self.assertNotIn("zz--one", offered)
        self.assertEqual(len(offered), 102)
        self.assertGreaterEqual(wiki_build.MAX_LINK_IDS, len(offered))

    def test_wiki_op_is_not_available_to_the_writer_session(self) -> None:
        self.store.save(make_page(RepoTools(self.repo, self.sha), id="other", title="Other"))
        runner = self.scripts(block({"op": "wiki", "page": "other"}), PAGE_ANSWER)
        self.build(runner, page_ids=["svc"])
        self.assertIn("the code wiki is not available", runner.prompts("expert")[1])

    def test_unverified_citations_after_the_reask_are_stored_and_break_the_page(self) -> None:
        # Only svc/auth.go was read: the page cites svc/server.go too. The re-ask is answered with the same citation.
        runner = self.scripts(block({"op": "read", "path": "svc/auth.go"}), PAGE_ANSWER, PAGE_ANSWER)
        report = self.build(runner)
        self.assertEqual(len(runner.prompts("expert")), 3)
        self.assertIn("CITATION CHECK", runner.prompts("expert")[2])
        self.assertEqual(report["written"][0]["state"], "broken")
        self.assertEqual(report["written"][0]["unverified"], ["svc/server.go"])
        self.assertEqual(self.store.load("svc").unverified, ["svc/server.go"])
        self.assertEqual(self.store.load("svc").basis, basis_for(RepoTools(self.repo, self.sha), ["svc/auth.go"]))

    def test_a_reask_that_drops_the_claim_leaves_a_clean_page(self) -> None:
        fixed = PAGE_ANSWER.replace("`svc/server.go:3` 이 응답한다.", "응답은 서버가 한다.")
        report = self.build(self.scripts(block({"op": "read", "path": "svc/auth.go"}), PAGE_ANSWER, fixed))
        self.assertEqual((report["written"][0]["state"], report["written"][0]["unverified"]), ("fresh", []))

    def test_basis_keeps_shown_files_that_are_in_scope_or_cited_and_drops_the_rest(self) -> None:
        reads = block({"op": "read", "path": "svc/auth.go"}, {"op": "read", "path": "svc/server.go"},
                      {"op": "read", "path": "cli/tool.py"}, {"op": "read", "path": "apps/agent/main.go"},
                      {"op": "read", "path": "svc/helper.py"})
        answer = PAGE_ANSWER.replace("없음\n## 관련", "`apps/agent/main.go:7` 도 본다.\n## 관련", 1)
        report = self.build(self.scripts(reads, answer))
        self.assertEqual(sorted(self.store.load("svc").basis),
                         ["apps/agent/main.go", "svc/auth.go", "svc/helper.py", "svc/server.go"])  # cli/tool.py: neither
        self.assertEqual(report["written"][0]["basis_files"], 4)

    def test_a_fallback_answer_never_becomes_a_page(self) -> None:
        def canned(prompt: str, *args: Any, **kwargs: Any) -> LLMResult:
            return LLMResult(BUILTIN_PROVIDER, "## 역할\ncanned\n## 흐름\ncanned", failures=["claude: not installed"])

        report = self.build(canned)
        self.assertEqual(report["written"], [])
        self.assertEqual(report["failed"], 1)
        self.assertTrue(report["skipped"][0]["reason"].startswith("llm_fallback"))
        self.assertEqual(self.store.ids(), [])
        self.assertEqual(self.by_type("wiki_page_skipped")[0]["page"], "svc")

    def test_a_fallback_leaves_an_existing_page_untouched(self) -> None:
        self.store.save(make_page(RepoTools(self.repo, self.sha), title="Keep me"))
        before = (self.store.pages_dir / "svc.md").read_text()

        def canned(prompt: str, *args: Any, **kwargs: Any) -> LLMResult:
            return LLMResult(BUILTIN_PROVIDER, "canned", failures=[])

        report = self.build(canned, page_ids=["svc"])
        self.assertEqual(report["failed"], 1)
        self.assertEqual((self.store.pages_dir / "svc.md").read_text(), before)

    def test_answers_without_sections_or_empty_answers_are_skipped(self) -> None:
        report = self.build(self.scripts("I could not do it."))
        self.assertTrue(report["skipped"][0]["reason"].startswith("bad_format"))
        self.assertEqual(self.store.ids(), [])
        report = self.build(self.scripts("   "))
        self.assertTrue(report["skipped"][0]["reason"].startswith("no_answer"))

    def test_human_pages_are_kept_unless_forced(self) -> None:
        tools = RepoTools(self.repo, self.sha)
        self.store.save(make_page(tools, title="By hand", author={"kind": "human", "user": "owner"}, prompt_version=""))
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        report = self.build(runner, page_ids=["svc"])
        self.assertEqual(runner.calls, [])  # not even asked
        self.assertEqual(report["failed"], 0)
        self.assertIn("human-written page kept (state: fresh)", report["skipped"][0]["reason"])
        self.assertEqual(self.store.load("svc").title, "By hand")
        report = self.build(self.scripts(READ_BOTH, PAGE_ANSWER), builder=self.builder(force=True), page_ids=["svc"])
        self.assertEqual(report["written"][0]["page"], "svc")
        self.assertEqual(self.store.load("svc").author["kind"], "llm")

    def test_a_page_saved_by_a_person_during_the_session_wins(self) -> None:
        tools = RepoTools(self.repo, self.sha)

        def edit_then_answer(prompt: str, *args: Any, **kwargs: Any) -> LLMResult:
            self.store.save(make_page(tools, title="By hand", author={"kind": "human", "user": "owner"}))
            return LLMResult("claude-cli", PAGE_ANSWER, "expert-s1")

        report = self.build(edit_then_answer)
        self.assertIn("saved during the build", report["skipped"][0]["reason"])
        self.assertEqual(self.store.load("svc").title, "By hand")

    def test_stale_rewrite_gets_the_existing_narrative_and_what_changed(self) -> None:
        self.build(self.scripts(READ_BOTH, PAGE_ANSWER))
        write(self.repo, "svc/auth.go", AUTH_GO.replace("Bearer ", "Token "))
        sha2 = commit(self.repo, "edit")
        runner = self.scripts(READ_BOTH, PAGE_ANSWER.replace("확인한다", "다시 확인한다"))
        report = self.build(runner, stale=True)
        first = runner.prompts("expert")[0]
        self.assertIn("--- Existing narrative of this page", first)
        self.assertIn("`svc/auth.go:6` 에서 토큰을 확인한다.", first)
        self.assertIn("These files changed after this narrative was written: svc/auth.go", first)
        self.assertIn("Rewrite only what changed", first)
        self.assertEqual(report["pages"], ["svc"])
        self.assertEqual(self.store.load("svc").written_sha, sha2)
        self.assertIn("다시 확인한다", self.store.load("svc").body)
        # Now fresh: --stale has nothing to do.
        self.assertEqual(self.build(self.scripts("unused"), stale=True)["pages"], [])

    def test_a_running_build_blocks_a_second_one(self) -> None:
        with self.store.build_lock():
            with self.assertRaises(BuildError) as ctx:
                self.build(self.scripts("x"))
        self.assertEqual(ctx.exception.code, 4)

    def test_the_lock_is_released_when_the_build_ends(self) -> None:
        self.build(self.scripts(READ_BOTH, PAGE_ANSWER))
        with self.store.build_lock():
            pass

    def test_a_vanished_scope_is_skipped(self) -> None:
        self.store.save(make_page(RepoTools(self.repo, self.sha), id="gone", scope=["old/dir/"], basis={}, body="## a\nx"))
        report = self.build(self.scripts("unused"), page_ids=["gone"])
        self.assertTrue(report["skipped"][0]["reason"].startswith("scope_missing"))

    def test_unusable_projects_teams_and_commits(self) -> None:
        with self.assertRaises(BuildError):
            self.builder(sha="deadbeef")
        with self.assertRaises(BuildError) as ctx:
            self.builder(team="no-such-team")
        self.assertIn("team no-such-team", str(ctx.exception))
        with patch.object(ProjectManager, "load", side_effect=ValueError("no project")), self.assertRaises(BuildError):
            self.builder()

    def test_retrieval_off_is_refused(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL": "0"}), self.assertRaises(BuildError):
            self.build(self.scripts("x"))

    def test_the_build_can_target_an_older_commit(self) -> None:
        write(self.repo, "svc/auth.go", AUTH_MOVED)
        commit(self.repo, "later")
        report = self.build(self.scripts(READ_BOTH, PAGE_ANSWER), builder=self.builder(sha=self.sha))
        self.assertEqual(report["sha"], self.sha)
        self.assertEqual(self.store.load("svc").written_sha, self.sha)


# --- CLI -----------------------------------------------------------------------------------------

class WriterSessionTests(BuildCase):
    """The writer's turns: one session per page, its own role, a bounded first prompt."""

    def test_follow_up_results_go_into_the_same_session(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        self.build(runner)
        calls = [c for c in runner.calls if c["role"] == "expert"]
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0]["session_id"])
        self.assertEqual(calls[1]["session_id"], "expert-s1")  # the id the first call returned
        self.assertEqual(calls[1]["provider"], "claude")
        self.assertTrue(self.by_type("agent_started")[0]["resumed"] is False)

    def test_a_session_that_cannot_resume_gets_the_task_and_the_results_again(self) -> None:
        runner = FakeRunner({"expert": [READ_BOTH, PAGE_ANSWER], "critic": ["c"], "judge": ["j"]},
                            lose_session_for="expert")
        report = self.build(runner)
        prompts = runner.prompts("expert")
        replay = prompts[-1]
        self.assertIn("Page id: svc", replay)
        self.assertIn("=== Earlier in this turn ===", replay)
        self.assertIn("REPO RESULTS", replay)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["written"][0]["page"], "svc")

    def test_every_call_carries_the_writer_role_and_never_the_experts_persona(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        builder = self.builder()
        persona = builder.engine.persona_mgr.get_persona(builder.lineup.expert.persona) or {}
        self.build(runner, builder)
        self.assertTrue(runner.calls)
        for call in runner.calls:
            self.assertEqual(call["system"], wiki_build.WRITER_SYSTEM)
        self.assertIn("documentation writer", wiki_build.WRITER_SYSTEM)
        prompt = runner.prompts("expert")[0]
        self.assertTrue(persona.get("system_prompt"))
        self.assertNotIn(persona["system_prompt"][:60], prompt)
        self.assertNotIn("SSH Security Engineer", prompt)
        # The team member still decides which providers run the session.
        self.assertTrue(builder.lineup.expert.providers is not None)

    def test_the_first_prompt_has_one_map_and_the_one_block_rule(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        self.build(runner)
        first = runner.prompts("expert")[0]
        self.assertIn("write exactly ONE repo block per reply", first)
        self.assertEqual(first.count("Repository map of svc"), 1)  # the scope map only, not the whole-repository map
        self.assertNotIn("Repository map of the whole", first)

    def test_dry_run_reports_sizes_and_calls_no_llm(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        builder = self.builder()
        with patch.object(builder.engine.llm, "run_session_prompt", side_effect=runner):
            sizes = builder.dry_run(page_ids=["svc"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.store.ids(), [])
        item = sizes["pages"][0]
        self.assertEqual(item["page"], "svc")
        self.assertEqual(item["total"], len(
            (wiki_build.WRITER_SYSTEM + "\n\n" + builder.engine._compose(
                wiki_build.build_prompt("wikitest", self.sha, wiki_build.Target("svc", ["svc/"]), builder.engine._repo,
                                        [], builder.engine._language_rule, wiki_build.headings_for("Korean"), None,
                                        "Korean"), "", True, True)).encode("utf-8")))
        self.assertGreater(item["scope_map"], 0)
        self.assertLessEqual(item["scope_map"] + item["protocol"], item["total"])


class ViewerListingTests(unittest.TestCase):
    """What the dashboard list shows of a page without opening it: a one-line summary and the pages it links."""

    def test_summary_is_the_first_paragraph_of_the_first_section(self) -> None:
        body = "# Title\n\n## Role\n\nHandles `login` and\nsessions, see [[auth]].\n\nSecond paragraph.\n## Flow\nother\n"
        self.assertEqual(code_wiki.page_summary(body), "Handles login and sessions, see auth.")

    def test_summary_leaves_out_line_citations_and_the_parentheses_they_leave_behind(self) -> None:
        body = "## 역할\n인증은 `svc/auth.go#Check` 에서 한다 (`svc/auth.go:6`), 응답은 `svc/server.go:3-9` 가 만든다.\n"
        self.assertEqual(code_wiki.page_summary(body), "인증은 svc/auth.go#Check 에서 한다, 응답은 가 만든다.")

    def test_summary_skips_code_blocks_headings_and_bullet_marks(self) -> None:
        body = "## A\n```\nnot prose\n```\n- first item\n- second\n"
        self.assertEqual(code_wiki.page_summary(body), "first item second")
        self.assertEqual(code_wiki.page_summary("## Only headings\n## Here\n"), "")
        self.assertEqual(code_wiki.page_summary(""), "")

    def test_summary_is_cut_at_the_limit(self) -> None:
        text = code_wiki.page_summary("## A\n" + "word " * 100)
        self.assertEqual(len(text), code_wiki.SUMMARY_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_links_keep_order_and_drop_self_unknown_duplicates_and_code_blocks(self) -> None:
        body = ("## Role\nx\n## Related\n- [[b]] why\n- [[me]] itself\n- [[ghost]] deleted\n- `[[a]]` again\n- [[b]] twice\n"
                "```\n[[c]]\n```\n")
        self.assertEqual(code_wiki.page_links(body, "me", {"me", "a", "b", "c"}), ["b", "a"])

    def test_links_ignore_text_that_is_not_a_page_id(self) -> None:
        self.assertEqual(code_wiki.page_links("[[Not An Id]] [[-x]] [[a]]", "z", {"a", "-x"}), ["a"])


class CliTests(BuildCase):
    def run_cli(self, *argv: str, stdin: str = "") -> Any:
        import guardian

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, "argv", ["guardian.py", *argv]), patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                guardian.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
        text = out.getvalue()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        return code, payload, text, err.getvalue()

    def test_save_list_show_delete(self) -> None:
        body = BODY.replace("`svc/auth.go:6`", "`svc/auth.go:6`, `svc/server.go:3`")
        code, payload, _text, _err = self.run_cli(
            "--code-wiki-save", "--project", "wikitest", "--actor", "owner",
            stdin=json.dumps({"id": "svc", "title": "Service", "scope": ["svc"], "body": body}))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "fresh")
        self.assertEqual(payload["page"]["scope"], ["svc/"])  # directories get a trailing slash
        self.assertEqual(payload["page"]["author"], {"kind": "human", "user": "owner"})
        self.assertEqual(sorted(payload["page"]["basis"]), ["svc/auth.go", "svc/server.go"])  # strong citations only
        self.assertEqual(payload["page"]["written_sha"], self.sha)

        code, payload, _t, _e = self.run_cli("--code-wiki-list", "--project", "wikitest", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["sha"], self.sha)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["pages"], [{"id": "svc", "title": "Service", "scope": ["svc/"],
                                             "author": {"kind": "human", "user": "owner"},
                                             "updated": payload["pages"][0]["updated"], "state": "fresh",
                                             "kind": "dir", "links": [],
                                             "summary": "인증은 svc/auth.go#Check 에서 한다."}])

        code, payload, _t, _e = self.run_cli("--code-wiki-show", "svc", "--project", "wikitest", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(set(payload), {"ok", "project", "sha", "page", "narrative", "state", "staleness", "generated"})
        self.assertEqual(payload["narrative"], Page.from_text(self.store.pages_dir.joinpath("svc.md").read_text()).body)
        self.assertEqual(set(payload["staleness"]), {"state", "sha", "written_sha", "changed", "removed", "new", "broken",
                                                    "unverified", "notes"})
        self.assertIn("Repository map of svc", payload["generated"])
        self.assertNotIn("body", payload["page"])

        code, _p, text, _e = self.run_cli("--code-wiki-show", "svc", "--project", "wikitest")
        self.assertEqual(code, 0)
        self.assertIn("svc  [fresh]  Service", text)

        code, payload, _t, _e = self.run_cli("--code-wiki-delete", "svc", "--project", "wikitest")
        self.assertEqual((code, payload["deleted"]), (0, "svc"))
        code, payload, _t, _e = self.run_cli("--code-wiki-delete", "svc", "--project", "wikitest")
        self.assertEqual(code, 3)
        code, payload, _t, _e = self.run_cli("--code-wiki-show", "svc", "--project", "wikitest", "--json")
        self.assertEqual((code, payload["ok"]), (3, False))

    def test_state_follows_the_requested_sha(self) -> None:
        self.run_cli("--code-wiki-save", "--project", "wikitest",
                     stdin=json.dumps({"id": "svc", "title": "Service", "scope": ["svc/"], "body": BODY}))
        write(self.repo, "svc/auth.go", AUTH_MOVED)
        sha2 = commit(self.repo, "move")
        states = {}
        for sha in (self.sha, sha2, "HEAD"):
            _c, payload, _t, _e = self.run_cli("--code-wiki-list", "--project", "wikitest", "--sha", sha)
            states[sha] = payload["pages"][0]["state"]
        self.assertEqual(states, {self.sha: "fresh", sha2: "broken", "HEAD": "broken"})

    def test_invalid_input_is_exit_2_with_a_message(self) -> None:
        good = {"id": "svc", "title": "Service", "scope": ["svc/"], "body": BODY}
        for label, payload in [
            ("bad id", {**good, "id": "../x"}), ("no title", {**good, "title": ""}), ("scope missing", {**good, "scope": ["nope/"]}),
            ("scope secret", {**good, "scope": ["apps/agent/.env"]}), ("scope traversal", {**good, "scope": ["../x"]}),
            ("big body", {**good, "body": "x" * (MAX_BODY_BYTES + 1)}), ("extra field", {**good, "author": "me"}),
            ("not an object", ["x"]), ("scope not a list", {**good, "scope": "svc/"}), ("body not text", {**good, "body": 5}),
        ]:
            with self.subTest(label):
                code, out, _t, _e = self.run_cli("--code-wiki-save", "--project", "wikitest", stdin=json.dumps(payload))
                self.assertEqual(code, 2)
                self.assertFalse(out["ok"])
                self.assertTrue(out["error"])
        code, out, _t, _e = self.run_cli("--code-wiki-save", "--project", "wikitest", stdin="not json")
        self.assertEqual((code, out["ok"]), (2, False))
        self.assertEqual(self.store.ids(), [])
        code, out, _t, _e = self.run_cli("--code-wiki-list", "--project", "wikitest", "--sha", "deadbeef")
        self.assertEqual(code, 2)
        with patch.object(ProjectManager, "load", side_effect=__import__("project_registry").ProjectValidationError("no project 'x'")):
            code, out, _t, _e = self.run_cli("--code-wiki-list", "--project", "x")
        self.assertEqual((code, "no project" in out["error"]), (2, True))

    def test_build_through_the_cli(self) -> None:
        runner = self.scripts(READ_BOTH, PAGE_ANSWER)
        result_file = self.root / "result.json"
        with patch("llm_runner.LLMRunner.run_session_prompt", side_effect=runner):
            code, payload, _t, _e = self.run_cli("--code-wiki-build", "--project", "wikitest", "--json",
                                                 "--events-file", str(self.events_path), "--result-file", str(result_file))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["written"][0]["page"], "svc")
        self.assertEqual(json.loads(result_file.read_text())["written"], payload["written"])
        self.assertEqual(self.by_type("wiki_page_written")[0]["page"], "svc")
        self.assertEqual(self.store.ids(), ["svc"])

    def test_dry_run_prints_sizes_without_an_llm_call(self) -> None:
        with patch("llm_runner.LLMRunner.run_session_prompt", side_effect=AssertionError("no LLM call")):
            code, payload, _t, _e = self.run_cli("--code-wiki-build", "--project", "wikitest", "--page", "svc",
                                                 "--dry-run", "--json")
            text_code, _p, text, _e = self.run_cli("--code-wiki-build", "--project", "wikitest", "--page", "svc",
                                                   "--dry-run")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["pages"][0]["page"], "svc")
        self.assertGreater(payload["pages"][0]["total"], payload["pages"][0]["task"])
        self.assertEqual(text_code, 0)
        self.assertIn("svc [dir]: total", text)
        self.assertEqual(self.store.ids(), [])

    def test_build_exit_codes(self) -> None:
        code, payload, _t, err = self.run_cli("--code-wiki-build", "--project", "wikitest", "--json", "--page", "ghost")
        self.assertEqual((code, payload["ok"]), (3, False))
        self.store.save(make_page(RepoTools(self.repo, self.sha)))
        code, _p, _t, err = self.run_cli("--code-wiki-build", "--project", "wikitest")
        self.assertEqual(code, 2)
        self.assertIn("pass --page ID, --stale, --all, --kind or --regroup", err)
        with self.store.build_lock():
            code, _p, _t, err = self.run_cli("--code-wiki-build", "--project", "wikitest", "--all")
        self.assertEqual(code, 4)

        def canned(prompt: str, *args: Any, **kwargs: Any) -> LLMResult:
            return LLMResult(BUILTIN_PROVIDER, "canned", failures=[])

        with patch("llm_runner.LLMRunner.run_session_prompt", side_effect=canned):
            code, _p, text, _e = self.run_cli("--code-wiki-build", "--project", "wikitest", "--all")
        self.assertEqual(code, 1)  # nothing written and the attempt failed
        self.assertIn("skipped  svc: llm_fallback", text)


if __name__ == "__main__":
    unittest.main()

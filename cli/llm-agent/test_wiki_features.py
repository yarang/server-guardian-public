"""Feature pages of the code wiki (docs/design/code-wiki-coverage.md, A): one page per design document."""
import unittest
from typing import Any

from code_wiki import Page, WikiView, basis_for, save_human_page
from project_registry import ProjectValidationError, parse_project
from repo_tools import RepoTools
from test_code_wiki import BuildCase, commit, make_page, write
from test_retrieval_loop import block
from testenv import isolate_data_dir, restore_data_dir
from wiki_build import BuildError, doc_mentions, feature_candidates, select_targets

DESIGN = ("# Auth design\n\n## Intent\nTokens are checked in `svc/auth.go:6`.\n\n## Serving\n"
          "server.go answers; helper.py is shared. Tests live in svc/auth_test.go.\n"
          "See also docs/guide.md and docs/design/other.md.\n\n```\n# not a heading\n```\n")
ONE_FILE = "# Small\n\nOnly svc/auth.go is involved.\n"
FEATURE_ANSWER = ("# 인증 설계\n\n## 설계 의도\n토큰 검사를 한곳에 둔다 (`docs/design/auth.md:4`).\n"
                  "## 구현 위치\n`svc/auth.go#Check` 가 검사한다 (`svc/auth.go:6`).\n"
                  "## 설계와 다른 점\n없음.\n## 주의점\n없음.\n## 관련 페이지\n없음.\n")


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class FeatureCase(BuildCase):
    def setUp(self) -> None:
        super().setUp()
        write(self.repo, "docs/design/auth.md", DESIGN)
        write(self.repo, "docs/design/small.md", ONE_FILE)
        write(self.repo, "docs/design/old/auth.md", DESIGN)
        write(self.repo, "svc/auth_test.go", "package svc\n")
        self.sha = commit(self.repo, "design")
        self.tools = RepoTools(self.repo, self.sha)


class CandidateTests(FeatureCase):
    def test_a_document_naming_two_files_gets_a_page(self) -> None:
        found = feature_candidates(self.tools)
        self.assertEqual([c["id"] for c in found], ["feature-auth"])  # small.md names one file; old/ is not matched
        page = found[0]
        self.assertEqual(page["scope"], ["docs/design/auth.md", "svc/auth.go", "svc/server.go", "svc/helper.py"])
        self.assertEqual(page["docs"], ["docs/guide.md"])  # other.md does not exist
        self.assertEqual((page["doc"], page["kind"], page["files"]), ("docs/design/auth.md", "feature", 3))

    def test_tests_and_ambiguous_names_are_left_out(self) -> None:
        write(self.repo, "other/server.go", "package other\n")
        tools = RepoTools(self.repo, commit(self.repo, "dup"))
        files, _docs = doc_mentions(tools, "docs/design/auth.md", DESIGN)
        self.assertEqual(files, ["svc/auth.go", "svc/helper.py"])  # server.go is now ambiguous, auth_test.go a test

    def test_project_globs(self) -> None:
        self.assertEqual([c["id"] for c in feature_candidates(self.tools, ["docs/design/old/*.md"])], ["feature-auth"])
        self.assertEqual(feature_candidates(self.tools, []), [])


class SelectionTests(FeatureCase):
    def test_an_empty_wiki_starts_with_directories_and_features(self) -> None:
        targets = select_targets(self.store, self.tools, [], False, False)
        self.assertEqual([(t.id, t.kind) for t in targets][-1], ("feature-auth", "feature"))
        self.assertIn(("svc", "dir"), [(t.id, t.kind) for t in targets])

    def test_kind_selects_candidates_existing_or_new(self) -> None:
        self.store.save(make_page(self.tools))
        targets = select_targets(self.store, self.tools, [], False, False, kind="feature")
        self.assertEqual([(t.id, t.kind, t.existing) for t in targets], [("feature-auth", "feature", None)])
        with self.assertRaises(BuildError):
            select_targets(self.store, self.tools, ["svc"], False, False, kind="feature")
        with self.assertRaises(BuildError):
            select_targets(self.store, self.tools, [], False, False)  # pages exist: something must be chosen

    def test_a_feature_page_takes_its_scope_from_the_document_now(self) -> None:
        self.store.save(make_page(self.tools, id="feature-auth", kind="feature", scope=["docs/design/auth.md", "svc/auth.go"]))
        target = select_targets(self.store, self.tools, ["feature-auth"], False, False)[0]
        self.assertEqual(target.scope[-1], "svc/helper.py")
        self.assertIsNotNone(target.existing)
        everything = select_targets(self.store, self.tools, [], False, True, kind="dir")
        self.assertEqual(everything, [])


class PageKindTests(FeatureCase):
    def test_round_trip_and_unchanged_dir_pages(self) -> None:
        dir_page = make_page(self.tools)
        self.assertNotIn("kind:", dir_page.to_text())
        feature = make_page(self.tools, id="feature-auth", kind="feature", docs=["docs/guide.md"])
        again = Page.from_text(feature.to_text())
        self.assertEqual((again.kind, again.docs), ("feature", ["docs/guide.md"]))
        self.assertEqual(again.meta()["kind"], "feature")
        with self.assertRaises(Exception):
            make_page(self.tools, kind="nope").validate()

    def test_a_person_editing_a_feature_page_keeps_its_kind(self) -> None:
        self.store.save(make_page(self.tools, id="feature-auth", kind="feature", docs=["docs/guide.md"],
                                  scope=["docs/design/auth.md"]))
        page = save_human_page(self.store, self.tools, {"id": "feature-auth", "title": "Auth", "scope": ["docs/design/auth.md"],
                                                        "body": "## 설계 의도\nx\n## 구현 위치\ny\n"}, "alice")
        self.assertEqual((page.kind, page.docs, page.author["kind"]), ("feature", ["docs/guide.md"], "human"))

    def test_first_prompt_lists_directories_before_features(self) -> None:
        self.store.save(make_page(self.tools, id="a-feature", kind="feature"))
        self.store.save(make_page(self.tools, id="zz-dir"))
        _text, shown, _total, _stale = WikiView(self.store, self.tools).first_prompt()
        self.assertEqual(shown, ["zz-dir", "a-feature"])


class FeatureWriterTests(FeatureCase):
    def test_writes_a_feature_page_from_the_document_and_the_code(self) -> None:
        reads = block({"op": "read", "path": "docs/design/auth.md"}, {"op": "read", "path": "svc/auth.go"})
        runner = self.scripts(reads, FEATURE_ANSWER)
        report = self.build(runner, kind="feature")
        self.assertEqual(report["pages"], ["feature-auth"])
        self.assertEqual(report["written"][0]["state"], "fresh", report)
        page = self.store.load("feature-auth")
        self.assertEqual((page.kind, page.docs, page.title), ("feature", ["docs/guide.md"], "인증 설계"))
        self.assertEqual(page.basis, basis_for(self.tools, ["docs/design/auth.md", "svc/auth.go"]))
        first = runner.prompts("expert")[0]
        for needle in ("feature page", "Design document: docs/design/auth.md", "3: ## Intent", "6: ## Serving",
                       "## 설계 의도", "## 구현 위치", "## 설계와 다른 점", "## 관련 페이지",
                       "Never state how the code behaves from the document alone", "docs/guide.md"):
            self.assertIn(needle, first)
        self.assertNotIn("not a heading", first)

    def test_editing_the_document_makes_the_page_stale(self) -> None:
        reads = block({"op": "read", "path": "docs/design/auth.md"}, {"op": "read", "path": "svc/auth.go"})
        self.build(self.scripts(reads, FEATURE_ANSWER), kind="feature")
        write(self.repo, "docs/design/auth.md", DESIGN + "\nA new decision.\n")
        tools = RepoTools(self.repo, commit(self.repo, "edit doc"))
        details = WikiView(self.store, tools).details(self.store.load("feature-auth"))
        self.assertEqual((details["state"], details["changed"]), ("stale", ["docs/design/auth.md"]))


class ProjectGlobTests(FeatureCase):
    def test_the_builder_uses_the_project_globs(self) -> None:
        self.project.wiki_docs = ["docs/design/old/*.md"]
        sizes = self.builder().dry_run(kind="feature")
        self.assertEqual([(p["page"], p["kind"], p["scope"][0]) for p in sizes["pages"]],
                         [("feature-auth", "feature", "docs/design/old/auth.md")])
        self.project.wiki_docs = []
        self.assertEqual(self.builder().dry_run(kind="feature")["pages"], [])


class ProjectDocsTests(unittest.TestCase):
    def base(self, **extra: Any) -> Any:
        return parse_project({"id": "p", "workspace": "~/guardian-workspaces/p", **extra})

    def test_wiki_docs(self) -> None:
        self.assertIsNone(self.base().wiki_docs)
        self.assertEqual(self.base(wiki={"docs": ["docs/rfc/*.md"]}).wiki_docs, ["docs/rfc/*.md"])
        self.assertEqual(self.base(wiki={"docs": []}).wiki_docs, [])
        for bad in (["/etc/*.md"], ["../x/*.md"], "docs/*.md", [3], ["a\nb"]):
            with self.assertRaises(ProjectValidationError, msg=repr(bad)):
                self.base(wiki={"docs": bad})



if __name__ == "__main__":
    unittest.main()

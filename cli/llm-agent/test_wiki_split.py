"""Large directories of the code wiki (docs/design/code-wiki-coverage.md, B): group pages and overview pages."""
import os
from typing import Any, Dict
from unittest.mock import patch

import wiki_build
from code_wiki import Page
from repo_tools import RepoTools
from test_code_wiki import BuildCase, commit, make_page, write
from testenv import isolate_data_dir, restore_data_dir
from wiki_build import BuildError, page_plan, select_targets
from wiki_split import Splitter

PAD = "# " + "x" * 1180 + "\n"  # ~1.2 KB per file, so groups clear MIN_GROUP_BYTES
LIMITS = {"GUARDIAN_CODE_WIKI_SPLIT_FILES": "3", "GUARDIAN_CODE_WIKI_SPLIT_BYTES": "16000"}
SIMPLE = "## 역할\n설명.\n## 흐름\n없음.\n## 주의점\n없음.\n## 관련 페이지\n없음.\n"

FILES: Dict[str, str] = {
    "big/hub.py": "def shared_thing():\n    return 1\n" + PAD * 3,
    "big/a.py": "from hub import shared_thing\n\n\ndef alpha():\n    return shared_thing()\n" + PAD,
    "big/b.py": "from a import alpha\nfrom hub import shared_thing\n\n\ndef beta():\n    return alpha() + shared_thing()\n" + PAD,
    "big/c.py": "from hub import shared_thing\n\n\ndef gamma():\n    return shared_thing()\n" + PAD,
    "big/d.py": "from c import gamma\nfrom hub import shared_thing\n\n\ndef delta():\n    return gamma() + shared_thing()\n" + PAD,
    "big/e.py": "def lonely():\n    return 0\n" + PAD,
    "big/sub/x.py": "def ex():\n    return 1\n" + PAD,
    "big/sub/y.py": "from x import ex\n\n\ndef why():\n    return ex()\n" + PAD,
    "big/tests/helper.py": "def helper_fixture():\n    return 1\n" + PAD,
}


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class SplitCase(BuildCase):
    def setUp(self) -> None:
        super().setUp()
        for path, text in FILES.items():
            write(self.repo, path, text)
        self.sha = commit(self.repo, "big")
        self.tools = RepoTools(self.repo, self.sha)
        env = patch.dict(os.environ, LIMITS)
        env.start()
        self.addCleanup(env.stop)


class PlanTests(SplitCase):
    def test_subdirectories_first_then_a_core_and_linked_groups(self) -> None:
        plan = Splitter(self.tools).plan("big/", "big")
        self.assertEqual([(g["id"], g["scope"], g["hubs"]) for g in plan], [
            ("big--sub", ["big/sub/"], []),
            ("big--core", ["big/hub.py"], ["big/hub.py"]),
            ("big--a", ["big/a.py", "big/b.py"], []),
            ("big--c", ["big/c.py", "big/d.py"], []),
        ])  # e.py is alone and small (the overview covers it); tests/ is never a group

    def test_the_plan_is_deterministic_and_none_within_the_limit(self) -> None:
        self.assertEqual(Splitter(self.tools).plan("big/", "big"), Splitter(self.tools).plan("big/", "big"))
        self.assertIsNone(Splitter(self.tools).plan("svc/", "svc"))
        with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI_SPLIT_FILES": "50"}):
            self.assertIsNone(Splitter(self.tools).plan("big/", "big"))


class SelectionTests(SplitCase):
    def ids(self, targets: Any) -> list:
        return [(t.id, t.kind) for t in targets]

    def test_an_empty_wiki_writes_groups_first(self) -> None:
        got = self.ids(select_targets(self.store, self.tools, [], False, False))
        self.assertEqual(got[:4], [("big--sub", "group"), ("big--core", "group"), ("big--a", "group"), ("big--c", "group")])
        self.assertIn(("big", "overview"), got)
        self.assertIn(("svc", "dir"), got)

    def test_kind_group_selects_groups_then_overviews_and_freezes_a_planned_directory(self) -> None:
        got = self.ids(select_targets(self.store, self.tools, [], False, False, kind="group"))
        self.assertEqual(got[-1], ("big", "overview"))
        self.store.save(make_page(self.tools, id="big--a", kind="group", scope=["big/a.py", "big/b.py"]))
        frozen = self.ids(select_targets(self.store, self.tools, [], False, False, kind="group"))
        self.assertEqual(frozen, [("big--a", "group"), ("big", "overview")])  # no new plan once groups exist
        dirs, groups = page_plan(self.tools, ["big--a"], regroup=["big"])
        self.assertEqual(len(groups), 4)
        again = self.ids(select_targets(self.store, self.tools, [], False, False, regroup=["big"]))
        self.assertEqual(again[-1], ("big", "overview"))
        self.assertEqual(len(again), 5)
        with self.assertRaises(BuildError):
            select_targets(self.store, self.tools, [], False, False, regroup=["svc"])
        with self.assertRaises(BuildError):
            select_targets(self.store, self.tools, [], False, False, kind="group", regroup=["big"])

    def test_a_named_group_page_of_a_frozen_directory_can_still_be_written(self) -> None:
        # big--a exists, so big's plan is frozen; big--c failed in that build and was never stored.
        self.store.save(make_page(self.tools, id="big--a", kind="group", scope=["big/a.py", "big/b.py"]))
        target = select_targets(self.store, self.tools, ["big--c", "big"], False, False)
        self.assertEqual([(t.id, t.kind, t.scope) for t in target],
                         [("big--c", "group", ["big/c.py", "big/d.py"]), ("big", "overview", ["big/"])])

    def test_a_dir_page_of_a_split_directory_becomes_an_overview(self) -> None:
        self.store.save(make_page(self.tools, id="big", scope=["big/"], body="## 역할\nold dir text\n## 흐름\nx\n"))
        target = select_targets(self.store, self.tools, ["big"], False, False)[0]
        self.assertEqual((target.kind, target.existing.kind), ("overview", "dir"))


class WriterTests(SplitCase):
    def test_groups_then_the_overview_with_its_groups_in_the_prompt(self) -> None:
        self.store.save(make_page(self.tools, id="big", scope=["big/"], body="## 역할\nold dir text\n## 흐름\nx\n"))
        self.store.save(make_page(self.tools, id="feature-x", kind="feature", scope=["docs/guide.md", "big/c.py"]))
        runner = self.scripts(*([SIMPLE] * 5))
        with patch.object(wiki_build, "MAX_FEATURE_PAGES", 0):
            report = self.build(runner, kind="group")
        self.assertEqual(report["pages"], ["big--sub", "big--core", "big--a", "big--c", "big"])
        self.assertEqual(report["failed"], 0, report)
        self.assertEqual(self.store.load("big--a").kind, "group")
        self.assertEqual(self.store.load("big").kind, "overview")
        prompts = runner.prompts("expert")
        self.assertIn("the overview page is [[big]]", prompts[0])
        self.assertIn("[[feature-x]]", prompts[3])  # big--c covers big/c.py, which the feature page names
        self.assertNotIn("[[feature-x]]", prompts[2])
        overview = prompts[4]
        for needle in ("This page is the overview of big/", "[[big--sub]]", "[[big--core]]", "[[big--c]]", "설명."):
            self.assertIn(needle, overview)
        self.assertNotIn("old dir text", overview)  # a dir page that became an overview is written afresh

    def test_page_kinds_round_trip(self) -> None:
        page = make_page(self.tools, id="big--a", kind="group")
        self.assertEqual(Page.from_text(page.to_text()).kind, "group")


class WriterStepCapTests(SplitCase):
    def test_a_large_step_is_cut_and_the_rest_deferred_and_the_setting_restored(self) -> None:
        import json

        from test_retrieval_loop import block

        reads = block(*({"op": "read", "path": p} for p in ("big/hub.py", "big/a.py", "big/b.py", "big/c.py")))
        runner = self.scripts(reads, SIMPLE)
        with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI_STEP_BYTES": "5000"}):
            os.environ.pop("GUARDIAN_RETRIEVAL_STEP_BYTES", None)
            self.build(runner, page_ids=["big--a"])
            self.assertNotIn("GUARDIAN_RETRIEVAL_STEP_BYTES", os.environ)  # only during the build
        prompts = runner.prompts("expert")
        self.assertIn("4 KB per step", prompts[0])  # the protocol states the cap
        self.assertIn("DEFERRED:", prompts[1])
        served = [e["data"] for e in self.events() if e["type"] == "retrieval_served"]
        self.assertTrue(served and all(sum(r["bytes"] for r in d["results"]) <= 5000 for d in served), json.dumps(served)[:300])

    def test_the_cap_can_be_turned_off_and_never_raises_an_explicit_lower_limit(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_CODE_WIKI_STEP_BYTES": "0", "GUARDIAN_RETRIEVAL_STEP_BYTES": "100"}):
            with wiki_build.writer_limits():
                self.assertEqual(os.environ["GUARDIAN_RETRIEVAL_STEP_BYTES"], "100")
        with patch.dict(os.environ, {"GUARDIAN_RETRIEVAL_STEP_BYTES": "100"}):
            os.environ.pop("GUARDIAN_CODE_WIKI_STEP_BYTES", None)
            with wiki_build.writer_limits():
                self.assertEqual(os.environ["GUARDIAN_RETRIEVAL_STEP_BYTES"], "100")
        os.environ.pop("GUARDIAN_RETRIEVAL_STEP_BYTES", None)
        with wiki_build.writer_limits():
            self.assertEqual(os.environ["GUARDIAN_RETRIEVAL_STEP_BYTES"], str(512 * 1024))
        self.assertNotIn("GUARDIAN_RETRIEVAL_STEP_BYTES", os.environ)

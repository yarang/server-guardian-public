import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List
from unittest.mock import patch

from repo_map import DEFAULT_MAP_BYTES, MAP_VERSION, build_map, repo_map_cap, repo_map_enabled, root_summary
from repo_mcp import RepoMcpServer
from repo_tools import RepoTools, describe_request, provided_paths
from test_repo_tools import COMMIT, git
from test_retrieval_loop import EngineHarness, FakeRunner, block

CORE_PY = (
    "class Core:\n"
    "    def run(self):\n"
    "        return helper()\n\n\n"
    "def helper():\n"
    "    return Core()\n\n\n"
    "def unused():\n"
    "    return helper()\n"
)
USE1_PY = "from core import Core, helper\n\n\ndef first():\n    helper()\n    helper()\n    return Core()\n"
USE2_PY = "from core import Core, helper\n\n\ndef second():\n    return helper(), Core()\n"


def commit_files(root: Path, files: Dict[str, str]) -> Path:
    """A throw-away git repository holding the given files."""
    repo = root / "ws"
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    # Fixed commit dates: the same files must give the same sha, or the determinism tests flake across a second boundary.
    stamp = "2026-01-01T00:00:00+0000"
    with patch.dict(os.environ, {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}):
        git(repo, *COMMIT, "commit", "-q", "-m", "one")
    return repo


def small_files() -> Dict[str, str]:
    return {
        "main.py": "def main():\n    pass\n",
        "app/core.py": CORE_PY,
        "app/use1.py": USE1_PY,
        "app/use2.py": USE2_PY,
        "app/README.md": "readme\n",
        "app/deep/a/b/leaf.go": "package b\n\nfunc Leaf() {}\n",
        "docs/one.md": "one\n",
        "docs/two.md": "two\n",
        "docs/three.png.txt": "three\n",
        ".env": "SECRET=1\n",
    }


def wide_files() -> Dict[str, str]:
    """Enough code in nested directories that the map needs well over a few KB."""
    files = {}
    for d in range(6):
        for sub in range(3):
            for n in range(5):
                body = "".join(f"def func_{d}_{sub}_{n}_{k}():\n    return {k}\n\n\n" for k in range(8))
                files[f"pkg{d}/mod{sub}/inner/file{n}.py"] = body
        files[f"pkg{d}/top.py"] = f"def top_{d}():\n    pass\n"
        files[f"pkg{d}/notes.md"] = "notes\n"
    return files


def covered(text: str) -> int:
    """Files a map accounts for: listed file lines plus every count in a '(N files...)' line."""
    total = 0
    for line in text.splitlines():
        if re.match(r"^  \S.*  \[(?:\d+ lines|large file, not indexed)\]", line):
            total += 1
        for found in re.finditer(r"\((?:\+|all )?(\d+) (?:non-code )?files(?:, folded)?\)", line):
            total += int(found.group(1))
    return total


class MapCase(unittest.TestCase):
    files = staticmethod(small_files)

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = commit_files(self.root, self.files())
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.tools = RepoTools(self.repo, self.sha)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class MapContentTests(MapCase):
    def test_header_and_layout(self) -> None:
        text = build_map(self.tools)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"--- Repository map (sha {self.sha[:7]}, {len(self.tools.readable_paths())} files, "
                                   "ranked by references) ---")
        self.assertIn("./", lines)  # files directly in the root
        self.assertIn("  main.py  [2 lines] main", lines)
        self.assertNotIn(".env", text)  # secrets are not readable paths

    def test_declarations_ranked_by_references_from_other_files(self) -> None:
        text = build_map(self.tools)
        line = next(l for l in text.splitlines() if l.startswith("  core.py"))
        # Counted by distinct referencing files: helper and Core are each used in 2 other files (helper's 3 lines
        # in use1.py count once; core.py's own uses do not count), so the tie is broken by name. run and unused are
        # never referenced from another file: unranked, alphabetical, after the ranked ones.
        self.assertEqual(line, "  core.py  [11 lines] Core, helper, Core.run, unused")

    def test_at_most_six_declarations_with_an_ellipsis(self) -> None:
        repo = commit_files(self.root / "m", {"many.py": "".join(f"def f{i}():\n    pass\n\n" for i in range(9))})
        tools = RepoTools(repo, git(repo, "rev-parse", "HEAD"))
        line = build_map(tools).splitlines()[2]
        self.assertTrue(line.endswith("f0, f1, f2, f3, f4, f5, …"), line)

    def test_non_code_files_fold_into_counts(self) -> None:
        text = build_map(self.tools)
        self.assertIn("docs/  (3 files)", text.splitlines())  # a directory with only non-code files
        app = text.split("app/\n", 1)[1].split("\n", 1)[0]
        self.assertTrue(app.startswith("  core.py"))
        self.assertIn("  (+1 non-code files)", text.splitlines())
        self.assertNotIn("README", text)
        self.assertNotIn("one.md", text)

    def test_uncapped_map_accounts_for_every_readable_file(self) -> None:
        self.assertEqual(covered(build_map(self.tools)), len(self.tools.readable_paths()))

    def test_deterministic_across_builds_and_instances(self) -> None:
        first = build_map(self.tools)
        self.assertEqual(build_map(self.tools).encode(), first.encode())
        other = RepoTools(self.repo, self.sha)
        self.assertEqual(build_map(other).encode(), first.encode())
        self.assertEqual(build_map(other, cap=500).encode(), build_map(self.tools, cap=500).encode())
        self.assertEqual(root_summary(other).encode(), root_summary(self.tools).encode())

    def test_deterministic_with_and_without_the_disk_cache(self) -> None:
        cache = self.root / "cache"
        cold = RepoTools(self.repo, self.sha, cache_dir=cache)
        text = build_map(cold)
        self.assertTrue(list(cache.rglob("*.json")))
        self.assertEqual(build_map(RepoTools(self.repo, self.sha, cache_dir=cache)), text)  # warm
        self.assertEqual(build_map(self.tools), text)  # memory only

    def test_root_summary(self) -> None:
        lines = root_summary(self.tools).splitlines()
        self.assertTrue(lines[0].startswith(f"--- Repository summary (sha {self.sha[:7]}, "))
        self.assertNotIn("Repository file list", "\n".join(lines))
        by_name = {l.split("  ")[0]: l for l in lines[1:]}
        self.assertEqual(sorted(by_name), ["./", "app/", "docs/"])
        self.assertEqual(by_name["app/"], "app/  5 files; top symbols: Core, helper, Core.run")
        self.assertEqual(by_name["docs/"], "docs/  3 files")


class MapCapTests(MapCase):
    files = staticmethod(wide_files)

    def test_default_cap_keeps_a_small_repository_whole(self) -> None:
        full = build_map(self.tools)
        self.assertEqual(build_map(self.tools, cap=DEFAULT_MAP_BYTES), full)
        self.assertNotIn("folded", full)

    def test_symbols_are_dropped_before_directories_are_folded(self) -> None:
        full = build_map(self.tools)
        cap = len(full.encode()) * 7 // 10
        capped = build_map(self.tools, cap=cap)
        self.assertLessEqual(len(capped.encode()), cap)
        self.assertNotIn("(…", capped)
        self.assertNotIn(", folded)", capped)  # shortening symbols was enough
        self.assertIn("{\"op\": \"map\"", capped)  # the hint says the map op expands it
        self.assertIn("] …", capped)
        self.assertEqual(covered(capped), len(self.tools.readable_paths()))

    def test_directories_are_folded_when_symbols_are_not_enough(self) -> None:
        paths = len(self.tools.readable_paths())
        for cap in (1500, 700, 300):
            with self.subTest(cap=cap):
                capped = build_map(self.tools, cap=cap)
                self.assertLessEqual(len(capped.encode()), max(cap, 400))
                self.assertIn("files, folded)", capped)
                self.assertIn("expand a directory with the map op", capped)
                self.assertEqual(covered(capped), paths)

    def test_deepest_directories_fold_first(self) -> None:
        capped = build_map(self.tools, cap=len(build_map(self.tools).encode()) // 4)
        lines = capped.splitlines()
        folded = [l.split("  ")[0] for l in lines if "files, folded)" in l]
        detailed = [l for l in lines if l.endswith("/") and not l.startswith("[")]
        self.assertTrue(folded and detailed)
        self.assertEqual(len({d.count("/") for d in folded}), 1)  # one level is folded, not a mixture
        self.assertLess(max(d.count("/") for d in detailed), folded[0].count("/") + 1)
        self.assertLessEqual(max(d.count("/") for d in detailed), folded[0].count("/"))

    def test_a_tiny_cap_gives_the_single_count_line(self) -> None:
        text = build_map(self.tools, cap=1)
        paths = len(self.tools.readable_paths())
        self.assertIn(f"(all {paths} files, folded)", text)
        self.assertEqual(covered(text), paths)


class MapRankingCapTests(unittest.TestCase):
    def test_the_file_with_the_fewest_references_loses_its_symbols_first(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = commit_files(Path(tmp), {
                "a.py": "def popular():\n    pass\n",
                "b.py": "def lonely():\n    pass\n",
                "c.py": "from a import popular\n\n\n" + "".join(
                    f"def caller_with_a_long_descriptive_name_{i}_for_the_cap():\n    popular()\n" for i in range(6)),
            })
            tools = RepoTools(repo, git(repo, "rev-parse", "HEAD"))
            full = build_map(tools)
            self.assertIn("  a.py  [2 lines] popular", full)
            # Shortening costs a hint line, so only a large saving (c.py's six long names) pays for it.
            capped = build_map(tools, cap=len(full.encode()) - 150)
            self.assertIn("  a.py  [2 lines] popular", capped)
            self.assertIn("  b.py  [2 lines] lonely", capped)
            self.assertIn("  c.py  [15 lines] …", capped)  # ties among 0-reference files: the later path drops first
            self.assertIn("] …", capped)
            self.assertEqual(capped.count("] …"), 1)


class EnvTests(unittest.TestCase):
    def test_cap_parsing(self) -> None:
        for value in ("", "0", "-5", "abc", "  ", "1.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"GUARDIAN_REPO_MAP_BYTES": value}):
                self.assertEqual(repo_map_cap(), 32768)
        with patch.dict(os.environ, {"GUARDIAN_REPO_MAP_BYTES": " 1000 "}):
            self.assertEqual(repo_map_cap(), 1000)
        with patch.dict(os.environ):
            os.environ.pop("GUARDIAN_REPO_MAP_BYTES", None)
            self.assertEqual(repo_map_cap(), DEFAULT_MAP_BYTES)

    def test_enabled_flag(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("GUARDIAN_REPO_MAP", None)
            self.assertTrue(repo_map_enabled())
        for value in ("0", "false", "OFF", " no "):
            with self.subTest(value=value), patch.dict(os.environ, {"GUARDIAN_REPO_MAP": value}):
                self.assertFalse(repo_map_enabled())
        for value in ("1", "", "yes"):
            with self.subTest(value=value), patch.dict(os.environ, {"GUARDIAN_REPO_MAP": value}):
                self.assertTrue(repo_map_enabled())


class MapOpTests(MapCase):
    def test_root_default_depth_two_folds_deeper_directories(self) -> None:
        result = self.tools.run({"op": "map"})
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["op"], result["target"]), ("map", "."))
        text = result["text"]
        self.assertIn("app/deep/  (1 files, folded)", text)  # level 2 is folded, its content counted
        self.assertIn("  core.py", text)
        self.assertEqual(covered(text), len(self.tools.readable_paths()))
        self.assertEqual(result["bytes"], len(text.encode()))

    def test_path_and_depth(self) -> None:
        shallow = self.tools.run({"op": "map", "path": "app", "depth": 1})["text"]
        self.assertTrue(shallow.startswith(f"--- Repository map of app (sha {self.sha[:7]}, 5 files, "))
        self.assertIn("app/deep/  (1 files, folded)", shallow)
        deep = self.tools.run({"op": "map", "path": "app/", "depth": 4})["text"]
        self.assertIn("app/deep/a/b/\n  leaf.go  [3 lines] Leaf", deep)
        self.assertNotIn("folded", deep)
        self.assertNotIn("main.py", deep)
        # depth is clamped like list; junk falls back to the default
        self.assertEqual(self.tools.run({"op": "map", "path": "app", "depth": 99})["text"], deep)
        self.assertEqual(self.tools.run({"op": "map", "path": "app", "depth": "x"})["text"],
                         self.tools.run({"op": "map", "path": "app", "depth": 2})["text"])

    def test_invalid_paths(self) -> None:
        for path, error in (("../x", "invalid path"), ("app/core.py", "not a directory"),
                            (".env", "excluded"), ("nope", "not found")):
            with self.subTest(path=path):
                result = self.tools.run({"op": "map", "path": path})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], error)

    def test_map_provides_no_file_and_mentions_nothing(self) -> None:
        result = self.tools.run({"op": "map", "path": "app", "depth": 1})
        self.assertEqual(provided_paths(result), [])
        self.assertEqual(self.tools.mentioned_in({**result, "text": "app/core.py main.py"}), [])
        self.assertEqual(describe_request({"op": "map"}), {"op": "map", "target": "."})
        self.assertEqual(describe_request({"op": "map", "path": "app"}), {"op": "map", "target": "app"})

    def test_result_limits_apply(self) -> None:
        result = self.tools.run({"op": "map"}, max_bytes=40)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode()), 40 + 40)


class McpMapTests(MapCase):
    def call(self, server: RepoMcpServer, **arguments: Any) -> Dict[str, Any]:
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "repo_map", "arguments": arguments}})["result"]

    def test_repo_map_tool(self) -> None:
        log = self.root / "calls.jsonl"
        server = RepoMcpServer(self.tools, str(log))
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        spec = {t["name"]: t for t in listed}["repo_map"]
        self.assertEqual(sorted(spec["inputSchema"]["properties"]), ["depth", "path"])
        self.assertNotIn("required", spec["inputSchema"])
        ok = self.call(server, path="app", depth=2, bogus="x")
        self.assertFalse(ok["isError"])
        self.assertIn("--- Repository map of app", ok["content"][0]["text"])
        self.assertEqual(self.call(server)["isError"], False)
        bad = self.call(server, path="../etc")
        self.assertTrue(bad["isError"])
        self.assertIn("invalid path", bad["content"][0]["text"])
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([r["op"] for r in records], ["map", "map", "map"])
        self.assertEqual(records[0]["paths"], [])
        self.assertEqual(records[0]["mentions"], [])


class PromptIntegrationTests(EngineHarness):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.dict(os.environ, {"GUARDIAN_DATA_DIR": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def first_prompt(self, **kwargs: Any) -> Dict[str, Any]:
        self.runner = FakeRunner(self.plain_scripts(["FINAL"]))
        return self.run_debate(self.engine(), self.runner, **kwargs)

    def started(self) -> Dict[str, Any]:
        return [e for e in self.events() if e["type"] == "debate_started"][0]["data"]["context"]

    def test_no_context_paths_get_the_map_instead_of_the_file_list(self) -> None:
        result = self.first_prompt()
        prompt = self.runner.prompts("expert")[0]
        self.assertIn(f"--- Repository map (sha {self.sha[:7]}, ", prompt)
        self.assertNotIn("--- Repository file list", prompt)
        self.assertIn("  tool.py  [10 lines] Box, Box.open, run", prompt)
        self.assertIn('{"op": "map", "path": "apps/agent", "depth": 2}', prompt)
        self.assertIn("map (a ranked overview of a directory", prompt)
        self.assertIn("exactly ONE repo block", prompt)  # the existing rules stay
        context = self.started()
        guide = prompt.split("--- Repository map", 1)[1].split("=== End of repository retrieval", 1)[0]
        self.assertEqual(context["map_bytes"], len(("--- Repository map" + guide).rstrip("\n").encode()))
        self.assertEqual(result["context"]["index"]["sha"], self.sha)

    def test_snapshot_and_event_fields(self) -> None:
        from code_index import INDEX_VERSION
        from debate_engine import RETRIEVAL_PROMPT_VERSION
        result = self.first_prompt()
        index = result["context"]["index"]
        self.assertEqual(index, {"sha": self.sha, "version": INDEX_VERSION, "files": 2, "symbols": index["symbols"],
                                 "prompt_version": RETRIEVAL_PROMPT_VERSION, "map_version": MAP_VERSION})
        self.assertGreaterEqual(index["symbols"], 5)
        context = self.started()
        self.assertGreater(context["map_bytes"], 0)
        self.assertEqual((context["prompt_version"], context["map_version"]), (RETRIEVAL_PROMPT_VERSION, MAP_VERSION))

    def test_context_paths_get_only_the_root_summary(self) -> None:
        result = self.first_prompt(context_paths=["cli/tool.py"])
        prompt = self.runner.prompts("expert")[0]
        self.assertEqual(prompt.count("--- Repository file list"), 1)  # the code pack's own list
        self.assertNotIn("--- Repository map", prompt)
        summary = prompt.split("--- Repository summary", 1)[1].split("=== End of repository retrieval", 1)[0]
        self.assertIn("apps/  ", summary)
        self.assertIn("cli/  ", summary)
        self.assertIn("top symbols: ", summary)
        self.assertNotIn("main.go", summary)  # no file level
        self.assertGreater(self.started()["map_bytes"], 0)
        self.assertIn("index", result["context"])

    def test_repo_map_off_restores_the_file_list(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_REPO_MAP": "0"}):
            result = self.first_prompt()
        prompt = self.runner.prompts("expert")[0]
        self.assertIn("--- Repository file list (", prompt)
        self.assertIn("\napps/agent/main.go\n", prompt)
        self.assertNotIn("--- Repository map", prompt)
        context = self.started()
        self.assertEqual((context["map_bytes"], context["map_version"]), (0, None))
        self.assertNotIn("index", result["context"])

    def test_map_size_uses_the_configured_cap(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_REPO_MAP_BYTES": "1"}):
            self.first_prompt()
        self.assertIn("files, folded)", self.runner.prompts("expert")[0])

    def test_the_mcp_guidance_names_repo_map(self) -> None:
        engine = self.engine()
        engine._repo = RepoTools(self.repo, self.sha)
        self.assertIn("mcp__repo__repo_map", engine._mcp_text())


def ranking_files() -> Dict[str, str]:
    users = {f"app/use{i}.py": "from lib import popular\n\n\ndef user():\n    popular()\n    popular()\n    ab()\n" for i in range(3)}
    return {
        "lib/lib.py": ("def popular():\n    pass\n\n\ndef chatty():\n    pass\n\n\ndef ab():\n    pass\n\n\n"
                       "def zeta():\n    pass\n\n\ndef lonely():\n    pass\n"),
        "app/loud.py": "from lib import chatty\n\n\ndef loud():\n" + "    chatty()\n" * 40,
        **users,
    }


class RankingTests(unittest.TestCase):
    """Eval 1: references count by distinct files; noise paths, tiny names and unreferenced declarations do not rank."""

    def build(self, files: Dict[str, str], **kwargs: Any) -> str:
        with TemporaryDirectory() as tmp:
            repo = commit_files(Path(tmp), files)
            return build_map(RepoTools(repo, git(repo, "rev-parse", "HEAD")), **kwargs)

    def test_distinct_referencing_files_beat_occurrences(self) -> None:
        text = self.build(ranking_files())
        line = next(l for l in text.splitlines() if l.startswith("  lib.py"))
        # popular: 3 files; chatty: 1 file with 40 lines (it ranked first when occurrences were counted); ab is used
        # in 3 files but has 2 characters; lonely and zeta have no outside reference. Unranked ones go last, A-Z.
        self.assertEqual(line, "  lib.py  [18 lines] popular, chatty, ab, lonely, zeta")

    def test_references_from_noise_paths_do_not_count(self) -> None:
        user = "from lib import target\n\n\ndef use():\n    target()\n"
        noise = {
            "lib/lib.py": "def target():\n    pass\n\n\ndef real():\n    pass\n",
            "app/button.stories.tsx": "target();\n",
            "ui/previews/p.py": user,
            ".design-sync/d.py": user,
            "pkg/z_test.go": "package pkg\nfunc TestZ() { target() }\n",
            "tests/test_z.py": user,
            "web/foo.test.tsx": "target();\n",
            "web/__tests__/q.ts": "target();\n",
            "app/real_user.py": "from lib import real\n\n\ndef go():\n    real()\n",
        }
        text = self.build(noise)
        line = next(l for l in text.splitlines() if l.startswith("  lib.py"))
        self.assertEqual(line, "  lib.py  [6 lines] real, target")  # target is referenced from noise files only
        # A declaration inside a noise file is never ranked, whatever uses its name.
        self.assertIn("  d.py  [5 lines] use", text)

    def test_noise_path_rules(self) -> None:
        from repo_map import is_noise_path
        for path in ("a/Button.stories.tsx", "x/previews/y.tsx", ".design-sync/a.tsx", "pkg/auth_test.go", "t/test_x.py",
                     "web/a.test.ts", "web/a.test.tsx", "web/__tests__/a.ts", "deep/.design-sync/previews/a.tsx"):
            self.assertTrue(is_noise_path(path), path)
        for path in ("apps/agent/auth.go", "cli/llm-agent/repo_map.py", "web/src/preview.tsx", "web/testing.ts",
                     "cli/contest.py", "src/previewsx/a.ts"):
            self.assertFalse(is_noise_path(path), path)

    def test_the_map_is_deterministic(self) -> None:
        first = self.build(ranking_files(), cap=400)
        self.assertEqual(self.build(ranking_files(), cap=400).encode(), first.encode())


class MapCoverageNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.repo = commit_files(Path(self._tmp.name), {
            "good/ok.py": "def fine():\n    pass\n",
            "bad/broken.py": "def broken(:\n",
            "bad/also.go": "package bad\nfunc Fine() {}\n",
            "big/huge.ts": "export const A = 1;\n// " + "x" * (1024 * 1024) + "\n",
        })
        self.tools = RepoTools(self.repo, git(self.repo, "rev-parse", "HEAD"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_note_follows_the_header_and_names_the_files(self) -> None:
        lines = build_map(self.tools).splitlines()
        self.assertEqual(lines[1], "[index coverage: 1 file failed to parse, 1 file over 1 MB not indexed — "
                                   "results may be incomplete; use grep for these: bad/broken.py, big/huge.ts]")
        self.assertIn("  broken.py  [1 lines]", lines)
        self.assertIn("  huge.ts  [large file, not indexed]", lines)

    def test_note_counts_only_the_requested_subtree(self) -> None:
        good = build_map(self.tools, "good/").splitlines()
        self.assertFalse(any(l.startswith("[index coverage") for l in good))
        bad = build_map(self.tools, "bad/").splitlines()
        self.assertIn("1 file failed to parse", bad[1])
        self.assertNotIn("over 1 MB", bad[1])
        op = self.tools.run({"op": "map", "path": "big"})
        self.assertIn("1 file over 1 MB not indexed", op["text"].splitlines()[1])

    def test_note_survives_a_tiny_cap_and_files_stay_accounted_for(self) -> None:
        text = build_map(self.tools, cap=1)
        self.assertEqual(text.splitlines()[1][:16], "[index coverage:")
        self.assertEqual(covered(text), len(self.tools.readable_paths()))


if __name__ == "__main__":
    unittest.main()

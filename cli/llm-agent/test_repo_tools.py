import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repo_tools import (
    RepoTools,
    clip_result,
    deferred_result,
    parse_repo_blocks,
    provided_paths,
    strip_repo_blocks,
    trim_after_first_block,
)

COMMIT = ("-c", "user.email=t@t", "-c", "user.name=t")
GO_SOURCE = (
    'package main\n\nimport "fmt"\n\n'
    "// Serve prints a greeting.\nfunc Serve() {\n\tfmt.Println(\"SERVE_BODY\")\n}\n\n"
    "func Other() {\n\tfmt.Println(\"OTHER_BODY\")\n}\n"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "ws"
    (repo / "apps/agent").mkdir(parents=True)
    (repo / "cli").mkdir()
    (repo / "apps/agent/main.go").write_text(GO_SOURCE)
    (repo / "apps/agent/notes.txt").write_text("alpha\nbeta TARGET gamma\ndelta\n")
    (repo / "cli/tool.py").write_text("import os\n\n\ndef run():\n    return 'RUN_BODY'\n\n\nclass Box:\n    def open(self):\n        return 1\n")
    (repo / "server-guardian-credentials.enc").write_text("TARGET ENC_SECRET\n")
    (repo / "apps/agent/.env").write_text("TARGET=ENV_SECRET\n")
    (repo / "apps/agent/blob.bin").write_bytes(b"\x00\x01TARGET binary")
    (repo / "apps/agent/big.txt").write_text("".join(f"line {i} TARGET\n" for i in range(8000)))
    outside = root / "outside.txt"
    outside.write_text("TARGET OUTSIDE_SECRET\n")
    os.symlink(outside, repo / "apps/agent/link.txt")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    # A submodule entry (gitlink) without a real submodule checkout.
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},vendor/sub")
    git(repo, *COMMIT, "commit", "-q", "-m", "one")
    return repo


class RepoToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.tools = RepoTools(self.repo, self.sha)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_result_shape(self) -> None:
        res = self.tools.run({"op": "read", "path": "apps/agent/notes.txt"})
        self.assertEqual(set(res), {"op", "target", "ok", "bytes", "truncated", "error", "text"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["bytes"], len(res["text"].encode()))
        self.assertIn("2| beta TARGET gamma", res["text"])

    def test_list_hides_excluded_entries_and_respects_depth(self) -> None:
        text = self.tools.run({"op": "list"})["text"]
        self.assertIn("apps/", text)
        self.assertNotIn("credentials", text)
        self.assertNotIn(".env", text)
        self.assertNotIn("link.txt", text)
        self.assertNotIn("vendor", text)
        self.assertNotIn("main.go", text)  # depth 2 stops above apps/agent/main.go
        deep = self.tools.run({"op": "list", "path": "apps/agent", "depth": 1})["text"]
        self.assertIn("apps/agent/main.go  (", deep)
        self.assertNotIn(".env", deep)
        self.assertFalse(self.tools.run({"op": "list", "path": "nope"})["ok"])
        self.assertEqual(self.tools.run({"op": "list", "path": "../etc"})["error"], "invalid path")

    def test_read_excludes_secrets_symlinks_binaries_and_submodules(self) -> None:
        for path in ("server-guardian-credentials.enc", "apps/agent/.env", "apps/agent/link.txt",
                     "apps/agent/blob.bin", "vendor/sub"):
            res = self.tools.run({"op": "read", "path": path})
            self.assertFalse(res["ok"], path)
            self.assertEqual(res["error"], "excluded", path)
            self.assertEqual(res["text"], "")
        self.assertNotIn("OUTSIDE_SECRET", json.dumps(self.tools.run({"op": "read", "path": "apps/agent/link.txt"})))
        self.assertEqual(self.tools.run({"op": "read", "path": "../outside.txt"})["error"], "invalid path")
        self.assertEqual(self.tools.run({"op": "read", "path": "missing.go"})["error"], "not found")

    def test_grep_skips_excluded_files(self) -> None:
        res = self.tools.run({"op": "grep", "pattern": "TARGET", "glob": "apps/agent/n*.txt"})
        self.assertTrue(res["ok"])
        self.assertIn("apps/agent/notes.txt:2: beta TARGET gamma", res["text"])
        everything = self.tools.run({"op": "grep", "pattern": "TARGET"})["text"]
        for leaked in ("ENC_SECRET", "ENV_SECRET", "OUTSIDE_SECRET", "binary"):
            self.assertNotIn(leaked, everything)

    def test_grep_glob_uses_code_context_rules(self) -> None:
        text = self.tools.run({"op": "grep", "pattern": "TARGET", "glob": "apps/**/not*.txt"})["text"]
        self.assertEqual(text, "apps/agent/notes.txt:2: beta TARGET gamma")
        self.assertEqual(self.tools.run({"op": "grep", "pattern": "nothing-here"})["text"], "(no matches)")

    def test_grep_limits_lines(self) -> None:
        res = self.tools.run({"op": "grep", "pattern": "line", "glob": "apps/agent/big.txt"})
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["text"].splitlines()), 201)  # 200 matches plus the notice

    def test_grep_cuts_long_lines(self) -> None:
        (self.repo / "long.txt").write_text("LONGLINE" + "x" * 1000 + "\n")
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-qm", "long")
        tools = RepoTools(self.repo, git(self.repo, "rev-parse", "HEAD"))
        line = tools.run({"op": "grep", "pattern": "LONGLINE"})["text"]
        self.assertLessEqual(len(line), len("long.txt:1: ") + 301)

    def test_grep_rejects_bad_patterns_and_cannot_inject_options(self) -> None:
        for pattern in ("(unclosed", "x" * 201, "", "a\nb", None):
            self.assertFalse(self.tools.run({"op": "grep", "pattern": pattern})["ok"], pattern)
        # A pattern that looks like an option is searched for, not interpreted.
        res = self.tools.run({"op": "grep", "pattern": "--open-files-in-pager=touch pwned"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "(no matches)")
        self.assertFalse((self.repo / "pwned").exists())
        # So is a glob that looks like an option.
        self.assertFalse(self.tools.run({"op": "grep", "pattern": "x", "glob": "--no-index"})["ok"])
        res = self.tools.run({"op": "grep", "pattern": "x", "glob": ":(top)../../etc/*"})
        self.assertNotIn("passwd", res["text"])

    def test_reads_the_pinned_commit_not_the_working_tree(self) -> None:
        (self.repo / "apps/agent/notes.txt").write_text("UNCOMMITTED\n")
        (self.repo / "untracked.txt").write_text("TARGET untracked\n")
        self.assertIn("beta TARGET", self.tools.run({"op": "read", "path": "apps/agent/notes.txt"})["text"])
        self.assertNotIn("UNCOMMITTED", self.tools.run({"op": "grep", "pattern": "UNCOMMITTED|untracked"})["text"])
        self.assertEqual(self.tools.run({"op": "read", "path": "untracked.txt"})["error"], "not found")

    def test_read_truncates_large_files(self) -> None:
        res = self.tools.run({"op": "read", "path": "apps/agent/big.txt"})
        self.assertTrue(res["truncated"])
        self.assertTrue(res["ok"])
        self.assertLess(res["bytes"], 64 * 1024 + 200)
        self.assertIn("truncated by Guardian", res["text"])

    def test_read_selectors(self) -> None:
        by_lines = self.tools.run({"op": "read", "path": "apps/agent/main.go:7-7"})["text"]
        self.assertIn('7| \tfmt.Println("SERVE_BODY")', by_lines)
        self.assertNotIn("OTHER_BODY", by_lines)
        self.assertIn('import "fmt"', by_lines)  # the import block comes with a partial read
        by_symbol = self.tools.run({"op": "read", "path": "cli/tool.py#Box.open"})["text"]
        self.assertIn("def open(self):", by_symbol)
        self.assertNotIn("RUN_BODY", by_symbol)
        self.assertEqual(self.tools.run({"op": "read", "path": "cli/tool.py#Nope"})["error"], "symbol not found")
        self.assertFalse(self.tools.run({"op": "read", "path": "cli/tool.py:9-2"})["ok"])

    def test_outline(self) -> None:
        text = self.tools.run({"op": "outline", "path": "apps/agent/main.go"})["text"]
        self.assertIn("6: func Serve()", text)
        self.assertNotIn("SERVE_BODY", text)
        self.assertFalse(self.tools.run({"op": "outline", "path": "apps/agent/notes.txt"})["ok"])
        self.assertEqual(self.tools.run({"op": "outline", "path": "apps/agent/.env"})["error"], "excluded")

    def test_bad_requests_become_errors(self) -> None:
        self.assertFalse(self.tools.run("read")["ok"])
        self.assertIn("unknown op", self.tools.run({"op": "write", "path": "x"})["error"])
        self.assertFalse(self.tools.run({"op": "read"})["ok"])

    def test_seen_sink_reports_read_code(self) -> None:
        seen = []
        self.tools.run({"op": "read", "path": "apps/agent/notes.txt"}, on_seen=lambda *a: seen.append(a))
        path, segments, whole = seen[0]
        self.assertEqual((path, whole), ("apps/agent/notes.txt", True))
        self.assertIn("beta TARGET gamma", segments[0])

        seen.clear()
        self.tools.run({"op": "read", "path": "apps/agent/big.txt"}, on_seen=lambda *a: seen.append(a))
        self.assertFalse(seen[0][2])  # truncated: not shown in full
        self.assertIn("line 0 TARGET", seen[0][1][0])
        self.assertNotIn("line 7999 TARGET", seen[0][1][0])

        seen.clear()
        self.tools.run({"op": "grep", "pattern": "TARGET"}, on_seen=lambda *a: seen.append(a))
        self.tools.run({"op": "outline", "path": "apps/agent/main.go"}, on_seen=lambda *a: seen.append(a))
        self.assertEqual(seen, [])  # only reads count as seen code

    def test_budget_clipped_read_reports_nothing_seen(self) -> None:
        seen = []
        res = self.tools.run({"op": "read", "path": "apps/agent/notes.txt"}, on_seen=lambda *a: seen.append(a), max_bytes=10)
        self.assertTrue(res["truncated"])
        self.assertEqual(seen, [])
        empty = clip_result({"text": "abc", "bytes": 3, "ok": True, "truncated": False, "error": None, "op": "x", "target": "x"}, 0)
        self.assertEqual((empty["ok"], empty["error"], empty["text"]), (False, "budget", ""))

    def test_read_that_would_be_cut_below_the_fraction_is_deferred(self) -> None:
        seen = []
        res = self.tools.run({"op": "read", "path": "apps/agent/big.txt"}, on_seen=lambda *a: seen.append(a),
                             max_bytes=1000, defer_below=0.5)
        self.assertEqual((res["ok"], res["error"], res["bytes"], res["text"], res["deferred"]), (False, "deferred", 0, "", True))
        self.assertEqual((res["op"], res["target"]), ("read", "apps/agent/big.txt"))
        self.assertEqual(res["request"], {"op": "read", "path": "apps/agent/big.txt"})
        self.assertEqual(seen, [])
        # A read that still keeps at least half is clipped as before; other ops are never deferred.
        big = len(self.tools.run({"op": "read", "path": "apps/agent/big.txt"})["text"].encode())
        clipped = self.tools.run({"op": "read", "path": "apps/agent/big.txt"}, max_bytes=big - 10, defer_below=0.5)
        self.assertTrue(clipped["truncated"])
        self.assertNotIn("deferred", self.tools.run({"op": "grep", "pattern": "TARGET"}, max_bytes=10, defer_below=0.5))

    def test_cited_paths_match_whole_tree_paths_only(self) -> None:
        text = ("See apps/agent/main.go:12-30 and `cli/tool.py#Box.open`, also ./apps/agent/notes.txt. "
                "Not real: apps/agent/missing.go, other/main.go. Secret: apps/agent/.env and server-guardian-credentials.enc.")
        self.assertEqual(self.tools.cited_paths(text), ["apps/agent/main.go", "cli/tool.py", "apps/agent/notes.txt"])
        self.assertEqual(self.tools.cited_paths("no paths here, main.go alone is not a tree path"), [])

    def test_provided_paths_follow_what_a_result_shows(self) -> None:
        read = self.tools.run({"op": "read", "path": "apps/agent/main.go:6-8"})
        self.assertEqual(provided_paths(read), ["apps/agent/main.go"])
        self.assertEqual(provided_paths(self.tools.run({"op": "outline", "path": "cli/tool.py"})), ["cli/tool.py"])
        grep = self.tools.run({"op": "grep", "pattern": "TARGET", "glob": "apps/agent/n*"})
        self.assertEqual(provided_paths(grep), ["apps/agent/notes.txt"])
        self.assertEqual(provided_paths(self.tools.run({"op": "list"})), [])
        self.assertEqual(provided_paths(self.tools.run({"op": "read", "path": "apps/agent/.env"})), [])
        self.assertEqual(provided_paths(deferred_result({"op": "read", "path": "apps/agent/main.go"})), [])


class RepoBlockParsingTests(unittest.TestCase):
    def test_parse_and_strip(self) -> None:
        text = 'Let me look.\n```repo\n{"requests": [{"op": "list"}, {"op": "read", "path": "a"}]}\n```\nthen more'
        self.assertEqual(parse_repo_blocks(text), [{"op": "list"}, {"op": "read", "path": "a"}])
        self.assertEqual(strip_repo_blocks(text), "Let me look.\n\nthen more")
        self.assertIsNone(parse_repo_blocks("```json\n{}\n```"))
        self.assertIsNone(parse_repo_blocks("plain answer"))

    def test_malformed_blocks_are_kept_as_errors(self) -> None:
        bad = parse_repo_blocks("```repo\n{not json\n```")
        self.assertEqual(len(bad), 1)
        self.assertIn("invalid JSON", bad[0])
        self.assertIn("requests", parse_repo_blocks('```repo\n{"nope": 1}\n```')[0])

    def test_requests_are_not_capped_and_merge_across_blocks(self) -> None:
        body = json.dumps({"requests": [{"op": "list"}] * 12})
        items = parse_repo_blocks(f"```repo\n{body}\n```\ntext\n```repo\n{body}\n```")
        self.assertEqual(len(items), 24)
        self.assertTrue(all(isinstance(i, dict) for i in items))

    def test_trim_after_first_block(self) -> None:
        first = '```repo\n{"requests": [{"op": "list"}]}\n```'
        second = '```repo\n{"requests": [{"op": "read", "path": "a"}]}\n```'
        text = f"Look.\n{first}\n\nNow I have read enough. {second}\nFabricated analysis."
        kept, discarded, blocks = trim_after_first_block(text)
        self.assertEqual(kept, f"Look.\n{first}")
        self.assertEqual(discarded, len(text) - len(f"Look.\n{first}") - 2)  # the blank line after the block is not counted
        self.assertEqual(blocks, 2)
        self.assertEqual(trim_after_first_block(f"{first}\n  \n"), (first, 0, 1))
        self.assertEqual(trim_after_first_block("final answer"), ("final answer", 0, 0))


if __name__ == "__main__":
    unittest.main()

"""
Tests of the compiler-assisted Go index (docs/design/debate-code-index-wiki.md, P2.6): the facts loader, the
extraction flow with a fake helper (cache, waiting, failures, cleanup), the Go-aware ops, the map ranking, the engine
events and the real helper (built with `go build`; skipped without a Go toolchain).
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import go_facts
from go_facts import GOINDEX_VERSION, GoFacts, GoFactsProvider, module_dirs, prune_store
from repo_map import build_map
from repo_mcp import RepoMcpServer
from repo_tools import RepoTools, provided_paths, provided_spans
from test_repo_tools import COMMIT, git
from test_retrieval_loop import EngineHarness, FakeRunner, block
from testenv import isolate_data_dir, restore_data_dir

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "goindex"

A_GO = 'package a\n\nfunc Handle() string {\n\treturn "a"\n}\n'
B_GO = 'package b\n\nfunc Handle() string {\n\treturn "b"\n}\n'
A_TEST_GO = 'package a\n\nimport "testing"\n\nfunc TestHandle(t *testing.T) {\n\tHandle()\n}\n'
MAIN_GO = ('package main\n\nimport (\n\t"strings"\n\n\t"example.com/m/a"\n)\n\n'
           'func main() {\n\tprintln(strings.ToUpper(a.Handle()))\n}\n')
HANDLER_TS = "export function Handle(event: string): string {\n  return event;\n}\n"
USE_TS = 'import { Handle } from "./handler";\nHandle("x");\n'
FILES = {
    "go.mod": "module example.com/m\n\ngo 1.22\n",
    "a/a.go": A_GO, "a/a_test.go": A_TEST_GO, "b/b.go": B_GO, "main.go": MAIN_GO,
    "web/handler.ts": HANDLER_TS, "web/use.ts": USE_TS,
}
GO_FILES = ["a/a.go", "a/a_test.go", "b/b.go", "main.go"]
GO_VERSION = "go1.99.0"


def setUpModule() -> None:
    isolate_data_dir()  # also points GUARDIAN_GOINDEX_BIN at a missing helper; tests set their own


def tearDownModule() -> None:
    restore_data_dir()


def make_go_repo(root: Path) -> Path:
    repo = root / "ws"
    for name, content in FILES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, *COMMIT, "commit", "-q", "-m", "one")
    return repo


def sample_facts(sha: str = "", go: str = GO_VERSION) -> Dict[str, Any]:
    """Facts matching FILES: Handle in packages a and b (only a's is called), main calling it and strings.ToUpper."""
    defs = [
        {"id": 1, "name": "Handle", "qualified": "Handle", "kind": "function", "pkg": "example.com/m/a",
         "path": "a/a.go", "start": 3, "end": 5, "exported": True, "test": False},
        {"id": 2, "name": "Handle", "qualified": "Handle", "kind": "function", "pkg": "example.com/m/b",
         "path": "b/b.go", "start": 3, "end": 5, "exported": True, "test": False},
        {"id": 3, "name": "main", "qualified": "main", "kind": "function", "pkg": "example.com/m", "path": "main.go",
         "start": 9, "end": 11, "exported": False, "test": False},
        {"id": 4, "name": "TestHandle", "qualified": "TestHandle", "kind": "function", "pkg": "example.com/m/a",
         "path": "a/a_test.go", "start": 5, "end": 7, "exported": True, "test": True},
        {"id": 5, "name": "ToUpper", "qualified": "ToUpper", "kind": "function", "pkg": "strings",
         "exported": True, "test": False, "external": True},
    ]
    refs = [
        {"def": 1, "path": "main.go", "line": 10, "enclosing": "main", "call": True, "test": False},
        {"def": 1, "path": "a/a_test.go", "line": 6, "enclosing": "TestHandle", "call": True, "test": True},
        {"def": 5, "path": "main.go", "line": 10, "enclosing": "main", "call": True, "test": False},
    ]
    packages = [{"path": p, "dir": d, "module": ".", "imports": [], "test": False, "errors": []}
                for p, d in (("example.com/m", "."), ("example.com/m/a", "a"), ("example.com/m/b", "b"))]
    return {"version": GOINDEX_VERSION, "sha": sha, "go": go, "goos": "linux", "goarch": "arm64", "modules": ["."],
            "module_errors": [], "packages": packages, "files": list(GO_FILES), "defs": defs, "refs": refs,
            "unresolved": []}


FAKE_HELPER = textwrap.dedent('''\
    #!{python}
    import json, os, subprocess, sys, time
    from pathlib import Path

    args = sys.argv[1:]
    opt = lambda name: args[args.index(name) + 1]
    here = Path(__file__).resolve().parent
    cfg = json.loads((here / "fake.json").read_text())
    root, out, sha = opt("-root"), opt("-out"), opt("-sha")
    with open(here / "calls.log", "a") as log:
        log.write(sha + "\\n")
    probe = {{"readable": Path(opt("-readable")).read_text(), "root_exists": os.path.isdir(root), "root_files": sorted(os.listdir(root)) if os.path.isdir(root) else [],
             "root": root, "cwd": os.getcwd(), "pid": os.getpid(), "argv": args,
             "env": {{k: os.environ.get(k) for k in ("AGENT_TOKEN", "OPENAI_API_KEY", "GUARDIAN_DATA_DIR", "CGO_ENABLED",
                                                   "GOCACHE", "GOTOOLCHAIN", "GOFLAGS", "GOPROXY", "PATH")}}}}
    mode = cfg.get("mode", "ok")
    if mode == "sleep":
        child = subprocess.Popen(["sleep", "60"])
        probe["child"] = child.pid
        (here / "probe.json").write_text(json.dumps(probe))
        time.sleep(60)
    (here / "probe.json").write_text(json.dumps(probe))
    time.sleep(cfg.get("delay", 0))
    facts = cfg["facts"]
    facts["sha"] = sha
    if mode == "fail":
        sys.stderr.write("boom: the build exploded\\nsecond line\\n")
        sys.exit(1)
    if mode == "module_error":
        facts["module_errors"] = [{{"dir": ".", "error": "cannot find module"}}]
        facts["modules"] = []
        Path(out).write_text(json.dumps(facts))
        sys.exit(1)
    if mode == "partial":
        facts["module_errors"] = [{{"dir": "tools", "error": "missing dependency"}}]
    if mode == "badversion":
        facts["version"] = 99
    Path(out).write_text(json.dumps(facts))
''')

FAKE_GO = textwrap.dedent('''\
    #!/bin/sh
    echo "$@" >> "$(dirname "$0")/go_calls.log"
    case "$1 $2" in
      "env GOVERSION") echo {version};;
      "clean -cache") rm -rf "$GOCACHE"/*;;
    esac
''')


def worktrees(repo: Path) -> List[str]:
    return [line for line in git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def leftover_temp_dirs() -> List[str]:
    return sorted(p.name for p in Path(tempfile.gettempdir()).glob(f"{go_facts.TMP_PREFIX}*"))


class FactsTests(unittest.TestCase):
    def test_lookups(self) -> None:
        facts = GoFacts.from_data(sample_facts("s"))
        assert facts is not None
        self.assertEqual([d["path"] for d in facts.find_defs("Handle")], ["a/a.go", "b/b.go"])
        self.assertEqual([d["id"] for d in facts.find_defs("TestHandle")], [4])
        self.assertEqual(facts.find_defs("ToUpper"), [])  # external definitions are not definitions of the repository
        self.assertEqual(len(facts.refs_to([1])), 2)
        callees = facts.callees(facts.find_defs("main")[0])
        self.assertEqual([(d["qualified"], lines) for d, lines in callees], [("Handle", [10]), ("ToUpper", [10])])
        self.assertEqual(facts.counts(), {"packages": 3, "defs": 4, "refs": 3, "unresolved": 0})
        self.assertTrue(facts.covers("a/a.go"))
        self.assertFalse(facts.covers("web/use.ts"))

    def test_method_symbols_match_the_qualified_name(self) -> None:
        data = sample_facts()
        data["defs"].append({"id": 6, "name": "Run", "qualified": "Server.Run", "kind": "method", "pkg": "p",
                             "path": "s.go", "start": 1, "end": 2, "exported": True, "test": False})
        facts = GoFacts.from_data(data)
        assert facts is not None
        self.assertEqual([d["id"] for d in facts.find_defs("Server.Run")], [6])
        self.assertEqual([d["id"] for d in facts.find_defs("Run")], [6])
        self.assertEqual(facts.find_defs("Other.Run"), [])

    def test_wrong_version_or_shape_is_not_facts(self) -> None:
        data = sample_facts()
        data["version"] = GOINDEX_VERSION + 1
        self.assertIsNone(GoFacts.from_data(data))
        self.assertIsNone(GoFacts.from_data({"version": GOINDEX_VERSION, "defs": "x", "refs": []}))
        self.assertIsNone(GoFacts.from_data({"version": GOINDEX_VERSION, "defs": [{"no": "id"}], "refs": []}))
        self.assertIsNone(GoFacts.from_data([]))

    def test_module_dirs_follow_the_mcp_client_rule(self) -> None:
        paths = ["go.mod", "apps/agent/go.mod", "vendor/x/go.mod", "web/node_modules/y/go.mod", "pkg/testdata/go.mod",
                 "apps/agent/main.go", "docs/go.mod.md"]
        self.assertEqual(module_dirs(paths), [".", "apps/agent"])


class StoreTests(unittest.TestCase):
    def test_prune_keeps_the_newest_twenty(self) -> None:
        with TemporaryDirectory() as tmp:
            store = Path(tmp)
            for i in range(25):
                path = store / f"{i:040d}.json"
                path.write_text("{}")
                (store / f"{i:040d}.status.json").write_text("{}")
                os.utime(path, (1000 + i, 1000 + i))
            self.assertEqual(prune_store(store), 5)
            left = sorted(p.name for p in store.glob("*.json") if not p.name.endswith(".status.json"))
            self.assertEqual(len(left), 20)
            self.assertEqual(left[0], f"{5:040d}.json")
            self.assertFalse((store / f"{0:040d}.status.json").exists())
            self.assertTrue((store / f"{24:040d}.status.json").exists())

    def test_reap_stale_temp_removes_only_old_dirs(self) -> None:
        old = Path(tempfile.mkdtemp(prefix=go_facts.TMP_PREFIX))
        fresh = Path(tempfile.mkdtemp(prefix=go_facts.TMP_PREFIX))
        try:
            os.utime(old, (1000, 1000))
            go_facts.reap_stale_temp()
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
        finally:
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(fresh, ignore_errors=True)


class ExtractionCase(unittest.TestCase):
    """A git repository with Go files, a fake goindex and a fake go in a temp dir, and a private data dir."""

    PROJECT = "proj"

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_go_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.helper = self.bin / "goindex"
        self.helper.write_text(FAKE_HELPER.format(python=sys.executable))
        self.helper.chmod(0o755)
        self.fake_go = self.bin / "go"
        self.fake_go.write_text(FAKE_GO.format(version=GO_VERSION))
        self.fake_go.chmod(0o755)
        self.configure({"mode": "ok", "facts": sample_facts()})
        self.data = self.root / "data"
        self.cache = self.data / "code-index" / self.PROJECT
        patches = [
            patch.dict(os.environ, {"GUARDIAN_GOINDEX_BIN": str(self.helper), "GUARDIAN_DATA_DIR": str(self.data),
                                    "AGENT_TOKEN": "tok-secret", "OPENAI_API_KEY": "key-secret",
                                    "GUARDIAN_GOINDEX_TIMEOUT": "30", "GUARDIAN_GOINDEX_WAIT": "30",
                                    "GUARDIAN_LLM_PROVIDERS": "none"}),
            patch.object(go_facts, "go_binary", return_value=str(self.fake_go)),
            patch.dict(go_facts._GO_VERSIONS, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.providers: List[GoFactsProvider] = []
        self.emitted: List[tuple] = []
        self._events_lock = threading.Lock()

    def tearDown(self) -> None:
        for provider in self.providers:
            provider.close()
        self._tmp.cleanup()

    def configure(self, cfg: Dict[str, Any]) -> None:
        (self.bin / "fake.json").write_text(json.dumps(cfg))

    def tools(self) -> RepoTools:
        tools = RepoTools(self.repo, self.sha, cache_dir=self.cache)
        assert tools.go_provider is not None
        self.providers.append(tools.go_provider)
        return tools

    def sink(self, kind: str, data: Dict[str, Any]) -> None:
        with self._events_lock:
            self.emitted.append((kind, data))

    def run_to_end(self, tools: Optional[RepoTools] = None) -> GoFactsProvider:
        tools = tools or self.tools()
        provider = tools.go_provider
        assert provider is not None
        provider.start(self.sink)
        if provider._thread is not None:
            provider._thread.join(60)
            self.assertFalse(provider._thread.is_alive())
        return provider

    def calls(self) -> int:
        log = self.bin / "calls.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def probe(self) -> Dict[str, Any]:
        return json.loads((self.bin / "probe.json").read_text())

    def assert_clean(self) -> None:
        self.assertEqual(worktrees(self.repo), [f"worktree {self.repo.resolve()}"])
        self.assertEqual(leftover_temp_dirs(), [])


class ExtractionTests(ExtractionCase):
    def test_extracts_in_a_throwaway_worktree_and_cleans_up(self) -> None:
        before = leftover_temp_dirs()
        provider = self.run_to_end()
        self.assertEqual(provider.state, "built")
        facts = provider.get()
        assert facts is not None
        self.assertEqual(facts.go, GO_VERSION)
        stored = self.cache / "go" / f"{self.sha}.json"
        self.assertTrue(stored.is_file())
        self.assertEqual(json.loads(stored.read_text())["sha"], self.sha)
        # The helper saw a checked-out tree of the commit, not the workspace itself.
        probe = self.probe()
        self.assertTrue(probe["root_exists"])
        self.assertIn("main.go", probe["root_files"])
        self.assertNotEqual(Path(probe["root"]).resolve(), self.repo.resolve())
        self.assertEqual(self.emitted[-1][0], "compiler_index")
        event = self.emitted[-1][1]
        self.assertEqual((event["lang"], event["sha"], event["status"]), ("go", self.sha, "built"))
        self.assertEqual((event["packages"], event["defs"], event["refs"], event["unresolved"]), (3, 4, 3, 0))
        self.assertIsNone(event["error"])
        self.assertIsInstance(event["ms"], int)
        self.assertEqual(len([e for e in self.emitted if e[0] == "compiler_index"]), 1)
        self.assertEqual(worktrees(self.repo), [f"worktree {self.repo.resolve()}"])
        self.assertEqual(leftover_temp_dirs(), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # the workspace itself is untouched

    def test_helper_arguments_and_child_environment(self) -> None:
        self.run_to_end()
        probe = self.probe()
        argv = probe["argv"]
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-module"], ["."])
        self.assertEqual(argv[argv.index("-sha") + 1], self.sha)
        self.assertIn("-timeout", argv)
        env = probe["env"]
        self.assertIsNone(env["AGENT_TOKEN"])
        self.assertIsNone(env["OPENAI_API_KEY"])
        self.assertIsNone(env["GUARDIAN_DATA_DIR"])
        self.assertEqual(env["CGO_ENABLED"], "0")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOFLAGS"], "-mod=readonly")
        self.assertEqual(env["GOPROXY"], "off")
        self.assertEqual(env["GOCACHE"], str(self.data / "code-index" / ".gocache"))
        self.assertTrue(env["PATH"].startswith(str(Path.home() / ".local" / "go" / "bin")))
        self.assertEqual(Path(probe["cwd"]).resolve().parent, Path(tempfile.gettempdir()).resolve())

    def test_only_readable_go_files_are_given_to_the_helper(self) -> None:
        self.run_to_end()
        self.assertEqual(self.probe()["readable"].split(), sorted(GO_FILES))

    def test_second_run_is_a_cache_hit(self) -> None:
        self.run_to_end()
        self.assertEqual(self.calls(), 1)
        self.emitted.clear()
        second = self.run_to_end()
        self.assertEqual(second.state, "cached")
        self.assertEqual(self.calls(), 1)
        self.assertIsNotNone(second.get(wait=0))
        self.assertEqual([(k, d["status"]) for k, d in self.emitted], [("compiler_index", "cached")])
        self.assert_clean()

    def test_a_different_go_version_rebuilds(self) -> None:
        self.run_to_end()
        self.fake_go.write_text(FAKE_GO.format(version="go2.0.0"))
        go_facts._GO_VERSIONS.clear()
        self.configure({"mode": "ok", "facts": sample_facts(go="go2.0.0")})
        again = self.run_to_end()
        self.assertEqual(again.state, "built")
        self.assertEqual(self.calls(), 2)

    def test_another_helper_version_is_failed_not_trusted(self) -> None:
        self.configure({"mode": "badversion", "facts": sample_facts()})
        provider = self.run_to_end()
        self.assertEqual(provider.state, "failed")
        self.assertIn("another version", provider.error)
        self.assertFalse((self.cache / "go" / f"{self.sha}.json").exists())
        self.assert_clean()

    def test_partial_module_failure_still_builds(self) -> None:
        self.configure({"mode": "partial", "facts": sample_facts()})
        provider = self.run_to_end()
        self.assertEqual(provider.state, "built")
        self.assertIn("missing dependency", self.emitted[-1][1]["error"])

    def test_helper_failure_and_all_modules_failing(self) -> None:
        self.configure({"mode": "fail", "facts": sample_facts()})
        provider = self.run_to_end()
        self.assertEqual(provider.state, "failed")
        self.assertEqual(self.emitted[-1][1]["status"], "failed")
        self.assertEqual(self.emitted[-1][1]["error"], "boom: the build exploded")
        self.assert_clean()
        self.emitted.clear()
        self.configure({"mode": "module_error", "facts": sample_facts()})
        provider = self.run_to_end()
        self.assertEqual(provider.state, "failed")
        self.assertEqual(self.emitted[-1][1]["error"], "cannot find module")
        self.assertIsNone(provider.get(wait=0))
        self.assert_clean()

    def test_failure_is_not_retried_within_the_run(self) -> None:
        self.configure({"mode": "fail", "facts": sample_facts()})
        provider = self.run_to_end()
        provider.start(self.sink)
        provider.start(self.sink)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(len(self.emitted), 1)

    def test_timeout_kills_the_process_group_and_cleans_up(self) -> None:
        self.configure({"mode": "sleep", "facts": sample_facts()})
        with patch.dict(os.environ, {"GUARDIAN_GOINDEX_TIMEOUT": "1"}):
            started = time.monotonic()
            provider = self.run_to_end()
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(provider.state, "timeout")
        self.assertEqual(self.emitted[-1][1]["status"], "timeout")
        probe = self.probe()
        for pid in (probe["pid"], probe["child"]):
            self.assert_process_gone(pid)
        self.assert_clean()
        self.assertEqual(provider.effective_state(), "timeout")
        self.assertIn("timeout", provider.note())

    def test_close_stops_a_running_extraction_and_cleans_up(self) -> None:
        self.configure({"mode": "sleep", "facts": sample_facts()})
        tools = self.tools()
        provider = tools.go_provider
        assert provider is not None
        provider.start(self.sink)
        deadline = time.monotonic() + 20
        while not (self.bin / "probe.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.bin / "probe.json").exists())
        provider.close()
        self.assertFalse(provider._thread.is_alive())  # type: ignore[union-attr]
        probe = self.probe()
        self.assert_process_gone(probe["pid"])
        self.assert_process_gone(probe["child"])
        self.assert_clean()

    def assert_process_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} is still running")

    def test_unavailable_when_helper_or_go_is_missing(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_GOINDEX_BIN": str(self.root / "nope")}):
            provider = self.run_to_end()
        self.assertEqual(provider.state, "unavailable")
        self.assertIsNone(provider._thread)
        self.assertEqual([(k, d["status"]) for k, d in self.emitted], [("compiler_index", "unavailable")])
        self.assertIn("goindex helper not found", self.emitted[0][1]["error"])
        self.assertIsNone(provider.get())
        self.assertIn("unavailable", provider.note())
        self.emitted.clear()
        with patch.object(go_facts, "go_binary", return_value=None):
            provider = self.run_to_end()
        self.assertEqual(provider.state, "unavailable")
        self.assertIn("go not found", self.emitted[0][1]["error"])
        self.assertEqual(self.calls(), 0)
        self.assert_clean()

    def test_repository_without_go_mod_does_nothing(self) -> None:
        other = self.root / "py"
        other.mkdir()
        (other / "a.py").write_text("x = 1\n")
        git(other, "init", "-q", "-b", "main")
        git(other, "add", "-A")
        git(other, *COMMIT, "commit", "-q", "-m", "one")
        tools = RepoTools(other, git(other, "rev-parse", "HEAD"), cache_dir=self.cache)
        assert tools.go_provider is not None
        tools.go_provider.start(self.sink)
        self.assertEqual(tools.go_provider.state, "idle")
        self.assertEqual(self.emitted, [])
        self.assertIsNone(tools.go_map_facts())
        self.assertEqual(tools.go_note(), "")

    def test_cleanup_failure_is_reported_and_recovered(self) -> None:
        provider = self.tools().go_provider
        assert provider is not None
        original = provider._git

        def failing_remove(args, env, check=True):  # type: ignore[no-untyped-def]
            if "remove" in args:
                return subprocess.CompletedProcess(args, 1, b"", b"fatal: cannot remove")
            return original(args, env, check)

        with patch.object(provider, "_git", failing_remove):
            provider.start(self.sink)
            provider._thread.join(60)  # type: ignore[union-attr]
        self.assertEqual(provider.state, "built")
        warnings = [d for k, d in self.emitted if k == "compiler_index_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("worktree remove failed", warnings[0]["message"])
        self.assert_clean()  # the temp dir was deleted and the worktree entry pruned

    def test_concurrent_runs_share_one_extraction(self) -> None:
        self.configure({"mode": "ok", "delay": 1.5, "facts": sample_facts()})
        first, second = self.tools(), self.tools()
        for tools in (first, second):
            assert tools.go_provider is not None
            tools.go_provider.start(self.sink)
        for tools in (first, second):
            assert tools.go_provider is not None
            tools.go_provider._thread.join(60)  # type: ignore[union-attr]
        self.assertEqual(self.calls(), 1)
        self.assertEqual(sorted(p.state for p in (first.go_provider, second.go_provider)), ["built", "cached"])  # type: ignore[union-attr]
        self.assertIsNotNone(first.go_provider.get(wait=0))  # type: ignore[union-attr]
        self.assertIsNotNone(second.go_provider.get(wait=0))  # type: ignore[union-attr]
        self.assert_clean()

    def test_build_cache_is_trimmed_over_the_limit(self) -> None:
        gocache = self.data / "code-index" / ".gocache"
        gocache.mkdir(parents=True)
        (gocache / "blob").write_bytes(b"x" * 20000)
        with patch.dict(os.environ, {"GUARDIAN_GOINDEX_CACHE_MB": "0.001"}):
            self.run_to_end()
        self.assertIn("clean -cache", (self.bin / "go_calls.log").read_text())
        self.assertFalse((gocache / "blob").exists())

    def test_build_cache_below_the_limit_is_kept(self) -> None:
        gocache = self.data / "code-index" / ".gocache"
        gocache.mkdir(parents=True)
        (gocache / "blob").write_bytes(b"x" * 100)
        self.run_to_end()
        self.assertNotIn("clean -cache", (self.bin / "go_calls.log").read_text())
        self.assertTrue((gocache / "blob").exists())

    def test_old_facts_are_pruned_after_a_build(self) -> None:
        store = self.cache / "go"
        store.mkdir(parents=True)
        for i in range(22):
            path = store / f"{i:040d}.json"
            path.write_text("{}")
            os.utime(path, (1000 + i, 1000 + i))
        self.run_to_end()
        facts_files = [p for p in store.glob("*.json") if not p.name.endswith(".status.json")]
        self.assertEqual(len(facts_files), 20)
        self.assertTrue((store / f"{self.sha}.json").exists())


class WaitingTests(ExtractionCase):
    def test_wait_times_out_once_and_falls_back_to_name_based_results(self) -> None:
        self.configure({"mode": "ok", "delay": 2.5, "facts": sample_facts()})
        tools = self.tools()
        provider = tools.go_provider
        assert provider is not None
        provider.start(self.sink)
        with patch.dict(os.environ, {"GUARDIAN_GOINDEX_WAIT": "0.3"}):
            started = time.monotonic()
            res = tools.refs("Handle")
            first = time.monotonic() - started
            started = time.monotonic()
            again = tools.refs("Handle")
            second = time.monotonic() - started
        self.assertLess(first, 2.0)
        self.assertLess(second, 0.2)  # only the first request waits
        for result in (res, again):
            self.assertTrue(result["ok"])
            self.assertEqual(result["resolved"], "name")
            self.assertIn("name-based matches", result["text"])
            self.assertIn("[type information for Go is still being prepared: results are name-based]", result["text"])
        self.assertIn("still being prepared", tools.defs("Handle")["text"].splitlines()[0])
        # The extraction goes on; once it is done the same request is type-checked.
        provider._thread.join(60)  # type: ignore[union-attr]
        done = tools.refs("Handle")
        self.assertEqual(done["resolved"], "types")
        self.assertIn("type-checked", done["text"])

    def test_callers_and_callees_report_why_there_is_no_answer(self) -> None:
        self.configure({"mode": "ok", "delay": 2.5, "facts": sample_facts()})
        tools = self.tools()
        assert tools.go_provider is not None
        tools.go_provider.start(self.sink)
        with patch.dict(os.environ, {"GUARDIAN_GOINDEX_WAIT": "0.2"}):
            res = tools.callers("Handle")
        self.assertFalse(res["ok"])
        self.assertIn("no type information yet (Go index pending); use refs", res["error"])
        tools.go_provider._thread.join(60)  # type: ignore[union-attr]
        self.assertTrue(tools.callers("Handle")["ok"])

    def test_failed_extraction_is_visible_to_a_reading_process(self) -> None:
        self.configure({"mode": "fail", "facts": sample_facts()})
        self.run_to_end()
        reader = self.tools()  # a repo MCP server: loads files, never extracts
        res = reader.refs("Handle")
        self.assertEqual(res["resolved"], "name")
        self.assertIn("[no type information for Go (failed): results are name-based]", res["text"])
        denied = reader.callers("Handle")
        self.assertFalse(denied["ok"])
        self.assertIn("Go index failed", denied["error"])

    def test_reader_waits_while_another_process_extracts(self) -> None:
        store = self.cache / "go"
        store.mkdir(parents=True)
        holder = os.open(store / f"{self.sha}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        reader = self.tools()
        facts_path = store / f"{self.sha}.json"

        def finish_later() -> None:
            time.sleep(0.6)
            facts_path.write_text(json.dumps(sample_facts(self.sha)))
            fcntl.flock(holder, fcntl.LOCK_UN)

        threading.Thread(target=finish_later, daemon=True).start()
        try:
            started = time.monotonic()
            res = reader.callers("Handle")
            self.assertTrue(res["ok"], res)
            self.assertLess(time.monotonic() - started, 10)
        finally:
            os.close(holder)

    def test_reader_gives_up_after_the_wait(self) -> None:
        store = self.cache / "go"
        store.mkdir(parents=True)
        holder = os.open(store / f"{self.sha}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            reader = self.tools()
            with patch.dict(os.environ, {"GUARDIAN_GOINDEX_WAIT": "0.3"}):
                res = reader.callers("Handle")
            self.assertFalse(res["ok"])
            self.assertIn("pending", res["error"])
        finally:
            os.close(holder)


class OpsCase(unittest.TestCase):
    """RepoTools over the FILES repository with sample facts stored for its commit (no extraction involved)."""

    facts: Optional[Dict[str, Any]] = None

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_go_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.cache = self.root / "cache"
        store = self.cache / "go"
        store.mkdir(parents=True)
        (store / f"{self.sha}.json").write_text(json.dumps(self.facts or sample_facts(self.sha)))
        patcher = patch.object(go_facts, "go_binary", return_value=None)  # accept the stored facts as they are
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = RepoTools(self.repo, self.sha, cache_dir=self.cache)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class GoOpsTests(OpsCase):
    def test_refs_are_type_checked_and_leave_the_uncalled_same_name_out(self) -> None:
        res = self.tools.refs("Handle")
        self.assertTrue(res["ok"])
        self.assertEqual(res["resolved"], "types")
        lines = res["text"].splitlines()
        self.assertEqual(lines[0], f"[refs of Handle: type-checked ({GO_VERSION}), 3 packages; non-Go files are name-based]")
        self.assertIn("[2 definitions match this name (packages: example.com/m/a, example.com/m/b); "
                      "references to all of them are listed]", lines)
        self.assertIn("a/a_test.go:6: \tHandle()", lines)
        self.assertIn("main.go:10: \tprintln(strings.ToUpper(a.Handle()))", lines)
        self.assertFalse(any(line.startswith("b/b.go:") for line in lines))
        # The TypeScript Handle is another language: its uses stay name-based, under their own header.
        header = next(i for i, line in enumerate(lines) if line.startswith("[refs of Handle in files without type information"))
        self.assertEqual(lines[header + 1:], ['web/use.ts:1: import { Handle } from "./handler";', 'web/use.ts:2: Handle("x");'])
        self.assertEqual(provided_paths(res), ["a/a_test.go", "main.go", "web/use.ts"])
        self.assertEqual(provided_spans(res), {"a/a_test.go": [(6, 6)], "main.go": [(10, 10)], "web/use.ts": [(1, 2)]})

    def test_refs_include_defs_adds_the_declaration_lines(self) -> None:
        text = self.tools.refs("Handle", include_defs=True)["text"]
        self.assertIn("a/a.go:3: func Handle() string {", text)
        self.assertIn("b/b.go:3: func Handle() string {", text)

    def test_refs_glob_limits_the_references(self) -> None:
        text = self.tools.refs("Handle", glob="a/*")["text"]
        self.assertIn("a/a_test.go:6:", text)
        self.assertNotIn("main.go:10", text)
        self.assertNotIn("web/use.ts", text)

    def test_refs_without_hits_still_say_type_checked(self) -> None:
        text = self.tools.refs("TestHandle")["text"]
        self.assertEqual(text.splitlines(), [f"[refs of TestHandle: type-checked ({GO_VERSION}), 3 packages; "
                                              "non-Go files are name-based]", "(no references found)"])

    def test_unknown_symbols_and_non_go_symbols_stay_name_based(self) -> None:
        res = self.tools.refs("Missing")
        self.assertEqual(res["resolved"], "name")
        self.assertNotIn("type-checked", res["text"])
        self.assertNotIn("still being prepared", res["text"])

    def test_files_without_type_information_keep_their_name_based_results(self) -> None:
        data = sample_facts(self.sha)
        data["files"] = [f for f in data["files"] if f != "b/b.go"]
        data["defs"] = [d for d in data["defs"] if d["id"] != 2]
        (self.cache / "go" / f"{self.sha}.json").write_text(json.dumps(data))
        text = RepoTools(self.repo, self.sha, cache_dir=self.cache).refs("Handle", include_defs=True)["text"]
        self.assertIn("web/use.ts:2", text)
        self.assertIn("b/b.go:3: func Handle() string {", text)  # b/b.go was not type-checked: name-based fallback

    def test_unresolved_locations_are_counted_in_the_footer(self) -> None:
        data = sample_facts(self.sha)
        data["unresolved"] = [{"path": "main.go", "line": 10, "name": "Handle", "reason": "type error"},
                              {"path": "x.go", "line": 2, "name": "Other", "reason": "type error"}]
        (self.cache / "go" / f"{self.sha}.json").write_text(json.dumps(data))
        tools = RepoTools(self.repo, self.sha, cache_dir=self.cache)
        footer = tools.refs("Handle")["text"].splitlines()[-1]
        self.assertTrue(footer.startswith("[index coverage: 1 location unresolved due to type errors"), footer)
        self.assertIn("use grep for these: main.go]", footer)
        self.assertNotIn("x.go", footer)
        self.assertIn("1 location unresolved due to type errors", tools.callers("Handle")["text"].splitlines()[-1])
        self.assertEqual(provided_paths(tools.refs("Handle")), ["a/a_test.go", "main.go", "web/use.ts"])

    def test_defs_show_both_packages_and_the_other_language_separately(self) -> None:
        res = self.tools.defs("Handle")
        self.assertEqual(res["resolved"], "types")
        self.assertEqual(res["text"].splitlines(), [
            f"[defs of Handle: type-checked ({GO_VERSION})]",
            "a/a.go:3-5 function func Handle() string {  (package example.com/m/a)",
            "b/b.go:3-5 function func Handle() string {  (package example.com/m/b)",
            "[defs of Handle in files without type information: name-based]",
            "web/handler.ts:1-3 function export function Handle(event: string): string {",
        ])
        self.assertEqual(provided_paths(res), [])
        self.assertEqual(self.tools.defs("main")["text"].splitlines()[1], "main.go:9-11 function func main() {")

    def test_defs_glob_filters_definitions(self) -> None:
        text = self.tools.defs("Handle", glob="b/*")["text"]
        self.assertIn("b/b.go:3-5", text)
        self.assertNotIn("a/a.go", text)
        self.assertNotIn("(package", text)  # a single package: no suffix

    def test_callers_lists_call_sites_with_the_enclosing_function(self) -> None:
        res = self.tools.callers("Handle")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["resolved"], "types")
        self.assertEqual(res["text"].splitlines()[-1], "main.go:10: main  println(strings.ToUpper(a.Handle()))")
        self.assertTrue(res["text"].splitlines()[0].startswith(f"[callers of Handle: type-checked ({GO_VERSION})"))
        self.assertIn("matched 2 definitions:", res["text"])
        with_tests = self.tools.callers("Handle", include_tests=True)["text"]
        self.assertIn("a/a_test.go:6: TestHandle  Handle()", with_tests)
        self.assertEqual(provided_paths(res), [])  # a location list, never code evidence
        self.assertEqual(provided_spans(res), {})
        self.assertEqual(self.tools.mentioned_in(res), [])

    def test_callers_glob_and_unknown_symbol(self) -> None:
        text = self.tools.callers("Handle", glob="a/*", include_tests=True)["text"]
        self.assertIn("a/a_test.go:6", text)
        self.assertNotIn("main.go", text)
        self.assertIn("(no calls found", self.tools.callers("Handle", glob="web/*")["text"])
        self.assertIn("no type-checked Go definition named Nope", self.tools.callers("Nope")["text"])

    def test_callees_show_definition_locations_and_external_targets(self) -> None:
        res = self.tools.callees("main")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["resolved"], "types")
        self.assertEqual(res["text"].splitlines()[1:], [
            "a/a.go:3  Handle  (called at line 10)",
            "strings.ToUpper  (called at line 10)",
        ])
        self.assertEqual(provided_paths(res), [])
        self.assertEqual(self.tools.mentioned_in(res), [])
        self.assertIn("(no calls found)", self.tools.callees("Handle")["text"])

    def test_requests_go_through_run_and_validate(self) -> None:
        run = self.tools.run({"op": "callers", "symbol": "Handle", "include_tests": True})
        self.assertTrue(run["ok"])
        self.assertIn("a/a_test.go:6:", run["text"])
        self.assertEqual(self.tools.run({"op": "callees", "symbol": "main"})["target"], "main")
        bad = self.tools.run({"op": "callers", "symbol": "not a symbol!"})
        self.assertFalse(bad["ok"])
        self.assertIn("invalid symbol", bad["error"])

    def test_non_go_repository_reports_no_type_information(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            (repo / "a.py").write_text("def f():\n    pass\n")
            git(repo, "init", "-q", "-b", "main")
            git(repo, "add", "-A")
            git(repo, *COMMIT, "commit", "-q", "-m", "one")
            tools = RepoTools(repo, git(repo, "rev-parse", "HEAD"), cache_dir=Path(tmp) / "c")
            res = tools.callers("f")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "no type information for this language (Go only); use refs")
            self.assertEqual(tools.refs("f")["resolved"], "name")

    def test_no_provider_means_the_old_behaviour(self) -> None:
        tools = RepoTools(self.repo, self.sha)  # no cache dir, so no facts
        self.assertIsNone(tools.go_provider)
        self.assertEqual(tools.refs("Handle")["text"].splitlines()[0],
                         "[refs of Handle: name-based matches, may include unrelated symbols with the same name]")
        self.assertFalse(tools.callers("Handle")["ok"])

    def test_mcp_tools_log_the_resolution(self) -> None:
        log = self.root / "calls.jsonl"
        server = RepoMcpServer(self.tools, str(log))
        out = server.call_tool("repo_callers", {"symbol": "Handle"})
        self.assertFalse(out["isError"])
        self.assertIn("main.go:10: main", out["content"][0]["text"])
        server.call_tool("repo_callees", {"symbol": "main"})
        server.call_tool("repo_refs", {"symbol": "Handle"})
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([(r["op"], r.get("resolved"), r["paths"]) for r in records], [
            ("callers", "types", []), ("callees", "types", []), ("refs", "types", ["a/a_test.go", "main.go", "web/use.ts"])])
        failed = server.call_tool("repo_callers", {"symbol": "bad symbol"})
        self.assertTrue(failed["isError"])
        self.assertNotIn("resolved", json.loads(log.read_text().splitlines()[-1]))


class MapTests(OpsCase):
    def test_go_files_rank_by_distinct_referencing_files(self) -> None:
        text = build_map(self.tools)
        lines = text.splitlines()
        self.assertIn(f"[Go files ranked by type-checked references ({GO_VERSION})]", lines)
        # Handle in a/a.go is referenced from main.go (a/a_test.go is a noise path); b's Handle by nothing.
        self.assertIn("  a.go  [5 lines] Handle", lines)
        self.assertEqual(self.tools.go_map_facts().find_defs("main")[0]["id"], 3)  # type: ignore[union-attr]

    def test_ranking_uses_types_not_names(self) -> None:
        data = sample_facts(self.sha)
        # main.go refers to the Handle of package a only: the Go declaration of b has no rank although the name is shared.
        facts = GoFacts.from_data(data)
        assert facts is not None
        self.assertEqual(facts.referencing_files(1), {"main.go", "a/a_test.go"})
        self.assertEqual(facts.referencing_files(2), set())

    def test_map_without_facts_notes_the_name_based_ranking(self) -> None:
        (self.cache / "go" / f"{self.sha}.json").unlink()
        tools = RepoTools(self.repo, self.sha, cache_dir=self.cache)
        assert tools.go_provider is not None
        with patch.object(tools.go_provider, "_disk_status", return_value={"status": "failed"}):
            text = build_map(tools)
        self.assertIn("[no type information for Go (failed): results are name-based]", text)

    def test_map_never_waits_for_an_extraction(self) -> None:
        tools = RepoTools(self.repo, self.sha, cache_dir=self.cache)
        assert tools.go_provider is not None
        provider = tools.go_provider
        provider._facts = None
        provider.state = "pending"
        started = time.monotonic()
        self.assertIsNone(tools.go_map_facts())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("still being prepared", build_map(tools))


class EngineEventsTests(EngineHarness, ExtractionCase):
    """The engine starts the extraction, holds the event until debate_started and records the snapshot."""

    PROJECT = "server-guardian"  # the project id run_debate uses, so the engine finds the stored facts

    def setUp(self) -> None:
        ExtractionCase.setUp(self)
        self.events_path = self.root / "events.jsonl"

    def tearDown(self) -> None:
        ExtractionCase.tearDown(self)

    def test_debate_uses_type_checked_callers_and_records_events(self) -> None:
        self.configure({"mode": "ok", "delay": 0.5, "facts": sample_facts()})
        runner = FakeRunner(self.plain_scripts([
            block({"op": "callers", "symbol": "Handle"}, {"op": "callees", "symbol": "main"}, {"op": "refs", "symbol": "Handle"}),
            "FINAL expert answer"]))
        result = self.run_debate(self.engine(), runner)

        first, second = runner.prompts("expert")
        self.assertIn("callers (Go only", first)
        self.assertIn('{"op": "callers", "symbol": "debateHub.followEvents"', first)
        self.assertIn("main.go:10: main  println(strings.ToUpper(a.Handle()))", second)
        self.assertIn("strings.ToUpper  (called at line 10)", second)
        events = self.events()
        types = [e["type"] for e in events]
        self.assertEqual(types.count("compiler_index"), 1)
        self.assertLess(types.index("debate_started"), types.index("compiler_index"))
        built = next(e for e in events if e["type"] == "compiler_index")
        self.assertEqual(built["thread_id"], result["thread_id"])
        self.assertEqual((built["data"]["status"], built["data"]["lang"], built["data"]["sha"]), ("built", "go", self.sha))
        served = next(e for e in events if e["type"] == "retrieval_served")
        self.assertEqual([(r["op"], r["resolved"]) for r in served["data"]["results"]],
                         [("callers", "types"), ("callees", "types"), ("refs", "types")])
        index = result["context"]["index"]
        self.assertEqual(index["go"], {"version": GOINDEX_VERSION, "go": GO_VERSION, "status": "built", "packages": 3})
        self.assertEqual(index["prompt_version"], "2026-11-11.p29")
        self.assert_clean()

    def test_cached_facts_are_used_by_the_first_prompt_map(self) -> None:
        self.run_to_end()
        self.emitted.clear()
        runner = FakeRunner(self.plain_scripts(["FINAL expert answer"]))
        result = self.run_debate(self.engine(), runner)
        first = runner.prompts("expert")[0]
        self.assertIn(f"[Go files ranked by type-checked references ({GO_VERSION})]", first)
        events = self.events()
        types = [e["type"] for e in events]
        self.assertEqual(types.index("debate_started"), 0)
        self.assertEqual(next(e for e in events if e["type"] == "compiler_index")["data"]["status"], "cached")
        self.assertEqual(result["context"]["index"]["go"]["status"], "cached")
        self.assertEqual(self.calls(), 1)

    def test_a_debate_that_ends_early_stops_the_extraction(self) -> None:
        self.configure({"mode": "sleep", "facts": sample_facts()})
        runner = FakeRunner(self.plain_scripts(["FINAL expert answer"]))
        result = self.run_debate(self.engine(), runner)
        self.assertIn(result["context"]["index"]["go"]["status"], ("pending",))
        probe = self.probe() if (self.bin / "probe.json").exists() else None
        if probe is not None:
            self.assert_process_gone(probe["pid"])
        self.assert_clean()

    def assert_process_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} is still running")


@unittest.skipIf(shutil.which("go") is None and not (Path.home() / ".local/go/bin/go").exists(),
                 "needs a Go toolchain")
class RealHelperTests(unittest.TestCase):
    """Builds tools/goindex with `go build`, indexes a temp git repository with it and asks the ops."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        go = shutil.which("go") or str(Path.home() / ".local/go/bin/go")
        cls.go = go
        cls.helper = cls.root / "bin" / "goindex"
        build = subprocess.run([go, "build", "-o", str(cls.helper), "."], cwd=TOOLS_DIR, capture_output=True, text=True,
                               env={**os.environ, "GOTOOLCHAIN": "local", "CGO_ENABLED": "0", "GOFLAGS": ""})
        if build.returncode != 0:
            raise AssertionError("go build failed: " + build.stderr)
        repo = cls.root / "ws"
        files = {
            "go.mod": "module example.com/m\n\ngo 1.22\n",
            "a/a.go": 'package a\n\n// Handle serves A.\nfunc Handle() string {\n\treturn "a"\n}\n\ntype Server struct{}\n\n'
                      'func (s *Server) Run() string {\n\treturn Handle()\n}\n',
            "b/b.go": 'package b\n\nfunc Handle() string {\n\treturn "b"\n}\n\nfunc Local() string {\n\treturn "local" + SecretHelper()\n}\n',
            "b/token_secret.go": 'package b\n\nfunc SecretHelper() string {\n\treturn "x"\n}\n',
            "main.go": 'package main\n\nimport (\n\t"strings"\n\n\t"example.com/m/a"\n\t_ "example.com/m/b"\n)\n\n'
                       'func main() {\n\ts := &a.Server{}\n\tprintln(strings.ToUpper(s.Run()))\n\tprintln(a.Handle())\n}\n',
            "main_test.go": 'package main\n\nimport "testing"\n\nfunc TestMain1(t *testing.T) {\n\tmain()\n}\n',
            "web/use.ts": 'export function Handle() {\n  return 1;\n}\n',
            ".env": "SECRET=1\n",
        }
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, *COMMIT, "commit", "-q", "-m", "one")
        cls.repo = repo
        cls.sha = git(repo, "rev-parse", "HEAD")
        cls.cache = cls.root / "data" / "code-index" / "proj"
        env = {"GUARDIAN_GOINDEX_BIN": str(cls.helper), "GUARDIAN_DATA_DIR": str(cls.root / "data"),
               "GUARDIAN_GOINDEX_TIMEOUT": "170"}
        cls._env = patch.dict(os.environ, env)
        cls._env.start()
        with patch.dict(go_facts._GO_VERSIONS, clear=True):
            cls.tools = RepoTools(repo, cls.sha, cache_dir=cls.cache)
            assert cls.tools.go_provider is not None
            cls.events: List[tuple] = []
            cls.tools.go_provider.start(lambda kind, data: cls.events.append((kind, data)))
            if cls.tools.go_provider._thread is not None:
                cls.tools.go_provider._thread.join(175)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env.stop()
        cls._tmp.cleanup()

    def test_extraction_built_and_left_nothing_behind(self) -> None:
        provider = self.tools.go_provider
        assert provider is not None
        self.assertEqual(provider.state, "built", provider.error)
        self.assertEqual(worktrees(self.repo), [f"worktree {self.repo.resolve()}"])
        self.assertEqual(leftover_temp_dirs(), [])
        data = json.loads((self.cache / "go" / f"{self.sha}.json").read_text())
        self.assertEqual(data["sha"], self.sha)
        self.assertEqual(data["version"], GOINDEX_VERSION)
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertNotIn("strings.ToUpper(", json.dumps(data))  # no source text

    def test_bait_same_name_in_two_packages_only_the_called_one_is_listed(self) -> None:
        refs = self.tools.refs("Handle")
        self.assertEqual(refs["resolved"], "types")
        lines = refs["text"].splitlines()
        self.assertFalse(any(line.startswith("b/b.go:") for line in lines), refs["text"])
        self.assertIn("a/a.go:11: \treturn Handle()", lines)
        self.assertIn("main.go:13: \tprintln(a.Handle())", lines)
        self.assertEqual(provided_paths(refs)[:2], ["a/a.go", "main.go"])
        callers = self.tools.callers("Handle")["text"].splitlines()
        self.assertEqual([l for l in callers if ":" in l and l[0] != "[" and not l.startswith(" ")][-2:], [
            "a/a.go:11: Server.Run  return Handle()", "main.go:13: main  println(a.Handle())"])
        self.assertFalse(any("b/b.go:" in l and "Handle" in l and l.count(":") == 2 for l in callers[2:]))
        defs = self.tools.defs("Handle")["text"]
        self.assertIn("a/a.go:4-6 function func Handle() string {  (package example.com/m/a)", defs)
        self.assertIn("b/b.go:3-5 function func Handle() string {  (package example.com/m/b)", defs)

    def test_methods_callees_and_tests(self) -> None:
        callers = self.tools.callers("Server.Run")["text"]
        self.assertIn("main.go:12: main  println(strings.ToUpper(s.Run()))", callers)
        callees = self.tools.callees("main")["text"]
        self.assertIn("a/a.go:10  Server.Run  (called at line 12)", callees)
        self.assertIn("a/a.go:4  Handle  (called at line 13)", callees)
        self.assertIn("strings.ToUpper  (called at line 12)", callees)
        self.assertIn("main_test.go:6: TestMain1  main()", self.tools.callers("main", include_tests=True)["text"])
        self.assertIn("(no calls found outside tests", self.tools.callers("main")["text"])

    def test_secret_named_go_file_is_compiled_but_not_reported(self) -> None:
        raw = (self.cache / "go" / f"{self.sha}.json").read_text()
        self.assertNotIn("SecretHelper", raw)
        self.assertNotIn("token_secret", raw)
        facts = self.tools.go_provider.get()  # type: ignore[union-attr]
        self.assertNotIn("b/token_secret.go", facts.files)  # type: ignore[union-attr]
        self.assertIn("b/b.go", facts.files)  # type: ignore[union-attr]
        # The call from b.go into the unreadable file is neither a reference nor an external callee.
        self.assertIn("(no calls found)", self.tools.callees("Local")["text"])
        self.assertFalse(self.tools.callers("SecretHelper")["text"].startswith("[callers"))

    def test_event_has_the_counts(self) -> None:
        kinds = [(k, d["status"]) for k, d in self.events if k == "compiler_index"]
        self.assertEqual(kinds, [("compiler_index", "built")])
        data = next(d for k, d in self.events if k == "compiler_index")
        self.assertGreaterEqual(data["packages"], 3)
        self.assertGreater(data["refs"], 5)


if __name__ == "__main__":
    unittest.main()

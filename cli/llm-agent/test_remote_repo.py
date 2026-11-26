"""Repository snapshots for debate agents on worker hosts (docs/design/remote-agents.md, phase W2).

Three layers, none of which starts ssh or a model CLI: the snapshot (build on the hub, import on the worker, read with
the same tools), the worker daemon (snapshot_has, `repo` runs, put-snapshot), and the router and engine (upload once,
calls come back and are checked, fallback keeps the spec). The last class holds the acceptance check: a seeded
defect task gives the same citation outcome whether the member reads on the hub or on a worker.
"""

import gc
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import worker
from debate_engine import McpTurn
from remote_agents import PROTOCOL, RemoteRouter
from repo_mcp import SNAPSHOT_TOOLS, RepoMcpServer, allowed_tool_names, tool_names
from repo_snapshot import (
    COMMIT_FILE, SnapshotError, build_snapshot, import_stream, is_key, prune_snapshots, quote_path, snapshot_commit,
)
from repo_tools import RepoTools
from team_manager import Lineup, Member
from test_remote_agents import FakeTransport, HubRunner, by_op, ok_ping, ok_run, workers
from test_repo_mcp_engine import McpRunner
from test_repo_tools import COMMIT, git, make_repo
from test_retrieval_loop import EngineHarness, FakeRunner
from testenv import isolate_data_dir, restore_data_dir

LIMITS: Dict[str, Optional[int]] = {"steps": 40, "step_bytes": 100_000, "turn_bytes": 1_000_000}


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def worker_calls(repo: Path, sha: str, ops: List[Tuple[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The records a worker's repo_mcp server would write for these calls (the real server on a scratch log)."""
    with TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "calls.jsonl")
        Path(log).touch()
        server = RepoMcpServer(RepoTools(repo, sha), log, max_steps=LIMITS["steps"], max_step_bytes=LIMITS["step_bytes"],
                               max_turn_bytes=LIMITS["turn_bytes"])
        for name, arguments in ops:
            server.call_tool(name, arguments)
        return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines()]


class SnapshotCase(unittest.TestCase):
    """A repository on the hub side and a worker's snapshot root."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(self.root)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.hub = RepoTools(self.repo, self.sha)
        self.out = self.root / "streams"
        self.out.mkdir(mode=0o700)
        self.store = self.root / "store"

    def imported(self, tools: Optional[RepoTools] = None) -> Tuple[Any, str, RepoTools]:
        snap = build_snapshot(tools or self.hub, self.out)
        with open(snap.path, "rb") as source:
            commit = import_stream(self.store, snap.key, source)
        return snap, commit, RepoTools(self.store / snap.key, commit, sha_label=self.sha)


class SnapshotTests(SnapshotCase):
    def test_same_files_same_key_and_same_commit(self) -> None:
        first, commit, _ = self.imported()
        again = build_snapshot(self.hub, self.out)
        self.assertEqual(first.key, again.key)
        self.assertTrue(is_key(first.key))
        other = TemporaryDirectory()
        self.addCleanup(other.cleanup)
        with open(first.path, "rb") as source:
            self.assertEqual(import_stream(Path(other.name), first.key, source), commit)

    def test_a_remote_reader_sees_what_the_hub_reader_sees(self) -> None:
        snap, _, remote = self.imported()
        kept = set(self.hub.readable_paths()) - set(snap.dropped)
        self.assertEqual(set(remote.readable_paths()), kept)
        for path in kept:
            self.assertEqual(remote.entries[path], self.hub.entries[path], path)
        for request in ({"op": "list", "path": "apps/agent", "depth": 1}, {"op": "read", "path": "apps/agent/notes.txt"},
                        {"op": "grep", "pattern": "TARGET"}, {"op": "outline", "path": "cli/tool.py"}):
            self.assertEqual(remote.run(request)["text"], self.hub.run(request)["text"], request)

    def test_nothing_a_hub_agent_may_not_see_is_in_the_snapshot(self) -> None:
        _, _, remote = self.imported()
        listing = subprocess.run(["git", "--git-dir", str(remote.workspace), "ls-tree", "-r", "--name-only", remote.sha],
                                 capture_output=True, text=True, check=True).stdout
        for hidden in (".env", "credentials.enc", "link.txt", "vendor"):
            self.assertNotIn(hidden, listing)
        self.assertNotIn("ENV_SECRET", remote.run({"op": "grep", "pattern": "ENV_SECRET"})["text"].replace("no matches", ""))

    def test_the_remote_map_names_the_hub_commit(self) -> None:
        _, commit, remote = self.imported()
        self.assertNotEqual(commit, self.sha)
        text = remote.run({"op": "map"})["text"]
        self.assertIn(f"sha {self.sha[:7]}", text)
        self.assertNotIn(commit[:7], text)

    def test_awkward_files_arrive_byte_for_byte(self) -> None:
        repo = self.root / "odd"
        repo.mkdir()
        files = {
            "crlf.txt": b"a\r\nb\r\n", "sp ace.txt": b"1\n", 'q"uote.txt': b"2\n", "back\\slash.txt": b"3\n",
            "한글.txt": "내용\n".encode(), "tab\there.txt": b"4\n", "new\nline.txt": b"5\n", "empty.txt": b"",
            ".gitattributes": b"*.txt text eol=crlf\n*.bin -text\n", "data.bin": bytes(range(256)),
        }
        for name, content in files.items():
            (repo / name).write_bytes(content)
        (repo / "run.sh").write_text("#!/bin/sh\n")
        os.chmod(repo / "run.sh", 0o755)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, *COMMIT, "commit", "-q", "-m", "odd")
        hub = RepoTools(repo, git(repo, "rev-parse", "HEAD"))
        snap, _, remote = self.imported(hub)
        kept = set(hub.readable_paths()) - set(snap.dropped)
        self.assertIn("crlf.txt", kept)
        self.assertEqual(set(remote.readable_paths()), kept)
        for path in kept:
            self.assertEqual(remote.entries[path], hub.entries[path], path)
        self.assertEqual(remote.entries["run.sh"][0], "100755")

    def test_quote_path(self) -> None:
        self.assertEqual(quote_path("a/b c.txt"), b"a/b c.txt")
        self.assertEqual(quote_path('q"x'), b'"q\\"x"')
        self.assertEqual(quote_path("a\nb"), b'"a\\nb"')
        self.assertEqual(quote_path("\x01"), b'"\\001"')
        self.assertEqual(quote_path('"lead'), b'"\\"lead"')
        self.assertEqual(quote_path("한글"), "한글".encode())

    def test_oversize_files_are_dropped_and_listed(self) -> None:
        snap = build_snapshot(self.hub, self.out, max_blob=1000)
        self.assertIn("apps/agent/big.txt", snap.dropped)
        smaller = build_snapshot(self.hub, self.out, max_total=1500)
        self.assertGreater(len(smaller.dropped), 0)
        self.assertNotEqual(snap.key, build_snapshot(self.hub, self.out).key)

    def test_importing_again_keeps_the_stored_snapshot(self) -> None:
        snap, commit, _ = self.imported()
        marker = self.store / snap.key / "marker"
        marker.write_text("x")
        with open(snap.path, "rb") as source:
            self.assertEqual(import_stream(self.store, snap.key, source), commit)
        self.assertTrue(marker.exists())
        self.assertEqual(snapshot_commit(self.store, snap.key), commit)


class HostileUploadTests(SnapshotCase):
    KEY = hashlib.sha256(b"k").hexdigest()

    def refuse(self, data: bytes, **kwargs: Any) -> str:
        with tempfile.TemporaryFile() as source:
            source.write(data)
            source.seek(0)
            with self.assertRaises(SnapshotError) as caught:
                import_stream(self.store, self.KEY, source, **kwargs)
        self.assertEqual([n for n in os.listdir(self.store) if n != "."], [], "an upload that failed left something")
        return str(caught.exception)

    def test_a_key_that_is_not_a_digest_is_refused(self) -> None:
        for bad in ("../x", "z" * 64, "a" * 63, ""):
            with self.assertRaises(SnapshotError):
                import_stream(self.store, bad, __import__("io").BytesIO(b""))

    def test_bytes_that_are_not_zlib(self) -> None:
        self.assertIn("unreadable", self.refuse(b"this is not compressed"))

    def test_a_truncated_stream(self) -> None:
        snap = build_snapshot(self.hub, self.out)
        data = snap.path.read_bytes()
        self.assertIn("incomplete", self.refuse(data[:len(data) // 2]))

    def test_a_decompression_bomb_stops_at_the_limit(self) -> None:
        self.assertIn("larger than", self.refuse(zlib.compress(b"\0" * 5_000_000), limit=100_000))

    def test_a_stream_git_rejects(self) -> None:
        self.assertIn("fast-import", self.refuse(zlib.compress(b"this is not a fast-import stream\n")))

    def test_a_path_that_leaves_the_tree_is_rejected(self) -> None:
        stream = (b"commit refs/heads/snapshot\ncommitter G <g@l> 0 +0000\ndata 1\nx\n"
                  b"M 100644 inline ../escape.txt\ndata 1\nx\n\ndone\n")
        self.refuse(zlib.compress(stream))
        self.assertFalse((self.root / "escape.txt").exists())

    def test_a_git_directory_or_a_dot_component_is_rejected(self) -> None:
        # checked after the import, so the result does not depend on what the worker's git version checks itself
        for name in (b".git/config", b"a/.GIT/hooks/x", b"a/./b"):
            with self.subTest(name=name):
                stream = (b"commit refs/heads/snapshot\ncommitter G <g@l> 0 +0000\ndata 1\nx\n"
                          b"M 100644 inline " + name + b"\ndata 1\nx\n\ndone\n")
                message = self.refuse(zlib.compress(stream))
                self.assertTrue("unsafe path" in message or "fast-import" in message, message)

    def test_unsafe_path_rules(self) -> None:
        from repo_snapshot import unsafe_path
        for good in ("a.py", "dir/sub/file.go", ".github/workflows/ci.yml", "x..y", ".gitignore", "back\\slash.txt"):
            self.assertIsNone(unsafe_path(good), good)
        for bad in ("../x", "a/../b", "./a", "a//b", "/abs", "a/", ".git", "a/.Git/x"):
            self.assertIsNotNone(unsafe_path(bad), bad)

    def test_a_stream_without_the_snapshot_ref(self) -> None:
        stream = (b"commit refs/heads/other\ncommitter G <g@l> 0 +0000\ndata 1\nx\nM 100644 inline a\ndata 1\nx\n\ndone\n")
        self.refuse(zlib.compress(stream))


class PruneTests(unittest.TestCase):
    def make(self, root: Path, name: str, age: float, now: float) -> None:
        path = root / name
        path.mkdir()
        marker = path / COMMIT_FILE
        marker.write_text("a" * 40 + "\n")
        os.utime(marker, (now - age, now - age))

    def test_ttl_then_least_recently_used(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 1_000_000.0
            keys = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(5)]
            for key, age in zip(keys, (10, 20, 30, 40, 100_000)):
                self.make(root, key, age, now)
            (root / ".incoming-old").mkdir()
            os.utime(root / ".incoming-old", (now - 7200, now - 7200))
            (root / ".incoming-new").mkdir()
            os.utime(root / ".incoming-new", (now - 5, now - 5))
            (root / "unrelated").mkdir()
            removed = prune_snapshots(root, ttl_seconds=3600, keep=2, now=now)
            self.assertEqual(sorted(removed), sorted([keys[4], keys[3], keys[2]]))
            self.assertEqual(sorted(n for n in os.listdir(root)), sorted([keys[0], keys[1], ".incoming-new", "unrelated"]))

    def test_touching_a_snapshot_protects_it(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = hashlib.sha256(b"x").hexdigest()
            self.make(root, key, 100_000, 1_000_000.0)
            self.assertIsNotNone(snapshot_commit(root, key, touch=True))
            self.assertEqual(prune_snapshots(root, ttl_seconds=3600, keep=8), [])

    def test_a_pinned_snapshot_is_never_removed_and_never_counts_against_keep(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 1_000_000.0
            pinned = hashlib.sha256(b"pinned").hexdigest()
            fresh = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(3)]
            self.make(root, pinned, 100_000, now)  # long past the ttl
            for index, key in enumerate(fresh):
                self.make(root, key, 10 + index, now)
            removed = prune_snapshots(root, ttl_seconds=3600, keep=1, now=now, pinned=frozenset({pinned}))
            # the pinned snapshot stays despite its age and does not take the one keep slot
            self.assertNotIn(pinned, removed)
            self.assertTrue((root / pinned / COMMIT_FILE).exists())
            self.assertEqual(sorted(removed), sorted(fresh[1:]))  # only the freshest unpinned survives
            self.assertEqual(prune_snapshots(root, ttl_seconds=3600, keep=8, now=now,
                                             pinned=frozenset({pinned})), [])
            # the pin gone: the ttl removes the aged snapshot as before
            self.assertEqual(prune_snapshots(root, ttl_seconds=3600, keep=8, now=now), [pinned])
            self.assertFalse((root / pinned).exists())


class ToolListTests(unittest.TestCase):
    def test_no_index_serves_only_the_five_snapshot_tools(self) -> None:
        self.assertEqual(sorted(SNAPSHOT_TOOLS), sorted(["repo_list", "repo_grep", "repo_read", "repo_outline", "repo_map"]))
        self.assertEqual(tool_names(no_index=True), [n for n in tool_names() if n in SNAPSHOT_TOOLS])
        self.assertTrue(set(tool_names()) > set(tool_names(no_index=True)))
        self.assertEqual(allowed_tool_names(no_index=True), [f"mcp__repo__{n}" for n in tool_names(no_index=True)])


class DaemonCase(unittest.TestCase):
    """The real daemon on a unix socket; the job command is a script that reports the spec it was given."""

    JOB = ("import json,sys\n"
           "p=json.loads(sys.stdin.read())\n"
           "spec=p.get('repo_mcp')\n"
           "if spec:\n"
           "    rec={'op':'read','target':'a','ok':True,'bytes':3,'truncated':False,'error':None,'paths':['a'],"
           "'spans':{},'limit':None,'mentions':[]}\n"
           "    open(spec['log'],'a').write(json.dumps(rec)+'\\n')\n"
           "print(json.dumps({'provider':'claude-cli','text':json.dumps(spec),'session_id':'s1','resumed':False,"
           "'failures':[]}))\n")

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="gw", dir="/tmp")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.dir]))
        self.sock = Path(self.dir) / "w.sock"
        patcher = patch.dict(os.environ, {"GUARDIAN_WORKER_SOCKET": str(self.sock), "GUARDIAN_WORKER_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        script = Path(self.dir) / "job.py"
        script.write_text(self.JOB)
        os.environ["GUARDIAN_WORKER_EXEC_CMD"] = json.dumps([sys.executable, str(script)])
        daemon = worker.Worker(max_jobs=2, queue_seconds=5.0)
        server = worker.make_server(self.sock, daemon)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop() -> None:
            daemon.shutdown()
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.snap = build_snapshot(RepoTools(self.repo, self.sha), Path(self.tmp.name))

    def ask(self, request: Dict[str, Any]) -> Dict[str, Any]:
        proc = subprocess.run([sys.executable, str(Path(worker.__file__)), "client"], input=json.dumps(request),
                              capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PYTHONPATH": str(Path(worker.__file__).parent)})
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def store(self) -> str:
        with open(self.snap.path, "rb") as source:
            return import_stream(worker.snapshot_root(), self.snap.key, source)

    def run_request(self, **repo: Any) -> Dict[str, Any]:
        field = {"key": self.snap.key, "sha_label": self.sha, "limits": LIMITS, "seed_steps": 0, "seed_bytes": 0, **repo}
        return {"op": "run", "protocol": PROTOCOL, "job": "0123456789abcdef", "prompt": "hi", "repo": field}


class WorkerDaemonRepoTests(DaemonCase):
    def test_snapshot_has_follows_the_store(self) -> None:
        ask = {"op": "snapshot_has", "protocol": PROTOCOL, "key": self.snap.key}
        self.assertEqual(self.ask(ask), {"ok": True, "present": False})
        self.store()
        self.assertEqual(self.ask(ask), {"ok": True, "present": True})
        self.assertEqual(self.ask({**ask, "key": "../x"}), {"ok": True, "present": False})  # nothing outside the store

    def test_ping_reports_whether_git_is_installed(self) -> None:
        self.assertIs(self.ask({"op": "ping", "protocol": PROTOCOL})["git"], True)

    def test_a_repo_run_gets_its_own_spec_and_returns_the_calls(self) -> None:
        commit = self.store()
        reply = self.ask(self.run_request(seed_steps=3, seed_bytes=500))
        self.assertTrue(reply["ok"], reply)
        spec = json.loads(reply["result"]["text"])
        self.assertEqual(spec["sha"], commit)
        self.assertEqual(spec["workspace"], str(worker.snapshot_root() / self.snap.key))
        self.assertTrue(spec["no_index"])
        self.assertEqual((spec["sha_label"], spec["seed_steps"], spec["seed_bytes"]), (self.sha, 3, 500))
        self.assertEqual(spec["limits"], LIMITS)
        self.assertEqual([c["op"] for c in reply["calls"]], ["read"])
        self.assertEqual(os.listdir(worker.jobs_root()), [], "the job directory outlived the job")

    def test_the_hub_cannot_hand_over_a_spec_of_its_own(self) -> None:
        self.store()
        request = {"op": "run", "protocol": PROTOCOL, "job": "0123456789abcdef", "prompt": "hi",
                   "repo_mcp": {"workspace": "/etc", "sha": "x", "log": "/tmp/x"}}
        reply = self.ask(request)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["text"], "null")
        self.assertNotIn("calls", reply)

    def test_a_missing_snapshot_is_refused_before_a_job_starts(self) -> None:
        reply = self.ask(self.run_request())
        self.assertFalse(reply["ok"])
        self.assertIn("snapshot missing", reply["error"])

    def test_malformed_repo_fields_are_refused(self) -> None:
        self.store()
        for bad in ({"key": "../x"}, {"key": 5}, {"sha_label": "a b"}, {"sha_label": "x" * 65}, {"sha_label": ""},
                    {"limits": {"steps": -1}}, {"limits": {"unknown": 1}}, {"limits": {"steps": True}},
                    {"limits": "all"}, {"seed_steps": -1}, {"seed_bytes": "7"}):
            reply = self.ask(self.run_request(**bad))
            self.assertFalse(reply["ok"], bad)
        self.assertTrue(self.ask(self.run_request(limits={"steps": None}))["ok"])

    def test_a_job_that_writes_too_many_calls_is_an_error(self) -> None:
        self.store()
        with patch.object(worker, "MAX_CALLS_BYTES", 10):
            reply = self.ask(self.run_request())
        self.assertFalse(reply["ok"])
        self.assertIn("tool call log", reply["error"])


class PutSnapshotModeTests(DaemonCase):
    def put(self, key: str, data: bytes) -> Dict[str, Any]:
        proc = subprocess.run([sys.executable, str(Path(worker.__file__)), "put-snapshot", key], input=data,
                              capture_output=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": str(Path(worker.__file__).parent)})
        return json.loads(proc.stdout.decode().strip().splitlines()[-1])

    def test_put_snapshot_stores_and_the_daemon_then_has_it(self) -> None:
        reply = self.put(self.snap.key, self.snap.path.read_bytes())
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(snapshot_commit(worker.snapshot_root(), self.snap.key), reply["commit"])
        self.assertTrue(self.ask({"op": "snapshot_has", "protocol": PROTOCOL, "key": self.snap.key})["present"])

    def test_put_snapshot_refuses_bad_input_without_traceback(self) -> None:
        self.assertFalse(self.put("../x", b"")["ok"])
        bad = self.put(self.snap.key, b"garbage")
        self.assertFalse(bad["ok"])
        self.assertIsNone(snapshot_commit(worker.snapshot_root(), self.snap.key))


class RouterRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(Path(self._tmp.name))
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.turns: List[McpTurn] = []

    def spec(self) -> Dict[str, Any]:
        turn = McpTurn(RepoTools(self.repo, self.sha), LIMITS)
        self.turns.append(turn)
        self.addCleanup(turn.close)
        return turn.spec

    def reply(self, calls: Any, text: str = "remote answer") -> Dict[str, Any]:
        return {**ok_run(text), "calls": calls}

    def router(self, **ops: Any) -> Tuple[RemoteRouter, HubRunner, FakeTransport]:
        script = {"ping": ok_ping(), "snapshot_has": {"ok": True, "present": False}, "run": self.reply([])}
        script.update(ops)
        hub, transport = HubRunner(), FakeTransport({"node-1": by_op(**script)})
        return RemoteRouter(hub, transport), hub, transport

    @staticmethod
    def sent(transport: FakeTransport, op: str) -> List[Dict[str, Any]]:
        return [r for _host, r, _t in transport.requests if r["op"] == op]

    def test_the_first_call_uploads_and_the_second_does_not(self) -> None:
        router, hub, transport = self.router()
        with workers("node-1"):
            first = router.run("node-1", "a", None, repo_mcp=self.spec())
            second = router.run("node-1", "b", None, repo_mcp=self.spec())
        self.assertEqual((first.host, second.host, hub.calls), ("node-1", "node-1", []))
        self.assertEqual(len(self.sent(transport, "put-snapshot")), 1)
        self.assertEqual(len(self.sent(transport, "snapshot_has")), 1)  # memoised for the host
        keys = {r["repo"]["key"] for r in self.sent(transport, "run")}
        self.assertEqual(len(keys), 1)
        self.assertEqual(len(router._snapshots), 1)

    def test_a_worker_that_has_the_snapshot_gets_no_upload(self) -> None:
        router, _, transport = self.router(snapshot_has={"ok": True, "present": True})
        with workers("node-1"):
            router.run("node-1", "a", None, repo_mcp=self.spec())
        self.assertEqual(self.sent(transport, "put-snapshot"), [])

    def test_the_request_never_carries_a_path_or_the_spec(self) -> None:
        spec = self.spec()
        router, _, transport = self.router()
        with workers("node-1"):
            router.run("node-1", "a", None, repo_mcp=spec)
        wire = json.dumps(transport.requests, default=str)
        for secret in (str(self.repo), spec["log"], spec["workspace"]):
            self.assertNotIn(secret, wire)
        self.assertTrue(all("repo_mcp" not in r for _h, r, _t in transport.requests))
        field = self.sent(transport, "run")[0]["repo"]
        self.assertEqual(set(field), {"key", "sha_label", "limits", "seed_steps", "seed_bytes"})
        self.assertEqual((field["sha_label"], field["limits"]), (self.sha, LIMITS))

    def test_the_worker_continues_the_turns_limits(self) -> None:
        spec = self.spec()
        Path(spec["log"]).write_text(json.dumps({"op": "read", "bytes": 700}) + "\n" + json.dumps({"op": "grep", "bytes": 50}) + "\n")
        router, _, transport = self.router()
        with workers("node-1"):
            router.run("node-1", "a", None, repo_mcp=spec)
        field = self.sent(transport, "run")[0]["repo"]
        self.assertEqual((field["seed_steps"], field["seed_bytes"]), (2, 750))

    def test_returned_calls_join_the_hub_log_without_paths_outside_the_snapshot(self) -> None:
        spec = self.spec()
        calls = worker_calls(self.repo, self.sha, [("repo_read", {"path": "apps/agent/notes.txt"})])
        forged = dict(calls[0], paths=["apps/agent/notes.txt", "apps/agent/.env", "nowhere/else.py"],
                      spans={"apps/agent/notes.txt": [[1, 3]], "apps/agent/.env": [[1, 9]]})
        router, _, _ = self.router(run=self.reply([forged]))
        with workers("node-1"):
            router.run("node-1", "a", None, repo_mcp=spec)
        records = [json.loads(line) for line in Path(spec["log"]).read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["paths"], ["apps/agent/notes.txt"])
        self.assertEqual(list(records[0]["spans"]), ["apps/agent/notes.txt"])
        self.assertEqual(records[0]["op"], "read")

    def test_a_reply_without_the_call_log_is_an_error_and_the_hub_answers_with_the_spec(self) -> None:
        spec = self.spec()
        router, hub, _ = self.router(run=ok_run())  # no "calls"
        with workers("node-1"):
            result = router.run("node-1", "a", None, repo_mcp=spec)
        self.assertEqual((result.host, result.text), ("", "hub answer"))
        self.assertIs(hub.calls[0]["repo_mcp"], spec)
        self.assertIn("tool call log", result.failures[0])

    def test_a_snapshot_that_cannot_be_built_does_not_mark_the_worker_down(self) -> None:
        spec = dict(self.spec(), workspace=str(Path(self._tmp.name) / "missing"))
        router, hub, transport = self.router()
        with workers("node-1"):
            result = router.run("node-1", "a", None, repo_mcp=spec)
            self.assertEqual(result.host, "")
            self.assertIn("cannot", result.failures[0])
            self.assertIsNone(router._down_reason("node-1"))
            again = router.run("node-1", "b", None, repo_mcp=self.spec())
        self.assertEqual(again.host, "node-1")
        self.assertIs(hub.calls[0]["repo_mcp"], spec)

    def test_a_worker_without_git_is_not_used_for_repo_calls(self) -> None:
        router, hub, transport = self.router(ping=dict(ok_ping(), git=False))
        with workers("node-1"):
            result = router.run("node-1", "a", None, repo_mcp=self.spec())
        self.assertEqual(result.host, "")
        self.assertIn("git", result.failures[0])
        self.assertEqual(self.sent(transport, "run"), [])

    def test_a_worker_without_the_git_field_is_not_refused(self) -> None:
        ping = {k: v for k, v in ok_ping().items() if k != "git"}
        router, _, _ = self.router(ping=ping)
        with workers("node-1"):
            self.assertEqual(router.run("node-1", "a", None, repo_mcp=self.spec()).host, "node-1")

    def test_a_refused_upload_falls_back(self) -> None:
        router, hub, transport = self.router()
        with workers("node-1"), patch.object(FakeTransport, "upload", return_value={"ok": False, "error": "disk full"}):
            result = router.run("node-1", "a", None, repo_mcp=self.spec())
        self.assertEqual(result.host, "")
        self.assertIn("disk full", result.failures[0])

    def test_a_call_without_a_spec_sends_no_repo_field(self) -> None:
        router, _, transport = self.router()
        with workers("node-1"):
            router.run("node-1", "a", None)
        self.assertNotIn("repo", self.sent(transport, "run")[0])
        self.assertEqual(self.sent(transport, "snapshot_has"), [])

    def test_the_snapshot_directory_goes_with_the_router(self) -> None:
        router, _, _ = self.router()
        with workers("node-1"):
            router.run("node-1", "a", None, repo_mcp=self.spec())
        directory = router._snapshot_dir
        assert directory is not None
        self.assertTrue(directory.exists())
        self.assertEqual(oct(directory.stat().st_mode & 0o777), "0o700")
        del router
        gc.collect()
        self.assertFalse(directory.exists())


class SeededDefectTests(EngineHarness):
    """Acceptance: the same task gives the same citation outcome on the hub and on a worker."""

    def setUp(self) -> None:
        super().setUp()
        self._env = patch.dict(os.environ, {"GUARDIAN_REPO_MCP": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def lineup(self, host: Any) -> Lineup:
        return Lineup(id="t", name="T", expert=Member("domain_architect", ["claude"], "mcp", host),
                      critics=[Member("critic_redteam", ["claude"], None)], judge=Member("jev_mcp_decision", ["claude"], None))

    def debate(self, runner: FakeRunner, host: Any, engine: Any) -> Dict[str, Any]:
        with patch.object(engine, "_resolve_lineup", return_value=self.lineup(host)), workers("node-1"):
            return self.run_debate(engine, runner)

    def local(self, ops: List[Tuple[str, Dict[str, Any]]], final: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        runner = McpRunner(self.plain_scripts([final]), {"expert": ops})
        result = self.debate(runner, None, self.engine())
        return result, self.events()

    def remote(self, ops: List[Tuple[str, Dict[str, Any]]], final: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        calls = worker_calls(self.repo, self.sha, ops)

        def run(_request: Dict[str, Any]) -> Dict[str, Any]:
            return {**ok_run(final), "calls": calls}

        transport = FakeTransport({"node-1": by_op(ping=ok_ping(), snapshot_has={"ok": True, "present": True}, run=run)})
        engine = self.engine()
        engine.remote = RemoteRouter(engine.llm, transport)
        runner = FakeRunner(self.plain_scripts(["unused"]))
        result = self.debate(runner, "node-1", engine)
        sent = [r for _h, r, _t in transport.requests if r["op"] == "run"]
        return result, self.events(), sent

    @staticmethod
    def checks(events: List[Dict[str, Any]]) -> Dict[str, Any]:
        data = [e["data"] for e in events if e["type"] == "citation_check"][0]
        return {k: data[k] for k in ("cited", "unverified", "asked", "mentioned", "short")}

    READ = [("repo_list", {"path": "apps/agent"}), ("repo_read", {"path": "apps/agent/notes.txt"})]

    def test_a_cited_and_read_file_passes_in_both_places(self) -> None:
        final = "FINAL the defect is at apps/agent/notes.txt:2"
        _, local_events = self.local(self.READ, final)
        _, remote_events, sent = self.remote(self.READ, final)
        self.assertEqual(self.checks(local_events)[ "unverified"], [])
        self.assertEqual(self.checks(remote_events), self.checks(local_events))
        self.assertEqual(len(sent), 1)
        self.assertIn("key", sent[0]["repo"])

    def test_citing_a_file_nobody_read_fails_in_both_places(self) -> None:
        final = "FINAL the defect is at cli/tool.py:5"
        _, local_events = self.local(self.READ, final)
        _, remote_events, _ = self.remote(self.READ, final)
        self.assertEqual(self.checks(local_events)["unverified"], ["cli/tool.py"])
        self.assertEqual(self.checks(remote_events), self.checks(local_events))

    def test_remote_tool_calls_become_the_same_steps(self) -> None:
        result, events, _ = self.remote(self.READ, "FINAL see apps/agent/notes.txt:2")
        served = [e for e in events if e["type"] == "retrieval_served" and e.get("role") == "Domain Expert"]
        self.assertEqual([e["data"]["step"] for e in served], [1, 2])
        self.assertTrue(all(e["data"]["via"] == "mcp" for e in served))
        entry = next(t for t in result["transcript"] if t["role"] == "Domain Expert")
        self.assertEqual(entry["retrievals"][1]["paths"], ["apps/agent/notes.txt"])
        self.assertIn("apps/agent/notes.txt", result["context"]["provided_files"]["Domain Expert"])
        self.assertEqual(result["context"]["retrieval"]["steps"], 2)

    def test_a_forged_path_does_not_back_a_citation(self) -> None:
        # A file over the snapshot's size cap is in the commit but not on the worker. The worker claims it showed it.
        (self.repo / "huge.txt").write_text("line\n" * 600_000)  # 3 MB
        git(self.repo, "add", "huge.txt")
        git(self.repo, *COMMIT, "commit", "-q", "-m", "huge")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.assertIn("huge.txt", RepoTools(self.repo, self.sha).readable_paths())
        calls = worker_calls(self.repo, self.sha, [("repo_read", {"path": "apps/agent/notes.txt"})])
        calls[0]["paths"] = ["apps/agent/notes.txt", "huge.txt"]
        calls[0]["spans"] = {"huge.txt": [[1, 50]]}
        transport = FakeTransport({"node-1": by_op(
            ping=ok_ping(), snapshot_has={"ok": True, "present": True},
            run=lambda _r: {**ok_run("FINAL bug at huge.txt:3"), "calls": calls})})
        engine = self.engine()
        engine.remote = RemoteRouter(engine.llm, transport)
        self.debate(FakeRunner(self.plain_scripts(["unused"])), "node-1", engine)
        self.assertEqual(self.checks(self.events())["unverified"], ["huge.txt"])

    def test_the_remote_prompt_lists_five_tools_and_no_symbol_index(self) -> None:
        _, _, sent = self.remote(self.READ, "FINAL x")
        wire = json.dumps(sent[0])
        self.assertIn("repo_list, repo_grep, repo_read, repo_outline and repo_map are available", wire)
        self.assertIn("There are no symbol, reference or call-graph tools", wire)
        self.assertNotIn("repo_defs", wire)


if __name__ == "__main__":
    unittest.main()

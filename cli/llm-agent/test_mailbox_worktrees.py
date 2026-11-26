"""The mailbox MCP's worktree layer (docs/design/resident-open-tools.md, O3): --worktrees mode.

The daemon side is the FakeDaemon of test_mailbox_mcp (one line in, one line out); its
task_worktree reply points at a real git bundle built exactly as the node builds it: a bare
repository imported from a W2 snapshot stream (refs/heads/snapshot, no HEAD), bundled with
`git bundle create`. The MCP then clones, collects a diff and removes the worktree with real git
in temp dirs. No network, no ssh, no provider CLI; the neutral names node-1 and claude@node-1.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from mailbox_mcp import TOOLS, MailboxMcpServer, allowed_tool_names, tool_specs
from repo_snapshot import build_snapshot, import_stream
from repo_tools import RepoTools
from test_mailbox_mcp import FakeDaemon
from test_repo_tools import git, make_repo


def run_git(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd is not None else None,
                          capture_output=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})


class WorktreeCase(unittest.TestCase):
    """A fake daemon, a real snapshot bundle and a --worktrees server with its own home."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="sgw", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.daemon = FakeDaemon(self.dir / "e1.sock")
        self.addCleanup(self.daemon.close)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.server = MailboxMcpServer(str(self.daemon.path), str(self.dir / "calls.jsonl"),
                                       timeout=15.0, worktrees=True, home=str(self.home))
        # a real stored snapshot and the bundle the daemon would write for it
        self.repo = make_repo(self.dir / "ws")
        sha = git(self.repo, "rev-parse", "HEAD")
        streams = self.dir / "streams"
        streams.mkdir(mode=0o700)
        snap = build_snapshot(RepoTools(self.repo, sha), streams)
        store = self.dir / "snapshots"
        with open(snap.path, "rb") as source:
            self.commit = import_stream(store, snap.key, source)
        self.bundle = self.dir / "k-1.bundle"
        done = run_git(["--git-dir", str(store / snap.key), "bundle", "create", str(self.bundle),
                        "refs/heads/snapshot"])
        self.assertEqual(done.returncode, 0, done.stderr.decode()[-300:])
        self.work = self.home / "work" / "tasks" / "k-1"

    def call(self, name: str, **arguments: Any) -> Dict[str, Any]:
        response = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
        assert response is not None
        return response["result"]

    def text(self, result: Dict[str, Any]) -> str:
        return result["content"][0]["text"]

    def payload(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return json.loads(self.text(result))

    def sent(self, tool: str) -> Dict[str, Any]:
        """The last request the daemon recorded for one tool."""
        for request in reversed(self.daemon.requests):
            if request["tool"] == tool:
                return request
        raise AssertionError(f"no {tool} request was sent")

    def claim_worktree(self) -> Dict[str, Any]:
        """One task_worktree call answered by the daemon with the real bundle."""
        self.daemon.reply = {"ok": True, "bundle": str(self.bundle), "commit": self.commit}
        result = self.call("task_worktree", task="k-1", generation=2)
        self.assertFalse(result["isError"], self.text(result))
        return self.payload(result)


class WorktreeToolTests(WorktreeCase):
    def test_task_worktree_clones_the_bundle_and_records_the_base(self) -> None:
        answer = self.claim_worktree()
        self.assertEqual(answer, {"path": str(self.work), "base": self.commit})
        self.assertEqual((self.work / "apps/agent/notes.txt").read_text(encoding="utf-8")
                         .splitlines()[0], "alpha")
        self.assertEqual((self.home / "work/tasks/.k-1.base").read_text(encoding="ascii").strip(),
                         self.commit)
        request = self.sent("task_worktree")
        self.assertEqual(request["args"], {"task": "k-1", "generation": 2})
        self.assertNotIn("op_id", request)  # answered on the node, like mail_inbox

    def test_a_second_call_answers_the_recorded_base_and_never_clones_over_work(self) -> None:
        self.claim_worktree()
        (self.work / "apps/agent/notes.txt").write_text("edited by the agent\n", encoding="utf-8")
        again = self.claim_worktree()
        self.assertEqual(again, {"path": str(self.work), "base": self.commit})
        self.assertIn("edited by the agent",
                      (self.work / "apps/agent/notes.txt").read_text(encoding="utf-8"))

    def test_a_daemon_refusal_comes_back_with_its_error(self) -> None:
        self.daemon.reply = {"ok": False, "error": "not_owner"}
        result = self.call("task_worktree", task="k-9", generation=1)
        self.assertTrue(result["isError"])
        self.assertEqual(self.text(result), "error: not_owner")

    def test_bad_input_never_touches_the_socket(self) -> None:
        for arguments in ({"task": "../x", "generation": 1}, {"task": "k-1", "generation": "two"},
                          {"task": "", "generation": 1}, {"generation": 1}):
            result = self.call("task_worktree", **arguments)
            self.assertTrue(result["isError"], arguments)
        self.assertEqual(self.daemon.requests, [])


class PatchTests(WorktreeCase):
    def update(self, **extra: Any) -> Dict[str, Any]:
        self.daemon.reply = {"ok": True}
        return self.call("task_update", task="k-1", generation=2, status="completed",
                         result="done", **extra)

    def test_a_completed_update_collects_the_diff_of_an_edited_and_an_added_file(self) -> None:
        self.claim_worktree()
        (self.work / "apps/agent/notes.txt").write_text("alpha changed\nbeta TARGET gamma\ndelta\n",
                                                        encoding="utf-8")
        (self.work / "apps/agent/new_file.txt").write_text("a whole new file\n", encoding="utf-8")
        result = self.update()
        self.assertFalse(result["isError"], self.text(result))
        args = self.sent("task_update")["args"]
        self.assertEqual(args["patch_base"], self.commit)
        self.assertIn("diff --git a/apps/agent/notes.txt b/apps/agent/notes.txt", args["patch"])
        self.assertIn("alpha changed", args["patch"])
        self.assertIn("diff --git a/apps/agent/new_file.txt b/apps/agent/new_file.txt",
                      args["patch"])
        self.assertIn("a whole new file", args["patch"])
        self.assertFalse(self.work.exists())                          # the worktree went
        self.assertFalse((self.home / "work/tasks/.k-1.base").exists())

    def test_a_caller_sent_patch_is_always_discarded(self) -> None:
        self.claim_worktree()
        (self.work / "apps/agent/new_file.txt").write_text("real change\n", encoding="utf-8")
        result = self.update(patch="FAKE PATCH", patch_base="0" * 40)
        self.assertFalse(result["isError"], self.text(result))
        args = self.sent("task_update")["args"]
        self.assertIn("real change", args["patch"])
        self.assertNotIn("FAKE PATCH", args["patch"])
        self.assertEqual(args["patch_base"], self.commit)

    def test_an_empty_diff_omits_both_keys(self) -> None:
        self.claim_worktree()  # a fresh clone, nothing edited
        result = self.update()
        self.assertFalse(result["isError"], self.text(result))
        args = self.sent("task_update")["args"]
        self.assertNotIn("patch", args)
        self.assertNotIn("patch_base", args)

    def test_an_oversized_patch_is_refused_before_the_socket(self) -> None:
        self.claim_worktree()
        (self.work / "big.txt").write_text("".join("x" * 60 + "\n" for _ in range(5000)),
                                           encoding="utf-8")  # ~305 KiB of diff, over the cap
        before = len(self.daemon.requests)
        result = self.update()
        self.assertTrue(result["isError"])
        self.assertEqual(self.text(result), "error: patch_too_large")
        self.assertEqual(len(self.daemon.requests), before)  # nothing was sent
        self.assertTrue(self.work.exists())                  # and the worktree stays to fix

    def test_a_failed_update_attaches_nothing_and_still_removes_the_worktree(self) -> None:
        self.claim_worktree()
        (self.work / "apps/agent/new_file.txt").write_text("a change\n", encoding="utf-8")
        self.daemon.reply = {"ok": True}
        result = self.call("task_update", task="k-1", generation=2, status="failed",
                           reason="stuck")
        self.assertFalse(result["isError"], self.text(result))
        args = self.sent("task_update")["args"]
        self.assertNotIn("patch", args)       # status failed attaches nothing
        self.assertNotIn("patch_base", args)
        self.assertFalse(self.work.exists())  # the task ended either way: the worktree went

    def test_an_accepted_task_release_removes_the_worktree_too(self) -> None:
        self.claim_worktree()
        self.daemon.reply = {"ok": True}
        result = self.call("task_release", task="k-1", generation=2, reason="cannot continue")
        self.assertFalse(result["isError"], self.text(result))
        self.assertFalse(self.work.exists())
        self.assertFalse((self.home / "work/tasks/.k-1.base").exists())

    def test_a_refused_update_keeps_the_worktree(self) -> None:
        self.claim_worktree()
        (self.work / "apps/agent/new_file.txt").write_text("a change\n", encoding="utf-8")
        self.daemon.reply = {"ok": False, "error": "bad_patch"}
        result = self.call("task_update", task="k-1", generation=2, status="completed",
                           result="done")
        self.assertTrue(result["isError"])
        self.assertEqual(self.text(result), "error: bad_patch")
        self.assertTrue(self.work.exists())  # the task stays open: fix and call again

    def test_an_update_without_a_worktree_attaches_nothing(self) -> None:
        result = self.update()  # no task_worktree call happened: no worktree exists
        self.assertFalse(result["isError"], self.text(result))
        args = self.sent("task_update")["args"]
        self.assertNotIn("patch", args)


class WorktreeModeTests(WorktreeCase):
    def test_the_closed_server_has_no_task_worktree_and_the_same_list_as_ever(self) -> None:
        closed = MailboxMcpServer(str(self.daemon.path), timeout=5.0)
        response = closed.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "task_worktree", "arguments": {"task": "k-1",
                                                                                    "generation": 1}}})
        assert response is not None
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(list(TOOLS), list(tool_specs()))
        self.assertNotIn("task_worktree", TOOLS)
        self.assertEqual(allowed_tool_names()[-1], "mcp__mailbox__task_release")

    def test_the_worktrees_server_lists_task_worktree_last(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert response is not None
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(names, list(TOOLS) + ["task_worktree", "artifact_publish",
                                               "ops_propose_package", "ops_propose_update"])
        self.assertEqual(allowed_tool_names(worktrees=True),
                         [f"mcp__mailbox__{name}" for name in names])
        tw = next(tool for tool in response["result"]["tools"] if tool["name"] == "task_worktree")
        self.assertEqual(tw["inputSchema"]["required"], ["task", "generation"])

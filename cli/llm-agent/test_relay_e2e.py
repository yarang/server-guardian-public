"""The two halves of the M2 relay against each other: the hub's mail_relay.RelaySession and the
node daemon's resident_node.ResidentManager over one socket.socketpair(). No ssh, no model: the
test plays the driver (bind, admit, the agent's tool calls on its socket, turn end) and checks
that an operator task claimed by one agent, a question to a second agent and the answer back
end with the right rows on the hub and the right files on the node."""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mail_relay
import mail_snapshots
import mail_store
import repo_snapshot
import resident_open
import resident_registry
import sandbox_info
import testenv
import worker
from db_manager import DecisionDBManager
from mailbox_mcp import MailboxMcpServer
from project_registry import Project, ProjectManager
from resident_node import ResidentManager
from test_repo_tools import git, make_repo

NODE = "node-1"
FIRST = f"claude@{NODE}"
SECOND = f"agy@{NODE}"


def wait_until(predicate, timeout=5.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class NodeTransport:
    """The ssh worker path of SnapshotEnsurer, short-circuited into this node's worker directory:
    snapshot_has looks at worker.snapshot_root(), upload imports the stream there, as put_snapshot does."""

    def __init__(self):
        self.uploads = []

    def call(self, host, request, timeout):
        if request.get("op") != "snapshot_has":
            return {"ok": False, "error": "unexpected op"}
        present = repo_snapshot.snapshot_commit(worker.snapshot_root(), request["key"]) is not None
        return {"ok": True, "present": present}

    def upload(self, host, key, stream, timeout):
        self.uploads.append(key)
        with open(stream, "rb") as source:
            repo_snapshot.import_stream(worker.snapshot_root(), key, source)
        return {"ok": True}


class RelayCase(unittest.TestCase):
    """The two halves over one socketpair; the subclasses hold the scenarios."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.dir = Path(tempfile.mkdtemp(prefix="sge", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = patch.dict(os.environ, {"GUARDIAN_WORKER_DIR": str(self.dir),
                                      "GUARDIAN_WORKER_SOCKET": str(self.dir / "w.sock")})
        env.start()
        self.addCleanup(env.stop)
        # the operator's --resident-add: the declaration file and the store rows together
        entries = self.declared_entries()
        path = resident_registry.declaration_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "agents": entries}), encoding="utf-8")
        for entry in entries:
            mail_store.MailStore().upsert_agent({**entry, "address": f"{entry['name']}@{NODE}"})

        hub_end, node_end = socket.socketpair()
        self.sockets = (hub_end, node_end)
        self.before_start()
        self.mgr = ResidentManager(worker.Worker(max_jobs=2, queue_seconds=1.0), root=self.dir / "agents",
                                   **self.manager_options())
        self.node_thread = threading.Thread(
            target=self.mgr.attach, args=(node_end.makefile("rb"), node_end.makefile("wb")), daemon=True)
        self.session = mail_relay.RelaySession(NODE, hub_end.makefile("rb"), hub_end.makefile("wb"),
                                               store=mail_store.MailStore())
        self.session.POLL_INTERVAL = 0.05
        self.stop = threading.Event()
        self.hub_thread = threading.Thread(target=self.session.run, args=(self.stop,), daemon=True)
        self.hub_thread.start()
        self.node_thread.start()

    def declared_entries(self):
        return [{"name": "claude", "node": NODE, "provider": "claude", "driver": "headless"},
                {"name": "agy", "node": NODE, "provider": "agy", "driver": "headless"}]

    def before_start(self):
        """A hook run after the declaration and before either half starts."""

    def manager_options(self):
        return {}

    def tearDown(self):
        self.stop.set()
        self.mgr.shutdown()
        for end in self.sockets:
            try:
                end.shutdown(socket.SHUT_RDWR)  # wakes both reader threads
            except OSError:
                pass
        self.hub_thread.join(5.0)
        self.node_thread.join(5.0)
        for end in self.sockets:
            end.close()
        self.assertFalse(self.hub_thread.is_alive(), "the hub session did not stop")

    # -- the driver's part ------------------------------------------------------------------------

    def bind(self, name):
        wait_until(lambda: (self.dir / "agents" / name / "state.json").is_file(),
                   what=f"the agents frame to create {name}")
        bound = self.mgr.begin_incarnation(name)
        self.assertTrue(bound["ok"], bound)
        done = self.mgr.complete_bind(name, bound["epoch"], session_id=f"s-{name}", pid=os.getpid())
        self.assertTrue(done["ok"], done)
        return bound

    def call(self, socket_path, tool, **args):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(10.0)
        try:
            conn.connect(str(socket_path))
            conn.sendall((json.dumps({"tool": tool, "args": args}) + "\n").encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        finally:
            conn.close()
        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))

    def turn(self, name):
        admitted = self.mgr.admit(name, "wake")
        self.assertTrue(admitted["ok"], admitted)
        return admitted["turn"]

    def end(self, name, turn):
        reply = self.mgr.end_turn(name, turn, "normal", session_id=f"s-{name}")
        self.assertTrue(reply.get("ok"), reply)

    def spool(self, name):
        return sorted(p.stem for p in (self.dir / "agents" / name / "spool").glob("*.json"))

    def states(self):
        return {m["id"]: m["state"] for m in mail_store.MailStore().list_mail(limit=200)}


class RelayEndToEnd(RelayCase):
    # -- the scenario -----------------------------------------------------------------------------

    def test_task_claimed_question_asked_answer_returned(self):
        first = self.bind("claude")
        second = self.bind("agy")
        rows = {row["address"]: row for row in mail_store.MailStore().agent_rows()}
        self.assertEqual((rows[FIRST]["state"], rows[SECOND]["state"]), ("ready", "ready"))

        created = mail_store.MailStore().task_create("operator", "check the disk report", "detail",
                                                     op_id="op-task", assignee=FIRST)
        self.assertTrue(created.get("ok"), created)
        task_id = created["id"]
        sent = mail_store.MailStore().send("operator", FIRST, "message", "start", "take the task",
                                           op_id="op-1", task=task_id)
        self.assertTrue(sent.get("ok"), sent)
        wait_until(lambda: sent["id"] in self.spool("claude"), what="the operator mail on the node")

        # claude's turn: read, claim, ask agy
        turn = self.turn("claude")
        inbox = self.call(first["socket"], "mail_inbox")
        self.assertTrue(inbox["ok"], inbox)
        self.assertIn(sent["id"], [m["id"] for m in inbox["messages"]])
        claimed = self.call(first["socket"], "task_claim", task=task_id)
        self.assertTrue(claimed.get("ok"), claimed)
        asked = self.call(first["socket"], "mail_send", to=SECOND, kind="question",
                          subject="disk", body="what is free on /var?", task=task_id)
        self.assertTrue(asked.get("ok"), asked)
        question = asked["id"]
        wait_until(lambda: question in self.spool("agy"), what="the question in agy's spool")
        self.end("claude", turn)
        wait_until(lambda: self.states().get(sent["id"]) == "acked", what="the operator mail acked")
        self.assertEqual(self.spool("claude"), [])

        # agy's turn: answer
        turn = self.turn("agy")
        inbox = self.call(second["socket"], "mail_inbox")
        self.assertEqual([m["id"] for m in inbox["messages"]], [question])
        self.assertEqual(inbox["messages"][0]["from"], FIRST)
        # a task assigned to claude is not agy's to see, but a reply may carry the task of the
        # mail it answers (the question carried it); a new mail with that task is still refused
        untagged = self.call(second["socket"], "mail_send", to=FIRST, kind="message", subject="disk",
                             body="no reply_to", task=task_id)
        self.assertEqual(untagged.get("error"), "invisible_task", untagged)
        answered = self.call(second["socket"], "mail_send", to=FIRST, kind="answer", subject="disk",
                             body="12 GB free", reply_to=question, task=task_id)
        self.assertTrue(answered.get("ok"), answered)
        answer = answered["id"]
        wait_until(lambda: answer in self.spool("claude"), what="the answer in claude's spool")
        self.end("agy", turn)
        wait_until(lambda: self.states().get(question) == "acked", what="the question acked")

        # claude's second turn: read the answer, finish the task
        turn = self.turn("claude")
        inbox = self.call(first["socket"], "mail_inbox")
        self.assertEqual([(m["id"], m["body"]) for m in inbox["messages"]], [(answer, "12 GB free")])
        listed = self.call(first["socket"], "task_list")
        task = next(t for t in listed["result"] if t["id"] == task_id)
        done = self.call(first["socket"], "task_update", task=task_id, generation=task["generation"],
                         status="completed", result="12 GB free on /var")
        self.assertTrue(done.get("ok"), done)
        self.end("claude", turn)
        wait_until(lambda: self.states().get(answer) == "acked", what="the answer acked")

        row = next(t for t in mail_store.MailStore().task_list("operator") if t["id"] == task_id)
        self.assertEqual((row["status"], row["result"]), ("completed", "12 GB free on /var"))
        self.assertEqual((self.spool("claude"), self.spool("agy")), ([], []))


    def test_project_task_snapshot_reaches_the_node_and_the_call_log_the_hub(self):
        repo = make_repo(self.dir / "repo")
        sha = git(repo, "rev-parse", "HEAD")
        loader = patch.object(ProjectManager, "load", return_value=Project(id="projx", workspace_raw=str(repo)))
        loader.start()
        self.addCleanup(loader.stop)
        transport = NodeTransport()
        self.session._ensurer = mail_snapshots.SnapshotEnsurer(NODE, transport=transport)
        first = self.bind("claude")

        created = mail_store.MailStore().task_create("operator", "read the tool", "detail", op_id="op-p",
                                                     assignee=FIRST, project="projx", sha=sha)
        self.assertTrue(created.get("ok"), created)
        task_id = created["id"]

        turn = self.turn("claude")
        claimed = self.call(first["socket"], "task_claim", task=task_id)
        self.assertTrue(claimed.get("ok"), claimed)
        table = lambda: self.mgr._task_table("claude").entry(task_id) or {}
        wait_until(lambda: table().get("state") == "ready", timeout=20.0, what="the snapshot ready on the node")
        self.assertEqual(len(transport.uploads), 1)
        self.assertEqual(table()["key"], transport.uploads[0])
        # the owned task pins its snapshot against the W2 prune
        self.assertIn(transport.uploads[0], worker.pinned_snapshot_keys())

        read = self.call(first["socket"], "repo_read", task=task_id, path="cli/tool.py")
        self.assertTrue(read.get("ok"), read)
        self.assertIn("RUN_BODY", json.dumps(read["result"]))
        secret = self.call(first["socket"], "repo_read", task=task_id, path="server-guardian-credentials.enc")
        self.assertNotIn("ENC_SECRET", json.dumps(secret))
        unowned = self.call(first["socket"], "repo_read", task="k-0000", path="cli/tool.py")
        self.assertEqual(unowned.get("error"), "no_task", unowned)

        listed = self.call(first["socket"], "task_list")
        task = next(t for t in listed["result"] if t["id"] == task_id)
        done = self.call(first["socket"], "task_update", task=task_id, generation=task["generation"],
                         status="completed", result="read it")
        self.assertTrue(done.get("ok"), done)
        self.end("claude", turn)

        calls = mail_store.MailStore().task_calls("operator", task_id)
        self.assertTrue(calls.get("ok"), calls)
        records = calls["calls"]["records"]
        self.assertEqual([(r["op"], r["target"], r["ok"]) for r in records],
                         [("read", "cli/tool.py", True), ("read", "server-guardian-credentials.enc", False)])
        self.assertFalse(calls["calls"]["dropped"])
        self.assertIsNone(self.mgr._task_table("claude").entry(task_id))  # the accepted end dropped it


class OpenAgentPatchEndToEnd(RelayCase):
    """O3 (docs/design/resident-open-tools.md rule 5): an agent declared tools "open" works in a
    git worktree of its task's snapshot; the sandboxed mailbox MCP collects the diff on
    task_update and the hub turns it into ONE manual-approval jev_mcp:apply_patch action. The
    sandbox itself is a temp layout (no root, no sudo); the MCP clones the bundle the daemon writes
    into the agent's sandbox socket directory, as it does inside the sandbox."""

    def declared_entries(self):
        return [{"name": "claude", "node": NODE, "provider": "claude", "driver": "headless",
                 "tools": "open"}]

    def before_start(self):
        version = sandbox_info.sandbox_version()  # this checkout's sandbox files
        # the probe verdict worker_probe.record-sandbox would write into the inventory
        verdict = patch.object(resident_registry, "inventory_sandbox",
                               return_value={"passed": True, "version": version})
        verdict.start()
        self.addCleanup(verdict.stop)
        sbox = self.dir / "sbx"
        sbox.mkdir()
        stand_ins = []
        for name in ("run", "launch.py"):
            path = sbox / name
            path.write_text("#!/bin/sh\n# a stand-in for the root-installed file\n", encoding="utf-8")
            os.chmod(path, 0o755)
            stand_ins.append(path)
        installed = sbox / "installed.json"
        installed.write_text(json.dumps({"version": version, "agents": ["claude"],
                                         "cli": resident_open.expected_cli_versions()}),
                             encoding="utf-8")
        sock_root = sbox / "sock"
        (sock_root / "claude").mkdir(parents=True, mode=0o2750)
        os.chmod(sock_root / "claude", 0o2750)
        os.chown(sock_root / "claude", -1, os.getgid())  # BSD mkdir takes the parent's group
        self.paths = resident_open.SandboxPaths(
            wrapper=stand_ins[0], launcher=stand_ins[1], installed=installed, sock_root=sock_root,
            home_root=sbox / "home", root_uid=os.getuid(), daemon_uid=os.getuid,
            agent_gid=lambda name: os.getgid(), member_gids=lambda: [os.getgid()])

    def manager_options(self):
        return {"sandbox_paths": self.paths}

    def mcp(self, server, tool, **arguments):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": tool, "arguments": arguments}})
        result = response["result"]
        text = result["content"][0]["text"]
        return result["isError"], (text if result["isError"] else json.loads(text))

    def test_open_agent_worktree_patch_becomes_one_proposed_action(self):
        repo = make_repo(self.dir / "repo")
        sha = git(repo, "rev-parse", "HEAD")
        status_before = git(repo, "status", "--porcelain")  # make_repo's gitlink has no checkout
        loader = patch.object(ProjectManager, "load", return_value=Project(id="projx", workspace_raw=str(repo)))
        loader.start()
        self.addCleanup(loader.stop)
        self.session._ensurer = mail_snapshots.SnapshotEnsurer(NODE, transport=NodeTransport())
        first = self.bind("claude")
        self.assertTrue(str(first["socket"]).startswith(str(self.paths.sock_dir("claude"))), first)

        created = mail_store.MailStore().task_create("operator", "rename the run body", "detail",
                                                     op_id="op-o3", assignee=FIRST, project="projx", sha=sha)
        self.assertTrue(created.get("ok"), created)
        task_id = created["id"]
        home = self.dir / "agent-home"
        home.mkdir()
        server = MailboxMcpServer(str(first["socket"]), timeout=20.0, worktrees=True, home=str(home))

        turn = self.turn("claude")
        failed, claimed = self.mcp(server, "task_claim", task=task_id)
        self.assertFalse(failed, claimed)
        table = lambda: self.mgr._task_table("claude").entry(task_id) or {}
        wait_until(lambda: table().get("state") == "ready", timeout=20.0, what="the snapshot ready on the node")
        generation = table()["generation"]
        failed, tree = self.mcp(server, "task_worktree", task=task_id, generation=generation)
        self.assertFalse(failed, tree)
        bundle = self.paths.sock_dir("claude") / "tasks" / f"{task_id}.bundle"
        self.assertTrue(bundle.is_file())
        work = Path(tree["path"])
        self.assertEqual(work, home / "work" / "tasks" / task_id)
        tool = work / "cli" / "tool.py"
        tool.write_text(tool.read_text(encoding="utf-8").replace("RUN_BODY", "RUN_DONE"), encoding="utf-8")
        (work / "cli" / "notes.md").write_text("# notes\nadded by the agent\n", encoding="utf-8")

        # a caller-sent patch is discarded; the MCP sends the worktree's own diff
        failed, done = self.mcp(server, "task_update", task=task_id, generation=generation,
                                status="completed", result="renamed RUN_BODY\nsecond line",
                                patch="forged")
        self.assertFalse(failed, done)
        action_id = done.get("action_id")
        self.assertIsInstance(action_id, int, done)
        self.end("claude", turn)

        self.assertFalse(work.exists())  # the accepted update removed the worktree
        wait_until(lambda: not bundle.exists(), what="the bundle removed with the task")
        thread_id = f"thread_task_{task_id.replace('-', '_')}"
        actions = DecisionDBManager().list_actions(thread_id)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual((action["id"], action["tool"], action["status"]),
                         (action_id, "jev_mcp:apply_patch", "PROPOSED"))
        edits = action["arguments"]["edits"] if isinstance(action["arguments"], dict) \
            else json.loads(action["arguments"])["edits"]
        self.assertEqual(sorted(e["file_path"] for e in edits), ["cli/notes.md", "cli/tool.py"])
        self.assertIn("RUN_DONE", json.dumps(edits))
        self.assertNotIn("forged", json.dumps(edits))
        row = next(t for t in mail_store.MailStore().task_list("operator") if t["id"] == task_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(mail_store.MailStore().task_patch_action(task_id), action_id)
        # the workspace itself was never touched
        self.assertEqual(git(repo, "status", "--porcelain"), status_before)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), sha)
        self.assertIn("RUN_BODY", (repo / "cli" / "tool.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

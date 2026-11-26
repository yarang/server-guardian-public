"""Tests for Phase 2 (resident artifact export, PD4) and Phase 3 (ops:system_update and
resident ops_propose_* proposals, PD3-a) of docs/design/resident-codex-and-policy.md."""

import base64
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mail_artifacts
import mail_relay
import mail_store
import ops_actions
import resident_monitor
import resident_node
import testenv
from db_manager import DecisionDBManager
from mailbox_mcp import MailboxMcpServer


class ArtifactStoreAndMonitorTests(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.store = mail_store.MailStore()

    def test_publish_list_get_pin_and_mask_secrets(self):
        secret_text = "report ok\nAuthorization: Bearer sk-ant-api03-secretvalue1234567890\n"
        b64 = base64.b64encode(secret_text.encode("utf-8")).decode("ascii")
        res = self.store.publish_artifact(
            "codex@node-1", "report.md", b64, "Audit report",
            rel_path="out/report.md", task="k-1", summary="summary line", op_id="op-1",
        )
        self.assertTrue(res["ok"], res)
        aid = res["id"]
        self.assertRegex(aid, mail_artifacts.ARTIFACT_ID_RE)
        self.assertEqual(res["filename"], "report.md")

        # Idempotent replay on same op_id returns same id and creates no duplicate
        replay = self.store.publish_artifact(
            "codex@node-1", "report.md", b64, "Audit report", op_id="op-1",
        )
        self.assertEqual(replay["id"], aid)
        items = self.store.list_artifacts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], aid)

        got = self.store.get_artifact(aid)
        self.assertIsNotNone(got)
        self.assertFalse(got["missing"])
        self.assertNotIn("sk-ant-api03-secretvalue1234567890", got["text"])

        # Pinning
        pin = self.store.pin_artifact(aid, pinned=True)
        self.assertTrue(pin["ok"])
        self.assertTrue(self.store.list_artifacts()[0]["pinned"])

        # Read-only monitor snapshot and artifact_body
        snap = resident_monitor.snapshot()
        self.assertEqual(len(snap["artifacts"]), 1)
        self.assertEqual(snap["artifacts"][0]["id"], aid)
        body = resident_monitor.artifact_body(aid)
        self.assertIsNotNone(body)
        self.assertEqual(body["filename"], "report.md")
        self.assertNotIn("sk-ant-api03-secretvalue1234567890", body["text"])

    def test_rejects_bad_filename_empty_and_oversize(self):
        b64 = base64.b64encode(b"hi").decode("ascii")
        self.assertEqual(self.store.publish_artifact("a@node-1", "../escape", b64, "t")["reason"], "bad_filename")
        self.assertEqual(self.store.publish_artifact("a@node-1", "x.txt", "", "t")["reason"], "bad_content")
        big = base64.b64encode(b"x" * (mail_artifacts.MAX_ARTIFACT_BYTES + 1)).decode("ascii")
        self.assertEqual(self.store.publish_artifact("a@node-1", "big.bin", big, "t")["reason"], "artifact_too_large")


class MailboxArtifactMcpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="artmcp-", dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        (self.home / "out").mkdir(parents=True)
        (self.home / "work").mkdir(parents=True)
        self.sock_path = self.root / "agent.sock"
        self.requests = []
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX)
        self._srv.bind(str(self.sock_path))
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.addCleanup(self._close)

    def _close(self):
        self._stop.set()
        self._thread.join(2.0)
        self._srv.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0])
                self.requests.append(req)
                conn.sendall((json.dumps({"ok": True, "id": "a-1234567890ab"}) + "\n").encode("utf-8"))
            finally:
                conn.close()

    def test_artifact_publish_reads_out_file_and_blocks_symlinks_and_escapes(self):
        server = MailboxMcpServer(str(self.sock_path), worktrees=True, home=str(self.home))
        (self.home / "out" / "notes.md").write_text("# deliverable\n", encoding="utf-8")
        ok = server.call_tool("artifact_publish", {"path": "notes.md", "title": "My Notes"})
        self.assertFalse(ok["isError"], ok)
        self.assertEqual(len(self.requests), 1)
        req = self.requests[0]
        self.assertEqual(req["tool"], "artifact_publish")
        self.assertEqual(req["args"]["filename"], "notes.md")
        self.assertEqual(req["args"]["rel_path"], "out/notes.md")
        self.assertEqual(base64.b64decode(req["args"]["content_b64"]), b"# deliverable\n")
        self.assertIn("op_id", req)

        # Path outside out/work is refused before touching the socket
        bad_escape = server.call_tool("artifact_publish", {"path": "/etc/passwd", "title": "bad"})
        self.assertTrue(bad_escape["isError"])
        # Symlink inside out/ is refused
        os.symlink("/etc/hosts", str(self.home / "out" / "link.txt"))
        bad_link = server.call_tool("artifact_publish", {"path": "link.txt", "title": "sym"})
        self.assertTrue(bad_link["isError"])
        self.assertIn("symlink", bad_link["content"][0]["text"])
        self.assertEqual(len(self.requests), 1)

    def test_closed_mailbox_does_not_offer_open_tools(self):
        closed = MailboxMcpServer(str(self.sock_path), worktrees=False, home=str(self.home))
        for name in ("artifact_publish", "ops_propose_package", "ops_propose_update"):
            res = closed.call_tool(name, {})
            self.assertTrue(res["isError"])
            self.assertIn("unknown tool", res["content"][0]["text"])


class OpsSystemUpdateAndProposalTests(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)

    def test_ops_system_update_validate_risk_measure_and_run_commands(self):
        self.assertIn("ops:system_update", ops_actions.ACTIONS)
        self.assertEqual(ops_actions.risk("ops:system_update", {"scope": "index"}), "low")
        self.assertEqual(ops_actions.risk("ops:system_update", {"scope": "security"}), "medium")
        self.assertEqual(ops_actions.risk("ops:system_update", {"scope": "upgrade"}), "high")

        self.assertEqual(ops_actions.validate("ops:system_update", {"target_host": "node-1", "scope": "Security"})["scope"],
                         "security")
        with self.assertRaises(ValueError):
            ops_actions.validate("ops:system_update", {"target_host": "node-1", "scope": "dist-upgrade"})

        sample = (
            "Listing... Done\n"
            "curl/noble-updates,noble-security 8.5.0-2ubuntu10.6 arm64 [upgradable from: 8.5.0-2ubuntu10.5]\n"
            "tzdata/noble-updates 2026a-0ubuntu0.24.04 all [upgradable from: 2025b-0ubuntu0.24.04]\n"
        )
        _, all_items = ops_actions.parse_measure("ops:system_update", {"scope": "upgrade"}, sample)
        self.assertEqual(all_items, ["curl=8.5.0-2ubuntu10.6", "tzdata=2026a-0ubuntu0.24.04"])
        _, sec_items = ops_actions.parse_measure("ops:system_update", {"scope": "security"}, sample)
        self.assertEqual(sec_items, ["curl=8.5.0-2ubuntu10.6"])

        self.assertIn("apt-get update", ops_actions.run_command("ops:system_update", {"scope": "index"}, {}))
        sec_cmd = ops_actions.run_command("ops:system_update", {"scope": "security"}, {"items": sec_items})
        self.assertIn("--only-upgrade --no-remove -- curl", sec_cmd)
        upg_cmd = ops_actions.run_command("ops:system_update", {"scope": "upgrade"}, {})
        self.assertIn("apt-get upgrade -y --no-remove", upg_cmd)

    def test_relay_propose_tools_stamp_node_enforce_open_and_record_proposed_action(self):
        store = mail_store.MailStore()
        entries = [
            {"name": "codex", "node": "node-1", "provider": "codex", "driver": "headless",
             "tools": "open", "address": "codex@node-1"},
            {"name": "claude", "node": "node-1", "provider": "claude", "driver": "headless",
             "address": "claude@node-1"},
        ]
        session = mail_relay.RelaySession("node-1", None, None, store=store, declarations=lambda: entries)
        with patch.object(session, "_node_entries", return_value=entries):
            # Closed agent refused
            closed = session._tool_ops_propose_update(
                "claude@node-1", {"scope": "index", "reason": "refresh"}, {"op_id": "op-c"},
            )
            self.assertFalse(closed["ok"])
            self.assertEqual(closed["reason"], "not_open")

            # Open agent proposes update -> stamps target_host=node-1 and creates PROPOSED action
            upd = session._tool_ops_propose_update(
                "codex@node-1", {"scope": "security", "reason": "apply curl CVE fix"}, {"op_id": "op-u"},
            )
            self.assertTrue(upd["ok"], upd)
            self.assertEqual(upd["tool"], "ops:system_update")
            self.assertEqual(upd["target_host"], "node-1")
            self.assertEqual(upd["status"], "PROPOSED")

            # Open agent proposes package install
            pkg = session._tool_ops_propose_package(
                "codex@node-1",
                {"manager": "apt", "packages": [{"name": "ripgrep"}], "reason": "fast code search", "task": "k-7"},
                {"op_id": "op-p"},
            )
            self.assertTrue(pkg["ok"], pkg)
            self.assertEqual(pkg["tool"], "ops:package_install")

            db = DecisionDBManager()
            upd_act = db.get_action(upd["action_id"])
            self.assertEqual(upd_act["status"], "PROPOSED")
            self.assertEqual(upd_act["project_id"], "fleet")
            self.assertEqual(upd_act["arguments"]["target_host"], "node-1")
            pkg_act = db.get_action(pkg["action_id"])
            self.assertEqual(pkg_act["arguments"]["target_host"], "node-1")
            self.assertEqual(pkg_act["arguments"]["packages"][0]["name"], "ripgrep")

            # Also verify artifact_publish via relay
            art = session._tool_artifact_publish(
                "codex@node-1",
                {"filename": "out.md", "content_b64": base64.b64encode(b"hello").decode("ascii"),
                 "title": "Output"},
                {"op_id": "op-a"},
            )
            self.assertTrue(art["ok"], art)
            self.assertEqual(len(store.list_artifacts("codex@node-1")), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""guardian.py wiring of the operator session commands (docs/design/operator-sessions.md, S1).

Every test points GUARDIAN_SESSION_KEYS_FILE into the isolated data dir: nothing here may touch
the real ~/.ssh/authorized_keys2 or authorized_keys."""

import base64
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import testenv  # noqa: E402


def _ed25519_key(seed: int) -> str:
    blob = (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def _args(**values):
    base = dict(json=False, session_add=None, session_remove=None, session_address=None,
                session_list=False, session_link=False, workstation=None, address=None, key=None,
                node=None)
    base.update(values)
    return SimpleNamespace(**base)


class OperatorSessionCommandTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(testenv.isolate_data_dir())
        self.keys = self.data / "authorized_keys2"
        self.env = patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE": str(self.keys)})
        self.env.start()
        self.servers = patch("group_manager.load_servers", return_value=([], None, None))
        self.servers.start()

    def tearDown(self):
        self.servers.stop()
        self.env.stop()
        testenv.restore_data_dir()

    def run_cli(self, **values):
        import guardian

        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                handled = guardian.handle_operator_session_commands(_args(**values))
            except SystemExit as exc:
                handled, code = True, exc.code
        return handled, code, out.getvalue(), err.getvalue()

    def test_no_session_flag_is_not_handled(self):
        handled, code, out, err = self.run_cli()
        self.assertFalse(handled)
        self.assertEqual((code, out, err), (0, "", ""))

    def test_add_writes_the_keys_file_and_list_shows_it(self):
        handled, code, _, _ = self.run_cli(session_add="ws-mac-1", address="100.64.1.5",
                                           key=_ed25519_key(1) + " comment")
        self.assertTrue(handled)
        self.assertEqual(code, 0)
        self.assertIn('from="100.64.1.5"', self.keys.read_text())
        with patch("session_registry.sshd_reads_keys2", return_value={"ok": True, "detail": "x"}):
            _, code, out, _ = self.run_cli(session_list=True, json=True)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([w["id"] for w in payload["workstations"]], ["ws-mac-1"])
        self.assertTrue(payload["workstations"][0]["keys_line"])
        self.assertEqual(payload["sessions"], [])

    def test_address_change_and_remove(self):
        self.run_cli(session_add="ws-mac-1", address="100.64.1.5", key=_ed25519_key(2))
        _, code, _, _ = self.run_cli(session_address="ws-mac-1", address="100.64.1.9")
        self.assertEqual(code, 0)
        self.assertIn('from="100.64.1.9"', self.keys.read_text())
        _, code, _, _ = self.run_cli(session_remove="ws-mac-1")
        self.assertEqual(code, 0)
        self.assertNotIn("ws-mac-1", self.keys.read_text().split("\n", 2)[2])

    def test_missing_options_and_refusals_exit_2(self):
        _, code, _, err = self.run_cli(session_add="ws-mac-1", address="100.64.1.5")
        self.assertEqual(code, 2)
        self.assertIn("--key", err)
        _, code, out, _ = self.run_cli(session_add="operator", address="100.64.1.5",
                                       key=_ed25519_key(3), json=True)
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])
        self.assertFalse(self.keys.exists())

    def test_session_link_exits_with_the_entry_code(self):
        import guardian

        fake = SimpleNamespace(entry=lambda workstation: 7 if workstation == "ws-mac-1" else 0)
        with patch.dict(sys.modules, {"session_link": fake}):
            with self.assertRaises(SystemExit) as caught:
                guardian.handle_operator_session_commands(_args(session_link=True, workstation="ws-mac-1"))
        self.assertEqual(caught.exception.code, 7)


if __name__ == "__main__":
    unittest.main()

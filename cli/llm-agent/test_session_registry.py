"""session_registry: workstation declarations, the keys renderer and the sshd check (S1b).

The whole module must leave the operator's real ~/.ssh alone: every test points
GUARDIAN_SESSION_KEYS_FILE into the isolated data dir, and the shared base class records the real
authorized_keys2/authorized_keys signatures in setUpClass and asserts them unchanged in
tearDownClass. No test ever calls the registry without the env var set.
"""

import base64
import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

import session_registry
import testenv
from session_registry import RegistrationError

# Public keys built here (the wire format of an ed25519 key), so no fixture carries a real key.
KEY = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32).decode("ascii")
OTHER_KEY = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"j" * 32).decode("ascii")

SERVERS = [{"id": "node-1", "name": "node-1"}, {"id": "site-a-arm1", "name": "hub"}]


def raw_key(type_name=b"ssh-ed25519", key_bytes=b"k" * 32):
    """A key line from hand-built blob parts (wrong types and lengths included)."""
    blob = (len(type_name).to_bytes(4, "big") + type_name
            + len(key_bytes).to_bytes(4, "big") + key_bytes)
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def entry(ws_id="ws-mac-1", address="100.64.0.5", key=KEY, node=None,
          added="2026-11-20T10:00:00Z"):
    return {"id": ws_id, "address": address, "key": key, "node": node, "added": added}


def _signature(path):
    """Existence and (when it exists) mtime and size, enough to prove nothing touched the file."""
    try:
        info = path.stat()
    except OSError:
        return (path.name, None)
    return (path.name, info.st_mtime_ns, info.st_size)


class SessionRegistryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_files = [Path.home() / ".ssh" / "authorized_keys2",
                          Path.home() / ".ssh" / "authorized_keys"]
        cls.real_before = [_signature(path) for path in cls.real_files]

    @classmethod
    def tearDownClass(cls):
        for path, before in zip(cls.real_files, cls.real_before):
            after = _signature(path)
            if after != before:  # assertEqual needs an instance; raise directly instead
                raise AssertionError(f"the test class touched {path}: {before} -> {after}")

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.keys_file = Path(os.environ["GUARDIAN_DATA_DIR"]) / "authorized_keys2"
        env = patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE": str(self.keys_file)})
        env.start()
        self.addCleanup(env.stop)

    def add(self, ws_id="ws-mac-1", address="100.64.0.5", key=KEY, **kwargs):
        kwargs.setdefault("servers", SERVERS)
        return session_registry.add_workstation(ws_id, address, key, **kwargs)

    def declarations(self):
        return json.loads(session_registry.declaration_path().read_text(encoding="utf-8"))

    def keys_text(self):
        return self.keys_file.read_text(encoding="utf-8")


class AddWorkstation(SessionRegistryTestCase):
    def test_stores_key_without_comment(self):
        stored = self.add(key=f"{KEY} ws-mac-1 at install", node="ws-node-1")
        self.assertEqual(stored["id"], "ws-mac-1")
        self.assertEqual(stored["address"], "100.64.0.5")
        self.assertEqual(stored["key"], KEY)
        self.assertEqual(stored["node"], "ws-node-1")
        self.assertRegex(stored["added"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(self.declarations(), [stored])
        self.assertEqual(
            stat.S_IMODE(os.stat(session_registry.declaration_path()).st_mode), 0o600)

    def test_add_without_comment_keeps_node_none(self):
        stored = self.add()
        self.assertIsNone(stored["node"])
        self.assertIsNone(self.declarations()[0]["node"])

    def test_tailnet_boundaries_accepted(self):
        self.add(ws_id="ws-a", address="100.64.0.1")
        self.add(ws_id="ws-z", address="100.127.255.254", key=OTHER_KEY)

    def test_refused_keys(self):
        bad = [
            "ssh-rsa " + KEY.split()[1],          # another key type
            raw_key(type_name=b"ecdsa-sha2-nistp256"),
            'command="x" ' + KEY,                  # options before the type
            KEY + "\n" + KEY,                      # embedded newline
            KEY + "\r",                            # CR
            KEY + "\x00",                          # NUL
            "ssh-ed25519 !!!not base64!!!",        # undecodable base64
            "ssh-ed25519 AA",                      # truncated base64
            raw_key(key_bytes=b"k" * 31),          # key not 32 bytes
            "ssh-ed25519",                         # base64 part missing
            42,                                    # not a string
        ]
        for key in bad:
            with self.assertRaises(RegistrationError, msg=repr(key)[:60]):
                self.add(key=key)
        self.assertFalse(session_registry.declaration_path().exists())
        self.assertFalse(self.keys_file.exists())

    def test_refused_addresses(self):
        bad = ["192.168.1.5", "100.63.255.255", "100.128.0.1", "8.8.8.8",
               "fd7a:115c:a1e0::1",                 # IPv6
               "100.64.0.0/10",                     # a CIDR, not an address
               "ws-mac-1.example",                  # a hostname
               ["100.64.0.5"], 5, None, ""]         # not a string at all
        for address in bad:
            with self.assertRaises(RegistrationError, msg=repr(address)):
                self.add(address=address)
        self.assertFalse(self.keys_file.exists())

    def test_refused_ids(self):
        for ws_id in ("operator", "hub",                 # reserved
                      "node-1", "site-a-arm1",           # inventory node ids (injected servers)
                      "bad id", "Node/1", "x" * 70):     # NODE_RE violations
            with self.assertRaises(RegistrationError, msg=ws_id):
                self.add(ws_id=ws_id)
        self.assertFalse(session_registry.declaration_path().exists())

    def test_refused_node(self):
        with self.assertRaises(RegistrationError):
            self.add(node="bad node!")
        self.assertFalse(session_registry.declaration_path().exists())

    def test_duplicate_id_and_key(self):
        self.add()
        with self.assertRaises(RegistrationError):    # same id, another key
            self.add(key=OTHER_KEY)
        with self.assertRaises(RegistrationError):    # same key, another id
            self.add(ws_id="ws-wsl-1", address="100.64.0.6", key=KEY)
        self.assertEqual([e["id"] for e in self.declarations()], ["ws-mac-1"])

    def test_inventory_unreadable_refuses(self):
        missing = Path(os.environ["GUARDIAN_DATA_DIR"]) / "missing-servers.json"
        with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(missing)}):
            with self.assertRaises(RegistrationError) as caught:
                session_registry.add_workstation("ws-mac-1", "100.64.0.5", KEY)
        self.assertIn("cannot read", str(caught.exception))
        self.assertFalse(session_registry.declaration_path().exists())
        self.assertFalse(self.keys_file.exists())


class LoadAndGet(SessionRegistryTestCase):
    def test_reads_create_nothing(self):
        self.assertEqual(session_registry.load_workstations(), ([], []))
        self.assertIsNone(session_registry.get_workstation("ws-mac-1"))
        self.assertEqual(session_registry.list_workstations(), ([], []))
        self.assertFalse(session_registry.declaration_path().exists())
        self.assertFalse(self.keys_file.exists())

    def test_load_skips_broken_entries(self):
        good = entry()
        broken = [
            {"id": "operator", "address": "100.64.0.5", "key": OTHER_KEY, "node": None,
             "added": "2026-11-20T10:00:00Z"},                     # reserved id
            entry(ws_id="ws-bad", address="10.0.0.1"),             # outside the tailnet
            entry(ws_id="ws-bad-key", key="not a key"),            # bad key
            entry(ws_id="ws-comment", key=KEY + " left in"),       # key not in stored form
            entry(ws_id="ws-mac-1", key=OTHER_KEY),                # duplicate id
            entry(ws_id="ws-dup-key", key=KEY),                    # duplicate key
            entry(ws_id="ws-no-ts", added="yesterday"),            # bad added
            entry(ws_id="ws-bad-node", node="nope!"),              # bad node
            "not an object",
        ]
        path = session_registry.declaration_path()
        path.write_text(json.dumps([good, *broken]), encoding="utf-8")
        entries, problems = session_registry.load_workstations()
        self.assertEqual([e["id"] for e in entries], ["ws-mac-1"])
        self.assertEqual(len(problems), len(broken))
        self.assertTrue(all(problem.startswith(path.name) for problem in problems))

    def test_load_refuses_non_list_file(self):
        path = session_registry.declaration_path()
        path.write_text('{"format": 1}', encoding="utf-8")
        entries, problems = session_registry.load_workstations()
        self.assertEqual(entries, [])
        self.assertEqual(len(problems), 1)

    def test_get_workstation(self):
        self.assertIsNone(session_registry.get_workstation("ws-mac-1"))
        self.add()
        self.assertEqual(session_registry.get_workstation("ws-mac-1")["key"], KEY)
        self.assertEqual(session_registry.get_workstation("nope"), None)


class RenderKeys(SessionRegistryTestCase):
    def test_header_lines_and_exact_line_format(self):
        self.add()
        self.add(ws_id="ws-wsl-1", address="100.64.0.6", key=OTHER_KEY)
        lines = self.keys_text().splitlines()
        self.assertEqual(lines[0], "# Guardian-owned file: written by guardian.py "
                                 "--session-add/--session-remove. Do not edit by hand.")
        self.assertEqual(lines[1], "# docs/design/operator-sessions.md (SD10). "
                                 "One line per workstation link key.")
        self.assertEqual(len(lines), 4)
        # Sorted by id: ws-mac-1 first, then ws-wsl-1.
        self.assertEqual(lines[2],
                         f'from="100.64.0.5",command="{session_registry.link_command("ws-mac-1")}",'
                         f'restrict {KEY} guardian-session:ws-mac-1')
        self.assertEqual(lines[3],
                         f'from="100.64.0.6",command="{session_registry.link_command("ws-wsl-1")}",'
                         f'restrict {OTHER_KEY} guardian-session:ws-wsl-1')
        text = "\n".join(lines)
        self.assertNotIn("no-touch-required", text)
        self.assertNotIn("permitopen", text)
        self.assertNotIn("pty,", text)

    def test_link_command_keeps_the_current_symlink(self):
        command = session_registry.link_command("ws-mac-1")
        self.assertTrue(command.startswith("/usr/bin/python3 "))
        self.assertIn("/.guardian/current/cli/llm-agent/guardian.py "
                      "--session-link --workstation ws-mac-1", command)
        self.assertNotIn("releases", command)

    def test_render_keys_matches_the_file(self):
        self.add()
        self.add(ws_id="ws-wsl-1", address="100.64.0.6", key=OTHER_KEY)
        self.assertEqual(self.keys_text(),
                         session_registry.render_keys(session_registry.load_workstations()[0]))


class SetAddress(SessionRegistryTestCase):
    def test_rewrites_from_line(self):
        self.add()
        changed = session_registry.set_address("ws-mac-1", "100.64.9.9")
        self.assertEqual(changed["address"], "100.64.9.9")
        self.assertIn('from="100.64.9.9"', self.keys_text())
        self.assertNotIn("100.64.0.5", self.keys_text())
        self.assertEqual(self.declarations()[0]["address"], "100.64.9.9")

    def test_refuses_unknown_and_bad_address(self):
        self.add()
        with self.assertRaises(RegistrationError):
            session_registry.set_address("nope", "100.64.9.9")
        with self.assertRaises(RegistrationError):
            session_registry.set_address("ws-mac-1", "192.168.0.1")
        self.assertIn('from="100.64.0.5"', self.keys_text())


class RemoveWorkstation(SessionRegistryTestCase):
    def test_removes_only_its_own_line(self):
        self.add()
        self.add(ws_id="ws-wsl-1", address="100.64.0.6", key=OTHER_KEY)
        removed = session_registry.remove_workstation("ws-mac-1")
        self.assertEqual(removed["id"], "ws-mac-1")
        self.assertEqual([e["id"] for e in self.declarations()], ["ws-wsl-1"])
        text = self.keys_text()
        self.assertNotIn("ws-mac-1", text)
        self.assertIn("guardian-session:ws-wsl-1", text)

    def test_header_only_after_the_last_entry(self):
        self.add()
        session_registry.remove_workstation("ws-mac-1")
        self.assertEqual(self.declarations(), [])
        self.assertEqual(self.keys_text(), session_registry.render_keys([]))
        self.assertTrue(self.keys_file.exists())

    def test_unknown_id(self):
        with self.assertRaises(RegistrationError):
            session_registry.remove_workstation("ws-mac-1")


class WriteKeys(SessionRegistryTestCase):
    def test_refuses_a_foreign_file(self):
        foreign = "# a hand-written file\nssh-ed25519 AAAA operator@host\n"
        self.keys_file.write_text(foreign, encoding="utf-8")
        with self.assertRaises(RegistrationError) as caught:
            session_registry.write_keys([])
        self.assertIn("not Guardian-owned", str(caught.exception))
        self.assertEqual(self.keys_text(), foreign)  # unchanged

    def test_mutators_refuse_a_foreign_file_and_change_nothing(self):
        self.add()
        self.keys_file.write_text("# a hand-written file\n", encoding="utf-8")
        with self.assertRaises(RegistrationError):
            self.add(ws_id="ws-wsl-1", address="100.64.0.6", key=OTHER_KEY)
        with self.assertRaises(RegistrationError):
            session_registry.set_address("ws-mac-1", "100.64.9.9")
        with self.assertRaises(RegistrationError):
            session_registry.remove_workstation("ws-mac-1")
        self.assertEqual([e["id"] for e in self.declarations()], ["ws-mac-1"])

    def test_mode_and_missing_parent(self):
        target = Path(os.environ["GUARDIAN_DATA_DIR"]) / "newdir" / "authorized_keys2"
        session_registry.write_keys([entry()], path=target)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(target.parent).st_mode), 0o700)


class KeysPath(SessionRegistryTestCase):
    def test_refuses_authorized_keys(self):
        with patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE":
                                     str(Path(os.environ["GUARDIAN_DATA_DIR"]) / "authorized_keys")}):
            with self.assertRaises(RegistrationError) as caught:
                session_registry.keys_path()
        self.assertIn("authorized_keys", str(caught.exception))

    def test_default_is_the_home_file(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUARDIAN_SESSION_KEYS_FILE", None)
            self.assertEqual(session_registry.keys_path(),
                             Path.home() / ".ssh" / "authorized_keys2")


class SshdCheck(SessionRegistryTestCase):
    @staticmethod
    def runner(calls, plain_rc, sudo_rc, stdout):
        def run(argv):
            calls.append(list(argv))
            if argv[0] == "sudo":
                return sudo_rc, (stdout if sudo_rc == 0 else ""), "sudo failed"
            return plain_rc, (stdout if plain_rc == 0 else ""), "plain failed"
        return run

    def test_ok_when_listed(self):
        listed = "authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2"
        for value in (listed, "authorizedkeysfile %h/.ssh/authorized_keys2",
                      "authorizedkeysfile /home/ubuntu/.ssh/authorized_keys2"):
            result = session_registry.sshd_reads_keys2(
                self.runner([], 0, 1, value + "\n"))
            self.assertIs(result["ok"], True, value)
            self.assertIn("authorized_keys2", result["detail"])

    def test_false_when_not_listed(self):
        result = session_registry.sshd_reads_keys2(
            self.runner([], 0, 1, "authorizedkeysfile .ssh/authorized_keys\n"))
        self.assertIs(result["ok"], False)

    def test_none_when_neither_runs(self):
        calls = []
        result = session_registry.sshd_reads_keys2(self.runner(calls, 1, 1, ""))
        self.assertIsNone(result["ok"])
        self.assertIn("could not run", result["detail"])
        self.assertEqual(calls, [["sshd", "-T"], ["sudo", "-n", "sshd", "-T"]])

    def test_sudo_fallback_used_when_plain_fails(self):
        calls = []
        result = session_registry.sshd_reads_keys2(
            self.runner(calls, 1, 0, "authorizedkeysfile .ssh/authorized_keys2\n"))
        self.assertIs(result["ok"], True)
        self.assertEqual(calls, [["sshd", "-T"], ["sudo", "-n", "sshd", "-T"]])

    def test_no_authorizedkeysfile_line_is_not_ok(self):
        result = session_registry.sshd_reads_keys2(self.runner([], 0, 1, "port 22\n"))
        self.assertIs(result["ok"], False)
        self.assertIn("no authorizedkeysfile line", result["detail"])


class ListWorkstations(SessionRegistryTestCase):
    def test_keys_line_flag(self):
        self.assertEqual(session_registry.list_workstations(), ([], []))
        self.add()
        self.add(ws_id="ws-wsl-1", address="100.64.0.6", key=OTHER_KEY)
        entries, problems = session_registry.list_workstations()
        self.assertEqual(problems, [])
        self.assertEqual([e["keys_line"] for e in entries], [True, True])

    def test_keys_line_false_when_the_file_misses_the_line(self):
        self.add()
        gone = Path(os.environ["GUARDIAN_DATA_DIR"]) / "gone-keys2"
        with patch.dict(os.environ, {"GUARDIAN_SESSION_KEYS_FILE": str(gone)}):
            entries, _ = session_registry.list_workstations()
            self.assertEqual([e["keys_line"] for e in entries], [False])
        # A stale line (the address moved but the file was not rewritten) is also False.
        self.keys_file.write_text(session_registry.render_keys(
            [entry(address="100.64.0.9")]), encoding="utf-8")
        entries, _ = session_registry.list_workstations()
        self.assertEqual([e["keys_line"] for e in entries], [False])


if __name__ == "__main__":
    unittest.main()

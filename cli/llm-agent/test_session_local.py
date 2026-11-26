"""session_local: the workstation-local contract of operator sessions (S2 of
docs/design/operator-sessions.md). The mirrored constants are pinned against the hub modules
session_link.py and mail_relay.py, the module is asserted to import nothing but the standard
library, and the framing, the request validators, the fixed texts, the paths, the config and the
blocking client are checked end to end (the client against a real AF_UNIX server thread in a
temp dir)."""

import ast
import hashlib
import io
import json
import os
import socket
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import session_local

# The workstation modules the stdlib-only guard covers: every module S2 installs on an operator
# workstation. Names that do not exist yet are skipped, and each may import the others, so
# allowed_local is the stems of this whole list.
WORKSTATION_MODULES = ["session_local.py", "session_uplink.py", "session_daemon.py",
                       "session_mcp.py", "session_hooks.py", "session_wake.py"]

# The fallback for Python 3.9, where sys.stdlib_module_names does not exist: the standard
# library modules any workstation module may plausibly import. Extra names only widen the
# guard on 3.9; a missing name would fail it wrongly, so the list errs on the wide side.
_FIXED_STDLIB = frozenset({
    "__future__", "abc", "argparse", "ast", "asyncio", "base64", "binascii", "bisect",
    "builtins", "bz2", "calendar", "cmath", "codecs", "collections", "contextlib", "copy",
    "copyreg", "csv", "ctypes", "dataclasses", "datetime", "decimal", "difflib", "dis",
    "doctest", "email", "enum", "errno", "fcntl", "filecmp", "fileinput", "fnmatch",
    "fractions", "functools", "gc", "getopt", "getpass", "gettext", "glob", "grp", "gzip",
    "hashlib", "heapq", "hmac", "html", "http", "importlib", "inspect", "io", "ipaddress",
    "itertools", "json", "linecache", "locale", "logging", "lzma", "mailbox", "math",
    "mimetypes", "mmap", "multiprocessing", "netrc", "numbers", "operator", "os", "pathlib",
    "pdb", "pickle", "pkgutil", "platform", "plistlib", "pprint", "profile", "pstats", "pty",
    "pwd", "py_compile", "queue", "quopri", "random", "re", "readline", "reprlib", "resource",
    "runpy", "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil", "signal",
    "site", "smtplib", "socket", "socketserver", "sqlite3", "ssl", "stat", "statistics",
    "string", "struct", "subprocess", "symtable", "sys", "sysconfig", "tarfile", "tempfile",
    "termios", "textwrap", "threading", "time", "timeit", "token", "tokenize", "trace",
    "traceback", "tracemalloc", "tty", "types", "typing", "unicodedata", "unittest", "urllib",
    "uuid", "venv", "warnings", "weakref", "webbrowser", "xml", "zipfile", "zipimport", "zlib",
})


def _stdlib_module_names():
    """sys.stdlib_module_names when the interpreter has it (3.10+), the fixed list otherwise."""
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names) | {"__future__"}
    return set(_FIXED_STDLIB)


def _imported_top_levels(path):
    """Every top-level module name the module at path imports; a relative import counts as the
    local module it is."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add((node.module or "(relative import)").split(".")[0])
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def assert_stdlib_only(testcase, path, allowed_local=()):
    """Asserts the module at path imports only the standard library, plus the local module
    names in allowed_local: the workstation modules must run on a bare macOS or Linux
    Python 3.9+, where no hub module exists."""
    allowed = set(allowed_local)
    for name in sorted(_imported_top_levels(path)):
        if name in allowed or name in _stdlib_module_names():
            continue
        testcase.fail(f"{os.path.basename(path)} imports {name!r}: not stdlib and not one of "
                      f"the allowed local modules {sorted(allowed)}")


class HubPinTests(unittest.TestCase):
    """The mirrored constants must equal the hub values; a hub change that must reach the
    workstations shows up here first. These imports exist only in the hub repository."""

    def test_link_constants_pin(self):
        import session_link

        self.assertEqual(session_local.LINK_PROTO, session_link.LINK_PROTO)
        self.assertEqual(session_local.LINK_VERSION, session_link.LINK_VERSION)

    def test_line_limit_pin(self):
        import mail_relay

        self.assertEqual(session_local.MAX_LINE_BYTES, mail_relay.MAX_LINE_BYTES)

    def test_review_shape_pins(self):
        import code_context
        import review_agent
        import session_link

        self.assertEqual(session_local._REVIEW_ID_RE.pattern, review_agent.RUN_ID.pattern)
        self.assertEqual(session_local._REVIEW_ID_RE.pattern, session_link.REVIEW_ID_RE.pattern)
        self.assertEqual(session_local._CONTEXT_REF_RE.pattern, code_context.REF_PATTERN.pattern)
        self.assertEqual(session_local._SHA1_RE.pattern, session_link._SHA1_RE.pattern)
        self.assertEqual(session_local._PROJECT_ID_RE.pattern, session_link._PROJECT_ID_RE.pattern)


class StdlibGuardTests(unittest.TestCase):
    def test_workstation_modules_import_stdlib_only(self):
        root = Path(__file__).resolve().parent
        allowed = {Path(name).stem for name in WORKSTATION_MODULES}
        checked = 0
        for name in WORKSTATION_MODULES:
            path = root / name
            if not path.exists():
                continue  # an S2 phase not built yet
            assert_stdlib_only(self, path, allowed_local=allowed)
            checked += 1
        self.assertGreaterEqual(checked, 1)  # session_local.py itself must always be covered

    def test_fixed_fallback_list_covers_session_local(self):
        # Python 3.9 has no sys.stdlib_module_names: with it absent (patched to None), the
        # fixed list alone must accept everything session_local actually imports.
        with patch.object(sys, "stdlib_module_names", None, create=True):
            path = Path(__file__).resolve().parent / "session_local.py"
            assert_stdlib_only(self, path, allowed_local=set())
            self.assertEqual(_stdlib_module_names(), set(_FIXED_STDLIB))

    def test_guard_rejects_a_hub_import(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "session_probe.py"
            bad.write_text("import json\nimport mail_store\n", encoding="utf-8")
            with self.assertRaises(AssertionError):
                assert_stdlib_only(self, bad)
            assert_stdlib_only(self, bad, allowed_local={"mail_store"})  # allowed: no raise


class FixedTextTests(unittest.TestCase):
    def test_wake_line_and_stop_reason(self):
        self.assertEqual(session_local.WAKE_LINE, "new mail from operator: call session_inbox")
        self.assertEqual(session_local.STOP_REASON, "new mail: call session_inbox")

    def test_unread_context_wording(self):
        self.assertEqual(session_local.unread_context(1),
                         "1 new message; read them with session_inbox")
        self.assertEqual(session_local.unread_context(3),
                         "3 new messages; read them with session_inbox")

    def test_limits_and_timings(self):
        s = session_local
        self.assertEqual(s.HUB_CALL_TIMEOUT, 20.0)
        self.assertEqual(s.LOCAL_CLIENT_TIMEOUT, 25.0)
        self.assertEqual(s.HOOK_CLIENT_TIMEOUT, 3.0)
        self.assertEqual(s.BUSY_AGE_CAP, 30 * 60)
        self.assertEqual(s.WAKE_INTERVAL, 600)
        self.assertEqual(s.IDLE_AFTER, 600)
        self.assertEqual(s.INPUT_QUIET, 60)
        self.assertEqual(s.MAIL_BODY_MAX, 32 * 1024)
        # The MCP socket outlives the daemon's hub wait; a hook gives up long before either.
        self.assertGreater(s.LOCAL_CLIENT_TIMEOUT, s.HUB_CALL_TIMEOUT)
        self.assertLess(s.HOOK_CLIENT_TIMEOUT, s.HUB_CALL_TIMEOUT)


class SkipSessionTests(unittest.TestCase):
    def test_ordinary_session_is_not_skipped(self):
        self.assertFalse(session_local.skip_session({}))
        self.assertFalse(session_local.skip_session({"CLAUDE_CODE_ENTRYPOINT": "cli"}))
        self.assertFalse(session_local.skip_session({"GUARDIAN_SESSION_CHILD": "0"}))

    def test_print_children_and_summary_children_are_skipped(self):
        self.assertTrue(session_local.skip_session({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        self.assertTrue(session_local.skip_session({"GUARDIAN_SESSION_CHILD": "1"}))
        self.assertTrue(session_local.skip_session({"CLAUDE_CODE_ENTRYPOINT": "cli",
                                                    "GUARDIAN_SESSION_CHILD": "1"}))


class FrameTests(unittest.TestCase):
    def test_round_trip(self):
        frame = {"op": "send", "to": "cleaner@ws-mac-1", "body": "안녕하세요"}
        data = session_local.encode_frame(frame)
        self.assertTrue(data.endswith(b"\n"))
        self.assertNotIn(b"\n", data[:-1])
        self.assertEqual(session_local.read_frame(io.BytesIO(data)), frame)

    def test_ensure_ascii_false_keeps_utf8(self):
        data = session_local.encode_frame({"body": "메일"})
        self.assertIn("메일".encode("utf-8"), data)  # not \\uXXXX escapes
        self.assertNotIn(b"\\u", data)

    def test_oversize_rejected(self):
        with self.assertRaises(ValueError):
            session_local.encode_frame({"body": "x" * (session_local.MAX_LINE_BYTES + 10)})

    def test_encode_boundary_is_the_limit(self):
        base = len(json.dumps({"body": ""}, ensure_ascii=False))
        body = "x" * (session_local.MAX_LINE_BYTES - base)
        data = session_local.encode_frame({"body": body})  # exactly at the limit: accepted
        self.assertEqual(len(data) - 1, session_local.MAX_LINE_BYTES)
        self.assertIn("body", session_local.read_frame(io.BytesIO(data)))
        with self.assertRaises(ValueError):  # one byte over
            session_local.encode_frame({"body": body + "x"})

    def test_read_boundary_is_the_limit(self):
        filler = b"x" * (session_local.MAX_LINE_BYTES - 11)  # {"body":"xxxxx"} is content
        line = b'{"body":"' + filler + b'"}\n'
        self.assertEqual(len(line) - 1, session_local.MAX_LINE_BYTES)
        frame = session_local.read_frame(io.BytesIO(line))
        self.assertEqual(frame, {"body": filler.decode("ascii")})
        with self.assertRaises(ValueError):  # one byte over
            session_local.read_frame(io.BytesIO(b'{"body":"' + b"x" * (session_local.MAX_LINE_BYTES
                                                                       - 10) + b'"}\n'))

    def test_eof_rejected(self):
        with self.assertRaises(ValueError):
            session_local.read_frame(io.BytesIO(b""))  # closed before a frame
        with self.assertRaises(ValueError):
            session_local.read_frame(io.BytesIO(b'{"op": "inbox"}'))  # no newline
        with self.assertRaises(ValueError):  # EOF mid-frame, not over the limit
            session_local.read_frame(io.BytesIO(b'{"op": "inbo' + b"x" * 10))

    def test_non_object_and_bad_json_rejected(self):
        for line in (b'["op"]\n', b"null\n", b"3\n", b"not json\n", b'{"op": "inbox",}\n'):
            with self.assertRaises(ValueError):
                session_local.read_frame(io.BytesIO(line))


class RequestValidatorTests(unittest.TestCase):
    def assert_problems(self, frame, *fragments):
        problems = session_local.validate_request(frame)
        self.assertTrue(problems, f"expected problems for {frame!r}")
        for fragment in fragments:
            self.assertTrue(any(fragment in problem for problem in problems),
                            f"{fragment!r} not reported for {frame!r}: {problems}")

    def assert_good(self, frame):
        problems = session_local.validate_request(frame)
        self.assertEqual(problems, [], f"unexpected problems for {frame!r}")

    def test_register_good(self):
        self.assert_good({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                          "tmux_socket": "/private/tmp/tmux-501/default", "tmux_pane": "%3",
                          "project_dir": "/Users/op/work",
                          "source": "claude", "name": "cleaner"})
        self.assert_good({"op": "register", "cli_session": "s-1", "cli_pid": 1, "tmux_socket": None,
                          "tmux_pane": None, "project_dir": None, "source": "codex",
                          "name": None})

    def test_register_bad(self):
        self.assert_problems({"op": "register", "cli_pid": 4242, "source": "claude"},
                             "missing field 'cli_session'")
        self.assert_problems({"op": "register", "cli_session": "s-1",
                              "source": "claude"}, "missing field 'cli_pid'")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": "4242",
                              "source": "claude"}, "'cli_pid' must be an int")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": True,
                              "source": "claude"}, "'cli_pid' must be an int")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 0,
                              "source": "claude"}, "'cli_pid' must be a process id")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                              "source": "claude", "tmux_socket": "default"},
                             "'tmux_socket' must be an absolute path")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                              "tmux_socket": 3, "source": "claude"},
                             "'tmux_socket' must be an absolute path")
        for pane in ("3", "%", "%3x", "%-3", "%٣"):
            self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                                  "tmux_pane": pane, "source": "claude"},
                                 "'tmux_pane' must be a tmux pane id")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                              "source": ""}, "'source' must not be empty")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                              "source": 3}, "'source' must be text")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                              "source": "claude", "name": "Cleaner"},
                             "'name' must be a session name")
        self.assert_problems({"op": "register", "cli_session": "s-1", "cli_pid": 4242,
                              "source": "claude", "cli_cwd": "/tmp"},
                             "unknown field 'cli_cwd'")

    def test_prompt_submit_and_stop(self):
        for op in ("prompt_submit", "stop"):
            self.assert_good({"op": op, "cli_session": "s-1"})
            self.assert_problems({"op": op}, "missing field 'cli_session'")
            self.assert_problems({"op": op, "cli_session": ""}, "'cli_session' must not be empty")
            self.assert_problems({"op": op, "cli_session": None}, "'cli_session' must be text")

    def test_end(self):
        self.assert_good({"op": "end", "cli_session": "s-1", "reason": "cli_exit"})
        self.assert_problems({"op": "end", "reason": "cli_exit"}, "missing field 'cli_session'")
        self.assert_problems({"op": "end", "cli_session": "s-1"}, "missing field 'reason'")
        self.assert_problems({"op": "end", "cli_session": "s-1", "reason": ""},
                             "'reason' must not be empty")

    def test_link_good(self):
        self.assert_good({"op": "link"})
        self.assert_good({"op": "link", "name": "cleaner", "away": True})
        self.assert_good({"op": "link", "name": None, "away": None})
        self.assert_good({"op": "link", "name": "cleaner"})
        self.assert_good({"op": "link", "away": False})

    def test_link_bad(self):
        self.assert_problems({"op": "link", "name": "Cleaner"}, "'name' must be a session name")
        self.assert_problems({"op": "link", "name": 5}, "'name' must be a session name")
        self.assert_problems({"op": "link", "away": "true"}, "'away' must be true or false")
        self.assert_problems({"op": "link", "away": 1}, "'away' must be true or false")

    def test_bare_mcp_ops(self):
        for op in ("inbox", "list", "status"):
            self.assert_good({"op": op})
            self.assert_problems({"op": op, "happy": True}, "unknown field 'happy'")

    def test_mcp_identity_fields_rejected(self):
        for field in session_local.IDENTITY_FIELDS:
            for op in ("link", "inbox", "send", "list", "status", "resident_query", "resident_query_get",
                       "review_submit", "review_result"):
                frame = {"op": op, field: "cleaner@ws-mac-1"}
                if op == "send":
                    frame.update({"to": "cleaner@ws-mac-1", "kind": "message",
                                  "subject": "hi", "body": "hi"})
                elif op == "resident_query":
                    frame.update({"to": "codex@node-1", "project": "server-guardian", "prompt": "hi"})
                elif op == "resident_query_get":
                    frame.update({"query_id": "q-20261123-0001"})
                elif op == "review_submit":
                    frame.update({"project": "server-guardian", "ref": "feature/x"})
                elif op == "review_result":
                    frame.update({"review_id": "review_20261123_101112_ab12cd"})
                self.assert_problems(frame, "stamps the session from the socket")

    def test_hook_identity_fields_are_unknown_fields(self):
        # On the control socket the identity keys are simply not part of any op.
        self.assert_problems({"op": "stop", "cli_session": "s-1", "from": "x"},
                             "unknown field 'from'")

    def test_send_good(self):
        self.assert_good({"op": "send", "to": "cleaner@ws-mac-1", "kind": "question",
                          "subject": "빌드 상태", "body": "안녕하세요", "thread": "t-1",
                          "reply_to": "m-1"})
        self.assert_good({"op": "send", "to": "operator", "kind": "answer", "subject": "",
                          "body": ""})
        self.assert_good({"op": "send", "to": "operator", "kind": "message", "subject": "s",
                          "body": "x" * session_local.MAIL_BODY_MAX})
        # The limit counts UTF-8 bytes, not characters.
        self.assert_good({"op": "send", "to": "operator", "kind": "message", "subject": "s",
                          "body": "x" * (session_local.MAIL_BODY_MAX - 3) + "요"})

    def test_send_bad(self):
        good = {"op": "send", "to": "operator", "kind": "message", "subject": "s", "body": "b"}
        for kind in ("status", "task_offer", "notice", "message ", "", None, 5):
            with self.subTest(kind=kind):
                self.assert_problems({**good, "kind": kind}, "'kind'")
        self.assert_problems({**good, "body": "x" * (session_local.MAIL_BODY_MAX + 1)},
                             "'body' is over the")
        self.assert_problems({**good, "body": "요" * (session_local.MAIL_BODY_MAX // 3 + 1)},
                             "'body' is over the")
        for field in ("to", "subject", "body"):
            with self.subTest(missing=field):
                self.assert_problems({key: value for key, value in good.items() if key != field},
                                     f"missing field '{field}'")
        self.assert_problems({**good, "to": ""}, "'to' must not be empty")
        self.assert_problems({**good, "body": 5}, "'body' must be text")
        self.assert_problems({**good, "thread": ""}, "'thread' must not be empty when set")
        self.assert_problems({**good, "reply_to": 9}, "'reply_to' must be text or null")

    def test_resident_query_good_and_bad(self):
        good = {"op": "resident_query", "to": "codex@node-1", "project": "server-guardian", "prompt": "check"}
        self.assert_good(good)
        self.assert_good({**good, "task": "t-1", "session_key": "project:server-guardian:review",
                          "mode": "fork", "timeout_sec": 120, "wait_sec": 15})
        self.assert_good({**good, "task": None, "session_key": "main", "mode": "in_place", "wait_sec": 0})
        self.assert_problems({**good, "session_key": "bad key"}, "'session_key'")
        self.assert_problems({**good, "mode": "detached"}, "'mode'")
        self.assert_problems({**good, "prompt": ""}, "'prompt' must be non-empty text")
        self.assert_problems({**good, "prompt": "x" * (session_local.MAIL_BODY_MAX + 1)}, "'prompt' is over the")
        self.assert_problems({**good, "timeout_sec": 5}, "'timeout_sec' must be an integer from 10 to 600")
        self.assert_problems({**good, "timeout_sec": 999}, "'timeout_sec' must be an integer from 10 to 600")
        self.assert_problems({**good, "wait_sec": 16}, "'wait_sec' must be an integer from 0 to 15")
        self.assert_problems({**good, "wait_sec": True}, "'wait_sec' must be an integer from 0 to 15")

    def test_resident_query_get_good_and_bad(self):
        self.assert_good({"op": "resident_query_get", "query_id": "q-20261123-0001"})
        self.assert_good({"op": "resident_query_get", "query_id": "q-20261123-0001", "wait_sec": 10})
        self.assert_problems({"op": "resident_query_get", "query_id": "bad"}, "'query_id' must match")
        self.assert_problems({"op": "resident_query_get", "query_id": "q-20261123-0001", "wait_sec": 20},
                             "'wait_sec' must be an integer from 0 to 15")

    def test_review_submit_good_and_bad(self):
        good = {"op": "review_submit", "project": "server-guardian", "ref": "feature/retry"}
        self.assert_good(good)
        self.assert_good({**good, "base": "main", "expect_sha": "a" * 40, "provider": "claude"})
        self.assert_good({**good, "base": None, "expect_sha": None, "provider": None})
        for key in ("project", "ref"):
            with self.subTest(missing=key):
                self.assert_problems({k: v for k, v in good.items() if k != key},
                                     f"missing field '{key}'")
        self.assert_problems({**good, "project": "Server-Guardian"}, "'project' must be a project id")
        self.assert_problems({**good, "project": "-p"}, "'project' must be a project id")
        self.assert_problems({**good, "ref": "-head"}, "'ref' is not a valid ref")
        self.assert_problems({**good, "ref": "bad ref"}, "'ref' is not a valid ref")
        self.assert_problems({**good, "ref": "x" * 101}, "'ref' is not a valid ref")
        self.assert_problems({**good, "base": "--glob"}, "'base' is not a valid ref")
        self.assert_problems({**good, "base": 5}, "'base' must be text or null")
        self.assert_problems({**good, "expect_sha": "A" * 40}, "'expect_sha' must be 40")
        self.assert_problems({**good, "expect_sha": "a" * 39}, "'expect_sha' must be 40")
        self.assert_problems({**good, "provider": ""}, "'provider' must not be empty when set")
        self.assert_problems({**good, "patch": "diff --git"}, "unknown field 'patch'")
        self.assert_problems({**good, "range": "main..head"}, "unknown field 'range'")
        self.assert_problems({**good, "action": 7}, "unknown field 'action'")

    def test_review_result_good_and_bad(self):
        self.assert_good({"op": "review_result", "review_id": "review_20261123_101112_ab12cd"})
        self.assert_good({"op": "review_result", "review_id": "review_20261123_101112_ab12cd",
                          "wait_sec": 15})
        for bad in ("review_20261123_101112", "review_2026112_101112_ab12cd", "x" * 5,
                    "review_20261123_101112_AB12CD"):
            with self.subTest(bad=bad):
                self.assert_problems({"op": "review_result", "review_id": bad},
                                     "'review_id' must match")
        self.assert_problems({"op": "review_result", "review_id": "review_20261123_101112_ab12cd",
                              "wait_sec": 16}, "'wait_sec' must be an integer from 0 to 15")
        self.assert_problems({"op": "review_result"}, "missing field 'review_id'")
        self.assert_problems({"op": "review_result", "review_id": "review_20261123_101112_ab12cd",
                              "result": True}, "unknown field 'result'")

    def test_unknown_op_and_shapes(self):
        self.assert_problems({"op": "restart"}, "unknown op 'restart'")
        self.assert_problems({}, "'op'")
        self.assert_problems({"op": 5}, "'op'")
        self.assert_problems(["op"], "must be a JSON object")
        self.assert_problems("send", "must be a JSON object")


class ReplyAndNameTests(unittest.TestCase):
    def test_ok_reply(self):
        self.assertEqual(session_local.ok_reply(), {"ok": True})
        self.assertEqual(session_local.ok_reply(unread=2, address="cleaner@ws-mac-1"),
                         {"ok": True, "unread": 2, "address": "cleaner@ws-mac-1"})

    def test_error_reply(self):
        self.assertEqual(session_local.error_reply(session_local.LINK_DOWN),
                         {"ok": False, "error": "link_down", "detail": ""})
        self.assertEqual(session_local.error_reply(session_local.BAD_REQUEST, "tmux_pane"),
                         {"ok": False, "error": "bad_request", "detail": "tmux_pane"})

    def test_error_codes_are_distinct_strings(self):
        codes = [session_local.LINK_DOWN, session_local.NOT_REGISTERED, session_local.NOT_LINKED,
                 session_local.TIMEOUT, session_local.BAD_REQUEST, session_local.HUB_ERROR,
                 session_local.DISABLED]
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            self.assertIsInstance(code, str)
            self.assertTrue(code)

    def test_valid_session_name(self):
        # The contract pattern constrains only the first character, so a trailing hyphen
        # ("c-") is a valid name; a leading one ("-c") is not.
        for good in ("cleaner", "a", "a1", "c" * 32, "c-"):
            self.assertTrue(session_local.valid_session_name(good), good)
        for bad in ("Cleaner", "-c", "c_d", "", "c" * 33, 5, None, "cleaner\n", "c d"):
            self.assertFalse(session_local.valid_session_name(bad), repr(bad))

    def test_address(self):
        self.assertEqual(session_local.address("cleaner", "ws-mac-1"), "cleaner@ws-mac-1")


class SessionPathsTests(unittest.TestCase):
    def test_layout(self):
        home = Path("/Users") / "op"
        paths = session_local.SessionPaths(home)
        root = home / ".guardian" / "session"
        self.assertEqual(paths.root, root)
        self.assertEqual(paths.key, root / "link-key")
        self.assertEqual(paths.known_hosts, root / "known_hosts")
        self.assertEqual(paths.config, root / "config.json")
        self.assertEqual(paths.spool, root / "spool")
        self.assertEqual(paths.run, root / "run")
        self.assertEqual(paths.control_socket, root / "run" / "control.sock")
        self.assertEqual(paths.log, root / "daemon.log")

    def test_session_socket_is_deterministic(self):
        paths = session_local.SessionPaths(Path("/Users") / "op")
        first = paths.session_socket("6f9d3b2e-2ad0-4f2b-9d1c-0a1b2c3d4e5f")
        self.assertEqual(first, paths.session_socket("6f9d3b2e-2ad0-4f2b-9d1c-0a1b2c3d4e5f"))
        self.assertNotEqual(first, paths.session_socket("another-session"))
        digest = hashlib.sha256(b"6f9d3b2e-2ad0-4f2b-9d1c-0a1b2c3d4e5f").hexdigest()[:16]
        self.assertEqual(first.name, f"s-{digest}.sock")
        self.assertEqual(first.parent, paths.run)

    def test_check_socket_path_boundary(self):
        self.assertIsNone(session_local.check_socket_path("/" + "a" * 102))   # 103 bytes
        with self.assertRaises(ValueError):
            session_local.check_socket_path("/" + "a" * 103)                  # 104 bytes
        with self.assertRaises(ValueError):
            session_local.check_socket_path("a" * 104)

    def test_session_socket_refuses_a_too_long_home(self):
        deep = Path("/" + "/".join(["work"] * 30))
        paths = session_local.SessionPaths(deep)
        with self.assertRaises(ValueError):
            paths.session_socket("any-session")

    def test_ensure_dirs_creates_and_narrows(self):
        with TemporaryDirectory() as home:
            paths = session_local.SessionPaths(Path(home))
            paths.ensure_dirs()
            for path in (paths.root, paths.spool, paths.run):
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            os.chmod(paths.spool, 0o755)  # an over-sharing umask, or a stray chmod
            paths.ensure_dirs()
            self.assertEqual(paths.spool.stat().st_mode & 0o777, 0o700)
            paths.ensure_dirs()  # idempotent
            self.assertEqual(paths.run.stat().st_mode & 0o777, 0o700)


GOOD_CONFIG = {
    "workstation": "ws-mac-1",
    "hub_address": "100.64.0.5",
    "hub_name": "guardian-link.example.net",
    "hub_alias": "site-a-arm1",
    "hub_user": "ubuntu",
}


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="session-local-cfg-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"

    def write(self, cfg):
        text = cfg if isinstance(cfg, str) else json.dumps(cfg)
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_good_config(self):
        cfg = self.write(GOOD_CONFIG)
        self.assertEqual(session_local.load_config(cfg), {**GOOD_CONFIG, "ssh_port": 22})

    def test_ssh_port_range_and_default(self):
        for port in (1, 22, 2222, 65535):
            with self.subTest(port=port):
                cfg = self.write({**GOOD_CONFIG, "ssh_port": port})
                self.assertEqual(session_local.load_config(cfg)["ssh_port"], port)
        self.assertEqual(session_local.load_config(self.write({**GOOD_CONFIG,
                                                               "hub_name": ""}))["hub_name"], "")

    def test_bad_values(self):
        cases = [
            ({**GOOD_CONFIG, "workstation": "WS-Mac-1"}, "workstation must match"),
            ({**GOOD_CONFIG, "workstation": "-ws"}, "workstation must match"),
            ({**GOOD_CONFIG, "workstation": "w" * 64}, "workstation must match"),
            ({**GOOD_CONFIG, "workstation": 5}, "workstation must match"),
            ({**GOOD_CONFIG, "hub_address": "site-a-arm1.example.net"}, "hub_address"),
            ({**GOOD_CONFIG, "hub_address": "100.64.0"}, "hub_address"),
            ({**GOOD_CONFIG, "hub_address": "100.64.0.5.6"}, "hub_address"),
            ({**GOOD_CONFIG, "hub_address": "999.1.1.1"}, "hub_address"),
            ({**GOOD_CONFIG, "hub_address": "100.64.0.256"}, "hub_address"),
            ({**GOOD_CONFIG, "hub_name": "-bad.example.net"}, "hub_name"),
            ({**GOOD_CONFIG, "hub_name": "bad-.example.net"}, "hub_name"),
            ({**GOOD_CONFIG, "hub_name": "a..net"}, "hub_name"),
            ({**GOOD_CONFIG, "hub_name": "a_b.example.net"}, "hub_name"),
            ({**GOOD_CONFIG, "hub_name": "x" * 254}, "hub_name"),
            ({**GOOD_CONFIG, "hub_name": 7}, "hub_name"),
            ({**GOOD_CONFIG, "hub_alias": ""}, "hub_alias must be a non-empty string"),
            ({**GOOD_CONFIG, "hub_user": "Op-User"}, "hub_user must match"),
            ({**GOOD_CONFIG, "hub_user": "-op"}, "hub_user must match"),
            ({**GOOD_CONFIG, "hub_user": ""}, "hub_user must match"),
            ({**GOOD_CONFIG, "ssh_port": 0}, "ssh_port must be an integer"),
            ({**GOOD_CONFIG, "ssh_port": 65536}, "ssh_port must be an integer"),
            ({**GOOD_CONFIG, "ssh_port": "22"}, "ssh_port must be an integer"),
            ({**GOOD_CONFIG, "ssh_port": True}, "ssh_port must be an integer"),
            ({**GOOD_CONFIG, "extra": True}, "unknown config keys"),
            ({key: value for key, value in GOOD_CONFIG.items() if key != "hub_user"},
             "missing config key 'hub_user'"),
        ]
        for cfg, fragment in cases:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError) as caught:
                    session_local.load_config(self.write(cfg))
                self.assertIn(fragment, str(caught.exception))

    def test_bad_file(self):
        with self.assertRaises(ValueError) as missing:
            session_local.load_config(Path(self._tmp.name) / "nope.json")
        self.assertIn("cannot read", str(missing.exception))
        for text in ("{", "[1, 2]", '"just a string"', ""):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    session_local.load_config(self.write(text))


class FrameServer(threading.Thread):
    """One real AF_UNIX server for the client tests: one connection at a time, reads one frame,
    records it and answers handler(frame). A handler that returns None sends nothing and holds
    the connection open until stop (or 5 s), so the client sees a timeout, not EOF."""

    def __init__(self, socket_path, handler):
        super().__init__(daemon=True, name="session-local-test-server")
        self.handler = handler
        self.received = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(socket_path))
        self._sock.listen(4)
        self._sock.settimeout(0.1)

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(5.0)
                read_file = conn.makefile("rb")
                try:
                    frame = session_local.read_frame(read_file)
                except (OSError, ValueError):
                    frame = {"op": "(unreadable)"}
                finally:
                    read_file.close()
                self.received.append(frame)
                reply = self.handler(frame)
                if reply is not None:
                    try:
                        conn.sendall(session_local.encode_frame(reply))
                    except OSError:
                        pass
                else:  # stay silent with the connection open: the client must time out
                    self._stop.wait(timeout=5.0)

    def stop(self):
        self._stop.set()
        self.join(timeout=5.0)
        self._sock.close()


class CallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="session-local-call-")
        self.addCleanup(self._tmp.cleanup)
        self.socket_path = Path(self._tmp.name) / "s.sock"
        session_local.check_socket_path(self.socket_path)  # a clear error if TMPDIR is deep

    def serve(self, handler):
        server = FrameServer(self.socket_path, handler)
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_round_trip(self):
        server = self.serve(lambda frame: session_local.ok_reply(unread=2))
        reply = session_local.call(self.socket_path, {"op": "status"}, timeout=5.0)
        self.assertEqual(reply, {"ok": True, "unread": 2})
        self.assertEqual(server.received, [{"op": "status"}])  # one frame, intact

    def test_error_reply_round_trip(self):
        self.serve(lambda frame: session_local.error_reply(session_local.LINK_DOWN,
                                                           "stream down"))
        reply = session_local.call(self.socket_path, {"op": "list"}, timeout=5.0)
        self.assertEqual(reply, {"ok": False, "error": "link_down", "detail": "stream down"})

    def test_utf8_round_trip(self):
        self.serve(lambda frame: session_local.ok_reply(echo=frame.get("body")))
        reply = session_local.call(self.socket_path, {"op": "send", "body": "안녕하세요"},
                                   timeout=5.0)
        self.assertEqual(reply, {"ok": True, "echo": "안녕하세요"})

    def test_missing_socket_raises_oserror(self):
        with self.assertRaises(OSError):
            session_local.call(self.socket_path, {"op": "status"}, timeout=5.0)

    def test_silent_server_times_out(self):
        server = self.serve(lambda frame: None)
        with self.assertRaises(OSError):
            session_local.call(self.socket_path, {"op": "inbox"}, timeout=0.3)
        self.assertEqual(server.received, [{"op": "inbox"}])  # the request did arrive

    def test_non_object_reply_raises_valueerror(self):
        self.serve(lambda frame: ["not", "an", "object"])
        with self.assertRaises(ValueError):
            session_local.call(self.socket_path, {"op": "list"}, timeout=5.0)


if __name__ == "__main__":
    unittest.main()

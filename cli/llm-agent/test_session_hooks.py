"""session_hooks: the CLI hooks of operator sessions (S2 of docs/design/operator-sessions.md,
"Hooks"). Every event is run in-process through main() against a real AF_UNIX fake daemon thread
(one frame per connection, frames recorded, canned replies), so the tests need no subprocess and
still exercise the true socket path, framing and timeouts. The fail-open rule is pinned from
every side: daemon down, a daemon slower than HOOK_CLIENT_TIMEOUT, non-JSON stdin, an unknown
event and skipped children all exit 0 with empty stdout, and stderr stays empty unless
GUARDIAN_SESSION_DEBUG=1 asks for one line. The prompt-submit and stop answers are checked
against session_local's fixed texts, and the planted mail subject and body of a reply must never
reach stdout (rule 8)."""

import io
import json
import os
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import session_hooks
import session_local


class FakeDaemon(threading.Thread):
    """A real AF_UNIX stand-in for the session daemon: one connection at a time, each request
    frame recorded and answered by `reply` (a frame, or a callable of the request frame) after
    `delay` seconds. A reply of None stays silent with the connection open, so the client times
    out instead of seeing EOF."""

    def __init__(self, socket_path, reply, delay=0.0):
        super().__init__(daemon=True, name="session-hooks-test-daemon")
        self.reply = reply
        self.delay = delay
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
                if self.delay:
                    time.sleep(self.delay)
                reply = self.reply(frame) if callable(self.reply) else self.reply
                if reply is None:
                    self._stop.wait(timeout=5.0)  # silent: the client must time out
                else:
                    try:
                        conn.sendall(session_local.encode_frame(reply))
                    except OSError:
                        pass

    def stop(self):
        self._stop.set()
        self.join(timeout=5.0)
        self._sock.close()


class HookCase(unittest.TestCase):
    """A short-lived home under /tmp (the control socket path must fit macOS's 104-byte
    sun_path) plus helpers to serve one fake daemon and run one hook event in-process."""

    def setUp(self):
        tmp = TemporaryDirectory(prefix="sesshook-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.control_socket = session_local.SessionPaths(self.home).control_socket
        session_local.check_socket_path(self.control_socket)  # a clear error if /tmp is deep
        self.control_socket.parent.mkdir(parents=True, mode=0o700)
        self.stderr = io.StringIO()

    def serve(self, reply, delay=0.0):
        daemon = FakeDaemon(self.control_socket, reply, delay=delay)
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon

    def hook(self, event, input_obj=None, env=None, stdin_text=None):
        """Runs one event and returns (exit code, stdout text). input_obj is dumped to stdin as
        JSON unless stdin_text overrides it (for the bad-input cases)."""
        text = stdin_text if stdin_text is not None else json.dumps(input_obj or {})
        stdout = io.StringIO()
        code = session_hooks.main([event], io.StringIO(text), stdout,
                                  env if env is not None else {}, self.home, stderr=self.stderr)
        return code, stdout.getvalue()


class SessionStartTests(HookCase):
    def test_full_register_frame(self):
        daemon = self.serve(session_local.ok_reply())
        env = {"CLAUDE_PID": "4242",
               "TMUX": "/tmp/tmux-501/default,12345,0",
               "TMUX_PANE": "%3",
               "CLAUDE_PROJECT_DIR": "/Users/op/work",
               "GUARDIAN_SESSION_NAME": "cleaner"}
        code, out = self.hook("session-start", {"session_id": "abc-1", "source": "startup"}, env)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")  # SessionStart answers nothing
        frame = {"op": "register", "cli_session": "abc-1", "cli_pid": 4242,
                 "tmux_socket": "/tmp/tmux-501/default", "tmux_pane": "%3",
                 "project_dir": "/Users/op/work", "source": "startup", "name": "cleaner"}
        self.assertEqual(daemon.received, [frame])
        self.assertEqual(session_local.validate_request(frame), [])  # the contract accepts it

    def test_bare_register_frame(self):
        daemon = self.serve(session_local.ok_reply())
        code, out = self.hook("session-start", {"session_id": "s-1"}, {})
        self.assertEqual((code, out), (0, ""))
        frame = daemon.received[0]
        self.assertEqual(frame, {"op": "register", "cli_session": "s-1",
                                 "cli_pid": os.getppid(), "tmux_socket": None, "tmux_pane": None,
                                 "project_dir": None, "source": "unknown", "name": None})
        self.assertEqual(session_local.validate_request(frame), [])

    def test_cli_pid_from_claude_pid_or_parent(self):
        daemon = self.serve(session_local.ok_reply())
        cases = [{"CLAUDE_PID": "4242"}, {"CLAUDE_PID": "007"},
                 {"CLAUDE_PID": "abc"}, {"CLAUDE_PID": ""}, {}]
        for env in cases:
            with self.subTest(env=env):
                code, out = self.hook("session-start", {"session_id": "s-1"}, env)
                self.assertEqual((code, out), (0, ""))
        self.assertEqual([f["cli_pid"] for f in daemon.received],
                         [4242, 7, os.getppid(), os.getppid(), os.getppid()])

    def test_tmux_parsing(self):
        daemon = self.serve(session_local.ok_reply())
        cases = [
            # an absolute first field of $TMUX is the server socket; $TMUX_PANE is the pane
            ({"TMUX": "/tmp/tmux-501/default,12345,0", "TMUX_PANE": "%3"},
             "/tmp/tmux-501/default", "%3"),
            ({"TMUX": "/tmp/tmux-501/default,12345,0", "TMUX_PANE": "%42"},
             "/tmp/tmux-501/default", "%42"),
            # a relative first field is not a socket path: no socket, and so no pane either
            ({"TMUX": "default,12345,0", "TMUX_PANE": "%3"}, None, None),
            # no $TMUX (outside tmux), or an empty one: nothing to wake
            ({"TMUX_PANE": "%3"}, None, None),
            ({"TMUX": "", "TMUX_PANE": "%3"}, None, None),
            # a $TMUX_PANE that is not %digits is not a pane id
            ({"TMUX": "/tmp/t/default,1,0", "TMUX_PANE": "3"}, "/tmp/t/default", None),
            ({"TMUX": "/tmp/t/default,1,0", "TMUX_PANE": "%3x"}, "/tmp/t/default", None),
            ({"TMUX": "/tmp/t/default,1,0", "TMUX_PANE": "%-3"}, "/tmp/t/default", None),
            ({"TMUX": "/tmp/t/default,1,0", "TMUX_PANE": "%٣"}, "/tmp/t/default", None),
            ({"TMUX": "/tmp/t/default,1,0"}, "/tmp/t/default", None),
        ]
        for env, _socket, _pane in cases:
            with self.subTest(env=env):
                code, out = self.hook("session-start", {"session_id": "s-1"}, env)
                self.assertEqual((code, out), (0, ""))
        for (env, wanted_socket, wanted_pane), frame in zip(cases, daemon.received):
            self.assertEqual(frame["tmux_socket"], wanted_socket, env)
            self.assertEqual(frame["tmux_pane"], wanted_pane, env)

    def test_session_name(self):
        daemon = self.serve(session_local.ok_reply())
        cases = [{"GUARDIAN_SESSION_NAME": "cleaner"},
                 {"GUARDIAN_SESSION_NAME": "Cleaner"},  # upper case: not a session name
                 {"GUARDIAN_SESSION_NAME": "-nope"},
                 {"GUARDIAN_SESSION_NAME": ""},
                 {}]
        for env in cases:
            with self.subTest(env=env):
                self.hook("session-start", {"session_id": "s-1"}, env)
        self.assertEqual([f["name"] for f in daemon.received],
                         ["cleaner", None, None, None, None])


class PromptSubmitTests(HookCase):
    def test_documented_frame(self):
        daemon = self.serve(session_local.ok_reply(unread=0))
        # The input's session_id wins over the environment's.
        code, out = self.hook("prompt-submit",
                              {"session_id": "s-1", "prompt": "hi", "permission_mode": "default"},
                              {"CLAUDE_CODE_SESSION_ID": "other"})
        self.assertEqual((code, out), (0, ""))  # unread 0: no context line
        self.assertEqual(daemon.received, [{"op": "prompt_submit", "cli_session": "s-1"}])
        self.assertEqual(session_local.validate_request(daemon.received[0]), [])

    def test_unread_context_line(self):
        replies = [session_local.ok_reply(unread=n, subject="비밀 제목", body="secret turn text")
                   for n in (1, 2, 5)]
        daemon = self.serve(lambda frame: replies.pop(0))
        for unread in (1, 2, 5):
            with self.subTest(unread=unread):
                code, out = self.hook("prompt-submit", {"session_id": "s-1"}, {})
                self.assertEqual(code, 0)
                expected = {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": session_local.unread_context(unread)}}
                self.assertEqual(out, json.dumps(expected) + "\n")  # exactly one JSON line
                self.assertEqual(json.loads(out), expected)
                self.assertNotIn("비밀", out)     # no subject ever reaches the session
                self.assertNotIn("secret", out)   # no body ever reaches the session
        self.assertEqual(len(daemon.received), 3)

    def test_no_context_without_positive_unread(self):
        replies = [session_local.ok_reply(unread=0),
                   session_local.ok_reply(),                    # no unread field at all
                   session_local.ok_reply(unread=-3),
                   session_local.ok_reply(unread=True),         # a bool is not a count
                   session_local.ok_reply(unread="2"),          # text is not a count
                   session_local.error_reply(session_local.LINK_DOWN, "down")]
        queue = list(replies)  # the server pops; the loop needs the cases intact
        daemon = self.serve(lambda frame: queue.pop(0))
        for reply in replies:
            with self.subTest(reply=reply):
                code, out = self.hook("prompt-submit", {"session_id": "s-1"}, {})
                self.assertEqual((code, out), (0, ""))
        self.assertEqual(len(daemon.received), len(replies))  # every run still told the daemon


class StopTests(HookCase):
    def test_documented_frame(self):
        daemon = self.serve(session_local.ok_reply(block=False))
        code, out = self.hook("stop", {"session_id": "s-1", "last_assistant_message": "done"}, {})
        self.assertEqual((code, out), (0, ""))  # block false: the turn just ends
        self.assertEqual(daemon.received, [{"op": "stop", "cli_session": "s-1"}])
        self.assertEqual(session_local.validate_request(daemon.received[0]), [])

    def test_block_only_when_the_daemon_blocks(self):
        replies = [session_local.ok_reply(block=True),
                   session_local.ok_reply(block=False),
                   session_local.ok_reply(),                 # no block field
                   session_local.ok_reply(block="yes"),      # only JSON true blocks
                   session_local.error_reply(session_local.LINK_DOWN)]
        queue = list(replies)  # the server pops; the loop needs the case count intact
        daemon = self.serve(lambda frame: queue.pop(0))
        expected_line = json.dumps({"decision": "block",
                                    "reason": session_local.STOP_REASON}) + "\n"
        outs = []
        for _ in replies:
            code, out = self.hook("stop", {"session_id": "s-1"}, {})
            self.assertEqual(code, 0)
            outs.append(out)
        self.assertEqual(outs, [expected_line, "", "", "", ""])

    def test_stop_hook_active_never_blocks(self):
        daemon = self.serve(session_local.ok_reply(block=True))
        code, out = self.hook("stop", {"session_id": "s-1", "stop_hook_active": True}, {})
        self.assertEqual((code, out), (0, ""))  # the loop guard swallows even a block answer
        # The stop op still went out, so the daemon ends the turn.
        self.assertEqual(daemon.received, [{"op": "stop", "cli_session": "s-1"}])


class SessionEndTests(HookCase):
    def test_documented_frame(self):
        daemon = self.serve(session_local.ok_reply())
        code, out = self.hook("session-end",
                              {"session_id": "s-1", "reason": "prompt_input_exit"}, {})
        self.assertEqual((code, out), (0, ""))  # SessionEnd answers nothing
        frame = {"op": "end", "cli_session": "s-1", "reason": "prompt_input_exit"}
        self.assertEqual(daemon.received, [frame])
        self.assertEqual(session_local.validate_request(frame), [])

    def test_reason_defaults_to_unknown(self):
        daemon = self.serve(session_local.ok_reply())
        inputs = [{"session_id": "s-1"},
                  {"session_id": "s-1", "reason": ""},
                  {"session_id": "s-1", "reason": None},
                  {"session_id": "s-1", "reason": 5}]
        for data in inputs:
            with self.subTest(data=data):
                code, out = self.hook("session-end", data, {})
                self.assertEqual((code, out), (0, ""))
        self.assertEqual([f["reason"] for f in daemon.received], ["unknown"] * len(inputs))


class CliSessionTests(HookCase):
    def test_session_id_from_env_when_input_has_none(self):
        daemon = self.serve(session_local.ok_reply())
        cases = [{}, {"session_id": ""}, {"session_id": 5}]  # no usable input session id
        for data in cases:
            with self.subTest(data=data):
                code, out = self.hook("prompt-submit", data, {"CLAUDE_CODE_SESSION_ID": "env-s"})
                self.assertEqual((code, out), (0, ""))
        self.assertEqual([f["cli_session"] for f in daemon.received], ["env-s"] * 3)

    def test_no_session_id_anywhere_sends_nothing(self):
        daemon = self.serve(session_local.ok_reply(unread=9))
        code, out = self.hook("prompt-submit", {"prompt": "hi"}, {"CLAUDE_CODE_SESSION_ID": ""})
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(daemon.received, [])  # not even a connection


class SkipTests(HookCase):
    def test_children_are_skipped_whole(self):
        daemon = self.serve(session_local.ok_reply(unread=9, block=True))
        cases = [{"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"},       # a --print child (S0 correction 5)
                 {"GUARDIAN_SESSION_CHILD": "1"},             # the summary child of SD6a
                 {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "GUARDIAN_SESSION_CHILD": "1",
                  "CLAUDE_CODE_SESSION_ID": "s-1"}]
        for env in cases:
            for event in ("session-start", "prompt-submit", "stop", "session-end"):
                with self.subTest(env=env, event=event):
                    code, out = self.hook(event, {"session_id": "s-1"}, env)
                    self.assertEqual((code, out), (0, ""))
        self.assertEqual(daemon.received, [])  # no daemon call at all


class FailOpenTests(HookCase):
    def test_daemon_down(self):
        # No server runs: the control socket does not exist.
        for event in ("session-start", "prompt-submit", "stop", "session-end"):
            with self.subTest(event=event):
                code, out = self.hook(event, {"session_id": "s-1"}, {})
                self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.stderr.getvalue(), "")

    def test_unknown_event(self):
        daemon = self.serve(session_local.ok_reply())
        for argv in ([], ["pre-tool-use"], ["SessionStart"]):  # not a wired event name
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                code = session_hooks.main(argv, io.StringIO('{"session_id": "s-1"}'), stdout,
                                          {}, self.home, stderr=self.stderr)
                self.assertEqual(code, 0)
                self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(daemon.received, [])

    def test_bad_stdin(self):
        daemon = self.serve(session_local.ok_reply())
        for text in ("not json", "", "[1, 2]", "null"):  # not one JSON object
            with self.subTest(text=text):
                code, out = self.hook("session-start", stdin_text=text)
                self.assertEqual((code, out), (0, ""))
        self.assertEqual(daemon.received, [])

    def test_slow_daemon_times_out(self):
        # The daemon answers, but only after 1.0 s; the patched 0.2 s HOOK_CLIENT_TIMEOUT gives
        # up first, and the hook still exits 0 with no stdout.
        daemon = self.serve(session_local.ok_reply(unread=4, block=True), delay=1.0)
        with patch.object(session_local, "HOOK_CLIENT_TIMEOUT", 0.2):
            code, out = self.hook("prompt-submit", {"session_id": "s-1"}, {})
            self.assertEqual((code, out), (0, ""))
            code, out = self.hook("stop", {"session_id": "s-1"}, {})
            self.assertEqual((code, out), (0, ""))
        # The server handles one connection at a time and sleeps before each reply, so the second
        # frame is read only after the first delay; wait for it before stopping the server.
        deadline = time.monotonic() + 5.0
        while len(daemon.received) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        daemon.stop()
        self.assertEqual(daemon.received, [{"op": "prompt_submit", "cli_session": "s-1"},
                                           {"op": "stop", "cli_session": "s-1"}])


class DebugLineTests(HookCase):
    def test_stderr_silent_without_debug(self):
        self.hook("prompt-submit", {"session_id": "s-1"}, {})  # daemon down, debug off
        self.assertEqual(self.stderr.getvalue(), "")  # not a byte

    def test_one_short_line_with_debug(self):
        env = {"GUARDIAN_SESSION_DEBUG": "1"}
        self.hook("prompt-submit", {"session_id": "s-1"}, env)  # daemon down, debug on
        line = self.stderr.getvalue()
        self.assertEqual(line.count("\n"), 1)  # exactly one line
        self.assertTrue(line.endswith("\n"))
        self.assertIn("session_hooks:", line)


class AgyHookTests(HookCase):
    def test_agy_session_start_registers_once_and_skips_when_socket_exists(self):
        daemon = self.serve(session_local.ok_reply())
        env = {"GUARDIAN_AGY_PID": "5150", "GUARDIAN_SESSION_NAME": "agy-main"}
        code, out = self.hook("agy-session-start",
                              {"conversationId": "c-1", "cwd": "/work/app"}, env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {})
        self.assertEqual(daemon.received, [{
            "op": "register", "cli_session": "agy-5150", "cli_pid": 5150,
            "tmux_socket": None, "tmux_pane": None, "project_dir": "/work/app",
            "source": "agy", "name": "agy-main"}])
        self.assertEqual(session_local.validate_request(daemon.received[0]), [])

        # Once the session socket exists, a second SessionStart (/new) must not re-register.
        sock = session_local.SessionPaths(self.home).session_socket("agy-5150")
        sock.touch()
        code, out = self.hook("agy-session-start",
                              {"conversationId": "c-2", "cwd": "/work/app"}, env)
        self.assertEqual((code, json.loads(out)), (0, {}))
        self.assertEqual(len(daemon.received), 1)

    def test_agy_pre_invocation_injects_unread_only_on_first_invocation_and_auto_registers(self):
        calls = []

        def reply(frame):
            calls.append(frame)
            if frame["op"] == "prompt_submit" and len(calls) == 1:
                return session_local.error_reply(session_local.NOT_REGISTERED, "missing")
            if frame["op"] == "register":
                return session_local.ok_reply()
            return session_local.ok_reply(unread=2, subject="SECRET-SUBJ", body="SECRET-BODY")

        self.serve(reply)
        env = {"GUARDIAN_AGY_PID": "5151"}
        code, out = self.hook("agy-pre-invocation",
                              {"conversationId": "c-1", "invocationNum": 0, "cwd": "/work"}, env)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload, {"injectSteps": [{"systemMessage": {
            "systemMessage": session_local.unread_context(2)}}]})
        self.assertNotIn("SECRET", out)
        self.assertEqual([c["op"] for c in calls], ["prompt_submit", "register", "prompt_submit"])

        # Later tool-loop invocations (invocationNum > 0) emit {} without touching the daemon.
        code, out = self.hook("agy-pre-invocation",
                              {"conversationId": "c-1", "invocationNum": 2}, env)
        self.assertEqual((code, json.loads(out)), (0, {}))
        self.assertEqual(len(calls), 3)

    def test_agy_stop_continues_once_and_respects_guards(self):
        self.serve(session_local.ok_reply(block=True))
        env = {"GUARDIAN_AGY_PID": "5152"}
        code, out = self.hook("agy-stop", {
            "conversationId": "c-1", "continuationActive": False,
            "terminationReason": "NO_TOOL_CALL"}, env)
        self.assertEqual((code, json.loads(out)),
                         (0, {"decision": "continue", "reason": session_local.STOP_REASON}))

        # continuationActive=True or non-normal terminationReason must not force-continue.
        for data in (
            {"conversationId": "c-1", "continuationActive": True,
             "terminationReason": "NO_TOOL_CALL"},
            {"conversationId": "c-1", "continuationActive": False,
             "terminationReason": "USER_CANCELED"},
        ):
            code, out = self.hook("agy-stop", data, env)
            self.assertEqual((code, json.loads(out)), (0, {}))

    def test_agy_hooks_fail_open_with_empty_json_when_daemon_is_down(self):
        for event in ("agy-session-start", "agy-pre-invocation", "agy-stop"):
            code, out = self.hook(event, {"conversationId": "c-1"}, {"GUARDIAN_AGY_PID": "5153"})
            self.assertEqual((code, json.loads(out)), (0, {}))


if __name__ == "__main__":
    unittest.main()

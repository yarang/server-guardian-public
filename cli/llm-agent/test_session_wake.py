"""session_wake: the tmux wake guard of S2 (docs/design/operator-sessions.md, "Wake"). The pure
pieces are checked against fixture strings and tables: the empty-input-line rule of S0
correction 3 (pane_input_empty, on capture-pane -e -p fixtures with real SGR text),
clients_quiet (guard 4, correction 4) and wake_policy (guards 1, 2 and 6). The Waker runs
behind a fake run() that records every argv list and kwargs and returns canned
CompletedProcess objects, so no test starts a real tmux or ps. One test collects every
send-keys argv across many wakes and asserts the only literal text ever sent is
session_local.WAKE_LINE."""

import inspect
import os
import subprocess
import unittest
from unittest.mock import patch

import session_local
import session_wake

ESC = "\x1b"
NBSP = " "
PROMPT = ESC + "[39m❯" + NBSP          # the CLI's empty input line (S0 correction 3)
HISTORY = ESC + "[48;5;237m"           # the background of a submitted prompt in the history
SOCKET = "/tmp/tmux-1000/default"
NOW = 1_000_000.0
PANE_PID = 4242


def done(rc=0, stdout=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


def ps_table(parents):
    """A ps answer where each queried pid prints the ppid the table maps it to (argv[4])."""
    def answer(argv):
        return done(0, stdout=parents[argv[4]] + "\n")
    return answer


class FakeRun:
    """A run() double: records every (argv, kwargs) it is called with and answers from a
    per-subcommand mapping, keyed by the tmux subcommand (argv[3]) or 'ps'. A value may be a
    CompletedProcess or a callable argv -> CompletedProcess; anything unmapped answers
    rc 0 with empty output."""

    def __init__(self, mapping=None):
        self.calls = []
        self.mapping = dict(mapping or {})

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        key = "ps" if argv[0] == "ps" else argv[3]
        value = self.mapping.get(key, done())
        return value(argv) if callable(value) else value


def happy_mapping(clients_stdout=None):
    """Answers a wake that passes every guard: a live pane running the CLI (pid == PANE_PID, so
    no ps walk), a quiet client, an empty input line and two good sends."""
    if clients_stdout is None:
        clients_stdout = f"{int(NOW - session_local.INPUT_QUIET - 1)}\n"
    return {
        "display-message": done(0, stdout=f"0 {PANE_PID}\n"),
        "list-clients": done(0, stdout=clients_stdout),
        "capture-pane": done(0, stdout=PROMPT),
        "send-keys": done(0),
    }


def target(pid=PANE_PID, pane="%3", socket=SOCKET):
    return session_wake.WakeTarget(pane, socket, pid)


class PaneInputEmptyTests(unittest.TestCase):
    """Guard 5 on capture fixtures; every fixture is the SGR text capture-pane -e -p prints."""

    def test_module_pins_the_pattern_characters(self):
        # The U+00A0 literal is invisible in source; this pin makes a mangled copy fail loudly.
        self.assertEqual(session_wake.PROMPT_CHAR, chr(0x276F))
        self.assertEqual(session_wake.NBSP, " ")

    def test_empty_prompt(self):
        self.assertTrue(session_wake.pane_input_empty(PROMPT))

    def test_empty_prompt_with_dim_suggestion(self):
        ghost = ESC + "[2mfly CI on three runners" + ESC + "[22m"
        self.assertTrue(session_wake.pane_input_empty(PROMPT + ghost))
        self.assertTrue(session_wake.pane_input_empty(PROMPT + ghost + ESC + "[0m"))

    def test_typed_text(self):
        self.assertFalse(session_wake.pane_input_empty(PROMPT + "fix the log"))

    def test_typed_text_after_a_dim_run(self):
        capture = PROMPT + ESC + "[2mfly " + ESC + "[22m" + "deploy"
        self.assertFalse(session_wake.pane_input_empty(capture))

    def test_history_line_above_an_empty_prompt(self):
        capture = HISTORY + "❯" + NBSP + "old command" + ESC + "[0m\n" + PROMPT
        self.assertTrue(session_wake.pane_input_empty(capture))

    def test_history_line_only(self):
        capture = HISTORY + "❯" + NBSP + "old command" + ESC + "[0m\n"
        self.assertFalse(session_wake.pane_input_empty(capture))

    def test_history_background_colon_form(self):
        # 48:5:237 in one token draws the same history background.
        capture = ESC + "[48:5:237m❯" + NBSP + "old\n"
        self.assertFalse(session_wake.pane_input_empty(capture))

    def test_no_prompt(self):
        self.assertFalse(session_wake.pane_input_empty("all clean, nothing to see\n"))

    def test_plain_space_after_the_prompt_character(self):
        self.assertFalse(session_wake.pane_input_empty(ESC + "[39m❯ fix the log"))
        self.assertFalse(session_wake.pane_input_empty(ESC + "[39m❯ "))

    def test_prompt_character_at_line_end(self):
        self.assertFalse(session_wake.pane_input_empty(ESC + "[39m❯"))

    def test_spaces_after_the_nbsp_stay_empty(self):
        self.assertTrue(session_wake.pane_input_empty(PROMPT + "   "))

    def test_combined_sgr_parameters(self):
        # "0;2" resets then dims and "2;39" dims with a default foreground: ghost text either way.
        self.assertTrue(session_wake.pane_input_empty(PROMPT + ESC + "[0;2mghost" + ESC + "[0m"))
        self.assertTrue(session_wake.pane_input_empty(PROMPT + ESC + "[2;39mghost" + ESC + "[22m"))
        # 22 inside a combined parameter turns dim off, so what follows is typed text.
        capture = PROMPT + ESC + "[2;39mgh" + ESC + "[22;39most"
        self.assertFalse(session_wake.pane_input_empty(capture))

    def test_last_non_history_prompt_line_wins(self):
        typed = ESC + "[39m❯" + NBSP + "typed earlier"
        history = HISTORY + "❯" + NBSP + "submitted" + ESC + "[0m\n"
        self.assertTrue(session_wake.pane_input_empty(typed + "\n" + history + PROMPT))
        self.assertFalse(session_wake.pane_input_empty(PROMPT + "\n" + typed))

    def test_other_escape_sequences_are_skipped(self):
        capture = ESC + "]0;a window title\x07" + PROMPT
        self.assertTrue(session_wake.pane_input_empty(capture))
        capture = ESC + "]0;a window title" + ESC + "\\" + PROMPT
        self.assertTrue(session_wake.pane_input_empty(capture))


class ClientsQuietTests(unittest.TestCase):
    """Guard 4 on list-clients output (S0 correction 4: the largest value wins)."""

    def test_largest_value_wins(self):
        out = f"{int(NOW - 90)}\n{int(NOW - 61)}\n{int(NOW - 300)}\n"
        self.assertTrue(session_wake.clients_quiet(out, NOW, quiet=60))
        out = f"{int(NOW - 90)}\n{int(NOW - 59)}\n"
        self.assertFalse(session_wake.clients_quiet(out, NOW, quiet=60))

    def test_quiet_at_the_boundary(self):
        self.assertTrue(session_wake.clients_quiet(f"{int(NOW - 60)}\n", NOW, quiet=60))

    def test_empty_output_fails_closed_by_default(self):
        self.assertFalse(session_wake.clients_quiet("", NOW))
        self.assertFalse(session_wake.clients_quiet("  \n", NOW))

    def test_empty_output_passes_when_allowed(self):
        self.assertTrue(session_wake.clients_quiet("", NOW, allow_no_client=True))
        self.assertTrue(session_wake.clients_quiet("\n", NOW, allow_no_client=True))

    def test_garbage_line_is_not_quiet(self):
        self.assertFalse(session_wake.clients_quiet("soon\n", NOW))
        self.assertFalse(session_wake.clients_quiet("yesterday\nnot-a-time\n", NOW))
        self.assertFalse(session_wake.clients_quiet("1.5\n", NOW))

    def test_default_quiet_is_the_contract_value(self):
        quiet = session_local.INPUT_QUIET
        self.assertFalse(session_wake.clients_quiet(f"{int(NOW - quiet + 1)}\n", NOW))
        self.assertTrue(session_wake.clients_quiet(f"{int(NOW - quiet)}\n", NOW))


class WakePolicyTests(unittest.TestCase):
    """Guards 1, 2 and 6, pure: the reasons are returned in the design's check order."""

    INTERVAL = session_local.WAKE_INTERVAL  # pinned: the policy must use the contract value

    def policy(self, **overrides):
        args = dict(from_kind="operator", state="idle", away=False, last_wake=None,
                    inbox_since_wake=False, now=NOW)
        args.update(overrides)
        return session_wake.wake_policy(**args)

    def test_ok(self):
        self.assertEqual(self.policy(), (True, "ok"))

    def test_only_operator_mail_wakes(self):
        self.assertEqual(self.policy(from_kind="session"), (False, "not_operator"))
        self.assertEqual(self.policy(from_kind="agent"), (False, "not_operator"))

    def test_only_idle_sessions(self):
        self.assertEqual(self.policy(state="busy"), (False, "not_idle"))
        self.assertEqual(self.policy(state="linked"), (False, "not_idle"))
        self.assertEqual(self.policy(state="offline"), (False, "not_idle"))

    def test_away_blocks(self):
        self.assertEqual(self.policy(away=True), (False, "away"))

    def test_one_wake_per_interval_without_an_inbox_call(self):
        self.assertEqual(self.policy(last_wake=NOW - self.INTERVAL + 1),
                         (False, "recent_wake"))
        self.assertEqual(self.policy(last_wake=NOW - self.INTERVAL), (True, "ok"))
        self.assertEqual(self.policy(last_wake=NOW), (False, "recent_wake"))

    def test_an_inbox_call_allows_the_next_wake(self):
        last = NOW - self.INTERVAL + 1
        self.assertEqual(self.policy(last_wake=last, inbox_since_wake=True), (True, "ok"))

    def test_reason_order(self):
        # from_kind, then state, then away, then the interval: the first refusal names itself.
        self.assertEqual(self.policy(from_kind="session", state="busy", away=True,
                                     last_wake=NOW), (False, "not_operator"))
        self.assertEqual(self.policy(state="busy", away=True, last_wake=NOW),
                         (False, "not_idle"))
        self.assertEqual(self.policy(away=True, last_wake=NOW), (False, "away"))


class WakeTargetTests(unittest.TestCase):
    """A bad target is refused before any subprocess runs."""

    def refused(self, bad_target):
        run = FakeRun()
        result = session_wake.Waker(run=run, clock=lambda: NOW).try_wake(bad_target)
        self.assertEqual(result, (False, "bad_target"))
        self.assertEqual(run.calls, [])

    def test_bad_pane(self):
        for pane in ("3", "%", "%x", "%-1", "%3 ", 3, None):
            self.refused(session_wake.WakeTarget(pane, SOCKET, 500))

    def test_bad_socket(self):
        for socket in ("tmp/sock", "", None, "sock\x00name", 5):
            self.refused(session_wake.WakeTarget("%3", socket, 500))

    def test_bad_pid(self):
        for pid in (0, 1, -5, True, "500", None):
            self.refused(session_wake.WakeTarget("%3", SOCKET, pid))

    def test_a_good_target_reaches_guard_3(self):
        run = FakeRun()
        result = session_wake.Waker(run=run, clock=lambda: NOW).try_wake(target())
        self.assertNotEqual(result[1], "bad_target")
        self.assertEqual(len(run.calls), 1)  # display-message ran, guard 3 answered empty


class PaneRunsCliTests(unittest.TestCase):
    """Guard 3: the pane still exists and still runs the recorded CLI process."""

    def waker(self, mapping):
        run = FakeRun(mapping)
        return session_wake.Waker(run=run, clock=lambda: NOW), run

    def test_pane_pid_is_cli_pid(self):
        waker, run = self.waker({"display-message": done(0, stdout=f"0 {PANE_PID}\n")})
        self.assertTrue(waker.pane_runs_cli(target(PANE_PID)))
        self.assertEqual(len(run.calls), 1)  # no ps walk when the pid matches outright

    def test_parent_chain_hit(self):
        waker, run = self.waker({
            "display-message": done(0, stdout=f"0 {PANE_PID}\n"),
            "ps": ps_table({"500": "4300", "4300": str(PANE_PID)}),
        })
        self.assertTrue(waker.pane_runs_cli(target(500)))
        self.assertEqual([argv for argv, _ in run.calls], [
            ["tmux", "-S", SOCKET, "display-message", "-p", "-t", "%3",
             "#{pane_dead} #{pane_pid}"],
            ["ps", "-o", "ppid=", "-p", "500"],
            ["ps", "-o", "ppid=", "-p", "4300"],
        ])

    def test_chain_miss_ends_at_init(self):
        waker, run = self.waker({
            "display-message": done(0, stdout=f"0 {PANE_PID}\n"),
            "ps": ps_table({"500": "4300", "4300": "1"}),
        })
        self.assertFalse(waker.pane_runs_cli(target(500)))
        self.assertEqual(len(run.calls), 3)

    def test_walk_is_capped_at_eight_hops(self):
        def climb(argv):  # a parent chain that never reaches the pane pid
            return done(0, stdout=str(int(argv[4]) + 1) + "\n")
        waker, run = self.waker({
            "display-message": done(0, stdout=f"0 {PANE_PID}\n"),
            "ps": climb,
        })
        self.assertFalse(waker.pane_runs_cli(target(500)))
        ps_calls = [argv for argv, _ in run.calls if argv[0] == "ps"]
        self.assertEqual(len(ps_calls), session_wake.PARENT_HOPS)

    def test_dead_pane(self):
        waker, run = self.waker({"display-message": done(0, stdout=f"1 {PANE_PID}\n")})
        self.assertFalse(waker.pane_runs_cli(target(PANE_PID)))
        self.assertEqual(len(run.calls), 1)

    def test_display_error_or_garbage(self):
        for rc, stdout in ((1, ""), (0, ""), (0, "nonsense"), (0, "0 notapid"),
                           (0, "0 42 43")):
            waker, _ = self.waker({"display-message": done(rc, stdout=stdout)})
            self.assertFalse(waker.pane_runs_cli(target(500)), (rc, stdout))

    def test_ps_error(self):
        waker, run = self.waker({
            "display-message": done(0, stdout=f"0 {PANE_PID}\n"),
            "ps": done(1),
        })
        self.assertFalse(waker.pane_runs_cli(target(500)))
        self.assertEqual(len(run.calls), 2)

    def test_ps_garbage(self):
        waker, _ = self.waker({
            "display-message": done(0, stdout=f"0 {PANE_PID}\n"),
            "ps": done(0, stdout="parent\n"),
        })
        self.assertFalse(waker.pane_runs_cli(target(500)))


class TryWakeTests(unittest.TestCase):
    """The guard chain and the send: order, reasons, exact argv lists and run() kwargs."""

    def waker(self, mapping=None, **kwargs):
        run = FakeRun(mapping)
        return session_wake.Waker(run=run, clock=lambda: NOW, **kwargs), run

    def argvs(self, run):
        return [argv for argv, _ in run.calls]

    def test_woken(self):
        waker, run = self.waker(happy_mapping())
        self.assertEqual(waker.try_wake(target()), (True, "woken"))
        self.assertEqual(self.argvs(run), [
            ["tmux", "-S", SOCKET, "display-message", "-p", "-t", "%3",
             "#{pane_dead} #{pane_pid}"],
            ["tmux", "-S", SOCKET, "list-clients", "-t", "%3", "-F", "#{client_activity}"],
            ["tmux", "-S", SOCKET, "capture-pane", "-e", "-p", "-t", "%3"],
            ["tmux", "-S", SOCKET, "send-keys", "-t", "%3", "-l", session_local.WAKE_LINE],
            ["tmux", "-S", SOCKET, "send-keys", "-t", "%3", "Enter"],
        ])

    def test_guard_3_refuses_before_guard_4(self):
        mapping = happy_mapping(f"{int(NOW)}\n")  # a client typing right now
        mapping["display-message"] = done(0, stdout=f"1 {PANE_PID}\n")  # ...but the pane is dead
        waker, run = self.waker(mapping)
        self.assertEqual(waker.try_wake(target()), (False, "pane_changed"))
        self.assertEqual(len(run.calls), 1)

    def test_client_active(self):
        waker, run = self.waker(happy_mapping(f"{int(NOW - 10)}\n"))
        self.assertEqual(waker.try_wake(target()), (False, "client_active"))
        self.assertEqual(len(run.calls), 2)

    def test_guard_4_refuses_before_guard_5(self):
        mapping = happy_mapping(f"{int(NOW - 10)}\n")
        mapping["capture-pane"] = done(0, stdout=PROMPT + "typed")
        waker, run = self.waker(mapping)
        self.assertEqual(waker.try_wake(target()), (False, "client_active"))
        self.assertEqual(len(run.calls), 2)  # the capture never runs

    def test_list_clients_error(self):
        mapping = happy_mapping()
        mapping["list-clients"] = done(1)
        waker, _ = self.waker(mapping)
        self.assertEqual(waker.try_wake(target()), (False, "client_active"))

    def test_input_not_empty(self):
        mapping = happy_mapping()
        mapping["capture-pane"] = done(0, stdout=PROMPT + "half-typed")
        waker, run = self.waker(mapping)
        self.assertEqual(waker.try_wake(target()), (False, "input_not_empty"))
        self.assertEqual(len(run.calls), 3)

    def test_capture_error(self):
        mapping = happy_mapping()
        mapping["capture-pane"] = done(1)
        waker, _ = self.waker(mapping)
        self.assertEqual(waker.try_wake(target()), (False, "input_not_empty"))

    def test_send_failed(self):
        for fail_on, calls_expected in ((1, 4), (2, 5)):  # the text send, then Enter
            seen = {"n": 0}

            def send(argv, fail_on=fail_on, seen=seen):
                seen["n"] += 1
                return done(1 if seen["n"] == fail_on else 0)
            mapping = happy_mapping()
            mapping["send-keys"] = send
            waker, run = self.waker(mapping)
            self.assertEqual(waker.try_wake(target()), (False, "send_failed"), fail_on)
            self.assertEqual(len(run.calls), calls_expected, fail_on)

    def test_no_client_blocks_by_default(self):
        waker, run = self.waker(happy_mapping(""))
        self.assertEqual(waker.try_wake(target()), (False, "client_active"))
        self.assertEqual(len(run.calls), 2)

    def test_no_client_passes_when_allowed(self):
        waker, run = self.waker(happy_mapping(""), allow_no_client=True)
        self.assertEqual(waker.try_wake(target()), (True, "woken"))
        self.assertEqual(len(run.calls), 5)

    def test_list_argv_timeout_and_scrubbed_env(self):
        base = {"PATH": "/usr/bin:/bin", "HOME": "/home/op"}
        with patch.dict(os.environ, base, clear=True):  # LANG unset: forced to C.UTF-8
            run = FakeRun(happy_mapping())
            session_wake.Waker(run=run, clock=lambda: NOW).try_wake(target())
        expected_env = dict(base, LANG="C.UTF-8")
        for argv, kwargs in run.calls:
            self.assertIsInstance(argv, list)
            self.assertEqual(kwargs["timeout"], 5)
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["env"], expected_env)
            self.assertNotIn("shell", kwargs)
        with patch.dict(os.environ, dict(base, LANG="ko_KR.UTF-8"), clear=True):
            run = FakeRun(happy_mapping())
            session_wake.Waker(run=run, clock=lambda: NOW).try_wake(target())
        self.assertEqual(run.calls[0][1]["env"], dict(base, LANG="ko_KR.UTF-8"))

    def test_custom_tmux_path_is_used(self):
        run = FakeRun(happy_mapping())
        session_wake.Waker("/opt/bin/tmux", run=run, clock=lambda: NOW).try_wake(target())
        self.assertEqual(run.calls[0][0][0], "/opt/bin/tmux")
        self.assertEqual(run.calls[0][0][1:4], ["-S", SOCKET, "display-message"])


class WakeLineOnlyTests(unittest.TestCase):
    """The one thing the wake ever types is session_local.WAKE_LINE (design "Wake")."""

    def test_only_the_wake_line_is_ever_typed(self):
        scenarios = [
            happy_mapping(),                            # a clean wake
            happy_mapping(f"{int(NOW - 3600)}\n"),      # another quiet client gap
            happy_mapping(""),                          # no client: blocked, nothing typed
            happy_mapping(),                            # a clean wake again
        ]
        sent = []
        for mapping in scenarios:
            run = FakeRun(mapping)
            session_wake.Waker(run=run, clock=lambda: NOW).try_wake(target())
            sent.extend(argv for argv, _ in run.calls if "send-keys" in argv)
        self.assertGreater(len(sent), 0)
        for argv in sent:
            if "-l" in argv:  # the literal-text send: WAKE_LINE, nothing else, ever
                self.assertEqual(argv, ["tmux", "-S", SOCKET, "send-keys", "-t", "%3", "-l",
                                        session_local.WAKE_LINE])
            else:  # the Enter key press, a separate call
                self.assertEqual(argv, ["tmux", "-S", SOCKET, "send-keys", "-t", "%3",
                                        "Enter"])
        typed_literals = {argv[-1] for argv in sent if "-l" in argv}
        self.assertEqual(typed_literals, {session_local.WAKE_LINE})

    def test_try_wake_takes_no_text_argument(self):
        params = tuple(inspect.signature(session_wake.Waker.try_wake).parameters)
        self.assertEqual(params, ("self", "target"))


if __name__ == "__main__":
    unittest.main()

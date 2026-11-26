"""sandbox_probe: the failure rules of the O1 sandbox probe (docs/design/resident-open-tools.md).

One test per rule family over a full passing line set, plus the stress-skipped path, the record
shape and the CLI exit codes. No network, no node: everything is line evaluation."""

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sandbox_probe as sp

HERE = Path(__file__).resolve().parent

PASSING = """\
sandbox_version=abc123def456
stress=ran
limits.PROBE_TASKS_MAX=64
run1.exit=0
id.uid=900
id.user=gsa-probe
id.groups=gsa-probe
sudo=refused
path.0.name=/home/node-user
path.0.state=hidden
path.1.name=/home/node-user/.guardian/llm.env
path.1.state=denied
path.2.name=/root
path.2.state=denied
write.home=ok
write.own_sock_dir=EACCES
write.0.name=/etc
write.0.state=EROFS
write.1.name=/usr/local
write.1.state=EROFS
write.tmp=private_ok
tcp.0.target=127.0.0.1:22
tcp.0.state=denied
tcp.1.target=100.64.0.10:22
tcp.1.state=denied
dns=ok
egress.api.anthropic.com=ok
egress.api.openai.com=ok
unix.0.path=/run/systemd/journal/socket
unix.0.state=connected
unix.1.path=/run/systemd/resolve/io.systemd.Resolve
unix.1.state=connected
unix.2.path=/run/tailscaled/tailscaled.sock
unix.2.state=denied
abstract.0.name=@com.example
abstract.0.state=refused
own_sock=ok
other_sock=denied
worker_sock=hidden
proc.foreign_visible=0
persist.daemon=spawned
persist.user_unit=refused
persist.crontab=refused
persist.leftover=0
hog.exit=137
hog.result=memory_error
hog.mib=240
hog.node_ms=15
hog.leftover=0
forks.exit=0
forks.capped_at=63
forks.errno=EAGAIN
forks.node_ms=18
forks.leftover=0
launcher.exit=3
launcher.result=no_credential
stop.started=1
stop.exit=0
stop.active=none
workload.exit=0
workload.rc=0
workload.seconds=100
workload.tail=OK
workload.memory_peak_mib=512
checks.done=1
probe.done=1
"""

BUSY = """\
sandbox_version=abc123def456
stress=skipped_busy
run1.exit=0
id.user=gsa-probe
id.groups=gsa-probe
sudo=refused
path.0.name=/root
path.0.state=denied
write.home=ok
write.own_sock_dir=EACCES
write.0.name=/etc
write.0.state=EROFS
tcp.0.target=100.64.0.10:22
tcp.0.state=denied
dns=ok
egress.api.anthropic.com=ok
egress.api.openai.com=ok
unix.0.path=/run/tailscaled/tailscaled.sock
unix.0.state=denied
own_sock=ok
other_sock=denied
worker_sock=hidden
proc.foreign_visible=0
persist.user_unit=refused
persist.crontab=refused
persist.leftover=0
launcher.exit=3
launcher.result=cli_missing
stop.started=1
stop.exit=0
stop.active=none
workload=skipped_busy
checks.done=1
probe.done=1
"""


def evaluate(text):
    return sp.evaluate(sp.parse_lines(text))


def flipped(text, needle):
    """The one failure the edited line set produces (asserting the rule is about that needle)."""
    verdict = evaluate(text)
    failures = [f for f in verdict["failures"] if needle in f]
    return verdict, failures


class Parse(unittest.TestCase):
    def test_last_value_wins_and_lines_without_equal_are_ignored(self):
        kv = sp.parse_lines("noise\na=1\nb=x\na=2\n")
        self.assertEqual(kv, {"a": "2", "b": "x"})


class Passing(unittest.TestCase):
    def test_full_passing_set(self):
        verdict = evaluate(PASSING)
        self.assertEqual(verdict["failures"], [])
        self.assertEqual(verdict["warnings"], [])
        self.assertTrue(verdict["passed"])
        self.assertTrue(verdict["finished"])
        self.assertEqual(verdict["version"], "abc123def456")
        self.assertEqual(verdict["stress"], "ran")
        self.assertEqual(verdict["memory_peak_mib"], 512)

    def test_allowed_journal_and_resolver_sockets_do_not_fail(self):
        verdict = evaluate(PASSING)
        self.assertFalse(any("unix" in f for f in verdict["failures"]))


class FailureRules(unittest.TestCase):
    def test_sudo_allowed(self):
        verdict, failures = flipped(PASSING.replace("sudo=refused", "sudo=allowed"), "sudo")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_extra_group(self):
        verdict, failures = flipped(PASSING.replace("id.groups=gsa-probe", "id.groups=gsa-probe sudo"), "groups")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_path_readable(self):
        verdict, failures = flipped(PASSING.replace("path.0.state=hidden", "path.0.state=readable"), "/home/node-user")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_write_home_refused(self):
        verdict, failures = flipped(PASSING.replace("write.home=ok", "write.home=EACCES"), "write.home")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_own_sock_dir_writable_fails(self):
        # O2: the daemon owns the sock dir; an agent that can write there could swap the socket.
        verdict, failures = flipped(PASSING.replace("write.own_sock_dir=EACCES", "write.own_sock_dir=ok"),
                                    "write.own_sock_dir")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_write_outside_ok(self):
        verdict, failures = flipped(PASSING.replace("write.0.state=EROFS", "write.0.state=ok"), "/etc")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_tcp_connected(self):
        verdict, failures = flipped(PASSING.replace("tcp.1.state=denied", "tcp.1.state=connected"), "100.64.0.10")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_dns_broken(self):
        verdict, failures = flipped(PASSING.replace("dns=ok", "dns=ETIMEOUT"), "dns")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_both_egress_failing_fails_one_only_warns(self):
        verdict, failures = flipped(PASSING.replace("egress.api.openai.com=ok", "egress.api.openai.com=ETIMEDOUT"),
                                    "egress")
        self.assertTrue(verdict["passed"], verdict["failures"])
        self.assertEqual(failures, [])
        self.assertEqual(len(verdict["warnings"]), 1)
        verdict = evaluate(PASSING.replace("egress.api.anthropic.com=ok", "egress.api.anthropic.com=ETIMEDOUT")
                           .replace("egress.api.openai.com=ok", "egress.api.openai.com=EPERM"))
        self.assertFalse(verdict["passed"])

    def test_unix_connected_outside_the_allow_list(self):
        verdict, failures = flipped(PASSING.replace("unix.2.state=denied", "unix.2.state=connected"), "tailscaled")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_abstract_connected_only_warns(self):
        verdict = evaluate(PASSING.replace("abstract.0.state=refused", "abstract.0.state=connected"))
        self.assertTrue(verdict["passed"], verdict["failures"])
        self.assertEqual(len(verdict["warnings"]), 1)

    def test_own_sock_broken(self):
        verdict, failures = flipped(PASSING.replace("own_sock=ok", "own_sock=EACCES"), "own_sock")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_foreign_sockets_connected(self):
        for key in ("other_sock", "worker_sock"):
            verdict, failures = flipped(PASSING.replace("%s=denied" % key, "%s=connected" % key)
                                        .replace("%s=hidden" % key, "%s=connected" % key), key)
            self.assertFalse(verdict["passed"], key)
            self.assertEqual(len(failures), 1, key)

    def test_foreign_processes_visible(self):
        verdict, failures = flipped(PASSING.replace("proc.foreign_visible=0", "proc.foreign_visible=41"), "foreign")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_persist_leftover_and_user_unit(self):
        verdict, failures = flipped(PASSING.replace("persist.leftover=0", "persist.leftover=1"), "leftover")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)
        verdict, failures = flipped(PASSING.replace("persist.user_unit=refused", "persist.user_unit=started"),
                                    "user_unit")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_crontab_available_only_warns(self):
        verdict = evaluate(PASSING.replace("persist.crontab=refused", "persist.crontab=available"))
        self.assertTrue(verdict["passed"], verdict["failures"])
        self.assertEqual(len(verdict["warnings"]), 1)

    def test_hog_not_capped_or_slow_node(self):
        verdict, failures = flipped(PASSING.replace("hog.result=memory_error", "hog.result=not_capped"), "hog")
        self.assertFalse(verdict["passed"])
        self.assertTrue(any("MemoryMax" in f for f in failures))
        verdict, failures = flipped(PASSING.replace("hog.node_ms=15", "hog.node_ms=2500"), "hog.node_ms")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_forks_not_capped_or_over_the_limit(self):
        verdict, failures = flipped(PASSING.replace("forks.capped_at=63", "forks.result=not_capped"), "forks")
        self.assertFalse(verdict["passed"])
        self.assertTrue(any("TasksMax" in f for f in failures))
        verdict, failures = flipped(PASSING.replace("forks.capped_at=63", "forks.capped_at=70"), "capped_at")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_caps_must_be_observed_not_assumed(self):
        # the wrapper refused the hog run (64) and printed nothing: no cap was shown
        text = PASSING.replace("hog.exit=137\nhog.result=memory_error\nhog.mib=240\n", "hog.exit=64\n")
        verdict, failures = flipped(text, "no memory cap observed")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)
        # killed by the cap without a MemoryError line still counts as capped
        killed = PASSING.replace("hog.result=memory_error\nhog.mib=240\n", "")
        self.assertTrue(evaluate(killed)["passed"], evaluate(killed)["failures"])
        # the forks run printed neither a cap nor not_capped
        text = PASSING.replace("forks.capped_at=63\nforks.errno=EAGAIN\n", "")
        verdict, failures = flipped(text, "no task cap observed")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_launcher_both_proofs_pass_and_anything_else_fails(self):
        # cli_missing proves the launcher runs just as well (no CLI copy installed)
        verdict = evaluate(PASSING.replace("launcher.result=no_credential", "launcher.result=cli_missing"))
        self.assertTrue(verdict["passed"], verdict["failures"])
        for bad in ("launcher.exit=0", "launcher.exit=64\nlauncher.result=unexpected:x"):
            text = PASSING.replace("launcher.exit=3\nlauncher.result=no_credential", bad)
            verdict, failures = flipped(text, "launcher")
            self.assertFalse(verdict["passed"], bad)
            self.assertEqual(len(failures), 1, (bad, verdict["failures"]))

    def test_stop_left_a_unit_active_or_failed(self):
        for bad in ("stop.exit=1", "stop.active=gsa-probe-1a2b3c4d",
                    "stop.exit=1\nstop.active=timeout"):
            text = PASSING.replace("stop.exit=0\nstop.active=none", bad)
            verdict, failures = flipped(text, "stop")
            self.assertFalse(verdict["passed"], bad)
            self.assertEqual(len(failures), 1, (bad, verdict["failures"]))

    def test_stop_proved_nothing_when_no_unit_ever_started(self):
        verdict, failures = flipped(PASSING.replace("stop.started=1", "stop.started=0"),
                                    "sleep-60")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_missing_o2_lines_do_not_fail_on_their_own(self):
        text = PASSING.replace("launcher.exit=3\nlauncher.result=no_credential\n", "") \
                     .replace("stop.started=1\nstop.exit=0\nstop.active=none\n", "")
        verdict = evaluate(text)
        self.assertTrue(verdict["passed"], verdict["failures"])

    def test_not_finished(self):
        verdict = evaluate(PASSING.replace("probe.done=1", "probe.done=0"))
        self.assertFalse(verdict["finished"])
        self.assertFalse(verdict["passed"])
        self.assertIn("probe did not finish", verdict["failures"])
        verdict = evaluate(PASSING.replace("checks.done=1\n", ""))
        self.assertFalse(verdict["finished"])


class StressSkipped(unittest.TestCase):
    def test_busy_node_skips_the_stress_rules(self):
        verdict = evaluate(BUSY)
        self.assertEqual(verdict["failures"], [])
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["stress"], "skipped_busy")
        self.assertIsNone(verdict["memory_peak_mib"])

    def test_busy_node_still_checks_the_rest(self):
        verdict, failures = flipped(BUSY.replace("tcp.0.state=denied", "tcp.0.state=connected"), "100.64.0.10")
        self.assertFalse(verdict["passed"])
        self.assertEqual(len(failures), 1)

    def test_missing_stress_lines_do_not_fail_on_their_own(self):
        text = PASSING.replace("stress=ran\n", "")
        verdict = evaluate(text)
        self.assertTrue(verdict["passed"], verdict["failures"])


class RecordAndCli(unittest.TestCase):
    def test_verdict_record_shape(self):
        verdict = evaluate(PASSING)
        record = sp.verdict_record(verdict, "2026-11-18")
        self.assertEqual(sorted(record), ["checked", "failures", "memory_peak_mib", "passed", "stress",
                                           "version", "warnings"])
        self.assertEqual(record["checked"], "2026-11-18")
        self.assertTrue(record["passed"])
        self.assertEqual(record["version"], "abc123def456")
        self.assertEqual(record["memory_peak_mib"], 512)

    def test_format_summary_is_korean_with_both_marks(self):
        text = sp.format_summary(evaluate(PASSING))
        self.assertIn("✅", text)
        self.assertIn("512 MiB", text)
        bad = sp.format_summary(evaluate(PASSING.replace("sudo=refused", "sudo=allowed")))
        self.assertIn("❌", bad)
        self.assertIn("sudo", bad)

    def test_cli_exit_codes(self):
        script = os.path.join(HERE, "sandbox_probe.py")

        def run(mode, text):
            return subprocess.run([sys.executable, script, mode], input=text, text=True, capture_output=True)

        run_ok = run("evaluate", PASSING)
        self.assertEqual(run_ok.returncode, 0, run_ok.stderr)
        self.assertIn("✅", run_ok.stdout)
        failed = run("evaluate", PASSING.replace("tcp.1.state=denied", "tcp.1.state=connected"))
        self.assertEqual(failed.returncode, 3)
        unfinished = run("evaluate", PASSING.replace("probe.done=1", ""))
        self.assertEqual(unfinished.returncode, 1)

    def test_cli_verdict_prints_today_utc(self):
        script = os.path.join(HERE, "sandbox_probe.py")
        out = subprocess.run([sys.executable, script, "verdict"], input=PASSING, text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        record = json.loads(out.stdout)
        self.assertTrue(record["passed"])
        self.assertEqual(record["checked"], datetime.now(timezone.utc).strftime("%Y-%m-%d"))


if __name__ == "__main__":
    unittest.main()

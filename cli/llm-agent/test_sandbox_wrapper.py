"""Tests for deploy/sandbox/guardian-sandbox-run.sh (docs/design/resident-open-tools.md, O1-O2).

Sources the wrapper with GUARDIAN_SANDBOX_SOURCE_ONLY=1 under /bin/bash (bash 3.2 on a Mac, the
oldest shell the repo's scripts must work under) and exercises the pure functions: the name regex,
the limits parser, the timeout and chdir bounds, build_argv's systemd-run line and the --stop
mode's dry-run form. Also locks the sandbox version formula (C1: the hub computes the same value
from its release tree). Nothing runs systemd-run or systemctl; check_agent and the owner/mode
check need the node (getent, GNU stat)."""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
WRAPPER = ROOT / "deploy" / "sandbox" / "guardian-sandbox-run.sh"
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")

# The wrapper sets PATH to the node's directories while sourced; keep the test's own PATH.
PRELUDE = textwrap.dedent("""\
    saved_path="$PATH"
    GUARDIAN_SANDBOX_SOURCE_ONLY=1 source "$1"
    PATH="$saved_path"
    set +e
    shift
    """)

GOOD_LIMITS = ("MEMORY_MAX=2G\nTASKS_MAX=512\nCPU_QUOTA=100%\nTIMEOUT_MAX=3600\n"
               "PROBE_MEMORY_MAX=256M\nPROBE_TASKS_MAX=64\n")

IP_DENY = ("-p IPAddressDeny=0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 "
           "172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4 ::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8")
IP_ALLOW = "-p IPAddressAllow=127.0.0.53/32 127.0.0.54/32"


def run_bash(body, *args):
    script = PRELUDE + body
    return subprocess.run([BASH, "-c", script, "test", str(WRAPPER), *[str(a) for a in args]],
                          capture_output=True, text=True, timeout=60)


@unittest.skipUnless(BASH and WRAPPER.exists(), "bash or the wrapper not found")
class AgentName(unittest.TestCase):
    def test_regex_accepts_and_refuses(self):
        proc = run_bash(textwrap.dedent("""\
            for name in "$@"; do
                if agent_name_ok "$name"; then echo "ok $name"; else echo "bad $name"; fi
            done
            """), "probe", "a1", "probe2", "Probe", "-x", "a..b", "../x", "a" * 17, "")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        want = ["ok probe", "ok a1", "ok probe2", "bad Probe", "bad -x", "bad a..b", "bad ../x",
                "bad " + "a" * 17, "bad "]
        self.assertEqual(proc.stdout.splitlines(), want)


@unittest.skipUnless(BASH and WRAPPER.exists(), "bash or the wrapper not found")
class ReadLimits(unittest.TestCase):
    def limits_run(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "limits.conf")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            # No subshell: the parser must set the LIMIT_* globals in this shell, so a refusal ends
            # the whole script (die exits 64) and stdout stays empty for the bad cases below.
            return run_bash(textwrap.dedent("""\
                read_limits "$1" >/dev/null 2>&1
                echo "rc=$?"
                echo "memory=${MEMORY_MAX:-unset}"
                echo "tasks=${TASKS_MAX:-unset}"
                echo "cpu=${CPU_QUOTA:-unset}"
                echo "timeout=${TIMEOUT_MAX:-unset}"
                echo "pmem=${PROBE_MEMORY_MAX:-unset}"
                echo "ptasks=${PROBE_TASKS_MAX:-unset}"
                """), path)

    def test_good_file_parses(self):
        proc = self.limits_run(GOOD_LIMITS)
        self.assertEqual(proc.stdout.splitlines(),
                         ["rc=0", "memory=2G", "tasks=512", "cpu=100%", "timeout=3600",
                          "pmem=256M", "ptasks=64"], proc.stderr)

    def test_comments_and_blank_lines_are_ignored(self):
        proc = self.limits_run("# a comment\n\n" + GOOD_LIMITS + "\n# trailing\n")
        self.assertTrue(proc.stdout.splitlines()[0].startswith("rc=0"), proc.stdout)

    def test_bad_value_unknown_key_and_missing_key_are_refused(self):
        cases = [GOOD_LIMITS.replace("MEMORY_MAX=2G", "MEMORY_MAX=2X"),
                 GOOD_LIMITS.replace("CPU_QUOTA=100%", "CPU_QUOTA=100"),
                 GOOD_LIMITS.replace("TASKS_MAX=512", "TASKS_MAX=lots"),
                 GOOD_LIMITS + "EXTRA_KEY=1\n",
                 GOOD_LIMITS.replace("TIMEOUT_MAX=3600\n", ""),
                 "MEMORY_MAX=2G TASKS_MAX=512\n"]
        for text in cases:
            proc = self.limits_run(text)
            # die exits the script: rc=64 either as the printed line or as the script's exit code.
            lines = proc.stdout.splitlines()
            outcome = lines[0] if lines else "rc=%d" % proc.returncode
            self.assertEqual(outcome, "rc=64", "%s (%r)" % (text, proc.stderr))


@unittest.skipUnless(BASH and WRAPPER.exists(), "bash or the wrapper not found")
class Bounds(unittest.TestCase):
    def test_timeout_bounds(self):
        proc = run_bash(textwrap.dedent("""\
            for t in "" 1 3600 3601 0 abc; do
                # die exits the subshell (64), so a refusal shows as the subshell failing.
                ( TIMEOUT_MAX=3600; check_timeout "$t" && echo "ok:$SB_TIMEOUT" ) || echo refused
            done
            """))
        self.assertEqual(proc.stdout.splitlines(),
                         ["ok:3600", "ok:1", "ok:3600", "refused", "refused", "refused"], proc.stderr)

    def test_chdir_bounds(self):
        proc = run_bash(textwrap.dedent("""\
            for d in /home/gsa-probe /home/gsa-probe/w /home/gsa-probe/../x /home/gsa-probex /tmp /home/gsa-probe//x; do
                ( SB_AGENT=probe; check_chdir "$d" && echo "ok:$SB_CHDIR" ) || echo refused
            done
            """))
        self.assertEqual(proc.stdout.splitlines(),
                         ["ok:/home/gsa-probe", "ok:/home/gsa-probe/w", "refused", "refused",
                          "refused", "refused"], proc.stderr)


@unittest.skipUnless(BASH and WRAPPER.exists(), "bash or the wrapper not found")
class BuildArgv(unittest.TestCase):
    BUILD = textwrap.dedent("""\
        SB_AGENT=probe
        SB_PROBE=0
        SB_TIMEOUT=3600
        SB_CHDIR=/home/gsa-probe
        MEMORY_MAX=2G
        TASKS_MAX=512
        CPU_QUOTA=100%
        PROBE_MEMORY_MAX=256M
        PROBE_TASKS_MAX=64
        build_argv '$HOME' 'a b' -p
        printf '%s\\n' "${ARGV[@]}"
        echo "unit=${SB_UNIT}"
        """)

    def test_contains_every_lockdown_property(self):
        proc = run_bash(self.BUILD)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        joined = " ".join(lines)
        self.assertEqual(lines[0], "systemd-run")
        for needle in ("--quiet --wait --collect --pipe --service-type=exec --expand-environment=no",
                       "--uid=gsa-probe", "--gid=gsa-probe", "--working-directory=/home/gsa-probe",
                       "-p NoNewPrivileges=yes", "-p CapabilityBoundingSet= -p AmbientCapabilities=",
                       "-p RestrictSUIDSGID=yes", "-p PrivateTmp=yes", "-p PrivateDevices=yes",
                       "-p ProtectSystem=strict", "-p ProtectHome=tmpfs", "-p ProtectProc=invisible",
                       "-p BindPaths=/home/gsa-probe",
                       "-p TemporaryFileSystem=/var/lib/guardian-sandbox:ro",
                       "-p BindPaths=/var/lib/guardian-sandbox/sock/probe",
                       IP_DENY, IP_ALLOW,
                       "-p MemoryMax=2G", "-p MemorySwapMax=0", "-p TasksMax=512", "-p CPUQuota=100%",
                       "-p RuntimeMaxSec=3600", "-p KillMode=control-group", "-p SendSIGKILL=yes",
                       "-p Environment=SHELL=/bin/bash PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8"):
            self.assertIn(needle, joined, needle)
        unit = [line for line in lines if line.startswith("--unit=")]
        self.assertEqual(len(unit), 1)
        suffix = unit[0][len("--unit=gsa-probe-"):]
        self.assertEqual(len(suffix), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in suffix), suffix)
        # the signal trap stops exactly the unit systemd-run starts (run_unit's SB_UNIT)
        self.assertIn("unit=" + unit[0][len("--unit="):], lines)

    def test_command_words_after_the_separator_unchanged(self):
        proc = run_bash(self.BUILD)
        lines = proc.stdout.splitlines()
        sep = lines.index("--")
        self.assertEqual(lines[sep + 1:-1], ["$HOME", "a b", "-p"])

    def test_probe_limits_pick_the_probe_values(self):
        proc = run_bash(self.BUILD.replace("SB_PROBE=0", "SB_PROBE=1"))
        joined = " ".join(proc.stdout.splitlines())
        self.assertIn("-p MemoryMax=256M", joined)
        self.assertIn("-p TasksMax=64", joined)
        self.assertNotIn("MemoryMax=2G", joined)


@unittest.skipUnless(BASH and WRAPPER.exists(), "bash or the wrapper not found")
class StopMode(unittest.TestCase):
    def test_dry_run_prints_the_two_systemctl_commands(self):
        proc = run_bash("SB_DRY=1\ndo_stop probe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ["systemctl stop gsa-probe-*.service",
                          "systemctl list-units --plain --no-legend gsa-probe-*.service"])

    def test_bad_name_is_refused_before_anything_runs(self):
        for name in ("Probe", "a..b", "", "../x", "gsa-probe"):
            proc = run_bash(textwrap.dedent("""\
                ( SB_DRY=1; do_stop "$1" ) && echo "ran" || echo "rc=$?"
                """), name)
            lines = proc.stdout.splitlines()
            self.assertTrue(lines and lines[-1] == "rc=64", (name, proc.stdout, proc.stderr))
            self.assertNotIn("systemctl", proc.stdout, name)

    def test_valid_names_reach_the_dry_run_print(self):
        for name in ("probe", "a1", "probe2"):
            proc = run_bash("SB_DRY=1\ndo_stop \"$1\"", name)
            self.assertEqual(proc.returncode, 0, (name, proc.stderr))
            self.assertEqual(proc.stdout.splitlines()[0], "systemctl stop gsa-%s-*.service" % name)


@unittest.skipUnless(WRAPPER.exists(), "the wrapper not found")
class SandboxVersion(unittest.TestCase):
    # C1: sha256 of these four repository files concatenated in this order, first 12 hex
    # characters. The node's installer writes it to installed.json; the hub computes the same
    # value from its release tree, so the formula may never drift from this list.
    FILES = ("guardian-sandbox-run.sh", "limits.conf", "guardian-sandbox.sudoers",
             "guardian-sandbox-launch.py")

    def test_all_four_files_exist(self):
        for name in self.FILES:
            path = ROOT / "deploy" / "sandbox" / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 0, name)

    def test_installer_hashes_exactly_these_four_files_in_order(self):
        installer = (ROOT / "scripts" / "lib" / "sandbox-remote-install.sh").read_text(encoding="utf-8")
        body = installer[installer.index("sandbox_version()"):installer.index("install_mv()",
                                                                             installer.index("sandbox_version()"))]
        positions = []
        for name in self.FILES:
            quoted = '"${FILES}/%s"' % name
            self.assertIn(quoted, body, name)
            positions.append(body.index(quoted))
        self.assertEqual(positions, sorted(positions), "the C1 concat order changed")
        self.assertEqual(len(re.findall(r'"\$\{FILES\}/', body)), len(self.FILES),
                         "the version must hash exactly the four shipped files")

    def test_formula_is_the_sha256_prefix_of_the_concatenation(self):
        digest = hashlib.sha256()
        for name in self.FILES:
            digest.update((ROOT / "deploy" / "sandbox" / name).read_bytes())
        value = digest.hexdigest()[:12]
        self.assertRegex(value, r"^[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()

"""End-to-end tests of scripts/install-session-link.sh, the S2 workstation installer, against fakes.

No hub, no ssh dial and nothing outside a throwaway world: a fake `ssh-keyscan` prints the host
line of a throwaway ed25519 key made with the real ssh-keygen (so the installer's
`ssh-keygen -lf -` fingerprint check really verifies it), fake `launchctl`/`systemctl` log their
argv to a file, a fake `claude` prints a version, a fake `diskutil` answers `diskutil info` from
FAKE_OWNERS (disabled, fail or enabled by default), and python3, ssh-keygen and git are real. HOME
and GUARDIAN_SESSION_HOME point into the world, so the tests can prove the installer never
touches ~/.ssh or ~/.claude/settings.json. The script runs from this checkout; its modules come
from cli/llm-agent here. Placeholders only: ws-mac-1, site-a-arm1, 100.64.0.1, hub.example.test,
user guardian.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MODULES = ["session_local.py", "session_uplink.py", "session_daemon.py", "session_mcp.py",
           "session_hooks.py", "session_wake.py"]

# Every fake tool is a python one-liner reading its script from the environment: the fixed scan
# line and the shared argv log live in the world, so each test can steer them.
FAKE_SSH_KEYSCAN = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    line = open(os.environ["FAKE_SCAN_LINE"]).read()
    sys.stdout.write(line if line.endswith("\\n") else line + "\\n")
""")

FAKE_ARG_LOGGER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    with open(os.environ["FAKE_LOG"], "a") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
""")

FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    print("2.1.329 (Claude Code)")
""")

# diskutil is steered by FAKE_OWNERS: "disabled" prints the line a home volume with APFS ownership
# turned off shows, "fail" exits 1 (diskutil broken), anything else prints Owners: Enabled.
FAKE_DISKUTIL = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    mode = os.environ.get("FAKE_OWNERS", "enabled")
    if mode == "fail":
        sys.exit(1)
    print("   Owners:                    " + ("Disabled" if mode == "disabled" else "Enabled"))
""")


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class SessionInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = Path(tempfile.mkdtemp(prefix="sg-session-installer-"))
        self.addCleanup(shutil.rmtree, self.world, ignore_errors=True)
        self.userhome = self.world / "userhome"   # HOME: must stay untouched
        self.gshome = self.world / "gshome"       # GUARDIAN_SESSION_HOME: the install root's home
        self.userhome.mkdir()
        self.gshome.mkdir()
        self.bin = self.world / "bin"
        self.bin.mkdir()
        for name, body in (("ssh-keyscan", FAKE_SSH_KEYSCAN), ("launchctl", FAKE_ARG_LOGGER),
                           ("systemctl", FAKE_ARG_LOGGER), ("claude", FAKE_CLAUDE),
                           ("diskutil", FAKE_DISKUTIL)):
            _exe(self.bin / name, body)
        self.tool_log = self.world / "tool-calls.log"
        # A real throwaway ed25519 key stands in for the hub's host key: the fingerprint the
        # installer computes with ssh-keygen -lf really verifies the line the fake scan prints.
        self.scan_key = self.world / "hub-host-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.scan_key)],
                       check=True, capture_output=True)
        self.scan_pub = (self.world / "hub-host-key.pub").read_text().strip()
        self.fingerprint = self.fingerprint_of(self.scan_pub)
        self.write_scan_line(self.scan_pub)

    # -- helpers -----------------------------------------------------------------------------------

    @staticmethod
    def fingerprint_of(pub_line: str) -> str:
        done = subprocess.run(["ssh-keygen", "-lf", "-"], input=pub_line + "\n",
                              capture_output=True, text=True, check=True)
        return done.stdout.split()[1]

    def write_scan_line(self, pub_line: str, host: str = "100.64.0.1") -> None:
        (self.world / "scan-line").write_text(f"{host} {pub_line}\n")

    def root(self, home=None) -> Path:
        return (home or self.gshome) / ".guardian" / "session"

    def run_script(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                   HOME=str(self.userhome),
                   GUARDIAN_SESSION_HOME=str(env_extra.pop("gshome", self.gshome)),
                   FAKE_SCAN_LINE=str(self.world / "scan-line"),
                   FAKE_LOG=str(self.tool_log),
                   PYTHONDONTWRITEBYTECODE="1")
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(REPO / "scripts" / "install-session-link.sh"), *args],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)

    def good(self, *extra: str) -> list:
        return ["--workstation", "ws-mac-1", "--hub-address", "100.64.0.1",
                "--hub-alias", "site-a-arm1", "--hub-user", "guardian",
                "--hub-fingerprint", self.fingerprint, *extra]

    def tool_calls(self) -> list:
        if not self.tool_log.exists():
            return []
        return [json.loads(line) for line in self.tool_log.read_text().splitlines()]

    # -- install -----------------------------------------------------------------------------------

    def test_install_no_service_builds_the_tree_and_prints_every_hand_step(self) -> None:
        done = self.run_script(*self.good("--hub-name", "hub.example.test", "--no-service"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        root = self.root()
        lib = root / "lib"
        for name in MODULES:
            self.assertTrue((lib / name).is_file(), name)
            self.assertEqual(oct((lib / name).stat().st_mode & 0o777), "0o644", name)
        self.assertTrue((lib / "VERSION").is_file())
        self.assertEqual(oct(root.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((root / "link-key").stat().st_mode & 0o777), "0o600")
        known = (root / "known_hosts").read_text()
        self.assertTrue(known.startswith("site-a-arm1 ssh-ed25519 "), known)  # port 22: plain alias
        # the pinned line is the alias plus the scanned type and key (no host, no comment)
        self.assertEqual(known.strip(),
                         "site-a-arm1 " + " ".join(self.scan_pub.split()[:2]))
        self.assertEqual(oct((root / "known_hosts").stat().st_mode & 0o777), "0o600")
        config = json.loads((root / "config.json").read_text())
        self.assertEqual(config, {"workstation": "ws-mac-1", "hub_address": "100.64.0.1",
                                  "hub_name": "hub.example.test", "hub_alias": "site-a-arm1",
                                  "hub_user": "guardian", "ssh_port": 22})
        self.assertEqual(oct((root / "config.json").stat().st_mode & 0o777), "0o600")
        self.assertIn("check-config ok", done.stdout)
        self.assertEqual((root / "cli-version").read_text().splitlines()[0],
                         "2.1.329 (Claude Code)")
        # the printed hand steps: the public key, the mcp add line, the four hooks
        pub = (root / "link-key.pub").read_text().strip()
        self.assertIn(pub, done.stdout)
        self.assertIn("Settings -> Workstations", done.stdout)
        self.assertIn("claude mcp add --scope user guardian-session", done.stdout)
        self.assertIn("session_mcp.py", done.stdout)
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            self.assertIn(event, done.stdout)
        for event in ("session-start", "prompt-submit", "stop", "session-end"):
            self.assertIn(event, done.stdout)
        for tool in ("mcp__guardian-session__session_inbox", "mcp__guardian-session__session_list",
                     "mcp__guardian-session__session_status"):
            self.assertIn(tool, done.stdout)
        # the private key never reaches the output, and HOME is never touched
        self.assertNotIn((root / "link-key").read_text(), done.stdout)
        self.assertFalse((self.userhome / ".ssh").exists())
        self.assertFalse((self.userhome / ".claude").exists())
        self.assertFalse((self.gshome / ".ssh").exists())

    def test_a_nonstandard_port_pins_the_bracketed_alias(self) -> None:
        other = self.world / "gshome-port"
        other.mkdir()
        done = self.run_script(*self.good("--ssh-port", "2222", "--no-service"),
                               gshome=str(other))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        known = self.root(other).joinpath("known_hosts").read_text()
        self.assertTrue(known.startswith("[site-a-arm1]:2222 ssh-ed25519 "), known)
        self.assertEqual(json.loads(self.root(other).joinpath("config.json").read_text())["ssh_port"],
                         2222)

    def test_second_run_keeps_the_same_key(self) -> None:
        first = self.run_script(*self.good("--no-service"))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        key = (self.root() / "link-key").read_bytes()
        pub = (self.root() / "link-key.pub").read_bytes()
        second = self.run_script(*self.good("--no-service"))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("existing key kept", second.stdout)
        self.assertEqual((self.root() / "link-key").read_bytes(), key)
        self.assertEqual((self.root() / "link-key.pub").read_bytes(), pub)

    def test_fingerprint_mismatch_exits_4_and_pins_nothing(self) -> None:
        other_key = self.world / "other-host-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other_key)],
                       check=True, capture_output=True)
        other_pub = (self.world / "other-host-key.pub").read_text().strip()
        self.write_scan_line(other_pub)   # the hub answers with a different host key
        done = self.run_script(*self.good("--no-service"))
        self.assertEqual(done.returncode, 4, done.stdout + done.stderr)
        self.assertFalse((self.root() / "known_hosts").exists())
        # both fingerprints are shown: the expected one and the scanned one
        self.assertIn(self.fingerprint, done.stderr + done.stdout)
        self.assertIn(self.fingerprint_of(other_pub), done.stderr + done.stdout)

    def test_bad_arguments_exit_2_and_write_nothing(self) -> None:
        for flag, value in (("--hub-address", "hub.example.test"),   # not an IPv4 literal
                            ("--workstation", "Bad Id!"),            # not a workstation id
                            ("--ssh-port", "ssh")):                  # not a port
            with self.subTest(f"{flag}={value}"):
                done = self.run_script(*self.good(flag, value, "--no-service"))
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertFalse(self.root().exists())
                self.assertFalse(self.gshome.joinpath(".guardian").exists())

    def test_dry_run_checks_and_writes_nothing(self) -> None:
        done = self.run_script(*self.good("--dry-run"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("dry-run", done.stdout)
        self.assertIn("known_hosts", done.stdout)   # the plan names the paths and commands
        self.assertFalse(self.gshome.joinpath(".guardian").exists())
        self.assertFalse(self.userhome.joinpath(".guardian").exists())

    def test_the_printed_output_has_no_bypass_flags(self) -> None:
        done = self.run_script(*self.good("--no-service"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("skipDangerousModePermissionPrompt", done.stdout + done.stderr)
        self.assertNotIn("dangerously", done.stdout + done.stderr)

    # -- disable and status ------------------------------------------------------------------------

    def test_disable_deletes_the_key_and_keeps_config_pin_and_spool(self) -> None:
        done = self.run_script(*self.good("--no-service"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        root = self.root()
        spool_file = root / "spool" / "m-1.json"
        spool_file.write_text('{"mail": {"id": "m-1"}, "state": "unread"}\n')
        done = self.run_script("--disable")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((root / "link-key").exists())
        self.assertFalse((root / "link-key.pub").exists())
        self.assertTrue((root / "config.json").is_file())
        self.assertTrue((root / "known_hosts").is_file())
        self.assertTrue(spool_file.is_file())
        self.assertIn("Settings -> Workstations", done.stdout)

    def test_status_reports_the_tree_without_key_material(self) -> None:
        done = self.run_script(*self.good("--no-service"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        done = self.run_script("--status")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("ws-mac-1", done.stdout)
        self.assertIn("site-a-arm1", done.stdout)
        self.assertIn("link key: present", done.stdout)
        self.assertIn("hub host key pin: present", done.stdout)
        self.assertIn("control socket missing", done.stdout)   # no daemon is running here
        self.assertNotIn((self.root() / "link-key").read_text(), done.stdout)
        self.assertNotIn(self.scan_pub, done.stdout)

    # -- the service -------------------------------------------------------------------------------

    @unittest.skipIf(sys.platform != "darwin", "the launchd branch runs on macOS only")
    def test_service_install_bootstraps_the_launchagent(self) -> None:
        done = self.run_script(*self.good())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        plist = self.gshome / "Library" / "LaunchAgents" / "dev.guardian.session-link.plist"
        text = plist.read_text()
        for placeholder in ("@PYTHON@", "@LIB@", "@HOME_DIR@", "@ROOT@"):
            self.assertNotIn(placeholder, text)
        self.assertIn("session_daemon.py", text)
        self.assertIn("--home", text)
        self.assertIn(str(self.gshome), text)
        calls = self.tool_calls()
        self.assertIn(["bootstrap", f"gui/{os.getuid()}", str(plist)], calls)
        self.assertIn(["bootout", f"gui/{os.getuid()}/dev.guardian.session-link"], calls,
                        calls)
        # an enabled home volume is the normal case: no ownership warning at all
        self.assertNotIn("could not read volume ownership", done.stdout + done.stderr)

    @unittest.skipIf(sys.platform != "darwin", "the launchd branch runs on macOS only")
    def test_ownership_disabled_home_volume_exits_3_before_anything_is_written(self) -> None:
        done = self.run_script(*self.good(), FAKE_OWNERS="disabled")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("--no-service", done.stderr)
        self.assertIn("Owners: Disabled", done.stderr)
        self.assertFalse(self.root().exists())              # nothing under the session root
        self.assertFalse((self.gshome / "Library").exists())   # and no plist either
        self.assertEqual(self.tool_calls(), [], "no launchctl call of any kind")

    @unittest.skipIf(sys.platform != "darwin", "the launchd branch runs on macOS only")
    def test_ownership_disabled_is_caught_by_a_dry_run_too(self) -> None:
        done = self.run_script(*self.good("--dry-run"), FAKE_OWNERS="disabled")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("ownership disabled", done.stderr)
        self.assertFalse(self.gshome.joinpath(".guardian").exists())

    @unittest.skipIf(sys.platform != "darwin", "the launchd branch runs on macOS only")
    def test_ownership_disabled_with_no_service_installs_the_files(self) -> None:
        done = self.run_script(*self.good("--no-service"), FAKE_OWNERS="disabled")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((self.root() / "config.json").is_file())
        self.assertIn("9/10 service: skipped (--no-service)", done.stdout)

    @unittest.skipIf(sys.platform != "darwin", "the launchd branch runs on macOS only")
    def test_diskutil_failure_warns_but_still_bootstraps(self) -> None:
        done = self.run_script(*self.good(), FAKE_OWNERS="fail")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("could not read volume ownership", done.stdout)
        plist = self.gshome / "Library" / "LaunchAgents" / "dev.guardian.session-link.plist"
        self.assertIn(["bootstrap", f"gui/{os.getuid()}", str(plist)], self.tool_calls())

    @unittest.skipIf(sys.platform != "linux", "the systemd branch runs on Linux only")
    def test_service_install_enables_the_systemd_user_unit(self) -> None:
        done = self.run_script(*self.good())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        unit = self.gshome / ".config" / "systemd" / "user" / "guardian-session-link.service"
        text = unit.read_text()
        for placeholder in ("@PYTHON@", "@LIB@", "@HOME_DIR@", "@ROOT@"):
            self.assertNotIn(placeholder, text)
        self.assertIn("Restart=always", text)
        self.assertIn("NoNewPrivileges=yes", text)
        calls = self.tool_calls()
        self.assertTrue(any(call[:3] == ["--user", "daemon-reload"] for call in calls), calls)
        self.assertIn(["--user", "enable", "--now", "guardian-session-link.service"], calls)


if __name__ == "__main__":
    unittest.main()

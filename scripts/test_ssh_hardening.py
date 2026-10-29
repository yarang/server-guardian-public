"""Tests for deploy/ssh/apply-ssh-hardening.sh with fake sudo, sshd, systemctl and id."""

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "ssh" / "apply-ssh-hardening.sh"
CONF = ROOT / "deploy" / "ssh" / "00-guardian-hardening.conf"

# sshd -T stand-in: drop-ins (sorted) are read before the rest of sshd_config and the first
# value of a keyword wins, like OpenSSH with "Include sshd_config.d/*.conf" at the top.
FAKE_SSHD = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import glob, os, sys
    if os.environ.get("FAKE_SSHD_T_FAIL") and sys.argv[1:] == ["-t"]:
        sys.exit(255)
    if sys.argv[1:] == ["-t"]:
        sys.exit(0)
    values = {}
    def take(path):
        for line in open(path):
            parts = line.split()
            if parts and not parts[0].startswith("#") and parts[0].lower() != "include":
                values.setdefault(parts[0].lower(), parts[1].lower())
    for f in sorted(glob.glob(os.path.join(os.environ["SG_SSHD_DROPIN_DIR"], "*.conf"))):
        take(f)
    take(os.environ["SG_SSHD_CONFIG"])
    values.setdefault("maxauthtries", "6")
    values.setdefault("permitrootlogin", "without-password")
    for k, v in values.items():
        print(k, v)
    """
)


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ApplySshHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.dropins = self.tmp / "sshd_config.d"
        self.dropins.mkdir()
        self.main = self.tmp / "sshd_config"
        self.main.write_text(f"Include {self.dropins}/*.conf\nPasswordAuthentication no\n")
        self.root_keys = self.tmp / "root_authorized_keys"
        self.root_keys.write_text(
            'no-port-forwarding,no-agent-forwarding,no-X11-forwarding,command="echo \'Please login as the user '
            '\\"ubuntu\\" rather than root.\';sleep 10" ssh-rsa AAAA cloud\n'
        )
        self.reloads = self.tmp / "reloads"
        _exe(self.bin / "sudo", '#!/bin/sh\n[ "$1" = -n ] && shift\nexec "$@"\n')
        _exe(self.bin / "id", '#!/bin/sh\necho "${FAKE_UID:-1000}"\n')
        _exe(self.bin / "sshd", FAKE_SSHD)
        _exe(
            self.bin / "systemctl",
            f'#!/bin/sh\n[ -n "$FAKE_RELOAD_FAIL" ] && exit 1\necho "$@" >> {self.reloads}\n',
        )
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "SG_HARDENING_SRC": str(CONF),
            "SG_SSHD_CONFIG": str(self.main),
            "SG_SSHD_DROPIN_DIR": str(self.dropins),
            "SG_ROOT_AUTHORIZED_KEYS": str(self.root_keys),
        }
        self.dst = self.dropins / "00-guardian-hardening.conf"

    def run_script(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)], env={**self.env, **env}, capture_output=True, text=True, timeout=30
        )

    def reload_count(self) -> int:
        return len(self.reloads.read_text().splitlines()) if self.reloads.exists() else 0

    def test_installs_then_is_idempotent(self) -> None:
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(first.stdout.strip(), "INSTALLED")
        self.assertEqual(self.dst.read_text(), CONF.read_text())
        self.assertEqual(self.reload_count(), 1)

        second = self.run_script()
        self.assertEqual((second.returncode, second.stdout.strip()), (0, "UNCHANGED"))
        self.assertEqual(self.reload_count(), 1)

    def test_updates_an_older_managed_file(self) -> None:
        self.dst.write_text("# old\nMaxAuthTries 4\n")
        out = self.run_script()
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "UPDATED"))
        self.assertEqual(self.dst.read_text(), CONF.read_text())
        self.assertFalse((self.dropins / "00-guardian-hardening.conf.bak").exists())

    def test_reverts_when_sshd_rejects_the_config(self) -> None:
        out = self.run_script(FAKE_SSHD_T_FAIL="1")
        self.assertEqual(out.returncode, 4)
        self.assertIn("REVERTED: sshd -t", out.stdout)
        self.assertFalse(self.dst.exists())
        self.assertEqual(self.reload_count(), 0)

    def test_restores_previous_file_when_reverting(self) -> None:
        self.dst.write_text("# previous\nMaxAuthTries 4\n")
        out = self.run_script(FAKE_SSHD_T_FAIL="1")
        self.assertEqual(out.returncode, 4)
        self.assertEqual(self.dst.read_text(), "# previous\nMaxAuthTries 4\n")

    def test_reverts_when_an_earlier_dropin_wins(self) -> None:
        (self.dropins / "00-aaa.conf").write_text("PermitRootLogin yes\n")
        out = self.run_script()
        self.assertEqual(out.returncode, 4)
        self.assertIn("PermitRootLogin is 'yes'", out.stdout)
        self.assertFalse(self.dst.exists())

    def test_reverts_when_reload_fails(self) -> None:
        out = self.run_script(FAKE_RELOAD_FAIL="1")
        self.assertEqual(out.returncode, 4)
        self.assertIn("could not reload", out.stdout)
        self.assertFalse(self.dst.exists())

    def test_skips_without_include(self) -> None:
        self.main.write_text("PasswordAuthentication no\n")
        out = self.run_script()
        self.assertEqual(out.returncode, 3)
        self.assertIn("does not include", out.stdout)
        self.assertFalse(self.dst.exists())

    def test_skips_when_root_has_a_usable_key(self) -> None:
        with self.root_keys.open("a") as f:
            f.write("ssh-ed25519 AAAA admin@laptop\n")
        out = self.run_script()
        self.assertEqual(out.returncode, 3)
        self.assertIn("1 root authorized key(s)", out.stdout)
        self.assertFalse(self.dst.exists())

    def test_missing_root_keys_file_is_fine(self) -> None:
        self.root_keys.unlink()
        out = self.run_script()
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "INSTALLED"))

    def test_skips_when_logged_in_as_root(self) -> None:
        out = self.run_script(FAKE_UID="0")
        self.assertEqual(out.returncode, 3)
        self.assertIn("logged in as root", out.stdout)
        self.assertFalse(self.dst.exists())


if __name__ == "__main__":
    unittest.main()

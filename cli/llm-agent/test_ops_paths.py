"""Deny rules for paths that ops cleanup actions delete or truncate (docs/design/server-ops-project.md, 6.3, P4)."""
import unittest

from ops_paths import check_deny_patterns, refusal


class DefaultDenyTests(unittest.TestCase):
    def test_bypass_attempts_are_refused(self) -> None:
        refused = [
            # not plain paths
            "", "relative/path", "/home/u/../../etc", "/home/u/./x", "/home/u/*", "/home/u/x?", "/home/u/[ab]",
            "/home/u/x y", "/home/u/x;rm -rf /", "/home/u/$(id)", "/home/u/`id`", "/home/u/x/", "//etc", "/home/u/x\n/etc", "/home/u/x\n",
            # system directories themselves
            "/", "/home", "/root", "/var", "/var/log", "/var/cache", "/tmp", "/opt", "/srv", "/mnt",
            # protected trees
            "/etc", "/etc/passwd", "/etc/ssh/sshd_config.d", "/boot/vmlinuz", "/usr/local/bin/tool", "/bin/sh",
            "/lib/modules", "/var/lib/docker", "/var/lib/docker/overlay2/abc", "/var/lib/gitea/data", "/proc/1",
            "/sys/kernel", "/dev/sda", "/run/user/1000", "/var/backups/dpkg.status.0",
            # home directories and protected names at any depth
            "/home/ubuntu", "/home/ubuntu/.ssh", "/home/ubuntu/.ssh/authorized_keys", "/home/u/.gnupg",
            "/home/u/.claude/projects", "/home/u/.codex/sessions", "/home/u/.guardian/data", "/home/u/.config/app",
            "/home/u/server-guardian", "/home/u/guardian-workspace/apps", "/home/u/guardian-eval/runs3",
            "/home/u/GITEA/repos", "/data/app/.Docker/config",
            # secret-looking material
            "/home/u/app/x.enc", "/home/u/app/.env", "/srv/app/tls.key", "/srv/app/cert.pem", "/srv/app/id_ed25519",
            "/srv/app/credentials.json", "/srv/app/secrets/db",
        ]
        for path in refused:
            with self.subTest(path=path):
                self.assertIsNotNone(refusal(path))

    def test_ordinary_cleanup_targets_are_allowed(self) -> None:
        for path in ("/home/u/app/cache", "/home/u/projects/build/tmp", "/var/log/app/old.log", "/var/log/nginx",
                     "/var/cache/app", "/tmp/build-123", "/srv/other/tmp", "/data/backups/2025-01.tar.gz", "/opt/app/logs"):
            with self.subTest(path=path):
                self.assertIsNone(refusal(path))


class PolicyDenyTests(unittest.TestCase):
    def test_policy_patterns_protect_their_matches_what_is_below_and_what_contains_them(self) -> None:
        deny = ["/srv/data/*", "/home/*/keep"]
        self.assertIn("project policy", refusal("/srv/data/a", deny))
        self.assertIn("project policy", refusal("/srv/data/a/b/c", deny))
        self.assertIn("contains a path", refusal("/srv/data", deny))
        self.assertIn("project policy", refusal("/home/u/keep", deny))
        self.assertIn("project policy", refusal("/home/u/keep/x", deny))
        self.assertIsNone(refusal("/srv/other", deny))
        self.assertIsNone(refusal("/home/u/tmp", deny))

    def test_patterns_are_validated(self) -> None:
        self.assertEqual(check_deny_patterns(None), [])
        self.assertEqual(check_deny_patterns(["/srv/data/*", "/srv/data/*"]), ["/srv/data/*"])
        for bad in ("/srv", ["relative"], ["/"], ["/srv/../etc"], ["/x\n"], ["/x"] * 51, [1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_deny_patterns(bad)


if __name__ == "__main__":
    unittest.main()

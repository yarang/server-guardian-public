"""Package install and removal actions (docs/design/fleet-provisioning.md, P2): adapters, validator, parsers, flow.

Outputs below are recorded from the real tools where a host had them (npm, brew on the Mac; pnpm, yarn, pipx and uv
in a scratch directory of a test host) and written from the tools' documented formats otherwise (apt, dnf, zypper,
apk, pacman); nothing here runs a package manager."""
import os
import subprocess
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

import ops_actions
import ops_facts
import ops_packages as pk
from mcp_client import ActionValidationError, MCPClient
from project_registry import parse_project
from test_ops_project import OPS, _FixedTargets
from testenv import isolate_data_dir, restore_data_dir

INSTALL, REMOVE = pk.INSTALL, pk.REMOVE


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def facts(**kw: Any) -> Dict[str, Any]:
    base = {"os_family": "linux", "os_id": "ubuntu", "os_version": "24.04", "arch": "arm64", "init": "systemd",
            "root": False, "sudo_nopasswd": True, "complete": True,
            "managers": {"apt-get": "/usr/bin/apt-get", "npm": "/usr/bin/npm"}, "services": [], "ports": []}
    base.update(kw)
    return base


MAC = facts(os_family="macos", os_id="macos", sudo_nopasswd=False, managers={"brew": "/opt/homebrew/bin/brew", "npm": "/opt/homebrew/bin/npm"})
FEDORA = facts(os_id="fedora", managers={"dnf": "/usr/bin/dnf"})


def args(manager: str, *names: str, **extra: Any) -> Dict[str, Any]:
    return {"target_host": "n1", "manager": manager, "packages": [{"name": n} for n in names], **extra}


def sections(installed: str = "", simulation: str = "", code: int = 0, services: str = "") -> str:
    return f"@@installed\n{installed}\n@@simulation\n{simulation}\n@@exit {code}\n@@services\n{services}\n"


APT_INSTALL = """Reading package lists...
Building dependency tree...
The following additional packages will be installed:
  libhwloc15
The following NEW packages will be installed:
  htop libhwloc15
0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.
Inst libhwloc15 (2.10.0-1build1 Ubuntu:24.04/noble [arm64])
Inst htop (3.3.0-4build1 Ubuntu:24.04/noble [arm64])
Conf libhwloc15 (2.10.0-1build1 Ubuntu:24.04/noble [arm64])
Conf htop (3.3.0-4build1 Ubuntu:24.04/noble [arm64])
"""
APT_UPGRADE = "Inst libc6 [2.39-0ubuntu8.2] (2.39-0ubuntu8.3 Ubuntu:24.04/noble-updates [arm64])\n"
APT_REMOVE = "Remv htop [3.3.0-4build1]\nRemv libhwloc15 [2.10.0-1build1]\n"
APT_REMOVE_SSH = "Remv openssh-server [1:9.6p1-3ubuntu13]\nRemv ubuntu-server [1.539]\n"
DNF_INSTALL = """Last metadata expiration check: 0:10:00 ago on Mon 19 Oct 2026.
Dependencies resolved.
================================================================================
 Package           Architecture   Version               Repository         Size
================================================================================
Installing:
 htop              x86_64         3.3.0-1.fc40          fedora            180 k
Installing dependencies:
 libhwloc-very-long-package-name
                   x86_64         2.10.0-1.fc40         fedora            200 k

Transaction Summary
================================================================================
Install  2 Packages

Total download size: 380 k
Installed size: 1.0 M
Operation aborted.
"""
BREW_UPGRADE = "ffmpeg 9.0.2 is already installed but outdated (so it will be upgraded).\n" \
    "==> Would upgrade 1 formula:\nffmpeg 9.0.2 -> 9.0.2_1\n==> Downloading https://ghcr.io/v2/homebrew/core/ffmpeg/manifests/9.0.2_1\n"
BREW_INSTALL = "==> Would install 1 formula:\ntree\n==> Would install 2 dependencies:\nlibfoo 1.2\nlibbar\n"
NPM_INSTALL = "add cowsay 1.6.0\nadd emoji-regex 8.0.0\nchange camelcase 5.0.0\nremove old-dep 1.0.0\n"


SUSE = facts(os_id="opensuse-leap", managers={"zypper": "/usr/bin/zypper"})
ALPINE = facts(os_id="alpine", root=True, sudo_nopasswd=False, managers={"apk": "/sbin/apk"})
ARCH = facts(os_id="arch", managers={"pacman": "/usr/bin/pacman"})
USER_TOOLS = facts(managers={tool: f"/home/u/.local/bin/{tool}" for tool in ("pnpm", "yarn", "pipx", "uv")})

# zypper: written from the documented dry-run output (no host had zypper).
ZYPPER_INSTALL = """Loading repository data...
Reading installed packages...
Resolving package dependencies...

The following 2 NEW packages are going to be installed:
  libhwloc15 htop

The following recommended package was automatically selected:
  libhwloc15

2 new packages to install.
"""
ZYPPER_REMOVE = "The following 2 packages are going to be REMOVED:\n  htop\n  libhwloc15\n\n2 packages to remove.\n"
ZYPPER_UPGRADE = "The following package is going to be upgraded:\n  glibc\n\nThe following package is going to be REMOVED:\n  old-lib\n"
# apk: written from the documented --simulate output.
APK_INSTALL = "(1/2) Installing libhwloc (2.10.0-r0)\n(2/2) Installing htop (3.3.0-r0)\nOK: 12 MiB in 30 packages\n"
APK_UPGRADE = "(1/1) Upgrading musl (1.2.4-r0 -> 1.2.4-r1)\nOK: 12 MiB in 30 packages\n"
APK_REMOVE = "(1/1) Purging htop (3.3.0-r0)\nOK: 11 MiB in 29 packages\n"
# pacman: `-S --print --print-format "%n %v"`.
PACMAN_INSTALL = "hwloc 2.10.0-1\nhtop 3.3.0-1\nwarning: tree-2.1.1-1 is up to date -- skipping\n"
PACMAN_REMOVE_BLOCKED = "error: failed to prepare transaction (could not satisfy dependencies)\n:: mpv: removing ffmpeg breaks dependency 'ffmpeg'\n"
# Recorded: pnpm 9 list -g --json, pipx 1.18 list --short, uv 0.13 tool list, yarn 1.22 (the package.json read).
PNPM_LIST = """[
  {"path": "/home/u/.local/share/pnpm/global/5", "private": false,
   "dependencies": {"is-odd": {"from": "is-odd", "version": "3.0.1", "resolved": "https://registry.npmjs.org/is-odd/-/is-odd-3.0.1.tgz"}}}
]"""
PNPM_EMPTY = '[\n  {"path": "/home/u/.local/share/pnpm/global/5", "private": false}\n]'
PIPX_LIST = "cowsay 6.1\npycowsay 0.0.0.2\n"
UV_LIST = "cowsay v6.1\n- cowsay\npycowsay v0.0.0.2\n- pycowsay\n"
APK_LIST = "htop-3.3.0-r0 x86_64 {htop} (GPL-2.0-or-later) [installed]\npy3-foo-1.0.2-r1 x86_64 {foo} (MIT) [installed]\n"


class ShapeTests(unittest.TestCase):
    def test_accepts_plain_requests_and_normalizes_the_manager(self) -> None:
        out = pk.shape(INSTALL, {"target_host": "n1", "manager": " NPM ", "packages": [{"name": "cowsay", "version": "1.6.0"}]})
        self.assertEqual(out["manager"], "npm")

    def test_refuses_what_is_not_a_plain_name_or_list(self) -> None:
        bad = [
            args("apt"),  # no packages
            args("snap", "htop"),  # not a supported manager
            args("npm", "git+https://example.com/x.git"),
            args("npm", "https://example.com/x.tgz"),
            args("apt", "htop; reboot"),
            args("apt", "-oAPT::Update=x"),
            args("apt", "htop", "htop"),
            {**args("apt", "htop"), "extra": 1},
            {**args("apt", "htop"), "packages": "htop"},
            {**args("apt", "htop"), "packages": [{"name": "htop", "flag": "--force"}]},
            {**args("apt", *[f"p{i}" for i in range(pk.MAX_PACKAGES + 1)])},
            {**args("apt", "htop"), "ignore_scripts": "yes"},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                pk.shape(INSTALL, data)

    def test_a_removal_names_no_version(self) -> None:
        with self.assertRaises(ValueError):
            pk.shape(REMOVE, {"target_host": "n1", "manager": "apt", "packages": [{"name": "htop", "version": "1"}]})


class CheckTests(unittest.TestCase):
    def test_auto_resolves_to_the_system_manager(self) -> None:
        self.assertEqual(pk.check(INSTALL, args("auto", "htop"), facts())["manager"], "apt")
        self.assertEqual(pk.check(INSTALL, args("auto", "tree"), MAC)["manager"], "brew")
        self.assertEqual(pk.check(INSTALL, args("auto", "htop"), FEDORA)["manager"], "dnf")
        with self.assertRaises(ValueError):
            pk.check(INSTALL, args("auto", "htop"), facts(managers={"npm": "/usr/bin/npm"}))

    def test_needs_facts_and_the_manager_on_the_host(self) -> None:
        with self.assertRaises(ValueError):
            pk.check(INSTALL, args("apt", "htop"), None)
        with self.assertRaisesRegex(ValueError, "not installed"):
            pk.check(INSTALL, args("dnf", "htop"), facts())

    def test_system_managers_need_root_or_passwordless_sudo(self) -> None:
        with self.assertRaisesRegex(ValueError, "sudo_required"):
            pk.check(INSTALL, args("apt", "htop"), facts(sudo_nopasswd=False))
        pk.check(INSTALL, args("apt", "htop"), facts(sudo_nopasswd=False, root=True))
        # npm and brew do not need sudo to be vouched for
        pk.check(INSTALL, args("npm", "cowsay"), facts(sudo_nopasswd=False))

    def test_brew_refuses_to_run_as_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "root"):
            pk.check(INSTALL, args("brew", "tree"), {**MAC, "root": True})

    def test_flags_belong_to_their_managers(self) -> None:
        with self.assertRaises(ValueError):
            pk.check(REMOVE, {**args("dnf", "htop"), "purge": True}, FEDORA)
        with self.assertRaises(ValueError):
            pk.check(INSTALL, {**args("apt", "htop"), "ignore_scripts": True}, facts())
        pk.check(REMOVE, {**args("apt", "htop"), "purge": True}, facts())
        pk.check(INSTALL, {**args("npm", "cowsay"), "ignore_scripts": True}, facts())

    def test_names_and_versions_follow_the_manager(self) -> None:
        with self.assertRaises(ValueError):
            pk.check(INSTALL, args("brew", "owner/tap/formula"), MAC)  # a tap would add third-party code
        with self.assertRaises(ValueError):
            pk.check(INSTALL, args("npm", "Cowsay"), facts())  # uppercase is not a registry name
        with self.assertRaises(ValueError):
            pk.check(INSTALL, {**args("npm", "cowsay"), "packages": [{"name": "cowsay", "version": "^1.0.0"}]}, facts())
        with self.assertRaises(ValueError):
            pk.check(INSTALL, {**args("npm", "cowsay"), "packages": [{"name": "cowsay", "version": "latest"}]}, facts())
        with self.assertRaises(ValueError):
            pk.check(INSTALL, {**args("brew", "tree"), "packages": [{"name": "tree", "version": "2.1.0"}]}, MAC)
        ok = pk.check(INSTALL, {**args("npm", "@scope/pkg"), "packages": [{"name": "@scope/pkg", "version": "1.2.3"}]}, facts())
        self.assertEqual(ok["packages"], [{"name": "@scope/pkg", "version": "1.2.3"}])
        pk.check(INSTALL, {**args("apt", "htop"), "packages": [{"name": "htop", "version": "3.3.0-4build1"}]}, facts())

    def test_the_built_in_protected_list_stops_removals_only(self) -> None:
        for name in ("openssh-server", "sudo", "systemd", "libc6", "linux-image-6.8.0-1015-oracle", "tailscale",
                     "server-guardian-agent", "apt", "dpkg"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "protected"):
                pk.check(REMOVE, args("apt", name), facts())
        pk.check(INSTALL, args("apt", "openssh-server"), facts())  # installing a protected package is fine
        pk.check(REMOVE, args("apt", "htop"), facts())
        with self.assertRaisesRegex(ValueError, "protected"):
            pk.check(REMOVE, args("apt", "nginx"), facts(), extra_protected=["nginx"])

    def test_bun_is_gated_until_verified(self) -> None:
        bun = facts(managers={"bun": "/home/u/.bun/bin/bun"})
        with self.assertRaisesRegex(ValueError, "not been verified"):
            pk.check(INSTALL, args("bun", "cowsay"), bun)
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "bun"}):
            self.assertEqual(pk.check(INSTALL, args("bun", "cowsay"), bun)["manager"], "bun")


class RiskTests(unittest.TestCase):
    def test_levels(self) -> None:
        self.assertEqual(pk.risk(INSTALL, args("apt", "htop")), "medium")
        self.assertEqual(pk.risk(INSTALL, args("bun", "x")), "high")
        self.assertEqual(pk.risk(INSTALL, args("npm", "x")), "medium")
        self.assertEqual(pk.risk(REMOVE, args("apt", *[f"p{i}" for i in range(6)])), "high")
        self.assertEqual(pk.risk(REMOVE, args("apt", "htop"), {"items": [f"remove:p{i}" for i in range(7)]}), "high")
        self.assertEqual(pk.risk(INSTALL, args("apt", "htop"), {"items": ["add:htop", "remove:vim"]}), "high")
        self.assertEqual(pk.risk(INSTALL, args("apt", "htop"), {"items": ["add:htop=3"]}), "medium")
        self.assertEqual(ops_actions.risk(INSTALL, args("apt", "htop"), None), "medium")


class ArgvTests(unittest.TestCase):
    def test_apt(self) -> None:
        a = pk.check(INSTALL, {**args("apt", "htop"), "packages": [{"name": "htop", "version": "3.3.0-4"}, {"name": "tree"}]}, facts())
        self.assertEqual(pk.argv(INSTALL, a),
                         ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y", "-o", "Dpkg::Options::=--force-confold",
                          "-o", "Debug::NoLocking=1", "install", "htop=3.3.0-4", "tree"])
        self.assertIn("-s", pk.argv(INSTALL, a, simulate=True))
        self.assertNotIn("-y", pk.argv(INSTALL, a, simulate=True))
        r = pk.check(REMOVE, {**args("apt", "htop"), "purge": True}, facts())
        self.assertEqual(pk.argv(REMOVE, r)[-2:], ["purge", "htop"])
        self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("apt", "htop"), facts()))[-2:], ["remove", "htop"])

    def test_dnf_brew_npm_bun(self) -> None:
        d = pk.check(INSTALL, {**args("dnf", "htop"), "packages": [{"name": "htop", "version": "3.3.0-1.fc40"}]}, FEDORA)
        self.assertEqual(pk.argv(INSTALL, d), ["dnf", "-y", "install", "htop-3.3.0-1.fc40"])
        self.assertEqual(pk.argv(INSTALL, d, simulate=True)[:2], ["dnf", "--assumeno"])
        b = pk.check(INSTALL, args("brew", "tree"), MAC)
        self.assertEqual(pk.argv(INSTALL, b), ["brew", "install", "--formula", "tree"])
        self.assertEqual(pk.argv(INSTALL, b, simulate=True), ["brew", "install", "--formula", "--dry-run", "tree"])
        br = pk.check(REMOVE, args("brew", "tree"), MAC)
        self.assertEqual(pk.argv(REMOVE, br), ["brew", "uninstall", "--formula", "tree"])
        self.assertEqual(pk.argv(REMOVE, br, simulate=True), ["brew", "uses", "--installed", "--formula", "tree"])
        n = pk.check(INSTALL, {**args("npm", "cowsay"), "packages": [{"name": "cowsay", "version": "1.6.0"}]}, facts())
        self.assertEqual(pk.argv(INSTALL, n), ["npm", "install", "-g", "--no-audit", "--no-fund", "cowsay@1.6.0"])
        safe = pk.check(INSTALL, {**args("npm", "cowsay"), "ignore_scripts": True}, facts())
        self.assertIn("--ignore-scripts", pk.argv(INSTALL, safe))
        self.assertNotIn("--ignore-scripts", pk.argv(INSTALL, n))
        self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("npm", "cowsay"), facts()), simulate=True),
                         ["npm", "uninstall", "-g", "--dry-run", "cowsay"])
        bun = {**args("bun", "cowsay"), "packages": [{"name": "cowsay"}]}
        self.assertEqual(pk.argv(INSTALL, bun), ["bun", "add", "-g", "cowsay"])
        self.assertEqual(pk.argv(REMOVE, bun), ["bun", "remove", "-g", "cowsay"])

    def test_commands_are_valid_shell_and_use_the_privilege_prefix(self) -> None:
        cases = [(INSTALL, pk.check(INSTALL, args("apt", "htop"), facts())), (REMOVE, pk.check(REMOVE, args("dnf", "htop"), FEDORA)),
                 (INSTALL, pk.check(INSTALL, args("brew", "tree"), MAC)), (INSTALL, pk.check(INSTALL, args("npm", "cowsay"), facts()))]
        for tool, a in cases:
            for command in (pk.run_command(tool, a), pk.measure_command(tool, a)):
                with self.subTest(manager=a["manager"], command=command[:60]):
                    done = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True)
                    self.assertEqual(done.returncode, 0, done.stderr)
        run = pk.run_command(INSTALL, pk.check(INSTALL, args("apt", "htop"), facts()))
        self.assertIn('$S env DEBIAN_FRONTEND=noninteractive apt-get -y', run)
        self.assertIn('sudo -n', run)
        self.assertTrue(pk.run_command(INSTALL, pk.check(INSTALL, args("brew", "tree"), MAC)).rstrip().endswith("brew install --formula tree"))

    def test_unresolved_manager_does_not_build_a_command(self) -> None:
        with self.assertRaises(ValueError):
            pk.run_command(INSTALL, args("auto", "htop"))


class ParserTests(unittest.TestCase):
    def items(self, manager: str, tool: str, a: Dict[str, Any], sim: str, code: int = 0):
        return pk.parse_simulation(manager, tool, a, pk.parse_sections(sections(simulation=sim, code=code)))

    def test_apt(self) -> None:
        a = args("apt", "htop")
        self.assertEqual(self.items("apt", INSTALL, a, APT_INSTALL), (["add:libhwloc15=2.10.0-1build1", "add:htop=3.3.0-4build1"], None))
        self.assertEqual(self.items("apt", INSTALL, a, APT_UPGRADE)[0], ["upgrade:libc6=2.39-0ubuntu8.3"])
        self.assertEqual(self.items("apt", REMOVE, a, APT_REMOVE)[0], ["remove:htop=3.3.0-4build1", "remove:libhwloc15=2.10.0-1build1"])
        items, error = self.items("apt", INSTALL, a, "E: Unable to locate package nosuchthing\n", 100)
        self.assertEqual(items, [])
        self.assertIn("Unable to locate", error)

    def test_dnf_with_a_wrapped_name_and_the_abort_exit(self) -> None:
        items, error = self.items("dnf", INSTALL, args("dnf", "htop"), DNF_INSTALL, code=1)
        self.assertIsNone(error)
        self.assertEqual(items, ["add:htop=3.3.0-1.fc40", "add:libhwloc-very-long-package-name=2.10.0-1.fc40"])
        _, error = self.items("dnf", INSTALL, args("dnf", "x"), "No match for argument: x\nError: Unable to find a match: x\n", 1)
        self.assertIn("No match", error)

    def test_brew_recorded_formats(self) -> None:
        a = args("brew", "ffmpeg")
        self.assertEqual(self.items("brew", INSTALL, a, BREW_UPGRADE)[0], ["upgrade:ffmpeg=9.0.2_1"])
        self.assertEqual(self.items("brew", INSTALL, a, BREW_INSTALL)[0], ["add:tree", "add:libfoo=1.2", "add:libbar"])
        _, error = self.items("brew", INSTALL, a, 'Warning: No available formula with the name "x".\nError: No formulae found for x.\n', 1)
        self.assertIn("No formulae found", error)

    def test_brew_remove_lists_the_dependents(self) -> None:
        items, _ = self.items("brew", REMOVE, args("brew", "openssl@3"), "aws-c-io\nwget\n")
        self.assertEqual(items, ["remove:openssl@3", "dependent:aws-c-io", "dependent:wget"])
        self.assertEqual(self.items("brew", REMOVE, args("brew", "tree"), "")[0], ["remove:tree"])

    def test_npm_recorded_format(self) -> None:
        items, _ = self.items("npm", INSTALL, args("npm", "cowsay"), NPM_INSTALL)
        self.assertEqual(items, ["add:cowsay=1.6.0", "add:emoji-regex=8.0.0", "upgrade:camelcase=5.0.0", "remove:old-dep=1.0.0"])

    def test_installed(self) -> None:
        self.assertEqual(pk.parse_installed("apt", "htop 3.3.0-4build1 ii\nvim 9.1 rc\n"), {"htop": "3.3.0-4build1"})
        self.assertEqual(pk.parse_installed("apt", "libc6 2.39-0ubuntu8.3 ii\n"), {"libc6": "2.39-0ubuntu8.3"})
        self.assertEqual(pk.parse_installed("dnf", "htop 3.3.0-1.fc40\npackage nope is not installed\n"), {"htop": "3.3.0-1.fc40"})
        self.assertEqual(pk.parse_installed("brew", "openssl@3 3.6.4 3.6.5\ntree 2.3.2\n"), {"openssl@3": "3.6.5", "tree": "2.3.2"})
        npm = '{"name": "lib", "dependencies": {"npm": {"version": "11.19.1", "overridden": false}}}'
        self.assertEqual(pk.parse_installed("npm", npm), {"npm": "11.19.1"})
        self.assertEqual(pk.parse_installed("npm", "{}"), {})
        self.assertIsNone(pk.parse_installed("npm", "npm ERR! nope"))

    def test_sections(self) -> None:
        parsed = pk.parse_sections(sections("a", "b", 100, "x.service"))
        self.assertEqual((parsed["installed"], parsed["simulation"], parsed["exit"], parsed["services"]), ("a", "b", 100, "x.service"))


class AssessTests(unittest.TestCase):
    def test_a_good_simulation(self) -> None:
        a = pk.check(INSTALL, args("apt", "htop"), facts())
        got = pk.assess(INSTALL, a, sections("", APT_INSTALL, 0, "ssh.service\ncron.service\n"))
        self.assertIsNone(got["error"])
        self.assertEqual(got["items"], ["add:libhwloc15=2.10.0-1build1", "add:htop=3.3.0-4build1"])
        self.assertEqual(got["installed"], {})
        self.assertEqual(got["services"], ["cron.service", "ssh.service"])
        self.assertNotIn("refusal", got)

    def test_a_removal_that_drags_a_protected_package_is_refused(self) -> None:
        a = pk.check(REMOVE, args("apt", "ubuntu-server"), facts())
        got = pk.assess(REMOVE, a, sections("ubuntu-server 1.539 ii", APT_REMOVE_SSH))
        self.assertIn("openssh-server", got["refusal"])
        self.assertIn("openssh-server", ops_actions.drift({}, got) or "")

    def test_removing_what_is_not_installed_is_an_error(self) -> None:
        a = pk.check(REMOVE, args("apt", "htop"), facts())
        got = pk.assess(REMOVE, a, sections("", "Package 'htop' is not installed, so not removed\n"))
        self.assertIn("is installed", got["error"])
        self.assertEqual(got["installed"], {})

    def test_brew_dependents_refuse_the_removal(self) -> None:
        a = pk.check(REMOVE, args("brew", "ffmpeg"), MAC)
        got = pk.assess(REMOVE, a, sections("ffmpeg 9.0.2", "mpv\n"))
        self.assertIn("mpv", got["refusal"])

    def test_missing_manager_and_failed_simulation(self) -> None:
        a = pk.check(INSTALL, args("apt", "htop"), facts())
        self.assertIn("not available", pk.assess(INSTALL, a, "apt is not available on this host\n")["error"])
        got = pk.assess(INSTALL, a, sections("", "E: Unable to locate package htop", 100))
        self.assertIn("Unable to locate", got["error"])
        self.assertIsNone(got["items"])
        self.assertIn("did not finish", pk.assess(INSTALL, a, "@@installed\n@@simulation\npartial\n")["error"])


class VerifyTests(unittest.TestCase):
    def test_install_and_remove(self) -> None:
        a = pk.check(INSTALL, {**args("apt", "htop"), "packages": [{"name": "htop", "version": "3.3.0-4"}]}, facts())
        ok = pk.verify(INSTALL, a, {"services": ["a.service"]}, {"installed": {"htop": "3.3.0-4build1"}, "services": ["a.service"]}, 0)
        self.assertTrue(ok["ok"], ok)
        wrong = pk.verify(INSTALL, a, {}, {"installed": {"htop": "3.2.0"}, "services": []}, 0)
        self.assertFalse(wrong["ok"])
        missing = pk.verify(INSTALL, a, {}, {"installed": {}, "services": []}, 0)
        self.assertIn("htop is not installed", missing["wrong"])
        r = pk.check(REMOVE, args("apt", "htop"), facts())
        self.assertTrue(pk.verify(REMOVE, r, {}, {"installed": {}, "services": []}, 0)["ok"])
        self.assertFalse(pk.verify(REMOVE, r, {}, {"installed": {"htop": "1"}, "services": []}, 0)["ok"])

    def test_a_stopped_service_or_unreadable_state_fails_the_verification(self) -> None:
        r = pk.check(REMOVE, args("apt", "htop"), facts())
        stopped = pk.verify(REMOVE, r, {"services": ["nginx.service", "ssh.service"]}, {"installed": {}, "services": ["ssh.service"]}, 0)
        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["services_stopped"], ["nginx.service"])
        self.assertFalse(pk.verify(REMOVE, r, {}, {"installed": None, "services": []}, 0)["ok"])


class P2bTests(unittest.TestCase):
    """zypper, apk, pacman (system managers) and pnpm, yarn, pipx, uv (user-level, preview is only the request)."""

    def sim(self, manager: str, tool: str, a: Dict[str, Any], text: str, code: int = 0):
        return pk.parse_simulation(manager, tool, a, pk.parse_sections(sections(simulation=text, code=code)))

    def test_auto_picks_the_distribution_manager(self) -> None:
        for host, manager in ((SUSE, "zypper"), (ALPINE, "apk"), (ARCH, "pacman")):
            with self.subTest(manager=manager), patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": manager}):
                self.assertEqual(pk.check(INSTALL, args("auto", "htop"), host)["manager"], manager)

    def test_unverified_system_managers_are_gated(self) -> None:
        for host, manager in ((SUSE, "zypper"), (ALPINE, "apk"), (ARCH, "pacman")):
            with self.subTest(manager=manager):
                with self.assertRaisesRegex(ValueError, "not been verified"):
                    pk.check(INSTALL, args(manager, "htop"), host)
                with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": f"bun, {manager}"}):
                    pk.check(INSTALL, args(manager, "htop"), host)

    def test_user_level_tools_were_run_and_need_no_gate_or_sudo(self) -> None:
        tools = facts(sudo_nopasswd=False, managers=USER_TOOLS["managers"])
        for manager in ("pnpm", "yarn", "pipx", "uv"):
            with self.subTest(manager=manager):
                self.assertEqual(pk.check(INSTALL, args(manager, "cowsay"), tools)["manager"], manager)

    def test_system_managers_still_need_root_or_sudo(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "apk,zypper,pacman"}):
            with self.assertRaisesRegex(ValueError, "sudo_required"):
                pk.check(INSTALL, args("zypper", "htop"), {**SUSE, "sudo_nopasswd": False})
            pk.check(INSTALL, args("apk", "htop"), ALPINE)  # root login

    def test_names_versions_and_flags(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "apk,zypper,pacman"}):
            with self.assertRaisesRegex(ValueError, "pinned version"):
                pk.check(INSTALL, {**args("pacman", "htop"), "packages": [{"name": "htop", "version": "3.3.0-1"}]}, ARCH)
            with self.assertRaises(ValueError):
                pk.check(INSTALL, args("pacman", "extra/htop"), ARCH)  # a repository prefix
            with self.assertRaises(ValueError):
                pk.check(INSTALL, args("apk", "Htop"), ALPINE)
            pk.check(INSTALL, {**args("apk", "htop"), "packages": [{"name": "htop", "version": "3.3.0-r0"}]}, ALPINE)
            pk.check(INSTALL, {**args("zypper", "htop"), "packages": [{"name": "htop", "version": "3.3.0-1.1"}]}, SUSE)
        with self.assertRaises(ValueError):
            pk.check(INSTALL, {**args("pipx", "cowsay"), "packages": [{"name": "cowsay", "version": ">=6"}]}, USER_TOOLS)
        with self.assertRaises(ValueError):
            pk.check(INSTALL, args("pipx", "cowsay[extra]"), USER_TOOLS)
        with self.assertRaisesRegex(ValueError, "one package"):
            pk.check(INSTALL, args("uv", "cowsay", "pycowsay"), USER_TOOLS)
        with self.assertRaises(ValueError):
            pk.check(INSTALL, {**args("uv", "cowsay"), "ignore_scripts": True}, USER_TOOLS)
        pk.check(INSTALL, {**args("pnpm", "cowsay"), "ignore_scripts": True}, USER_TOOLS)
        pk.check(INSTALL, {**args("yarn", "cowsay"), "ignore_scripts": True}, USER_TOOLS)
        ok = pk.check(INSTALL, {**args("pipx", "cowsay"), "packages": [{"name": "cowsay", "version": "6.1"}]}, USER_TOOLS)
        self.assertEqual(ok["packages"], [{"name": "cowsay", "version": "6.1"}])

    def test_protected_lists(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "apk,zypper,pacman"}):
            for manager, host, names in (("zypper", SUSE, ("openssh", "kernel-default", "glibc", "zypper", "sudo")),
                                         ("apk", ALPINE, ("openssh-server", "busybox", "musl", "apk-tools", "alpine-base")),
                                         ("pacman", ARCH, ("openssh", "linux", "linux-lts", "glibc", "pacman", "sudo"))):
                for name in names:
                    with self.subTest(manager=manager, name=name), self.assertRaisesRegex(ValueError, "protected"):
                        pk.check(REMOVE, args(manager, name), host)
            pk.check(REMOVE, args("pacman", "htop"), ARCH)
        with self.assertRaisesRegex(ValueError, "protected"):
            pk.check(REMOVE, args("pipx", "uv"), USER_TOOLS)

    def test_risk(self) -> None:
        for manager in ("pnpm", "yarn", "pipx", "uv", "bun"):
            self.assertEqual(pk.risk(INSTALL, args(manager, "x")), "high", manager)
        for manager in ("zypper", "apk", "pacman"):
            self.assertEqual(pk.risk(INSTALL, args(manager, "x")), "medium", manager)

    def test_argv(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "apk,zypper,pacman"}):
            z = pk.check(INSTALL, {**args("zypper", "htop"), "packages": [{"name": "htop", "version": "3.3.0-1.1"}]}, SUSE)
            self.assertEqual(pk.argv(INSTALL, z), ["zypper", "--non-interactive", "install", "htop=3.3.0-1.1"])
            self.assertEqual(pk.argv(INSTALL, z, simulate=True), ["zypper", "--non-interactive", "install", "--dry-run", "htop=3.3.0-1.1"])
            zr = pk.check(REMOVE, args("zypper", "htop"), SUSE)
            self.assertEqual(pk.argv(REMOVE, zr), ["zypper", "--non-interactive", "remove", "htop"])
            a = pk.check(INSTALL, {**args("apk", "htop"), "packages": [{"name": "htop", "version": "3.3.0-r0"}]}, ALPINE)
            self.assertEqual(pk.argv(INSTALL, a), ["apk", "add", "htop=3.3.0-r0"])
            self.assertEqual(pk.argv(INSTALL, a, simulate=True), ["apk", "add", "--simulate", "htop=3.3.0-r0"])
            self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("apk", "htop"), ALPINE)), ["apk", "del", "htop"])
            p = pk.check(INSTALL, args("pacman", "htop", "tree"), ARCH)
            self.assertEqual(pk.argv(INSTALL, p), ["pacman", "-S", "--noconfirm", "--needed", "htop", "tree"])
            self.assertEqual(pk.argv(INSTALL, p, simulate=True),
                             ["pacman", "-S", "--print", "--print-format", "%n %v", "--noconfirm", "--needed", "htop", "tree"])
            self.assertNotIn("-y", pk.argv(INSTALL, p))  # never a database refresh without a full upgrade
            pr = pk.check(REMOVE, args("pacman", "htop"), ARCH)
            self.assertEqual(pk.argv(REMOVE, pr), ["pacman", "-R", "--noconfirm", "htop"])
            self.assertEqual(pk.argv(REMOVE, pr, simulate=True), ["pacman", "-R", "--print", "--print-format", "%n %v", "--noconfirm", "htop"])
        pn = pk.check(INSTALL, {**args("pnpm", "is-odd"), "packages": [{"name": "is-odd", "version": "3.0.1"}]}, USER_TOOLS)
        self.assertEqual(pk.argv(INSTALL, pn), ["pnpm", "add", "-g", "is-odd@3.0.1"])
        self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("pnpm", "is-odd"), USER_TOOLS)), ["pnpm", "remove", "-g", "is-odd"])
        ys = pk.check(INSTALL, {**args("yarn", "cowsay"), "ignore_scripts": True}, USER_TOOLS)
        self.assertEqual(pk.argv(INSTALL, ys), ["yarn", "global", "add", "--non-interactive", "--ignore-scripts", "cowsay"])
        self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("yarn", "cowsay"), USER_TOOLS)),
                         ["yarn", "global", "remove", "--non-interactive", "cowsay"])
        px = pk.check(INSTALL, {**args("pipx", "cowsay"), "packages": [{"name": "cowsay", "version": "6.1"}]}, USER_TOOLS)
        self.assertEqual(pk.argv(INSTALL, px), ["pipx", "install", "cowsay==6.1"])
        self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("pipx", "cowsay"), USER_TOOLS)), ["pipx", "uninstall", "cowsay"])
        self.assertEqual(pk.argv(INSTALL, pk.check(INSTALL, args("uv", "cowsay"), USER_TOOLS)), ["uv", "tool", "install", "cowsay"])
        self.assertEqual(pk.argv(REMOVE, pk.check(REMOVE, args("uv", "cowsay"), USER_TOOLS)), ["uv", "tool", "uninstall", "cowsay"])

    def test_commands_are_valid_shell(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "apk,zypper,pacman"}):
            cases = [(INSTALL, pk.check(INSTALL, args("zypper", "htop"), SUSE)), (REMOVE, pk.check(REMOVE, args("apk", "htop"), ALPINE)),
                     (INSTALL, pk.check(INSTALL, args("pacman", "htop"), ARCH))]
        cases += [(INSTALL, pk.check(INSTALL, args(m, "cowsay"), USER_TOOLS)) for m in ("pnpm", "yarn", "pipx", "uv")]
        cases += [(REMOVE, pk.check(REMOVE, args(m, "cowsay"), USER_TOOLS)) for m in ("pnpm", "yarn")]
        for tool, a in cases:
            for command in (pk.run_command(tool, a), pk.measure_command(tool, a)):
                with self.subTest(manager=a["manager"], command=command[-50:]):
                    done = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True)
                    self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("$S zypper", pk.run_command(INSTALL, cases[0][1]))
        self.assertIn("$S pacman -S --print", pk.measure_command(INSTALL, cases[2][1]))  # a simulation needs root too
        self.assertNotIn("$S", pk.run_command(INSTALL, pk.check(INSTALL, args("pipx", "cowsay"), USER_TOOLS)).split("&&")[-1])
        self.assertIn("PNPM_HOME", pk.run_command(INSTALL, pk.check(INSTALL, args("pnpm", "cowsay"), USER_TOOLS)))
        self.assertIn("1.*", pk.run_command(INSTALL, pk.check(INSTALL, args("yarn", "cowsay"), USER_TOOLS)))

    def test_listed_only_tools_have_no_simulation(self) -> None:
        a = pk.check(INSTALL, args("pipx", "cowsay"), USER_TOOLS)
        got = pk.assess(INSTALL, a, sections("", "", 0, "ssh.service\n"))
        self.assertEqual(got["items"], ["add:cowsay"])
        self.assertIsNone(got["error"])
        removal = pk.assess(REMOVE, pk.check(REMOVE, args("uv", "cowsay"), USER_TOOLS), sections("cowsay v6.1\n- cowsay\n", "", 0))
        self.assertEqual(removal["items"], ["remove:cowsay"])

    def test_zypper_recorded_format(self) -> None:
        a = args("zypper", "htop")
        self.assertEqual(self.sim("zypper", INSTALL, a, ZYPPER_INSTALL)[0], ["add:libhwloc15", "add:htop"])
        self.assertEqual(self.sim("zypper", REMOVE, a, ZYPPER_REMOVE)[0], ["remove:htop", "remove:libhwloc15"])
        self.assertEqual(self.sim("zypper", INSTALL, a, ZYPPER_UPGRADE)[0], ["upgrade:glibc", "remove:old-lib"])
        items, error = self.sim("zypper", INSTALL, a, "Loading repository data...\nPackage 'nosuch' not found.\n", 104)
        self.assertEqual(items, [])
        self.assertIn("not found", error)

    def test_apk_recorded_format(self) -> None:
        a = args("apk", "htop")
        self.assertEqual(self.sim("apk", INSTALL, a, APK_INSTALL)[0], ["add:libhwloc=2.10.0-r0", "add:htop=3.3.0-r0"])
        self.assertEqual(self.sim("apk", INSTALL, a, APK_UPGRADE)[0], ["upgrade:musl=1.2.4-r1"])
        self.assertEqual(self.sim("apk", REMOVE, a, APK_REMOVE)[0], ["remove:htop=3.3.0-r0"])
        _, error = self.sim("apk", INSTALL, a, "ERROR: unable to select packages:\n  nosuch (no such package):\n", 1)
        self.assertIn("unable to select", error)

    def test_pacman_recorded_format(self) -> None:
        a = args("pacman", "htop")
        self.assertEqual(self.sim("pacman", INSTALL, a, PACMAN_INSTALL)[0], ["add:hwloc=2.10.0-1", "add:htop=3.3.0-1"])
        self.assertEqual(self.sim("pacman", REMOVE, a, "htop 3.3.0-1\n")[0], ["remove:htop=3.3.0-1"])
        self.assertEqual(self.sim("pacman", INSTALL, a, "linux 1:6.8.1-1\n")[0], ["add:linux=1:6.8.1-1"])
        _, error = self.sim("pacman", REMOVE, a, PACMAN_REMOVE_BLOCKED, 1)
        self.assertIn("could not satisfy dependencies", error)
        _, error = self.sim("pacman", INSTALL, a, "error: target not found: nosuch\n", 1)
        self.assertIn("target not found", error)

    def test_a_simulated_removal_of_a_protected_package_is_refused(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "apk,zypper,pacman"}):
            z = pk.check(REMOVE, args("zypper", "patterns-base"), SUSE)
            got = pk.assess(REMOVE, z, sections("patterns-base 1-1\n", "The following 2 packages are going to be REMOVED:\n  patterns-base openssh\n"))
            self.assertIn("openssh", got["refusal"])
            p = pk.check(REMOVE, args("pacman", "htop"), ARCH)
            got = pk.assess(REMOVE, p, sections("htop 3.3.0-1\n", "htop 3.3.0-1\nsudo 1.9.15-1\n"))
            self.assertIn("sudo", got["refusal"])

    def test_installed_formats(self) -> None:
        self.assertEqual(pk.parse_installed("zypper", "htop 3.3.0-1.1\npackage nope is not installed\n"), {"htop": "3.3.0-1.1"})
        self.assertEqual(pk.parse_installed("pacman", "htop 3.3.0-1\nlinux 1:6.8.1-1\n"), {"htop": "3.3.0-1", "linux": "1:6.8.1-1"})
        self.assertEqual(pk.parse_installed("apk", APK_LIST), {"htop": "3.3.0-r0", "py3-foo": "1.0.2-r1"})
        self.assertEqual(pk.parse_installed("apk", ""), {})
        self.assertEqual(pk.parse_installed("pnpm", PNPM_LIST), {"is-odd": "3.0.1"})
        self.assertEqual(pk.parse_installed("pnpm", PNPM_EMPTY), {})
        self.assertIsNone(pk.parse_installed("pnpm", "ERR_PNPM_NO_GLOBAL_BIN_DIR"))
        self.assertEqual(pk.parse_installed("yarn", "cowsay 1.6.0\n"), {"cowsay": "1.6.0"})
        self.assertEqual(pk.parse_installed("pipx", PIPX_LIST), {"cowsay": "6.1", "pycowsay": "0.0.0.2"})
        self.assertEqual(pk.parse_installed("uv", UV_LIST), {"cowsay": "6.1", "pycowsay": "0.0.0.2"})
        self.assertEqual(pk.parse_installed("uv", ""), {})

    def test_python_names_are_compared_normalized(self) -> None:
        self.assertEqual(pk.parse_installed("pipx", "Zope.Interface 6.0\n"), {"zope-interface": "6.0"})
        a = pk.check(INSTALL, args("pipx", "Zope.Interface"), USER_TOOLS)
        self.assertTrue(pk.verify(INSTALL, a, {}, {"installed": {"zope-interface": "6.0"}, "services": []}, 0)["ok"])
        r = pk.check(REMOVE, args("uv", "Zope_Interface"), USER_TOOLS)
        self.assertIn("is still installed", pk.verify(REMOVE, r, {}, {"installed": {"zope-interface": "6.0"}, "services": []}, 0)["wrong"][0])
        got = pk.assess(REMOVE, r, sections("zope-interface v6.0\n", "", 0))
        self.assertIsNone(got["error"])

    def test_zypper_notices_are_a_success(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGES_ALLOW_UNVERIFIED": "zypper"}):
            a = pk.check(INSTALL, args("zypper", "htop"), SUSE)
        after = {"installed": {"htop": "3.3.0-1.1"}, "services": []}
        for code, ok in ((0, True), (102, True), (103, True), (104, False)):
            self.assertEqual(pk.verify(INSTALL, a, {}, after, code)["ok"], ok, code)
        self.assertFalse(pk.verify(INSTALL, pk.check(INSTALL, args("apt", "htop"), facts()), {}, {"installed": {"htop": "1"}, "services": []}, 102)["ok"])


class FakeAgent:
    """Answers the measure commands from a queue, the host_facts read from `facts_out`, and records everything."""

    def __init__(self, measures: List[str], run_exit: int = 0, facts_out: str = ""):
        self.measures, self.run_exit, self.facts_out = list(measures), run_exit, facts_out
        self.calls: List[Dict[str, Any]] = []

    def execute_remote_ssh(self, target: str, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        self.calls.append({"target": target, "command": command, "labels": labels, "timeout": timeout_sec})
        op = (labels or {}).get("op", "")
        if op == "host_facts":
            return {"exit_code": 0, "stdout": self.facts_out, "stderr": ""}
        if op.endswith(":preview"):
            return {"exit_code": 0, "stdout": self.measures.pop(0) if self.measures else "", "stderr": ""}
        return {"exit_code": self.run_exit, "stdout": "done", "stderr": ""}

    def execute_command(self, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        return self.execute_remote_ssh("local", command, timeout_sec, labels)

    def runs(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if not (c["labels"]["op"].endswith(":preview") or c["labels"]["op"] == "host_facts")]


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        ops_facts.store_facts("n1", facts())
        self.addCleanup(lambda: os.path.exists(ops_facts.facts_dir() / "n1.json") and os.unlink(ops_facts.facts_dir() / "n1.json"))

    def client(self, agent: Any) -> MCPClient:
        return MCPClient(project=parse_project({**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE]}}), agent_client=agent)

    def test_validate_resolves_auto_and_applies_the_facts(self) -> None:
        c = self.client(FakeAgent([]))
        with _FixedTargets():
            out = c.validate(INSTALL, args("auto", "htop"))
            self.assertEqual(out["manager"], "apt")
            with self.assertRaisesRegex(ActionValidationError, "protected"):
                c.validate(REMOVE, args("auto", "openssh-server"))
            with self.assertRaisesRegex(ActionValidationError, "not installed"):
                c.validate(INSTALL, args("dnf", "htop"))
            ops_facts.store_facts("n1", facts(sudo_nopasswd=False))
            with self.assertRaisesRegex(ActionValidationError, "sudo_required"):
                c.validate(INSTALL, args("apt", "htop"))

    def test_facts_are_read_live_when_not_cached(self) -> None:
        os.unlink(ops_facts.facts_dir() / "n1.json")
        text = ("os_family: linux\nos_id: ubuntu\nos_version: 24.04\nkernel: 6.8.0\narch: arm64\ninit: systemd\nuser: ubuntu\n"
                "root: false\nsudo_nopasswd: true\nmanager: apt-get /usr/bin/apt-get\nfacts_complete: yes\n")
        parsed = ops_facts.parse_host_facts(text)
        self.assertTrue(parsed["complete"], parsed)
        agent = FakeAgent([], facts_out=text)
        with _FixedTargets():
            self.client(agent).validate(INSTALL, args("auto", "htop"))
        self.assertEqual([c["labels"]["op"] for c in agent.calls], ["host_facts"])
        self.assertIsNotNone(ops_facts.load_facts("n1"))  # and now cached
        self.assertEqual(agent.calls[0]["labels"]["source"], "ops_check")

    def test_incomplete_facts_refuse_the_action(self) -> None:
        os.unlink(ops_facts.facts_dir() / "n1.json")
        agent = FakeAgent([], facts_out="os_family: linux\n")
        with _FixedTargets(), self.assertRaisesRegex(ActionValidationError, "facts are not available"):
            self.client(agent).validate(INSTALL, args("auto", "htop"))

    def test_execute_measures_runs_and_verifies(self) -> None:
        a = pk.check(INSTALL, args("apt", "htop"), facts())
        agent = FakeAgent([sections("", APT_INSTALL, 0, "ssh.service\n"),
                           sections("htop 3.3.0-4build1 ii", "", 0, "ssh.service\n")])
        res = ops_actions.execute(agent, "n1", INSTALL, a, "fleet", 7)
        self.assertEqual(res["status"], "SUCCESS", res)
        self.assertTrue(res["verify"]["ok"])
        self.assertEqual(len(agent.runs()), 1)
        self.assertEqual(agent.runs()[0]["labels"], {"source": "action", "op": "package_install", "project": "fleet", "action": "7"})
        self.assertGreaterEqual(agent.calls[0]["timeout"], ops_actions.PACKAGE_MEASURE_TIMEOUT)

    def test_execute_does_not_run_when_the_simulation_would_remove_a_protected_package(self) -> None:
        a = pk.check(REMOVE, args("apt", "ubuntu-server"), facts())
        agent = FakeAgent([sections("ubuntu-server 1.539 ii", APT_REMOVE_SSH)])
        res = ops_actions.execute(agent, "n1", REMOVE, a, "fleet", 8)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("openssh-server", res["error"])
        self.assertEqual(agent.runs(), [])

    def test_a_run_that_leaves_the_package_missing_is_a_failure(self) -> None:
        a = pk.check(INSTALL, args("apt", "htop"), facts())
        agent = FakeAgent([sections("", APT_INSTALL), sections("", "")])
        res = ops_actions.execute(agent, "n1", INSTALL, a, "fleet", 9)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("htop is not installed", res["verify"]["wrong"])

    def test_drift_compares_the_simulated_items(self) -> None:
        stored = {"items": ["add:htop=3.3.0-4build1"], "bytes": None}
        self.assertIsNone(ops_actions.drift(stored, {"items": ["add:htop=3.3.0-4build1"], "bytes": None}))
        self.assertIn("also act on", ops_actions.drift(stored, {"items": ["add:htop=3.3.0-4build1", "add:evil=1"], "bytes": None}) or "")

    def test_default_policy_does_not_offer_packages(self) -> None:
        self.assertNotIn(INSTALL, parse_project(OPS).policy["allowed_actions"])
        self.assertIn(INSTALL, parse_project({**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE]}}).policy["allowed_actions"])


if __name__ == "__main__":
    unittest.main()

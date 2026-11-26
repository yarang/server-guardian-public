"""The sandbox helpers of open resident agents (docs/design/resident-open-tools.md, O2): the node.

Everything runs against a temp sandbox layout with SandboxPaths injected - the root uid, the
gsa-<name> user and group lookups and the daemon's supplementary groups are the test's own - so no
root, no sudo and no real sandbox unit is needed. The MCP copy is installed from this repository's
cli/llm-agent, so the closure test reads the real module set. No provider CLI ever runs here; the
task bundles of O3 are built from a real imported snapshot, so git runs in temp dirs. The neutral
name node-1 is used.
"""

import ast
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import resident_driver
import resident_open
from repo_snapshot import build_snapshot, import_stream
from repo_tools import RepoTools
from resident_open import SandboxPaths
from test_repo_tools import git, make_repo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
VERSION = "2747b475ef4a"  # a C1-shaped sandbox version (the node-1 probe of 2026-11-18)


class SandboxCase(unittest.TestCase):
    """One temp sandbox layout with everything installed, and a pass-through open_problem."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="sgo", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.sandbox = self.dir / "sandbox"
        self.sandbox.mkdir()
        self.wrapper = self._root_file("guardian-sandbox-run")
        self.launcher = self._root_file("launch.py")
        self.installed = self.sandbox / "installed.json"
        self.sock_root = self.sandbox / "sock"
        self.sock_root.mkdir()
        self.paths = SandboxPaths(
            wrapper=self.wrapper, launcher=self.launcher, installed=self.installed,
            sock_root=self.sock_root, home_root=self.dir / "home", root_uid=os.getuid(),
            daemon_uid=os.getuid, agent_gid=lambda name: os.getgid(),
            member_gids=lambda: [os.getgid()])
        self.versions = resident_open.expected_cli_versions()
        self.write_installed()
        self.sock_dir()

    # -- layout ---------------------------------------------------------------------------------

    def _root_file(self, name: str) -> Path:
        path = self.sandbox / name
        path.write_text("#!/bin/sh\n# a stand-in for the root-installed file\n", encoding="utf-8")
        os.chmod(path, 0o755)
        return path

    def write_installed(self, version: Optional[str] = VERSION, agents: Any = ("claude",),
                        cli: Any = None) -> None:
        record = {"version": version, "agents": list(agents),
                  "cli": dict(self.versions) if cli is None else cli}
        self.installed.write_text(json.dumps(record), encoding="utf-8")

    def sock_dir(self, name: str = "claude") -> Path:
        path = self.paths.sock_dir(name)
        path.mkdir(mode=0o2750, exist_ok=True)
        os.chown(path, -1, os.getgid())  # BSD mkdir takes the parent's group (wheel under /var)
        os.chmod(path, 0o2750)
        return path

    def open_spec(self, provider: str = "claude", **extra: Any) -> Dict[str, Any]:
        base = {"address": f"{provider}@node-1", "provider": provider, "driver": "headless",
                "tools": "open", "sandbox": {"version": VERSION}, "card": "start card", "peers": []}
        return {**base, **extra}

    def bind_socket(self, directory: Path, name: str) -> Path:
        """A real unix socket file, as a leftover incarnation would leave it."""
        path = directory / name
        end = socket.socket(socket.AF_UNIX)
        self.addCleanup(end.close)
        end.bind(str(path))
        return path


class SandboxPathsTests(unittest.TestCase):
    def test_the_defaults_are_the_production_paths(self) -> None:
        paths = SandboxPaths()
        self.assertEqual((str(paths.wrapper), str(paths.launcher), str(paths.installed),
                          str(paths.sock_root), str(paths.home_root)),
                         (resident_open.WRAPPER, resident_open.LAUNCHER, resident_open.INSTALLED,
                          resident_open.SOCK_ROOT, resident_open.HOME_ROOT))
        self.assertEqual(paths.root_uid, 0)
        self.assertEqual(paths.agent_user("claude"), "gsa-claude")
        self.assertEqual(paths.agent_group("claude"), "gsa-claude")
        self.assertEqual(str(paths.home("claude")), resident_open.HOME_ROOT + "/gsa-claude")
        self.assertEqual(str(paths.workdir("claude")),
                         resident_open.HOME_ROOT + "/gsa-claude/work")


class CapabilityTests(SandboxCase):
    def test_ready_when_both_entry_points_are_root_files_and_installed_json_parses(self) -> None:
        self.assertEqual(resident_open.capability(self.paths),
                         {"open_tools": True, "sandbox_version": VERSION})

    def test_not_ready_when_a_file_is_missing_a_symlink_or_not_root_owned(self) -> None:
        self.wrapper.unlink()
        self.assertEqual(resident_open.capability(self.paths)["open_tools"], False)
        self.wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(self.wrapper, 0o755)
        elsewhere = self.sandbox / "elsewhere"
        elsewhere.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(elsewhere, 0o755)
        self.launcher.unlink()
        self.launcher.symlink_to(elsewhere)  # a symlink is not a regular file
        self.assertEqual(resident_open.capability(self.paths)["open_tools"], False)
        self.launcher.unlink()
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(self.launcher, 0o755)
        other_root = SandboxPaths(wrapper=self.wrapper, launcher=self.launcher,
                                  installed=self.installed, root_uid=os.getuid() + 4000)
        self.assertEqual(resident_open.capability(other_root)["open_tools"], False)

    def test_not_ready_when_installed_json_does_not_parse_and_version_null_when_absent(self) -> None:
        self.installed.write_text("{not json", encoding="utf-8")
        self.assertEqual(resident_open.capability(self.paths),
                         {"open_tools": False, "sandbox_version": None})
        self.write_installed(version=None)
        self.assertEqual(resident_open.capability(self.paths),
                         {"open_tools": True, "sandbox_version": None})


class OpenProblemTests(SandboxCase):
    def test_no_problem_when_the_sandbox_matches_the_spec(self) -> None:
        self.assertIsNone(resident_open.open_problem("claude", self.open_spec(), self.paths))

    def test_blocked_wins_over_every_later_reason(self) -> None:
        self.wrapper.unlink()  # would be sandbox_missing on its own
        self.assertEqual(resident_open.open_problem(
            "claude", self.open_spec(blocked="hub_said_no"), self.paths), "hub_said_no")

    def test_provider_name_and_their_order(self) -> None:
        self.assertEqual(resident_open.open_problem("gemini", self.open_spec(provider="gemini"),
                                                    self.paths), "open_provider")
        # the provider check comes before the name check
        self.assertEqual(resident_open.open_problem("Bad Name", self.open_spec(provider="gemini"),
                                                    self.paths), "open_provider")
        self.assertIsNone(resident_open.open_problem("claude", self.open_spec(provider="codex"),
                                                     self.paths))
        self.assertEqual(resident_open.open_problem("Bad_Name", self.open_spec(), self.paths),
                         "open_name")
        self.assertEqual(resident_open.open_problem("claude-with-a-too-long-name-1234",
                                                    self.open_spec(), self.paths), "open_name")

    def test_sandbox_missing_for_files_and_installed_json(self) -> None:
        self.launcher.unlink()
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_missing")
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(self.launcher, 0o755)
        self.installed.write_text("[", encoding="utf-8")
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_missing")

    def test_version_mismatch_including_a_spec_without_a_version(self) -> None:
        self.write_installed(version="0f0f0f0f0f0f")
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_version_mismatch")
        self.write_installed()
        spec = self.open_spec()
        del spec["sandbox"]
        self.assertEqual(resident_open.open_problem("claude", spec, self.paths),
                         "sandbox_version_mismatch")
        spec = self.open_spec(sandbox={})
        self.assertEqual(resident_open.open_problem("claude", spec, self.paths),
                         "sandbox_version_mismatch")

    def test_agent_and_cli_entries(self) -> None:
        self.write_installed(agents=[])
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_agent_missing")
        self.write_installed(cli={})
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_cli_missing")
        self.write_installed(cli={**self.versions, "claude": "0.0.1"})
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_cli_missing")

    def test_the_socket_directory_must_be_a_real_directory_owned_by_the_daemon(self) -> None:
        self.paths.sock_dir("claude").rmdir()
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_sock_dir")
        plain = self.paths.sock_dir("claude")
        plain.write_text("not a directory", encoding="utf-8")
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_sock_dir")
        plain.unlink()
        real = self.sock_dir()
        elsewhere = self.sandbox / "elsewhere"
        elsewhere.mkdir()
        real.rmdir()
        real.symlink_to(elsewhere)  # a symlink to a directory is not a real directory
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_sock_dir")
        real.unlink()
        self.sock_dir()
        self.paths.daemon_uid = lambda: os.getuid() + 4000  # owned by someone else (the agent)
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_sock_dir")
        self.paths.daemon_uid = os.getuid
        real.chmod(0o2770)  # group-writable: the agent could swap entries under the daemon
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_sock_dir")
        real.chmod(0o2750)
        self.assertIsNone(resident_open.open_problem("claude", self.open_spec(), self.paths))
        gid = os.getgid()
        self.paths.agent_gid = lambda name: gid + 4000
        self.paths.member_gids = lambda: [gid, gid + 4000]  # a member, but not the dir's group
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_sock_dir")

    def test_the_agent_group_must_be_one_of_the_daemons_groups(self) -> None:
        self.paths.agent_gid = lambda name: None
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_group")
        self.paths.agent_gid = lambda name: os.getgid()
        self.paths.member_gids = lambda: [os.getgid() + 4000]
        self.assertEqual(resident_open.open_problem("claude", self.open_spec(), self.paths),
                         "sandbox_group")
        self.paths.member_gids = lambda: [os.getgid()]
        self.assertIsNone(resident_open.open_problem("claude", self.open_spec(), self.paths))


class WrapperTests(SandboxCase):
    def test_stop_runs_the_wrapper_through_passwordless_sudo(self) -> None:
        calls: List[Any] = []

        def run(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)

        self.assertTrue(resident_open.stop("claude", self.paths, run=run))
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["sudo", "-n", str(self.wrapper), "--stop", "claude"])
        self.assertEqual(kwargs.get("timeout"), resident_open.STOP_TIMEOUT)
        self.assertTrue(kwargs.get("capture_output"))

    def test_stop_is_false_on_a_failure_or_when_sudo_cannot_run(self) -> None:
        false_run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1)  # noqa: E731
        self.assertFalse(resident_open.stop("claude", self.paths, run=false_run))

        def broken(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
            raise OSError("no sudo")

        self.assertFalse(resident_open.stop("claude", self.paths, run=broken))

    def test_turn_command_is_the_wrapper_with_the_launcher_after_the_dash_dash(self) -> None:
        self.assertEqual(resident_open.turn_command("claude", 930, self.paths),
                         ["sudo", "-n", str(self.wrapper), "--timeout", "930",
                          "--chdir", str(self.dir / "home" / "gsa-claude" / "work"), "claude", "--",
                          "/usr/bin/python3", "-I", str(self.launcher)])
        # the whole number is the caller's to shape; the command itself stays literal
        self.assertEqual(resident_open.turn_command("agy", 1, self.paths)[-1], str(self.launcher))


class LaunchInputTests(unittest.TestCase):
    def test_the_exact_frame_of_c2(self) -> None:
        payload = resident_open.launch_input("claude", "2.1.301", ["claude", "--print"],
                                             {".guardian/turn/mcp.json": "{}"}, "the prompt")
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(json.loads(payload.decode("utf-8")),
                         {"format": 1, "provider": "claude", "version": "2.1.301",
                          "argv": ["claude", "--print"],
                          "files": {".guardian/turn/mcp.json": "{}"}, "stdin": "the prompt"})

    def test_every_rule_the_launcher_would_refuse(self) -> None:
        for bad in (
            lambda: resident_open.launch_input("gemini", "1.0", ["gemini"], {}, ""),
            lambda: resident_open.launch_input("claude", "../escape", ["claude"], {}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["agy"], {}, ""),   # argv[0]
            lambda: resident_open.launch_input("claude", "1.0", [], {}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["claude"], {"/abs": "x"}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["claude"], {"": "x"}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["claude"], {"a/../b": "x"}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["claude"], {"a\0b": "x"}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["claude"], {"a": 5}, ""),
            lambda: resident_open.launch_input("claude", "1.0", ["claude"], {}, b"bytes"),
        ):
            with self.assertRaises(ValueError):
                bad()


class McpCopyTests(SandboxCase):
    def test_the_copy_holds_the_closure_with_the_contract_modes(self) -> None:
        sock = self.sock_dir()
        dest = resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertEqual(dest, sock / "mcp" / "abc123def456")
        for module in resident_open.MCP_COPY_MODULES:
            path = dest / f"{module}.py"
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path.read_bytes(), (HERE / f"{module}.py").read_bytes(), module)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640, module)
        self.assertEqual(stat.S_IMODE((sock / "mcp").stat().st_mode), 0o2750)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o2750)

    def test_identical_files_are_skipped_and_changed_files_are_rewritten(self) -> None:
        dest = resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        target = dest / "mailbox_mcp.py"
        stamp = target.stat().st_mtime_ns
        resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertEqual(target.stat().st_mtime_ns, stamp)  # untouched, not rewritten
        target.write_text("tampered", encoding="utf-8")
        resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertEqual(target.read_bytes(), (HERE / "mailbox_mcp.py").read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_other_versions_go_by_hand_a_symlink_is_unlinked_never_followed(self) -> None:
        sock = self.sock_dir()
        dest = resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        old = sock / "mcp" / "oldversion01"
        old.mkdir()
        precious = self.dir / "precious.txt"
        precious.write_text("keep me", encoding="utf-8")
        (old / "evil.py").symlink_to(precious)
        (old / "real.py").write_text("x", encoding="utf-8")
        nested = old / "nested"
        nested.mkdir()
        (nested / "deep.py").write_text("x", encoding="utf-8")
        loose = sock / "mcp" / "loose.txt"
        loose.write_text("x", encoding="utf-8")
        resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertFalse(old.exists())
        self.assertFalse(loose.exists())
        self.assertEqual(precious.read_text(), "keep me")  # unlinked, not followed
        self.assertTrue(dest.is_dir())                     # the current copy stays

    def test_the_module_never_uses_rmtree(self) -> None:
        # C7 forbids rmtree on the agent's socket directory; the module must not even import it.
        tree = ast.parse((HERE / "resident_open.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertNotIn("shutil", [alias.name for alias in node.names])
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "shutil")
            elif isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, "rmtree")

    def test_a_tampered_target_refuses_instead_of_writing_through_it(self) -> None:
        dest = resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        target = dest / "mailbox_mcp.py"
        elsewhere = self.dir / "outside.txt"
        elsewhere.write_text("do not touch", encoding="utf-8")
        target.unlink()
        target.symlink_to(elsewhere)
        with self.assertRaises(OSError):  # O_NOFOLLOW: the symlink is refused, never followed
            resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertEqual(elsewhere.read_text(), "do not touch")


def _stat_with_gid(info: "os.stat_result", gid: int) -> "os.stat_result":
    """A copy of one stat result with another st_gid (os.stat_result cannot be mutated)."""
    return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid,
                           gid, info.st_size, info.st_atime, info.st_mtime, info.st_ctime))


class GroupRepairTests(SandboxCase):
    """The agent's group gsa-<name> is never left to the setgid parent alone: the daemon once
    chmod'ed mcp/ while not yet a member of the group, Linux strips S_ISGID then, and every entry
    below carried the daemon's own group - the agent could not read its MCP copy. The group is
    set and repaired explicitly instead. These tests run as an ordinary user, so a chown to
    another group is only recorded, never performed; a wrapped _lstat stands in for the group a
    repair would have left behind."""

    def _lstat_reporting(self, gid: int, real_first: bool = False) -> Any:
        """A stand-in for resident_open._lstat that reports `gid` as every path's group - at once,
        or, with real_first, only from the second look at each path onwards (as though the repair
        had happened between the two looks)."""
        real = resident_open._lstat
        seen: Any = set()

        def wrapped(path: Path) -> Any:
            info = real(path)
            if info is None:
                return None
            if real_first and path not in seen:
                seen.add(path)
                return info
            return _stat_with_gid(info, gid)

        return wrapped

    def test_a_directory_with_the_wrong_group_is_chowned_and_chmodded_afterwards(self) -> None:
        directory = self.sock_dir() / "mcp"
        directory.mkdir()
        os.chmod(directory, 0o750)  # what a stripped S_ISGID bit leaves behind
        agent_gid = os.getgid() + 4000
        chowns: List[Any] = []

        def record_chown(path: Any, uid: int, gid: int, **kwargs: Any) -> None:
            # the mode snapshot proves the chmod had not run yet when the chown was made
            chowns.append((path, uid, gid, stat.S_IMODE(os.stat(directory).st_mode)))

        with mock.patch("resident_open._lstat",
                        side_effect=self._lstat_reporting(agent_gid, real_first=True)), \
                mock.patch("os.chown", side_effect=record_chown):
            resident_open._ensure_dir(directory, 0o2750, agent_gid)
        self.assertEqual(len(chowns), 1)
        path, uid, gid, mode_then = chowns[0]
        self.assertEqual((path, uid, gid), (directory, -1, agent_gid))
        self.assertEqual(mode_then, 0o750)                                     # before the chmod
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o2750)     # and after it

    def test_ensure_dir_raises_when_the_group_still_differs_after_the_repair(self) -> None:
        directory = self.sock_dir() / "mcp"
        directory.mkdir()
        found = directory.stat().st_gid
        agent_gid = found + 4000
        with mock.patch("os.chown") as chown:  # records and "succeeds" but changes nothing
            with self.assertRaises(OSError) as caught:
                resident_open._ensure_dir(directory, 0o2750, agent_gid)
        chown.assert_called_once()
        message = str(caught.exception)  # names the path and both gids, never file contents
        self.assertIn(str(directory), message)
        self.assertIn(str(agent_gid), message)
        self.assertIn(str(found), message)

    def test_an_mcp_file_with_the_right_bytes_and_mode_but_the_wrong_group_is_repaired(self) -> None:
        dest = resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        target = dest / "mailbox_mcp.py"
        stamp = target.stat().st_mtime_ns
        agent_gid = os.getgid() + 4000
        self.paths.agent_gid = lambda name: agent_gid
        fchowns: List[Any] = []
        with mock.patch("resident_open._lstat", side_effect=self._lstat_reporting(agent_gid)), \
                mock.patch("os.fchown",
                           side_effect=lambda fd, uid, gid: fchowns.append((fd, uid, gid))):
            resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertEqual(len(fchowns), len(resident_open.MCP_COPY_MODULES))  # repaired, not skipped
        for _fd, uid, gid in fchowns:
            self.assertEqual((uid, gid), (-1, agent_gid))
        self.assertEqual(target.stat().st_mtime_ns, stamp)                   # and never rewritten

    def test_a_missing_agent_group_refuses_the_copy_before_anything_is_created(self) -> None:
        self.paths.agent_gid = lambda name: None
        with self.assertRaises(OSError):
            resident_open.install_mcp_copy("claude", "abc123def456", HERE, self.paths)
        self.assertFalse((self.sock_dir() / "mcp").exists())


class StaleSocketTests(SandboxCase):
    def test_only_real_sockets_are_removed_and_the_current_one_stays(self) -> None:
        directory = self.sock_dir()
        stale = self.bind_socket(directory, "e1.sock")
        plain = directory / "e5.sock"
        plain.write_text("not a socket", encoding="utf-8")
        current = self.bind_socket(directory, "e7.sock")
        resident_open.remove_stale_sockets(directory, keep=current)
        self.assertFalse(stale.exists())
        self.assertTrue(plain.exists())     # a name alone proves nothing
        self.assertTrue(current.exists())

    def test_clear_removes_every_entry_but_keeps_the_directory_itself(self) -> None:
        directory = self.sock_dir()
        stale = self.bind_socket(directory, "e1.sock")
        mcp = directory / "mcp" / "abc123def456"
        mcp.mkdir(parents=True)
        (mcp / "mailbox_mcp.py").write_text("x", encoding="utf-8")
        planted = directory / "planted.txt"
        planted.write_text("x", encoding="utf-8")
        resident_open.clear_sock_dir("claude", self.paths)
        self.assertFalse(stale.exists())
        self.assertFalse(mcp.exists())
        self.assertFalse(planted.exists())
        self.assertTrue(directory.is_dir())  # the installer's directory stays


class TaskBundleCase(SandboxCase):
    """A real stored snapshot (built from a temp git repository and imported like a worker would
    import it) to bundle from, in the temp sandbox layout. No hub, no relay; real git only."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.dir / "ws")
        sha = git(self.repo, "rev-parse", "HEAD")
        streams = self.dir / "streams"
        streams.mkdir(mode=0o700)
        snap = build_snapshot(RepoTools(self.repo, sha), streams)
        store = self.dir / "snapshots"
        with open(snap.path, "rb") as source:
            self.commit = import_stream(store, snap.key, source)
        self.git_dir = store / snap.key
        self.tasks = self.sock_dir() / "tasks"

    def write(self, task: str = "k-1") -> Path:
        bundle = resident_open.write_task_bundle("claude", task, self.git_dir, self.commit, self.paths)
        self.assertIsNotNone(bundle)
        assert bundle is not None
        return bundle


class TaskBundleTests(TaskBundleCase):
    def test_the_bundle_is_written_with_the_contract_modes_and_the_snapshot_commit(self) -> None:
        bundle = self.write()
        self.assertEqual(bundle, self.tasks / "k-1.bundle")
        self.assertEqual(stat.S_IMODE(self.tasks.stat().st_mode), 0o2750)
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o640)
        self.assertEqual(resident_open._bundle_ref(bundle), self.commit)
        # a real clone of the bundle checks out the snapshot commit (the MCP's own flow)
        work = self.dir / "clone"
        cloned = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(bundle), str(work)],
                                capture_output=True)
        self.assertEqual(cloned.returncode, 0, cloned.stderr.decode()[-300:])
        checked = subprocess.run(["git", "checkout", "--quiet", self.commit], cwd=str(work),
                                 capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr.decode()[-300:])
        self.assertEqual((work / "apps/agent/notes.txt").read_text(encoding="utf-8")
                         .splitlines()[0], "alpha")

    def test_a_bundle_of_the_same_commit_is_left_alone_and_a_stale_one_is_replaced(self) -> None:
        bundle = self.write()
        stamp = bundle.stat().st_mtime_ns
        self.assertEqual(resident_open.write_task_bundle("claude", "k-1", self.git_dir, self.commit,
                                                         self.paths), bundle)
        self.assertEqual(bundle.stat().st_mtime_ns, stamp)  # untouched, not rebuilt
        bundle.write_bytes(b"# v2 git bundle\nnot a ref\n")  # a bundle of some other commit
        self.assertEqual(resident_open.write_task_bundle("claude", "k-1", self.git_dir, self.commit,
                                                         self.paths), bundle)
        self.assertEqual(resident_open._bundle_ref(bundle), self.commit)  # rewritten

    def test_a_reused_bundle_with_the_wrong_group_is_repaired_not_rebuilt(self) -> None:
        bundle = self.write()
        stamp = bundle.stat().st_mtime_ns
        other = os.getgid() + 1  # an older bundle written while the setgid bit was lost
        self.paths.agent_gid = lambda name: other
        fixed: List[Any] = []
        with mock.patch.object(resident_open, "_ensure_dir"), \
                mock.patch.object(resident_open, "_fix_file_group",
                                  side_effect=lambda path, mode, gid: fixed.append((path, mode, gid))):
            self.assertEqual(resident_open.write_task_bundle("claude", "k-1", self.git_dir, self.commit,
                                                             self.paths), bundle)
        self.assertEqual(fixed, [(bundle, 0o640, other)])
        self.assertEqual(bundle.stat().st_mtime_ns, stamp)  # not rebuilt

    def test_a_failing_git_run_writes_nothing_and_leaves_no_temp_behind(self) -> None:
        def failing(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, 128, stderr=b"fatal: not a repository")

        self.assertIsNone(resident_open.write_task_bundle("claude", "k-1", self.git_dir, self.commit,
                                                          self.paths, run=failing))
        self.assertFalse(self.tasks.joinpath("k-1.bundle").exists())
        self.assertEqual(list(self.tasks.iterdir()), [])

    def test_a_missing_agent_group_refuses_instead_of_writing(self) -> None:
        self.paths.agent_gid = lambda name: None
        with self.assertRaises(OSError):
            resident_open.write_task_bundle("claude", "k-1", self.git_dir, self.commit, self.paths)
        self.assertFalse(self.tasks.exists())  # not even the directory

    def test_the_bundle_temp_file_is_chowned_to_the_agent_group_before_the_rename(self) -> None:
        chowns: List[Any] = []

        def record_chown(path: Any, uid: int, gid: int, **kwargs: Any) -> None:
            chowns.append((Path(path), uid, gid))

        with mock.patch("os.chown", side_effect=record_chown):  # an ordinary user cannot chown
            bundle = self.write("k-3")
        self.assertIn((self.tasks / ".k-3.bundle.tmp", -1, os.getgid()), chowns)
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o640)
        self.assertFalse((self.tasks / ".k-3.bundle.tmp").exists())  # renamed into place after

    def test_a_task_id_that_is_not_a_file_name_writes_nothing(self) -> None:
        for bad in ("../x", "a/b", "", "a b", "x" * 65):
            self.assertIsNone(resident_open.write_task_bundle("claude", bad, self.git_dir,
                                                              self.commit, self.paths), bad)
        self.assertFalse(self.tasks.exists())  # not even the directory

    def test_remove_unlinks_after_lstat_and_ignores_names_that_are_not_file_names(self) -> None:
        bundle = self.write()
        decoy = self.sock_dir() / "escape.bundle"  # what ../x.bundle would name if it were joined
        decoy.write_text("precious", encoding="utf-8")
        resident_open.remove_task_bundle("claude", "k-1", self.paths)
        self.assertFalse(bundle.exists())
        self.assertTrue(self.tasks.is_dir())  # the directory stays
        for bad in ("../escape", "a/b", "", None, 5):
            resident_open.remove_task_bundle("claude", bad, self.paths)  # type: ignore[arg-type]
        self.assertTrue(decoy.exists())       # no other path was touched
        resident_open.remove_task_bundle("claude", "k-9", self.paths)  # a missing bundle is fine

    def test_clear_sock_dir_takes_the_tasks_directory_with_it(self) -> None:
        self.write()
        self.write("k-2")
        directory = self.sock_dir()
        resident_open.clear_sock_dir("claude", self.paths)
        self.assertFalse(self.tasks.exists())
        self.assertTrue(directory.is_dir())   # the installer's directory stays, as ever


class VersionTests(unittest.TestCase):
    def test_the_versions_match_the_provider_table_and_the_driver_pin(self) -> None:
        table = json.loads((ROOT / "configs" / "resident-providers.json").read_text(encoding="utf-8"))
        versions = resident_open.expected_cli_versions()
        self.assertEqual(sorted(versions), ["agy", "claude", "codex"])
        self.assertEqual(versions["agy"], table["providers"]["agy"]["version"])
        self.assertEqual(versions["agy"], resident_driver.AGY_VERSION)
        self.assertEqual(versions["agy"], resident_driver.expected_agy_version())
        self.assertEqual(versions["claude"], table["providers"]["claude"]["version"])
        self.assertEqual(versions["codex"], table["providers"]["codex"]["version"])


class McpClosureTests(unittest.TestCase):
    def test_the_copy_list_covers_every_repository_import_of_its_modules(self) -> None:
        """The copy must import nothing outside itself: a module missing from the list would fail
        only inside the sandbox, at turn time, on the node. The scan is the whole module (ast.walk
        finds the lazy imports inside functions too)."""
        repo_modules = {p.stem for p in HERE.glob("*.py")}
        closure = set(resident_open.MCP_COPY_MODULES)
        for module in resident_open.MCP_COPY_MODULES:
            path = HERE / f"{module}.py"
            self.assertTrue(path.is_file(), path)
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
            missing = (found & repo_modules) - closure
            self.assertFalse(missing, f"{module} imports {sorted(missing)}: add to MCP_COPY_MODULES")


if __name__ == "__main__":
    unittest.main()

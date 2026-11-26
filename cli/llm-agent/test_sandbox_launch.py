"""Tests for deploy/sandbox/guardian-sandbox-launch.py (docs/design/resident-open-tools.md, O2, C2).

Loads the launcher from deploy/sandbox with importlib and drives its functions against a fake
HOME and a fake CLI directory (the launcher's parameters), so nothing touches /usr/local/lib or a
real home. main() itself runs as a subprocess: once with --test-mode, the only mode that reads the
GUARDIAN_LAUNCH_TEST_* variables, and once without it to prove those variables stay unread."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHER = ROOT / "deploy" / "sandbox" / "guardian-sandbox-launch.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("guardian_sandbox_launch", str(LAUNCHER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_launcher(args, payload, env_overrides):
    """Run the real file with python3 -I, like the unit does, with the test-mode env or without."""
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-I", str(LAUNCHER)] + args,
                          input=payload, text=True, capture_output=True, timeout=60, env=env)


def make_cli(cli_root, provider="claude", version="2.1.301", body="#!/bin/sh\nexit 0\n", mode=0o755):
    directory = Path(cli_root) / ("%s-%s" % (provider, version))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / provider
    path.write_text(body, encoding="utf-8")
    os.chmod(str(path), mode)
    return str(path)


def write_credential(home, token="tok-1", mode=0o600):
    path = Path(home) / ".guardian" / "credentials" / "claude-oauth-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(str(path), mode)
    return str(path)


def request(**overrides):
    base = {"format": 1, "provider": "claude", "version": "2.1.301",
            "argv": ["claude", "--version"], "files": {}, "stdin": ""}
    base.update(overrides)
    return base


def as_module():
    return load_launcher()


@unittest.skipUnless(LAUNCHER.exists(), "the launcher not found")
class BadInput(unittest.TestCase):
    def refuse(self, raw, reason):
        module = as_module()
        with self.assertRaises(module.LaunchError) as caught:
            module.read_request(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
        self.assertEqual(caught.exception.code, 2, caught.exception.message)
        self.assertTrue(caught.exception.message.startswith("launch: bad_input"), caught.exception.message)
        self.assertIn(reason, caught.exception.message)

    def test_malformed_json(self):
        self.refuse(b"not json at all", "not_json")
        self.refuse("é".encode("latin-1"), "stdin_not_utf8")
        self.refuse([1, 2], "not_object")

    def test_unknown_and_missing_keys(self):
        self.refuse(request(extra=1), "unknown_key:extra")
        for key in ("format", "provider", "version", "argv", "files", "stdin"):
            payload = request()
            del payload[key]
            self.refuse(payload, "missing_key:%s" % key)

    def test_format_provider_version_argv(self):
        for format_value in (2, "1", True, None):
            self.refuse(request(format=format_value), "format")
        for provider in ("gemini", "CLAUDE", ""):
            self.refuse(request(provider=provider, argv=[provider]), "provider")
        for version in ("", "v2", "2.1.301!!", "x" * 40, 2):
            self.refuse(request(version=version), "version")
        self.refuse(request(argv=[]), "argv")
        self.refuse(request(argv=[1, 2]), "argv")
        self.refuse(request(argv=["cla"]), "argv0")
        self.refuse(request(files={"a": 1}), "files")
        self.refuse(request(files={"a": ["b"]}), "files")
        self.refuse(request(stdin=None), "stdin")

    def test_bad_file_paths(self):
        module = as_module()
        for rel, reason in (("", "path_empty"), ("/etc/passwd", "path_absolute"),
                            ("../escape", "path_dotdot"), ("a/../b", "path_dotdot"),
                            ("a//b", "path_component"), ("a/\0b", "path_nul")):
            with self.assertRaises(module.LaunchError) as caught:
                module.check_relative_path(rel)
            self.assertEqual(caught.exception.code, 2)
            self.assertIn(reason, caught.exception.message, rel)


@unittest.skipUnless(LAUNCHER.exists(), "the launcher not found")
class WriteFiles(unittest.TestCase):
    def test_nested_files_are_0600_in_0700_dirs(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "home")
            os.makedirs(home)
            module.write_files(home, {"work/a/b/notes.txt": "hello",
                                      "top.txt": "overwritten"})
            file_path = Path(home) / "work" / "a" / "b" / "notes.txt"
            self.assertEqual(file_path.read_text(encoding="utf-8"), "hello")
            self.assertEqual(oct(stat.S_IMODE(os.stat(str(file_path)).st_mode)), "0o600")
            for directory in (Path(home) / "work", Path(home) / "work" / "a"):
                self.assertEqual(oct(stat.S_IMODE(os.stat(str(directory)).st_mode)), "0o700")
            # a second write replaces the file and keeps the mode
            module.write_files(home, {"top.txt": "second"})
            replaced = Path(home) / "top.txt"
            self.assertEqual(replaced.read_text(encoding="utf-8"), "second")
            self.assertEqual(oct(stat.S_IMODE(os.stat(str(replaced)).st_mode)), "0o600")
            # no temp file is left behind
            names = [p.name for p in Path(home).rglob("*")]
            self.assertNotIn(next((n for n in names if n.startswith(".launch-")), ""), names)

    def test_symlink_components_are_refused(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            os.makedirs(str(home))
            real = home / "real"
            real.mkdir()
            os.symlink("real", str(home / "link"))
            for rel in ("link/file.txt", "link/sub/file.txt"):
                with self.assertRaises(module.LaunchError) as caught:
                    module.write_files(str(home), {rel: "x"})
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("symlink", caught.exception.message)
            # the final component being a symlink is refused too
            os.symlink("real.txt", str(home / "alias.txt"))
            with self.assertRaises(module.LaunchError) as caught:
                module.write_files(str(home), {"alias.txt": "x"})
            self.assertIn("symlink", caught.exception.message)


@unittest.skipUnless(LAUNCHER.exists(), "the launcher not found")
class ResolveCli(unittest.TestCase):
    def test_every_shape_but_the_right_one_is_cli_missing(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp) / "cli")
            make_cli(root, mode=0o644)  # not executable
            for version in ("2.1.301", "9.9.9"):
                with self.assertRaises(module.LaunchError) as caught:
                    module.resolve_cli(root, "claude", version)
                self.assertEqual((caught.exception.code, caught.exception.message), (3, "launch: cli_missing"))
            make_cli(root, mode=0o766)  # executable but group-writable
            with self.assertRaises(module.LaunchError) as caught:
                module.resolve_cli(root, "claude", "2.1.301")
            self.assertEqual(caught.exception.message, "launch: cli_missing")
            # owned by this test user: fine only with the --test-mode owner override
            make_cli(root, mode=0o755)
            with self.assertRaises(module.LaunchError):
                module.resolve_cli(root, "claude", "2.1.301", allow_nonroot=False)
            self.assertEqual(module.resolve_cli(root, "claude", "2.1.301", allow_nonroot=True),
                             str(Path(root) / "claude-2.1.301" / "claude"))

    def test_a_directory_in_place_of_the_cli_is_missing(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cli"
            (root / "claude-2.1.301").mkdir(parents=True)
            (root / "claude-2.1.301" / "claude").mkdir()
            with self.assertRaises(module.LaunchError) as caught:
                module.resolve_cli(str(root), "claude", "2.1.301")
            self.assertEqual(caught.exception.message, "launch: cli_missing")


@unittest.skipUnless(LAUNCHER.exists(), "the launcher not found")
class BuildEnv(unittest.TestCase):
    def test_claude_env_is_exactly_the_listed_keys_with_the_file_token(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "home")
            os.makedirs(home)
            write_credential(home, token="  tok-1  \n")
            user = module.pwd.getpwuid(os.getuid()).pw_name
            self.assertEqual(module.build_env(home, "claude"), {
                "HOME": home, "USER": user, "LOGNAME": user,
                "PATH": "%s:/usr/local/bin:/usr/bin:/bin" % os.path.join(home, ".local/bin"),
                "LANG": "C.UTF-8", "SHELL": "/bin/bash",
                "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-1"})

    def test_bad_credentials_are_refused(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "home")
            os.makedirs(home)
            with self.assertRaises(module.LaunchError) as caught:
                module.build_env(home, "claude")
            self.assertEqual((caught.exception.code, caught.exception.message),
                             (3, "launch: no_credential"))
            write_credential(home, mode=0o644)  # group/other bits set
            with self.assertRaises(module.LaunchError):
                module.build_env(home, "claude")
            write_credential(home, token="   \n", mode=0o600)  # present but empty after strip
            with self.assertRaises(module.LaunchError):
                module.build_env(home, "claude")

    def test_agy_needs_only_its_own_login_marker(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "home")
            os.makedirs(home)
            with self.assertRaises(module.LaunchError) as caught:
                module.build_env(home, "agy")
            self.assertEqual((caught.exception.code, caught.exception.message),
                             (3, "launch: no_credential"))
            token = Path(home) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token.parent.mkdir(parents=True)
            token.write_text("agy-login\n", encoding="utf-8")
            env = module.build_env(home, "agy")
            self.assertEqual(sorted(env), sorted([
                "HOME", "USER", "LOGNAME", "PATH", "LANG", "SHELL", "AGY_CLI_DISABLE_AUTO_UPDATE"]))
            self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")

    def test_codex_needs_0600_auth_json_and_sets_codex_home(self):
        module = as_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = str(Path(tmp) / "home")
            os.makedirs(home)
            with self.assertRaises(module.LaunchError) as caught:
                module.build_env(home, "codex")
            self.assertEqual((caught.exception.code, caught.exception.message),
                             (3, "launch: no_credential"))
            auth = Path(home) / ".codex" / "auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"tokens": {"access_token": "t"}}\n', encoding="utf-8")
            os.chmod(str(auth), 0o644)  # group/other bits set: refused
            with self.assertRaises(module.LaunchError):
                module.build_env(home, "codex")
            os.chmod(str(auth), 0o600)
            env = module.build_env(home, "codex")
            self.assertEqual(env["CODEX_HOME"], os.path.join(home, ".codex"))
            self.assertTrue(env["PATH"].startswith(os.path.join(home, ".local/bin") + ":"))


@unittest.skipUnless(LAUNCHER.exists(), "the launcher not found")
class MainSubprocess(unittest.TestCase):
    """The real file, started the way the unit starts it, against fakes from the test env."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name) / "home"
        cli_root = Path(self._tmp.name) / "cli"
        os.makedirs(str(home))
        make_cli(str(cli_root), body=(
            "#!/bin/sh\n"
            "printf 'token=%s\\n' \"${CLAUDE_CODE_OAUTH_TOKEN:-none}\"\n"
            "printf 'home=%s\\n' \"${HOME}\"\n"
            "printf 'cwd='; pwd\n"
            "printf 'file='; cat note.txt\n"
            "printf 'stdin='; cat\n"
            "printf 'env='; env | cut -d= -f1 | sort | tr '\\n' ','\n"
            "echo to_stderr >&2\n"
            "exit 42\n"))
        write_credential(str(home))
        self.env = {"GUARDIAN_LAUNCH_TEST_CLI_ROOT": str(cli_root),
                    "GUARDIAN_LAUNCH_TEST_HOME": str(home),
                    "GUARDIAN_LAUNCH_TEST_ALLOW_NONROOT_CLI": "1"}
        self.home = home

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_claude_turn_env_files_stdin_and_exit_code(self):
        payload = json.dumps(request(argv=["claude"], files={"work/note.txt": "hi\n"},
                                     stdin="hello\n"))
        proc = run_launcher(["--test-mode"], payload, self.env)
        self.assertEqual(proc.returncode, 42, proc.stderr)
        lines = proc.stdout.splitlines()
        # /var is a symlink to /private/var on macOS, so compare the resolved working directory
        self.assertEqual(lines[:5], ["token=tok-1", "home=%s" % self.home,
                                     "cwd=%s" % os.path.realpath(str(self.home / "work")),
                                     "file=hi", "stdin=hello"], proc.stdout)
        self.assertEqual(proc.stderr, "to_stderr\n")
        # the CLI saw exactly the C2 environment: the shell itself adds PWD, SHLVL and _
        env_line = [line for line in lines if line.startswith("env=")][0]
        seen = set(env_line[len("env="):].rstrip(",").split(","))
        seen -= {"PWD", "SHLVL", "_"}
        self.assertEqual(sorted(seen), ["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION",
                                        "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER",
                                        "HOME", "LANG", "LOGNAME", "PATH", "SHELL", "USER"])

    def test_signal_death_becomes_128_plus_n(self):
        make_cli(self.env["GUARDIAN_LAUNCH_TEST_CLI_ROOT"], version="3.0.0",
                 body="#!/bin/sh\nkill -TERM $$\n")
        payload = json.dumps(request(version="3.0.0", argv=["claude"]))
        proc = run_launcher(["--test-mode"], payload, self.env)
        self.assertEqual(proc.returncode, 128 + 15, proc.stderr)

    def test_bad_input_exits_2_with_the_reason(self):
        proc = run_launcher(["--test-mode"], json.dumps(request(provider="gemini")), self.env)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("launch: bad_input provider", proc.stderr)

    def test_arguments_are_refused_without_test_mode(self):
        for args in (["--bogus"], ["--test-mode", "extra"], ["positional"]):
            proc = run_launcher(args, "{}", self.env)
            self.assertEqual(proc.returncode, 64, args)
            self.assertIn("launch: usage", proc.stderr)

    def test_the_test_env_is_ignored_without_test_mode(self):
        # GUARDIAN_LAUNCH_TEST_CLI_ROOT points at a working fake CLI; production must not read it,
        # so the real CLI root answers instead: exit 3 (cli_missing where nothing is installed,
        # no_credential on a probe node whose sandbox really carries the claude copy), and the
        # fake home stays untouched either way.
        proc = run_launcher([], json.dumps(request(argv=["claude"])), self.env)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertTrue("launch: cli_missing" in proc.stderr
                        or "launch: no_credential" in proc.stderr, proc.stderr)
        self.assertEqual(sorted(os.listdir(str(self.home))), [".guardian"])

    def test_launch_refuses_a_credential_with_group_or_other_bits(self):
        # sandbox-credential.sh writes 0600 through umask 077; a looser file is refused
        write_credential(str(self.home), mode=0o644)
        proc = run_launcher(["--test-mode"], json.dumps(request(argv=["claude"])), self.env)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("launch: no_credential", proc.stderr)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Sandbox launcher: one open-agent turn inside its unit (docs/design/resident-open-tools.md, O2).

The worker daemon starts this inside the gsa-<agent> sandbox unit as
`/usr/bin/python3 -I /usr/local/lib/guardian-sandbox/launch.py`. stdin carries one UTF-8 JSON
object until EOF:

  {"format": 1,
   "provider": "claude" | "agy",
   "version": "<the M0 table version, e.g. 2.1.301>",
   "argv": ["claude", ...],            # argv[0] must equal provider; replaced with the copy
   "files": {"<path relative to $HOME>": "<text>"},   # written before the CLI starts
   "stdin": "<text for the CLI's stdin>"}

It resolves the pinned CLI copy (root-owned, not group- or world-writable, executable), writes
the requested files under $HOME (parents 0700, files 0600, atomic rename), builds the CLI
environment from nothing (its own environment never reaches the CLI) and runs the CLI in
$HOME/work with stdin, stdout and stderr passed through unchanged. It never prints a credential
or the environment.

Exit: the CLI's exit code (128+n for a signal); 2 `launch: bad_input <short reason>`;
3 `launch: cli_missing`, `launch: no_credential` or `launch: internal <reason>`.
Standard library only, python3.8+.

Tests may start it with the single argument --test-mode; only then does it read
GUARDIAN_LAUNCH_TEST_CLI_ROOT, GUARDIAN_LAUNCH_TEST_HOME and GUARDIAN_LAUNCH_TEST_ALLOW_NONROOT_CLI.
Without that argument every argument is refused and those variables are not even looked at.
"""

import collections
import errno
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile

CLI_ROOT = "/usr/local/lib/guardian-sandbox/cli"
WORK_RELATIVE = "work"
OUT_RELATIVE = "out"
LOCAL_BIN_RELATIVE = ".local/bin"
CREDENTIAL_RELATIVE = ".guardian/credentials/claude-oauth-token"
AGY_TOKEN_RELATIVE = ".gemini/antigravity-cli/antigravity-oauth-token"
CODEX_AUTH_RELATIVE = ".codex/auth.json"
CLI_PATH = "/usr/local/bin:/usr/bin:/bin"
PROVIDERS = ("claude", "agy", "codex")
REQUEST_KEYS = frozenset(("format", "provider", "version", "argv", "files", "stdin"))
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")


class LaunchError(Exception):
    """A refusal with its exit code and the exact stderr line to print."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


Options = collections.namedtuple("Options", "cli_root home allow_nonroot")
Options.__new__.__defaults__ = (None, None, False)


def read_test_options():
    """The --test-mode overrides (tests only; production never calls this)."""
    return Options(cli_root=os.environ.get("GUARDIAN_LAUNCH_TEST_CLI_ROOT") or None,
                   home=os.environ.get("GUARDIAN_LAUNCH_TEST_HOME") or None,
                   allow_nonroot=os.environ.get("GUARDIAN_LAUNCH_TEST_ALLOW_NONROOT_CLI") == "1")


def _errno_name(exc):
    return errno.errorcode.get(exc.errno) or type(exc).__name__


# --- input -----------------------------------------------------------------------------------------

def read_request(raw):
    """stdin bytes -> the validated request dict, or LaunchError(2, launch: bad_input ...)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LaunchError(2, "launch: bad_input stdin_not_utf8")
    try:
        obj = json.loads(text)
    except ValueError:
        raise LaunchError(2, "launch: bad_input not_json")
    if not isinstance(obj, dict):
        raise LaunchError(2, "launch: bad_input not_object")
    extra = sorted(set(obj) - REQUEST_KEYS)
    if extra:
        raise LaunchError(2, "launch: bad_input unknown_key:%s" % extra[0])
    missing = sorted(REQUEST_KEYS - set(obj))
    if missing:
        raise LaunchError(2, "launch: bad_input missing_key:%s" % missing[0])
    if isinstance(obj["format"], bool) or not isinstance(obj["format"], int) or obj["format"] != 1:
        raise LaunchError(2, "launch: bad_input format")
    if obj["provider"] not in PROVIDERS:
        raise LaunchError(2, "launch: bad_input provider")
    if not isinstance(obj["version"], str) or not VERSION_RE.match(obj["version"]):
        raise LaunchError(2, "launch: bad_input version")
    argv = obj["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) for word in argv):
        raise LaunchError(2, "launch: bad_input argv")
    if argv[0] != obj["provider"]:
        raise LaunchError(2, "launch: bad_input argv0")
    files = obj["files"]
    if not isinstance(files, dict):
        raise LaunchError(2, "launch: bad_input files")
    for key, value in files.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise LaunchError(2, "launch: bad_input files")
    if not isinstance(obj["stdin"], str):
        raise LaunchError(2, "launch: bad_input stdin")
    return {"provider": obj["provider"], "version": obj["version"], "argv": argv,
            "files": files, "stdin": obj["stdin"]}


# --- files under $HOME -----------------------------------------------------------------------------

def check_relative_path(rel):
    """One files key: relative, non-empty, no .. or empty component, no NUL byte."""
    if not rel:
        raise LaunchError(2, "launch: bad_input path_empty")
    if os.path.isabs(rel):
        raise LaunchError(2, "launch: bad_input path_absolute:%s" % rel)
    if "\0" in rel:
        raise LaunchError(2, "launch: bad_input path_nul")
    for part in rel.split("/"):
        if part == "..":
            raise LaunchError(2, "launch: bad_input path_dotdot:%s" % rel)
        if part == "":
            raise LaunchError(2, "launch: bad_input path_component:%s" % rel)


def check_no_symlink(home, rel):
    """Refuse a path whose existing components under $HOME (the target included) are symlinks."""
    current = home
    for part in rel.split("/"):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return  # the first missing component: everything below it is created fresh
        except OSError as exc:
            raise LaunchError(2, "launch: bad_input path_error:%s (%s)" % (rel, _errno_name(exc)))
        if stat.S_ISLNK(info.st_mode):
            raise LaunchError(2, "launch: bad_input symlink:%s" % rel)


def ensure_dir(path):
    """Create path and its missing parents, each 0700; existing directories are left alone."""
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
    for item in reversed(missing):
        os.mkdir(item, 0o700)
        os.chmod(item, 0o700)


def write_files(home, files):
    """Write every requested file: temp name + rename, mode 0600, parent directories 0700."""
    for rel in files:
        check_relative_path(rel)
    for rel, text in files.items():
        target = os.path.join(home, rel)
        check_no_symlink(home, rel)
        try:
            ensure_dir(os.path.dirname(target))
            handle_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".launch-")
            try:
                with os.fdopen(handle_fd, "wb") as handle:
                    handle.write(text.encode("utf-8"))
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except LaunchError:
            raise
        except OSError as exc:
            raise LaunchError(2, "launch: bad_input write_failed:%s (%s)" % (rel, _errno_name(exc)))


# --- the CLI copy and the environment ---------------------------------------------------------------

def resolve_cli(cli_root, provider, version, allow_nonroot=False):
    """/usr/local/lib/guardian-sandbox/cli/<provider>-<version>/<provider>, or cli_missing."""
    path = os.path.join(cli_root, "%s-%s" % (provider, version), provider)
    try:
        info = os.stat(path)
    except OSError:
        raise LaunchError(3, "launch: cli_missing")
    if not stat.S_ISREG(info.st_mode):
        raise LaunchError(3, "launch: cli_missing")
    if info.st_uid != 0 and not allow_nonroot:
        raise LaunchError(3, "launch: cli_missing")
    if info.st_mode & 0o022 or not info.st_mode & 0o111:
        raise LaunchError(3, "launch: cli_missing")
    return path


def claude_token(home):
    """The stripped content of $HOME/.guardian/credentials/claude-oauth-token, or no_credential."""
    path = os.path.join(home, CREDENTIAL_RELATIVE)
    try:
        info = os.stat(path)
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        raise LaunchError(3, "launch: no_credential")
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not raw.strip():
        raise LaunchError(3, "launch: no_credential")
    return raw.decode("utf-8", "replace").strip()


def check_codex_auth(home):
    """Verify $HOME/.codex/auth.json exists, is owned by this user, mode 0600, and non-empty."""
    path = os.path.join(home, CODEX_AUTH_RELATIVE)
    try:
        info = os.stat(path)
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        raise LaunchError(3, "launch: no_credential")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or not raw.strip():
        raise LaunchError(3, "launch: no_credential")


def build_env(home, provider):
    """The CLI environment, built from nothing (C2): never a copy of the launcher's own."""
    entry = pwd.getpwuid(os.getuid())
    local_bin = os.path.join(home, LOCAL_BIN_RELATIVE)
    env = {"HOME": home, "USER": entry.pw_name, "LOGNAME": entry.pw_name,
           "PATH": "%s:%s" % (local_bin, CLI_PATH), "LANG": "C.UTF-8", "SHELL": "/bin/bash"}
    if provider == "claude":
        env["DISABLE_AUTOUPDATER"] = "1"
        env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "false"
        env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token(home)
    elif provider == "agy":
        # agy disables its updater only on the exact lowercase "true"; "1" leaves it on.
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        if not os.path.exists(os.path.join(home, AGY_TOKEN_RELATIVE)):
            raise LaunchError(3, "launch: no_credential")
    elif provider == "codex":
        check_codex_auth(home)
        env["CODEX_HOME"] = os.path.join(home, ".codex")
    else:
        raise LaunchError(2, "launch: bad_input provider")
    return env


# --- the turn ---------------------------------------------------------------------------------------

def run_cli(cli_path, argv, stdin_text, env, cwd):
    """Run the CLI copy: cwd, the given stdin text, stdio passed through; its exit code back."""
    try:
        proc = subprocess.run([cli_path] + list(argv[1:]), input=stdin_text.encode("utf-8"),
                              env=env, cwd=cwd)
    except OSError as exc:
        raise LaunchError(3, "launch: internal %s" % _errno_name(exc))
    if proc.returncode < 0:
        return 128 - proc.returncode  # killed by signal n
    return proc.returncode


def launch(request, options):
    """One validated request -> the CLI's exit code (all refusals raise LaunchError)."""
    home = options.home or pwd.getpwuid(os.getuid()).pw_dir
    cli_path = resolve_cli(options.cli_root or CLI_ROOT, request["provider"],
                           request["version"], options.allow_nonroot)
    work = os.path.join(home, WORK_RELATIVE)
    try:
        ensure_dir(work)
        ensure_dir(os.path.join(home, OUT_RELATIVE))
        ensure_dir(os.path.join(home, LOCAL_BIN_RELATIVE))
    except OSError as exc:
        raise LaunchError(3, "launch: internal work_dir (%s)" % _errno_name(exc))
    write_files(home, request["files"])
    env = build_env(home, request["provider"])
    return run_cli(cli_path, request["argv"], request["stdin"], env, work)


def main(argv):
    if argv and argv != ["--test-mode"]:
        sys.stderr.write("launch: usage: guardian-sandbox-launch.py [--test-mode]\n")
        return 64
    options = read_test_options() if argv else Options()
    try:
        return launch(read_request(sys.stdin.buffer.read()), options)
    except LaunchError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.code
    except OSError as exc:
        sys.stderr.write("launch: internal %s\n" % _errno_name(exc))
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

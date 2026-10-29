"""End-to-end tests of scripts/probe-worker.sh and scripts/install-worker.sh against a fake fleet.

No real host is involved. A fake `ssh` maps host names to directories (each is that host's $HOME) and runs the remote
command locally with HOME set there; the hub's nested `ssh node-1 ...` re-enters the same fake, so the acceptance
check goes through the real chain hub -> ssh -> worker.py client -> unix socket -> the daemon. A fake `systemctl
--user` starts the real worker.py as a detached process, a fake `loginctl` keeps the linger flag in a file, and a
fake `claude` on the node's ~/.local/bin makes the worker list a provider. Facts and CLI check output of the probe are
canned because /proc/meminfo and the real CLIs do not exist here. The scripts run from a throw-away git repository
holding copies of the files they need, so the tests control the revisions (install reads files from a git rev).
"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli" / "llm-agent"))

from remote_agents import PROTOCOL  # noqa: E402

COPIED = [
    "scripts/probe-worker.sh",
    "scripts/install-worker.sh",
    "scripts/lib/inventory.sh",
    "scripts/lib/llm-check.sh",
    "scripts/lib/worker-common.sh",
    "scripts/lib/worker-facts.sh",
    "scripts/lib/worker-remote-install.sh",
    "cli/llm-agent/worker_probe.py",
    "configs/worker-requirements.json",
    "deploy/worker/files.txt",
    "deploy/worker/guardian-worker.service",
]

FAKE_SSH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, subprocess, sys
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--":
            i += 1
            break
        if args[i] in ("-o", "-i", "-p", "-l"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            break
    host, cmd = args[i], " ".join(args[i + 1:])
    world = os.environ["FAKE_WORLD"]
    home = os.path.join(world, "homes", host)
    if not os.path.isdir(home):
        sys.stderr.write("ssh: Could not resolve hostname %s\\n" % host)
        sys.exit(255)
    caller = os.environ.get("FAKE_CALLER", "admin")
    if caller == "hub" and host in os.environ.get("FAKE_UNREACHABLE_FROM_HUB", "").split(","):
        sys.exit(255)
    env = dict(os.environ, HOME=home, FAKE_CALLER="hub" if host == "hub-1" else "node")
    env.pop("GUARDIAN_LLM_PROVIDERS", None)
    stdin = None
    if cmd == "bash -s":
        script = sys.stdin.read()
        canned = None
        if "hostname_short=" in script:
            canned = os.path.join(world, host + ".facts")
        elif "PONG" in script:
            canned = os.path.join(world, host + ".check")
        if canned:
            sys.stdout.write(open(canned).read())
            sys.exit(0)
        stdin = script.encode()
    result = subprocess.run(["bash", "-c", cmd], cwd=home, env=env, input=stdin)
    sys.exit(result.returncode)
    """
)

FAKE_SYSTEMCTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, signal, subprocess, sys, time
    home = os.environ["HOME"]
    state = os.path.join(home, ".fake")
    os.makedirs(state, exist_ok=True)
    pidfile = os.path.join(state, "pid")
    unit = os.path.join(home, ".config", "systemd", "user", "guardian-worker.service")
    args = sys.argv[1:]
    assert args[0] == "--user", args
    action = args[1]

    def stop():
        try:
            pid = int(open(pidfile).read())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(60):
                os.kill(pid, 0)
                time.sleep(0.05)
        except OSError:
            pass
        os.unlink(pidfile)

    if action == "daemon-reload":
        sys.exit(0)
    if action == "disable":
        stop()
        sys.exit(0)
    if not os.path.exists(unit):
        sys.stderr.write("Unit guardian-worker.service not found\\n")
        sys.exit(5)
    text = open(unit).read()
    if "@" in text:
        sys.stderr.write("unit still holds a placeholder\\n")
        sys.exit(6)
    if action == "enable":
        sys.exit(0)
    assert action == "restart", args
    if os.environ.get("FAKE_NO_START"):
        sys.exit(0)
    stop()
    env = dict(os.environ)
    env.pop("GUARDIAN_LLM_PROVIDERS", None)
    for line in text.splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            env[key] = value
    with open(os.path.join(state, "worker.log"), "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(home, ".guardian", "worker", "worker.py"), "serve"],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    open(pidfile, "w").write(str(proc.pid))
    """
)

FAKE_LOGINCTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    state = os.path.join(os.environ["HOME"], ".fake")
    flag = os.path.join(state, "linger")
    os.makedirs(state, exist_ok=True)
    args = sys.argv[1:]
    if args[0] == "show-user":
        print("Linger=" + ("yes" if os.path.exists(flag) else "no"))
    elif args[0] == "enable-linger":
        if os.environ.get("FAKE_LINGER_DENY"):
            sys.exit(1)
        open(flag, "w").close()
    """
)

CAPABLE_FACTS = textwrap.dedent(
    """\
    hostname_short=node-1
    arch=aarch64
    cpu_count=4
    mem_total_kib=8388608
    mem_available_kib=6291456
    python3_version=3.11.2
    git_version=2.39.2
    disk_free_kib=20971520
    systemd_user=yes
    linger=no
    sudo_nopass=no
    cli_path.claude=/home/user/.local/bin/claude
    """
)
SMALL_FACTS = CAPABLE_FACTS.replace("mem_total_kib=8388608", "mem_total_kib=1048576").replace(
    "mem_available_kib=6291456", "mem_available_kib=524288")
CHECK_OK = "  ✅ claude\n"
CHECK_FAILED = "  ❌ claude: Not logged in\n"

INVENTORY = [
    {"id": "hub-1", "host": "100.64.0.1", "guardian_role": "hub", "tier": 1},
    {"id": "node-1", "host": "100.64.0.2", "tier": 1},
    {"id": "node-2", "host": "100.64.0.3", "tier": 2},
]


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class WorkerScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        # /tmp, not TMPDIR: the worker socket path must stay below the unix socket limit (104 bytes on macOS).
        self.world = Path(tempfile.mkdtemp(prefix="sgw", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.world, ignore_errors=True)
        self.addCleanup(self._kill_daemons)
        for host in ("hub-1", "node-1", "node-2"):
            (self.world / "homes" / host).mkdir(parents=True)
        self.hub_home = self.world / "homes" / "hub-1"
        self.node_home = self.world / "homes" / "node-1"
        (self.hub_home / ".guardian" / "data").mkdir(parents=True)
        self.inventory = self.hub_home / ".guardian" / "data" / "servers.json"
        self.inventory.write_text(json.dumps(INVENTORY, indent=2))
        (self.world / "node-1.facts").write_text(CAPABLE_FACTS)
        (self.world / "node-1.check").write_text(CHECK_OK)
        (self.world / "node-2.facts").write_text(CAPABLE_FACTS)
        (self.world / "node-2.check").write_text(CHECK_OK)
        # The node's CLI: only its presence matters (the worker lists installed providers).
        (self.node_home / ".local" / "bin").mkdir(parents=True)
        _exe(self.node_home / ".local" / "bin" / "claude", "#!/bin/sh\nexit 0\n")

        self.bin = self.world / "bin"
        self.bin.mkdir()
        _exe(self.bin / "ssh", FAKE_SSH)
        _exe(self.bin / "systemctl", FAKE_SYSTEMCTL)
        _exe(self.bin / "loginctl", FAKE_LOGINCTL)
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n")  # the acceptance loop waits 2 s between tries
        _exe(self.bin / "sudo", "#!/bin/sh\nexit 1\n")

        self.repo = self.world / "repo"
        for rel in COPIED:
            self._copy(rel)
        self._copy_worker_code()
        self.git("init", "-q")
        self.commit("first")

    # -- helpers ---------------------------------------------------------------------------------------

    def _copy(self, rel: str) -> None:
        dest = self.repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)

    def _copy_worker_code(self) -> None:
        for line in (ROOT / "deploy/worker/files.txt").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                self._copy(line.strip())

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
            cwd=self.repo, capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def env(self, **extra: str) -> dict:
        env = dict(os.environ)
        env.pop("GUARDIAN_LLM_PROVIDERS", None)
        env.update(
            PATH=f"{self.bin}{os.pathsep}{env['PATH']}",
            FAKE_WORLD=str(self.world),
            HUB_HOST="hub-1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.update(extra)
        return env

    def run_script(self, name: str, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / name), *args],
            cwd=self.repo, env=self.env(**env), capture_output=True, text=True, timeout=120)

    def probe(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return self.run_script("probe-worker.sh", *args, **env)

    def install(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return self.run_script("install-worker.sh", *args, **env)

    def entry(self, host: str = "node-1") -> dict:
        return next(s for s in json.loads(self.inventory.read_text()) if s["id"] == host)

    def _kill_daemons(self) -> None:
        for pidfile in (self.world / "homes").glob("*/.fake/pid"):
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def probed_node(self) -> None:
        done = self.probe("node-1", "--record")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def worker_dir(self) -> Path:
        return self.node_home / ".guardian" / "worker"

    def ping(self) -> dict:
        done = subprocess.run(
            ["bash", "-c", "ssh -- hub-1 \"ssh -o BatchMode=yes -- node-1 'python3 ~/.guardian/worker/worker.py client'\""],
            input=json.dumps({"op": "ping", "protocol": PROTOCOL}) + "\n", env=self.env(), capture_output=True, text=True, timeout=30)
        return json.loads(done.stdout.strip().splitlines()[-1])

    # -- probe -----------------------------------------------------------------------------------------

    def test_probe_capable_host_without_record_changes_nothing(self) -> None:
        before = self.inventory.read_text()
        done = self.probe("node-1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("node-1", done.stdout)
        self.assertEqual(self.inventory.read_text(), before)

    def test_probe_record_stores_the_verdict(self) -> None:
        self.probed_node()
        capable = self.entry()["worker_capable"]
        self.assertIs(capable["passed"], True)
        self.assertEqual(capable["clis"], ["claude"])
        self.assertNotIn("worker", self.entry())

    def test_probe_small_host_is_not_capable_and_record_turns_the_flag_off(self) -> None:
        (self.world / "node-1.facts").write_text(SMALL_FACTS)
        done = self.probe("node-1", "--record")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIs(self.entry()["worker_capable"]["passed"], False)
        self.assertIs(self.entry()["worker"], False)

    def test_probe_failed_cli_check_is_not_capable(self) -> None:
        (self.world / "node-1.check").write_text(CHECK_FAILED)
        self.assertEqual(self.probe("node-1").returncode, 3)

    def test_probe_hub_that_cannot_reach_the_host_is_not_capable(self) -> None:
        done = self.probe("node-1", FAKE_UNREACHABLE_FROM_HUB="node-1")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)

    def test_probe_refuses_the_hub_and_unknown_hosts(self) -> None:
        self.assertEqual(self.probe("hub-1").returncode, 2)
        self.assertEqual(self.probe("node-9").returncode, 2)
        self.assertEqual(self.probe("bad host").returncode, 2)

    def test_probe_max_jobs_scales_the_memory_demand(self) -> None:
        self.assertEqual(self.probe("node-1", "--max-jobs", "2").returncode, 0)
        self.assertEqual(self.probe("node-1", "--max-jobs", "8").returncode, 3)

    # -- install ---------------------------------------------------------------------------------------

    def test_install_needs_a_passing_probe(self) -> None:
        done = self.install("node-1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("probe-worker.sh", done.stderr)
        (self.world / "node-1.facts").write_text(SMALL_FACTS)
        self.probe("node-1", "--record")
        self.assertEqual(self.install("node-1").returncode, 1)
        self.assertFalse(self.worker_dir().exists())

    def test_install_refuses_the_hub(self) -> None:
        self.assertEqual(self.install("hub-1").returncode, 2)

    def test_install_dry_run_touches_nothing(self) -> None:
        self.probed_node()
        before = self.inventory.read_text()
        done = self.install("node-1", "--dry-run")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("dry-run", done.stdout)
        self.assertFalse(self.worker_dir().exists())
        self.assertEqual(self.inventory.read_text(), before)

    def test_install_unknown_revision_fails_before_any_upload(self) -> None:
        self.probed_node()
        done = self.install("node-1", "--rev", "no-such-rev")
        self.assertEqual(done.returncode, 1)
        self.assertFalse(self.worker_dir().exists())

    def test_install_happy_path(self) -> None:
        self.probed_node()
        done = self.install("node-1", "--max-jobs", "3")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for name in ("worker.py", "remote_agents.py", "llm_runner.py", "env_policy.py", "REVISION"):
            self.assertTrue((self.worker_dir() / name).is_file(), name)
        self.assertEqual(oct(self.worker_dir().stat().st_mode & 0o777), "0o700")
        unit = (self.node_home / ".config/systemd/user/guardian-worker.service").read_text()
        self.assertIn("GUARDIAN_WORKER_MAX_JOBS=3", unit)
        self.assertIn(str(self.node_home / ".local/bin"), unit)  # the CLI's directory is on the unit's PATH
        self.assertNotIn("@", unit)
        self.assertTrue((self.node_home / ".fake/linger").exists())
        flag = self.entry()
        self.assertIs(flag["worker"], True)
        self.assertEqual(flag["worker_install"]["max_jobs"], 3)
        self.assertEqual(flag["worker_install"]["rev"], self.git("rev-parse", "HEAD")[:12])
        reply = self.ping()  # the hub's own path, once more, after the script finished
        self.assertIs(reply["ok"], True)
        self.assertEqual(reply["max_jobs"], 3)
        self.assertIn("claude", reply["providers"])
        self.assertEqual(flag["worker_install"]["version"], reply["version"])

    def test_install_uses_the_probe_max_jobs_by_default(self) -> None:
        self.probe("node-1", "--record", "--max-jobs", "1")
        self.assertEqual(self.install("node-1").returncode, 0)
        self.assertEqual(self.ping()["max_jobs"], 1)

    def test_install_installs_the_revision_not_the_working_tree(self) -> None:
        self.probed_node()
        committed = (self.repo / "cli/llm-agent/worker.py").read_text()
        (self.repo / "cli/llm-agent/worker.py").write_text(committed + "\nRAISE_IF_INSTALLED = (\n")  # broken, uncommitted
        done = self.install("node-1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.worker_dir() / "worker.py").read_text(), committed)

    def test_update_keeps_the_previous_version_and_a_failed_update_restores_it(self) -> None:
        self.probed_node()
        self.assertEqual(self.install("node-1").returncode, 0)
        first = (self.worker_dir() / "worker.py").read_text()
        (self.repo / "cli/llm-agent/worker.py").write_text(first + "\n# second revision\n")
        second_rev = self.commit("second")
        done = self.install("node-1", "--rev", second_rev)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.worker_dir() / "previous" / "worker.py").read_text(), first)
        second = (self.worker_dir() / "worker.py").read_text()
        self.assertTrue(second.endswith("# second revision\n"))
        # Third revision, but the unit does not restart the daemon: the old process keeps running its old code, the
        # acceptance check sees a version that is not the staged one and the second revision is put back.
        (self.repo / "cli/llm-agent/worker.py").write_text(second + "# third revision\n")
        third_rev = self.commit("third")
        before = self.entry()["worker_install"]
        done = self.install("node-1", "--rev", third_rev, FAKE_NO_START="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("old process", done.stderr)
        self.assertEqual((self.worker_dir() / "worker.py").read_text(), second)
        self.assertEqual(self.entry()["worker_install"], before)  # the flag keeps describing what really runs
        self.assertIs(self.ping()["ok"], True)

    def test_a_failed_upgrade_from_a_smaller_file_set_is_rolled_back(self) -> None:
        # Protocol 1 -> 2 grew the set from four files to sixteen: the backup of an old host holds only the old four.
        self.probed_node()
        self.assertEqual(self.install("node-1").returncode, 0)
        old_set = {"worker.py", "remote_agents.py", "llm_runner.py", "env_policy.py", "REVISION", "previous"}
        for path in self.worker_dir().iterdir():
            if path.name not in old_set and path.is_file():
                path.unlink()
        shutil.rmtree(self.worker_dir() / "previous", ignore_errors=True)
        old = (self.worker_dir() / "worker.py").read_text()
        (self.repo / "cli/llm-agent/worker.py").write_text(old + "\n# next revision\n")
        rev = self.commit("next")
        done = self.install("node-1", "--rev", rev, FAKE_NO_START="1")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("is missing", done.stderr)
        self.assertEqual((self.worker_dir() / "worker.py").read_text(), old)

    def test_failed_first_install_leaves_the_host_unflagged(self) -> None:
        self.probed_node()
        done = self.install("node-1", FAKE_NO_START="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("worker daemon not running", done.stderr)
        self.assertNotIn("worker", self.entry())
        self.assertNotIn("worker_install", self.entry())

    def test_install_without_linger_installs_but_does_not_flag(self) -> None:
        self.probed_node()
        done = self.install("node-1", FAKE_LINGER_DENY="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("linger", done.stderr + done.stdout)
        self.assertNotIn("worker", self.entry())

    def test_install_refuses_files_that_do_not_parse(self) -> None:
        self.probed_node()
        (self.repo / "cli/llm-agent/llm_runner.py").write_text("def broken(:\n")
        rev = self.commit("broken")
        done = self.install("node-1", "--rev", rev)
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("worker", self.entry())

    def test_disable_and_uninstall(self) -> None:
        self.probed_node()
        self.assertEqual(self.install("node-1").returncode, 0)
        done = self.install("node-1", "--disable")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIs(self.entry()["worker"], False)
        self.assertNotIn("worker_install", self.entry())
        self.assertTrue((self.worker_dir() / "worker.py").exists())  # --disable keeps the files
        done = self.install("node-1", "--uninstall")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.worker_dir().exists())
        self.assertFalse((self.node_home / ".config/systemd/user/guardian-worker.service").exists())
        self.assertFalse((self.node_home / ".fake/pid").exists())  # the daemon was stopped

    def test_uninstall_leaves_a_directory_that_is_not_ours(self) -> None:
        self.probed_node()
        foreign = self.worker_dir()
        foreign.mkdir(parents=True)
        (foreign / "notes.txt").write_text("not a worker")
        done = self.install("node-1", "--uninstall")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((foreign / "notes.txt").exists())

    def test_inventory_edits_keep_other_hosts_untouched(self) -> None:
        before = self.entry("node-2")
        self.probed_node()
        self.assertEqual(self.install("node-1").returncode, 0)
        self.assertEqual(self.entry("node-2"), before)
        self.assertTrue((self.hub_home / ".guardian/data/servers.json.bak").exists())


if __name__ == "__main__":
    unittest.main()

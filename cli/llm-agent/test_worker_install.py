"""The worker file set of deploy/worker/files.txt is closed (docs/design/remote-agents.md, W4a).

scripts/install-worker.sh copies exactly the files of that list into ~/.guardian/worker/ on a host. A new import in the
worker that is not on the list would pass every other test (the repository has all modules) and then fail on the host.
These tests start the real daemon and client from a directory that holds only the listed files, and scan the files for
imports of repository modules that are not in the set.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Set

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FILES_TXT = ROOT / "deploy" / "worker" / "files.txt"

# Imports of repository modules that the worker never reaches on a host. Each needs a reason.
#   group_manager  remote_agents.worker_hosts() reads the hub's inventory; only the hub calls it
NOT_ON_THE_HOST = {"group_manager"}


def listed_files() -> List[Path]:
    names = []
    for line in FILES_TXT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(ROOT / line)
    return names


def repo_modules() -> Set[str]:
    return {p.stem for p in HERE.glob("*.py")}


def imported_repo_modules(path: Path) -> Set[str]:
    found = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & repo_modules()


class WorkerFileSetTests(unittest.TestCase):
    def test_the_list_names_existing_files_of_the_cli_directory(self) -> None:
        files = listed_files()
        self.assertIn("worker.py", [p.name for p in files])
        for path in files:
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path.parent, HERE, f"{path} is not flat in cli/llm-agent (the host holds a flat directory)")

    def test_the_host_installer_moves_exactly_the_set(self) -> None:
        text = (ROOT / "scripts" / "lib" / "worker-remote-install.sh").read_text()
        body = text.split("CODE=(", 1)[1].split(")", 1)[0]
        self.assertEqual(sorted(body.split()), sorted(p.name for p in listed_files()))

    def test_the_code_version_covers_exactly_the_set(self) -> None:
        from remote_agents import VERSION_FILES
        self.assertEqual(sorted(VERSION_FILES), sorted(p.name for p in listed_files()))

    def test_every_repository_import_is_in_the_set(self) -> None:
        in_set = {p.stem for p in listed_files()}
        for path in listed_files():
            missing = imported_repo_modules(path) - in_set - NOT_ON_THE_HOST
            self.assertFalse(missing, f"{path.name} imports {sorted(missing)}: add to deploy/worker/files.txt")

    def test_the_exceptions_are_still_needed(self) -> None:
        # An allow-list entry nobody imports any more hides nothing but also documents nothing.
        used = set()
        for path in listed_files():
            used |= imported_repo_modules(path)
        self.assertEqual(NOT_ON_THE_HOST - used, set(), "drop modules nobody imports from NOT_ON_THE_HOST")

    def test_the_daemon_and_client_run_from_the_set_alone(self) -> None:
        # /tmp: the socket path must stay below the unix socket limit (104 bytes on macOS).
        base = Path(tempfile.mkdtemp(prefix="sgc", dir="/tmp"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        code, elsewhere = base / "code", base / "cwd"
        code.mkdir()
        elsewhere.mkdir()
        for path in listed_files():
            shutil.copy2(path, code / path.name)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GUARDIAN_LLM_PROVIDERS")}
        env.update(GUARDIAN_WORKER_DIR=str(base), GUARDIAN_WORKER_SOCKET=str(base / "w.sock"),
                   PYTHONDONTWRITEBYTECODE="1")
        daemon = subprocess.Popen([sys.executable, str(code / "worker.py"), "serve"], cwd=elsewhere, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(daemon.stdout.close)  # type: ignore[union-attr]
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.terminate)
        reply = None
        for _ in range(100):
            if daemon.poll() is not None:
                self.fail("the daemon exited: " + (daemon.stdout.read() if daemon.stdout else ""))
            if (base / "w.sock").exists():
                done = subprocess.run([sys.executable, str(code / "worker.py"), "client"], cwd=elsewhere, env=env,
                                      input='{"op":"ping","protocol":2}', capture_output=True, text=True, timeout=30)
                reply = json.loads(done.stdout.strip().splitlines()[-1])
                if reply.get("ok"):
                    break
            subprocess.run(["sleep", "0.1"])
        self.assertIsNotNone(reply)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(len(reply["version"]), 12)
        self.assertIsInstance(reply["providers"], list)
        self.assertIn("git", reply)


if __name__ == "__main__":
    unittest.main()

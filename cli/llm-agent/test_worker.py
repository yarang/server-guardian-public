"""The worker daemon's relay surface (docs/design/resident-agents.md, M2c): the relay op and mode.

The rest of the daemon (ping, run, cancel, snapshot_has) is covered by test_remote_agents.py and
test_remote_repo.py; those paths are unchanged by the resident duties. No network, no ssh.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import worker


class StubManager:
    """Stands in for the ResidentManager: records the handoff, writes one line as hello would."""

    def __init__(self) -> None:
        self.attach_count = 0
        self.rfile: Optional[Any] = None
        self.wfile: Optional[Any] = None

    def attach(self, rfile: Any, wfile: Any) -> None:
        self.attach_count += 1
        self.rfile, self.wfile = rfile, wfile
        wfile.write(b'{"t": "hello", "protocol": 3}\n')
        wfile.flush()

    def shutdown(self) -> None:
        pass


class RelayCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="gwr", dir="/tmp")  # short: unix socket path limits
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.dir]))
        self.sock = Path(self.dir) / "w.sock"
        env = patch.dict(os.environ, {"GUARDIAN_WORKER_SOCKET": str(self.sock), "GUARDIAN_WORKER_DIR": self.dir})
        env.start()
        self.addCleanup(env.stop)
        self.daemon = worker.Worker(max_jobs=2, queue_seconds=5.0)
        server = worker.make_server(self.sock, self.daemon)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop() -> None:
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)

    def connect(self, line: bytes) -> socket.socket:
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(10.0)
        conn.connect(str(self.sock))
        conn.sendall(line)
        return conn


class RelayOpTests(RelayCase):
    def test_the_relay_op_hands_the_connection_to_the_resident_manager(self) -> None:
        stub = StubManager()
        self.daemon.resident = stub
        conn = self.connect(b'{"op": "relay", "protocol": 3}\n')
        reader = conn.makefile("rb")
        # No ordinary reply line is written: the first line on the stream is the manager's own hello.
        self.assertEqual(json.loads(reader.readline()), {"t": "hello", "protocol": 3})
        self.assertEqual(stub.attach_count, 1)
        self.assertIsNotNone(stub.rfile)
        conn.close()

    def test_a_relay_op_with_another_protocol_gets_a_one_line_refusal(self) -> None:
        conn = self.connect(b'{"op": "relay", "protocol": 2}\n')
        reply = json.loads(conn.makefile("rb").readline())
        self.assertFalse(reply["ok"])
        self.assertIn("protocol mismatch", reply["error"])
        self.assertEqual(reply["protocol"], 3)
        self.assertIsNone(self.daemon.resident)

    def test_the_manager_is_created_lazily_and_no_agent_directory_appears(self) -> None:
        self.assertIsNone(self.daemon.resident)
        conn = self.connect(b'{"op": "relay", "protocol": 3}\n')
        reader = conn.makefile("rb")
        hello = json.loads(reader.readline())
        self.assertEqual((hello["t"], hello["protocol"], hello["version"]),
                         ("hello", 3, worker.STARTED_VERSION))
        conn.sendall(b'{"t": "reply", "re": "d1", "ok": false}\n')  # the hub refuses the handshake
        self.assertEqual(reader.readline(), b"")  # the daemon ends the refused stream: EOF, no reply line
        self.assertIsNotNone(self.daemon.resident)
        self.assertFalse((Path(self.dir) / "agents").exists())  # nothing without an agents frame


class RelayModeTests(unittest.TestCase):
    def test_relay_without_a_daemon_prints_the_error_line(self) -> None:
        temp = tempfile.mkdtemp(prefix="gwr", dir="/tmp")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", temp]))
        env = {**os.environ, "GUARDIAN_WORKER_SOCKET": str(Path(temp) / "missing.sock")}
        done = subprocess.run([sys.executable, str(Path(worker.__file__)), "relay"],
                              capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]),
                         {"t": "error", "error": "worker daemon not running"})


if __name__ == "__main__":
    unittest.main()

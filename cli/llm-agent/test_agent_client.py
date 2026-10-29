import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from agent_client import NodeAgentClient, agent_token


class AgentTokenTests(unittest.TestCase):
    def test_env_var_wins(self) -> None:
        with patch.dict(os.environ, {"AGENT_TOKEN": "from-env"}):
            self.assertEqual(agent_token(), "from-env")

    def test_reads_agent_env_file(self) -> None:
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "agent.env"
            env_file.write_text("AGENT_HOST=127.0.0.1\nAGENT_TOKEN='file-token'\n", encoding="utf-8")
            with patch.dict(os.environ, {"GUARDIAN_AGENT_ENV": str(env_file)}):
                os.environ.pop("AGENT_TOKEN", None)
                self.assertEqual(agent_token(), "file-token")

    def test_missing_everywhere(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_AGENT_ENV": "/nonexistent/agent.env"}):
            os.environ.pop("AGENT_TOKEN", None)
            self.assertIsNone(agent_token())


class AuthorizationHeaderTests(unittest.TestCase):
    @patch("agent_client.urllib.request.urlopen")
    def test_bearer_header_sent(self, urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = b'{"status": "healthy"}'
        urlopen.return_value.__enter__.return_value = resp
        NodeAgentClient("http://127.0.0.1:9870", token="tok").get_status()

        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")

    @patch("agent_client.urllib.request.urlopen")
    def test_no_header_without_token(self, urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = b'{}'
        urlopen.return_value.__enter__.return_value = resp
        NodeAgentClient("http://127.0.0.1:9870", token="").get_status()

        self.assertIsNone(urlopen.call_args.args[0].get_header("Authorization"))


class FleetStreamTests(unittest.TestCase):
    @patch("agent_client.urllib.request.urlopen")
    def test_command_goes_in_post_body_not_url(self, urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.__iter__.return_value = iter([b"event: start\n", b'data: {"type": "start"}\n', b"\n",
                                           b'data: {"type": "complete"}\n'])
        resp.__enter__.return_value = resp
        urlopen.return_value = resp
        client = NodeAgentClient("http://127.0.0.1:9870", token="tok")
        events = list(client.stream_fleet_exec("uptime; id", targets=["a", "b"]))

        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "http://127.0.0.1:9870/api/fleet/exec/stream")
        self.assertEqual(json.loads(req.data), {"command": "uptime; id", "targets": ["a", "b"]})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual([e["type"] for e in events], ["start", "complete"])


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from persona_manager import PersonaManager
from mcp_client import MCPClient
from debate_engine import DebateEngine

# The debate runs offline (built-in fallback, under a second) and always writes to a throwaway
# data dir. GUARDIAN_LIVE_LLM_TESTS=1 calls the real provider CLIs instead, which takes minutes.
_data_dir = TemporaryDirectory()
# No goindex helper: this debate runs on the real repository, which has a go.mod.
_env = {"GUARDIAN_JEV_GATE": "off", "GUARDIAN_DATA_DIR": _data_dir.name,
        "GUARDIAN_GOINDEX_BIN": os.path.join(_data_dir.name, "no-goindex-helper")}
if os.environ.get("GUARDIAN_LIVE_LLM_TESTS") != "1":
    _env["GUARDIAN_LLM_PROVIDERS"] = "none"
_env_patch = patch.dict(os.environ, _env)


def setUpModule() -> None:
    # These tests cover the LLM debate flow; the jev gate has its own tests.
    _env_patch.start()


def tearDownModule() -> None:
    _env_patch.stop()
    _data_dir.cleanup()

class TestDebateSystem(unittest.TestCase):
    def test_persona_manager(self):
        pm = PersonaManager()
        personas = pm.list_personas()
        self.assertGreaterEqual(len(personas), 3)

        arch = pm.get_persona("domain_architect")
        self.assertIsNotNone(arch)
        self.assertEqual(arch.get("role"), "Domain Expert Agent")

    def test_mcp_client(self):
        mcp = MCPClient()
        tools = mcp.list_tools()
        self.assertGreaterEqual(len(tools), 3)

        res = mcp.call_tool("jev_mcp:execute_code_action", {
            "file_path": "/tmp/test_mcp.txt",
            "content": "Hello JEV-MCP"
        })
        # Writes outside the workspace are refused.
        self.assertEqual(res.get("status"), "FAILED")

    def test_debate_engine(self):
        engine = DebateEngine(max_rounds=1)
        res = engine.run_debate("Go Agent에 HTTP Rate Limiter 미들웨어 설계")
        self.assertIn("transcript", res)
        self.assertGreater(len(res["transcript"]), 0)


if __name__ == "__main__":
    unittest.main()

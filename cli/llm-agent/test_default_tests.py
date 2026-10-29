import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_client import TEST_TIMEOUT, ActionValidationError, MCPClient
from project_registry import ProjectValidationError, parse_project

VALID = {"id": "svc", "repo": "https://git.example.com/me/svc.git"}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIPPED = REPO_ROOT / "configs" / "projects" / "server-guardian.json"


class ParseDefaultListTests(unittest.TestCase):
    def test_string_default_is_unchanged(self) -> None:
        p = parse_project({**VALID, "tests": {"default": "go test ./..."}})
        self.assertEqual((p.default_test, p.default_tests), ("go test ./...", ["go test ./..."]))

    def test_list_default_keeps_order_and_first_entry(self) -> None:
        p = parse_project({**VALID, "tests": {"default": ["go vet ./...", "go test -race ./..."]}})
        self.assertEqual(p.default_tests, ["go vet ./...", "go test -race ./..."])
        self.assertEqual(p.default_test, "go vet ./...")

    def test_bad_lists_are_refused(self) -> None:
        bad = [
            ["go vet ./...", "bash -c id"],          # not in the allow-list
            ["go test ./...", "scripts/ci.sh"],      # path-prefixed script
            ["go vet -vettool=/bin/sh ./..."],       # refused flag
            ["go test ./...", 3],                    # not a string
            ["go test ./...", "  "],                 # blank
            ["go test ./..."] * 6,                   # more than 5
            {"cmd": "go test ./..."},                # wrong type
        ]
        for default in bad:
            with self.assertRaises(ProjectValidationError, msg=json.dumps(default)):
                parse_project({**VALID, "tests": {"default": default}})

    def test_error_names_the_failing_entry(self) -> None:
        with self.assertRaisesRegex(ProjectValidationError, r"tests\.default\[2\]"):
            parse_project({**VALID, "tests": {"default": ["go test ./...", "cargo test"]}})


class GoVetPrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mcp = MCPClient(workspace=REPO_ROOT)

    def test_go_vet_takes_package_args_and_v(self) -> None:
        self.assertEqual(self.mcp._test_argv("go vet ./apps/agent/..."), ["go", "vet", "./apps/agent/..."])
        self.assertEqual(self.mcp._test_argv("go vet -v ./..."), ["go", "vet", "-v", "./..."])

    def test_go_vet_refuses_program_running_flags(self) -> None:
        for cmd in ("go vet -vettool=/bin/sh ./...", "go vet -exec=sh ./...", "go vet -toolexec=sh ./...",
                    "go vet -vettool /bin/sh ./...", "go vet ../x"):
            with self.assertRaises(ActionValidationError, msg=cmd):
                self.mcp._test_argv(cmd)

    def test_go_test_accepts_race_and_still_refuses_exec(self) -> None:
        self.assertIn("-race", self.mcp._test_argv("go test -race ./..."))
        with self.assertRaises(ActionValidationError):
            self.mcp._test_argv("go test -exec sh ./...")


class RunDefaultTestSequenceTests(unittest.TestCase):
    def mcp(self, steps):
        return MCPClient(workspace=REPO_ROOT, project=SimpleNamespace(
            id="demo", default_test=steps[0], default_tests=steps, test_commands=[]))

    @staticmethod
    def fake(codes):
        results = iter(codes)

        def run(argv, **kwargs):
            code = next(results)
            return SimpleNamespace(returncode=code, stdout=f"out {argv[-1]}", stderr="boom" if code else "")
        return run

    def test_success_lists_every_step(self) -> None:
        mcp = self.mcp(["pytest -q", "go vet ./apps/agent/...", "go test ./apps/agent/..."])
        with patch("mcp_client.subprocess.run", side_effect=self.fake([0, 0, 0])) as run:
            res = mcp.run_default_test()
        self.assertEqual(run.call_count, 3)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual([s["step"] for s in res["steps"]], [1, 2, 3])
        self.assertEqual(res["cmd"], "pytest -q && go vet ./... && go test ./...")
        for call in run.call_args_list[1:]:
            self.assertEqual(call.kwargs["cwd"], REPO_ROOT / "apps" / "agent")

    def test_stops_at_the_first_failing_step_and_names_it(self) -> None:
        mcp = self.mcp(["pytest -q", "go vet ./apps/agent/...", "go test ./apps/agent/..."])
        with patch("mcp_client.subprocess.run", side_effect=self.fake([0, 1])) as run:
            res = mcp.run_default_test()
        self.assertEqual(run.call_count, 2)  # step 3 never ran
        self.assertEqual(res["exit_code"], 1)
        self.assertEqual(res["cmd"], "go vet ./...")
        self.assertTrue(res["stderr"].startswith("step 2/3 failed: go vet ./..."), res["stderr"])
        self.assertIn("boom", res["stderr"])
        self.assertEqual([s["exit_code"] for s in res["steps"]], [0, 1])

    def test_steps_share_one_budget(self) -> None:
        mcp = self.mcp(["pytest -q", "pytest -x"])
        with patch("mcp_client.subprocess.run", side_effect=self.fake([0, 0])) as run:
            mcp.run_default_test(TEST_TIMEOUT)
        timeouts = [c.kwargs["timeout"] for c in run.call_args_list]
        self.assertLessEqual(timeouts[0], TEST_TIMEOUT)
        self.assertLessEqual(timeouts[1], timeouts[0])

    def test_exhausted_budget_fails_the_step_without_running_it(self) -> None:
        mcp = self.mcp(["pytest -q"])
        with patch("mcp_client.subprocess.run") as run:
            res = mcp.run_default_test(timeout=0)
        run.assert_not_called()
        self.assertEqual(res["exit_code"], -1)
        self.assertIn("step 1/1 failed", res["stderr"])

    def test_catalog_lists_all_steps(self) -> None:
        mcp = self.mcp(["pytest -q", "go vet ./..."])
        text = mcp.describe(mcp.tool("jev_mcp:run_tests"))
        self.assertIn("1. pytest -q; 2. go vet ./...", text)


class ShippedServerGuardianProjectTests(unittest.TestCase):
    def test_default_gate_resolves_as_intended(self) -> None:
        project = parse_project(json.loads(SHIPPED.read_text()))
        self.assertEqual(len(project.default_tests), 3)
        mcp = MCPClient(workspace=REPO_ROOT, project=project)
        plan = mcp.default_test_argvs()
        self.assertEqual(plan[0], (["python3", "-m", "unittest", "discover", "-s", "cli/llm-agent", "-p", "test_*.py", "-q"],
                                   REPO_ROOT))
        self.assertEqual(plan[1], (["go", "vet", "./..."], REPO_ROOT / "apps" / "agent"))
        self.assertEqual(plan[2], (["go", "test", "-race", "./..."], REPO_ROOT / "apps" / "agent"))


if __name__ == "__main__":
    unittest.main()

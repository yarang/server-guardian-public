"""The provisioning team, its tool gate and its prompts (docs/design/fleet-provisioning.md, chapter 9, P6)."""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import debate_engine
import ops_checks
import ops_targets
import plan_ops
from debate_engine import DebateEngine
from debate_events import EventWriter
from mcp_client import MCPClient
from ops_checks import OpsChecks
from project_registry import ProjectManager, parse_project
from team_manager import TeamManager, judge_tools
from test_ops_project import GROUPS, OPS, SERVERS, _FixedTargets
from test_retrieval_loop import FakeRunner
from testenv import isolate_data_dir, restore_data_dir

PACKAGE_TOOLS = ["ops:package_install", "ops:package_remove"]


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def builtin_fleet():
    return ProjectManager(directory=Path("/nonexistent")).load("fleet")


class ToolGateTests(unittest.TestCase):
    """The judge's tools are the team list narrowed by the persona and by the project's policy: all three must agree."""

    def judge_tools(self, team_id: str, project=None):
        teams = TeamManager(Path("/nonexistent"))
        lineup = teams.load(team_id)
        judge = teams.persona_mgr.get_persona(lineup.judge.persona) or {}
        return MCPClient(project=project or builtin_fleet()).project_tools(judge_tools(lineup, judge))

    def test_the_provisioning_judge_gets_the_package_tools_the_restart_and_the_batch_tools(self) -> None:
        self.assertEqual(sorted(self.judge_tools("fleet-provision")),
                         sorted([*PACKAGE_TOOLS, "ops:restart_service", "ops:run_command_set", "ops:run_script"]))

    def test_the_cleanup_judge_gets_no_batch_tool(self) -> None:
        self.assertFalse(set(self.judge_tools("fleet-ops")) & {"ops:run_command_set", "ops:run_script"})

    def test_the_cleanup_judge_still_gets_no_package_tool_although_the_project_allows_them(self) -> None:
        project = builtin_fleet()
        self.assertTrue(set(PACKAGE_TOOLS) <= set(project.policy["allowed_actions"]))
        tools = self.judge_tools("fleet-ops")
        self.assertTrue(tools)
        self.assertFalse(set(tools) & set(PACKAGE_TOOLS))

    def test_a_project_without_the_package_tools_leaves_the_provisioning_judge_none(self) -> None:
        project = parse_project(OPS)  # a new ops project is opt-in: its default policy has no package tool
        self.assertEqual(self.judge_tools("fleet-provision", project), ["ops:restart_service"])

    def test_the_team_files_load_and_name_known_personas(self) -> None:
        lineup = TeamManager(Path("/nonexistent")).load("fleet-provision")
        self.assertEqual(lineup.expert.persona, "ops_provisioner")
        self.assertEqual([m.persona for m in lineup.critics], ["ops_provision_safety_critic", "ops_dependency_critic"])
        self.assertEqual(lineup.judge.persona, "ops_provision_judge")
        self.assertEqual(TeamManager(Path("/nonexistent")).get_team("fleet-provision")["category"], "operations")


class CalibrationTests(unittest.TestCase):
    """The provisioning team grades its objections and does not invent prohibitions; fleet-ops keeps its own critic."""

    def prompt(self, persona: str) -> str:
        return (TeamManager(Path("/nonexistent")).persona_mgr.get_persona(persona) or {}).get("system_prompt", "")

    def test_critics_grade_each_objection(self) -> None:
        for persona in ("ops_provision_safety_critic", "ops_dependency_critic"):
            with self.subTest(persona=persona):
                self.assertIn("[차단]", self.prompt(persona))
                self.assertIn("[권고]", self.prompt(persona))

    def test_the_judge_does_not_turn_advice_into_prohibitions_and_names_the_tool_limit(self) -> None:
        text = self.prompt("ops_provision_judge")
        self.assertIn("금지 조건", text)
        self.assertIn("이 도구로는 못 한다", text)

    def test_the_cleanup_team_keeps_the_critic_it_always_had(self) -> None:
        lineup = TeamManager(Path("/nonexistent")).load("fleet-ops")
        self.assertIn("ops_safety_critic", [m.persona for m in lineup.critics])
        self.assertNotIn("[차단]", self.prompt("ops_safety_critic"))


class BundlesCheckTests(unittest.TestCase):
    def checks(self, provisioning: bool) -> OpsChecks:
        project = parse_project(OPS)
        self.agent = MagicMock()
        return OpsChecks(project, agent=self.agent, resolved=ops_targets.resolve(project, SERVERS, GROUPS),
                         provisioning=provisioning)

    def test_the_check_is_unknown_to_a_lineup_without_package_tools(self) -> None:
        checks = self.checks(False)
        out = checks.run({"op": "bundles"})
        self.assertFalse(out["ok"])
        self.assertIn("unknown check 'bundles'", out["error"])
        self.assertNotIn("bundles", (out["error"] or "").split("known:")[1])

    def test_the_list_needs_no_host_and_can_be_cited(self) -> None:
        checks = self.checks(True)
        out = checks.run({"op": "bundles"})
        self.assertTrue(out["ok"], out)
        data = json.loads(out["text"])
        self.assertEqual([b["name"] for b in data["bundles"]], ["nginx", "postgresql"])
        self.assertIn("apt", data["bundles"][0]["managers"])
        # the call is served without a host, so a citation of it verifies whatever host token it carries
        self.assertEqual(checks.unverified_citations("[#1 bundles n1]"), (1, []))
        self.assertEqual(checks.unverified_citations("[#2 bundles n1]"), (1, ["[#2 bundles n1]"]))
        # hub-local: nothing was sent to a host
        self.assertFalse(self.agent.execute_remote_ssh.called)
        self.assertFalse(self.agent.execute_command.called)

    def test_a_bundle_is_expanded_for_one_manager(self) -> None:
        out = self.checks(True).run({"op": "bundles", "name": "nginx", "manager": "apt"})
        self.assertTrue(out["ok"], out)
        data = json.loads(out["text"])
        self.assertEqual((data["bundle"], data["manager"]), ("nginx", "apt"))
        self.assertIn("nginx", data["install"])

    def test_bad_requests_are_answered_with_errors(self) -> None:
        checks = self.checks(True)
        for request, text in (({"op": "bundles", "name": "nginx"}, "both name and manager"),
                              ({"op": "bundles", "name": "nope", "manager": "apt"}, "unknown bundle"),
                              ({"op": "bundles", "name": "nginx", "manager": "pacman"}, "no package list"),
                              ({"op": "bundles", "host": "n1"}, "unknown arguments")):
            with self.subTest(request=request):
                out = checks.run(request)
                self.assertFalse(out["ok"])
                self.assertIn(text, out["error"])


class PlanRuleTests(unittest.TestCase):
    """The plan the prompt shows is a plan the engine accepts."""

    @staticmethod
    def example_plan() -> Dict[str, Any]:
        text = debate_engine.JUDGE_PLAN_EXAMPLE
        for placeholder, value in (("<what the order achieves>", "nginx on the web nodes"), ("<other target id>", "n2"),
                                   ("<target id>", "n1"), ("<package>", "nginx"), ("<name>.service", "nginx.service")):
            text = text.replace(placeholder, value)
        body = {"decision": "x", "constraints": [], "actions": [], **json.loads("{" + text + "}")}
        return plan_ops.parse_plan("```json\n" + json.dumps(body) + "\n```\nVERDICT: APPROVED") or {}

    def test_the_example_of_the_prompt_is_parsed_and_normalized(self) -> None:
        plan = self.example_plan()
        normal = plan_ops.normalize_plan(plan)
        self.assertEqual([s["id"] for s in normal["steps"]], ["s1", "s2", "s3"])
        self.assertEqual(normal["steps"][1]["requires"], ["s1"])
        self.assertEqual(normal["steps"][2]["on_failure"], "continue")
        self.assertEqual({s["tool"] for s in normal["steps"]}, {*PACKAGE_TOOLS, "ops:restart_service"})

    def test_the_rule_names_the_tools_the_gate_allows(self) -> None:
        for tool in (*PACKAGE_TOOLS, "ops:restart_service"):
            self.assertIn(tool, debate_engine.JUDGE_PLAN_EXAMPLE)


class PromptGateTests(unittest.TestCase):
    """Only a lineup that can propose package actions sees the bundles check and the plan rules."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "builtin").mkdir()
        self.manager = ProjectManager(directory=self.root / "custom", builtin_dir=self.root / "builtin")
        self.manager.directory.mkdir()
        policy = {"allowed_actions": ["ops:apt_clean", "ops:restart_service", *PACKAGE_TOOLS]}
        (self.manager.directory / "ops1.json").write_text(json.dumps({**OPS, "policy": policy}))
        self.events_path = self.root / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_debate(self, team: str, batch: bool = False) -> FakeRunner:
        # batch: the project opts in to the batch tools (the builtin fleet project does since F1)
        policy = {"allowed_actions": ["ops:apt_clean", "ops:restart_service", *PACKAGE_TOOLS,
                                      *(["ops:run_command_set", "ops:run_script"] if batch else [])]}
        (self.manager.directory / "ops1.json").write_text(json.dumps({**OPS, "policy": policy}))
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        engine = DebateEngine(max_rounds=1, db_path=self.root / f"{team}.db", events=EventWriter(str(self.events_path)))
        engine.project_mgr = self.manager
        with _FixedTargets(), patch.object(engine.llm, "run_session_prompt", side_effect=runner):
            try:
                engine.run_debate("install nginx on the ARM nodes", project_id="ops1", team=team)
            finally:
                engine.events.close()
        return runner

    @staticmethod
    def seen(runner: FakeRunner, role: str) -> str:
        return "\n".join(runner.prompts(role) + runner.system_texts(role)
                         + [str(c["plain_prompt"]) for c in runner.calls if c["role"] == role])

    def test_the_provisioning_team_is_told_about_bundles_and_plans(self) -> None:
        runner = self.run_debate("fleet-provision")
        expert, judge = self.seen(runner, "expert"), self.seen(runner, "judge")
        self.assertIn("- bundles (name, manager)", expert)
        self.assertIn("Package orders:", expert)
        self.assertIn("go into a plan, not into actions", judge)
        self.assertIn("ops:package_install", judge)

    def test_only_a_judge_with_a_batch_tool_reads_the_batch_rules(self) -> None:
        marker = "run it on ONE host first"
        self.assertIn(marker, self.seen(self.run_debate("fleet-provision", batch=True), "judge"))
        self.assertNotIn(marker, self.seen(self.run_debate("fleet-provision"), "judge"))
        self.assertNotIn(marker, self.seen(self.run_debate("fleet-ops", batch=True), "judge"))

    def test_the_cleanup_team_gets_the_prompts_it_always_had(self) -> None:
        runner = self.run_debate("fleet-ops")
        expert, judge = self.seen(runner, "expert"), self.seen(runner, "judge")
        self.assertNotIn("bundles", expert)
        self.assertNotIn("Package orders:", expert)
        self.assertNotIn("go into a plan", judge)
        self.assertNotIn("ops:package_install", judge)
        self.assertEqual(ops_checks.protocol_text(), ops_checks.protocol_text(False))

    def test_the_prompt_version_names_this_change(self) -> None:
        self.assertEqual(debate_engine.RETRIEVAL_PROMPT_VERSION, "2026-10-23.p20")


if __name__ == "__main__":
    unittest.main()

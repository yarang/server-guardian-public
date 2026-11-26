"""The specialist fleet teams: services and containers, mesh (docs/design/server-ops-project.md, P8)."""
import json
import unittest
from pathlib import Path
from typing import Any, Dict, List

import ops_checks
from mcp_client import MCPClient
from project_registry import ProjectManager
from role_cards import persona_text
from team_manager import TeamManager, judge_tools

# the lineups the specialists must not disturb: fleet-ops runs the nightly watch and keeps its members
FLEET_OPS = {"expert": "ops_sre", "critics": ["ops_safety_critic", "ops_capacity_critic"], "judge": "ops_judge"}

TEAMS: Dict[str, Dict[str, Any]] = {
    "fleet-services": {"expert": "ops_service_engineer",
                       "critics": ["ops_service_safety_critic", "ops_service_cause_critic"],
                       "judge": "ops_service_judge", "tools": ["ops:restart_service"]},
    "fleet-mesh": {"expert": "ops_mesh_engineer", "critics": ["ops_mesh_risk_critic"],
                   "judge": "ops_mesh_judge", "tools": []},
}
CRITICS = [c for team in TEAMS.values() for c in team["critics"]]
# service_status, top_processes, ... : every check a specialist prompt names must exist
KNOWN_CHECKS = set(ops_checks.CHECKS)


def teams() -> TeamManager:
    return TeamManager(Path("/nonexistent"))


def persona(persona_id: str) -> Dict[str, Any]:
    return teams().persona_mgr.get_persona(persona_id) or {}


def builtin_fleet():
    return ProjectManager(directory=Path("/nonexistent")).load("fleet")


class TeamFileTests(unittest.TestCase):
    def test_the_team_files_load_with_their_members(self) -> None:
        for team_id, want in TEAMS.items():
            with self.subTest(team=team_id):
                lineup = teams().load(team_id)
                self.assertEqual(lineup.expert.persona, want["expert"])
                self.assertEqual([m.persona for m in lineup.critics], want["critics"])
                self.assertEqual(lineup.judge.persona, want["judge"])
                self.assertEqual(lineup.max_rounds, 2)
                self.assertEqual(teams().get_team(team_id)["category"], "operations")

    def test_every_member_uses_the_roles_of_the_other_ops_personas(self) -> None:
        for team_id, want in TEAMS.items():
            for pid, role in [(want["expert"], "Domain Expert"), *[(c, "Critic Agent") for c in want["critics"]],
                              (want["judge"], "JEV-MCP Decision Agent")]:
                with self.subTest(team=team_id, persona=pid):
                    self.assertEqual(persona(pid).get("role"), role)

    def test_fleet_ops_keeps_its_lineup(self) -> None:
        lineup = teams().load("fleet-ops")
        self.assertEqual(lineup.expert.persona, FLEET_OPS["expert"])
        self.assertEqual([m.persona for m in lineup.critics], FLEET_OPS["critics"])
        self.assertEqual(lineup.judge.persona, FLEET_OPS["judge"])
        self.assertEqual(builtin_fleet().team, "fleet-ops")


class ToolGateTests(unittest.TestCase):
    def judge_tools(self, team_id: str) -> List[str]:
        lineup = teams().load(team_id)
        return MCPClient(project=builtin_fleet()).project_tools(judge_tools(lineup, persona(lineup.judge.persona)))

    def test_each_team_lists_only_tools_the_builtin_fleet_policy_allows(self) -> None:
        allowed = set((builtin_fleet().policy or {})["allowed_actions"])
        for team_id, want in TEAMS.items():
            with self.subTest(team=team_id):
                self.assertTrue(set(teams().load(team_id).allowed_tools or []) <= allowed)
                self.assertEqual(sorted(self.judge_tools(team_id)), sorted(want["tools"]))

    def test_the_judge_persona_lists_what_its_team_lists(self) -> None:
        for team_id, want in TEAMS.items():
            with self.subTest(team=team_id):
                self.assertEqual(persona(want["judge"]).get("mcp_capabilities") or [], want["tools"])
                self.assertEqual(teams().load(team_id).allowed_tools, want["tools"])

    def test_the_mesh_team_proposes_no_action_and_the_services_team_only_restarts(self) -> None:
        self.assertEqual(self.judge_tools("fleet-mesh"), [])
        self.assertEqual(self.judge_tools("fleet-services"), ["ops:restart_service"])


class CalibrationTests(unittest.TestCase):
    """Critics grade their objections and invent no prohibitions, as the provisioning critics do."""

    def prompt(self, persona_id: str) -> str:
        # what reaches the model: an opted-in persona's citation sentence comes from its ops role card (C1)
        return persona_text(persona(persona_id))

    def test_critics_grade_each_objection(self) -> None:
        for pid in CRITICS:
            with self.subTest(persona=pid):
                self.assertIn("[차단]", self.prompt(pid))
                self.assertIn("[권고]", self.prompt(pid))
                self.assertIn("[#번호 조회 호스트]", self.prompt(pid))

    def test_judges_do_not_turn_advice_into_prohibitions(self) -> None:
        for team in TEAMS.values():
            with self.subTest(judge=team["judge"]):
                self.assertIn("금지 조건", self.prompt(team["judge"]))
                self.assertIn("[권고]", self.prompt(team["judge"]))

    def test_the_mesh_judge_states_that_it_proposes_no_action(self) -> None:
        text = self.prompt("ops_mesh_judge")
        self.assertIn("조치를 제안하지 않습니다", text)
        self.assertIn("사람이 직접 할 단계", text)

    def test_the_service_prompts_name_the_protected_units_and_the_tool_limit(self) -> None:
        engineer, judge = self.prompt("ops_service_engineer"), self.prompt("ops_service_judge")
        for unit in ("ssh", "tailscaled", "headscale"):
            self.assertIn(unit, engineer)
        self.assertIn("도구 밖", engineer)
        self.assertIn("이 도구로는 못 한다", judge)

    def test_the_specialist_critics_leave_capacity_to_fleet_ops(self) -> None:
        for pid in ("ops_service_safety_critic", "ops_service_cause_critic"):
            with self.subTest(persona=pid):
                self.assertIn("다른 검토자의 일", self.prompt(pid))

    def test_fleet_ops_is_calibrated_like_the_specialists(self) -> None:
        # the operator's decision of 2026-10-30: the watch team grades objections too (it kept its members)
        for pid in FLEET_OPS["critics"]:
            with self.subTest(persona=pid):
                self.assertIn("[차단]", self.prompt(pid))
                self.assertIn("[권고]", self.prompt(pid))
                self.assertIn("[#번호 조회 호스트]", self.prompt(pid))
                self.assertIn("다른 검토자의 일", self.prompt(pid))
        judge = self.prompt(FLEET_OPS["judge"])
        self.assertIn("금지 조건", judge)
        self.assertIn("[권고]", judge)
        self.assertIn("이 도구로는 못 한다", judge)

    def test_the_capacity_critic_asks_for_no_config_check(self) -> None:
        # no check reads journald or logrotate settings: the prompt names that as out of the tools
        text = self.prompt("ops_capacity_critic")
        self.assertIn("도구 밖", text)
        for name in ("journal_usage", "package_cache", "docker_usage", "largest_dirs", "largest_files"):
            self.assertIn(name, KNOWN_CHECKS)
            self.assertIn(name, text)


class CheckNamesTests(unittest.TestCase):
    """A specialist may only point the agents at checks that exist (and are offered to its lineup)."""

    CHECKS_BY_PERSONA = {
        "ops_service_engineer": ("service_status", "top_processes", "uptime_load", "docker_usage", "host_facts"),
        "ops_mesh_engineer": ("ops_hosts", "headscale_status", "service_status", "uptime_load"),
    }

    def test_the_checks_a_prompt_names_exist(self) -> None:
        for pid, names in self.CHECKS_BY_PERSONA.items():
            for name in names:
                with self.subTest(persona=pid, check=name):
                    self.assertIn(name, KNOWN_CHECKS)
                    self.assertIn(name, persona(pid).get("system_prompt", ""))

    def test_no_specialist_asks_for_the_provisioning_only_check(self) -> None:
        for pid in [t["expert"] for t in TEAMS.values()] + CRITICS:
            with self.subTest(persona=pid):
                self.assertNotIn("bundles", persona(pid).get("system_prompt", ""))


class FilesAreValidJsonTests(unittest.TestCase):
    def test_every_new_file_parses(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent / "configs"
        for team_id, want in TEAMS.items():
            json.loads((root / "teams" / f"{team_id}.json").read_text(encoding="utf-8"))
            for pid in [want["expert"], *want["critics"], want["judge"]]:
                data = json.loads((root / "personas" / f"{pid}.json").read_text(encoding="utf-8"))
                self.assertEqual(data["id"], pid)


if __name__ == "__main__":
    unittest.main()

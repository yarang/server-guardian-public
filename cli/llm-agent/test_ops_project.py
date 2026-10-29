"""Ops projects without a repository (docs/design/server-ops-project.md, P1)."""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import config_ops
import ops_targets
import publish_ops
import wiki_ops
import workspace_ops
from db_manager import DecisionDBManager
from debate_engine import DebateEngine
from debate_events import EventWriter
from team_manager import EXPERT_ROLE
from mcp_client import SERVER_TOOLS, ActionValidationError, MCPClient
from project_registry import (
    NoWorkspace,
    ProjectManager,
    ProjectValidationError,
    Workspaces,
    parse_project,
    workspace_status,
)
from test_retrieval_loop import FakeRunner
from testenv import isolate_data_dir, restore_data_dir

SERVERS = [
    {"id": "hub1", "name": "Hub", "guardian_role": "hub", "hostname": "hub.example"},
    {"id": "n1", "name": "Node 1", "hostname": "n1.example"},
    {"id": "n2", "name": "Node 2", "hostname": "n2.example"},
    {"id": "n3", "name": "Node 3"},
]
GROUPS = [
    {"id": "arm", "name": "ARM", "members": ["hub1", "n1", "n2"]},
    {"id": "x86", "name": "x86", "members": ["n3"]},
]
OPS = {"id": "ops1", "kind": "ops", "targets": {"groups": ["arm"]}}
BATCH_DEFAULTS = {"max_parallel_hosts": 2, "auto": False, "max_plans_per_day": 0, "max_steps": 20,
                  "scripts": "approve", "script_groups": []}


def ops(**overrides: Any) -> Dict[str, Any]:
    return {**OPS, **overrides}


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class ParseOpsProjectTests(unittest.TestCase):
    def test_minimal_ops_project_has_no_workspace_and_the_server_tools(self) -> None:
        p = parse_project(OPS)
        self.assertTrue(p.is_ops)
        self.assertEqual(p.targets, {"all": False, "groups": ["arm"], "nodes": [], "exclude": []})
        opt_in = {"ops:package_install", "ops:package_remove", "ops:run_command_set", "ops:run_script"}
        self.assertEqual(p.policy, {"allowed_actions": sorted(SERVER_TOOLS - opt_in),
                                    "batch": BATCH_DEFAULTS, "include_hub": False, "deny_paths": [], "deny_commands": [],
                                    "protected_packages": [],
                                    "auto_run": [], "max_auto_runs_per_day": 0, "watch": None})
        with self.assertRaises(NoWorkspace):
            p.workspace
        self.assertEqual(workspace_status(p), {"kind": "ops", "present": False})

    def test_rejects_repository_keys_bad_targets_and_unknown_policy(self) -> None:
        bad = [
            ops(repo="https://git.example.com/me/x.git"),
            ops(workspace="~/x"),
            ops(tests={"default": "go test ./..."}),
            ops(context={"paths": ["*.go"]}),
            ops(targets={}),
            ops(targets={"all": False}),
            ops(targets={"groups": "arm"}),
            ops(targets={"groups": ["arm"], "hosts": ["n1"]}),
            ops(targets={"nodes": ["bad id"]}),
            ops(policy={"allowed_actions": ["jev_mcp:apply_patch"]}),
            ops(policy={"auto_run": ["x"], "max_auto_runs_per_day": 1}),
            ops(policy={"deny_paths": ["relative/*"]}),
            ops(policy={"deny_paths": "/srv"}),
            ops(policy={"include_hub": "yes"}),
            ops(runbook={"repo": "x"}),
            ops(kind="infra"),
            ops(id="server-guardian"),
            {"id": "svc", "repo": "https://git.example.com/me/svc.git", "targets": {"all": True}},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ProjectValidationError):
                parse_project(data)

    def test_builtin_fleet_project_targets_every_node_but_the_hub(self) -> None:
        p = ProjectManager(directory=Path("/nonexistent")).load("fleet")
        self.assertEqual(p.team, "fleet-ops")
        with self.assertRaises(ProjectValidationError):
            parse_project({**OPS, "team": "Bad Team"})
        resolved = ops_targets.resolve(p, SERVERS, GROUPS)
        self.assertEqual(resolved["ids"], ["n1", "n2", "n3"])
        self.assertFalse(resolved["include_hub"])


class ResolveTargetsTests(unittest.TestCase):
    def test_groups_nodes_and_exclude_keep_servers_order_without_the_hub(self) -> None:
        p = parse_project(ops(targets={"groups": ["arm"], "nodes": ["n3"], "exclude": ["n2"]}))
        resolved = ops_targets.resolve(p, SERVERS, GROUPS)
        self.assertEqual(resolved["ids"], ["n1", "n3"])
        self.assertEqual(resolved["hub"], "hub1")
        self.assertEqual(resolved["hosts"][0], {"id": "n1", "name": "Node 1", "host": "n1.example", "groups": ["ARM"]})

    def test_unknown_groups_and_nodes_are_reported_not_raised(self) -> None:
        p = parse_project(ops(targets={"groups": ["gone"], "nodes": ["n1", "n9"]}))
        resolved = ops_targets.resolve(p, SERVERS, GROUPS)
        self.assertEqual(resolved["ids"], ["n1"])
        self.assertEqual(resolved["missing"], {"groups": ["gone"], "nodes": ["n9"]})

    def test_check_target_allows_only_resolved_hosts_and_the_hub_only_as_local(self) -> None:
        p = parse_project(OPS)
        resolved = ops_targets.resolve(p, SERVERS, GROUPS)
        self.assertEqual(ops_targets.check_target(p, "n2", resolved), "n2")
        for target in ("n3", "hub1", "local", None, ""):
            with self.subTest(target=target), self.assertRaises(ops_targets.TargetScopeError):
                ops_targets.check_target(p, target, resolved)
        with_hub = parse_project(ops(policy={"include_hub": True}))
        resolved = ops_targets.resolve(with_hub, SERVERS, GROUPS)
        self.assertEqual(ops_targets.check_target(with_hub, None, resolved), "local")
        with self.assertRaises(ops_targets.TargetScopeError):
            ops_targets.check_target(with_hub, "hub1", resolved)

    def test_prompt_text_lists_the_hosts_and_says_the_hub_is_out(self) -> None:
        p = parse_project(OPS)
        text = ops_targets.prompt_text(p, ops_targets.resolve(p, SERVERS, GROUPS))
        self.assertIn("- n1 (Node 1, n1.example) groups: ARM", text)
        self.assertIn("The hub is not a target of this project.", text)
        self.assertNotIn("hub1", text.replace("hub is", ""))


class _FixedTargets:
    """Resolves every ops project against SERVERS and GROUPS instead of servers.json and the hub database."""

    def __enter__(self):
        real = ops_targets.resolve
        self._patch = patch.object(ops_targets, "resolve",
                                   side_effect=lambda project, servers=None, groups=None: real(project, SERVERS, GROUPS))
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class OpsClientTests(unittest.TestCase):
    def test_ops_client_offers_only_server_tools_and_checks_the_target(self) -> None:
        client = MCPClient(project=parse_project(OPS))
        self.assertIsNone(client.workspace)
        server = ["jev_mcp:server_remediation", "ops:docker_prune", "ops:journal_vacuum", "ops:apt_clean",
                  "ops:remove_paths", "ops:truncate_logs", "ops:remove_old_kernels", "ops:restart_service"]
        # the catalog lists what the kind offers; the policy (package and batch tools are opt-in) narrows what a judge may propose
        self.assertEqual([t["name"] for t in client.list_tools()],
                         server + ["ops:package_install", "ops:package_remove", "ops:run_command_set", "ops:run_script"])
        self.assertEqual(client.project_tools(None), server)
        self.assertEqual(client.project_tools(["jev_mcp:run_tests"]), [])
        with _FixedTargets():
            args = client.validate("jev_mcp:server_remediation", {"command": "df -h", "risk_level": "low",
                                                                  "target_host": "n1"})
            self.assertEqual(args["target_host"], "n1")
            for target in ("n3", "hub1", "local"):
                with self.subTest(target=target), self.assertRaisesRegex(ActionValidationError, "target|hub"):
                    client.validate("jev_mcp:server_remediation",
                                    {"command": "df -h", "risk_level": "low", "target_host": target})
        with self.assertRaisesRegex(ActionValidationError, "belongs to code projects"):
            client.validate("jev_mcp:run_tests", {"test_cmd": "go test ./..."})

    def test_a_policy_without_the_tool_refuses_it(self) -> None:
        client = MCPClient(project=parse_project(ops(policy={"allowed_actions": []})))
        self.assertEqual(client.project_tools(None), [])
        with self.assertRaisesRegex(ActionValidationError, "policy"):
            client.validate("jev_mcp:server_remediation", {"command": "df", "risk_level": "low", "target_host": "n1"})

    def test_code_projects_no_longer_offer_server_remediation(self) -> None:
        with TemporaryDirectory() as tmp:
            code = parse_project({"id": "svc", "workspace": "~/guardian-workspaces/svc"})
            client = MCPClient(workspace=Path(tmp), project=code)
            self.assertNotIn("jev_mcp:server_remediation", client.project_tools(None))
            self.assertNotIn("server_remediation", client.catalog_text(None))
            with self.assertRaisesRegex(ActionValidationError, "belongs to ops projects"):
                client.validate("jev_mcp:server_remediation", {"command": "df", "risk_level": "low"})
            # Without a project (tests, embedding) every tool stays available.
            self.assertIn("jev_mcp:server_remediation", MCPClient(workspace=Path(tmp)).project_tools(None))


class OpsManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.servers = root / "servers.json"
        self.servers.write_text(json.dumps(SERVERS))
        self._env = patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(self.servers)})
        self._env.start()
        (root / "builtin").mkdir()
        self.manager = ProjectManager(directory=root / "custom", builtin_dir=root / "builtin")
        self.db = DecisionDBManager(db_path=root / "d.db")

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_save_refuses_unknown_nodes_and_warns_when_nothing_matches(self) -> None:
        code, payload = config_ops.save_project(self.manager, json.dumps(ops(targets={"nodes": ["n9"]})))
        self.assertEqual(code, config_ops.INVALID)
        self.assertIn("unknown node n9", payload["error"])
        code, payload = config_ops.save_project(self.manager, json.dumps(ops(targets={"nodes": ["hub1"]})))
        self.assertEqual(code, 0)
        self.assertIn("the targets match no node now", payload["warnings"][0])

    def test_groups_cannot_be_checked_without_the_hub_database(self) -> None:
        from db_manager import default_db_path

        self.assertFalse(default_db_path().exists())
        code, payload = config_ops.save_project(self.manager, json.dumps(ops(targets={"groups": ["arm"]})))
        self.assertEqual(code, 0)
        self.assertEqual(payload["warnings"], ["node groups not checked: this host has no hub database"])
        resolved = ops_targets.resolve(self.manager.load("ops1"))
        self.assertEqual((resolved["missing"]["groups"], resolved["groups_unknown"]), ([], True))

    def test_report_sync_publish_reset_and_wiki_treat_an_ops_project_as_having_no_repository(self) -> None:
        self.assertEqual(config_ops.save_project(self.manager, json.dumps(ops(targets={"nodes": ["n1"]})))[0], 0)
        _, report = config_ops.projects_report(self.manager)
        entry = report["projects"][0]
        self.assertEqual((entry["errors"], entry["status"]), ([], {"kind": "ops", "present": False}))
        self.assertEqual(entry["targets"]["ids"], ["n1"])

        code, payload = config_ops.sync_projects(self.manager, "ops1")
        self.assertEqual(code, config_ops.CONFLICT)
        self.assertIn("ops project", payload["error"])
        code, payload = config_ops.sync_projects(self.manager, "all")
        self.assertEqual((code, payload["results"]), (0, []))

        workspaces = Workspaces(self.db, self.manager)
        self.assertIsNone(workspaces.for_project("ops1").workspace)
        with self.assertRaisesRegex(publish_ops.PublishRefused, "ops project"):
            publish_ops._resolve(workspaces, "ops1", None)
        with self.assertRaisesRegex(workspace_ops.ResetRefused, "ops project"):
            workspace_ops._context(workspaces, "ops1")
        with patch("project_registry.ProjectManager", return_value=self.manager), \
                self.assertRaisesRegex(wiki_ops.OpenError, "ops project"):
            wiki_ops.open_store("ops1")


class OpsDebateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "builtin").mkdir()
        self.manager = ProjectManager(directory=self.root / "custom", builtin_dir=self.root / "builtin")
        self.manager.directory.mkdir()
        (self.manager.directory / "ops1.json").write_text(json.dumps(OPS))
        self.events_path = self.root / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_debate(self, runner: FakeRunner, teams: Any = None, **kwargs: Any) -> Dict[str, Any]:
        engine = DebateEngine(max_rounds=1, db_path=self.root / "d.db", events=EventWriter(str(self.events_path)),
                              team_mgr=teams)
        engine.project_mgr = self.manager
        self.engine = engine
        with _FixedTargets(), patch.object(engine.llm, "run_session_prompt", side_effect=runner), \
                patch.object(engine, "_save_debate_log", side_effect=lambda *a, **k: (
                    engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5], context=a[6]) or self.root / "l.md")):
            try:
                return engine.run_debate("free disk space on the ARM nodes", project_id="ops1", **kwargs)
            finally:
                engine.events.close()

    @staticmethod
    def judge_answer() -> str:
        actions = [
            {"tool": "jev_mcp:server_remediation", "reason": "clean",
             "arguments": {"command": "docker image prune -f", "risk_level": "low", "target_host": "n1"}},
            {"tool": "jev_mcp:server_remediation", "reason": "outside",
             "arguments": {"command": "docker image prune -f", "risk_level": "low", "target_host": "n3"}},
            {"tool": "jev_mcp:apply_patch", "reason": "code",
             "arguments": {"edits": [{"file_path": "a.go", "old": "x", "new": "y"}]}},
        ]
        body = {"decision": "Prune images on n1.", "constraints": ["Only n1 is touched."], "checks": [], "actions": actions}
        return "Analysis.\n```json\n" + json.dumps(body) + "\n```\nVERDICT: APPROVED"

    def test_ops_debate_reads_no_code_lists_the_targets_and_validates_actions(self) -> None:
        answer = self.judge_answer()
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": [answer, answer]})
        self.run_debate(runner)

        expert = runner.prompts("expert")[0]
        self.assertNotIn("Repository retrieval", expert)
        self.assertIn("Ops project ops1: the only hosts actions may target", expert)
        self.assertIn("- n2 (Node 2, n2.example) groups: ARM", expert)
        # The tool catalog is part of the judge's standing instructions (or its first message without them).
        judge = runner.prompts("judge")[0] + runner.system_texts("judge")[0] + str(runner.calls[-1]["plain_prompt"])
        self.assertIn("jev_mcp:server_remediation", judge)
        self.assertNotIn("apply_patch", judge)
        self.assertIn("Give every action a target_host taken from the target list", judge)
        self.assertIn("the server checks you received, cited as [#call check host]", judge)

        started = next(json.loads(line) for line in self.events_path.read_text().splitlines()
                       if json.loads(line).get("type") == "debate_started")
        self.assertEqual(started["data"]["targets"] if "data" in started else started["targets"], ["n1", "n2"])

        actions = sorted(self.engine.db.list_actions(project_id="ops1"), key=lambda a: a["id"])
        self.assertEqual([a["status"] for a in actions], ["PROPOSED", "INVALID", "INVALID"])
        self.assertIn("not a target of project ops1", actions[1]["result"]["error"])
        self.assertIn("belongs to code projects", actions[2]["result"]["error"])

    def test_agents_check_hosts_with_ops_blocks_and_get_the_results_back(self) -> None:
        agent = MagicMock()
        agent.execute_remote_ssh.return_value = {"exit_code": 0, "stdout": "/dev/sda1 ext4 50G 48G 96% /", "stderr": ""}
        ask = '```ops\n{"requests": [{"op": "disk_usage", "host": "n1"}, {"op": "disk_usage", "host": "n3"}]}\n```\nguess'
        runner = FakeRunner({"expert": [ask, "n1 is 96% full [#1 disk_usage n1]"], "critic": ["critique"],
                             "judge": ["Needs work.\nVERDICT: REJECTED"]})
        with patch("ops_checks.OpsChecks._agent", return_value=agent):
            result = self.run_debate(runner)

        first, second = runner.prompts("expert")
        self.assertIn("=== Server checks (read-only) ===", first + runner.system_texts("expert")[0])
        self.assertIn("=== SERVER CHECK RESULTS (step 1) ===", second)
        self.assertIn("--- #1 disk_usage n1\n/dev/sda1 ext4 50G 48G 96% /", second)
        self.assertIn("--- #2 disk_usage n3 (failed)", second)
        self.assertIn("5 characters you wrote after your ops block", second)
        agent.execute_remote_ssh.assert_called_once()
        self.assertEqual(agent.execute_remote_ssh.call_args.kwargs["labels"]["source"], "ops_check")

        events = [json.loads(line) for line in self.events_path.read_text().splitlines()]
        served = [e for e in events if e.get("type") == "retrieval_served"]
        self.assertEqual([(e.get("data", e)["via"], len(e.get("data", e)["results"])) for e in served], [("ops", 2)])
        expert = next(t for t in result["transcript"] if t["role"] == EXPERT_ROLE)
        self.assertEqual([(r["call"], r["host"], r["ok"]) for r in expert["retrievals"]],
                         [(1, "n1", True), (2, None, False)])

    def test_an_invented_check_citation_is_sent_back_once(self) -> None:
        agent = MagicMock()
        agent.execute_remote_ssh.return_value = {"exit_code": 0, "stdout": "/dev/sda1 96% /", "stderr": ""}
        ask = '```ops\n{"requests": [{"op": "disk_usage", "host": "n1"}]}\n```'
        runner = FakeRunner({"expert": [ask, "n1 is full [#7 disk_usage n1]", "n1 is full [#1 disk_usage n1]"],
                             "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        with patch("ops_checks.OpsChecks._agent", return_value=agent):
            result = self.run_debate(runner)
        prompts = runner.prompts("expert")
        self.assertEqual(len(prompts), 3)
        self.assertIn("=== CITATION CHECK ===", prompts[2])
        self.assertIn("[#7 disk_usage n1]", prompts[2])
        expert = next(t for t in result["transcript"] if t["role"] == EXPERT_ROLE)
        self.assertEqual(expert["content"], "n1 is full [#1 disk_usage n1]")
        self.assertNotIn("unverified_citations", expert)
        events = [json.loads(line) for line in self.events_path.read_text().splitlines()]
        checks = [e.get("data", e) for e in events if e.get("type") == "citation_check"]
        self.assertEqual([(c["unverified"], c["asked"]) for c in checks], [(["[#7 disk_usage n1]"], True), ([], True)])

    def started(self) -> Dict[str, Any]:
        event = next(json.loads(line) for line in self.events_path.read_text().splitlines()
                     if json.loads(line).get("type") == "debate_started")
        return event.get("data", event)

    def test_the_project_team_runs_unless_the_request_names_one(self) -> None:
        (self.manager.directory / "ops1.json").write_text(json.dumps({**OPS, "team": "fleet-ops"}))
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        self.run_debate(runner)
        started = self.started()
        self.assertEqual(started["team"], "fleet-ops")
        self.assertEqual([m["agent_name"] for m in started["members"]],
                         ["Fleet Operations Engineer (SRE)", "Operations Safety Reviewer", "Capacity & Recurrence Reviewer",
                          "Fleet Operations Judge"])
        judge = runner.prompts("judge")[0] + runner.system_texts("judge")[0]
        self.assertIn("ops:apt_clean", judge)
        self.assertNotIn("run_tests", judge)

        self.events_path.unlink()
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        self.run_debate(runner, team="default")
        self.assertEqual(self.started()["team"], "default")

    def test_a_missing_project_team_falls_back_with_a_warning(self) -> None:
        (self.manager.directory / "ops1.json").write_text(json.dumps({**OPS, "team": "no-such-team"}))
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        self.run_debate(runner)
        started = self.started()
        self.assertEqual(started["team"], "default")
        self.assertTrue(any("no-such-team" in w for w in started["warnings"]))

    def test_code_tools_of_a_team_are_ignored_with_a_warning(self) -> None:
        from team_manager import TeamManager

        teams = TeamManager(self.root / "teams")
        teams.save({"id": "repo-tools", "expert": {"persona": "ops_sre", "code_tools": "mcp"},
                    "critic": "ops_safety_critic", "judge": {"persona": "ops_judge", "code_tools": "mcp"}})
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        self.run_debate(runner, teams=teams, team="repo-tools")
        warnings = self.started()["warnings"]
        self.assertTrue(any(w.startswith("code_tools of expert, judge are ignored") for w in warnings), warnings)

        self.events_path.unlink()
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["Needs work.\nVERDICT: REJECTED"]})
        self.run_debate(runner, teams=teams, team="default")
        self.assertFalse(any("code_tools" in w for w in self.started().get("warnings", [])))

    def test_a_judge_without_tools_warns_unless_the_team_chose_that(self) -> None:
        from team_manager import TeamManager

        teams = TeamManager(self.root / "teams")
        base = {"expert": "ops_sre", "critic": "ops_safety_critic", "judge": "ops_judge"}
        # report-only on purpose: the team lists no tool
        teams.save({"id": "report-only", "allowed_tools": [], **base})
        # opt-in package tools that the default policy of a new ops project does not allow: no overlap
        teams.save({"id": "no-overlap", "allowed_tools": ["ops:package_install"], **base})
        for team, expected in (("report-only", False), ("no-overlap", True)):
            with self.subTest(team=team):
                if self.events_path.exists():
                    self.events_path.unlink()
                runner = FakeRunner({"expert": ["plan"], "critic": ["critique"],
                                     "judge": ["Needs work.\nVERDICT: REJECTED"]})
                self.run_debate(runner, teams=teams, team=team)
                warned = any("allows no server tool" in w for w in self.started().get("warnings", []))
                self.assertEqual(warned, expected)

    def test_context_paths_are_refused(self) -> None:
        runner = FakeRunner({"expert": ["plan"], "critic": ["critique"], "judge": ["VERDICT: REJECTED"]})
        with self.assertRaisesRegex(ProjectValidationError, "reads no code"):
            self.run_debate(runner, context_paths=["apps/**/*.go"])


if __name__ == "__main__":
    unittest.main()

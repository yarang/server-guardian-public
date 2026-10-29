import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import config_ops
from db_manager import DecisionDBManager
from group_manager import (
    SEED_MARKER,
    UNGROUPED,
    GroupManager,
    GroupNotFoundError,
    GroupValidationError,
    TargetError,
    catalog,
    memberships,
    parse_group,
    resolve_members,
    resolve_target,
    seed_definitions,
    slugify,
)

HERE = Path(__file__).resolve().parent

SERVERS = [
    {"id": "hub", "name": "site-a-arm1", "hostname": "hub.example", "arch": "aarch64", "group": "Site A Cluster",
     "tier": "Tier-1", "role": "llm_cli_host", "guardian_role": "hub", "tailscale_tags": ["tag:fleet", "tag:gitea"],
     "cli_support": ["claude", "codex"]},
    {"id": "a2", "name": "site-a-ec1", "hostname": "a2.example", "arch": "x86_64", "group": "Site A Cluster",
     "tier": "Tier-2", "role": "ssh_target_node", "guardian_role": "node", "tailscale_tags": ["tag:fleet"],
     "cli_support": ["ssh_agent_remote_exec"]},
    {"id": "b1", "name": "site-b-arm1", "hostname": "b1.example", "arch": "aarch64", "group": "Site B Cluster",
     "tier": "Tier-1", "role": "llm_cli_host", "guardian_role": "node", "tailscale_tags": ["tag:fleet"],
     "cli_support": ["claude"]},
    {"id": "ws", "name": "workstation", "hostname": "ws.lan", "arch": "arm64", "group": "On-Premise Workstation",
     "tier": "Tier-2"},
]


def cond(field, op, value):
    return {"field": field, "op": op, "value": value}


def rule(*conds):
    return {"conditions": list(conds)}


class ParseGroupTests(unittest.TestCase):
    def test_normalizes_and_dedupes(self) -> None:
        g = parse_group({"name": "  ARM   hosts ", "include": ["a2", "a2"], "rules": [rule(cond("arch", "eq", " aarch64 "))]})
        self.assertEqual(g["name"], "ARM hosts")
        self.assertEqual(g["include"], ["a2"])
        self.assertEqual(g["rules"][0]["conditions"][0]["value"], "aarch64")
        self.assertNotIn("id", g)

    def test_rejects_invalid_definitions(self) -> None:
        bad = [
            [],
            {"name": ""},
            {"name": "x" * 65},
            {"name": "a\nb"},
            {"name": "g", "extra": 1},
            {"name": "g", "id": "Bad Id"},
            {"name": "g", "tone": "purple"},
            {"name": "g", "include": "hub"},
            {"name": "g", "include": ["-oProxyCommand=x"]},
            {"name": "g", "exclude": ["a b"]},
            {"name": "g", "rules": [{"conditions": []}]},
            {"name": "g", "rules": [{}]},
            {"name": "g", "rules": [rule(cond("hostname", "eq", "x"))]},
            {"name": "g", "rules": [rule(cond("tier", "glob", "T*"))]},
            {"name": "g", "rules": [rule(cond("cli_support", "eq", "claude"))]},
            {"name": "g", "rules": [rule(cond("tier", "eq", ""))]},
            {"name": "g", "rules": [rule(cond("tier", "eq", 1))]},
            {"name": "g", "rules": [rule(cond("tier", "eq", "x" * 129))]},
            {"name": "g", "rules": [rule(cond("tier", "eq", "T"))] * 21},
            {"name": "g", "rules": [rule(*[cond("tier", "eq", "T")] * 11)]},
            {"name": "g", "include": [f"s{i}" for i in range(501)]},
        ]
        for data in bad:
            with self.subTest(data=str(data)[:80]):
                with self.assertRaises(GroupValidationError):
                    parse_group(data)

    def test_preview_does_not_need_a_name(self) -> None:
        self.assertEqual(parse_group({"include": ["hub"]}, require_name=False)["name"], "")

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Site B Cluster"), "site-b-cluster")
        self.assertEqual(slugify("  Tier-1 / ARM!! "), "tier-1-arm")
        self.assertRegex(slugify("한글 그룹"), r"^group-[0-9a-f]{8}$")


class RuleEvaluationTests(unittest.TestCase):
    def members(self, **group):
        return resolve_members(parse_group({"name": "g", **group}), SERVERS)

    def test_scalar_ops_are_case_insensitive(self) -> None:
        self.assertEqual(self.members(rules=[rule(cond("tier", "eq", "tier-1"))]), ["hub", "b1"])
        self.assertEqual(self.members(rules=[rule(cond("guardian_role", "ne", "hub"))]), ["a2", "b1", "ws"])
        # A missing field never equals a value and is always "not equal".
        self.assertEqual(self.members(rules=[rule(cond("role", "ne", "llm_cli_host"))]), ["a2", "ws"])

    def test_list_ops(self) -> None:
        self.assertEqual(self.members(rules=[rule(cond("cli_support", "contains", "claude"))]), ["hub", "b1"])
        self.assertEqual(self.members(rules=[rule(cond("tailscale_tags", "not_contains", "tag:fleet"))]), ["ws"])

    def test_name_glob(self) -> None:
        self.assertEqual(self.members(rules=[rule(cond("name", "glob", "SITE-*-ARM?"))]), ["hub", "b1"])

    def test_rule_is_and_group_is_or_and_exclude_wins(self) -> None:
        rules = [
            rule(cond("arch", "eq", "aarch64"), cond("guardian_role", "eq", "node")),
            rule(cond("arch", "eq", "arm64")),
        ]
        self.assertEqual(self.members(rules=rules), ["b1", "ws"])
        self.assertEqual(self.members(rules=rules, include=["a2"], exclude=["ws", "a2"]), ["b1"])

    def test_unknown_include_is_ignored_in_members(self) -> None:
        self.assertEqual(self.members(include=["gone", "hub"]), ["hub"])

    def test_catalog_lists_fields_ops_and_seen_values(self) -> None:
        fields = {f["field"]: f for f in catalog(SERVERS)["fields"]}
        self.assertEqual(set(fields), {"tier", "arch", "guardian_role", "role", "tailscale_tags", "cli_support", "name"})
        self.assertEqual(fields["arch"]["values"], ["aarch64", "arm64", "x86_64"])
        self.assertEqual(fields["tailscale_tags"]["ops"], ["contains", "not_contains"])
        self.assertIn("tag:gitea", fields["tailscale_tags"]["values"])
        self.assertEqual(fields["name"]["ops"], ["glob", "eq", "ne"])

    def test_memberships_counts_each_group_and_ungrouped(self) -> None:
        groups = [{"name": "A", "members": ["hub", "a2"]}, {"name": "ARM", "members": ["hub", "b1"]}]
        self.assertEqual(
            memberships(groups, ["hub", "a2", "b1", "ws"]),
            {"A": ["hub", "a2"], "ARM": ["hub", "b1"], UNGROUPED: ["ws"]},
        )
        # Only probed ids count; a user group called "Ungrouped" is not overwritten.
        self.assertEqual(memberships([{"name": UNGROUPED, "members": ["hub"]}], ["hub", "ws"]),
                         {UNGROUPED: ["hub"], f"{UNGROUPED} (no group)": ["ws"]})


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.servers_path = root / "servers.json"
        self.servers_path.write_text(json.dumps(SERVERS))
        self.db = DecisionDBManager(root / "db.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def manager(self) -> GroupManager:
        return GroupManager(db=self.db, servers_path=self.servers_path)


class SeedTests(StoreTestCase):
    def test_seeds_one_static_group_per_legacy_group_once(self) -> None:
        groups = {g["id"]: g for g in self.manager().list_groups()}
        self.assertEqual(set(groups), {"site-a-cluster", "site-b-cluster", "on-premise-workstation"})
        site_a = groups["site-a-cluster"]
        self.assertEqual((site_a["name"], site_a["include"], site_a["rules"], site_a["members"]),
                         ("Site A Cluster", ["hub", "a2"], [], ["hub", "a2"]))
        self.assertEqual(self.db.get_meta(SEED_MARKER), "1")

    def test_deleting_every_group_does_not_reseed(self) -> None:
        m = self.manager()
        for g in m.list_groups():
            m.delete(g["id"])
        self.assertEqual(self.manager().list_groups(), [])

    def test_seed_waits_for_servers_json(self) -> None:
        missing = Path(self.tmp.name) / "nope.json"
        m = GroupManager(db=self.db, servers_path=missing)
        self.assertEqual(m.list_groups(), [])
        self.assertIsNone(self.db.get_meta(SEED_MARKER))
        self.assertIn("cannot read", m.report()["warnings"][0])
        self.assertEqual(len(self.manager().list_groups()), 3)

    def test_existing_groups_block_the_seed(self) -> None:
        self.db.save_node_group({"id": "mine", "name": "Mine", "include": ["ws"]}, "mine")
        self.assertEqual([g["id"] for g in self.manager().list_groups()], ["mine"])

    def test_seed_definitions_dedupe_names_case_insensitively(self) -> None:
        defs = seed_definitions([{"id": "x", "group": "Lab"}, {"id": "y", "group": " lab "}, {"id": "z"}])
        self.assertEqual([(d["id"], d["include"]) for d in defs], [("lab", ["x", "y"])])


class CrudTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.seed_node_groups([], SEED_MARKER)  # start without seeded groups

    def test_create_update_and_rename_keep_the_id(self) -> None:
        m = self.manager()
        res = m.save({"name": "ARM nodes", "rules": [rule(cond("arch", "eq", "aarch64")), rule(cond("arch", "eq", "arm64"))], "tone": "info"})
        self.assertEqual((res["id"], res["members"]), ("arm-nodes", ["hub", "b1", "ws"]))
        created = self.db.get_node_group("arm-nodes")["created_at"]

        res = m.save({"id": "arm-nodes", "name": "ARM fleet", "rules": [rule(cond("arch", "eq", "aarch64")), rule(cond("arch", "eq", "arm64"))], "exclude": ["ws"]})
        self.assertEqual((res["id"], res["members"]), ("arm-nodes", ["hub", "b1"]))
        stored = self.db.get_node_group("arm-nodes")
        self.assertEqual((stored["name"], stored["created_at"], stored["tone"]), ("ARM fleet", created, None))

    def test_names_are_unique_case_insensitively_and_ids_get_suffixes(self) -> None:
        m = self.manager()
        m.save({"name": "Lab"})
        with self.assertRaisesRegex(GroupValidationError, "already used by lab"):
            m.save({"name": " LAB "})
        self.assertEqual(m.save({"name": "Lab!"})["id"], "lab-2")

    def test_warnings_for_unknown_ids_and_empty_groups(self) -> None:
        res = self.manager().save({"name": "Old", "include": ["gone"], "exclude": ["also-gone"]})
        self.assertEqual(res["members"], [])
        self.assertEqual(res["warnings"], [
            "include: gone is not in servers.json",
            "exclude: also-gone is not in servers.json",
            "no servers match this group",
        ])

    def test_delete_unknown(self) -> None:
        with self.assertRaises(GroupNotFoundError):
            self.manager().delete("nope")
        with self.assertRaises(GroupNotFoundError):
            self.manager().delete("../x")

    def test_report_and_preview(self) -> None:
        m = self.manager()
        m.save({"name": "Hub", "include": ["hub"]})
        report = m.report()
        self.assertEqual([g["id"] for g in report["groups"]], ["hub"])
        self.assertEqual(report["ungrouped"], ["a2", "b1", "ws"])
        self.assertEqual(report["servers_file"], str(self.servers_path))
        self.assertEqual(m.preview({"rules": [rule(cond("tier", "eq", "Tier-1"))]})["members"], ["hub", "b1"])


class ConfigOpsTests(StoreTestCase):
    def test_exit_codes(self) -> None:
        m = self.manager()
        code, payload = config_ops.save_group(m, json.dumps({"name": "site B cluster", "include": ["b1"]}))
        self.assertEqual((code, payload["ok"]), (2, False))
        self.assertIn("already used", payload["error"])
        code, payload = config_ops.save_group(m, "{not json")
        self.assertEqual(code, 2)
        code, payload = config_ops.save_group(m, "x" * (65 * 1024))
        self.assertEqual(code, 2)
        code, payload = config_ops.save_group(m, json.dumps({"name": "New", "include": ["b1"]}))
        self.assertEqual((code, payload["id"], payload["members"]), (0, "new", ["b1"]))
        code, payload = config_ops.preview_group(m, json.dumps({"rules": [{"conditions": []}]}))
        self.assertEqual(code, 2)
        code, payload = config_ops.delete_group(m, "new")
        self.assertEqual((code, payload), (0, {"ok": True, "id": "new"}))
        code, payload = config_ops.delete_group(m, "new")
        self.assertEqual((code, payload["ok"]), (3, False))
        code, payload = config_ops.groups_report(m)
        self.assertEqual(code, 0)
        self.assertIn("catalog", payload)


class ResolveTargetTests(unittest.TestCase):
    GROUPS = [
        {"id": "arm", "name": "ARM Nodes", "members": ["hub", "b1"]},
        {"id": "empty", "name": "Empty", "members": []},
        {"id": "workstation", "name": "Shadow", "members": ["hub"]},
    ]

    def test_server_first_then_group_by_id_or_name(self) -> None:
        self.assertEqual(resolve_target("a2", SERVERS, self.GROUPS)["ids"], ["a2"])
        self.assertEqual(resolve_target("hub.example", SERVERS, self.GROUPS)["ids"], ["hub"])
        # A server named "workstation" wins over the group with that id.
        self.assertEqual(resolve_target("workstation", SERVERS, self.GROUPS), {"kind": "server", "id": "ws", "name": "workstation", "ids": ["ws"]})
        self.assertEqual(resolve_target("arm", SERVERS, self.GROUPS)["ids"], ["hub", "b1"])
        self.assertEqual(resolve_target("arm nodes", SERVERS, self.GROUPS)["kind"], "group")

    def test_errors_list_close_matches(self) -> None:
        with self.assertRaisesRegex(TargetError, "Did you mean: .*ARM Nodes"):
            resolve_target("ARM Node", SERVERS, self.GROUPS)
        with self.assertRaisesRegex(TargetError, "has no members"):
            resolve_target("empty", SERVERS, self.GROUPS)
        with self.assertRaisesRegex(TargetError, "--groups"):
            resolve_target("zzzzzz", SERVERS, self.GROUPS)


class RegisteredNodeInventoryTests(unittest.TestCase):
    """Entries the dashboard writes (docs/design/node-registration.md): id, name and metadata only.
    hostname/user/port live in the hub's SSH fleet file, there is no legacy `group`, and the file
    is found through GUARDIAN_SERVERS_FILE (~/.guardian/data/servers.json on the hub)."""

    REGISTERED = [
        {"id": "hub", "name": "hub", "guardian_role": "hub", "tier": "Tier-1", "arch": "aarch64",
         "hostname": "hub.example", "user": "ubuntu", "port": 22, "group": "Core", "recommended": True,
         "cli_support": ["claude"], "tailscale_tags": ["tag:fleet"]},
        {"id": "edge-1", "name": "edge-1", "guardian_role": "node", "tier": "Tier-2", "arch": "x86_64",
         "cli_support": ["ssh_agent_remote_exec"], "tailscale_tags": ["tag:fleet"], "description": "Edge <1> & co"},
        {"id": "edge-2", "name": "edge-2", "guardian_role": "node"},
    ]

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "data" / "servers.json"
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(self.REGISTERED))
        self.db = DecisionDBManager(self.root / "db.sqlite")
        self.addCleanup(self.tmp.cleanup)

    def test_a_set_servers_file_is_never_replaced_by_a_fallback(self) -> None:
        import group_manager

        missing = self.root / "gone.json"
        with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(missing)}):
            self.assertEqual(group_manager.servers_file(), missing)
            servers, error, path = group_manager.load_servers()
        self.assertEqual((servers, path), ([], missing))
        self.assertIn(str(missing), error)
        self.path.write_text("not json")
        with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(self.path)}):
            self.assertIn(str(self.path), group_manager.load_servers()[1])

    def test_servers_file_env_is_used_and_entries_load_without_connection_fields(self) -> None:
        import group_manager

        with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(self.path)}):
            self.assertEqual(group_manager.servers_file(), self.path)
            manager = GroupManager(db=self.db)
        self.assertEqual([s["id"] for s in manager.servers], ["hub", "edge-1", "edge-2"])
        self.assertIsNone(manager.servers_error)
        self.assertEqual(manager.servers_path, self.path)

    def test_rules_catalog_and_ungrouped_work_without_hostname_user_port_or_group(self) -> None:
        manager = GroupManager(db=self.db, servers_path=self.path)
        # Only `hub` carries the legacy group string, so the one-time seed creates a single group.
        report = manager.report()
        self.assertEqual([g["name"] for g in report["groups"]], ["Core"])
        self.assertEqual(report["ungrouped"], ["edge-1", "edge-2"])
        saved = manager.save({"name": "Agents", "rules": [rule(cond("guardian_role", "ne", "hub"))]})
        self.assertEqual(saved["members"], ["edge-1", "edge-2"])
        by_tier = manager.preview({"rules": [rule(cond("tier", "eq", "tier-2"), cond("cli_support", "contains", "ssh_agent_remote_exec"))]})
        self.assertEqual(by_tier["members"], ["edge-1"])
        by_arch = manager.preview({"rules": [rule(cond("arch", "eq", "x86_64"))]})
        self.assertEqual(by_arch["members"], ["edge-1"])
        fields = {f["field"]: f["values"] for f in report["catalog"]["fields"]}
        self.assertEqual(fields["arch"], ["aarch64", "x86_64"])
        self.assertEqual(fields["name"], ["edge-1", "edge-2", "hub"])
        self.assertEqual(report["warnings"], [])

    def test_resolve_target_by_id_or_name_and_hostname_only_when_present(self) -> None:
        servers = self.REGISTERED
        self.assertEqual(resolve_target("edge-1", servers, [])["ids"], ["edge-1"])
        self.assertEqual(resolve_target("hub.example", servers, [])["ids"], ["hub"])
        # A missing hostname never matches an empty or unrelated target.
        for bad in ("", "None", "null"):
            with self.assertRaises(TargetError):
                resolve_target(bad, servers, [])
        # The hub reports hostname/user/port from the fleet file via /api/nodes; the same ids resolve.
        overlay = [{**s, "hostname": "203.0.113.9", "connection": "fleet_file"} if s["id"] == "edge-1" else s for s in servers]
        self.assertEqual(resolve_target("203.0.113.9", overlay, [])["ids"], ["edge-1"])

    def test_cli_groups_json_reads_the_hub_inventory_path(self) -> None:
        env = {**os.environ, "GUARDIAN_DATA_DIR": str(self.root / "data"), "GUARDIAN_SERVERS_FILE": str(self.path),
               "GUARDIAN_LLM_PROVIDERS": "none"}
        env.pop("GUARDIAN_DB_PATH", None)
        done = subprocess.run([sys.executable, str(HERE / "guardian.py"), "--groups", "--json"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        self.assertEqual(out["servers_file"], str(self.path))
        self.assertEqual(out["ungrouped"], ["edge-1", "edge-2"])
        self.assertEqual(out["warnings"], [])


class CliTests(unittest.TestCase):
    """guardian.py as the Go hub runs it: JSON on stdout, exit codes per config_ops."""

    def run_cli(self, data_dir: str, servers: Path, *args: str, stdin: str = ""):
        env = {**os.environ, "GUARDIAN_DATA_DIR": data_dir, "GUARDIAN_SERVERS_FILE": str(servers),
               "GUARDIAN_LLM_PROVIDERS": "none"}
        env.pop("GUARDIAN_DB_PATH", None)
        return subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], input=stdin, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_groups_save_preview_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            servers = Path(tmp) / "servers.json"
            servers.write_text(json.dumps(SERVERS))
            out = self.run_cli(tmp, servers, "--groups", "--json")
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertEqual(len(report["groups"]), 3)

            out = self.run_cli(tmp, servers, "--group-save", "--json",
                               stdin=json.dumps({"name": "Tier 1", "rules": [rule(cond("tier", "eq", "Tier-1"))]}))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["members"], ["hub", "b1"])

            out = self.run_cli(tmp, servers, "--group-save", "--json", stdin=json.dumps({"name": "x", "bogus": 1}))
            self.assertEqual(out.returncode, 2)
            self.assertFalse(json.loads(out.stdout)["ok"])

            out = self.run_cli(tmp, servers, "--group-preview", "--json", stdin=json.dumps({"include": ["ws"]}))
            self.assertEqual(json.loads(out.stdout)["members"], ["ws"])

            out = self.run_cli(tmp, servers, "--group-delete", "tier-1", "--json")
            self.assertEqual(out.returncode, 0, out.stderr)
            out = self.run_cli(tmp, servers, "--group-delete", "tier-1", "--json")
            self.assertEqual(out.returncode, 3)

            out = self.run_cli(tmp, servers, "--groups")
            self.assertIn("site-b-cluster", out.stdout)


class CliTargetTests(unittest.TestCase):
    def test_hub_groups_are_used_and_local_db_is_the_fallback(self) -> None:
        import guardian

        client = MagicMock()
        client.get_nodes.return_value = {"nodes": SERVERS}
        client.get_groups.return_value = {"groups": [{"id": "arm", "name": "ARM", "members": ["hub", "b1"]}]}
        self.assertIsNone(guardian.resolve_cli_target(client, "all"))
        self.assertEqual(guardian.resolve_cli_target(client, "ARM")["ids"], ["hub", "b1"])

        client.get_groups.side_effect = RuntimeError("HTTP 404")
        local = MagicMock(servers=SERVERS)
        local.list_groups.return_value = [{"id": "site-b", "name": "Site B", "members": ["b1"]}]
        with patch("group_manager.GroupManager", return_value=local), \
                patch("db_manager.default_db_path", return_value=Path(__file__)):
            self.assertEqual(guardian.resolve_cli_target(client, "site-b")["ids"], ["b1"])
        # Off the hub (no database file) groups are unavailable rather than a new empty database.
        with patch("db_manager.default_db_path", return_value=Path("/nonexistent/db.sqlite")):
            with self.assertRaises(TargetError):
                guardian.resolve_cli_target(client, "site-b")


class FleetIntegrationTests(unittest.TestCase):
    def test_diagnosis_is_scoped_and_summarized_per_user_group(self) -> None:
        from multi_agent import MultiAgentSystem

        system = MultiAgentSystem.__new__(MultiAgentSystem)
        system.client = MagicMock()
        system.client.diagnose_fleet.return_value = {
            "total_nodes": 2, "online_count": 1, "duration_sec": 1.0,
            "results": [{"id": "hub", "name": "hub", "status": "ONLINE"}, {"id": "ws", "name": "ws", "status": "OFFLINE"}],
        }
        system.client.get_groups.return_value = {"groups": [
            {"name": "A", "members": ["hub"]}, {"name": "B", "members": ["hub", "b1"]},
        ]}
        system.llm = MagicMock()
        system.llm.run_prompt.return_value = ("builtin", "ok")

        res = system.run_fleet_diagnostic(["hub", "ws"])
        system.client.diagnose_fleet.assert_called_once_with(["hub", "ws"])
        self.assertEqual(res["target"], "hub,ws")
        context = system.llm.run_prompt.call_args.args[1]
        self.assertEqual(context["fleet_groups"], {"A": ["hub"], "B": ["hub"], UNGROUPED: ["ws"]})

    def test_builtin_summary_counts_a_node_in_each_group(self) -> None:
        from llm_runner import LLMRunner

        with patch.dict(os.environ, {"GUARDIAN_LLM_PROVIDERS": "none"}):
            runner = LLMRunner()
        fleet = [{"id": "hub", "name": "hub", "status": "ONLINE"}, {"id": "ws", "name": "ws", "status": "OFFLINE"}]
        text = runner._expert_reasoning("fleet", {"fleet_summary": fleet,
                                                  "fleet_groups": {"A": ["hub"], "B": ["hub"], UNGROUPED: ["ws"]}})
        self.assertRegex(text, r"- A\s+: 1/1")
        self.assertRegex(text, r"- B\s+: 1/1")
        self.assertRegex(text, r"- Ungrouped\s+: 0/1")
        # Without group data there are no group lines (and no legacy servers.json group).
        text = runner._expert_reasoning("fleet", {"fleet_summary": [{"id": "x", "group": "Legacy", "status": "ONLINE"}]})
        self.assertNotIn("Legacy", text)

    def test_client_sends_targets(self) -> None:
        from agent_client import NodeAgentClient

        client = NodeAgentClient("http://hub.test", token="")
        with patch.object(client, "_fetch_json", return_value={}) as fetch:
            client.diagnose_fleet(["a b", "c"])
            self.assertEqual(fetch.call_args.args[0], "/api/fleet/diagnose?targets=a+b%2Cc")
            client.execute_fleet("uptime", targets=["c"])
            self.assertEqual(fetch.call_args.kwargs["data"]["targets"], ["c"])
            client.diagnose_fleet()
            self.assertEqual(fetch.call_args.args[0], "/api/fleet/diagnose")


if __name__ == "__main__":
    unittest.main()

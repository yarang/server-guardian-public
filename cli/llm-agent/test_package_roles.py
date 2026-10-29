"""Role-based and derived protection for package actions (docs/design/fleet-provisioning.md, 6.2, P4).

No model and no package manager: facts are written out, the roles come from configs/package-roles.json or a scratch
file, and the inventory is a list in this file."""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List
from unittest.mock import patch

import ops_actions
import ops_facts
import ops_packages as pk
import ops_targets
import package_roles
from mcp_client import ActionValidationError, MCPClient
from project_registry import ProjectValidationError, parse_project
from test_ops_packages import FakeAgent, args, facts, sections
from test_ops_project import GROUPS, OPS
from testenv import isolate_data_dir, restore_data_dir

INSTALL, REMOVE = pk.INSTALL, pk.REMOVE
CATALOG = package_roles.load_roles()


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def inventory(roles: Any) -> List[Dict[str, Any]]:
    node = {"id": "n1", "name": "Node 1", "hostname": "n1.example"}
    if roles is not None:
        node["ops_roles"] = roles
    return [{"id": "hub1", "name": "Hub", "guardian_role": "hub", "hostname": "hub.example"}, node,
            {"id": "n2", "name": "Node 2", "hostname": "n2.example"}]


class _Inventory:
    def __init__(self, roles: Any) -> None:
        self.servers = inventory(roles)

    def __enter__(self) -> "_Inventory":
        real = ops_targets.resolve
        self._patch = patch.object(ops_targets, "resolve",
                                   side_effect=lambda project, servers=None, groups=None: real(project, self.servers, GROUPS))
        self._patch.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._patch.stop()


class RolesFileTests(unittest.TestCase):
    def test_the_repository_file_is_valid_and_covers_the_first_roles(self) -> None:
        self.assertEqual(set(CATALOG), {"web", "database", "gitea", "headscale", "llm_cli_host", "build", "monitoring"})
        for name, role in CATALOG.items():
            self.assertTrue(role["description"], name)
            self.assertTrue(set(role["protected"]) <= set(pk.MANAGERS), name)

    def test_the_report_is_what_the_dashboard_reads(self) -> None:
        code, out = package_roles.report()
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual([r["name"] for r in out["roles"]], sorted(CATALOG))
        self.assertIn("apt", out["managers"])

    def test_an_invalid_file_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "roles.json"
            path.write_text("{not json", encoding="utf-8")
            code, out = package_roles.report(path)
            self.assertEqual(code, 2)
            self.assertFalse(out["ok"])
            self.assertEqual(out["roles"], [])
            with self.assertRaises(ValueError):
                package_roles.load_roles(Path(tmp) / "missing.json")

    def test_validation_refuses_bad_shapes(self) -> None:
        good = {"version": 1, "roles": {"web": {"description": "d", "protected": {"apt": ["nginx*"]}}}}
        self.assertEqual(package_roles.validate_roles(good)["web"]["protected"], {"apt": ["nginx*"]})
        bad: List[Any] = [
            [], {"version": 2, "roles": good["roles"]}, {"version": 1, "roles": {}},
            {"version": 1, "roles": {"Web": good["roles"]["web"]}},
            {"version": 1, "roles": {"web": {"description": "d", "protected": {"apt": ["*"]}}}},
            {"version": 1, "roles": {"web": {"description": "d", "protected": {"apt": ["a b"]}}}},
            {"version": 1, "roles": {"web": {"description": "d", "protected": {"cargo": ["x"]}}}},
            {"version": 1, "roles": {"web": {"description": "d", "protected": {"apt": []}}}},
            {"version": 1, "roles": {"web": {"description": "d", "protected": {}}}},
            {"version": 1, "roles": {"web": {"description": "d\nx", "protected": {"apt": ["x"]}}}},
            {"version": 1, "roles": {"web": {"description": "d", "protected": {"apt": ["x"]}, "extra": 1}}},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                package_roles.validate_roles(data)

    def test_pattern_checks(self) -> None:
        self.assertEqual(package_roles.check_patterns(["nginx*", "nginx*", "@scope/pkg", "lib?x"]), ["nginx*", "@scope/pkg", "lib?x"])
        self.assertEqual(package_roles.check_patterns(None), [])
        for bad in ("*", "?", "a b", "x[1]", "", "-x", "a;b", 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                package_roles.check_patterns([bad])
        with self.assertRaises(ValueError):
            package_roles.check_patterns("nginx")
        with self.assertRaises(ValueError):
            package_roles.check_patterns(["a"] * (package_roles.MAX_PROJECT_PATTERNS + 1))


class ProtectionTests(unittest.TestCase):
    def test_derived_takes_service_owners_and_listening_processes(self) -> None:
        f = facts(services=[{"unit": "nginx.service", "package": "nginx"}, {"unit": "x.service", "package": "unknown"},
                            {"unit": "y.service", "package": "weird name"}],
                  ports=[{"listen": "0.0.0.0:6379", "process": "redis-server"}, {"listen": ":22", "process": "unknown"},
                         {"listen": ":9", "process": "a/b"}])
        self.assertEqual(package_roles.derived(f), [("nginx", "running nginx.service"), ("redis-server", "listening on 0.0.0.0:6379")])
        self.assertEqual(package_roles.derived(None), [])

    def test_a_listening_port_protects_the_package_that_owns_its_binary(self) -> None:
        f = facts(ports=[{"listen": "0.0.0.0:5432", "process": "postgres", "package": "postgresql-16"},
                         {"listen": "0.0.0.0:80", "process": "nginx", "package": "unknown"},  # old format: process name
                         {"listen": ":9", "process": "x", "package": "a/b"}])
        self.assertEqual(package_roles.derived(f), [("nginx", "listening on 0.0.0.0:80"),
                                                    ("postgresql-16", "listening on 0.0.0.0:5432")])
        p = package_roles.protection([], f, (), CATALOG)
        self.assertEqual(p.source_of("apt", "postgresql-16"), ("postgresql-16", "listening on 0.0.0.0:5432"))
        self.assertIsNone(p.source_of("apt", "postgres"))  # the process name is not a package here

    def test_blind_spots_name_unknown_owners_and_hidden_visibility(self) -> None:
        self.assertEqual(package_roles.blind_spots(None), [])
        self.assertEqual(package_roles.blind_spots(facts(ports_owner_visibility="full",
                                                         ports=[{"listen": ":80", "process": "nginx", "package": "nginx"}])), [])
        both = package_roles.blind_spots(facts(ports_owner_visibility="partial", ports=[
            {"listen": f"0.0.0.0:{n}", "process": "unknown", "package": "unknown"} for n in (22, 111, 25, 26)]))
        self.assertEqual(len(both), 2)
        self.assertIn("hidden", both[0])
        self.assertIn("0.0.0.0:111, 0.0.0.0:22, 0.0.0.0:25 and 1 more", both[1])
        # Facts stored before the visibility line existed say nothing about it; a service without a package is no blind spot.
        self.assertEqual(package_roles.blind_spots(facts(services=[{"unit": "x.service", "package": "unknown"}])), [])

    def test_blind_spots_matter_for_system_managers_only(self) -> None:
        f = facts(ports=[{"listen": "0.0.0.0:22", "process": "unknown", "package": "unknown"}])
        p = package_roles.protection([], f, (), CATALOG)
        self.assertEqual(len(p.blind_for("apt")), 1)
        self.assertEqual(p.blind_for("npm"), [])
        self.assertEqual(package_roles.protection([], facts(), (), CATALOG).blind_for("apt"), [])

    def test_sources_are_merged_per_manager_and_named(self) -> None:
        f = facts(services=[{"unit": "nginx.service", "package": "nginx"}])
        p = package_roles.protection(["web"], f, ["internal-*"], CATALOG)
        self.assertEqual(p.unknown_roles, [])
        self.assertEqual(p.source_of("apt", "nginx-core"), ("nginx*", "role web"))
        self.assertEqual(p.source_of("apt", "internal-tool"), ("internal-*", "project policy"))
        self.assertEqual(p.source_of("npm", "internal-cli"), ("internal-*", "project policy"))  # "*" key: every manager
        self.assertIsNone(p.source_of("npm", "nginx-core"))  # role patterns of apt do not leak into npm
        self.assertIsNone(p.source_of("apt", "htop"))
        self.assertEqual(p.source_of("apt", "nginx:arm64")[0], "nginx*")  # architecture suffix is ignored
        self.assertTrue(any("role web" in line for line in p.describe("apt")))

    def test_derived_protection_applies_to_system_managers_only(self) -> None:
        f = facts(services=[{"unit": "redis-server.service", "package": "redis-server"}])
        p = package_roles.protection([], f, (), CATALOG)
        self.assertEqual(p.source_of("apt", "redis-server"), ("redis-server", "running redis-server.service"))
        self.assertIsNone(p.source_of("npm", "redis-server"))

    def test_unknown_roles_are_reported(self) -> None:
        p = package_roles.protection(["web", "wbe"], None, (), CATALOG)
        self.assertEqual(p.unknown_roles, ["wbe"])

    def test_an_unreadable_roles_file_raises(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_PACKAGE_ROLES": "/nonexistent/roles.json"}), self.assertRaises(ValueError):
            package_roles.protection([], None)


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        ops_facts.store_facts("n1", facts(services=[{"unit": "redis-server.service", "package": "redis-server"}]))
        self.addCleanup(lambda: os.path.exists(ops_facts.facts_dir() / "n1.json") and os.unlink(ops_facts.facts_dir() / "n1.json"))

    def client(self, **policy: Any) -> MCPClient:
        return MCPClient(project=parse_project({**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE], **policy}}),
                         agent_client=FakeAgent([]))

    def test_a_role_protects_its_packages_from_removal(self) -> None:
        with _Inventory(["web"]):
            c = self.client()
            with self.assertRaisesRegex(ActionValidationError, r"nginx-core is protected \(nginx\* .*role web"):
                c.validate(REMOVE, args("apt", "nginx-core"))
            self.assertEqual(c.validate(REMOVE, args("apt", "htop"))["manager"], "apt")
            self.assertEqual(c.validate(INSTALL, args("apt", "nginx-core"))["manager"], "apt")  # install is not blocked

    def test_without_the_role_the_same_removal_is_allowed(self) -> None:
        with _Inventory(None):
            self.assertEqual(self.client().validate(REMOVE, args("apt", "nginx-core"))["manager"], "apt")

    def test_a_running_service_protects_its_package(self) -> None:
        with _Inventory(None), self.assertRaisesRegex(ActionValidationError, "running redis-server.service"):
            self.client().validate(REMOVE, args("apt", "redis-server"))

    def test_the_project_policy_adds_to_every_manager(self) -> None:
        with _Inventory(None):
            c = self.client(protected_packages=["internal-*"])
            for manager in ("apt", "npm"):
                with self.subTest(manager=manager), self.assertRaisesRegex(ActionValidationError, "project policy"):
                    c.validate(REMOVE, args(manager, "internal-tool"))

    def test_an_unknown_role_refuses_every_package_action_on_the_host(self) -> None:
        with _Inventory(["wbe"]), self.assertRaisesRegex(ActionValidationError, "does not define: wbe"):
            self.client().validate(INSTALL, args("apt", "htop"))

    def test_the_protection_is_recomputed_at_the_preview(self) -> None:
        out = sections("nginx-core 1.24 ii", "Remv nginx-core [1.24]\n")
        with _Inventory(["web"]):
            c = MCPClient(project=parse_project({**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE]}}), agent_client=FakeAgent([out]))
            with self.assertRaises(ActionValidationError):
                c.ops_preview(REMOVE, args("apt", "nginx-core"))

    def test_the_measured_preview_lists_what_is_protected(self) -> None:
        agent = FakeAgent([sections("", "Inst htop (3.3.0 Ubuntu [arm64])\n")])
        with _Inventory(["web"]):
            c = MCPClient(project=parse_project({**OPS, "policy": {"allowed_actions": [INSTALL, REMOVE]}}), agent_client=agent)
            preview = c.ops_preview(INSTALL, args("apt", "htop"))
        self.assertIn("nginx* (role web)", preview["protected"])

    def test_a_removal_on_a_host_with_a_blind_spot_is_high_risk(self) -> None:
        blind_facts = facts(ports=[{"listen": "0.0.0.0:22", "process": "unknown", "package": "unknown"}])
        a = pk.check(REMOVE, args("apt", "htop"), blind_facts)
        agent = FakeAgent([sections("htop 3 ii", "Remv htop [3]\n")])
        found = ops_actions.measure(agent, "n1", REMOVE, a, "fleet",
                                    protection=package_roles.protection([], blind_facts, (), CATALOG))
        self.assertEqual(len(found["blind"]), 1)
        self.assertEqual(ops_actions.risk(REMOVE, a, found), "high")
        self.assertEqual(ops_actions.risk(INSTALL, a, {"blind": found["blind"]}), "medium")  # only removals are held back
        clear = ops_actions.measure(FakeAgent([sections("htop 3 ii", "Remv htop [3]\n")]), "n1", REMOVE, a, "fleet",
                                    protection=package_roles.protection([], facts(), (), CATALOG))
        self.assertNotIn("blind", clear)
        self.assertEqual(ops_actions.risk(REMOVE, a, clear), "medium")

    def test_a_simulation_that_would_remove_a_role_package_is_refused(self) -> None:
        a = pk.check(REMOVE, args("apt", "htop"), facts())
        protection = package_roles.protection(["web"], facts(), (), CATALOG)
        agent = FakeAgent([sections("htop 3 ii", "Remv htop [3]\nRemv nginx-core [1.24]\n")])
        res = ops_actions.execute(agent, "n1", REMOVE, a, "fleet", 3, protection=protection)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("role web", res["error"])
        self.assertEqual(agent.runs(), [])


class InventoryAndPolicyTests(unittest.TestCase):
    def test_resolve_carries_the_roles_and_host_roles_reads_them(self) -> None:
        project = parse_project(OPS)
        with _Inventory(["web", "database"]):
            resolved = ops_targets.resolve(project)
            self.assertEqual(resolved["roles"]["n1"], ["web", "database"])
            self.assertEqual(ops_targets.host_roles(project, "n1", resolved), ["web", "database"])
            self.assertEqual(ops_targets.host_roles(project, "n2", resolved), [])
            self.assertEqual(ops_targets.host_roles(project, "local", resolved), [])

    def test_policy_protected_packages_is_validated(self) -> None:
        p = parse_project({**OPS, "policy": {"protected_packages": ["nginx*", "nginx*", "x"]}})
        self.assertEqual(p.policy["protected_packages"], ["nginx*", "x"])
        self.assertEqual(parse_project(OPS).policy["protected_packages"], [])
        with self.assertRaisesRegex(ProjectValidationError, "policy.protected_packages"):
            parse_project({**OPS, "policy": {"protected_packages": ["*"]}})
        with self.assertRaisesRegex(ProjectValidationError, "policy.protected_packages"):
            parse_project({**OPS, "policy": {"protected_packages": "nginx"}})


if __name__ == "__main__":
    unittest.main()

"""Install bundles for package actions (docs/design/fleet-provisioning.md, 6.3).

No model and no package manager: the bundles come from configs/package-bundles.json or a scratch file, the facts are
written out, and expanded bundles are handed to the same validator as packages typed by hand."""
import datetime
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict
from unittest.mock import patch

import ops_facts
import ops_packages as pk
import package_bundles
import package_roles
from mcp_client import ActionValidationError, MCPClient
from project_registry import parse_project
from test_ops_packages import FEDORA, MAC, FakeAgent, args, facts
from test_ops_project import OPS
from test_package_roles import _Inventory
from testenv import isolate_data_dir, restore_data_dir

CATALOG = package_bundles.load_bundles()


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def bundle(**packages: Any) -> Dict[str, Any]:
    return {"version": 1, "bundles": {"demo": {"description": "d", "reviewed": "2026-10-19", "packages": packages}}}


class BundlesFileTests(unittest.TestCase):
    def test_the_repository_file_has_the_first_two_bundles(self) -> None:
        self.assertEqual(set(CATALOG), {"nginx", "postgresql"})
        for name, entry in CATALOG.items():
            self.assertTrue(entry["description"], name)
            for manager, lists in entry["packages"].items():
                self.assertIn(manager, pk.MANAGERS)
                self.assertTrue(lists["required"], f"{name}/{manager}")

    def test_the_report_is_what_the_judge_reads(self) -> None:
        code, out = package_bundles.report()
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual([b["name"] for b in out["bundles"]], ["nginx", "postgresql"])

    def test_an_invalid_file_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundles.json"
            path.write_text("{not json")
            code, out = package_bundles.report(path)
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertIn("cannot read", out["error"])
        self.assertEqual(package_bundles.report(Path("/nonexistent/bundles.json"))[0], 2)

    def test_bad_shapes_name_the_problem(self) -> None:
        bad = [
            (None, "must be an object"),
            ({"version": 2, "bundles": {}}, "must be an object"),
            ({"version": 1, "bundles": {}}, "1 to"),
            ({"version": 1, "bundles": {"Web": {"packages": {}}}}, "bundle name"),
            ({"version": 1, "bundles": {"w": {"packages": {}, "extra": 1}}}, "expected only"),
            ({"version": 1, "bundles": {"w": {"description": "a\nb", "reviewed": "2026-10-19", "packages": {"apt": {"required": ["x"]}}}}}, "one line"),
            ({"version": 1, "bundles": {"w": {"notes": ["a\nb"], "reviewed": "2026-10-19", "packages": {"apt": {"required": ["x"]}}}}}, "notes must be"),
            (bundle(), "packages must map"),
            ({"version": 1, "bundles": {"w": {"packages": {"apt": {"required": ["x"]}}}}}, "reviewed must be"),
            ({"version": 1, "bundles": {"w": {"reviewed": "last week", "packages": {"apt": {"required": ["x"]}}}}}, "reviewed must be"),
            ({"version": 1, "bundles": {"w": {"reviewed": "2026-13-40", "packages": {"apt": {"required": ["x"]}}}}}, "reviewed must be"),
            (bundle(rpm={"required": ["x"]}), "not a package manager"),
            (bundle(apt={"required": ["x"], "extra": []}), "only \"required\" and \"optional\""),
            (bundle(apt={"required": []}), "required is empty"),
            (bundle(apt={}), "must be a list"),
            (bundle(apt={"required": ["-bad"]}), "not a valid apt package name"),
            (bundle(apt={"required": ["x y"]}), "not a valid apt package name"),
            (bundle(apt={"required": ["x", "x"]}), "twice"),
            (bundle(apt={"required": ["x"], "optional": ["x"]}), "as required and optional"),
            (bundle(apt={"required": ["x"], "optional": "y"}), "must be a list"),
            (bundle(apt={"required": [f"p{i}" for i in range(pk.MAX_PACKAGES + 1)]}), "at most"),
            (bundle(apt={"required": [f"p{i}" for i in range(pk.MAX_PACKAGES)], "optional": ["more"]}), "more than"),
        ]
        for data, message in bad:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                package_bundles.validate_bundles(data)

    def test_names_are_checked_per_manager(self) -> None:
        # a scoped npm package is a package name for npm and not for apt
        package_bundles.validate_bundles(bundle(npm={"required": ["@scope/tool"]}))
        with self.assertRaisesRegex(ValueError, "not a valid apt package name"):
            package_bundles.validate_bundles(bundle(apt={"required": ["@scope/tool"]}))


class StaleTests(unittest.TestCase):
    """`reviewed` is the date a person last checked names and versions; an old one is a warning, not an error."""

    def catalog(self, **dates: str) -> dict:
        return {n: {"reviewed": d} for n, d in dates.items()}

    def test_a_bundle_older_than_the_limit_is_reported_oldest_first(self) -> None:
        today = datetime.date(2027, 6, 1)
        out = package_bundles.stale(self.catalog(a="2026-10-19", b="2026-12-01", c="2027-05-01"), today, 180)
        self.assertEqual([(o["name"], o["age_days"]) for o in out], [("a", 225), ("b", 182)])

    def test_the_limit_itself_is_not_stale(self) -> None:
        today = datetime.date(2027, 4, 17)
        self.assertEqual(package_bundles.stale(self.catalog(a="2026-10-19"), today, 180), [])

    def test_the_limit_comes_from_the_environment(self) -> None:
        with patch.dict(os.environ, {"GUARDIAN_BUNDLE_MAX_AGE_DAYS": "30"}):
            self.assertEqual(package_bundles.max_age_days(), 30)
        for bad in ("soon", "0", "-5"):
            with patch.dict(os.environ, {"GUARDIAN_BUNDLE_MAX_AGE_DAYS": bad}):
                self.assertEqual(package_bundles.max_age_days(), 1 if bad != "soon" else package_bundles.DEFAULT_MAX_AGE_DAYS)

    def test_a_stale_bundle_still_expands_and_the_report_lists_it(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            data = bundle(apt={"required": ["htop"]})
            data["bundles"]["demo"]["reviewed"] = "2020-01-01"
            path.write_text(json.dumps(data))
            code, out = package_bundles.report(path)
            self.assertEqual(code, 0)
            self.assertEqual([s["name"] for s in out["stale"]], ["demo"])
            self.assertEqual(package_bundles.expand("demo", "apt", catalog=package_bundles.load_bundles(path))["reviewed"],
                             "2020-01-01")

    def test_the_repository_bundles_carry_a_date(self) -> None:
        for name, entry in CATALOG.items():
            datetime.date.fromisoformat(entry["reviewed"])
            self.assertEqual(package_bundles.expand(name, next(iter(entry["packages"])), catalog=CATALOG)["reviewed"],
                             entry["reviewed"])


class ExpandTests(unittest.TestCase):
    def test_the_default_is_the_required_packages_only(self) -> None:
        out = package_bundles.expand("nginx", "apt", catalog=CATALOG)
        self.assertEqual(out["install"], ["nginx"])
        self.assertEqual(out["optional_available"], ["certbot", "python3-certbot-nginx"])
        self.assertTrue(out["notes"])

    def test_optional_packages_are_asked_for_by_name(self) -> None:
        out = package_bundles.expand("nginx", "apt", optional=["certbot"], catalog=CATALOG)
        self.assertEqual(out["install"], ["nginx", "certbot"])
        self.assertEqual(out["optional_available"], ["python3-certbot-nginx"])

    def test_what_the_host_has_is_left_out_and_reported(self) -> None:
        out = package_bundles.expand("postgresql", "dnf", optional=["postgresql-contrib"], installed=["postgresql-server"],
                                     catalog=CATALOG)
        self.assertEqual(out["install"], ["postgresql-contrib"])
        self.assertEqual(out["already_installed"], ["postgresql-server"])

    def test_a_required_package_can_be_skipped_but_is_reported(self) -> None:
        out = package_bundles.expand("postgresql", "dnf", skip=["postgresql-server"], catalog=CATALOG)
        self.assertEqual(out["install"], [])
        self.assertEqual(out["skipped_required"], ["postgresql-server"])

    def test_unknown_names_are_refused(self) -> None:
        cases = [
            (("mysql", "apt"), {}, "unknown bundle"),
            (("nginx", "pacman"), {}, "no package list for pacman"),
            (("nginx", "apt"), {"optional": ["nginx"]}, "not an optional package"),
            (("nginx", "apt"), {"optional": ["htop"]}, "not an optional package"),
            (("nginx", "apt"), {"skip": ["certbot"]}, "not a required package"),
        ]
        for positional, kwargs, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                package_bundles.expand(*positional, catalog=CATALOG, **kwargs)

    def test_without_a_catalog_the_repository_file_is_used(self) -> None:
        self.assertEqual(package_bundles.expand("postgresql", "brew")["install"], ["postgresql@17"])


class BundleAndRoleTests(unittest.TestCase):
    def test_a_role_protects_what_its_bundle_installs(self) -> None:
        """Installing a bundle and then giving the host the matching role must protect the required packages."""
        roles = package_roles.load_roles()
        for bundle_name, role in (("nginx", "web"), ("postgresql", "database")):
            protection = package_roles.protection([role], None, catalog=roles)
            for manager, lists in CATALOG[bundle_name]["packages"].items():
                for name in lists["required"]:
                    with self.subTest(bundle=bundle_name, manager=manager, package=name):
                        self.assertIsNotNone(protection.source_of(manager, name))


class ValidatorTests(unittest.TestCase):
    """Every package an expanded bundle yields passes the same validator as a package typed by hand."""

    def setUp(self) -> None:
        self.addCleanup(lambda: os.path.exists(ops_facts.facts_dir() / "n1.json") and os.unlink(ops_facts.facts_dir() / "n1.json"))

    def client(self) -> MCPClient:
        return MCPClient(project=parse_project({**OPS, "policy": {"allowed_actions": [pk.INSTALL, pk.REMOVE]}}),
                         agent_client=FakeAgent([]))

    def test_every_bundle_installs_through_the_validator_on_its_managers(self) -> None:
        host_facts = {"apt": facts(), "dnf": FEDORA, "brew": MAC}
        for bundle_name, entry in CATALOG.items():
            for manager, lists in entry["packages"].items():
                if manager not in host_facts:
                    continue
                ops_facts.store_facts("n1", host_facts[manager])
                names = package_bundles.expand(bundle_name, manager, optional=lists["optional"], catalog=CATALOG)["install"]
                with self.subTest(bundle=bundle_name, manager=manager), _Inventory(None):
                    out = self.client().validate(pk.INSTALL, args(manager, *names))
                    self.assertEqual(out["manager"], manager)
                    self.assertEqual([p["name"] for p in out["packages"]], names)

    def test_a_protected_package_in_a_bundle_still_cannot_be_removed(self) -> None:
        ops_facts.store_facts("n1", facts())
        with _Inventory(["web"]), self.assertRaisesRegex(ActionValidationError, "role web"):
            self.client().validate(pk.REMOVE, args("apt", *package_bundles.expand("nginx", "apt", catalog=CATALOG)["install"]))


class EnvironmentTests(unittest.TestCase):
    def test_the_environment_variable_points_to_another_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            path.write_text(json.dumps(bundle(apt={"required": ["htop"]})))
            with patch.dict(os.environ, {"GUARDIAN_PACKAGE_BUNDLES": str(path)}):
                self.assertEqual(list(package_bundles.load_bundles()), ["demo"])


if __name__ == "__main__":
    unittest.main()

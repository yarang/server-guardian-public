import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config_ops
from persona_manager import PersonaManager, PersonaValidationError, validate_persona
from team_manager import TeamManager

HERE = Path(__file__).resolve().parent
PERSONA = {"id": "sre_critic", "name": "SRE Critic", "role": "Critic Agent", "system_prompt": "Find outage risks."}
TEAM = {"id": "sre", "name": "SRE", "expert": "domain_architect", "critic": [{"persona": "sre_critic"}],
        "judge": "jev_mcp_decision"}


class PersonaStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.custom = Path(self._tmp.name) / "personas"
        self.mgr = PersonaManager(custom_dir=self.custom)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_custom_overrides_builtin_and_delete_restores_it(self) -> None:
        builtin = self.mgr.get_persona("critic_redteam")
        self.mgr.save_persona({**builtin, "name": "Edited", "source": "builtin"})
        self.assertEqual(self.mgr.get_persona("critic_redteam")["name"], "Edited")
        listed = {p["id"]: p for p in self.mgr.list_personas()}
        self.assertEqual(listed["critic_redteam"]["source"], "custom")
        self.assertNotIn("source", json.loads((self.custom / "critic_redteam.json").read_text()))
        self.assertTrue(self.mgr.delete_persona("critic_redteam"))
        self.assertEqual(self.mgr.get_persona("critic_redteam")["name"], builtin["name"])
        self.assertFalse(self.mgr.delete_persona("critic_redteam"))

    def test_validation(self) -> None:
        with self.assertRaisesRegex(PersonaValidationError, "system_prompt: required"):
            validate_persona({**PERSONA, "system_prompt": " "})
        with self.assertRaisesRegex(PersonaValidationError, "id"):
            validate_persona({**PERSONA, "id": "../x"})
        with self.assertRaisesRegex(PersonaValidationError, "unknown tool"):
            validate_persona({**PERSONA, "mcp_capabilities": ["rm"]})
        self.assertNotIn("extra", validate_persona({**PERSONA, "extra": 1}))
        # A persona file written before 2026-10-23 may still carry a temperature: it loads and is dropped.
        self.assertNotIn("temperature", validate_persona({**PERSONA, "temperature": 5}))

    def test_shipped_personas_are_valid(self) -> None:
        for persona in PersonaManager(custom_dir=self.custom).list_personas():
            validate_persona(persona)


class ConfigOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.personas = PersonaManager(custom_dir=root / "personas")
        self.teams = TeamManager(root / "teams", persona_mgr=self.personas)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persona_and_team_round_trip(self) -> None:
        code, out = config_ops.save_team(self.teams, json.dumps(TEAM))
        self.assertEqual(code, 2)
        self.assertIn("unknown persona 'sre_critic'", out["error"])

        self.assertEqual(config_ops.save_persona(self.personas, json.dumps(PERSONA))[0], 0)
        code, out = config_ops.save_team(self.teams, json.dumps(TEAM))
        self.assertEqual((code, out["id"]), (0, "sre"))

        code, report = config_ops.teams_report(self.teams, ["claude"])
        entries = {t["id"]: t for t in report["teams"]}
        self.assertEqual(entries["sre"]["source"], "custom")
        saved = entries["sre"]["data"]
        self.assertEqual({k: v for k, v in saved.items() if k not in ("created", "modified")}, TEAM)
        self.assertEqual(saved["created"], saved["modified"])  # stamped by the server on save
        self.assertEqual(entries["cross-review"]["source"], "builtin")
        self.assertTrue(any("none of" in w for w in entries["cross-review"]["warnings"]))
        self.assertIn("jev_mcp:run_tests", [t["name"] for t in report["catalog"]["tools"]])
        self.assertEqual(report["catalog"]["installed"], ["claude"])
        self.assertEqual(report["catalog"]["host_labels"], ["worker:any", "worker:other"])

        self.assertEqual(config_ops.delete_team(self.teams, "sre")[0], 0)
        self.assertEqual(config_ops.delete_team(self.teams, "cross-review")[0], 2)
        self.assertEqual(config_ops.delete_persona(self.personas, "sre_critic")[0], 0)

    def test_catalog_offers_the_given_enabled_workers_as_hosts(self) -> None:
        _, report = config_ops.teams_report(self.teams, [], workers=["node-1", "node-2"])
        self.assertEqual(report["catalog"]["workers"], ["node-1", "node-2"])

    def test_report_shows_proposers_and_warns_when_none_is_installed(self) -> None:
        team = {"id": "drafts", "expert": {"persona": "domain_architect", "providers": ["claude"]},
                "proposers": [{"persona": "devops_specialist", "providers": ["codex"]}],
                "critic": "critic_redteam", "judge": "jev_mcp_decision"}
        self.assertEqual(config_ops.save_team(self.teams, json.dumps(team))[0], 0)
        _, report = config_ops.teams_report(self.teams, ["claude"])
        entry = next(t for t in report["teams"] if t["id"] == "drafts")
        self.assertEqual(entry["lineup"]["proposers"][0]["persona"], "devops_specialist")
        self.assertTrue(any("devops_specialist" in w and "installed" in w for w in entry["warnings"]))
        self.assertEqual(report["catalog"]["max_proposers"], 2)
        plain = next(t for t in report["teams"] if t["id"] == "cross-review")
        self.assertNotIn("proposers", plain["lineup"])

    def test_category_is_validated_and_timestamps_are_stamped_by_the_server(self) -> None:
        team = {"id": "ops1", "category": "operations", "created": "2001-01-01T00:00:00+00:00",
                "expert": "domain_architect", "critic": "critic_redteam", "judge": "jev_mcp_decision"}
        self.assertEqual(config_ops.save_team(self.teams, json.dumps(team))[0], 0)
        first = self.teams.get_team("ops1")
        self.assertEqual((first["category"], first["created"]), ("operations", "2001-01-01T00:00:00+00:00"))
        self.assertNotEqual(first["modified"], first["created"])

        # Editing keeps the stored "created" even if the client sends another one or none.
        edited = {k: v for k, v in team.items() if k != "created"}
        self.assertEqual(config_ops.save_team(self.teams, json.dumps({**edited, "name": "Ops one"}))[0], 0)
        self.assertEqual(self.teams.get_team("ops1")["created"], "2001-01-01T00:00:00+00:00")

        code, out = config_ops.save_team(self.teams, json.dumps({**team, "category": "fun"}))
        self.assertEqual(code, config_ops.INVALID)
        self.assertIn("category", out["error"])
        code, out = config_ops.save_team(self.teams, json.dumps({**team, "id": "ops2", "created": "yesterday"}))
        self.assertEqual(code, config_ops.INVALID)
        self.assertIn("created", out["error"])

        _, report = config_ops.teams_report(self.teams, [])
        self.assertIn("development", report["catalog"]["categories"])

    def test_copy_of_a_builtin_team_keeps_its_created_date(self) -> None:
        builtin = self.teams.get_team("cross-review")
        self.assertTrue(builtin["created"] and builtin["category"])
        self.assertEqual(config_ops.save_team(self.teams, json.dumps({**builtin, "created": None}))[0], 0)
        self.assertEqual(self.teams.get_team("cross-review")["created"], builtin["created"])

    def test_bad_category_in_a_stored_team_is_a_warning(self) -> None:
        self.teams.directory.mkdir(parents=True)
        (self.teams.directory / "odd.json").write_text(json.dumps(
            {"id": "odd", "category": "fun", "expert": "domain_architect", "critic": "critic_redteam", "judge": "jev_mcp_decision"}))
        _, report = config_ops.teams_report(self.teams, [])
        odd = next(t for t in report["teams"] if t["id"] == "odd")
        self.assertEqual(odd["errors"], [])
        self.assertTrue(any("category" in w for w in odd["warnings"]))

    def test_shipped_teams_have_a_category_and_a_creation_date(self) -> None:
        _, report = config_ops.teams_report(self.teams, [])
        for entry in report["teams"]:
            if entry["source"] == "builtin":
                self.assertIn(entry["data"].get("category"), report["catalog"]["categories"], entry["id"])
                self.assertTrue(entry["data"].get("created"), entry["id"])
                self.assertEqual(entry["errors"], [])

    def test_delete_distinguishes_unknown_from_builtin(self) -> None:
        code, out = config_ops.delete_team(self.teams, "no-such-team")
        self.assertEqual((code, out["ok"]), (config_ops.NOT_FOUND, False))
        self.assertIn("no team", out["error"])
        code, out = config_ops.delete_team(self.teams, "cross-review")
        self.assertEqual(code, config_ops.INVALID)
        self.assertIn("builtin", out["error"])
        self.assertEqual(config_ops.delete_persona(self.personas, "no_such_persona")[0], config_ops.NOT_FOUND)
        self.assertEqual(config_ops.delete_persona(self.personas, "critic_redteam")[0], config_ops.INVALID)
        # Deleted once, a custom id is unknown the second time.
        config_ops.save_persona(self.personas, json.dumps(PERSONA))
        self.assertEqual(config_ops.delete_persona(self.personas, "sre_critic")[0], 0)
        self.assertEqual(config_ops.delete_persona(self.personas, "sre_critic")[0], config_ops.NOT_FOUND)

    def test_bad_input(self) -> None:
        self.assertIn("invalid JSON", config_ops.save_team(self.teams, "{")[1]["error"])
        self.assertIn("larger than", config_ops.save_persona(self.personas, " " * 70000)[1]["error"])
        self.assertEqual(config_ops.save_persona(self.personas, "[]")[0], 2)

    def test_invalid_stored_team_is_reported_not_raised(self) -> None:
        self.teams.directory.mkdir(parents=True)
        (self.teams.directory / "broken.json").write_text(json.dumps({"id": "broken", "expert": "x"}))
        _, report = config_ops.teams_report(self.teams, [])
        broken = next(t for t in report["teams"] if t["id"] == "broken")
        self.assertIsNone(broken["lineup"])
        self.assertTrue(broken["errors"])


class CommandSetConfigTests(unittest.TestCase):
    """config_ops wrappers for the command set store (exit codes the hub maps to HTTP)."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        (Path(self._tmp.name) / "builtin").mkdir()
        self._env = patch.dict(os.environ, {"GUARDIAN_DATA_DIR": self._tmp.name,
                                            "GUARDIAN_COMMAND_SETS": str(Path(self._tmp.name) / "builtin")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    SET = {"id": "list-tmp", "description": "List /var/tmp", "os": ["linux"], "script": "ls -l /var/tmp",
           "risk": "low", "reversible": True, "undo": "read only"}

    def test_save_then_delete(self) -> None:
        code, payload = config_ops.save_command_set(json.dumps(self.SET))
        self.assertEqual((code, payload["ok"], payload["id"]), (0, True, "list-tmp"))
        self.assertEqual(config_ops.delete_command_set("list-tmp"), (0, {"ok": True, "id": "list-tmp"}))
        self.assertEqual(config_ops.delete_command_set("list-tmp")[0], 3)

    def test_confirm_exit_codes(self) -> None:
        config_ops.save_command_set(json.dumps(self.SET))
        self.assertEqual(config_ops.confirm_command_set("list-tmp"), (0, {"ok": True, "id": "list-tmp"}))
        self.assertEqual(config_ops.confirm_command_set("nope")[0], 3)
        (Path(self._tmp.name) / "builtin" / "ls-b.json").write_text(json.dumps({**self.SET, "id": "ls-b", "reviewed": "2026-10-01"}))
        self.assertEqual(config_ops.confirm_command_set("ls-b")[0], 2)

    def test_invalid_input_is_exit_2(self) -> None:
        self.assertEqual(config_ops.save_command_set("not json")[0], 2)
        self.assertEqual(config_ops.save_command_set(json.dumps({**self.SET, "script": "if then fi"}))[0], 2)
        self.assertEqual(config_ops.save_command_set(json.dumps([1]))[0], 2)

    def test_a_builtin_set_cannot_be_deleted(self) -> None:
        (Path(self._tmp.name) / "builtin" / "list-tmp.json").write_text(json.dumps({**self.SET, "reviewed": "2026-10-01"}))
        code, payload = config_ops.delete_command_set("list-tmp")
        self.assertEqual(code, 2)
        self.assertIn("builtin", payload["error"])

    def test_the_cli_saves_lists_and_deletes(self) -> None:
        run = lambda *a, stdin="": subprocess.run([sys.executable, str(HERE / "guardian.py"), *a], input=stdin,
                                                  env=dict(os.environ), capture_output=True, text=True, timeout=60)
        self.assertEqual(run("--command-set-save", "--json", stdin=json.dumps(self.SET)).returncode, 0)
        listed = json.loads(run("--command-sets", "--json").stdout)
        self.assertEqual([s["id"] for s in listed["sets"]], ["list-tmp"])
        self.assertEqual(run("--command-set-delete", "list-tmp", "--json").returncode, 0)
        self.assertEqual(run("--command-set-delete", "list-tmp", "--json").returncode, 3)


class GuardianCliContractTests(unittest.TestCase):
    """Runs guardian.py the way the hub API does: JSON on stdout, exit 2 on validation errors."""

    def _run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = {**os.environ, "GUARDIAN_DATA_DIR": self._tmp.name}
        return subprocess.run([sys.executable, str(HERE / "guardian.py"), *args], input=stdin, env=env,
                              capture_output=True, text=True, timeout=60)

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_list_delete(self) -> None:
        bad = self._run("--team-save", "--json", stdin=json.dumps(TEAM))
        self.assertEqual(bad.returncode, 2)
        self.assertFalse(json.loads(bad.stdout)["ok"])

        self.assertEqual(self._run("--persona-save", "--json", stdin=json.dumps(PERSONA)).returncode, 0)
        self.assertEqual(self._run("--team-save", "--json", stdin=json.dumps(TEAM)).returncode, 0)
        self.assertTrue((Path(self._tmp.name) / "teams" / "sre.json").exists())

        teams = json.loads(self._run("--teams", "--json").stdout)
        self.assertIn("sre", [t["id"] for t in teams["teams"]])
        personas = json.loads(self._run("--personas", "--json").stdout)
        self.assertIn("sre_critic", [p["id"] for p in personas["personas"]])

        self.assertEqual(self._run("--team-delete", "sre", "--json").returncode, 0)
        self.assertEqual(self._run("--team-delete", "default", "--json").returncode, 2)
        self.assertEqual(self._run("--team-delete", "no-such-team", "--json").returncode, 3)
        self.assertEqual(self._run("--persona-delete", "no_such_persona", "--json").returncode, 3)
        self.assertEqual(self._run("--project-delete", "no-such-project", "--json").returncode, 3)


if __name__ == "__main__":
    unittest.main()

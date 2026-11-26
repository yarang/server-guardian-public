"""The Guardian skills for the LLM CLIs (deploy/llm-skills) stay in step with the engine they describe."""
import json
import re
import unittest
from pathlib import Path

import ops_paths

ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS = ROOT / "deploy" / "llm-skills"
GUARDIAN = (Path(__file__).resolve().parent / "guardian.py").read_text(encoding="utf-8")


def skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


class CleanupDenyListTests(unittest.TestCase):
    """The local cleanup skill refuses at least what ops_paths refuses for Guardian's own actions."""

    def test_every_protected_tree_system_dir_and_name_is_listed(self) -> None:
        text = skill("guardian-host-cleanup")
        listed = set(re.findall(r"`([^`]+)`", text))
        for group in (ops_paths.PROTECTED_TREES, ops_paths.SYSTEM_DIRS, ops_paths.PROTECTED_NAMES):
            for entry in sorted(group):
                with self.subTest(entry=entry):
                    self.assertIn(entry, listed)

    def test_the_system_dirs_are_named_as_never_removed_themselves(self) -> None:
        text = skill("guardian-host-cleanup")
        rule = text[text.index("Never remove or empty these directories themselves"):]
        rule = rule[:rule.index("\n- ")] if "\n- " in rule else rule
        for entry in sorted(ops_paths.SYSTEM_DIRS):
            with self.subTest(entry=entry):
                self.assertIn(f"`{entry}`", rule)


class CommandTests(unittest.TestCase):
    def test_every_guardian_flag_a_skill_names_exists(self) -> None:
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "guardian.py" not in line:
                    continue
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", line.split("guardian.py", 1)[1]):
                    with self.subTest(skill=path.parent.name, flag=flag):
                        self.assertIn(f'"{flag}"', GUARDIAN)

    def test_the_fleet_skill_names_every_operations_team(self) -> None:
        text = skill("guardian-fleet-ops")
        for path in sorted((ROOT / "configs" / "teams").glob("*.json")):
            team = json.loads(path.read_text(encoding="utf-8"))
            if team.get("category") == "operations":
                with self.subTest(team=team["id"]):
                    self.assertIn(f"`{team['id']}`", text)
        for flag in ("--plans", "--plan-show", "--plan-run", "--plan-reject"):
            self.assertIn(flag, text)

    def test_the_node_skills_check_the_system_first(self) -> None:
        for name in ("guardian-host-check", "guardian-host-cleanup"):
            with self.subTest(skill=name):
                self.assertIn("/etc/os-release", skill(name))

    def test_the_manifest_lists_every_skill(self) -> None:
        manifest = json.loads((SKILLS / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest), sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md")))


if __name__ == "__main__":
    unittest.main()

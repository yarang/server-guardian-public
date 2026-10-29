"""Typed cleanup actions of ops projects (docs/design/server-ops-project.md, chapter 6, P3)."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List
from unittest.mock import patch

import action_ops
import ops_actions
from db_manager import DecisionDBManager
from mcp_client import ActionValidationError, MCPClient
from ops_actions import drift, measure_bytes, parse_size
from project_registry import parse_project
from test_ops_project import OPS, _FixedTargets
from testenv import isolate_data_dir, restore_data_dir

GiB = 1024 ** 3
MiB = 1024 ** 2
JOURNAL = "Archived and active journals take up {} in the file system."


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class FakeAgent:
    """Answers measure commands from a queue of outputs and records every command with its labels."""

    def __init__(self, measures: List[str], run_exit: int = 0):
        self.measures = list(measures)
        self.run_exit = run_exit
        self.calls: List[Dict[str, Any]] = []

    def execute_remote_ssh(self, target: str, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        self.calls.append({"target": target, "command": command, "labels": labels})
        if labels and labels["op"].endswith(":preview"):
            return {"exit_code": 0, "stdout": self.measures.pop(0) if self.measures else "", "stderr": ""}
        return {"exit_code": self.run_exit, "stdout": "Deleted", "stderr": ""}

    def execute_command(self, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        return self.execute_remote_ssh("local", command, timeout_sec, labels)

    def runs(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if not c["labels"]["op"].endswith(":preview")]


def client(agent: Any) -> MCPClient:
    return MCPClient(project=parse_project(OPS), agent_client=agent)


class CatalogTests(unittest.TestCase):
    def test_arguments_are_validated_and_normalized(self) -> None:
        c = client(FakeAgent([]))
        with _FixedTargets():
            self.assertEqual(c.validate("ops:journal_vacuum", {"target_host": "n1", "max_size": "500m"})["max_size"], "500M")
            self.assertEqual(c.validate("ops:docker_prune", {"target_host": "n2", "scope": "build_cache"})["target_host"], "n2")
            bad = [
                ("ops:docker_prune", {"target_host": "n1", "scope": "all"}),
                ("ops:docker_prune", {"target_host": "n1"}),
                ("ops:journal_vacuum", {"target_host": "n1", "max_size": "50M"}),
                ("ops:journal_vacuum", {"target_host": "n1", "max_size": "1T"}),
                ("ops:journal_vacuum", {"target_host": "n1", "max_size": "500M; reboot"}),
                ("ops:apt_clean", {"target_host": "n3"}),
                ("ops:apt_clean", {"target_host": "hub1"}),
                ("ops:apt_clean", {}),
                ("ops:apt_clean", {"target_host": "n1", "extra": "x"}),
            ]
            for tool, args in bad:
                with self.subTest(tool=tool, args=args), self.assertRaises(ActionValidationError):
                    c.validate(tool, args)

    def test_commands_are_fixed(self) -> None:
        self.assertTrue(ops_actions.run_command("ops:docker_prune", {"scope": "dangling_images"}, {}).endswith("$D image prune -f"))
        self.assertTrue(ops_actions.run_command("ops:docker_prune", {"scope": "build_cache"}, {}).endswith("$D builder prune -f"))
        self.assertEqual(ops_actions.run_command("ops:journal_vacuum", {"max_size": "2G"}, {}), "sudo -n journalctl --vacuum-size=2G")
        self.assertEqual(ops_actions.run_command("ops:apt_clean", {}, {}), "sudo -n apt-get clean")

    def test_sizes_and_measurements(self) -> None:
        self.assertEqual(parse_size("12.5MB"), 12_500_000)
        self.assertEqual(parse_size("1.2G"), int(1.2 * GiB))
        self.assertEqual(parse_size("0B"), 0)
        self.assertEqual(parse_size("3.4kB"), 3400)
        self.assertIsNone(parse_size("nothing"))
        dangling = {"scope": "dangling_images"}
        self.assertEqual(measure_bytes("ops:docker_prune", dangling, "a1 100MB\nb2 1.5GB\n"), 1_600_000_000)
        self.assertEqual(measure_bytes("ops:docker_prune", dangling, ""), 0)
        self.assertIsNone(measure_bytes("ops:docker_prune", dangling, "permission denied while trying to connect"))
        cache = {"scope": "build_cache"}
        self.assertEqual(measure_bytes("ops:docker_prune", cache, "Images|1GB (50%)\nBuild Cache|2.5GB\n"), 2_500_000_000)
        self.assertIsNone(measure_bytes("ops:docker_prune", cache, "Cannot connect"))
        journal = {"max_size": "500M"}
        self.assertEqual(measure_bytes("ops:journal_vacuum", journal, JOURNAL.format("1.5G")), int(1.5 * GiB) - 500 * MiB)
        self.assertEqual(measure_bytes("ops:journal_vacuum", journal, JOURNAL.format("200.0M")), 0)
        self.assertEqual(measure_bytes("ops:apt_clean", {}, "204800\t/var/cache/apt/archives"), 204800 * 1024)
        self.assertIsNone(measure_bytes("ops:apt_clean", {}, "du: cannot access"))
        # Seen on a fleet node: du warns about the root-only partial/ directory before printing the total.
        warned = "du: cannot read directory '/var/cache/apt/archives/partial': Permission denied\n481656\t/var/cache/apt/archives\n"
        self.assertEqual(measure_bytes("ops:apt_clean", {}, warned), 481656 * 1024)

    def test_a_host_without_docker_is_an_error_not_an_unknown_size(self) -> None:
        agent = FakeAgent(["docker is not installed on this host\n"])
        preview = ops_actions.measure(agent, "n1", "ops:docker_prune", {"scope": "dangling_images"}, "ops1")
        self.assertEqual((preview["bytes"], preview["error"]), (None, "docker is not installed on this host"))
        self.assertIn("command -v docker", ops_actions.measure_command("ops:docker_prune", {"scope": "build_cache"}))
        self.assertIn("command -v docker", ops_actions.run_command("ops:docker_prune", {"scope": "build_cache"}, {}))

    def test_drift_needs_half_again_and_a_floor(self) -> None:
        self.assertIsNone(drift({"bytes": GiB}, {"bytes": int(1.5 * GiB)}))
        self.assertIn("would now delete", drift({"bytes": GiB}, {"bytes": 2 * GiB}))
        self.assertIsNone(drift({"bytes": 0}, {"bytes": 200 * MiB}))  # small growth stays under the floor
        self.assertIsNotNone(drift({"bytes": 0}, {"bytes": 300 * MiB}))
        self.assertIsNone(drift(None, {"bytes": 10 * GiB}))
        self.assertIsNone(drift({"bytes": None, "error": "x"}, {"bytes": GiB}))

    def test_execute_measures_runs_and_verifies(self) -> None:
        agent = FakeAgent(["204800\t/var/cache/apt/archives", "8\t/var/cache/apt/archives"])
        result = ops_actions.execute(agent, "n1", "ops:apt_clean", {"target_host": "n1"}, "ops1")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["verify"], {"before": 204800 * 1024, "after": 8192, "freed": 204800 * 1024 - 8192, "ok": True})
        self.assertEqual([c["labels"]["op"] for c in agent.calls], ["apt_clean:preview", "apt_clean", "apt_clean:preview"])
        self.assertEqual({c["labels"]["source"] for c in agent.calls}, {"action"})
        failed = ops_actions.execute(FakeAgent(["1\t/var/cache/apt/archives", "1\t/var/cache/apt/archives"], run_exit=1), "n1", "ops:apt_clean", {}, "ops1")
        self.assertEqual((failed["status"], failed["verify"]["ok"]), ("FAILED", False))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = DecisionDBManager(db_path=Path(self._tmp.name) / "d.db")
        self._targets = _FixedTargets().__enter__()

    def tearDown(self) -> None:
        self._targets.__exit__(None, None, None)
        self._tmp.cleanup()

    def propose(self, agent: FakeAgent, tool: str = "ops:apt_clean", **args: Any) -> int:
        stored = action_ops.record_proposals(self.db, client(agent), "t1", 1, [
            {"tool": tool, "arguments": {"target_host": "n1", **args}, "reason": "clean"}])
        return stored[0]["id"]

    def run_now(self, agent: FakeAgent, action_id: int) -> Dict[str, Any]:
        return action_ops.run_action(self.db, client(agent), action_id, confirm=lambda _a: True, actor="tester")

    def test_a_proposal_stores_its_preview_and_a_run_records_the_verification(self) -> None:
        action_id = self.propose(FakeAgent(["204800\t/var/cache/apt/archives"]))
        action = self.db.get_action(action_id)
        self.assertEqual((action["status"], action["preview"]["bytes"]), ("PROPOSED", 204800 * 1024))
        self.assertIn("Preview: would free 200.0 MiB when proposed", action_ops.describe_action(action))

        agent = FakeAgent(["210000\t/var/cache/apt/archives", "210000\t/var/cache/apt/archives", "4\t/var/cache/apt/archives"])
        self.run_now(agent, action_id)
        action = self.db.get_action(action_id)
        self.assertEqual(action["status"], "EXECUTED")
        self.assertEqual(action["result"]["verify"]["after"], 4096)
        self.assertEqual(len(agent.runs()), 1)

    def test_a_target_that_grew_too_much_makes_the_action_stale_without_running_it(self) -> None:
        action_id = self.propose(FakeAgent(["1024\t/var/cache/apt/archives"]))  # 1 MiB when proposed
        agent = FakeAgent([f"{2 * 1024 * 1024}\t/var/cache/apt/archives"])  # 2 GiB now
        with self.assertRaisesRegex(RuntimeError, "is stale: it would now delete 2.0 GiB"):
            self.run_now(agent, action_id)
        action = self.db.get_action(action_id)
        self.assertEqual(action["status"], "STALE")
        self.assertEqual(action["result"]["preview_now"]["bytes"], 2 * GiB)
        self.assertEqual(agent.runs(), [])

    def test_a_failed_measurement_keeps_the_action_proposed(self) -> None:
        action_id = self.propose(FakeAgent(["1024\t/var/cache/apt/archives"]))
        agent = FakeAgent([])
        agent.execute_remote_ssh = lambda *a, **k: {"exit_code": 255, "stdout": "", "stderr": "timeout"}
        with self.assertRaisesRegex(RuntimeError, "cannot measure before running: host not reached"):
            self.run_now(agent, action_id)
        self.assertEqual(self.db.get_action(action_id)["status"], "PROPOSED")

    def test_one_action_per_host_at_a_time(self) -> None:
        action_id = self.propose(FakeAgent(["1024\t/var/cache/apt/archives"]))
        with action_ops.host_lock("n1"):
            with self.assertRaisesRegex(RuntimeError, "another action is running on n1"):
                self.run_now(FakeAgent(["1024\t/var/cache/apt/archives", "1024\t/var/cache/apt/archives", "0\t/var/cache/apt/archives"]), action_id)
        self.assertEqual(self.db.get_action(action_id)["status"], "PROPOSED")
        with action_ops.host_lock("n2"):  # another host does not block it
            self.run_now(FakeAgent(["1024\t/var/cache/apt/archives", "1024\t/var/cache/apt/archives", "0\t/var/cache/apt/archives"]), action_id)
        self.assertEqual(self.db.get_action(action_id)["status"], "EXECUTED")
        with self.assertRaises(RuntimeError):
            with action_ops.host_lock("../etc"):
                pass

    def test_server_remediation_takes_the_host_lock_too(self) -> None:
        stored = action_ops.record_proposals(self.db, client(FakeAgent([])), "t1", 1, [
            {"tool": "jev_mcp:server_remediation", "reason": "x",
             "arguments": {"command": "uptime", "risk_level": "low", "target_host": "n2"}}])
        self.assertIsNone(self.db.get_action(stored[0]["id"])["preview"])
        with action_ops.host_lock("n2"), self.assertRaisesRegex(RuntimeError, "another action is running on n2"):
            self.run_now(FakeAgent([]), stored[0]["id"])


KERNELS = """running 6.8.0-1015-oracle
linux-image-6.8.0-1010-oracle
linux-modules-6.8.0-1010-oracle
linux-headers-6.8.0-1010
linux-headers-6.8.0-1010-oracle
linux-image-6.8.0-1015-oracle
linux-headers-6.8.0-1015
linux-image-6.8.0-1020-oracle
linux-modules-extra-6.8.0-1020-oracle
linux-image-oracle
"""


class MediumActionTests(unittest.TestCase):
    def test_validation_of_the_medium_actions(self) -> None:
        v = ops_actions.validate
        self.assertEqual(v("ops:remove_paths", {"paths": ["/tmp/a", "/tmp/a", "/opt/app/logs"]})["paths"],
                         ["/tmp/a", "/opt/app/logs"])
        self.assertEqual(v("ops:restart_service", {"unit": "nginx"})["unit"], "nginx.service")
        self.assertEqual(v("ops:truncate_logs", {"paths": ["/var/log/syslog.1"]})["paths"], ["/var/log/syslog.1"])
        bad = [
            ("ops:remove_paths", {"paths": []}),
            ("ops:remove_paths", {"paths": ["/tmp/x"] * 0 + [f"/tmp/{i}" for i in range(51)]}),
            ("ops:remove_paths", {"paths": ["/etc/nginx"]}),
            ("ops:remove_paths", {"paths": ["/tmp/ok", "/home/u/.ssh"]}),
            ("ops:remove_paths", {"paths": "/tmp/x"}),
            ("ops:remove_paths", {"paths": ["/tmp/x"], "older_than_days": True}),
            ("ops:remove_paths", {"paths": ["/tmp/x"], "older_than_days": 0}),
            ("ops:truncate_logs", {"paths": ["/var/log"]}),
            ("ops:truncate_logs", {"paths": ["/var/lib/x.log"]}),
            ("ops:truncate_logs", {"paths": ["/var/logs/x"]}),
            ("ops:restart_service", {"unit": "sshd"}),
            ("ops:restart_service", {"unit": "ssh.service"}),
            ("ops:restart_service", {"unit": "tailscaled"}),
            ("ops:restart_service", {"unit": "headscale"}),
            ("ops:restart_service", {"unit": "systemd-journald"}),
            ("ops:restart_service", {"unit": "server-guardian-agent"}),
            ("ops:restart_service", {"unit": "nginx.socket"}),
            ("ops:restart_service", {"unit": "x; reboot"}),
            ("ops:restart_service", {"unit": "-x"}),
            ("ops:docker_prune", {"scope": "volumes"}),
        ]
        for tool, args in bad:
            with self.subTest(tool=tool, args=args), self.assertRaises(ValueError):
                v(tool, args)
        self.assertRaisesRegex(ValueError, "project policy", v, "ops:remove_paths", {"paths": ["/srv/data/x"]},
                               ["/srv/data/*"])

    def test_risk_per_action(self) -> None:
        self.assertEqual(ops_actions.risk("ops:docker_prune", {"scope": "build_cache"}), "low")
        self.assertEqual(ops_actions.risk("ops:docker_prune", {"scope": "unused_images"}), "medium")
        self.assertEqual(ops_actions.risk("ops:apt_clean"), "low")
        self.assertEqual(ops_actions.risk("ops:remove_paths"), "medium")
        view = action_ops.action_view({"tool": "ops:docker_prune", "arguments": {"scope": "stopped_containers"}}, None)
        self.assertEqual(view["risk"], "medium")

    def test_commands(self) -> None:
        rm = ops_actions.run_command("ops:remove_paths", {"paths": ["/tmp/a b"]} and {"paths": ["/tmp/a"]}, {})
        self.assertIn("mountpoint -q", rm)
        self.assertTrue(rm.endswith("$S rm -rf --one-file-system -- /tmp/a"))
        old = ops_actions.run_command("ops:remove_paths", {"paths": ["/tmp/a"], "older_than_days": 30}, {})
        self.assertIn("find \"$p\" -xdev -type f -mtime +30 -delete", old)
        self.assertNotIn("rm -rf", old)
        self.assertIn("$S truncate -s 0", ops_actions.run_command("ops:truncate_logs", {"paths": ["/var/log/x.1"]}, {}))
        self.assertEqual(ops_actions.run_command("ops:restart_service", {"unit": "nginx.service"}, {}),
                         "sudo -n systemctl restart -- nginx.service")
        purge = ops_actions.run_command("ops:remove_old_kernels", {}, {"items": ["linux-image-6.8.0-1010-oracle", "evil;rm"]})
        self.assertEqual(purge, "sudo -n apt-get purge -y -- linux-image-6.8.0-1010-oracle")
        self.assertIn("no old kernel", ops_actions.run_command("ops:remove_old_kernels", {}, {"items": []}))
        self.assertIn("container prune -f", ops_actions.run_command("ops:docker_prune", {"scope": "stopped_containers"}, {}))
        self.assertIn("image prune -a -f", ops_actions.run_command("ops:docker_prune", {"scope": "unused_images"}, {}))

    def test_measurements_of_the_medium_actions(self) -> None:
        pm = ops_actions.parse_measure
        out = "1048576\t12\t1700000000\t/tmp/a\nmissing\t/tmp/b\n"
        self.assertEqual(pm("ops:remove_paths", {"paths": ["/tmp/a", "/tmp/b"]}, out), (1048576, ["/tmp/a"]))
        self.assertEqual(pm("ops:truncate_logs", {}, "2048\t/var/log/x.1\nskip\t/var/log/dir\n"), (2048, ["/var/log/x.1"]))
        self.assertEqual(pm("ops:docker_prune", {"scope": "stopped_containers"},
                            "abc 0B (virtual 77.8MB) web\ndef 1.5kB (virtual 1GB) job\n"), (1500, ["abc", "def"]))
        self.assertEqual(pm("ops:docker_prune", {"scope": "unused_images"}, "Images|3GB (60%)\nBuild Cache|0B"),
                         (3_000_000_000, None))
        self.assertEqual(ops_actions.old_kernel_packages(KERNELS), [
            "linux-headers-6.8.0-1010", "linux-headers-6.8.0-1010-oracle", "linux-image-6.8.0-1010-oracle",
            "linux-modules-6.8.0-1010-oracle"])
        self.assertIsNone(ops_actions.old_kernel_packages("dpkg-query: error"))
        self.assertEqual(ops_actions.old_kernel_packages("running 6.8.0-1\n"), [])

    def test_drift_on_items_the_proposal_did_not_show(self) -> None:
        self.assertIsNone(drift({"bytes": 10, "items": ["/tmp/a"]}, {"bytes": 10, "items": ["/tmp/a"]}))
        self.assertIsNone(drift({"items": ["a", "b"]}, {"items": ["a"]}))
        self.assertIn("would now also act on /tmp/b", drift({"bytes": 10, "items": ["/tmp/a"]},
                                                            {"bytes": 20, "items": ["/tmp/a", "/tmp/b"]}))
        self.assertIn("linux-image-6.8.0-1015", drift({"items": []}, {"items": ["linux-image-6.8.0-1015"]}))

    def test_restart_and_kernel_runs_are_verified(self) -> None:
        agent = FakeAgent(["active\nNRestarts=0", "active\nNRestarts=1"])
        result = ops_actions.execute(agent, "n1", "ops:restart_service", {"unit": "nginx.service"}, "ops1")
        self.assertEqual((result["verify"]["active"], result["verify"]["ok"]), ("active", True))
        agent = FakeAgent(["activating", "failed"])
        self.assertFalse(ops_actions.execute(agent, "n1", "ops:restart_service", {"unit": "x.service"}, "ops1")["verify"]["ok"])
        agent = FakeAgent([KERNELS, "running 6.8.0-1015-oracle\nlinux-image-6.8.0-1015-oracle\n"])
        result = ops_actions.execute(agent, "n1", "ops:remove_old_kernels", {}, "ops1")
        self.assertEqual(len(result["verify"]["removed"]), 4)
        self.assertEqual((result["verify"]["left"], result["verify"]["ok"]), ([], True))
        self.assertIn("apt-get purge -y -- linux-headers-6.8.0-1010", agent.runs()[0]["command"])

    def test_a_unit_the_host_does_not_have_is_a_measurement_error(self) -> None:
        agent = FakeAgent(["inactive\nNRestarts=0\nActiveEnterTimestamp=\nUnit cron.service could not be found.\n"])
        preview = ops_actions.measure(agent, "n1", "ops:restart_service", {"unit": "cron.service"}, "ops1")
        self.assertEqual(preview["error"], "cron.service does not exist on this host")

    def test_a_failed_measurement_stops_execute_before_the_run(self) -> None:
        agent = FakeAgent(["docker is not installed on this host"])
        result = ops_actions.execute(agent, "n1", "ops:docker_prune", {"scope": "unused_images"}, "ops1")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(agent.runs(), [])


class LocalRemediationTests(unittest.TestCase):
    def test_an_ops_project_runs_hub_commands_through_the_agent(self) -> None:
        agent = FakeAgent([])
        project = parse_project({**OPS, "policy": {"include_hub": True}})
        c = MCPClient(project=project, agent_client=agent)
        with _FixedTargets():
            result = c.call_tool("jev_mcp:server_remediation", {"command": "uptime", "risk_level": "low"})
        self.assertEqual((result["status"], result["target_host"]), ("SUCCESS", "local"))
        self.assertEqual(agent.calls, [{"target": "local", "command": "uptime",
                                        "labels": {"source": "action", "op": "server_remediation", "project": "ops1"}}])


AUTO = {**OPS, "policy": {"auto_run": ["ops:apt_clean", "ops:docker_prune"], "max_auto_runs_per_day": 2}}


class AutoRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = DecisionDBManager(db_path=Path(self._tmp.name) / "d.db")
        self._targets = _FixedTargets().__enter__()

    def tearDown(self) -> None:
        self._targets.__exit__(None, None, None)
        self._tmp.cleanup()

    def client(self, agent: FakeAgent, data: Dict[str, Any] = AUTO) -> MCPClient:
        return MCPClient(project=parse_project(data), agent_client=agent)

    def propose(self, c: MCPClient, *proposals: Dict[str, Any]) -> List[int]:
        stored = action_ops.record_proposals(self.db, c, "t1", 1, [
            {"reason": "clean", **p, "arguments": {"target_host": "n1", **p.get("arguments", {})}} for p in proposals])
        return [item["id"] for item in stored]

    def test_policy_validation(self) -> None:
        self.assertEqual(parse_project(AUTO).policy["max_auto_runs_per_day"], 2)
        bad = [
            {"auto_run": ["ops:remove_paths"], "max_auto_runs_per_day": 1},
            {"auto_run": ["jev_mcp:server_remediation"], "max_auto_runs_per_day": 1},
            {"auto_run": ["ops:apt_clean"]},
            {"auto_run": ["ops:apt_clean"], "max_auto_runs_per_day": 0},
            {"auto_run": ["ops:apt_clean"], "max_auto_runs_per_day": 101},
            {"auto_run": ["ops:apt_clean"], "max_auto_runs_per_day": True},
            {"auto_run": ["ops:apt_clean"], "max_auto_runs_per_day": 1, "allowed_actions": ["ops:journal_vacuum"]},
            {"auto_run": "ops:apt_clean", "max_auto_runs_per_day": 1},
        ]
        for policy in bad:
            with self.subTest(policy=policy), self.assertRaises(Exception):
                parse_project({**OPS, "policy": policy})

    def test_only_low_risk_actions_named_in_the_policy_run_by_themselves(self) -> None:
        apt = "204800\t/var/cache/apt/archives"
        agent = FakeAgent([apt, "1 1kB", apt, apt, "0\t/var/cache/apt/archives"])
        c = self.client(agent)
        ids = self.propose(c,
                           {"tool": "ops:apt_clean"},
                           {"tool": "ops:docker_prune", "arguments": {"scope": "stopped_containers"}},
                           {"tool": "ops:journal_vacuum", "arguments": {"max_size": "500M"}})
        outcomes = action_ops.auto_run_actions(self.db, c, ids)
        self.assertEqual([(o["tool"], o["status"]) for o in outcomes], [("ops:apt_clean", "EXECUTED")])
        statuses = [self.db.get_action(i) for i in ids]
        self.assertEqual([(a["status"], a["decided_by"]) for a in statuses],
                         [("EXECUTED", "auto:ops1"), ("PROPOSED", None), ("PROPOSED", None)])

    def test_the_daily_limit_and_the_switches(self) -> None:
        apt = "204800\t/var/cache/apt/archives"
        c = self.client(FakeAgent([apt] * 20))
        ids = self.propose(c, *({"tool": "ops:apt_clean"} for _ in range(3)))
        self.assertEqual(len(action_ops.auto_run_actions(self.db, c, ids)), 2)
        self.assertEqual(action_ops.auto_runs_today(self.db, "ops1"), 2)
        self.assertEqual(self.db.get_action(ids[2])["status"], "PROPOSED")
        more = self.propose(c, {"tool": "ops:apt_clean"})
        self.assertEqual(action_ops.auto_run_actions(self.db, c, more), [])
        # Without a real jev gate, or with the hub's switches off, nothing runs by itself.
        fresh = DecisionDBManager(db_path=Path(self._tmp.name) / "e.db")
        ids = action_ops.record_proposals(fresh, c, "t2", 1, [{"tool": "ops:apt_clean", "arguments": {"target_host": "n1"}}])
        self.assertEqual(action_ops.auto_run_actions(fresh, c, [ids[0]["id"]], ops=False), [])
        with patch.dict("os.environ", {"GUARDIAN_AUTO_APPLY": "off"}):
            self.assertEqual(action_ops.auto_run_actions(fresh, c, [ids[0]["id"]]), [])
        with patch.dict("os.environ", {"GUARDIAN_UI_ACTIONS": "off"}):
            self.assertEqual(action_ops.auto_run_actions(fresh, c, [ids[0]["id"]]), [])

    def test_a_project_without_auto_run_never_runs_by_itself(self) -> None:
        c = self.client(FakeAgent(["204800\t/var/cache/apt/archives"] * 4), OPS)
        ids = self.propose(c, {"tool": "ops:apt_clean"})
        self.assertEqual(action_ops.auto_run_actions(self.db, c, ids), [])


if __name__ == "__main__":
    unittest.main()

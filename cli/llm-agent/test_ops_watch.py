"""Nightly watch of ops projects (docs/design/server-ops-project.md, P6)."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

import ops_watch
from project_registry import ProjectManager, ProjectValidationError, parse_project
from test_ops_project import OPS, _FixedTargets
from testenv import isolate_data_dir, restore_data_dir

# disk_usage on site-a-ec1 (2026-10-16), with / raised to 91% and inode use of /boot raised to 88%.
DF = """Filesystem     Type     1024-blocks     Used Available Capacity Mounted on
/dev/sda1      ext4        46212176 41959296  4236496      91% /
efivarfs       efivarfs         256      235       21      92% /sys/firmware/efi/efivars
/dev/sda16     ext4          901520   184948    653444      23% /boot
/dev/sda15     vfat          106832     6250    100582       6% /boot/efi

# inodes
Filesystem      Inodes  IUsed   IFree IUse% Mounted on
/dev/sda1      5939200 237000 5702200    4% /
efivarfs             0      0       0     - /sys/firmware/efi/efivars
/dev/sda16       58496  51476    7020   88% /boot
"""
JOURNAL = "Archived and active journals take up 4.0G in the file system.\n"
# headscale_status on the control server (2026-10-18), shortened; the certificate end date and one node changed.
HEADSCALE = """service: active
version: v0.29.4
server_url: https://hs.example.test
certificate_not_after: Dec 25 12:15:28 2026 GMT
db_kib: 268
# backup timers
backup: headscale-backup.service success Sun 2026-10-18 00:11:26 UTC
# nodes (name, IPv4, tags or user, state, last seen)
nodes: 4, online: 2
node\tsite-y-ec2\t100.64.0.1\ttag:mesh-ctl\tonline\t2026-10-17T07:06Z
node\tsite-b-ec1\t100.64.0.19\ttag:fleet\toffline\t2026-10-17T22:00Z
node\tsite-b-ec2\t100.64.0.21\ttag:fleet\toffline\t2026-10-18T00:40Z
node\tlaptop\t100.64.0.26\towner\toffline\t2026-10-10T08:00Z
"""
# 2026-10-18 01:00 UTC
NOW = 1792285200
WATCHED = {**OPS, "policy": {"watch": {"disk_percent": 85, "inode_percent": 85, "journal_max": "3G"}}}


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


class FakeAgent:
    """disk_usage and journal_usage answers per host; start_debate records the task."""

    def __init__(self, df: Dict[str, str], journal: Dict[str, str], headscale: Dict[str, str] = None):
        self.df, self.journal, self.headscale = df, journal, headscale or {}
        self.debates: List[Dict[str, str]] = []

    def execute_remote_ssh(self, host: str, command: str, timeout_sec: int = 10, labels=None) -> Dict[str, Any]:
        if labels["op"] == "headscale_status":
            return {"exit_code": 0, "stdout": self.headscale.get(host, "headscale is not installed on this host\n"),
                    "stderr": ""} if host in self.df else {"exit_code": 255, "stdout": "", "stderr": "unreachable"}
        out = (self.df if labels["op"] == "disk_usage" else self.journal).get(host)
        return {"exit_code": 255, "stdout": "", "stderr": "unreachable"} if out is None else \
            {"exit_code": 0, "stdout": out, "stderr": ""}

    def start_debate(self, task: str, project: str) -> Dict[str, Any]:
        self.debates.append({"task": task, "project": project})
        return {"id": f"dbt_{len(self.debates)}", "status": "running"}


class ParseTests(unittest.TestCase):
    def test_watch_policy(self) -> None:
        self.assertIsNone(ops_watch.parse_watch(None))
        self.assertEqual(ops_watch.parse_watch({}), {"disk_percent": 85, "inode_percent": 85, "journal_max": "3G"})
        self.assertEqual(ops_watch.parse_watch({"journal_max": "500m"})["journal_max"], "500M")
        for bad in ([], {"disk_percent": 40}, {"disk_percent": 100}, {"inode_percent": True},
                    {"journal_max": "1T"}, {"journal_max": "3G;x"}, {"disk": 90}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ops_watch.parse_watch(bad)
        self.assertEqual(parse_project(WATCHED).policy["watch"]["journal_max"], "3G")
        with self.assertRaises(ProjectValidationError):
            parse_project({**OPS, "policy": {"watch": {"disk_percent": 10}}})

    def test_df_and_findings(self) -> None:
        space, inodes = ops_watch.parse_df(DF)
        self.assertEqual([(s["mount"], s["percent"]) for s in space],
                         [("/", 91), ("/sys/firmware/efi/efivars", 92), ("/boot", 23), ("/boot/efi", 6)])
        self.assertEqual([(i["mount"], i["percent"]) for i in inodes], [("/", 4), ("/boot", 88)])
        watch = ops_watch.parse_watch({})
        found = ops_watch.findings_for("n1", {"ok": True, "text": DF, "call": 1},
                                       {"ok": True, "text": JOURNAL, "call": 2}, watch)
        self.assertEqual([(f["kind"], f["mount"], f["value"]) for f in found],
                         [("disk", "/", "91%"), ("inodes", "/boot", "88%"), ("journal", "/var/log/journal", "4.0 GiB")])
        self.assertEqual(ops_watch.findings_for("n1", {"ok": False}, None, watch), [])
        self.assertTrue(ops_watch.parse_watch({"headscale": True})["headscale"])
        with self.assertRaises(ValueError):
            ops_watch.parse_watch({"headscale": "yes"})
        quiet = ops_watch.findings_for("n1", {"ok": True, "text": DF.replace("91%", "29%").replace("88%", "4%"), "call": 1},
                                       {"ok": True, "text": JOURNAL.replace("4.0G", "1.2G"), "call": 2}, watch)
        self.assertEqual(quiet, [])


class HeadscaleTests(unittest.TestCase):
    def found(self, text: str, now: int = NOW):
        return [(f["kind"], f["mount"], f["value"])
                for f in ops_watch.headscale_findings("hs", {"ok": True, "text": text, "call": 4}, now)]

    def test_a_healthy_server_has_only_long_offline_tagged_nodes(self) -> None:
        # site-b-ec2 went offline 20 minutes ago and the laptop is a person's device: neither is a finding.
        self.assertEqual(self.found(HEADSCALE), [("mesh_offline", "site-b-ec1", "offline since 2026-10-17T22:00Z")])

    def test_service_certificate_and_backup(self) -> None:
        text = (HEADSCALE.replace("service: active", "service: failed")
                .replace("Dec 25 12:15:28 2026", "Oct 30 12:00:00 2026")
                .replace("success Sun", "exit-code Sun"))
        self.assertEqual(self.found(text)[:3], [("headscale_service", "headscale.service", "failed"),
                                                ("headscale_cert", "tls", "12 days"),
                                                ("headscale_backup", "headscale-backup.service", "exit-code")])
        stale = self.found(HEADSCALE, now=NOW + 3 * 86400)
        self.assertIn(("headscale_backup", "headscale-backup.service",
                       "last run Sun 2026-10-18 00:11:26 UTC"), stale)
        self.assertEqual(self.found("certificate_not_after: unknown\nbackup: hs.service success \n")[0][2],
                         "last run never")

    def test_hosts_without_headscale_and_failed_checks_have_no_findings(self) -> None:
        self.assertEqual(self.found("headscale is not installed on this host\n"), [])
        self.assertEqual(ops_watch.headscale_findings("hs", {"ok": False}, NOW), [])


class WatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._targets = _FixedTargets().__enter__()
        self.project = parse_project(WATCHED)

    def tearDown(self) -> None:
        self._targets.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_one_debate_for_new_findings_then_quiet_for_a_week(self) -> None:
        agent = FakeAgent({"n1": DF, "n2": DF.replace("91%", "40%").replace("88%", "4%")},
                          {"n1": JOURNAL, "n2": JOURNAL.replace("4.0G", "1.0G")})
        report = ops_watch.watch_project(self.project, agent, self.data, now=1_000_000)
        self.assertEqual((report["checked"], report["unreachable"]), (["n1", "n2"], []))
        self.assertEqual(len(report["new"]), 3)
        self.assertEqual(report["debate"]["id"], "dbt_1")
        self.assertEqual(agent.debates[0]["project"], "ops1")
        task = agent.debates[0]["task"]
        self.assertIn("- n1: 디스크 사용률 91% (임계값 85%, /)", task)
        self.assertIn("- n1: journal 크기 4.0 GiB (임계값 3G, /var/log/journal)", task)
        self.assertNotIn("n2:", task)
        state = json.loads((self.data / "ops-watch" / "ops1.json").read_text())
        self.assertEqual(set(state), {"n1|disk|/", "n1|inodes|/boot", "n1|journal|/var/log/journal"})

        again = ops_watch.watch_project(self.project, agent, self.data, now=1_000_000 + 86400)
        self.assertEqual((len(again["findings"]), again["new"], again["debate"]), (3, [], None))
        later = ops_watch.watch_project(self.project, agent, self.data, now=1_000_000 + 8 * 86400)
        self.assertEqual((len(later["new"]), later["debate"]["id"]), (3, "dbt_2"))

    def test_headscale_runs_only_when_the_watch_asks_for_it(self) -> None:
        quiet_df = DF.replace("91%", "40%").replace("88%", "4%")
        agent = FakeAgent({"n1": quiet_df, "n2": quiet_df}, {"n1": "", "n2": ""}, {"n1": HEADSCALE})
        self.assertEqual(ops_watch.watch_project(self.project, agent, self.data, dry_run=True, now=NOW)["findings"], [])
        project = parse_project({**OPS, "policy": {"watch": {"headscale": True}}})
        report = ops_watch.watch_project(project, agent, self.data, now=NOW)
        self.assertEqual([(f["host"], f["kind"]) for f in report["new"]], [("n1", "mesh_offline")])
        task = agent.debates[0]["task"]
        self.assertIn("- n1: 메시 노드 오프라인 offline since 2026-10-17T22:00Z (임계값 60 min, site-b-ec1)", task)
        self.assertIn("headscale 서비스 재시작", task)

    def test_dry_run_unreachable_hosts_and_projects_without_watch(self) -> None:
        agent = FakeAgent({"n1": DF}, {"n1": JOURNAL})
        report = ops_watch.watch_project(self.project, agent, self.data, dry_run=True)
        self.assertEqual((report["unreachable"], len(report["new"]), report["debate"]), (["n2"], 3, None))
        self.assertEqual(agent.debates, [])
        self.assertFalse((self.data / "ops-watch").exists())
        self.assertIn("no policy.watch", ops_watch.watch_project(parse_project(OPS), agent, self.data)["skipped"])

    def test_watch_all_picks_ops_projects_with_a_watch_policy(self) -> None:
        root = self.data / "projects"
        (root / "builtin").mkdir(parents=True)
        (root / "custom").mkdir()
        (root / "custom" / "ops1.json").write_text(json.dumps(WATCHED))
        (root / "custom" / "ops2.json").write_text(json.dumps({**OPS, "id": "ops2"}))
        (root / "custom" / "svc.json").write_text(json.dumps({"id": "svc", "workspace": "~/guardian-workspaces/svc"}))
        manager = ProjectManager(directory=root / "custom", builtin_dir=root / "builtin")
        agent = FakeAgent({"n1": DF, "n2": DF}, {"n1": JOURNAL, "n2": JOURNAL})
        reports = ops_watch.watch_all(manager, agent, self.data, dry_run=True)
        self.assertEqual([r["project"] for r in reports], ["ops1"])
        self.assertEqual(len(reports[0]["findings"]), 6)

    def test_builtin_fleet_project_watches_with_conservative_thresholds(self) -> None:
        fleet = ProjectManager(directory=self.data / "none").load("fleet")
        self.assertEqual(fleet.policy["watch"], {"disk_percent": 85, "inode_percent": 85, "journal_max": "3G",
                                                "headscale": True})
        self.assertEqual(fleet.policy["auto_run"], [])


if __name__ == "__main__":
    unittest.main()

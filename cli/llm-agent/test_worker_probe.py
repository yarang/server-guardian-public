"""worker_probe: requirements, fact parsing, the verdict and the inventory edits (docs/design/remote-agents.md)."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

import worker_probe as wp

HERE = os.path.dirname(os.path.abspath(__file__))


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


REQUIREMENTS = os.path.join(HERE, "..", "..", "configs", "worker-requirements.json")

GOOD_FACTS = """\
hostname_short=node-1
arch=aarch64
mem_total_kib=12000000
mem_available_kib=9000000
python3_version=3.11.2
git_version=2.39.2
disk_free_kib=40000000
hub_path=ok
---llm-check---
  ✅ claude
  ❌ codex: 401 Unauthorized
"""


def verdict(text=GOOD_FACTS, max_jobs=None, **overrides):
    req = dict(wp.load_requirements(REQUIREMENTS))
    req.update(overrides)
    return wp.evaluate(wp.parse_facts(text), req, max_jobs)


class Requirements(unittest.TestCase):
    def test_shipped_file_is_valid(self):
        self.assertEqual(wp.validate_requirements(json.loads(read(REQUIREMENTS))), [])

    def test_rejects_bad_values(self):
        good = json.loads(read(REQUIREMENTS))
        for key, bad in (("min_total_memory_mib", 0), ("default_max_jobs", True), ("python_min", "three"),
                         ("min_working_cli", "1"), ("free_memory_mib_per_job", -5)):
            data = dict(good, **{key: bad})
            self.assertTrue(wp.validate_requirements(data), key)

    def test_rejects_unknown_and_missing_keys(self):
        good = json.loads(read(REQUIREMENTS))
        self.assertTrue(any("unknown" in p for p in wp.validate_requirements(dict(good, memory=1))))
        del good["python_min"]
        self.assertTrue(any("missing" in p for p in wp.validate_requirements(good)))
        self.assertTrue(wp.validate_requirements([]))


class Facts(unittest.TestCase):
    def test_parse_splits_facts_and_cli_lines(self):
        facts = wp.parse_facts(GOOD_FACTS)
        self.assertEqual(facts["mem_total_kib"], "12000000")
        self.assertEqual(facts["clis_ok"], ["claude"])
        self.assertEqual(facts["clis_failed"], ["codex"])

    def test_cli_lines_before_the_marker_are_not_cli_results(self):
        facts = wp.parse_facts("  ✅ claude\nhub_path=ok\n")
        self.assertEqual(facts["clis_ok"], [])


class Verdict(unittest.TestCase):
    def conditions(self, v):
        return {c["name"]: c["ok"] for c in v["conditions"]}

    def test_capable_host_passes(self):
        v = verdict()
        self.assertTrue(v["passed"], v["conditions"])
        self.assertEqual(v["max_jobs"], 2)
        self.assertEqual(v["clis"], ["claude"])

    def test_every_condition_can_fail_alone(self):
        cases = {
            "memory_total": GOOD_FACTS.replace("mem_total_kib=12000000", "mem_total_kib=2000000"),
            "memory_free": GOOD_FACTS.replace("mem_available_kib=9000000", "mem_available_kib=1000000"),
            "python": GOOD_FACTS.replace("python3_version=3.11.2", "python3_version=3.8.10"),
            "git": GOOD_FACTS.replace("git_version=2.39.2\n", ""),
            "llm_cli": GOOD_FACTS.replace("✅ claude", "❌ claude: no"),
            "disk": GOOD_FACTS.replace("disk_free_kib=40000000", "disk_free_kib=500000"),
            "hub_path": GOOD_FACTS.replace("hub_path=ok", "hub_path=unreachable"),
        }
        for name, text in cases.items():
            v = verdict(text)
            self.assertFalse(v["passed"], name)
            failed = [n for n, ok in self.conditions(v).items() if not ok]
            self.assertEqual(failed, [name])

    def test_missing_facts_fail_instead_of_passing(self):
        v = verdict("hostname_short=node-1\n")
        self.assertFalse(v["passed"])
        self.assertFalse(any(self.conditions(v).values()))

    def test_free_memory_scales_with_max_jobs(self):
        # 3 GiB available: fine for 1 or 2 jobs of 1.5 GiB, not for 3
        text = GOOD_FACTS.replace("mem_available_kib=9000000", "mem_available_kib=3200000")
        self.assertTrue(verdict(text, 2)["passed"])
        self.assertFalse(verdict(text, 3)["passed"])

    def test_python_minor_compares_numerically(self):
        self.assertTrue(verdict(GOOD_FACTS.replace("3.11.2", "3.10.0"))["passed"])  # 3.10 > 3.9, not a string compare

    def test_garbage_numbers_fail(self):
        v = verdict(GOOD_FACTS.replace("mem_total_kib=12000000", "mem_total_kib=lots"))
        self.assertFalse(self.conditions(v)["memory_total"])


class Table(unittest.TestCase):
    def test_table_marks_failures_and_exits_nonzero(self):
        bad = verdict(GOOD_FACTS.replace("hub_path=ok", "hub_path=unreachable"))
        text = wp.format_verdict(bad)
        self.assertIn("❌ hub_path", text)
        self.assertIn("not capable", text)
        run = subprocess.run([sys.executable, os.path.join(HERE, "worker_probe.py"), "table"],
                             input=json.dumps(bad), text=True, capture_output=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn("✅ memory_total", run.stdout)
        run = subprocess.run([sys.executable, os.path.join(HERE, "worker_probe.py"), "table"],
                             input=json.dumps(verdict()), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0)


class InventoryEdits(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "servers.json")
        self.lock = os.path.join(self.dir.name, "fleet.lock")
        self.entries = [
            {"id": "hub-1", "guardian_role": "hub", "zeta": 1, "alpha": "한글"},
            {"id": "node-1", "tier": 2, "unknown_future_field": {"a": [1, 2]}},
        ]
        self.write(self.entries)
        os.chmod(self.path, 0o640)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def load(self):
        return json.loads(read(self.path))

    def passed(self):
        return {"passed": True, "max_jobs": 2, "clis": ["claude"],
                "conditions": [{"name": "disk", "ok": True, "detail": "ok"}]}

    def test_record_keeps_other_entries_key_order_and_mode(self):
        before = read(self.path)
        wp.record_probe(self.path, "node-1", self.passed(), self.lock)
        data = self.load()
        self.assertEqual(data[0], self.entries[0])
        self.assertEqual(list(data[0]), ["id", "guardian_role", "zeta", "alpha"])
        self.assertEqual(data[1]["unknown_future_field"], {"a": [1, 2]})
        self.assertTrue(data[1]["worker_capable"]["passed"])
        self.assertEqual(data[1]["worker_capable"]["clis"], ["claude"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        self.assertEqual(read(self.path + ".bak"), before)
        self.assertIn("한글", read(self.path))  # ensure_ascii stays off
        self.assertEqual([n for n in os.listdir(self.dir.name) if ".tmp-" in n], [])

    def test_failed_probe_turns_the_worker_off(self):
        wp.record_probe(self.path, "node-1", self.passed(), self.lock)
        wp.set_worker(self.path, "node-1", True, {"rev": "abc"}, self.lock)
        self.assertIs(self.load()[1]["worker"], True)
        bad = {"passed": False, "max_jobs": 2, "clis": [],
               "conditions": [{"name": "memory_total", "ok": False, "detail": "2000 MiB, needs 4096 MiB"}]}
        wp.record_probe(self.path, "node-1", bad, self.lock)
        entry = self.load()[1]
        self.assertIs(entry["worker"], False)
        self.assertEqual(entry["worker_capable"]["reasons"], ["memory_total: 2000 MiB, needs 4096 MiB"])

    def test_enable_needs_a_passed_probe(self):
        with self.assertRaises(SystemExit):
            wp.set_worker(self.path, "node-1", True, None, self.lock)
        self.assertNotIn("worker", self.load()[1])
        wp.record_probe(self.path, "node-1", dict(self.passed(), passed=False), self.lock)
        with self.assertRaises(SystemExit):
            wp.set_worker(self.path, "node-1", True, None, self.lock)

    def test_disable_is_always_allowed_and_drops_install_info(self):
        wp.record_probe(self.path, "node-1", self.passed(), self.lock)
        wp.set_worker(self.path, "node-1", True, {"rev": "abc"}, self.lock)
        self.assertEqual(self.load()[1]["worker_install"], {"rev": "abc"})
        wp.set_worker(self.path, "node-1", False, None, self.lock)
        entry = self.load()[1]
        self.assertIs(entry["worker"], False)
        self.assertNotIn("worker_install", entry)

    def test_the_hub_and_unknown_hosts_are_refused(self):
        for host in ("hub-1", "ghost"):
            with self.assertRaises(SystemExit):
                wp.record_probe(self.path, host, self.passed(), self.lock)
        with self.assertRaises(SystemExit):
            wp.record_probe(self.path, "../etc/passwd", self.passed(), self.lock)
        self.assertEqual(self.load(), self.entries)

    def test_a_broken_inventory_is_left_alone(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ValueError):
            wp.record_probe(self.path, "node-1", self.passed(), self.lock)
        self.assertEqual(read(self.path), "{not json")

    def test_cli_round_trip_as_the_script_pipes_it_to_the_hub(self):
        arg = base64.b64encode(json.dumps(self.passed()).encode()).decode()
        env = dict(os.environ, HOME=self.dir.name)  # the default lock lives under $HOME/.ssh
        run = subprocess.run([sys.executable, "-"] + ["record-probe", self.path, "node-1", arg],
                             input=read(os.path.join(HERE, "worker_probe.py")),
                             text=True, capture_output=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(self.load()[1]["worker_capable"]["passed"])

    def test_evaluate_cli_reads_facts_from_stdin(self):
        run = subprocess.run([sys.executable, os.path.join(HERE, "worker_probe.py"), "evaluate",
                              "--requirements", REQUIREMENTS, "--max-jobs", "1"],
                             input=GOOD_FACTS, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertTrue(out["passed"])
        self.assertEqual(out["max_jobs"], 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests of scripts/llm_load_probe.py (R0 of docs/design/remote-llm-hosts.md). No real /proc reading is asserted."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "cli" / "llm-agent"))

import llm_load_probe as probe  # noqa: E402
from llm_load_probe import Proc  # noqa: E402


def proc(pid, ppid, argv, self_ticks=0, child_ticks=0, rss=0, start=None, comm=None):
    return Proc(pid=pid, ppid=ppid, comm=comm or Path(argv[0]).name[:15], argv=list(argv),
                start=pid if start is None else start, self_ticks=self_ticks, child_ticks=child_ticks, rss=rss)


def tree(**extra):
    """systemd -> Go agent -> guardian.py -> claude (node) -> repo_mcp.py -> git; plus Gitea and an operator claude."""
    procs = {
        1: proc(1, 0, ["/sbin/init"]),
        10: proc(10, 1, ["/home/u/.guardian/current/bin/agent"]),
        20: proc(20, 10, ["python3", "/home/u/.guardian/current/cli/llm-agent/guardian.py", "--debate", "x"]),
        21: proc(21, 20, ["git", "cat-file", "--batch"]),
        30: proc(30, 20, ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "--print"], comm="claude"),
        31: proc(31, 30, ["/usr/bin/python3", "/home/u/cli/llm-agent/repo_mcp.py", "--workspace", "w"]),
        32: proc(32, 31, ["git", "ls-tree", "-r"]),
        40: proc(40, 20, ["codex", "exec", "--sandbox", "read-only", "-"]),
        50: proc(50, 1, ["/usr/local/bin/gitea", "web"]),
        60: proc(60, 1, ["tmux"]),
        61: proc(61, 60, ["claude"]),
    }
    procs.update(extra)
    return procs


class ClassifyTests(unittest.TestCase):
    def test_classes_follow_the_first_role_up_the_tree(self):
        classes = probe.classify(tree())
        self.assertEqual(classes[20], "engine")
        self.assertEqual(classes[21], "engine")
        self.assertEqual(classes[30], "llm")
        self.assertEqual(classes[40], "llm")
        self.assertEqual(classes[31], "retrieval")
        self.assertEqual(classes[32], "retrieval")
        for pid in (1, 10, 50, 60):
            self.assertEqual(classes[pid], "other")

    def test_an_operator_session_is_not_an_engine_turn(self):
        self.assertEqual(probe.classify(tree())[61], "other")

    def test_parse_stat_with_spaces_and_parentheses_in_comm(self):
        fields = " ".join(str(n) for n in range(3, 30))
        comm, f = probe.parse_stat(f"123 (a (b) c) {fields}")
        self.assertEqual(comm, "a (b) c")
        self.assertEqual(f[1], "4")  # field 4 = ppid

    def test_read_procs_from_a_fake_proc_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "42"
            d.mkdir()
            f = ["S", "7"] + ["0"] * 9 + ["5", "6", "1", "2"] + ["0"] * 4 + ["999", "0", "10"]
            (d / "stat").write_text(f"42 (claude) {' '.join(f)}\n")
            (d / "cmdline").write_bytes(b"claude\0--print\0")
            (root / "self").mkdir()
            p = probe.read_procs(root, page_size=4096)[42]
        self.assertEqual((p.ppid, p.self_ticks, p.child_ticks, p.start, p.rss), (7, 11, 3, 999, 40960))
        self.assertEqual(p.argv, ["claude", "--print"])


class TrackerTests(unittest.TestCase):
    def step(self, tracker, procs):
        return tracker.step(procs, probe.classify(procs))

    def test_deltas_per_class(self):
        t = probe.Tracker()
        self.step(t, tree())
        later = tree()
        later[30] = proc(30, 20, later[30].argv, self_ticks=100, comm="claude")
        later[20] = proc(20, 10, later[20].argv, self_ticks=7)
        later[50] = proc(50, 1, later[50].argv, self_ticks=3)
        ticks = self.step(t, later)
        self.assertEqual(ticks, {"llm": 100, "retrieval": 0, "engine": 7, "other": 3})

    def test_reaped_child_time_is_not_counted_twice_and_goes_to_its_class(self):
        t = probe.Tracker()
        first = tree()
        first[30] = proc(30, 20, first[30].argv, self_ticks=80, comm="claude")
        self.step(t, first)  # baseline: claude has used 80 ticks
        later = tree()
        del later[30], later[31], later[32]
        # claude used 20 more ticks, exited and was reaped: its 100 ticks reach guardian.py's cutime.
        later[20] = proc(20, 10, later[20].argv, child_ticks=100)
        ticks = self.step(t, later)
        self.assertEqual(ticks["llm"], 20)
        self.assertEqual(ticks["engine"], 0)

    def test_a_reused_pid_is_a_new_process(self):
        t = probe.Tracker()
        first = tree()
        first[21] = proc(21, 20, ["git", "cat-file"], self_ticks=50)
        self.step(t, first)
        later = tree()
        later[21] = proc(21, 20, ["git", "cat-file"], self_ticks=4, start=9999)
        self.assertEqual(self.step(t, later)["engine"], 4)


def sample(util=0.5, llm=0.6, turns=1, avail=0.5, llm_rss=0.6, psi_cpu=1.0, psi_mem=0.0):
    total, mem_total = 1000, 12 * 2**30
    busy = int(total * util)
    used = mem_total * (1 - avail)
    classes = {c: {"ticks": 0, "rss": 0, "procs": 0} for c in probe.CLASSES}
    classes["llm"].update(ticks=int(busy * llm) if turns else 0, rss=int(used * llm_rss) if turns else 0, procs=turns)
    classes["engine"]["ticks"] = int(busy * (1 - llm) / 2)
    return {"ts": "2026-10-30T00:00:00Z", "cpu": {"busy": busy, "total": total},
            "mem": {"total": mem_total, "available": int(mem_total * avail), "swap_used": 0},
            "psi": {"cpu_some": psi_cpu, "mem_some": psi_mem, "mem_full": 0.0}, "classes": classes, "llm_turns": turns}


class ReportTests(unittest.TestCase):
    rule = dict(probe.DECISION_RULE, min_busy_samples=3)

    def test_not_enough_busy_samples(self):
        r = probe.summarise([sample(turns=0)] * 10 + [sample()], rule=dict(probe.DECISION_RULE))
        self.assertEqual(r["verdict"], "insufficient")

    def test_no_turns_at_all(self):
        self.assertEqual(probe.summarise([sample(turns=0)] * 5)["verdict"], "insufficient")

    def test_go_needs_pressure_and_share(self):
        r = probe.summarise([sample(util=0.9, llm=0.7)] * 5, rule=self.rule)
        self.assertEqual(r["verdict"], "go")
        self.assertAlmostEqual(r["cpu_share"]["llm"], 0.7, places=2)

    def test_no_pressure_is_not_needed(self):
        r = probe.summarise([sample(util=0.3, llm=0.9, avail=0.6)] * 5, rule=self.rule)
        self.assertEqual(r["verdict"], "not-needed")

    def test_pressure_from_something_else(self):
        r = probe.summarise([sample(util=0.95, llm=0.1, llm_rss=0.1)] * 5, rule=self.rule)
        self.assertEqual(r["verdict"], "elsewhere")

    def test_memory_or_psi_pressure_counts(self):
        r = probe.summarise([sample(util=0.2, avail=0.05)] * 5, rule=self.rule)
        self.assertEqual(r["verdict"], "go")
        r = probe.summarise([sample(util=0.2, psi_mem=25.0)] * 5, rule=self.rule)
        self.assertEqual(r["verdict"], "go")

    def test_idle_samples_do_not_count_as_busy(self):
        r = probe.summarise([sample(util=0.99, turns=0)] * 5 + [sample(util=0.2)] * 3, rule=self.rule)
        self.assertEqual(r["busy_samples"], 3)
        self.assertEqual(r["verdict"], "not-needed")

    def test_load_samples_skips_a_cut_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples-2026-10-30.jsonl"
            path.write_text(json.dumps(sample()) + "\n" + '{"ts": "2026', encoding="utf-8")
            self.assertEqual(len(probe.load_samples(Path(tmp))), 1)


class HistoryTests(unittest.TestCase):
    def write_job(self, root, job, events):
        d = root / job
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def test_concurrency_from_agent_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_job(root, "a", [
                {"ts": "2026-10-30T00:00:00.000Z", "type": "debate_started", "data": {}},
                {"ts": "2026-10-30T00:01:40.000Z", "type": "agent_finished", "data": {"provider": "claude", "duration_ms": 100000}},
                {"ts": "2026-10-30T00:02:00.000Z", "type": "agent_finished", "data": {"provider": "builtin", "fallback": True}},
            ])
            self.write_job(root, "b", [
                {"ts": "2026-10-30T00:01:00.500Z", "type": "agent_finished", "data": {"provider": "codex", "duration_ms": 50000}},
                {"ts": "2026-10-29T00:00:00.000Z", "type": "agent_finished", "data": {"provider": "codex", "duration_ms": 1000}},
            ])
            since = probe._parse_ts("2026-10-29T12:00:00Z")
            h = probe.summarise_history(probe.iter_turns(root, since))
        self.assertEqual((h["debates"], h["turns"], h["fallback_turns"]), (1, 2, 1))
        self.assertEqual(h["providers"], {"claude": 1, "codex": 1})
        self.assertEqual(h["peak_concurrency"], 2)
        # claude 0-100 s, codex 10.5-60.5 s: 100 s busy, 50 s of it with two turns.
        self.assertAlmostEqual(h["busy_hours"] * 3600, 100.0, places=3)
        self.assertAlmostEqual(h["time_at_concurrency"]["2"], 0.5, places=3)

    def test_timestamp_fraction(self):
        self.assertAlmostEqual(probe._parse_ts("2026-10-08T05:21:03.120Z") - probe._parse_ts("2026-10-08T05:21:03Z"), 0.12, places=3)


class HostTests(unittest.TestCase):
    def test_tier1_selection(self):
        inv = [{"id": "hub-1", "guardian_role": "hub", "tier": "Tier-1"}, {"id": "node-1", "tier": "Tier-2"},
               {"id": "node-2", "tier": "tier-1"}]
        self.assertEqual([h["id"] for h in probe.tier1_hosts(inv)], ["hub-1", "node-2"])

    def test_parse_probe_and_versions(self):
        facts = probe.parse_probe("cli_claude=2.1.300 (Claude Code)\ncli_agy=\nncpu=2\nnoise line\nBAD=1\n")
        self.assertEqual(facts, {"cli_claude": "2.1.300 (Claude Code)", "cli_agy": "", "ncpu": "2"})
        self.assertEqual(probe.version_tuple(facts["cli_claude"]), (2, 1, 300))
        self.assertIsNone(probe.version_tuple(""))

    def test_claude_minimum_matches_the_runner(self):
        import llm_runner

        self.assertEqual(probe.CLAUDE_MIN_VERSION, llm_runner.CLAUDE_RECORDED_PROMPT_VERSION)

    def test_probe_reads_no_credentials(self):
        for word in (".claude", ".codex", ".guardian", "credentials", "auth.json", "llm.env"):
            self.assertNotIn(word, probe.REMOTE_PROBE)

    def test_duration(self):
        self.assertEqual(probe.parse_duration("48h"), 172800)
        self.assertEqual(probe.parse_duration("90"), 90)
        with self.assertRaises(Exception):
            probe.parse_duration("soon")


if __name__ == "__main__":
    unittest.main()

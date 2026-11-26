"""Tests for scripts/probe-resident.sh and scripts/lib/resident-probe-remote.sh.

Fake claude, agy and codex executables stand in for the real CLIs; no network, no ssh, no real home.
"""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "probe-resident.sh"
REMOTE = ROOT / "scripts" / "lib" / "resident-probe-remote.sh"
TOKEN = "dummy-oauth-token-7f3a"
# The fake agy reports the table's version, so a table update does not break the happy path.
AGY_VERSION = json.loads((ROOT / "configs" / "resident-providers.json").read_text())["providers"]["agy"]["version"]

FAKE_CLAUDE = """\
#!/usr/bin/env python3
import json, os, re, sys
RECORD, MODE, VERSION = {record!r}, {mode!r}, {version!r}
args = sys.argv[1:]
if args == ["--version"]:
    print(VERSION + " (Claude Code)")
    sys.exit(0)
with open(RECORD, "a") as fh:
    fh.write(json.dumps({{"cli": "claude", "env": dict(os.environ), "args": args}}) + "\\n")
prompt = args[args.index("-p") + 1] if "-p" in args else ""
if MODE == "bang":
    m = re.search(r"(\\S+/bang-claude)", prompt)
    open(m.group(1), "w").close()
if MODE == "guard":
    os.makedirs(os.path.join(os.environ["HOME"], ".claude"), exist_ok=True)
    with open(os.path.join(os.environ["HOME"], ".claude", "settings.json"), "w") as fh:
        fh.write('{{"theme": "dark"}}')
print("Mail says: PINEAPPLE")
"""

FAKE_AGY = """\
#!/usr/bin/env python3
import json, os, sys
RECORD, VERSION = {record!r}, {version!r}
if sys.argv[1:] == ["--version"]:
    print(VERSION)
    sys.exit(0)
line = sys.stdin.readline()
marker = os.path.join(os.environ["HOME"], ".gemini", "antigravity-cli", "cache", "onboarding.json")
onboarding = open(marker).read() if os.path.exists(marker) else None
# The real agy logs its updater decision under .gemini/antigravity-cli/log/: disabled only on the
# exact lowercase "true", a spawned background update process otherwise.
log_dir = os.path.join(os.environ["HOME"], ".gemini", "antigravity-cli", "log")
os.makedirs(log_dir, exist_ok=True)
if os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE") == "true":
    log_line = "auto_updater.go:247] Auto-update disabled via environment variable AGY_CLI_DISABLE_AUTO_UPDATE\\n"
else:
    log_line = "auto_updater.go:334] Spawned background update process\\n"
with open(os.path.join(log_dir, "cli-test.log"), "w") as fh:
    fh.write(log_line)
with open(RECORD, "a") as fh:
    fh.write(json.dumps({{"cli": "agy", "env": dict(os.environ), "stdin": line, "onboarding": onboarding}}) + "\\n")
print(json.dumps({{"event": "init", "conversation_id": "c-1"}}))
print(json.dumps({{"event": "result", "result": {{"status": "success", "response": "PINEAPPLE",
                                                "conversation_id": "c-1"}}}}))
"""

FAKE_CODEX = """\
#!/usr/bin/env python3
import sys
print("codex-cli 0.163.0")
"""


class ResidentProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resprobe-test-")).resolve()
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.scratch = self.tmp / "scratch"
        self.record = self.tmp / "record.jsonl"
        for d in (self.home, self.bin, self.scratch):
            d.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_fakes(self, mode="ok", version="2.1.301", token=True):
        fakes = {
            "claude": FAKE_CLAUDE.format(record=str(self.record), mode=mode, version=version),
            "agy": FAKE_AGY.format(record=str(self.record), version=AGY_VERSION),
            "codex": FAKE_CODEX,
        }
        for name, body in fakes.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        if token:
            (self.home / ".guardian").mkdir()
            (self.home / ".guardian" / "llm.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=%s\n" % TOKEN)
            agy_dir = self.home / ".gemini" / "antigravity-cli"
            agy_dir.mkdir(parents=True)
            (agy_dir / "antigravity-oauth-token").write_text("agy-token")

    def run_probe(self, *extra):
        env = {
            "HOME": str(self.home),
            "TMPDIR": str(self.scratch),
            # /bin first: on a Mac that is bash 3.2, the oldest shell the scripts must run under.
            "PATH": "%s:/bin:/usr/bin:%s" % (self.bin, os.environ.get("PATH", "")),
            "PROBE_LEAK": "1",
            "LANG": "C.UTF-8",
        }
        return subprocess.run(["/bin/bash", str(PROBE), "--local", *extra], cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=120)

    def lines(self, proc):
        out = {}
        for line in proc.stdout.splitlines():
            key, sep, val = line.partition("=")
            if sep:
                out.setdefault(key, val)
        return out

    def records(self, cli):
        if not self.record.exists():
            return []
        recs = [json.loads(line) for line in self.record.read_text().splitlines()]
        return [r for r in recs if r["cli"] == cli]

    def test_happy_path(self):
        self.make_fakes()
        proc = self.run_probe()
        kv = self.lines(proc)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(kv["guard"], "ok")
        for key, want in (("claude.mcp", "pass"), ("claude.closure", "pass"), ("claude.version_changed", "no"),
                          ("agy.mcp", "pass"), ("agy.closure", "pass"), ("agy.version_changed", "no"),
                          ("agy.autoupdate", "off"), ("codex.status", "not_resident"),
                          ("codex.resident", "false")):
            self.assertEqual(kv.get(key), want, key)
        turns = self.records("claude")
        self.assertEqual(len(turns), 1)
        env = turns[0]["env"]
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].startswith(str(self.scratch)), env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), TOKEN)
        self.assertNotIn("PROBE_LEAK", env)
        self.assertNotIn(TOKEN, " ".join(turns[0]["args"]))
        agy_env = self.records("agy")[0]["env"]
        self.assertTrue(agy_env["HOME"].startswith(str(self.scratch)))
        # agy honours only the exact lowercase "true": anything else leaves the updater on.
        self.assertEqual(agy_env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertNotIn("PROBE_LEAK", agy_env)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        self.assertEqual(list(self.scratch.iterdir()), [], "scratch directory left behind")

    def test_agy_onboarding_seeded(self):
        self.make_fakes()
        proc = self.run_probe("--provider", "agy")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rec = self.records("agy")[0]
        self.assertIsNotNone(rec["onboarding"], "onboarding marker not seeded in the scratch home")
        self.assertEqual(json.loads(rec["onboarding"]), {"consumerOnboardingComplete": True,
                                                         "enterpriseOnboardingComplete": False,
                                                         "onboardingComplete": True})
        self.assertFalse((self.home / ".gemini" / "antigravity-cli" / "cache").exists(), "real home touched")

    def test_closure_failure(self):
        self.make_fakes(mode="bang")
        proc = self.run_probe()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.lines(proc)["claude.closure"], "fail")

    def test_guard_change(self):
        self.make_fakes(mode="guard")
        proc = self.run_probe()
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertEqual(self.lines(proc)["guard"], "changed")
        self.assertIn("guard.path=.claude/settings.json", proc.stdout.splitlines())

    def test_version_change(self):
        self.make_fakes(version="2.2.0")
        proc = self.run_probe()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        kv = self.lines(proc)
        self.assertEqual(kv["claude.version"], "2.2.0")
        self.assertEqual(kv["claude.version_changed"], "yes")

    def test_no_token(self):
        self.make_fakes(token=False)
        proc = self.run_probe("--provider", "claude")
        kv = self.lines(proc)
        self.assertEqual(kv["claude.status"], "skipped")
        self.assertEqual(kv["claude.reason"], "no_token")
        self.assertEqual(self.records("claude"), [])

    def test_provider_filter(self):
        self.make_fakes()
        proc = self.run_probe("--provider", "agy")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        keys = self.lines(proc)
        self.assertIn("agy.mcp", keys)
        self.assertFalse([k for k in keys if k.startswith(("claude.", "codex."))], keys)

    def test_misuse(self):
        self.assertEqual(self.run_probe("--provider", "gemini").returncode, 2)

    def test_launch_isolated_refuses(self):
        root = self.tmp / "root"
        (root / "inside").mkdir(parents=True)
        (self.tmp / "rootx").mkdir()
        (self.tmp / "elsewhere").mkdir()
        marker = self.tmp / "marker"
        script = textwrap.dedent("""\
            RESIDENT_PROBE_SOURCE_ONLY=1 source "$1"
            ROOT="$2"
            check() { launch_isolated "$@" -- touch "$MARKER" 2>/dev/null; echo "rc=$?"; [ -e "$MARKER" ] && echo ran; rm -f "$MARKER"; }
            check CLAUDE_CONFIG_DIR "$3"
            check CLAUDE_CONFIG_DIR "$4"
            check PYTHONPATH "$2/inside"
            check CLAUDE_CONFIG_DIR "$2/inside" PATH=/tmp
            check CLAUDE_CONFIG_DIR "$2/inside"
            """)
        env = {"PATH": "/bin:/usr/bin:" + os.environ.get("PATH", ""), "HOME": str(self.home), "MARKER": str(marker)}
        proc = subprocess.run(["/bin/bash", "-c", script, "test", str(REMOTE), str(root), str(self.tmp / "elsewhere"),
                               str(self.tmp / "rootx")], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.split(), ["rc=5", "rc=5", "rc=5", "rc=5", "rc=0", "ran"], proc.stderr)


if __name__ == "__main__":
    unittest.main()

"""The headless driver of the resident agents (docs/design/resident-agents.md, M2d) end to end.

The harness is test_relay_e2e's: the real hub relay (mail_relay.RelaySession) and the real node
manager (resident_node.ResidentManager) over one socketpair, with the real store on the hub side.
Only the model is fake: FakeRunner plays each turn, records it and talks to the agent socket with
JSON lines exactly as the mailbox MCP would. No real claude or agy runs (the agy version check is
injected); the neutral names node-1, claude@node-1 and agy@node-1 are used.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import mail_relay
import mail_store
import resident_driver
import resident_open
import resident_registry
import testenv
import worker
from mailbox_mcp import TOOLS as MAILBOX_TOOLS
from mailbox_mcp import allowed_tool_names
from repo_mcp import SNAPSHOT_TOOLS
from resident_driver import AGY_VERSION, HOOK_PY_SOURCE, START_LINE, WAKE_LINE
from resident_driver import HeadlessDriver, TurnContext, TurnResult
from resident_node import ResidentManager

NODE = "node-1"
FIRST = f"claude@{NODE}"
SECOND = f"agy@{NODE}"
ROOT = Path(__file__).resolve().parent.parent.parent


def wait_until(predicate: Any, what: str = "the condition", timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def call(socket_path: Any, tool: str, **args: Any) -> Dict[str, Any]:
    """One mailbox tool call on the agent socket, as the MCP would make it."""
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(10.0)
    try:
        conn.connect(str(socket_path))
        conn.sendall((json.dumps({"tool": tool, "args": args}) + "\n").encode("utf-8"))
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
    finally:
        conn.close()
    return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))


def ok_turn() -> Callable[[TurnContext, str], None]:
    return lambda ctx, prompt: None  # the FakeRunner default: ok, the session unchanged


def fail_turn(error: str = "boom", missing: bool = False) -> Callable[[TurnContext, str], TurnResult]:
    return lambda ctx, prompt: TurnResult(ok=False, error=error, missing_session=missing)


class FakeRunner:
    """Plays the model: records every turn and pops the agent's next scripted function.

    A script runs on the turn's thread, may talk to ctx.socket like the mailbox MCP would and
    returns a TurnResult (None falls back to an ok turn that keeps the session id, so an agy first
    turn reports a conversation id like the real CLI)."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self.script_errors: List[Any] = []
        self._scripts: Dict[str, List[Callable[[TurnContext, str], Optional[TurnResult]]]] = {}
        self.lock = threading.Lock()

    def queue(self, name: str, *scripts: Callable[[TurnContext, str], Optional[TurnResult]]) -> None:
        with self.lock:
            self._scripts.setdefault(name, []).extend(scripts)

    @staticmethod
    def _default(ctx: TurnContext) -> TurnResult:
        return TurnResult(ok=True, session_id=ctx.session_id or f"c-{ctx.name}")

    def run(self, ctx: TurnContext, prompt: str) -> TurnResult:
        record: Dict[str, Any] = {"name": ctx.name, "prompt": prompt, "card": ctx.card,
                                  "provider": ctx.provider, "epoch": ctx.epoch,
                                  "session_id": ctx.session_id, "first": ctx.first,
                                  "socket": ctx.socket, "at": time.monotonic(),
                                  "tools": (ctx.spec or {}).get("tools"),
                                  "mcp_dir": getattr(ctx, "mcp_dir", ""),
                                  "ok": None, "error": ""}
        with self.lock:
            self.records.append(record)
            scripts = self._scripts.get(ctx.name) or []
            script = scripts.pop(0) if scripts else None
        if script is None:
            result = self._default(ctx)
        else:
            try:
                result = script(ctx, prompt)
            except Exception as exc:  # an assertion in a script must fail the test, not lose the turn
                with self.lock:
                    self.script_errors.append((ctx.name, repr(exc)))
                result = TurnResult(ok=False, error=f"script failed: {exc!r}")
            if result is None:
                result = self._default(ctx)
        with self.lock:
            record["ok"], record["error"] = result.ok, result.error
        return result


class HeadlessDriverEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.dir = Path(tempfile.mkdtemp(prefix="sgd", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # The agy token the bind symlinks: a fake node-user home
        self.home = self.dir / "nodehome"
        token = self.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("fake-token", encoding="utf-8")
        env = patch.dict(os.environ, {"GUARDIAN_WORKER_DIR": str(self.dir),
                                      "GUARDIAN_WORKER_SOCKET": str(self.dir / "w.sock"),
                                      "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # a short marker card instead of resident_cards' text (test_resident_cards checks the card)
        cards = patch.object(mail_relay, "card_text", lambda entry: f"CARD-MARKER {entry['address']}")
        cards.start()
        self.addCleanup(cards.stop)
        # the operator's --resident-add: the declaration file and the store rows together
        entries = [{"name": "claude", "node": NODE, "provider": "claude", "driver": "headless"},
                   {"name": "agy", "node": NODE, "provider": "agy", "driver": "headless"}]
        path = resident_registry.declaration_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "agents": entries}), encoding="utf-8")
        for entry in entries:
            mail_store.MailStore().upsert_agent({**entry, "address": f"{entry['name']}@{NODE}"})

        # O2 hook: the open-agent subclass points the driver and the manager at a temp sandbox and
        # opens the claude entry's spec; the closed tests change nothing
        self.sandbox_paths: Optional[resident_open.SandboxPaths] = None
        self.prepare_open_sandbox()
        paths = self.sandbox_paths if self.sandbox_paths is not None else resident_open.DEFAULT_PATHS
        self.fake = FakeRunner()
        self.driver = HeadlessDriver(runner=self.fake, tick=0.1, sweep_age=0.5,
                                     backoff=(0.2, 0.2, 0.2),
                                     version_check=lambda ctx: AGY_VERSION, sandbox_paths=paths)
        self.extra_drivers: List[HeadlessDriver] = []
        self.daemon = worker.Worker(max_jobs=2, queue_seconds=1.0)
        self.mgr = ResidentManager(self.daemon, root=self.dir / "agents", driver=self.driver,
                                   sandbox_paths=paths)

        hub_end, node_end = socket.socketpair()
        self.sockets = (hub_end, node_end)
        self.node_thread = threading.Thread(
            target=self.mgr.attach, args=(node_end.makefile("rb"), node_end.makefile("wb")), daemon=True)
        self.session = mail_relay.RelaySession(NODE, hub_end.makefile("rb"), hub_end.makefile("wb"),
                                               store=mail_store.MailStore())
        self.session.POLL_INTERVAL = 0.05
        self.stop = threading.Event()
        self.hub_thread = threading.Thread(target=self.session.run, args=(self.stop,), daemon=True)
        self.hub_thread.start()
        self.node_thread.start()
        # the driver is attached by start_driver(), after the test queued its scripts: the first
        # pass must not run a start turn with the default script before the scripts are in place

    def tearDown(self) -> None:
        self.stop.set()
        for driver in self.extra_drivers:
            driver.stop()
        self.mgr.shutdown()
        for end in self.sockets:
            try:
                end.shutdown(socket.SHUT_RDWR)  # wakes both reader threads
            except OSError:
                pass
        self.hub_thread.join(5.0)
        self.node_thread.join(5.0)
        for end in self.sockets:
            end.close()
        self.assertFalse(self.hub_thread.is_alive(), "the hub session did not stop")
        if self.fake.script_errors:
            self.fail(f"script errors: {self.fake.script_errors}")

    # -- helpers --------------------------------------------------------------------------------

    def prepare_open_sandbox(self) -> None:
        """A hook for the open-agent subclass (O2); the closed tests need nothing."""

    def start_driver(self) -> None:
        """Binds the driver to the running manager: the tests queue their scripts first."""
        self.driver.attach(self.mgr)

    def records(self, name: str) -> List[Dict[str, Any]]:
        with self.fake.lock:
            return [record for record in self.fake.records if record["name"] == name]

    def wait_records(self, name: str, count: int, what: str = "") -> None:
        wait_until(lambda: len(self.records(name)) >= count,
                   what=what or f"{count} turn(s) of {name}")

    def spool(self, name: str) -> List[str]:
        return sorted(p.stem for p in (self.dir / "agents" / name / "spool").glob("*.json"))

    def states(self) -> Dict[str, str]:
        return {m["id"]: m["state"] for m in mail_store.MailStore().list_mail(limit=200)}

    def agent_row(self, address: str) -> Dict[str, Any]:
        return next((row for row in mail_store.MailStore().agent_rows() if row["address"] == address), {})

    def wait_agent_down(self, address: str, reason: str) -> None:
        def down() -> bool:
            row = self.agent_row(address)
            if row.get("state") != "down":
                return False
            return any(event.get("kind") == "agent_down"
                       and (event.get("detail") or {}).get("reason") == reason
                       for event in mail_store.MailStore().events(limit=100))
        wait_until(down, what=f"{address} down ({reason})")

    def send(self, to: str, subject: str, body: str, op_id: str) -> Dict[str, Any]:
        sent = mail_store.MailStore().send("operator", to, "message", subject, body, op_id=op_id)
        self.assertTrue(sent.get("ok"), sent)
        return sent

    # -- 1: two agents, bind, start cards, one question and its answer --------------------------

    def test_two_agents_bind_start_and_exchange_mail(self) -> None:
        release = threading.Event()
        seen: Dict[str, Any] = {}

        def start_turn(name: str) -> Callable[[TurnContext, str], None]:
            def script(ctx: TurnContext, prompt: str) -> None:
                agents = call(ctx.socket, "mail_agents")
                self.assertTrue(agents.get("ok"), agents)
                inbox = call(ctx.socket, "mail_inbox")
                self.assertTrue(inbox.get("ok"), inbox)
                self.assertEqual(inbox["messages"], [])
            return script

        def claude_asks(ctx: TurnContext, prompt: str) -> None:
            inbox = call(ctx.socket, "mail_inbox")
            self.assertEqual([m["subject"] for m in inbox["messages"]], ["start"])
            question = call(ctx.socket, "mail_send", to=SECOND, kind="question", subject="disk",
                            body="what is free on /var?")
            self.assertTrue(question.get("ok"), question)
            seen["question"] = question["id"]
            release.wait(10.0)  # hold the resident slot: agy must not start a turn meanwhile

        def agy_answers(ctx: TurnContext, prompt: str) -> None:
            inbox = call(ctx.socket, "mail_inbox")
            self.assertEqual([m["id"] for m in inbox["messages"]], [seen["question"]])
            self.assertEqual(inbox["messages"][0]["from"], FIRST)
            answer = call(ctx.socket, "mail_send", to=FIRST, kind="answer", subject="disk",
                          body="12 GB free", reply_to=seen["question"])
            self.assertTrue(answer.get("ok"), answer)
            seen["answer"] = answer["id"]

        def claude_reads_answer(ctx: TurnContext, prompt: str) -> None:
            inbox = call(ctx.socket, "mail_inbox")
            self.assertEqual([m["body"] for m in inbox["messages"]], ["12 GB free"])

        self.fake.queue("claude", start_turn("claude"), claude_asks, claude_reads_answer)
        self.fake.queue("agy", start_turn("agy"), agy_answers)
        self.start_driver()

        # both agents bind by themselves and get one start turn each
        self.wait_records("claude", 1, "claude's start turn")
        self.wait_records("agy", 1, "agy's start turn")
        claude_first, agy_first = self.records("claude")[0], self.records("agy")[0]
        self.assertEqual(claude_first["prompt"], START_LINE)
        self.assertIn("CARD-MARKER claude@node-1", claude_first["card"])
        self.assertEqual(agy_first["prompt"], "CARD-MARKER agy@node-1\n\n" + START_LINE)
        self.assertTrue(claude_first["first"])
        self.assertFalse(agy_first["first"])

        sent = self.send(FIRST, "start", "take the task", "op-1")
        self.wait_records("claude", 2, "claude's wake turn")
        wait_until(lambda: "question" in seen and seen["question"] in self.spool("agy"),
                   what="the question in agy's spool")
        # the resident slot is claude's (D5): agy gets no turn until claude's turn ends
        time.sleep(0.3)
        self.assertEqual(len(self.records("agy")), 1)

        release.set()
        self.wait_records("agy", 2, "agy's turn after the slot went free")
        self.wait_records("claude", 3, "claude reading the answer")
        wait_until(lambda: self.states().get(seen["answer"]) == "acked", what="the answer acked")
        states = self.states()
        self.assertEqual((states.get(sent["id"]), states.get(seen["question"]), states.get(seen["answer"])),
                         ("acked", "acked", "acked"))
        self.assertEqual((self.spool("claude"), self.spool("agy")), ([], []))

    # -- 2: mail that arrives during a turn coalesces into one continuation turn ---------------

    def test_two_mails_during_one_turn_coalesce_into_one_turn(self) -> None:
        inbox_read = threading.Event()
        release = threading.Event()
        seen: Dict[str, Any] = {}

        def start_and_hang(ctx: TurnContext, prompt: str) -> None:
            call(ctx.socket, "mail_agents")
            call(ctx.socket, "mail_inbox")  # empty: the mails below arrive during this turn
            inbox_read.set()
            release.wait(10.0)

        def read_everything(ctx: TurnContext, prompt: str) -> None:
            inbox = call(ctx.socket, "mail_inbox")
            seen["ids"] = [m["id"] for m in inbox["messages"]]

        self.fake.queue("claude", start_and_hang, read_everything)
        self.start_driver()
        self.wait_records("claude", 1, "claude's start turn")
        wait_until(inbox_read.is_set, what="the start turn's inbox call")
        first = self.send(FIRST, "one", "first", "op-1")
        second = self.send(FIRST, "two", "second", "op-2")
        wait_until(lambda: {first["id"], second["id"]} <= set(self.spool("claude")),
                   what="both mails in the spool")
        release.set()

        self.wait_records("claude", 2, "the following turn")
        self.assertEqual(self.records("claude")[1]["prompt"], WAKE_LINE)
        wait_until(lambda: "ids" in seen, what="the continuation turn's inbox")
        self.assertEqual(seen["ids"], [first["id"], second["id"]])
        time.sleep(0.4)  # exactly one turn followed: both mails were read in it
        self.assertEqual(len(self.records("claude")), 2)
        states = self.states()
        self.assertEqual((states.get(first["id"]), states.get(second["id"])), ("acked", "acked"))
        self.assertEqual(self.spool("claude"), [])

    # -- 3: the lost cap exits the agent, the backoff rebinds -----------------------------------

    def test_lost_cap_exits_and_rebinds_after_the_backoff(self) -> None:
        self.fake.queue("claude", fail_turn("boom"), fail_turn("boom"), fail_turn("boom"))
        self.start_driver()
        self.wait_agent_down(FIRST, "lost_turns")
        self.assertEqual(sum(1 for r in self.records("claude") if not r["ok"]), 3)
        # after the backoff (0.2 s) the agent binds again at a new epoch and completes a start turn
        wait_until(lambda: self.agent_row(FIRST)["state"] == "ready"
                   and self.agent_row(FIRST)["epoch"] >= 4, what="the rebind after the backoff")
        self.wait_records("claude", 4, "the start turn of the rebound agent")
        self.assertEqual(self.records("claude")[3]["prompt"], START_LINE)
        self.assertTrue(self.records("claude")[3]["ok"])

    # -- 4: a resume that reports a new session rebinds fresh ----------------------------------

    def test_resume_new_session_rebinds_fresh(self) -> None:
        self.start_driver()
        self.wait_records("claude", 1, "claude's start turn")
        first_session = self.records("claude")[0]["session_id"]
        # a daemon restart in the middle: the stored session survives, the driver does not
        self.driver.stop()
        paths = self.sandbox_paths if self.sandbox_paths is not None else resident_open.DEFAULT_PATHS
        second = HeadlessDriver(runner=self.fake, tick=0.1, sweep_age=0.5, backoff=(0.2, 0.2, 0.2),
                                version_check=lambda ctx: AGY_VERSION, sandbox_paths=paths)
        self.extra_drivers.append(second)
        second.attach(self.mgr)

        self.fake.queue("claude", lambda ctx, prompt: TurnResult(ok=True, session_id="c-changed"))
        wait_until(lambda: len(self.records("claude")) >= 3, what="the resume and the fresh restart")
        resume, fresh = self.records("claude")[1], self.records("claude")[2]
        self.assertEqual(resume["prompt"], WAKE_LINE)       # kind resume: the stored session continues
        self.assertFalse(resume["first"])
        self.assertEqual(resume["session_id"], first_session)
        self.assertEqual(fresh["prompt"], START_LINE)       # resume_new_session: start card again
        self.assertTrue(fresh["first"])
        self.assertNotEqual(fresh["session_id"], first_session)
        self.assertEqual(fresh["epoch"], resume["epoch"] + 1)
        self.assertGreater(resume["epoch"], self.records("claude")[0]["epoch"])
        wait_until(lambda: self.agent_row(FIRST)["epoch"] >= fresh["epoch"], what="the hub at the new epoch")

    # -- 5: stalled turns sweep at sweep_age, not in a tight loop, and are capped ---------------

    def test_stalled_turns_sweep_slowly_and_are_capped(self) -> None:
        # the default script ends every turn ok without reading: every turn with mail stalls
        self.start_driver()
        self.wait_records("claude", 1, "claude's start turn")
        sent = self.send(FIRST, "work", "do it", "op-1")
        self.wait_records("claude", 2, "the wake turn that reads nothing")   # stalled = 1
        self.wait_records("claude", 3, "the first sweep")                   # stalled = 2
        self.wait_records("claude", 4, "the second sweep")                  # stalled = 3
        self.wait_agent_down(FIRST, "stalled_turns")
        wake, sweep1, sweep2 = (record["at"] for record in self.records("claude")[1:4])
        # one turn per sweep_age window since the stall began, never a tight loop
        self.assertGreaterEqual(sweep1 - wake, 0.8 * self.driver.sweep_age)
        self.assertGreaterEqual(sweep2 - sweep1, 0.8 * self.driver.sweep_age)
        self.assertEqual(len([r for r in self.records("claude") if r["at"] <= sweep2]), 4)
        # the stalled turns were ok, so the mail was never acknowledged
        self.assertTrue(all(r["ok"] for r in self.records("claude")[:4]))
        self.assertIn(sent["id"], self.spool("claude"))

    # -- 5b: rebinds of lost sessions -----------------------------------------------------------

    def test_a_lost_first_claude_turn_never_reuses_its_session_id(self) -> None:
        self.fake.queue("claude", fail_turn("boom"), ok_turn())
        self.start_driver()
        self.wait_records("claude", 2, "the retry after the lost first turn")
        first, second = self.records("claude")[:2]
        self.assertTrue(first["first"] and second["first"])
        self.assertNotEqual(first["session_id"], second["session_id"])  # a new uuid, never the same
        self.assertEqual(second["epoch"], first["epoch"] + 1)           # a fresh incarnation
        self.assertEqual(second["prompt"], START_LINE)

    def test_missing_session_rebinds_free_once_then_counts(self) -> None:
        def missing(ctx: TurnContext, prompt: str) -> TurnResult:
            return TurnResult(ok=False, error="gone", missing_session=True)

        self.fake.queue("claude", missing, missing, missing, missing)
        self.start_driver()
        self.wait_agent_down(FIRST, "lost_turns")
        # the first missing rebind in a row is free: the cap needs 1 + lost_cap failed turns
        self.assertEqual(sum(1 for r in self.records("claude") if not r["ok"]), 4)
        self.assertEqual(len({r["epoch"] for r in self.records("claude")[:4]}), 4)  # a rebind each time

    # -- 6: the agy home ------------------------------------------------------------------------

    def agent_dir(self, name: str) -> Path:
        return self.dir / "agents" / name

    def test_agy_home_seed_files_token_and_hook(self) -> None:
        self.start_driver()
        self.wait_records("agy", 1, "agy's start turn")
        home = self.agent_dir("agy") / "home"
        seed = [".gemini/config/mcp_config.json", ".gemini/config/hooks.json",
                ".gemini/antigravity-cli/settings.json", ".gemini/antigravity-cli/cache/onboarding.json"]
        for rel in seed:
            path = home / rel
            self.assertTrue(path.is_file(), path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        # the MCP config points both servers at this incarnation's socket
        sockets = list(self.agent_dir("agy").glob("e*.sock"))
        self.assertEqual(len(sockets), 1)
        config = json.loads((home / ".gemini/config/mcp_config.json").read_text())
        self.assertEqual(sorted(config["mcpServers"]), ["mailbox", "repo"])
        here = Path(resident_driver.__file__).resolve().parent
        for key, script in (("mailbox", "mailbox_mcp.py"), ("repo", "resident_repo_mcp.py")):
            self.assertEqual(config["mcpServers"][key]["command"], sys.executable)
            self.assertEqual(config["mcpServers"][key]["args"],
                             [str(here / script), "--socket", str(sockets[0])])
        # the permissions allow exactly the mailbox and repo tools and trust the workdir
        settings = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
        self.assertEqual(settings["permissions"]["allow"],
                         [f"mcp(mailbox/{tool})" for tool in MAILBOX_TOOLS]
                         + [f"mcp(repo/{tool})" for tool in SNAPSHOT_TOOLS])
        self.assertEqual(settings["trustedWorkspaces"], [str(self.agent_dir("agy") / "work")])
        onboarding = json.loads((home / ".gemini/antigravity-cli/cache/onboarding.json").read_text())
        self.assertEqual(sorted(onboarding), ["consumerOnboardingComplete", "enterpriseOnboardingComplete",
                                              "onboardingComplete"])
        # the token is a symlink to the node user's own token, never a copy
        link = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)),
                         str(self.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"))
        # hook.py: closed to the mailbox and repo MCP servers, fail closed on anything else
        hook = self.agent_dir("agy") / "hook.py"
        self.assertEqual(stat.S_IMODE(os.stat(hook).st_mode), 0o700)
        self.assertEqual((hook).read_text(), HOOK_PY_SOURCE)
        deny_reason = '{"decision": "deny", "reason": "closed: only mailbox and repo MCP tools"}'
        deny = {"toolCall": {"name": "write_file", "args": {"path": "/etc/passwd"}}}
        other_server = {"toolCall": {"name": "call_mcp_tool",
                                     "args": {"ServerName": "shell", "toolName": "run"}}}
        allow_mail = {"toolCall": {"name": "call_mcp_tool",
                                   "args": {"ServerName": "mailbox", "toolName": "mail_inbox"}}}
        allow_repo = {"toolCall": {"name": "call_mcp_tool",
                                   "args": {"ServerName": "repo", "toolName": "repo_read"}}}
        self.assertEqual(self.hook_output(hook, "PreToolUse", json.dumps(deny)), deny_reason)
        self.assertEqual(self.hook_output(hook, "PreToolUse", json.dumps(other_server)), deny_reason)
        self.assertEqual(self.hook_output(hook, "PreToolUse", json.dumps(allow_mail)),
                         '{"decision": "allow"}')
        self.assertEqual(self.hook_output(hook, "PreToolUse", json.dumps(allow_repo)),
                         '{"decision": "allow"}')
        self.assertEqual(self.hook_output(hook, "PreToolUse", "not json"), deny_reason)
        self.assertEqual(self.hook_output(hook, "SessionStart", json.dumps({"x": 1})), "{}")

    @staticmethod
    def hook_output(hook: Path, label: str, payload: str) -> str:
        proc = subprocess.run([sys.executable, str(hook), label], input=payload, capture_output=True,
                              text=True, timeout=30)
        return (proc.stdout or "").strip()

    def test_changed_hooks_json_refuses_the_next_turn(self) -> None:
        self.start_driver()
        self.wait_records("agy", 1, "agy's start turn")
        hooks = (self.agent_dir("agy") / "home" / ".gemini" / "config" / "hooks.json")
        hooks.write_text("{}\n", encoding="utf-8")
        sent = self.send(SECOND, "work", "do it", "op-1")
        # the turn is admitted and then refused by the pre-turn check, before any model runs
        wait_until(lambda: any(name == "agy" and "hooks.json" in error
                               for name, error in self.driver.turn_errors),
                   what="the lost turn that names the hooks.json check")
        wait_until(lambda: any(event.get("kind") == "turn_ended" and event.get("agent") == SECOND
                               and (event.get("detail") or {}).get("end_reason") == "lost"
                               for event in mail_store.MailStore().events(limit=100)),
                   what="the lost turn on the hub")
        self.assertEqual(len(self.records("agy")), 1)             # no model turn ran
        self.assertIn(sent["id"], self.spool("agy"))              # and the mail stays unread

    def test_reordered_settings_json_passes_the_seed_check(self) -> None:
        # agy rewrites settings.json after its first turn: same value, other key order and indent
        rel = ".gemini/antigravity-cli/settings.json"
        expected = resident_driver.agy_seed_texts(self.dir / "a", "/w", "/s.sock")[rel]
        value = json.loads(expected)
        reordered = json.dumps({"permissions": value["permissions"],
                                "trustedWorkspaces": value["trustedWorkspaces"]}, indent=4)
        self.assertNotEqual(reordered, expected)
        self.assertTrue(resident_driver._seed_matches(rel, reordered, expected))
        # any change of content still fails closed
        more_allowed = json.loads(expected)
        more_allowed["permissions"]["allow"].append("mcp(shell/run)")
        self.assertFalse(resident_driver._seed_matches(rel, json.dumps(more_allowed), expected))
        more_trusted = json.loads(expected)
        more_trusted["trustedWorkspaces"].append("/")
        self.assertFalse(resident_driver._seed_matches(rel, json.dumps(more_trusted), expected))
        extra_key = dict(json.loads(expected), mode="yolo")
        self.assertFalse(resident_driver._seed_matches(rel, json.dumps(extra_key), expected))
        self.assertFalse(resident_driver._seed_matches(rel, expected + "}", expected))
        # hooks.json stays byte for byte: a reordered copy is a change
        hooks = ".gemini/config/hooks.json"
        hooks_text = resident_driver.agy_seed_texts(self.dir / "a", "/w", "/s.sock")[hooks]
        self.assertFalse(resident_driver._seed_matches(hooks, json.dumps(json.loads(hooks_text)), hooks_text))

    def test_a_rewritten_settings_json_does_not_lose_the_next_turn(self) -> None:
        self.start_driver()
        self.wait_records("agy", 1, "agy's start turn")
        settings = self.agent_dir("agy") / "home" / ".gemini" / "antigravity-cli" / "settings.json"
        value = json.loads(settings.read_text(encoding="utf-8"))
        settings.write_text(json.dumps({"permissions": value["permissions"],
                                        "trustedWorkspaces": value["trustedWorkspaces"]}, indent=2),
                            encoding="utf-8")
        self.send(SECOND, "work", "do it", "op-1")
        self.wait_records("agy", 2, "agy's second turn")
        self.assertEqual([error for name, error in self.driver.turn_errors if name == "agy"], [])

    def test_every_lost_turn_is_logged_with_its_reason(self) -> None:
        self.start_driver()
        self.wait_records("agy", 1, "agy's start turn")
        hooks = self.agent_dir("agy") / "home" / ".gemini" / "config" / "hooks.json"
        with patch.object(resident_driver, "log") as logged:
            hooks.write_text("{}\n", encoding="utf-8")
            self.send(SECOND, "work", "do it", "op-1")
            wait_until(lambda: any("lost (agy seed file changed: .gemini/config/hooks.json)" in str(c)
                                   for c in logged.call_args_list),
                       what="the log line that names the lost turn's reason")

    # -- 7: the pinned agy version and the closed claude argv -----------------------------------

    def test_agy_version_matches_the_provider_table(self) -> None:
        table = json.loads((ROOT / "configs" / "resident-providers.json").read_text(encoding="utf-8"))
        self.assertEqual(AGY_VERSION, table["providers"]["agy"]["version"])
        self.assertEqual(resident_driver.expected_agy_version(), table["providers"]["agy"]["version"])

    def test_agy_env_disables_the_auto_updater(self) -> None:
        runner = resident_driver.CliRunner()
        ctx = TurnContext(name="agy", address=SECOND, provider="agy", epoch=3,
                          socket="/tmp/nowhere.sock", session_id=None, first=True, card="",
                          workdir="/tmp", agent_dir="/tmp/agent")
        env = runner.agy_env(ctx)
        # agy honours only the exact lowercase "true"; "1" left the updater on (1.5.9 -> 1.5.12).
        self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertEqual(env["HOME"], "/tmp/agent/home")

    def test_claude_argv_is_closed_to_the_mailbox_and_repo_mcp(self) -> None:
        runner = resident_driver.CliRunner()
        ctx = TurnContext(name="claude", address=FIRST, provider="claude", epoch=3,
                          socket="/tmp/nowhere.sock", session_id="s-1", first=False, card="card text",
                          workdir="/tmp", agent_dir="/tmp")
        argv = runner.claude_argv(ctx, "/tmp/mcp.json")
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        # the allowed tools are derived: every mailbox tool plus every repo snapshot tool
        allowed = allowed_tool_names() + resident_driver.repo_tool_names
        self.assertEqual(resident_driver.repo_tool_names,
                         [f"mcp__repo__{tool}" for tool in SNAPSHOT_TOOLS])
        self.assertEqual(argv[argv.index("--allowedTools") + 1], ",".join(allowed))
        self.assertEqual(argv[argv.index("--allowedTools") + 1].split(","), allowed)
        self.assertEqual(len(allowed), len(allowed_tool_names()) + len(SNAPSHOT_TOOLS))
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "card text")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "/tmp/mcp.json")
        self.assertEqual(argv[argv.index("--resume") + 1], "s-1")
        self.assertFalse(any(flag.startswith("--dangerously") for flag in argv), argv)
        # a first turn starts the session instead of resuming it
        fresh = TurnContext(name="claude", address=FIRST, provider="claude", epoch=1,
                            socket="/tmp/nowhere.sock", session_id="s-new", first=True, card="card",
                            workdir="/tmp", agent_dir="/tmp")
        argv = runner.claude_argv(fresh, "/tmp/mcp.json")
        self.assertEqual(argv[argv.index("--session-id") + 1], "s-new")
        self.assertNotIn("--resume", argv)
        # the MCP config holds exactly the mailbox and repo servers on this incarnation's socket
        with tempfile.TemporaryDirectory() as scratch:
            path = runner._mcp_config(ctx, Path(scratch))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(config["mcpServers"]), ["mailbox", "repo"])
            here = Path(resident_driver.__file__).resolve().parent
            for key, script in (("mailbox", "mailbox_mcp.py"), ("repo", "resident_repo_mcp.py")):
                server = config["mcpServers"][key]
                self.assertEqual(server["command"], sys.executable)
                self.assertEqual(server["args"], [str(here / script), "--socket", "/tmp/nowhere.sock"])
                self.assertEqual(sorted(server["env"]), sorted(
                    key for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ))


class OpenAgentEndToEnd(HeadlessDriverEndToEnd):
    """Open agents (docs/design/resident-open-tools.md, O2) end to end: the same harness, with the
    claude entry declared tools: "open" against a temp sandbox layout (SandboxPaths injected - the
    root uid and the gsa-<name> user and group are the test's own) and resident_open.stop faked, so
    no sudo and no sandbox unit ever runs. agy stays closed in the same harness."""

    VERSION = "2747b475ef4a"

    def prepare_open_sandbox(self) -> None:
        sandbox = self.dir / "sandbox"
        sandbox.mkdir()
        for name in ("guardian-sandbox-run", "launch.py"):
            path = sandbox / name
            path.write_text("#!/bin/sh\n# a stand-in for the root-installed file\n", encoding="utf-8")
            os.chmod(path, 0o755)
        installed = sandbox / "installed.json"
        installed.write_text(json.dumps(
            {"version": self.VERSION, "agents": ["claude"],
             "cli": resident_open.expected_cli_versions()}), encoding="utf-8")
        sock_root = sandbox / "sock"
        sock_root.mkdir()
        (sock_root / "claude").mkdir(mode=0o2750)
        os.chown(sock_root / "claude", -1, os.getgid())  # BSD mkdir takes the parent's group
        self.sandbox_paths = resident_open.SandboxPaths(
            wrapper=sandbox / "guardian-sandbox-run", launcher=sandbox / "launch.py",
            installed=installed, sock_root=sock_root, home_root=self.dir / "home",
            root_uid=os.getuid(), daemon_uid=os.getuid,
            agent_gid=lambda name: os.getgid(), member_gids=lambda: [os.getgid()])
        self.open_extra: Dict[str, Any] = {"tools": "open", "sandbox": {"version": self.VERSION}}
        self.stop_ok = True
        self.stop_calls: List[str] = []
        real_spec = mail_relay.agent_spec

        def open_spec(entry: Dict[str, Any], table: Dict[str, Any], peers: Any = None,
                      **kwargs: Any) -> Dict[str, Any]:
            out = real_spec(entry, table, peers=peers, **kwargs)
            if entry.get("name") == "claude":
                out.update(self.open_extra)  # the hub brief's C6 fields, injected for this harness
            return out

        spec_patch = patch.object(mail_relay, "agent_spec", open_spec)
        spec_patch.start()
        self.addCleanup(spec_patch.stop)

        def fake_stop(name: str, paths: Any = None, run: Any = None) -> bool:
            self.stop_calls.append(name)
            return self.stop_ok

        stop_patch = patch.object(resident_open, "stop", fake_stop)
        stop_patch.start()
        self.addCleanup(stop_patch.stop)

    # -- helpers --------------------------------------------------------------------------------

    def refused(self, break_it: Callable[[], None], reason: str) -> None:
        """One broken-sandbox turn: the start turn ran, `break_it` breaks the sandbox, mail arrives
        and the next turn must be refused without any process, exiting the agent with the reason."""
        self.start_driver()
        self.wait_records("claude", 1, "the open start turn")
        break_it()
        sent = self.send(FIRST, "work", "do it", "op-9")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("no process may start")):
            wait_until(lambda: any(name == "claude" and error == reason
                                   for name, error in self.driver.turn_errors),
                       what=f"the refused turn ({reason})")
            self.wait_agent_down(FIRST, reason)
        self.assertEqual(len(self.records("claude")), 1)   # no model turn ran at all
        self.assertIn(sent["id"], self.spool("claude"))    # and the mail stays unread

    def rewrite_installed(self, **changes: Any) -> None:
        record = json.loads(self.sandbox_paths.installed.read_text(encoding="utf-8"))
        record.update(changes)
        self.sandbox_paths.installed.write_text(json.dumps(record), encoding="utf-8")

    # -- the happy path -------------------------------------------------------------------------

    def test_an_open_agent_binds_in_the_sandbox_and_runs_its_turns(self) -> None:
        self.start_driver()
        self.wait_records("claude", 1, "the open start turn")
        record = self.records("claude")[0]
        self.assertEqual(record["tools"], "open")
        sock_dir = self.sandbox_paths.sock_dir("claude")
        self.assertEqual(Path(record["socket"]).parent, sock_dir)      # C7: the sandbox socket
        self.assertEqual(list(sock_dir.glob("e*.sock")), [Path(record["socket"])])
        mcp = Path(record["mcp_dir"])
        self.assertEqual(mcp.parent, sock_dir / "mcp")                 # C7: the MCP copy
        self.assertTrue((mcp / "mailbox_mcp.py").is_file())
        self.assertTrue((mcp / "resident_repo_mcp.py").is_file())
        self.assertIn("claude", self.stop_calls)                       # C4: stop before every turn

        def answer(ctx: TurnContext, prompt: str) -> None:
            inbox = call(ctx.socket, "mail_inbox")
            self.assertEqual([message["subject"] for message in inbox["messages"]], ["work"])

        self.fake.queue("claude", answer)
        sent = self.send(FIRST, "work", "do it", "op-1")
        self.wait_records("claude", 2, "the wake turn over the sandbox socket")
        wait_until(lambda: self.states().get(sent["id"]) == "acked", what="the mail acked")

    # -- every open_problem reason refuses the turn without a process ----------------------------

    def test_a_missing_sandbox_refuses_the_turn(self) -> None:
        self.refused(lambda: self.sandbox_paths.wrapper.unlink(), "sandbox_missing")

    def test_a_version_mismatch_refuses_the_turn(self) -> None:
        self.refused(lambda: self.rewrite_installed(version="0f0f0f0f0f0f"),
                     "sandbox_version_mismatch")

    def test_a_missing_agent_entry_refuses_the_turn(self) -> None:
        self.refused(lambda: self.rewrite_installed(agents=[]), "sandbox_agent_missing")

    def test_a_missing_cli_copy_refuses_the_turn(self) -> None:
        self.refused(lambda: self.rewrite_installed(cli={}), "sandbox_cli_missing")

    def test_a_wrong_socket_directory_owner_refuses_the_turn(self) -> None:
        def break_it() -> None:
            self.sandbox_paths.daemon_uid = lambda: os.getuid() + 4000
        self.refused(break_it, "sandbox_sock_dir")

    def test_a_missing_group_membership_refuses_the_turn(self) -> None:
        def break_it() -> None:
            self.sandbox_paths.member_gids = lambda: [os.getgid() + 4000]
        self.refused(break_it, "sandbox_group")

    def test_a_blocked_spec_refuses_the_turn(self) -> None:
        def break_it() -> None:
            self.open_extra["blocked"] = "hub_said_no"
            path = resident_registry.declaration_path()
            entries = json.loads(path.read_text(encoding="utf-8"))
            entries["agents"][0]["peers"] = ["agy@node-1"]  # a signature change: the hub resends
            path.write_text(json.dumps(entries), encoding="utf-8")
            state = self.dir / "agents" / "claude" / "state.json"
            wait_until(lambda: "hub_said_no" in state.read_text(encoding="utf-8"),
                       what="the blocked spec to reach the node")
        self.refused(break_it, "hub_said_no")

    def test_a_spec_turned_closed_refuses_the_turn_bound_in_the_sandbox(self) -> None:
        def break_it() -> None:
            self.open_extra.clear()  # the hub now sends a closed spec for the same epoch
            path = resident_registry.declaration_path()
            entries = json.loads(path.read_text(encoding="utf-8"))
            entries["agents"][0]["peers"] = ["agy@node-1"]  # a signature change: the hub resends
            path.write_text(json.dumps(entries), encoding="utf-8")
            state = self.dir / "agents" / "claude" / "state.json"
            wait_until(lambda: '"open"' not in state.read_text(encoding="utf-8"),
                       what="the closed spec to reach the node")
        self.refused(break_it, "open_spec_changed")

    def test_a_failed_stop_refuses_the_turn_and_starts_nothing(self) -> None:
        self.start_driver()
        self.wait_records("claude", 1, "the open start turn")
        self.stop_ok = False
        sent = self.send(FIRST, "work", "do it", "op-1")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("no process may start")):
            wait_until(lambda: any(name == "claude" and error == "sandbox_stop_failed"
                                   for name, error in self.driver.turn_errors),
                       what="the stop failure to refuse the turn")
            self.wait_agent_down(FIRST, "sandbox_stop_failed")
        self.assertEqual(len(self.records("claude")), 1)
        self.assertIn(sent["id"], self.spool("claude"))

    def test_a_launcher_refusal_exits_the_agent_with_the_sandbox_reason(self) -> None:
        self.fake.queue("claude", ok_turn(), lambda ctx, prompt: TurnResult(
            ok=False, error="the sandbox launcher refused the turn", refusal="sandbox_no_credential"))
        self.start_driver()
        self.wait_records("claude", 1, "the open start turn")
        sent = self.send(FIRST, "work", "do it", "op-1")
        self.wait_records("claude", 2, "the wake turn whose launcher refused")
        self.assertFalse(self.records("claude")[1]["ok"])
        self.wait_agent_down(FIRST, "sandbox_no_credential")
        self.assertIn(sent["id"], self.spool("claude"))  # the refused turn ended lost

    def test_a_closed_agent_of_the_same_fleet_never_touches_the_sandbox(self) -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the closed path never builds a sandbox command")

        with patch.object(resident_open, "turn_command", boom), \
                patch.object(resident_open, "launch_input", boom):
            self.start_driver()
            self.wait_records("agy", 1, "the closed start turn of agy")
            self.wait_records("claude", 1, "the open start turn of claude")
        self.assertEqual([record["tools"] for record in self.records("agy")], [None])
        self.assertEqual(set(self.stop_calls), {"claude"})  # only the open agent stops units


class FakeProc:
    """A Popen stand-in whose first communicate times out (so _run_open_process kills and stops)."""

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> Optional[int]:
        return None

    def kill(self) -> None:
        self.killed = True

    def communicate(self, input: Any = None, timeout: Any = None) -> Any:
        if not self.killed:
            raise subprocess.TimeoutExpired("sudo", timeout)
        return b"", b""


class OpenRunnerTests(unittest.TestCase):
    """The runner's open-turn shapes (O2): the argv of both providers, the launcher files, the C2
    frame, the wrapper command and the refusal parsing. No process runs: the one process call is
    faked, and the rest never reaches the plumbing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sgu", dir="/tmp"))  # short: unix socket path limits
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = resident_open.SandboxPaths(
            wrapper=self.tmp / "run", launcher=self.tmp / "launch.py",
            installed=self.tmp / "installed.json", sock_root=self.tmp / "sock",
            home_root=self.tmp / "home")
        self.runner = resident_driver.CliRunner(sandbox_paths=self.paths)
        self.mcp = self.tmp / "sock" / "claude" / "mcp" / "abc123def456"
        self.socket = str(self.tmp / "sock" / "claude" / "e3.sock")

    def ctx(self, provider: str = "claude", **over: Any) -> TurnContext:
        base: Dict[str, Any] = dict(
            name="claude", address=FIRST, provider=provider, epoch=3, socket=self.socket,
            session_id="s-1", first=False, card="card text",
            workdir=str(self.tmp / "home" / "gsa-claude" / "work"), agent_dir=str(self.tmp / "agent"),
            spec={"tools": "open", "sandbox": {"version": "v"}}, mcp_dir=str(self.mcp))
        base.update(over)
        return TurnContext(**base)

    def fake_process(self, code: int, out: str, err: str) -> Tuple[List[Dict[str, Any]], Any]:
        calls: List[Dict[str, Any]] = []

        def fake(name: str, argv: List[str], payload: bytes, timeout: float) -> Any:
            calls.append({"name": name, "argv": list(argv), "payload": payload, "timeout": timeout})
            return code, out, err, None

        return calls, fake

    def test_open_claude_argv_keeps_the_tools_and_pins_the_mcp_config(self) -> None:
        argv = self.runner.open_claude_argv(self.ctx())
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertNotIn("--tools", argv)      # the tools stay: the sandbox holds (O2)
        self.assertNotIn("--allowedTools", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1],
                         str(self.tmp / "home" / "gsa-claude" / ".guardian" / "turn" / "mcp.json"))
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "card text")
        self.assertEqual(argv[argv.index("--resume") + 1], "s-1")
        fresh = self.runner.open_claude_argv(self.ctx(first=True, session_id="s-new"))
        self.assertEqual(fresh[fresh.index("--session-id") + 1], "s-new")

    def test_open_mcp_config_points_both_servers_at_the_copy_and_the_open_socket(self) -> None:
        config = self.runner.open_mcp_config(self.ctx())
        self.assertEqual(sorted(config["mcpServers"]), ["mailbox", "repo"])
        for key, script, extra in (("mailbox", "mailbox_mcp.py", ["--worktrees"]),
                                   ("repo", "resident_repo_mcp.py", [])):
            server = config["mcpServers"][key]
            self.assertEqual(server, {"type": "stdio", "command": "/usr/bin/python3",
                                      "args": ["-E", "-s", str(self.mcp / script),
                                               "--socket", self.socket, *extra]}, key)
            self.assertNotIn("env", server)  # the launcher builds the sandbox environment (C2)
        # O3: only the open mailbox server runs with --worktrees; the closed configs carry none
        closed = self.runner._mcp_config(self.ctx(), Path(self.tmp))
        self.assertEqual(closed.read_text(encoding="utf-8").count("--worktrees"), 0)

    def test_open_agy_files_have_no_hook_no_token_and_no_permission_list(self) -> None:
        files = self.runner.open_agy_files(self.ctx("agy"))
        self.assertEqual(sorted(files), [".gemini/antigravity-cli/cache/onboarding.json",
                                         ".gemini/antigravity-cli/settings.json",
                                         ".gemini/config/mcp_config.json"])
        settings = json.loads(files[".gemini/antigravity-cli/settings.json"])
        self.assertEqual(settings,
                         {"trustedWorkspaces": [str(self.tmp / "home" / "gsa-claude" / "work")]})
        onboarding = json.loads(files[".gemini/antigravity-cli/cache/onboarding.json"])
        self.assertEqual(onboarding, resident_driver.AGY_ONBOARDING)
        config = json.loads(files[".gemini/config/mcp_config.json"])
        self.assertEqual(config["mcpServers"]["mailbox"],
                         {"command": "/usr/bin/python3",
                          "args": ["-E", "-s", str(self.mcp / "mailbox_mcp.py"),
                                   "--socket", self.socket, "--worktrees"]})
        self.assertEqual(config["mcpServers"]["repo"],
                         {"command": "/usr/bin/python3",
                          "args": ["-E", "-s", str(self.mcp / "resident_repo_mcp.py"),
                                   "--socket", self.socket]})

    def test_run_routes_an_open_spec_to_the_sandbox_and_a_closed_one_away_from_it(self) -> None:
        with patch.object(self.runner, "_run_open", return_value=TurnResult(True)) as open_run:
            self.assertTrue(self.runner.run(self.ctx(), "p").ok)
            open_run.assert_called_once()
        closed = self.ctx(spec={})
        with patch.object(self.runner, "_run_open", side_effect=AssertionError("never sandboxed")):
            with patch.object(self.runner, "_run_claude", return_value=TurnResult(True)) as closed_run:
                self.assertTrue(self.runner.run(closed, "p").ok)
                closed_run.assert_called_once()

    def test_run_open_builds_the_c2_frame_and_the_wrapper_command_for_claude(self) -> None:
        out = json.dumps({"type": "result", "subtype": "success", "session_id": "s-9",
                          "result": "done"}) + "\n"
        calls, fake = self.fake_process(0, out, "")
        with patch.object(self.runner, "_run_open_process", side_effect=fake):
            result = self.runner._run_open(self.ctx(), "the prompt", 900.0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.session_id, "s-9")
        call = calls[0]
        self.assertEqual(call["name"], "claude")
        wrapper_timeout = int(900.0 + resident_open.WRAPPER_SLACK)
        self.assertEqual(call["argv"],
                         resident_open.turn_command("claude", wrapper_timeout, self.paths))
        self.assertEqual(call["timeout"], float(wrapper_timeout) + 30.0)
        frame = json.loads(call["payload"].decode("utf-8"))
        versions = resident_open.expected_cli_versions()
        self.assertEqual((frame["provider"], frame["version"], frame["stdin"]),
                         ("claude", versions["claude"], "the prompt"))
        self.assertEqual(frame["argv"], self.runner.open_claude_argv(self.ctx()))
        self.assertEqual(sorted(frame["files"]), [".guardian/turn/mcp.json"])
        self.assertEqual(json.loads(frame["files"][".guardian/turn/mcp.json"]),
                         self.runner.open_mcp_config(self.ctx()))

    def test_run_open_of_agy_sends_the_stream_json_line_and_its_files(self) -> None:
        out = json.dumps({"event": "result", "result": {"status": "SUCCESS",
                                                        "conversation_id": "c-7",
                                                        "response": "hello"}}) + "\n"
        calls, fake = self.fake_process(0, out, "")
        with patch.object(self.runner, "_run_open_process", side_effect=fake):
            result = self.runner._run_open(self.ctx("agy"), "the prompt", 900.0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.session_id, "c-7")
        frame = json.loads(calls[0]["payload"].decode("utf-8"))
        self.assertEqual(frame["provider"], "agy")
        self.assertEqual(frame["stdin"],
                         json.dumps({"event": "user", "message": {"content": "the prompt"}}) + "\n")
        self.assertEqual(frame["argv"], self.runner.agy_argv(self.ctx("agy"), 900.0))
        self.assertEqual(sorted(frame["files"]), [".gemini/antigravity-cli/cache/onboarding.json",
                                                  ".gemini/antigravity-cli/settings.json",
                                                  ".gemini/config/mcp_config.json"])

    def test_open_codex_argv_and_run_open_fresh_and_resume(self) -> None:
        fresh_ctx = self.ctx("codex", first=True, session_id=None)
        fresh_argv = self.runner.open_codex_argv(fresh_ctx)
        self.assertEqual(fresh_argv[:2], ["codex", "exec"])
        self.assertNotIn("resume", fresh_argv[:4])
        self.assertIn("--json", fresh_argv)
        self.assertIn('sandbox_mode="danger-full-access"', fresh_argv)
        self.assertTrue(any("mcp_servers.mailbox.args=" in a and "--worktrees" in a for a in fresh_argv))
        self.assertTrue(any("mcp_servers.repo.args=" in a for a in fresh_argv))

        resume_ctx = self.ctx("codex", first=False, session_id="th-42")
        resume_argv = self.runner.open_codex_argv(resume_ctx)
        self.assertEqual(resume_argv[:3], ["codex", "exec", "resume"])
        self.assertEqual(resume_argv[-2:], ["th-42", "-"])

        out = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-99"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "done"}}),
        ]) + "\n"
        calls, fake = self.fake_process(0, out, "")
        with patch.object(self.runner, "_run_open_process", side_effect=fake):
            result = self.runner._run_open(fresh_ctx, "the prompt", 900.0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.session_id, "th-99")
        frame = json.loads(calls[0]["payload"].decode("utf-8"))
        self.assertEqual(frame["provider"], "codex")
        self.assertEqual(frame["version"], resident_open.expected_cli_versions()["codex"])
        self.assertEqual(frame["files"], {})
        self.assertEqual(frame["stdin"], "the prompt")

        calls2, fake2 = self.fake_process(1, "", "Error: thread not found\n")
        with patch.object(self.runner, "_run_open_process", side_effect=fake2):
            missing = self.runner._run_open(resume_ctx, "hi", 900.0)
        self.assertFalse(missing.ok)
        self.assertTrue(missing.missing_session)

    def test_pass_one_allows_open_codex_and_skips_closed_codex(self) -> None:
        class _StubMgr:
            def __init__(self, infos: Dict[str, Dict[str, Any]]) -> None:
                self._infos = infos
                self.root = "/tmp"
            def agent_info(self, name: str) -> Optional[Dict[str, Any]]:
                return self._infos.get(name)

        mgr = _StubMgr({
            "codex_closed": {"spec": {"provider": "codex", "driver": "headless"}, "spec_version": 1},
            "codex_open": {"spec": {"provider": "codex", "driver": "headless", "tools": "open"},
                           "spec_version": 1, "ready": False, "epoch": 0},
        })
        driver = resident_driver.HeadlessDriver()
        driver.manager = mgr
        bound: List[str] = []
        with patch.object(driver, "_bind", side_effect=lambda name, *_: bound.append(name)):
            driver._pass_one("codex_closed", 1.0)
            driver._pass_one("codex_open", 1.0)
        self.assertEqual(bound, ["codex_open"])

    def test_a_launcher_exit_three_becomes_a_sandbox_refusal(self) -> None:
        calls, fake = self.fake_process(3, "", "launch: no_credential\n")
        with patch.object(self.runner, "_run_open_process", side_effect=fake):
            result = self.runner._run_open(self.ctx(), "p", 900.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal, "sandbox_no_credential")
        calls, fake = self.fake_process(3, "", "launch: cli_missing\n")
        with patch.object(self.runner, "_run_open_process", side_effect=fake):
            result = self.runner._run_open(self.ctx(), "p", 900.0)
        self.assertEqual(result.refusal, "sandbox_cli_missing")
        calls, fake = self.fake_process(1, "", "boom")
        with patch.object(self.runner, "_run_open_process", side_effect=fake):
            result = self.runner._run_open(self.ctx(), "p", 900.0)
        self.assertIsNone(result.refusal)          # an ordinary failure stays a lost turn
        self.assertIn("code 1", result.error)

    def test_a_timed_out_open_turn_stops_the_units_after_the_kill(self) -> None:
        proc = FakeProc()
        with patch.object(resident_driver.subprocess, "Popen", return_value=proc), \
                patch.object(self.runner, "_stop_sandbox") as stopped:
            code, out, err, failure = self.runner._run_open_process("claude", ["sudo"], b"{}", 5.0)
        self.assertIsNone(code)
        self.assertIn("timed out", failure or "")
        stopped.assert_called_once_with("claude")
        self.assertTrue(proc.killed)


class RepoToolNameTests(unittest.TestCase):
    """The repo tool names are derived from repo_mcp.SNAPSHOT_TOOLS so this run stays independent of
    resident_repo_mcp.py (the other M2e run creates it); when that module is present, the two must
    agree, or the closure a turn gets would not cover the tools the server serves."""

    def test_the_derived_names_match_the_resident_repo_mcp(self) -> None:
        try:
            import resident_repo_mcp
        except ImportError:
            self.skipTest("resident_repo_mcp.py is not in this tree (the other M2e run adds it)")
        self.assertEqual(resident_driver.REPO_SERVER_KEY, resident_repo_mcp.SERVER_KEY)
        self.assertEqual(resident_driver.repo_tool_names, resident_repo_mcp.allowed_tool_names())


if __name__ == "__main__":
    unittest.main()

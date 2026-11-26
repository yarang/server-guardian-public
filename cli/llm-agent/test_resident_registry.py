"""resident_registry: the declaration file, the registration checks, peers_changed and removal."""

import json
import os
import unittest
from unittest.mock import patch

import mail_store
import resident_registry
import sandbox_info
import testenv
from resident_registry import RegistrationError

# A stub of configs/resident-providers.json shaped like the real M0 table (claude and agy resident
# headless, the tmux driver unverified, codex refused). The resident budget figures live in
# configs/worker-requirements.json since M2a, not in the table's budget block.
TABLE = {
    "format": 1,
    "providers": {
        "claude": {"resident": True, "default_driver": "headless",
                   "drivers": {"headless": {"status": "pass"}, "tmux": {"status": "unverified"}}},
        "agy": {"resident": True, "default_driver": "headless",
                "drivers": {"headless": {"status": "pass"}}},
        "codex": {"resident": False, "open_resident": True, "open_driver": "headless",
                  "default_driver": None, "drivers": {}},
    },
}

CHARACTER = {"id": "resident-helper", "format": 2, "name": "Resident Helper",
             "seats": ["solo"], "persona": {"identity": "helper", "expertise": ["checks"],
                                            "stance": "stay read-only", "checks": ["look first"]}}
LEGACY_PERSONA = {"id": "legacy-persona", "system_prompt": "you are helpful"}


def server(node="node-1", clis=("claude", "agy"), memory=8192, worker=True, protocol=3,
           max_jobs=2):
    return {"id": node, "name": node, "hostname": f"{node}.example", "worker": worker,
            "worker_capable": {"passed": True, "max_jobs": max_jobs, "clis": list(clis),
                               "memory_free_mib": memory},
            "worker_install": {"protocol": protocol, "rev": "r1", "max_jobs": max_jobs}}


class Personas:
    """A stand-in for PersonaManager.get_persona."""

    def __init__(self, personas):
        self.personas = personas

    def get_persona(self, persona_id):
        return self.personas.get(persona_id)


def write_character_file():
    from persona_manager import custom_personas_dir
    directory = custom_personas_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{CHARACTER['id']}.json").write_text(json.dumps(CHARACTER), encoding="utf-8")


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)
        self.servers = [server()]

    def add(self, name="claude", node="node-1", provider="claude", **kwargs):
        kwargs.setdefault("servers", self.servers)
        kwargs.setdefault("table", TABLE)
        return resident_registry.add_agent(name, node, provider, **kwargs)

    def declarations(self):
        return json.loads(resident_registry.declaration_path().read_text(encoding="utf-8"))


class RegistrationChecks(RegistryTestCase):
    def test_bad_and_reserved_names(self):
        for name in ("Claude", "claude!", "x" * 40):
            with self.assertRaises(RegistrationError):
                self.add(name=name)
        with self.assertRaises(RegistrationError):
            self.add(name="operator")
        with self.assertRaises(RegistrationError):
            self.add(name="hub")

    def test_duplicate_on_node(self):
        self.add(name="claude")
        with self.assertRaises(RegistrationError):
            self.add(name="claude")

    def test_node_checks(self):
        with self.assertRaises(RegistrationError):  # not in the inventory
            self.add(node="node-9")
        with self.assertRaises(RegistrationError):  # worker false
            self.add(servers=[server(worker=False)])
        with self.assertRaises(RegistrationError):  # probe did not pass
            broken = server()
            broken["worker_capable"]["passed"] = False
            self.add(servers=[broken])
        with self.assertRaises(RegistrationError):  # no worker_install at all
            missing = server()
            del missing["worker_install"]
            self.add(servers=[missing])
        with self.assertRaises(RegistrationError) as caught:  # protocol 2
            self.add(servers=[server(protocol=2)])
        self.assertIn("protocol 3", str(caught.exception))

    def test_provider_checks(self):
        with self.assertRaises(RegistrationError):  # not installed on the node
            self.add(provider="gemini")
        with self.assertRaises(RegistrationError) as caught:  # refused by the table
            self.add(provider="codex", servers=[server(clis=("claude", "codex"))])
        self.assertIn("M0 table", str(caught.exception))
        with self.assertRaises(RegistrationError) as caught:  # unverified driver
            self.add(driver="tmux")
        self.assertIn("unverified", str(caught.exception))

    def test_character_checks(self):
        personas = Personas({"resident-helper": CHARACTER, "legacy-persona": LEGACY_PERSONA})
        with self.assertRaises(RegistrationError):  # unknown
            self.add(character="nope", personas=personas)
        with self.assertRaises(RegistrationError):  # format 1 persona, not a character
            self.add(character="legacy-persona", personas=personas)
        self.add(character="resident-helper", personas=personas)

    def test_character_from_the_custom_dir(self):
        write_character_file()
        entry = self.add(character="resident-helper")
        self.assertEqual(entry["character"], "resident-helper")

    def test_peers_checks(self):
        self.add(name="claude", node="node-1")
        self.add(name="claude", node="node-2", servers=[server(), server("node-2")])
        with self.assertRaises(RegistrationError):  # unknown peer
            self.add(name="agy", peers=["ghost@node-1"])
        with self.assertRaises(RegistrationError) as caught:  # bare name on two nodes
            self.add(name="agy", peers=["claude"])
        self.assertIn("ambiguous", str(caught.exception))
        entry = self.add(name="agy", peers=["claude@node-2"])
        self.assertEqual(entry["peers"], ["claude@node-2"])  # stored as full addresses

    def test_budget_checks(self):
        # The figures come from configs/worker-requirements.json (512 per instance, 512 per turn since M2a).
        self.add(name="fits", servers=[server(memory=4096)])  # 2*1536 + 1*512 + 512 = 4096, fits
        resident_registry.remove_agent("fits@node-1")
        with self.assertRaises(RegistrationError) as caught:  # one MiB short
            self.add(servers=[server(memory=4095)])
        self.assertIn("budget", str(caught.exception))
        with self.assertRaises(RegistrationError) as caught:  # no figure recorded
            bare = server()
            del bare["worker_capable"]["memory_free_mib"]
            self.add(servers=[bare])
        self.assertIn("re-probe", str(caught.exception))

    def test_budget_figures_come_from_the_worker_requirements(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker-requirements.json"
            path.write_text(json.dumps({"default_max_jobs": 2, "free_memory_mib_per_job": 1536,
                                        "resident_memory_mib": 1024, "resident_turn_memory_mib": 512}))
            with patch.object(resident_registry, "requirements_path", return_value=path):
                self.add(name="fits", servers=[server(memory=4608)])  # 2*1536 + 1*1024 + 512, fits
                resident_registry.remove_agent("fits@node-1")
                with self.assertRaises(RegistrationError) as caught:  # one MiB short
                    self.add(servers=[server(memory=4607)])
        self.assertIn("budget", str(caught.exception))

    def test_requirements_without_budget_figures_refuse(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker-requirements.json"
            path.write_text(json.dumps({"default_max_jobs": 2, "free_memory_mib_per_job": 1536}))
            with patch.object(resident_registry, "requirements_path", return_value=path):
                with self.assertRaises(RegistrationError) as caught:
                    self.add()
        self.assertIn("resident budget figure", str(caught.exception))

    def test_second_instance_counts_in_the_budget(self):
        self.add(name="claude")  # 2*1536 + 1*512 + 512 = 4096 of 8192
        self.add(name="agy")     # 2*1536 + 2*512 + 512 = 4608 of 8192
        with self.assertRaises(RegistrationError):  # 2*1536 + 3*512 + 512 = 5120 > 5000
            self.add(name="third", servers=[server(memory=5000)])
        self.add(name="third", servers=[server(memory=5120)])


class RegistrationHappyPath(RegistryTestCase):
    def test_add_writes_the_file_and_the_store(self):
        self.add(name="agy", provider="agy", peers=[])  # peers must be declared first
        entry = self.add(name="claude", peers=["agy"])  # a unique bare name resolves
        self.assertEqual(entry, {"name": "claude", "node": "node-1", "provider": "claude",
                                 "driver": "headless", "peers": ["agy@node-1"]})
        data = self.declarations()
        self.assertEqual(data["format"], 1)
        self.assertIn("modified", data)
        self.assertEqual(data["agents"][1], {"name": "claude", "node": "node-1",
                                             "provider": "claude", "driver": "headless",
                                             "peers": ["agy@node-1"]})
        rows = {r["address"]: r for r in mail_store.MailStore().agent_rows()}
        row = rows["claude@node-1"]
        self.assertEqual(row["state"], "down")
        self.assertEqual(row["epoch"], 0)
        self.assertIsNotNone(row["not_ready_since"])
        events = {e["kind"] for e in mail_store.MailStore().events()}
        self.assertIn("agent_added", events)

    def test_add_sends_peers_changed_to_existing_agents(self):
        self.add(name="claude")
        self.add(name="agy")
        store = mail_store.MailStore()
        notices = store.inbox_queued("claude@node-1")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["kind"], "status")
        self.assertEqual(notices[0]["subject"], "peers_changed")
        self.assertEqual(notices[0]["from_kind"], "hub")
        self.assertIn("agy@node-1", notices[0]["body"])
        self.assertEqual(store.inbox_queued("agy@node-1"), [])  # the new agent is not told

    def test_explicit_peers_not_notified_for_others(self):
        self.add(name="agy")
        self.add(name="claude", peers=["agy@node-1"])
        self.add(name="solo", node="node-2", servers=[server(), server("node-2")])
        # claude's explicit list did not change when solo was added, so it gets no notice
        self.assertEqual(mail_store.MailStore().inbox_queued("claude@node-1"), [])
        # agy's default list gained claude, then solo: one notice each
        notices = mail_store.MailStore().inbox_queued("agy@node-1")
        self.assertEqual(len(notices), 2)
        self.assertIn("added: solo@node-2", notices[-1]["body"])


class Declarations(RegistryTestCase):
    def test_missing_file_loads_empty(self):
        self.assertEqual(resident_registry.load_declarations(table=TABLE), ([], []))
        agents, problems = resident_registry.list_agents()
        self.assertEqual((agents, problems), ([], []))
        self.assertEqual(os.listdir(os.environ["GUARDIAN_DATA_DIR"]), [])

    def test_hand_edited_bad_entry_is_skipped(self):
        self.add(name="claude")
        good = self.declarations()
        good["agents"].append({"name": "Bad Name", "node": "node-1", "provider": "claude",
                               "driver": "headless"})
        resident_registry.declaration_path().write_text(json.dumps(good), encoding="utf-8")
        entries, problems = resident_registry.load_declarations(table=TABLE)
        self.assertEqual([e["name"] for e in entries], ["claude"])
        self.assertEqual(len(problems), 1)
        agents, load_problems = resident_registry.list_agents()
        self.assertEqual(len(agents), 1)
        self.assertEqual(load_problems, problems)

    def test_bad_peer_entry_is_skipped(self):
        self.add(name="claude")
        good = self.declarations()
        good["agents"].append({"name": "agy", "node": "node-1", "provider": "agy",
                               "driver": "headless", "peers": ["ghost@node-1"]})
        resident_registry.declaration_path().write_text(json.dumps(good), encoding="utf-8")
        entries, problems = resident_registry.load_declarations(table=TABLE)
        self.assertEqual([e["name"] for e in entries], ["claude"])
        self.assertTrue(problems)

    def test_load_through_the_inventory_env(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "servers.json"
            path.write_text(json.dumps(self.servers), encoding="utf-8")
            with patch.dict(os.environ, {"GUARDIAN_SERVERS_FILE": str(path)}):
                entry = resident_registry.add_agent("claude", "node-1", "claude", table=TABLE)
        self.assertEqual(entry["name"], "claude")


class Removal(RegistryTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.add(name="claude")
        self.second = self.add(name="agy")
        self.store = mail_store.MailStore()
        for address, session in (("claude@node-1", "s-1"), ("agy@node-1", "s-2")):
            bind = self.store.bind_begin(address)
            self.store.bind_complete(address, bind["epoch"], session)
        self.turn = self.store.admit_turn("agy@node-1", 1, "wake")["turn"]

    def test_removal_order(self):
        task = self.store.task_create("operator", "work", op_id="m0", assignee="claude@node-1")
        claim = self.store.task_claim("claude@node-1", task["id"], op_id="m1", epoch=1,
                                      turn=self.store.admit_turn("claude@node-1", 1, "wake")["turn"])
        operator_mail = self.store.send("operator", "claude@node-1", "message", "s", "b",
                                        op_id="m2")
        agent_mail = self.store.send("agy@node-1", "claude@node-1", "message", "s", "b",
                                     op_id="m3", epoch=1, turn=self.turn)
        resident_registry.remove_agent("claude@node-1")
        # (1) new mail is refused with agent_retired
        refused = self.store.send("operator", "claude@node-1", "message", "s", "b", op_id="m4")
        self.assertEqual(refused["reason"], "agent_retired")
        # (2) in_progress tasks are back to pending, assignee cleared
        row = next(t for t in self.store.task_list("operator") if t["id"] == task["id"])
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["owner"])
        self.assertIsNone(row["assignee"])
        self.assertEqual(row["reason"], "agent_retired")
        self.assertTrue(claim["ok"])  # the claim itself was fine before the removal
        # (3) unacked mail is undeliverable and the agent sender got one notice
        states = {m["id"]: m["state"] for m in self.store.list_mail()}
        self.assertEqual(states[operator_mail["id"]], "undeliverable")
        self.assertEqual(states[agent_mail["id"]], "undeliverable")
        notices = [m for m in self.store.inbox_queued("agy@node-1") if m["subject"] == "undeliverable"]
        self.assertEqual(len(notices), 1)
        self.assertIn(agent_mail["id"], notices[0]["body"])
        # (6) the declaration is gone, the peer lists lost the address
        data = self.declarations()
        self.assertEqual([a["name"] for a in data["agents"]], ["agy"])
        peers_notices = [m for m in self.store.inbox_queued("agy@node-1")
                         if m["subject"] == "peers_changed"]
        self.assertEqual(len(peers_notices), 1)
        self.assertIn("claude@node-1", peers_notices[0]["body"])
        events = {e["kind"] for e in self.store.events()}
        self.assertIn("agent_retiring", events)
        self.assertIn("retire_pending_node", events)
        # the store row stays, retiring
        row_agent = next(a for a in self.store.agent_rows() if a["address"] == "claude@node-1")
        self.assertEqual(row_agent["state"], "retiring")

    def test_removal_drops_address_from_explicit_peers(self):
        self.add(name="solo", node="node-2", peers=["claude@node-1", "agy@node-1"],
                 servers=[server(), server("node-2")])
        resident_registry.remove_agent("claude@node-1")
        solo = next(a for a in self.declarations()["agents"] if a["name"] == "solo")
        self.assertEqual(solo["peers"], ["agy@node-1"])

    def test_remove_unknown_address(self):
        with self.assertRaises(RegistrationError):
            resident_registry.remove_agent("ghost@node-1")


class Listing(RegistryTestCase):
    def test_list_joins_the_store_state(self):
        self.add(name="claude")
        store = mail_store.MailStore()
        bind = store.bind_begin("claude@node-1")
        store.bind_complete("claude@node-1", bind["epoch"], "s-1")
        agents, problems = resident_registry.list_agents()
        self.assertEqual(problems, [])
        self.assertEqual(agents[0]["address"], "claude@node-1")
        self.assertEqual(agents[0]["state"], "ready")
        self.assertEqual(agents[0]["epoch"], 1)
        self.assertEqual(agents[0]["session_id"], "s-1")


class OpenRegistration(RegistryTestCase):
    """The preconditions of a declaration with tools: "open" (resident-open-tools.md O2): claude or
    agy, a name the sandbox user gsa-<name> can carry, the node's sandbox probe passed at exactly
    this release's sandbox version, and the open memory figure. The hub's sandbox version is faked
    because the checkout may not hold the launcher file yet."""

    HUB_VERSION = "0f1e2d3c4b5a"

    def setUp(self):
        super().setUp()
        patcher = patch.object(resident_registry.sandbox_info, "sandbox_version",
                               return_value=self.HUB_VERSION)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.servers = [self.good_server()]

    def good_server(self, memory=8192, version=None, passed=True, clis=("claude", "agy")):
        entry = server(memory=memory, clis=clis)
        entry["resident_sandbox"] = {"version": version or self.HUB_VERSION, "passed": passed,
                                     "checked": "2026-11-19"}
        return entry

    def test_bad_tools_value_is_refused(self):
        with self.assertRaises(RegistrationError) as caught:
            self.add(tools="both")
        self.assertIn("closed or open", str(caught.exception))

    def test_open_writes_the_key_and_closed_stays_implicit(self):
        entry = self.add(tools="open")
        self.assertEqual(entry["tools"], "open")
        self.assertEqual(self.declarations()["agents"][0]["tools"], "open")
        added = next(e for e in mail_store.MailStore().events() if e["kind"] == "agent_added")
        self.assertEqual(added["detail"]["tools"], "open")
        resident_registry.remove_agent("claude@node-1")
        closed = self.add(name="agy", provider="agy")
        self.assertNotIn("tools", closed)
        self.assertNotIn("tools", self.declarations()["agents"][0])

    def test_only_claude_agy_and_codex_can_be_open(self):
        table = json.loads(json.dumps(TABLE))
        table["providers"]["gemini"] = {"resident": True, "default_driver": "headless",
                                        "drivers": {"headless": {"status": "pass"}}}
        with self.assertRaises(RegistrationError) as caught:
            self.add(provider="gemini", tools="open", table=table,
                     servers=[self.good_server(clis=("claude", "agy", "gemini"))])
        self.assertIn("open tools", str(caught.exception))

    def test_codex_is_allowed_for_open_tools_and_refused_for_closed(self):
        servers = [self.good_server(clis=("claude", "agy", "codex"))]
        with self.assertRaises(RegistrationError) as caught:
            self.add(name="codex", provider="codex", tools="closed", servers=servers)
        self.assertIn("M0 table", str(caught.exception))
        with self.assertRaises(RegistrationError) as caught:
            self.add(name="codex", provider="codex", tools="open", driver="tmux", servers=servers)
        self.assertIn("headless", str(caught.exception))
        entry = self.add(name="codex", provider="codex", tools="open", servers=servers)
        self.assertEqual(entry["provider"], "codex")
        self.assertEqual(entry["driver"], "headless")
        self.assertEqual(entry["tools"], "open")
        entries, problems = resident_registry.load_declarations(table=TABLE)
        self.assertEqual(problems, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["address"], "codex@node-1")

    def test_the_name_must_fit_the_sandbox_user(self):
        for name in ("agent-one", "x" * 17):  # a dash and 17 characters pass NAME_RE but not C8
            with self.assertRaises(RegistrationError) as caught:
                self.add(name=name, tools="open")
            self.assertIn("open agent", str(caught.exception))
        self.add(name="x" * 16, tools="open")  # 16 characters fit the sandbox user

    def test_a_node_without_a_passed_verdict_is_refused(self):
        for verdict in (None, {"version": self.HUB_VERSION, "passed": False}):
            entry = server()
            if verdict is not None:
                entry["resident_sandbox"] = verdict
            with self.assertRaises(RegistrationError) as caught:
                self.add(tools="open", servers=[entry])
            self.assertIn("re-run scripts/probe-sandbox.sh", str(caught.exception))

    def test_a_verdict_for_another_sandbox_version_is_stale(self):
        with self.assertRaises(RegistrationError) as caught:
            self.add(tools="open", servers=[self.good_server(version="000000000000")])
        self.assertIn("re-run scripts/probe-sandbox.sh", str(caught.exception))
        self.assertIn("000000000000", str(caught.exception))
        self.assertIn(self.HUB_VERSION, str(caught.exception))

    def test_a_broken_release_tree_refuses_open_registration(self):
        with patch.object(resident_registry.sandbox_info, "sandbox_version",
                          side_effect=sandbox_info.SandboxVersionError("cannot read launch.py")):
            with self.assertRaises(RegistrationError) as caught:
                self.add(tools="open")
        self.assertIn("sandbox version", str(caught.exception))

    def test_an_open_instance_counts_the_open_budget_figure(self):
        # The real configs figures: 2*1536 + 2048 + 512 = 5632 (resident_open_memory_mib 2048).
        self.add(name="one", tools="open", servers=[self.good_server(memory=5632)])
        resident_registry.remove_agent("one@node-1")
        with self.assertRaises(RegistrationError) as caught:
            self.add(tools="open", servers=[self.good_server(memory=5631)])  # one MiB short
        self.assertIn("budget", str(caught.exception))

    def test_closed_and_open_instances_add_up(self):
        # One closed instance (512) next to an open one (2048): 2*1536 + 512 + 2048 + 512 = 6144.
        self.add(name="shut", servers=[self.good_server()])
        self.add(name="open1", tools="open", servers=[self.good_server(memory=6144)])
        with self.assertRaises(RegistrationError):
            self.add(name="open2", tools="open", servers=[self.good_server(memory=6143)])

    def test_a_missing_open_figure_refuses_open_only(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker-requirements.json"
            path.write_text(json.dumps({"default_max_jobs": 2, "free_memory_mib_per_job": 1536,
                                        "resident_memory_mib": 512, "resident_turn_memory_mib": 512}))
            with patch.object(resident_registry, "requirements_path", return_value=path):
                self.add(name="shut", servers=[self.good_server()])  # closed needs no open figure
                resident_registry.remove_agent("shut@node-1")
                with self.assertRaises(RegistrationError) as caught:
                    self.add(tools="open", servers=[self.good_server()])
        self.assertIn("open resident budget figure", str(caught.exception))


class OpenDeclarations(RegistryTestCase):
    """The static rules a loaded open entry must pass; the inventory verdict and the budget stay
    with the registration (the relay re-checks the verdict on every agents frame it builds), so
    loading needs no sandbox version at all."""

    def write(self, *agents):
        path = resident_registry.declaration_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "agents": list(agents)}), encoding="utf-8")

    def test_a_bad_tools_value_is_skipped(self):
        self.write({"name": "claude", "node": "node-1", "provider": "claude",
                    "driver": "headless", "tools": "both"})
        entries, problems = resident_registry.load_declarations(table=TABLE)
        self.assertEqual(entries, [])
        self.assertTrue(problems)

    def test_an_open_entry_of_another_provider_is_skipped(self):
        table = json.loads(json.dumps(TABLE))
        table["providers"]["gemini"] = {"resident": True, "default_driver": "headless",
                                        "drivers": {"headless": {"status": "pass"}}}
        self.write({"name": "gem", "node": "node-1", "provider": "gemini",
                    "driver": "headless", "tools": "open"})
        entries, problems = resident_registry.load_declarations(table=table)
        self.assertEqual(entries, [])
        self.assertTrue(problems)

    def test_an_open_name_the_sandbox_user_cannot_carry_is_skipped(self):
        self.write({"name": "open-agent", "node": "node-1", "provider": "claude",
                    "driver": "headless", "tools": "open"})
        entries, problems = resident_registry.load_declarations(table=TABLE)
        self.assertEqual(entries, [])
        self.assertIn("open agent", problems[0])

    def test_a_good_open_entry_and_an_explicit_closed_one_load(self):
        self.write({"name": "claude", "node": "node-1", "provider": "claude",
                    "driver": "headless", "tools": "open"},
                   {"name": "agy", "node": "node-1", "provider": "agy",
                    "driver": "headless", "tools": "closed"})
        entries, problems = resident_registry.load_declarations(table=TABLE)
        self.assertEqual(problems, [])
        self.assertEqual([e["tools"] for e in entries], ["open", "closed"])


if __name__ == "__main__":
    unittest.main()

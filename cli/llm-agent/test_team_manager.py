import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from db_manager import DecisionDBManager
from debate_engine import JUDGE_VERDICT_RULE, DebateEngine, draft_labels, max_parallel_agents
from llm_runner import BUILTIN_PROVIDER, LLMResult, LLMRunner
from team_manager import (
    CRITIC_ROLE,
    EXPERT_ROLE,
    MAX_PROPOSERS,
    TeamManager,
    TeamValidationError,
    default_lineup,
    judge_tools,
    parse_lineup,
    validate_lineup,
)
from testenv import isolate_data_dir, restore_data_dir


def setUpModule() -> None:
    # These tests cover team lineups; the jev gate has its own tests.
    import os
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    # Keep the symbol index cache (and anything else) out of the real data directory.
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


PANEL_TEAM = {
    "id": "panel",
    "name": "Panel",
    "expert": {"persona": "domain_architect", "providers": ["claude"]},
    "critic": [
        {"persona": "critic_redteam", "providers": ["codex"]},
        {"persona": "devops_specialist", "providers": ["agy"]},
    ],
    "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]},
    "max_rounds": 2,
    "allowed_tools": ["jev_mcp:run_tests"],
}


class LineupParsingTests(unittest.TestCase):
    def test_single_critic_object_and_string_members(self) -> None:
        lineup = parse_lineup({"id": "t", "expert": "domain_architect", "critic": {"persona": "critic_redteam"},
                               "judge": "jev_mcp_decision"})
        self.assertEqual([m.persona for m in lineup.critics], ["critic_redteam"])
        self.assertEqual(lineup.expert.providers, [])

    def test_missing_role_and_bad_id_are_rejected(self) -> None:
        with self.assertRaisesRegex(TeamValidationError, "judge: missing"):
            parse_lineup({"id": "t", "expert": "domain_architect", "critic": "critic_redteam"})
        with self.assertRaisesRegex(TeamValidationError, "id"):
            parse_lineup({**PANEL_TEAM, "id": "../etc"})

    def test_validation_errors(self) -> None:
        bad = {**PANEL_TEAM, "critic": [{"persona": "nobody"}, {"persona": "nobody"}],
               "judge": {"persona": "jev_mcp_decision", "providers": ["gpt9"]},
               "max_rounds": 99, "allowed_tools": ["rm_rf"]}
        with self.assertRaises(TeamValidationError) as ctx:
            validate_lineup(parse_lineup(bad))
        message = str(ctx.exception)
        for fragment in ("unknown persona 'nobody'", "different persona", "unknown provider", "max_rounds", "rm_rf"):
            self.assertIn(fragment, message)

    def test_warnings_for_high_risk_tool_and_same_provider(self) -> None:
        team = {**PANEL_TEAM, "critic": [{"persona": "critic_redteam", "providers": ["claude"]}],
                "allowed_tools": ["jev_mcp:server_remediation"]}
        warnings = validate_lineup(parse_lineup(team))
        self.assertTrue(any("high risk" in w for w in warnings))
        self.assertTrue(any("same provider" in w for w in warnings))

    def test_retired_tool_is_accepted_with_a_warning_and_dropped(self) -> None:
        team = {**PANEL_TEAM, "allowed_tools": ["jev_mcp:run_tests", "jev_mcp:git_commit_push"]}
        warnings = validate_lineup(parse_lineup(team))  # no longer "unknown tool"
        self.assertTrue(any("git_commit_push" in w and "retired" in w for w in warnings), warnings)
        lineup = parse_lineup(team)
        self.assertEqual(judge_tools(lineup, {}), ["jev_mcp:run_tests"])
        self.assertEqual(judge_tools(lineup, {"mcp_capabilities": ["jev_mcp:git_commit_push", "jev_mcp:run_tests"]}),
                         ["jev_mcp:run_tests"])

    def test_session_keys(self) -> None:
        single = default_lineup()
        self.assertEqual(single.session_key(CRITIC_ROLE, single.critics[0]), CRITIC_ROLE)
        panel = parse_lineup(PANEL_TEAM)
        self.assertEqual(panel.session_key(CRITIC_ROLE, panel.critics[1]), f"{CRITIC_ROLE}:devops_specialist")
        self.assertEqual(panel.session_key("JEV-MCP Judge", panel.judge), "JEV-MCP Judge")

    def test_team_can_only_narrow_judge_tools(self) -> None:
        lineup = parse_lineup({**PANEL_TEAM, "allowed_tools": ["jev_mcp:run_tests", "jev_mcp:apply_patch"]})
        persona = {"mcp_capabilities": ["jev_mcp:run_tests"]}
        self.assertEqual(judge_tools(lineup, persona), ["jev_mcp:run_tests"])
        self.assertEqual(judge_tools(default_lineup(), persona), ["jev_mcp:run_tests"])
        self.assertIsNone(judge_tools(default_lineup(), {}))
        self.assertEqual(judge_tools(parse_lineup({**PANEL_TEAM, "allowed_tools": []}), persona), [])


PROPOSER_TEAM = {
    "id": "drafts",
    "name": "Drafts",
    "expert": {"persona": "domain_architect", "providers": ["claude"]},
    "proposers": [{"persona": "devops_specialist", "providers": ["codex"]}],
    "critic": {"persona": "critic_redteam", "providers": ["agy"]},
    "judge": {"persona": "jev_mcp_decision", "providers": ["claude"]},
    "max_rounds": 2,
}


class ProposerTeamTests(unittest.TestCase):
    def test_absent_proposers_keep_the_snapshot_shape(self) -> None:
        self.assertNotIn("proposers", parse_lineup(PANEL_TEAM).snapshot())
        self.assertNotIn("proposers", default_lineup().snapshot())
        snap = parse_lineup(PROPOSER_TEAM).snapshot()
        self.assertEqual(snap["proposers"], [{"persona": "devops_specialist", "providers": ["codex"]}])

    def test_session_keys_of_proposers(self) -> None:
        lineup = parse_lineup(PROPOSER_TEAM)
        self.assertEqual(lineup.session_key(EXPERT_ROLE, lineup.expert), EXPERT_ROLE)
        self.assertEqual(lineup.session_key(EXPERT_ROLE, lineup.proposers[0]), f"{EXPERT_ROLE}:devops_specialist")

    def test_validation(self) -> None:
        three = {**PROPOSER_TEAM, "proposers": [{"persona": "devops_specialist"}, {"persona": "design_expert"},
                                                {"persona": "general_assistant"}]}
        with self.assertRaisesRegex(TeamValidationError, f"at most {MAX_PROPOSERS}"):
            validate_lineup(parse_lineup(three))
        clash = {**PROPOSER_TEAM, "proposers": [{"persona": "domain_architect", "providers": ["codex"]}]}
        with self.assertRaisesRegex(TeamValidationError, "different persona"):
            validate_lineup(parse_lineup(clash))
        unknown = {**PROPOSER_TEAM, "proposers": [{"persona": "nobody"}]}
        with self.assertRaisesRegex(TeamValidationError, r"proposers\[0\]: unknown persona"):
            validate_lineup(parse_lineup(unknown))
        with self.assertRaisesRegex(TeamValidationError, "proposers: expected a list"):
            parse_lineup({**PROPOSER_TEAM, "proposers": "devops_specialist"})

    def test_diversity_warning(self) -> None:
        # devops_specialist's home seat is critic, so the proposer seat also warns about role_card; that is not this test
        self.assertEqual([w for w in validate_lineup(parse_lineup(PROPOSER_TEAM)) if "same provider" in w], [])
        same = {**PROPOSER_TEAM, "proposers": [{"persona": "devops_specialist", "providers": ["claude"]}]}
        self.assertTrue(any("same provider" in w for w in validate_lineup(parse_lineup(same))))
        self.assertFalse(any("same provider" in w for w in validate_lineup(parse_lineup(PANEL_TEAM))))

    def test_draft_order_rotates_per_round(self) -> None:
        self.assertEqual(draft_labels(3, 1), [0, 1, 2])
        self.assertEqual(draft_labels(3, 2), [1, 2, 0])
        self.assertEqual(draft_labels(2, 2), [1, 0])
        self.assertEqual(draft_labels(2, 3), [0, 1])

    def test_parallel_limit(self) -> None:
        with patch.dict("os.environ", {"GUARDIAN_MAX_PARALLEL_AGENTS": "7"}):
            self.assertEqual(max_parallel_agents(), 7)
        with patch.dict("os.environ", {"GUARDIAN_MAX_PARALLEL_AGENTS": "0"}):
            self.assertEqual(max_parallel_agents(), 1)
        with patch.dict("os.environ", {"GUARDIAN_MAX_PARALLEL_AGENTS": "x"}):
            self.assertEqual(max_parallel_agents(), 4)


class ProposerDebateTests(unittest.TestCase):
    def _engine(self, tmp: str, team=PROPOSER_TEAM) -> DebateEngine:
        manager = TeamManager(Path(tmp) / "teams")
        manager.save(team)
        return DebateEngine(db_path=Path(tmp) / "d.db", team_mgr=manager)

    @staticmethod
    def _save_to_db(engine: DebateEngine, tmp: str):
        return lambda *a, **k: engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or Path(tmp) / "l.md"

    @staticmethod
    def _fake(calls, lock, builtin_for=None):
        def fake(prompt, context=None, session_id=None, provider=None, providers=None, instructions=None, **_ignored):
            prompt = prompt + (instructions.text() if instructions else "")
            with lock:
                calls.append({"prompt": prompt, "providers": providers})
                n = len(calls)
            name = providers[0]
            if name == builtin_for:
                return LLMResult(BUILTIN_PROVIDER, "canned")
            if JUDGE_VERDICT_RULE in prompt:
                return LLMResult(f"{name}-cli", "Not yet.\nVERDICT: REJECTED", f"s{n}")
            return LLMResult(f"{name}-cli", f"text from {name}", f"s{n}")
        return fake

    def test_first_round_drafts_then_the_lead_consolidates(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            calls: list = []
            with patch.object(engine.llm, "run_session_prompt", side_effect=self._fake(calls, threading.Lock())), \
                    patch.object(engine, "_save_debate_log", side_effect=self._save_to_db(engine, tmp)):
                result = engine.run_debate("task", project_id="server-guardian", team="drafts")

            # round 1: 2 drafts + critic + judge; round 2: lead + critic + judge
            self.assertEqual(len(calls), 7)
            experts = [t for t in result["transcript"] if t["role"] == EXPERT_ROLE]
            self.assertEqual([(t["round"], t.get("draft")) for t in experts], [(1, "A"), (1, "B"), (2, None)])
            self.assertEqual(experts[0]["provider"], "claude-cli")
            self.assertEqual(experts[1]["provider"], "codex-cli")

            critic1 = next(c for c in calls if c["providers"] == ["agy"])["prompt"]
            self.assertIn("### Proposal A\ntext from claude", critic1)
            self.assertIn("### Proposal B\ntext from codex", critic1)
            self.assertNotIn("DevOps", critic1)  # the drafters' names stay out of the critics' prompt
            judge1 = next(c for c in calls if JUDGE_VERDICT_RULE in c["prompt"])["prompt"]
            self.assertIn("### Proposal B\ntext from codex", judge1)

            # round 2: only the lead writes, and it sees both drafts plus the critique
            lead2 = calls[4]["prompt"]
            self.assertEqual(calls[4]["providers"], ["claude"])
            self.assertIn("Independent drafts written in an earlier round", lead2)
            self.assertIn("### Proposal B\ntext from codex", lead2)
            critic2 = calls[5]["prompt"]
            self.assertNotIn("### Proposal", critic2)
            self.assertIn("Domain Expert Proposal:", critic2)

            roles = {s["role"] for s in engine.db.list_agent_sessions(result["thread_id"])}
            self.assertEqual(roles, {"Domain Expert", "Domain Expert:devops_specialist", "Critic Agent",
                                     "JEV-MCP Judge"})
            self.assertEqual(engine.db.get_thread_history(result["thread_id"])["team"]["proposers"][0]["persona"],
                             "devops_specialist")

    def test_draft_names_option_shows_the_authors_to_critics_and_judge(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            calls: list = []
            with patch.dict("os.environ", {"GUARDIAN_DRAFT_NAMES": "1"}), \
                    patch.object(engine.llm, "run_session_prompt", side_effect=self._fake(calls, threading.Lock())), \
                    patch.object(engine, "_save_debate_log", side_effect=self._save_to_db(engine, tmp)):
                engine.run_debate("task", project_id="server-guardian", team="drafts")

            critic1 = next(c for c in calls if c["providers"] == ["agy"])["prompt"]
            self.assertIn("### Proposal B (by DevOps & Cloud Native Specialist)\ntext from codex", critic1)
            self.assertIn("each heading names its author", critic1)
            self.assertNotIn("labels are neutral", critic1)
            judge1 = next(c for c in calls if JUDGE_VERDICT_RULE in c["prompt"])["prompt"]
            self.assertIn("(by DevOps & Cloud Native Specialist)", judge1)

    def test_follow_up_drafts_again_and_rounds_keep_counting(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp, {**PROPOSER_TEAM, "max_rounds": 1})
            calls: list = []
            with patch.object(engine.llm, "run_session_prompt", side_effect=self._fake(calls, threading.Lock())), \
                    patch.object(engine, "_save_debate_log", side_effect=self._save_to_db(engine, tmp)):
                first = engine.run_debate("task", project_id="server-guardian", team="drafts")
                second = engine.run_debate("more", project_id="server-guardian", thread_id=first["thread_id"])

            experts = [t for t in second["transcript"] if t["role"] == EXPERT_ROLE]
            # The stored transcript keeps no draft labels (like fallbacks), the live round does.
            self.assertEqual([(t["round"], t.get("draft")) for t in experts],
                             [(1, None), (1, None), (2, "B"), (2, "A")])
            # Order rotated in round 2: the proposer's draft is shown as A, the lead's as B.
            self.assertEqual(experts[2]["provider"], "claude-cli")
            second_critic = [c for c in calls if c["providers"] == ["agy"]][1]["prompt"]
            self.assertTrue(second_critic.index("text from codex") < second_critic.index("text from claude"))
            self.assertEqual(len(calls), 8)

    def test_a_builtin_proposer_makes_the_round_unverified(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            calls: list = []
            with patch.object(engine.llm, "run_session_prompt",
                              side_effect=self._fake(calls, threading.Lock(), builtin_for="codex")), \
                    patch.object(engine, "_save_debate_log", return_value=Path(tmp) / "l.md"):
                result = engine.run_debate("task", project_id="server-guardian", team="drafts")

            self.assertTrue(result["final_decision"]["unverified"])
            self.assertEqual(len(calls), 4)  # the debate ends after the unverified first round


class CodeToolsTests(unittest.TestCase):
    def team(self, **member) -> dict:
        return {**PANEL_TEAM, "expert": {"persona": "domain_architect", "providers": ["claude"], **member}}

    def test_code_tools_mcp_is_parsed_and_absent_means_none(self) -> None:
        self.assertEqual(parse_lineup(self.team(code_tools="mcp")).expert.code_tools, "mcp")
        self.assertIsNone(parse_lineup(self.team()).expert.code_tools)
        self.assertIsNone(parse_lineup(self.team(code_tools=None)).expert.code_tools)
        self.assertIsNone(parse_lineup({**PANEL_TEAM, "expert": "domain_architect"}).expert.code_tools)

    def test_code_tools_mcp_is_valid_for_claude_codex_and_host_default(self) -> None:
        for providers in (["claude"], ["codex"], ["codex", "claude"], []):
            lineup = parse_lineup(self.team(providers=providers, code_tools="mcp"))
            self.assertEqual((lineup.expert.providers, lineup.expert.code_tools), (providers, "mcp"))
        critic = {**PANEL_TEAM, "critic": [{"persona": "critic_redteam", "providers": ["codex"], "code_tools": "mcp"}]}
        self.assertEqual(parse_lineup(critic).critics[0].code_tools, "mcp")

    def test_invalid_code_tools_is_rejected(self) -> None:
        for bad in ("shell", "MCP", "", 1, True, ["mcp"]):
            with self.assertRaisesRegex(TeamValidationError, "expert.code_tools"):
                parse_lineup(self.team(code_tools=bad))
        critic = {**PANEL_TEAM, "critic": [{"persona": "critic_redteam", "code_tools": "bash"}]}
        with self.assertRaisesRegex(TeamValidationError, r"critic\[0\].code_tools"):
            parse_lineup(critic)

    def test_snapshot_carries_code_tools_only_when_set(self) -> None:
        snap = parse_lineup(self.team(code_tools="mcp")).snapshot()
        self.assertEqual(snap["expert"], {"persona": "domain_architect", "providers": ["claude"], "code_tools": "mcp"})
        self.assertNotIn("code_tools", snap["judge"])
        self.assertEqual(parse_lineup(snap).expert.code_tools, "mcp")  # a continued thread restores it

    def test_saved_team_file_keeps_code_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            mgr = TeamManager(Path(tmp) / "custom", builtin_dir=Path(tmp) / "none")
            mgr.save(self.team(code_tools="mcp"))
            self.assertEqual(mgr.get_team("panel")["expert"]["code_tools"], "mcp")
            self.assertEqual(mgr.load("panel").expert.code_tools, "mcp")
            with self.assertRaises(TeamValidationError):
                mgr.save(self.team(code_tools="shell"))


class TeamManagerTests(unittest.TestCase):
    def test_save_load_and_reject_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = TeamManager(Path(tmp))
            # run_tests is high risk (it executes workspace code), so allowing it warns.
            self.assertEqual(manager.save(PANEL_TEAM),
                             ["allowed_tools: jev_mcp:run_tests is high risk; every proposal still needs a person's approval"])
            self.assertEqual(manager.load("panel").snapshot()["critic"][1]["providers"], ["agy"])
            with self.assertRaises(TeamValidationError):
                manager.save({**PANEL_TEAM, "id": "broken", "expert": {"persona": "nobody"}})
            self.assertIsNone(manager.get_team("broken"))
            with self.assertRaisesRegex(TeamValidationError, "no team"):
                manager.load("missing")

    def test_an_id_with_a_trailing_newline_is_refused(self) -> None:
        # "$" also matches just before a trailing newline, so only fullmatch refuses "panel\n"
        # (which would otherwise name a file "panel\n.json").
        with self.assertRaisesRegex(TeamValidationError, "id"):
            parse_lineup({**PANEL_TEAM, "id": "panel\n"})
        with TemporaryDirectory() as tmp:
            manager = TeamManager(Path(tmp))
            manager.save(PANEL_TEAM)
            self.assertEqual(manager.load("panel").id, "panel")
            self.assertIsNone(manager.get_team("panel\n"))
            self.assertFalse(manager.is_custom("panel\n"))

    def test_default_without_file_and_mismatched_id(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = TeamManager(Path(tmp))
            self.assertEqual(manager.load(None).id, "default")
            self.assertEqual(manager.load("default").critics[0].persona, "critic_redteam")
            (Path(tmp) / "alias.json").write_text(json.dumps(PANEL_TEAM), encoding="utf-8")
            with self.assertRaisesRegex(TeamValidationError, "declares id"):
                manager.load("alias")

    def test_shipped_teams_are_valid(self) -> None:
        manager = TeamManager()
        ids = [t["id"] for t in manager.list_teams()]
        self.assertIn("default", ids)
        for team_id in ids:
            manager.load(team_id)


class RunnerProviderPreferenceTests(unittest.TestCase):
    def test_member_providers_override_host_order(self) -> None:
        runner = LLMRunner(providers=["claude"])
        runner.available_providers = ["claude"]
        runner.installed_providers = ["claude", "codex"]
        with patch.object(runner, "_session_codex", return_value=("hi", "s1")) as codex, \
                patch.object(runner, "_session_claude") as claude:
            result = runner.run_session_prompt("p", providers=["agy", "codex"])
        self.assertEqual((result.provider, result.session_id), ("codex-cli", "s1"))
        codex.assert_called_once()
        claude.assert_not_called()

    def test_no_installed_preference_falls_back(self) -> None:
        runner = LLMRunner()
        runner.installed_providers = []
        result = runner.run_session_prompt("p", providers=["agy"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertIn("none of ['agy'] is installed", runner.last_error)


class TeamDebateTests(unittest.TestCase):
    def _engine(self, tmp: str, max_rounds=None) -> DebateEngine:
        manager = TeamManager(Path(tmp) / "teams")
        manager.save(PANEL_TEAM)
        engine = DebateEngine(max_rounds=max_rounds, db_path=Path(tmp) / "d.db", team_mgr=manager)
        return engine

    @staticmethod
    def _save_to_db(engine: DebateEngine, tmp: str):
        return lambda *a, **k: engine.db.save_debate_session(*a[:4], decision=a[4], team=a[5]) or Path(tmp) / "l.md"

    def test_panel_debate_calls_each_member_with_its_providers(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            calls = []
            lock = threading.Lock()

            def fake(prompt, context=None, session_id=None, provider=None, providers=None, instructions=None,
                     **_ignored):
                # A claude member gets its rules as the system prompt: the judge's verdict rule is there, not in prompt.
                prompt = prompt + (instructions.text() if instructions else "")
                with lock:
                    calls.append({"prompt": prompt, "providers": providers})
                    n = len(calls)
                name = providers[0]
                if JUDGE_VERDICT_RULE in prompt:  # only the judge prompt asks for the verdict line (the repo map may name VERDICT_* symbols)
                    return LLMResult(f"{name}-cli", "Not yet.\nVERDICT: REJECTED", f"s{n}")
                return LLMResult(f"{name}-cli", f"text from {name}", f"s{n}")

            with patch.object(engine.llm, "run_session_prompt", side_effect=fake), \
                    patch.object(engine, "_save_debate_log", side_effect=self._save_to_db(engine, tmp)):
                result = engine.run_debate("task", project_id="server-guardian", team="panel")

            # max_rounds comes from the team: 2 rounds x (expert + 2 critics + judge)
            self.assertEqual(len(calls), 8)
            self.assertEqual(sorted(c["providers"][0] for c in calls[:4]), ["agy", "claude", "claude", "codex"])
            critic_steps = [t for t in result["transcript"] if t["role"] == CRITIC_ROLE]
            self.assertEqual({t["provider"] for t in critic_steps}, {"codex-cli", "agy-cli"})
            judge_prompt = next(c["prompt"] for c in calls if JUDGE_VERDICT_RULE in c["prompt"])
            self.assertIn("### Critique from Security & Architecture Red Teamer", judge_prompt)
            self.assertIn("text from agy", judge_prompt)
            # The team narrows the judge's tool catalog.
            self.assertIn("jev_mcp:run_tests", judge_prompt)
            self.assertNotIn("jev_mcp:server_remediation", judge_prompt)

            roles = {s["role"] for s in engine.db.list_agent_sessions(result["thread_id"])}
            self.assertEqual(roles, {"Domain Expert", "JEV-MCP Judge",
                                     "Critic Agent:critic_redteam", "Critic Agent:devops_specialist"})
            self.assertEqual(engine.db.get_thread_history(result["thread_id"])["team"]["id"], "panel")

    def test_continued_thread_reuses_stored_lineup(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp, max_rounds=1)
            seen = []

            def fake(prompt, context=None, session_id=None, provider=None, providers=None, **_ignored):
                seen.append(providers)
                return LLMResult("codex-cli", "VERDICT: REJECTED", None)

            with patch.object(engine.llm, "run_session_prompt", side_effect=fake), \
                    patch.object(engine, "_save_debate_log", side_effect=self._save_to_db(engine, tmp)):
                first = engine.run_debate("task", project_id="server-guardian", team="panel")
                second = engine.run_debate("more", project_id="server-guardian", thread_id=first["thread_id"])

            self.assertEqual(second["team"]["id"], "panel")
            self.assertEqual(sorted(seen[4:]), sorted(seen[:4]))  # critics run in parallel

    def test_one_builtin_panel_member_makes_round_unverified(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp)

            def fake(prompt, context=None, session_id=None, provider=None, providers=None, **_ignored):
                if providers == ["agy"]:
                    return LLMResult(BUILTIN_PROVIDER, "canned")
                return LLMResult(f"{providers[0]}-cli", "VERDICT: APPROVED", None)

            with patch.object(engine.llm, "run_session_prompt", side_effect=fake), \
                    patch.object(engine, "_save_debate_log", return_value=Path(tmp) / "l.md"):
                result = engine.run_debate("task", project_id="server-guardian", team="panel")

            self.assertTrue(result["final_decision"]["unverified"])
            self.assertFalse(result["consensus_reached"])

    def test_persona_override_keeps_legacy_signature(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = self._engine(tmp, max_rounds=1)
            with patch.object(engine.llm, "run_session_prompt",
                              return_value=LLMResult("codex-cli", "VERDICT: REJECTED")) as run, \
                    patch.object(engine, "_save_debate_log", return_value=Path(tmp) / "l.md"):
                result = engine.run_debate("task", project_id="server-guardian", critic_id="devops_specialist")

            self.assertEqual(result["team"]["critic"], [{"persona": "devops_specialist", "providers": []}])
            self.assertTrue(all("providers" not in c.kwargs for c in run.call_args_list))


class FallbackReportingTests(unittest.TestCase):
    def test_runner_reports_skipped_providers(self) -> None:
        runner = LLMRunner()
        runner.installed_providers = ["claude", "codex"]
        with patch.object(runner, "_session_claude", side_effect=RuntimeError("claude exited with code 1: 429")), \
                patch.object(runner, "_session_codex", return_value=("ok", "s1")):
            result = runner.run_session_prompt("p", providers=["claude", "codex"])
        self.assertEqual(result.provider, "codex-cli")
        self.assertEqual(result.failures, ["claude: claude exited with code 1: 429"])

    def test_engine_prints_and_records_fallbacks(self) -> None:
        import io
        from contextlib import redirect_stdout

        with TemporaryDirectory() as tmp:
            engine = DebateEngine(max_rounds=1, db_path=Path(tmp) / "d.db")

            def fake(prompt, context=None, session_id=None, provider=None):
                if JUDGE_VERDICT_RULE in prompt:
                    return LLMResult("codex-cli", "VERDICT: REJECTED", None,
                                     failures=["claude: timed out after 300s\n  (second line)"])
                return LLMResult("claude-cli", "text", None)

            out = io.StringIO()
            with patch.object(engine.llm, "run_session_prompt", side_effect=fake), \
                    patch.object(engine, "_save_debate_log", return_value=Path(tmp) / "l.md"), redirect_stdout(out):
                result = engine.run_debate("task", project_id="server-guardian")

        judge = result["transcript"][-1]
        self.assertEqual(judge["fallbacks"], ["claude: timed out after 300s (second line)"])
        self.assertNotIn("fallbacks", result["transcript"][0])
        self.assertIn("↪ [JEV-MCP Judge] skipped claude: timed out after 300s", out.getvalue())


class ThreadTeamColumnTests(unittest.TestCase):
    def test_existing_database_gains_column(self) -> None:
        import sqlite3

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                             "task_prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'IN_PROGRESS', "
                             "consensus_reached INTEGER DEFAULT 0, final_round INTEGER DEFAULT 0, "
                             "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
                conn.execute("INSERT INTO threads (thread_id, project_id, task_prompt) VALUES ('t1', 'p', 'x');")
            db = DecisionDBManager(path)
            self.assertIsNone(db.get_thread_history("t1")["team"])
            db.save_debate_session("p", "t1", "x", [], None, team={"id": "panel"})
            db.save_debate_session("p", "t1", "x", [], None)
            self.assertEqual(db.get_thread_history("t1")["team"], {"id": "panel"})


if __name__ == "__main__":
    unittest.main()

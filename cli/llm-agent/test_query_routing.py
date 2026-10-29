"""An explicit target decides the query scope; fleet keywords such as "전체" apply only without one."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

FLEET_PROMPT = "서버 전체 보안과 리소스 상태를 점검하고 해결 조치 계획을 세워줘"


def make_system():
    from multi_agent import MultiAgentSystem

    system = MultiAgentSystem.__new__(MultiAgentSystem)
    system.client = MagicMock()
    system.client.get_metrics.return_value = {"hostname": "hub"}
    system.client.get_ssh_security.return_value = {"score": 80, "rules": []}
    system.llm = MagicMock()
    system.llm.run_prompt.return_value = ("builtin", "analysis")
    system.active_persona = {"id": "server_guardian"}
    system.run_fleet_diagnostic = MagicMock(return_value={"target": "all"})
    system.run_security_auditor = MagicMock(return_value={"security_score": 80})
    system.run_remediation_agent = MagicMock(return_value={})
    return system


class HandleQueryScopeTest(unittest.TestCase):
    def query(self, system, text, **kwargs):
        with redirect_stdout(io.StringIO()):
            return system.handle_query(text, **kwargs)

    def test_keyword_selects_the_fleet_without_a_target(self) -> None:
        system = make_system()
        res = self.query(system, FLEET_PROMPT)
        system.run_fleet_diagnostic.assert_called_once_with(None)
        self.assertEqual(res["diagnostic"]["target"], "all")
        system.run_security_auditor.assert_called_once_with(None, fleet=True)

    def test_explicit_local_target_overrides_the_keyword(self) -> None:
        system = make_system()
        res = self.query(system, FLEET_PROMPT, target_host="local")
        system.run_fleet_diagnostic.assert_not_called()
        self.assertEqual(res["diagnostic"]["target"], "local")
        system.run_security_auditor.assert_called_once_with()

    def test_explicit_server_target_overrides_the_keyword(self) -> None:
        system = make_system()
        system.client.execute_remote_ssh.return_value = {"stdout": "Linux aarch64"}
        res = self.query(system, FLEET_PROMPT, target_host="site-a-ec1")
        system.run_fleet_diagnostic.assert_not_called()
        self.assertEqual(res["diagnostic"]["target"], "site-a-ec1")
        system.run_security_auditor.assert_called_once_with(["site-a-ec1"], fleet=True)

    def test_explicit_all_target_and_groups_stay_fleet(self) -> None:
        system = make_system()
        self.query(system, "리소스 상태 진단", target_host="all")
        system.run_fleet_diagnostic.assert_called_once_with(None)

        system = make_system()
        self.query(system, "리소스 상태 진단", target_host="site-b", target_ids=["b1", "b2"])
        system.run_fleet_diagnostic.assert_called_once_with(["b1", "b2"])


class QueryScopeTest(unittest.TestCase):
    """handle_query reports its scope; render_results prints it before the agent reports."""

    def scope(self, text, **kwargs):
        with redirect_stdout(io.StringIO()):
            return make_system().handle_query(text, **kwargs).get("scope")

    def test_scope_names_its_source(self) -> None:
        self.assertEqual(self.scope(FLEET_PROMPT), {"kind": "fleet", "source": "keyword", "target": "all", "keyword": "전체"})
        self.assertEqual(self.scope(FLEET_PROMPT, target_host="local"), {"kind": "local", "source": "target", "target": "local"})
        self.assertEqual(self.scope("리소스 상태 진단"), {"kind": "local", "source": "default", "target": "local"})
        self.assertEqual(self.scope("리소스 상태 진단", target_host="b1"), {"kind": "server", "source": "target", "target": "b1"})
        self.assertEqual(
            self.scope("리소스 상태 진단", target_host="site-b", target_ids=["b1", "b2"]),
            {"kind": "group", "source": "target", "target": "site-b", "nodes": ["b1", "b2"]},
        )

    def test_chat_answers_carry_no_scope(self) -> None:
        self.assertIsNone(self.scope("안녕, 너는 누구야?"))

    def render(self, results):
        import guardian

        out = io.StringIO()
        with redirect_stdout(out):
            guardian.render_results(results)
        return out.getvalue().splitlines()[2]

    def test_render_puts_the_scope_first(self) -> None:
        line = self.render({
            "scope": {"kind": "fleet", "source": "keyword", "target": "all", "keyword": "전체"},
            "diagnostic": {"target": "all", "total_nodes": 24, "online_nodes": 23, "analysis": "ok"},
        })
        self.assertEqual(line, "📍 Scope: all 24 servers - chosen by '전체' in the query; add --target local for this host only")

    def test_render_names_explicit_and_default_targets(self) -> None:
        local = {"kind": "local", "source": "target", "target": "local"}
        self.assertEqual(self.render({"scope": local, "diagnostic": {"target": "local"}}),
                         "📍 Scope: the local agent's host only - --target local")
        default = dict(local, source="default")
        self.assertEqual(self.render({"scope": default, "diagnostic": {"target": "local"}}),
                         "📍 Scope: the local agent's host only - no --target given")
        group = {"kind": "group", "source": "target", "target": "site-b", "nodes": ["b1", "b2"]}
        self.assertEqual(self.render({"scope": group, "security": {"target": "b1,b2", "fleet_summary": {"nodes": 2}}}),
                         "📍 Scope: node group 'site-b' (2 servers) - --target site-b")


class CliTargetTest(unittest.TestCase):
    """guardian.py passes target_host=None to handle_query only when --target is omitted."""

    def run_cli(self, argv):
        import guardian

        system = MagicMock()
        system.handle_query.return_value = {}
        with patch("sys.argv", ["guardian.py", *argv]), \
                patch("guardian.MultiAgentSystem", return_value=system), \
                patch("guardian.resolve_cli_target", return_value=None), \
                patch("guardian.render_results"):
            guardian.main()
        return system.handle_query.call_args

    def test_omitted_target_lets_the_query_decide(self) -> None:
        call = self.run_cli([FLEET_PROMPT])
        self.assertIsNone(call.kwargs["target_host"])

    def test_explicit_local_target_is_passed_through(self) -> None:
        call = self.run_cli(["--target", "local", FLEET_PROMPT])
        self.assertEqual(call.kwargs["target_host"], "local")


if __name__ == "__main__":
    unittest.main()

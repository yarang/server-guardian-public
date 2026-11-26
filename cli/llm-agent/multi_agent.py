"""
Multi-Agent Orchestrator for Server Guardian (Python Implementation).
Orchestrates 4 specialized agents:
- SupervisorAgent: Intent routing and plan decomposition
- DiagnosticAgent: System telemetries & bottleneck detection (Local, Single Remote, Fleet-wide)
- SecurityAuditorAgent: sshd_config & CIS benchmarks assessment
- RemediationAgent: Human-in-the-loop safe action planning
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Any, Dict, List, Optional

import character
from agent_client import NodeAgentClient
from llm_runner import LLMRunner
from persona_manager import PersonaManager


# Query words that mean "every server" when the caller named no target (see handle_query).
FLEET_KEYWORDS = ["전체 서버", "모든 서버", "전체", "fleet", "all servers"]


def query_scope(
    target_host: Optional[str], target_ids: Optional[List[str]], is_fleet: bool, keyword: Optional[str]
) -> Dict[str, Any]:
    """Which nodes a query's diagnosis and audit cover, and what decided it.

    kind: fleet (every node), group (target_ids), server (one remote host) or local (the agent
    the CLI talks to). source: target (an explicit target), keyword (a fleet word in the query,
    named in `keyword`) or default (neither: local)."""
    if target_ids:
        kind = "group"
    elif is_fleet:
        kind = "fleet"
    elif target_host not in (None, "local"):
        kind = "server"
    else:
        kind = "local"
    if target_ids or target_host is not None:
        source = "target"
    elif keyword is not None:
        source = "keyword"
    else:
        source = "default"
    scope: Dict[str, Any] = {"kind": kind, "source": source, "target": target_host or ("all" if is_fleet else "local")}
    if target_ids:
        scope["nodes"] = list(target_ids)
    if source == "keyword":
        scope["keyword"] = keyword
    return scope


class MultiAgentSystem:
    def __init__(self, agent_url: str = "http://localhost:9870", persona_id: str = "server_guardian"):
        self.client = NodeAgentClient(agent_url)
        self.llm = LLMRunner()
        self.persona_mgr = PersonaManager()
        self.active_persona = self.persona_mgr.get_persona(persona_id) or {
            "id": "server_guardian",
            "name": "Server Guardian System Guardian",
            "role": "Infrastructure & SSH Guardian Agent",
            # same text as configs/personas/server_guardian.json, for a checkout without the personas directory
            "system_prompt": ("당신은 Server Guardian의 서버 운영 도우미입니다. 서버 자원 병목(CPU, 메모리, 디스크, 상위 "
                              "프로세스) 분석, sshd 설정 보안 감사, 안전한 조치 계획 수립을 맡습니다. 판단은 받은 측정값과 "
                              "점검 결과에만 기대고, 없는 수치는 지어내지 마세요. 조치는 제안만 하며, 실행은 사람이 승인한 "
                              "뒤에 이루어집니다. 조치를 제안할 때는 대상 서버, 명령, 영향, 되돌리는 방법을 함께 적으세요."),
        }

    def set_active_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        persona = self.persona_mgr.get_persona(persona_id)
        if persona:
            self.active_persona = persona
            return persona
        return None

    def get_active_persona(self) -> Dict[str, Any]:
        return self.active_persona

    def list_personas(self) -> List[Dict[str, Any]]:
        return self.persona_mgr.list_personas()

    def run_diagnostic_agent(self, target_host: Optional[str] = None) -> Dict[str, Any]:
        """Diagnostic Agent: Telemetry & Bottleneck analysis for a single host."""
        print(f"🔍 [Diagnostic Agent] Gathering telemetry for target: {target_host or 'local'}...")

        if target_host and target_host not in ["local", "all", "fleet"]:
            # Single Remote Fleet Host
            res = self.client.execute_remote_ssh(target_host, "uname -s -m && uptime && free -m && df -h /")
            prompt = f"Analyze the following remote host ({target_host}) telemetry and report any saturation:"
            provider, analysis = self.llm.run_prompt(prompt, {"raw_output": res.get("stdout")})
            return {
                "agent": "DiagnosticAgent",
                "target": target_host,
                "analysis": analysis,
                "provider": provider,
            }

        # Local Host
        metrics = self.client.get_metrics()
        prompt = "Analyze system resource metrics, CPU/memory saturation, and top processes. Flag any bottlenecks."
        provider, analysis = self.llm.run_prompt(prompt, {"metrics": metrics})
        return {
            "agent": "DiagnosticAgent",
            "target": "local",
            "metrics": metrics,
            "analysis": analysis,
            "provider": provider,
        }

    def fleet_groups(self) -> Optional[List[Dict[str, Any]]]:
        """User node groups from the hub for per-group summaries; None when unavailable."""
        try:
            return self.client.get_groups().get("groups") or []
        except Exception:
            return None

    def run_fleet_diagnostic(self, targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Diagnostic Agent: parallel diagnosis across the fleet (or only `targets`, explicit node ids).
        Executes parallel SSH probes via Go Agent and synthesizes holistic infrastructure health.
        """
        scope = f"{len(targets)} selected servers" if targets else "all servers"
        print(f"🌐 [Diagnostic Agent] Initiating parallel diagnosis across {scope}...")
        target_label = ",".join(targets) if targets else "all"
        try:
            diag_res = self.client.diagnose_fleet(targets) if targets else self.client.diagnose_fleet()
        except Exception as e:
            return {
                "agent": "DiagnosticAgent",
                "target": target_label,
                "error": str(e),
            }

        total_count = diag_res.get("total_nodes", 0)
        online_count = diag_res.get("online_count", 0)
        duration_sec = round(diag_res.get("duration_sec", 0.0), 2)
        fleet_results = diag_res.get("results", [])

        # AI Synthesis
        prompt = (
            f"Analyze this fleet-wide health check report of {total_count} servers ({online_count} online, {duration_sec}s). "
            f"Summarize overall cluster stability, load trends, memory/disk usage, and flag any abnormal nodes."
        )
        context: Dict[str, Any] = {"fleet_summary": fleet_results}
        groups = self.fleet_groups()
        if groups is not None:
            from group_manager import memberships

            context["fleet_groups"] = memberships(groups, [str(r.get("id")) for r in fleet_results])
        provider, analysis = self.llm.run_prompt(prompt, context)

        return {
            "agent": "DiagnosticAgent",
            "target": target_label,
            "total_nodes": total_count,
            "online_nodes": online_count,
            "probe_duration_sec": duration_sec,
            "fleet_results": fleet_results,
            "analysis": analysis,
            "provider": provider,
        }

    def run_fleet_exec(self, command: str, timeout_sec: int = 15, targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """Execute a command across every node (or only `targets`, explicit node ids) with a live progress bar."""
        import sys
        print(f"🌐 [Fleet Exec] Streaming '{command}' across {f'{len(targets)} servers' if targets else 'all servers'}...")
        results = []
        total = 0
        completed = 0
        success = 0
        failed = 0
        start_time = time.time()

        try:
            for evt in self.client.stream_fleet_exec(command, targets):
                etype = evt.get("type")
                if etype == "start":
                    total = evt.get("total", 0)
                elif etype == "node_done":
                    completed = evt.get("completed", completed + 1)
                    success = evt.get("success", success)
                    failed = evt.get("failed", failed)
                    res = evt.get("result")
                    if res:
                        results.append(res)
                    pct = int((completed / (total or 1)) * 100)
                    bar_len = 24
                    filled = int(bar_len * completed / (total or 1))
                    bar = "█" * filled + "░" * (bar_len - filled)
                    sys.stdout.write(f"\r   [{bar}] {completed}/{total} ({pct}%) 🟢 {success} 성공 | 🔴 {failed} 실패")
                    sys.stdout.flush()
                elif etype == "complete":
                    total = evt.get("total", total)
                    success = evt.get("success", success)
                    failed = evt.get("failed", failed)
        except Exception as e:
            # Fall back to the non-streaming endpoint only when the stream never started; once
            # nodes have begun running the command, retrying would run it a second time.
            if total or completed:
                print(f"\n   ⚠️ 스트림이 끊겼습니다 ({e}). 이미 시작된 서버에서 명령이 계속 실행될 수 있습니다.")
                return {
                    "command": command,
                    "total_nodes": total,
                    "success_count": success,
                    "failed_count": failed,
                    "duration_sec": time.time() - start_time,
                    "results": results,
                    "error": str(e),
                }
            return self.client.execute_fleet(command, timeout_sec=timeout_sec, targets=targets)

        dur = time.time() - start_time
        print(f"\n   ✨ 완료: 총 {total}대 중 {success}대 성공 ({dur:.2f}초 소요)\n")
        return {
            "command": command,
            "total_nodes": total,
            "success_count": success,
            "failed_count": failed,
            "duration_sec": dur,
            "results": results,
        }



    def run_security_auditor(self, targets: Optional[List[str]] = None, fleet: bool = False) -> Dict[str, Any]:
        """Security Auditor Agent: CIS Benchmark & SSH hardening.

        fleet=True audits the nodes over SSH (hub /api/fleet/ssh; all nodes when targets is
        empty) and summarizes per user node group. Otherwise the hub's own sshd is audited."""
        if fleet:
            return self._run_fleet_security_auditor(targets)
        print("🛡️  [Security Auditor Agent] Inspecting SSH configurations and keys...")
        config = self.client.get_ssh_config()
        security = self.client.get_ssh_security()
        keys = self.client.get_authorized_keys()

        prompt = "Audit SSH daemon settings against CIS benchmarks and evaluate brute-force attack exposure."
        provider, analysis = self.llm.run_prompt(prompt, {"config": config, "security": security, "keys": keys})
        return {
            "agent": "SecurityAuditor",
            "security_score": security.get("score", 0),
            "rules": security.get("rules", []),
            "analysis": analysis,
            "provider": provider,
        }

    def _run_fleet_security_auditor(self, targets: Optional[List[str]]) -> Dict[str, Any]:
        from ssh_audit import llm_context, render_ssh_audit

        target_label = ",".join(targets) if targets else "all"
        print(f"🛡️  [Security Auditor Agent] Auditing sshd on {f'{len(targets)} servers' if targets else 'all servers'}...")
        try:
            report = self.client.fleet_ssh_audit(targets)
        except Exception as e:
            return {"agent": "SecurityAuditor", "target": target_label, "error": str(e)}
        groups = self.fleet_groups()
        summary = report.get("summary") or {}
        prompt = (
            "Audit the SSH daemon settings of these fleet nodes against CIS benchmarks. Summarize per node group, "
            "name the nodes and rules that fail, and say which nodes could not be audited."
        )
        context = {"fleet_ssh": llm_context(report, groups), "fleet_ssh_report": render_ssh_audit(report, groups)}
        provider, analysis = self.llm.run_prompt(prompt, context)
        return {
            "agent": "SecurityAuditor",
            "target": target_label,
            "security_score": summary.get("avg_score"),
            "fleet_summary": summary,
            "rules": [],
            "analysis": analysis,
            "provider": provider,
        }

    def run_remediation_agent(self, issue_type: str = "ssh_hardening") -> Dict[str, Any]:
        """Remediation Agent: Generates safe actionable plan requiring human approval."""
        print(f"🔧 [Remediation Agent] Generating remediation plan for {issue_type}...")
        security = self.client.get_ssh_security()

        actions = []
        for rule in security.get("rules", []):
            if rule.get("status") == "FAIL":
                rule_id = rule.get("rule_id")
                if rule_id == "SEC-SSH-001":
                    actions.append({
                        "id": "fix-root-login",
                        "title": "Disable Direct Root Login",
                        "command": 'echo "PermitRootLogin no" | sudo tee -a /etc/ssh/sshd_config.d/guardian.conf && sudo sshd -t',
                        "risk_level": "LOW",
                        "requires_approval": True,
                    })
                elif rule_id == "SEC-SSH-002":
                    actions.append({
                        "id": "fix-password-auth",
                        "title": "Enforce Key-Only SSH Authentication",
                        "command": 'echo "PasswordAuthentication no" | sudo tee -a /etc/ssh/sshd_config.d/guardian.conf && sudo sshd -t',
                        "risk_level": "HIGH",
                        "requires_approval": True,
                        "warning": "Ensure at least one valid public key is registered in authorized_keys before running this!",
                    })
                elif rule_id == "SEC-SSH-004":
                    actions.append({
                        "id": "fix-max-auth-tries",
                        "title": "Limit Maximum Authentication Attempts to 3",
                        "command": 'echo "MaxAuthTries 3" | sudo tee -a /etc/ssh/sshd_config.d/guardian.conf && sudo sshd -t',
                        "risk_level": "LOW",
                        "requires_approval": True,
                    })

        return {
            "agent": "RemediationAgent",
            "proposed_actions": actions,
            "requires_human_approval": True,
        }

    def run_supervisor_chat(self, user_query: str) -> Dict[str, Any]:
        """Supervisor Agent: Conversational reasoning using the active persona."""
        persona = self.active_persona
        # a character (format 2) has no system_prompt: its rendered persona block is the role line
        system_prompt = (character.body_text(persona) if character.is_character(persona)
                         else persona.get("system_prompt", "당신은 Server Guardian 수호자 AI입니다."))
        name = persona.get("name", "Supervisor Agent")
        role = persona.get("role", "AI Agent")

        # The persona's system_prompt is the role line, alone: no default "Linux System & SSH Security Engineer" header
        # and no persona JSON (temperature, focus lists) in the model's prompt. The context only feeds the no-LLM text.
        prompt = f"사용자 질의: {user_query}\n\n위 지침에 따라 답변하십시오. 받지 않은 측정값이나 서버 상태는 지어내지 마십시오."
        provider, response = self.llm.run_prompt(prompt, {"user_query": user_query, "persona": persona},
                                                 system=system_prompt)
        return {
            "agent": "SupervisorAgent",
            "persona_id": persona.get("id"),
            "persona_name": name,
            "analysis": response,
            "provider": provider,
        }

    def handle_query(
        self, user_query: str, target_host: Optional[str] = None, target_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Supervisor Agent: Smart intent classification and routing.

        target_ids (a resolved node group) scopes fleet diagnosis to those nodes. An explicit
        target_host or target_ids decides the scope; the fleet keywords in the query ("전체",
        "fleet", ...) only apply when no target was given (target_host None)."""
        lower = user_query.lower()
        keyword = next((w for w in FLEET_KEYWORDS if w in lower), None)
        if target_ids or target_host is not None:
            is_fleet = bool(target_ids) or target_host in ["all", "fleet"]
        else:
            is_fleet = keyword is not None

        check_security = any(k in lower for k in ["ssh", "보안", "security", "인증", "점수", "audit"])
        check_system = any(k in lower for k in ["시스템", "리소스", "자원", "metric", "상태", "진단", "diagnos", "cpu", "ram", "메모리", "디스크", "부하"])
        check_fix = any(k in lower for k in ["해결", "조치", "fix", "remediat", "플랜", "패치"])
        is_chat_identity = any(k in lower for k in ["누구", "너는", "당신", "소개", "안녕", "hello", "hi", "who are you", "무슨 일", "기능", "역할"])

        print(f"\n👑 [Supervisor Agent] Routing query: '{user_query}' (Target: {'ALL FLEET' if is_fleet else (target_host or 'local')})")
        scope = query_scope(target_host, target_ids, is_fleet, keyword)
        results = {}

        # 1. Identity / General Conversation -> Direct LLM Chat
        if is_chat_identity and not (check_security or check_system or check_fix or is_fleet):
            results["supervisor"] = self.run_supervisor_chat(user_query)
            return results

        # 2. Fleet-wide or Single Diagnostic
        if is_fleet:
            results["diagnostic"] = self.run_fleet_diagnostic(target_ids)
        elif check_system:
            results["diagnostic"] = self.run_diagnostic_agent(target_host)

        # 3. Security Audit
        if check_security:
            # A fleet, group or remote node target audits those nodes over SSH; local stays local.
            remote = target_host not in (None, "local", "all", "fleet")
            if is_fleet or remote:
                ids = target_ids or ([target_host] if remote else None)
                results["security"] = self.run_security_auditor(ids, fleet=True)
            else:
                results["security"] = self.run_security_auditor()

        # 4. Remediation
        if check_fix:
            results["remediation"] = self.run_remediation_agent()

        # Fallback for unclassified general queries -> LLM Supervisor Chat
        if not results:
            results["supervisor"] = self.run_supervisor_chat(user_query)
        elif "diagnostic" in results or "security" in results:
            results["scope"] = scope

        return results

    def execute_approved_action(self, action_id: str, command: str, target_host: Optional[str] = None) -> Dict[str, Any]:
        """Execute an action after explicit human approval."""
        print(f"⚡ [Execution] Running approved action ({action_id}): {command}")
        if target_host and target_host not in ["local", "all"]:
            return self.client.execute_remote_ssh(target_host, command)
        return self.client.execute_command(command)

"""
jev gate: an independent judgment on debate outcomes, via the jev-mcp server.

When the LLM judge approves, the debate is submitted to jev-mcp as a "verification" decision:
the approved approach is checked against concrete constraints (the judge's requirements, or
the critic's points as a fallback), plus the rationale and how hard the change is to reverse.

  PASSED    every constraint is satisfied, the rationale is sound and the change is reversible
  DISPUTED  a constraint is not clearly satisfied, the rationale is weak, or the change is
            hard to reverse (score >= 2 of 3)
  PENDING   no evaluation yet (no TYPESAFE_API_KEY, API failure, or jev-mcp unreachable)

Only PASSED releases the judge's proposed actions; otherwise they stay HELD.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from llm_runner import load_llm_env
from mcp_stdio import MCPError, StdioMCPClient

PASSED, DISPUTED, PENDING = "PASSED", "DISPUTED", "PENDING"
ADOPT, REVISE = "adopt", "revise"
MAX_CONSTRAINTS = 10
FALLBACK_CONSTRAINT = (
    "The approach resolves the red-team critique's security, edge-case and performance concerns."
)

CATEGORY_KEYWORDS = [
    ("security", ("ssh", "auth", "token", "보안", "인증", "security", "key", "firewall", "acl", "tls")),
    ("testing", ("test", "테스트", "coverage", "pytest")),
    ("data", ("database", "sqlite", "db", "backup", "백업", "데이터")),
    ("api", ("api", "endpoint", "rest", "http")),
    ("dependency", ("dependency", "library", "package", "upgrade", "의존성")),
    ("architecture", ("architecture", "design", "구조", "설계", "refactor")),
    ("infrastructure", ("server", "서버", "systemd", "kernel", "deploy", "배포", "network", "tailscale")),
]


def infer_category(text: str) -> str:
    lower = text.lower()
    for category, words in CATEGORY_KEYWORDS:
        if any(w in lower for w in words):
            return category
    return "other"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def critique_points(critique: str, limit: int = MAX_CONSTRAINTS) -> List[str]:
    """Numbered or bulleted points of the critique, as fallback verification constraints."""
    points = []
    for line in critique.splitlines():
        match = re.match(r"^\s*(?:\d+[.)]|[-*•])\s+(.*\S)", line)
        if not match:
            continue
        text = re.sub(r"[*_`#]+", "", match.group(1)).strip(" :")
        if len(text) >= 15:
            points.append(f"The approach addresses this concern: {_clip(text, 250)}")
        if len(points) >= limit:
            break
    return points


def build_decision(project_id: str, thread_id: str, task: str, proposal: str, critique: str,
                   judgment: str, constraints: Optional[List[str]] = None,
                   code_context: Optional[Dict[str, Any]] = None, decision: str = "",
                   actions: Optional[List[Dict[str, Any]]] = None, rationale: str = "") -> Dict[str, Any]:
    """Arguments for jev_submit_decision: verify the approved approach against constraints.

    The approach under review ("chosen") is the judge's final decision, not the expert's draft: the
    judge usually adopts parts of the critique, and the constraints describe that adjusted decision.
    decision is the judge's own summary (falls back to its judgment text); actions are the changes
    it proposed, shown so jev can spot a change that contradicts the decision.
    code_context is the snapshot of the code the agents read; its commit and files are recorded.
    rationale is the judge's own "rationale" field; without it the judgment prose outside the json
    block is used, which after a repair or re-ask can be only a short note about the correction.
    """
    prose = re.sub(r"```json.*?```", "", judgment, flags=re.DOTALL)
    prose = re.sub(r"(?im)^\s*VERDICT:.*$", "", prose)
    rationale = rationale.strip() or prose
    checks = [c for c in (constraints or []) if c.strip()][:MAX_CONSTRAINTS]
    source = "judge"
    if not checks:
        checks, source = critique_points(critique), "critique"
    if not checks:
        checks, source = [FALLBACK_CONSTRAINT], "fallback"
    code_line, tags = "", []
    if code_context:
        files = ", ".join(f["path"] for f in code_context.get("files", [])[:15])
        code_line = f"Code reviewed at commit {code_context.get('sha', '')[:12]}: {files}\n\n"
        tags = [f"commit:{code_context.get('sha', '')[:12]}"]
    changes = ""
    for action in (actions or [])[:5]:
        args = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
        target = args.get("file_path") or args.get("branch") or args.get("test_cmd") or args.get("command") or ""
        body = args.get("content") or args.get("message") or ""
        edits = args.get("edits") if isinstance(args.get("edits"), list) else []
        if edits:
            target = ", ".join(dict.fromkeys(str(e.get("file_path", "")) for e in edits if isinstance(e, dict)))
            body = " | ".join(f"{_clip(str(e.get('old', '')), 120)!r} -> {_clip(str(e.get('new', '')), 200)!r}"
                              for e in edits[:4] if isinstance(e, dict))
        changes += f"- {action.get('tool')} {target}: {_clip(str(body), 600)}\n"
    if changes:
        changes = f"\n\nChanges the judge proposed with this decision (must agree with it):\n{changes}"
    return {
        "title": _clip(f"[{project_id}] {task}", 120),
        "context": _clip(
            f"{code_line}Task: {task}\n\nOriginal proposal:\n{_clip(proposal, 1500)}\n\n"
            f"Red-team critique:\n{_clip(critique, 1200)}{changes}",
            4500,
        ),
        "category": infer_category(f"{task}\n{proposal}"),
        "shape": "verification",
        "constraints": checks,
        # The decision must state every constraint, so it gets more room than the other fields.
        "chosen": _clip(decision or prose.strip(), 3000) or _clip(proposal, 1500)
        or "The proposal approved by the judge.",
        "rationale": _clip(rationale, 2000) or "The judge approved the proposal.",
        "tags": ["server-guardian", project_id, thread_id, f"constraints:{source}", *tags],
        "status": "proposed",
    }


def gate_from_evaluation(eval_status: Optional[str], evaluation: Optional[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """Maps a jev-mcp evaluation summary to PASSED / DISPUTED / PENDING with reasons."""
    if eval_status != "done" or not evaluation:
        return PENDING, [f"jev evaluation {eval_status or 'unavailable'}"]
    reasons = []
    checks = evaluation.get("checks") or []
    for check in checks:
        if check.get("verdict") != "yes":
            reasons.append(f"constraint {check.get('verdict')} (noul {check.get('noul')}): {check.get('constraint')}")
    fit = evaluation.get("fit")
    if fit:  # selection-shaped decisions recorded before the verification gate
        if fit.get("recommended") != ADOPT:
            reasons.append(f"jev recommends '{fit.get('recommended')}' (confidence {fit.get('confidence')})")
        elif fit.get("band") == "low":
            reasons.append(f"jev confidence is low ({fit.get('confidence')})")
    elif not checks:
        reasons.append("jev returned no constraint checks")
    rationale = evaluation.get("rationale") or {}
    if rationale and rationale.get("verdict") != "yes":
        reasons.append(f"rationale judged {rationale.get('verdict')} (noul {rationale.get('noul')})")
    reversibility = evaluation.get("reversibility") or {}
    score = reversibility.get("score")
    if score is not None and score >= 2:
        reasons.append(f"hard to reverse: {reversibility.get('label')} (score {score})")
    return (DISPUTED, reasons) if reasons else (PASSED, [])


class JevGate:
    """Submits and re-checks debate decisions on the configured jev MCP server."""

    def __init__(self, server: str = "jev", timeout: float = 120.0):
        self.server = server
        self.timeout = timeout

    def _client(self) -> StdioMCPClient:
        base_env = {**os.environ, **load_llm_env()}
        return StdioMCPClient.from_config(self.server, base_env=base_env, timeout=self.timeout)

    def review(self, decision_args: Dict[str, Any]) -> Dict[str, Any]:
        """Submits a decision and returns {gate, reasons, jev_decision_id, eval_status, evaluation}."""
        try:
            with self._client() as client:
                result = client.call_tool("jev_submit_decision", decision_args)
        except MCPError as exc:
            return {"gate": PENDING, "reasons": [f"jev-mcp error: {exc}"], "jev_decision_id": None,
                    "eval_status": "error", "evaluation": None}
        return self._from_decision(result)

    def recheck(self, jev_decision_id: Optional[int], decision_args: Dict[str, Any]) -> Dict[str, Any]:
        """Retries queued evaluations and reads the latest result (resubmits if never recorded)."""
        if not jev_decision_id:
            return self.review(decision_args)
        try:
            with self._client() as client:
                client.call_tool("jev_process_queue", {"limit": 20})
                result = client.call_tool("jev_get_decision", {"decision_id": int(jev_decision_id)})
        except MCPError as exc:
            return {"gate": PENDING, "reasons": [f"jev-mcp error: {exc}"], "jev_decision_id": jev_decision_id,
                    "eval_status": "error", "evaluation": None}
        return self._from_decision(result)

    @staticmethod
    def _from_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
        gate, reasons = gate_from_evaluation(decision.get("eval_status"), decision.get("evaluation"))
        note = decision.get("evaluation_note")
        if gate == PENDING and note:
            reasons.append(str(note))
        evaluation = decision.get("evaluation") or {}
        reasons.extend(
            f"jev finding: {f}" for f in evaluation.get("findings", [])
            if gate != PASSED and not f.startswith("Constraint ")
        )
        return {
            "gate": gate,
            "reasons": reasons,
            "jev_decision_id": decision.get("id"),
            "eval_status": decision.get("eval_status"),
            "evaluation": decision.get("evaluation"),
        }

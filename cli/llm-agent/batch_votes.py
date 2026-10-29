"""Critic votes on a stored plan (docs/design/batch-execution.md, chapter 6): the question, the parsing, the counting.

No model and no host here; the debate engine asks each critic in its own session and hands the answer to
`cast_vote`. A vote counts only for the plan it names (the first 12 characters of plan_ops.plan_hash). An objection
counts only when it cites a server-state check this debate really ran, so an objection nobody can verify is advice,
and a critic who casts no valid vote abstains (which counts as approving, but is stored as such).
"""
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import batch_gate
import command_sets
from batch_gate import ABSTAIN, APPROVE, OPPOSE

HASH_CHARS = 12
MAX_REASON = 600
MAX_CITES = 8
MAX_SCRIPT_SHOWN = 8192  # above the 4096-byte script limit: the critic sees all of what the hash covers
MAX_ARGS_SHOWN = 2000

VOTE_BLOCK = re.compile(r"```vote[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

# The answer format, repeated as the format reminder of the turn so a repaired answer keeps the block.
VOTE_FORMAT = (
    'End your answer with exactly one fenced block:\n```vote\n'
    '{"plan": "<the 12-character plan hash above>", "vote": "approve" or "oppose", "reason": "one or two sentences", '
    '"cites": ["[#3 disk_usage n1]"]}\n```\n'
    "Oppose only for a concrete risk. An objection counts only when cites names at least one server check result "
    "of this debate in exactly the form [#number check host]; without one it is kept as advice and the plan is not "
    "held. If you have no concrete objection, approve."
)


def short_hash(plan_hash: str) -> str:
    return plan_hash[:HASH_CHARS]


def _shown(args: Dict[str, Any]) -> str:
    text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= MAX_ARGS_SHOWN else text[:MAX_ARGS_SHOWN] + " ...(cut)"


def _step_line(step: Dict[str, Any], preview: Dict[str, Any]) -> List[str]:
    args = dict(step.get("arguments") or {})
    host = args.pop("target_host", "?")
    args.pop("script", None)  # shown below in full, not squeezed into one line
    reversible = "undoable" if batch_gate.is_reversible(step) else "NOT undoable"
    lines = [f"- {step['step']} on {host}: {step['tool']} {_shown(args)} "
             f"[risk {step['risk']}, {reversible}{', RUNS WITHOUT A PERSON' if step.get('free_script') else ''}]"]
    script = preview.get("script") or (step.get("arguments") or {}).get("script")
    if isinstance(script, str) and script:
        shown = script if len(script) <= MAX_SCRIPT_SHOWN else script[:MAX_SCRIPT_SHOWN] + "\n... (cut)"
        lines.append("  script:\n" + "\n".join("    " + line for line in shown.splitlines()))
    return lines


def vote_task(task: str, goal: str, plan_hash: str, steps: List[Dict[str, Any]],
              previews: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """The question one critic is asked: the plan as it is stored, and how to answer."""
    previews = previews or {}
    lines = [f"Task: {task}", "", f"=== PLAN VOTE: plan {short_hash(plan_hash)} ===",
             f"Goal: {goal or '(none given)'}", "Steps (in the order they are stored):"]
    for step in steps:
        lines += _step_line(step, previews.get(str(step["step"]), {}))
    if any(step.get("free_script") for step in steps):
        lines += ["", "NOTE: the steps marked RUNS WITHOUT A PERSON run as soon as the critics have voted, with no human "
                      "reading them first and no way to undo them. Your vote is the only review: read each script "
                      "line by line and oppose on any concrete risk."]
    lines += ["",
              "The judge approved this plan; you vote on exactly this plan. Where the risk depends on the state of "
              "the hosts, check it first with an ops block, then answer.",
              VOTE_FORMAT]
    return "\n".join(lines)


def repair_message(problem: str) -> str:
    return ("=== VOTE CHECK ===\n"
            f"Your answer cannot be used as a vote: {problem}.\n"
            "Reply with ONLY the vote block, nothing else.\n" + VOTE_FORMAT)


def _parse_block(raw: str, plan_hash: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "the vote block is not valid JSON"
    if not isinstance(data, dict):
        return None, "the vote block is not a JSON object"
    named = str(data.get("plan") or "").strip().lower()
    if len(named) < HASH_CHARS or not plan_hash.startswith(named):
        return None, f"it names plan {named[:40]!r}, not {short_hash(plan_hash)}"
    kind = data.get("vote")
    if kind not in (APPROVE, OPPOSE):
        return None, 'vote must be "approve" or "oppose"'
    cites = data.get("cites", [])
    if not isinstance(cites, list):
        return None, "cites must be a list"
    return {"vote": kind, "reason": str(data.get("reason") or "").strip()[:MAX_REASON],
            "cites": [str(c).strip()[:200] for c in cites if isinstance(c, str)][:MAX_CITES]}, None


def parse_vote(text: str, plan_hash: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(vote, None) or (None, why not). Only blocks that name this plan count. An answer with several valid blocks
    (a quoted one, an injected one) is read in the direction of caution: the last valid oppose wins over any approve,
    so one forged approve block cannot erase an objection; a block that is broken is ignored when another is valid."""
    blocks = VOTE_BLOCK.findall(text or "")
    if not blocks:
        return None, "it has no ```vote block"
    parsed, problem = [], None
    for raw in blocks:
        vote, why = _parse_block(raw, plan_hash)
        if vote is not None:
            parsed.append(vote)
        else:
            problem = why
    if not parsed:
        return None, problem
    opposed = [v for v in parsed if v["vote"] == OPPOSE]
    return (opposed or parsed)[-1], None


def cast_vote(critic: str, text: str, plan_hash: str, verified: Callable[[str], bool],
              failed: Optional[str] = None) -> Dict[str, Any]:
    """The stored vote of one critic: {"critic", "vote", "reason", "cites", "counted"[, "note"]}.

    cites keeps only the verified citations (verified(cite) must say yes). An oppose is counted only with at least
    one of them; without, it stays an oppose with counted false and a note. No valid vote, or failed (the critic
    could not be asked), is an abstention."""
    if failed:
        return {"critic": critic, "vote": ABSTAIN, "reason": "", "cites": [], "counted": False, "note": failed}
    vote, problem = parse_vote(text, plan_hash)
    if vote is None:
        return {"critic": critic, "vote": ABSTAIN, "reason": "", "cites": [], "counted": False,
                "note": f"no valid vote: {problem}"}
    cites = [c for c in vote["cites"] if verified(c)]
    out = {"critic": critic, "vote": vote["vote"], "reason": vote["reason"], "cites": cites, "counted": False}
    if vote["vote"] == OPPOSE:
        out["counted"] = bool(cites)
        if not cites:
            out["note"] = "no verified server-state citation: kept as advice"
    return out


def verifier(ops: Any) -> Callable[[str], bool]:
    """verified(cite) for cast_vote: the cite names at least one check this debate served, and nothing unserved.

    OpsChecks.unverified_citations answers (0, []) for text with no [#n check host] in it; that is no citation, not
    a clean one, so it must not pass."""
    def verified(cite: str) -> bool:
        found, bad = ops.unverified_citations(cite)
        return found >= 1 and not bad
    return verified


def votes_enabled(project: Any) -> bool:
    """Whether a plan of this ops project is put to the critics: the project opted in to automatic batches, or it
    offers a batch tool. A plain fleet-ops project (neither) is never asked, so its prompts and cost stay as they were."""
    policy = getattr(project, "policy", None) or {}
    if (policy.get("batch") or {}).get("auto") is True:
        return True
    return any(tool in command_sets.TOOLS for tool in policy.get("allowed_actions") or [])

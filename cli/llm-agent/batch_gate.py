"""The batch gate (docs/design/batch-execution.md, chapter 7): may a plan run without a person?

`decide` is pure: it reads a plan's steps, the critics' votes and the project's policy, and answers with whether
every condition for an automatic run holds, plus every reason when one does not. Nothing here runs a command, asks
a model or reads a host; the debate engine stores the answer and (from B3) plan_ops runs a released plan.

The gate only ever says "no" more often than a person would: an irreversible, high-risk, invalid or objected step
always goes to a person, and an unknown tool counts as irreversible. One opt-in exception: with policy.batch.scripts
"vote", an ad-hoc script on a host of policy.batch.script_groups needs a clean vote with at least one approval.
"""
from typing import Any, Callable, Dict, List, Optional

import action_ops
import command_sets
import ops_actions
import ops_packages

APPROVE, OPPOSE, ABSTAIN = "approve", "oppose", "abstain"
VOTES = (APPROVE, OPPOSE, ABSTAIN)
NO_VOTE = "no vote was cast"

# Steps in these states are not "ready to run": the plan has already left the state the votes were cast on.
READY = ("PROPOSED",)


def _always(value: bool) -> Callable[[Dict[str, Any], str], bool]:
    return lambda _args, _risk: value


def _docker_prune(args: Dict[str, Any], _risk: str) -> bool:
    """Dangling images and the build cache come back by pulling or building again; containers and images do not."""
    return args.get("scope") in ops_actions.LOW_SCOPES


def _install_is_contained(args: Dict[str, Any]) -> bool:
    """A system package manager install is undone by removing the package. npm, brew and the other managers run third
    party code while installing (lifecycle scripts, formula code) that removal does not undo, unless scripts are off."""
    return args.get("manager") in ops_packages.SYSTEM_MANAGERS or args.get("ignore_scripts") is True


# Can the step be undone? tool -> (arguments, risk) -> bool. run_command_set is not here: the set declares it
# (step_facts puts that into the step), and a script is never reversible. A tool missing from the table is not.
REVERSIBLE: Dict[str, Callable[[Dict[str, Any], str], bool]] = {
    "ops:apt_clean": _always(True),  # the package lists and cached archives are fetched again
    "ops:docker_prune": _docker_prune,
    "ops:journal_vacuum": _always(False),
    "ops:truncate_logs": _always(False),
    "ops:remove_paths": _always(False),
    "ops:remove_old_kernels": _always(False),
    "ops:restart_service": _always(True),
    ops_packages.INSTALL: lambda args, risk: risk != "high" and _install_is_contained(args),
    ops_packages.REMOVE: _always(False),
    "ops:run_script": _always(False),
}


def is_reversible(step: Dict[str, Any]) -> bool:
    """Whether a step can be undone (see REVERSIBLE); a command set says so itself, but never when it is high risk."""
    tool, risk = step.get("tool", ""), step.get("risk", "high")
    if tool == "ops:run_command_set":
        return bool(step.get("reversible")) and risk != "high"
    rule = REVERSIBLE.get(tool)
    return bool(rule and rule(step.get("arguments") or {}, risk))


def tally(votes: List[Dict[str, Any]]) -> Dict[str, int]:
    """Votes per kind, and how many objections count (an oppose with a verified server-state check)."""
    out = {kind: 0 for kind in VOTES}
    out["objections"] = 0
    for item in votes:
        kind = item.get("vote")
        if kind in out:
            out[kind] += 1
        if kind == OPPOSE and item.get("counted"):
            out["objections"] += 1
    return out


def step_problem(item: Dict[str, Any]) -> Optional[str]:
    """Why one step cannot run without a person, or None. The gate asks it for every step of a plan, and the runner
    asks it again for a step that is about to start, with the risk measured at that moment."""
    name = item.get("step")
    if item.get("status") not in READY:
        return f"step {name} is {item.get('status')}, not ready to run"
    if item.get("unmeasured") and (item.get("tool") in ops_packages.TOOLS or item.get("tool") in command_sets.TOOLS):
        # Package tools rate their risk from the measurement; a command set or script that was not measured is one
        # the critics voted on without seeing the state it will meet.
        return f"step {name} ({item.get('tool')}) was not measured at proposal, so its risk is unknown"
    if item.get("unconfirmed"):
        # A set saved from the dashboard declares its own risk and reversibility; until the operator has confirmed
        # exactly this text, a person decides (docs/design/batch-execution.md, 12.4).
        return f"step {name}: command set {(item.get('arguments') or {}).get('set')} has not been confirmed for automatic runs"
    if item.get("free_script") and item.get("tool") == command_sets.TOOL_SCRIPT:
        # policy.batch.scripts "vote": a script on a host of script_groups is released by the vote instead of a
        # person. Only this tool on those hosts; a high set or a high package action never gets here.
        return None
    if item.get("risk") == "high":
        return f"step {name} ({item.get('tool')}) is high risk"
    if not is_reversible(item):
        return f"step {name} ({item.get('tool')}) is irreversible"
    return None


def _conditions(steps: List[Dict[str, Any]], votes: List[Dict[str, Any]], policy: Dict[str, Any],
                runs_today: int, jev_passed: bool, env: Optional[Dict[str, str]]) -> List[str]:
    """The reasons a plan cannot run on its own, without the opt-in (policy.batch.auto) condition.

    A project that has not opted in has no daily cap yet (0, which opting in requires to be raised): the shadow
    decision does not fail on a cap nobody set."""
    reasons: List[str] = []
    if not action_ops.auto_apply_enabled(env):
        reasons.append("automatic runs are switched off (GUARDIAN_AUTO_APPLY or GUARDIAN_UI_ACTIONS)")
    if not jev_passed:
        reasons.append("the jev gate did not pass")
    for item in votes:
        if item.get("vote") == OPPOSE and item.get("counted"):
            reasons.append(f"objection from {item.get('critic')}: {str(item.get('reason') or '')[:200]}")
    if not steps:
        reasons.append("the plan has no steps")
    for item in steps:
        problem = step_problem(item)
        if problem:
            reasons.append(problem)
    if any(item.get("free_script") for item in steps) and tally(votes)[APPROVE] == 0:
        # An abstention counts as approval for every other plan; a script nobody has read must not run on that.
        reasons.append("a script would run without a person, and no critic cast an approving vote")
    limit = int(policy.get("max_steps") or 0)
    if len(steps) > limit:
        reasons.append(f"{len(steps)} steps, more than max_steps {limit}")
    cap = int(policy.get("max_plans_per_day") or 0)
    if runs_today >= cap and (cap > 0 or policy.get("auto") is True):
        reasons.append(f"{runs_today} plans ran today, at most {cap} per day")
    return reasons


def decide(steps: List[Dict[str, Any]], votes: List[Dict[str, Any]], policy: Dict[str, Any], runs_today: int,
           jev_passed: bool, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """{"auto", "would_auto", "reasons", "notes", "tally"}.

    steps: {"step", "tool", "arguments", "risk", "status"[, "reversible"]} per step. votes: {"critic", "vote",
    "reason", "cites", "counted"} per critic. policy: the project's policy.batch. env defaults to os.environ.

    auto is true only when every condition holds, including policy.batch.auto. would_auto is the same without
    that condition: what the gate would have decided had the project opted in (shadow mode, B2). Reasons lists
    every unmet condition, not just the first. A critic who cast no valid vote counts as approving; when nobody
    cast one the plan can still run, and notes says so.
    """
    reasons = _conditions(steps, votes, policy, runs_today, jev_passed, env)
    would_auto = not reasons
    opted_in = policy.get("auto") is True
    if not opted_in:
        reasons = ["policy.batch.auto is off", *reasons]
    counts = tally(votes)
    notes = [NO_VOTE] if counts[APPROVE] + counts[OPPOSE] == 0 else []
    return {"auto": would_auto and opted_in, "would_auto": would_auto, "reasons": reasons, "notes": notes,
            "tally": counts}

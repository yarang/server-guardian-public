"""Automatic running of a released plan (docs/design/batch-execution.md, chapters 7 and 8; phase B3).

The debate engine calls `release` after the vote pass stored a gate that says `auto`. Nothing stored is trusted
as it stands: the plan hash is computed again, the gate is decided again from the stored steps and votes under the
policy and kill switches of this moment, and the start is claimed in the database (which also enforces the daily cap
of plans). Each step is checked once more, on the measured state of its host, right before it runs.
"""
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import action_ops
import batch_gate
import plan_ops
from db_manager import DecisionDBManager
from mcp_client import MCPClient


def auto_actor(project_id: str) -> str:
    """decided_by of the steps of an auto-started plan. Not `auto:<project>`: that actor is counted against the cap
    of single actions (policy.max_auto_runs_per_day), and the two caps must stay independent."""
    return f"autoplan:{project_id}"[:64]


def _fresh_batch_policy(reload: Optional[Callable[[], Any]], fallback: Dict[str, Any],
                        fallback_free: Optional[Set[str]] = None) -> Tuple[Dict[str, Any], Set[str]]:
    """(policy.batch, hosts where scripts run without a person) as saved and resolved right now. `reload` loads the
    project again (the engine's own client holds the policy of the debate's start, which may be hours old, and a
    host may have left a group since the vote); without it what was given at release time is all there is."""
    if reload is None:
        return fallback, set(fallback_free or ())
    project = reload()
    return (getattr(project, "policy", None) or {}).get("batch") or {}, plan_ops.free_script_hosts(project)


def step_guard(db: DecisionDBManager, plan_id: str, plan_hash: str, reload: Optional[Callable[[], Any]] = None,
               env: Optional[Dict[str, str]] = None, policy: Optional[Dict[str, Any]] = None,
               free: Optional[Set[str]] = None) -> plan_ops.Guard:
    """The check made under the host lock, before each step: the plan is still the one the critics voted on, the
    operator has not switched automatic runs off since (kill switches, a saved policy.batch.auto), and the step,
    rated on the state measured now, is still one the gate would release (not high risk, undoable, measured).
    A reason ends the step STALE and its dependents SKIPPED; steps of other hosts meet the same check."""
    def guard(action: Dict[str, Any], now: Dict[str, Any]) -> Optional[str]:
        if not action_ops.auto_apply_enabled(env):
            return "automatic runs were switched off while the plan ran"
        try:
            batch, free_now = _fresh_batch_policy(reload, policy or {}, free)
        except Exception as exc:  # fail closed: a policy that cannot be read is not an opt-in
            return f"the project policy could not be read again: {type(exc).__name__}"
        if batch.get("auto") is not True:
            return "policy.batch.auto was switched off while the plan ran"
        if plan_ops.plan_hash(db, plan_id) != plan_hash:
            return "the plan changed while it ran"
        current = db.get_action(action["id"]) or action
        problem = batch_gate.step_problem(plan_ops.gate_item(current, now, free=free_now))
        return f"it no longer qualifies for an automatic run: {problem}" if problem else None
    return guard


def _refused(reasons: List[str]) -> Dict[str, Any]:
    return {"started": False, "reasons": reasons}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def release(db: DecisionDBManager, mcp: Any, plan_id: str, env: Optional[Dict[str, str]] = None,
            load_project: Optional[Callable[[str], Any]] = None, **run_options: Any) -> Dict[str, Any]:
    """Runs a plan the batch gate released. Returns {"started": False, "reasons": [...]} when anything does not
    hold (the plan then waits for a person, untouched), else {"started": True, "actor", "counts", "steps"}
    (plus "error" when the run itself could not be driven to the end). load_project(project_id) reads the project
    as saved now; the engine passes it, so a policy changed during a long debate counts."""
    plan = db.get_plan(plan_id)
    if plan is None:
        return _refused([f"plan {plan_id} not found"])
    stored = plan.get("gate")
    if not isinstance(stored, dict) or not stored.get("auto"):
        return _refused(["the plan has no stored gate record that released it"])
    current = plan_ops.plan_hash(db, plan_id)
    if not current or stored.get("plan_hash") != current or plan.get("plan_hash") != current:
        return _refused(["the plan changed after the critics voted on it"])
    try:
        project = load_project(plan.get("project_id")) if load_project else plan_ops._plan_project(mcp, plan)
    except Exception as exc:  # fail closed
        return _refused([f"the project could not be loaded: {type(exc).__name__}: {exc}"[:300]])
    if project is None or not getattr(project, "is_ops", False):
        return _refused(["only plans of ops projects run by themselves"])
    if getattr(project, "id", None) != plan.get("project_id"):
        return _refused(["the project does not match the plan's project"])
    if load_project:
        # The engine's own client holds the project of the debate's start; the run validates every step against
        # allowed_actions and deny_paths, so it gets a client for the project as saved now.
        mcp = MCPClient(project=project, agent_client=getattr(mcp, "_agent_client", None))
    policy = (project.policy or {}).get("batch") or {}
    # jev_passed: the vote pass only ran behind a passed jev gate; a plan still HELD has steps that are not READY.
    free = plan_ops.free_script_hosts(project)
    now = batch_gate.decide(plan_ops.gate_steps(db, plan_id, free=free), plan.get("votes") or [], policy,
                            plan_ops.auto_plans_today(db, project.id), jev_passed=True, env=env)
    if not now["auto"]:
        return _refused(now["reasons"])
    actor = auto_actor(project.id)
    at = _now()
    why = db.claim_auto_plan(plan_id, project.id, int(policy.get("max_plans_per_day") or 0), actor, at[:10], at,
                             current)
    if why:
        return _refused([why])
    outcome: Dict[str, Any]
    try:
        reload = (lambda: load_project(project.id)) if load_project else None
        guard = step_guard(db, plan_id, current, reload, env, policy, free)
        result = plan_ops.run_plan(db, mcp, plan_id, actor, guard=guard, **run_options)
        outcome = {"counts": result["counts"]}
        report: Dict[str, Any] = {"started": True, "actor": actor, "counts": result["counts"], "steps": result["steps"]}
    except Exception as exc:  # the start is spent either way: a person looks at the plan, which may be half run
        outcome = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        report = {"started": True, "actor": actor, "error": str(exc), "counts": {}, "steps": []}
    finally:
        db.finish_auto_plan(plan_id, {"finished_at": _now(), **outcome})
    return report

"""
Plans: an ordered set of catalog actions on one or more hosts, decided together (docs/design/fleet-provisioning.md,
chapters 7.2, 7.3 and 8).

The judge's decision carries `"plan": {"goal", "stop_plan"?, "steps": [{"id", "host", "tool", "args", "requires"?,
"on_failure"?}]}`. Every step is stored as an ordinary decision_actions row (plan_id, step, requires, on_failure), so
preview, decided_by, audit labels and STALE work per step as they do for a single action.

Validation has two levels. A *structural* error (unknown shape, duplicate id, a cycle, a limit) refuses the whole
plan: nothing is stored. A *step* error (the action itself does not validate on its host) stores that step as INVALID
with the reason; the plan stays reviewable and the step counts as failed when the plan runs.

Running: hosts in parallel (at most MAX_PARALLEL_HOSTS), one step per host at a time in plan order. A step starts when
every step it requires is EXECUTED. A step that fails (FAILED, STALE, INVALID, REJECTED) stops its host unless its
on_failure is "continue"; the not-started steps then end SKIPPED. With stop_plan, the first failure skips every step
that has not started. There is no automatic rollback.
"""

import getpass
import hashlib
import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import action_ops
import batch_gate
import command_sets
import ops_actions
import ops_targets
from action_ops import ACTOR_PATTERN, CONFLICT, INVALID_INPUT, NOT_FOUND, Result, _error
from db_manager import DecisionDBManager
from mcp_client import ActionValidationError, parse_judge_block

MAX_STEPS = 100
MAX_STEPS_PER_HOST = 20
MAX_PARALLEL_HOSTS = 2
# How long a plan waits for a host that another action still holds before the step fails without running.
HOST_WAIT_SECONDS = 120.0
HOST_POLL_SECONDS = 2.0

STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
PLAN_ID = re.compile(r"^plan_[A-Za-z0-9_]{1,100}$")
ON_FAILURE = ("stop", "continue")
# Statuses after which a step will not change again by itself.
TERMINAL = ("EXECUTED", "FAILED", "STALE", "SKIPPED", "INVALID", "REJECTED")
# Terminal statuses that count as a failure of the step itself (they stop the plan with stop_plan, and their host
# with on_failure stop). A rejected step was a person's decision, a skipped one a consequence.
FAILURES = ("FAILED", "STALE", "INVALID")


class PlanError(ValueError):
    """The plan as a whole cannot be stored (the message names the first problem)."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_plan(judge_text: str) -> Optional[Dict[str, Any]]:
    """The `plan` object of the judge's decision block, or None when it has none."""
    plan = parse_judge_block(judge_text).get("plan")
    return plan if isinstance(plan, dict) else None


# --- validation (no model, no host) -----------------------------------------------------------


def normalize_plan(plan: Any) -> Dict[str, Any]:
    """Checks the shape and graph of a plan. Returns {"goal", "stop_plan", "steps"} with steps normalized
    (id, host, tool, args, requires, on_failure) in plan order. Raises PlanError."""
    if not isinstance(plan, dict):
        raise PlanError(["plan must be an object"])
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError(["plan.steps must be a non-empty list"])
    if len(raw_steps) > MAX_STEPS:
        raise PlanError([f"a plan has at most {MAX_STEPS} steps, this one has {len(raw_steps)}"])

    problems: List[str] = []
    steps: List[Dict[str, Any]] = []
    seen: set = set()
    for index, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            problems.append(f"step {index}: must be an object")
            continue
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not STEP_ID.match(step_id):
            problems.append(f"step {index}: id must be a short name (letters, digits, - and _)")
            continue
        if step_id in seen:
            problems.append(f"step {step_id}: duplicate id")
            continue
        seen.add(step_id)
        host, tool, args = raw.get("host"), raw.get("tool"), raw.get("args", {})
        if not isinstance(host, str) or not action_ops.HOST_ID.match(host):
            problems.append(f"step {step_id}: host must be a host id")
        if not isinstance(tool, str) or not ops_actions.is_catalog(tool):
            problems.append(f"step {step_id}: tool must be an ops catalog action, got {tool!r}")
        if not isinstance(args, dict):
            problems.append(f"step {step_id}: args must be an object")
        requires = raw.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            problems.append(f"step {step_id}: requires must be a list of step ids")
            requires = []
        on_failure = raw.get("on_failure", "stop")
        if on_failure not in ON_FAILURE:
            problems.append(f"step {step_id}: on_failure must be one of {', '.join(ON_FAILURE)}")
        steps.append({"id": step_id, "host": host, "tool": tool, "args": args if isinstance(args, dict) else {},
                      "requires": list(dict.fromkeys(requires)), "on_failure": on_failure})
    if problems:
        raise PlanError(problems)

    ids = {s["id"] for s in steps}
    for step in steps:
        for need in step["requires"]:
            if need == step["id"]:
                problems.append(f"step {step['id']}: requires itself")
            elif need not in ids:
                problems.append(f"step {step['id']}: requires unknown step {need}")
    per_host: Dict[str, int] = {}
    for step in steps:
        per_host[step["host"]] = per_host.get(step["host"], 0) + 1
    for host, count in per_host.items():
        if count > MAX_STEPS_PER_HOST:
            problems.append(f"host {host}: at most {MAX_STEPS_PER_HOST} steps per host, this plan has {count}")
    if problems:
        raise PlanError(problems)

    cycle = find_cycle(steps)
    if cycle:
        raise PlanError([f"the steps wait for each other in a cycle: {' -> '.join(cycle)}"])
    return {"goal": str(plan.get("goal") or "")[:300], "stop_plan": plan.get("stop_plan") is True, "steps": steps}


def dependency_graph(steps: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Per step id the ids it waits for: its `requires` plus, on its host, the step before it in plan order."""
    graph: Dict[str, List[str]] = {}
    previous: Dict[str, str] = {}
    for step in steps:
        waits = list(step["requires"])
        before = previous.get(step["host"])
        if before and before not in waits:
            waits.append(before)
        graph[step["id"]] = waits
        previous[step["host"]] = step["id"]
    return graph


def find_cycle(steps: List[Dict[str, Any]]) -> Optional[List[str]]:
    """A cycle of the combined graph (explicit requires plus the per-host plan order), else None."""
    graph = dependency_graph(steps)
    state: Dict[str, int] = {}  # 1 on the current path, 2 done

    def visit(node: str, path: List[str]) -> Optional[List[str]]:
        state[node] = 1
        path.append(node)
        for nxt in graph[node]:
            if state.get(nxt) == 1:
                return path[path.index(nxt):] + [nxt]
            if nxt not in state:
                found = visit(nxt, path)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return None

    for node in graph:
        if node not in state:
            found = visit(node, [])
            if found:
                return found
    return None


def plan_as_actions(plan: Any) -> List[Dict[str, Any]]:
    """The steps of a plan in the shape of single proposals, for the jev gate to review next to the judge's
    actions. [] when the plan does not pass normalize_plan (it is refused when recording)."""
    try:
        steps = normalize_plan(plan)["steps"]
    except PlanError:
        return []
    return [{"tool": s["tool"], "arguments": {**s["args"], "target_host": s["host"]},
             "reason": f"plan step {s['id']}"} for s in steps]


# --- recording ---------------------------------------------------------------------------------


def new_plan_id(db: DecisionDBManager, thread_id: str, round_num: int) -> str:
    base = f"plan_{thread_id.removeprefix('thread_')}_r{round_num}"
    candidate, n = base, 1
    while db.get_plan(candidate) is not None:
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def record_plan(db: DecisionDBManager, mcp: Any, thread_id: str, round_num: int, plan: Any,
                allowed: Optional[List[str]] = None, held: bool = False,
                base_sha: Optional[str] = None, seen: Any = None) -> Dict[str, Any]:
    """Validates and stores a plan. Nothing is executed.

    Raises PlanError for a structural problem (nothing stored). Otherwise returns {"plan_id", "steps": [{"id",
    "action_id", "step", "host", "tool", "status", "error"}]}; a step that does not validate is stored INVALID.
    A step that waits for another one is measured at proposal time like any action, but a measurement error is not
    an error there: the thing it acts on may be created by the step it requires (a unit its package installs), so
    the preview is stored as deferred and measured again right before the step runs.
    """
    normal = normalize_plan(plan)
    steps = normal["steps"]
    plan_id = new_plan_id(db, thread_id, round_num)
    # The same project the single actions of this debate belong to.
    project_id = getattr(mcp, "project_id", None)
    db.add_plan(plan_id, thread_id, round_num, project_id, normal["goal"], normal["stop_plan"])
    out: List[Dict[str, Any]] = []
    for step in steps:
        tool = step["tool"]
        arguments = {**step["args"], "target_host": step["host"]}
        reason = f"plan {plan_id} step {step['id']}"
        status, error = ("HELD" if held else "PROPOSED"), None
        try:
            arguments = action_ops.validate_proposal(
                mcp, {"tool": tool, "arguments": arguments}, allowed, base_sha, seen, thread_id)
        except ActionValidationError as exc:
            status, error = "INVALID", str(exc)
            arguments = getattr(exc, "arguments", arguments)
            arguments = arguments if isinstance(arguments, dict) else {"raw": arguments}
        action_id = db.add_action(thread_id, round_num, tool, arguments, reason, status, base_sha=base_sha,
                                  project_id=project_id, plan_id=plan_id, step=step["id"],
                                  requires=step["requires"], on_failure=step["on_failure"])
        if error:
            db.update_action(action_id, "INVALID", result={"error": error})
        else:
            preview = mcp.ops_preview(tool, arguments)
            if preview.get("error") and step["requires"]:
                # The hash stays: a set edited after the proposal must still be STALE when the step runs.
                preview = {"deferred": True, "reason": preview["error"],
                           **({"script_sha": preview["script_sha"]} if preview.get("script_sha") else {})}
            db.set_action_preview(action_id, preview)
            if preview.get("refusal"):
                status, error = "INVALID", preview["refusal"]
                db.update_action(action_id, "INVALID", result={"error": error})
        out.append({"id": step["id"], "action_id": action_id, "host": step["host"], "tool": tool,
                    "status": status, "error": error})
    return {"plan_id": plan_id, "steps": out}


# --- identity and gate inputs (docs/design/batch-execution.md, chapters 6 and 7) ----------------------------


def plan_hash(db: DecisionDBManager, plan_id: str) -> Optional[str]:
    """What a vote is cast on: sha256 of the canonical JSON of the stored plan, or None when there is no such plan.

    It is computed from the stored rows, not from the judge's text, so the batch gate recomputes the same value right
    before it releases a plan: any difference means the plan changed after the critics saw it. A command set or script
    contributes the hash of the text that will run (the preview's script_sha, which a deferred preview keeps).
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        return None
    steps = []
    for row in db.plan_steps(plan_id):
        args = dict(row.get("arguments") or {})
        host = args.pop("target_host", None)
        steps.append({"id": row.get("step"), "host": host, "tool": row.get("tool"), "args": args,
                      "requires": row.get("requires") or [], "on_failure": row.get("on_failure"),
                      "sha": (row.get("preview") or {}).get("script_sha")})
    body = {"goal": plan.get("goal") or "", "stop_plan": bool(plan.get("stop_plan")), "steps": steps}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def free_script_hosts(project: Any) -> Set[str]:
    """The hosts where an ad-hoc script may run without a person (policy.batch.scripts "vote"): the project's targets
    that belong to policy.batch.script_groups, resolved now. Empty unless the project opted in to automatic plans and
    to "vote", and empty whenever the groups cannot be read (fail closed)."""
    batch = (getattr(project, "policy", None) or {}).get("batch") or {}
    if batch.get("scripts") != "vote" or batch.get("auto") is not True or not batch.get("script_groups"):
        return set()
    try:
        found = ops_targets.group_hosts(project, batch["script_groups"])
    except Exception:
        return set()
    return set() if found["error"] else set(found["ids"])


def gate_item(row: Dict[str, Any], preview: Optional[Dict[str, Any]] = None,
              free: Optional[Set[str]] = None) -> Dict[str, Any]:
    """One stored step as batch_gate reads it: step, tool, arguments, risk, status and, for a command set, what the
    set declares about being undoable. With `preview` the risk is rated from that measurement instead of the stored one.
    `free` is free_script_hosts of the project: an ad-hoc script aimed at one of them is marked free_script."""
    measured = {**row, "preview": preview} if preview is not None else row
    view = action_ops.action_view(measured, None)
    item = {"step": row.get("step"), "tool": row["tool"], "arguments": row.get("arguments") or {},
            "risk": view["risk"], "status": row["status"]}
    if row["tool"] == command_sets.TOOL_SET:
        item["reversible"] = command_sets.reversible(row["tool"], row.get("arguments"))
        try:
            item["unconfirmed"] = not command_sets.is_confirmed(command_sets.get_set((row.get("arguments") or {}).get("set")))
        except ValueError:
            item["unconfirmed"] = True
    if row["tool"] == command_sets.TOOL_SCRIPT and free and (row.get("arguments") or {}).get("target_host") in free:
        item["free_script"] = True
    shown = (preview if preview is not None else row.get("preview")) or {}
    if shown.get("deferred") or shown.get("error"):
        item["unmeasured"] = True  # the risk was rated without a measurement
    return item


def gate_steps(db: DecisionDBManager, plan_id: str, free: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """The steps of a plan as batch_gate.decide reads them (`free`: see gate_item)."""
    return [gate_item(row, free=free) for row in db.plan_steps(plan_id)]


def auto_plans_today(db: DecisionDBManager, project_id: str) -> int:
    """How many plans of the project the engine started on its own today (UTC), whatever their outcome; counted from
    the stored gate records (decision_plans.gate.auto_run), which a later human action cannot change."""
    return db.count_auto_plans(project_id, time.strftime("%Y-%m-%d", time.gmtime()))


# --- running -----------------------------------------------------------------------------------


def _fail_not_run(db: DecisionDBManager, action_id: int, error: str, status: str = "FAILED",
                  actor: Optional[str] = None) -> None:
    """Settles a PROPOSED step that never started."""
    db.update_action(action_id, status, result={"status": status, "error": error, "not_run": True},
                     decided_by=actor, expected_status="PROPOSED")


def _skip(db: DecisionDBManager, action_id: int, reason: str, actor: Optional[str]) -> None:
    db.update_action(action_id, "SKIPPED", result={"status": "SKIPPED", "error": reason}, decided_by=actor,
                     expected_status="PROPOSED")


def _acquire_host(stack: ExitStack, host: str, wait_seconds: float, poll: float) -> None:
    """Takes the per-host action lock, waiting up to wait_seconds for another action to finish.
    Raises RuntimeError when the host stays busy."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            stack.enter_context(action_ops.host_lock(host))
            return
        except RuntimeError as exc:
            if "another action is running" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(poll)


Guard = Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]


def _run_step(db: DecisionDBManager, mcp: Any, action_id: int, actor: str, wait_seconds: float,
              poll: float, guard: Optional[Guard] = None) -> str:
    """Runs one PROPOSED step through the same guards as a single action. Returns its final status.

    guard(action, measured_now) is asked under the host lock, after the step was measured again and before it runs;
    a reason ends the step STALE (the automatic path uses it to re-check risk and reversibility on the real state)."""
    action = db.get_action(action_id)
    if action is None or action["status"] != "PROPOSED":
        return (action or {}).get("status", "FAILED")
    try:
        client = action_ops._client(mcp, action)
        try:
            client.validate(action["tool"], action["arguments"])
        except ActionValidationError as exc:
            _fail_not_run(db, action_id, f"no longer passes validation: {exc}", "INVALID", actor)
            return "INVALID"
        host = (action.get("arguments") or {}).get("target_host") or "local"
        with ExitStack() as stack:
            _acquire_host(stack, host, wait_seconds, poll)
            now = client.ops_preview(action["tool"], action["arguments"])
            if now.get("error"):
                _fail_not_run(db, action_id, f"cannot measure before running: {now['error']}", actor=actor)
                return "FAILED"
            stored = action.get("preview") or {}
            if stored.get("deferred"):
                # Nothing to compare with: the proposal could not measure it. Keep what is measured now, so the
                # step's record shows what it ran against, and still enforce the refusal rules.
                if now.get("refusal"):
                    db.update_action(action_id, "STALE", result={"error": now["refusal"], "preview_now": now},
                                     decided_by=actor, expected_status="PROPOSED")
                    return "STALE"
                if stored.get("script_sha") and now.get("script_sha") != stored["script_sha"]:
                    db.update_action(action_id, "STALE", result={"error": ops_actions.drift(stored, now),
                                                                 "preview_now": now},
                                     decided_by=actor, expected_status="PROPOSED")
                    return "STALE"
                db.set_action_preview(action_id, now)
            else:
                reason = ops_actions.drift(stored, now)
                if reason:
                    db.update_action(action_id, "STALE", result={"error": reason, "preview_now": now},
                                     decided_by=actor, expected_status="PROPOSED")
                    return "STALE"
            reason = guard(action, now) if guard else None
            if reason:
                db.update_action(action_id, "STALE", result={"error": reason, "preview_now": now},
                                 decided_by=actor, expected_status="PROPOSED")
                return "STALE"
            # The row may have been written while this run waited for the host lock; run what is stored now, and
            # only when it is what was measured and checked above.
            latest = db.get_action(action_id)
            if latest is None or latest["tool"] != action["tool"] or latest["arguments"] != action["arguments"]:
                db.update_action(action_id, "STALE", result={"error": "the step changed while it waited for the host"},
                                 decided_by=actor, expected_status="PROPOSED")
                return "STALE"
            action_ops._execute(db, client, latest, action_id, actor)
    except (RuntimeError, ValueError) as exc:
        # The host stayed busy, the project could not be resolved, or another run took the step.
        _fail_not_run(db, action_id, str(exc), actor=actor)
    except Exception as exc:  # one step's unexpected error must not abort the steps of other hosts
        _fail_not_run(db, action_id, f"{type(exc).__name__}: {exc}", actor=actor)
    return (db.get_action(action_id) or {}).get("status", "FAILED")


def _step_ok(step: Dict[str, Any]) -> bool:
    result = step.get("result") or {}
    verify = result.get("verify")
    return step["status"] == "EXECUTED" and not (isinstance(verify, dict) and verify.get("ok") is False)


def parallel_hosts(project: Any) -> int:
    """Hosts a plan of this project runs at once: policy.batch.max_parallel_hosts, else MAX_PARALLEL_HOSTS."""
    batch = (getattr(project, "policy", None) or {}).get("batch")
    value = batch.get("max_parallel_hosts") if isinstance(batch, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else MAX_PARALLEL_HOSTS


def _plan_project(mcp: Any, plan: Dict[str, Any]) -> Any:
    client = mcp.for_project(plan.get("project_id")) if hasattr(mcp, "for_project") else mcp
    return getattr(client, "project", None)


def run_plan(db: DecisionDBManager, mcp: Any, plan_id: str, actor: Optional[str] = None,
             max_parallel: Optional[int] = None, host_wait: float = HOST_WAIT_SECONDS,
             host_poll: float = HOST_POLL_SECONDS, guard: Optional[Guard] = None) -> Dict[str, Any]:
    """Runs the PROPOSED steps of a plan. Raises LookupError (unknown plan) and RuntimeError (cannot start).

    Steps that already ended keep their state, so a plan whose run was interrupted can be run again: only the
    PROPOSED steps are started. Returns {"plan_id", "counts", "steps": [{"step", "action_id", "host", "status"}]}.
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        raise LookupError(f"plan {plan_id} not found")
    who = actor or getpass.getuser()
    steps = db.plan_steps(plan_id)
    held = [s["step"] for s in steps if s["status"] == "HELD"]
    if held:
        raise RuntimeError(f"plan {plan_id} is HELD by the jev gate (steps {', '.join(held)}); "
                           f"run --jev-recheck {plan['thread_id']} first")
    if any(s["status"] == "RUNNING" for s in steps):
        raise RuntimeError(f"plan {plan_id} has a step that is still RUNNING; wait for it or mark it failed first")
    if not any(s["status"] == "PROPOSED" for s in steps):
        raise RuntimeError(f"plan {plan_id} has no step waiting to run")

    with ExitStack() as stack:
        try:
            stack.enter_context(action_ops.host_lock(plan_id))  # one run of a plan at a time
        except RuntimeError:
            raise RuntimeError(f"plan {plan_id} is already running") from None
        if max_parallel is None:
            try:
                max_parallel = parallel_hosts(_plan_project(mcp, plan))
            except ValueError as exc:  # the project file was removed or edited into an invalid state
                raise RuntimeError(f"plan {plan_id}: its project is not available: {exc}") from None
        _drive(db, mcp, plan, who, max_parallel, host_wait, host_poll, guard)
    return plan_status(db, plan_id)


def _settle_skips(db: DecisionDBManager, plan: Dict[str, Any], who: str, active: set) -> None:
    """Moves to SKIPPED every PROPOSED step (not in active) that can no longer start: its plan was stopped, an
    earlier step on its host failed with on_failure stop, or a step it requires did not succeed. Repeats until
    nothing changes, because a skipped step makes the steps that require it skip too."""
    changed = True
    while changed:
        changed = False
        steps = db.plan_steps(plan["id"])
        by_step = {s["step"]: s for s in steps}
        stopped = plan["stop_plan"] and any(s["status"] in FAILURES for s in steps)
        failed_hosts = {_host(s) for s in steps if s["status"] in FAILURES and (s.get("on_failure") or "stop") == "stop"}
        for step in steps:
            if step["status"] != "PROPOSED" or step["id"] in active:
                continue
            reason = None
            if stopped:
                reason = "the plan was stopped by an earlier failure (stop_plan)"
            elif _host(step) in failed_hosts:
                reason = f"an earlier step on {_host(step)} failed and its on_failure is stop"
            else:
                bad = next((w for w in step.get("requires") or []
                            if w in by_step and by_step[w]["status"] in TERMINAL and not _step_ok(by_step[w])), None)
                if bad:
                    reason = f"step {bad} it requires did not succeed ({by_step[bad]['status']})"
            if reason:
                _skip(db, step["id"], reason, who)
                changed = True


def _host(step: Dict[str, Any]) -> str:
    return (step.get("arguments") or {}).get("target_host") or "local"


def _drive(db: DecisionDBManager, mcp: Any, plan: Dict[str, Any], who: str, max_parallel: int,
           host_wait: float, host_poll: float, guard: Optional[Guard] = None) -> None:
    """The scheduler loop: starts every ready step (the first undecided step of each idle host, at most
    max_parallel at once) and settles the steps that can no longer start. One loop rather than a thread per host,
    so a step that waits for a step on another host never holds a thread that step needs."""
    plan_id = plan["id"]
    running: Dict[Future, Tuple[int, str]] = {}  # future -> (action id, host)
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        while True:
            active = {action_id for action_id, _ in running.values()}
            _settle_skips(db, plan, who, active)
            steps = db.plan_steps(plan_id)
            by_step = {s["step"]: s for s in steps}
            busy = {host for _, host in running.values()}
            for host in dict.fromkeys(_host(s) for s in steps):
                if host in busy or len(running) >= max_parallel:
                    continue
                # Plan order on a host: its first step that has not ended goes first.
                head = next((s for s in steps if _host(s) == host and s["status"] not in TERMINAL), None)
                if head is None or head["status"] != "PROPOSED" or head["id"] in active:
                    continue
                if all(w in by_step and _step_ok(by_step[w]) for w in head.get("requires") or []):
                    running[pool.submit(_run_step, db, mcp, head["id"], who, host_wait, host_poll, guard)] = (head["id"], host)
            if not running:
                # Nothing runs and nothing could start: what is left waits for a step that will not run.
                for step in db.plan_steps(plan_id):
                    if step["status"] == "PROPOSED":
                        _skip(db, step["id"], "it can no longer start in this run", who)
                return
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done:
                running.pop(future)
                future.result()  # _run_step settles its own errors


def plan_status(db: DecisionDBManager, plan_id: str) -> Dict[str, Any]:
    steps = db.plan_steps(plan_id)
    counts: Dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"plan_id": plan_id, "counts": counts,
            "steps": [{"step": s["step"], "action_id": s["id"], "host": (s.get("arguments") or {}).get("target_host"),
                       "status": s["status"]} for s in steps]}


def reject_plan(db: DecisionDBManager, plan_id: str, actor: Optional[str] = None) -> int:
    """Rejects every PROPOSED or HELD step of a plan. Returns how many were rejected."""
    who = actor or getpass.getuser()
    return sum(1 for s in db.plan_steps(plan_id) if action_ops.reject_action(db, s["id"], actor=who))


# --- views and JSON commands -------------------------------------------------------------------


def plan_view(db: DecisionDBManager, mcp: Any, plan_id: str) -> Optional[Dict[str, Any]]:
    """The plan, its steps (with the risk of each) grouped by host, and the status counts."""
    plan = db.get_plan(plan_id)
    if plan is None:
        return None
    rows = db.plan_steps(plan_id)
    steps = [{**action_ops.action_view(s, mcp), "reversible": batch_gate.is_reversible(gate_item(s))} for s in rows]
    hosts: Dict[str, List[str]] = {}
    for s in steps:
        hosts.setdefault((s.get("arguments") or {}).get("target_host") or "local", []).append(s["step"])
    counts: Dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    levels = {"low": 0, "medium": 1, "high": 2}
    top = max((s.get("risk", "unknown") for s in steps), key=lambda r: levels.get(r, 2), default="low")
    return {**plan, "steps": steps, "hosts": [{"host": h, "steps": ids} for h, ids in hosts.items()],
            "counts": counts, "risk": top, "runnable": counts.get("PROPOSED", 0) > 0 and not counts.get("HELD")}


def list_plans_json(db: DecisionDBManager, mcp: Any, thread_id: Optional[str] = None,
                    project_id: Optional[str] = None) -> Result:
    plans = [plan_view(db, mcp, p["id"]) for p in reversed(db.list_plans(thread_id or None, project_id or None))]
    return 0, {"plans": plans}


def show_plan_json(db: DecisionDBManager, mcp: Any, plan_id: str) -> Result:
    if not PLAN_ID.match(plan_id):
        return _error(INVALID_INPUT, "plan id: use plan_ and letters, digits or _")
    view = plan_view(db, mcp, plan_id)
    if view is None:
        return _error(NOT_FOUND, f"plan {plan_id} not found")
    return 0, {"plan": view}


def run_plan_json(db: DecisionDBManager, mcp: Any, plan_id: str, actor: str, **options: Any) -> Result:
    """Runs an approved plan. The caller has already shown it to a person who confirmed it."""
    if not ACTOR_PATTERN.match(actor):
        return _error(INVALID_INPUT, "actor: use 1-64 letters, digits or @:._-")
    if not PLAN_ID.match(plan_id):
        return _error(INVALID_INPUT, "plan id: use plan_ and letters, digits or _")
    try:
        outcome = run_plan(db, mcp, plan_id, actor, **options)
    except LookupError as exc:
        return _error(NOT_FOUND, str(exc))
    except RuntimeError as exc:
        return _error(CONFLICT, str(exc))
    ok = set(outcome["counts"]) <= {"EXECUTED"}
    return 0, {"ok": ok, "outcome": outcome, "plan": plan_view(db, mcp, plan_id)}


def reject_plan_json(db: DecisionDBManager, mcp: Any, plan_id: str, actor: str) -> Result:
    if not ACTOR_PATTERN.match(actor):
        return _error(INVALID_INPUT, "actor: use 1-64 letters, digits or @:._-")
    if not PLAN_ID.match(plan_id):
        return _error(INVALID_INPUT, "plan id: use plan_ and letters, digits or _")
    if db.get_plan(plan_id) is None:
        return _error(NOT_FOUND, f"plan {plan_id} not found")
    rejected = reject_plan(db, plan_id, actor)
    if not rejected:
        return _error(CONFLICT, f"plan {plan_id} has no PROPOSED or HELD step to reject")
    return 0, {"ok": True, "rejected": rejected, "plan": plan_view(db, mcp, plan_id)}

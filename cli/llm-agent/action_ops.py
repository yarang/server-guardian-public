"""
Human-approved execution of judge-proposed tool actions.

Lifecycle: PROPOSED -> (person approves) RUNNING -> EXECUTED | FAILED, or PROPOSED|HELD -> REJECTED.
An ops cleanup action (ops_actions) whose target grew too much since it was proposed goes PROPOSED -> STALE instead
of running. Server tools hold a per-host lock while they run, so two actions never work on one host at once.
HELD actions (jev gate not passed yet) become PROPOSED after a passing --jev-recheck. A tool that raises
is stored as FAILED; a RUNNING action whose process was killed is moved to FAILED by --fail-stale-action.
Proposals that fail validation are stored as INVALID and can never run. The steps of a plan (plan_ops) are actions
too, but they run only with their plan; a step that never started ends SKIPPED.
"""

import fcntl
import getpass
import json
import os
import re
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

from db_manager import CLOSED_STATUS, DecisionDBManager, data_dir
import artifact_ops
import ops_actions
import ops_packages
import patch_ops
from mcp_client import SERVER_TOOLS, TOOLS, WRITE_TOOLS, ActionValidationError, MCPClient
from project_registry import ProjectValidationError, WorkspaceBusy, workspace_lock


def _tool_spec(name: str) -> Dict[str, Any]:
    return next((t for t in TOOLS if t["name"] == name), {})


def _client(mcp: Any, action: Dict[str, Any]) -> MCPClient:
    """The MCPClient for an action's project: mcp is either one client or a resolver
    (project_registry.Workspaces) that maps each action to its project's workspace."""
    return mcp.for_action(action) if hasattr(mcp, "for_action") else mcp


def record_proposals(
    db: DecisionDBManager,
    mcp: MCPClient,
    thread_id: str,
    round_num: int,
    proposals: List[Dict[str, Any]],
    allowed: Optional[List[str]] = None,
    held: bool = False,
    base_sha: Optional[str] = None,
    seen: Any = None,
) -> List[Dict[str, Any]]:
    """Validates and stores the judge's proposals. Nothing is executed.

    held=True stores valid proposals as HELD (waiting for the jev gate) instead of PROPOSED.
    base_sha is the commit of the code the agents read; write tools only run while the workspace is there.
    seen is the code context pack the agents read; file edits must stay inside it (proposal_checks).
    Without a base_sha (no code context) a file-changing proposal is based on the workspace HEAD at this moment,
    so it can be published and the stale-base check still applies to it.
    """
    stored = []
    for proposal in proposals:
        tool = str(proposal.get("tool", ""))
        arguments = proposal.get("arguments", {})
        reason = str(proposal.get("reason", ""))[:1000]
        base = base_sha
        if base is None and tool in FILE_TOOLS and mcp.workspace is not None:
            from code_context import head_sha

            base = head_sha(mcp.workspace)
        try:
            arguments = validate_proposal(mcp, proposal, allowed, base, seen, thread_id)
            status, error = ("HELD" if held else "PROPOSED"), None
        except ActionValidationError as exc:
            status, error = "INVALID", str(exc)
            arguments = getattr(exc, "arguments", arguments)
            arguments = arguments if isinstance(arguments, dict) else {"raw": arguments}
        action_id = db.add_action(thread_id, round_num, tool or "<missing>", arguments, reason, status, base_sha=base,
                                  project_id=getattr(mcp, "project_id", None))
        if error:
            db.update_action(action_id, "INVALID", result={"error": error})
        elif ops_actions.is_catalog(tool):
            # What the cleanup would delete now; the run compares against it (drift) and the dialog shows it.
            preview = mcp.ops_preview(tool, arguments)
            db.set_action_preview(action_id, preview)
            if preview.get("refusal"):
                # The simulation broke a package rule (a protected package would go): never offered for approval.
                status, error = "INVALID", preview["refusal"]
                db.update_action(action_id, "INVALID", result={"error": error})
        stored.append({"id": action_id, "tool": tool, "status": status, "error": error})
    return stored


def validate_proposal(mcp: MCPClient, proposal: Dict[str, Any], allowed: Optional[List[str]],
                      base_sha: Optional[str], seen: Any, thread_id: Optional[str] = None) -> Dict[str, Any]:
    """The checks that decide whether a proposal is stored as INVALID. Returns the validated arguments.

    Raises ActionValidationError; its `repairable` flag is set when reading more code and resending could fix it.
    """
    arguments = mcp.validate(str(proposal.get("tool", "")), proposal.get("arguments", {}), allowed)
    try:
        proposal_checks(mcp, str(proposal.get("tool", "")), arguments, base_sha, seen, thread_id)
    except ActionValidationError as exc:
        exc.arguments = arguments  # the normalized arguments are what an INVALID record keeps
        raise
    return arguments


def invalid_proposals(mcp: MCPClient, proposals: List[Dict[str, Any]], allowed: Optional[List[str]],
                      base_sha: Optional[str], seen: Any, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Proposals record_proposals would store as INVALID, without storing anything.

    Each item is {"index", "tool", "error", "repairable"}.
    """
    found = []
    for index, proposal in enumerate(proposals):
        try:
            validate_proposal(mcp, proposal, allowed, base_sha, seen, thread_id)
        except ActionValidationError as exc:
            found.append({"index": index, "tool": str(proposal.get("tool", "")), "error": str(exc),
                          "repairable": bool(getattr(exc, "repairable", False))})
    return found


def proposal_checks(mcp: MCPClient, tool: str, arguments: Dict[str, Any], base_sha: Optional[str], seen: Any,
                    thread_id: Optional[str] = None) -> None:
    """Checks a file change against the code the judge read. Raises ActionValidationError.

    - apply_patch: every replaced text lies inside the shown code and matches exactly once at base_sha.
    - execute_code_action: an existing file may only be overwritten when it was shown completely.
    - write_artifact: base_version names the thread's latest version (repairable: the judge can resend it).
    """
    if tool == artifact_ops.TOOL:
        if thread_id and mcp.project_id:
            try:
                artifact_ops.ArtifactStore(mcp.project_id, thread_id).check_base(arguments.get("base_version"))
            except artifact_ops.ArtifactError as exc:
                error = ActionValidationError(str(exc))
                error.repairable = True
                raise error from exc
        return
    reader = patch_ops.git_reader(mcp.workspace, base_sha)
    try:
        if tool == "jev_mcp:apply_patch":
            patch_ops.check_seen(arguments["edits"], seen)
            patch_ops.plan(arguments["edits"], reader)
        elif tool == "jev_mcp:execute_code_action":
            path = str(mcp._workspace_path(arguments["file_path"]).relative_to(mcp.workspace))
            if reader(path) is not None and (seen is None or path not in seen.whole):
                raise patch_ops.PatchError(
                    f"{path} exists and was not shown in full; change it with jev_mcp:apply_patch instead"
                )
    except patch_ops.PatchError as exc:
        error = ActionValidationError(str(exc))
        error.repairable = exc.repairable
        raise error from exc


def action_diff(action: Dict[str, Any], mcp: MCPClient) -> Optional[str]:
    """Unified diff of a file-changing action against its base commit (or the workspace), if computable."""
    args = action.get("arguments") or {}
    if mcp.workspace is None:  # a project without a repository: no files to diff
        return None
    reader = patch_ops.git_reader(mcp.workspace, action.get("base_sha"))
    try:
        if action["tool"] == "jev_mcp:apply_patch":
            original, updated = patch_ops.plan(patch_ops.normalize_edits(args.get("edits")), reader)
        elif action["tool"] == "jev_mcp:execute_code_action" and isinstance(args.get("content"), str):
            path = str(args.get("file_path", ""))
            original, updated = {path: reader(path)}, {path: args["content"]}
        else:
            return None
    except (patch_ops.PatchError, OSError, ValueError):
        return None
    return patch_ops.unified_diff(original, updated)


def _describe_package_preview(preview: Dict[str, Any]) -> List[str]:
    """What the simulation made at proposal time would change: one line per item, then the running services."""
    if preview.get("error"):
        return [f"Preview: {preview['error']}"]
    items = preview.get("items") or []
    lines = [f"Preview: the simulation would change {len(items)} package(s) when proposed"]
    lines.extend(f"  {i}" for i in items[:40])
    if len(items) > 40:
        lines.append(f"  … {len(items) - 40} more")
    return lines


def describe_action(action: Dict[str, Any], mcp: Optional[MCPClient] = None) -> str:
    """Human-readable summary shown before approval."""
    spec = _tool_spec(action["tool"])
    lines = [
        f"Action #{action['id']}  [{action['status']}]  thread {action['thread_id']} round {action['round_num']}",
        f"Tool:   {action['tool']}  (risk: "
        f"{ops_actions.risk(action['tool'], action.get('arguments'), action.get('preview')) if ops_actions.is_catalog(action['tool']) else spec.get('risk', 'unknown')})",
        f"Reason: {action.get('reason') or '-'}",
    ]
    if action.get("base_sha"):
        lines.append(f"Base:   {action['base_sha']} (write tools run only while the workspace is at this commit)")
    if action.get("preview"):
        preview = action["preview"]
        if action["tool"] in ops_packages.TOOLS:
            lines.extend(_describe_package_preview(preview))
        else:
            lines.append(f"Preview: would free {ops_actions.human(preview.get('bytes'))} when proposed"
                         + (f" ({preview['error']})" if preview.get("error") else ""))
    args = dict(action.get("arguments") or {})
    if action["tool"] == artifact_ops.TOOL and isinstance(args.get("content"), str):
        lines.extend(_describe_artifact(action, args, getattr(_client(mcp, action), "project", None) if mcp is not None else None))
    elif action["tool"] == "jev_mcp:execute_code_action" and isinstance(args.get("content"), str):
        content = args.pop("content")
        lines.append(f"Args:   {json.dumps(args, ensure_ascii=False)}")
        lines.append(f"Content ({len(content)} chars):")
        lines.extend("  | " + line for line in content.splitlines()[:60])
        if content.count("\n") >= 60:
            lines.append("  | …")
    else:
        lines.append(f"Args:   {json.dumps(args, ensure_ascii=False, indent=2)}")
    diff = action_diff(action, _client(mcp, action) if mcp is not None else MCPClient())
    if diff:
        lines.append("Diff vs base:")
        lines.extend("  " + line for line in diff.splitlines()[:200])
    if action.get("result"):
        lines.append(f"Result: {json.dumps(action['result'], ensure_ascii=False)[:500]}")
    return "\n".join(lines)


def _describe_artifact(action: Dict[str, Any], args: Dict[str, Any], project: Any = None) -> List[str]:
    """The approval screen of a write_artifact action: where it lands, what the checks found, the head of the text."""
    content = args["content"]
    shown = {key: value for key, value in args.items() if key != "content"}
    lines = [f"Args:   {json.dumps(shown, ensure_ascii=False)}", f"Content ({len(content.encode('utf-8'))} bytes):"]
    lines.extend("  | " + artifact_ops.printable(line) for line in content.splitlines()[:60])
    if content.count("\n") >= 60:
        lines.append("  | …")
    project = action.get("project_id")
    latest = None
    if project:
        latest = artifact_ops.ArtifactStore(project, action["thread_id"]).latest()
    lines.append("Stores as: " + artifact_ops.format_version((latest or 0) + 1)
                 + (f" (now: {artifact_ops.format_version(latest)})" if latest else " (the first version)"))
    # The same checks as the write, with the project's own types and size limit.
    try:
        report = artifact_ops.check_arguments(args, project)
    except artifact_ops.ArtifactError as exc:
        report = artifact_ops.Report(errors=[f"L1: {exc}"])
    lines.extend(f"Check:  {text}" for text in [*report.errors, *report.warnings])
    return lines


def stale_reason(action: Dict[str, Any], mcp: MCPClient) -> Optional[str]:
    """Why a write action must not run because the workspace moved past the code the judge read, else None.

    Actions without any base_sha (rows stored before proposals recorded the workspace HEAD) are not checked.
    """
    if action["tool"] == artifact_ops.TOOL:
        if not (action.get("project_id") and action.get("thread_id")):
            return None
        try:
            artifact_ops.ArtifactStore(action["project_id"], action["thread_id"]).check_base(
                (action.get("arguments") or {}).get("base_version"))
        except artifact_ops.ArtifactError as exc:
            return str(exc)
        return None
    base = action.get("base_sha")
    if not base or action["tool"] not in WRITE_TOOLS:
        return None
    from code_context import head_sha

    head = head_sha(mcp.workspace)
    if head == base:
        return None
    return f"workspace moved from {base[:12]} to {(head or 'unknown')[:12]}; debate again on the current code"


FILE_TOOLS = ("jev_mcp:apply_patch", "jev_mcp:execute_code_action")

# Actions of resident task patches (resident-open-tools.md rule 5 / OD4): their thread is a task, not
# a debate, so no jev gate passed and no `auto:engine` path may ever run them, whatever
# GUARDIAN_AUTO_APPLY says. A person approves or rejects them like any other action.
TASK_THREAD_PREFIX = "thread_task_"


def is_task_patch_action(action: Dict[str, Any]) -> bool:
    """Whether the action is a resident task's patch (thread thread_task_<task id>)."""
    return str(action.get("thread_id") or "").startswith(TASK_THREAD_PREFIX)

# decided_by of an action the engine ran itself; ACTOR_PATTERN accepts it, humans show up under their own name.
AUTO_ACTOR = "auto:engine"
_OFF_VALUES = ("off", "0", "false", "no")
# Project kinds whose actions may run without a person (None: no project, tests and embedding); an ops project
# only through its auto_run policy (ops_auto_approvable). A design project never does, whatever its stored
# actions say.
AUTO_APPLY_KINDS = (None, "code", "ops")


def auto_apply_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Whether code-change actions run without a person's approval (default on).

    GUARDIAN_AUTO_APPLY=off turns it off; GUARDIAN_UI_ACTIONS=off (the hub's "no action runs without the CLI"
    switch) turns it off too, so the engine can never do what the dashboard is forbidden to do.
    """
    env = os.environ if env is None else env
    return (env.get("GUARDIAN_AUTO_APPLY", "").strip().lower() not in _OFF_VALUES
            and env.get("GUARDIAN_UI_ACTIONS", "").strip().lower() != "off")


def is_auto_approvable(action: Dict[str, Any]) -> bool:
    """Whether an action may skip the approval tab: a file change that only writes into the workspace clone.

    Everything else stays with a person: run_tests and server_remediation (they execute code), and an
    execute_code_action asking for run_test, which would run the workspace's tests as a side effect. A
    resident task patch never skips it either (rule 5 / OD4: a task has no debate verdict). Changes
    are not published; the publish step stays the human gate.
    """
    if action.get("tool") not in FILE_TOOLS:
        return False
    if is_task_patch_action(action):
        return False
    return not (action["tool"] == "jev_mcp:execute_code_action" and (action.get("arguments") or {}).get("run_test"))


def ops_auto_actor(project_id: str) -> str:
    """decided_by of a cleanup action an ops project's auto_run policy ran (docs/design/server-ops-project.md, 7.5)."""
    return f"auto:{project_id}"


def auto_runs_today(db: DecisionDBManager, project_id: str) -> int:
    """How many actions the ops project's auto_run started today (UTC); a run counts whatever its outcome."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    actor = ops_auto_actor(project_id)
    return sum(1 for a in db.list_actions(project_id=project_id)
               if a.get("decided_by") == actor and str(a.get("updated_at") or "").startswith(today))


def ops_auto_approvable(db: DecisionDBManager, client: Any, action: Dict[str, Any]) -> bool:
    """Whether an ops project's policy lets the engine run this action itself: a low-risk catalog action named in
    policy.auto_run, while today's auto runs of the project stay under policy.max_auto_runs_per_day."""
    policy = getattr(getattr(client, "project", None), "policy", None) or {}
    tool = action.get("tool", "")
    if tool not in (policy.get("auto_run") or []) or not ops_actions.is_catalog(tool):
        return False
    if tool in ops_actions.BATCH_TOOLS:  # a set or script is released by a plan's batch gate, never one by one
        return False
    if ops_actions.risk(tool, action.get("arguments")) != "low":
        return False
    return auto_runs_today(db, client.project_id) < int(policy.get("max_auto_runs_per_day") or 0)


def auto_run_actions(db: DecisionDBManager, mcp: Any, action_ids: List[int],
                     on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
                     ops: bool = True) -> List[Dict[str, Any]]:
    """Runs the auto-approvable ones of the given PROPOSED actions, lowest id first, as AUTO_ACTOR.

    In an ops project the auto-approvable ones are those its auto_run policy allows (ops_auto_approvable), run as
    auto:<project>. ops=False leaves ops actions alone (the engine passes it when the jev gate is switched off: an
    ops auto run needs a real jev PASSED).

    Ascending order keeps sibling_hashes chaining valid (the judge designs siblings against one commit). The
    chain stops at the first action that does not end EXECUTED (failed, stale, workspace busy, no longer valid):
    later siblings may build on it, so they stay PROPOSED for a person. Returns one {"id", "tool", "status",
    "error"} per attempted action; on_result sees each as it settles.
    """
    outcomes: List[Dict[str, Any]] = []
    if not auto_apply_enabled():
        return outcomes
    for action_id in sorted(action_ids):
        action = db.get_action(action_id)
        if action is None or action["status"] != "PROPOSED" or action.get("plan_id"):
            continue  # a plan step only runs with its plan, approved by a person
        if is_task_patch_action(action):
            continue  # a resident task patch never runs itself (rule 5 / OD4), whatever this switch says
        try:
            client = _client(mcp, action)
        except ValueError:
            continue
        # the kind of the action's own project: a Workspaces resolver has none, and a project may have been re-saved
        if getattr(client, "kind", None) not in AUTO_APPLY_KINDS:
            break
        if getattr(client, "kind", None) == "ops":
            if not ops or not ops_auto_approvable(db, client, action):
                continue
            actor = ops_auto_actor(client.project_id)
        elif is_auto_approvable(action):
            actor = AUTO_ACTOR
        else:
            continue
        try:
            result = run_action(db, mcp, action_id, confirm=lambda _action: True, actor=actor)
            status, error = db.get_action(action_id)["status"], result.get("error")
        except (RuntimeError, LookupError) as exc:
            status, error = (db.get_action(action_id) or {}).get("status", "PROPOSED"), str(exc)
        outcome = {"id": action_id, "tool": action["tool"], "status": status, "error": error}
        outcomes.append(outcome)
        if on_result is not None:
            on_result(outcome)
        if status != "EXECUTED":
            break
    return outcomes


def sibling_hashes(db: DecisionDBManager, action: Dict[str, Any]) -> Dict[str, str]:
    """Per file, the content hash the last EXECUTED sibling action left behind.

    Siblings are executed file-changing actions of the same thread with the same (non-empty) base_sha; the
    judge designs them together against one commit, so they may build on each other's uncommitted changes.
    The hashes come from the `file_hashes` each action recorded in its result. The latest execution wins
    (applied_at_ns, then id); an action without that record never counts.
    """
    base = action.get("base_sha")
    if not base:
        return {}
    done = [a for a in db.list_actions(action["thread_id"], status="EXECUTED")
            if a["id"] != action["id"] and a.get("base_sha") == base and a["tool"] in FILE_TOOLS
            and isinstance(a.get("result"), dict) and isinstance(a["result"].get("file_hashes"), dict)]
    return last_file_hashes(done)


def last_file_hashes(done: List[Dict[str, Any]]) -> Dict[str, str]:
    """Per file, the hash recorded by the latest of the given executed file actions (applied_at_ns, then id)."""
    done = sorted(done, key=lambda a: (a["result"].get("applied_at_ns", 0), a["id"]))
    hashes: Dict[str, str] = {}
    for a in done:
        hashes.update({p: h for p, h in a["result"]["file_hashes"].items() if isinstance(h, str)})
    return hashes


def run_action(
    db: DecisionDBManager,
    mcp: MCPClient,
    action_id: int,
    confirm: Callable[[Dict[str, Any]], bool],
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-validates a PROPOSED action, asks confirm(), then executes it exactly once."""
    action = db.get_action(action_id)
    if action is None:
        raise LookupError(f"action #{action_id} not found")
    if action["status"] == "HELD":
        raise RuntimeError(
            f"action #{action_id} is HELD by the jev gate; run --jev-recheck {action['thread_id']} first"
        )
    if action["status"] != "PROPOSED":
        raise RuntimeError(f"action #{action_id} is {action['status']}, only PROPOSED actions can run")
    if action.get("plan_id"):
        raise RuntimeError(f"action #{action_id} is step {action.get('step')} of plan {action['plan_id']}; "
                           "run the plan (its steps keep their order and their host dependencies)")
    try:
        mcp = _client(mcp, action)
    except ValueError as exc:
        raise RuntimeError(f"action #{action_id}: project {action.get('project_id')!r}: {exc}") from exc
    stale = stale_reason(action, mcp)
    if stale:
        raise RuntimeError(f"action #{action_id}: {stale}")
    try:
        mcp.validate(action["tool"], action["arguments"])
    except ActionValidationError as exc:
        db.update_action(action_id, "INVALID", result={"error": str(exc)}, expected_status="PROPOSED")
        raise RuntimeError(f"action #{action_id} no longer passes validation: {exc}") from exc
    if not confirm(action):
        return {"status": "SKIPPED"}

    who = actor or getpass.getuser()
    with ExitStack() as stack:
        if action["tool"] in FILE_TOOLS:
            _hold_workspace(stack, mcp, action_id)
        if action["tool"] in SERVER_TOOLS:
            target = (action.get("arguments") or {}).get("target_host") or "local"
            _hold_host(stack, target, action_id)
            if ops_actions.is_catalog(action["tool"]):
                _check_drift(db, mcp, action, action_id)
        return _execute(db, mcp, action, action_id, who)


HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@contextmanager
def host_lock(host: str):
    """The per-host flock (<data dir>/ops-locks/<host>.lock) that server tools hold while they run. Non-blocking:
    raises RuntimeError when another action holds the host."""
    if not HOST_ID.match(host):
        raise RuntimeError(f"invalid host id {host!r}")
    directory = data_dir() / "ops-locks"
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / f"{host}.lock", "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another action is running on {host}; run this one when it has finished") from None
        yield
    finally:
        handle.close()


def _hold_host(stack: ExitStack, host: str, action_id: int) -> None:
    try:
        stack.enter_context(host_lock(host))
    except RuntimeError as exc:
        raise RuntimeError(f"action #{action_id}: {exc}") from None


def _check_drift(db: DecisionDBManager, mcp: MCPClient, action: Dict[str, Any], action_id: int) -> None:
    """Measures a cleanup action again before it runs: a failed measurement keeps it PROPOSED, a target that grew
    too much since the proposal makes it STALE. Raises RuntimeError in both cases."""
    now = mcp.ops_preview(action["tool"], action["arguments"])
    if now.get("error"):
        raise RuntimeError(f"action #{action_id}: cannot measure before running: {now['error']}")
    reason = ops_actions.drift(action.get("preview"), now)
    if reason:
        db.update_action(action_id, "STALE", result={"error": reason, "preview_now": now}, expected_status="PROPOSED")
        raise RuntimeError(f"action #{action_id} is stale: {reason}")


def _hold_workspace(stack: ExitStack, mcp: MCPClient, action_id: int) -> None:
    """Holds the per-workspace lock (the one publish, sync and reset take) while a file tool writes and tests.

    Only file tools take it, and nothing below call_tool takes it again (publish runs its own test through
    MCPClient.run_default_test, never through run_action), so it is never acquired twice. The lock is
    non-blocking: a busy workspace is reported before the action is marked RUNNING.
    """
    try:
        stack.enter_context(workspace_lock(mcp.workspace, "another publish, project sync or workspace reset is "
                                           "running in this workspace; run the action again when it has finished"))
    except WorkspaceBusy as exc:
        raise RuntimeError(f"action #{action_id}: {exc}") from None
    except ProjectValidationError:
        pass  # not a git workspace: no publish, sync or reset can run there either


def _expected_sha(action: Dict[str, Any]) -> Dict[str, Any]:
    """The script hash a command set or script step was checked with: the executor refuses to run anything else."""
    sha = (action.get("preview") or {}).get("script_sha")
    return {"expected_sha": sha} if sha and action["tool"] in ops_actions.BATCH_TOOLS else {}


def _execute(db: DecisionDBManager, mcp: MCPClient, action: Dict[str, Any], action_id: int, who: str) -> Dict[str, Any]:
    """PROPOSED -> RUNNING -> EXECUTED | FAILED around the tool call."""
    # The status guard makes concurrent runs of the same action impossible.
    if not db.update_action(action_id, "RUNNING", decided_by=who, expected_status="PROPOSED"):
        raise RuntimeError(f"action #{action_id} was taken by another run")
    try:
        if action["tool"] in FILE_TOOLS:
            result = mcp.call_tool(action["tool"], action["arguments"], siblings=sibling_hashes(db, action))
            if result.get("file_hashes"):
                result["applied_at_ns"] = time.time_ns()
        elif action["tool"] in SERVER_TOOLS:
            result = mcp.call_tool(action["tool"], action["arguments"], action_id=action_id,
                                   **_expected_sha(action))
        elif action["tool"] == artifact_ops.TOOL:
            result = mcp.call_tool(action["tool"], action["arguments"], action_id=action_id,
                                   thread_id=action["thread_id"])
        else:
            result = mcp.call_tool(action["tool"], action["arguments"])
    except Exception as exc:  # a tool that raises must not leave the action RUNNING forever
        result = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
    except BaseException:  # Ctrl-C or a termination signal while the tool ran
        db.update_action(action_id, "FAILED", result={"status": "FAILED", "error": "interrupted"},
                         expected_status="RUNNING")
        raise
    final = "EXECUTED" if result.get("status") == "SUCCESS" else "FAILED"
    if not db.update_action(action_id, final, result=result, expected_status="RUNNING"):
        # An operator already settled the action (--fail-stale-action): keep that state and record this late
        # result next to it without touching the status.
        current = db.get_action(action_id)
        recorded = dict(current.get("result") or {}) if current else {}
        recorded["late_result"] = result
        db.set_action_result(action_id, recorded)
        result = {**result, "late": True, "recorded_status": current["status"] if current else None}
    return result


def fail_stale_action(db: DecisionDBManager, action_id: int, actor: Optional[str] = None) -> bool:
    """Operator recovery: moves an action stuck in RUNNING (its process was killed) to FAILED.

    Returns False when the action is not RUNNING. Only use it once no run of the action is alive.
    """
    who = actor or getpass.getuser()
    # decided_by stays the original approver; the operator and the time go into the result.
    marked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return db.update_action(action_id, "FAILED", result={
        "status": "FAILED", "error": f"marked failed by {who} at {marked_at}: the run was interrupted",
        "marked_failed_by": who, "marked_failed_at": marked_at}, expected_status="RUNNING")


class RecheckConflict(RuntimeError):
    """The review to recheck is out of date: the thread's latest debate ended in a later round."""


def release_held_actions(db: DecisionDBManager, thread_id: str, round_num: Optional[int] = None) -> List[int]:
    """Moves a thread's HELD actions to PROPOSED once the jev gate has passed.

    With round_num only the actions proposed in that round are released (the round the review covers).
    """
    released = []
    for action in db.list_actions(thread_id, status="HELD"):
        if round_num is not None and action["round_num"] != round_num:
            continue
        if db.update_action(action["id"], "PROPOSED", expected_status="HELD"):
            released.append(action["id"])
    return released


def reject_action(db: DecisionDBManager, action_id: int, actor: Optional[str] = None) -> bool:
    who = actor or getpass.getuser()
    return any(
        db.update_action(action_id, "REJECTED", decided_by=who, expected_status=current)
        for current in ("PROPOSED", "HELD")
    )


def recheck_thread(db: DecisionDBManager, thread_id: str, gate: Any = None,
                   mcp: Any = None) -> Optional[Dict[str, Any]]:
    """Re-runs the jev evaluation of a thread's latest review; releases that round's HELD actions when it passes.

    With mcp, the released code changes are applied like any other auto-approved action (see auto_run_actions).
    Returns {"gate", "released", "auto_applied", "review"}, or None when the thread has no jev review. Raises RecheckConflict
    when a later debate on the thread ended in a newer round than the review (it would judge superseded work).
    """
    from jev_gate import PASSED, JevGate

    latest = db.latest_jev_review(thread_id)
    if latest is None:
        return None
    final_round = db.thread_final_round(thread_id) or 0
    if final_round > latest["round_num"]:
        raise RecheckConflict(
            f"the jev review of thread {thread_id} covers round {latest['round_num']}, but its latest debate ended "
            f"in round {final_round} without a new review; debate again instead of rechecking the old decision")
    review = (gate or JevGate()).recheck(latest.get("jev_decision_id"), latest["request"])
    db.save_jev_review(latest["thread_id"], latest["round_num"], review, latest["request"], review_id=latest["id"])
    released: List[int] = []
    # A CLOSED thread stays closed: the recheck still updates the review and releases held actions.
    closed = db.get_thread_status(thread_id) == CLOSED_STATUS
    if review["gate"] == PASSED:
        released = release_held_actions(db, thread_id, latest["round_num"])
        if not closed:
            db.set_thread_status(thread_id, "APPROVED")
    elif not closed:
        db.set_thread_status(thread_id, "DISPUTED" if review["gate"] == "DISPUTED" else "PENDING_JEV")
    auto_applied = auto_run_actions(db, mcp, released) if mcp is not None and released else []
    return {"gate": review["gate"], "released": released, "auto_applied": auto_applied,
            "review": db.latest_jev_review(thread_id)}


# --- JSON commands for the hub API (see docs/design/debate-teams-api.md, section 4) -------------

INVALID_INPUT, NOT_FOUND, CONFLICT = 2, 3, 4
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9@:._-]{1,64}$")
# The thread ids the engine creates. The hub (apps/agent/debate_handler.go threadPattern) accepts exactly these.
THREAD_ID_PATTERN = re.compile(r"^thread_[A-Za-z0-9_]{1,80}$")
Result = Tuple[int, Dict[str, Any]]


def _error(code: int, message: str) -> Result:
    return code, {"ok": False, "error": message}


def action_view(action: Dict[str, Any], mcp: Any) -> Dict[str, Any]:
    """Stored action plus the tool's risk and description, for review before approval."""
    spec = _tool_spec(action["tool"])
    level = (ops_actions.risk(action["tool"], action.get("arguments"), action.get("preview"))
             if ops_actions.is_catalog(action["tool"])
             else spec.get("risk", "unknown"))
    return {**action, "risk": level, "tool_description": spec.get("description", "")}


def list_actions_json(
    db: DecisionDBManager, mcp: Any, thread_id: Optional[str], project_id: Optional[str] = None,
) -> Result:
    actions = [action_view(a, mcp) for a in reversed(db.list_actions(thread_id or None, project_id=project_id or None))]
    return 0, {"actions": actions}


def show_action_json(db: DecisionDBManager, mcp: MCPClient, action_id: int) -> Result:
    action = db.get_action(action_id)
    if action is None:
        return _error(NOT_FOUND, f"action #{action_id} not found")
    view = action_view(action, mcp)
    try:
        diff = action_diff(action, _client(mcp, action))
    except ValueError as exc:
        diff, view["workspace_error"] = None, str(exc)
    if diff is not None:
        view["diff"] = diff
    return 0, {"action": view}


def run_action_json(db: DecisionDBManager, mcp: MCPClient, action_id: int, actor: str) -> Result:
    """Runs an approved action. The caller has already shown it to a person who confirmed it."""
    if not ACTOR_PATTERN.fullmatch(actor):
        return _error(INVALID_INPUT, "actor: use 1-64 letters, digits or @:._-")
    try:
        result = run_action(db, mcp, action_id, confirm=lambda _action: True, actor=actor)
    except LookupError as exc:
        return _error(NOT_FOUND, str(exc))
    except RuntimeError as exc:
        return _error(CONFLICT, str(exc))
    action = action_view(db.get_action(action_id), mcp)
    return 0, {"ok": action["status"] == "EXECUTED", "action": action, "result": result}


def reject_action_json(db: DecisionDBManager, mcp: MCPClient, action_id: int, actor: str) -> Result:
    if not ACTOR_PATTERN.fullmatch(actor):
        return _error(INVALID_INPUT, "actor: use 1-64 letters, digits or @:._-")
    action = db.get_action(action_id)
    if action is None:
        return _error(NOT_FOUND, f"action #{action_id} not found")
    if not reject_action(db, action_id, actor=actor):
        return _error(CONFLICT, f"action #{action_id} is {action['status']}, only PROPOSED or HELD can be rejected")
    return 0, {"ok": True, "action": action_view(db.get_action(action_id), mcp)}


def fail_stale_action_json(db: DecisionDBManager, mcp: Any, action_id: int, actor: str) -> Result:
    if not ACTOR_PATTERN.fullmatch(actor):
        return _error(INVALID_INPUT, "actor: use 1-64 letters, digits or @:._-")
    action = db.get_action(action_id)
    if action is None:
        return _error(NOT_FOUND, f"action #{action_id} not found")
    if not fail_stale_action(db, action_id, actor):
        return _error(CONFLICT, f"action #{action_id} is {action['status']}, only RUNNING actions can be failed")
    return 0, {"ok": True, "action": action_view(db.get_action(action_id), mcp)}


def published_branch(db: DecisionDBManager, thread_id: str) -> Optional[str]:
    """The branch a thread was published to (a PUSHED publication), else None. Published threads are final."""
    pushed = [p for p in db.list_publications(thread_id) if p["status"] == "PUSHED"]
    return pushed[-1]["branch"] if pushed else None


def published_refusal(db: DecisionDBManager, thread_id: str) -> Optional[str]:
    """Why a thread cannot be continued: it was published (docs/design/publish-thread.md, D11); else None."""
    branch = published_branch(db, thread_id)
    return f"thread {thread_id} was published as branch {branch}; start a new thread" if branch else None


def published_threads_json(db: DecisionDBManager) -> Result:
    """The ids of threads with a PUSHED publication; the hub overlays them as thread_published."""
    return 0, {"ok": True, "threads": db.published_thread_ids()}


def closed_threads_json(db: DecisionDBManager) -> Result:
    """The CLOSED thread ids; the hub uses them to hide threads closed outside the dashboard."""
    return 0, {"ok": True, "threads": db.closed_thread_ids()}


def close_thread_json(db: DecisionDBManager, thread_id: str) -> Result:
    """Sets a thread CLOSED (idempotent); records are kept, a new debate on the thread reopens it."""
    outcome = db.close_thread(thread_id)
    if outcome is None:
        return _error(NOT_FOUND, f"thread {thread_id} not found")
    return 0, {"ok": True, **outcome}


def recheck_thread_json(db: DecisionDBManager, thread_id: str, gate: Any = None, mcp: Any = None) -> Result:
    try:
        outcome = recheck_thread(db, thread_id, gate, mcp)
    except RecheckConflict as exc:
        return _error(CONFLICT, str(exc))
    if outcome is None:
        return _error(NOT_FOUND, f"no jev review for thread {thread_id}")
    return 0, {"ok": True, **outcome}

"""
JSON commands for the repository audit, called by guardian.py (docs/design/repo-audit-mode.md, phases A1 and A2).

Each command returns (exit_code, payload) like wiki_ops: 0 success, 2 invalid input or an unusable project/commit
(payload {"ok": false, "error": "..."}), 3 an unknown audit, 4 the audit is already running. Planning lists the units and
shards (dry run) or writes the plan under <GUARDIAN_DATA_DIR>/repo-audit/<project>/<audit id>/. Running the shards
(run_audit) imports audit_run, and with it the engine, only when called: the planning and report paths stay free of
the debate and action machinery.
"""

from typing import Any, Dict, List, Optional, Tuple

from repo_audit import AuditError, AuditStore, build_plan, build_report, default_audit_dir, plan_summary
from wiki_ops import OpenError, open_tools, open_store

INVALID = 2
NOT_FOUND = 3

Result = Tuple[int, Dict[str, Any]]


def _invalid(message: str) -> Result:
    return INVALID, {"ok": False, "error": message}


def plan_audit(project_id: Optional[str], question: str, scope: Optional[List[str]] = None,
               sha: Optional[str] = None, dry_run: bool = False) -> Result:
    """Plans an audit of the project's repository at sha (default HEAD).

    {"ok", "dry_run", "audit": id or None, "summary": plan_summary}. Without dry_run the plan is stored and the audit
    is `planned`; a dry run writes nothing."""
    try:
        project, _store = open_store(project_id)
        tools = open_tools(project, sha)
        plan = build_plan(tools, project.id, tools.sha, question, scope)
        audit_id = None
        if not dry_run:
            audit_id = AuditStore(default_audit_dir(project.id)).create(plan)
    except (OpenError, AuditError) as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "dry_run": dry_run, "audit": audit_id, "summary": plan_summary(plan)}


def list_audits(project_id: Optional[str]) -> Result:
    try:
        project, _store = open_store(project_id)
        store = AuditStore(default_audit_dir(project.id))
    except (OpenError, AuditError) as exc:
        return _invalid(str(exc))
    return 0, {"ok": True, "project": project.id, "audits": store.list()}


def show_audit(project_id: Optional[str], audit_id: str) -> Result:
    """The audit's metadata, its plan summary and the shards still to run."""
    try:
        project, _store = open_store(project_id)
        store = AuditStore(default_audit_dir(project.id))
        plan = store.load_plan(audit_id)
        meta = store.meta(audit_id)
        pending = [s["id"] for s in store.pending(audit_id)]
    except OpenError as exc:
        return _invalid(str(exc))
    except AuditError as exc:
        return NOT_FOUND, {"ok": False, "error": str(exc)}
    return 0, {"ok": True, "audit": meta, "summary": plan_summary(plan), "pending_shards": pending}


def run_audit(project_id: Optional[str], audit_id: str, team: Optional[str] = None, language: Optional[str] = None,
              events_file: Optional[str] = None, max_shards: Optional[int] = None) -> Result:
    """Runs the shards of a planned audit that are not done (resumes a paused one) and writes its report.

    {"ok", "audit", "state", "shards_run", "done", "deferred", "failed", "pending", "report": totals}."""
    from audit_run import AuditRunner, RunError  # the engine is imported only here
    from debate_events import EventWriter

    events = EventWriter(events_file)
    try:
        runner = AuditRunner(project_id, audit_id, team=team, language_name=language, events=events)
        result = runner.run(max_shards=max_shards)
    except RunError as exc:
        return exc.code, {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the run marked the audit paused; the caller gets JSON, not a traceback
        return 1, {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        events.close()
    return 0, {"ok": True, **result}


def audit_report(project_id: Optional[str], audit_id: str) -> Result:
    """The audit's report, rebuilt from the plan and the shard records (no LLM) and stored as report.json."""
    try:
        project, _store = open_store(project_id)
        store = AuditStore(default_audit_dir(project.id))
        plan = store.load_plan(audit_id)
        state = store.meta(audit_id).get("state", "planned")
        report = build_report(plan, store.records(audit_id), state)
        if state not in ("planned", "running"):  # nothing to report yet / the run writes its own report
            store.save_report(audit_id, report)
    except OpenError as exc:
        return _invalid(str(exc))
    except AuditError as exc:
        return NOT_FOUND, {"ok": False, "error": str(exc)}
    return 0, {"ok": True, "report": report}

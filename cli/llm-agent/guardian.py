#!/usr/bin/env python3
"""
Server Guardian - Multi-Agent CLI
Interactive and headless execution entrypoint for the Python Multi-Agent system.
"""

import argparse
import json
import os
import sys
from multi_agent import MultiAgentSystem


def scope_line(results: dict) -> str:
    """One line naming which nodes the results cover and what chose them (results["scope"])."""
    scope = results["scope"]
    kind, source, target = scope.get("kind"), scope.get("source"), scope.get("target")
    diag, sec = results.get("diagnostic") or {}, results.get("security") or {}
    count = diag.get("total_nodes")
    if count is None:
        count = (sec.get("fleet_summary") or {}).get("nodes")
    if count is None and scope.get("nodes"):
        count = len(scope["nodes"])
    counted = f" ({count} servers)" if count is not None else ""

    if kind == "fleet":
        what = f"all {count} servers" if count is not None else "all servers"
    elif kind == "group":
        what = f"node group '{target}'{counted}"
    elif kind == "server":
        what = f"server {target}"
    else:
        what = "the local agent's host only"

    if source == "keyword":
        why = f"chosen by '{scope.get('keyword')}' in the query; add --target local for this host only"
    elif source == "target":
        why = f"--target {target}"
    else:
        why = "no --target given"
    return f"📍 Scope: {what} - {why}"


def render_results(results: dict):
    print("\n------------------- [ Multi-Agent Summary ] -------------------")
    if "scope" in results:
        print(scope_line(results))
    if "supervisor" in results:
        sup = results["supervisor"]
        p_name = sup.get("persona_name", "Supervisor Agent")
        p_id = sup.get("persona_id", "server_guardian")
        print(f"\n[👑 Agent Persona: {p_name} ({p_id})] (Engine: {sup.get('provider')})")
        print(sup.get("analysis", ""))

    if "diagnostic" in results:
        diag = results["diagnostic"]
        target = diag.get("target", "local")
        if diag.get("error"):
            print(f"\n[🔍 Diagnostic Agent] (Target: {target}) ❌ {diag['error']}")
        elif "total_nodes" in diag:
            print(f"\n[🌐 Fleet Diagnostic Agent] (Total: {diag.get('total_nodes')} Nodes | Online: {diag.get('online_nodes')} | Duration: {diag.get('probe_duration_sec')}s)")
        else:
            print(f"\n[🔍 Diagnostic Agent] (Target: {target}, Engine: {diag.get('provider')})")
        print(diag.get("analysis", ""))

    if "security" in results:
        sec = results["security"]
        if sec.get("error"):
            print(f"\n[🛡️  Security Auditor Agent] (Target: {sec.get('target')}) ❌ {sec['error']}")
        elif "fleet_summary" in sec:
            fs = sec["fleet_summary"]
            score = sec.get("security_score")
            print(f"\n[🛡️  Security Auditor Agent] (Target: {sec.get('target')}, {fs.get('audited', 0)}/{fs.get('nodes', 0)} nodes audited, "
                  f"Avg score: {'n/a' if score is None else score}/100, Engine: {sec.get('provider')})")
            print(sec.get("analysis", ""))
        else:
            score = sec.get("security_score", 0)
            print(f"\n[🛡️  Security Auditor Agent] (Target: local, Score: {score}/100, Engine: {sec.get('provider')})")
            print(sec.get("analysis", ""))

    if "remediation" in results:
        rem = results["remediation"]
        actions = rem.get("proposed_actions", [])
        print("\n[🔧 Remediation Agent (Human-in-the-Loop Required)]")
        if not actions:
            print(" ✅ 긴급하게 조치할 위험 항목이 없습니다.")
        else:
            for i, act in enumerate(actions, 1):
                risk = act.get("risk_level", "LOW")
                print(f"\n [Action #{i}] {act.get('title')} (위험도: {risk})")
                print(f"   명령어: {act.get('command')}")
                if act.get("warning"):
                    print(f"   ⚠️ 주의: {act.get('warning')}")
            print("\n 💡 승인 방법: 관리자 UI 또는 CLI에서 승인 후 실행하십시오.")
    print("---------------------------------------------------------------\n")


def handle_action_json(args, db, mcp) -> bool:
    """JSON forms of the action commands for the hub API.

    Exit codes: 0 ok, 2 bad input, 3 not found, 4 state conflict. --run-action requires --yes,
    because the dashboard has already shown the action to the person who approved it.
    """
    import action_ops

    import getpass

    actor = args.actor or getpass.getuser()
    if getattr(args, "plan_run", None):
        import plan_ops

        if not args.yes:
            code, payload = action_ops.INVALID_INPUT, {"ok": False, "error": "--plan-run requires --yes"}
        else:
            code, payload = plan_ops.run_plan_json(db, mcp, args.plan_run, actor)
    elif getattr(args, "plan_reject", None):
        import plan_ops

        code, payload = plan_ops.reject_plan_json(db, mcp, args.plan_reject, actor)
    elif getattr(args, "plan_show", None):
        import plan_ops

        code, payload = plan_ops.show_plan_json(db, mcp, args.plan_show)
    elif getattr(args, "plans", None) is not None:
        import plan_ops

        code, payload = plan_ops.list_plans_json(db, mcp, args.plans, args.project)
    elif args.run_action:
        if not args.yes:
            code, payload = action_ops.INVALID_INPUT, {"ok": False, "error": "--run-action --json requires --yes"}
        else:
            code, payload = action_ops.run_action_json(db, mcp, args.run_action, actor)
    elif args.reject_action:
        code, payload = action_ops.reject_action_json(db, mcp, args.reject_action, actor)
    elif args.show_action:
        code, payload = action_ops.show_action_json(db, mcp, args.show_action)
    elif getattr(args, "fail_stale_action", None):
        code, payload = action_ops.fail_stale_action_json(db, mcp, getattr(args, "fail_stale_action", None), actor)
    elif args.jev_recheck:
        code, payload = action_ops.recheck_thread_json(db, args.jev_recheck, mcp=mcp)
    elif getattr(args, "close_thread", None):
        code, payload = action_ops.close_thread_json(db, args.close_thread)
    elif getattr(args, "closed_threads", False):
        code, payload = action_ops.closed_threads_json(db)
    elif getattr(args, "published_threads", False):
        code, payload = action_ops.published_threads_json(db)
    elif getattr(args, "workspace_reset", None):
        import workspace_ops

        if getattr(args, "plan", False):
            code, payload = workspace_ops.reset_plan_json(db, mcp, args.workspace_reset)
            if getattr(args, "release", None):
                code, payload = action_ops.INVALID_INPUT, {"ok": False, "error": "--release applies to --yes, not --plan"}
        elif not args.yes:
            code, payload = action_ops.INVALID_INPUT, {"ok": False, "error": "--workspace-reset needs --plan or --yes"}
        else:
            code, payload = workspace_ops.reset_apply_json(db, mcp, args.workspace_reset, actor,
                                                           release=getattr(args, "release", None) or [])
    elif getattr(args, "publications", None):
        import workspace_ops

        code, payload = workspace_ops.publications_json(db, args.publications)
    elif getattr(args, "publish_thread", None):
        import publish_ops

        if getattr(args, "plan", False):
            code, payload = publish_ops.publish_plan_json(db, mcp, args.publish_thread, args.branch)
        elif not args.yes:
            code, payload = action_ops.INVALID_INPUT, {"ok": False, "error": "--publish-thread --json requires --yes"}
        else:
            code, payload = publish_ops.publish_thread_json(db, mcp, args.publish_thread, actor, args.branch)
    elif args.actions is not None:
        code, payload = action_ops.list_actions_json(db, mcp, args.actions, args.project)
    else:
        return False
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    if code:
        sys.exit(code)
    return True


def handle_publish(args, db, mcp) -> bool:
    """--publish-thread without --json: shows the plan, asks for a typed 'yes' (or --yes), then publishes."""
    import getpass

    import action_ops
    import publish_ops

    def show(plan) -> None:
        print(f"Publish {plan['thread_id']} ({plan['project_id']}): branch {plan['branch']} -> {plan['remote']}")
        print(f"Base:    {plan['base_sha']}")
        print(f"Commit:  {plan['subject']}")
        print("Files:   " + ", ".join(plan["files"]))
        if plan["retry"]:
            print("Retry:   the commit exists locally; only the push is repeated")

    def confirm(plan) -> bool:
        show(plan)
        if args.yes:
            return True
        if not sys.stdin.isatty():
            print("❌ refusing to publish without a terminal; pass --yes after reviewing the plan", file=sys.stderr)
            return False
        return input("\nCommit and push? Type 'yes' to continue: ").strip().lower() == "yes"

    try:
        outcome = publish_ops.publish_thread(db, mcp, args.publish_thread, args.actor or getpass.getuser(),
                                             args.branch or None, confirm=confirm, plan_only=getattr(args, "plan", False))
    except LookupError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(action_ops.NOT_FOUND)
    except (publish_ops.PublishInputError, publish_ops.PublishRefused) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        tests = getattr(exc, "details", {}).get("tests")
        if tests:
            print((tests.get("stdout", "") + tests.get("stderr", ""))[-2000:], file=sys.stderr)
        sys.exit(action_ops.INVALID_INPUT if isinstance(exc, publish_ops.PublishInputError) else action_ops.CONFLICT)
    if outcome["status"] == "PLAN":
        show(outcome)
        return True
    if outcome["status"] == "SKIPPED":
        print("Skipped.")
        return True
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    if outcome["status"] != "PUSHED":
        sys.exit(1)  # the push failed after the commit was made (the JSON form reports it as ok=false, exit 0)
    return True


def handle_action_commands(args) -> bool:
    """Lists, shows, runs or rejects judge-proposed tool actions. Returns True when handled.

    The plain (non --json) forms exit with the same codes as the JSON forms: 2 bad input, 3 not found,
    4 state conflict; 1 is kept for a tool or push that ran and failed.
    """
    if not (args.actions is not None or args.show_action or args.run_action or args.reject_action
            or getattr(args, "fail_stale_action", None)
            or args.jev_status is not None or args.jev_recheck or getattr(args, "close_thread", None)
            or getattr(args, "closed_threads", False) or getattr(args, "published_threads", False)
            or getattr(args, "publish_thread", None) or getattr(args, "workspace_reset", None)
            or getattr(args, "publications", None)
            or getattr(args, "plans", None) is not None or getattr(args, "plan_show", None)
            or getattr(args, "plan_run", None) or getattr(args, "plan_reject", None)):
        return False

    from action_ops import (CONFLICT, NOT_FOUND, close_thread_json, describe_action, recheck_thread, reject_action,
                            run_action)
    from db_manager import DecisionDBManager

    from project_registry import Workspaces

    db = DecisionDBManager()
    # Each action runs in its own project's workspace.
    mcp = Workspaces(db)
    # The workspace reset and publications commands exist for the hub API: they always answer in JSON.
    json_only = (getattr(args, "workspace_reset", None) or getattr(args, "publications", None)
                 or getattr(args, "published_threads", False) or getattr(args, "plans", None) is not None
                 or getattr(args, "plan_show", None) or getattr(args, "plan_run", None)
                 or getattr(args, "plan_reject", None))
    if (args.json or json_only) and handle_action_json(args, db, mcp):
        return True

    def print_review(review) -> None:
        print(f"{review['thread_id']}  gate={review['gate']}  jev_decision={review.get('jev_decision_id')}  "
              f"updated {review['updated_at']}")
        for reason in review.get("reasons") or []:
            print(f"   - {reason}")
        fit = (review.get("evaluation") or {}).get("fit")
        if fit:
            print(f"   fit: recommended={fit.get('recommended')} confidence={fit.get('confidence')} band={fit.get('band')}")

    if args.jev_status is not None:
        reviews = [db.latest_jev_review(args.jev_status)] if args.jev_status else db.list_jev_reviews()
        reviews = [r for r in reviews if r]
        if args.json:
            print(json.dumps(reviews, indent=2, ensure_ascii=False, default=str))
        elif not reviews:
            print("No jev reviews.")
        for review in reviews if not args.json else []:
            print_review(review)
        return True

    if getattr(args, "closed_threads", False):
        threads = db.closed_thread_ids()
        print("\n".join(threads) if threads else "No closed threads.")
        return True

    if getattr(args, "close_thread", None):
        code, payload = close_thread_json(db, args.close_thread)
        if code:
            print(f"❌ {payload['error']}", file=sys.stderr)
            sys.exit(code)
        note = "already closed" if payload["already_closed"] else f"closed (was {payload['previous_status']})"
        print(f"Thread {payload['thread_id']} {note}. Records are kept; a new debate on it reopens it.")
        return True

    if getattr(args, "publish_thread", None):
        return handle_publish(args, db, mcp)

    if args.jev_recheck:
        try:
            outcome = recheck_thread(db, args.jev_recheck, mcp=mcp)
        except RuntimeError as exc:  # RecheckConflict: the review is out of date
            print(f"❌ {exc}", file=sys.stderr)
            sys.exit(CONFLICT)
        if outcome is None:
            print(f"❌ no jev review for thread {args.jev_recheck}", file=sys.stderr)
            sys.exit(NOT_FOUND)
        if outcome["gate"] == "PASSED":
            print(f"✅ jev gate PASSED. Released actions: {outcome['released'] or 'none'}")
            for item in outcome["auto_applied"]:
                note = f" ({item['error']})" if item["error"] else ""
                print(f"🛠️  Auto-applied action #{item['id']} {item['tool']}: {item['status']}{note}")
        print_review(outcome["review"])
        return True

    if args.actions is not None:
        items = db.list_actions(args.actions or None, project_id=args.project)
        if args.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
        elif not items:
            print("No proposed actions.")
        else:
            for a in items:
                print(f"#{a['id']:<4} {a['status']:<9} {a['tool']:<30} {a['thread_id']}  {(a.get('reason') or '')[:60]}")
        return True

    if args.show_action:
        action = db.get_action(args.show_action)
        if action is None:
            print(f"❌ action #{args.show_action} not found", file=sys.stderr)
            sys.exit(NOT_FOUND)
        print(describe_action(action, mcp))
        return True

    stale_id = getattr(args, "fail_stale_action", None)
    if stale_id:
        from action_ops import fail_stale_action

        if fail_stale_action(db, stale_id, actor=args.actor):
            print(f"Action #{stale_id} marked FAILED.")
            return True
        found = db.get_action(stale_id)
        print(f"❌ action #{stale_id} " + ("is not RUNNING" if found else "not found"), file=sys.stderr)
        sys.exit(CONFLICT if found else NOT_FOUND)

    if args.reject_action:
        if reject_action(db, args.reject_action, actor=args.actor):
            print(f"Action #{args.reject_action} rejected.")
            return True
        found = db.get_action(args.reject_action)
        print(f"❌ action #{args.reject_action} " + (f"is {found['status']}, only PROPOSED or HELD can be rejected"
                                                    if found else "not found"), file=sys.stderr)
        sys.exit(CONFLICT if found else NOT_FOUND)

    def confirm(action) -> bool:
        print(describe_action(action, mcp))
        if args.yes:
            return True
        if not sys.stdin.isatty():
            print("❌ refusing to run without a terminal; pass --yes after reviewing the action", file=sys.stderr)
            return False
        return input(f"\nRun action #{action['id']}? Type 'yes' to execute: ").strip().lower() == "yes"

    try:
        result = run_action(db, mcp, args.run_action, confirm, actor=args.actor)
    except LookupError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(NOT_FOUND)
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(CONFLICT)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") not in ("SUCCESS", "SKIPPED"):
        sys.exit(1)
    return True


def print_teams(report: dict) -> None:
    """Human-readable form of config_ops.teams_report."""
    if not report["teams"]:
        print("No teams defined (debates use the default lineup).")
        return

    def fmt(m):
        return f"{m['persona']}({','.join(m['providers']) or 'default'})"

    for entry in report["teams"]:
        status = "INVALID" if entry["errors"] else "ok"
        print(f"{entry['id']}  {entry['name']}  [{entry['source']}, {status}]")
        lineup = entry.get("lineup")
        if lineup:
            if lineup.get("flow"):
                print(f"   flow  : {lineup['flow']['kind']}")
            if lineup.get("flow", {}).get("stages"):
                print(f"   stages: {' -> '.join(fmt(m) for m in lineup['flow']['stages'])}")
            if lineup.get("flow", {}).get("sides"):
                flow = lineup["flow"]
                for label, m in zip("AB", flow["sides"]):
                    print(f"   side {label}: {fmt(m)}  {m['stance']}")
                print(f"   exchanges: {flow['exchanges']}")
            print(f"   expert: {fmt(lineup['expert'])}")
            if lineup.get("proposers"):
                print(f"   draft : {', '.join(fmt(m) for m in lineup['proposers'])}")
            if lineup["critic"]:
                print(f"   critic: {', '.join(fmt(m) for m in lineup['critic'])}")
            print(f"   judge : {fmt(lineup['judge'])}")
            if lineup["allowed_tools"] is not None:
                print(f"   tools : {', '.join(lineup['allowed_tools']) or '(none)'}")
        for msg in entry["errors"]:
            print(f"   ❌ {msg}")
        for msg in entry["warnings"]:
            print(f"   ⚠️  {msg}")
    print(f"Custom teams: {report['custom_dir']}")


def handle_group_commands(args) -> bool:
    """Node group listing/editing (docs/design/node-groups.md). Save/preview read JSON from stdin."""
    if not (args.groups or args.group_save or args.group_delete or args.group_preview):
        return False
    import config_ops
    from group_manager import GroupManager

    manager = GroupManager()
    if args.groups:
        code, payload = config_ops.groups_report(manager)
        if not args.json:
            names = {s["id"]: s.get("name") or s["id"] for s in manager.servers}
            for g in payload["groups"]:
                print(f"{g['id']:<28} {g['name']:<28} {len(g['members']):>3} nodes  "
                      f"{', '.join(names.get(m, m) for m in g['members'])}")
                for w in g["warnings"]:
                    print(f"   ⚠️  {w}")
            if payload["ungrouped"]:
                print(f"{'(ungrouped)':<57} {len(payload['ungrouped']):>3} nodes  {', '.join(payload['ungrouped'])}")
            for w in payload["warnings"]:
                print(f"⚠️  {w}")
            return True
    elif args.group_save:
        code, payload = config_ops.save_group(manager, sys.stdin.read())
    elif args.group_preview:
        code, payload = config_ops.preview_group(manager, sys.stdin.read())
    else:
        code, payload = config_ops.delete_group(manager, args.group_delete)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    if code:
        sys.exit(code)
    return True


def _local_group_manager():
    """The hub database's GroupManager when this host has that database (the CLI runs on the
    hub), else None: elsewhere it would create an empty database."""
    from db_manager import default_db_path
    from group_manager import GroupManager

    return GroupManager() if default_db_path().exists() else None


def load_cli_groups(client):
    """User node groups (with resolved `members`) from the hub, else the local hub database;
    None when neither is available."""
    try:
        return client.get_groups().get("groups") or []
    except Exception:
        local = _local_group_manager()
        return local.list_groups() if local else None


def resolve_cli_target(client, target: str):
    """--target: 'local'/'all'/'fleet' keep their meaning, then a node id/name/hostname, then a
    user group (id or name). Returns None for the keywords, else group_manager.resolve_target's dict.

    Groups come from the hub (/api/groups, same answer as the dashboard); when that fails the
    local database is used, which is the same data when the CLI runs on the hub."""
    from group_manager import resolve_target

    if target in ("local", "all", "fleet"):
        return None
    try:
        nodes = client.get_nodes().get("nodes") or []
    except Exception:
        nodes = []
    try:
        groups = client.get_groups().get("groups") or []
    except Exception:
        groups = []
        local = _local_group_manager()
        if local:
            nodes = nodes or local.servers
            groups = local.list_groups()
    return resolve_target(target, nodes, groups)


def run_ssh_audit(client, resolved, as_json: bool, refresh: bool = False) -> int:
    """--ssh-audit: per-node sshd audit of --target (a node, a group, or the whole fleet),
    summarized per user group. Exit 0 when the audit ran (whatever it found), 1 when the hub call failed."""
    from group_manager import memberships
    from ssh_audit import group_rows, render_ssh_audit

    targets = resolved["ids"] if resolved else None
    try:
        report = client.fleet_ssh_audit(targets, refresh=refresh)
    except Exception as e:
        print(f"❌ Fleet SSH audit failed: {e}", file=sys.stderr)
        return 1
    groups = load_cli_groups(client)
    if as_json:
        out = dict(report)
        ids = [str(n.get("id")) for n in report.get("nodes") or []]
        out["groups"] = memberships(groups, ids) if groups is not None else None
        out["group_rows"] = group_rows(report, groups)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(render_ssh_audit(report, groups))
    return 0


def handle_config_commands(args) -> bool:
    """Persona and team listing/editing. Save commands read JSON from stdin.

    Exit codes follow config_ops: 0 success, 2 validation error (the hub API maps it to HTTP 400).
    """
    if handle_group_commands(args):
        return True
    if (getattr(args, "command_set_save", False) or getattr(args, "command_set_delete", None)
            or getattr(args, "command_set_confirm", None)):
        import config_ops

        if args.command_set_save:
            code, payload = config_ops.save_command_set(sys.stdin.read())
        elif args.command_set_confirm:
            code, payload = config_ops.confirm_command_set(args.command_set_confirm)
        else:
            code, payload = config_ops.delete_command_set(args.command_set_delete)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if code:
            sys.exit(code)
        return True
    if getattr(args, "workers", False):
        import remote_agents

        print(json.dumps(remote_agents.worker_report(), indent=2, ensure_ascii=False))
        return True
    if (getattr(args, "package_roles", False) or getattr(args, "package_bundles", False)
            or getattr(args, "command_sets", False)):
        if getattr(args, "package_roles", False):
            import package_roles as catalog
        elif getattr(args, "command_sets", False):
            import command_sets as catalog
        else:
            import package_bundles as catalog

        code, payload = catalog.report()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if code:
            sys.exit(code)
        return True
    if not (args.teams or args.personas or args.persona_save or args.persona_delete
            or args.team_save or args.team_delete or args.projects or args.project_save
            or args.project_delete or args.project_sync):
        return False

    import config_ops
    from persona_manager import PersonaManager
    from team_manager import TeamManager

    if args.projects or args.project_save or args.project_delete or args.project_sync:
        from project_registry import ProjectManager

        projects = ProjectManager()
        if args.projects:
            code, payload = config_ops.projects_report(projects)
            if not args.json:
                for p in payload["projects"]:
                    st = p.get("status") or {}
                    state = "no clone" if not st.get("present") else f"{st.get('branch')}@{(st.get('head') or '')[:12]}"
                    print(f"{p['id']:<24} [{p['source']}] {state:<28} {st.get('workspace', '')}")
                    for err in p["errors"]:
                        print(f"   ❌ {err}")
                return True
        elif args.project_save:
            code, payload = config_ops.save_project(projects, sys.stdin.read())
        elif args.project_delete:
            code, payload = config_ops.delete_project(projects, args.project_delete)
        else:
            code, payload = config_ops.sync_projects(projects, args.project_sync)
            if not args.json:
                for r in payload.get("results") or [{"id": args.project_sync, "ok": False, "error": payload.get("error")}]:
                    if r.get("ok"):
                        where = f"{r.get('branch') or ''}@{(r.get('head') or '')[:12]}"
                        note = f" ({r['reason']})" if r.get("reason") else ""
                        print(f"  {r['id']:<24} {r.get('action')}: {where}{note}")
                    else:
                        print(f"  {r['id']:<24} failed: {r.get('error')}")
                if code:
                    sys.exit(code)
                return True
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        if code:
            sys.exit(code)
        return True

    personas = PersonaManager()
    teams = TeamManager(persona_mgr=personas)
    if args.teams:
        from llm_runner import LLMRunner

        code, payload = config_ops.teams_report(teams, LLMRunner().installed_providers)
        if not args.json:
            print_teams(payload)
            return True
    elif args.personas:
        code, payload = config_ops.personas_report(personas)
        if not args.json:
            for p in payload["personas"]:
                print(f"{p['id']:<20} {p.get('role', ''):<28} [{p['source']}] {p.get('name', '')}")
            return True
    elif args.persona_save:
        code, payload = config_ops.save_persona(personas, sys.stdin.read())
    elif args.persona_delete:
        code, payload = config_ops.delete_persona(personas, args.persona_delete)
    elif args.team_save:
        code, payload = config_ops.save_team(teams, sys.stdin.read())
    else:
        code, payload = config_ops.delete_team(teams, args.team_delete)

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if code:
        sys.exit(code)
    return True


def handle_wiki_commands(args) -> bool:
    """Code wiki commands (docs/design/debate-code-index-wiki.md, P3). Returns True when one was handled.

    list, show, save and delete always answer in JSON (the hub API reads them; --json is accepted for symmetry),
    with the exit codes of wiki_ops: 0 ok, 2 invalid input, 3 unknown page, 4 a build is already running. The build
    prints a summary (or its result JSON with --json) and exits 0 when it ran, 1 when it wrote nothing because every
    attempted page failed (no provider answered, no page in the answer), 2/3/4 when it could not start.
    The file commands (file_view, docs/design/code-wiki-coverage.md C) answer in JSON with the same codes; 3 is a path
    that is not a readable file of the commit.
    """
    wanted = {name: getattr(args, name, None) for name in (
        "code_wiki_build", "code_wiki_list", "code_wiki_show", "code_wiki_save", "code_wiki_delete",
        "code_file_list", "code_file_show", "code_file_read")}
    if not any(wanted.values()):
        return False
    import re

    import wiki_ops

    if wanted["code_wiki_build"]:
        return run_wiki_build(args)
    if wanted["code_file_list"] or wanted["code_file_show"] or wanted["code_file_read"]:
        import file_view

        if wanted["code_file_list"]:
            code, payload = file_view.list_files(args.project, args.sha)
        elif wanted["code_file_show"]:
            code, payload = file_view.show_file(args.project, args.code_file_show, args.sha)
        else:
            start = args.start if args.start is not None else 1
            end = args.end if args.end is not None else start + file_view.MAX_READ_LINES - 1
            code, payload = file_view.read_lines(args.project, args.code_file_read, start, end, args.sha)
    elif wanted["code_wiki_list"]:
        code, payload = wiki_ops.list_pages(args.project, args.sha)
    elif args.code_wiki_show:
        code, payload = wiki_ops.show_page(args.project, args.code_wiki_show, args.sha)
        if not code and not args.json:
            print(f"{payload['page']['id']}  [{payload['state']}]  {payload['page']['title']}")
            print(payload["generated"])
            print(payload["narrative"])
            return True
    elif args.code_wiki_save:
        import getpass

        user = args.actor or re.sub(r"[^A-Za-z0-9@:._-]", "_", getpass.getuser())[:64] or "user"
        code, payload = wiki_ops.save_page(args.project, sys.stdin.read(), user, args.sha)
    else:
        code, payload = wiki_ops.delete_page(args.project, args.code_wiki_delete)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if code:
        sys.exit(code)
    return True


def run_wiki_build(args) -> bool:
    """--code-wiki-build: writes wiki pages with the team's Expert (wiki_build.WikiBuilder). One build per project."""
    from debate_events import EventWriter
    from wiki_build import BuildError, WikiBuilder

    events = EventWriter(getattr(args, "events_file", None))
    try:
        builder = WikiBuilder(args.project, team=args.team, sha=args.sha, force=args.force,
                              language_name=args.language, events=events)
        if getattr(args, "dry_run", False):
            sizes = builder.dry_run(page_ids=args.page or [], stale=args.stale, everything=args.all, kind=args.kind,
                                    regroup=args.regroup)
            if args.json:
                print(json.dumps({"ok": True, "dry_run": True, **sizes}, indent=2, ensure_ascii=False))
            else:
                print(f"Code wiki dry run of {sizes['project']} at {sizes['sha'][:7]} (team {sizes['team']}): "
                      "first prompt sizes in bytes, no LLM call")
                for item in sizes["pages"]:
                    if "error" in item:
                        print(f"  {item['page']}: {item['error']}")
                    else:
                        print(f"  {item['page']} [{item['kind']}]: total {item['total']} = protocol {item['protocol']} (guide "
                              f"{item['guide']}) + task {item['task']} (scope map {item['scope_map']}, existing "
                              f"narrative {item['existing']}) + role line")
            return True
        report = builder.run(page_ids=args.page or [], stale=args.stale, everything=args.all, kind=args.kind,
                             regroup=args.regroup)
    except BuildError as exc:
        print(f"❌ Code wiki: {exc}", file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(exc.code)
    finally:
        events.close()
    if args.result_file:
        tmp = f"{args.result_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, args.result_file)
    if args.json:
        print(json.dumps({"ok": True, **report}, indent=2, ensure_ascii=False))
    else:
        print(f"Code wiki of {report['project']} at {report['sha'][:7]} (team {report['team']}): "
              f"{len(report['written'])} written, {len(report['skipped'])} skipped")
        for item in report["written"]:
            print(f"  written  {item['page']}  [{item['state']}]  {item['basis_files']} basis files, "
                  f"{item['citations']} cited files, {len(item['unverified'])} unverified")
        for item in report["skipped"]:
            print(f"  skipped  {item['page']}: {item['reason']}")
    if report["failed"] and not report["written"]:
        sys.exit(1)
    return True


def handle_ops_watch(args) -> bool:
    """--ops-watch: the nightly check of ops projects with a watch policy (ops_watch.py). Returns True when handled."""
    if getattr(args, "ops_watch", None) is None:
        return False
    import os

    import ops_watch
    from agent_client import NodeAgentClient
    from db_manager import data_dir
    from project_registry import ProjectManager

    agent = NodeAgentClient(os.environ.get("AGENT_URL", "http://127.0.0.1:9870"))
    reports = ops_watch.watch_all(ProjectManager(), agent, data_dir(), args.ops_watch or None,
                                  dry_run=bool(getattr(args, "dry_run", False)))
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False, default=str))
        return True
    for r in reports:
        if r.get("error") or r.get("skipped"):
            print(f"{r['project']}: {r.get('error') or r.get('skipped')}")
            continue
        print(f"{r['project']}: {len(r['checked'])} hosts checked, {len(r['unreachable'])} unreachable, "
              f"{len(r['findings'])} over a threshold ({len(r['new'])} new)")
        for f in r["findings"]:
            print(f"   {f['host']:<22} {f['kind']:<8} {f['value']:>10} (threshold {f['threshold']}, {f['mount']})")
        if r["debate"]:
            print(f"   started debate {r['debate'].get('id')}")
        elif r["new"] and args.dry_run:
            print("   dry run: no debate started")


def handle_artifact_convert(args) -> bool:
    """--artifact-convert NAME: makes a DOCX or PPTX of a stored Markdown artifact (artifact_ops.ArtifactStore.convert).

    Always answers in JSON (the hub API reads it) with the exit codes of the other config commands: 0 done, 2 invalid
    input or a refused conversion, 3 unknown project, thread, version or file, 4 the derived file already exists."""
    name = getattr(args, "artifact_convert", None)
    if not name:
        return False
    import artifact_ops
    from project_registry import ProjectManager, ProjectValidationError

    def fail(code: int, message: str) -> None:
        print(json.dumps({"error": message}, ensure_ascii=False))
        sys.exit(code)

    project_id, thread, number = args.project, args.thread, getattr(args, "artifact_version", None)
    if not project_id or not thread or not number:
        fail(2, "--artifact-convert needs --project, --thread and --artifact-version")
    try:
        project = ProjectManager().load(project_id)
    except ProjectValidationError as exc:
        fail(3, str(exc))
    try:
        version = artifact_ops.parse_base_version(number)
    except artifact_ops.ArtifactError as exc:
        fail(2, str(exc))
    store = artifact_ops.ArtifactStore(project_id, thread)
    if version is None or not store.manifest(version):
        fail(3, f"{number} does not exist in thread {thread}")
    try:
        entry = store.convert(version, name, getattr(args, "artifact_format", None) or "docx")
    except artifact_ops.ArtifactError as exc:
        message = str(exc)
        fail(4 if "already exists" in message else 3 if "not available" in message else 2, message)
    print(json.dumps({"ok": True, "file": entry, "source": entry["source"], "source_sha256": entry["source_sha256"],
                      "result_sha256": entry["sha256"], "bytes": entry["bytes"]}, ensure_ascii=False))
    return True


def handle_report_keep(args) -> bool:
    """--report-keep: stores the debate report of --project/--thread as a `report.md` artifact version (report_snapshot.py).

    Answers in JSON like --artifact-convert: 0 stored (with the manifest and warnings), 2 refused by the validators or a
    design project, 3 unknown project or no report, 4 the latest version already holds this report."""
    if not getattr(args, "report_keep", False):
        return False
    import report_snapshot
    from artifact_ops import ArtifactError
    from project_registry import ProjectManager, ProjectValidationError

    def fail(code: int, message: str) -> None:
        print(json.dumps({"error": message}, ensure_ascii=False))
        sys.exit(code)

    if not args.project or not args.thread:
        fail(2, "--report-keep needs --project and --thread")
    try:
        project = ProjectManager().load(args.project)
    except ProjectValidationError as exc:
        fail(3, str(exc))
    try:
        manifest = report_snapshot.keep_report(project, args.thread)
    except report_snapshot.AlreadyKept as exc:
        fail(4, str(exc))
    except ArtifactError as exc:
        message = str(exc)
        fail(3 if message.startswith("no report") else 2, message)
    entry = next(f for f in manifest["files"] if f["path"] == report_snapshot.REPORT_NAME)
    print(json.dumps({"ok": True, "version": manifest["version"], "file": entry, "sha256": entry["sha256"],
                      "bytes": entry["bytes"], "warnings": manifest.get("warnings", [])}, ensure_ascii=False))
    return True


def handle_artifacts_prune(args) -> bool:
    """--artifacts-prune: expires old versions of design artifacts (artifact_ops.prune_all). Returns True when handled.

    A design project's artifacts.retention_days decides; the report snapshots of other projects expire after the default
    (artifact_ops.DEFAULT_RETENTION_DAYS); a directory whose project is gone is left alone. --dry-run lists what would expire and changes nothing."""
    if not getattr(args, "artifacts_prune", False):
        return False
    import artifact_ops
    from project_registry import ProjectManager, ProjectValidationError

    manager = ProjectManager()

    def retention_for(project_id: str):
        try:
            project = manager.load(project_id)
        except ProjectValidationError:
            return None
        return project.artifacts["retention_days"] if project.is_design else artifact_ops.DEFAULT_RETENTION_DAYS

    dry = bool(getattr(args, "dry_run", False))
    expired = artifact_ops.prune_all(retention_for, dry_run=dry)
    if args.json:
        print(json.dumps({"ok": True, "dry_run": dry, "expired": expired}, indent=2, ensure_ascii=False))
        return True
    total = sum(len(v) for v in expired.values())
    print(f"{'Would expire' if dry else 'Expired'} {total} artifact version(s)")
    for project_id, items in expired.items():
        for item in items:
            print(f"  {project_id}  {item}")
    return True


def handle_audit_commands(args) -> bool:
    """Repository audit commands (docs/design/repo-audit-mode.md, phases A1 and A2). Returns True when handled.

    --repo-audit QUESTION plans an audit of --project at --sha, narrowed by --audit-scope globs; --dry-run prints the plan
    summary and writes nothing. --repo-audit-run ID runs (or resumes) the shards of a planned audit with --team's Expert
    and writes its report; --repo-audit-report ID prints the report. --repo-audit-list and --repo-audit-show ID answer
    in JSON. Exit codes as wiki_ops: 0 ok, 2 invalid input, 3 unknown audit, 4 the audit is already running.
    """
    question = getattr(args, "repo_audit", None)
    show_id = getattr(args, "repo_audit_show", None)
    list_audits = getattr(args, "repo_audit_list", False)
    run_id = getattr(args, "repo_audit_run", None)
    report_id = getattr(args, "repo_audit_report", None)
    if not (question or list_audits or show_id or run_id or report_id):
        return False
    import audit_ops

    if question:
        code, payload = audit_ops.plan_audit(args.project, question, getattr(args, "audit_scope", None), args.sha,
                                             dry_run=bool(getattr(args, "dry_run", False)))
        if not code and not args.json:
            summary = payload["summary"]
            print(f"Audit plan of {summary['project']} at {summary['sha'][:7]}: {summary['units']} units in "
                  f"{summary['files']} files, {summary['shards']} shards, {summary['bytes']} bytes"
                  + (" (dry run, nothing written)" if payload["dry_run"] else f"; audit {payload['audit']}"))
            for reason, count in sorted(summary["excluded"].items()):
                print(f"  excluded {count} {reason}")
            return True
    elif run_id:
        code, payload = audit_ops.run_audit(args.project, run_id, team=args.team, language=args.language,
                                            events_file=getattr(args, "events_file", None),
                                            max_shards=getattr(args, "audit_max_shards", None))
    elif report_id:
        code, payload = audit_ops.audit_report(args.project, report_id)
    elif list_audits:
        code, payload = audit_ops.list_audits(args.project)
    else:
        code, payload = audit_ops.show_audit(args.project, show_id)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if code:
        sys.exit(code)
    return True


def handle_session_commands(args) -> bool:
    """Runs debate and session management options. Returns True when one was handled."""
    if handle_ops_watch(args):
        return True
    if handle_artifacts_prune(args):
        return True
    if handle_report_keep(args):
        return True
    if handle_artifact_convert(args):
        return True
    if handle_action_commands(args):
        return True
    if handle_config_commands(args):
        return True
    if handle_wiki_commands(args):
        return True
    if handle_audit_commands(args):
        return True
    if not (args.debate or args.attach or args.sessions is not None or args.prune_sessions is not None):
        return False

    from db_manager import DecisionDBManager
    from debate_engine import DebateEngine, decision_status
    from session_ops import open_in_tmux, pick_session

    if args.debate:
        from code_context import ContextError
        from project_registry import ProjectValidationError
        from team_manager import TeamValidationError

        from debate_events import EventWriter

        # Without --events-file the writer is a no-op (direct CLI runs write no events).
        events = EventWriter(getattr(args, "events_file", None))
        engine = DebateEngine(max_rounds=args.rounds, events=events)
        if args.thread:
            # A published thread is final (D11): refuse before any agent runs, not at publish time.
            import action_ops

            refusal = action_ops.published_refusal(DecisionDBManager(), args.thread)
            if refusal:
                print(f"❌ {refusal}", file=sys.stderr)
                events.close()
                sys.exit(action_ops.CONFLICT)
        extra = {}
        if args.context_path:
            extra["context_paths"] = args.context_path
        if args.context_ref:
            extra["context_ref"] = args.context_ref
        try:
            result = engine.run_debate(args.debate, project_id=args.project, thread_id=args.thread, team=args.team,
                                       **extra)
        except TeamValidationError as e:
            print(f"❌ Team {args.team}: {e}", file=sys.stderr)
            sys.exit(2)
        except ContextError as e:
            print(f"❌ Code context: {e}", file=sys.stderr)
            sys.exit(2)
        except ProjectValidationError as e:
            print(f"❌ Project: {e}", file=sys.stderr)
            sys.exit(2)
        finally:
            events.close()
        if args.result_file:
            # The hub API reads the result from this file; stdout carries the progress log.
            tmp = f"{args.result_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, args.result_file)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            decision = result.get("final_decision") or {}
            status = decision_status(decision)
            if status == "REVISION_REQUIRED":
                status = "NOT APPROVED"
            print(f"Thread: {result['thread_id']} | Status: {status} | Log: {result['log_file']}")
        return True

    db = DecisionDBManager()
    if args.prune_sessions is not None:
        removed = db.prune_agent_sessions(args.prune_sessions)
        print(f"Removed {removed} session(s) unused for {args.prune_sessions} day(s).")
        return True

    if args.attach:
        sessions = db.list_agent_sessions(args.attach)
        if not sessions:
            print(f"❌ No stored sessions for thread {args.attach}", file=sys.stderr)
            sys.exit(1)
        try:
            stored = pick_session(sessions, args.role)
            hint = open_in_tmux(stored)
        except (LookupError, RuntimeError, OSError) as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Opened {stored['role']} ({stored['provider']} {stored['session_id']}) in tmux. Run: {hint}")
        return True

    sessions = db.list_agent_sessions(args.sessions or None)
    if args.json:
        print(json.dumps(sessions, indent=2, ensure_ascii=False))
    elif not sessions:
        print("No persisted agent sessions.")
    else:
        for s in sessions:
            print(f"{s['thread_id']}  {s['role']:<14} {s['provider']:<7} {s['session_id']}  last used {s['last_used_at']}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Server Guardian Multi-Agent CLI")
    parser.add_argument("query", nargs="*", help="Natural language query for the Multi-Agent system")
    parser.add_argument("--fleet", action="store_true", help="Parallel diagnosis across the fleet (or only --target's server or node group)")
    parser.add_argument("--exec", dest="exec_cmd", help="Execute a command on --target (a server, a node group, or all)")
    parser.add_argument("--target", default=None,
                        help="Target: local, all, a server id/name (e.g. site-a-arm1), or a node group id/name (see --groups). "
                             "Default local; when given it overrides fleet keywords such as '전체' in the query")
    parser.add_argument("--persona", default="server_guardian", help="Agent persona ID (server_guardian, general_assistant, devops_specialist, domain_architect, critic_redteam)")
    parser.add_argument("--ssh-audit", action="store_true",
                        help="Audit sshd on every node over SSH (or only --target's server or node group), summarized per node group")
    parser.add_argument("--refresh", action="store_true", help="With --ssh-audit: probe again instead of using the hub's 60s cache")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted text")
    parser.add_argument("--agent-url", default=os.getenv("AGENT_URL", "http://localhost:9870"), help="Agent URL")
    debate = parser.add_argument_group("multi-agent debate sessions")
    debate.add_argument("--debate", metavar="TASK", help="Run a dialectic debate on TASK (continue a thread with --thread)")
    debate.add_argument("--thread", help="Thread ID to continue with --debate")
    debate.add_argument("--project", default=None,
                        help="Project for --debate, or a filter for --actions (configs/projects; default server-guardian)")
    debate.add_argument("--team", help="Team lineup for --debate (configs/teams/<id>.json; see --teams)")
    debate.add_argument("--teams", action="store_true", help="List debate teams and validate them")
    debate.add_argument("--workers", action="store_true",
                        help="Report the model workers (probe result and a live ping per enabled host) as JSON")
    debate.add_argument("--package-roles", action="store_true",
                        help="List the host roles of configs/package-roles.json (JSON) and validate the file")
    debate.add_argument("--package-bundles", action="store_true",
                        help="List the install bundles of configs/package-bundles.json (JSON) and validate the file")
    debate.add_argument("--command-set-save", action="store_true",
                        help="Validate and save an operator command set read as JSON from stdin (reviewed is stamped today)")
    debate.add_argument("--command-set-delete", metavar="ID", help="Delete an operator command set (builtin sets stay)")
    debate.add_argument("--command-set-confirm", metavar="ID",
                        help="Confirm the text of an operator command set so the batch gate may release plans that use it")
    debate.add_argument("--command-sets", action="store_true",
                        help="List the command sets (configs/command-sets and the data dir's command-sets; JSON) and validate them")
    debate.add_argument("--result-file", metavar="PATH", help="Also write the --debate result JSON to PATH")
    debate.add_argument("--events-file", metavar="PATH",
                        help="Append the debate's progress events (JSON lines) to PATH for the hub's Agents monitor")
    debate.add_argument("--context-path", action="append", metavar="GLOB",
                        help="Workspace files the agents read for --debate (repeatable, e.g. 'apps/agent/*.go')")
    debate.add_argument("--context-ref", metavar="REF", help="Commit or branch for --context-path (default HEAD)")

    config = parser.add_argument_group("personas and teams (save commands read JSON from stdin)")
    config.add_argument("--personas", action="store_true", help="List personas (builtin and custom)")
    config.add_argument("--persona-save", action="store_true", help="Validate and save a custom persona")
    config.add_argument("--persona-delete", metavar="ID", help="Delete a custom persona")
    config.add_argument("--team-save", action="store_true", help="Validate and save a custom team")
    config.add_argument("--team-delete", metavar="ID", help="Delete a custom team")
    config.add_argument("--projects", action="store_true", help="List projects with their workspace state")
    config.add_argument("--project-save", action="store_true", help="Validate and save a custom project")
    config.add_argument("--project-delete", metavar="ID", help="Delete a custom project (keeps the clone)")
    config.add_argument("--project-sync", metavar="ID", help="Clone or fast-forward a project's workspace ('all' for every project)")
    watch = parser.add_argument_group("ops watch (docs/design/server-ops-project.md, P6)")
    watch.add_argument("--ops-watch", nargs="?", const="", metavar="PROJECT",
                       help="Check ops projects with policy.watch and start a debate for new findings (one project, or all)")
    watch.add_argument("--artifacts-prune", action="store_true",
                       help="Expire design artifact versions older than the project's artifacts.retention_days "
                            "(the latest version of a thread is kept; with --dry-run only list them)")
    watch.add_argument("--artifact-convert", metavar="NAME",
                       help="Make a DOCX or PPTX of a stored Markdown artifact (needs --project, --thread, --artifact-version; JSON output)")
    watch.add_argument("--report-keep", action="store_true",
                       help="Store the debate report of --project/--thread as a report.md artifact version (JSON output)")
    watch.add_argument("--artifact-version", metavar="VERSION", help="Artifact version for --artifact-convert, e.g. artifact@v3")
    watch.add_argument("--artifact-format", default="docx", choices=["docx", "pptx"], help="Target format for --artifact-convert")
    groups = parser.add_argument_group("node groups (stored in the hub database; save/preview read JSON from stdin)")
    groups.add_argument("--groups", action="store_true", help="List node groups with their resolved members")
    groups.add_argument("--group-save", action="store_true", help="Validate and save a node group (create, or update by id)")
    groups.add_argument("--group-delete", metavar="ID", help="Delete a node group")
    groups.add_argument("--group-preview", action="store_true", help="Resolve a group definition's members without saving")
    debate.add_argument("--rounds", type=int, default=None, help="Maximum debate rounds for --debate (default: team's, else 3)")
    debate.add_argument("--sessions", nargs="?", const="", metavar="THREAD", help="List persisted agent sessions (optionally for one thread)")
    debate.add_argument("--attach", metavar="THREAD", help="Open a thread's agent session interactively in tmux")
    debate.add_argument("--role", help="Agent role for --attach (prefix match, e.g. critic)")
    debate.add_argument("--prune-sessions", type=int, metavar="DAYS", help="Forget sessions unused for DAYS days")
    actions = parser.add_argument_group("judge-proposed tool actions (run only after approval)")
    actions.add_argument("--actions", nargs="?", const="", metavar="THREAD", help="List proposed actions (optionally for one thread)")
    actions.add_argument("--show-action", type=int, metavar="ID", help="Show one action in full")
    actions.add_argument("--run-action", type=int, metavar="ID", help="Review and execute a PROPOSED action")
    actions.add_argument("--reject-action", type=int, metavar="ID", help="Reject a PROPOSED action")
    actions.add_argument("--fail-stale-action", type=int, metavar="ID",
                         help="Mark an action stuck in RUNNING (its process was killed) as FAILED")
    actions.add_argument("--plans", nargs="?", const="", metavar="THREAD",
                         help="List plans (optionally for one thread); JSON output (docs/design/fleet-provisioning.md)")
    actions.add_argument("--plan-show", metavar="ID", help="Show one plan with its steps by host (JSON output)")
    actions.add_argument("--plan-run", metavar="ID", help="Run the PROPOSED steps of a plan (JSON output, needs --yes)")
    actions.add_argument("--plan-reject", metavar="ID", help="Reject the PROPOSED or HELD steps of a plan (JSON output)")
    actions.add_argument("--yes", action="store_true", help="Skip the confirmation prompt for --run-action, --plan-run and --publish-thread")
    actions.add_argument("--actor", help="Who approved or rejected the action (recorded as decided_by; default: OS user)")
    actions.add_argument("--jev-status", nargs="?", const="", metavar="THREAD", help="Show jev gate reviews")
    actions.add_argument("--close-thread", metavar="THREAD",
                         help="Close a thread (status CLOSED): records stay, dashboards hide it; a new debate on it reopens it")
    actions.add_argument("--closed-threads", action="store_true",
                         help="List the ids of CLOSED threads (with --json: {\"threads\": [...]})")
    actions.add_argument("--published-threads", action="store_true",
                         help="List the ids of threads with a pushed publication (JSON output: {\"threads\": [...]})")
    actions.add_argument("--publish-thread", metavar="THREAD",
                         help="Commit a thread's executed file actions to a new branch and push it (see docs/design/publish-thread.md)")
    actions.add_argument("--workspace-reset", metavar="PROJECT",
                         help="Discard stray changes in a project's workspace, keeping files of executed unpublished actions "
                              "(--plan lists them, --yes discards; JSON output)")
    actions.add_argument("--release", action="append", metavar="THREAD",
                         help="With --workspace-reset --yes: also discard this thread's kept files and record that it was "
                              "released (it can never be published); repeatable")
    actions.add_argument("--publications", metavar="THREAD",
                         help="List a thread's publish attempts, newest first (JSON output)")
    actions.add_argument("--branch", metavar="NAME", help="Branch name for --publish-thread (default: guardian/<thread>)")
    actions.add_argument("--plan", action="store_true",
                         help="With --publish-thread or --workspace-reset: check every precondition and print the plan; change nothing")
    actions.add_argument("--jev-recheck", metavar="THREAD", help="Re-run the jev evaluation; releases HELD actions when it passes")

    wiki = parser.add_argument_group("code wiki (docs/design/debate-code-index-wiki.md; save reads JSON {id, title, scope, body} from stdin)")
    wiki.add_argument("--code-wiki-build", action="store_true",
                      help="Write wiki pages for --project with the team's Expert: --page ID (repeatable), --stale or --all "
                           "(neither: the default page set, only while the project has no pages)")
    wiki.add_argument("--code-wiki-list", action="store_true", help="List the project's wiki pages with their state (JSON)")
    wiki.add_argument("--code-wiki-show", metavar="ID", help="Show a wiki page: narrative, generated overview, staleness details")
    wiki.add_argument("--code-wiki-save", action="store_true", help="Save a person's edit of a wiki page (JSON from stdin)")
    wiki.add_argument("--code-wiki-delete", metavar="ID", help="Delete a wiki page")
    wiki.add_argument("--code-file-list", action="store_true", help="List the readable files of --project at --sha (JSON)")
    wiki.add_argument("--code-file-show", metavar="PATH",
                      help="One file without an LLM: outline, name-based references, wiki pages and documents that cover it (JSON)")
    wiki.add_argument("--code-file-read", metavar="PATH", help="Lines --start..--end of a readable text file (JSON, at most 400 lines)")
    wiki.add_argument("--start", type=int, metavar="N", help="With --code-file-read: first line (1-based)")
    wiki.add_argument("--end", type=int, metavar="N", help="With --code-file-read: last line (inclusive)")
    wiki.add_argument("--page", action="append", metavar="ID", help="With --code-wiki-build: a page to (re)write; repeatable")
    wiki.add_argument("--stale", action="store_true", help="With --code-wiki-build: rewrite the pages that are stale or broken")
    wiki.add_argument("--all", action="store_true", help="With --code-wiki-build: rewrite every page")
    wiki.add_argument("--kind", choices=("dir", "overview", "group", "feature"),
                      help="With --code-wiki-build: only pages of this kind; alone, every candidate page of it "
                           "(feature: one page per design document; group: the group pages of large directories, then "
                           "their overview pages; docs/design/code-wiki-coverage.md A and B)")
    wiki.add_argument("--regroup", action="append", metavar="ID",
                      help="With --code-wiki-build: plan the groups of this split directory page again and write its new "
                           "group pages and its overview; repeatable")
    wiki.add_argument("--dry-run", action="store_true",
                      help="With --code-wiki-build: print the size breakdown of each page's first prompt and stop (no LLM call, "
                           "nothing written). With --ops-watch: report the findings without starting a debate. With --artifacts-prune: list "
                           "what would expire. "
                           "With --repo-audit: print the plan summary only")
    wiki.add_argument("--sha", metavar="SHA", help="Commit the wiki commands read (default: the project workspace's HEAD)")
    wiki.add_argument("--force", action="store_true", help="With --code-wiki-build: also overwrite pages written by a person")
    wiki.add_argument("--language", metavar="NAME", help="With --code-wiki-build: language of the pages (default Korean, "
                                                          "or GUARDIAN_CODE_WIKI_LANGUAGE); with --repo-audit-run: "
                                                          "language of the claims (GUARDIAN_AUDIT_LANGUAGE)")

    audit = parser.add_argument_group("repository audit (docs/design/repo-audit-mode.md)")
    audit.add_argument("--repo-audit", metavar="QUESTION",
                       help="Plan an audit of --project: the question is asked of every unit (declaration or file) in scope")
    audit.add_argument("--audit-scope", action="append", metavar="GLOB",
                       help="With --repo-audit: only paths matching this glob (default: every readable file); repeatable")
    audit.add_argument("--repo-audit-run", metavar="ID",
                       help="Run (or resume) the shards of a planned audit with --team's Expert (--language: Korean by "
                            "default, GUARDIAN_AUDIT_LANGUAGE); a quota failure pauses it. Findings below "
                            "GUARDIAN_AUDIT_MIN_SEVERITY (medium) or not confirmed by the re-check session "
                            "(GUARDIAN_AUDIT_RECHECK=off skips it) go to the report's notes. Writes the report")
    audit.add_argument("--audit-max-shards", type=int, metavar="N",
                       help="With --repo-audit-run: stop after N shards (the audit stays resumable)")
    audit.add_argument("--repo-audit-report", metavar="ID", help="The audit's report from its shard records (JSON)")
    audit.add_argument("--repo-audit-list", action="store_true", help="List the project's audits (JSON)")
    audit.add_argument("--repo-audit-show", metavar="ID", help="One audit: state, plan summary, shards still to run (JSON)")

    args = parser.parse_args()
    if args.actor is not None:
        # Recorded as decided_by / publication actor: one format for the plain and the JSON forms.
        from action_ops import ACTOR_PATTERN, INVALID_INPUT

        if not ACTOR_PATTERN.match(args.actor):
            message = "actor: use 1-64 letters, digits or @:._-"
            if args.json:
                print(json.dumps({"ok": False, "error": message}))
            else:
                print(f"❌ --{message}", file=sys.stderr)
            sys.exit(INVALID_INPUT)
    if handle_session_commands(args):
        return
    system = MultiAgentSystem(args.agent_url, persona_id=args.persona)
    from group_manager import TargetError

    # Only an explicit --target overrides the query's fleet keywords (see handle_query).
    target_given = args.target is not None
    if not target_given:
        args.target = "local"

    try:
        resolved = resolve_cli_target(system.client, args.target)
    except TargetError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(2)
    # A group target becomes an explicit id list; the hub checks each id against servers.json.
    group_ids = resolved["ids"] if resolved and resolved["kind"] == "group" else None
    target_host = resolved["id"] if resolved and resolved["kind"] == "server" else args.target
    query_target = target_host if target_given else None

    if args.ssh_audit:
        sys.exit(run_ssh_audit(system.client, resolved, args.json, refresh=args.refresh))

    if args.fleet:
        diag = system.run_fleet_diagnostic(targets=group_ids or (resolved["ids"] if resolved else None))
        if args.json:
            print(json.dumps(diag, indent=2, ensure_ascii=False))
        else:
            render_results({"diagnostic": diag})
        return

    if args.exec_cmd:
        if args.target in ["all", "fleet"] or group_ids:
            report = system.run_fleet_exec(args.exec_cmd, targets=group_ids)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                scope = f"group '{resolved['name']}'" if group_ids else "fleet"
                print(f"\n🌐 [Fleet Exec] Command: '{args.exec_cmd}' across {report.get('total_nodes')} servers in {scope} ({report.get('duration_sec')}s)")
                print(f" • Success: {report.get('success_count')} | Failed: {report.get('failed_count')}\n")
                for r in report.get("results", []):
                    icon = "🟢" if r.get("exit_code") == 0 else "🔴"
                    print(f" {icon} [{r.get('id')}] ({r.get('duration_ms')}ms)")
                    out = r.get("stdout") or r.get("stderr")
                    for line in out.splitlines()[:3]:
                        print(f"    {line}")
        else:
            res = system.execute_approved_action("exec", args.exec_cmd, target_host=target_host)
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"Exit Code: {res.get('exit_code')}")
                if res.get("stdout"):
                    print(f"Stdout:\n{res.get('stdout')}")
                if res.get("stderr"):
                    print(f"Stderr:\n{res.get('stderr')}")
        return

    if args.query:
        query_text = " ".join(args.query)
        try:
            results = system.handle_query(query_text, target_host=query_target, target_ids=group_ids)
            if args.json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                render_results(results)
        except Exception as e:
            print(f"❌ Error running multi-agent query: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Interactive REPL Mode
    active_p = system.get_active_persona()
    print("=====================================================")
    print("🤖 Server Guardian Multi-Agent CLI System (Python)")
    print(f"Connected to Target Agent: {args.agent_url}")
    print(f"Active Persona: {active_p.get('name')} ({active_p.get('id')})")
    print("Commands:")
    print(" - /persona list : 등록된 페르소나 목록 조회")
    print(" - /persona use <id> : 페르소나 변경 (예: /persona use general_assistant)")
    print(" - type 'exit' or 'quit' to exit")
    print("=====================================================\n")

    while True:
        try:
            cur_id = system.get_active_persona().get("id", "guardian")
            line = input(f"guardian[{cur_id}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not line:
            continue
        if line.lower() in ["exit", "quit"]:
            print("Bye!")
            break

        if line.startswith("/persona"):
            parts = line.split()
            if len(parts) == 1 or parts[1] in ["list", "ls"]:
                print("\n🎭 [Available Agent Personas]")
                for p in system.list_personas():
                    marker = " 🟢 (active)" if p.get("id") == cur_id else ""
                    print(f" • [{p.get('id')}] {p.get('name')}{marker}")
                    print(f"   Role: {p.get('role')}")
                    print(f"   System: {p.get('system_prompt')[:60]}...")
                print()
                continue
            elif parts[1] == "use" and len(parts) > 2:
                target_p = parts[2]
                switched = system.set_active_persona(target_p)
                if switched:
                    print(f"✅ Persona switched to: {switched.get('name')} ({switched.get('id')})")
                else:
                    print(f"❌ Persona '{target_p}' not found.")
                continue
            elif parts[1] in ["current", "info"]:
                curr = system.get_active_persona()
                print(f"\n🎭 Active Persona: {curr.get('name')} ({curr.get('id')})")
                print(f" Role: {curr.get('role')}")
                print(f" System Prompt: {curr.get('system_prompt')}\n")
                continue

        try:
            results = system.handle_query(line, target_host=query_target, target_ids=group_ids)
            render_results(results)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

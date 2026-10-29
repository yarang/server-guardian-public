"""
Fleet SSH audit presentation (docs/design/fleet-ssh-audit.md): per-group aggregation and the
text report shared by `guardian.py --ssh-audit`, the Security Auditor agent and the built-in
engine. The hub (GET /api/fleet/ssh) does the probing and scoring; nothing here re-evaluates
rules or invents values for nodes that could not be audited.
"""

from typing import Any, Dict, List, Optional, Sequence

from group_manager import memberships

ALL_NODES = "All nodes"


def _label(node: Dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "?")


def worst_rule(nodes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The rule failing on the most audited nodes ({rule_id, title, fail}); None when none fail.
    Ties keep the evaluation order (the first rule wins)."""
    counts: Dict[str, Dict[str, Any]] = {}
    for n in nodes:
        for r in (n.get("report") or {}).get("rules") or []:
            entry = counts.setdefault(r.get("rule_id"), {"rule_id": r.get("rule_id"), "title": r.get("title", ""), "fail": 0})
            if r.get("status") == "FAIL":
                entry["fail"] += 1
    best = None
    for entry in counts.values():
        if entry["fail"] and (best is None or entry["fail"] > best["fail"]):
            best = entry
    return best


def group_buckets(report: Dict[str, Any], groups: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, List[str]]:
    """Group name -> node ids of this report. A node in several groups counts in each, nodes in
    none go under "Ungrouped". Without group data (None) everything is one "All nodes" bucket."""
    ids = [str(n.get("id")) for n in report.get("nodes") or []]
    if groups is None:
        return {ALL_NODES: ids} if ids else {}
    return memberships(groups, ids)


def group_rows(report: Dict[str, Any], groups: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Per-group summary rows; groups with no node in this report are left out."""
    by_id = {str(n.get("id")): n for n in report.get("nodes") or []}
    rows = []
    for name, ids in group_buckets(report, groups).items():
        members = [by_id[i] for i in ids if i in by_id]
        if not members:
            continue
        audited = [n for n in members if n.get("report")]
        scores = [n["report"].get("score", 0) for n in audited]
        rows.append({
            "group": name,
            "nodes": len(members),
            "audited": len(audited),
            "unreachable": sum(1 for n in members if n.get("status") == "unreachable"),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "failing_nodes": [_label(n) for n in audited if (n["report"].get("failed_count") or 0) > 0],
            "worst_rule": worst_rule(audited),
        })
    return rows


def _score(value: Any) -> str:
    return "—" if value is None else f"{value:g}" if isinstance(value, float) else str(value)


def render_ssh_audit(report: Dict[str, Any], groups: Optional[Sequence[Dict[str, Any]]]) -> str:
    """Human report: per-group table, then each node's FAIL rules, then nodes not (fully) audited."""
    s = report.get("summary") or {}
    nodes = report.get("nodes") or []
    lines = [
        f"🛡️  Fleet SSH audit: {s.get('nodes', len(nodes))} nodes, {s.get('audited', 0)} audited, "
        f"{s.get('unreachable', 0)} unreachable, {s.get('errors', 0)} errors · average score {_score(s.get('avg_score'))}"
        f" · generated {report.get('generated_at', '?')}{' (cached)' if report.get('cached') else ''}",
    ]
    if groups is None:
        lines.append("⚠️  Node groups unavailable (hub /api/groups failed); showing all nodes together.")
    if not nodes:
        lines.append("No nodes were audited.")
        return "\n".join(lines)

    rows = group_rows(report, groups)
    width = max([len("Group")] + [len(r["group"]) for r in rows])
    lines.append("")
    lines.append(f"{'Group':<{width}}  {'Nodes':>5}  {'Audited':>7}  {'Avg score':>9}  Failing nodes")
    for r in rows:
        failing = f"{len(r['failing_nodes'])}" + (f" ({', '.join(r['failing_nodes'])})" if r["failing_nodes"] else "")
        lines.append(f"{r['group']:<{width}}  {r['nodes']:>5}  {r['audited']:>7}  {_score(r['avg_score']):>9}  {failing}")

    failing_nodes = [n for n in nodes if n.get("report") and (n["report"].get("failed_count") or 0) > 0]
    lines.append("")
    if failing_nodes:
        lines.append("Failing rules by node:")
        for n in failing_nodes:
            lines.append(f" ❌ {_label(n)} (score {n['report'].get('score')}, {n.get('source') or '?'})")
            for r in n["report"].get("rules") or []:
                if r.get("status") != "FAIL":
                    continue
                lines.append(f"    [{r.get('rule_id')}] {r.get('title')}: {r.get('current_value') or '—'} (expected {r.get('expected_value') or '—'})")
                if r.get("remediation"):
                    lines.append(f"      fix: {r['remediation']}")
    else:
        lines.append("No audited node has a failing rule.")

    partial = [n for n in nodes if n.get("status") == "partial"]
    if partial:
        lines.append("")
        lines.append("Partial results (values may be incomplete):")
        for n in partial:
            lines.append(f" ⚠️  {_label(n)}: {n.get('source') or '?'}")
            for note in (n.get("notes") or [])[:3]:
                lines.append(f"    - {note}")
    missing = [n for n in nodes if not n.get("report")]
    if missing:
        lines.append("")
        lines.append("Not audited:")
        for n in missing:
            lines.append(f" ⛔ {_label(n)} {n.get('status')}: {n.get('error') or 'no result'}")
    return "\n".join(lines)


def llm_context(report: Dict[str, Any], groups: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    """Compact fleet audit for an LLM prompt: summary, group rows and each node's non-passing rules."""
    nodes = []
    for n in report.get("nodes") or []:
        rep = n.get("report") or {}
        nodes.append({
            "id": n.get("id"),
            "status": n.get("status"),
            "source": n.get("source"),
            "score": rep.get("score"),
            "findings": [
                {k: r.get(k) for k in ("rule_id", "status", "current_value", "expected_value")}
                for r in rep.get("rules") or [] if r.get("status") != "PASS"
            ],
            "keys": len(n.get("keys") or []),
            "error": n.get("error") or None,
        })
    return {
        "summary": report.get("summary") or {},
        "groups": group_rows(report, groups),
        "nodes": nodes,
        "generated_at": report.get("generated_at"),
    }

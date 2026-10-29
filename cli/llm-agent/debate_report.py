"""Standalone Markdown report for one debate thread, with a Mermaid flow and two SVG charts.

Everything here is a pure function of its inputs (no clock, no randomness), so the same transcript always yields
byte-identical output. Only `write_report` touches the filesystem.

Contract (the hub API and the dashboard rely on it):
- report: `<project_dir>/<thread_id>.md`, assets: `<project_dir>/<thread_id>.assets/<name>.svg`
- asset names match ASSET_NAME_RE; the Markdown links them as `![alt](<thread_id>.assets/<name>.svg)`
- Mermaid diagrams are inline fenced ```mermaid blocks
"""

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

ASSET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.svg$")
SUMMARY_ASSET = "summary.svg"
RETRIEVAL_ASSET = "retrieval.svg"

MAX_ROUNDS = 20  # columns of the summary chart and rounds of the flow diagram
MAX_AGENTS = 12  # rows of the summary chart and bars of the retrieval chart
MAX_SVG_BYTES = 200 * 1024
TITLE_LIMIT = 80

STATUS_LABELS = {
    "APPROVED": "APPROVED (승인)",
    "UNVERIFIED": "UNVERIFIED (LLM 사용 불가, 내장 대체 문구만 사용)",
    "DISPUTED": "DISPUTED (judge 승인, jev 게이트 미통과; 조치 보류)",
    "PENDING_JEV": "PENDING_JEV (judge 승인, jev 평가 대기; 조치 보류)",
}
DEFAULT_STATUS_LABEL = "REVISION_REQUIRED (수정 필요 / 최대 라운드 도달)"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MERMAID_BAD = str.maketrans({'"': "'", ";": ",", "#": "", "<": "", ">": "", "`": "'", "\\": "/",
                              "{": "(", "}": ")", "\n": " ", "\r": " ", "\t": " "})


def status_label(status: str) -> str:
    """Human label for a decision status word."""
    return STATUS_LABELS.get(status, DEFAULT_STATUS_LABEL)


def _role_kind(role: str) -> str:
    """'expert', 'critic', 'judge' or 'other' for a transcript role string."""
    low = (role or "").lower()
    for kind in ("judge", "critic", "expert"):
        if kind in low:
            return kind
    return "other"


def _mm(text: Any, limit: int = 60) -> str:
    """Text that is safe inside a Mermaid label or participant alias: no quotes, ';', '#', '<>', newlines."""
    clean = _CONTROL_RE.sub("", str(text if text is not None else "")).translate(_MERMAID_BAD)
    clean = clean.replace("%%", "%")
    clean = " ".join(clean.split())
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean or "-"


def _cell(text: Any, limit: int = 200) -> str:
    """Text for a Markdown table cell: one line, pipes escaped."""
    clean = " ".join(_CONTROL_RE.sub("", str(text if text is not None else "")).split())
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean.replace("|", "\\|") or "-"


def _x(text: Any, limit: int = 0) -> str:
    """XML-escaped text for SVG content (and attribute values), optionally truncated first."""
    clean = " ".join(_CONTROL_RE.sub("", str(text if text is not None else "")).split())
    if limit and len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return _xml_escape(clean, {'"': "&quot;", "'": "&apos;"})


def _size(n: int) -> str:
    """Compact byte size such as '812 B' or '1.2 KB'."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _retrievals(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = step.get("retrievals")
    return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []


def _round_of(step: Dict[str, Any]) -> int:
    return _int(step.get("round"))


def _agents(transcript: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Distinct (agent, role) pairs in order of first appearance."""
    seen: List[Tuple[str, str]] = []
    for step in transcript:
        key = (str(step.get("agent", "")), str(step.get("role", "")))
        if key not in seen:
            seen.append(key)
    return seen


def _rounds(transcript: Sequence[Dict[str, Any]]) -> List[int]:
    return sorted({_round_of(s) for s in transcript})


# ---------------------------------------------------------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------------------------------------------------------

def mermaid_flow(transcript: Sequence[Dict[str, Any]], status: str = "", max_rounds: int = MAX_ROUNDS) -> str:
    """A `sequenceDiagram` of the debate (without code fences). Valid Mermaid for any agent names or labels."""
    agents = _agents(transcript)
    ids = {key: f"A{i + 1}" for i, key in enumerate(agents)}
    lines = ["sequenceDiagram"]
    if not agents:
        lines.append('    participant T as Task')
        lines.append(f"    Note over T: {_mm(status or 'no transcript')}")
        return "\n".join(lines)
    for key in agents:
        lines.append(f'    participant {ids[key]} as {_mm(key[0], 40)} ({_mm(key[1], 30)})')

    rounds = _rounds(transcript)
    for number in rounds[:max_rounds]:
        steps = [s for s in transcript if _round_of(s) == number]

        def ident(step: Dict[str, Any]) -> str:
            return ids[(str(step.get("agent", "")), str(step.get("role", "")))]

        by_kind: Dict[str, List[Dict[str, Any]]] = {"expert": [], "critic": [], "judge": [], "other": []}
        for step in steps:
            by_kind[_role_kind(str(step.get("role", "")))].append(step)
        lines.append(f"    Note over {ident(steps[0])},{ident(steps[-1])}: Round {number}")
        for step in steps:
            kind = _role_kind(str(step.get("role", "")))
            reads = len(_retrievals(step))
            label = _mm(step.get("provider", "?"), 30)
            if reads:
                label += f" / {reads} reads"
            me = ident(step)
            if kind == "expert":
                targets = by_kind["critic"] or by_kind["judge"]
                word = "proposal"
            elif kind == "critic":
                targets = by_kind["judge"] or by_kind["expert"]
                word = "critique"
            elif kind == "judge":
                targets = by_kind["expert"]
                word = "verdict approved" if step.get("consensus") else "verdict"
            else:
                targets = []
                word = "reply"
            if kind == "expert":
                for target in targets or [step]:
                    lines.append(f"    {me}->>{ident(target)}: {word} ({label})")
            else:
                target = targets[0] if targets else step
                lines.append(f"    {me}->>{ident(target)}: {word} ({label})")
    if len(rounds) > max_rounds:
        lines.append(f"    Note over A1: +{len(rounds) - max_rounds} more rounds")
    everyone = [ids[k] for k in agents]
    over = f"{everyone[0]},{everyone[-1]}" if len(everyone) > 1 else everyone[0]
    lines.append(f"    Note over {over}: Decision {_mm(status or 'n/a', 40)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------------------------------------------

_ROLE_FILL = {"expert": "#dbeafe", "critic": "#fee2e2", "judge": "#fef3c7", "other": "#e5e7eb"}
_STATUS_FILL = {"APPROVED": "#bbf7d0", "UNVERIFIED": "#fecaca", "DISPUTED": "#fed7aa", "PENDING_JEV": "#fef08a"}
_TEXT = "#111827"
_MUTED = "#4b5563"
_FONT = "font-family=\"Helvetica, Arial, sans-serif\""


def _svg_open(width: int, height: int, title: str) -> List[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">',
        f"<title>{_x(title)}</title>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
    ]


def _text(x: int, y: int, content: Any, size: int = 12, fill: str = _TEXT, anchor: str = "start",
          weight: str = "normal", limit: int = 0) -> str:
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}" {_FONT}>{_x(content, limit)}</text>')


def _fit(svg: List[str]) -> str:
    out = "\n".join(svg) + "\n"
    if len(out.encode("utf-8")) > MAX_SVG_BYTES:  # unreachable with the row/column caps; kept as a hard guard
        raise ValueError("svg too large")
    return out


def summary_svg(transcript: Sequence[Dict[str, Any]], status: str = "", thread_id: str = "") -> str:
    """Lane chart: one row per agent, one column per round, a box per step, and the decision as a final badge."""
    all_agents = _agents(transcript)
    all_rounds = _rounds(transcript)
    agents = all_agents[:MAX_AGENTS]
    rounds = all_rounds[:MAX_ROUNDS]
    label_w, cell_w, cell_h, gap, top, badge_w = 230, 148, 58, 8, 64, 190
    width = label_w + len(rounds) * (cell_w + gap) + badge_w + 20
    note_rows = (1 if len(all_agents) > len(agents) else 0) + (1 if len(all_rounds) > len(rounds) else 0)
    height = top + max(1, len(agents)) * (cell_h + gap) + 24 + note_rows * 18
    width = max(width, 420)
    svg = _svg_open(width, height, f"Debate summary {thread_id}".strip())
    svg.append(_text(12, 24, f"Debate rounds {thread_id}".strip(), 15, weight="bold"))
    svg.append(_text(12, 44, f"{len(all_rounds)} rounds, {len(all_agents)} agents", 11, _MUTED))
    for col, number in enumerate(rounds):
        x = label_w + col * (cell_w + gap)
        svg.append(_text(x + cell_w // 2, top - 8, f"Round {number}", 12, anchor="middle", weight="bold"))
    for row, key in enumerate(agents):
        y = top + row * (cell_h + gap)
        kind = _role_kind(key[1])
        svg.append(f'<rect x="8" y="{y}" width="{label_w - 16}" height="{cell_h}" rx="6" fill="{_ROLE_FILL[kind]}" '
                   f'stroke="#9ca3af"/>')
        svg.append(_text(16, y + 24, key[0], 12, weight="bold", limit=28))
        svg.append(_text(16, y + 42, key[1], 11, _MUTED, limit=32))
        for col, number in enumerate(rounds):
            steps = [s for s in transcript if _round_of(s) == number
                     and (str(s.get("agent", "")), str(s.get("role", ""))) == key]
            if not steps:
                continue
            x = label_w + col * (cell_w + gap)
            step = steps[0]
            size = sum(len(str(s.get("content", "")).encode("utf-8")) for s in steps)
            reads = sum(len(_retrievals(s)) for s in steps)
            svg.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" rx="6" fill="{_ROLE_FILL[kind]}" '
                       f'stroke="#6b7280"/>')
            svg.append(_text(x + 8, y + 20, step.get("provider", "?"), 12, weight="bold", limit=20))
            svg.append(_text(x + 8, y + 37, f"output {_size(size)}", 11, _MUTED))
            if reads:
                svg.append(_text(x + 8, y + 51, f"{reads} retrievals", 11, _MUTED))
    bx = label_w + len(rounds) * (cell_w + gap) + 8
    fill = _STATUS_FILL.get(status, "#e5e7eb")
    svg.append(f'<rect x="{bx}" y="{top}" width="{badge_w - 8}" height="{cell_h}" rx="10" fill="{fill}" stroke="#374151"/>')
    svg.append(_text(bx + (badge_w - 8) // 2, top + 24, "Decision", 11, _MUTED, "middle"))
    svg.append(_text(bx + (badge_w - 8) // 2, top + 44, status or "n/a", 13, anchor="middle", weight="bold", limit=20))
    note_y = top + max(1, len(agents)) * (cell_h + gap) + 14
    if len(all_agents) > len(agents):
        svg.append(_text(12, note_y, f"+{len(all_agents) - len(agents)} more agents not shown", 11, _MUTED))
        note_y += 18
    if len(all_rounds) > len(rounds):
        svg.append(_text(12, note_y, f"+{len(all_rounds) - len(rounds)} more rounds not shown", 11, _MUTED))
    svg.append("</svg>")
    return _fit(svg)


def retrieval_bytes(transcript: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, int, int]]:
    """(agent, role, bytes read, retrievals) per agent that retrieved anything, in order of first appearance."""
    totals: Dict[Tuple[str, str], List[int]] = {}
    for step in transcript:
        records = _retrievals(step)
        if not records:
            continue
        slot = totals.setdefault((str(step.get("agent", "")), str(step.get("role", ""))), [0, 0])
        slot[0] += sum(_int(r.get("bytes")) for r in records)
        slot[1] += len(records)
    return [(k[0], k[1], v[0], v[1]) for k, v in totals.items()]


def retrieval_svg(transcript: Sequence[Dict[str, Any]], thread_id: str = "") -> str:
    """Horizontal bar chart of bytes read per agent. Empty string when nothing was retrieved."""
    rows = retrieval_bytes(transcript)
    if not rows:
        return ""
    shown = rows[:MAX_AGENTS]
    label_w, bar_max, row_h, top = 250, 420, 30, 56
    width = label_w + bar_max + 150
    height = top + len(shown) * row_h + 20 + (18 if len(rows) > len(shown) else 0)
    peak = max(r[2] for r in shown) or 1
    svg = _svg_open(width, height, f"Retrieval bytes {thread_id}".strip())
    svg.append(_text(12, 24, "Bytes read per agent", 15, weight="bold"))
    svg.append(_text(12, 42, f"{sum(r[2] for r in rows)} bytes in {sum(r[3] for r in rows)} retrievals", 11, _MUTED))
    for i, (agent, role, nbytes, count) in enumerate(shown):
        y = top + i * row_h
        length = max(1, round(bar_max * nbytes / peak)) if nbytes else 1
        svg.append(_text(12, y + 17, f"{agent} ({role})", 12, limit=34))
        svg.append(f'<rect x="{label_w}" y="{y + 3}" width="{length}" height="{row_h - 10}" fill="#60a5fa" stroke="#1d4ed8"/>')
        svg.append(_text(label_w + length + 8, y + 17, f"{_size(nbytes)} / {count}", 11, _MUTED))
    if len(rows) > len(shown):
        svg.append(_text(12, top + len(shown) * row_h + 14, f"+{len(rows) - len(shown)} more agents not shown", 11, _MUTED))
    svg.append("</svg>")
    return _fit(svg)


# ---------------------------------------------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------------------------------------------

def _title(task: str) -> str:
    first = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    first = " ".join(_CONTROL_RE.sub("", first).split())
    if len(first) > TITLE_LIMIT:
        first = first[: TITLE_LIMIT - 1].rstrip() + "…"
    return first or "(제목 없음)"


_FENCE_LINE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(\s{0,3})(#{1,6})(\s+\S.*|\s*)$")
# Lines that open a new item of a judge's decision or rationale: `[R1]`, `[1]`, `[추가]`, `(1)`.
_ITEM_LINE_RE = re.compile(r"^\s*(?:\[(?:R?\d{1,2}|추가|Extra)\]|\(\d{1,2}\))\s", re.IGNORECASE)
_BOLD_ITEM_RE = re.compile(r"^(\s*)(\[(?:R?\d{1,2}|추가|Extra)\]|\(\d{1,2}\))(?=\s)", re.IGNORECASE)


def _outside_fences(text: str) -> List[Tuple[str, bool]]:
    """(line, in_fence) per line; fence lines themselves count as inside. Unclosed fences run to the end."""
    out: List[Tuple[str, bool]] = []
    fence = ""
    for line in str(text or "").splitlines():
        match = _FENCE_LINE_RE.match(line)
        if fence:
            out.append((line, True))
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line.strip()[len(match.group(1)):].strip():
                fence = ""
        elif match:
            fence = match.group(1)
            out.append((line, True))
        else:
            out.append((line, False))
    return out


def demote_headings(text: Any, base: int = 4) -> str:
    """Agent text with its headings moved below the report's own: the shallowest becomes `base` hashes.

    Agents write their own `#`/`##` outline; pasted as is, it outranks the report's sections. Fenced code is
    left alone, and no heading goes deeper than ###### (deeper levels are clamped).
    """
    lines = _outside_fences(text)
    levels = [len(m.group(2)) for ln, inside in lines if not inside for m in [_HEADING_RE.match(ln)] if m]
    if not levels:
        return "\n".join(ln for ln, _ in lines)
    shift = base - min(levels)
    out: List[str] = []
    for ln, inside in lines:
        match = None if inside else _HEADING_RE.match(ln)
        out.append(f"{match.group(1)}{'#' * max(1, min(6, len(match.group(2)) + shift))}{match.group(3)}" if match else ln)
    return "\n".join(out)


def _item_paragraphs(text: Any, bold: bool = True) -> List[str]:
    """Lines of a decision or rationale where every item marker starts its own paragraph.

    Consecutive lines are one paragraph in Markdown, so `[R1] ...` / `[R2] ...` would run together. The marker is
    set in bold so the items read as a list; fenced code is left alone.
    """
    out: List[str] = []
    for ln, inside in _outside_fences(text):
        if not inside and _ITEM_LINE_RE.match(ln):
            if out and out[-1].strip():
                out.append("")
            if bold:
                ln = _BOLD_ITEM_RE.sub(lambda m: f"{m.group(1)}**{m.group(2)}**", ln, count=1)
        out.append(ln)
    return out


def _rationale_lines(text: str) -> List[str]:
    """The rationale as paragraphs; a leading heading that only repeats the section name is dropped."""
    lines = _item_paragraphs(demote_headings(text, base=4), bold=False)
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is not None:
        match = _HEADING_RE.match(lines[first])
        if match and re.fullmatch(r"(근거|rationale)\s*:?", match.group(3).strip(), re.IGNORECASE):
            lines = lines[first + 1:]
            while lines and not lines[0].strip():
                lines.pop(0)
    return lines


_REQ_MARK = re.compile(r"^\s*[>*_ -]*\[(R\d{1,2})\]\s*(.*)$")


def _refs_of(value: Any) -> List[str]:
    return [r for r in value if isinstance(r, str) and r] if isinstance(value, list) else []


def _aligned_refs(value: Any, count: int) -> List[List[str]]:
    """The refs lists padded or cut to `count` entries (a stored decision may predate refs)."""
    items = [_refs_of(r) for r in value] if isinstance(value, list) else []
    return (items + [[] for _ in range(count)])[:count]


def _refs_suffix(refs: List[str]) -> str:
    return f" ({', '.join(refs)})" if refs else ""


def _requirements(text: Any) -> List[Tuple[str, str]]:
    """(id, title) per `[R1] title: text` line of the decision; the title is the text before the first ':'."""
    found: List[Tuple[str, str]] = []
    for line in str(text or "").splitlines():
        match = _REQ_MARK.match(line)
        if match and match.group(1) not in [r for r, _ in found]:
            body = match.group(2)
            found.append((match.group(1), body.split(":", 1)[0].strip() if ":" in body else body.strip()[:60]))
    return found


def _trace_lines(decision: Dict[str, Any], constraints: List[str], constraint_refs: List[List[str]],
                 checks: List[str], check_refs: List[List[str]], actions: List[Dict[str, Any]]) -> List[str]:
    """The `### 추적` table: per requirement id its constraints, checks and files; "변경 없음" when no action."""
    requirements = _requirements(decision.get("decision"))
    if not requirements:
        return []
    lines = ["", "### 추적", "", "| 요구사항 | 제목 | 제약 | 확인 | 파일 |", "| --- | --- | --- | --- | --- |"]
    for rid, title in requirements:
        files: List[str] = []
        for item in actions:
            path = item.get("file_path")
            if rid in _refs_of(item.get("refs")) and isinstance(path, str) and path not in files:
                files.append(path)
        has_action = any(rid in _refs_of(a.get("refs")) for a in actions)
        lines.append(f"| {rid} | {_cell(title, 80)} | {sum(rid in r for r in constraint_refs)} "
                     f"| {sum(rid in r for r in check_refs)} "
                     f"| {_cell(', '.join(files)) if files else ('(경로 미상)' if has_action else '변경 없음')} |")
    return lines


def _summary_lines(decision: Optional[Dict[str, Any]], status: str) -> List[str]:
    lines = [f"- 결정 상태: **{status_label(status)}**"]
    if not decision:
        lines.append("- 최종 결정이 기록되지 않았습니다.")
        return lines
    if decision.get("final_round") is not None:
        lines.append(f"- 최종 라운드: {_cell(decision.get('final_round'))}")
    if decision.get("llm_error"):
        lines.append(f"- LLM 오류: {_cell(decision.get('llm_error'), 300)}")
    gate = decision.get("jev_gate")
    jev = decision.get("jev") if isinstance(decision.get("jev"), dict) else {}
    if gate or jev:
        lines.append(f"- jev 게이트: **{_cell(gate or jev.get('gate'))}**")
        for reason in (jev.get("reasons") or [])[:10]:
            lines.append(f"  - {_cell(reason, 300)}")
    text = decision.get("decision")
    if isinstance(text, str) and text.strip():
        lines += ["", "### 판결", ""] + _item_paragraphs(demote_headings(text.strip()))
    else:
        judgment = _FENCE_RE.sub("", str(decision.get("judgment") or "")).strip()
        if judgment:
            excerpt = judgment[:800] + ("…" if len(judgment) > 800 else "")
            lines += ["", "### 판결 (발췌)", ""] + _item_paragraphs(demote_headings(excerpt))
    rationale = decision.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        lines += ["", "### 근거", ""] + _rationale_lines(rationale.strip())
    constraints = [c for c in (decision.get("constraints") or []) if isinstance(c, str)]
    constraint_refs = _aligned_refs(decision.get("constraint_refs"), len(constraints))
    if constraints:
        lines += ["", "### 제약", ""] + [f"- {_cell(c, 300)}{_refs_suffix(r)}" for c, r in zip(constraints, constraint_refs)]
    checks = [c for c in (decision.get("checks") or []) if isinstance(c, str)]
    check_refs = _aligned_refs(decision.get("check_refs"), len(checks))
    if checks:
        lines += ["", "### 확인 항목", ""] + [f"- {_cell(c, 300)}{_refs_suffix(r)}" for c, r in zip(checks, check_refs)]
    actions = [a for a in (decision.get("actions") or []) if isinstance(a, dict)]
    if actions:
        with_refs = any(_refs_of(a.get("refs")) for a in actions)
        header = "| # | 도구 | 상태 | 비고 |" + (" 요구사항 |" if with_refs else "")
        lines += ["", "### 제안된 조치", "", header, "| --- | --- | --- | --- |" + (" --- |" if with_refs else "")]
        for item in actions:
            row = (f"| {_cell(item.get('id'))} | {_cell(item.get('tool'))} | {_cell(item.get('status'))} "
                   f"| {_cell(item.get('error') or '')} |")
            if with_refs:
                row += f" {_cell(', '.join(_refs_of(item.get('refs'))))} |"
            lines.append(row)
    lines += _trace_lines(decision, constraints, constraint_refs, checks, check_refs, actions)
    return lines


def build_report(
    project_id: str,
    thread_id: str,
    task: str,
    transcript: Sequence[Dict[str, Any]],
    decision: Optional[Dict[str, Any]],
    status: str,
    team: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    updated: str = "",
) -> Tuple[str, Dict[str, str]]:
    """The report Markdown and its assets ({file name: svg text}). Deterministic for the same arguments."""
    assets: Dict[str, str] = {SUMMARY_ASSET: summary_svg(transcript, status, thread_id)}
    retrieval = retrieval_svg(transcript, thread_id)
    if retrieval:
        assets[RETRIEVAL_ASSET] = retrieval
    rounds = len(_rounds(transcript))
    ctx = "-"
    if context:
        files = context.get("files") or []
        ctx = f"`{str(context.get('sha', ''))[:12]}` · {len(files)} files"

    md: List[str] = [
        f"# 토론 결과: {_title(task)}",
        "",
        "| 항목 | 값 |",
        "| --- | --- |",
        f"| 프로젝트 | `{_cell(project_id)}` |",
        f"| 스레드 ID | `{_cell(thread_id)}` |",
        f"| 팀 | {('`' + _cell(team.get('id')) + '`') if team else '-'} |",
        f"| 코드 컨텍스트 | {ctx} |",
        f"| 결정 상태 | {status_label(status)} |",
        f"| 업데이트 | {_cell(updated)} |",
        f"| 라운드 | {rounds} |",
        "",
        "## 요약",
        "",
        *_summary_lines(decision, status),
        "",
        "## 진행 흐름",
        "",
        "```mermaid",
        mermaid_flow(transcript, status),
        "```",
        "",
        "## 라운드 요약",
        "",
        f"![라운드 요약]({thread_id}.assets/{SUMMARY_ASSET})",
        "",
    ]
    if retrieval:
        md += ["## 검색 사용량", "", f"![에이전트별 검색 바이트]({thread_id}.assets/{RETRIEVAL_ASSET})", ""]
    md += ["## 토론 기록", ""]
    for index, step in enumerate(transcript):
        if index:
            md += ["---", ""]
        md.append(f"### Round {step.get('round')} · {step.get('agent')} ({step.get('role')})")
        md.append("")
        md.append(f"*Provider: {step.get('provider')}*")
        md.append("")
        for failure in step.get("fallbacks", []) or []:
            md.append(f"> skipped {failure}")
            md.append("")
        md.append(demote_headings(step.get("content", "")))
        md.append("")
    return "\n".join(md).rstrip("\n") + "\n", assets


# ---------------------------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    """Write text to path via a temp file in the same directory and os.replace."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_report(project_dir: Path, thread_id: str, markdown: str, assets: Dict[str, str]) -> Path:
    """Write `<thread_id>.md` and `<thread_id>.assets/*.svg`, removing svg files this save no longer produces.

    Assets go first so the report never links a missing file. Only regular files named like an asset are ever
    removed; symlinks (and anything else) are left alone.
    """
    if not thread_id or "/" in thread_id or "\\" in thread_id or thread_id in (".", ".."):
        raise ValueError(f"unsafe thread id: {thread_id!r}")
    for name in assets:
        if not ASSET_NAME_RE.match(name):
            raise ValueError(f"invalid asset name: {name!r}")
    project_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = project_dir / f"{thread_id}.assets"
    if assets_dir.is_symlink():
        print(f"warning: {assets_dir} is a symlink; assets not written", file=sys.stderr)
    else:
        if assets:
            assets_dir.mkdir(exist_ok=True)
        if assets_dir.is_dir():
            for name, svg in assets.items():
                _atomic_write(assets_dir / name, svg)
            with os.scandir(assets_dir) as entries:
                stale = [e for e in entries
                         if ASSET_NAME_RE.match(e.name) and e.name not in assets
                         and e.is_file(follow_symlinks=False) and not e.is_symlink()]
            for entry in stale:
                os.unlink(entry.path)
            if not assets:
                try:
                    assets_dir.rmdir()  # only succeeds when empty
                except OSError:
                    pass
    md_path = project_dir / f"{thread_id}.md"
    _atomic_write(md_path, markdown)
    return md_path

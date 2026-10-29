"""
Deterministic digest of a debate thread's earlier rounds (design/design-project.md, decision 12, step 1).

The engine replays the whole transcript only to agents that start a fresh CLI session (the others resume theirs).
On a long thread that replay is the largest part of the first prompt. When it passes a threshold this module
condenses every round except the latest one: each entry keeps its head and is marked as condensed. No LLM is
involved; the output depends only on the transcript.

The digest is navigation only, never evidence: the header says so, and the line-span rule of evidence.py still
decides what a citation may rest on (only lines shown to the session count).
"""

import os
import re
from typing import Any, Dict, List, Optional

DIGEST_VERSION = 1
DEFAULT_MIN_BYTES = 16 * 1024
# Characters kept from the head of an earlier entry, per role.
HEAD_CHARS = {"Domain Expert": 1200, "Critic Red Team": 800, "Judge": 800}
DEFAULT_HEAD_CHARS = 800
VERDICT_LINE = re.compile(r"^.*VERDICT\s*[:：].*$", re.IGNORECASE | re.MULTILINE)
HEADER = (
    "(Rounds before round {latest} are condensed: {before} bytes became {after}. This summary is navigation only, "
    "not evidence; open the code again before you quote or cite it. Round {latest} is complete.)"
)


def enabled() -> bool:
    """GUARDIAN_THREAD_DIGEST=off (also 0/false/no) keeps the full replay."""
    return os.environ.get("GUARDIAN_THREAD_DIGEST", "on").strip().lower() not in ("off", "0", "false", "no")


def min_bytes() -> int:
    """Replays shorter than this are sent as they are (GUARDIAN_THREAD_DIGEST_MIN_BYTES)."""
    raw = os.environ.get("GUARDIAN_THREAD_DIGEST_MIN_BYTES", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MIN_BYTES
    return value if value >= 0 else DEFAULT_MIN_BYTES


def format_entry(entry: Dict[str, Any], content: Optional[str] = None) -> str:
    text = entry.get("content", "") if content is None else content
    return f"[Round {entry.get('round')}] {entry.get('role')} ({entry.get('agent')}):\n{text}"


def _condense(entry: Dict[str, Any]) -> str:
    text = str(entry.get("content", "") or "")
    limit = HEAD_CHARS.get(str(entry.get("role")), DEFAULT_HEAD_CHARS)
    if len(text) <= limit:
        return format_entry(entry, text)
    head = text[:limit].rstrip()
    # A cut inside a code fence would leave it open and swallow the text that follows.
    if head.count("```") % 2:
        head += "\n```"
    kept = head
    if str(entry.get("role")) == "Judge":
        verdicts = VERDICT_LINE.findall(text[limit:])
        if verdicts:
            kept += "\n" + verdicts[-1].strip()
    return format_entry(entry, f"{kept}\n… [{len(text) - limit} more characters condensed]")


def _round_number(entry: Dict[str, Any]) -> int:
    try:
        return int(entry.get("round") or 0)
    except (TypeError, ValueError):
        return 0


def format_history(transcript: List[Dict[str, Any]], force: bool = False) -> str:
    """The earlier rounds as text for a fresh session: in full below the threshold, digested above it."""
    full = "\n\n".join(format_entry(t) for t in transcript)
    if not transcript or (not force and (not enabled() or len(full.encode("utf-8")) < min_bytes())):
        return full
    latest = max(_round_number(t) for t in transcript)
    older = [t for t in transcript if _round_number(t) < latest]
    if not older:
        return full
    parts = [_condense(t) for t in older] + [format_entry(t) for t in transcript if _round_number(t) >= latest]
    body = "\n\n".join(parts)
    header = HEADER.format(latest=latest, before=len(full.encode("utf-8")), after=len(body.encode("utf-8")))
    digest = f"{header}\n\n{body}"
    return digest if len(digest) < len(full) else full

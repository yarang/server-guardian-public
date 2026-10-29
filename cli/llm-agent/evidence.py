"""
Evidence for the citations of debate agents (docs/design/debate-agent-retrieval.md, "P2.5").

Two independent parts, both deterministic and stdlib only:

1. Line-span provenance. A strong citation (`path:N`, `path:N-M`, `path#Symbol`, `path` + "line N") is backed
   only by lines the agent session was really shown, give or take GUARDIAN_CITATION_SLACK lines, or by a file it
   received whole. The helpers here work on sorted, merged (start, end) spans of 1-based inclusive line numbers;
   a file shown whole is the single span (1, WHOLE_FILE_END).
2. The optional ```findings block an agent may end its answer with: JSON [{"claim", "path", "line", "quote"}].
   Each quote is looked up (whitespace-normalized) in the file at the pinned commit within line +/- slack.
"""

import json
import os
import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Protocol, Set, Tuple

Span = Tuple[int, int]

# A file shown completely is stored as one span reaching this line, so coverage code needs no special case.
WHOLE_FILE_END = 1_000_000_000
DEFAULT_CITATION_SLACK = 3
MAX_FINDINGS = 50
MAX_QUOTE_CHARS = 300
MAX_SHOWN_RANGES_LISTED = 10


def citation_slack() -> int:
    """GUARDIAN_CITATION_SLACK: how many lines beside a shown line a citation may still point at (default 3, 0 = exact)."""
    try:
        value = int(os.environ.get("GUARDIAN_CITATION_SLACK", "").strip())
    except ValueError:
        return DEFAULT_CITATION_SLACK
    return value if value >= 0 else DEFAULT_CITATION_SLACK


# --- spans ---------------------------------------------------------------------------------------

def coalesce(spans: Any) -> List[Span]:
    """Sorted spans with overlapping and touching ones merged. Malformed items are dropped."""
    clean: List[Span] = []
    for item in spans or []:
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if start >= 1 and end >= start:
            clean.append((start, end))
    clean.sort()
    merged: List[List[int]] = []
    for start, end in clean:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def is_whole(spans: List[Span]) -> bool:
    return bool(spans) and spans[0][0] <= 1 and spans[-1][1] >= WHOLE_FILE_END


def lines_covered(spans: List[Span], first: int, last: int, slack: int) -> int:
    """How many of the lines first..last lie within slack lines of a shown span."""
    total = 0
    # Merge the slack-extended spans first so lines between two close spans are counted once.
    for start, end in coalesce([(max(1, s - slack), e + slack) for s, e in spans]):
        lo, hi = max(first, start), min(last, end)
        if hi >= lo:
            total += hi - lo + 1
    return total


def line_covered(spans: List[Span], number: int, slack: int) -> bool:
    return any(start - slack <= number <= end + slack for start, end in spans)


def range_covered(spans: List[Span], first: int, last: int, slack: int) -> bool:
    """Rule for `path:N-M`: the first line is covered (with slack) and at least half of the lines are."""
    if last < first:
        last = first
    if not line_covered(spans, first, slack):
        return False
    count = last - first + 1
    return lines_covered(spans, first, last, slack) * 2 >= count


def overlaps(spans: List[Span], declaration: Span) -> bool:
    """Whether a declaration span shares a line with a shown span."""
    return any(start <= declaration[1] and declaration[0] <= end for start, end in spans)


def describe_spans(spans: List[Span], limit: int = MAX_SHOWN_RANGES_LISTED) -> str:
    """Short text for a re-ask: 'lines 10-20, 55, 100-120 (+3 more ranges)'."""
    if is_whole(spans):
        return "the whole file"
    if not spans:
        return "no lines"
    parts = [str(s) if s == e else f"{s}-{e}" for s, e in spans[:limit]]
    more = len(spans) - len(parts)
    return "lines " + ", ".join(parts) + (f" (+{more} more ranges)" if more > 0 else "")


# --- where a citation points ---------------------------------------------------------------------

# The detail that follows a path token: ":12", ":12-20", "#Symbol", " line 12", " lines 12-20", " (L12)". It must accept
# every text repo_tools.STRONG_SUFFIX accepts, so a strong citation always has a location.
_CLOSERS = r"[`'\"*)\]]*"
LOCATION = re.compile(
    _CLOSERS + r"(?:"
    r":\s*(?P<a>\d{1,7})(?:\s*[-–]\s*L?(?P<b>\d{1,7}))?"
    r"|#(?P<sym>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"|\s*,?\s*\(?(?:[Ll]ines?|L)\s*(?P<la>\d{1,7})(?:\s*[-–]\s*L?(?P<lb>\d{1,7}))?"
    r")"
)

# ("lines", first, last) or ("symbol", name)
Location = Tuple[Any, ...]


def parse_location(text: str, pos: int) -> Optional[Location]:
    found = LOCATION.match(text, pos)
    if found is None:
        return None
    if found.group("sym"):
        return ("symbol", found.group("sym"))
    first = int(found.group("a") or found.group("la") or 0)
    second = found.group("b") or found.group("lb")
    last = int(second) if second else first
    return ("lines", first, max(first, last))


# --- short citations (P2.7) ----------------------------------------------------------------------

class ShortRef(NamedTuple):
    """A citation that is not a whole repository path: `guardian.py:755`, `dir/name.py#Sym`, or a bare `line 510`.

    raw: the text shown to people ("guardian.py:755", "line 510"); candidates: tree paths it can mean, or () for a
    line reference with no file in reach; location: ("lines", a, b) or ("symbol", name); line_only: no path written.
    """

    raw: str
    candidates: Tuple[str, ...]
    location: Optional[Location]
    line_only: bool


# A file name with an extension that starts with a letter: rules out versions (go1.26.8), times (12:30), hosts.
_FILE_EXT = re.compile(r"[^/]\.[A-Za-z][A-Za-z0-9]{0,9}$")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)\s*([^\s`]*)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
# Fences holding prose-like text (call trees, listings): their path-less "line N" annotations are citations.
# Fences with another info string (python, go, json, diff...) are code, where "line 5" is program text.
_TEXT_FENCES = {"", "text", "txt", "plain", "plaintext", "tree", "console", "output", "log", "ascii", "diagram", "none"}
_LINE_ONLY = re.compile(
    r"(?<![A-Za-z0-9_])(?:[Ll]ines?|줄)\s*(?P<a>\d{1,7})(?:\s*[-–~]\s*L?(?P<b>\d{1,7}))?"
    r"|(?<=\()L\s?(?P<la>\d{1,7})(?:\s*[-–]\s*L?(?P<lb>\d{1,7}))?"
)
_OF_PATH = re.compile(r"\s*(?:of|in|의)\s+[`'\"*(]*(?P<tok>[\w.\-/]+)")


def _clean_token(token: str) -> str:
    token = token.rstrip(".,-")
    while token.startswith("./"):
        token = token[2:]
    return token.lstrip("/")


def _shown_location(token: str, where: Location) -> str:
    if where[0] == "symbol":
        return f"{token}#{where[1]}"
    return f"{token}:{where[1]}" if where[1] == where[2] else f"{token}:{where[1]}-{where[2]}"


def _line_location(found: "re.Match[str]") -> Location:
    first = int(found.group("a") or found.group("la"))
    second = found.group("b") or found.group("lb")
    return ("lines", first, max(first, int(second)) if second else first)


def scan_short_refs(text: str, known: Set[str], by_name: Dict[str, List[str]]) -> List[ShortRef]:
    """Strong citations that do not name a whole repository path, in order of appearance, without repeats.

    1. Short paths: a token ending in a file name with an extension that is a suffix of tree paths (`guardian.py`,
       `llm-agent/guardian.py`, not a path of the tree by itself), followed by a strong suffix (`:N`, `:N-M`, `#Sym`,
       " line N"). A token that matches no tree path is ignored. A bare mention is not a citation but becomes the
       file that later path-less line references of the same context belong to.
    2. Path-less line references: `line N`, `lines N-M`, `(line N)`, `(L N)`, `줄 N`, not attached to a path. Each
       belongs to the nearest preceding path or short path (whole or bare) of its context, or to "<line> of <path>"
       written right after it; with none it is a line reference without a file (candidates ()).
       Context: the line itself, then its list item or paragraph. A nested list item inherits the file of the
       enclosing item; a paragraph (blank line) or heading resets it; a fenced block inherits the paragraph before
       it and blank lines inside it do not reset. Fences with a language info string (```python) are program text,
       so only their explicit paths count; fences with no info string or a text-like one (text, tree, console...)
       are checked like prose. ```findings blocks are skipped.
    """
    refs: List[ShortRef] = []
    seen: Set[Tuple[str, Tuple[str, ...]]] = set()

    def add(ref: ShortRef) -> None:
        marker = (ref.raw, ref.candidates)
        if marker not in seen:
            seen.add(marker)
            refs.append(ref)

    def candidates_of(token: str) -> Tuple[str, ...]:
        if token in known:
            return (token,)
        if not _FILE_EXT.search(token):
            return ()
        name = token.rsplit("/", 1)[-1]
        return tuple(sorted(p for p in by_name.get(name, []) if p == token or p.endswith("/" + token)))

    stack: List[List[Any]] = [[-1, None]]  # [indent, context] with context = candidates of the nearest file
    fence: Optional[str] = None
    before_blank: Any = None  # the file of the paragraph that a blank line just ended (a fence right after it inherits it)
    for line in (text or "").splitlines():
        opened = _FENCE.match(line)
        if opened is not None:
            if fence is None and stack[-1][1] is None:
                stack[-1][1] = before_blank
            fence = None if fence is not None else opened.group(1).lower()
            before_blank = None
            continue
        if fence == "findings":
            continue
        if fence is None:
            if not line.strip() or _HEADING.match(line):
                before_blank = (stack[-1][1] or before_blank) if not line.strip() else None
                stack = [[-1, None]]
                if not line.strip():
                    continue
            else:
                before_blank = None
            bullet = _BULLET.match(line)
            if bullet is not None:
                indent = len(bullet.group(1))
                while len(stack) > 1 and stack[-1][0] >= indent:
                    stack.pop()
                stack.append([indent, stack[-1][1]])
        check_lines = fence is None or fence in _TEXT_FENCES
        events: List[Tuple[int, int, Optional[ShortRef], Optional[Tuple[str, ...]]]] = []
        consumed: List[Tuple[int, int]] = []
        for match in re.finditer(r"[\w.\-/]+", line):
            token = _clean_token(match.group(0))
            cands = candidates_of(token) if token else ()
            if not cands:
                continue
            found = LOCATION.match(line, match.end())
            if found is not None:
                consumed.append((match.end(), found.end()))
            if found is not None and token not in known:
                where = parse_location(line, match.end())
                if where is not None:
                    events.append((match.start(), 0, ShortRef(_shown_location(token, where), cands, where, False), cands))
                    continue
            events.append((match.start(), 0, None, cands))
        if check_lines:
            for match in _LINE_ONLY.finditer(line):
                if any(a <= match.start() < b for a, b in consumed):
                    continue
                where = _line_location(match)
                raw = f"line {where[1]}" if where[1] == where[2] else f"lines {where[1]}-{where[2]}"
                trailing = _OF_PATH.match(line, match.end())
                named = candidates_of(_clean_token(trailing.group("tok"))) if trailing else ()
                events.append((match.start(), 1, ShortRef(raw, named, where, True), named or None))
        events.sort(key=lambda e: (e[0], e[1]))
        for _pos, _order, ref, cands in events:
            if ref is not None and ref.line_only:
                if ref.candidates:
                    add(ref)
                else:
                    add(ref._replace(candidates=tuple(stack[-1][1] or ())))
                continue
            if ref is not None:
                add(ref)
            if cands:
                stack[-1][1] = cands
    return refs


# --- the findings block --------------------------------------------------------------------------

# The closing fence must start its own line: a JSON string cannot hold a raw line break, so a quote that itself
# contains ``` (a fenced example in the code) can never end the block early.
FINDINGS_BLOCK = re.compile(r"```findings[ \t]*\r?\n(?:(.*?)\r?\n)?[ \t]*```[ \t]*(?=\r?\n|$)", re.DOTALL)
_NUMBERED_PREFIX = re.compile(r"^\s*\d+\|\s?")


def strip_findings(text: str) -> str:
    """The text without its ```findings blocks (verdict and decision parsing must not read inside them)."""
    return FINDINGS_BLOCK.sub("", text or "")


def parse_findings(text: str) -> Optional[List[Any]]:
    """The items of the last ```findings block (at most MAX_FINDINGS), or None when the text has no such block.

    A block whose JSON is invalid, or is not a list, yields an empty list: it was present, nothing could be checked.
    """
    blocks = FINDINGS_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except ValueError:
        return []
    return data[:MAX_FINDINGS] if isinstance(data, list) else []


def normalize_text(text: str) -> str:
    return " ".join(text.split())


class SourceReader(Protocol):
    def readable(self, path: str) -> bool: ...

    def source_text(self, path: str) -> Optional[str]: ...


def check_findings(reader: SourceReader, items: List[Any], slack: int) -> List[Dict[str, Any]]:
    """One {"path", "line", "ok", "reason"} per item: is its quote in the file within line +/- slack?

    The quote is compared whitespace-normalized, after dropping "N| " prefixes copied from numbered results and cutting it
    to MAX_QUOTE_CHARS. A quote spanning several lines may run on past line + slack by that many lines.
    """
    checked: List[Dict[str, Any]] = []
    for item in items[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            checked.append({"path": "", "line": 0, "ok": False, "reason": "item is not an object"})
            continue
        path = item.get("path") if isinstance(item.get("path"), str) else ""
        line = item.get("line") if isinstance(item.get("line"), int) and not isinstance(item.get("line"), bool) else 0
        quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
        checked.append({"path": path[:200], "line": line, **_check_one(reader, path, line, quote, slack)})
    return checked


def _check_one(reader: SourceReader, path: str, line: int, quote: str, slack: int) -> Dict[str, Any]:
    from repo_tools import normalize_path  # repo_tools imports this module, so import it late

    norm = normalize_path(path) if path else None
    if not norm or not reader.readable(norm):
        return {"ok": False, "reason": "path is not a readable file of this commit"}
    if line < 1:
        return {"ok": False, "reason": "line must be a positive integer"}
    quote_lines = [_NUMBERED_PREFIX.sub("", q) for q in quote.splitlines()]
    needle = normalize_text(" ".join(quote_lines))[:MAX_QUOTE_CHARS]
    if not needle:
        return {"ok": False, "reason": "empty quote"}
    source = reader.source_text(norm)
    if source is None:
        return {"ok": False, "reason": "file is not readable"}
    lines = source.splitlines()
    if line > len(lines):
        return {"ok": False, "reason": f"line is beyond the end of the file ({len(lines)} lines)"}
    extra = max(len([q for q in quote_lines if q.strip()]) - 1, 0)
    window = lines[max(1, line - slack) - 1:min(len(lines), line + slack + extra)]
    if needle in normalize_text(" ".join(window)):
        return {"ok": True, "reason": "verified"}
    return {"ok": False, "reason": f"quote not found within {slack} lines of line {line}"}

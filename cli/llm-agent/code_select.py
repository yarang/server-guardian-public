"""
Partial file selection for code context packs: symbols, line ranges and outlines.

Selectors (appended to a context path):
- `path#Name` / `path#Type.Method`: one top-level Go func, method or type, a Python def/class
  (dotted names reach methods), or a top-level TypeScript/JavaScript declaration (see code_index). Go is parsed by layout, which assumes gofmt-formatted code: a
  declaration starts at column 0 and its body ends at the next line that is exactly "}" or ")".
- `path:120-180`: a line range (1-based, inclusive). Lines stay stable because packs are pinned
  to a commit, and ranges reach code that has no name, such as handlers declared inside main().

A file read only partially also gets its import block verbatim (edits to a function often need new
imports, so the judge may change it) and an outline of top-level signatures with line numbers, so
agents can see what else exists and ask for it.
"""

import ast
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

SYMBOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
RANGE_PATTERN = re.compile(r"^(\d{1,6})-(\d{1,6})$")
MAX_OUTLINE_LINES = 200

Span = Tuple[int, int]  # 1-based inclusive line numbers


@dataclass(frozen=True)
class Selector:
    """What to take from the files a glob matches: the whole file, a symbol or a line range."""

    glob: str
    symbol: Optional[str] = None
    lines: Optional[Span] = None

    @property
    def label(self) -> str:
        if self.symbol:
            return f"#{self.symbol}"
        if self.lines:
            return f"lines {self.lines[0]}-{self.lines[1]}"
        return "whole file"


def parse_selector(pattern: str) -> Selector:
    """Splits 'glob#Symbol' or 'glob:START-END'. Raises ValueError for a malformed selector."""
    if "#" in pattern:
        glob, symbol = pattern.rsplit("#", 1)
        if not glob or not SYMBOL_PATTERN.match(symbol):
            raise ValueError(f"invalid symbol selector: {pattern!r} (use path#Name or path#Type.Method)")
        return Selector(glob, symbol=symbol)
    head, sep, tail = pattern.rpartition(":")
    if sep and RANGE_PATTERN.match(tail):
        start, end = (int(x) for x in RANGE_PATTERN.match(tail).groups())
        if not head or start < 1 or end < start:
            raise ValueError(f"invalid line range: {pattern!r} (use path:START-END with START <= END)")
        return Selector(head, lines=(start, end))
    return Selector(pattern)


TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def language(path: str) -> Optional[str]:
    if path.endswith(".go"):
        return "go"
    if path.endswith(".py"):
        return "python"
    if path.endswith(TS_EXTENSIONS):
        return "ts"
    return None


# --- Go ---------------------------------------------------------------------------------------

def _go_decl_span(lines: List[str], start: int) -> Span:
    """Span of the declaration starting at index start, plus its leading comment block."""
    first = start
    while first > 0 and lines[first - 1].startswith("//"):
        first -= 1
    line = lines[start].rstrip()
    if line.endswith("{") or line.endswith("("):
        closer = "}" if line.endswith("{") else ")"
        for end in range(start + 1, len(lines)):
            if lines[end].rstrip() == closer:
                return first + 1, end + 1
        return first + 1, len(lines)
    return first + 1, start + 1


def go_symbol(source: str, symbol: str) -> List[Span]:
    lines = source.splitlines()
    receiver, _, name = symbol.rpartition(".")
    if receiver:
        decl = re.compile(rf"^func \(\s*\w*\s*\*?{re.escape(receiver)}(?:\[[^\]]*\])?\s*\)\s*{re.escape(name)}\s*[(\[]")
    else:
        decl = re.compile(
            rf"^(?:func\s+{re.escape(name)}\s*[(\[]|type\s+{re.escape(name)}\b|(?:var|const)\s+{re.escape(name)}\b)"
        )
    return [_go_decl_span(lines, i) for i, line in enumerate(lines) if decl.match(line)]


def go_imports(source: str) -> List[Span]:
    """The import declarations: 'import (...)' blocks and single 'import "x"' lines."""
    lines = source.splitlines()
    spans = []
    for i, line in enumerate(lines):
        if line.startswith("import ("):
            end = next((j for j in range(i + 1, len(lines)) if lines[j].rstrip() == ")"), len(lines) - 1)
            spans.append((i + 1, end + 1))
        elif re.match(r'^import\s+(\w+\s+)?"', line):
            spans.append((i + 1, i + 1))
    return spans


def go_outline(source: str) -> List[str]:
    """Package clause and top-level declarations; imports are shown verbatim instead."""
    return [f"{number}: {line.rstrip().rstrip('{').rstrip()}"
            for number, line in enumerate(source.splitlines(), 1)
            if re.match(r"^(package|func|type|var|const)\b", line)]


# --- Python -----------------------------------------------------------------------------------

def _py_nodes(tree: ast.Module, symbol: str) -> List[ast.AST]:
    parts = symbol.split(".")
    scope: List[ast.AST] = list(tree.body)
    found: List[ast.AST] = []
    for depth, part in enumerate(parts):
        found = [n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 and n.name == part]
        if depth < len(parts) - 1:
            scope = [child for n in found if isinstance(n, ast.ClassDef) for child in n.body]
    return found


def python_symbol(source: str, symbol: str) -> List[Span]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans = []
    for node in _py_nodes(tree, symbol):
        start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        spans.append((start, node.end_lineno or node.lineno))
    return spans


def python_imports(source: str) -> List[Span]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [(n.lineno, n.end_lineno or n.lineno) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]


def python_outline(source: str) -> List[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(f"{node.lineno}: {lines[node.lineno - 1].rstrip()}")
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"{child.lineno}: {lines[child.lineno - 1].rstrip()}")
    return out


# --- shared -----------------------------------------------------------------------------------

def symbol_spans(path: str, source: str, symbol: str) -> List[Span]:
    lang = language(path)
    if lang == "go":
        return go_symbol(source, symbol)
    if lang == "python":
        return python_symbol(source, symbol)
    if lang == "ts":
        from code_index import ts_symbol_spans  # code_index imports this module, so import it late
        return ts_symbol_spans(source, symbol)
    return []


def declaration_lines(path: str, source: str) -> List[Span]:
    """Outline lines that are a complete declaration verbatim (package, const, var, type in Go).

    They count as seen code, so a judge can anchor a new declaration after one. Function signature
    lines are left out: changing a signature without seeing the body is not allowed.
    """
    if language(path) != "go":
        return []
    return [(n, n) for n, line in enumerate(source.splitlines(), 1)
            if re.match(r"^(package|const|var|type)\b", line)]


def import_spans(path: str, source: str) -> List[Span]:
    lang = language(path)
    if lang == "go":
        return go_imports(source)
    if lang == "python":
        return python_imports(source)
    if lang == "ts":
        from code_index import ts_import_spans
        return ts_import_spans(source)
    return []


def outline(path: str, source: str) -> List[str]:
    lang = language(path)
    if lang == "ts":
        from code_index import ts_outline
        lines = ts_outline(source)
    else:
        lines = go_outline(source) if lang == "go" else python_outline(source) if lang == "python" else []
    if len(lines) > MAX_OUTLINE_LINES:
        lines = lines[:MAX_OUTLINE_LINES] + [f"... {len(lines) - MAX_OUTLINE_LINES} more"]
    return lines


def outline_spans(path: str, source: str) -> List[Span]:
    """The source lines an outline of this file lists, as single-line spans (those lines are shown with their number)."""
    found = (re.match(r"(\d+): ", entry) for entry in outline(path, source))
    return [(int(m.group(1)), int(m.group(1))) for m in found if m]


def merge_spans(spans: List[Tuple[Span, str]]) -> List[Tuple[Span, List[str]]]:
    """Merges overlapping or adjacent spans, keeping the labels that asked for each."""
    merged: List[Tuple[Span, List[str]]] = []
    for (start, end), label in sorted(spans):
        if merged and start <= merged[-1][0][1] + 1:
            (m_start, m_end), labels = merged[-1]
            merged[-1] = ((m_start, max(m_end, end)), labels + [label])
        else:
            merged.append(((start, end), [label]))
    return merged


def render_partial(path: str, source: str, spans: List[Tuple[Span, str]]) -> str:
    """Outline plus the selected spans of one file."""
    lines = source.splitlines()
    parts = []
    summary = outline(path, source)
    if summary:
        parts.append(f"[outline of {path}: line: declaration]")
        parts.extend(summary)
    for (start, end), labels in merge_spans(spans):
        end = min(end, len(lines))
        parts.append(f"[{', '.join(dict.fromkeys(labels))}: lines {start}-{end}]")
        parts.extend(lines[start - 1:end])
    return "\n".join(parts)

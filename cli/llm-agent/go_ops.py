"""
Text of the Go-aware ops (docs/design/debate-code-index-wiki.md, "1층 확장", P2.6): refs and defs answered from
type-checked facts, plus callers and callees. Pure functions over GoFacts and the symbol index; RepoTools owns
validation, limits and the result envelope.

Every header line starts with "[" and none contains a `path:N: ` part, so repo_tools.provided_paths counts only the
`path:line: text` lines of refs as shown code (callers and callees provide no file at all, like defs).
"""

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from code_index import CodeIndex, coverage_note
from go_facts import GoFacts

LineTexts = Callable[[List[Tuple[str, int]]], Dict[Tuple[str, int], str]]
Matcher = Callable[[str], bool]

CALLERS_HEADER = ("[callers of {symbol}: type-checked ({go}); a call through an interface is listed under the "
                  "interface method]")
CALLEES_HEADER = ("[callees of {symbol}: type-checked ({go}); standard library and dependency targets are shown as "
                  "package.Name]")
NAME_BASED_REFS_HEADER = ("[refs of {symbol} in files without type information (non-Go files, Go files that were "
                          "not type-checked): name-based matches, may include unrelated symbols with the same name]")
NAME_BASED_DEFS_HEADER = "[defs of {symbol} in files without type information: name-based]"


def _clip(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def refs_text(facts: GoFacts, index: CodeIndex, symbol: str, identifier: str, in_scope: Optional[Matcher],
              include_defs: bool, line_texts: LineTexts, max_lines: int, max_chars: int) -> Tuple[str, bool]:
    """Lines of `refs` for a symbol the facts know: resolved references first, then name-based leftovers."""
    gdefs = facts.find_defs(symbol)
    typed = {(str(r["path"]), int(r["line"])) for r in facts.refs_to(int(d["id"]) for d in gdefs)
             if in_scope is None or in_scope(str(r["path"]))}
    if include_defs:
        typed |= {(str(d["path"]), int(d["start"])) for d in gdefs if in_scope is None or in_scope(str(d["path"]))}
    skip = set() if include_defs else {(d["path"], d["start"]) for d in index.find_defs(symbol)}
    leftover: List[Tuple[str, int]] = []
    for path, numbers in sorted(index.find_refs(identifier).items()):
        if (path.endswith(".go") and facts.covers(path)) or (in_scope is not None and not in_scope(path)):
            continue
        leftover.extend((path, n) for n in numbers if (path, n) not in skip)
    ordered = sorted(typed)
    total = len(ordered) + len(leftover)
    shown_typed = ordered[:max_lines]
    shown_other = leftover[:max(0, max_lines - len(shown_typed))]
    texts = line_texts(shown_typed + shown_other)

    def render(hits: Sequence[Tuple[str, int]]) -> List[str]:
        return [f"{path}:{number}: {_clip(texts.get((path, number), ''), max_chars)}" for path, number in hits]

    out = [f"[refs of {symbol}: type-checked ({facts.go}), {facts.package_count} packages; "
           "non-Go files are name-based]"]
    if len(gdefs) > 1:
        # Several declarations share the name; their paths stay out of the text (a path in a result counts as a mention).
        packages = ", ".join(sorted({str(d["pkg"]) for d in gdefs}))
        out.append(f"[{len(gdefs)} definitions match this name (packages: {packages}); "
                   "references to all of them are listed]")
    if shown_typed:
        out += render(shown_typed)
    elif not shown_other:
        out.append("(no references found)")
    if shown_other:
        out.append(NAME_BASED_REFS_HEADER.format(symbol=symbol))
        out += render(shown_other)
    truncated = total > max_lines
    if truncated:
        out.append("[... more references; narrow with a glob ...]")
    coverage = index.coverage(in_scope, identifier)
    coverage["unresolved"] = [str(u["path"]) for u in facts.unresolved_named(identifier, in_scope)]
    note = coverage_note(coverage)
    if note:
        out.append(note)
    return "\n".join(out), truncated


def _signature(index: CodeIndex, symbol: str, definition: Dict[str, Any]) -> str:
    for decl in index.find_defs(symbol):
        if decl["path"] == definition["path"] and decl["start"] == definition["start"]:
            return str(decl["signature"])
    return str(definition["qualified"])


def defs_text(facts: GoFacts, index: CodeIndex, symbol: str, in_scope: Optional[Matcher], max_lines: int,
              ) -> Tuple[str, bool]:
    """Lines of `defs` for a symbol the facts know: type-checked definitions, then name-based leftovers."""
    gdefs = [d for d in facts.find_defs(symbol) if in_scope is None or in_scope(str(d["path"]))]
    packages = {d["pkg"] for d in gdefs}
    lines = []
    for d in gdefs:
        line = f"{d['path']}:{d['start']}-{d['end']} {d['kind']} {_signature(index, symbol, d)}"
        lines.append(line + (f"  (package {d['pkg']})" if len(packages) > 1 else ""))
    leftover = [f"{d['path']}:{d['start']}-{d['end']} {d['kind']} {d['signature']}" for d in index.find_defs(symbol)
                if not (d["path"].endswith(".go") and facts.covers(d["path"]))
                and (in_scope is None or in_scope(d["path"]))]
    truncated = len(lines) + len(leftover) > max_lines
    shown = lines[:max_lines]
    other = leftover[:max(0, max_lines - len(shown))]
    out = [f"[defs of {symbol}: type-checked ({facts.go})]"]
    if shown:
        out += shown
    elif not other:
        out.append("(no definitions found)")
    if other:
        out.append(NAME_BASED_DEFS_HEADER.format(symbol=symbol))
        out += other
    if truncated:
        out.append("[... more definitions; narrow with a glob ...]")
    return "\n".join(out), truncated


def callers_text(facts: GoFacts, symbol: str, identifier: str, in_scope: Optional[Matcher], include_tests: bool,
                 line_texts: LineTexts, max_lines: int, max_chars: int) -> Tuple[str, bool]:
    """Call sites of a symbol: `path:line: enclosing  text`, only references that are calls."""
    gdefs = facts.find_defs(symbol)
    if not gdefs:
        return (f"(no type-checked Go definition named {symbol}; use defs or refs)", False)
    ids = [int(d["id"]) for d in gdefs]
    calls = sorted({(str(r["path"]), int(r["line"]), str(r.get("enclosing") or ""))
                    for r in facts.refs_to(ids)
                    if r.get("call") and (include_tests or not r.get("test")) and (in_scope is None or in_scope(str(r["path"])))})
    shown = calls[:max_lines]
    texts = line_texts([(path, line) for path, line, _enclosing in shown])
    out = [CALLERS_HEADER.format(symbol=symbol, go=facts.go)]
    if len(gdefs) > 1:
        out.append(f"matched {len(gdefs)} definitions:")
        out += [f"  {d['path']}:{d['start']}-{d['end']} {d['qualified']} (package {d['pkg']})" for d in gdefs[:10]]
    if not shown:
        out.append("(no calls found)" if include_tests else "(no calls found outside tests; set include_tests to see tests)")
    for path, line, enclosing in shown:
        out.append(f"{path}:{line}: {enclosing or '(package level)'}  "
                   f"{_clip(texts.get((path, line), '').strip(), max_chars)}")
    truncated = len(calls) > max_lines
    if truncated:
        out.append("[... more callers; narrow with a glob ...]")
    note = coverage_note({"unresolved": [str(u["path"]) for u in facts.unresolved_named(identifier, in_scope)]})
    if note:
        out.append(note)
    return "\n".join(out), truncated


def callees_text(facts: GoFacts, symbol: str, max_lines: int) -> Tuple[str, bool]:
    """What the body of a function or method calls: definition locations, or package.Name outside the module."""
    callable_defs = [d for d in facts.find_defs(symbol) if d["kind"] in ("function", "method")]
    if not callable_defs:
        known = bool(facts.find_defs(symbol))
        return ((f"({symbol} is not a function or method)" if known
                 else f"(no type-checked Go function named {symbol}; use defs or refs)"), False)
    out = [CALLEES_HEADER.format(symbol=symbol, go=facts.go)]
    count = 0
    truncated = False
    for d in callable_defs:
        if len(callable_defs) > 1:
            out.append(f"--- {d['qualified']} ({d['path']}:{d['start']}-{d['end']}, package {d['pkg']}) ---")
        for callee, lines in facts.callees(d):
            if count >= max_lines:
                truncated = True
                break
            where = (f"{callee['pkg']}.{callee['qualified']}" if callee.get("external")
                     else f"{callee['path']}:{callee['start']}  {callee['qualified']}")
            out.append(f"{where}  (called at line{'s' if len(lines) > 1 else ''} {', '.join(map(str, lines[:8]))})")
            count += 1
        if truncated:
            break
    if count == 0:
        out.append("(no calls found)")
    if truncated:
        out.append("[... more callees ...]")
    return "\n".join(out), truncated

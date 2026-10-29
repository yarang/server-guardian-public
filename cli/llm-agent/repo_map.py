"""
Repository map for debate agents (docs/design/debate-code-index-wiki.md, section 2a).

A deterministic, text-only overview built from the symbol index of one commit: for every directory its files with
their line count and the declarations most referenced from other files. It is a guide to where to read, never
evidence. The same sha and INDEX_VERSION give byte-identical text (all ties are broken by path, then name).

Layout (directories sorted by path, root first as `./`; files sorted by path):

    --- Repository map (sha 98e0be7, 479 files, ranked by references) ---
    cli/llm-agent/
      debate_engine.py  [1702 lines] DebateEngine, DebateEngine.run, …
      (+3 non-code files)
    docs/design/  (32 files)
    [folded: ...]

Precise rules:

- Code file: a readable path whose extension has a parser (code_index.language: Go, Python, TS/JS). It is always
  listed as `name  [N lines]` followed by up to 6 of its declarations (qualified names), ranked by reverse-reference
  count, then by qualified name; `…` is added when more declarations exist than are shown. A code file without
  declarations shows no symbols; a file over the index size limit shows `[large file, not indexed]`.
- Reverse-reference count of a declaration: the number of DISTINCT files, other than the declaring one, in which
  its identifier (the last component of the qualified name) occurs. It is name based, like `refs`, except for Go
  declarations that the compiler-assisted facts know (go_facts, P2.6; only when they are ready, the map never waits
  longer than GUARDIAN_GOINDEX_MAP_WAIT): those count the distinct files that really reference the declaration, and
  a line below the header says which ranking the Go files got. Files that
  are noise for ranking (is_noise_path: stories, previews, `.design-sync/`, `__tests__/`, test files) do not
  count as referencing files, and identifiers of at most MIN_RANKED_NAME characters (`i`, `ok`) are never
  ranked, nor are declarations of the noise files themselves (a preview constant named `nodes` is not an important
  symbol because other files use the word). A declaration with count 0 is unranked: it is listed after the ranked ones, alphabetically.
- Non-code file: any other readable path (docs, images, JSON, ...). They are never listed one by one. In a directory
  that also has code files the line `  (+N non-code files)` counts them; a directory with only non-code files is
  the single line `dir/  (N files)`. Counts cover the files directly in that directory.
- Size cap (build_map cap): first symbols are dropped, starting with the files whose best declaration has the
  fewest references (the file keeps its path and line count and shows `…`); when even that is too big, the deepest
  directories are folded into `dir/  (N files, folded)` lines (N counts everything below), one level at a time,
  re-granting symbols to the best-ranked files whenever the folded layout leaves room. Every readable file is
  therefore either listed or counted in a folded line. When anything was folded or dropped a final `[...]` line
  says the map op expands it. When files of the requested subtree failed to parse or were over the index size
  limit, a `[index coverage: ...]` line follows the header (see code_index.coverage_note). The smallest layout (one count line) is returned even if it exceeds a tiny cap.
- Depth (map op): directory level 0 is the requested path itself; `depth` D shows levels 0..D-1 and folds the
  directories of level D, with everything below them, into count lines.
"""

import fnmatch
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from code_index import Decl, coverage_note, language

MAP_VERSION = 3  # bump when the map layout or ranking changes (recorded in debate_started and the snapshot)
DEFAULT_MAP_BYTES = 32 * 1024
MIN_RANKED_NAME = 2  # identifiers up to this length are too common to rank
NOISE_DIRS = frozenset({".design-sync", "previews", "__tests__"})
NOISE_FILES = ("*.stories.*", "*_test.go", "test_*.py", "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx")
MAX_SYMBOLS_PER_FILE = 6
ROOT_SUMMARY_SYMBOLS = 3
ELLIPSIS = "…"
FOLD_HINT = ('[folded or shortened to fit: expand a directory with the map op, e.g. '
             '{"op": "map", "path": "<dir>", "depth": 2}]')


def repo_map_enabled() -> bool:
    """GUARDIAN_REPO_MAP=0 (also false/off/no) restores the plain file list in the first prompt."""
    return os.environ.get("GUARDIAN_REPO_MAP", "1").strip().lower() not in ("0", "false", "off", "no")


def repo_map_cap() -> int:
    """GUARDIAN_REPO_MAP_BYTES, default 32768; empty, 0, negative or non-numeric values mean the default.

    This is a prompt-size rule for the first prompt only; it is separate from the (unlimited by default)
    GUARDIAN_RETRIEVAL_* budgets that apply to what an agent requests.
    """
    try:
        value = int(os.environ.get("GUARDIAN_REPO_MAP_BYTES", "").strip())
    except ValueError:
        return DEFAULT_MAP_BYTES
    return value if value > 0 else DEFAULT_MAP_BYTES


def is_noise_path(path: str) -> bool:
    """Whether references from this file say little about importance: stories, previews, design-sync output, tests."""
    *directories, name = path.split("/")
    return any(d in NOISE_DIRS for d in directories) or any(fnmatch.fnmatchcase(name, p) for p in NOISE_FILES)


class _FileInfo:
    """One readable file: where it is, its line count and its declarations ranked best first."""

    __slots__ = ("path", "directory", "name", "code", "lines", "ranked", "best")

    def __init__(self, path: str, code: bool, lines: Optional[int], ranked: List[Tuple[int, str]]) -> None:
        self.path = path
        self.directory, _, self.name = path.rpartition("/")
        self.code = code
        self.lines = lines
        self.ranked = ranked  # [(reference count, qualified name)], best first, unique names
        self.best = ranked[0][0] if ranked else -1


class _Model:
    """Files of one commit with the reference counts of their declarations (built once per call)."""

    def __init__(self, tools: Any) -> None:
        self.sha: str = tools.sha_label
        index = tools.code_index()
        self.index = index
        # Compiler-assisted facts (P2.6), when the Go index is ready: Go declarations then rank by type-checked
        # references instead of by name. go_note says which ranking the Go files got ("" without a Go module).
        self.facts = getattr(tools, "go_map_facts", lambda: None)()
        self.go_note = ""
        if self.facts is not None:
            self.go_note = f"[Go files ranked by type-checked references ({self.facts.go})]"
        else:
            self.go_note = getattr(tools, "go_note", lambda: "")()
        # identifier -> files outside the noise paths in which it occurs
        referencing: Dict[str, Set[str]] = {}
        for decl in index.decls:
            ident = decl["name"]
            if ident not in referencing:
                referencing[ident] = {p for p in index.refs.get(ident, {}) if not is_noise_path(p)}
        by_path: Dict[str, List[Decl]] = {}
        for decl in index.decls:
            by_path.setdefault(decl["path"], []).append(decl)
        self.files: List[_FileInfo] = []
        for path in tools.readable_paths():
            if language(path) is None:
                self.files.append(_FileInfo(path, False, None, []))
                continue
            best: Dict[str, int] = {}
            noise = is_noise_path(path)
            typed = self.facts is not None and path.endswith(".go") and self.facts.covers(path)
            for decl in by_path.get(path, []):
                ident = decl["name"]
                if typed:
                    count = 0 if noise else self._typed_count(path, decl)
                else:
                    count = 0 if noise or len(ident) <= MIN_RANKED_NAME else len(referencing[ident] - {path})
                best[decl["qualified"]] = max(count, best.get(decl["qualified"], 0))
            ranked = sorted(((count, name) for name, count in best.items()), key=lambda item: (-item[0], item[1]))
            self.files.append(_FileInfo(path, True, index.lines.get(path), ranked))
        self.symbols = len(index.decls)
        self.indexed_files = int(index.stats["files"])

    def _typed_count(self, path: str, decl: Decl) -> int:
        """Distinct files outside the noise paths (and not the declaring one) that really reference the declaration."""
        assert self.facts is not None
        definition = self.facts.def_at(path, int(decl["start"]), str(decl["qualified"]))
        if definition is None:
            return 0
        return len({p for p in self.facts.referencing_files(int(definition["id"])) if not is_noise_path(p)} - {path})


def _file_line(info: _FileInfo, with_symbols: bool) -> str:
    size = "[large file, not indexed]" if info.lines is None else f"[{info.lines} lines]"
    line = f"  {info.name}  {size}"
    if not info.ranked:
        return line
    shown = info.ranked[:MAX_SYMBOLS_PER_FILE] if with_symbols else []
    names = [name for _count, name in shown]
    if len(shown) < len(info.ranked):
        names.append(ELLIPSIS)
    return f"{line} {', '.join(names)}"


def _plan(model: _Model, prefix: str, level: Optional[int], header: str) -> Tuple[List[Any], bool]:
    """Items of the layout in output order: str (a fixed line) or _FileInfo (a code file line).

    level is the deepest directory level shown in detail (None: all); directories below it fold into count lines
    placed at their own position. Returns (items, anything folded).
    """
    files = [f for f in model.files if f.path.startswith(prefix)]
    items: List[Any] = [header]
    note = coverage_note(model.index.coverage(lambda path: path.startswith(prefix)))
    if note:
        items.append(note)
    if model.go_note and any(f.path.endswith(".go") for f in files):
        items.append(model.go_note)
    if level is not None and level < 0:
        items.append(f"(all {len(files)} files, folded)")
        return items, True
    detail: Dict[str, List[_FileInfo]] = {}
    folded: Dict[str, int] = {}
    for info in files:
        rel = info.path[len(prefix):]
        parts = rel.split("/")
        depth = len(parts) - 1  # directory level of the file relative to the requested path
        if level is None or depth <= level:
            detail.setdefault(info.directory, []).append(info)
        else:
            key = prefix + "/".join(parts[:level + 1]) + "/"
            folded[key] = folded.get(key, 0) + 1
    blocks: List[Tuple[str, List[Any]]] = []  # (sort key: the directory path, "" for the root; output lines)
    for directory, members in detail.items():
        code = sorted((m for m in members if m.code), key=lambda m: m.path)
        other = len(members) - len(code)
        label = f"{directory}/" if directory else "./"
        if not code:
            blocks.append((directory, [f"{label}  ({other} files)"]))
            continue
        lines: List[Any] = [label, *code]
        if other:
            lines.append(f"  (+{other} non-code files)")
        blocks.append((directory, lines))
    for directory, count in folded.items():
        blocks.append((directory.rstrip("/"), [f"{directory}  ({count} files, folded)"]))
    blocks.sort(key=lambda block: block[0])
    for _key, lines in blocks:
        items.extend(lines)
    return items, bool(folded)


def _fit(items: List[Any], folded: bool, cap: int) -> Optional[int]:
    """The largest number K of ranked code files that may keep their symbols so the text fits cap, or None.

    Files are ranked by their best declaration's reference count (path breaks ties); the others show `…` only.
    """
    ranked = sorted((f for f in items if isinstance(f, _FileInfo) and f.ranked), key=lambda f: (-f.best, f.path))
    dropped_total = 0
    deltas: List[int] = []
    for item in items:
        if isinstance(item, _FileInfo):
            dropped_total += len(_file_line(item, False).encode("utf-8")) + 1
        else:
            dropped_total += len(item.encode("utf-8")) + 1
    for info in ranked:
        deltas.append(len(_file_line(info, True).encode("utf-8")) - len(_file_line(info, False).encode("utf-8")))
    hint = len(FOLD_HINT.encode("utf-8")) + 1
    base = dropped_total - 1  # lines are joined by "\n"
    best: Optional[int] = None
    running = base
    for k in range(len(ranked) + 1):
        if k:
            running += deltas[k - 1]
        shortened = folded or k < len(ranked)
        if running + (hint if shortened else 0) <= cap:
            best = k
    return best


def _render(items: List[Any], folded: bool, keep: Optional[int]) -> str:
    """Text of the layout; the best `keep` ranked files keep their symbols (None: all files do)."""
    ranked = sorted((f for f in items if isinstance(f, _FileInfo) and f.ranked), key=lambda f: (-f.best, f.path))
    kept: Set[str] = {f.path for f in (ranked if keep is None else ranked[:keep])}
    shortened = folded or (keep is not None and keep < len(ranked))
    lines = [item if isinstance(item, str) else _file_line(item, item.path in kept) for item in items]
    if shortened:
        lines.append(FOLD_HINT)
    return "\n".join(lines)


def _header(model: _Model, prefix: str, count: int) -> str:
    scope = f" of {prefix.rstrip('/')}" if prefix else ""
    return f"--- Repository map{scope} (sha {model.sha[:7]}, {count} files, ranked by references) ---"


# @MX:ANCHOR: The single generator behind the first prompt, the map op and the tests.
# @MX:REASON: Determinism (same sha + INDEX_VERSION -> same bytes) and the "every file is listed or counted" rule
#   must hold for all callers.
def build_map(tools: Any, prefix: str = "", depth: Optional[int] = None, cap: Optional[int] = None) -> str:
    """The repository map of the subtree prefix ("" for the root, otherwise `dir/` with a trailing slash).

    depth (None: unlimited) limits directory nesting as described in the module docstring; cap (None: none) is
    the byte limit. tools is a RepoTools (sha, readable_paths(), code_index()).
    """
    model = _Model(tools)
    count = sum(1 for f in model.files if f.path.startswith(prefix))
    header = _header(model, prefix, count)
    top: Optional[int] = None if depth is None else max(depth, 1) - 1
    items, folded = _plan(model, prefix, top, header)
    if cap is None:
        return _render(items, folded, None)
    deepest = max((len(f.path[len(prefix):].split("/")) - 1 for f in model.files if f.path.startswith(prefix)),
                  default=0)
    level = deepest if top is None else min(top, deepest)
    while level >= -1:
        items, folded = _plan(model, prefix, level, header)
        keep = _fit(items, folded, cap)
        if keep is not None:
            return _render(items, folded, keep)
        level -= 1
    items, folded = _plan(model, prefix, -1, header)
    return _render(items, folded, 0)


def root_summary(tools: Any) -> str:
    """One line per top-level directory: file count and its 3 most referenced declarations.

    Used in the first prompt instead of the file list when the debate already has code context paths. Files
    directly in the root are the line `./`. Ties are broken by name, so the text is deterministic.
    """
    model = _Model(tools)
    groups: Dict[str, List[_FileInfo]] = {}
    for info in model.files:
        top, sep, _rest = info.path.partition("/")
        groups.setdefault(f"{top}/" if sep else "./", []).append(info)
    lines = [f"--- Repository summary (sha {model.sha[:7]}, {len(model.files)} files; expand a directory with the "
             "map op) ---"]
    for name in sorted(groups):
        members = groups[name]
        ranked: Dict[str, int] = {}
        for info in members:
            for count, qualified in info.ranked:
                ranked[qualified] = max(count, ranked.get(qualified, -1))
        top_symbols = [q for q, _c in sorted(ranked.items(), key=lambda item: (-item[1], item[0]))][
            :ROOT_SUMMARY_SYMBOLS]
        line = f"{name}  {len(members)} files"
        if top_symbols:
            line += "; top symbols: " + ", ".join(top_symbols)
        lines.append(line)
    return "\n".join(lines)

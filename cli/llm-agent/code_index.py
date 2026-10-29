"""
Symbol index for debate agents (docs/design/debate-code-index-wiki.md, layer 1).

One parser per language turns a file into declarations and identifier occurrences:

- declaration: {"name", "qualified", "kind", "path", "start", "end", "signature"}; `start` is the line of the
  declaration keyword (leading comments are not part of it) and `signature` is that line cut to 200 characters.
- identifier occurrences: {identifier: [line, ...]} with ascending unique lines.

Python is parsed with `ast`. Go and TypeScript/JavaScript are parsed by layout, like code_select: gofmt/prettier
formatted code is assumed, declarations start at column 0 and nested declarations are not seen. References are
by NAME only: there is no type information, so a symbol with the same name elsewhere shares its occurrences.

TypeScript/JavaScript layout rules (.ts .tsx .js .jsx .mjs .cjs):

- A declaration starts at a column-0 line: optional `export`, `default`, `declare`, `abstract`, `async`, then
  function / class / interface / type / enum / const enum / namespace / const / let / var and a name.
  `export default function () {` and `export default class {` are recorded under the name `default`.
- It ends at the first of (scanning the following lines): a column-0 line that starts with `}`, `)` or `]` and
  does not itself open a new block (it does not end with `{`, `(`, `[` or `=>`, so `): Foo {` and `} else {` are
  signature or body continuations); or the last non-blank line before the next column-0 line of any other kind
  (the next declaration, an import/export statement, a comment). Indented lines and blank lines belong to the
  declaration. One-line declarations end where they start.
- Not seen: class members, declarations inside functions, decorators on their own line, CommonJS exports.

Identifier scanning strips comments, string literals and template literal text (the `${...}` expressions of a
template are scanned); JSX tag names (`<Name`) are plain identifier tokens and therefore included. Known limits:
regex literals containing quotes or `//`, and apostrophes in JSX text, can hide the rest of that line.

Per-blob results are cached as JSON (declarations, identifier occurrences and the line count) under
`<cache dir>/blobs/<id[:2]>/<id>.json`, keyed by the git blob id, so a new commit only reparses changed files.
Of the source only the first line of each declaration is stored, as `signature` cut to 200 characters, and
never bodies. Before the line is cut, the contents of string literals in it (Python quotes, triple quotes and
r/b/f/u prefixes; Go double quotes, runes and raw backticks; TypeScript quotes and backticks) are replaced by
`"…"`, so `API_KEY = "abc"` is cached as `API_KEY = "…"`. This keeps the shape of the declaration but not a
literal value; the redaction is by layout of that one line (an unterminated literal is redacted to the end of
the line) and comments on the line are kept. defs and outline of TypeScript declarations are the only readers of
the signature: the TypeScript outline used for code context packs is built from the raw line, and the Go and Python
outlines never use the cache. A cache file with another INDEX_VERSION (older ones kept literals and had no "lines"
field) is invalid and is reparsed.
The cache is optional: a missing or unwritable directory falls back to memory for that run. Cache files are
touched on every hit and pruned by age and total size (see prune_cache).
"""

import ast
import json
import os
import re
import tempfile
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from code_select import _go_decl_span, language

# Bump when a parser or the stored shape changes: cache files with another version are ignored.
INDEX_VERSION = 2
MAX_INDEXED_BYTES = 1024 * 1024  # larger files are treated as generated and are not indexed
MAX_SIGNATURE_CHARS = 200
MAX_LINES_PER_IDENTIFIER = 5000
BLOB_BATCH = 100

Decl = Dict[str, Any]
Refs = Dict[str, List[int]]
FileIndex = Dict[str, Any]  # {"decls": [...], "refs": {...}, "error": bool, "lines": int}
BlobReader = Callable[[List[str]], Dict[str, bytes]]

_BLOB_ID = re.compile(r"^[0-9a-f]{40,64}$")
_PROJECT_ID = re.compile(r"[^A-Za-z0-9._-]")


# A string literal on one line: optional Python prefix, then triple quotes, double quotes, single quotes or a
# backtick string. Each form may be unterminated (it then runs to the end of the line, the safe side). A prefixed
# form needs a non-word character before it; if that fails the quote itself is still matched without the prefix.
_STRING_LITERAL = re.compile(
    r"""(?:(?<![A-Za-z0-9_])[rRbBuUfF]{1,2})?
        (?: \"\"\".*?(?:\"\"\"|$)
          | \'\'\'.*?(?:\'\'\'|$)
          | "(?:[^"\\]|\\.|\\$)*(?:"|$)
          | '(?:[^'\\]|\\.|\\$)*(?:'|$)
          | `[^`]*(?:`|$) )""",
    re.VERBOSE)
REDACTED = '"…"'


def redact_literals(line: str) -> str:
    """The line with the contents of its string literals (prefix and quotes included) replaced by "…"."""
    return _STRING_LITERAL.sub(REDACTED, line)


def _decl(name: str, qualified: str, kind: str, start: int, end: int, line: str, redact: bool = True) -> Decl:
    text = line.strip()
    return {"name": name, "qualified": qualified, "kind": kind, "start": start, "end": end,
            "signature": (redact_literals(text) if redact else text)[:MAX_SIGNATURE_CHARS]}


def _add_ref(refs: Refs, name: str, line: int) -> None:
    lines = refs.setdefault(name, [])
    if (not lines or lines[-1] != line) and len(lines) < MAX_LINES_PER_IDENTIFIER:
        lines.append(line)


# --- Python -----------------------------------------------------------------------------------

def parse_python(source: str) -> Tuple[List[Decl], Refs]:
    """Declarations and identifier occurrences of a Python file. Raises SyntaxError/ValueError on bad input."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(source)
    lines = source.splitlines()
    decls: List[Decl] = []

    def line_text(number: int) -> str:
        return lines[number - 1] if 0 < number <= len(lines) else ""

    def walk_class(node: ast.ClassDef, prefix: str) -> None:
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decls.append(_decl(child.name, f"{prefix}.{child.name}", "method", child.lineno,
                                   child.end_lineno or child.lineno, line_text(child.lineno)))
            elif isinstance(child, ast.ClassDef):
                qualified = f"{prefix}.{child.name}"
                decls.append(_decl(child.name, qualified, "class", child.lineno,
                                   child.end_lineno or child.lineno, line_text(child.lineno)))
                walk_class(child, qualified)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decls.append(_decl(node.name, node.name, "function", node.lineno, node.end_lineno or node.lineno,
                               line_text(node.lineno)))
        elif isinstance(node, ast.ClassDef):
            decls.append(_decl(node.name, node.name, "class", node.lineno, node.end_lineno or node.lineno,
                               line_text(node.lineno)))
            walk_class(node, node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    decls.append(_decl(target.id, target.id, "const" if target.id.isupper() else "variable",
                                       node.lineno, node.end_lineno or node.lineno, line_text(node.lineno)))

    refs: Refs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            _add_ref(refs, node.id, node.lineno)
        elif isinstance(node, ast.Attribute):
            _add_ref(refs, node.attr, node.end_lineno or node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _add_ref(refs, node.name, node.lineno)
        elif isinstance(node, ast.alias):
            _add_ref(refs, node.name.rpartition(".")[2], getattr(node, "lineno", 0) or 1)
    for lines_of in refs.values():
        lines_of.sort()
    return decls, refs


# --- identifier scanner shared by Go and TypeScript ------------------------------------------

_GO_KEYWORDS = frozenset(
    "break case chan const continue default defer else fallthrough for func go goto if import interface map "
    "package range return select struct switch type var".split())
_TS_KEYWORDS = frozenset(
    "break case catch class const continue debugger default delete do else enum export extends false finally for "
    "function if import in instanceof new null return super switch this throw true try typeof var void while with "
    "yield".split())

_GO_TOKEN = re.compile(
    r"""(?P<nl>\n)
      | (?P<skip>//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?|`[^`]*`?)
      | (?P<num>[0-9][A-Za-z0-9_.]*)
      | (?P<id>[A-Za-z_][A-Za-z0-9_]*)""",
    re.VERBOSE | re.DOTALL)
_TS_TOKEN = re.compile(
    r"""(?P<nl>\n)
      | (?P<skip>//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?)
      | (?P<tpl>`)
      | (?P<br>[{}])
      | (?P<num>[0-9][A-Za-z0-9_.$]*)
      | (?P<id>[A-Za-z_$][A-Za-z0-9_$]*)""",
    re.VERBOSE | re.DOTALL)
# Template literal text up to the closing backtick or the next `${`.
_TPL_BODY = re.compile(r"(?:[^`\\$]|\\.|\$(?!\{))*(`|\$\{)?", re.DOTALL)


def scan_identifiers(source: str, ts: bool) -> Refs:
    """Identifier tokens of Go (ts=False) or TypeScript/JavaScript (ts=True) with their 1-based lines."""
    token = _TS_TOKEN if ts else _GO_TOKEN
    keywords = _TS_KEYWORDS if ts else _GO_KEYWORDS
    refs: Refs = {}
    line, pos, depth, size = 1, 0, 0, len(source)
    templates: List[int] = []  # brace depth at which each open `${` expression started

    def template_text(start: int) -> int:
        """Consumes template text from start; returns the position after it."""
        nonlocal line
        body = _TPL_BODY.match(source, start)
        assert body is not None  # the pattern can match the empty string
        line += source.count("\n", start, body.end())
        if body.group(1) == "${":
            templates.append(depth)
        return body.end()

    while pos < size:
        found = token.search(source, pos)
        if found is None:
            break
        kind = found.lastgroup
        pos = found.end()
        if kind == "nl":
            line += 1
        elif kind == "skip":
            line += found.group().count("\n")
        elif kind == "id":
            name = found.group()
            if name not in keywords:
                _add_ref(refs, name, line)
        elif kind == "tpl":
            pos = template_text(pos)
        elif kind == "br":
            if found.group() == "{":
                depth += 1
            elif templates and templates[-1] == depth:
                templates.pop()
                pos = template_text(pos)  # back in the template text after `}`
            else:
                depth = max(depth - 1, 0)
    return refs


# --- Go ---------------------------------------------------------------------------------------

_GO_FUNC = re.compile(r"^func\s+([A-Za-z_]\w*)")
_GO_METHOD = re.compile(
    r"^func\s*\(\s*(?:[A-Za-z_]\w*\s+)?\*?\s*([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*([A-Za-z_]\w*)")
_GO_TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)\s*(\w*)")
_GO_VALUE = re.compile(r"^(var|const)\s+([A-Za-z_]\w*)")
_GO_BLOCK = re.compile(r"^(var|const|type)\s*\(\s*$")
_GO_BLOCK_ENTRY = re.compile(r"^\t([A-Za-z_]\w*)(?:\s+(\w+))?")


def parse_go(source: str) -> Tuple[List[Decl], Refs]:
    """Declarations (gofmt layout, see code_select) and identifier occurrences of a Go file."""
    lines = source.splitlines()
    decls: List[Decl] = []
    for i, line in enumerate(lines):
        if not line or line[0] in " \t/":
            continue
        number = i + 1
        method = _GO_METHOD.match(line)
        if method:
            receiver, name = method.groups()
            decls.append(_decl(name, f"{receiver}.{name}", "method", number, _go_decl_span(lines, i)[1], line))
            continue
        func = _GO_FUNC.match(line)
        if func:
            decls.append(_decl(func.group(1), func.group(1), "function", number, _go_decl_span(lines, i)[1], line))
            continue
        block = _GO_BLOCK.match(line)
        if block:
            end = _go_decl_span(lines, i)[1]
            for j in range(i + 1, min(end - 1, len(lines))):
                entry = _GO_BLOCK_ENTRY.match(lines[j])
                if entry and entry.group(1) != "_":
                    kind = block.group(1)
                    if kind == "type":
                        kind = entry.group(2) if entry.group(2) in ("struct", "interface") else "type"
                    decls.append(_decl(entry.group(1), entry.group(1), kind, j + 1, _go_decl_span(lines, j)[1],
                                       lines[j]))
            continue
        declared = _GO_TYPE.match(line)
        if declared:
            kind = declared.group(2) if declared.group(2) in ("struct", "interface") else "type"
            decls.append(_decl(declared.group(1), declared.group(1), kind, number, _go_decl_span(lines, i)[1], line))
            continue
        value = _GO_VALUE.match(line)
        if value:
            decls.append(_decl(value.group(2), value.group(2), value.group(1), number, _go_decl_span(lines, i)[1], line))
    return decls, scan_identifiers(source, ts=False)


# --- TypeScript / JavaScript ------------------------------------------------------------------

_TS_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?P<kind>function|class|interface|type|enum|const\s+enum|namespace|const|let|var)"
    r"(?:\s*\*\s*|\s+)(?P<name>[A-Za-z_$][\w$]*)")
_TS_ANON_DEFAULT = re.compile(r"^export\s+default\s+(?:async\s+)?(function|class)\b")
_TS_CONTINUES = re.compile(r"(?:[{(\[]|=>)$")
_TS_IMPORT = re.compile(r"^import\b")
_TS_FROM = re.compile(r"""\bfrom\s*['"]""")


def _ts_end(lines: List[str], start: int) -> int:
    """0-based index of the last line of the declaration that begins at index start (see the module docstring)."""
    last = start
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        if line[0] in " \t":
            last = j
            continue
        if line[0] in "})]":
            last = j
            if _TS_CONTINUES.search(line.rstrip()):
                continue
            return j
        return last
    return last


def parse_ts_declarations(source: str, redact: bool = True) -> List[Decl]:
    """Column-0 declarations of a TypeScript/JavaScript file (redact=False keeps string literals in signatures)."""
    lines = source.splitlines()
    decls: List[Decl] = []
    for i, line in enumerate(lines):
        if not line or line[0] in " \t/*":
            continue
        found = _TS_DECL.match(line)
        name = kind = None
        if found:
            kind, name = found.group("kind"), found.group("name")
            if kind == "type" and not re.match(r"\s*[<=]", line[found.end():]):
                continue
            if kind == "class" and name in ("extends", "implements"):
                name = "default"
            kind = "enum" if kind.startswith("const") and kind.endswith("enum") else kind
        else:
            anon = _TS_ANON_DEFAULT.match(line)
            if anon:
                kind, name = anon.group(1), "default"
        if name is None or kind is None:
            continue
        decls.append(_decl(name, name, kind, i + 1, _ts_end(lines, i) + 1, line, redact))
    return decls


def parse_ts(source: str) -> Tuple[List[Decl], Refs]:
    """Declarations and identifier occurrences (JSX tag names included) of a TypeScript/JavaScript file."""
    return parse_ts_declarations(source), scan_identifiers(source, ts=True)


def ts_symbol_spans(source: str, symbol: str) -> List[Tuple[int, int]]:
    """Line spans of the top-level declarations called symbol (members such as Type.method are not indexed)."""
    return [(d["start"], d["end"]) for d in parse_ts_declarations(source) if d["qualified"] == symbol]


def ts_outline(source: str) -> List[str]:
    """'N: first line' of every top-level declaration, the layout of the Python outline.

    Built from the raw line (literals kept): the outline is shown from the file just read, never from the cache.
    """
    return [f"{d['start']}: {d['signature']}" for d in parse_ts_declarations(source, redact=False)]


def ts_import_spans(source: str) -> List[Tuple[int, int]]:
    """Column-0 import statements, which may span several lines (up to the line holding `from '...'`)."""
    lines = source.splitlines()
    spans: List[Tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not _TS_IMPORT.match(line):
            continue
        end = i
        if not _TS_FROM.search(line) and not re.match(r"""^import\s*['"]""", line):
            for j in range(i + 1, min(i + 200, len(lines))):
                if _TS_FROM.search(lines[j]):
                    end = j
                    break
            else:
                continue  # not a recognisable import statement
        spans.append((i + 1, end + 1))
    return spans


# --- dispatch ---------------------------------------------------------------------------------

def parse_file(path: str, source: str) -> Optional[FileIndex]:
    """{"decls", "refs", "error", "lines"} for a source file, or None for a language without a parser.

    A file the parser rejects yields error=True and no declarations or references (its line count stays).
    """
    lang = language(path)
    parser = {"go": parse_go, "python": parse_python, "ts": parse_ts}.get(lang or "")
    if parser is None:
        return None
    lines = len(source.splitlines())
    try:
        decls, refs = parser(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {"decls": [], "refs": {}, "error": True, "lines": lines}
    return {"decls": decls, "refs": refs, "error": False, "lines": lines}


# --- cache ------------------------------------------------------------------------------------

def default_cache_dir(project_id: Optional[str]) -> Optional[Path]:
    """<GUARDIAN_DATA_DIR>/code-index/<project>, or None when the project id is unusable.

    GUARDIAN_DATA_DIR is resolved as in db_manager.data_dir (default ~/.guardian/data) but nothing is created
    here: directories appear only when the first cache file is written.
    """
    cleaned = _PROJECT_ID.sub("_", str(project_id or "")).strip(".")
    if not cleaned:
        return None
    root = Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()
    return root / "code-index" / cleaned[:80]


_PRUNE_LOCK = threading.Lock()
_PRUNED: set = set()  # cache directories already pruned by this process
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_MB = 256


def _env_number(name: str, default: float) -> float:
    """A non-negative number from the environment; empty or invalid values mean the default (0 disables)."""
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def prune_cache(cache_dir: Path, now: Optional[float] = None) -> int:
    """Deletes blob cache files of one project directory; returns how many were removed.

    First every file written by another INDEX_VERSION (it can never be used again, and files from before version 2
    may hold unredacted signature text) and every file whose mtime is older than GUARDIAN_CODE_INDEX_MAX_AGE_DAYS
    (default 30; 0 disables), then,
    when the rest is still larger than GUARDIAN_CODE_INDEX_MAX_MB (default 256; 0 disables), the oldest files
    until it fits. mtime is the last write or cache hit (see CodeIndex._load). Only `blobs/*/*.json` and stale
    `.tmp-*` files below `blobs/` are touched; empty shard directories are removed.
    """
    blobs = Path(cache_dir) / "blobs"
    if not blobs.is_dir():
        return 0
    moment = time.time() if now is None else now
    max_age = _env_number("GUARDIAN_CODE_INDEX_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS) * 86400
    max_bytes = _env_number("GUARDIAN_CODE_INDEX_MAX_MB", DEFAULT_MAX_MB) * 1024 * 1024
    files: List[Tuple[float, int, Path]] = []
    removed = 0
    for path in blobs.glob("*/*"):
        if not (path.name.endswith(".json") or path.name.startswith(".tmp-")):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        # A temp file is only garbage when an interrupted write left it behind (an hour old).
        stale_temp = path.name.startswith(".tmp-") and moment - info.st_mtime > 3600
        if stale_temp or (max_age and path.name.endswith(".json") and moment - info.st_mtime > max_age) or (
                path.name.endswith(".json") and _cached_version(path) != INDEX_VERSION):
            removed += _unlink(path)
        elif path.name.endswith(".json"):
            files.append((info.st_mtime, info.st_size, path))
    if max_bytes:
        total = sum(size for _mtime, size, _path in files)
        for _mtime, size, path in sorted(files, key=lambda item: (item[0], str(item[2]))):
            if total <= max_bytes:
                break
            if _unlink(path):
                removed += 1
                total -= size
    for shard in blobs.iterdir():
        try:
            shard.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed


def _cached_version(path: Path) -> Any:
    """The "version" field of a blob cache file, or None when it cannot be read or decoded."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("version") if isinstance(data, dict) else None


def _unlink(path: Path) -> int:
    try:
        path.unlink()
        return 1
    except OSError:
        return 0


def _valid_cached(data: Any, lang: str) -> bool:
    """Whether a decoded cache file has the current version, the right language and the expected shape."""
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or data.get("lang") != lang:
        return False
    decls, refs = data.get("decls"), data.get("refs")
    lines = data.get("lines")
    if (not isinstance(decls, list) or not isinstance(refs, dict) or not isinstance(data.get("error"), bool)
            or isinstance(lines, bool) or not isinstance(lines, int)):
        return False
    for decl in decls:
        if not (isinstance(decl, dict) and all(isinstance(decl.get(k), str) for k in ("name", "qualified", "kind", "signature"))
                and all(isinstance(decl.get(k), int) for k in ("start", "end"))):
            return False
    return all(isinstance(k, str) and isinstance(v, list) and all(isinstance(n, int) for n in v)
               for k, v in refs.items())


COVERAGE_PREFIX = "[index coverage: "
COVERAGE_PATHS = 10


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def coverage_note(coverage: Dict[str, List[str]]) -> str:
    """The one-line footer for the gaps of a defs/refs/map answer, or "" when there are none.

    coverage is CodeIndex.coverage(): files that failed to parse, files over the size limit and files where the
    identifier hit the occurrence cap. Counts that are zero are left out; up to COVERAGE_PATHS affected paths
    are named. It starts with COVERAGE_PREFIX, which repo_tools.provided_paths and mentioned_in rely on.
    An optional "unresolved" list (P2.6, one path per location the Go type checker could not resolve) adds
    "N locations unresolved due to type errors".
    """
    parts: List[str] = []
    errors, large, capped = coverage.get("errors", []), coverage.get("large", []), coverage.get("capped", [])
    unresolved = coverage.get("unresolved", [])
    if errors:
        parts.append(f"{_count(len(errors), 'file')} failed to parse")
    if large:
        parts.append(f"{_count(len(large), 'file')} over {MAX_INDEXED_BYTES // (1024 * 1024)} MB not indexed")
    if capped:
        parts.append(f"{_count(len(capped), 'file')} with this identifier capped at {MAX_LINES_PER_IDENTIFIER} "
                     "occurrences")
    if unresolved:
        parts.append(f"{_count(len(unresolved), 'location')} unresolved due to type errors")
    if not parts:
        return ""
    affected = sorted(dict.fromkeys([*errors, *large, *capped, *unresolved]))
    named = ", ".join(affected[:COVERAGE_PATHS])
    if len(affected) > COVERAGE_PATHS:
        named += f" (+{len(affected) - COVERAGE_PATHS} more)"
    return f"{COVERAGE_PREFIX}{', '.join(parts)} — results may be incomplete; use grep for these: {named}]"


def without_coverage_note(text: str) -> str:
    """The text without its index coverage footer line (its paths are not content the agent was shown)."""
    return "\n".join(line for line in text.split("\n") if not line.startswith(COVERAGE_PREFIX))


class CodeIndex:
    """Declarations and a reverse identifier map for the files of one commit, built lazily and once.

    files are (path, blob id, size) of the paths the caller allows (RepoTools.readable_paths); read_blobs
    returns the content of the blob ids it is given. Safe to share between threads: ensure() builds under a lock
    and the result is never mutated afterwards.
    """

    def __init__(self, files: Iterable[Tuple[str, str, int]], read_blobs: BlobReader,
                 cache_dir: Optional[Path] = None) -> None:
        self._files = list(files)
        self._read_blobs = read_blobs
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._lock = threading.Lock()
        self._built = False
        self._cache_writable = self.cache_dir is not None
        self.decls: List[Decl] = []
        self.refs: Dict[str, Dict[str, List[int]]] = {}  # identifier -> {path: [lines]}
        self.lines: Dict[str, int] = {}  # indexed path -> line count
        self.errors: List[str] = []  # paths whose parse failed
        self.skipped_large: List[str] = []
        self.stats = {"files": 0, "cache_hits": 0, "parsed": 0}

    # @MX:ANCHOR: Builds the per-commit aggregate that defs, refs and the tests read.
    # @MX:REASON: Called from every defs/refs request across threads; must build once and stay immutable after.
    def ensure(self) -> "CodeIndex":
        """Builds the index on first use (parsing only blobs that have no valid cache file)."""
        if self._built:
            return self
        with self._lock:
            if not self._built:
                self._build()
                self._built = True
        return self

    # --- queries ---------------------------------------------------------------------------

    def find_defs(self, symbol: str) -> List[Decl]:
        """Declarations named symbol; `Type.Method` matches by qualified name (or its trailing components)."""
        self.ensure()
        if "." in symbol:
            tail = "." + symbol
            found = [d for d in self.decls if d["qualified"] == symbol or d["qualified"].endswith(tail)]
        else:
            found = [d for d in self.decls if d["name"] == symbol]
        return found

    def find_refs(self, identifier: str) -> Dict[str, List[int]]:
        self.ensure()
        return self.refs.get(identifier, {})

    def coverage(self, match: Optional[Callable[[str], bool]] = None,
                 identifier: Optional[str] = None) -> Dict[str, List[str]]:
        """Files an answer cannot cover, limited to the paths match accepts (None: all).

        "errors": files that failed to parse; "large": files over MAX_INDEXED_BYTES that were not indexed;
        "capped" (only with an identifier): files where that identifier reached MAX_LINES_PER_IDENTIFIER lines, so
        later occurrences are missing. Each list is sorted.
        """
        self.ensure()
        wanted = match or (lambda _path: True)
        found = {"errors": sorted(p for p in self.errors if wanted(p)),
                 "large": sorted(p for p in self.skipped_large if wanted(p)),
                 "capped": []}
        if identifier is not None:
            found["capped"] = sorted(p for p, lines in self.refs.get(identifier, {}).items()
                                     if len(lines) >= MAX_LINES_PER_IDENTIFIER and wanted(p))
        return found

    # --- building --------------------------------------------------------------------------

    def _blob_file(self, blob_id: str) -> Optional[Path]:
        if self.cache_dir is None or not _BLOB_ID.match(blob_id):
            return None
        return self.cache_dir / "blobs" / blob_id[:2] / f"{blob_id}.json"

    def _load(self, blob_id: str, lang: str) -> Optional[FileIndex]:
        target = self._blob_file(blob_id)
        if target is None:
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not _valid_cached(data, lang):
            return None
        try:
            os.utime(target)  # a hit refreshes the mtime that prune_cache goes by
        except OSError:
            pass
        return data

    def _prune_once(self) -> None:
        """Prunes this project's cache on the first cache write of the process (see prune_cache)."""
        if self.cache_dir is None:
            return
        key = str(self.cache_dir)
        with _PRUNE_LOCK:
            if key in _PRUNED:
                return
            _PRUNED.add(key)
        try:
            prune_cache(self.cache_dir)
        except OSError:
            pass

    def _store(self, blob_id: str, lang: str, parsed: FileIndex) -> None:
        """Atomic write (temp file + rename); the first failure turns the cache off for this run."""
        target = self._blob_file(blob_id)
        if target is None or not self._cache_writable:
            return
        payload = {"version": INDEX_VERSION, "lang": lang, **parsed}
        tmp_name = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_name, target)
            tmp_name = None
            self._prune_once()
        except (OSError, ValueError):
            self._cache_writable = False
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _build(self) -> None:
        wanted: List[Tuple[str, str, str]] = []  # (path, blob id, language)
        for path, blob_id, size in self._files:
            lang = language(path)
            if lang is None:
                continue
            if size > MAX_INDEXED_BYTES:
                self.skipped_large.append(path)
                continue
            wanted.append((path, blob_id, lang))

        results: Dict[Tuple[str, str], FileIndex] = {}
        todo: Dict[Tuple[str, str], str] = {}  # (blob id, language) -> a path with that content
        for path, blob_id, lang in wanted:
            key = (blob_id, lang)
            if key in results or key in todo:
                continue
            cached = self._load(blob_id, lang)
            if cached is not None:
                results[key] = cached
                self.stats["cache_hits"] += 1
            else:
                todo[key] = path
        keys = list(todo)
        for offset in range(0, len(keys), BLOB_BATCH):
            chunk = keys[offset:offset + BLOB_BATCH]
            blobs = self._read_blobs(list(dict.fromkeys(blob_id for blob_id, _lang in chunk)))
            for key in chunk:
                blob_id, lang = key
                data = blobs.get(blob_id)
                parsed: Optional[FileIndex] = None
                if data is not None and b"\0" not in data[:8192]:
                    parsed = parse_file(todo[key], data.decode("utf-8", errors="replace"))
                parsed = parsed or {"decls": [], "refs": {}, "error": False, "lines": 0}
                results[key] = parsed
                self.stats["parsed"] += 1
                self._store(blob_id, lang, parsed)

        decls: List[Decl] = []
        for path, blob_id, lang in sorted(wanted):
            parsed = results.get((blob_id, lang))
            if parsed is None:
                continue
            self.stats["files"] += 1
            self.lines[path] = int(parsed.get("lines", 0))
            if parsed.get("error"):
                self.errors.append(path)
            for decl in parsed["decls"]:
                decls.append({**decl, "path": path})
            for name, lines in parsed["refs"].items():
                self.refs.setdefault(name, {})[path] = lines
        decls.sort(key=lambda d: (d["path"], d["start"], d["qualified"]))
        self.decls = decls


def blob_batch_parse(raw: bytes, blob_ids: Sequence[str]) -> Dict[str, bytes]:
    """Splits `git cat-file --batch` output for blob_ids (in request order) into {blob id: content}."""
    found: Dict[str, bytes] = {}
    pos = 0
    for blob_id in blob_ids:
        end = raw.find(b"\n", pos)
        if end < 0:
            break
        header = raw[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b"blob" and header[2].isdigit():
            size = int(header[2])
            found[blob_id] = raw[pos:pos + size]
            pos += size + 1
    return found

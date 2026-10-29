"""
File view of the code wiki (docs/design/code-wiki-coverage.md, C): one repository file as people see it in the
dashboard, made without an LLM from the symbol index, the wiki pages and the documents of the commit.

Commands return (exit_code, payload) like wiki_ops: 0 success, 2 invalid input or an unusable project/commit,
3 a path that is not a readable file of the commit (secret patterns, symlinks, submodules and binary files are never
served). Every answer is computed at --sha (default: the project workspace's HEAD).

References are name based (as the repository map ranks them): a declared name counts only when every declaration
of that name is in one file, names of two characters or fewer are ignored, and only files of the same language are
linked, and only when the using file imports the declaring one (Python `import`/`from`, TS/JS `from '…/name'`,
`import()` or `require()`, Go: the same directory or an import path ending in its directory). A production file never
"uses" a test, story or preview file; files of that kind are marked "test" when they use this one. The answer says so
in "references".
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from code_select import language
from repo_map import is_noise_path
from code_wiki import PageNotFound, WikiError, WikiView, in_scope, strong_citation_paths
from wiki_ops import INVALID, NOT_FOUND, OpenError, Result, _invalid, open_store, open_tools

MAX_LIST_FILES = 20000
MAX_READ_LINES = 400
MAX_READ_BYTES = 64 * 1024
MAX_REFERENCE_FILES = 50
MAX_DOCS = 50
MAX_DOC_LINES = 20
MAX_DOC_BYTES = 1024 * 1024
MIN_NAME_CHARS = 3
DOC_SUFFIXES = (".md", ".markdown", ".mdx", ".rst")
REFERENCES_NOTE = ("name based: a name counts when all its declarations are in one file of the same language "
                   "and the using file imports that file")

_TOKEN = re.compile(r"[\w.\-/]+")
_TITLE = re.compile(r"^#\s+(.+?)\s*#*\s*$")


def _open(project_id: Optional[str], sha: Optional[str]) -> Tuple[Any, Any, Any]:
    project, store = open_store(project_id)
    return project, store, open_tools(project, sha)


def list_files(project_id: Optional[str], sha: Optional[str] = None) -> Result:
    """{"ok", "project", "sha", "files": [{path, size}], "truncated"}: the readable files of the commit, by path."""
    try:
        project, _store, tools = _open(project_id, sha)
    except OpenError as exc:
        return _invalid(str(exc))
    paths = tools.readable_paths()
    files = [{"path": p, "size": tools.entries[p][1]} for p in paths[:MAX_LIST_FILES]]
    return 0, {"ok": True, "project": project.id, "sha": tools.sha, "files": files,
               "truncated": len(paths) > MAX_LIST_FILES}


def _owners(index: Any) -> Dict[str, Set[str]]:
    """Declared name -> files that declare it."""
    owners: Dict[str, Set[str]] = {}
    for decl in index.decls:
        owners.setdefault(decl["name"], set()).add(decl["path"])
    return owners


def _ranked(counts: Dict[str, int]) -> List[Dict[str, Any]]:
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_REFERENCE_FILES]
    return [{"path": p, "count": n, "test": is_noise_path(p)} for p, n in top]


def _module_stem(path: str) -> str:
    """The name an import uses for a file: the file name without its extension, or the directory of an index file."""
    directory, _, name = path.rpartition("/")
    stem = name.split(".", 1)[0]
    if stem in ("index", "__init__") and directory:
        stem = directory.rsplit("/", 1)[-1]
    return stem


def imports(user: str, text: str, owner: str) -> bool:
    """Whether the source text of user imports owner (see the module docstring for the forms recognised)."""
    lang = language(owner)
    user_dir, owner_dir = user.rpartition("/")[0], owner.rpartition("/")[0]
    if lang == "go":
        return user_dir == owner_dir or bool(owner_dir) and re.search(
            r'"[^"\n]*/' + re.escape(owner_dir.rsplit("/", 1)[-1]) + r'"', text) is not None
    stem = re.escape(_module_stem(owner))
    if lang == "python":
        return re.search(r"^\s*(?:from\s+[\w.]*\b" + stem + r"\b|import\s+[\w., ]*\b" + stem + r"\b)",
                         text, re.MULTILINE) is not None
    return re.search(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)['"][^'"\n]*\b""" + stem
                     + r"""(?:\.[cm]?[jt]sx?)?(?:/index)?['"]""", text) is not None


def references(index: Any, path: str, texts: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(incoming, outgoing): files that use the names this file declares, and files whose names this file uses,
    each with the number of reference lines, most first. texts(paths) returns {path: source text}."""
    lang = language(path)
    noisy = is_noise_path(path)
    owners = _owners(index)
    unique = {name: next(iter(paths)) for name, paths in owners.items()
              if len(paths) == 1 and len(name) >= MIN_NAME_CHARS}
    incoming: Dict[str, int] = {}
    outgoing: Dict[str, int] = {}
    for name, where in index.refs.items():
        owner = unique.get(name)
        if owner is None:
            continue
        if owner == path:
            for other, lines in where.items():
                if other != path and language(other) == lang:
                    incoming[other] = incoming.get(other, 0) + len(lines)
        elif path in where and language(owner) == lang and (noisy or not is_noise_path(owner)):
            outgoing[owner] = outgoing.get(owner, 0) + len(where[path])
    sources = texts([path, *incoming])
    own = sources.get(path, "")
    incoming = {p: n for p, n in incoming.items() if imports(p, sources.get(p, ""), path)}
    outgoing = {p: n for p, n in outgoing.items() if imports(path, own, p)}
    return _ranked(incoming), _ranked(outgoing)


def outline(index: Any, path: str) -> List[Dict[str, Any]]:
    """Declarations of the file in line order (signatures have string literals masked, as in the index cache)."""
    decls = sorted((d for d in index.decls if d["path"] == path), key=lambda d: (d["start"], d["end"]))
    return [{"name": d["name"], "qualified": d["qualified"], "kind": d["kind"], "start": d["start"],
             "end": d["end"], "signature": d["signature"]} for d in decls]


def pages_for(store: Any, tools: Any, path: str) -> List[Dict[str, Any]]:
    """Wiki pages that cover the file: through their scope, their basis (read when written) or a strong citation."""
    view = WikiView(store, tools)
    found = []
    for page in view.pages:
        if in_scope(path, page.scope):
            via = "scope"
        elif path in page.basis:
            via = "basis"
        elif path in strong_citation_paths(page.body, tools):
            via = "cited"
        else:
            continue
        found.append({"id": page.id, "title": page.title, "state": view.details(page)["state"], "via": via})
    return found


def docs_for(tools: Any, path: str) -> List[Dict[str, Any]]:
    """Documents of the commit that mention the file by its full path, or by its name when no other readable file
    has that name; with the document title (first `# ` line) and the lines that mention it."""
    readable = tools.readable_paths()
    name = path.rsplit("/", 1)[-1]
    name_unique = sum(1 for p in readable if p.rsplit("/", 1)[-1] == name) == 1
    candidates = [p for p in readable if p.lower().endswith(DOC_SUFFIXES) and p != path
                  and tools.entries[p][1] <= MAX_DOC_BYTES]
    found = []
    for doc, text in sorted(tools.source_texts(candidates).items()):
        title = ""
        lines: List[int] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not title:
                match = _TITLE.match(line)
                if match:
                    title = match.group(1)
            for token in _TOKEN.findall(line):
                token = token.rstrip(".,-")
                while token.startswith("./"):
                    token = token[2:]
                if token == path or (name_unique and token == name):
                    lines.append(number)
                    break
            if len(lines) >= MAX_DOC_LINES:
                break
        if lines:
            found.append({"path": doc, "title": title, "lines": lines})
        if len(found) >= MAX_DOCS:
            break
    return found


def show_file(project_id: Optional[str], path: str, sha: Optional[str] = None) -> Result:
    """{"ok", "project", "sha", "path", "language", "size", "lines", "indexed", "outline", "incoming", "outgoing",
    "references", "pages", "docs"}."""
    try:
        project, store, tools = _open(project_id, sha)
    except OpenError as exc:
        return _invalid(str(exc))
    if not isinstance(path, str) or not tools.readable(path):
        return NOT_FOUND, {"ok": False, "error": f"{path!r} is not a readable file of {tools.sha[:7]}"}
    index = tools.code_index()
    text = tools.source_text(path)
    indexed = path in index.lines
    incoming, outgoing = references(index, path, tools.source_texts) if indexed else ([], [])
    try:
        pages = pages_for(store, tools, path)
    except (PageNotFound, WikiError):
        pages = []
    return 0, {
        "ok": True, "project": project.id, "sha": tools.sha, "path": path, "language": language(path),
        "size": tools.entries[path][1], "binary": text is None,
        "lines": index.lines.get(path) or (len(text.splitlines()) if text is not None else 0),
        "indexed": indexed, "outline": outline(index, path) if indexed else [],
        "incoming": incoming, "outgoing": outgoing, "references": REFERENCES_NOTE,
        "pages": pages, "docs": docs_for(tools, path),
    }


def read_lines(project_id: Optional[str], path: str, start: Any, end: Any, sha: Optional[str] = None) -> Result:
    """{"ok", "project", "sha", "path", "start", "end", "total", "lines": [text], "truncated"}: lines start..end
    (1-based, inclusive) of a readable text file, at most MAX_READ_LINES lines and MAX_READ_BYTES bytes."""
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return _invalid("start and end must be line numbers with 1 <= start <= end")
    if end - start + 1 > MAX_READ_LINES:
        return _invalid(f"at most {MAX_READ_LINES} lines per request")
    try:
        project, _store, tools = _open(project_id, sha)
    except OpenError as exc:
        return _invalid(str(exc))
    text = tools.source_text(path) if isinstance(path, str) and tools.readable(path) else None
    if text is None:
        return NOT_FOUND, {"ok": False, "error": f"{path!r} is not a readable text file of {tools.sha[:7]}"}
    all_lines = text.splitlines()
    if start > max(len(all_lines), 1):
        return _invalid(f"start {start} is past the end of the file ({len(all_lines)} lines)")
    lines: List[str] = []
    size = 0
    truncated = False
    for line in all_lines[start - 1:end]:
        size += len(line.encode("utf-8")) + 1
        if size > MAX_READ_BYTES:
            truncated = True
            break
        lines.append(line)
    return 0, {"ok": True, "project": project.id, "sha": tools.sha, "path": path, "start": start,
               "end": start + len(lines) - 1, "total": len(all_lines), "lines": lines, "truncated": truncated}


__all__ = ["INVALID", "NOT_FOUND", "list_files", "read_lines", "show_file"]

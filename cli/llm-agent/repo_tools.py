"""
Read-only repository tools for debate agents (docs/design/debate-agent-retrieval.md).

The debate engine runs these on behalf of agents that have no file tools of their own: an agent
writes a ```repo request block, the engine calls RepoTools.run() for each request and sends the
results back into the same session. Only committed content at a pinned sha is read, with the rules
of code_context: secret file names, symlinks, submodules and binary files are never returned, and
uncommitted edits left by judge actions never leak in.

Every result is {"op", "target", "ok", "bytes", "truncated", "error", "text"}.
"""

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from code_context import (
    GIT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_PATTERN_CHARS,
    SUBMODULE_MODE,
    SYMLINK_MODE,
    ContextError,
    _git,
    is_secret,
    matches,
)
import go_ops
from code_wiki import PageNotFound, WikiError, WikiStore, WikiView
from code_index import CodeIndex, blob_batch_parse, coverage_note, without_coverage_note
from evidence import WHOLE_FILE_END, Location, ShortRef, Span, coalesce, parse_location, scan_short_refs
from go_facts import GoFacts, GoFactsProvider, goindex_map_wait, module_dirs
from repo_map import build_map
from code_select import (
    SYMBOL_PATTERN,
    declaration_lines,
    import_spans,
    outline,
    parse_selector,
    symbol_spans,
)

OPS = ("list", "grep", "read", "outline", "defs", "refs", "map", "callers", "callees", "wiki")
MAX_LIST_LINES = 400
MAX_GREP_LINES = 200
MAX_GREP_LINE_CHARS = 300
MAX_DEFS_LINES = 100
MAX_REFS_LINES = 200
MAX_CALLERS_LINES = 200
MAX_CALLEES_LINES = 200
REFS_HEADER = "[refs of {symbol}: name-based matches, may include unrelated symbols with the same name]"
REFS_BLOB_CHUNK = 25
DEFAULT_DEPTH = 2
MAX_DEPTH = 4
# git grep output is cut here so a pattern matching everything cannot fill memory.
MAX_GREP_OUTPUT_BYTES = 4 * 1024 * 1024

# Called for read results with (path, shown text segment, whole file shown) so the caller can track what was seen.
SeenSink = Callable[[str, List[str], bool], None]


def _result(op: str, target: str, ok: bool = True, text: str = "", truncated: bool = False,
            error: Optional[str] = None, resolved: Optional[str] = None) -> Dict[str, Any]:
    result = {
        "op": op,
        "target": target,
        "ok": ok,
        "bytes": len(text.encode("utf-8")),
        "truncated": truncated,
        "error": error,
        "text": text,
    }
    if resolved is not None:
        result["resolved"] = resolved  # "types" (compiler-assisted) or "name" for defs/refs/callers/callees
    return result


def _fail(op: str, target: str, error: str) -> Dict[str, Any]:
    return _result(op, target, ok=False, error=error)


class InvalidRequest(str):
    """A malformed piece of a repo block; RepoTools.run turns it into an error result."""


# A fenced block whose info string is exactly "repo".
REPO_BLOCK = re.compile(r"```repo[ \t]*\r?\n(.*?)```", re.DOTALL)


def parse_repo_blocks(text: str) -> Optional[List[Any]]:
    """Requests from all the ```repo blocks in an agent reply, or None when the reply has no block.

    Malformed JSON or shapes become InvalidRequest items so they still cost a step and are
    answered with an error. The list is not capped: when GUARDIAN_RETRIEVAL_REQUESTS is set the
    engine serves that many and defers the rest (see DebateEngine._serve_step).
    """
    blocks = REPO_BLOCK.findall(text or "")
    if not blocks:
        return None
    items: List[Any] = []
    for body in blocks:
        try:
            data = json.loads(body)
        except ValueError as exc:
            items.append(InvalidRequest(f"invalid JSON in repo block: {exc}"))
            continue
        listed = data.get("requests") if isinstance(data, dict) else data
        if not isinstance(listed, list):
            items.append(InvalidRequest('repo block must be {"requests": [ ... ]}'))
            continue
        items.extend(listed)
    return items or [InvalidRequest("repo block has no requests")]


def strip_repo_blocks(text: str) -> str:
    """The reply without its ```repo blocks."""
    return REPO_BLOCK.sub("", text or "").strip()


def trim_after_first_block(text: str) -> Tuple[str, int, int]:
    """(text up to the end of the first ```repo block, characters discarded after it, block count).

    Whatever a model writes after a request block was written before it saw any result, so the
    engine drops it. Whitespace after the block is not counted. A reply without a block is returned whole.
    """
    text = text or ""
    first = REPO_BLOCK.search(text)
    if first is None:
        return text, 0, 0
    tail = text[first.end():]
    return text[:first.end()], len(tail.strip()), len(REPO_BLOCK.findall(text))


# Tool-call markup from other agent protocols. None of it is executed here; only ```repo blocks are served.
PSEUDO_TOOL_TAG = re.compile(
    r"<\s*(?:antml:)?(?:function_calls|function_call|invoke|tool_use|tool_calls|tool_call)\b[^>]*>", re.IGNORECASE)
PSEUDO_TOOL_NAME = re.compile(r"""\bname\s*=\s*["']([^"'<>]{1,80})["']""")
_QUOTED_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def find_pseudo_tool_calls(text: str) -> Optional[Tuple[str, int, List[str]]]:
    """(text before the first tool-call markup, characters discarded from it on, tool names) or None.

    Markup such as <function_calls>, <invoke name="glob"> or <tool_use> inside backticks or a fenced
    block is a quotation, not a call, and is ignored. The names come from the name attribute of the
    invoke / tool_use / tool_call tags, in order, without repeats.
    """
    text = text or ""
    quoted = [m.span() for m in _QUOTED_CODE.finditer(text)]
    hits = [m for m in PSEUDO_TOOL_TAG.finditer(text) if not any(a <= m.start() < b for a, b in quoted)]
    if not hits:
        return None
    names: List[str] = []
    for hit in hits:
        tag = hit.group(0)
        if re.match(r"<\s*(?:antml:)?(?:invoke|tool_use|tool_call)\b", tag, re.IGNORECASE):
            found = PSEUDO_TOOL_NAME.search(tag)
            if found and found.group(1) not in names:
                names.append(found.group(1))
    start = hits[0].start()
    return text[:start].rstrip(), len(text[start:].strip()), names


def deferred_result(request: Dict[str, Any]) -> Dict[str, Any]:
    """The result of a request that was not run in this step; "request" lets the engine resend it verbatim."""
    info = describe_request(request)
    return {**_fail(info["op"], info["target"], "deferred"), "deferred": True, "request": request}


def provided_paths(result: Dict[str, Any]) -> List[str]:
    """Repository files whose code a successful result put in front of the agent (list, defs and map show none)."""
    if not result.get("ok") or not result.get("text"):
        return []
    text = result["text"]
    if result["op"] == "read":
        found = re.match(r"--- File: (.+?) \(\d+ bytes\) ---", text)
        return [found.group(1)] if found else []
    if result["op"] == "outline":
        found = re.match(r"\[outline of (.+?): line: declaration\]", text)
        return [found.group(1)] if found else []
    if result["op"] in ("grep", "refs"):
        # refs prints grep-format lines; its header line has no "path:N: " part. defs provides no file.
        paths = (m.group(1) for m in re.finditer(r"^(.+?):\d+: ", without_coverage_note(text), re.MULTILINE))
        return list(dict.fromkeys(paths))
    return []


TRUNCATION_MARK = "[... truncated by Guardian ...]"
_NUMBERED_LINE = re.compile(r"(\d+)\|(?: |$)")
_OUTLINE_ENTRY = re.compile(r"(\d+): ")
_GREP_LINE = re.compile(r"^(.+?):(\d+): ", re.MULTILINE)


def provided_spans(result: Dict[str, Any]) -> Dict[str, List[Span]]:
    """Line spans of each file that a successful result put in front of the agent, per path (see provided_paths).

    Read from the result text, so it holds for engine-served and MCP-served results alike and for results that
    max_bytes cut: a whole-file read that was not truncated is the whole file (1, WHOLE_FILE_END); a truncated read
    is the numbered lines that arrived complete; a partial read (`path:A-B`, `path#Symbol`) is its numbered lines
    (the import block included) plus the outline entries it lists, each outline entry a single line; an outline is
    its entries; grep and refs are each matched line. defs, map and list show no code and give nothing.
    """
    paths = provided_paths(result)
    if not paths:
        return {}
    text, op = result["text"], result["op"]
    truncated = bool(result.get("truncated"))
    spans: List[Span] = []
    if op == "read":
        body = text.split("\n")[1:]
        while body and body[-1].startswith(TRUNCATION_MARK):
            body.pop()
        partial = bool(body) and body[0].startswith(("[outline of ", "[lines "))
        if not partial and not truncated:
            return {paths[0]: [(1, WHOLE_FILE_END)]}
        in_outline = False
        numbers: List[int] = []
        for line in body:
            if line.startswith("[outline of "):
                in_outline = True
            elif line.startswith("[lines "):
                in_outline = False
            elif in_outline:
                found = _OUTLINE_ENTRY.match(line)
                if found:
                    spans.append((int(found.group(1)),) * 2)
            else:
                found = _NUMBERED_LINE.match(line)
                if found:
                    numbers.append(int(found.group(1)))
        if truncated and numbers:
            numbers.pop()  # the cut may have ended inside this line
        spans.extend((n, n) for n in numbers)
        return {paths[0]: coalesce(spans)}
    if op == "outline":
        body = text.split("\n")[1:]
        for line in body[:-1] if truncated else body:
            found = _OUTLINE_ENTRY.match(line)
            if found:
                spans.append((int(found.group(1)),) * 2)
        return {paths[0]: coalesce(spans)}
    per_path: Dict[str, List[Span]] = {}
    for found in _GREP_LINE.finditer(without_coverage_note(text)):
        per_path.setdefault(found.group(1), []).append((int(found.group(2)),) * 2)
    return {path: coalesce(per_path.get(path, [])) for path in paths}


def clip_result(result: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """Cuts the result text to limit bytes (marking it truncated); limit <= 0 leaves no text."""
    data = result["text"].encode("utf-8")
    if len(data) <= max(limit, 0):
        return result
    if limit <= 0:
        return {**result, "ok": False, "bytes": 0, "truncated": True, "error": "budget", "text": ""}
    text = data[:limit].decode("utf-8", errors="ignore") + "\n[... truncated by Guardian ...]"
    return {**result, "bytes": len(text.encode("utf-8")), "truncated": True, "text": text}


def normalize_path(raw: Any) -> Optional[str]:
    """A repository-relative path, or None when it is unusable (absolute parts, '..', NUL, newline)."""
    if not isinstance(raw, str) or any(c in raw for c in "\0\n\r"):
        return None
    path = raw.strip()
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def _literal_prefix(glob: str) -> str:
    """Directory part of a glob before its first wildcard, used to narrow git grep."""
    while glob.startswith("./"):
        glob = glob[2:]
    head = re.split(r"[*?]", glob, maxsplit=1)[0]
    return head.rsplit("/", 1)[0] + "/" if "/" in head else ""


# What may follow a path to make a citation strong: ":12", ":12-20", "#Symbol", " line 12", " (L12)".
STRONG_SUFFIX = re.compile(r"[`'\"*)\]]*(?::\s*\d|#[A-Za-z_]|\s*,?\s*\(?(?:[Ll]ines?|L)\s*\d)")


class RepoTools:
    """list / grep / read / outline / defs / refs / map / callers / callees / wiki over the committed content of one workspace at a fixed sha.

    Instances are safe to share between threads: the tree is read once and never mutated, and the symbol
    index behind defs/refs is built once under a lock. cache_dir (optional) is where the index keeps its
    per-blob parse results (see code_index); without it the index lives in memory only.
    """

    def __init__(self, workspace: Path, sha: str, cache_dir: Optional[Path] = None,
                 go_provider: Optional[GoFactsProvider] = None, wiki_dir: Optional[Path] = None,
                 sha_label: Optional[str] = None) -> None:
        self.workspace = workspace
        self.sha = sha
        # The commit named in what an agent reads (the map header). A worker reads a snapshot whose commit id is its
        # own; the label is the hub's commit, so a result looks the same wherever it was produced.
        self.sha_label = sha_label or sha
        self.cache_dir = Path(cache_dir) if cache_dir else None
        # Code wiki pages of the project (code_wiki.py); without it the wiki op answers "not available".
        self.wiki_dir = Path(wiki_dir) if wiki_dir else None
        self._index: Optional[CodeIndex] = None
        self._index_lock = threading.Lock()
        # path -> (mode, size, blob id) for every entry of the commit.
        self.entries: Dict[str, Tuple[str, int, str]] = {}
        raw = _git(workspace, "ls-tree", "-r", "-l", "-z", "--full-tree", sha)
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            fields = meta.decode().split()
            size = int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else 0
            blob_id = fields[2] if len(fields) > 2 else ""
            self.entries[path.decode("utf-8", errors="replace")] = (fields[0], size, blob_id)
        # Compiler-assisted Go facts (P2.6): the engine starts the extraction; other processes only read them.
        self.go_provider: Optional[GoFactsProvider] = go_provider
        if self.go_provider is None and self.cache_dir is not None:
            self.go_provider = GoFactsProvider(workspace, sha, self.cache_dir / "go", self.go_modules,
                                               self.go_readable_paths)

    # --- helpers -----------------------------------------------------------------------------

    def readable_paths(self) -> List[str]:
        """Paths an agent may see: no secrets, symlinks or submodules (binary files are found on read)."""
        return sorted(p for p, (mode, _size, _blob) in self.entries.items()
                      if mode not in (SYMLINK_MODE, SUBMODULE_MODE) and not is_secret(p))

    def snapshot_files(self, paths: Sequence[str], batch_bytes: int = 8 * 1024 * 1024) -> Iterator[Tuple[str, str, bytes]]:
        """(path, mode, content) of the given paths, read in batches of about batch_bytes (repo_snapshot.py).

        Paths that are not entries of the commit, or whose blob cannot be read, are skipped."""
        batch: List[str] = []
        size = 0

        def flush() -> Iterator[Tuple[str, str, bytes]]:
            blobs = self._read_blobs(list(dict.fromkeys(self.entries[p][2] for p in batch)))
            for path in batch:
                mode, _size, blob_id = self.entries[path]
                if blob_id in blobs:
                    yield path, mode, blobs[blob_id]

        for path in paths:
            entry = self.entries.get(path)
            if entry is None:
                continue
            batch.append(path)
            size += entry[1]
            if size >= batch_bytes:
                yield from flush()
                batch, size = [], 0
        if batch:
            yield from flush()

    def go_modules(self) -> List[str]:
        """Directories of the Go modules of this commit (go.mod files the agents may see)."""
        return module_dirs(self.readable_paths())

    def go_readable_paths(self) -> List[str]:
        return [p for p in self.readable_paths() if p.endswith(".go")]

    def go_map_facts(self) -> Optional[GoFacts]:
        """Facts for ranking Go files in the repository map: never waits longer than GUARDIAN_GOINDEX_MAP_WAIT (0)."""
        provider = self.go_provider
        if provider is None or not provider.has_go():
            return None
        return provider.get(wait=goindex_map_wait())

    def go_note(self) -> str:
        """The map line about how Go files were ranked, or "" when the repository has no Go module."""
        provider = self.go_provider
        if provider is None or not provider.has_go():
            return ""
        return provider.note()

    def _known_paths(self) -> Set[str]:
        known = self.__dict__.get("_readable_set")
        if known is None:
            known = self._readable_set = set(self.readable_paths())
        return known

    def cited_refs(self, text: str) -> Dict[str, bool]:
        """Readable repository files a piece of text refers to, in order of first mention, with strength.

        Matches whole path tokens only ("apps/agent/main.go", also in "main.go:12", "main.go#Sym" or
        inline code); words and paths that are not in the commit tree are ignored. The value is True for a
        strong citation (`path:N`, `path:N-M`, `path#Symbol`, `path` followed by "line N" / "L N") and False
        for a bare mention; a path cited strongly anywhere is strong.
        """
        return {path: strong for path, (strong, _locations) in self.cited_locations(text).items()}

    def cited_locations(self, text: str) -> Dict[str, Tuple[bool, List[Location]]]:
        """Like cited_refs, with where each strong citation of a path points.

        Per path: (strong, locations), locations being ("lines", first, last) for `path:N`, `path:N-M` and
        "line N" / "L N" forms, or ("symbol", name) for `path#Symbol`, in order of appearance without repeats.
        """
        known = self._known_paths()
        found: Dict[str, Tuple[bool, List[Location]]] = {}
        text = text or ""
        for match in re.finditer(r"[\w.\-/]+", text):
            token = match.group(0).rstrip(".,-")
            while token.startswith("./"):
                token = token[2:]
            token = token.lstrip("/")
            if token in known:
                strong = bool(STRONG_SUFFIX.match(text, match.end()))
                was_strong, locations = found.get(token, (False, []))
                if strong:
                    where = parse_location(text, match.end())
                    if where is not None and where not in locations:
                        locations = [*locations, where]
                found[token] = (was_strong or strong, locations)
        return found

    def short_refs(self, text: str) -> List[ShortRef]:
        """Citations by short path (`guardian.py:755`) or by line only (`line 510`) with the tree paths they can mean."""
        by_name = self.__dict__.get("_by_name")
        if by_name is None:
            by_name = {}
            for path in self._known_paths():
                by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)
            self._by_name = by_name
        return scan_short_refs(text, self._known_paths(), by_name)

    def readable(self, path: str) -> bool:
        """Whether path is a file of this commit that agents may see."""
        return path in self._known_paths()

    def source_text(self, path: str) -> Optional[str]:
        """The text of a readable, non-binary file at the pinned commit; None when it is not readable. Cached."""
        cache = self.__dict__.setdefault("_source_cache", {})
        if path in cache:
            return cache[path]
        text: Optional[str] = None
        if path in self.entries and not self._excluded(path):
            try:
                blob = self._blob(path)
            except ContextError:
                blob = b"\0"
            if b"\0" not in blob[:8192]:
                text = blob.decode("utf-8", errors="replace")
        if len(cache) >= 64:
            cache.pop(next(iter(cache)))
        cache[path] = text
        return text

    def source_texts(self, paths: List[str]) -> Dict[str, str]:
        """Texts of several readable, non-binary files through one `git cat-file --batch` (unreadable ones left out)."""
        wanted = [p for p in dict.fromkeys(paths) if p in self.entries and not self._excluded(p)]
        blobs = self._read_blobs(list(dict.fromkeys(self.entries[p][2] for p in wanted)))
        out: Dict[str, str] = {}
        for path in wanted:
            data = blobs.get(self.entries[path][2])
            if data is not None and b"\0" not in data[:8192]:
                out[path] = data.decode("utf-8", errors="replace")
        return out

    def cited_paths(self, text: str) -> List[str]:
        """Readable repository files a piece of text refers to, in order of first mention."""
        return list(self.cited_refs(text))

    def mentioned_in(self, result: Dict[str, Any]) -> List[str]:
        """Tree paths that occur inside the content a successful result returned.

        A list, defs, map, callers, callees or wiki result is only a listing of names (a wiki page is navigation notes
        written by someone else, never code) and mentions nothing; read, outline, grep and refs text does (for example
        a path kept in a string constant of the code that was read).
        """
        if (not result.get("ok") or not result.get("text")
                or result.get("op") in ("list", "defs", "map", "callers", "callees", "wiki")):
            return []
        return self.cited_paths(without_coverage_note(result["text"]))

    def _excluded(self, path: str) -> bool:
        entry = self.entries.get(path)
        return entry is not None and (entry[0] in (SYMLINK_MODE, SUBMODULE_MODE) or is_secret(path))

    def _blob(self, path: str) -> bytes:
        return _git(self.workspace, "cat-file", "blob", f"{self.sha}:{path}")

    def _read_blobs(self, blob_ids: List[str]) -> Dict[str, bytes]:
        """Contents of blobs by id through one `git cat-file --batch` process per call."""
        if not blob_ids:
            return {}
        try:
            proc = subprocess.run(["git", "cat-file", "--batch"], cwd=self.workspace, capture_output=True,
                                  input="".join(f"{b}\n" for b in blob_ids).encode(), timeout=GIT_TIMEOUT * 4)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContextError(f"git cat-file failed: {exc}") from exc
        if proc.returncode != 0:
            raise ContextError("git cat-file failed: " + proc.stderr.decode(errors="replace").strip()[:200])
        return blob_batch_parse(proc.stdout, blob_ids)

    def code_index(self) -> CodeIndex:
        """The symbol index of this commit (built on the first defs/refs request, then shared)."""
        with self._index_lock:
            if self._index is None:
                files = [(p, self.entries[p][2], self.entries[p][1]) for p in self.readable_paths()]
                self._index = CodeIndex(files, self._read_blobs, self.cache_dir)
        return self._index.ensure()

    # --- dispatch ----------------------------------------------------------------------------

    # @MX:ANCHOR: Single entry point for every agent-requested repository read.
    # @MX:REASON: The debate engine and the tests call it; it enforces the exclusion and size rules.
    def run(self, request: Any, on_seen: Optional[SeenSink] = None, max_bytes: Optional[int] = None,
            defer_below: Optional[float] = None) -> Dict[str, Any]:
        """Runs one request dict ({"op": ..., ...}) and returns a result dict. Never raises.

        max_bytes cuts the result text. on_seen gets the code a read really returned; a read cut
        by max_bytes reports nothing, because the exact spans that survived are not known.
        With defer_below (a fraction), a read that max_bytes would cut to less than that fraction
        of its size is not returned at all but deferred, so the agent asks for it again whole.
        """
        if isinstance(request, InvalidRequest):
            return _fail("?", "repo block", str(request))
        if not isinstance(request, dict):
            return _fail("?", "?", "request must be a JSON object")
        op = request.get("op")
        if op not in OPS:
            return _fail(str(op)[:40], str(request.get("path") or request.get("pattern") or request.get("symbol") or "")[:80],
                         f"unknown op (use one of {', '.join(OPS)})")
        pending: List[Tuple[str, List[str], bool]] = []
        try:
            if op == "list":
                result = self.list(request.get("path", ""), request.get("depth", DEFAULT_DEPTH))
            elif op == "grep":
                result = self.grep(request.get("pattern"), request.get("glob"), bool(request.get("ignore_case", False)))
            elif op == "read":
                result = self.read(request.get("path"), lambda path, segs, whole: pending.append((path, segs, whole)))
            elif op == "defs":
                result = self.defs(request.get("symbol"), request.get("glob"))
            elif op == "refs":
                result = self.refs(request.get("symbol"), request.get("glob"), request.get("include_defs") is True)
            elif op == "callers":
                result = self.callers(request.get("symbol"), request.get("glob"), request.get("include_tests") is True)
            elif op == "callees":
                result = self.callees(request.get("symbol"))
            elif op == "map":
                result = self.repo_map(request.get("path", ""), request.get("depth", DEFAULT_DEPTH))
            elif op == "wiki":
                result = self.wiki(request.get("page"), request.get("query"))
            else:
                result = self.outline(request.get("path"))
        except ContextError as exc:
            result = _fail(op, str(request.get("path") or request.get("pattern") or request.get("symbol") or "")[:80],
                           str(exc)[:200])
        if (max_bytes is not None and defer_below and op == "read" and result["ok"]
                and max_bytes < result["bytes"] * defer_below):
            return deferred_result(request)
        if max_bytes is not None:
            clipped = clip_result(result, max_bytes)
            if clipped is not result:
                pending = []
            result = clipped
        if on_seen is not None:
            for path, segments, whole in pending:
                on_seen(path, segments, whole)
        return result

    # --- operations --------------------------------------------------------------------------

    def list(self, path: Any = "", depth: Any = DEFAULT_DEPTH) -> Dict[str, Any]:
        """Files and directories below path, down to depth levels (1-4), with file sizes."""
        norm = normalize_path(path if path not in (None, ".") else "")
        target = norm if norm else "."
        if norm is None:
            return _fail("list", str(path)[:80], "invalid path")
        if isinstance(depth, bool) or not isinstance(depth, int):
            depth = DEFAULT_DEPTH
        depth = max(1, min(MAX_DEPTH, depth))
        if norm in self.entries:
            return _fail("list", target, "excluded" if self._excluded(norm) else "not a directory")
        prefix = f"{norm}/" if norm else ""
        lines: List[str] = []
        seen_dirs = set()
        found = False
        for file_path in self.readable_paths():
            if not file_path.startswith(prefix):
                continue
            found = True
            parts = file_path[len(prefix):].split("/")
            for i in range(1, min(len(parts) - 1, depth) + 1):
                directory = prefix + "/".join(parts[:i]) + "/"
                if directory not in seen_dirs:
                    seen_dirs.add(directory)
                    lines.append(directory)
            if len(parts) <= depth:
                lines.append(f"{file_path}  ({self.entries[file_path][1]} bytes)")
        if not found:
            return _fail("list", target, "not found")
        lines.sort(key=lambda line: line.split("  (")[0])
        truncated = len(lines) > MAX_LIST_LINES
        if truncated:
            lines = lines[:MAX_LIST_LINES] + ["[... more entries; list a subdirectory ...]"]
        return _result("list", target, text="\n".join(lines), truncated=truncated)

    def grep(self, pattern: Any, glob: Any = None, ignore_case: bool = False) -> Dict[str, Any]:
        """Regex search (git grep -n -I -E) in the committed files, as 'path:line: text'."""
        label = str(pattern)[:80]
        if not isinstance(pattern, str) or not pattern:
            return _fail("grep", label, "pattern is required")
        if len(pattern) > MAX_PATTERN_CHARS:
            return _fail("grep", label, f"pattern too long (max {MAX_PATTERN_CHARS})")
        if any(c in pattern for c in "\0\n\r"):
            return _fail("grep", label, "invalid pattern")
        try:
            re.compile(pattern)
        except (re.error, RecursionError, ValueError) as exc:
            return _fail("grep", label, f"invalid regular expression: {exc}")
        if glob is not None and (not isinstance(glob, str) or len(glob) > MAX_PATTERN_CHARS
                                 or glob.startswith("-") or any(c in glob for c in "\0\n\r")):
            return _fail("grep", label, "invalid glob")
        target = pattern[:120] + (f" in {glob}" if glob else "")

        # The pattern follows -e and paths follow --, so neither can be read as an option.
        args = ["grep", "-n", "-I", "-E", "-z", "--no-color", "--no-recurse-submodules"]
        if ignore_case:
            args.append("-i")
        args += ["-e", pattern, self.sha, "--"]
        if glob:
            prefix = _literal_prefix(glob)
            if prefix:
                args.append(f":(literal){prefix}")
        try:
            proc = subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _fail("grep", target, f"git grep failed: {exc}")
        if proc.returncode not in (0, 1):
            return _fail("grep", target, "git grep failed: " + proc.stderr.decode(errors="replace").strip()[:200])

        out: List[str] = []
        truncated = len(proc.stdout) > MAX_GREP_OUTPUT_BYTES
        header = f"{self.sha}:".encode()
        for record in proc.stdout[:MAX_GREP_OUTPUT_BYTES].split(b"\n"):
            if not record.startswith(header):
                continue
            parts = record[len(header):].split(b"\0", 2)
            if len(parts) != 3:
                continue
            path = parts[0].decode("utf-8", errors="replace")
            if self._excluded(path) or path not in self.entries or (glob and not matches(path, glob)):
                continue
            if len(out) >= MAX_GREP_LINES:
                truncated = True
                break
            text = parts[2].decode("utf-8", errors="replace").rstrip("\r")
            if len(text) > MAX_GREP_LINE_CHARS:
                text = text[:MAX_GREP_LINE_CHARS] + "…"
            out.append(f"{path}:{parts[1].decode()}: {text}")
        if truncated:
            out.append("[... more matches; narrow the pattern or glob ...]")
        if not out:
            return _result("grep", target, text="(no matches)")
        return _result("grep", target, text="\n".join(out), truncated=truncated)

    def read(self, spec: Any, on_seen: Optional[SeenSink] = None) -> Dict[str, Any]:
        """A file, or `path#Symbol` / `path:120-180`. Lines are numbered 'N| code' so answers can cite them."""
        label = str(spec)[:120]
        if not isinstance(spec, str) or not spec.strip():
            return _fail("read", label, "path is required")
        try:
            selector = parse_selector(spec.strip())
        except ValueError as exc:
            return _fail("read", label, str(exc))
        path = normalize_path(selector.glob)
        if not path:
            return _fail("read", label, "invalid path")
        entry = self.entries.get(path)
        if entry is None:
            return _fail("read", label, "not found")
        if self._excluded(path):
            return _fail("read", label, "excluded")
        blob = self._blob(path)
        if b"\0" in blob[:8192]:
            return _fail("read", label, "excluded")
        source = blob.decode("utf-8", errors="replace")
        source_lines = source.splitlines()

        if selector.symbol or selector.lines:
            if selector.lines:
                spans = [selector.lines]
            else:
                spans = symbol_spans(path, source, selector.symbol or "")
                if not spans:
                    return _fail("read", label, "symbol not found")
            spans = [(s, min(e, len(source_lines))) for s, e in spans if s <= len(source_lines)]
            if not spans:
                return _fail("read", label, f"file has only {len(source_lines)} lines")
            # Like a code context pack: the import block comes with a partial file.
            shown = spans + import_spans(path, source)
            body = self._numbered(path, source_lines, shown, outline(path, source))
            seen_spans = shown + declaration_lines(path, source)
            segments = ["".join(source.splitlines(keepends=True)[s - 1:e]) for s, e in seen_spans]
            whole = False
        else:
            body = "\n".join(f"{n}| {line}" for n, line in enumerate(source_lines, 1))
            segments = [source]
            whole = True

        data = body.encode("utf-8")
        truncated = len(data) > MAX_FILE_BYTES
        if truncated:
            body = data[:MAX_FILE_BYTES].decode("utf-8", errors="ignore")
            if whole:
                # Only the complete lines that fit were seen.
                kept = body.count("\n")
                segments = ["\n".join(source_lines[:kept])]
            else:
                segments = []  # a cut partial read cannot be matched to exact spans, so none counts as seen
            body += "\n[... truncated by Guardian ...]"
            whole = False
        if on_seen is not None:
            on_seen(path, segments, whole and not truncated)
        return _result("read", label, text=f"--- File: {path} ({entry[1]} bytes) ---\n{body}", truncated=truncated)

    @staticmethod
    def _symbol_error(symbol: Any, glob: Any) -> Optional[str]:
        """Why a defs/refs symbol or glob is unusable, or None."""
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.match(symbol):
            return "invalid symbol (use Name or Type.Method)"
        if glob is not None and (not isinstance(glob, str) or len(glob) > MAX_PATTERN_CHARS
                                 or glob.startswith("-") or any(c in glob for c in "\0\n\r")):
            return "invalid glob"
        return None

    def _go_facts_for(self, symbol: str, glob: Optional[str], index: Optional[CodeIndex],
                      force: bool = False) -> Tuple[Optional[GoFacts], str]:
        """(facts, "") when type-checked Go facts know the symbol, else (None, a note for name-based results).

        Only a request about Go waits for an extraction in progress (see GoFactsProvider.get): a symbol the symbol
        index finds in a .go file, a glob that selects only .go files, or force (callers/callees). Other symbols use
        the facts only when they are already loaded.
        """
        provider = self.go_provider
        if provider is None or not provider.has_go():
            return None, ""
        facts = provider.peek()
        if facts is None:
            about_go = force or (glob is not None and glob.endswith(".go")) or (
                index is not None and any(d["path"].endswith(".go") for d in index.find_defs(symbol)))
            if not about_go:
                return None, ""
            facts = provider.get()
        if facts is None:
            return None, provider.note()
        return (facts if facts.find_defs(symbol) else None), ""

    def defs(self, symbol: Any, glob: Any = None) -> Dict[str, Any]:
        """Where a symbol is declared: 'path:start-end kind signature' (Name or Type.Method, any language).

        Go definitions come from the type-checked facts when they are available (P2.6); the rest is name-based.
        """
        label = str(symbol)[:120]
        error = self._symbol_error(symbol, glob)
        if error:
            return _fail("defs", label, error)
        target = symbol + (f" in {glob}" if glob else "")
        index = self.code_index()
        in_scope = (lambda path: matches(path, glob)) if glob else None
        facts, go_note = self._go_facts_for(symbol, glob, index)
        note = coverage_note(index.coverage(in_scope))
        if facts is not None:
            text, truncated = go_ops.defs_text(facts, index, symbol, in_scope, MAX_DEFS_LINES)
            return _result("defs", target, text="\n".join([text, *([note] if note else [])]), truncated=truncated,
                           resolved="types")
        found = [d for d in index.find_defs(symbol) if not glob or matches(d["path"], glob)]
        head = [go_note] if go_note else []
        if not found:
            return _result("defs", target, text="\n".join([*head, "(no definitions found)", *([note] if note else [])]),
                           resolved="name")
        lines = [f"{d['path']}:{d['start']}-{d['end']} {d['kind']} {d['signature']}" for d in found[:MAX_DEFS_LINES]]
        truncated = len(found) > MAX_DEFS_LINES
        if truncated:
            lines.append("[... more definitions; narrow with a glob ...]")
        if note:
            lines.append(note)
        return _result("defs", target, text="\n".join([*head, *lines]), truncated=truncated, resolved="name")

    def refs(self, symbol: Any, glob: Any = None, include_defs: bool = False) -> Dict[str, Any]:
        """Lines where the symbol is used, as 'path:line: text' under a header saying how they were found.

        Go symbols with type-checked facts (P2.6) list only the real references; everything else is by name:
        for Type.Method the last component is the identifier. Declaration lines of the symbol are left out
        unless include_defs is set.
        """
        label = str(symbol)[:120]
        error = self._symbol_error(symbol, glob)
        if error:
            return _fail("refs", label, error)
        target = symbol + (f" in {glob}" if glob else "")
        index = self.code_index()
        identifier = symbol.rpartition(".")[2]
        in_scope = (lambda path: matches(path, glob)) if glob else None
        facts, go_note = self._go_facts_for(symbol, glob, index)
        if facts is not None:
            text, truncated = go_ops.refs_text(facts, index, symbol, identifier, in_scope, include_defs,
                                               self._line_texts, MAX_REFS_LINES, MAX_GREP_LINE_CHARS)
            return _result("refs", target, text=text, truncated=truncated, resolved="types")
        skip = set() if include_defs else {(d["path"], d["start"]) for d in index.find_defs(symbol)}
        hits: List[Tuple[str, int]] = []
        for path, numbers in sorted(index.find_refs(identifier).items()):
            if glob and not matches(path, glob):
                continue
            hits.extend((path, n) for n in numbers if (path, n) not in skip)
        header = REFS_HEADER.format(symbol=symbol)
        head = [header, *([go_note] if go_note else [])]
        note = coverage_note(index.coverage(in_scope, identifier))
        if not hits:
            return _result("refs", target, text="\n".join([*head, "(no references found)", *([note] if note else [])]),
                           resolved="name")
        truncated = len(hits) > MAX_REFS_LINES
        shown = hits[:MAX_REFS_LINES]
        texts = self._line_texts(shown)
        out = list(head)
        for path, number in shown:
            text = texts.get((path, number), "")
            if len(text) > MAX_GREP_LINE_CHARS:
                text = text[:MAX_GREP_LINE_CHARS] + "…"
            out.append(f"{path}:{number}: {text}")
        if truncated:
            out.append("[... more references; narrow with a glob ...]")
        if note:
            out.append(note)
        return _result("refs", target, text="\n".join(out), truncated=truncated, resolved="name")

    def _go_only_facts(self, op: str, symbol: Any, glob: Any = None) -> Tuple[Optional[GoFacts], Optional[Dict[str, Any]]]:
        """(facts, None) for callers/callees, or (None, the failure result) when the symbol is unusable or no type
        information exists (not a Go repository, the extraction failed, or it is not finished within the wait)."""
        label = str(symbol)[:120]
        error = self._symbol_error(symbol, glob)
        if error:
            return None, _fail(op, label, error)
        provider = self.go_provider
        if provider is None or not provider.has_go():
            return None, _fail(op, label, "no type information for this language (Go only); use refs")
        facts = provider.get()
        if facts is None:
            state = provider.effective_state() or "not available"
            return None, _fail(op, label, f"no type information yet (Go index {state}); use refs")
        return facts, None

    def callers(self, symbol: Any, glob: Any = None, include_tests: bool = False) -> Dict[str, Any]:
        """Call sites of a Go function or method: 'path:line: enclosing  text', calls only (type-checked)."""
        facts, failure = self._go_only_facts("callers", symbol, glob)
        if failure is not None:
            return failure
        assert facts is not None
        target = symbol + (f" in {glob}" if glob else "")
        in_scope = (lambda path: matches(path, glob)) if glob else None
        text, truncated = go_ops.callers_text(facts, symbol, symbol.rpartition(".")[2], in_scope, include_tests,
                                              self._line_texts, MAX_CALLERS_LINES, MAX_GREP_LINE_CHARS)
        return _result("callers", target, text=text, truncated=truncated, resolved="types")

    def callees(self, symbol: Any) -> Dict[str, Any]:
        """What a Go function or method calls, with definition locations (package.Name outside the module)."""
        facts, failure = self._go_only_facts("callees", symbol)
        if failure is not None:
            return failure
        assert facts is not None
        text, truncated = go_ops.callees_text(facts, symbol, MAX_CALLEES_LINES)
        return _result("callees", symbol, text=text, truncated=truncated, resolved="types")

    def _line_texts(self, hits: List[Tuple[str, int]]) -> Dict[Tuple[str, int], str]:
        """Text of each (path, line) hit, reading the blobs in batches (one git process per batch of files)."""
        wanted: Dict[str, List[int]] = {}
        for path, number in hits:
            wanted.setdefault(path, []).append(number)
        paths = list(wanted)
        texts: Dict[Tuple[str, int], str] = {}
        for offset in range(0, len(paths), REFS_BLOB_CHUNK):
            chunk = paths[offset:offset + REFS_BLOB_CHUNK]
            blobs = self._read_blobs(list(dict.fromkeys(self.entries[p][2] for p in chunk)))
            for path in chunk:
                source = blobs.get(self.entries[path][2], b"").decode("utf-8", errors="replace").splitlines()
                for number in wanted[path]:
                    texts[(path, number)] = source[number - 1].rstrip("\r") if 0 < number <= len(source) else ""
        return texts

    def repo_map(self, path: Any = "", depth: Any = DEFAULT_DEPTH) -> Dict[str, Any]:
        """The repository map (see repo_map) of a directory, directories deeper than depth (1-4) folded.

        Validated like list. Not capped here: the result limits of run() apply (GUARDIAN_REPO_MAP_BYTES is a
        rule for the first prompt only).
        """
        norm = normalize_path(path if path not in (None, ".") else "")
        target = norm if norm else "."
        if norm is None:
            return _fail("map", str(path)[:80], "invalid path")
        if isinstance(depth, bool) or not isinstance(depth, int):
            depth = DEFAULT_DEPTH
        depth = max(1, min(MAX_DEPTH, depth))
        if norm in self.entries:
            return _fail("map", target, "excluded" if self._excluded(norm) else "not a directory")
        prefix = f"{norm}/" if norm else ""
        if prefix and not any(p.startswith(prefix) for p in self.readable_paths()):
            return _fail("map", target, "not found")
        return _result("map", target, text=build_map(self, prefix, depth=depth))

    def wiki(self, page: Any = None, query: Any = None) -> Dict[str, Any]:
        """A code wiki page (`page` id) or a search over the pages (`query`; without both, all pages are listed).

        Navigation notes only: the result provides no file, no line span and no path mention to the citation check
        (provided_paths and provided_spans give nothing for op "wiki", mentioned_in ignores it). The text starts with
        `[wiki page <id> — navigation notes, not evidence; state: fresh|stale|broken]`.
        """
        label = str(page if page not in (None, "") else (query or ""))[:120]
        if self.wiki_dir is None:
            return _fail("wiki", label, "the code wiki is not available")
        if page not in (None, ""):
            if not isinstance(page, str):
                return _fail("wiki", label, "page must be a page id")
            try:
                text, cut = WikiView(WikiStore(self.wiki_dir), self).page_text(page)
            except PageNotFound:
                return _fail("wiki", label, "page not found (search with query)")
            except WikiError as exc:
                return _fail("wiki", label, str(exc)[:200])
            return _result("wiki", label, text=text, truncated=cut)
        if query not in (None, "") and (not isinstance(query, str) or len(query) > MAX_PATTERN_CHARS):
            return _fail("wiki", label, f"query must be text of at most {MAX_PATTERN_CHARS} characters")
        view = WikiView(WikiStore(self.wiki_dir), self)
        return _result("wiki", label, text=view.search_text(query or ""))

    def outline(self, path_arg: Any) -> Dict[str, Any]:
        """Top-level declaration signatures with line numbers (Go, Python, TypeScript/JavaScript)."""
        label = str(path_arg)[:120]
        path = normalize_path(path_arg)
        if not path:
            return _fail("outline", label, "invalid path")
        if path not in self.entries:
            return _fail("outline", label, "not found")
        if self._excluded(path):
            return _fail("outline", label, "excluded")
        blob = self._blob(path)
        if b"\0" in blob[:8192]:
            return _fail("outline", label, "excluded")
        lines = outline(path, blob.decode("utf-8", errors="replace"))
        if not lines:
            return _fail("outline", label, "no outline available (Go, Python, TypeScript and JavaScript files only)")
        return _result("outline", label, text=f"[outline of {path}: line: declaration]\n" + "\n".join(lines))

    # --- rendering ---------------------------------------------------------------------------

    @staticmethod
    def _numbered(path: str, lines: List[str], spans: List[Tuple[int, int]], summary: List[str]) -> str:
        """Outline plus the numbered selected spans (merged when they touch)."""
        parts: List[str] = []
        if summary:
            parts.append(f"[outline of {path}: line: declaration]")
            parts.extend(summary)
        merged: List[List[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            end = min(end, len(lines))
            parts.append(f"[lines {start}-{end}]")
            parts.extend(f"{n}| {lines[n - 1]}" for n in range(start, end + 1))
        return "\n".join(parts)


def describe_request(request: Any) -> Dict[str, str]:
    """{"op", "target"} for events and records, without any file content."""
    if not isinstance(request, dict):
        return {"op": "?", "target": "?"}
    op = str(request.get("op", "?"))[:40]
    if op == "grep":
        target = str(request.get("pattern", ""))[:120] + (f" in {request['glob']}" if request.get("glob") else "")
    elif op in ("defs", "refs", "callers"):
        target = str(request.get("symbol", ""))[:120] + (f" in {request['glob']}" if request.get("glob") else "")
    elif op == "callees":
        target = str(request.get("symbol", ""))[:120]
    elif op in ("list", "map"):
        target = str(request.get("path") or ".")[:120]
    elif op == "wiki":
        target = str(request.get("page") or request.get("query") or "")[:120]
    else:
        target = str(request.get("path", ""))[:120]
    return {"op": op, "target": target}

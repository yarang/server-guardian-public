"""
Code wiki, layer 2 (docs/design/debate-code-index-wiki.md, "2층. 코드 위키").

Pages are navigation notes about a directory or feature of a project's repository, kept in the hub's data directory
(<GUARDIAN_DATA_DIR>/code-wiki/<project>/pages/<id>.md), never in the target repository. A page is a front matter
(below) plus the narrative; the generated layer (the repository map of the page's scope) is not stored but made
when the page is read. Nothing a page says is evidence: a wiki result provides no file, no line span and no
mention to the citation check (repo_tools.provided_paths / provided_spans / mentioned_in).

Front matter: a restricted YAML subset, parsed and written here without a YAML library. The document starts with a
line `---` and the front matter ends at the next line that is exactly `---`; the narrative follows.

    key: value            value = one JSON value on the line (string, number, list, object, true/false/null), or
                          a bare token of the characters A-Za-z0-9_.@/+:- (a string unless it reads as a number,
                          true, false or null)
    key:                  followed by indented lines `  "<string>": "<string>"` (a block mapping of strings; the
                          writer quotes both sides, the reader also accepts bare tokens)
    # comment             a whole-line comment (ignored); blank lines are ignored

Anything else is a FrontMatterError. The writer emits every string JSON-quoted (ensure_ascii=False), so a value never
contains a raw line break, and lines are split on "\\n" only.

Staleness (page_state) is computed against a sha every time and never stored:
  fresh   every basis blob is unchanged and nothing cited is missing;
  stale   a basis file changed or was removed, or a readable file in scope that is not in the basis is new
          since the page was written (absent from the tree at written_sha);
  broken  a cited path is missing, a `path#Symbol` citation names a symbol the code index (or, for Go, the
          compiler-assisted facts) cannot find, or citations were still unverified when the page was written.
"""

import contextlib
import datetime
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from code_context import ContextError, _git
from code_index import language
from evidence import parse_location
from repo_map import build_map

try:  # POSIX only; the hub runs on Linux and macOS
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

PAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY_BYTES = 256 * 1024
MAX_TITLE_CHARS = 200
MAX_SCOPE_ENTRIES = 60  # a feature page lists its design document and the files it names
MAX_BASIS_FILES = 400
MAX_UNVERIFIED = 50
MAX_UNVERIFIED_CHARS = 300
MAX_PAGE_FILE_BYTES = MAX_BODY_BYTES + 256 * 1024
LIST_LIMIT = 20  # pages a query lists
FIRST_PROMPT_LINES = 40  # fresh pages listed in the first prompt
DETAIL_LIMIT = 100  # entries per list in the staleness details
SHOWN_LIMIT = 20  # entries per list in the warning text
NARRATIVE_RESULT_BYTES = 48 * 1024  # the narrative part of one wiki op result
GENERATED_MAP_BYTES = 12 * 1024
GENERATED_MAP_DEPTH = 3
SYMBOL_LOOKUPS = 200  # symbol citations of one page that are checked against the index
SHA = re.compile(r"^[0-9a-f]{7,64}$")
BLOB = re.compile(r"^[0-9a-f]{40,64}$")
UPDATED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
USER = re.compile(r"^[A-Za-z0-9@:._-]{1,64}$")
_PROJECT_ID = re.compile(r"[^A-Za-z0-9._-]")
AUTHOR_KINDS = ("llm", "human")
# Page kinds (docs/design/code-wiki-coverage.md): a directory page; the overview page of a directory split into group
# pages (`<id>--<name>`); the feature page of one design document. The kind is absent from the front matter of a dir
# page, so pages written before kinds existed read the same.
PAGE_KINDS = ("dir", "overview", "group", "feature")
KIND_ORDER = {"dir": 0, "overview": 0, "feature": 1, "group": 2}  # order of the fresh pages in the first prompt
GROUP_SEPARATOR = "--"  # a group page id is `<overview id>--<name>`
MAX_DOCS = 20


class WikiError(ValueError):
    """A page or request that is invalid (the CLI maps it to exit code 2)."""


class PageNotFound(LookupError):
    """No page with that id (exit code 3)."""


class BuildBusy(RuntimeError):
    """Another build holds the project's lock (exit code 4)."""


class FrontMatterError(WikiError):
    """The front matter is not in the supported subset."""


# --- location and switches -----------------------------------------------------------------------

def wiki_enabled() -> bool:
    """Whether debate agents see the wiki (a list of fresh pages in the prompt and the wiki op).

    Off by default since 2026-10-18: in evaluation 2 agents never opened a listed page and the wiki changed neither
    correctness nor cost (docs/analysis/2026-10-13-retrieval-eval2/results.md), so it serves people through the
    dashboard. GUARDIAN_CODE_WIKI=1 (also true/on/yes) turns the debate exposure back on.
    """
    return os.environ.get("GUARDIAN_CODE_WIKI", "0").strip().lower() in ("1", "true", "on", "yes")


def data_root() -> Path:
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()


def default_wiki_dir(project_id: Optional[str]) -> Optional[Path]:
    """<GUARDIAN_DATA_DIR>/code-wiki/<project>, or None when the project id is unusable. Creates nothing.

    Unlike code-index/ this directory is user content and stays in the nightly data tarball (db_backup).
    """
    cleaned = _PROJECT_ID.sub("_", str(project_id or "")).strip(".")
    if not cleaned:
        return None
    return data_root() / "code-wiki" / cleaned[:80]


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- front matter --------------------------------------------------------------------------------

_KEY = re.compile(r"^([a-z][a-z0-9_]*):(?:[ \t]+(.*))?$")
_BARE = re.compile(r"^[A-Za-z0-9_.@/+:-]+$")  # a bare value may also hold ":" (2026-10-12T09:00:00Z)
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def _parse_value(raw: str, where: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise FrontMatterError(f"{where}: empty value")
    if raw[0] in '"[{' or _NUMBER.match(raw) or raw in ("true", "false", "null"):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise FrontMatterError(f"{where}: not a JSON value ({exc})") from exc
    if _BARE.match(raw):
        return raw
    raise FrontMatterError(f"{where}: unsupported value (quote strings with double quotes)")


def _parse_scalar_prefix(raw: str, where: str) -> Tuple[str, str]:
    """A quoted or bare string at the start of raw and the rest after it."""
    if raw.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except ValueError as exc:
            raise FrontMatterError(f"{where}: bad quoted string ({exc})") from exc
        if not isinstance(value, str):
            raise FrontMatterError(f"{where}: expected a string")
        return value, raw[end:]
    found = re.match(r"[A-Za-z0-9_.@/+-]+", raw)
    if not found:
        raise FrontMatterError(f"{where}: expected a string")
    return found.group(0), raw[found.end():]


def _parse_mapping_line(line: str, where: str) -> Tuple[str, str]:
    key, rest = _parse_scalar_prefix(line.strip(), where)
    rest = rest.lstrip()
    if not rest.startswith(":"):
        raise FrontMatterError(f"{where}: expected `key: value`")
    value, tail = _parse_scalar_prefix(rest[1:].lstrip(), where)
    if tail.strip():
        raise FrontMatterError(f"{where}: unexpected text after the value")
    return key, value


def parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """Splits a page file into (front matter fields, narrative). Raises FrontMatterError."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise FrontMatterError("the file must start with a line `---`")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r") == "---"), None)
    if end is None:
        raise FrontMatterError("the front matter has no closing `---` line")
    fields: Dict[str, Any] = {}
    current: Optional[str] = None
    for number, raw in enumerate(lines[1:end], 2):
        line = raw.rstrip("\r")
        where = f"line {number}"
        if not line.strip() or line.startswith("#"):
            continue
        if line[0] in " \t":
            if current is None or not isinstance(fields.get(current), dict):
                raise FrontMatterError(f"{where}: unexpected indented line")
            key, value = _parse_mapping_line(line, where)
            if key in fields[current]:
                raise FrontMatterError(f"{where}: duplicate key {key!r}")
            fields[current][key] = value
            continue
        found = _KEY.match(line)
        if not found:
            raise FrontMatterError(f"{where}: expected `key: value`")
        key, raw_value = found.group(1), found.group(2)
        if key in fields:
            raise FrontMatterError(f"{where}: duplicate key {key!r}")
        if raw_value is None or not raw_value.strip():
            fields[key] = {}  # a block mapping follows
            current = key
        else:
            fields[key] = _parse_value(raw_value, where)
            current = None
    body = "\n".join(lines[end + 1:])
    return fields, body


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_front_matter(fields: Dict[str, Any]) -> str:
    """The `---` delimited front matter for fields (dict values that are all strings become block mappings)."""
    out = ["---"]
    for key, value in fields.items():
        if not _KEY.match(f"{key}:"):
            raise FrontMatterError(f"bad key {key!r}")
        if key == "basis" and isinstance(value, dict) and value:
            out.append(f"{key}:")
            out.extend(f"  {_quote(k)}: {_quote(v)}" for k, v in value.items())
        else:
            out.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    out.append("---")
    return "\n".join(out) + "\n"


# --- pages ---------------------------------------------------------------------------------------

def normalize_body(body: str) -> str:
    return body.replace("\r\n", "\n").strip("\n") + "\n"


def _norm_path(raw: Any) -> Optional[str]:
    from repo_tools import normalize_path  # late: repo_tools imports this module

    return normalize_path(raw)


@dataclass
class Page:
    id: str
    title: str
    scope: List[str]
    written_sha: str
    basis: Dict[str, str]
    author: Dict[str, str]
    updated: str
    body: str
    prompt_version: str = ""
    unverified: List[str] = field(default_factory=list)
    kind: str = "dir"
    docs: List[str] = field(default_factory=list)  # other documents a feature page's design document names

    @property
    def human(self) -> bool:
        return self.author.get("kind") == "human"

    def validate(self) -> None:
        """Raises WikiError when a field is out of bounds (everything a save or a load relies on)."""
        if not isinstance(self.id, str) or not PAGE_ID.match(self.id):
            raise WikiError("page id must match ^[a-z0-9][a-z0-9-]{0,63}$")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > MAX_TITLE_CHARS \
                or any(c in self.title for c in "\r\n\0"):
            raise WikiError(f"title must be one line of 1-{MAX_TITLE_CHARS} characters")
        if not isinstance(self.scope, list) or not 1 <= len(self.scope) <= MAX_SCOPE_ENTRIES:
            raise WikiError(f"scope must list 1-{MAX_SCOPE_ENTRIES} paths")
        for entry in self.scope:
            if not isinstance(entry, str) or not entry or len(entry) > 1024 or any(c in entry for c in "\r\n\0"):
                raise WikiError("scope entries must be non-empty one-line paths")
        if not isinstance(self.written_sha, str) or not SHA.match(self.written_sha):
            raise WikiError("written_sha must be a hex commit id")
        if not isinstance(self.basis, dict) or len(self.basis) > MAX_BASIS_FILES:
            raise WikiError(f"basis must map at most {MAX_BASIS_FILES} paths to blob ids")
        for path, blob in self.basis.items():
            if not isinstance(path, str) or not path or len(path) > 1024 or any(c in path for c in "\r\n\0") \
                    or not isinstance(blob, str) or not BLOB.match(blob):
                raise WikiError("basis entries must be `path: blob id`")
        author = self.author
        if not isinstance(author, dict) or author.get("kind") not in AUTHOR_KINDS:
            raise WikiError("author.kind must be llm or human")
        if author["kind"] == "human":
            if not isinstance(author.get("user"), str) or not USER.match(author["user"]):
                raise WikiError("author.user must be 1-64 letters, digits or @:._-")
        else:
            for key in ("provider", "job"):
                value = author.get(key)
                if not isinstance(value, str) or len(value) > 128 or any(c in value for c in "\r\n\0"):
                    raise WikiError(f"author.{key} must be a short one-line string")
        if not isinstance(self.updated, str) or not UPDATED.match(self.updated):
            raise WikiError("updated must be a UTC time like 2026-10-12T09:00:00Z")
        if not isinstance(self.prompt_version, str) or len(self.prompt_version) > 64 or "\n" in self.prompt_version:
            raise WikiError("prompt_version must be a short one-line string")
        if not isinstance(self.unverified, list) or len(self.unverified) > MAX_UNVERIFIED or not all(
                isinstance(u, str) and len(u) <= MAX_UNVERIFIED_CHARS and "\n" not in u for u in self.unverified):
            raise WikiError(f"unverified must list at most {MAX_UNVERIFIED} one-line citations")
        if self.kind not in PAGE_KINDS:
            raise WikiError(f"kind must be one of {', '.join(PAGE_KINDS)}")
        if not isinstance(self.docs, list) or len(self.docs) > MAX_DOCS or not all(
                isinstance(d, str) and d and len(d) <= 1024 and not any(c in d for c in "\r\n\0") for d in self.docs):
            raise WikiError(f"docs must list at most {MAX_DOCS} one-line paths")
        if not isinstance(self.body, str) or "\0" in self.body:
            raise WikiError("body must be text")
        if len(self.body.encode("utf-8")) > MAX_BODY_BYTES:
            raise WikiError(f"body is over {MAX_BODY_BYTES // 1024} KB")

    def to_text(self) -> str:
        self.validate()
        fields: Dict[str, Any] = {
            "id": self.id, "title": self.title, "scope": self.scope, "written_sha": self.written_sha,
            "basis": dict(self.basis), "author": self.author, "updated": self.updated,
            "prompt_version": self.prompt_version, "unverified": self.unverified,
        }
        if self.kind != "dir":
            fields["kind"] = self.kind
        if self.docs:
            fields["docs"] = list(self.docs)
        return dump_front_matter(fields) + normalize_body(self.body)

    @classmethod
    def from_text(cls, text: str) -> "Page":
        fields, body = parse_front_matter(text)
        author = fields.get("author")
        page = cls(
            id=fields.get("id"), title=fields.get("title"), scope=fields.get("scope"),  # type: ignore[arg-type]
            written_sha=fields.get("written_sha"), basis=fields.get("basis", {}),  # type: ignore[arg-type]
            author=author, updated=fields.get("updated"),  # type: ignore[arg-type]
            body=body, prompt_version=fields.get("prompt_version", ""), unverified=fields.get("unverified", []),
            kind=fields.get("kind", "dir"), docs=fields.get("docs", []),  # type: ignore[arg-type]
        )
        page.validate()
        return page

    def meta(self) -> Dict[str, Any]:
        """Everything but the narrative."""
        return {"id": self.id, "title": self.title, "scope": list(self.scope), "author": dict(self.author),
                "updated": self.updated, "written_sha": self.written_sha, "prompt_version": self.prompt_version,
                "basis": dict(self.basis), "unverified": list(self.unverified), "kind": self.kind,
                "docs": list(self.docs)}


# --- storage -------------------------------------------------------------------------------------

def _private_dir(path: Path) -> None:
    """Creates path with mode 0700 (missing parents too); a directory that already exists is left as it is."""
    if path.is_dir():
        return
    if not path.parent.is_dir():
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


class WikiStore:
    """The pages of one project: <directory>/pages/<id>.md, written atomically with mode 0600 in 0700 directories."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.pages_dir = self.directory / "pages"

    def _path(self, page_id: str) -> Path:
        if not isinstance(page_id, str) or not PAGE_ID.match(page_id):
            raise WikiError("page id must match ^[a-z0-9][a-z0-9-]{0,63}$")
        return self.pages_dir / f"{page_id}.md"

    def ids(self) -> List[str]:
        try:
            names = os.listdir(self.pages_dir)
        except OSError:
            return []
        return sorted(n[:-3] for n in names if n.endswith(".md") and PAGE_ID.match(n[:-3]))

    def load(self, page_id: str) -> Page:
        path = self._path(page_id)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise PageNotFound(f"no wiki page {page_id!r}") from exc
        except OSError as exc:
            raise WikiError(f"cannot read page {page_id!r}: {exc.strerror or exc}") from exc
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_PAGE_FILE_BYTES + 1)
        if len(raw) > MAX_PAGE_FILE_BYTES:
            raise WikiError(f"page {page_id!r} is over the size limit")
        try:
            page = Page.from_text(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise WikiError(f"page {page_id!r} is not UTF-8") from exc
        if page.id != page_id:
            raise WikiError(f"page file {page_id}.md declares id {page.id!r}")
        return page

    def load_all(self) -> Tuple[List[Page], List[Dict[str, str]]]:
        """(pages sorted by id, [{id, error}] for files that cannot be read)."""
        pages: List[Page] = []
        errors: List[Dict[str, str]] = []
        for page_id in self.ids():
            try:
                pages.append(self.load(page_id))
            except (WikiError, PageNotFound) as exc:
                errors.append({"id": page_id, "error": str(exc)})
        return pages, errors

    def save(self, page: Page) -> None:
        text = page.to_text()  # validates
        path = self._path(page.id)
        _private_dir(self.directory.parent)
        _private_dir(self.directory)
        _private_dir(self.pages_dir)
        fd, tmp = tempfile.mkstemp(dir=self.pages_dir, prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def delete(self, page_id: str) -> bool:
        try:
            os.unlink(self._path(page_id))
        except FileNotFoundError:
            return False
        return True

    @contextlib.contextmanager
    def build_lock(self) -> Iterator[None]:
        """Held for a whole build; a second build of the project raises BuildBusy."""
        _private_dir(self.directory.parent)
        _private_dir(self.directory)
        fd = os.open(self.directory / "build.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise BuildBusy("another code wiki build is running for this project") from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield
        finally:
            os.close(fd)  # closing releases the flock


# --- citations of a body -------------------------------------------------------------------------

_TOKEN = re.compile(r"[\w.\-/]+")
_EXT = re.compile(r"\.([A-Za-z][A-Za-z0-9]{0,7})$")


def _top(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else ""


def _tree_facts(tools: Any, extra: List[str]) -> Dict[str, Any]:
    """Extensions, top-level directories and basenames of the readable tree (and of extra paths); cached on tools."""
    cache = tools.__dict__.setdefault("_wiki_tree_facts", {})
    key = tuple(sorted(extra))
    if key not in cache:
        paths = list(tools.readable_paths()) + list(extra)
        exts = {m.group(1) for p in paths for m in [_EXT.search(p)] if m}
        cache[key] = {"exts": exts, "tops": {_top(p) for p in paths if "/" in p},
                      "names": {p.rsplit("/", 1)[-1] for p in paths}}
    return cache[key]


def _citation_text(path: str, where: Any) -> str:
    if where is None:
        return path
    if where[0] == "symbol":
        return f"{path}#{where[1]}"
    return f"{path}:{where[1]}" if where[1] == where[2] else f"{path}:{where[1]}-{where[2]}"


def symbol_exists(tools: Any, path: str, symbol: str) -> bool:
    """Whether `path#symbol` can be found: by the code index, by the language's declaration parser, or (Go) by the
    compiler-assisted facts when they are available. A language without a parser cannot be checked: True."""
    if language(path) is None:
        return True
    try:
        if any(d["path"] == path for d in tools.code_index().find_defs(symbol)):
            return True
        from code_select import symbol_spans

        source = tools.source_text(path)
        if source is not None and symbol_spans(path, source, symbol):
            return True
        provider = getattr(tools, "go_provider", None)
        if path.endswith(".go") and provider is not None and provider.has_go():
            facts = provider.peek()
            if facts is not None and any(d["path"] == path for d in facts.find_defs(symbol)):
                return True
        return False
    except (ContextError, OSError, ValueError, RecursionError):
        return True  # cannot tell: do not call it broken


def broken_citations(body: str, tools: Any, basis: List[str], scope: List[str]) -> List[Dict[str, str]]:
    """Citations of the body that no longer hold: [{citation, reason}], at most DETAIL_LIMIT.

    A token is a citation when it is a readable path, or when it names a path that is not in the tree and looks like one:
    its extension occurs among the repository's files and it is either cited strongly (`:N`, `#Symbol`, "line N") or has
    a directory part whose first component is a known top-level directory (unless, without a location, it is a path
    relative to one of the page's scope directories that exists). A strong citation with a bare file name
    (`auth.go#Check`) that is not in the tree counts unless some other directory has a file of that name (a sloppy
    but resolvable reference). Readable paths cited as `path#Symbol` must still have the symbol.
    """
    facts = _tree_facts(tools, list(basis))
    tops = facts["tops"] | {_top(s) for s in scope if "/" in s}
    tops |= {s.strip("/") for s in scope if s.strip("/") and "/" not in s.strip("/")}
    found: Dict[str, str] = {}
    lookups = 0
    symbols: Dict[Tuple[str, str], bool] = {}
    for match in _TOKEN.finditer(body or ""):
        raw = match.group(0)
        if raw.startswith("//"):
            continue  # the rest of a URL
        token = raw.rstrip(".,-")
        while token.startswith("./"):
            token = token[2:]
        token = token.lstrip("/")
        if not token:
            continue
        where = parse_location(body, match.end())
        if tools.readable(token):
            if where is not None and where[0] == "symbol" and lookups < SYMBOL_LOOKUPS:
                lookups += 1
                key = (token, where[1])
                if key not in symbols:
                    symbols[key] = symbol_exists(tools, token, where[1])
                if not symbols[key]:
                    found.setdefault(_citation_text(token, where), "symbol not found")
            continue
        ext = _EXT.search(token)
        if not ext or ext.group(1) not in facts["exts"]:
            continue
        if where is None:
            if "/" not in token or _top(token) not in tops:
                continue
            # A plain mention relative to a scope directory (`scripts/build-ui.mjs` on the page of apps/web-desktop/)
            # names a real file; only citations with a location must carry the full path.
            if any(tools.readable(entry + token) for entry in scope if entry.endswith("/") and entry != "./"):
                continue
        elif "/" not in token and token in facts["names"]:
            continue
        found.setdefault(_citation_text(token, where), "path not found")
    return [{"citation": c, "reason": r} for c, r in list(found.items())[:DETAIL_LIMIT]]


def strong_citation_paths(body: str, tools: Any) -> List[str]:
    """Readable paths the body cites strongly (path:N, path:N-M, path#Symbol, "line N"), in order of appearance."""
    return [path for path, (strong, _where) in tools.cited_locations(body).items() if strong]


# --- staleness -----------------------------------------------------------------------------------

def in_scope(path: str, scope: List[str]) -> bool:
    for entry in scope:
        if entry in ("./", "/", "."):
            return True
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry or path.startswith(entry + "/"):
            return True
    return False


def paths_at(tools: Any, sha: str) -> Optional[Set[str]]:
    """Every path of the tree at sha, or None when that commit is not available. Cached on tools."""
    cache = tools.__dict__.setdefault("_wiki_trees", {})
    if sha not in cache:
        if sha == tools.sha:
            cache[sha] = set(tools.entries)
        else:
            try:
                raw = _git(tools.workspace, "ls-tree", "-r", "--name-only", "-z", "--full-tree", sha)
                cache[sha] = {p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p}
            except (ContextError, OSError):
                cache[sha] = None
    return cache[sha]


def _more(items: List[str], limit: int = SHOWN_LIMIT) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def page_state(page: Page, tools: Any) -> Dict[str, Any]:
    """Staleness of a page at tools.sha: {state, sha, written_sha, changed, removed, new, broken, unverified, notes}."""
    changed: List[str] = []
    removed: List[str] = []
    for path, blob in page.basis.items():
        entry = tools.entries.get(path)
        if not tools.readable(path):
            removed.append(path)
        elif entry is not None and entry[2] != blob:
            changed.append(path)
    notes: List[str] = []
    new: List[str] = []
    candidates = [p for p in tools.readable_paths() if in_scope(p, page.scope) and p not in page.basis]
    if candidates and page.written_sha != tools.sha:
        old = paths_at(tools, page.written_sha)
        if old is None:
            notes.append(f"commit {page.written_sha[:7]} is not available, so new files could not be detected")
        else:
            new = [p for p in candidates if p not in old]
    broken = broken_citations(page.body, tools, list(page.basis), page.scope)
    unverified = list(page.unverified)
    if broken or unverified:
        state = "broken"
    elif changed or removed or new:
        state = "stale"
    else:
        state = "fresh"
    return {"state": state, "sha": tools.sha, "written_sha": page.written_sha, "changed": changed[:DETAIL_LIMIT],
            "removed": removed[:DETAIL_LIMIT], "new": new[:DETAIL_LIMIT], "broken": broken, "unverified": unverified,
            "notes": notes}


def warning_lines(details: Dict[str, Any]) -> List[str]:
    """The staleness block of a rendered page (empty for a fresh one)."""
    if details["state"] == "fresh":
        return []
    lines = [f"WARNING: this page is {details['state']}: check it against the code before relying on it."]
    if details["changed"]:
        lines.append("These files changed after this narrative was written: " + _more(details["changed"]))
    if details["removed"]:
        lines.append("These files it was based on no longer exist or are no longer readable: "
                     + _more(details["removed"]))
    if details["new"]:
        lines.append("Files in its scope that it does not cover: " + _more(details["new"]))
    if details["broken"]:
        lines.append("Broken citations: " + _more([f"{b['citation']} ({b['reason']})" for b in details["broken"]]))
    if details["unverified"]:
        lines.append("The writer could not verify these citations: " + _more(details["unverified"]))
    lines.extend(details["notes"])
    return lines


# --- generated layer and rendering ---------------------------------------------------------------

def generated_layer(page: Page, tools: Any) -> str:
    """The repository map of each scope entry at tools.sha (regenerated on every read; never stored)."""
    parts: List[str] = []
    for entry in page.scope:
        if entry in ("./", "/", "."):
            parts.append(build_map(tools, "", depth=GENERATED_MAP_DEPTH, cap=GENERATED_MAP_BYTES))
        elif entry.endswith("/"):
            if any(p.startswith(entry) for p in tools.readable_paths()):
                parts.append(build_map(tools, entry, depth=GENERATED_MAP_DEPTH, cap=GENERATED_MAP_BYTES))
            else:
                parts.append(f"{entry}  (no readable files at this commit)")
        else:
            outline = tools.outline(entry)
            parts.append(outline["text"] if outline["ok"] else f"{entry}  (file)")
    return "\n".join(parts)


def author_text(author: Dict[str, str]) -> str:
    if author.get("kind") == "human":
        return f"a person ({author.get('user')})"
    return f"an LLM session ({author.get('provider')})"


def render_page(page: Page, tools: Any, details: Dict[str, Any]) -> Tuple[str, bool]:
    """(text of the wiki op result for one page, whether the narrative was cut)."""
    lines = [f"[wiki page {page.id} — navigation notes, not evidence; state: {details['state']}]"]
    lines.extend(warning_lines(details))
    lines.append(f"--- Generated overview of {', '.join(page.scope)} (sha {tools.sha[:7]}; made from the current "
                 "code just now, not evidence) ---")
    lines.append(generated_layer(page, tools))
    lines.append(f"--- Narrative: {page.title} (written at sha {page.written_sha[:7]} by {author_text(page.author)}, "
                 f"{page.updated}; notes that may be outdated and are not evidence: read the code and cite it, "
                 "not this page) ---")
    body = page.body
    cut = False
    data = body.encode("utf-8")
    if len(data) > NARRATIVE_RESULT_BYTES:
        body = data[:NARRATIVE_RESULT_BYTES].decode("utf-8", errors="ignore") + "\n[... page narrative cut by Guardian ...]"
        cut = True
    lines.append(body.rstrip("\n"))
    return "\n".join(lines), cut


class WikiView:
    """The pages of a project seen at one commit: states, the first-prompt list and the text of the wiki op."""

    def __init__(self, store: WikiStore, tools: Any) -> None:
        self.store = store
        self.tools = tools
        self._loaded: Optional[Tuple[List[Page], List[Dict[str, str]]]] = None
        self._details: Dict[str, Dict[str, Any]] = {}

    @property
    def pages(self) -> List[Page]:
        if self._loaded is None:
            self._loaded = self.store.load_all()
        return self._loaded[0]

    @property
    def load_errors(self) -> List[Dict[str, str]]:
        self.pages
        assert self._loaded is not None
        return self._loaded[1]

    def details(self, page: Page) -> Dict[str, Any]:
        if page.id not in self._details:
            self._details[page.id] = page_state(page, self.tools)
        return self._details[page.id]

    def states(self) -> Dict[str, str]:
        return {p.id: self.details(p)["state"] for p in self.pages}

    @staticmethod
    def _line_text(text: str, limit: int = 120) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def first_prompt(self) -> Tuple[str, List[str], int, int]:
        """(prompt section, ids listed, pages, pages not fresh). The section is "" when no page is fresh."""
        states = self.states()
        fresh = sorted((p for p in self.pages if states[p.id] == "fresh"), key=lambda p: KIND_ORDER.get(p.kind, 9))
        total, not_fresh = len(self.pages), len(self.pages) - len(fresh)
        if not fresh:
            return "", [], total, not_fresh
        shown = fresh[:FIRST_PROMPT_LINES]
        lines = [
            "=== Code wiki (navigation notes) ===",
            "The pages below were written by earlier sessions or by people. The wiki is a guide to where to look, not "
            "evidence: do not cite it, cite code you have read yourself, and where the wiki disagrees with the code "
            "the code is right. Open a page with "
            '{"op": "wiki", "page": "<id>"} (or the mcp__repo__repo_wiki tool), search with '
            '{"op": "wiki", "query": "<text>"}. Pages that are fresh at this commit:',
        ]
        for page in shown:
            lines.append(f"{page.id} — {self._line_text(page.title)} ({self._line_text(', '.join(page.scope), 160)})")
        if len(fresh) > len(shown):
            lines.append(f"... {len(fresh) - len(shown)} more fresh pages (search with the wiki op)")
        if not_fresh:
            lines.append(f"{not_fresh} more pages are stale or broken: a wiki search lists them with their state; "
                         "check them against the code.")
        lines.append("=== End of code wiki ===")
        return "\n".join(lines), [p.id for p in shown], total, not_fresh

    def page_text(self, page_id: str) -> Tuple[str, bool]:
        page = self.store.load(page_id)  # fresh from disk: a build may have rewritten it
        self._details.pop(page.id, None)
        return render_page(page, self.tools, self.details(page))

    def search_text(self, query: str) -> str:
        needle = query.strip().lower()
        hits = [p for p in self.pages
                if not needle or needle in p.title.lower() or needle in " ".join(p.scope).lower()
                or needle in p.body.lower() or needle in p.id]
        if not self.pages:
            return "[wiki: no pages — navigation notes, not evidence]"
        label = f'search "{self._line_text(query, 80)}"' if needle else "pages"
        lines = [f"[wiki {label}: {len(hits)} of {len(self.pages)} pages — navigation notes, not evidence]"]
        for page in hits[:LIST_LIMIT]:
            lines.append(f"{page.id}  [{self.details(page)['state']}]  {self._line_text(page.title)}  "
                         f"({self._line_text(', '.join(page.scope), 160)})")
        if len(hits) > LIST_LIMIT:
            lines.append(f"... {len(hits) - LIST_LIMIT} more (narrow the query)")
        return "\n".join(lines)

    def listing(self) -> List[Dict[str, Any]]:
        """The page list of the CLI: id, title, scope, author, updated, state, kind, and for the viewer a one-line
        summary and the ids of the other listed pages the narrative links as `[[id]]`."""
        known = {p.id for p in self.pages}
        return [{"id": p.id, "title": p.title, "scope": list(p.scope), "author": dict(p.author),
                 "updated": p.updated, "state": self.details(p)["state"], "kind": p.kind,
                 "summary": page_summary(p.body), "links": page_links(p.body, p.id, known)} for p in self.pages]


# --- what the viewer shows of a page without opening it ------------------------------------------

SUMMARY_CHARS = 160
MAX_PAGE_LINKS = 60
_WIKILINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,63})\]\]")
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_FENCE_LINE = re.compile(r"^\s{0,3}(```|~~~)")
_LINE_CITATION = re.compile(r"`[^`\s]+:\d+(?:-\d+)?`")  # `path/file.go:12` or `path/file.go:12-20`
_EMPTY_PARENS = re.compile(r"\(\s*[^\w\s()]*\s*\)")  # "()" left behind by removed citations


def _prose_lines(body: str) -> Iterator[str]:
    """Lines of a page body outside fenced code blocks."""
    fenced = False
    for line in body.split("\n"):
        if _FENCE_LINE.match(line):
            fenced = not fenced
        elif not fenced:
            yield line


def page_summary(body: str, limit: int = SUMMARY_CHARS) -> str:
    """The first paragraph of the first section of a page, as one plain line of at most `limit` characters.

    Headings differ by language and by page kind, so the first section is taken whatever it is called. Line citations
    (`path:12-20`, which the writer puts on every claim) are left out, as are backticks and `[[id]]` brackets; a
    page without prose gives ""."""
    paragraph: List[str] = []
    for line in _prose_lines(body):
        if _HEADING_LINE.match(line):
            if paragraph:
                break
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip().lstrip("-*+ ").strip())
    text = _LINE_CITATION.sub("", " ".join(paragraph))
    previous = None
    while previous != text:  # nested parentheses empty out from the inside
        previous, text = text, _EMPTY_PARENS.sub("", text)
    text = " ".join(text.split())
    text = re.sub(r"\s+([.,;:!?])", r"\1", _WIKILINK.sub(lambda m: m.group(1), text)).replace("`", "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def page_links(body: str, own_id: str, known: Set[str]) -> List[str]:
    """Ids of the other listed pages the body links as `[[id]]`, in order of first appearance."""
    out: List[str] = []
    for line in _prose_lines(body):
        for match in _WIKILINK.finditer(line):
            target = match.group(1)
            if target != own_id and target in known and target not in out:
                out.append(target)
                if len(out) >= MAX_PAGE_LINKS:
                    return out
    return out


# --- saving a page by hand -----------------------------------------------------------------------

def normalize_scope(tools: Any, raw: Any) -> List[str]:
    """Scope entries as stored: directories with a trailing slash, files as they are. Every entry must be a readable
    directory or file of the commit; raises WikiError otherwise."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SCOPE_ENTRIES or not all(isinstance(s, str) for s in raw):
        raise WikiError(f"scope must be a list of 1-{MAX_SCOPE_ENTRIES} paths")
    readable = tools.readable_paths()
    out: List[str] = []
    for entry in raw:
        if entry.strip() in (".", "./", "/"):
            norm = ""
        else:
            norm = _norm_path(entry)
            if norm is None or not norm:
                raise WikiError(f"invalid scope path {entry[:80]!r}")
        if norm == "":
            item = "./"
        elif tools.readable(norm):
            item = norm
        elif any(p.startswith(norm + "/") for p in readable):
            item = norm + "/"
        else:
            raise WikiError(f"scope path {norm[:80]!r} is not a readable file or directory at {tools.sha[:7]}")
        if item not in out:
            out.append(item)
    return out


def basis_for(tools: Any, paths: List[str]) -> Dict[str, str]:
    """{path: blob id} of the readable paths (at most MAX_BASIS_FILES, in the order given)."""
    out: Dict[str, str] = {}
    for path in paths:
        if tools.readable(path) and path not in out and len(out) < MAX_BASIS_FILES:
            out[path] = tools.entries[path][2]
    return out


def save_human_page(store: WikiStore, tools: Any, payload: Any, user: str, now: Optional[str] = None) -> Page:
    """A page saved from an editor: {id, title, scope, body}. The server writes the front matter: author is the
    user, written_sha the commit the paths were checked against, and basis the blobs of the files the body cites
    strongly. Raises WikiError."""
    if not isinstance(payload, dict):
        raise WikiError("expected a JSON object {id, title, scope, body}")
    unknown = set(payload) - {"id", "title", "scope", "body"}
    if unknown:
        raise WikiError(f"unknown fields: {', '.join(sorted(map(str, unknown))[:5])}")
    body = payload.get("body")
    if not isinstance(body, str):
        raise WikiError("body must be text")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise WikiError(f"body is over {MAX_BODY_BYTES // 1024} KB")
    title = payload.get("title")
    title = title.strip() if isinstance(title, str) else title
    try:  # a person's edit keeps what kind of page it is
        existing: Optional[Page] = store.load(payload.get("id")) if isinstance(payload.get("id"), str) else None
    except (PageNotFound, WikiError):
        existing = None
    page = Page(
        id=payload.get("id"), title=title, scope=normalize_scope(tools, payload.get("scope")),  # type: ignore[arg-type]
        written_sha=tools.sha, basis=basis_for(tools, strong_citation_paths(body, tools)),
        author={"kind": "human", "user": user}, updated=now or utc_now(), body=normalize_body(body),
        kind=existing.kind if existing else "dir", docs=list(existing.docs) if existing else [],
    )
    page.validate()
    store.save(page)
    return page

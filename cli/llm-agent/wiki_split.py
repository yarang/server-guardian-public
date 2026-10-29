"""
Splitting large directory scopes of the code wiki (docs/design/code-wiki-coverage.md, B), without an LLM.

A default directory page whose scope is over the limit (more than SPLIT_FILES code files or SPLIT_BYTES bytes; tests,
stories and previews do not count) becomes an overview page with the same id, and its code is covered by group pages
`<dir id>--<name>`:

1. Subdirectories with code are split first (each is a group, or split again when it is over the limit, down to
   MAX_DEPTH levels below the page's directory).
2. Code files directly in a split directory are grouped by their reference graph: A uses B when A references a name
   declared only in B (same language, and A imports B; see file_view.imports). Hub files, linked to at least a third
   of the files (and to at least 3), form a `core` group. The others are merged by average linkage within the limit
   (Splitter.cluster); files left alone go to a `misc` group. A group is named after its most used file.

Everything here is deterministic for a commit: the same tree gives the same groups.
"""

import os
from typing import Any, Dict, List, Optional, Set, Tuple

from code_select import language
from file_view import imports
from repo_map import is_noise_path

SPLIT_FILES = 15
SPLIT_BYTES = 200 * 1024
MAX_DEPTH = 4
MIN_GROUP_FILES = 2
HUB_SHARE = 3  # a file used by at least 1/HUB_SHARE of the directory's files is a hub
MIN_HUB_USERS = 3
MIN_GROUP_BYTES = 2048  # smaller groups are left to the overview page
SMALL_SHARE = 8  # groups under max_bytes / SMALL_SHARE are packed with their neighbours
MAX_SCOPE = 60  # code_wiki.MAX_SCOPE_ENTRIES
MIN_LINKAGE = 0.25  # links per file pair below which two groups are not merged
EXTRA_CODE_SUFFIXES = (".sh", ".rs")
TEST_DIRS = {"tests", "test", "testdata", "fixtures"}  # test code is covered by the overview, not by groups


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def limits() -> Tuple[int, int]:
    """(files, bytes) above which a scope is split; GUARDIAN_CODE_WIKI_SPLIT_FILES / _SPLIT_BYTES override them."""
    return (_env_int("GUARDIAN_CODE_WIKI_SPLIT_FILES", SPLIT_FILES),
            _env_int("GUARDIAN_CODE_WIKI_SPLIT_BYTES", SPLIT_BYTES))


def is_code(path: str) -> bool:
    return language(path) is not None or path.endswith(EXTRA_CODE_SUFFIXES)


def slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "root"


class Splitter:
    """Groups of one commit. code_files(prefix) and sizes come from the repo tools; the index is built lazily."""

    def __init__(self, tools: Any, max_files: Optional[int] = None, max_bytes: Optional[int] = None) -> None:
        self.tools = tools
        default_files, default_bytes = limits()
        self.max_files = max_files or default_files
        self.max_bytes = max_bytes or default_bytes
        self.code = [p for p in tools.readable_paths()
                     if is_code(p) and not is_noise_path(p) and not set(p.split("/")[:-1]) & TEST_DIRS]
        self._index: Any = None

    # --- sizes -----------------------------------------------------------------------------------

    def size(self, path: str) -> int:
        return int(self.tools.entries[path][1])

    def under(self, directory: str) -> List[str]:
        return [p for p in self.code if p.startswith(directory)]

    def over(self, files: List[str]) -> bool:
        return len(files) > self.max_files or sum(self.size(p) for p in files) > self.max_bytes

    # --- the plan --------------------------------------------------------------------------------

    def plan(self, directory: str, page_id: str) -> Optional[List[Dict[str, Any]]]:
        """Groups of a directory page (`dir/`), or None when the directory is within the limit. Each group is
        {id, scope, files, bytes, label, hubs}; ids are `<page_id>--<label>`, unique, at most 64 characters."""
        if not self.over(self.under(directory)):
            return None
        groups: List[Dict[str, Any]] = []
        self._split(directory, directory, 0, groups)
        groups = [g for g in self._pack_small(groups) if g["bytes"] >= MIN_GROUP_BYTES]
        used: Set[str] = set()
        for group in groups:
            base = f"{page_id}--{slug(group['label'])}"[:64].strip("-")
            gid, n = base, 2
            while gid in used:
                suffix = f"-{n}"
                gid, n = base[:64 - len(suffix)] + suffix, n + 1
            used.add(gid)
            group["id"] = gid
        return groups

    def _split(self, root: str, directory: str, depth: int, out: List[Dict[str, Any]]) -> None:
        files = self.under(directory)
        rel = directory[len(root):].strip("/")
        if directory != root and (not self.over(files) or depth >= MAX_DEPTH):
            if len(files) >= MIN_GROUP_FILES:
                out.append(self._group(rel, [directory], files, parent=directory.rstrip("/").rsplit("/", 1)[0]))
            return
        direct = [p for p in files if "/" not in p[len(directory):]]
        subdirs = sorted({directory + p[len(directory):].split("/", 1)[0] + "/"
                          for p in files if "/" in p[len(directory):]})
        for sub in subdirs:
            self._split(root, sub, depth + 1, out)
        if len(direct) < MIN_GROUP_FILES:
            return
        if not self.over(direct) and subdirs:
            out.append(self._group(f"{rel}-files" if rel else "files", list(direct), direct, parent=directory))
            return
        for name, members, hubs in self.cluster(direct):
            label = f"{rel}-{name}" if rel else name
            out.append(self._group(label, list(members), members, hubs, parent=directory))

    def _pack_small(self, groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Consecutive groups under 1/SMALL_SHARE of the byte limit with the same parent directory are packed together
        (within the limit), so a directory with many tiny files does not become many tiny pages. A pack is labelled
        with the labels' common prefix and `etc`."""
        small = self.max_bytes // SMALL_SHARE
        out: List[Dict[str, Any]] = []
        pack: List[Dict[str, Any]] = []

        def flush() -> None:
            if not pack:
                return
            if len(pack) == 1:
                out.append(pack[0])
            else:
                words = [g["label"].split("-") for g in pack]
                common: List[str] = []
                for parts in zip(*words):
                    if len(set(parts)) != 1:
                        break
                    common.append(parts[0])
                out.append({"label": "-".join(common + ["etc"]),
                            "scope": [e for g in pack for e in g["scope"]],
                            "files": sum(g["files"] for g in pack), "bytes": sum(g["bytes"] for g in pack),
                            "hubs": [h for g in pack for h in g["hubs"]], "parent": pack[0]["parent"]})
            pack.clear()

        for group in groups:
            if group["bytes"] >= small or group["hubs"]:
                flush()
                out.append(group)
                continue
            if pack and (pack[0]["parent"] != group["parent"]
                         or sum(g["files"] for g in pack) + group["files"] > self.max_files
                         or sum(g["bytes"] for g in pack) + group["bytes"] > self.max_bytes
                         or sum(len(g["scope"]) for g in pack) + len(group["scope"]) > MAX_SCOPE):
                flush()
            pack.append(group)
        flush()
        return out

    def _group(self, label: str, scope: List[str], files: List[str], hubs: Optional[List[str]] = None,
               parent: str = "") -> Dict[str, Any]:
        return {"label": label, "scope": scope, "files": len(files), "bytes": sum(self.size(p) for p in files),
                "hubs": list(hubs or []), "parent": parent}

    # --- clustering ------------------------------------------------------------------------------

    def index(self) -> Any:
        if self._index is None:
            self._index = self.tools.code_index()
        return self._index

    def edges(self, files: List[str]) -> Dict[Tuple[str, str], int]:
        """(user, owner) -> reference lines between files of the set (name based, same language, import checked)."""
        index = self.index()
        members = set(files)
        owners: Dict[str, Set[str]] = {}
        for decl in index.decls:
            owners.setdefault(decl["name"], set()).add(decl["path"])
        found: Dict[Tuple[str, str], int] = {}
        for name, where in index.refs.items():
            paths = owners.get(name)
            if not paths or len(paths) != 1 or len(name) < 3:
                continue
            owner = next(iter(paths))
            if owner not in members:
                continue
            for user, lines in where.items():
                if user != owner and user in members and language(user) == language(owner):
                    found[(user, owner)] = found.get((user, owner), 0) + len(lines)
        texts = self.tools.source_texts(sorted({u for u, _o in found}))
        return {pair: n for pair, n in found.items() if imports(pair[0], texts.get(pair[0], ""), pair[1])}

    def cluster(self, files: List[str]) -> List[Tuple[str, List[str], List[str]]]:
        """(name, files, hubs) groups of a flat set of files.

        Hub files (linked to at least a third of the set) form the `core` group: entry points and engines that
        everything touches would otherwise pull every file into one group. The other files are agglomerated by average
        linkage: the pair of groups with the most links per file pair (a link is one file using another, either way)
        merges while the result stays within the limit and the linkage is at least MIN_LINKAGE. Files left alone go to
        `misc`. Every group is cut in path order if it is still over the limit."""
        files = sorted(files)
        edges = self.edges(files)
        links: Dict[Tuple[str, str], int] = {}
        users: Dict[str, Set[str]] = {f: set() for f in files}
        degree: Dict[str, Set[str]] = {f: set() for f in files}
        for user, owner in edges:
            users[owner].add(user)
            degree[user].add(owner)
            degree[owner].add(user)
            pair = (min(user, owner), max(user, owner))
            links[pair] = links.get(pair, 0) + 1
        threshold = max(MIN_HUB_USERS, -(-len(files) // HUB_SHARE))
        hubs = sorted(f for f in files if len(degree[f]) >= threshold)
        groups: List[List[str]] = [[f] for f in files if f not in hubs]

        def between(a: List[str], b: List[str]) -> int:
            return sum(links.get((min(x, y), max(x, y)), 0) for x in a for y in b)

        while True:
            best: Optional[Tuple[float, str, str, int, int]] = None
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    n = between(groups[i], groups[j])
                    if n == 0 or self.over(groups[i] + groups[j]):
                        continue
                    score = n / (len(groups[i]) * len(groups[j]))
                    if score < MIN_LINKAGE:
                        continue
                    # Highest linkage first; ties go to the pair whose first files sort first (deterministic).
                    candidate = (-score, groups[i][0], groups[j][0], i, j)
                    if best is None or candidate < best:
                        best = candidate
            if best is None:
                break
            i, j = best[3], best[4]
            groups[i] = sorted(groups[i] + groups[j])
            del groups[j]
        result: List[Tuple[str, List[str], List[str]]] = []
        for k, chunk in enumerate(self._chunks(hubs)):
            result.append(("core" if k == 0 else f"core-{k + 1}", chunk, chunk))
        misc: List[str] = []
        for members in sorted(groups, key=lambda m: m[0]):
            if len(members) < MIN_GROUP_FILES:
                misc.extend(members)
                continue
            for chunk in self._chunks(members):
                result.append((self._name(chunk, users), chunk, []))
        for k, chunk in enumerate(self._chunks(sorted(misc))):
            result.append(("misc" if k == 0 else f"misc-{k + 1}", chunk, []))
        return result

    def _chunks(self, files: List[str]) -> List[List[str]]:
        """Consecutive pieces of files within the limit (a single file over the byte limit is a piece of its own)."""
        out: List[List[str]] = []
        current: List[str] = []
        size = 0
        for f in files:
            if current and (len(current) + 1 > self.max_files or size + self.size(f) > self.max_bytes):
                out.append(current)
                current, size = [], 0
            current.append(f)
            size += self.size(f)
        if current:
            out.append(current)
        return out

    @staticmethod
    def _name(files: List[str], users: Dict[str, Set[str]]) -> str:
        """Stem of the file of the group the most files use (ties: the first by path)."""
        best = sorted(files, key=lambda f: (-len(users.get(f, ())), f))[0]
        return best.rsplit("/", 1)[-1].split(".", 1)[0]

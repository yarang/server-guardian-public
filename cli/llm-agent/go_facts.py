"""
Compiler-assisted Go facts for debate agents (docs/design/debate-code-index-wiki.md, "1층 확장", P2.6).

The type-checked definitions and references of a Go repository are extracted by `goindex` (tools/goindex, a standard
library Go helper that runs `go list -export` and go/types) in a throwaway git worktree of the pinned commit, and
stored as JSON per sha. This module is the engine side and stays standard-library only:

- GoFacts: the parsed JSON with the lookups defs / refs / callers / callees / the map ranking need.
- GoFactsProvider: finds the helper and the Go toolchain, runs the extraction in a background thread (one worktree
  per sha, file lock so concurrent debates share one extraction, process group kill on timeout, worktree and temp
  dir removed in a finally), stores `<store dir>/<sha>.json` keyed by (sha, GOINDEX_VERSION, go version), keeps the
  newest KEEP_FACTS per project, trims the Go build cache, and hands the facts to whoever asks (waiting at most
  GUARDIAN_GOINDEX_WAIT seconds, once). Processes that only read (the repo MCP servers) use the same class without
  starting an extraction: they load the stored file, or wait while another process holds the lock.

Environment (all optional): GUARDIAN_GOINDEX_BIN (default ~/.local/libexec/guardian/goindex), GUARDIAN_GOINDEX_TIMEOUT
(180 s), GUARDIAN_GOINDEX_WAIT (45 s), GUARDIAN_GOINDEX_CACHE_MB (1024; 0 disables trimming),
GUARDIAN_GOINDEX_MAP_WAIT (0 s: the first prompt's map never waits for an extraction).
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from env_policy import tool_env

try:  # POSIX only; without it concurrent extractions are not serialized
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# Must equal Version in tools/goindex/facts.go: facts with another version are treated as missing.
GOINDEX_VERSION = 1
KEEP_FACTS = 20
DEFAULT_TIMEOUT = 180.0
# Below the 60 s tool-call timeout some agent CLIs (codex) apply to MCP calls, so a waiting call still answers.
DEFAULT_WAIT = 45.0
DEFAULT_CACHE_MB = 1024.0
TMP_PREFIX = "guardian-goindex-"
STALE_TMP_SECONDS = 3600
GIT_STEP_TIMEOUT = 120
MODULE_SKIP_DIRS = frozenset({".git", "node_modules", "vendor", "testdata"})
# The statuses of the compiler_index event.
STATUSES = ("built", "cached", "failed", "timeout", "unavailable")
_HELPER_GRACE = 30.0  # the helper stops itself this long after the engine would have killed it (orphan guard)


def _number(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def goindex_timeout() -> float:
    value = _number("GUARDIAN_GOINDEX_TIMEOUT", DEFAULT_TIMEOUT)
    return value if value > 0 else DEFAULT_TIMEOUT


def goindex_wait() -> float:
    return _number("GUARDIAN_GOINDEX_WAIT", DEFAULT_WAIT)


def goindex_map_wait() -> float:
    return _number("GUARDIAN_GOINDEX_MAP_WAIT", 0.0)


def goindex_cache_mb() -> float:
    return _number("GUARDIAN_GOINDEX_CACHE_MB", DEFAULT_CACHE_MB)


def helper_path() -> Optional[str]:
    """The goindex executable (GUARDIAN_GOINDEX_BIN or ~/.local/libexec/guardian/goindex), or None when absent."""
    raw = os.environ.get("GUARDIAN_GOINDEX_BIN", "").strip() or "~/.local/libexec/guardian/goindex"
    path = os.path.expanduser(raw)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def search_path() -> str:
    """PATH for the children: the user-level Go first (the hub service's PATH lacks it), like MCPClient.tool_environ."""
    home = Path.home()
    return os.pathsep.join([str(home / ".local" / "go" / "bin"), str(home / ".local" / "bin"),
                            os.environ.get("PATH", "")])


def go_binary() -> Optional[str]:
    return shutil.which("go", path=search_path())


def child_env(gocache: Path) -> Dict[str, str]:
    """Environment of the helper and the git/go processes it starts: no tokens or credentials (env_policy.tool_env),
    a private build cache, and the settings that keep go from fetching anything or running C compilers."""
    env = tool_env(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env.update({
        "PATH": search_path(), "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly", "CGO_ENABLED": "0",
        "GOPROXY": "off", "GOWORK": "off", "GOCACHE": str(gocache), "GIT_TERMINAL_PROMPT": "0",
    })
    return env


_GO_VERSIONS: Dict[str, Optional[str]] = {}


def go_version(go: str, env: Dict[str, str]) -> Optional[str]:
    """`go1.26.8`-style version of a go binary (cached per binary path), or None when it cannot be asked."""
    if go in _GO_VERSIONS:
        return _GO_VERSIONS[go]
    version = None
    try:
        out = subprocess.run([go, "env", "GOVERSION"], capture_output=True, text=True, timeout=30, env=env).stdout
        version = out.strip() or None
    except (OSError, subprocess.SubprocessError):
        version = None
    _GO_VERSIONS[go] = version
    return version


# --- facts ----------------------------------------------------------------------------------------------------

Def = Dict[str, Any]
Ref = Dict[str, Any]


class GoFacts:
    """Type-checked facts of one commit (the JSON of tools/goindex) with the lookups the ops need."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data
        self.go: str = str(data.get("go") or "")
        self.sha: str = str(data.get("sha") or "")
        defs = [d for d in data["defs"] if isinstance(d, dict)]
        self.defs_by_id: Dict[int, Def] = {int(d["id"]): d for d in defs}
        self.own_defs: List[Def] = [d for d in defs if not d.get("external")]
        self.refs_by_def: Dict[int, List[Ref]] = {}
        self.refs: List[Ref] = [r for r in data["refs"] if isinstance(r, dict)]
        for ref in self.refs:
            self.refs_by_def.setdefault(int(ref["def"]), []).append(ref)
        self.unresolved: List[Dict[str, Any]] = [u for u in data.get("unresolved") or [] if isinstance(u, dict)]
        self.files: Set[str] = {str(p) for p in data.get("files") or []}
        paths = {str(p["path"]) for p in data.get("packages") or [] if isinstance(p, dict) and p.get("path")}
        self.package_count = len(paths)
        self._by_name: Dict[str, List[Def]] = {}
        for d in self.own_defs:
            self._by_name.setdefault(str(d["name"]), []).append(d)
        self._at: Dict[Tuple[str, int, str], Def] = {(str(d["path"]), int(d["start"]), str(d["qualified"])): d
                                                       for d in self.own_defs}

    @classmethod
    def from_data(cls, data: Any) -> Optional["GoFacts"]:
        """The facts of a decoded JSON document, or None when it is not facts of this GOINDEX_VERSION."""
        if not isinstance(data, dict) or data.get("version") != GOINDEX_VERSION:
            return None
        if not isinstance(data.get("defs"), list) or not isinstance(data.get("refs"), list):
            return None
        try:
            return cls(data)
        except (KeyError, TypeError, ValueError):
            return None

    # --- queries ---------------------------------------------------------------------------------------

    def find_defs(self, symbol: str) -> List[Def]:
        """Own definitions named symbol; `Type.Method` matches the qualified name or its trailing components."""
        if "." in symbol:
            tail = "." + symbol
            found = [d for d in self.own_defs if d["qualified"] == symbol or str(d["qualified"]).endswith(tail)]
        else:
            found = list(self._by_name.get(symbol, []))
        return sorted(found, key=lambda d: (d["path"], d["start"], d["qualified"]))

    def def_at(self, path: str, start: int, qualified: str) -> Optional[Def]:
        return self._at.get((path, start, qualified))

    def refs_to(self, def_ids: Iterable[int]) -> List[Ref]:
        out: List[Ref] = []
        for def_id in def_ids:
            out.extend(self.refs_by_def.get(def_id, []))
        return out

    def callees(self, definition: Def) -> List[Tuple[Def, List[int]]]:
        """Functions called inside the body of definition, with the call lines, in order of first call."""
        found: Dict[int, List[int]] = {}
        for ref in self.refs:
            if (ref.get("call") and ref.get("path") == definition["path"]
                    and ref.get("enclosing") == definition["qualified"]
                    and definition["start"] <= int(ref["line"]) <= definition["end"]):
                found.setdefault(int(ref["def"]), []).append(int(ref["line"]))
        out = [(self.defs_by_id[i], sorted(set(lines))) for i, lines in found.items() if i in self.defs_by_id]
        out.sort(key=lambda item: (item[1][0], item[0]["qualified"]))
        return out

    def unresolved_named(self, name: str, match: Optional[Callable[[str], bool]] = None) -> List[Dict[str, Any]]:
        """Identifiers called name that the checker could not resolve (in packages with type errors)."""
        return [u for u in self.unresolved if u.get("name") == name and (match is None or match(str(u["path"])))]

    def referencing_files(self, def_id: int) -> Set[str]:
        """Files with a reference to the definition (declaring file included when it refers to itself)."""
        return {str(r["path"]) for r in self.refs_by_def.get(def_id, [])}

    def covers(self, path: str) -> bool:
        """Whether a Go file was type-checked (files of other platforms or build tags were not)."""
        return path in self.files

    def counts(self) -> Dict[str, int]:
        return {"packages": self.package_count, "defs": len(self.own_defs), "refs": len(self.refs),
                "unresolved": len(self.unresolved)}


# --- locking, storage -----------------------------------------------------------------------------------------

class _FileLock:
    """Advisory exclusive lock on a file (flock); a no-op where fcntl is missing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def try_acquire(self) -> bool:
        if fcntl is None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True  # an unwritable store cannot be shared anyway
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(self, timeout: float, cancel: threading.Event) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            if cancel.is_set() or time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def held_elsewhere(self) -> bool:
        """Whether another process holds the lock (probed without keeping it)."""
        if fcntl is None or not self.path.exists():
            return False
        if self.try_acquire():
            self.release()
            return False
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)  # closing drops the flock
            except OSError:
                pass
            self._fd = None


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prune_store(store_dir: Path, keep: int = KEEP_FACTS) -> int:
    """Keeps the newest `keep` fact files of a project (by mtime, which cache hits refresh); returns how many went."""
    try:
        files = [p for p in store_dir.glob("*.json") if not p.name.endswith(".status.json")
                 and not p.name.startswith(".tmp-")]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for old in files[keep:]:
        stem = old.name[:-len(".json")]
        for name in (old.name, f"{stem}.status.json", f"{stem}.lock"):
            try:
                (store_dir / name).unlink()
                removed += name == old.name
            except OSError:
                pass
    return removed


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def reap_stale_temp(now: Optional[float] = None) -> int:
    """Removes extraction temp dirs an interrupted run left behind (older than STALE_TMP_SECONDS)."""
    moment = time.time() if now is None else now
    removed = 0
    root = Path(tempfile.gettempdir())
    try:
        candidates = list(root.glob(f"{TMP_PREFIX}*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.is_dir() and moment - path.stat().st_mtime > STALE_TMP_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def module_dirs(paths: Iterable[str]) -> List[str]:
    """Directories (repository-relative, "." for the root) of the go.mod files among paths, like
    MCPClient._go_modules: nothing below .git, node_modules, vendor or testdata."""
    found = []
    for path in paths:
        parts = path.split("/")
        if parts[-1] == "go.mod" and not MODULE_SKIP_DIRS & set(parts[:-1]):
            found.append("/".join(parts[:-1]) or ".")
    return sorted(found)


# --- provider -------------------------------------------------------------------------------------------------

EventSink = Callable[[str, Dict[str, Any]], None]


class GoFactsProvider:
    """Facts of one sha: loads them from the store, or extracts them in a background thread (start()).

    modules and go_paths are callables returning the repository-relative go.mod directories and readable .go files of
    the commit. get() never raises. A provider is used for one run: after a failure or timeout it does not retry.
    """

    def __init__(self, workspace: Path, sha: str, store_dir: Path,
                 modules: Callable[[], List[str]], go_paths: Callable[[], List[str]]) -> None:
        self.workspace = Path(workspace)
        self.sha = sha
        self.store_dir = Path(store_dir)
        # <GUARDIAN_DATA_DIR>/code-index/.gocache: one build cache for all projects, apart from the user's own. The dot
        # keeps it apart from project cache directories (default_cache_dir strips leading dots from project ids).
        self.gocache = self.store_dir.parent.parent / ".gocache"
        self._modules = modules
        self._go_paths = go_paths
        self.state = "idle"  # idle | pending | built | cached | failed | timeout | unavailable
        self.error = ""
        self.result: Dict[str, Any] = {}
        self._facts: Optional[GoFacts] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._waited = False
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._sink: Optional[EventSink] = None

    # --- paths -----------------------------------------------------------------------------------------

    @property
    def facts_file(self) -> Path:
        return self.store_dir / f"{self.sha}.json"

    @property
    def status_file(self) -> Path:
        return self.store_dir / f"{self.sha}.status.json"

    @property
    def lock_file(self) -> Path:
        return self.store_dir / f"{self.sha}.lock"

    def has_go(self) -> bool:
        cached = self.__dict__.get("_has_go")
        if cached is None:
            cached = self._has_go = bool(self._modules())
        return cached

    # --- reading ---------------------------------------------------------------------------------------

    def _load_disk(self, expected_go: Optional[str] = None, lookup_go: bool = False) -> Optional[GoFacts]:
        """The stored facts when they are valid: right version, this sha, and this go version (expected_go, or the
        installed one when lookup_go is set and a go binary exists)."""
        try:
            data = json.loads(self.facts_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        facts = GoFacts.from_data(data)
        if facts is None or (facts.sha and facts.sha != self.sha):
            return None
        if lookup_go and expected_go is None:
            expected_go = self._known_go_version()
        if expected_go and facts.go != expected_go:
            return None
        try:
            os.utime(self.facts_file)  # a hit keeps the file among the newest
        except OSError:
            pass
        return facts

    def _disk_status(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def peek(self) -> Optional[GoFacts]:
        """The facts if they are available right now (loaded, or stored by anyone); never waits."""
        if self._facts is not None:
            return self._facts
        if self.state == "idle" and self._thread is None:
            facts = self._load_disk(lookup_go=True)
            if facts is not None:
                self._facts = facts
        return self._facts

    def _known_go_version(self) -> Optional[str]:
        go = go_binary()
        return go_version(go, child_env(self.gocache)) if go else None

    def get(self, wait: Optional[float] = None) -> Optional[GoFacts]:
        """The facts, waiting at most `wait` seconds (default GUARDIAN_GOINDEX_WAIT) for an extraction in progress.

        Only the first call that finds an extraction in progress waits; later calls answer immediately, so a slow
        extraction delays the agent once, not on every request.
        """
        facts = self.peek()
        if facts is not None or not self.has_go():
            return facts
        limit = goindex_wait() if wait is None else wait
        if self.state == "pending":
            if limit > 0 and self._first_wait():
                self._ready.wait(limit)
            return self._facts
        if self.state == "idle" and self._thread is None:
            # Not started here: another process may be extracting (repo MCP servers).
            lock = _FileLock(self.lock_file)
            if limit > 0 and lock.held_elsewhere() and self._first_wait():
                deadline = time.monotonic() + limit
                while time.monotonic() < deadline and lock.held_elsewhere():
                    if self.facts_file.exists():
                        break
                    time.sleep(0.25)
            return self.peek()
        return self._facts

    def _first_wait(self) -> bool:
        """True for the first caller that is about to wait (a call with no time to wait does not use it up)."""
        with self._lock:
            first, self._waited = not self._waited, True
        return first

    def effective_state(self) -> str:
        """pending / failed / timeout / unavailable / "" (nothing known) for the notes of name-based answers."""
        if self._facts is not None:
            return ""
        if self.state in ("pending", "failed", "timeout", "unavailable"):
            return self.state
        if self.state == "idle" and self._thread is None and self.has_go():
            if _FileLock(self.lock_file).held_elsewhere():
                return "pending"
            status = self._disk_status().get("status")
            return status if status in ("failed", "timeout", "unavailable") else ""
        return ""

    def note(self) -> str:
        """A line for answers that fall back to name-based results for Go, or "" when there is nothing to say."""
        state = self.effective_state()
        if state == "pending":
            return "[type information for Go is still being prepared: results are name-based]"
        if state:
            return f"[no type information for Go ({state}): results are name-based]"
        return ""

    def summary(self) -> Dict[str, Any]:
        """The `index.go` entry of the thread snapshot."""
        facts = self._facts
        return {"version": GOINDEX_VERSION, "go": facts.go if facts else self.result.get("go", ""),
                "status": self.state, "packages": facts.package_count if facts else 0}

    # --- extraction ------------------------------------------------------------------------------------

    def start(self, sink: Optional[EventSink] = None) -> None:
        """Begins making the facts available: cached facts at once, else an extraction in a daemon thread.

        sink(event type, data) receives the one compiler_index event (and compiler_index_warning for cleanup
        problems). Does nothing for a repository without go.mod or when already started.
        """
        if self.state != "idle" or self._thread is not None or not self.has_go():
            return
        self._sink = sink
        go, helper = go_binary(), helper_path()
        if go is None or helper is None:
            what = "go" if go is None else "goindex helper"
            self._finish("unavailable", error=f"{what} not found", started=time.monotonic())
            return
        env = child_env(self.gocache)
        version = go_version(go, env)
        facts = self._load_disk(version)
        if facts is not None:
            self._facts = facts
            self._finish("cached", started=time.monotonic(), facts=facts)
            return
        self.state = "pending"
        self._thread = threading.Thread(target=self._extract, args=(go, helper, env, version),
                                        name="goindex", daemon=True)
        self._thread.start()

    def _emit(self, kind: str, data: Dict[str, Any]) -> None:
        if self._sink is not None:
            try:
                self._sink(kind, data)
            except Exception:  # noqa: BLE001 - reporting must never break the extraction
                pass

    def _finish(self, status: str, error: str = "", started: float = 0.0, facts: Optional[GoFacts] = None) -> None:
        counts = facts.counts() if facts else {"packages": 0, "defs": 0, "refs": 0, "unresolved": 0}
        data = {"lang": "go", "sha": self.sha, "status": status, **counts,
                "ms": int((time.monotonic() - started) * 1000) if started else 0, "error": error[:300] or None}
        self.result = {**data, "go": facts.go if facts else ""}
        self.error = error
        if status != "cached":
            try:
                _write_json_atomic(self.status_file, {"status": status, "error": error[:300], "ms": data["ms"],
                                                      "go": self.result["go"], "version": GOINDEX_VERSION})
            except OSError:
                pass
        self.state = status
        self._ready.set()
        self._emit("compiler_index", data)

    def _extract(self, go: str, helper: str, env: Dict[str, str], version: Optional[str]) -> None:
        started = time.monotonic()
        timeout = goindex_timeout()
        status, error, facts = "failed", "", None
        cached = False
        lock = _FileLock(self.lock_file)
        tmp: Optional[Path] = None
        worktree: Optional[Path] = None
        try:
            if not lock.acquire(timeout + _HELPER_GRACE, self._cancel):
                raise _Failure("failed", "another extraction of this commit did not finish")
            facts = self._load_disk(version)  # a concurrent debate may have finished while we waited
            if facts is not None:
                status, cached = "cached", True
            else:
                try:
                    self.status_file.unlink()
                except OSError:
                    pass
                self.gocache.mkdir(parents=True, exist_ok=True, mode=0o700)
                reap_stale_temp()
                self._git(["worktree", "prune"], env, check=False)
                tmp = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))  # 0700
                worktree = tmp / "src"
                created = self._git(["-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(worktree),
                                     self.sha], env, check=False)
                if created.returncode != 0:
                    raise _Failure("failed", "git worktree add failed: " + _tail(created.stderr))
                facts, status, error = self._run_helper(helper, env, tmp, worktree, timeout, version)
                if facts is not None:
                    self._facts = facts  # available to waiters before the cleanup below
                    self._ready.set()
        except _Failure as failure:
            status, error = failure.status, failure.message
        except Exception as exc:  # noqa: BLE001 - nothing may escape a background thread unreported
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self._cleanup(tmp, worktree, env)
            if not cached:
                self._trim_cache(go, env)
            lock.release()
        if facts is not None:
            self._facts = facts
            if not cached:
                prune_store(self.store_dir)
        self._finish(status, error=error, started=started, facts=self._facts)

    def _run_helper(self, helper: str, env: Dict[str, str], tmp: Path, worktree: Path, timeout: float,
                    version: Optional[str]) -> Tuple[Optional[GoFacts], str, str]:
        modules = self._modules()
        readable = tmp / "readable.txt"
        readable.write_text("".join(f"{p}\n" for p in self._go_paths()), encoding="utf-8")
        os.chmod(readable, 0o600)
        out = tmp / "facts.json"
        argv = [helper, "-root", str(worktree), "-readable", str(readable), "-out", str(out), "-sha", self.sha,
                "-timeout", f"{int(timeout + _HELPER_GRACE)}s"]
        for module in modules:
            argv += ["-module", module]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(tmp),
                                start_new_session=True)
        self._proc = proc
        deadline = time.monotonic() + timeout
        stderr = b""
        try:
            while True:
                try:
                    _out, stderr = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if self._cancel.is_set():
                        _kill_group(proc)
                        proc.communicate()
                        raise _Failure("failed", "cancelled")
                    if time.monotonic() >= deadline:
                        _kill_group(proc)
                        proc.communicate()
                        raise _Failure("timeout", f"no result after {int(timeout)} s")
        finally:
            self._proc = None
        code = proc.returncode
        if code == 3:
            return None, "timeout", f"the helper gave up after {int(timeout)} s"
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        facts = GoFacts.from_data(data)
        if code != 0:
            problems = [m.get("error", "") for m in (data or {}).get("module_errors", [])
                        if isinstance(m, dict)] if isinstance(data, dict) else []
            return None, "failed", (problems[0] if problems else _tail(stderr)) or f"goindex exited with {code}"
        if facts is None:
            raise _Failure("failed", "goindex wrote facts of another version or format")
        if version and facts.go != version:
            raise _Failure("failed", f"goindex used {facts.go}, expected {version}")
        notes = [f"{m.get('dir')}: {m.get('error')}" for m in data.get("module_errors", []) if isinstance(m, dict)]
        _write_json_atomic(self.facts_file, data)
        return facts, "built", "; ".join(notes)[:300]

    def _git(self, args: List[str], env: Dict[str, str], check: bool = True) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run(["git", "-C", str(self.workspace), *args], capture_output=True, env=env,
                              timeout=GIT_STEP_TIMEOUT, check=check)

    def _cleanup(self, tmp: Optional[Path], worktree: Optional[Path], env: Dict[str, str]) -> None:
        """Removes the worktree and the temp dir whatever happened; problems become a warning event."""
        problems: List[str] = []
        if worktree is not None and worktree.exists():
            try:
                removed = self._git(["worktree", "remove", "--force", str(worktree)], env, check=False)
                if removed.returncode != 0 and worktree.exists():
                    problems.append("git worktree remove failed: " + _tail(removed.stderr))
            except (OSError, subprocess.SubprocessError) as exc:
                problems.append(f"git worktree remove failed: {exc}")
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
            if tmp.exists():
                problems.append("temp dir could not be removed")
        if worktree is not None:
            try:
                self._git(["worktree", "prune"], env, check=False)  # drops the entry of a worktree that is gone
            except (OSError, subprocess.SubprocessError):
                pass
        if problems:
            self._emit("compiler_index_warning", {"lang": "go", "sha": self.sha, "message": "; ".join(problems)[:300]})

    def _trim_cache(self, go: str, env: Dict[str, str]) -> None:
        limit = goindex_cache_mb()
        if limit <= 0 or not self.gocache.is_dir():
            return
        if _dir_size(self.gocache) > limit * 1024 * 1024:
            try:
                subprocess.run([go, "clean", "-cache"], capture_output=True, env=env, timeout=GIT_STEP_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self, join: float = 30.0) -> None:
        """Stops a running extraction (its helper's process group is killed) and waits for its cleanup."""
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            _kill_group(proc)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join)


class _Failure(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _tail(raw: Any) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[0] if lines else "")[:300]


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass

"""Repository snapshots for the model workers (docs/design/remote-agents.md, W2).

A debate member placed on a worker host reads the repository through the same repo_* tools as a member on the hub,
so the worker needs the files. The hub does not trust the worker with a clone (it holds no credentials and no remote)
and does not give it ssh access back: the hub builds a `git fast-import` stream of the committed, readable files at the
pinned sha and sends it over the ssh session of the worker client. The worker imports it into a bare repository of its
own, which repo_mcp.py then reads exactly like a clone (RepoTools needs nothing else).

What a snapshot holds is what RepoTools.readable_paths() allows an agent to see: no secret files, symlinks or
submodules. It is a stream of blobs and one commit with a fixed identity and date, so the same files always give the same
key and the same commit id; the key (a digest of path, mode and blob id of every file kept, plus the size caps) names the
snapshot on the worker, which therefore skips the upload when it has it. A file larger than MAX_BLOB_BYTES, or one that
would take the snapshot over MAX_TOTAL_BYTES, is left out (RepoSnapshot.dropped lists them): a remote member does not
see it in repo_list, where a member on the hub would.

Standard library only. The hub side is build_snapshot(); the worker side is import_stream(), snapshot_commit() and
prune_snapshots().
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, List, Optional, Sequence, Tuple

from repo_tools import RepoTools

# Bump when the stream or the key derivation changes: workers then import a fresh snapshot instead of reusing an old one.
SNAPSHOT_FORMAT = 1
MAX_BLOB_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# What a worker accepts after decompression: the content cap plus path names and per-file framing.
MAX_STREAM_BYTES = MAX_TOTAL_BYTES + 16 * 1024 * 1024
SNAPSHOT_REF = "refs/heads/snapshot"
COMMIT_FILE = "guardian-commit"
IDENTITY = "Guardian <guardian@localhost> 0 +0000"
KEY_LENGTH = 64
DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_KEEP = 8
IMPORT_TIMEOUT = 120
_CHUNK = 256 * 1024


class SnapshotError(RuntimeError):
    """A snapshot cannot be built or imported; the message says why."""


@dataclass
class RepoSnapshot:
    """A built snapshot: the compressed stream on disk and what it holds."""

    key: str
    path: Path  # the zlib-compressed fast-import stream
    files: int
    content_bytes: int
    dropped: List[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.path.stat().st_size


def is_key(value: object) -> bool:
    return isinstance(value, str) and len(value) == KEY_LENGTH and all(c in "0123456789abcdef" for c in value)


# --- hub side -------------------------------------------------------------------------------------


def quote_path(path: str) -> bytes:
    """A path as fast-import reads it: plain when it can be, C-style quoted when it holds a quote, a backslash or
    a control character (a quoted path may span only one line, so a newline must be escaped)."""
    raw = path.encode("utf-8")
    plain = raw and raw[:1] != b'"' and not any(c < 0x20 or c in (0x22, 0x5C, 0x7F) for c in raw)
    if plain:
        return raw
    out = bytearray(b'"')
    for byte in raw:
        if byte == 0x22:
            out += b'\\"'
        elif byte == 0x5C:
            out += b"\\\\"
        elif byte == 0x0A:
            out += b"\\n"
        elif byte == 0x09:
            out += b"\\t"
        elif byte < 0x20 or byte == 0x7F:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    out += b'"'
    return bytes(out)


def _selection(tools: RepoTools, max_blob: int, max_total: int) -> Tuple[List[str], List[str]]:
    """(paths kept, paths dropped) in path order: readable files within the caps."""
    kept: List[str] = []
    dropped: List[str] = []
    total = 0
    for path in tools.readable_paths():
        size = tools.entries[path][1]
        if "�" in path or size > max_blob or total + size > max_total:
            dropped.append(path)
            continue
        kept.append(path)
        total += size
    return kept, dropped


def snapshot_key(tools: RepoTools, paths: Sequence[str]) -> str:
    """Digest of the snapshot's content: format version, then path, mode and blob id of each file kept."""
    digest = hashlib.sha256(f"guardian-snapshot {SNAPSHOT_FORMAT}\n".encode())
    for path in paths:
        mode, _size, blob_id = tools.entries[path]
        digest.update(f"{mode} {blob_id} ".encode() + path.encode("utf-8") + b"\n")
    return digest.hexdigest()


def _stream(tools: RepoTools, paths: Sequence[str]) -> Iterator[bytes]:
    message = b"snapshot"
    yield (f"commit {SNAPSHOT_REF}\ncommitter {IDENTITY}\ndata {len(message)}\n").encode() + message + b"\n"
    for path, mode, content in tools.snapshot_files(paths):
        # git stores only these two modes for a regular file.
        fmode = "100755" if mode == "100755" else "100644"
        yield f"M {fmode} inline ".encode() + quote_path(path) + b"\n" + f"data {len(content)}\n".encode()
        yield content + b"\n"
    yield b"\ndone\n"


def build_snapshot(tools: RepoTools, directory: Path, max_blob: int = MAX_BLOB_BYTES,
                   max_total: int = MAX_TOTAL_BYTES) -> RepoSnapshot:
    """Writes the compressed stream of the commit's readable files into directory (created 0700 by the caller)."""
    kept, dropped = _selection(tools, max_blob, max_total)
    key = snapshot_key(tools, kept)
    target = Path(directory) / f"{key}.stream.z"
    compressor = zlib.compressobj(6)
    content = 0
    try:
        with open(target, "wb") as out:
            for part in _stream(tools, kept):
                content += len(part)
                out.write(compressor.compress(part))
            out.write(compressor.flush())
    except OSError as exc:
        raise SnapshotError(f"cannot write the snapshot stream: {exc}") from exc
    return RepoSnapshot(key=key, path=target, files=len(kept), content_bytes=content, dropped=dropped)


# --- worker side ----------------------------------------------------------------------------------


def snapshot_path(root: Path, key: str) -> Path:
    if not is_key(key):
        raise SnapshotError("not a snapshot key")
    return Path(root) / key


def snapshot_commit(root: Path, key: str, touch: bool = False) -> Optional[str]:
    """The commit id of the stored snapshot `key`, or None when this worker does not hold it.
    touch=True marks the snapshot used now, so that pruning keeps what a running job reads."""
    path = snapshot_path(root, key) / COMMIT_FILE
    try:
        commit = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        return None
    if touch:
        try:
            os.utime(path)
        except OSError:
            pass
    return commit


def _inflate(source: BinaryIO, limit: int) -> Iterator[bytes]:
    """The decompressed bytes of a zlib stream, never more than limit (a hostile stream cannot fill the disk)."""
    inflater = zlib.decompressobj()
    total = 0
    try:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            pending = chunk
            while pending:
                out = inflater.decompress(pending, 1 << 20)
                total += len(out)
                if total > limit:
                    raise SnapshotError(f"snapshot larger than {limit} bytes")
                if out:
                    yield out
                rest = inflater.unconsumed_tail
                if not out and rest == pending:
                    break  # no progress: the input after the end of the stream
                pending = rest
        tail = inflater.flush()
        total += len(tail)
        if total > limit:
            raise SnapshotError(f"snapshot larger than {limit} bytes")
        if tail:
            yield tail
    except zlib.error as exc:
        raise SnapshotError(f"unreadable snapshot stream: {exc}") from exc
    if not inflater.eof:
        raise SnapshotError("snapshot stream is incomplete")


def import_stream(root: Path, key: str, source: BinaryIO, limit: int = MAX_STREAM_BYTES) -> str:
    """Imports a compressed fast-import stream as snapshot `key` under root and returns its commit id.

    The repository is built beside the final place and renamed into it, so a reader sees a complete snapshot or none;
    an upload that fails leaves nothing behind. An existing snapshot of the same key is kept (same key, same content)."""
    final = snapshot_path(root, key)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    existing = snapshot_commit(root, key, touch=True)
    if existing:
        return existing
    work = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
    try:
        repo = work / "repo"
        _run_git(["init", "--bare", "--quiet", str(repo)])
        with open(work / "fast-import.err", "w+b") as errfile:
            proc = subprocess.Popen(["git", "--git-dir", str(repo), "fast-import", "--quiet", "--done"],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errfile)
            try:
                assert proc.stdin is not None
                for part in _inflate(source, limit):
                    proc.stdin.write(part)
                proc.stdin.close()
                proc.wait(timeout=IMPORT_TIMEOUT)
            except (SnapshotError, OSError, subprocess.TimeoutExpired) as exc:
                proc.kill()
                proc.wait()
                if isinstance(exc, SnapshotError):
                    raise
                raise SnapshotError(f"git fast-import failed: {exc}") from exc
            if proc.returncode != 0:
                errfile.seek(0)
                raise SnapshotError("git fast-import failed: " + errfile.read().decode(errors="replace").strip()[-200:])
        commit = _run_git(["--git-dir", str(repo), "rev-parse", "--verify", SNAPSHOT_REF]).strip()
        (repo / COMMIT_FILE).write_text(commit + "\n", encoding="ascii")
        try:
            os.rename(repo, final)
        except OSError:
            if snapshot_commit(root, key) is None:  # not a lost race with an identical upload
                raise SnapshotError("cannot store the snapshot") from None
        return snapshot_commit(root, key) or commit
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_git(args: List[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], capture_output=True, timeout=IMPORT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"git failed: {exc}") from exc
    if proc.returncode != 0:
        raise SnapshotError("git failed: " + proc.stderr.decode(errors="replace").strip()[-200:])
    return proc.stdout.decode(errors="replace")


def prune_snapshots(root: Path, ttl_seconds: float = DEFAULT_TTL_SECONDS, keep: int = DEFAULT_KEEP,
                    now: Optional[float] = None) -> List[str]:
    """Removes snapshots not used for ttl_seconds, then the least recently used beyond `keep`; returns their keys.
    Leftovers of an interrupted import (.incoming-*) older than an hour go too."""
    root = Path(root)
    now = time.time() if now is None else now
    removed: List[str] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return removed
    used: List[Tuple[float, str]] = []
    for name in names:
        path = root / name
        if name.startswith(".incoming-"):
            try:
                if now - path.stat().st_mtime > 3600:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
            continue
        if not is_key(name):
            continue
        try:
            stamp = (path / COMMIT_FILE).stat().st_mtime
        except OSError:
            stamp = 0.0  # an incomplete snapshot is never reused
        used.append((stamp, name))
    used.sort()
    stale = [item for item in used if now - item[0] > ttl_seconds]
    fresh = [item for item in used if item not in stale]
    surplus = fresh[:max(0, len(fresh) - max(keep, 0))]
    for _stamp, name in stale + surplus:
        shutil.rmtree(root / name, ignore_errors=True)
        removed.append(name)
    return removed

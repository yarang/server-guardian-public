#!/usr/bin/env python3
"""
Online backup of the hub decision database and the rest of its state.

The database copy uses SQLite's backup API, so it is consistent while the debate engine is
writing. Next to it, a tarball (guardian_data-<stamp>.tar.gz) holds the data dir
(GUARDIAN_DATA_DIR: teams, personas, projects, inventory, jev ADR DB, ...) plus the hub's fleet SSH
files ~/.ssh/guardian_fleet.conf and ~/.ssh/guardian_known_hosts when they exist. Left out: backups/
itself, code-index/ (the symbol index cache, rebuilt on demand; code-wiki/ is user content and stays in), every *.log (debate-jobs/*/output.log is the big one), and the live database files, which the
consistent copy above replaces. Private keys and /etc/guardian are never read.

Keeps the newest N copies of each kind in GUARDIAN_DATA_DIR/backups (default 14). With
--replicate HOST (or GUARDIAN_BACKUP_REPLICA) every new file is also sent over SSH to
HOST:~/.guardian/hub-backups, which keeps the same number of copies, so losing the hub disk does
not lose the state. Restore procedure: docs/operations/backup-restore.md

Exit status: 0 ok, 1 database backup failed, 2 replication failed, 3 data tarball failed
(the first failure wins; the other steps are still attempted).

Usage: python3 db_backup.py [--keep N] [--dest DIR] [--replicate HOST] [--no-data] [--ssh-dir DIR]
"""

import argparse
import datetime
import os
import shlex
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from db_manager import data_dir, default_db_path

BACKUP_PREFIX = "guardian_decisions-"
DB_PATTERN = f"{BACKUP_PREFIX}*.db"
DATA_PREFIX = "guardian_data-"
DATA_PATTERN = f"{DATA_PREFIX}*.tar.gz"
# The only files taken from ~/.ssh: the hub's fleet host blocks and the pinned host keys of
# dashboard-registered nodes. An allowlist, so a private key can never end up in the tarball.
FLEET_FILES = ("guardian_fleet.conf", "guardian_known_hosts")


def backup_database(source: Path, dest_dir: Path, keep: int, now: Optional[datetime.datetime] = None) -> Path:
    """Copies source into dest_dir with a timestamped name and prunes old copies."""
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    target = dest_dir / f"{BACKUP_PREFIX}{stamp}.db"
    partial = target.with_suffix(".db.partial")
    with sqlite3.connect(source) as src, sqlite3.connect(partial) as dst:
        src.backup(dst)
    partial.rename(target)
    target.chmod(0o600)
    prune_backups(dest_dir, keep)
    return target


def prune_backups(dest_dir: Path, keep: int, pattern: str = DB_PATTERN) -> List[Path]:
    """Deletes all but the newest `keep` backups matching pattern. Returns the removed files."""
    backups = sorted(dest_dir.glob(pattern))
    removed = backups[: max(len(backups) - keep, 0)]
    for path in removed:
        path.unlink()
    return removed


def _is_excluded(rel: Path, skip_rel: Iterable[Path]) -> bool:
    """True for data dir entries that stay out of the tarball (rel is relative to the data dir)."""
    if rel.parts and rel.parts[0] in ("backups", "code-index"):  # code-index/ is a cache that is rebuilt on demand
        return True
    if rel.parts[:1] == ("design-artifacts",) and ".cache" in rel.parts:  # derived previews of artifacts
        return True
    if rel.suffix == ".log" or rel.name.endswith(".partial"):
        return True
    return any(rel == s or s in rel.parents for s in skip_rel)


def backup_data(
    data: Path,
    dest_dir: Path,
    keep: int,
    ssh_dir: Optional[Path] = None,
    skip: Iterable[Path] = (),
    now: Optional[datetime.datetime] = None,
) -> Path:
    """Writes guardian_data-<stamp>.tar.gz (data dir as data/, fleet SSH files as fleet/), prunes old ones.

    skip lists paths (files or directories) to leave out besides the fixed exclusions, e.g. the live
    database files and a backup directory that lives inside the data dir. Files that disappear while
    the archive is written (a running debate job) are skipped.
    """
    if not data.is_dir():
        raise FileNotFoundError(f"data dir not found: {data}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    data = data.resolve()
    skip_rel: List[Path] = []
    for path in skip:
        try:
            skip_rel.append(path.resolve().relative_to(data))
        except ValueError:
            pass  # outside the data dir, nothing to exclude
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    target = dest_dir / f"{DATA_PREFIX}{stamp}.tar.gz"
    partial = target.with_name(target.name + ".partial")
    try:
        with tarfile.open(partial, "w:gz") as tar:
            for root, dirs, files in os.walk(data):
                root_path = Path(root)
                rel_root = root_path.relative_to(data)
                dirs[:] = sorted(d for d in dirs if not _is_excluded(rel_root / d, skip_rel))
                # os.walk lists symlinks to directories under dirs but does not descend into them.
                entries = [root_path / d for d in dirs] + [root_path / f for f in sorted(files)]
                if root_path == data:
                    entries.insert(0, root_path)
                for entry in entries:
                    rel = entry.relative_to(data)
                    if entry != data and _is_excluded(rel, skip_rel):
                        continue
                    arcname = "data" if entry == data else f"data/{rel.as_posix()}"
                    try:
                        tar.add(entry, arcname=arcname, recursive=False)
                    except FileNotFoundError:
                        continue
            if ssh_dir is not None:
                for name in FLEET_FILES:
                    source = ssh_dir / name
                    if source.is_file():
                        tar.add(source, arcname=f"fleet/{name}", recursive=False)
        partial.chmod(0o600)
        partial.rename(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    prune_backups(dest_dir, keep, DATA_PATTERN)
    return target


REPLICA_DIR = ".guardian/hub-backups"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def replicate_backup(backup: Path, host: str, keep: int, pattern: str = DB_PATTERN) -> None:
    """Copies backup to host:~/.guardian/hub-backups and prunes old copies matching pattern there."""
    def run(args: List[str]) -> None:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=120)

    run(["ssh", *SSH_OPTS, host, f"mkdir -p ~/{REPLICA_DIR} && chmod 700 ~/{REPLICA_DIR}"])
    run(["scp", "-q", "-p", *SSH_OPTS, str(backup), f"{host}:{REPLICA_DIR}/{backup.name}"])
    prune = (
        f"cd ~/{REPLICA_DIR} && ls -1 {pattern} | sort | head -n -{int(keep)} "
        f"| xargs -r rm -f -- ; ls -1 {pattern} | wc -l"
    )
    run(["ssh", *SSH_OPTS, host, f"sh -c {shlex.quote(prune)}"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--keep", type=int, default=14, help="number of backups to keep")
    parser.add_argument("--dest", type=Path, default=None, help="backup directory")
    parser.add_argument(
        "--replicate",
        default=os.environ.get("GUARDIAN_BACKUP_REPLICA") or None,
        help="SSH host that keeps an off-hub copy (default: GUARDIAN_BACKUP_REPLICA)",
    )
    parser.add_argument("--no-data", action="store_true", help="only back up the database, skip the data dir tarball")
    parser.add_argument("--ssh-dir", type=Path, default=Path.home() / ".ssh", help="directory with the fleet SSH files")
    args = parser.parse_args(argv)
    keep = max(args.keep, 1)
    dest = args.dest or data_dir() / "backups"
    status = 0
    written: List[Tuple[Path, str]] = []

    db_path = default_db_path()
    try:
        target = backup_database(db_path, dest, keep)
    except (OSError, sqlite3.Error) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        status = 1
    else:
        print(f"backup written: {target}")
        written.append((target, DB_PATTERN))

    if not args.no_data:
        live_db = [db_path, *(db_path.with_name(db_path.name + s) for s in ("-wal", "-shm", "-journal"))]
        try:
            tarball = backup_data(data_dir(), dest, keep, args.ssh_dir, skip=[*live_db, dest])
        except (OSError, tarfile.TarError) as exc:
            print(f"data backup failed: {exc}", file=sys.stderr)
            status = status or 3
        else:
            print(f"data backup written: {tarball}")
            written.append((tarball, DATA_PATTERN))

    if args.replicate:
        for path, pattern in written:
            try:
                replicate_backup(path, args.replicate, keep, pattern)
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", "") or exc
                print(f"replication to {args.replicate} failed: {detail}", file=sys.stderr)
                status = status or 2
                continue
            print(f"replicated to {args.replicate}:~/{REPLICA_DIR}/{path.name}")
    return status


if __name__ == "__main__":
    sys.exit(main())

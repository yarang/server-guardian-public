"""The sandbox version of a release tree and the open-agent name rule
(docs/design/resident-open-tools.md, phase O2: the shared contract's C1 and C8).

`sandbox_version` hashes the four files a node's sandbox consists of, concatenated in the fixed
order, and keeps the first 12 hex characters. The node's installer writes the same value to
/etc/guardian-sandbox/installed.json (`version`), the hub computes it from its release tree (every
release ships deploy/), so the two sides can tell whether a node still runs the sandbox this
release shipped: a probe verdict recorded for another version is stale, and an open agent is then
neither registered nor run.
"""

import hashlib
import re
from pathlib import Path
from typing import Optional, Tuple

# The four files of C1, in order; deploy/ ships in every release tree.
SANDBOX_FILES: Tuple[str, ...] = (
    "deploy/sandbox/guardian-sandbox-run.sh",
    "deploy/sandbox/limits.conf",
    "deploy/sandbox/guardian-sandbox.sudoers",
    "deploy/sandbox/guardian-sandbox-launch.py",
)

# C8: an open agent runs as its own Unix user gsa-<name>, so the resident name (the part before
# the @) must fit it: shorter than a mail name and without the dash the mail address allows.
SANDBOX_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")

VERSION_CHARS = 12


class SandboxVersionError(RuntimeError):
    """A sandbox file of the tree is missing or unreadable, so the version cannot be computed."""


def sandbox_version(root: Optional[Path] = None) -> str:
    """The sandbox version of a repository or release root (default: this checkout's root): the
    first 12 hex characters of the sha256 over the four sandbox files' bytes concatenated in the
    SANDBOX_FILES order. Raises SandboxVersionError naming the file when one cannot be read."""
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative in SANDBOX_FILES:
        path = base / relative
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise SandboxVersionError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()[:VERSION_CHARS]

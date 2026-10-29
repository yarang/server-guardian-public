"""
Keeps a finished debate report as a document version (docs/design/office-export.md, D1 option (c)).

A report of a code or ops debate lives as <data>/decisions/projects/<project>/<thread>.md, outside the artifact store,
so the Word/PowerPoint conversion of office-export.md could not reach it. keep_report() copies the report text, as
it is on disk now, into the thread's artifact store as `report.md`, a new immutable version with the same validators,
secret scan, sha256 manifest and retention as every other artifact. Nothing here runs on an agent's request: the dashboard
button calls it, a person decides.

The text is stored as it is. A report that the artifact validators refuse (raw HTML, a credential-shaped string, more than
artifacts.max_bytes) is refused with their message; nothing is rewritten to make it pass.
"""

import hashlib
import os
import stat
from typing import Any, Dict

import artifact_ops
from artifact_ops import ArtifactError, ArtifactStore

REPORT_NAME = "report.md"
REPORT_TITLE = "Debate report"
REPORT_AGENT = "report-snapshot"


class AlreadyKept(ArtifactError):
    """The latest version already holds this exact report text."""


def _read_report(project_id: str, thread: str) -> str:
    """The report text; ArtifactError when there is none. Symlinks and non-regular files are not followed."""
    from db_manager import decisions_dir

    if artifact_ops.safe_id(project_id) != project_id or artifact_ops.safe_id(thread) != thread:
        raise ArtifactError("the project and the thread need usable ids")
    path = decisions_dir() / "projects" / project_id / f"{thread}.md"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ArtifactError(f"no report for thread {thread} in project {project_id}") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ArtifactError("the report is not a regular file")
        data = handle.read(artifact_ops.safety_limit() + 1)
    if len(data) > artifact_ops.safety_limit():
        raise ArtifactError(f"the report exceeds the safety limit of {artifact_ops.safety_limit()} bytes per file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactError("the report is not valid UTF-8") from None


def keep_report(project: Any, thread: str) -> Dict[str, Any]:
    """Stores the report of `thread` as a new `report.md` version. Returns the manifest plus `warnings`.

    Raises ArtifactError (AlreadyKept when the latest version holds the same text). Design projects are refused: their
    artifacts come from approved write_artifact actions, and a report snapshot would join that lineage."""
    if project.is_design:
        raise ArtifactError(f"project {project.id} is a design project; its documents are its artifacts")
    text = _read_report(project.id, thread)
    store = ArtifactStore(project.id, thread)
    latest = store.latest()
    if latest:
        manifest = store.manifest(latest) or {}
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if any(f["path"] == REPORT_NAME and f.get("sha256") == digest and not f.get("expired")
               for f in manifest.get("files", [])):
            raise AlreadyKept(f"{artifact_ops.format_version(latest)} already holds this report")
    return store.write(REPORT_NAME, text, project=project,
                       base_version=artifact_ops.format_version(latest) if latest else None,
                       title=REPORT_TITLE, agent=REPORT_AGENT)

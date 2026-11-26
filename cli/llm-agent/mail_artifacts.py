"""Hub storage for resident agent artifacts (docs/design/resident-codex-and-policy.md, PD4).

Open resident agents publish deliverables from $HOME/out/ (or $HOME/work/) via `artifact_publish`
on mailbox_mcp.py. The relay forwards base64 content inside the 1 MiB NDJSON frame; this module
validates size and filename, masks secrets on text payloads, writes the bytes atomically under
`<GUARDIAN_DATA_DIR>/resident-artifacts/<safe_agent>/<id>/<filename>` (mode 0600, O_NOFOLLOW), and
records metadata in the `resident_artifacts` table of mail.db (schema 6).

Removing a resident agent never deletes published artifacts on the hub. Standard library only.
"""

import base64
import hashlib
import json
import mimetypes
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ARTIFACT_ID_RE = re.compile(r"^a-[0-9a-f]{12}$", re.ASCII)
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
SAFE_DIR_RE = re.compile(r"[^A-Za-z0-9._@-]+")
MAX_ARTIFACT_BYTES = 512 * 1024  # fits safely inside 1 MiB NDJSON relay line after base64
ARTIFACT_TTL = timedelta(days=90)
TEXT_EXTENSIONS = frozenset((
    ".txt", ".md", ".mmd", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".sh", ".bash", ".sql", ".csv", ".tsv",
    ".html", ".css", ".xml", ".svg", ".diff", ".patch", ".log", ".env", ".rst",
))
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def artifacts_root(data_dir: Path) -> Path:
    return Path(data_dir) / "resident-artifacts"


def safe_agent_dir(agent: str) -> str:
    cleaned = SAFE_DIR_RE.sub("_", str(agent or "").strip()).strip(".")
    return cleaned or "unknown"


_BEARER_OR_KEY = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+|\b(sk-[A-Za-z0-9_-]{16,})\b"
)


def sanitize_filename(name: Any) -> Optional[str]:
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    if not stripped or "/" in stripped or "\\" in stripped or stripped in (".", ".."):
        return None
    if not FILENAME_RE.fullmatch(stripped):
        return None
    return stripped


def is_text_artifact(filename: str, mime: str) -> bool:
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return True
    lower_mime = (mime or "").lower().split(";", 1)[0].strip()
    return lower_mime.startswith("text/") or lower_mime in (
        "application/json", "application/xml", "application/javascript", "application/x-sh",
    )


def _mask_text(text: str) -> str:
    masked = text
    try:
        from audit_run import mask_secrets
        masked = mask_secrets(masked)
    except Exception:  # noqa: BLE001
        pass
    return _BEARER_OR_KEY.sub(lambda m: (m.group(1) + "***") if m.group(1) else "***", masked)


def artifact_file_path(data_dir: Path, agent: str, artifact_id: str, filename: str) -> Path:
    return artifacts_root(data_dir) / safe_agent_dir(agent) / artifact_id / filename


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _read_checked(path: Path) -> Optional[bytes]:
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES * 2:
            return None
        chunks: List[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


class ArtifactStoreMixin:
    """Mixin for MailStore: publishing, listing, retrieving, and pinning resident artifacts."""

    def _artifact_data_dir(self) -> Path:
        # MailStore stores mail.db at <data_dir>/mailbox/mail.db
        return self.path.parent.parent

    def publish_artifact(self, agent: str, filename: str, content_b64: str, title: str, *,
                         rel_path: str = "", task: Optional[str] = None, summary: str = "",
                         mime: str = "", op_id: Optional[str] = None) -> Dict[str, Any]:
        import mail_store

        if mail_store.resident_off():
            return {"ok": False, "reason": "resident_off", "detail": "GUARDIAN_RESIDENT=off"}
        safe_name = sanitize_filename(filename)
        if safe_name is None:
            return {"ok": False, "reason": "bad_filename",
                    "detail": f"filename must match {FILENAME_RE.pattern}"}
        if not isinstance(title, str) or not title.strip():
            return {"ok": False, "reason": "bad_title", "detail": "title must be a non-empty string"}
        if not isinstance(content_b64, str) or not content_b64:
            return {"ok": False, "reason": "bad_content", "detail": "content_b64 is required"}
        try:
            raw = base64.b64decode(content_b64.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError):
            return {"ok": False, "reason": "bad_content", "detail": "content_b64 is not valid base64"}
        if not raw:
            return {"ok": False, "reason": "empty_artifact", "detail": "artifact file is empty"}
        if len(raw) > MAX_ARTIFACT_BYTES:
            return {"ok": False, "reason": "artifact_too_large",
                    "detail": f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes"}

        guessed_mime = (mime or "").strip() or (mimetypes.guess_type(safe_name)[0] or "application/octet-stream")
        masked = False
        if is_text_artifact(safe_name, guessed_mime):
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                cleaned = _mask_text(decoded)
                masked = cleaned != decoded
                raw = cleaned.encode("utf-8")

        sha = hashlib.sha256(raw).hexdigest()
        now_dt = self.now()
        created = mail_store.format_ts(now_dt)
        expires = mail_store.format_ts(now_dt + ARTIFACT_TTL)
        clean_title = title.strip()[:200]
        clean_summary = (summary or "").strip()[:1000]
        clean_rel = (rel_path or safe_name).strip()[:256]
        clean_task = task.strip() if isinstance(task, str) and task.strip() else None

        with self._write() as conn:
            if op_id:
                prior = conn.execute(
                    "SELECT answer_json FROM operations WHERE sender=? AND op_id=?",
                    (agent, op_id),
                ).fetchone()
                if prior is not None:
                    return json.loads(prior["answer_json"])
            artifact_id = mail_store.new_id("a-")
            target = artifact_file_path(self._artifact_data_dir(), agent, artifact_id, safe_name)
            _write_atomic(target, raw)
            conn.execute(
                "INSERT INTO resident_artifacts (id, agent, task, title, filename, rel_path, mime, "
                "sha256, size_bytes, summary, pinned, created, expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (artifact_id, agent, clean_task, clean_title, safe_name, clean_rel, guessed_mime,
                 sha, len(raw), clean_summary, created, expires),
            )
            answer = {
                "ok": True,
                "id": artifact_id,
                "agent": agent,
                "filename": safe_name,
                "title": clean_title,
                "mime": guessed_mime,
                "sha256": sha,
                "size_bytes": len(raw),
                "masked": masked,
                "created": created,
                "expires": expires,
            }
            if op_id:
                conn.execute(
                    "INSERT INTO operations (sender, op_id, answer_json, created) VALUES (?, ?, ?, ?)",
                    (agent, op_id, json.dumps(answer, ensure_ascii=False), created),
                )
            self._event("artifact_published", agent=agent,
                        detail={"id": artifact_id, "filename": safe_name, "size_bytes": len(raw),
                                "task": clean_task, "masked": masked}, conn=conn)
            return answer

    def list_artifacts(self, agent: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._ready()
        if conn is None:
            return []
        cap = max(1, min(int(limit), 500))
        if agent:
            rows = conn.execute(
                "SELECT * FROM resident_artifacts WHERE agent=? ORDER BY created DESC, id DESC LIMIT ?",
                (agent, cap),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM resident_artifacts ORDER BY created DESC, id DESC LIMIT ?",
                (cap,),
            ).fetchall()
        return [self._artifact_row_dict(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise ValueError(f"bad artifact id {artifact_id!r}")
        conn = self._ready()
        if conn is None:
            return None
        row = conn.execute("SELECT * FROM resident_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            return None
        meta = self._artifact_row_dict(row)
        path = artifact_file_path(self._artifact_data_dir(), meta["agent"], meta["id"], meta["filename"])
        raw = _read_checked(path)
        if raw is None:
            return {**meta, "missing": True, "content_b64": None, "text": None}
        text: Optional[str] = None
        if is_text_artifact(meta["filename"], meta["mime"]):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        return {
            **meta,
            "missing": False,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "text": text,
        }

    def pin_artifact(self, artifact_id: str, pinned: bool = True) -> Dict[str, Any]:
        if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.fullmatch(artifact_id):
            return {"ok": False, "reason": "bad_id", "detail": f"bad artifact id {artifact_id!r}"}
        with self._write() as conn:
            cur = conn.execute("UPDATE resident_artifacts SET pinned=? WHERE id=?",
                               (1 if pinned else 0, artifact_id))
            if cur.rowcount == 0:
                return {"ok": False, "reason": "not_found", "detail": f"no artifact {artifact_id}"}
            self._event("artifact_pinned", detail={"id": artifact_id, "pinned": bool(pinned)}, conn=conn)
            return {"ok": True, "id": artifact_id, "pinned": bool(pinned)}

    @staticmethod
    def _artifact_row_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "agent": row["agent"],
            "task": row["task"],
            "title": row["title"],
            "filename": row["filename"],
            "rel_path": row["rel_path"],
            "mime": row["mime"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "summary": row["summary"] or "",
            "pinned": bool(row["pinned"]),
            "created": row["created"],
            "expires": row["expires"],
        }

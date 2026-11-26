"""Solo agent: one role answers one question alone, read-only (docs/design/solo-agent.md).

It is not a flow. There is no Judge, round, thread, approval or action: the answer is labelled SOLO and says it is
unverified. The agent call is the debate engine's own (`DebateEngine._ask`: the ```repo block loop, the repo MCP
server, the citation re-ask, provider fallback), driven from outside the way `audit_run.py` and `wiki_build.py` drive
it, so this module changes no debate code. What makes the closed paths closed:

- the engine gets a scratch decision database (never the real one) and no jev gate (`jev_gate=False`, a falsy value
  that is not None), so no session row, decision or gate call can reach production state;
- the prompt is a persona, the question and `SOLO_RULES`; none of the Judge's action texts is part of it, and a
  persona that judges or proposes actions is refused;
- the answer is never parsed for actions or a verdict, only for citations (`evidence`-style line-span rule).

Records live under `<GUARDIAN_DATA_DIR>/solo-runs/<id>/` (`result.json`, `events.jsonl`) and expire after 30 days.
"""
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import character
import ops_targets
from code_context import ContextError
from db_manager import data_dir
from debate_engine import DebateEngine, RolePrompt, answer_language_rule, repo_mcp_enabled
from debate_events import EventWriter
from llm_runner import BUILTIN_PROVIDER
from mcp_client import MCPClient
from ops_checks import OpsChecks
from persona_manager import PersonaManager
from project_registry import ProjectManager, ProjectValidationError
from role_cards import persona_text
from team_manager import EXPERT_ROLE, TeamManager

SOLO_PROMPT_VERSION = 4  # 2: the general (no-project) rules; 3: general rules no longer promise a tool-less run;
# 4: the language rule without the debate's decision words (a parallel branch's 2, merged)
LABEL = "SOLO"
NOTE = "unverified, no Judge"
GENERAL_NOTE = "unverified, no Judge, no sources"
# A general question attaches no repository and no ops checks, but any provider may answer it (operator's word,
# 2026-10-30): codex and agy keep their built-in tools, so "no sources" is a label, not a guarantee. zai is left out: it
# would send a free-form question to a third-party endpoint that is enabled for code work only. exaone is allowed
# (operator's decision 2026-11-09).
GENERAL_EXCLUDED = ("zai",)
SESSION_KEY = "solo"
DEFAULT_MAX_SOLO = 2
DEFAULT_KEEP_DAYS = 30
DEFAULT_LANGUAGE = "Korean"  # like the audit and the wiki; --language or GUARDIAN_SOLO_LANGUAGE overrides
USAGE, BUSY = 2, 4  # exit codes of a refused run

SOLO_RULES = (
    "You answer this question alone: no other agent reviews your answer and no judge decides on it. Answer from the "
    "code (or the server checks) you read, cite every claim with its full repository path and lines (`path:START-END` "
    "or `path#Symbol`), say plainly what you could not verify, and keep the answer short. This is advice only: do "
    "not propose patches, commands or actions, and do not write action blocks, a decision or a verdict line.")

GENERAL_RULES = (
    "You answer this question alone: no other agent reviews your answer and no judge decides on it. No repository or "
    "server is attached to this run, so answer from general knowledge: do not go looking for files in the working "
    "directory, do not cite file paths or lines, do not claim to have read or checked anything in a real system, say "
    "plainly what you are unsure of or cannot know without looking at one, and keep the answer short. This is advice "
    "only: do not propose patches, commands or actions, and do not write action blocks, a decision or a verdict line.")

# A persona that judges or acts (by id, or by naming the action tool) cannot answer alone: the builtin Judges are
# `*_judge` and `jev_mcp_decision`; `design_judge` also names `jev_mcp` in its text.
_ID_MARKERS = ("judge", "jev_mcp")
_TEXT_MARKER = "jev_mcp"
_REF = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w\-]+\.([A-Za-z0-9]{1,8})):(\d+)")


# The id form of a record, also what the hub (apps/agent/solo_handler.go) generates and passes as --solo-id.
RUN_ID = re.compile(r"^solo_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")


class SoloError(Exception):
    """A solo run that cannot start. `code` is the exit code (USAGE, or BUSY when every slot is taken)."""

    def __init__(self, message: str, code: int = USAGE) -> None:
        super().__init__(message)
        self.code = code


def solo_root() -> Path:
    return data_dir() / "solo-runs"


def max_solo() -> int:
    try:
        return max(1, int(os.environ.get("GUARDIAN_MAX_SOLO", DEFAULT_MAX_SOLO)))
    except ValueError:
        return DEFAULT_MAX_SOLO


@contextlib.contextmanager
def solo_slot(root: Path, limit: int) -> Iterator[int]:
    """One of `limit` slots, held with flock so a crashed run frees its slot. Refuses (BUSY), never queues."""
    root.mkdir(parents=True, exist_ok=True)
    for number in range(limit):
        handle = open(root / f".slot-{number}", "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            continue
        try:
            yield number
        finally:
            handle.close()  # closing releases the lock
        return
    raise SoloError(f"busy: {limit} solo run(s) already running (GUARDIAN_MAX_SOLO)", BUSY)


def resolve_persona(manager: PersonaManager, role: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """The persona id and record of the role; default is the default team's expert. A role that judges is refused."""
    persona_id = role or TeamManager(persona_mgr=manager).load(None).expert.persona
    persona = manager.get_persona(persona_id)
    if persona is None:
        raise SoloError(f"unknown role {persona_id!r}")
    if character.is_character(persona):
        # a character's seats say where it may run (agent-characters.md, C2); no id or text markers to read
        if "solo" not in (persona.get("seats") or []):
            raise SoloError(f"role {persona_id!r} has no solo seat")
        return persona_id, persona
    text = str(persona.get("system_prompt") or "")
    if any(marker in persona_id.lower() for marker in _ID_MARKERS) or _TEXT_MARKER in text:
        raise SoloError(f"role {persona_id!r} judges or proposes actions: a solo run only answers")
    return persona_id, persona


def missing_refs(text: str, known: List[str]) -> List[str]:
    """`file.ext:N` citations that name no file of the tree: an extension the tree uses, but no such path or name."""
    paths = set(known)
    names = {p.rsplit("/", 1)[-1] for p in paths}
    suffixes = {n.rsplit(".", 1)[-1] for n in names if "." in n}
    found: List[str] = []
    for match in _REF.finditer(text or ""):
        path, suffix = match.group(1).lstrip("./"), match.group(2)
        if suffix in suffixes and path not in paths and path.rsplit("/", 1)[-1] not in names:
            ref = f"{path}:{match.group(3)}"
            if ref not in found:
                found.append(ref)
    return found


def citation_summary(citations: Dict[str, List[str]]) -> str:
    return (f"{len(citations['verified'])} verified, {len(citations['unshown'])} unshown, "
            f"{len(citations['missing'])} missing")


class SoloRunner:
    """One solo question. run() returns the result dict (also stored as result.json)."""

    def __init__(self, project_id: Optional[str], role: Optional[str] = None, provider: Optional[str] = None,
                 language_name: Optional[str] = None, root: Optional[Path] = None,
                 run_id: Optional[str] = None, general: bool = False) -> None:
        if run_id is not None and not RUN_ID.match(run_id):
            raise SoloError(f"invalid solo id {run_id!r}")
        self.general = general
        self.project: Optional[Any] = None
        if general:
            if project_id:
                raise SoloError("a general question has no project")
        else:
            try:
                self.project = ProjectManager().load(project_id)
            except ProjectValidationError as exc:
                raise SoloError(f"project: {exc}") from exc
            if self.project.is_design:
                raise SoloError(f"project {self.project.id} is a design project: a solo run reads code or server "
                                "checks only, use a design debate")
        if general and provider in GENERAL_EXCLUDED:
            raise SoloError(f"provider {provider!r} cannot answer a general question: it is enabled for code work only")
        self.root = root or solo_root()
        self.run_id = run_id or f"solo_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.provider = provider
        self.language_name = language_name
        self.record = self.root / self.run_id
        # The decision database is scratch: _call stores agent sessions in it, and the real one must never see them.
        self._scratch = Path(tempfile.mkdtemp(prefix="guardian-solo-"))
        self.events = EventWriter(str(self.record / "events.jsonl"), thread_id=self.run_id)
        try:
            self.engine = DebateEngine(max_rounds=1, db_path=self._scratch / "session.db", jev_gate=False,
                                       events=self.events)
            self.persona_id, self.persona = resolve_persona(self.engine.persona_mgr, role)
            available = self.engine.llm.available_providers
            if provider and provider not in available:
                raise SoloError(f"provider {provider!r} is not available here ({', '.join(available) or 'none'})")
            # The providers _ask may use: the pinned one, or the default order (a general question skips GENERAL_EXCLUDED).
            self.providers: Optional[List[str]] = [provider] if provider else None
            if general and not provider:
                self.providers = [name for name in available if name not in GENERAL_EXCLUDED]
                if not self.providers:
                    raise SoloError("a general question needs a provider other than zai, and none is available here")
        except BaseException:
            self.close()
            raise

    # --- setup -------------------------------------------------------------------------------------------------

    def _prepare(self, question: str, paths: Optional[List[str]], ref: Optional[str]) -> Tuple[str, str]:
        """Binds the engine to the project (repository tools or server checks). Returns (task text, commit sha)."""
        engine, project = self.engine, self.project
        language = self.language_name or os.environ.get("GUARDIAN_SOLO_LANGUAGE", "").strip() or DEFAULT_LANGUAGE
        engine._wiki_exposure = False
        engine._language_rule = answer_language_rule(language, debate=False)
        engine._context_changed = False
        engine._ops = None
        if project is None:  # general question: no repository, no server checks, nothing to read
            if paths or ref:
                raise SoloError("a general question reads no code: drop the context paths and ref")
            engine._context_text = ""
            return f"Question: {question}\n\n", ""
        task = f"Project: {project.id}\nQuestion: {question}\n\n"
        if project.is_ops:
            if paths or ref:
                raise SoloError(f"project {project.id} is an ops project: it reads no code, drop the context paths")
            engine.mcp = MCPClient(workspace=None, project=project)
            engine._setup_retrieval(None, None, project.id, repository=False)
            targets = ops_targets.resolve(project)
            engine._ops = OpsChecks(project, resolved=targets, provisioning=False)
            engine._context_text = ""
            return task + ops_targets.prompt_text(project, targets) + "\n\n", ""
        if not project.has_workspace:
            raise SoloError(f"project {project.id} has no workspace to read")
        engine.mcp = MCPClient(workspace=project.workspace, project=project)
        try:
            pack = engine._resolve_context(SimpleNamespace(context=None), None, paths, ref, project.context)
        except ContextError as exc:
            raise SoloError(f"context: {exc}") from exc
        pack = engine._setup_retrieval(pack, None, project.id)
        if engine._repo is None:
            raise SoloError("retrieval is not available for this workspace (GUARDIAN_RETRIEVAL off, or not a git repository)")
        engine._flush_compiler_events()
        engine._context_text = pack.text if pack and pack.snapshot.get("paths") else ""
        return task, str(engine._repo.sha)

    @property
    def mode(self) -> str:
        """`general` (no project), `ops` (server checks) or `code` (repository tools)."""
        if self.project is None:
            return "general"
        return "ops" if self.project.is_ops else "code"

    def prompt(self, task: str) -> RolePrompt:
        return RolePrompt(persona=persona_text(self.persona), task=task,
                          rules=f"{GENERAL_RULES if self.general else SOLO_RULES} {self.engine._language_rule}")

    # --- checks on the answer ------------------------------------------------------------------------------------

    def citations(self, name: str, text: str) -> Dict[str, List[str]]:
        """verified / unshown / missing citations of the answer. The engine's check resolves tree paths only, so a
        `file.ext:N` that names no file of the tree is found here (`missing`)."""
        engine = self.engine
        if engine._repo is None:
            return {"verified": [], "unshown": [], "missing": []}
        check = engine._citation_check(SESSION_KEY, EXPERT_ROLE, name, text)
        held = set(check.never) | {path for path, _missed, _shown in check.partial}
        return {"verified": [p for p in check.paths if p not in held], "unshown": list(check.unverified),
                "missing": missing_refs(text, engine._repo.readable_paths())}

    # --- run -----------------------------------------------------------------------------------------------------

    def run(self, question: str, context_paths: Optional[List[str]] = None, context_ref: Optional[str] = None) -> Dict[str, Any]:
        question = question.strip()
        if not question:
            raise SoloError("the question is empty")
        try:
            with solo_slot(self.root, max_solo()):
                return self._run(question, context_paths, context_ref)
        finally:
            self.close()

    def _run(self, question: str, paths: Optional[List[str]], ref: Optional[str]) -> Dict[str, Any]:
        record = self.record
        record.mkdir(parents=True, exist_ok=True)
        engine, started = self.engine, time.monotonic()
        try:
            task, sha = self._prepare(question, paths, ref)
            project_id = self.project.id if self.project is not None else ""
            self.events.emit("solo_started", {"project": project_id, "mode": self.mode, "role": self.persona_id,
                                              "sha": sha, "prompt_version": SOLO_PROMPT_VERSION})
            name = str(self.persona.get("name") or EXPERT_ROLE)
            trace: Dict[str, Any] = {}
            code_tools = "mcp" if engine._repo is not None and repo_mcp_enabled() else None
            provider, text, failures = engine._ask(
                self.run_id, EXPERT_ROLE, self.prompt(task), {}, "", SESSION_KEY,
                self.providers, round_num=1, agent_name=name, trace=trace,
                code_tools=code_tools)
            degraded = provider == BUILTIN_PROVIDER
            citations = ({"verified": [], "unshown": [], "missing": []} if degraded or self.general
                         else self.citations(name, text))
            result: Dict[str, Any] = {
                "id": self.run_id, "label": LABEL, "note": GENERAL_NOTE if self.general else NOTE,
                "mode": self.mode, "prompt_version": SOLO_PROMPT_VERSION,
                "project": project_id, "role": self.persona_id, "provider": provider, "sha": sha,
                "question": question, "answer": text, "degraded": degraded, "citations": citations,
                "summary": "no sources" if self.general else citation_summary(citations), "steps": trace.get("steps"),
                "failures": [*failures, *([engine.llm.last_error] if degraded and engine.llm.last_error else [])],
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "elapsed_s": round(time.monotonic() - started, 1),
            }
            self.events.emit("solo_finished", {"degraded": degraded, "summary": result["summary"]})
            self._store(record, result)
            return result
        finally:
            engine._close_go_index()
            self.events.close()

    @staticmethod
    def _store(record: Path, result: Dict[str, Any]) -> None:
        tmp = record / "result.json.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, record / "result.json")

    def close(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)


def render(result: Dict[str, Any]) -> str:
    """The plain-text form of a result: the label first, so it is never read as a verdict."""
    head = (f"{LABEL} answer ({result.get('note') or NOTE})"
            + (" - DEGRADED: no provider answered" if result.get("degraded") else ""))
    sha = str(result.get("sha") or "")[:12]
    where = f"project {result['project']}" if result.get("project") else "general question (no project)"
    lines = [head, f"{where} | role {result['role']} | provider {result['provider']}"
             + (f" | commit {sha}" if sha else ""), f"citations: {result['summary']}", "", str(result["answer"]).rstrip()]
    for kind in ("unshown", "missing"):
        if result["citations"].get(kind):
            lines += ["", f"{kind} citations: " + ", ".join(result["citations"][kind][:10])]
    return "\n".join(lines) + "\n"


def prune(root: Optional[Path] = None, days: Optional[int] = None, now: Optional[float] = None) -> List[str]:
    """Removes solo records older than `days` (default 30, GUARDIAN_SOLO_KEEP_DAYS). Returns the removed ids."""
    root = root or solo_root()
    if days is None:
        try:
            days = int(os.environ.get("GUARDIAN_SOLO_KEEP_DAYS", DEFAULT_KEEP_DAYS))
        except ValueError:
            days = DEFAULT_KEEP_DAYS
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed: List[str] = []
    if not root.is_dir():
        return removed
    for entry in sorted(root.glob("solo_*")):
        stamp = entry / "result.json"
        mtime = (stamp if stamp.exists() else entry).stat().st_mtime
        if entry.is_dir() and mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed

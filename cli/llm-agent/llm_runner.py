"""LLM runner for installed provider CLIs and built-in heuristic reasoning.

Provider CLIs are used with their own subscription login (claude, codex, agy, ...).
Providers are tried in order and the next one is used when a call fails.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field

from env_policy import provider_env
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Provider name returned when no external LLM CLI produced a response.
BUILTIN_PROVIDER = "built-in-expert"
# Every built-in reply starts with this line, so nobody mistakes it for a model's analysis.
BUILTIN_BANNER = "⚠️ [LLM 응답 없음] 모든 LLM CLI 호출이 실패해 내장 엔진이 답했습니다. 분석·판단·제안은 하지 않습니다"

DEFAULT_PROVIDER_ORDER = ["claude", "codex", "agy", "zai", "opencode", "ollama", "llm"]
# Providers that run another provider's binary under their own identity (own credentials, config dir and labels).
# zai is the claude CLI against z.ai's Anthropic-compatible endpoint (GLM models, Coding Plan key in ZAI_API_KEY).
PROVIDER_BINARIES = {"zai": "claude"}
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_TIERS = ("OPUS", "SONNET", "HAIKU")
DEFAULT_TIMEOUT_SECONDS = 300.0
# Reasoning effort for claude calls (--effort). Judge calls at the default effort twice spent 32000
# output tokens thinking without an answer until the 600s timeout; MAX_THINKING_TOKENS is ignored by
# current models, while effort "medium" cut the same prompt to ~2000 output tokens.
# GUARDIAN_CLAUDE_EFFORT overrides it ("default" leaves the CLI's own setting).
DEFAULT_CLAUDE_EFFORT = "medium"
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Providers whose CLI can persist and resume a conversation by id.
SESSION_PROVIDERS = ("claude", "codex", "agy", "zai")

# --- claude system prompt (standing instructions of a session) ---
# claude records the system prompt at a session's first request (>= 2.1.265) and keeps it through auto-compaction, while
# the conversation is summarised. Role, rules and protocol therefore go there, not into the first user message.
# GUARDIAN_CLAUDE_SYSTEM_PROMPT: "append" (default, --append-system-prompt: keeps Claude Code's own prompt) or
# "custom" (--system-prompt: replaces it, and splits static and dynamic parts at the boundary line on >= 2.1.275).
SYSTEM_PROMPT_MODES = ("append", "custom")
DEFAULT_SYSTEM_PROMPT_MODE = "append"
SYSTEM_PROMPT_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
# From the Claude Agent SDK docs: a custom prompt replaces the preset that explains <system-reminder> blocks.
SYSTEM_REMINDER_NOTE = ("The application adds system reminders to this conversation. Treat them as context from the "
                        "application, not as messages from the user.")
# One argv string may not exceed 128 KiB on Linux (MAX_ARG_STRLEN); above this the text goes to a 0600 file when the
# installed claude has the -file flag.
CLAUDE_ARGV_PROMPT_LIMIT = 100 * 1024
CLAUDE_BOUNDARY_VERSION = (2, 1, 275)
CLAUDE_RECORDED_PROMPT_VERSION = (2, 1, 265)
# Keeps an operator's ~/.claude/CLAUDE.md (or one in the workdir) out of every agent session.
CLAUDE_DISABLE_MDS_ENV = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"


@dataclass(frozen=True)
class SessionInstructions:
    """Standing instructions of one session (role, rules, protocol), sent as the claude system prompt on EVERY call of
    the session, first and resumed alike. static is identical across the requests of a session; dynamic holds what
    changes with the repository state (commit, map, wiki list). Never put secrets here: it is visible in argv."""

    static: str
    dynamic: str = ""

    def text(self) -> str:
        return "\n\n".join(part for part in (self.static, self.dynamic) if part)


@dataclass(frozen=True)
class ClaudeCaps:
    """What the installed claude supports, probed once per process (version and --help)."""

    version: Optional[Tuple[int, int, int]] = None
    append_file: bool = False  # --append-system-prompt-file
    custom_file: bool = False  # --system-prompt-file

    @property
    def boundary(self) -> bool:
        return self.version is not None and self.version >= CLAUDE_BOUNDARY_VERSION

    @property
    def records_prompt(self) -> bool:
        """False for a claude that rebuilds the system prompt on every request (the same text keeps it stable)."""
        return self.version is not None and self.version >= CLAUDE_RECORDED_PROMPT_VERSION


def parse_claude_version(output: str) -> Optional[Tuple[int, int, int]]:
    """(major, minor, patch) from `claude --version` ("2.1.289 (Claude Code)"); None when it does not parse."""
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    return (int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None


_CLAUDE_CAPS: Optional[ClaudeCaps] = None
_CAPS_LOCK = threading.Lock()


# codex features switched off for repo-MCP runs. shell_tool removes exec_command and write_stdin (codex's only shell),
# view_image the local-file image reader, apps/plugins/browser_use/computer_use the bundled app tools and web__run.
# apply_patch has no switch in codex 0.160.0; it cannot write under --sandbox read-only and never echoes file content.
CODEX_MCP_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "view_image", "multi_agent", "multi_agent_v2", "image_generation", "goals",
    "apps", "plugins", "browser_use", "computer_use", "skill_search", "tool_suggest", "hooks",
)
# stderr fragments of a codex that does not know a flag above (older CLI) or renamed a feature.
CODEX_OLD_CLI_MARKERS = ("unknown feature flag", "unexpected argument", "unknown configuration field",
                         "unrecognized option", "unknown option", "unrecognized subcommand")
# --json item types that mean codex ran its own shell, edited files or searched the web.
CODEX_FORBIDDEN_ITEMS = ("command_execution", "file_change", "web_search")


@dataclass
class LLMResult:
    """Response from a session-aware call. session_id is None for stateless providers."""

    provider: str
    text: str
    session_id: Optional[str] = None
    resumed: bool = False
    # Why earlier providers in the order were skipped ("claude: timed out after 300s", ...).
    failures: List[str] = field(default_factory=list)
    # Fleet host id the call ran on ('' = this machine); set by remote_agents, a session id is only valid there.
    host: str = ""


def provider_binary(name: str) -> str:
    """The executable a provider runs (zai runs claude)."""
    return PROVIDER_BINARIES.get(name, name)


def zai_config_dir(source: Dict[str, str]) -> Path:
    """claude config dir of the zai identity: stable (sessions are resumed from it) and without the operator's login."""
    return Path(source.get("GUARDIAN_ZAI_CONFIG_DIR") or "~/.guardian/claude-zai").expanduser()


def zai_claude_env(source: Dict[str, str]) -> Dict[str, str]:
    """Non-secret environment that points the claude CLI at z.ai (the key is added by the caller, or by the shell as
    "$ZAI_API_KEY" where it must stay out of argv). Models are left to the server unless ZAI_MODEL or
    ZAI_{OPUS,SONNET,HAIKU}_MODEL name one."""
    env = {"ANTHROPIC_BASE_URL": source.get("ZAI_BASE_URL") or ZAI_DEFAULT_BASE_URL,
           "CLAUDE_CONFIG_DIR": str(zai_config_dir(source)), CLAUDE_DISABLE_MDS_ENV: "1"}
    for tier in ZAI_TIERS:
        model = source.get(f"ZAI_{tier}_MODEL") or source.get("ZAI_MODEL")
        if model:
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
    return env


def llm_env_path() -> Path:
    """File with provider credentials such as CLAUDE_CODE_OAUTH_TOKEN (KEY=value lines, mode 600)."""
    return Path(os.environ.get("GUARDIAN_LLM_ENV", "~/.guardian/llm.env")).expanduser()


def load_llm_env(path: Optional[Path] = None) -> Dict[str, str]:
    """Parses a shell-style env file. Non-interactive ssh/nohup runs do not source shell profiles,
    so provider credentials are read from this file instead."""
    path = path or llm_env_path()
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def session_workdir() -> Path:
    """Stable, empty working directory shared by all persisted sessions.

    claude stores sessions per working directory, so resume only works from the same path.
    """
    path = Path(os.environ.get("GUARDIAN_LLM_WORKDIR", "~/.guardian/llm-sessions")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def interactive_resume_command(provider: str, session_id: str) -> List[str]:
    """Command that opens a persisted session interactively (used for tmux attach)."""
    commands = {
        "claude": ["claude", "--resume", session_id],
        "codex": ["codex", "resume", session_id],
        "agy": ["agy", "--conversation", session_id],
        "zai": ["claude", "--resume", session_id],
    }
    if provider not in commands:
        raise ValueError(f"provider {provider!r} does not support session resume")
    return commands[provider]


class LLMRunner:
    _CLAUDE_ENV_KEYS = {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "API_TIMEOUT_MS",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
    }

    def __init__(
        self,
        claude_config_path: Optional[Path] = None,
        providers: Optional[List[str]] = None,
    ) -> None:
        # Optional Anthropic-compatible endpoint override (e.g. Z.ai). Off by default so
        # the claude CLI uses its own subscription login.
        env_config = os.environ.get("GUARDIAN_CLAUDE_CONFIG")
        self.claude_config_path = claude_config_path or (Path(env_config).expanduser() if env_config else None)

        # Environment for every provider CLI: the caller's env plus the credentials file.
        self.base_env: Dict[str, str] = {**os.environ, **load_llm_env()}
        # agy and user-level codex installs live in ~/.local/bin, which non-interactive shells omit.
        # Put it first, as Ubuntu's ~/.profile does, so a user install wins over an older system one.
        local_bin = str(Path.home() / ".local" / "bin")
        path_entries = [e for e in self.base_env.get("PATH", "").split(os.pathsep) if e and e != local_bin]
        self.base_env["PATH"] = os.pathsep.join([local_bin, *path_entries])
        order = providers or self._provider_order_from_env()
        search_path = self.base_env.get("PATH")
        self.available_providers = [name for name in order if self._installed(name, search_path)]
        # Every known provider installed here; a team member may pick one outside the host's default order.
        self.installed_providers = [name for name in DEFAULT_PROVIDER_ORDER if self._installed(name, search_path)]
        self.detected_cli = self.available_providers[0] if self.available_providers else None
        self.timeout = self._timeout_from_env()
        # Reason the most recent external CLI call failed, if it did.
        self.last_error: Optional[str] = None

    def _installed(self, name: str, search_path: Optional[str]) -> bool:
        """The provider's binary is on PATH and, for zai, its own key is present (claude's login does not count)."""
        if shutil.which(provider_binary(name), path=search_path) is None:
            return False
        return name != "zai" or bool(self.base_env.get("ZAI_API_KEY"))

    @staticmethod
    def _provider_order_from_env() -> List[str]:
        raw = os.environ.get("GUARDIAN_LLM_PROVIDERS", "")
        names = [name.strip() for name in raw.split(",") if name.strip()]
        return names or list(DEFAULT_PROVIDER_ORDER)

    @staticmethod
    def _timeout_from_env() -> float:
        try:
            return max(float(os.environ.get("GUARDIAN_LLM_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)), 1.0)
        except ValueError:
            return DEFAULT_TIMEOUT_SECONDS

    def _provider_env(self, name: str) -> Dict[str, str]:
        """Minimal environment for one provider CLI: locale, paths and that provider's own credentials.

        Never AGENT_TOKEN, jev keys or another provider's credentials (env_policy), so a
        prompt-injected CLI cannot read them from its environment.
        """
        return provider_env(name, self.base_env)

    def _claude_effort_args(self) -> List[str]:
        effort = self.base_env.get("GUARDIAN_CLAUDE_EFFORT", DEFAULT_CLAUDE_EFFORT).strip().lower()
        return ["--effort", effort] if effort in CLAUDE_EFFORTS else []

    def _zai_process_config(self) -> Tuple[Dict[str, str], float]:
        """Environment of the claude binary run as zai: base env and ZAI_* only (env_policy), mapped onto the names claude
        reads. ZAI_* itself is dropped, and the claude login (OAuth token, ANTHROPIC_API_KEY) never gets in."""
        source = self._provider_env("zai")
        key = source.get("ZAI_API_KEY")
        if not key:
            raise RuntimeError("ZAI_API_KEY is not set")
        env = {k: v for k, v in source.items() if not k.startswith("ZAI_")}
        env.update(zai_claude_env(self.base_env))
        env["ANTHROPIC_AUTH_TOKEN"] = key
        config_dir = zai_config_dir(self.base_env)
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir.chmod(0o700)
        marker = config_dir / ".claude.json"
        if not marker.exists():
            # A fresh config dir would start claude's first-run onboarding, which z.ai's guide skips this way.
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"hasCompletedOnboarding": True}, handle)
        return env, self.timeout

    def _claude_family_config(self, provider: str) -> Tuple[Dict[str, str], float]:
        """Process env and timeout of a claude-binary provider: claude itself or zai."""
        return self._zai_process_config() if provider == "zai" else self._claude_process_config()

    def _claude_process_config(self) -> Tuple[Dict[str, str], float]:
        process_env = self._provider_env("claude")
        # Agents get their instructions from us only: no CLAUDE.md of the operator or of the workdir reaches them.
        process_env[CLAUDE_DISABLE_MDS_ENV] = "1"
        timeout = self.timeout
        if self.claude_config_path is None:
            return process_env, timeout

        try:
            config = json.loads(self.claude_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}

        configured_env = config.get("env", {}) if isinstance(config, dict) else {}
        if isinstance(configured_env, dict):
            for key in self._CLAUDE_ENV_KEYS:
                value = configured_env.get(key)
                if isinstance(value, str):
                    process_env[key] = value

        if process_env.get("ANTHROPIC_AUTH_TOKEN"):
            process_env.pop("ANTHROPIC_API_KEY", None)

        try:
            timeout = max(int(process_env["API_TIMEOUT_MS"]) / 1000, 1.0)
        except (KeyError, TypeError, ValueError):
            pass

        return process_env, timeout

    # --- claude system prompt ---

    def claude_caps(self) -> ClaudeCaps:
        """Version and flag support of the installed claude, probed once per process (`--version`, `--help`)."""
        global _CLAUDE_CAPS
        with _CAPS_LOCK:
            if _CLAUDE_CAPS is None:
                _CLAUDE_CAPS = self._probe_claude()
            return _CLAUDE_CAPS

    def _probe_claude(self) -> ClaudeCaps:
        env = self._provider_env("claude")

        def ask(flag: str) -> str:
            try:
                proc = subprocess.run(["claude", flag], capture_output=True, text=True, timeout=30, env=env,
                                      cwd=str(session_workdir()))
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("claude %s failed, assuming no optional flags: %s", flag, exc)
                return ""
            return proc.stdout or "" if proc.returncode == 0 else ""

        version = parse_claude_version(ask("--version"))
        help_text = ask("--help")
        caps = ClaudeCaps(
            version=version,
            # --help prints "--append-system-prompt[-file]" or the two flags apart
            append_file=bool(re.search(r"--append-system-prompt(?:-file|\[-file\])", help_text)),
            custom_file=bool(re.search(r"--system-prompt(?:-file|\[-file\])", help_text)),
        )
        if version is None:
            logger.warning("claude version unknown: system prompts go in argv, without the dynamic boundary")
        elif not caps.records_prompt:
            logger.info("claude %s rebuilds the system prompt on every request (recording needs >= %s); the same "
                        "text is sent each time", ".".join(map(str, version)),
                        ".".join(map(str, CLAUDE_RECORDED_PROMPT_VERSION)))
        return caps

    def _claude_system_mode(self) -> str:
        mode = self.base_env.get("GUARDIAN_CLAUDE_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT_MODE).strip().lower()
        return mode if mode in SYSTEM_PROMPT_MODES else DEFAULT_SYSTEM_PROMPT_MODE

    # @MX:NOTE: The text is the same on every call of a session. With the default append mode the boundary line does
    # @MX:NOTE: not apply (it splits --system-prompt only), so static comes first and dynamic last for prefix caching.
    def _claude_system_args(self, instructions: SessionInstructions, scratch: Path) -> List[str]:
        """claude flags carrying the standing instructions: --append-system-prompt (default) or --system-prompt.

        Text over CLAUDE_ARGV_PROMPT_LIMIT goes to a 0600 file in scratch when this claude has the -file flag;
        otherwise it stays in argv.
        """
        caps = self.claude_caps()
        custom = self._claude_system_mode() == "custom"
        if custom:
            static = f"{SYSTEM_REMINDER_NOTE}\n\n{instructions.static}"
            joiner = f"\n{SYSTEM_PROMPT_BOUNDARY}\n" if (caps.boundary and instructions.dynamic) else "\n\n"
            text = static + joiner + instructions.dynamic if instructions.dynamic else static
        else:
            text = instructions.text()
        flag = "--system-prompt" if custom else "--append-system-prompt"
        has_file = caps.custom_file if custom else caps.append_file
        if len(text.encode("utf-8")) > CLAUDE_ARGV_PROMPT_LIMIT:
            if has_file:
                path = scratch / "system_prompt.txt"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                return [f"{flag}-file", str(path)]
            logger.warning("system prompt is %d bytes and this claude has no %s-file: sending it in argv",
                           len(text.encode("utf-8")), flag)
        return [flag, text]

    # --- Provider invocations: each returns the response text or raises RuntimeError ---

    @staticmethod
    def _check(name: str, proc: subprocess.CompletedProcess, output: Optional[str] = None) -> str:
        text = (output if output is not None else proc.stdout or "").strip()
        if proc.returncode == 0 and text:
            return text
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{name} exited with code {proc.returncode}: {detail[-300:]}")

    def _run_claude(self, prompt: str, workdir: str, provider: str = "claude") -> str:
        process_env, timeout = self._claude_family_config(provider)
        proc = subprocess.run(
            ["claude", "--print", "--output-format", "text", "--no-session-persistence", "--tools", "",
             *self._claude_effort_args()],
            # --tools is variadic and would swallow a positional prompt, so send it on stdin.
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=process_env,
            cwd=workdir,
        )
        return self._check(provider, proc)

    def _run_codex(self, prompt: str, workdir: str) -> str:
        output_file = Path(workdir) / "codex_last_message.txt"
        proc = subprocess.run(
            [
                "codex", "exec",
                "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only",
                "--color", "never",
                "-o", str(output_file),
                "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=workdir,
            env=self._provider_env("codex"),
        )
        output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        return self._check("codex", proc, output)

    def _agy(self, prompt: str, session_id: Optional[str], workdir: str) -> Tuple[str, str]:
        """Runs one agy turn with the prompt on stdin (stream-json), never in argv where ps shows it."""
        args = [
            "agy",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--print-timeout", f"{int(self.timeout)}s",
            "--disable-slash-commands",
            # Print mode with an empty argument: the prompt comes from the stream on stdin.
            "--print=",
        ]
        if session_id:
            args += ["--conversation", session_id]
        message = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        proc = subprocess.run(
            args, input=message, capture_output=True, text=True, timeout=self.timeout + 15, cwd=workdir,
            env=self._provider_env("agy"),
        )
        raw = self._check("agy", proc)
        result = None
        # Assistant text per step: a turn may hold several agent_response steps (with tool steps between).
        steps: Dict[Any, str] = {}
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "result":
                result = event.get("result") or {}
            elif event.get("event") == "step_update":
                update = event.get("step_update") or {}
                if update.get("step_type") == "agent_response" and update.get("text_delta"):
                    index = update.get("step_index", len(steps))
                    steps[index] = steps.get(index, "") + str(update["text_delta"])
        if result is None:
            raise RuntimeError(f"agy returned no result event: {raw[:200]}")
        if result.get("status") != "SUCCESS" or not str(result.get("response", "")).strip():
            detail = result.get("error") or result.get("response")
            raise RuntimeError(f"agy status {result.get('status')}: {str(detail)[:300]}")
        # `response` may carry only the last step, so the turn text is built from every agent_response step.
        blocks = list(steps.values())
        final = str(result["response"]).strip()
        squeeze = lambda t: " ".join(t.split())  # noqa: E731 - whitespace differences must not cause a duplicate
        if not blocks or squeeze(final) not in squeeze(self._join_turn(blocks)):
            blocks.append(final)
        return self._join_turn(blocks), str(result.get("conversation_id") or session_id)

    def _run_agy(self, prompt: str, workdir: str) -> str:
        return self._agy(prompt, None, workdir)[0]

    def _run_simple(self, name: str, args: List[str], workdir: str) -> str:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=self.timeout, cwd=workdir, env=self._provider_env(name)
        )
        return self._check(name, proc)

    def _invoker(self, name: str) -> Optional[Callable[[str, str], str]]:
        simple = {
            "opencode": lambda p, d: self._run_simple("opencode", ["opencode", "run", p], d),
            "ollama": lambda p, d: self._run_simple("ollama", ["ollama", "run", "llama3", p], d),
            "llm": lambda p, d: self._run_simple("llm", ["llm", p], d),
        }
        runners = {"claude": self._run_claude, "codex": self._run_codex, "agy": self._run_agy,
                   "zai": lambda p, d: self._run_claude(p, d, "zai")}
        return runners.get(name) or simple.get(name)

    # --- Session-aware invocations: return (text, session_id) or raise RuntimeError ---

    @staticmethod
    def _join_turn(blocks: List[str]) -> str:
        """The text of one turn: every assistant text block, in order, separated by blank lines."""
        return "\n\n".join(b.strip() for b in blocks if b and b.strip())

    # @MX:NOTE: `claude --output-format json` `result` holds only the LAST text block of a turn. A model that
    # @MX:NOTE: writes a ```repo block, thinks, then writes more text would hide the block from the engine.
    @classmethod
    def _parse_claude_stream(cls, raw: str, after_tools: bool = False) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        """(turn text, session id, result event) from `--output-format stream-json --verbose` lines.

        The turn text joins the text blocks of all top-level assistant messages (sub-agent messages
        carry parent_tool_use_id and are skipped). When no assistant text was seen, the result
        event's own `result` is used. With after_tools (MCP runs) only the text written after the
        last tool call counts: narration between tool calls is not part of the answer.
        """
        blocks: List[str] = []
        final: Optional[Dict[str, Any]] = None
        session_id: Optional[str] = None
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("session_id"):
                session_id = str(event["session_id"])
            if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
                content = (event.get("message") or {}).get("content")
                if isinstance(content, str):
                    blocks.append(content)
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text":
                            blocks.append(str(b.get("text", "")))
                        elif b.get("type") == "tool_use" and after_tools:
                            blocks.clear()
            elif event.get("type") == "result":
                final = event
        text = cls._join_turn(blocks)
        if not text and final is not None:
            text = str(final.get("result") or "").strip()
        return text, session_id, final

    @staticmethod
    def _repo_mcp_server_args(spec: Dict[str, Any]) -> List[str]:
        """The repo_mcp.py command line for a spec: {workspace, sha, log, limits[, cache_dir, wiki_dir]} and, for a
        repository snapshot on a model worker, {no_index, sha_label, seed_steps, seed_bytes}."""
        args = [str(Path(__file__).resolve().parent / "repo_mcp.py"),
                "--workspace", str(spec["workspace"]), "--sha", str(spec["sha"]), "--log", str(spec["log"])]
        limits = spec.get("limits") or {}
        for flag, key in (("--max-steps", "steps"), ("--max-step-bytes", "step_bytes"), ("--max-turn-bytes", "turn_bytes")):
            if limits.get(key):
                args += [flag, str(int(limits[key]))]
        if spec.get("cache_dir"):
            args += ["--cache-dir", str(spec["cache_dir"])]
        if spec.get("wiki_dir"):
            args += ["--wiki-dir", str(spec["wiki_dir"])]
        if spec.get("no_index"):
            args.append("--no-index")
        if spec.get("sha_label"):
            args += ["--sha-label", str(spec["sha_label"])]
        for flag, key in (("--seed-steps", "seed_steps"), ("--seed-bytes", "seed_bytes")):
            if spec.get(key):
                args += [flag, str(int(spec[key]))]
        return args

    # @MX:WARN: Writes a temp MCP config that starts a subprocess with access to the pinned repository.
    # @MX:REASON: The config dir is private (0700/0600) and removed in finally; the server can only call RepoTools.
    def _claude_mcp_config(self, spec: Dict[str, Any], env: Dict[str, str], scratch: Path) -> Path:
        """Writes the MCP config defining server "repo" (repo_mcp.py) and returns its path (mode 0600)."""
        import sys

        from repo_mcp import SERVER_KEY

        args = self._repo_mcp_server_args(spec)
        server_env = {k: env[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in env}
        config = {"mcpServers": {SERVER_KEY: {"type": "stdio", "command": sys.executable, "args": args, "env": server_env}}}
        path = scratch / "mcp.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return path

    def _session_claude(self, prompt: str, session_id: Optional[str], workdir: str,
                        repo_mcp: Optional[Dict[str, Any]] = None,
                        instructions: Optional[SessionInstructions] = None,
                        provider: str = "claude") -> Tuple[str, str]:
        """One claude turn. With repo_mcp ({"workspace", "sha", "log", "limits"}) the repo_* MCP tools are attached.
        instructions become the system prompt (_claude_system_args) of a first and of a resumed turn alike.

        Verified flags (claude 2.1.289): `--tools ""` hides every built-in tool and leaves MCP tools alone;
        `--mcp-config <file> --strict-mcp-config` loads only our server; `--allowedTools mcp__repo__...`
        pre-approves exactly those tools. In --print mode a tool that is not allowed is denied at once, never
        prompted, so no permission mode is needed (and bypassPermissions is never used).
        provider "zai" runs the same binary and flags with z.ai's environment (_zai_process_config).
        """
        process_env, timeout = self._claude_family_config(provider)
        sid = session_id or str(uuid.uuid4())
        flag = ["--resume", sid] if session_id else ["--session-id", sid]
        base = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--tools", "",
                *self._claude_effort_args()]
        with tempfile.TemporaryDirectory(prefix="guardian_repomcp_") as scratch:
            system_args = self._claude_system_args(instructions, Path(scratch)) if instructions else []
            mcp_args: List[str] = []
            if repo_mcp:
                from repo_mcp import allowed_tool_names

                config = self._claude_mcp_config(repo_mcp, process_env, Path(scratch))
                mcp_args = ["--mcp-config", str(config), "--strict-mcp-config",
                            "--allowedTools", ",".join(allowed_tool_names(bool(repo_mcp.get("no_index"))))]

            def run(extra: List[str]) -> subprocess.CompletedProcess:
                return subprocess.run([*base, *system_args, *extra, *flag], input=prompt, capture_output=True, text=True,
                                      timeout=timeout, env=process_env, cwd=workdir)

            proc = run(mcp_args)
            if mcp_args and proc.returncode != 0 and "unknown option" in (proc.stderr or "").lower():
                # An older claude without these flags: the member falls back to repo blocks.
                logger.warning("claude rejected the MCP flags, retrying without repo tools: %s", (proc.stderr or "")[:200])
                mcp_args = []
                proc = run([])
        raw = self._check(provider, proc)
        text, event_sid, final = self._parse_claude_stream(raw, after_tools=bool(mcp_args))
        if final is None:
            raise RuntimeError(f"{provider} returned no result event: {raw[:200]}")
        if final.get("is_error") or not text:
            raise RuntimeError(f"{provider} reported an error: {str(final.get('result'))[:300]}")
        return text, str(final.get("session_id") or event_sid or sid)

    # @MX:WARN: Every flag below removes a built-in codex tool; a missing one would give the model a file or shell channel.
    # @MX:REASON: Verified on codex 0.160.0 (docs/design/debate-agent-retrieval.md). --strict-config makes an unknown
    # @MX:REASON: config field and --disable of an unknown feature a startup error, so a changed CLI fails closed.
    def _codex_mcp_args(self, spec: Dict[str, Any], env: Dict[str, str]) -> List[str]:
        """codex flags that attach the repo_* MCP server and remove codex's own shell and file tools.

        Nothing is written to disk and ~/.codex/config.toml is not touched: the server is defined with -c overrides and
        --ignore-user-config keeps the user's other MCP servers, plugins and hooks out (auth still comes from CODEX_HOME).
        """
        import sys

        from repo_mcp import SERVER_KEY

        server_args = self._repo_mcp_server_args(spec)
        server_env = {k: env[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in env}
        env_table = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in server_env.items()) + "}"
        prefix = f"mcp_servers.{SERVER_KEY}"
        overrides = [
            f"{prefix}.command={json.dumps(sys.executable)}",
            f"{prefix}.args={json.dumps(server_args)}",
            f"{prefix}.env={env_table}",
            # Without this a read-only exec run refuses every MCP call ("requires approval, but approval policy is never").
            f'{prefix}.default_tools_approval_mode="approve"',
            f"{prefix}.startup_timeout_sec=30",
            'web_search="disabled"',
            'approval_policy="never"',
        ]
        args = ["--ignore-user-config", "--strict-config"]
        for feature in CODEX_MCP_DISABLED_FEATURES:
            args += ["--disable", feature]
        for override in overrides:
            args += ["-c", override]
        return args

    @staticmethod
    def _codex_rejected_mcp_flags(proc: subprocess.CompletedProcess) -> bool:
        """True when codex failed at startup because this version does not know one of the MCP/lockdown flags."""
        if proc.returncode == 0:
            return False
        stderr = (proc.stderr or "").lower()
        return any(marker in stderr for marker in CODEX_OLD_CLI_MARKERS)

    def _session_codex(self, prompt: str, session_id: Optional[str], workdir: str,
                       repo_mcp: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """One codex turn. With repo_mcp ({"workspace", "sha", "log", "limits"}) the repo_* tools are attached and
        codex's own shell, file-view, web and sub-agent tools are removed (_codex_mcp_args); a codex that does not know
        those flags is run once more without MCP so the member falls back to repo blocks."""
        process_env = self._provider_env("codex")
        with tempfile.TemporaryDirectory(prefix="guardian_codex_") as scratch:
            output_file = Path(scratch) / "last_message.txt"
            common = ["--skip-git-repo-check", "--json", "-o", str(output_file)]

            def run(extra: List[str]) -> subprocess.CompletedProcess:
                if output_file.exists():
                    output_file.unlink()
                if session_id:
                    # `exec resume` accepts neither --sandbox nor --color; set the sandbox through config.
                    args = ["codex", "exec", "resume", *common, *extra, "-c", 'sandbox_mode="read-only"', session_id, "-"]
                else:
                    args = ["codex", "exec", *common, *extra, "--color", "never", "--sandbox", "read-only", "-"]
                return subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=self.timeout,
                                      cwd=workdir, env=process_env)

            mcp_args = self._codex_mcp_args(repo_mcp, process_env) if repo_mcp else []
            proc = run(mcp_args)
            if mcp_args and self._codex_rejected_mcp_flags(proc):
                logger.warning("codex rejected the MCP flags, retrying without repo tools: %s", (proc.stderr or "")[:200])
                mcp_args = []
                proc = run([])
            output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        last_message = self._check("codex", proc, output)
        sid = session_id
        messages: List[str] = []
        started = False
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if mcp_args and item.get("type") in CODEX_FORBIDDEN_ITEMS:
                # The lockdown flags should make these impossible; if one appears anyway the turn is not trusted.
                raise RuntimeError(f"codex used a built-in tool ({item.get('type')}) in a repo-MCP run")
            if event.get("type") == "thread.started" and event.get("thread_id") and not started:
                sid, started = event["thread_id"], True
            elif mcp_args and item.get("type") == "mcp_tool_call" and item.get("server") == "repo":
                messages.clear()  # as for claude: only the text written after the last repo tool call is the answer
            elif event.get("type") == "item.completed" and item.get("type") == "agent_message":
                if str(item.get("text", "")).strip():
                    messages.append(str(item["text"]))
        # -o holds only the final message; the --json events carry every agent message of the turn.
        if not messages or messages[-1].strip() != last_message.strip():
            messages.append(last_message)
        text = self._join_turn(messages)
        if not sid:
            raise RuntimeError("codex did not report a thread id")
        return text, sid

    def _session_agy(self, prompt: str, session_id: Optional[str], workdir: str) -> Tuple[str, str]:
        return self._agy(prompt, session_id, workdir)

    def _build_prompt(self, prompt: str, context: Optional[Dict[str, Any]], system: Optional[str] = None) -> str:
        """Wraps a prompt in the default System/Context header. system replaces the header's role line (a caller whose
        prompt carries its own role, such as the wiki writer); an empty system sends the prompt unwrapped."""
        if system is not None:
            return f"System: {system}\n\n{prompt}" if system else prompt
        context_str = json.dumps(context or {}, indent=2, ensure_ascii=False)
        return (
            f"System: You are an expert Linux System & SSH Security Engineer.\n"
            f"Context Data:\n{context_str}\n\n"
            f"Task: {prompt}"
        )

    def _fallback(self, prompt: str, context: Optional[Dict[str, Any]], errors: List[str]) -> str:
        self.last_error = " | ".join(errors) if errors else "no supported LLM CLI found on PATH"
        # @MX:WARN: Built-in output is canned text, not a real model response.
        # @MX:REASON: Callers that make decisions must check for BUILTIN_PROVIDER and treat the result as unverified.
        logger.warning("All LLM CLIs failed, using built-in fallback: %s", self.last_error)
        return f"{BUILTIN_BANNER} (원인: {self.last_error})\n{self._expert_reasoning(prompt, context or {})}"

    def run_session_prompt(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        provider: Optional[str] = None,
        providers: Optional[List[str]] = None,
        repo_mcp: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
        instructions: Optional[SessionInstructions] = None,
        plain_prompt: Optional[str] = None,
    ) -> LLMResult:
        """
        Runs the prompt in a persisted CLI session so later calls can resume the conversation.

        The process exits after each call, so an idle session holds no memory. When session_id
        and provider are given, that session is resumed first; if it fails, the next providers
        start a new session. Providers without session support run statelessly.
        providers restricts the call to those providers, in that order (a team member's preference),
        even when GUARDIAN_LLM_PROVIDERS leaves them out; when none is installed the built-in
        fallback answers.
        repo_mcp (see _session_claude, _session_codex) attaches the read-only repo tools, but only when claude, zai or
        codex is the provider that actually runs; every other provider ignores it.
        system (see _build_prompt) replaces the default "Linux & SSH Security Engineer" role line, which would
        otherwise tell every session, on every turn, that it is an SSH engineer. A turn that resumes a session
        is sent as it is: the session already has its role, and repeating the header would restate (or, with the
        default, contradict) it.
        instructions (SessionInstructions) are the session's standing instructions. claude (and zai, the same binary) receives them as its system
        prompt on every call, so they survive auto-compaction, and `prompt` then carries only the turn's task: no
        System header, no role line. plain_prompt is the complete message for providers without a system-prompt
        mechanism (codex, agy, the rest): it replaces `prompt` for them and is wrapped exactly as before.
        """
        plain = prompt if plain_prompt is None else plain_prompt
        full_prompt = self._build_prompt(plain, context, system)
        resumed_prompt = plain if system is not None else full_prompt
        claude_first = prompt
        if instructions and context:
            claude_first = f"Context Data:\n{json.dumps(context, indent=2, ensure_ascii=False)}\n\n{prompt}"
        self.last_error = None
        errors: List[str] = []
        order = list(self.available_providers)
        if providers:
            order = [name for name in providers if name in self.installed_providers]
            if not order:
                errors.append(f"none of {providers} is installed (installed: {self.installed_providers})")
        if provider in order:
            order.remove(provider)
            order.insert(0, provider)
        workdir = str(session_workdir())

        for name in order:
            resume_id = session_id if (session_id and name == provider) else None
            turn_prompt = resumed_prompt if resume_id else full_prompt
            try:
                if name in ("claude", "zai") and instructions:
                    text, sid = self._session_claude(prompt if resume_id else claude_first, resume_id, workdir,
                                                     repo_mcp=repo_mcp, instructions=instructions, provider=name)
                elif name in ("claude", "zai"):
                    text, sid = self._session_claude(turn_prompt, resume_id, workdir, repo_mcp=repo_mcp, provider=name)
                elif name == "codex":
                    text, sid = self._session_codex(turn_prompt, resume_id, workdir, repo_mcp=repo_mcp)
                elif name == "agy":
                    text, sid = self._session_agy(turn_prompt, resume_id, workdir)
                else:
                    invoke = self._invoker(name)
                    if invoke is None:
                        errors.append(f"{name}: unsupported provider")
                        continue
                    with tempfile.TemporaryDirectory(prefix="guardian_llm_") as scratch:
                        return LLMResult(f"{name}-cli", invoke(full_prompt, scratch), failures=list(errors))
                return LLMResult(f"{name}-cli", text, sid, resumed=resume_id is not None, failures=list(errors))
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: timed out after {self.timeout:.0f}s")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {exc}")
            logger.info("LLM provider %s failed, trying next: %s", name, errors[-1])

        return LLMResult(BUILTIN_PROVIDER, self._fallback(prompt, context, errors), failures=list(errors))

    # @MX:ANCHOR: Routes analysis prompts to the selected provider CLI.
    # @MX:REASON: Diagnostic, security and debate agents share this external integration boundary.
    def run_prompt(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """
        Runs the prompt with the first provider CLI that succeeds, else the built-in engine.
        Returns: (provider_name, response_text)
        """
        full_prompt = self._build_prompt(prompt, context)
        self.last_error = None
        errors: List[str] = []
        # Run in an empty directory so agentic CLIs cannot read or modify the repository.
        with tempfile.TemporaryDirectory(prefix="guardian_llm_") as workdir:
            for name in self.available_providers:
                invoke = self._invoker(name)
                if invoke is None:
                    errors.append(f"{name}: unsupported provider")
                    continue
                try:
                    return f"{name}-cli", invoke(full_prompt, workdir)
                except subprocess.TimeoutExpired:
                    errors.append(f"{name}: timed out after {self.timeout:.0f}s")
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    errors.append(f"{name}: {exc}")
                logger.info("LLM provider %s failed, trying next: %s", name, errors[-1])

        return BUILTIN_PROVIDER, self._fallback(prompt, context, errors)

    def _expert_reasoning(self, prompt: str, context: Dict[str, Any]) -> str:
        lower = prompt.lower()

        # Debate roles need a model; canned critiques, proposals or verdicts would read as real
        # analysis in the transcript. The debate engine marks such rounds UNVERIFIED.
        if "decide if consensus is reached" in lower or "consensus" in lower or "judge" in lower:
            return "판정자 응답을 만들 수 없습니다. 이 라운드는 판정되지 않았습니다 (UNVERIFIED)."
        if "critique this proposal" in lower or "critique" in lower or "red team" in lower:
            return "비평자 응답을 만들 수 없습니다. 이 제안은 검토되지 않았습니다."
        if "architectural proposal" in lower or "proposal" in lower or "domain expert" in lower:
            return "전문가 응답을 만들 수 없습니다. 제안이 없습니다."

        # 0. Fleet-Wide Infrastructure Analysis
        if "fleet_summary" in context:
            fleet = context["fleet_summary"] or []
            if not fleet:
                # No probe results: say so instead of inventing a fleet size.
                return "🌐 [클러스터 진단] 서버 진단 결과가 없습니다. fleet 진단을 실행한 뒤 다시 요청하세요."
            total = len(fleet)
            online = sum(1 for s in fleet if s.get("status") == "ONLINE")
            offline_names = [s.get("name") or s.get("id", "?") for s in fleet if s.get("status") != "ONLINE"]
            offline = len(offline_names)
            pct = online / total * 100
            lines = [
                f"🌐 [클러스터 전체 진단 리포트] 총 {total}대 서버 응답 확인 (가동률: {pct:.1f}%)",
                f" • 정상 가동(ONLINE): 🟢 {online}대 | 장애/오프라인: {'🔴 ' + str(offline) if offline > 0 else '없음 (0대)'}",
            ]

            
            # Per user node group (group_manager.memberships): a server in several groups counts
            # in each, servers in none under "Ungrouped". Without group data, no group lines.
            fleet_groups = context.get("fleet_groups")
            if fleet_groups is not None:
                by_id = {str(s.get("id")): s for s in fleet}
                lines.append(" • 노드 그룹별 가동 현황:")
                for g_name, ids in fleet_groups.items():
                    members = [by_id[i] for i in ids if i in by_id]
                    if not members:
                        continue
                    on = sum(1 for s in members if s.get("status") == "ONLINE")
                    lines.append(f"   - {g_name:22}: {on}/{len(members)} 노드 정상 가동")

            # Built-in engine: report only what the probe measured, no judgement it cannot make.
            if offline_names:
                lines.append(f" • 확인 필요: 응답 없는 서버 {', '.join(offline_names)}")
            else:
                lines.append(" • 모든 서버가 진단에 응답했습니다. (내장 엔진 요약: 부하·자원 분석은 LLM 응답이 필요합니다)")
            return "\n".join(lines)

        # 0-1. Fleet SSH audit: the hub already scored every node; repeat its report as is.
        if context.get("fleet_ssh_report"):
            return str(context["fleet_ssh_report"])

        # 1. SSH & Security Audit
        if any(w in lower for w in ["ssh", "security", "보안", "취약점", "감사"]):
            sec = context.get("security", {})
            score = sec.get("score")
            score_text = f"{score}/100점" if isinstance(score, (int, float)) else "점수 없음 (감사 결과 미수신)"
            lines = [f"🛡️ [보안 감사 결과] 종합 점수: {score_text}"]
            rules = sec.get("rules", [])
            for r in rules:
                status = r.get("status")
                rule_id = r.get("rule_id", "SEC")
                title = r.get("title", "")
                cur = r.get("current_value", "")
                exp = r.get("expected_value", "")
                remed = r.get("remediation", "")

                if status == "FAIL":
                    lines.append(f" ❌ [{rule_id}] {title} (현재: {cur} → 권장: {exp})")
                    if remed:
                        lines.append(f"    조치: {remed}")
                elif status == "WARN":
                    lines.append(f" ⚠️ [{rule_id}] {title}: {remed}")
            return "\n".join(lines)

        # 2. System Metrics & Diagnostics
        if any(w in lower for w in ["metric", "상태", "자원", "리소스", "병목", "diagnos"]):
            m = context.get("metrics") or {}
            if not m:
                return "📊 [시스템 진단] 받은 메트릭이 없습니다."
            hostname = m.get("hostname", "Unknown")
            os_name = m.get("os", "")
            arch = m.get("arch", "")
            cpu = m.get("cpu_usage_pct", 0.0)
            mem = m.get("memory_pct", 0.0)
            disk = m.get("disk_pct", 0.0)
            load = m.get("load_avg", [0, 0, 0])

            lines = [
                f"📊 [시스템 진단 리포트] 호스트: {hostname} ({os_name}/{arch})",
                f" • CPU 점유율: {cpu:.1f}% | Load Average: {load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}",
                f" • 물리 메모리 사용률: {mem:.1f}%",
                f" • 루트 디스크 점유율: {disk:.1f}%",
            ]
            procs = m.get("top_processes", [])
            if procs:
                lines.append(" • 상위 리소스 점유 프로세스:")
                for p in procs[:3]:
                    lines.append(
                        f"   - PID {p.get('pid')} ({p.get('user')}): {p.get('command')} "
                        f"(CPU: {p.get('cpu')}%, MEM: {p.get('mem')}%)"
                    )
            if m.get("issues"):
                lines.append(f" • 읽지 못한 값: {', '.join(m['issues'])}")
            return "\n".join(lines)

        persona = context.get("persona") if context else None
        if persona and not any(w in lower for w in ["fleet", "누구", "너는", "당신", "소개", "who are you"]):
            return f"{persona.get('name', '선택한 페르소나')}의 응답을 만들 수 없습니다. LLM CLI가 동작할 때 다시 요청하세요."

        # 3. Help text: what the tool does, with no claims about the current state of the fleet.
        if any(w in lower for w in ["누구", "너는", "당신", "소개", "안녕", "hello", "hi", "who are you", "무슨 일", "기능", "역할"]):
            return (
                "🤖 Server Guardian입니다. LLM이 동작할 때 다음을 할 수 있습니다:\n"
                "   - 서버 자원(CPU/메모리/디스크) 진단과 병목 분석\n"
                "   - sshd 설정과 authorized_keys 보안 감사\n"
                "   - 사람의 승인을 거치는 조치 제안과 실행\n"
                "지금은 LLM CLI를 쓸 수 없어 측정값 요약(fleet 진단, SSH 감사, 메트릭)만 보여 줄 수 있습니다."
            )

        return "이 요청에 답할 수 없습니다. 내장 엔진은 측정값 요약만 할 수 있습니다."

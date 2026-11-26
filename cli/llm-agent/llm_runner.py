"""LLM runner for installed provider CLIs and built-in heuristic reasoning.

Provider CLIs are used with their own subscription login (claude, codex, agy, ...).
Providers are tried in order and the next one is used when a call fails.
exaone has no CLI: it calls the EXAONE API over HTTP (urllib, stdlib only) and is ready when its key is set.
opencode runs K-EXAONE through the opencode 2.x CLI (docs/design/opencode-exaone.md).
"""

import http.client
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

from env_policy import provider_env
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Provider name returned when no external LLM CLI produced a response.
BUILTIN_PROVIDER = "built-in-expert"
# Every built-in reply starts with this line, so nobody mistakes it for a model's analysis.
BUILTIN_BANNER = "⚠️ [LLM 응답 없음] 모든 LLM CLI 호출이 실패해 내장 엔진이 답했습니다. 분석·판단·제안은 하지 않습니다"

# Every provider a seat, a team member or Quick ask may name: validation, the editor catalog, installed_providers.
KNOWN_PROVIDERS = ["claude", "codex", "agy", "zai", "exaone", "opencode", "ollama", "llm"]
# The chain used when neither a seat nor GUARDIAN_LLM_PROVIDERS names one. opencode (K-EXAONE through the
# opencode 2.x CLI) took exaone's place (docs/design/opencode-exaone.md, decision 4 of 2026-11-11); exaone
# stays a known provider that an explicit pick can still choose.
DEFAULT_PROVIDER_ORDER = ["claude", "codex", "agy", "zai", "opencode", "ollama", "llm"]
# Providers that run another provider's binary under their own identity (own credentials, config dir and labels).
# zai is the claude CLI against z.ai's Anthropic-compatible endpoint (GLM models, Coding Plan key in ZAI_API_KEY).
PROVIDER_BINARIES = {"zai": "claude"}
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_TIERS = ("OPUS", "SONNET", "HAIKU")
# --- exaone: the EXAONE API over plain HTTP, no binary, no session (docs/design/exaone-provider.md) ---
EXAONE_DEFAULT_BASE_URL = "https://api.exaone.ai/v1"
EXAONE_DEFAULT_MODEL = "k-exaone_v2.5"
# Reasoning is always on; the API's own default is "max", its docs suggest "high" for coding (operator chose high).
EXAONE_DEFAULT_EFFORT = "high"
# An unknown EXAONE_REASONING_EFFORT falls back to the default.
EXAONE_EFFORTS = ("low", "high", "max")
# max_tokens counts reasoning too; 65536 leaves a long answer room inside the 512 Ki-token context.
EXAONE_DEFAULT_MAX_TOKENS = 65536
# Replay turns grow: above this prompt byte cap the call fails before sending, so the next provider gets the turn.
EXAONE_DEFAULT_MAX_PROMPT_BYTES = 1536 * 1024
# The operator's key allows one request per second (measured 2026-11-09); request starts are spaced at least this far.
EXAONE_MIN_INTERVAL = 1.1
# A retry-after above this many seconds is a minute/day limit: fail at once instead of waiting out the turn timeout.
EXAONE_MAX_RETRY_AFTER = 30.0
EXAONE_MAX_RETRIES = 5
# SSE streaming is the default (EXAONE_STREAM=0 turns it off, kept for diagnosis): the service cuts a request that
# sends no byte for 600 s, while a streamed answer keeps sending chunks (production-like trial 2026-11-10,
# docs/design/exaone-provider.md "Streaming"). While a stream is read this is the socket timeout (no byte for this
# long fails the turn); the total deadline stays EXAONE_TIMEOUT.
EXAONE_DEFAULT_IDLE_TIMEOUT = 120.0
# Tests may point the provider at a local http.server fake by accepting loopback http; no env variable turns this on.
_EXAONE_ALLOW_LOOPBACK_HTTP = False
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
# Keep code_mode_host on: the functions.exec tool it adds is the code-mode host, has no file API, and carries the MCP
# calls themselves (disabling it breaks MCP; checked on 0.160.0 and 0.163.0, 2026-11-15).
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
    # Timing and token usage of the successful turn, set only by exaone and opencode ({} for every other
    # provider and for a fallback answer). Never holds the key, the prompt or the answer text.
    stats: Dict[str, Any] = field(default_factory=dict)


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


# Pacing state of the exaone provider: one process-wide lock spaces request starts, so the parallel agents of one
# debate queue for a moment instead of colliding on the key's one-request-per-second limit. The lock is never held
# while a request runs; other processes (a second debate, a worker host with the same key) are covered by the 429 rule.
_EXAONE_PACE_LOCK = threading.Lock()
_EXAONE_NEXT_START = 0.0


def _exaone_pace() -> None:
    """Waits until the next exaone request may start: sleep under the module lock, reserve the slot, release, return.
    time.monotonic/time.sleep go through the module reference so tests can patch them."""
    global _EXAONE_NEXT_START
    with _EXAONE_PACE_LOCK:
        wait = _EXAONE_NEXT_START - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _EXAONE_NEXT_START = time.monotonic() + EXAONE_MIN_INTERVAL


def _exaone_base_url(source: Dict[str, str]) -> str:
    """Validated EXAONE_BASE_URL with a trailing "/" stripped: https only, plus a loopback http URL when tests allow."""
    base = (source.get("EXAONE_BASE_URL") or EXAONE_DEFAULT_BASE_URL).strip().rstrip("/")
    loopback = base.startswith(("http://127.0.0.1:", "http://localhost:"))
    if not base.startswith("https://") and not (_EXAONE_ALLOW_LOOPBACK_HTTP and loopback):
        raise RuntimeError(f"EXAONE_BASE_URL must start with https:// (got {base})")
    return base


def _int_setting(source: Dict[str, str], name: str, default: int) -> int:
    """int value of an env-file setting; the default when the setting is absent or not an integer."""
    try:
        return int(str(source[name]).strip())
    except (KeyError, TypeError, ValueError):
        return default


def _positive_float_setting(source: Dict[str, str], name: str, default: float) -> float:
    """Positive float value of an env-file setting; the default when it is absent, not numeric or not positive."""
    try:
        value = float(str(source[name]).strip())
    except (KeyError, TypeError, ValueError):
        return default
    return value if value > 0.0 else default


def _exaone_stream_flag(source: Dict[str, str]) -> bool:
    """Streaming is on by default: only EXAONE_STREAM set to 0/false/no/off (case-insensitive, stripped) turns it
    off. Unset, empty and any other value leave it on. EXAONE_STREAM=0 is the diagnosis switch back to the JSON
    path, whose request stays byte for byte as before."""
    return str(source.get("EXAONE_STREAM") or "").strip().lower() not in ("0", "false", "no", "off")


def _exaone_retry_seconds(headers: Any) -> Optional[float]:
    """Seconds to wait from retry-after-ms (milliseconds) or retry-after (seconds, numeric only); None when absent."""
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name) if headers else None
        if value is None:
            continue
        try:
            return float(str(value).strip()) * scale
        except ValueError:
            continue
    return None


def _exaone_error_message(status: int, headers: Any, body: bytes, key: str) -> str:
    """`HTTP <status> (x-request-id <id>): <first 200 chars of the body>`; server text can never echo the key back.
    The key is redacted before the 200-char cut, so a key straddling the cut cannot leave its prefix behind."""
    request_id = str((headers.get("x-request-id") if headers else None) or "-").replace(key, "<redacted>")
    text = body.decode("utf-8", "replace").replace(key, "<redacted>")[:200]
    return f"HTTP {status} (x-request-id {request_id}): {text}"


# max_tokens counts reasoning: the model spent the whole budget thinking, so raise it instead of a retry.
# One text for both the JSON answer and the streamed answer, so the failure reads the same either way.
EXAONE_LENGTH_ERROR = "exaone: reasoning used max_tokens (finish_reason length); raise EXAONE_MAX_TOKENS"


def _exaone_answer(payload: Any) -> str:
    """The turn text: choices[0].message.content, stripped. reasoning_content is dropped, never returned or stored."""
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"malformed answer: {str(payload)[:200]}") from exc
    if finish_reason == "length":
        raise RuntimeError(EXAONE_LENGTH_ERROR)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("empty answer")
    return content.strip()


def _exaone_json_body(response: Any) -> Any:
    """Read callback of a non-streamed turn: the whole body, parsed as JSON."""
    raw = response.read().decode("utf-8")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"answer is not JSON: {raw[:200]}") from exc


def _exaone_usage_fields(usage: Any) -> Dict[str, int]:
    """The token counts of a usage object (a JSON answer's or a stream chunk's): only prompt_tokens,
    completion_tokens and reasoning_tokens, each only when it is an int. reasoning_tokens falls back to
    completion_tokens_details.reasoning_tokens. Anything else in the object is dropped."""
    if not isinstance(usage, dict):
        return {}
    fields: Dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            fields[name] = value
    if "reasoning_tokens" not in fields:
        details = usage.get("completion_tokens_details")
        value = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            fields["reasoning_tokens"] = value
    return fields


def _exaone_stream_chunk(payload: str, parts: List[str], key: str,
                         info: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """One SSE data payload (not [DONE]): appends delta.content to parts, returns the chunk's finish_reason.

    Raises RuntimeError for a payload that is not a JSON object or carries an error object; both redact the key
    before the 200-char cut, so a key straddling the cut cannot leave its prefix behind. delta.reasoning_content
    is dropped (never stored, never returned), exactly like message.reasoning_content of a JSON answer. Only a
    non-empty string counts as a finish_reason: "" (sent on non-final chunks) and non-strings read as None.

    info (optional, mutable) receives the usage counts of any chunk that carries a usage object: the final
    chunk sends one with empty choices, so it is read before the choices handling below could return early.
    """
    try:
        chunk = json.loads(payload)
    except ValueError:
        chunk = None
    if not isinstance(chunk, dict):
        raise RuntimeError(f"malformed stream chunk: {payload}".replace(key, "<redacted>")[:200]) from None
    if "error" in chunk:
        error = chunk["error"]
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error, ensure_ascii=False)
        raise RuntimeError(f"stream error: {str(error)}".replace(key, "<redacted>")[:200]) from None
    if info is not None:
        fields = _exaone_usage_fields(chunk.get("usage"))
        if fields:
            info.update(fields)
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    if isinstance(content, str):
        parts.append(content)
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) and reason else None


def _exaone_deadline_text(total: float, source: str) -> str:
    """The one total-deadline failure text of a streamed turn; source names where the limit came from:
    "(EXAONE_TIMEOUT)" when the provider env set it, otherwise "(turn timeout)" for the runner's own limit."""
    return f"exaone: stream passed the total deadline of {total:g} s ({source})"


def _exaone_read_stream(lines: Iterable[bytes], deadline: float, idle: float, key: str,
                        deadline_text: str, info: Optional[Dict[str, Any]] = None) -> str:
    """Joins a streamed EXAONE answer (SSE) into the turn text; every failure is a RuntimeError.

    lines are the raw byte lines of the body, deadline the absolute time.monotonic() total deadline (deadline_text
    is its ready failure text) and idle the idle timeout in seconds. A payload that is not JSON or carries an
    error, a stream that ends without [DONE] or without a finish_reason, an idle read and the total deadline all
    fail the turn: partial text is never returned, because a cut-off critique would read as a whole one.

    info (optional, mutable) collects what the stats of a successful turn need: the time of the first data line
    ("first_line_at", absolute time.monotonic()), the finish_reason and the usage token counts. It is only read
    by the caller after this function returned, so a failed turn's half-filled info is simply discarded.
    """
    parts: List[str] = []
    finish_reason: Optional[str] = None
    done = False
    try:
        for raw in lines:
            if time.monotonic() > deadline:
                raise RuntimeError(deadline_text)
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue  # blank lines, comment lines (":"), event:/id:/retry: lines and any other non-data line
            if info is not None and "first_line_at" not in info:
                info["first_line_at"] = time.monotonic()
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                done = True
                break
            reason = _exaone_stream_chunk(payload, parts, key, info)
            if reason is not None:
                finish_reason = reason
    except (TimeoutError, socket.timeout):
        # The response was opened with the idle timeout as its socket timeout: no byte arrived for idle seconds.
        raise RuntimeError(f"exaone: no data for {idle:g} s (EXAONE_IDLE_TIMEOUT)") from None
    if not done or finish_reason is None:
        raise RuntimeError("exaone: stream ended early (no [DONE] or finish_reason)")
    if finish_reason == "length":
        raise RuntimeError(EXAONE_LENGTH_ERROR)
    text = "".join(parts)
    if not text.strip():
        raise RuntimeError("empty answer")
    if info is not None:
        info["finish_reason"] = finish_reason
    return text.strip()


def _exaone_stream_body(response: Any, deadline: float, idle: float, key: str, deadline_text: str,
                        info: Optional[Dict[str, Any]] = None) -> str:
    """Read callback of a streamed turn: thin glue that feeds response.readline() lines into _exaone_read_stream."""

    def lines() -> Iterator[bytes]:
        while True:
            raw = response.readline()
            if not raw:
                return  # end of the body
            yield raw

    return _exaone_read_stream(lines(), deadline, idle, key, deadline_text, info)


# --- opencode: K-EXAONE through the opencode 2.x CLI, no sessions (docs/design/opencode-exaone.md) ---
OPENCODE_EXAONE_BASE_URL = "https://api.exaone.ai/v1"
# The whole process gets this deadline: opencode waits forever on a stalled stream, and its internal retries
# (11 requests on a 500) already cost 82-84 s. The HTTP provider needed 1200 s (longest request 1129 s).
OPENCODE_DEFAULT_TIMEOUT = 1200.0
# Read by the runner, never handed to the CLI itself.
OPENCODE_GUARDIAN_SETTINGS = ("OPENCODE_BIN", "OPENCODE_TIMEOUT", "OPENCODE_EXAONE_EFFORT", "OPENCODE_EXAONE_MODEL")
# Removes everything local (project config, the claude-code bridge, updates, model fetches) so a turn depends
# only on the passed config and the model flag.
OPENCODE_DISABLE_FLAGS = (
    "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_FREE_MODELS",
)
# EXAONE-side overload (429, provider.rate-limit) is retried: opencode already retries ~84 s inside itself, but
# the service's concurrent-request cap outlives that. All attempts together share the one OPENCODE_TIMEOUT
# deadline; each pause also carries random.uniform(0, 5) seconds of jitter.
OPENCODE_MAX_RETRIES = 2
OPENCODE_RETRY_BACKOFF = (30.0, 60.0)
# Readiness cache: `<bin> --version` runs once per binary path and modification time, so building runner
# after runner (one LLMRunner probes every known provider twice) never re-runs the binary.
_OPENCODE_2X_CACHE: Dict[Tuple[str, float], bool] = {}


def _opencode_major(version_output: str) -> Optional[int]:
    """Major version from `opencode --version`: the v2 line prints `opencode v2.2.7`, the Homebrew 1.x line a
    bare `1.19.5`. None when the output has neither shape."""
    match = re.match(r"\s*(?:opencode\s+)?v?(\d+)\.\d+", version_output)
    return int(match.group(1)) if match else None


def _opencode_timeout_source(source: Dict[str, str]) -> str:
    """"OPENCODE_TIMEOUT" when the provider env holds the value in use (a positive number), else "default": an
    invalid value falls back to OPENCODE_DEFAULT_TIMEOUT, so it is the default that bounds the turn."""
    try:
        value = float(str(source.get("OPENCODE_TIMEOUT") or "").strip())
    except ValueError:
        return "default"
    return "OPENCODE_TIMEOUT" if value > 0.0 else "default"


def _opencode_scratch_env(env: Dict[str, str], scratch: str) -> Dict[str, str]:
    """A copy of env whose XDG config, data and state dirs sit inside scratch (removed by the caller), so
    opencode keeps no session, prompt copy or log line after the call. XDG_CACHE_HOME is left as given."""
    env = dict(env)
    for kind in ("CONFIG", "DATA", "STATE"):
        xdg_dir = Path(scratch) / kind.lower()
        xdg_dir.mkdir(parents=True, exist_ok=True)
        env[f"XDG_{kind}_HOME"] = str(xdg_dir)
    return env


def _opencode_config(model: str, effort: str, repo_mcp: Optional[Dict[str, Any]] = None,
                     env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The v2 config handed over as OPENCODE_CONFIG_CONTENT: the EXAONE guide's shape with Guardian's
    deviations (tools off, permission deny, the HTTP provider's effort and output limit). apiKey is the
    literal `{env:OPENCODE_EXAONE_API_KEY}` string, which opencode expands itself: the key never enters the
    config, the argv or the environment.

    With repo_mcp ({"workspace", "sha", "log", "limits"}) the read-only repo_* MCP tools are attached
    instead: the model offers tools, the local server "repo" runs with codemode off (else opencode routes
    every tool call through an execute interpreter) and a 60 s timeout, and the permission allows exactly
    the repo tools (repo_repo_read, ...). opencode passes its whole environment to the MCP child, the key
    and OPENCODE_CONFIG_CONTENT included, so the command starts with `/usr/bin/env -i` and rebuilds a clean
    environment from env's PATH, HOME, LANG and LC_ALL (the same names as server_env in _claude_mcp_config,
    each only when set); nothing else is handed to the server."""
    config: Dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"exaone/{model}",
        "providers": {"exaone": {
            "package": "@opencode/ai/providers/openai-compatible",
            "name": "EXAONE",
            "settings": {"baseURL": OPENCODE_EXAONE_BASE_URL, "apiKey": "{env:OPENCODE_EXAONE_API_KEY}"},
            "models": {model: {
                "name": "K-EXAONE 2.5",
                "capabilities": {"tools": False, "input": ["text"], "output": ["text"], "temperature": True,
                                 "reasoning": {"supported": True}},
                "compatibility": {"reasoningField": "reasoning_content"},
                "body": {"reasoning_effort": effort, "temperature": 1.0, "top_p": 0.95},
                "limit": {"context": 524288, "output": EXAONE_DEFAULT_MAX_TOKENS},
            }},
        }},
        "permission": {"*": "deny"},
    }
    if repo_mcp:
        import sys

        from repo_mcp import SERVER_KEY, opencode_tool_names

        command = ["/usr/bin/env", "-i",
                   *(f"{name}={env[name]}" for name in ("PATH", "HOME", "LANG", "LC_ALL") if name in (env or {})),
                   sys.executable, *LLMRunner._repo_mcp_server_args(repo_mcp)]
        config["providers"]["exaone"]["models"][model]["capabilities"]["tools"] = True
        config["mcp"] = {SERVER_KEY: {"type": "local", "command": command, "enabled": True,
                                      "codemode": False, "timeout": 60000}}
        permission: Dict[str, Any] = {"*": "deny"}
        permission.update({name: "allow" for name in opencode_tool_names(bool(repo_mcp.get("no_index")))})
        config["permission"] = permission
    return config


def _opencode_error_text(error: Any) -> str:
    """`error <type>: <message> (status <status>, request <x-request-id>)` from an opencode error event; the
    status and the request id only when present, the id only from error.response.headers when that field is a
    dict (case-insensitive key). Nothing else survives: no headers, no response or request body, never the key."""
    detail = error if isinstance(error, dict) else {}
    text = f"error {detail.get('type') or 'unknown'}: {str(detail.get('message') or '').strip()}".rstrip()
    extras = []
    if detail.get("status") is not None:
        extras.append(f"status {detail['status']}")
    response = detail.get("response")
    headers = response.get("headers") if isinstance(response, dict) else None
    if isinstance(headers, dict):
        for name, value in headers.items():
            if str(name).lower() == "x-request-id" and value:
                extras.append(f"request {value}")
                break
    return f"{text} ({', '.join(extras)})" if extras else text


def _opencode_parse(stdout: str) -> Tuple[str, Optional[str], int, int]:
    """(answer of the last step, failure text or None, served tool calls, refused tool calls) from `--format
    json` event lines.

    Blank and non-JSON lines are ignored (unknown event shapes too). A step_start begins a new step and drops
    the texts of earlier ones: after a tool call opencode continues in a new step, and only the last step is
    the answer. A tool_use counts as served when its part's state completed, and as refused otherwise (an
    error status is opencode denying the call). An error event and a last step_finish with reason "length"
    each produce their own failure text; the caller checks the exit code and the emptiness of the answer.
    """
    texts: List[str] = []
    finish_reason: Optional[str] = None
    failure: Optional[str] = None
    served = refused = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "error":
            failure = _opencode_error_text(event.get("error"))
        elif kind == "step_start":
            texts = []
            finish_reason = None
        elif kind == "text":
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        elif kind == "tool_use":
            part = event.get("part")
            state = part.get("state") if isinstance(part, dict) else None
            status = state.get("status") if isinstance(state, dict) else None
            if status == "completed":
                served += 1
            else:
                refused += 1
        elif kind == "step_finish":
            part = event.get("part")
            finish_reason = part.get("reason") if isinstance(part, dict) else None
    if failure is not None:
        return "", failure, served, refused
    if finish_reason == "length":
        return "", "answer cut at the output limit (reason length)", served, refused
    return "".join(texts).strip(), None, served, refused


def _opencode_rate_limited(stdout: str) -> bool:
    """True when the LAST error event in the JSON lines is EXAONE overload: error type provider.rate-limit or
    status 429 (int or numeric string). The English message text is never matched."""
    detail: Dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            error = event.get("error")
            detail = error if isinstance(error, dict) else {}
    if detail.get("type") == "provider.rate-limit":
        return True
    try:
        return float(detail.get("status")) == 429.0
    except (TypeError, ValueError):
        return False


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
        self.installed_providers = [name for name in KNOWN_PROVIDERS if self._installed(name, search_path)]
        self.detected_cli = self.available_providers[0] if self.available_providers else None
        self.timeout = self._timeout_from_env()
        # Reason the most recent external CLI call failed, if it did.
        self.last_error: Optional[str] = None
        # Per-turn stats of the provider attempt running on THIS thread: the runner is shared by the parallel
        # agents of one debate, so a plain instance attribute would leak one agent's turn into another's event.
        # run_session_prompt resets it at the start of each provider attempt; exaone and opencode fill it on
        # success. Never a plain attribute; see _copy_turn_stats.
        self._turn = threading.local()

    def _installed(self, name: str, search_path: Optional[str]) -> bool:
        """The provider's binary is on PATH and, for zai, its own key is present (claude's login does not count).
        exaone has no binary: it is installed when EXAONE_API_KEY is set (no which lookup runs for it).
        opencode is installed when its key is set and OPENCODE_BIN is an opencode 2.x binary."""
        if name == "exaone":
            return bool(self.base_env.get("EXAONE_API_KEY"))
        if name == "opencode":
            # The key first: without it the provider is never ready, so no binary runs (decision: the build
            # changes nothing until a key is placed).
            return bool(self.base_env.get("OPENCODE_EXAONE_API_KEY")) and self._opencode_2x(search_path)
        if shutil.which(provider_binary(name), path=search_path) is None:
            return False
        return name != "zai" or bool(self.base_env.get("ZAI_API_KEY"))

    def _opencode_2x(self, search_path: Optional[str]) -> bool:
        """True when OPENCODE_BIN resolves to an opencode 2.x binary: the config schema and --standalone are
        the v2 line the EXAONE guide documents, while the Homebrew 1.x line knows neither. `--version` runs
        once per binary path and modification time; any error counts as not installed."""
        binary = shutil.which(self.base_env.get("OPENCODE_BIN") or "opencode", path=search_path)
        if binary is None:
            return False
        try:
            cache_key = (binary, os.stat(binary).st_mtime)
        except OSError:
            return False
        if cache_key in _OPENCODE_2X_CACHE:
            return _OPENCODE_2X_CACHE[cache_key]
        try:
            # Even --version writes a log line under XDG_DATA_HOME: the probe gets throwaway XDG dirs too.
            with tempfile.TemporaryDirectory(prefix="guardian_opencode_") as scratch:
                env = _opencode_scratch_env(self._provider_env("opencode"), scratch)
                proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, env=env)
            ready = _opencode_major(proc.stdout or "") == 2
        except (OSError, subprocess.SubprocessError):
            ready = False
        _OPENCODE_2X_CACHE[cache_key] = ready
        return ready

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

    # --- opencode: one stateless EXAONE turn through the opencode 2.x CLI ---

    def _opencode_settings(self) -> Tuple[str, float, str, str, Dict[str, str]]:
        """(binary, timeout, effort, model, process env) from the opencode provider env (env_policy passes
        OPENCODE_* only). The four Guardian-only settings are consumed here and removed from the env."""
        source = self._provider_env("opencode")
        if not source.get("OPENCODE_EXAONE_API_KEY"):
            raise RuntimeError("OPENCODE_EXAONE_API_KEY is not set")
        binary = source.get("OPENCODE_BIN") or "opencode"
        timeout = _positive_float_setting(source, "OPENCODE_TIMEOUT", OPENCODE_DEFAULT_TIMEOUT)
        effort = str(source.get("OPENCODE_EXAONE_EFFORT") or EXAONE_DEFAULT_EFFORT).strip().lower()
        effort = effort if effort in EXAONE_EFFORTS else EXAONE_DEFAULT_EFFORT
        model = source.get("OPENCODE_EXAONE_MODEL") or EXAONE_DEFAULT_MODEL
        env = {k: v for k, v in source.items() if k not in OPENCODE_GUARDIAN_SETTINGS}
        return binary, timeout, effort, model, env

    def _opencode_invocation(self, repo_mcp: Optional[Dict[str, Any]] = None) -> Tuple[List[str], Dict[str, str], float]:
        """argv, environment (still without the per-call XDG dirs) and timeout of one opencode turn. The
        config travels in OPENCODE_CONFIG_CONTENT, the model in -m: opencode never runs without both, so it
        cannot fall back to a free model. With repo_mcp the config also attaches the repo_* MCP tools
        (_opencode_config)."""
        binary, timeout, effort, model, env = self._opencode_settings()
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(_opencode_config(model, effort, repo_mcp=repo_mcp, env=env))
        for flag in OPENCODE_DISABLE_FLAGS:
            env[flag] = "1"
        args = [binary, "run", "--standalone", "--format", "json", "--title", "guardian", "-m", f"exaone/{model}"]
        return args, env, timeout

    @staticmethod
    def _opencode_communicate(proc: subprocess.Popen, prompt: str, timeout: float) -> Tuple[str, str]:
        """Sends the prompt on stdin and waits for the turn, with a deadline on the whole process. On expiry
        the whole process GROUP dies: opencode waits forever on a stalled stream, and its children (a live
        provider bridge) hold the stdout pipe open, so killing only the direct child would hang the reap."""
        try:
            return proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise RuntimeError(f"timed out after {timeout:.0f}s (OPENCODE_TIMEOUT)") from None

    def _run_opencode(self, prompt: str, workdir: str, repo_mcp: Optional[Dict[str, Any]] = None) -> str:
        """One EXAONE turn through `opencode run` (stateless, no session, label opencode-cli).

        The prompt travels on stdin, never in argv (turns reach hundreds of KB). Sessions and prompt copies
        stay inside a scratch XDG dir removed after the call; the package cache persists under HOME. The
        answer is the text of the LAST step (a refused skill tool call makes opencode ask again); an error
        event, a cut answer (reason length), a non-zero exit or an empty answer fails the turn. With repo_mcp
        the turn offers the repo_* MCP tools through the config (_opencode_config); served and refused tool
        calls are counted and logged separately.

        An EXAONE rate limit is retried (_opencode_rate_limited): at most OPENCODE_MAX_RETRIES extra attempts,
        one scratch dir and one pacing slot each, with OPENCODE_RETRY_BACKOFF pauses. All attempts together
        never exceed OPENCODE_TIMEOUT: every attempt gets only the time that remains of it, and a pause that
        would pass the deadline fails with the original error instead of sleeping.

        On success the thread's turn stats hold the elapsed time, the timeout and where it came from (no token
        counts: the CLI reports none in the shape we read), so a debate event can carry the deadline margin.
        """
        started = time.monotonic()
        args, env, timeout = self._opencode_invocation(repo_mcp)
        timeout_source = _opencode_timeout_source(self._provider_env("opencode"))
        cache_home = Path(env.get("HOME") or os.path.expanduser("~")) / ".cache" / "guardian-opencode"
        cache_home.mkdir(parents=True, exist_ok=True)
        env["XDG_CACHE_HOME"] = str(cache_home)
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            with tempfile.TemporaryDirectory(prefix="guardian_opencode_") as scratch:
                attempt_env = _opencode_scratch_env(env, scratch)
                _exaone_pace()
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True, cwd=workdir, env=attempt_env)
                stdout, stderr = self._opencode_communicate(proc, prompt, max(0.0, deadline - time.monotonic()))
            answer, failure, served, refused = _opencode_parse(stdout)
            if served or refused:
                logger.info("opencode: %d tool call(s) served, %d refused", served, refused)
            if failure is None and proc.returncode != 0:
                detail = (stderr or "").strip()[-300:]  # stderr only: stdout holds no answer here and echoes the prompt
                failure = f"exited with code {proc.returncode}" + (f": {detail}" if detail else "")
            if failure is None and not answer:
                failure = "empty answer"
            if failure is None:
                self._turn.stats = {"elapsed_s": round(time.monotonic() - started, 1),
                                    "timeout_s": timeout, "timeout_source": timeout_source}
                return answer
            if attempt >= OPENCODE_MAX_RETRIES or not _opencode_rate_limited(stdout):
                raise RuntimeError(failure)
            pause = OPENCODE_RETRY_BACKOFF[attempt] + random.uniform(0, 5)
            if time.monotonic() + pause > deadline:
                raise RuntimeError(failure) from None  # the pause no longer fits the turn's own deadline
            logger.info("opencode: EXAONE rate limit on attempt %d, retrying in %.0f s", attempt + 1, pause)
            attempt += 1
            time.sleep(pause)

    # --- exaone: one stateless HTTP turn per call ---

    def _exaone_config(self) -> Tuple[str, str, Dict[str, Any]]:
        """(key, base url, request parameters) from the exaone provider env (env_policy), never from os.environ."""
        source = self._provider_env("exaone")
        key = source.get("EXAONE_API_KEY")
        if not key:
            raise RuntimeError("EXAONE_API_KEY is not set")
        effort = str(source.get("EXAONE_REASONING_EFFORT") or EXAONE_DEFAULT_EFFORT).strip().lower()
        timeout_source = "turn timeout"  # the runner's own limit (GUARDIAN_LLM_TIMEOUT), not EXAONE_TIMEOUT
        try:
            timeout = float(source["EXAONE_TIMEOUT"])
            timeout_source = "EXAONE_TIMEOUT"
        except (KeyError, TypeError, ValueError):
            timeout = self.timeout
        params = {
            "model": source.get("EXAONE_MODEL") or EXAONE_DEFAULT_MODEL,
            "effort": effort if effort in EXAONE_EFFORTS else EXAONE_DEFAULT_EFFORT,
            "max_tokens": _int_setting(source, "EXAONE_MAX_TOKENS", EXAONE_DEFAULT_MAX_TOKENS),
            "max_prompt_bytes": _int_setting(source, "EXAONE_MAX_PROMPT_BYTES", EXAONE_DEFAULT_MAX_PROMPT_BYTES),
            "timeout": timeout,
            "timeout_source": timeout_source,
            "stream": _exaone_stream_flag(source),
            "idle_timeout": _positive_float_setting(source, "EXAONE_IDLE_TIMEOUT", EXAONE_DEFAULT_IDLE_TIMEOUT),
        }
        return key, _exaone_base_url(source), params

    def _invoke_exaone(self, prompt: str) -> str:
        """One EXAONE turn: POST {base}/chat/completions with the combined prompt as a single user message.

        Retrieval stays in the prompt (```repo blocks): the request carries no tools and no chat_template_kwargs.
        Streaming is the default: the request asks for SSE, read line by line. EXAONE_STREAM=0 turns it off for
        diagnosis and asks for one JSON answer instead, a request unchanged byte for byte.

        On success the thread's turn stats hold the elapsed time (pacing and retries included, which is what the
        total deadline measures), the timeout and its source, the effort, the stream flag, the retry count, the
        first byte and the finish reason of the successful attempt, the prompt size and the token counts when the
        server sent usage (docs/design/exaone-provider.md, "Deadline margin"): the margin is read from events
        instead of inferred from event gaps. Never the key, the prompt or the answer text.
        """
        key, base, params = self._exaone_config()
        size = len(prompt.encode("utf-8"))
        if size > params["max_prompt_bytes"]:
            raise RuntimeError(f"prompt is {size} bytes, above EXAONE_MAX_PROMPT_BYTES ({params['max_prompt_bytes']})")
        body = json.dumps({
            "model": params["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 1.0,
            "top_p": 0.95,
            "reasoning_effort": params["effort"],
            "max_tokens": params["max_tokens"],
            "stream": params["stream"],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "text/event-stream" if params["stream"] else "application/json"})
        started = time.monotonic()
        info: Dict[str, Any] = {}
        if params["stream"]:
            text = self._exaone_stream_turn(request, params, key, info)
        else:
            payload = self._exaone_send(request, params["timeout"], key, _exaone_json_body, info=info)
            if isinstance(payload, dict):
                info.update(_exaone_usage_fields(payload.get("usage")))
                try:
                    reason = payload["choices"][0].get("finish_reason")
                except (KeyError, IndexError, TypeError, AttributeError):
                    reason = None
                if isinstance(reason, str) and reason:
                    info["finish_reason"] = reason
            text = _exaone_answer(payload)
        stats: Dict[str, Any] = {
            "elapsed_s": round(time.monotonic() - started, 1),
            "timeout_s": params["timeout"],
            "timeout_source": params["timeout_source"],
            "effort": params["effort"],
            "stream": bool(params["stream"]),
            "retries": int(info.get("retries") or 0),
            "prompt_bytes": size,
        }
        if isinstance(info.get("finish_reason"), str) and info["finish_reason"]:
            stats["finish_reason"] = info["finish_reason"]
        first_line, attempt_at = info.get("first_line_at"), info.get("attempt_at")
        if first_line is not None and attempt_at is not None:
            stats["first_byte_s"] = round(first_line - attempt_at, 1)
        stats.update({name: info[name] for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens")
                      if name in info})
        self._turn.stats = stats
        return text

    def _exaone_stream_turn(self, request: urllib.request.Request, params: Dict[str, Any], key: str,
                            info: Optional[Dict[str, Any]] = None) -> str:
        """One streamed turn: urlopen gets the idle timeout as its socket timeout, while params["timeout"] is the
        total deadline of the whole turn (connection, retries and reading included), so a stream cannot outlive it.
        _exaone_send enforces the deadline across retries, the read callback while the body streams. info (see
        _invoke_exaone) collects the facts of the successful attempt for the turn stats."""
        deadline = time.monotonic() + params["timeout"]
        text = _exaone_deadline_text(params["timeout"], params["timeout_source"])
        return self._exaone_send(
            request, params["idle_timeout"], key,
            lambda response: _exaone_stream_body(response, deadline, params["idle_timeout"], key, text, info),
            (deadline, text), info)

    def _exaone_send(self, request: urllib.request.Request, timeout: float, key: str,
                     read: Callable[[Any], Any],
                     deadline: Optional[Tuple[float, str]] = None,
                     info: Optional[Dict[str, Any]] = None) -> Any:
        """Sends one paced request per attempt; a 429/5xx with a retry-after of at most EXAONE_MAX_RETRY_AFTER seconds
        is retried (at most EXAONE_MAX_RETRIES times, with jitter), every other error fails at once so the fallback
        chain moves on instead of waiting out the turn timeout. read consumes the open response and runs inside the
        try, so only an HTTPError raised before the body starts is retried: a failure while reading the body (a
        broken, malformed or cut-off stream) propagates at once and fails the turn without another attempt.
        Streamed turns pass deadline as (absolute time.monotonic() limit, failure text): once the limit is past, or
        a retry sleep would pass it, the turn fails with that text, so retries cannot outlive the total deadline.
        info (optional, mutable) receives the start time of each attempt ("attempt_at") and, when a retry is taken,
        its count ("retries"); the caller reads it only after a success."""
        retries = 0
        while True:
            if info is not None:
                info["attempt_at"] = time.monotonic()
            if deadline is not None and time.monotonic() > deadline[0]:
                raise RuntimeError(deadline[1]) from None
            _exaone_pace()
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return read(response)
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read()
                finally:
                    exc.close()  # the error holds the open response; free the socket now
                retriable = exc.code == 429 or exc.code >= 500
                wait = _exaone_retry_seconds(exc.headers) if retriable else None
                if wait is not None and wait <= EXAONE_MAX_RETRY_AFTER and retries < EXAONE_MAX_RETRIES:
                    pause = wait + random.uniform(0, 0.25)
                    if deadline is not None and time.monotonic() + pause > deadline[0]:
                        raise RuntimeError(deadline[1]) from None
                    retries += 1
                    if info is not None:
                        info["retries"] = retries
                    time.sleep(pause)
                    continue
                raise RuntimeError(_exaone_error_message(exc.code, exc.headers, body, key)) from None
            except (OSError, http.client.HTTPException) as exc:
                # Network errors (URLError, socket.timeout/TimeoutError, a reset connection, a broken response)
                # fail the turn as a RuntimeError, which the run_* loops catch and move the chain on.
                raise RuntimeError(f"request failed: {exc}") from exc

    @staticmethod
    def _label(name: str) -> str:
        """Result label of a provider: binary providers answer as <name>-cli; exaone calls an API (exaone-api)."""
        return "exaone-api" if name == "exaone" else f"{name}-cli"

    def _invoker(self, name: str) -> Optional[Callable[[str, str], str]]:
        simple = {
            "ollama": lambda p, d: self._run_simple("ollama", ["ollama", "run", "llama3", p], d),
            "llm": lambda p, d: self._run_simple("llm", ["llm", p], d),
        }
        runners = {"claude": self._run_claude, "codex": self._run_codex, "agy": self._run_agy,
                   "zai": lambda p, d: self._run_claude(p, d, "zai"),
                   "exaone": lambda p, d: self._invoke_exaone(p),
                   "opencode": self._run_opencode}
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

    def _copy_turn_stats(self) -> Dict[str, Any]:
        """A copy of this thread's stats of the provider attempt that just succeeded ({} when it set none).

        The stats live in a threading.local, so the parallel agents of one debate (which share this runner) never
        read another thread's turn; the copy keeps the LLMResult independent of what the next attempt overwrites."""
        return dict(getattr(self._turn, "stats", None) or {})

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
        repo_mcp (see _session_claude, _session_codex, _opencode_config) attaches the read-only repo tools, but only
        when claude, zai, codex or opencode is the provider that actually runs; every other provider ignores it.
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
            # A fresh stats holder per attempt: only exaone and opencode fill it, and only the attempt that
            # succeeds hands its numbers on to the result (a fallback answer carries none of them).
            self._turn.stats = {}
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
                elif name == "opencode":
                    # Stateless (no session id to resume or store), but repo_mcp applies: the repo_* tools
                    # travel in the config, exactly as the generic path runs the turn.
                    with tempfile.TemporaryDirectory(prefix="guardian_llm_") as scratch:
                        return LLMResult(self._label(name), self._run_opencode(full_prompt, scratch, repo_mcp=repo_mcp),
                                         failures=list(errors), stats=self._copy_turn_stats())
                else:
                    invoke = self._invoker(name)
                    if invoke is None:
                        errors.append(f"{name}: unsupported provider")
                        continue
                    with tempfile.TemporaryDirectory(prefix="guardian_llm_") as scratch:
                        # exaone and opencode record their turn stats; every other provider leaves them empty.
                        # Copy them only after the turn ran: the invoker is what fills them.
                        answer = invoke(full_prompt, scratch)
                        stats = self._copy_turn_stats() if name in ("exaone", "opencode") else {}
                        return LLMResult(self._label(name), answer, failures=list(errors), stats=stats)
                return LLMResult(self._label(name), text, sid, resumed=resume_id is not None, failures=list(errors))
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: timed out after {self.timeout:.0f}s")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                errors.append(f"{name}: {exc}")
            logger.info("LLM provider %s failed, trying next: %s", name, errors[-1])

        return LLMResult(BUILTIN_PROVIDER, self._fallback(prompt, context, errors), failures=list(errors))

    # @MX:ANCHOR: Routes analysis prompts to the selected provider CLI.
    # @MX:REASON: Diagnostic, security and debate agents share this external integration boundary.
    def run_prompt(self, prompt: str, context: Optional[Dict[str, Any]] = None,
                   system: Optional[str] = None) -> Tuple[str, str]:
        """
        Runs the prompt with the first provider CLI that succeeds, else the built-in engine.
        system replaces the default role line and leaves the context out of the model's prompt (see _build_prompt); the
        built-in fallback still reads the context.
        Returns: (provider_name, response_text)
        """
        full_prompt = self._build_prompt(prompt, context, system)
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
                    return self._label(name), invoke(full_prompt, workdir)
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

"""
Operator helpers for persisted debate sessions.

Debate agents run their provider CLIs non-interactively and exit after every call, so idle
sessions hold no memory. An operator can reopen any stored session interactively in a tmux
window; closing that window ends the process again while the conversation stays on disk.
"""

import os
import re
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from llm_runner import interactive_resume_command, llm_env_path, load_llm_env, session_workdir, zai_claude_env


def tmux_session_name() -> str:
    return os.environ.get("GUARDIAN_TMUX_SESSION", "guardian")


def window_name(thread_id: str, role: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
    return f"{thread_id[-6:]}-{slug}"[:40]


def shell_with_llm_env(command: List[str], provider: str = "claude") -> str:
    """Shell line that loads the credentials file before exec'ing the CLI.

    The file is sourced inside the window, so tokens never appear in tmux or process arguments.
    zai runs the claude binary against z.ai: the line drops the claude login and maps ZAI_API_KEY onto the name claude
    reads, as a shell expansion (the key itself is never written into the command).
    """
    env_file = shlex.quote(str(llm_env_path()))
    load = f"if [ -f {env_file} ]; then set -a; . {env_file}; set +a; fi; "
    if provider != "zai":
        return f"{load}exec {shlex.join(command)}"
    settings = " ".join(f"{k}={shlex.quote(v)}" for k, v in zai_claude_env({**os.environ, **load_llm_env()}).items())
    return (f"{load}unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY; "
            f'export ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY" {settings}; exec {shlex.join(command)}')


def tmux_open_commands(stored: Dict[str, Any], has_session: bool) -> List[List[str]]:
    """Builds the tmux commands that open a stored agent session in its own window."""
    session = tmux_session_name()
    workdir = str(session_workdir())
    resume = shell_with_llm_env(interactive_resume_command(stored["provider"], stored["session_id"]),
                                provider=stored["provider"])
    name = window_name(stored["thread_id"], stored["role"])
    if has_session:
        return [["tmux", "new-window", "-t", f"{session}:", "-n", name, "-c", workdir, resume]]
    return [["tmux", "new-session", "-d", "-s", session, "-n", name, "-c", workdir, resume]]


def open_in_tmux(stored: Dict[str, Any]) -> str:
    """Opens the session in tmux and returns the command the operator should run to see it."""
    if shutil.which("tmux") is None:
        raise RuntimeError("tmux is not installed")
    session = tmux_session_name()
    has_session = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0
    for command in tmux_open_commands(stored, has_session):
        subprocess.run(command, check=True)
    if os.environ.get("TMUX"):
        return f"tmux switch-client -t {session}"
    return f"tmux attach -t {session}"


def pick_session(sessions: List[Dict[str, Any]], role: Optional[str]) -> Dict[str, Any]:
    """Selects one stored session of a thread, by role name prefix when several exist."""
    if role:
        wanted = role.lower()
        matches = [s for s in sessions if s["role"].lower().startswith(wanted)]
    else:
        matches = sessions
    if not matches:
        raise LookupError(f"no stored session matches role {role!r}")
    if len(matches) > 1:
        roles = ", ".join(s["role"] for s in matches)
        raise LookupError(f"several sessions match, choose one with --role ({roles})")
    return matches[0]

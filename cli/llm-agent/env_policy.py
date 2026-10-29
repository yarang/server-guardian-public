"""
Environment policy for the child processes Guardian starts.

Tool actions (tests, git, remediation) and provider CLIs run model-influenced code, so they get a
minimal environment instead of the hub agent's full one: never AGENT_TOKEN, jev keys or another
provider's credentials. This narrows what a prompt-injected process can read from its env; it does
not stop it from reading credential files the hub user can read (~/.guardian/agent.env).
"""

import re
from typing import Dict, Iterable, Mapping, Tuple

# Variables every child keeps: locale, terminal, temp dirs, proxies and CA bundles.
BASE_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
}
BASE_PREFIXES = ("LC_", "XDG_")

# Extra variables tool actions need for builds and tests (git identity, Go, Python, npm, cgo).
TOOL_PREFIXES = ("GO", "PYTHON", "GIT_", "CGO_", "NODE_", "NPM_CONFIG_", "npm_config_")
TOOL_NAMES = {"CC", "CXX", "VIRTUAL_ENV"}

# Credentials each provider CLI may see; a provider never gets another provider's keys.
PROVIDER_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "claude": ("ANTHROPIC_", "CLAUDE_"),
    "codex": ("OPENAI_", "CODEX_"),
    "agy": ("GEMINI_", "GOOGLE_", "ANTIGRAVITY_", "AGY_"),
    "zai": ("ZAI_",),
    "opencode": ("OPENCODE_",),
    "ollama": ("OLLAMA_",),
    "llm": ("LLM_", "OPENAI_"),
}
PROVIDER_NAMES: Dict[str, Tuple[str, ...]] = {
    "claude": ("API_TIMEOUT_MS",),
}

# Never passed to any child, even when a provider prefix would match.
NEVER_PREFIXES = ("AGENT_", "TYPESAFE_", "GUARDIAN_")
# AUTH also catches GOAUTH and *_AUTH_*; GIT_AUTHOR_* is an identity, not a credential.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE_KEY|CREDENTIAL|AUTH(?!OR)", re.IGNORECASE)


def is_secret_name(name: str) -> bool:
    """True for variables that look like credentials (AGENT_TOKEN, OPENAI_API_KEY, DB_PASSWORD, ...)."""
    return name.startswith(NEVER_PREFIXES) or bool(SECRET_NAME.search(name))


def _select(source: Mapping[str, str], names: Iterable[str], prefixes: Tuple[str, ...]) -> Dict[str, str]:
    names = set(names)
    return {k: v for k, v in source.items() if k in names or k.startswith(prefixes)}


def base_env(source: Mapping[str, str]) -> Dict[str, str]:
    """Locale, paths and network settings only; no credentials."""
    return _select(source, BASE_NAMES, BASE_PREFIXES)


def tool_env(source: Mapping[str, str]) -> Dict[str, str]:
    """Environment for tool actions: base plus build/test variables, minus anything secret-looking."""
    env = {**base_env(source), **_select(source, TOOL_NAMES, TOOL_PREFIXES)}
    return {k: v for k, v in env.items() if not is_secret_name(k)}


def provider_env(name: str, source: Mapping[str, str]) -> Dict[str, str]:
    """Environment for one provider CLI: base plus that provider's own credentials only."""
    own = _select(source, PROVIDER_NAMES.get(name, ()), PROVIDER_PREFIXES.get(name, ()))
    own = {k: v for k, v in own.items() if not k.startswith(NEVER_PREFIXES)}
    return {**base_env(source), **own}

"""
Projects the debate engine can read and change: one git workspace per project ("kind": "code", the
default), or a set of fleet hosts without any repository ("kind": "ops", docs/design/server-ops-project.md).

A project file (JSON) names the repository, its workspace clone on the hub, the branch to track,
and the test commands the judge may run there. Builtin projects live in configs/projects in this
repository; projects added from the dashboard go to <GUARDIAN_DATA_DIR>/projects and override a
builtin project with the same id, like teams.

    {
      "id": "my-service",                        # [a-z0-9][a-z0-9_-]{0,63}
      "name": "My Service",
      "description": "...",
      "repo": "https://git.example.com/me/my-service.git",   # https only; optional; "${VAR}" reads it from
                                                             # the environment (unset: no repo)
      "branch": "main",
      "workspace": "~/guardian-workspaces/my-service",          # optional; this is the default
      "tests": {
        "default": "cargo test",                 # what execute_code_action runs with run_test and what gates a publish
        "commands": [{"prefix": "cargo test", "flags": {"--release": false, "-p": true}}]
      },
      "context": {"paths": ["src/**/*.rs"], "ref": "HEAD"}     # optional default code context
    }

An ops project names target hosts and a policy instead of a repository:

    {
      "id": "fleet-ops", "kind": "ops", "name": "...",
      "targets": {"all": false, "groups": ["arm-nodes"], "nodes": ["site-a-arm2"], "exclude": []},
      "policy": {"allowed_actions": ["jev_mcp:server_remediation"], "include_hub": false}
    }

Its debates read no code; its actions are server tools whose target host must be one of the resolved
targets (ops_targets.py). It has no workspace, so sync, publish, workspace reset and the wiki refuse it.

A design project reviews documents and diagrams instead (docs/design/design-project.md). It names the artifact
types it works on and a review rubric; the artifact text is pasted into the task. It has no workspace either,
and one tool, write_artifact (policy.allowed_actions; [] turns it off, the judge's actions list then stays empty):

    {
      "id": "arch-docs", "kind": "design", "name": "...",
      "artifacts": {"types": ["markdown", "mermaid", "svg"], "max_bytes": 262144, "retention_days": 30, "reference_repo": null,
                    "required_sections": {"decisions.md": ["Context", "Decision", "Consequences"]}},
      "review": {"rubric": ["consistency", "missing requirements", "diagram accuracy"]}
    }

Workspaces are plain clones. sync() clones a missing one and fast-forwards a clean one (no modified
tracked files and no untracked ones, and no publish running); it never discards local changes, and judge branches stay where they are.
"""

import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import artifact_ops
from typing import Any, Dict, Iterator, List, Optional, Tuple

PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_PROJECT_ID = "server-guardian"
CODE_KIND = "code"
OPS_KIND = "ops"
DESIGN_KIND = "design"
KINDS = (CODE_KIND, OPS_KIND, DESIGN_KIND)
# Kinds without a repository: sync, publish, workspace reset, the wiki and file reads refuse them.
NO_WORKSPACE_KINDS = (OPS_KIND, DESIGN_KIND)
KIND_LABELS = {CODE_KIND: "a code project", OPS_KIND: "an ops project", DESIGN_KIND: "a design project"}
# Keys only a code project takes; an ops or design project that names one is refused instead of silently ignored.
CODE_ONLY_KEYS = ("repo", "branch", "workspace", "tests", "context", "wiki")
# Artifact types a design project may name. html, png, jpeg and pdf wait for the phases that can show them safely.
ARTIFACT_TYPES = ("markdown", "mermaid", "svg")
LATER_ARTIFACT_TYPES = ("html", "png", "jpeg", "pdf")
ARTIFACT_KEYS = {"types", "max_bytes", "retention_days", "reference_repo", "required_sections"}
MAX_REQUIRED_SECTIONS = 32
DEFAULT_MAX_ARTIFACT_BYTES = 262144
MAX_ARTIFACT_BYTES = 1048576
# Versions that are not the latest of their thread expire after this many days (0 keeps everything).
DEFAULT_RETENTION_DAYS = artifact_ops.DEFAULT_RETENTION_DAYS
MAX_RETENTION_DAYS = artifact_ops.MAX_RETENTION_DAYS
REVIEW_KEYS = {"rubric"}
DEFAULT_RUBRIC = ("consistency", "missing requirements", "diagram accuracy")
MAX_RUBRIC_ITEMS = 10
DESIGN_POLICY_KEYS = {"allowed_actions", "resident_query_agents"}
DESIGN_ACTIONS = (artifact_ops.TOOL,)
CODE_POLICY_KEYS = {"resident_query_agents"}
TARGET_KEYS = {"all", "groups", "nodes", "exclude"}
# Policy keys implemented so far; later phases add theirs (deny_paths, auto_run, ...), unknown keys are refused.
POLICY_KEYS = {"allowed_actions", "include_hub", "deny_paths", "deny_commands", "protected_packages", "auto_run", "max_auto_runs_per_day", "batch",
               "watch", "resident_query_agents"}
MAX_AUTO_RUNS_PER_DAY = 100
MAX_TARGET_IDS = 200
TARGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
# https://host[:port]/path, no credentials in the URL (use a git credential helper on the hub).
REPO_PATTERN = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._~/-]+$")
PREFIX_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,63}$")
FLAG_PATTERN = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9-]{0,40}$")
# A test prefix may not start a shell or an interpreter that runs arbitrary code from arguments.
FORBIDDEN_PROGRAMS = {
    "sh", "bash", "zsh", "dash", "fish", "env", "sudo", "su", "xargs", "eval", "exec", "nohup",
    "python", "python3", "node", "perl", "ruby", "php", "lua", "deno", "bun", "awk", "sed",
    "ssh", "scp", "curl", "wget", "rm", "mv", "cp", "dd", "git",
}
PACKAGE_RUNNERS = {"npx", "bunx", "pnpx"}
RUNNER_SUBCOMMANDS = {("npm", "exec"), ("pnpm", "dlx"), ("pnpm", "exec"), ("yarn", "dlx"), ("yarn", "exec")}
MAX_DEFAULT_TESTS = 5
MAX_TEST_COMMANDS = 10
MAX_WIKI_DOC_GLOBS = 20
MAX_TEXT = 500
GIT_TIMEOUT = 300


class ProjectValidationError(ValueError):
    """A project file that cannot be used."""


class WorkspaceBusy(ProjectValidationError):
    """Another publish or sync holds the workspace lock; nothing was changed."""


class NoWorkspace(ProjectValidationError):
    """An ops or design project has no repository: the git features (sync, publish, reset, wiki, files) do not apply."""


@dataclass
class Project:
    id: str
    name: str = ""
    description: str = ""
    repo: Optional[str] = None
    branch: str = "main"
    workspace_raw: Optional[str] = None
    default_test: Optional[str] = None  # first entry of default_tests
    default_tests: List[str] = field(default_factory=list)
    test_commands: List[Tuple[Tuple[str, ...], Dict[str, bool]]] = field(default_factory=list)
    context: Optional[Dict[str, Any]] = None
    # wiki.docs: globs of the design documents that get a feature page (None: wiki_build.DEFAULT_DOC_GLOBS)
    wiki_docs: Optional[List[str]] = None
    kind: str = CODE_KIND
    # ops projects: {"all", "groups", "nodes", "exclude"} and {"allowed_actions", "include_hub"}
    targets: Dict[str, Any] = field(default_factory=dict)
    policy: Dict[str, Any] = field(default_factory=dict)
    # Team of the project's debates when the request names none and the thread stored none (configs/teams).
    team: Optional[str] = None
    # design projects: {"types", "max_bytes", "reference_repo"} and {"rubric"}
    artifacts: Dict[str, Any] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ops(self) -> bool:
        return self.kind == OPS_KIND

    @property
    def is_design(self) -> bool:
        return self.kind == DESIGN_KIND

    @property
    def has_workspace(self) -> bool:
        """Whether the project has a repository clone; ops and design projects have none."""
        return self.kind not in NO_WORKSPACE_KINDS

    @property
    def kind_label(self) -> str:
        """"an ops project" / "a design project" / "a code project", for messages."""
        return KIND_LABELS.get(self.kind, f"a {self.kind} project")

    @property
    def clone_path(self) -> Path:
        """Where sync() keeps the clone (GUARDIAN_WORKSPACE for the default project when set)."""
        if not self.has_workspace:
            raise NoWorkspace(f"project {self.id} is {self.kind_label} and has no repository")
        if self.id == DEFAULT_PROJECT_ID and os.environ.get("GUARDIAN_WORKSPACE"):
            return Path(os.environ["GUARDIAN_WORKSPACE"]).expanduser().resolve()
        return Path(self.workspace_raw or f"~/guardian-workspaces/{self.id}").expanduser().resolve()

    @property
    def workspace(self) -> Path:
        """The clone this project's debates read and its actions change.

        The default project keeps its older setup: GUARDIAN_WORKSPACE when set (the hub), else the
        configured clone, else this checkout (read-only, see mcp_client.GUARDIAN_ROOT).
        """
        path = self.clone_path
        explicit = self.id == DEFAULT_PROJECT_ID and os.environ.get("GUARDIAN_WORKSPACE")
        if self.id == DEFAULT_PROJECT_ID and not explicit and not (path / ".git").exists():
            from mcp_client import GUARDIAN_ROOT

            return GUARDIAN_ROOT.resolve()
        return path


def _text(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ProjectValidationError(f"{where} must be a string of at most {MAX_TEXT} characters")
    return value


def _test_command(raw: Any, i: int) -> Tuple[Tuple[str, ...], Dict[str, bool]]:
    where = f"tests.commands[{i}]"
    if not isinstance(raw, dict):
        raise ProjectValidationError(f"{where} must be an object")
    try:
        prefix = tuple(shlex.split(str(raw.get("prefix", ""))))
    except ValueError as exc:
        raise ProjectValidationError(f"{where}.prefix: {exc}") from exc
    if not prefix or len(prefix) > 4 or not all(PREFIX_TOKEN.match(t) for t in prefix):
        raise ProjectValidationError(f"{where}.prefix must be 1-4 plain words (e.g. 'cargo test')")
    if os.path.basename(prefix[0]) in FORBIDDEN_PROGRAMS or "/" in prefix[0]:
        raise ProjectValidationError(f"{where}.prefix may not start with {prefix[0]!r}")
    # Package runners fetch and run whatever package an argument names: pin the package.
    if prefix[0] in PACKAGE_RUNNERS and len(prefix) < 2 or prefix[:2] in RUNNER_SUBCOMMANDS:
        raise ProjectValidationError(f"{where}.prefix must name the tool, e.g. 'npx vitest run', not a bare package runner")
    flags = raw.get("flags", {})
    if not isinstance(flags, dict) or len(flags) > 30:
        raise ProjectValidationError(f"{where}.flags must be an object of at most 30 flags")
    for name, takes_value in flags.items():
        if not FLAG_PATTERN.fullmatch(str(name)) or not isinstance(takes_value, bool):
            raise ProjectValidationError(f"{where}.flags: {name!r} must map a flag like '-v' to true/false")
    return prefix, dict(flags)


TEAM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]{0,63})\}")


def _team(data: Dict[str, Any]) -> Optional[str]:
    team = data.get("team") or None
    if team is not None and (not isinstance(team, str) or not TEAM_ID.fullmatch(team)):
        raise ProjectValidationError("team must be a team id such as fleet-ops")
    return team


def _target_ids(raw: Any, where: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TARGET_IDS or not all(
            isinstance(i, str) and TARGET_ID.match(i) for i in raw):
        raise ProjectValidationError(f"{where} must be a list of at most {MAX_TARGET_IDS} node or group ids")
    return list(dict.fromkeys(raw))


BATCH_KEYS = {"max_parallel_hosts", "auto", "max_plans_per_day", "max_steps", "scripts", "script_groups"}
BATCH_SCRIPT_MODES = ("approve", "vote")
BATCH_DEFAULT_PARALLEL_HOSTS = 2
BATCH_MAX_PARALLEL_HOSTS = 8
BATCH_DEFAULT_STEPS = 20
BATCH_MAX_STEPS = 100  # plan_ops.MAX_STEPS: a plan has no more, so a higher cap could never bind


def _batch_int(raw: Dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ProjectValidationError(f"policy.batch.{key} must be an integer from {low} to {high}")
    return value


def _parse_resident_query_agents(raw: Any) -> Optional[List[str]]:
    """Optional policy.resident_query_agents (docs/design/resident-session-query.md, QD1):
    None = any declared resident agent; [] = disabled for this project; [addr, ...] = allowlist."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_TARGET_IDS or not all(isinstance(a, str) for a in raw):
        raise ProjectValidationError("policy.resident_query_agents must be a list of resident addresses (name@node)")
    import mail_store
    cleaned: List[str] = []
    for addr in raw:
        val = addr.strip()
        if not mail_store.valid_address(val):
            raise ProjectValidationError(f"policy.resident_query_agents: invalid resident address {addr!r}")
        if val not in cleaned:
            cleaned.append(val)
    return cleaned


def _parse_batch(raw: Any) -> Dict[str, Any]:
    """policy.batch of an ops project (docs/design/batch-execution.md): how many hosts a plan works on at once and
    whether a plan the critics did not oppose may run without a person (off by default, capped per day)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProjectValidationError("policy.batch must be an object")
    unknown = sorted(set(raw) - BATCH_KEYS)
    if unknown:
        raise ProjectValidationError(f"policy.batch: unknown keys {unknown}; known: {sorted(BATCH_KEYS)}")
    hosts = _batch_int(raw, "max_parallel_hosts", BATCH_DEFAULT_PARALLEL_HOSTS, 1, BATCH_MAX_PARALLEL_HOSTS)
    auto = raw.get("auto", False)
    if not isinstance(auto, bool):
        raise ProjectValidationError("policy.batch.auto must be true or false")
    plans = _batch_int(raw, "max_plans_per_day", 0, 0, MAX_AUTO_RUNS_PER_DAY)
    if auto and not plans:
        raise ProjectValidationError("policy.batch.auto needs max_plans_per_day of at least 1")
    steps = _batch_int(raw, "max_steps", BATCH_DEFAULT_STEPS, 1, BATCH_MAX_STEPS)
    # Ad-hoc scripts are irreversible: "approve" always sends them to a person; "vote" lets the critics' clean vote
    # release them on the hosts of script_groups (docs/design/batch-execution.md, 12.5), which needs auto.
    scripts = raw.get("scripts", "approve")
    if scripts not in BATCH_SCRIPT_MODES:
        raise ProjectValidationError(f"policy.batch.scripts must be one of {', '.join(BATCH_SCRIPT_MODES)}")
    script_groups = _target_ids(raw.get("script_groups"), "policy.batch.script_groups")
    if scripts == "vote" and not auto:
        raise ProjectValidationError('policy.batch.scripts "vote" needs policy.batch.auto')
    if scripts == "vote" and not script_groups:
        raise ProjectValidationError('policy.batch.scripts "vote" needs script_groups: the host groups where scripts may run')
    if script_groups and scripts != "vote":
        raise ProjectValidationError('policy.batch.script_groups is only used with scripts "vote"')
    return {"max_parallel_hosts": hosts, "auto": auto, "max_plans_per_day": plans, "max_steps": steps,
            "scripts": scripts, "script_groups": script_groups}


def _parse_ops(data: Dict[str, Any], pid: str) -> Project:
    from mcp_client import SERVER_TOOLS

    named = [k for k in CODE_ONLY_KEYS if data.get(k) not in (None, "", {}, [])]
    if named:
        raise ProjectValidationError(f"an ops project has no repository; remove {', '.join(named)}")
    if data.get("runbook") is not None:
        raise ProjectValidationError("runbook is reserved and must be null for now")
    _refuse_design_keys(data)
    targets = data.get("targets")
    if not isinstance(targets, dict):
        raise ProjectValidationError("targets must be an object with all, groups, nodes and exclude")
    unknown = sorted(set(targets) - TARGET_KEYS)
    if unknown:
        raise ProjectValidationError(f"targets: unknown keys {unknown}; known: {sorted(TARGET_KEYS)}")
    every = targets.get("all", False)
    if not isinstance(every, bool):
        raise ProjectValidationError("targets.all must be true or false")
    parsed = {"all": every, **{k: _target_ids(targets.get(k), f"targets.{k}") for k in ("groups", "nodes", "exclude")}}
    if not (every or parsed["groups"] or parsed["nodes"]):
        raise ProjectValidationError("targets: set all, or name at least one group or node")
    policy = data.get("policy") or {}
    if not isinstance(policy, dict):
        raise ProjectValidationError("policy must be an object")
    unknown = sorted(set(policy) - POLICY_KEYS)
    if unknown:
        raise ProjectValidationError(f"policy: unknown keys {unknown}; known: {sorted(POLICY_KEYS)}")
    # Package install and removal change what runs on a host: a project lists them explicitly (never by default).
    import ops_packages

    # The same holds for the batch steps (command sets, scripts): a project opts in by naming them.
    import command_sets

    allowed = policy.get("allowed_actions", sorted(SERVER_TOOLS - set(ops_packages.TOOLS) - set(command_sets.TOOLS)))
    if not isinstance(allowed, list) or not all(isinstance(t, str) for t in allowed):
        raise ProjectValidationError("policy.allowed_actions must be a list of tool names")
    bad = [t for t in allowed if t not in SERVER_TOOLS]
    if bad:
        raise ProjectValidationError(f"policy.allowed_actions: {bad} are not server tools; known: {sorted(SERVER_TOOLS)}")
    include_hub = policy.get("include_hub", False)
    if not isinstance(include_hub, bool):
        raise ProjectValidationError("policy.include_hub must be true or false")
    import ops_paths

    try:
        deny_paths = ops_paths.check_deny_patterns(policy.get("deny_paths"))
    except ValueError as exc:
        raise ProjectValidationError(f"policy.{exc}") from exc
    try:
        deny_commands = command_sets.check_deny_commands(policy.get("deny_commands"))
    except ValueError as exc:
        raise ProjectValidationError(f"policy.{exc}") from exc
    # protected_packages: name patterns package actions never remove on top of the built-in list and the host roles.
    import package_roles

    try:
        protected_packages = package_roles.check_patterns(policy.get("protected_packages"), "protected_packages")
    except ValueError as exc:
        raise ProjectValidationError(f"policy.{exc}") from exc
    # auto_run: low-risk catalog actions the engine may run without a person, once jev passed the decision.
    import ops_actions

    auto_run = policy.get("auto_run") or []
    if not isinstance(auto_run, list) or not all(isinstance(t, str) for t in auto_run):
        raise ProjectValidationError("policy.auto_run must be a list of tool names")
    low = sorted(t for t in ops_actions.ACTIONS if ops_actions.risk(t, {"scope": ops_actions.LOW_SCOPES[0]}) == "low")
    bad = [t for t in auto_run if t not in low]
    if bad:
        raise ProjectValidationError(f"policy.auto_run: {bad} cannot run automatically; only low-risk catalog actions: {low}")
    missing = [t for t in auto_run if t not in allowed]
    if missing:
        raise ProjectValidationError(f"policy.auto_run: {missing} must also be in allowed_actions")
    max_runs = policy.get("max_auto_runs_per_day", 0)
    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or not 0 <= max_runs <= MAX_AUTO_RUNS_PER_DAY:
        raise ProjectValidationError(f"policy.max_auto_runs_per_day must be an integer from 0 to {MAX_AUTO_RUNS_PER_DAY}")
    if auto_run and not max_runs:
        raise ProjectValidationError("policy.auto_run needs max_auto_runs_per_day of at least 1")
    batch = _parse_batch(policy.get("batch"))
    if batch["scripts"] == "vote" and command_sets.TOOL_SCRIPT not in allowed:
        raise ProjectValidationError(f'policy.batch.scripts "vote" needs {command_sets.TOOL_SCRIPT} in allowed_actions')
    import ops_watch

    try:
        watch = ops_watch.parse_watch(policy.get("watch"))
    except ValueError as exc:
        raise ProjectValidationError(f"policy.{exc}") from exc
    rqa = _parse_resident_query_agents(policy.get("resident_query_agents"))
    ops_policy = {"allowed_actions": list(dict.fromkeys(allowed)), "include_hub": include_hub, "deny_paths": deny_paths,
                  "deny_commands": deny_commands,
                  "protected_packages": protected_packages, "auto_run": list(dict.fromkeys(auto_run)),
                  "max_auto_runs_per_day": max_runs, "watch": watch, "batch": batch}
    if rqa is not None:
        ops_policy["resident_query_agents"] = rqa
    return Project(
        id=pid,
        name=_text(data.get("name"), "name") or pid,
        description=_text(data.get("description"), "description"),
        kind=OPS_KIND,
        team=_team(data),
        targets=parsed,
        policy=ops_policy,
    )


def _refuse_design_keys(data: Dict[str, Any]) -> None:
    for key in ("artifacts", "review"):
        if data.get(key) is not None:
            raise ProjectValidationError(f"{key} belongs to design projects; set \"kind\": \"design\"")


def _parse_artifacts(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProjectValidationError("artifacts must be an object with types, max_bytes, retention_days, reference_repo and required_sections")
    unknown = sorted(set(raw) - ARTIFACT_KEYS)
    if unknown:
        raise ProjectValidationError(f"artifacts: unknown keys {unknown}; known: {sorted(ARTIFACT_KEYS)}")
    types = raw.get("types", list(ARTIFACT_TYPES))
    if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
        raise ProjectValidationError(f"artifacts.types must be a non-empty list of: {', '.join(ARTIFACT_TYPES)}")
    for t in types:
        if t in LATER_ARTIFACT_TYPES:
            raise ProjectValidationError(f"artifacts.types: {t} is not supported yet (a later phase of design projects)")
        if t not in ARTIFACT_TYPES:
            raise ProjectValidationError(f"artifacts.types: unknown type {t!r}; known: {', '.join(ARTIFACT_TYPES)}")
    max_bytes = raw.get("max_bytes", DEFAULT_MAX_ARTIFACT_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
        raise ProjectValidationError(f"artifacts.max_bytes must be an integer from 1 to {MAX_ARTIFACT_BYTES}")
    days = raw.get("retention_days", DEFAULT_RETENTION_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_RETENTION_DAYS:
        raise ProjectValidationError(
            f"artifacts.retention_days must be an integer from 0 (keep every version) to {MAX_RETENTION_DAYS}")
    if raw.get("reference_repo") is not None:
        raise ProjectValidationError("artifacts.reference_repo is reserved and must be null for now")
    return {"types": list(dict.fromkeys(types)), "max_bytes": max_bytes, "retention_days": days,
            "reference_repo": None, "required_sections": _parse_required_sections(raw.get("required_sections"))}


def _parse_required_sections(raw: Any) -> Dict[str, List[str]]:
    """artifacts.required_sections: Markdown file name -> headings it must contain. Empty by default."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProjectValidationError("artifacts.required_sections must map a Markdown file name to a list of headings")
    parsed: Dict[str, List[str]] = {}
    for name, headings in raw.items():
        if not isinstance(name, str) or not name.endswith(".md") or not artifact_ops.NAME_PATTERN.fullmatch(name):
            raise ProjectValidationError(f"artifacts.required_sections: {name!r} is not a Markdown artifact name")
        if (not isinstance(headings, list) or not 1 <= len(headings) <= MAX_REQUIRED_SECTIONS
                or not all(isinstance(h, str) and h.strip() and len(h) <= 120 and h.isprintable() for h in headings)):
            raise ProjectValidationError(
                f"artifacts.required_sections.{name} must be a list of 1 to {MAX_REQUIRED_SECTIONS} headings")
        parsed[name] = list(dict.fromkeys(h.strip() for h in headings))
    return parsed


def _parse_review(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProjectValidationError("review must be an object with a rubric")
    unknown = sorted(set(raw) - REVIEW_KEYS)
    if unknown:
        raise ProjectValidationError(f"review: unknown keys {unknown}; known: {sorted(REVIEW_KEYS)}")
    rubric = raw.get("rubric", list(DEFAULT_RUBRIC))
    if (not isinstance(rubric, list) or not 1 <= len(rubric) <= MAX_RUBRIC_ITEMS or not all(
            isinstance(r, str) and r.strip() and len(r) <= 200 and r.isprintable() for r in rubric)):
        raise ProjectValidationError(f"review.rubric must list 1..{MAX_RUBRIC_ITEMS} short single-line criteria")
    return {"rubric": list(dict.fromkeys(r.strip() for r in rubric))}


def _parse_design(data: Dict[str, Any], pid: str) -> Project:
    named = [k for k in CODE_ONLY_KEYS if data.get(k) not in (None, "", {}, [])]
    if named:
        raise ProjectValidationError(f"a design project has no repository; remove {', '.join(named)}")
    if data.get("targets") is not None:
        raise ProjectValidationError("targets belong to ops projects; set \"kind\": \"ops\"")
    if data.get("runbook") is not None:
        raise ProjectValidationError("runbook is reserved and must be null for now")
    policy = data.get("policy") or {}
    if not isinstance(policy, dict):
        raise ProjectValidationError("policy must be an object")
    unknown = sorted(set(policy) - DESIGN_POLICY_KEYS)
    if unknown:
        raise ProjectValidationError(f"policy: unknown keys {unknown}; known: {sorted(DESIGN_POLICY_KEYS)}")
    allowed = policy.get("allowed_actions", list(DESIGN_ACTIONS))
    if (not isinstance(allowed, list) or not all(isinstance(a, str) for a in allowed)
            or not set(allowed) <= set(DESIGN_ACTIONS)):
        raise ProjectValidationError(
            f"policy.allowed_actions: a design project may allow {', '.join(DESIGN_ACTIONS)} (or [] for none)")
    rqa = _parse_resident_query_agents(policy.get("resident_query_agents"))
    design_policy: Dict[str, Any] = {"allowed_actions": list(dict.fromkeys(allowed))}
    if rqa is not None:
        design_policy["resident_query_agents"] = rqa
    return Project(
        id=pid,
        name=_text(data.get("name"), "name") or pid,
        description=_text(data.get("description"), "description"),
        kind=DESIGN_KIND,
        policy=design_policy,
        artifacts=_parse_artifacts(data.get("artifacts")),
        review=_parse_review(data.get("review")),
    )


def design_prompt_text(project: Project) -> str:
    """What the agents of a design debate are told about the project, appended to the task."""
    return (
        f"Design project {project.id}: the artifact under review is pasted into the task (types this project works "
        f"on: {', '.join(project.artifacts['types'])}). "
        f"Review it against this rubric: {'; '.join(project.review['rubric'])}. "
        "You cannot read files or run anything: judge only the text you were given, and quote it when you object."
    )


def parse_project(data: Any) -> Project:
    """Validates a project file. Raises ProjectValidationError."""
    if not isinstance(data, dict):
        raise ProjectValidationError("a project must be a JSON object")
    pid = data.get("id")
    if not isinstance(pid, str) or not PROJECT_ID_PATTERN.fullmatch(pid):
        raise ProjectValidationError("id: use 1-64 lowercase letters, digits, - or _ (starting with a letter or digit)")
    kind = data.get("kind", CODE_KIND)
    if kind not in KINDS:
        raise ProjectValidationError(f"kind must be {CODE_KIND!r}, {OPS_KIND!r} or {DESIGN_KIND!r}")
    if kind in (OPS_KIND, DESIGN_KIND):
        if pid == DEFAULT_PROJECT_ID:
            raise ProjectValidationError(f"{DEFAULT_PROJECT_ID} is the code project of this repository")
        return _parse_ops(data, pid) if kind == OPS_KIND else _parse_design(data, pid)
    for key in ("targets", "runbook"):
        if data.get(key) is not None:
            raise ProjectValidationError(f"{key} belongs to ops projects; set \"kind\": \"ops\"")
    code_policy: Dict[str, Any] = {}
    if data.get("policy") is not None:
        raw_pol = data["policy"]
        if not isinstance(raw_pol, dict):
            raise ProjectValidationError("policy must be an object")
        extra_keys = sorted(set(raw_pol) - CODE_POLICY_KEYS)
        if extra_keys:
            raise ProjectValidationError(f"policy ({', '.join(extra_keys)}) belongs to ops projects; set \"kind\": \"ops\"")
        rqa = _parse_resident_query_agents(raw_pol.get("resident_query_agents"))
        if rqa is not None:
            code_policy["resident_query_agents"] = rqa
    _refuse_design_keys(data)
    repo = data.get("repo") or None
    # "${GUARDIAN_SELF_REPO}": the URL comes from the hub's environment (the installer writes it into agent.env), so
    # a project file in git names no account. Unset means the project has no repo URL (it still has its workspace).
    env_ref = ENV_REF.fullmatch(repo) if isinstance(repo, str) else None
    if env_ref:
        repo = os.environ.get(env_ref.group(1), "").strip() or None
    if repo is not None and (not isinstance(repo, str) or not REPO_PATTERN.fullmatch(repo) or ".." in repo):
        raise ProjectValidationError("repo must be an https URL without credentials, e.g. https://git.example.com/me/repo.git")
    branch = data.get("branch") or "main"
    if not isinstance(branch, str) or not BRANCH_PATTERN.fullmatch(branch) or ".." in branch:
        raise ProjectValidationError("branch: invalid branch name")
    workspace_raw = data.get("workspace") or None
    if workspace_raw is not None:
        if not isinstance(workspace_raw, str) or not workspace_raw.startswith("~/") or ".." in workspace_raw.split("/"):
            raise ProjectValidationError("workspace must be a path under the home directory, like ~/guardian-workspaces/<id>")
    tests = data.get("tests") or {}
    if not isinstance(tests, dict):
        raise ProjectValidationError("tests must be an object")
    commands_raw = tests.get("commands") or []
    if not isinstance(commands_raw, list) or len(commands_raw) > MAX_TEST_COMMANDS:
        raise ProjectValidationError(f"tests.commands must be a list of at most {MAX_TEST_COMMANDS}")
    commands = [_test_command(c, i) for i, c in enumerate(commands_raw)]
    default_raw = tests.get("default") or None
    if isinstance(default_raw, str):
        default_tests = [default_raw]
    elif isinstance(default_raw, list):
        if not 1 <= len(default_raw) <= MAX_DEFAULT_TESTS or not all(isinstance(c, str) and c.strip() for c in default_raw):
            raise ProjectValidationError(f"tests.default as a list needs 1..{MAX_DEFAULT_TESTS} non-empty command strings")
        default_tests = list(default_raw)
    elif default_raw is None:
        default_tests = []
    else:
        raise ProjectValidationError("tests.default must be a string or a list of strings")
    default_test = default_tests[0] if default_tests else None
    context = data.get("context") or None
    if context is not None:
        paths = context.get("paths") if isinstance(context, dict) else None
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise ProjectValidationError("context.paths must be a non-empty list of path patterns")
    wiki = data.get("wiki") or None
    wiki_docs: Optional[List[str]] = None
    if wiki is not None:
        docs = wiki.get("docs") if isinstance(wiki, dict) else None
        if docs is not None and (not isinstance(docs, list) or len(docs) > MAX_WIKI_DOC_GLOBS or not all(
                isinstance(g, str) and g and len(g) <= 200 and not g.startswith("/") and ".." not in g.split("/")
                and not any(c in g for c in "\r\n\0") for g in docs)):
            raise ProjectValidationError(f"wiki.docs must list at most {MAX_WIKI_DOC_GLOBS} repository-relative globs "
                                         "like docs/design/*.md")
        wiki_docs = list(docs) if docs is not None else None
    project = Project(
        id=pid,
        name=_text(data.get("name"), "name") or pid,
        description=_text(data.get("description"), "description"),
        repo=repo,
        branch=branch,
        workspace_raw=workspace_raw,
        default_test=default_test,
        default_tests=default_tests,
        test_commands=commands,
        context=context,
        wiki_docs=wiki_docs,
        team=_team(data),
        policy=code_policy,
    )
    if repo is None and workspace_raw is None:
        raise ProjectValidationError("give a repo to clone, or the workspace of an existing clone")
    home = Path.home().resolve()
    if project.id != DEFAULT_PROJECT_ID and home not in project.clone_path.parents:
        raise ProjectValidationError("workspace must be inside the home directory")
    if default_tests:
        # Every step is checked against the project's own allow-list, so the default can always run.
        from mcp_client import ActionValidationError, MCPClient

        checker = MCPClient(workspace=project.workspace, project=project)
        for index, command in enumerate(default_tests, 1):
            try:
                checker._test_argv(command)
            except ActionValidationError as exc:
                where = f"tests.default[{index}]" if isinstance(default_raw, list) else "tests.default"
                raise ProjectValidationError(f"{where}: {exc}") from exc
    return project


def check_ops_targets(project: Project) -> Dict[str, Any]:
    """Refuses an ops project that names unknown groups or nodes; returns the resolved targets.

    Only checked on save: a project already on disk stays loadable when a group it names is deleted later
    (resolve() then reports it under missing). Without a readable servers.json nothing can be checked.
    """
    import ops_targets

    resolved = ops_targets.resolve(project)
    if resolved["error"]:
        return resolved
    missing = [f"group {g}" for g in resolved["missing"]["groups"]] + [f"node {n}" for n in resolved["missing"]["nodes"]]
    if missing:
        raise ProjectValidationError(f"targets: unknown {', '.join(missing)}")
    script_groups = ((project.policy or {}).get("batch") or {}).get("script_groups") or []
    if script_groups:
        scripts = ops_targets.group_hosts(project, script_groups)
        if scripts["missing"]:
            raise ProjectValidationError(f"policy.batch.script_groups: unknown group {', '.join(scripts['missing'])}")
    return resolved


def builtin_projects_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "configs" / "projects"


def custom_projects_dir() -> Path:
    configured = os.environ.get("GUARDIAN_PROJECTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "projects"


def _git(args: List[str], cwd: Optional[Path] = None, timeout: int = 60) -> subprocess.CompletedProcess:
    # GIT_TERMINAL_PROMPT=0: a private repo without stored credentials fails instead of hanging.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


@contextmanager
def workspace_lock(ws: Path, busy: str) -> Iterator[None]:
    """The per-workspace flock that serializes publish and sync (<git dir>/guardian-publish.lock).

    Raises WorkspaceBusy(busy) when another process holds it; the lock is released on exit.
    """
    found = _git(["rev-parse", "--absolute-git-dir"], cwd=ws)
    git_dir = found.stdout.strip() if found.returncode == 0 else ""
    if not git_dir:
        raise ProjectValidationError(f"cannot locate the git directory of {ws}")
    with open(Path(git_dir) / "guardian-publish.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WorkspaceBusy(busy) from None
        yield


def workspace_status(project: Project) -> Dict[str, Any]:
    """Whether the workspace exists, and its branch, HEAD and modified-file count (ops and design projects have none)."""
    if not project.has_workspace:
        return {"kind": project.kind, "present": False}
    ws = project.workspace
    status: Dict[str, Any] = {"workspace": str(ws), "present": (ws / ".git").exists()}
    if not status["present"]:
        return status
    head = _git(["rev-parse", "HEAD"], cwd=ws)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws)
    dirty = _git(["status", "--porcelain", "--untracked-files=no"], cwd=ws)
    status.update(
        head=head.stdout.strip() or None,
        branch=branch.stdout.strip() or None,
        modified=len([line for line in dirty.stdout.splitlines() if line.strip()]),
    )
    return status


class ProjectManager:
    """Loads, saves and syncs project files (builtin read-only, custom overrides by id)."""

    def __init__(self, directory: Optional[Path] = None, builtin_dir: Optional[Path] = None):
        self.directory = directory or custom_projects_dir()
        self.builtin_dir = builtin_dir or builtin_projects_dir()

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_projects(self) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for source, directory in (("builtin", self.builtin_dir), ("custom", self.directory)):
            for path in sorted(directory.glob("*.json")):
                data = self._read(path)
                if data is not None:
                    merged[path.stem] = {**data, "source": source}
        return list(merged.values())

    def get_raw(self, project_id: str) -> Optional[Dict[str, Any]]:
        if not PROJECT_ID_PATTERN.fullmatch(project_id or ""):
            return None
        for directory in (self.directory, self.builtin_dir):
            path = directory / f"{project_id}.json"
            if path.exists():
                return self._read(path)
        return None

    def load(self, project_id: Optional[str]) -> Project:
        """Resolves a project id (default: server-guardian) to a validated project."""
        pid = project_id or DEFAULT_PROJECT_ID
        data = self.get_raw(pid)
        if data is None:
            raise ProjectValidationError(f"no project {pid!r} (see --projects)")
        project = parse_project({k: v for k, v in data.items() if k != "source"})
        if project.id != pid:
            raise ProjectValidationError(f"{pid}.json declares id {project.id!r}")
        return project

    def is_custom(self, project_id: str) -> bool:
        return bool(PROJECT_ID_PATTERN.fullmatch(project_id or "")) and (self.directory / f"{project_id}.json").exists()

    def save(self, data: Dict[str, Any]) -> Project:
        data = {k: v for k, v in data.items() if k != "source"} if isinstance(data, dict) else data
        project = parse_project(data)
        if project.is_ops:
            check_ops_targets(project)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{project.id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return project

    def delete(self, project_id: str) -> bool:
        """Deletes a custom project file. The workspace clone is left on disk."""
        if not self.is_custom(project_id):
            return False
        (self.directory / f"{project_id}.json").unlink()
        return True

    # @MX:WARN: [AUTO] Clones and updates git repositories on the hub.
    # @MX:REASON: Never discards local work: it only clones a missing workspace and fast-forwards a clean one.
    def sync(self, project: Project) -> Dict[str, Any]:
        """Clones a missing workspace, or fetches and fast-forwards a clean one on the project branch."""
        ws = project.clone_path
        from mcp_client import GUARDIAN_ROOT

        if ws == GUARDIAN_ROOT.resolve():
            raise ProjectValidationError("the workspace may not be the running Server Guardian checkout")
        if not (ws / ".git").exists():
            if ws.exists() and any(ws.iterdir()):
                raise ProjectValidationError(f"{ws} exists and is not a git clone")
            if not project.repo:
                raise ProjectValidationError(f"{ws} is missing and the project has no repo to clone")
            ws.parent.mkdir(parents=True, exist_ok=True)
            out = _git(["clone", "-q", "--branch", project.branch, "--", project.repo, str(ws)], timeout=GIT_TIMEOUT)
            if out.returncode != 0:
                raise ProjectValidationError(f"git clone failed: {out.stderr.strip()[-300:]}")
            self._ensure_identity(ws)
            return {"action": "cloned", **workspace_status(project)}

        self._ensure_identity(ws)
        with workspace_lock(ws, "a publish is running in this workspace; sync again when it has finished"):
            return self._update(project, ws)

    @staticmethod
    def _update(project: Project, ws: Path) -> Dict[str, Any]:
        """Fetch and fast-forward a workspace whose publish lock the caller holds."""
        fetch = _git(["fetch", "-q", "origin"], cwd=ws, timeout=GIT_TIMEOUT)
        if fetch.returncode != 0:
            raise ProjectValidationError(f"git fetch failed: {fetch.stderr.strip()[-300:]}")
        status = workspace_status(project)
        # Untracked files count as local work: executed actions leave new files untracked, and moving HEAD
        # under them would make their thread unpublishable.
        local = _git(["status", "--porcelain"], cwd=ws).stdout.splitlines()
        dirty = len([line for line in local if line.strip()])
        if dirty:
            return {"action": "skipped", "reason": f"{dirty} modified or untracked files", **status}
        if status.get("branch") != project.branch:
            checkout = _git(["checkout", "-q", project.branch], cwd=ws)
            if checkout.returncode != 0:
                return {"action": "skipped", "reason": f"cannot check out {project.branch}: {checkout.stderr.strip()[-200:]}", **status}
        merge = _git(["merge", "-q", "--ff-only", f"origin/{project.branch}"], cwd=ws)
        if merge.returncode != 0:
            return {"action": "skipped", "reason": f"not a fast-forward: {merge.stderr.strip()[-200:]}", **workspace_status(project)}
        return {"action": "updated", **workspace_status(project)}

    @staticmethod
    def _ensure_identity(ws: Path) -> None:
        """Judge commits need an author; set a local one when the clone has none."""
        if _git(["config", "user.email"], cwd=ws).returncode != 0:
            _git(["config", "user.email", f"guardian@{socket.gethostname()}"], cwd=ws)
        if _git(["config", "user.name"], cwd=ws).returncode != 0:
            _git(["config", "user.name", "Server Guardian"], cwd=ws)


class Workspaces:
    """Gives each action or thread an MCPClient bound to its project's workspace (cached per project)."""

    def __init__(self, db: Any, manager: Optional[ProjectManager] = None):
        self.db = db
        self.manager = manager or ProjectManager()
        self._clients: Dict[str, Any] = {}

    def for_project(self, project_id: Optional[str]):
        from mcp_client import MCPClient

        pid = project_id or DEFAULT_PROJECT_ID
        if pid not in self._clients:
            project = self.manager.load(pid)
            self._clients[pid] = MCPClient(workspace=project.workspace if project.has_workspace else None, project=project)
        return self._clients[pid]

    def for_thread(self, thread_id: str):
        return self.for_project(self.db.thread_project(thread_id))

    def for_action(self, action: Dict[str, Any]):
        return self.for_project(action.get("project_id") or self.db.thread_project(action["thread_id"]))

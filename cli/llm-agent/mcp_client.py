"""
JEV-MCP tool executor for Server Guardian.

The debate judge proposes tool actions; nothing here runs on its own. Actions are validated
against the tool catalog, stored as PROPOSED, and executed only after a person approves
them (see action_ops.py). The tools run in-process; this is not an MCP protocol client.

Guardrails:
- execute_code_action writes only inside the workspace (GUARDIAN_WORKSPACE, default: repo root)
- run_tests runs allowlisted commands without a shell, in the workspace
- committing and pushing is the operator's publish step (publish_ops.py); the old git_commit_push tool is retired
- server_remediation runs the approved command locally, or on a fleet host through the agent
- server tools belong to ops projects and code tools to code projects (project_registry kind); in an ops
  project the target host must be one of the project's resolved targets (ops_targets)
- a design project offers only write_artifact (KIND_TOOLS): a new version of an artifact in the design store, never a
  file of a repository; like an ops project it has no workspace
"""

import json
import os
import re
import shlex
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import artifact_ops
import ops_actions
import ops_packages
import patch_ops
from code_context import is_secret
from env_policy import tool_env
from evidence import strip_findings

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "jev_mcp:execute_code_action",
        "description": "Write a file inside the workspace, optionally running the test suite afterwards",
        "parameters": {"file_path": "string", "content": "string", "run_test": "boolean?"},
        "risk": "medium",
    },
    {
        "name": "jev_mcp:apply_patch",
        "description": "Change existing files with exact search-and-replace edits, or create new files "
        "(old = \"\"). Each old text must appear exactly once in its file and inside the code you were shown. "
        "All edits apply or none; with check, Python files are compiled and Go files gofmt-checked and built, "
        "and failures are reverted. Example: {\"edits\": [{\"file_path\": \"apps/agent/metrics.go\", "
        "\"old\": \"timeoutSec = 10\", \"new\": \"timeoutSec = ExecDefaultTimeout\"}], \"check\": true}",
        "parameters": {"edits": "list", "check": "boolean?"},
        "risk": "medium",
    },
    {
        "name": "jev_mcp:run_tests",
        "description": "Run an allowlisted test or build command in the workspace "
        "(go test, go vet, pytest, python3 -m pytest/unittest, npm run build, npm test) with only the listed "
        "flags; tests run the workspace's code",
        "parameters": {"test_cmd": "string"},
        # Tests execute workspace code, which earlier judge edits may have changed.
        "risk": "high",
    },
    {
        "name": "jev_mcp:server_remediation",
        "description": "Run a shell command on the hub, or on target_host via the agent's fleet SSH",
        "parameters": {"command": "string", "risk_level": "string", "target_host": "string?"},
        "risk": "high",
    },
    # Typed cleanup actions of ops projects (ops_actions.py): measured before and after, refused when what they
    # would delete grew too much since the proposal. Prefer them to a free command.
    # The risk shown here is the tool's highest; ops_actions.risk() gives one action's (docker_prune by scope).
    *({"name": name, "description": desc, "parameters": params, "risk": ops_actions.risk(name)}
      for name, (params, desc) in ops_actions.ACTIONS.items()),
    {
        "name": artifact_ops.TOOL,
        "description": "Store a Markdown, Mermaid or SVG text as a new version of the thread's artifacts (design "
        "projects). content is the whole file. name is a lowercase file name such as screen-flow.md, flow.mmd or "
        "icon.svg. base_version is the version you built on (artifact@v3) and is required once the thread has "
        "one. The text is checked before it is stored; a person approves the write",
        "parameters": {"name": "string", "content": "string", "base_version": "string?", "title": "string?"},
        "risk": "medium",
    },
]

TEST_COMMAND_PREFIXES: Tuple[Tuple[str, ...], ...] = (
    ("go", "test"),
    ("go", "vet"),
    ("pytest",),
    ("python3", "-m", "pytest"),
    ("python3", "-m", "unittest"),
    ("npm", "run", "build"),
    ("npm", "test"),
)
# Flags each test command may use: name -> whether it takes a value. Anything else is refused, so
# options that run arbitrary programs (go test -exec/-toolexec/-o, pytest -p/-c/--rootdir,
# unittest -t outside the workspace) can never be smuggled in after an allowlisted prefix.
TEST_FLAGS: Dict[Tuple[str, ...], Dict[str, bool]] = {
    ("go", "test"): {"-v": False, "-race": False, "-short": False, "-cover": False, "-failfast": False,
                     "-run": True, "-count": True, "-timeout": True},
    # go vet: no -vettool (runs an arbitrary program) and no -exec/-toolexec; only package args and -v.
    ("go", "vet"): {"-v": False},
    ("pytest",): {"-q": False, "-v": False, "-x": False, "-k": True, "--maxfail": True},
    ("python3", "-m", "pytest"): {"-q": False, "-v": False, "-x": False, "-k": True, "--maxfail": True},
    ("python3", "-m", "unittest"): {"-v": False, "-q": False, "-f": False, "-k": True,
                                     "discover": False, "-s": True, "-p": True},
    ("npm", "run", "build"): {},
    ("npm", "test"): {},
}
# Positional arguments: workspace-relative paths (Go packages may end in /...) or dotted module names.
TEST_PATH_ARG = re.compile(r"^(\./)?[A-Za-z0-9_][A-Za-z0-9_./-]*$|^\./\.\.\.$|^\.$")
TEST_MODULE_ARG = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
TEST_VALUE = re.compile(r"^[A-Za-z0-9_.*/^$|()\[\] :=+-]{1,200}$")
# Time limit of every test run a tool or the publish gate starts (run_tests, run_test, run_default_test).
TEST_TIMEOUT = 600
PROTECTED_BRANCHES = {"main", "master", "develop", "release"}
# Tools that change files or git state in the workspace.
WRITE_TOOLS = {"jev_mcp:execute_code_action", "jev_mcp:apply_patch"}
# Tools that act on servers: only ops projects offer them, and code projects offer every other tool.
SERVER_TOOLS = {"jev_mcp:server_remediation", *ops_actions.ACTIONS}
# Tools of design projects (docs/design/design-project.md). write_artifact stays out of WRITE_TOOLS and
# action_ops.FILE_TOOLS: it writes the artifact store, not a workspace, so it never auto-applies.
DESIGN_TOOLS = {artifact_ops.TOOL}
# The tools each project kind is offered; the kinds partition the catalog. Without a project (tests,
# embedding) every tool is offered.
KIND_TOOLS = {
    "ops": set(SERVER_TOOLS),
    "design": set(DESIGN_TOOLS),
    "code": {t["name"] for t in TOOLS} - SERVER_TOOLS - DESIGN_TOOLS,
}
# Tools removed from the catalog. Old DB rows may still name them; they list fine but can never run.
RETIRED_TOOLS = {"jev_mcp:git_commit_push": "git_commit_push was retired; use publish"}
# The checkout this code runs from; on the hub the agent serves the dashboard and debates from it.
GUARDIAN_ROOT = Path(__file__).resolve().parent.parent.parent
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
}


class ActionValidationError(ValueError):
    """Raised when a proposed action does not match the tool catalog."""


def workspace_root() -> Path:
    configured = os.environ.get("GUARDIAN_WORKSPACE")
    root = Path(configured).expanduser() if configured else Path(__file__).resolve().parent.parent.parent
    return root.resolve()


class MCPClient:
    def __init__(self, workspace: Optional[Path] = None, agent_client: Any = None, project: Any = None):
        """project (project_registry.Project) adds its test commands to the allow-list and names
        the test that execute_code_action runs; without one only the builtin commands apply.

        The project's kind picks the tool set (KIND_TOOLS): an ops project gets the server tools, a design
        project none, a code project every other tool. Ops and design projects have no workspace
        (self.workspace is None). Without a project (tests, embedding) every tool is offered."""
        self.project = project
        self.kind: Optional[str] = getattr(project, "kind", None)
        has_workspace = getattr(project, "has_workspace", True)
        self.workspace: Optional[Path] = (workspace or workspace_root()).resolve() if has_workspace else None
        self._agent_client = agent_client
        self.project_id = getattr(project, "id", None)
        extra = list(getattr(project, "test_commands", None) or [])
        self.test_prefixes: Tuple[Tuple[str, ...], ...] = TEST_COMMAND_PREFIXES + tuple(p for p, _ in extra)
        self.test_flags: Dict[Tuple[str, ...], Dict[str, bool]] = {**TEST_FLAGS, **{p: f for p, f in extra}}
        single = getattr(project, "default_test", None)
        # tests.default is one command or a list; default_test stays the first entry for older callers.
        self.default_tests: List[str] = list(getattr(project, "default_tests", None) or ([single] if single else []))
        self.default_test: Optional[str] = self.default_tests[0] if self.default_tests else None

    def offers(self, name: str) -> bool:
        """Whether this project's kind has the tool (every tool without a project)."""
        if self.kind is None:
            return True
        return name in KIND_TOOLS.get(self.kind, ())

    def list_tools(self) -> List[Dict[str, Any]]:
        """Returns the tool catalog shown to the judge."""
        return [dict(tool) for tool in TOOLS if self.offers(tool["name"])]

    def tool(self, name: str) -> Optional[Dict[str, Any]]:
        return next((t for t in TOOLS if t["name"] == name), None)

    def project_tools(self, allowed: Optional[List[str]]) -> List[str]:
        """The tools a judge may propose in this project: allowed (None: every tool) narrowed to the project's
        kind and, in an ops or design project, to its policy.allowed_actions."""
        names = [t["name"] for t in TOOLS if self.offers(t["name"]) and (allowed is None or t["name"] in allowed)]
        if self.kind in ("ops", "design"):
            policy = set((getattr(self.project, "policy", None) or {}).get("allowed_actions") or [])
            names = [n for n in names if n in policy]
        return names

    def catalog_text(self, allowed: Optional[List[str]] = None) -> str:
        """Plain-text tool catalog for prompts."""
        lines = []
        for t in TOOLS:
            if not self.offers(t["name"]):
                continue
            if allowed is not None and t["name"] not in allowed:
                continue
            params = ", ".join(f"{k}: {v}" for k, v in t["parameters"].items())
            lines.append(f"- {t['name']} ({t['risk']} risk): {self.describe(t)}. Arguments: {{{params}}}")
        return "\n".join(lines)

    def describe(self, tool: Dict[str, Any]) -> str:
        """The tool's description for the judge; run_tests also names this project's prefixes and default test."""
        if tool["name"] != "jev_mcp:run_tests":
            return tool["description"]
        prefixes = ", ".join(" ".join(p) for p in self.test_prefixes)
        text = f"{tool['description']}. Allowed command prefixes in this project: {prefixes}"
        text += (". Allowed flags per command (anything else is refused: no -C, cd, env assignments or shell "
                 f"operators): {self._flags_text()}. Other arguments must be workspace-relative paths or dotted "
                 "module names; commands run from the workspace root (Go packages as ./apps/agent/...)")
        if self.default_test:
            if len(self.default_tests) == 1:
                text += f". The project's default test: {self.default_test}"
            else:
                steps = "; ".join(f"{i}. {c}" for i, c in enumerate(self.default_tests, 1))
                text += f". The project's default test runs these steps in order and stops at the first failure: {steps}"
        return text

    def _flags_text(self, prefix: Optional[Tuple[str, ...]] = None) -> str:
        """Allowed flags of one test command prefix (or of every prefix), e.g. 'go vet: -v'; 'VALUE' marks a value."""
        def one(p: Tuple[str, ...]) -> str:
            flags = self.test_flags.get(p) or {}
            names = [f"{n} VALUE" if takes else n for n, takes in flags.items()]
            return f"{' '.join(p)}: {', '.join(names) if names else 'no flags'}"
        return one(prefix) if prefix is not None else "; ".join(one(p) for p in self.test_prefixes)

    def validate(self, tool_name: str, arguments: Any, allowed: Optional[List[str]] = None) -> Dict[str, Any]:
        """Checks an action against the catalog and the tool's own guardrails. Returns normalized arguments."""
        spec = self.tool(tool_name)
        if spec is None:
            if tool_name in RETIRED_TOOLS:
                raise ActionValidationError(RETIRED_TOOLS[tool_name])
            raise ActionValidationError(f"unknown tool '{tool_name}'")
        if not self.offers(tool_name):
            kind = ("ops projects (server hosts)" if tool_name in SERVER_TOOLS else
                    "design projects (artifacts)" if tool_name in DESIGN_TOOLS else "code projects (repositories)")
            raise ActionValidationError(f"tool '{tool_name}' belongs to {kind}; project {self.project_id} is not one")
        if allowed is not None and tool_name not in allowed:
            raise ActionValidationError(f"tool '{tool_name}' is not allowed for this agent")
        if self.kind in ("ops", "design") and tool_name not in self.project_tools(None):
            raise ActionValidationError(f"tool '{tool_name}' is not in the policy of project {self.project_id}")
        if not isinstance(arguments, dict):
            raise ActionValidationError("arguments must be an object")
        # A key the tool does not take, set to null, says nothing (a model fills fields it half remembers, such as a
        # title_hint: null): it is dropped. Any other unknown argument still refuses the action.
        arguments = {k: v for k, v in arguments.items() if v is not None or k in spec["parameters"]}
        unknown = set(arguments) - set(spec["parameters"])
        if unknown:
            raise ActionValidationError(f"unknown arguments: {sorted(unknown)}")
        for name, type_name in spec["parameters"].items():
            optional = type_name.endswith("?")
            base = type_name.rstrip("?")
            if name not in arguments:
                if optional:
                    continue
                raise ActionValidationError(f"missing argument '{name}'")
            if not TYPE_CHECKS[base](arguments[name]):
                raise ActionValidationError(f"argument '{name}' must be a {base}")

        if tool_name in WRITE_TOOLS and self.workspace == GUARDIAN_ROOT.resolve():
            raise ActionValidationError(
                "the workspace is the running Server Guardian checkout; set GUARDIAN_WORKSPACE to a separate clone"
            )
        if tool_name == "jev_mcp:execute_code_action":
            self._workspace_path(arguments["file_path"])
            if arguments.get("run_test") and not self.default_test:
                raise ActionValidationError(f"run_test needs a default test: {self.no_default_test_message()}")
        elif tool_name == "jev_mcp:apply_patch":
            try:
                edits = patch_ops.normalize_edits(arguments["edits"])
            except patch_ops.PatchError as exc:
                raise ActionValidationError(str(exc)) from exc
            for edit in edits:
                resolved = self._workspace_path(edit["file_path"])
                relative = resolved.relative_to(self.workspace).as_posix()
                # Check the resolved workspace-relative path (also reached through symlinks or an absolute path)
                # and the path as given; an absolute path is not checked as given, so a parent directory outside
                # the workspace cannot make an ordinary file look secret.
                if is_secret(relative) or (not Path(edit["file_path"]).is_absolute() and is_secret(edit["file_path"])):
                    raise ActionValidationError(f"editing secret-looking files is not allowed: {edit['file_path']}")
            return {**arguments, "edits": edits}
        elif tool_name == "jev_mcp:run_tests":
            self._go_module_argv(self._test_argv(arguments["test_cmd"]))
        elif tool_name == artifact_ops.TOOL:
            try:
                report = artifact_ops.check_arguments(arguments, self.project)
            except artifact_ops.ArtifactError as exc:
                raise ActionValidationError(str(exc)) from exc
            if not report.ok:
                raise ActionValidationError("; ".join(report.errors))
        elif tool_name in SERVER_TOOLS:
            if tool_name == "jev_mcp:server_remediation" and not arguments["command"].strip():
                raise ActionValidationError("command is empty")
            if ops_actions.is_catalog(tool_name):
                policy = getattr(self.project, "policy", None) or {}
                try:
                    arguments = ops_actions.validate(tool_name, arguments, policy.get("deny_paths") or (),
                                                     policy.get("deny_commands") or ())
                except ValueError as exc:
                    raise ActionValidationError(str(exc)) from exc
            if self.kind == "ops":
                import ops_targets

                try:
                    target = ops_targets.check_target(self.project, arguments.get("target_host"))
                except ops_targets.TargetScopeError as exc:
                    raise ActionValidationError(str(exc)) from exc
                if tool_name in ops_packages.TOOLS:
                    # Against the host's facts (cached for 15 minutes, else read live): manager present, sudo, protection.
                    facts = ops_actions.host_facts(self._agent(), target, self.project_id or "")
                    protection = self._package_protection(target, facts)
                    try:
                        arguments = ops_packages.check(tool_name, arguments, facts, protection)
                    except ValueError as exc:
                        raise ActionValidationError(str(exc)) from exc
                return {**arguments, "target_host": target}
        return dict(arguments)

    def _package_protection(self, target: str, facts: Optional[Dict[str, Any]]) -> Any:
        """The protected packages of one host (docs/design/fleet-provisioning.md 6.2): the roles of its inventory
        entry, what runs on it (from the facts) and the project's additions, computed at every validation so a role
        added after a proposal protects at the run too. A role the roles file does not define refuses the action."""
        import ops_targets
        import package_roles

        roles = ops_targets.host_roles(self.project, target)
        extra = (getattr(self.project, "policy", None) or {}).get("protected_packages") or ()
        try:
            protection = package_roles.protection(roles, facts, extra)
        except ValueError as exc:
            raise ActionValidationError(f"package roles are not available: {exc}") from exc
        if protection.unknown_roles:
            raise ActionValidationError(
                f"{target} has ops_roles that configs/package-roles.json does not define: "
                f"{', '.join(protection.unknown_roles)}; fix the node's roles first")
        return protection

    def _protection_of(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """The protection of a validated package action's host (None for every other action)."""
        if tool_name not in ops_packages.TOOLS or self.kind != "ops":
            return None
        target = args.get("target_host") or "local"
        return self._package_protection(target, ops_actions.host_facts(self._agent(), target, self.project_id or ""))

    def ops_preview(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """The measurement of a catalog action on its host now ({"bytes", "text", "error"}, see ops_actions)."""
        args = self.validate(tool_name, arguments)
        return ops_actions.measure(self._agent(), args.get("target_host") or "local", tool_name, args,
                                   self.project_id or "", protection=self._protection_of(tool_name, args))

    # --- guardrails --------------------------------------------------------------

    def _workspace_path(self, file_path: str) -> Path:
        candidate = Path(file_path)
        resolved = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ActionValidationError(f"file_path escapes the workspace: {file_path}")
        if ".git" in resolved.relative_to(self.workspace).parts:
            raise ActionValidationError("writing inside .git is not allowed")
        return resolved

    def _test_argv(self, test_cmd: str) -> List[str]:
        try:
            argv = shlex.split(test_cmd)
        except ValueError as exc:
            raise ActionValidationError(f"cannot parse test_cmd: {exc}") from exc
        # Longest prefix first, so a project's "cargo test" wins over a shorter one.
        prefix = next((p for p in sorted(self.test_prefixes, key=len, reverse=True) if tuple(argv[: len(p)]) == p), None)
        if prefix is None:
            raise ActionValidationError(f"test_cmd is not allowlisted: {test_cmd}")
        if any(token in {";", "&&", "||", "|", ">", "<"} for token in argv):
            raise ActionValidationError("test_cmd must be a single command")
        flags = self.test_flags[prefix]
        rest = argv[len(prefix):]
        i = 0
        while i < len(rest):
            token = rest[i]
            name, has_inline, inline = token.partition("=") if token.startswith("-") else (token, "", "")
            if name in flags:
                if flags[name]:
                    value = inline if has_inline else (rest[i + 1] if i + 1 < len(rest) else None)
                    if value is None or not TEST_VALUE.match(value) or ".." in value.split("/"):
                        raise ActionValidationError(f"invalid value for {name} in test_cmd")
                    if name in ("-s",) and (value.startswith("/") or ".." in value.split("/")):
                        raise ActionValidationError("-s must be a workspace-relative directory")
                    i += 1 if has_inline else 2
                    continue
                if has_inline:
                    raise ActionValidationError(f"{name} takes no value")
                i += 1
                continue
            if token.startswith("-"):
                raise ActionValidationError(f"flag {name} is not allowed in test_cmd (allowed: {self._flags_text(prefix)})")
            if not (TEST_PATH_ARG.match(token) or TEST_MODULE_ARG.match(token)) or ".." in token.split("/"):
                raise ActionValidationError(f"argument {token!r} is not a workspace path or module name")
            if token.startswith("/"):
                raise ActionValidationError("absolute paths are not allowed in test_cmd")
            i += 1
        return argv

    @staticmethod
    def _check_branch(branch: str) -> None:
        if not BRANCH_PATTERN.fullmatch(branch) or ".." in branch or branch.endswith((".lock", "/")):
            raise ActionValidationError(f"invalid branch name: {branch}")
        if branch in PROTECTED_BRANCHES:
            raise ActionValidationError(f"refusing to commit to protected branch '{branch}'")

    # --- execution ---------------------------------------------------------------

    # @MX:WARN: [AUTO] Executes file writes, git commits and shell commands.
    # @MX:REASON: Callers must run validate() and obtain human approval first (action_ops.run_action does both).
    def call_tool(self, tool_name: str, arguments: Dict[str, Any],
                  siblings: Optional[Dict[str, str]] = None, action_id: Optional[int] = None,
                  thread_id: Optional[str] = None, expected_sha: Optional[str] = None) -> Dict[str, Any]:
        """Executes a validated tool action and returns a result dict with a status.

        expected_sha: the script_sha the action was approved with; a command set or script that renders to anything
        else now is not run.

        thread_id names the debate thread that proposed the action; write_artifact stores into its versions.

        siblings (path -> content hash left by the last executed sibling action of the same decision) lets file
        tools build on uncommitted changes those siblings made; see patch_ops.uncommitted_conflicts.
        """
        try:
            args = self.validate(tool_name, arguments)
        except ActionValidationError as exc:
            return {"status": "FAILED", "error": str(exc)}

        if tool_name == "jev_mcp:execute_code_action":
            path = self._workspace_path(args["file_path"])
            rel = str(path.relative_to(self.workspace))
            conflicts = patch_ops.uncommitted_conflicts(self.workspace, [rel], siblings)
            if conflicts:
                return {"status": "FAILED", "error": "uncommitted changes in the files to edit: " + "; ".join(conflicts)}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            result: Dict[str, Any] = {"status": "SUCCESS", "message": f"wrote {rel}",
                                      "file_hashes": patch_ops.file_hashes(self.workspace, [rel])}
            if args.get("run_test"):
                try:
                    result["tests"] = self.run_default_test()
                except ActionValidationError as exc:  # the file is written; the test could not even start
                    result["tests"] = {"exit_code": -1, "stdout": "", "stderr": str(exc)}
                result["status"] = "SUCCESS" if result["tests"]["exit_code"] == 0 else "FAILED"
            return result

        if tool_name == "jev_mcp:apply_patch":
            paths = sorted({e["file_path"] for e in args["edits"]})
            conflicts = patch_ops.uncommitted_conflicts(self.workspace, paths, siblings)
            if conflicts:
                return {"status": "FAILED", "error": "uncommitted changes in the files to edit: " + "; ".join(conflicts)}
            try:
                return patch_ops.apply(self.workspace, args["edits"], bool(args.get("check")),
                                       lambda p: self._workspace_path(p))
            except patch_ops.PatchError as exc:
                return {"status": "FAILED", "error": str(exc), "reverted": False}

        if tool_name == "jev_mcp:run_tests":
            argv, cwd = self._go_module_argv(self._test_argv(args["test_cmd"]))
            res = self._run(argv, timeout=TEST_TIMEOUT, cwd=cwd)
            return {"status": "SUCCESS" if res["exit_code"] == 0 else "FAILED", **res}

        if tool_name == artifact_ops.TOOL:
            return self._write_artifact(args, thread_id)

        if ops_actions.is_catalog(tool_name):
            try:
                protection = self._protection_of(tool_name, args)
            except ActionValidationError as exc:
                return {"status": "FAILED", "error": str(exc)}
            return ops_actions.execute(self._agent(), args.get("target_host") or "local", tool_name, args,
                                       self.project_id or "", action_id, protection, expected_sha)

        if tool_name == "jev_mcp:server_remediation":
            target = args.get("target_host")
            if target and target not in {"local", "localhost"}:
                labels = {"source": "action", "op": tool_name.split(":")[-1], "project": self.project_id or "",
                          **({"action": str(action_id)} if action_id is not None else {})}
                res = self._agent().execute_remote_ssh(target, args["command"], timeout_sec=60, labels=labels)
                exit_code = res.get("exit_code", 1)
                return {"status": "SUCCESS" if exit_code == 0 else "FAILED", "target_host": target, **res}
            if self.kind == "ops":
                # Through the hub agent, so the run is in its audit log like every other server command.
                labels = {"source": "action", "op": tool_name.split(":")[-1], "project": self.project_id or "",
                          **({"action": str(action_id)} if action_id is not None else {})}
                res = self._agent().execute_command(args["command"], timeout_sec=120, labels=labels)
                exit_code = res.get("exit_code", 1)
                return {"status": "SUCCESS" if exit_code == 0 else "FAILED", "target_host": "local", **res}
            res = self._run(["bash", "-c", args["command"]], timeout=120, cwd=self.workspace or Path.home())
            return {"status": "SUCCESS" if res["exit_code"] == 0 else "FAILED", "target_host": "local", **res}

        return {"status": "FAILED", "error": f"unknown tool '{tool_name}'"}

    def _write_artifact(self, args: Dict[str, Any], thread_id: Optional[str]) -> Dict[str, Any]:
        if not self.project_id or not thread_id:
            return {"status": "FAILED", "error": "write_artifact needs a project and a debate thread"}
        try:
            store = artifact_ops.ArtifactStore(self.project_id, thread_id)
            manifest = store.write(args["name"], args["content"], project=self.project,
                                   base_version=args.get("base_version"), title=args.get("title") or "",
                                   agent="judge")
        except artifact_ops.StaleBase as exc:
            return {"status": "STALE", "error": str(exc)}
        except (artifact_ops.ArtifactError, OSError) as exc:
            return {"status": "FAILED", "error": str(exc)}
        version = artifact_ops.format_version(manifest["version"])
        return {"status": "SUCCESS", "message": f"stored {args['name']} as {version}", "version": version,
                "artifact": args["name"], "warnings": manifest.get("warnings", [])}

    def no_default_test_message(self) -> str:
        return f"set tests.default for project {self.project_id or 'server-guardian'}"

    def default_test_argvs(self) -> List[Tuple[List[str], Path]]:
        """(argv, cwd) of every step of the project's tests.default after the allow-list check and Go module
        rewriting, in run order.

        Raises ActionValidationError when the project names no default test or a step is not an allowed command.
        There is deliberately no fallback test: an unconfigured project has nothing to gate on.
        """
        if not self.default_tests:
            raise ActionValidationError(self.no_default_test_message())
        return [self._go_module_argv(self._test_argv(cmd)) for cmd in self.default_tests]

    def default_test_argv(self) -> Tuple[List[str], Path]:
        """(argv, cwd) of the first step of tests.default (see default_test_argvs)."""
        return self.default_test_argvs()[0]

    def run_default_test(self, timeout: int = TEST_TIMEOUT) -> Dict[str, Any]:
        """Runs the project's tests.default steps in order, the way run_tests runs a command: no shell, each
        from the module that holds its Go packages, with the tool environment. Used by
        execute_code_action(run_test) and the publish gate, so both give the same verdict.

        `timeout` is the budget of the whole sequence; each step gets what is left. Stops at the first failing
        step. Returns {"cmd", "exit_code", "stdout", "stderr", "steps"}: on failure cmd/stdout/stderr are the
        failing step's and stderr starts with "step i/n failed: ..."; on success cmd joins all steps with " && ".
        """
        plan = self.default_test_argvs()
        total = len(plan)
        deadline = time.monotonic() + timeout
        steps: List[Dict[str, Any]] = []
        res: Dict[str, Any] = {"stdout": "", "stderr": ""}
        for index, (argv, cwd) in enumerate(plan, 1):
            cmd = " ".join(argv)
            remaining = math.ceil(deadline - time.monotonic())
            if remaining <= 0:
                res = {"exit_code": -1, "stdout": "", "stderr": f"the {timeout} s test budget ran out before this step"}
            else:
                res = self._run(argv, timeout=remaining, cwd=cwd)
            steps.append({"step": index, "cmd": cmd, "exit_code": res["exit_code"]})
            if res["exit_code"] != 0:
                note = f"step {index}/{total} failed: {cmd} (exit {res['exit_code']})"
                return {"cmd": cmd, "exit_code": res["exit_code"], "stdout": res["stdout"],
                        "stderr": f"{note}\n{res['stderr']}".rstrip(), "steps": steps}
        return {"cmd": " && ".join(st["cmd"] for st in steps), "exit_code": 0, "stdout": res["stdout"],
                "stderr": res["stderr"], "steps": steps}

    def _go_module_argv(self, argv: List[str]) -> Tuple[List[str], Path]:
        """Runs `go ...` from the module that holds its package arguments.

        `go test ./apps/agent/...` fails at a repository root without go.mod; it becomes
        `go test ./...` in apps/agent. Other commands run from the workspace root unchanged.
        """
        if not argv or argv[0] != "go" or (self.workspace / "go.mod").is_file():
            return argv, self.workspace
        packages = [a for a in argv[2:] if a.startswith("./")]
        if packages == ["./..."]:
            # "go test ./..." at a root without go.mod means the repository's only Go module.
            modules = self._go_modules()
            if len(modules) == 1:
                return argv, modules[0]
            if len(modules) > 1:
                names = ", ".join(str(m.relative_to(self.workspace)) for m in modules)
                raise ActionValidationError(f"several Go modules ({names}); name the package path, e.g. ./apps/agent/...")
        if not packages:
            return argv, self.workspace
        module = None
        for pkg in packages:
            directory = (self.workspace / pkg.rstrip("/.").rstrip("/")).resolve()
            while directory != self.workspace and self.workspace in directory.parents:
                if (directory / "go.mod").is_file():
                    break
                directory = directory.parent
            if directory == self.workspace or self.workspace not in directory.parents:
                return argv, self.workspace
            if module not in (None, directory):
                raise ActionValidationError("go packages from more than one module in one test_cmd")
            module = directory
        rewritten = [argv[0], argv[1]]
        for arg in argv[2:]:
            if arg.startswith("./"):
                rel = os.path.relpath(self.workspace / arg.rstrip("/"), module).replace(os.sep, "/")
                arg = "." if rel == "." else "./" + rel
            rewritten.append(arg)
        return rewritten, module

    def _go_modules(self) -> List[Path]:
        skip = {".git", "node_modules", "vendor"}
        return sorted(p.parent for p in self.workspace.rglob("go.mod") if not skip & set(p.relative_to(self.workspace).parts))

    @staticmethod
    def tool_environ() -> Dict[str, str]:
        """Environment of every child a tool action starts (also used by publish_ops for its git calls)."""
        # The hub agent's PATH lacks the user-level Go and CLI installs; never fetch other toolchains.
        # Tool actions run model-influenced code: no tokens or credentials in their environment.
        home = Path.home()
        return {**tool_env(os.environ), "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly",
                "PATH": os.pathsep.join([str(home / ".local" / "go" / "bin"), str(home / ".local" / "bin"),
                                         os.environ.get("PATH", "")])}

    def _run(self, argv: List[str], timeout: int, cwd: Optional[Path] = None) -> Dict[str, Any]:
        env = self.tool_environ()
        try:
            proc = subprocess.run(argv, cwd=cwd or self.workspace, capture_output=True, text=True, timeout=timeout,
                                  env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"exit_code": -1, "stdout": "", "stderr": str(exc)}
        return {"exit_code": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}

    def _agent(self) -> Any:
        if self._agent_client is None:
            from agent_client import NodeAgentClient

            self._agent_client = NodeAgentClient(os.environ.get("AGENT_URL", "http://127.0.0.1:9870"))
        return self._agent_client


def parse_judge_block(judge_text: str) -> Dict[str, Any]:
    """Returns the last ```json {...}``` object in the judge's reply that has actions, a plan or constraints."""
    # An optional ```findings block quotes code, which may contain fences or verdict-like text: never read inside it.
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", strip_findings(judge_text), flags=re.DOTALL)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and ("actions" in data or "plan" in data or "constraints" in data):
            return data
    return {}


REQUIREMENT_ID = re.compile(r"^R\d{1,2}$")


def clean_refs(value: Any) -> List[str]:
    """Requirement ids (R1, R12) of a refs value: other forms dropped, duplicates removed, order kept."""
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, str) and REQUIREMENT_ID.match(item.strip()) and item.strip() not in out:
            out.append(item.strip())
    return out


def _raw_actions(judge_text: str) -> List[Dict[str, Any]]:
    actions = parse_judge_block(judge_text).get("actions")
    return [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []


def parse_actions(judge_text: str) -> List[Dict[str, Any]]:
    """The judge's proposed actions. Returns [] when absent or malformed.

    The traceability key `refs` is not part of an action: it is dropped here (see parse_action_refs), so it is
    never validated, stored as tool input or sent to jev.
    """
    return [{k: v for k, v in a.items() if k != "refs"} for a in _raw_actions(judge_text)]


def parse_action_refs(judge_text: str) -> List[List[str]]:
    """Requirement ids each action implements; index-aligned with parse_actions."""
    return [clean_refs(a.get("refs")) for a in _raw_actions(judge_text)]


def _items(judge_text: str, key: str, limit: int) -> List[Tuple[str, List[str]]]:
    """(text, refs) of a constraints/checks list whose items are strings or {"text", "refs"} objects."""
    items = parse_judge_block(judge_text).get(key)
    if not isinstance(items, list):
        return []
    found: List[Tuple[str, List[str]]] = []
    for item in items:
        refs: List[str] = []
        if isinstance(item, dict):
            refs, item = clean_refs(item.get("refs")), item.get("text")
        if isinstance(item, str) and item.strip():
            found.append((" ".join(item.split())[:300], refs))
    return found[:limit]


def _string_list(judge_text: str, key: str, limit: int) -> List[str]:
    return [text for text, _ in _items(judge_text, key, limit)]


def parse_constraints(judge_text: str, limit: int = 10) -> List[str]:
    """Decision requirements: what the approved decision itself must state (jev verifies these)."""
    return _string_list(judge_text, "constraints", limit)


def parse_constraint_refs(judge_text: str, limit: int = 10) -> List[List[str]]:
    """Requirement ids each constraint enforces; index-aligned with parse_constraints."""
    return [refs for _, refs in _items(judge_text, "constraints", limit)]


def parse_checks(judge_text: str, limit: int = 10) -> List[str]:
    """Implementation checks: what to confirm in the code after the actions are applied (not sent to jev)."""
    return _string_list(judge_text, "checks", limit)


def parse_check_refs(judge_text: str, limit: int = 10) -> List[List[str]]:
    """Requirement ids each check verifies; index-aligned with parse_checks."""
    return [refs for _, refs in _items(judge_text, "checks", limit)]


def _keep_paragraphs(text: str) -> str:
    """Whitespace inside each line collapsed, line breaks kept, runs of blank lines reduced to one."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: List[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def parse_decision(judge_text: str, limit: int = 3000) -> str:
    """The judge's final approach in its own words, including any changes it made to the proposal.

    Paragraphs and list lines the judge wrote are kept: the dashboard renders the decision as Markdown
    and publish turns its paragraphs into the commit body.
    """
    decision = parse_judge_block(judge_text).get("decision")
    return _keep_paragraphs(decision)[:limit].rstrip() if isinstance(decision, str) else ""


def parse_rationale(judge_text: str, limit: int = 2000, keep_paragraphs: bool = False) -> str:
    """Why the judge chose its approach (evidence, rejected alternatives, accepted risks); sent to jev.

    keep_paragraphs keeps the judge's line breaks (the dashboard renders the stored rationale as Markdown).
    """
    rationale = parse_judge_block(judge_text).get("rationale")
    if not isinstance(rationale, str):
        return ""
    if keep_paragraphs:
        return _keep_paragraphs(rationale)[:limit].rstrip()
    return " ".join(rationale.split())[:limit]

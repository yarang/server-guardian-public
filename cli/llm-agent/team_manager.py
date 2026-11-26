"""
Debate team lineups for Server Guardian.

A team is declarative: it names which persona fills each debate role, which provider CLIs each
member may use, and what the judge may propose. The debate engine still calls every member as an
individual agent, so a team never adds hidden LLM calls of its own.

Builtin team files live in configs/teams/<id>.json; teams saved from the dashboard go to
GUARDIAN_TEAMS_DIR (default <GUARDIAN_DATA_DIR>/teams) and override builtin ones by id:

    {
      "id": "security-review",
      "name": "Security review",
      "expert": {"persona": "domain_architect", "providers": ["claude"]},
      "critic": [{"persona": "critic_redteam", "providers": ["codex"]},
                 {"persona": "devops_specialist", "providers": ["agy"]}],
      "judge":  {"persona": "jev_mcp_decision", "providers": ["claude"]},
      "max_rounds": 3,
      "allowed_tools": ["jev_mcp:run_tests"]
    }

"host" is optional: a model-worker host (remote_agents.py) the member thinks on, an inventory id or a label
(worker:any, worker:other); without it the member runs on the hub.
"code_tools" is optional: "mcp" lets a claude, codex or opencode member call read-only repo tools (needs GUARDIAN_REPO_MCP=1 on the host).
On a member with a "host" the tools read a snapshot of the commit on the worker: repo_list, repo_grep, repo_read,
repo_outline and repo_map, without the symbol index (repo_defs, repo_refs, repo_callers, repo_callees).
Only the critic may be a panel (a list). The expert and the judge are always one agent, so the
proposal and the approval each have a single accountable author.

"flow" is optional and picks the middle of a round (docs/design/team-flows.md); without it the team is a "debate":
the expert (and proposers) draft, the critic panel attacks, the judge decides. {"flow": {"kind": "panel"}} has the
expert and 1-2 proposers answer blind and the judge decide between them (no critic); {"flow": {"kind": "verify"}}
has the expert write alone and the critics check the result against a checklist. {"flow": {"kind": "pipeline",
"stages": [member, ...]}} runs 2-4 members in order, each one rewriting or extending the previous result; the critic
is optional and the expert is the lead who consolidates after a rejection. {"flow": {"kind": "versus", "exchanges": 1,
"sides": [member + "stance", member + "stance"]}} has two advocates defend the stances written in the team file, answer
each other once or twice, and the judge decide between them (no critic, no proposers, one round). The judge is in every
flow.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

import character
from llm_runner import KNOWN_PROVIDERS
from mcp_client import RETIRED_TOOLS, TOOLS
from persona_manager import PersonaManager
from remote_agents import host_syntax_error, inventory_host_error
from role_cards import home_seat

EXPERT_ROLE = "Domain Expert"
CRITIC_ROLE = "Critic Agent"
JUDGE_ROLE = "JEV-MCP Judge"

CODE_TOOLS = ("mcp",)  # allowed values of a member's optional "code_tools"
DEFAULT_TEAM_ID = "default"
MAX_CRITICS = 4
MAX_PROPOSERS = 2  # extra drafters next to the lead expert (multi-perspective.md)
MAX_ROUNDS_LIMIT = 10
# Flow kinds the engine runs (team-flows.md). A team without "flow" is a "debate".
FLOW_DEBATE, FLOW_PANEL, FLOW_VERIFY, FLOW_PIPELINE, FLOW_VERSUS = "debate", "panel", "verify", "pipeline", "versus"
FLOW_KINDS = (FLOW_DEBATE, FLOW_PANEL, FLOW_VERIFY, FLOW_PIPELINE, FLOW_VERSUS)
MIN_STAGES, MAX_STAGES = 2, 4  # members of a pipeline flow (each one is an LLM call in round 1)
SIDES = 2  # advocates of a versus flow
MAX_EXCHANGES = 2  # answer rounds of a versus flow; each one is two LLM calls
MAX_STANCE_CHARS = 400
SIDE_LABELS = ("A", "B")
TEAM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Optional list metadata of a team file (not part of the lineup, so snapshots do not carry it).
TEAM_CATEGORIES = ("development", "operations", "design-docs", "general")


class TeamValidationError(ValueError):
    """Raised when a team definition is incomplete or refers to unknown personas, providers or tools."""


@dataclass
class Member:
    """One agent of a lineup: a persona plus the provider CLIs it may use, in order."""

    persona: str
    providers: List[str] = field(default_factory=list)
    # "mcp" gives a claude, codex or opencode member read-only repo_* MCP tools (repo_mcp.py); None keeps engine-mediated repo blocks.
    code_tools: Optional[str] = None
    # Where the member thinks: None = the hub; a placement label (remote_agents.LABELS) or an inventory host id of an
    # enabled model worker. With code_tools "mcp" the worker reads a snapshot of the commit (five tools, no symbol index).
    host: Optional[str] = None
    # The position a versus advocate defends, written by the team author (never by a model); None for every other seat.
    stance: Optional[str] = None
    # The seat id of a flow (flow_graph.py) this member was compiled from; None for a member of a team file.
    seat: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Snapshot form: code_tools, host, stance and seat appear only when set, so older snapshots keep their shape."""
        data = asdict(self)
        for optional in ("code_tools", "host", "stance", "seat"):
            if data[optional] is None:
                del data[optional]
        return data


@dataclass
class Lineup:
    """Resolved team: who fills each debate role."""

    id: str
    expert: Member
    critics: List[Member]
    judge: Member
    name: str = ""
    max_rounds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    # Default code context: {"paths": [glob...], "ref": "HEAD"} (code_context).
    context: Optional[Dict[str, Any]] = None
    # Extra drafters: they write a proposal in round 1 next to the lead expert, blind to each other.
    proposers: List[Member] = field(default_factory=list)
    # The middle of a round (FLOW_KINDS); "debate" is the shape every team had before flows existed.
    flow_kind: str = FLOW_DEBATE
    # The members of a pipeline flow, in order (empty for every other flow).
    stages: List[Member] = field(default_factory=list)
    # The advocates of a versus flow (exactly two, side A first) and how often they answer each other.
    sides: List[Member] = field(default_factory=list)
    exchanges: int = 1
    # Set by the engine when the run was started from a flow template (flow_templates.apply_to_lineup); not a team field.
    template: Optional[Dict[str, Any]] = None
    # Set by the engine when the run was started from a flow (flow_graph.compile_flow): its id, hash, parameter values,
    # the characters seated and, for shapes the five kinds do not name, the custom steps; not a team field.
    flow_graph: Optional[Dict[str, Any]] = None

    def stage_key(self, index: int) -> str:
        """Session key of a pipeline stage: by position, so one persona in two stages keeps two sessions."""
        return f"{EXPERT_ROLE}:stage{index}"

    def side_key(self, index: int) -> str:
        """Session key of a versus advocate: by position, so one persona on both sides keeps two sessions."""
        return f"{EXPERT_ROLE}:side{index}"

    def session_key(self, role: str, member: Member) -> str:
        """Key for the member's persisted CLI session.

        A single critic keeps the plain role name, so threads started before panels existed
        resume their sessions. Panel members get the persona id appended.
        """
        if role == CRITIC_ROLE and len(self.critics) > 1:
            return f"{role}:{member.persona}"
        if role == EXPERT_ROLE and any(member is p for p in self.proposers):
            return f"{role}:{member.persona}"  # the lead keeps the plain role name
        return role

    def snapshot(self) -> Dict[str, Any]:
        """Plain dict stored with the thread, so a decision can be traced to its lineup."""
        data = {
            "id": self.id,
            "name": self.name,
            "expert": self.expert.to_dict(),
            "critic": [m.to_dict() for m in self.critics],
            "judge": self.judge.to_dict(),
            "max_rounds": self.max_rounds,
            "allowed_tools": self.allowed_tools,
            "context": self.context,
        }
        if self.proposers:  # absent when empty, so snapshots of older teams keep their shape
            data["proposers"] = [m.to_dict() for m in self.proposers]
        if self.flow_kind != FLOW_DEBATE:  # likewise: a debate team's snapshot has no "flow"
            data["flow"] = {"kind": self.flow_kind}
            if self.stages:
                data["flow"]["stages"] = [m.to_dict() for m in self.stages]
            if self.sides:
                data["flow"]["sides"] = [m.to_dict() for m in self.sides]
                data["flow"]["exchanges"] = self.exchanges
        if self.template:  # absent unless a template started the run, so older snapshots keep their shape
            data["template"] = self.template
        if self.flow_graph:  # likewise: absent unless a flow started the run
            data["flow_graph"] = self.flow_graph
        return data


def default_lineup() -> Lineup:
    """The lineup debates used before teams existed."""
    return Lineup(
        id=DEFAULT_TEAM_ID,
        name="Default lineup",
        expert=Member("domain_architect"),
        critics=[Member("critic_redteam")],
        judge=Member("jev_mcp_decision"),
    )


def _member(raw: Any, where: str) -> Member:
    if isinstance(raw, str):
        return Member(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("persona"), str) or not raw["persona"]:
        raise TeamValidationError(f"{where}: expected {{\"persona\": \"<id>\", \"providers\": [...]}}")
    providers = raw.get("providers", [])
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        raise TeamValidationError(f"{where}.providers: expected a list of provider names")
    code_tools = raw.get("code_tools")
    if code_tools is not None and code_tools not in CODE_TOOLS:
        raise TeamValidationError(f"{where}.code_tools: expected null or one of {list(CODE_TOOLS)}")
    host = raw.get("host")
    if host is not None:
        problem = host_syntax_error(host)
        if problem:
            raise TeamValidationError(f"{where}.host: {problem}")
    seat = raw.get("seat")
    if seat is not None and not isinstance(seat, str):
        raise TeamValidationError(f"{where}.seat: expected the seat id of a flow")
    return Member(raw["persona"], list(providers), code_tools, host, seat=seat)


def _side(raw: Any, where: str) -> Member:
    """A versus advocate: a member plus the stance it defends (a sentence or two, at most MAX_STANCE_CHARS)."""
    member = _member(raw, where)
    stance = raw.get("stance") if isinstance(raw, dict) else None
    if not isinstance(stance, str) or not stance.strip():
        raise TeamValidationError(f"{where}.stance: expected the position this side defends (a short text)")
    if len(stance) > MAX_STANCE_CHARS:
        raise TeamValidationError(f"{where}.stance: at most {MAX_STANCE_CHARS} characters (got {len(stance)})")
    member.stance = stance.strip()
    return member


class FlowSpec(NamedTuple):
    """What a team's "flow" object adds to a lineup."""

    kind: str = FLOW_DEBATE
    stages: List[Member] = []
    sides: List[Member] = []
    exchanges: int = 1


def _flow(raw: Any) -> FlowSpec:
    """The parsed "flow" object of a team; absent means a debate."""
    if raw is None:
        return FlowSpec()
    if not isinstance(raw, dict):
        raise TeamValidationError(f'flow: expected {{"kind": "<{"|".join(FLOW_KINDS)}>"}}')
    kind = raw.get("kind", FLOW_DEBATE)
    if kind not in FLOW_KINDS:
        raise TeamValidationError(f"flow.kind: unknown flow {kind!r}; known: {list(FLOW_KINDS)}")
    own = {FLOW_PIPELINE: ("stages",), FLOW_VERSUS: ("sides", "exchanges")}.get(kind, ())
    extra = sorted(set(raw) - {"kind", *own})
    if extra:
        raise TeamValidationError(f"flow: unknown field(s) {extra} for a {kind} flow")
    if kind == FLOW_PIPELINE:
        stages_raw = raw.get("stages")
        if not isinstance(stages_raw, list):
            raise TeamValidationError("flow.stages: expected a list of members")
        return FlowSpec(kind, stages=[_member(m, f"flow.stages[{i}]") for i, m in enumerate(stages_raw)])
    if kind == FLOW_VERSUS:
        sides_raw = raw.get("sides")
        if not isinstance(sides_raw, list):
            raise TeamValidationError("flow.sides: expected a list of two members, each with a stance")
        exchanges = raw.get("exchanges", 1)
        if not isinstance(exchanges, int) or isinstance(exchanges, bool) or not 1 <= exchanges <= MAX_EXCHANGES:
            raise TeamValidationError(f"flow.exchanges: expected an integer from 1 to {MAX_EXCHANGES}")
        return FlowSpec(kind, sides=[_side(m, f"flow.sides[{i}]") for i, m in enumerate(sides_raw)],
                        exchanges=exchanges)
    return FlowSpec(kind)


def parse_lineup(data: Dict[str, Any]) -> Lineup:
    """Builds a lineup from a team dict without checking personas or providers."""
    if not isinstance(data, dict):
        raise TeamValidationError("team must be a JSON object")
    team_id = data.get("id")
    if not isinstance(team_id, str) or not TEAM_ID_PATTERN.fullmatch(team_id):
        raise TeamValidationError("id: use 1-64 lowercase letters, digits, '-' or '_'")
    flow = _flow(data.get("flow"))
    flow_kind = flow.kind
    # A panel and a versus flow have no critic seat and a pipeline's is optional: "critic" may be left out (or empty).
    for role in ("expert", "critic", "judge"):
        if role not in data and not (role == "critic" and flow_kind in (FLOW_PANEL, FLOW_PIPELINE, FLOW_VERSUS)):
            raise TeamValidationError(f"{role}: missing")
    critic_raw = data.get("critic", [])
    critic_raw = critic_raw if isinstance(critic_raw, list) else [critic_raw]
    critics = [_member(c, f"critic[{i}]") for i, c in enumerate(critic_raw)]
    proposers_raw = data.get("proposers") or []
    if not isinstance(proposers_raw, list):
        raise TeamValidationError("proposers: expected a list of members")
    proposers = [_member(m, f"proposers[{i}]") for i, m in enumerate(proposers_raw)]

    max_rounds = data.get("max_rounds")
    if max_rounds is not None and (not isinstance(max_rounds, int) or isinstance(max_rounds, bool)):
        raise TeamValidationError("max_rounds: expected an integer")
    allowed_tools = data.get("allowed_tools")
    if allowed_tools is not None and (
        not isinstance(allowed_tools, list) or not all(isinstance(t, str) for t in allowed_tools)
    ):
        raise TeamValidationError("allowed_tools: expected a list of tool names")

    context = data.get("context")
    if context is not None:
        from code_context import ContextError, validate_patterns, validate_ref

        if not isinstance(context, dict):
            raise TeamValidationError('context: expected {"paths": [glob...], "ref": "HEAD"}')
        try:
            context = {"paths": validate_patterns(context.get("paths", [])),
                       "ref": validate_ref(str(context.get("ref") or "HEAD"))}
        except ContextError as exc:
            raise TeamValidationError(f"context: {exc}") from exc
        if not context["paths"]:
            context = None

    return Lineup(
        id=team_id,
        name=str(data.get("name", "")),
        expert=_member(data["expert"], "expert"),
        critics=critics,
        judge=_member(data["judge"], "judge"),
        max_rounds=max_rounds,
        allowed_tools=allowed_tools,
        context=context,
        proposers=proposers,
        flow_kind=flow_kind,
        stages=list(flow.stages),
        sides=list(flow.sides),
        exchanges=flow.exchanges,
    )


def _card_seat(where: str) -> str:
    """The role card seat of a lineup position: drafters, stages and sides write like the expert."""
    if where == "judge":
        return "judge"
    return "critic" if where.startswith("critic") else "expert"


def validate_lineup(lineup: Lineup, persona_mgr: Optional[PersonaManager] = None) -> List[str]:
    """Checks references and limits. Raises on errors; returns warnings worth showing."""
    persona_mgr = persona_mgr or PersonaManager()
    errors: List[str] = []
    warnings: List[str] = []

    flow = lineup.flow_kind
    if flow == FLOW_PANEL:
        if lineup.critics:
            errors.append("critic: a panel flow has no critic (the judge decides between the blind answers)")
        if not lineup.proposers:
            errors.append("proposers: a panel flow needs 1-%d proposers next to the expert" % MAX_PROPOSERS)
        if lineup.max_rounds is not None and lineup.max_rounds != 1:
            errors.append("max_rounds: a panel flow runs one round (nothing is critiqued, so a second round would "
                          "only repeat the answers)")
    elif flow == FLOW_PIPELINE:
        if not MIN_STAGES <= len(lineup.stages) <= MAX_STAGES:
            errors.append(f"flow.stages: a pipeline flow needs {MIN_STAGES}-{MAX_STAGES} stages "
                          f"(got {len(lineup.stages)}; each one is an LLM call)")
        if lineup.proposers:
            errors.append("proposers: a pipeline flow has its stages; use flow.stages instead")
        if not lineup.critics and lineup.max_rounds is not None and lineup.max_rounds != 1:
            errors.append("max_rounds: a pipeline flow without a critic runs one round (nothing is critiqued, so a "
                          "second round would only repeat the stages)")
    elif flow == FLOW_VERSUS:
        if len(lineup.sides) != SIDES:
            errors.append(f"flow.sides: a versus flow needs exactly {SIDES} sides (got {len(lineup.sides)})")
        if lineup.critics:
            errors.append("critic: a versus flow has no critic (the sides answer each other and the judge decides)")
        if lineup.proposers:
            errors.append("proposers: a versus flow has its sides; use flow.sides instead")
        if lineup.max_rounds is not None and lineup.max_rounds != 1:
            errors.append("max_rounds: a versus flow runs one round (a rejection would replay the same two cases; "
                          "raise flow.exchanges for a longer exchange)")
    elif not lineup.critics:
        errors.append("critic: at least one critic is required")
    if flow == FLOW_VERIFY and lineup.proposers:
        errors.append("proposers: a verify flow has one author, the expert")
    if len(lineup.critics) > MAX_CRITICS:
        errors.append(f"critic: at most {MAX_CRITICS} critics (each one is an LLM call per round)")
    critic_ids = [m.persona for m in lineup.critics]
    if len(set(critic_ids)) != len(critic_ids):
        errors.append("critic: each panel member needs a different persona (it keys the member's session)")

    if len(lineup.proposers) > MAX_PROPOSERS:
        errors.append(f"proposers: at most {MAX_PROPOSERS} extra proposers (each one is an LLM call in round 1)")
    drafter_ids = [lineup.expert.persona, *(m.persona for m in lineup.proposers)]
    if len(set(drafter_ids)) != len(drafter_ids):
        errors.append("proposers: the expert and each proposer need a different persona (it keys the session)")

    members = [("expert", lineup.expert, "expert"),
               *((f"proposers[{i}]", m, "proposer") for i, m in enumerate(lineup.proposers)),
               *((f"flow.stages[{i}]", m, "stage") for i, m in enumerate(lineup.stages)),
               *((f"flow.sides[{i}]", m, "side") for i, m in enumerate(lineup.sides)),
               *((f"critic[{i}]", m, "verifier" if flow == FLOW_VERIFY else "critic")
                 for i, m in enumerate(lineup.critics)),
               ("judge", lineup.judge, "judge")]
    for where, member, seat in members:
        persona = persona_mgr.get_persona(member.persona)
        if persona is None:
            errors.append(f"{where}: unknown persona {member.persona!r}")
        else:
            allowed = character.persona_seats(persona)
            if allowed is not None and seat not in allowed:
                # a character (format 2) sits only in the seats it names; a format 1 persona has no seat list
                errors.append(f"{where}: persona {member.persona!r} may sit in {', '.join(allowed) or 'none'}, "
                              f"not {seat}")
            elif home_seat(persona) not in (None, _card_seat(where)):
                # the card follows the home seat (role_cards.py): elsewhere the persona keeps its role and its card
                warnings.append(f"{where}: persona {member.persona!r} has role_card {home_seat(persona)}; it keeps "
                                "that seat's role and card here")
        unknown = [p for p in member.providers if p not in KNOWN_PROVIDERS]
        if unknown:
            errors.append(f"{where}: unknown provider(s) {unknown}; known: {KNOWN_PROVIDERS}")
        if member.host:
            problem = inventory_host_error(member.host)
            if problem:
                errors.append(f"{where}.host: {problem}")

    if lineup.max_rounds is not None and not 1 <= lineup.max_rounds <= MAX_ROUNDS_LIMIT:
        errors.append(f"max_rounds: must be between 1 and {MAX_ROUNDS_LIMIT}")

    tool_risk = {t["name"]: t["risk"] for t in TOOLS}
    for tool in lineup.allowed_tools or []:
        if tool in RETIRED_TOOLS:  # accepted so older teams keep working; judge_tools drops it
            warnings.append(f"allowed_tools: {tool} is retired and ignored ({RETIRED_TOOLS[tool]})")
        elif tool not in tool_risk:
            errors.append(f"allowed_tools: unknown tool {tool!r}; known: {sorted(tool_risk)}")
        elif tool_risk[tool] == "high":
            warnings.append(f"allowed_tools: {tool} is high risk; every proposal still needs a person's approval")

    expert_providers = lineup.expert.providers[:1]
    if expert_providers and any(m.providers[:1] == expert_providers for m in lineup.critics):
        warnings.append("a critic prefers the same provider as the expert; different providers catch more")

    for i in range(1, len(lineup.stages)):
        if lineup.stages[i].providers[:1] == lineup.stages[i - 1].providers[:1]:
            warnings.append(f"flow.stages[{i}] prefers the same provider as the stage before it (or none); "
                            "a hand-over between different providers adds more")
            break

    if len(lineup.sides) == SIDES and lineup.sides[0].providers[:1] == lineup.sides[1].providers[:1]:
        warnings.append("flow.sides: both sides prefer the same provider (or none); advocates on different providers "
                        "argue less alike")

    if lineup.proposers:
        drafter_providers = {tuple(m.providers[:1]) for m in (lineup.expert, *lineup.proposers)}
        if len(drafter_providers) == 1:
            warnings.append("the expert and its proposers prefer the same provider (or none); "
                            "independent drafts need different providers to add much")

    if errors:
        raise TeamValidationError("; ".join(errors))
    return warnings


def validate_team_meta(data: Dict[str, Any]) -> None:
    """Checks the optional list metadata: "category" (fixed list) and "created"/"modified" (ISO 8601)."""
    category = data.get("category")
    if category is not None and category not in TEAM_CATEGORIES:
        raise TeamValidationError(f"category: use one of {', '.join(TEAM_CATEGORIES)}")
    for key in ("created", "modified"):
        value = data.get(key)
        if value is None:
            continue
        try:
            if not isinstance(value, str):
                raise ValueError(value)
            datetime.fromisoformat(value)
        except ValueError:
            raise TeamValidationError(f"{key}: use an ISO 8601 timestamp") from None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def judge_tools(lineup: Lineup, judge_persona: Dict[str, Any]) -> Optional[List[str]]:
    """Tools the judge may propose: the team list narrowed by the persona's own list.

    None means every catalog tool. A team can only narrow what the persona allows, never widen it. The persona's own
    list is its `tools` for a character (format 2), its `mcp_capabilities` before C2.
    """
    own = character.persona_tools(judge_persona)
    if lineup.allowed_tools is None:
        return own
    team_tools = [t for t in lineup.allowed_tools if t not in RETIRED_TOOLS]
    if own is None:
        return team_tools
    return [t for t in team_tools if t in own]


def builtin_teams_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "configs" / "teams"


def custom_teams_dir() -> Path:
    configured = os.environ.get("GUARDIAN_TEAMS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "teams"


class TeamManager:
    """Loads and saves team files. Saving validates first, so a stored team always resolves.

    Builtin teams (configs/teams in the repository) are read-only defaults. Saved teams go to the
    custom directory (GUARDIAN_TEAMS_DIR, else <GUARDIAN_DATA_DIR>/teams) and override a builtin
    team with the same id, so dashboard edits never touch the git checkout.
    """

    def __init__(
        self,
        directory: Optional[Path] = None,
        persona_mgr: Optional[PersonaManager] = None,
        builtin_dir: Optional[Path] = None,
    ):
        self.directory = directory or custom_teams_dir()
        self.builtin_dir = builtin_dir or builtin_teams_dir()
        self.persona_mgr = persona_mgr or PersonaManager()

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_teams(self) -> List[Dict[str, Any]]:
        """Team files, custom overriding builtin by file name. Each carries "source": builtin|custom."""
        merged: Dict[str, Dict[str, Any]] = {}
        for source, directory in (("builtin", self.builtin_dir), ("custom", self.directory)):
            for path in sorted(directory.glob("*.json")):
                data = self._read(path)
                if data is not None:
                    merged[path.stem] = {**data, "source": source}
        return list(merged.values())

    def get_team(self, team_id: str) -> Optional[Dict[str, Any]]:
        if not TEAM_ID_PATTERN.fullmatch(team_id):
            return None
        for directory in (self.directory, self.builtin_dir):
            path = directory / f"{team_id}.json"
            if path.exists():
                return self._read(path)
        return None

    def is_custom(self, team_id: str) -> bool:
        return TEAM_ID_PATTERN.fullmatch(team_id) is not None and (self.directory / f"{team_id}.json").exists()

    def load(self, team_id: Optional[str]) -> Lineup:
        """Resolves a team id to a validated lineup; None or 'default' gives the built-in lineup."""
        if not team_id or (team_id == DEFAULT_TEAM_ID and self.get_team(team_id) is None):
            return default_lineup()
        data = self.get_team(team_id)
        if data is None:
            raise TeamValidationError(f"no team {team_id!r}")
        lineup = parse_lineup(data)
        if lineup.id != team_id:
            raise TeamValidationError(f"{team_id}.json declares id {lineup.id!r}")
        validate_lineup(lineup, self.persona_mgr)
        return lineup

    def save(self, data: Dict[str, Any]) -> List[str]:
        """Validates and writes a team file to the custom directory. Returns warnings."""
        data = {k: v for k, v in data.items() if k != "source"} if isinstance(data, dict) else data
        lineup = parse_lineup(data)
        validate_team_meta(data)
        warnings = validate_lineup(lineup, self.persona_mgr)
        # The server owns the timestamps: "created" survives edits (also of a builtin copy), "modified" is now.
        now = _now_iso()
        previous = self.get_team(lineup.id) or {}
        data = {**data, "created": previous.get("created") or data.get("created") or now, "modified": now}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{lineup.id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return warnings

    def delete(self, team_id: str) -> bool:
        """Deletes a custom team. Builtin teams cannot be deleted."""
        if not self.is_custom(team_id):
            return False
        (self.directory / f"{team_id}.json").unlink()
        return True

"""AI suggestion of a persona and its profile (docs/design/agent-characters.md, section 2.6).

A person describes the character in a few words; one LLM turn drafts the persona fields and the profile (`tagline`,
`look`) in the form of the guide. Nothing is saved: the draft goes back to the editor, where the person reads it,
changes it and saves it the usual way. The turn runs without tools in an empty directory (`LLMRunner.run_prompt`),
sees only the brief, the chosen seat and kind, and up to two builtin personas of that seat as style examples.
"""
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import character
import persona_lint
from llm_runner import BUILTIN_PROVIDER, LLMRunner
from persona_manager import PROFILE_LIMITS, PersonaManager, PersonaValidationError, validate_persona
from role_cards import persona_text
from team_manager import TeamManager

SUGGEST_PROMPT_VERSION = 1
SEATS = ("expert", "proposer", "critic", "judge")
KINDS = ("code", "ops", "design")
BRIEF_LIMIT = 1000
# The guide's limit for the persona text (section 3); above it the draft comes back with a warning.
PROMPT_SOFT_LIMIT = persona_lint.LENGTH_LIMIT
MAX_EXAMPLES = 2
ROLE_LABELS = {"expert": "Domain Expert Agent", "proposer": "Domain Expert Agent", "critic": "Critic Agent",
               "judge": "JEV-MCP Decision Agent"}
# Team category -> project kind, to pick examples written for the same kind.
CATEGORY_KINDS = {"development": "code", "operations": "ops", "design-docs": "design"}
USAGE, FAILED = 2, 5

PROMPT = """You draft one agent character for Server Guardian, a tool whose debates seat several LLM agents \
(expert, proposer, critic, judge) on code, server operations or design documents. Write in Korean.

The person describes the character:
<brief>
{brief}
</brief>
Seat: {seat}. Project kind: {kind}.

Rules for the persona (the system prompt the agent receives):
- Say who the character is: identity, expertise, the stance that makes it differ from other {seat}s, what it checks \
first, what it leaves to other seats. Start with "당신은 ...입니다."
- At most {limit} characters. No host counts, version numbers, file paths of a repository or environment variables. \
No claim to act on its own; the engine decides what runs. No tool names.
- Keep the seat's existing conventions where the examples show them (citing evidence, grading objections, saying \
plainly when nothing is wrong), in one or two sentences, not more.
- No real or public persons, brands or existing fictional characters, in any field.

Rules for the profile (shown to people only, never sent to an agent):
- tagline: one line, at most {tagline} characters, what the character is good for.
- look: at most {look} characters, the appearance of a fictional person for a portrait (age range, hair, clothing, \
one object); no text to draw, no real person.

Examples of builtin personas in this seat (style only; do not copy their scope):
{examples}
{current}
Answer with one JSON object and nothing else:
{{"id": "lowercase_id", "name": "short display name", "domain": "one line", "system_prompt": "...", \
"focus_areas": ["..."], "critique_focus": ["..."], "profile": {{"tagline": "...", "look": "..."}}}}
"""


class SuggestError(Exception):
    def __init__(self, message: str, code: int = USAGE) -> None:
        super().__init__(message)
        self.code = code


def seat_personas(teams: List[Dict[str, Any]], seat: str, kind: str) -> List[str]:
    """Persona ids that teams seat in `seat`, those of teams of `kind` first, without repeats."""
    def members(team: Dict[str, Any]) -> List[Any]:
        if seat == "expert":
            return [team.get("expert")]
        if seat == "judge":
            return [team.get("judge")]
        value = team.get("proposers" if seat == "proposer" else "critic") or []
        return value if isinstance(value, list) else [value]

    matched: List[str] = []
    other: List[str] = []
    for team in teams:
        same_kind = CATEGORY_KINDS.get(team.get("category") or "") == kind
        for member in members(team):
            pid = member if isinstance(member, str) else (member or {}).get("persona")
            if isinstance(pid, str) and pid not in matched and pid not in other:
                (matched if same_kind else other).append(pid)
    return matched + other


def build_prompt(brief: str, seat: str, kind: str, examples: List[Dict[str, Any]],
                 current: Optional[Dict[str, Any]] = None) -> str:
    # An example that opted into role cards is shown with its card: the draft keeps the seat conventions until C2.
    shown = "\n".join(f"- {e.get('name', e.get('id'))}: {persona_text(e)}" for e in examples) or "(none)"
    draft = ""
    if current and str(current.get("system_prompt") or "").strip():
        kept = {k: current[k] for k in ("name", "domain", "system_prompt") if current.get(k)}
        draft = f"\nThe person's current draft, to improve rather than replace:\n{json.dumps(kept, ensure_ascii=False)}\n"
    return PROMPT.format(brief=brief, seat=seat, kind=kind, limit=PROMPT_SOFT_LIMIT, tagline=PROFILE_LIMITS["tagline"],
                         look=PROFILE_LIMITS["look"], examples=shown, current=draft)


def parse_answer(text: str) -> Dict[str, Any]:
    """The first JSON object of the answer (a fenced block or bare). Raises SuggestError."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise SuggestError("the model answered without a JSON object", FAILED)
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise SuggestError(f"the model's JSON did not parse: {exc}", FAILED) from exc
    if not isinstance(data, dict):
        raise SuggestError("the model's JSON is not an object", FAILED)
    return data


def _free_id(wanted: Any, name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "_", str(wanted or name or "").lower()).strip("_-")[:56] or "character"
    if not base[0].isalnum():
        base = f"c{base}"[:56]
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def clean_draft(data: Dict[str, Any], seat: str, taken: set) -> Tuple[Dict[str, Any], List[str]]:
    """A draft the editor can load: known fields only, a free id, the seat's role label; plus the guide's warnings."""
    draft: Dict[str, Any] = {
        "id": _free_id(data.get("id"), str(data.get("name") or ""), taken),
        "name": str(data.get("name") or "").strip(),
        "role": ROLE_LABELS[seat],
        "domain": str(data.get("domain") or "").strip(),
        "system_prompt": str(data.get("system_prompt") or "").strip(),
    }
    for key in ("focus_areas", "critique_focus"):
        items = [str(v).strip() for v in data.get(key) or [] if str(v).strip()] if isinstance(data.get(key), list) else []
        if items:
            draft[key] = items[:5]
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    draft["profile"] = {k: str(profile.get(k) or "").strip()[:limit] for k, limit in PROFILE_LIMITS.items()}
    try:
        draft = validate_persona(draft)
    except PersonaValidationError as exc:
        raise SuggestError(f"the draft is not a valid persona: {exc}", FAILED) from exc
    # The guide's warn findings, plus tool names: the model never picks tools, so naming one in a draft is noise.
    warnings = persona_lint.warnings(draft["system_prompt"])
    warnings += [f"system_prompt {f['what']} ({f['match']!r})" for f in persona_lint.check_text(draft["system_prompt"])
                 if f["rule"] == "tool"]
    return draft, warnings


def suggest(request: Any, manager: Optional[PersonaManager] = None,
            runner_factory: Callable[[Optional[List[str]]], LLMRunner] = lambda providers: LLMRunner(providers=providers)
            ) -> Dict[str, Any]:
    """Drafts a persona from the request {brief, seat, kind, provider?, current?}. Raises SuggestError."""
    if not isinstance(request, dict):
        raise SuggestError("request must be a JSON object")
    brief = str(request.get("brief") or "").strip()
    seat = request.get("seat") or "expert"
    kind = request.get("kind") or "code"
    provider = request.get("provider") or None
    if not brief:
        raise SuggestError("brief: describe the character in a few words")
    if len(brief) > BRIEF_LIMIT:
        raise SuggestError(f"brief: at most {BRIEF_LIMIT} characters")
    if seat not in SEATS:
        raise SuggestError(f"seat: use one of {', '.join(SEATS)}")
    if kind not in KINDS:
        raise SuggestError(f"kind: use one of {', '.join(KINDS)}")
    if provider is not None and (not isinstance(provider, str) or not re.match(r"^[a-z]{2,16}$", provider)):
        raise SuggestError("provider: a provider name such as claude, codex or agy")
    manager = manager or PersonaManager()
    personas = {p["id"]: p for p in manager.list_personas()}
    examples = [personas[pid] for pid in seat_personas(TeamManager(persona_mgr=manager).list_teams(), seat, kind)
                if pid in personas and personas[pid].get("source") == "builtin"][:MAX_EXAMPLES]
    current = request.get("current") if isinstance(request.get("current"), dict) else None
    # Suggestions fill the format 1 form; a character (format 2) has none of those fields, and its editor is C4.
    if current is not None and character.is_character(current):
        raise SuggestError("suggestions write format 1 personas; characters are edited from C4 on")
    runner = runner_factory([provider] if provider else None)
    name, text = runner.run_prompt(build_prompt(brief, seat, kind, examples, current), system="")
    if name == BUILTIN_PROVIDER:
        raise SuggestError(f"no LLM CLI answered: {runner.last_error or 'none installed'}", FAILED)
    draft, warnings = clean_draft(parse_answer(text), seat, set(personas))
    return {"ok": True, "suggestion": draft, "warnings": warnings, "provider": name,
            "examples": [e["id"] for e in examples], "prompt_version": SUGGEST_PROMPT_VERSION}


def run_cli(text: str) -> Tuple[int, Dict[str, Any]]:
    """--persona-suggest: request JSON on stdin, the draft as JSON on stdout. Exit 2 bad request, 5 model failure."""
    try:
        request = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        return USAGE, {"ok": False, "error": f"invalid JSON: {exc}"}
    try:
        return 0, suggest(request)
    except SuggestError as exc:
        return exc.code, {"ok": False, "error": str(exc)}

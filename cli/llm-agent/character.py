"""Character files: persona format 2 (docs/design/agent-characters.md, C2).

A character is a reusable agent identity. Its `persona` block is the only part that reaches the model, rendered
through one fixed template (`render_persona`) so every character reads the same way and no stray instruction can
hide in the wording. `seats` names where it may sit, `kinds` the project kinds it was written for, `tools` narrows
the judge exactly like the format 1 `mcp_capabilities`, and `profile` is shown to people only. Format 1 files (a raw
`system_prompt`, no "format" key) keep loading and behaving as before: `is_character` switches every format-aware
caller, and this module imports no other local module.
"""

from typing import Any, List, Mapping, Optional

# Where a character may sit: the debate engine's positions plus the solo runner.
SEATS = ("expert", "proposer", "critic", "verifier", "judge", "stage", "side", "solo")
# Project kinds a character may be written for (absent or empty: any kind).
KINDS = ("code", "ops", "design")

# Limits of the persona block's fields (agent-characters.md, section 3): (most characters per value, fewest items,
# most items). identity and stance are single texts, so their item counts are 1 and 1.
PERSONA_LIMITS = {
    "identity": (120, 1, 1),
    "expertise": (60, 1, 4),
    "stance": (200, 1, 1),
    "checks": (80, 1, 5),
    "boundaries": (80, 0, 3),
}
# The rendered block's own bounds (persona_lint reports both: an error above the limit at save, a note above 500).
RENDER_LIMIT = 700
RENDER_NOTE = 500


def is_character(persona: Mapping[str, Any]) -> bool:
    """True for a format 2 character file; every format 1 persona is sent and checked as before."""
    return persona.get("format") == 2


def render_persona(block: Mapping[str, Any]) -> str:
    """The fixed Korean template of a persona block: a character's whole own text, lines joined with "\\n".

    A hand-edited file may hold any shape, so this never raises: a block that is not a mapping renders like an
    empty one, a list field that is not a list and a text field that is not a text read as empty.
    """
    if not isinstance(block, Mapping):
        block = {}

    def text(field: str) -> str:
        value = block.get(field)
        return value.strip() if isinstance(value, str) else ""

    def items(field: str) -> List[str]:
        value = block.get(field)
        return [str(item).strip() for item in value] if isinstance(value, list) else []

    lines = [
        f"당신은 {text('identity')}입니다.",
        f"전문 분야: {', '.join(items('expertise'))}.",
        f"관점: {text('stance')}",
        "먼저 확인할 것:",
        *[f"- {item}" for item in items('checks')],
    ]
    boundaries = items("boundaries")
    if boundaries:
        lines.append("맡지 않는 일:")
        lines.extend(f"- {boundary}" for boundary in boundaries)
    return "\n".join(lines)


def persona_tools(persona: Mapping[str, Any]) -> Optional[List[str]]:
    """The tools a persona allows its judge to propose: `tools` for a character, `mcp_capabilities` before C2.

    An empty list or a missing key means None, as today: the whole catalog, which the team list can only narrow.
    The format 2 validator refuses an empty `tools` (ambiguous for a ceiling), so an empty list reaches here only
    from a hand-edited file."""
    tools = persona.get("tools") if is_character(persona) else persona.get("mcp_capabilities")
    return tools or None


def persona_seats(persona: Mapping[str, Any]) -> Optional[List[str]]:
    """The seats a character may take; None (a format 1 persona, or no list) means no seat limit."""
    if is_character(persona):
        seats = persona.get("seats")
        return list(seats) if isinstance(seats, list) else None
    return None


def body_text(persona: Mapping[str, Any]) -> str:
    """The persona's own text without a role card: the rendered block of a character, the system_prompt before C2."""
    if is_character(persona):
        block = persona.get("persona")
        return render_persona(block if isinstance(block, Mapping) else {})
    return str(persona.get("system_prompt") or "")

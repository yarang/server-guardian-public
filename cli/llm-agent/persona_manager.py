"""
Persona Manager for Server Guardian.

Personas come from two directories:
- builtin: configs/personas in the repository (read-only defaults shipped with the code)
- custom:  GUARDIAN_PERSONAS_DIR, else <GUARDIAN_DATA_DIR>/personas (edited from the dashboard)

A custom persona with the same id overrides the builtin one, so dashboard edits never touch the
git checkout. Deleting the custom file brings the builtin back.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import character
import persona_lint
from mcp_client import TOOLS
from role_cards import CARD_KINDS, CARD_SEATS

PERSONA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TEXT_LIMITS = {"name": 200, "role": 200, "domain": 500, "system_prompt": 20000}
# Only system_prompt (and mcp_capabilities, for the judge's tools) reaches the model. domain, focus_areas and
# critique_focus are stored and shown, never sent: a focus that should steer an agent belongs in its system_prompt.
LIST_FIELDS = ("focus_areas", "critique_focus", "mcp_capabilities")
# profile is shown to people only (docs/design/agent-characters.md): a one-line tagline and the look of the portrait.
PROFILE_LIMITS = {"tagline": 120, "look": 200}
# role_card opts the persona into the engine's seat rules (role_cards.py, agent-characters.md C1): its value is the
# persona's home seat, and its system_prompt then leaves the seat rules out. role_card_kind names the kind of cards it
# gets (absent: the code cards).
ALLOWED_FIELDS = {"id", "profile", "role_card", "role_card_kind", *TEXT_LIMITS, *LIST_FIELDS}
# "temperature" was removed 2026-10-23 (never passed to a model CLI): old files still load, validation drops it.
REQUIRED_FIELDS = ("name", "role", "system_prompt")
# Format 2, the character file (agent-characters.md, sections 2.1 and 3, phase C2): these fields and no others.
CHARACTER_FIELDS = {"id", "format", "name", "profile", "seats", "kinds", "role_card", "role_card_kind", "persona",
                    "tools"}
# What a format 1 persona carries that a character carries elsewhere: persona block, seats, kinds, tools.
FORMAT1_FIELDS = ("system_prompt", "role", "domain", "focus_areas", "critique_focus", "mcp_capabilities")


class PersonaValidationError(ValueError):
    """Raised when a persona definition is incomplete or malformed."""


def builtin_personas_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "configs" / "personas"


def custom_personas_dir() -> Path:
    configured = os.environ.get("GUARDIAN_PERSONAS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser() / "personas"


def validate_persona(data: Any) -> Dict[str, Any]:
    """Returns the persona with only known fields. Raises PersonaValidationError.

    A file without "format" is format 1 (a raw system_prompt, unchanged since C2); "format": 2 is a character file
    (agent-characters.md, C2); anything else is refused.
    """
    if not isinstance(data, dict):
        raise PersonaValidationError("persona must be a JSON object")
    fmt = data.get("format")
    if fmt is None:
        return _validate_format1(data)
    if fmt == 2:
        return _validate_character(data)
    raise PersonaValidationError("format: 2 or absent")


def _profile_errors(profile: Any) -> List[str]:
    if not isinstance(profile, dict) or set(profile) - set(PROFILE_LIMITS):
        return [f"profile: an object with {', '.join(PROFILE_LIMITS)}"]
    return [f"profile.{name}: expected text of at most {limit} characters"
            for name, limit in PROFILE_LIMITS.items()
            if profile.get(name) is not None and (not isinstance(profile.get(name), str) or len(profile[name]) > limit)]


def _validate_format1(data: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    persona_id = data.get("id")
    if not isinstance(persona_id, str) or not PERSONA_ID_PATTERN.fullmatch(persona_id):
        errors.append("id: use 1-64 lowercase letters, digits, '-' or '_'")
    for name in REQUIRED_FIELDS:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name}: required")
    for name, limit in TEXT_LIMITS.items():
        value = data.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            errors.append(f"{name}: expected text of at most {limit} characters")
    for name in LIST_FIELDS:
        value = data.get(name)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) and len(v) <= 500 for v in value)
        ):
            errors.append(f"{name}: expected a list of short texts")
    if data.get("profile") is not None:
        errors.extend(_profile_errors(data["profile"]))
    if data.get("role_card") is not None and data.get("role_card") not in CARD_SEATS:
        errors.append(f"role_card: one of {', '.join(CARD_SEATS)}")
    if data.get("role_card_kind") is not None:
        if data.get("role_card_kind") not in CARD_KINDS:
            errors.append(f"role_card_kind: one of {', '.join(CARD_KINDS)}")
        elif data.get("role_card") is None:
            errors.append("role_card_kind: needs role_card")
    known_tools = {t["name"] for t in TOOLS}
    unknown_tools = [t for t in data.get("mcp_capabilities") or [] if isinstance(t, str) and t not in known_tools]
    if unknown_tools:
        errors.append(f"mcp_capabilities: unknown tool(s) {unknown_tools}; known: {sorted(known_tools)}")
    if errors:
        raise PersonaValidationError("; ".join(errors))
    return {k: v for k, v in data.items() if k in ALLOWED_FIELDS}


def _distinct_choices(value: Any, choices: Tuple[str, ...], required: bool) -> Optional[List[str]]:
    """`value` as a list of distinct strings from `choices`; None when it is not one (empty only when not required)."""
    if (not isinstance(value, list) or not value and required
            or any(not isinstance(v, str) or v not in choices for v in value)
            or len(set(value)) != len(value)):
        return None
    return value


def _character_errors(data: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    persona_id = data.get("id")
    if not isinstance(persona_id, str) or not PERSONA_ID_PATTERN.fullmatch(persona_id):
        errors.append("id: use 1-64 lowercase letters, digits, '-' or '_'")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name: required")
    elif len(name) > TEXT_LIMITS["name"]:
        errors.append(f"name: expected text of at most {TEXT_LIMITS['name']} characters")
    legacy = [field for field in FORMAT1_FIELDS if field in data]
    if legacy:
        errors.append(f"format 1 field(s) {legacy} do not belong in a character: the persona block, seats, kinds "
                      "and tools carry them")
    unknown = sorted(set(data) - CHARACTER_FIELDS)
    if unknown:
        errors.append(f"unknown field(s) {unknown}")

    seats = data.get("seats")
    if _distinct_choices(seats, character.SEATS, required=True) is None:
        seats = None
        errors.append(f"seats: a non-empty list of distinct seats from {', '.join(character.SEATS)}")
    kinds = data.get("kinds")
    if kinds is not None and _distinct_choices(kinds, character.KINDS, required=False) is None:
        kinds = None
        errors.append(f"kinds: a list of distinct kinds from {', '.join(character.KINDS)}")

    block = data.get("persona")
    if not isinstance(block, dict) or set(block) - set(character.PERSONA_LIMITS):
        block = None
        errors.append(f"persona: an object with {', '.join(character.PERSONA_LIMITS)}")
    else:
        for field, (limit, fewest, most) in character.PERSONA_LIMITS.items():
            value = block.get(field)
            if fewest == most == 1:  # identity and stance: single texts
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"persona.{field}: required")
                elif len(value) > limit:
                    errors.append(f"persona.{field}: expected text of at most {limit} characters")
            else:
                if value is None and fewest == 0:  # boundaries may be left out
                    value = []
                if (not isinstance(value, list) or not fewest <= len(value) <= most
                        or any(not isinstance(v, str) or not v.strip() or len(v) > limit for v in value)):
                    errors.append(f"persona.{field}: expected {fewest}-{most} texts of at most {limit} characters")

    # A character's text carries no seat rules (that is what the cards are for), so a card seat needs its card.
    role_card = data.get("role_card")
    if role_card is None and seats is not None and any(s in CARD_SEATS for s in seats):
        errors.append(f"role_card: required for a seat in {', '.join(CARD_SEATS)} (a character's text carries no seat "
                      "rules)")
    if role_card is not None:
        if role_card not in CARD_SEATS:
            errors.append(f"role_card: one of {', '.join(CARD_SEATS)}")
        elif seats is not None and role_card not in seats:
            errors.append(f"role_card: {role_card} is not one of the persona's seats")
    kind = data.get("role_card_kind")
    if kind is not None:
        if kind not in CARD_KINDS:
            errors.append(f"role_card_kind: one of {', '.join(CARD_KINDS)}")
        elif role_card is None:
            errors.append("role_card_kind: needs role_card")
        elif kinds is not None and kinds and kind not in kinds:
            errors.append(f"role_card_kind: {kind} is not one of the persona's kinds")

    tools = data.get("tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, str) for t in tools)):
        errors.append("tools: expected a list of tool names")
    elif tools == []:
        # `tools` is a ceiling, so an empty list is ambiguous: leaving the key out means the whole catalog.
        errors.append("tools: leave the key out for the whole catalog; an empty list is not allowed")
    else:
        known_tools = {t["name"] for t in TOOLS}
        unknown_tools = [t for t in tools or [] if t not in known_tools]
        if unknown_tools:
            errors.append(f"tools: unknown tool(s) {unknown_tools}; known: {sorted(known_tools)}")

    if data.get("profile") is not None:
        errors.extend(_profile_errors(data["profile"]))

    # Content rules on what will reach the model: seat and kind rules (the lint's `move`), autonomy claims and the
    # rendered length are errors for a character; drifting facts stay warnings (persona_lint.persona_warnings).
    if block is not None:
        for finding in persona_lint.check_text(character.render_persona(block)):
            if (finding["level"] == persona_lint.MOVE or finding["rule"] == "autonomy"
                    or (finding["rule"] == "length" and finding["level"] == persona_lint.WARN)):
                match = f" ({finding['match']!r})" if finding["match"] else ""
                errors.append(f"persona: {finding['what']}{match}")
    return errors


def _validate_character(data: Dict[str, Any]) -> Dict[str, Any]:
    errors = _character_errors(data)
    if errors:
        raise PersonaValidationError("; ".join(errors))
    return {k: v for k, v in data.items() if k in CHARACTER_FIELDS}


class PersonaManager:
    def __init__(self, personas_dir: Optional[Path] = None, custom_dir: Optional[Path] = None):
        # personas_dir holds the builtin personas; custom_dir the editable overrides.
        self.personas_dir = personas_dir or builtin_personas_dir()
        self.custom_dir = custom_dir or custom_personas_dir()

    @staticmethod
    def _read_dir(directory: Path) -> List[Dict[str, Any]]:
        personas = []
        for p_file in sorted(directory.glob("*.json")):
            try:
                data = json.loads(p_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("id"):
                personas.append(data)
        return personas

    def list_personas(self) -> List[Dict[str, Any]]:
        """All personas, custom overriding builtin by id. Each carries "source": builtin|custom."""
        merged: Dict[str, Dict[str, Any]] = {}
        for source, directory in (("builtin", self.personas_dir), ("custom", self.custom_dir)):
            for data in self._read_dir(directory):
                merged[data["id"]] = {**data, "source": source}
        return list(merged.values())

    def get_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """Finds a persona by ID, preferring the custom override."""
        for directory in (self.custom_dir, self.personas_dir):
            for data in self._read_dir(directory):
                if data.get("id") == persona_id:
                    return data
        return None

    def is_custom(self, persona_id: str) -> bool:
        return PERSONA_ID_PATTERN.fullmatch(persona_id) is not None and (self.custom_dir / f"{persona_id}.json").exists()

    def save_persona(self, persona: Dict[str, Any]) -> Path:
        """Validates and writes the persona to the custom directory."""
        # The list and the report add display-only keys ("source", a character's "rendered"); a client that sends
        # a listed persona back keeps working, the way a team save drops its "source".
        if isinstance(persona, dict):
            persona = {k: v for k, v in persona.items() if k not in ("source", "rendered")}
        clean = validate_persona(persona)
        existing = self.get_persona(clean["id"])
        if existing is not None and character.is_character(existing) and not character.is_character(clean):
            # a format 1 file under a character's id would replace the character (or hide the builtin one)
            raise PersonaValidationError(f"{clean['id']} is a character (format 2); delete it first or pick another id")
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.custom_dir / f"{clean['id']}.json"
        tmp_path = target_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(target_path)
        return target_path

    def delete_persona(self, persona_id: str) -> bool:
        """Deletes a custom persona. Builtin personas cannot be deleted."""
        if not self.is_custom(persona_id):
            return False
        (self.custom_dir / f"{persona_id}.json").unlink()
        return True

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
from typing import Any, Dict, List, Optional

from mcp_client import TOOLS

PERSONA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TEXT_LIMITS = {"name": 200, "role": 200, "domain": 500, "system_prompt": 20000}
LIST_FIELDS = ("focus_areas", "critique_focus", "mcp_capabilities")
ALLOWED_FIELDS = {"id", *TEXT_LIMITS, *LIST_FIELDS}
# "temperature" was removed 2026-10-23 (never passed to a model CLI): old files still load, validation drops it.
REQUIRED_FIELDS = ("name", "role", "system_prompt")


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
    """Returns the persona with only known fields. Raises PersonaValidationError."""
    if not isinstance(data, dict):
        raise PersonaValidationError("persona must be a JSON object")
    errors: List[str] = []
    persona_id = data.get("id")
    if not isinstance(persona_id, str) or not PERSONA_ID_PATTERN.match(persona_id):
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
    known_tools = {t["name"] for t in TOOLS}
    unknown_tools = [t for t in data.get("mcp_capabilities") or [] if isinstance(t, str) and t not in known_tools]
    if unknown_tools:
        errors.append(f"mcp_capabilities: unknown tool(s) {unknown_tools}; known: {sorted(known_tools)}")
    if errors:
        raise PersonaValidationError("; ".join(errors))
    return {k: v for k, v in data.items() if k in ALLOWED_FIELDS}


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
        return PERSONA_ID_PATTERN.match(persona_id) is not None and (self.custom_dir / f"{persona_id}.json").exists()

    def save_persona(self, persona: Dict[str, Any]) -> Path:
        """Validates and writes the persona to the custom directory."""
        clean = validate_persona(persona)
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

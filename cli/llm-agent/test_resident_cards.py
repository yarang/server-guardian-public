"""Golden dump of the resident start card (docs/design/resident-agents.md, M2d).

One card per golden case, built by `resident_cards.card_text` with a fake personas object: no real persona
file is read, so the dump changes only when the card module changes. The expected dump lives in
`golden/resident_cards.txt`; a wording change must show up in its diff and nothing else. The open card
(declarations with `tools: "open"`, resident-open-tools.md O2) is pinned the same way by
`golden/resident_cards_open.txt`. Regenerate on purpose with `GOLDEN_UPDATE=1 python3 -m unittest
test_resident_cards`, then review the diff sentence by sentence: the card opens every fresh resident
conversation.
"""

import os
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

import resident_cards
import testenv

GOLDEN = Path(__file__).parent / "golden" / "resident_cards.txt"
OPEN_GOLDEN = Path(__file__).parent / "golden" / "resident_cards_open.txt"

# A format 2 character (character.py render_persona shapes its persona block), and a persona whose body is
# empty but whose profile tagline is set (the body_text of a persona without a system prompt is empty).
CHARACTER: Dict[str, Any] = {
    "id": "c1",
    "format": 2,
    "name": "코드 탐사자",
    "persona": {"identity": "코드 탐사자", "expertise": ["코드 읽기", "테스트 작성"],
                "stance": "직접 읽은 코드만 근거로 삼습니다.", "checks": ["파일의 실제 줄"]},
    "profile": {"tagline": "코드를 직접 확인하는 탐사자"},
}
TAGLINE_PERSONA: Dict[str, Any] = {"id": "c3", "profile": {"tagline": "한 줄 소개만 남은 캐릭터"}}

# (label, declaration entry): the four golden cases, a to d.
CASES = (
    ("a: no character", {"address": "claude@node-1", "name": "claude", "provider": "claude"}),
    ("b: character c1", {"address": "agy@node-1", "name": "agy", "provider": "agy", "character": "c1"}),
    ("c: empty body, tagline", {"address": "agy@node-2", "name": "agy", "provider": "agy", "character": "c3"}),
    ("d: unknown character id", {"address": "claude@node-2", "name": "claude", "provider": "claude",
                                 "character": "c9"}),
)

# The open card's two cases: the same declarations as a and b with `tools: "open"`.
OPEN_CASES = (
    ("a: no character", {"address": "claude@node-1", "name": "claude", "provider": "claude",
                         "tools": "open"}),
    ("b: character c1", {"address": "agy@node-1", "name": "agy", "provider": "agy",
                         "character": "c1", "tools": "open"}),
)


class FakePersonas:
    """The persona lookup of the golden cases: `get_persona` over a plain dict, like PersonaManager."""

    def __init__(self, personas: Dict[str, Dict[str, Any]]):
        self.personas = personas

    def get_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        return self.personas.get(persona_id)


class RaisingPersonas:
    """A persona manager that fails on every lookup: the card must still be written."""

    def get_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("persona store unavailable")


def fake_personas() -> FakePersonas:
    return FakePersonas({"c1": CHARACTER, "c3": TAGLINE_PERSONA})


def dump() -> str:
    personas = fake_personas()
    return "".join(f"=== {label}\n{resident_cards.card_text(entry, personas)}\n\n"
                   for label, entry in CASES)


def open_dump() -> str:
    personas = fake_personas()
    return "".join(f"=== {label}\n{resident_cards.card_text(entry, personas)}\n\n"
                   for label, entry in OPEN_CASES)


class ResidentCardTests(unittest.TestCase):
    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)

    def test_dump_matches_golden(self):
        text = dump()
        if os.environ.get("GOLDEN_UPDATE") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(text, encoding="utf-8")
        self.assertTrue(GOLDEN.exists(), "run once with GOLDEN_UPDATE=1 to record the dump")
        self.assertEqual(text, GOLDEN.read_text(encoding="utf-8"),
                         "resident cards changed; diff golden/resident_cards.txt after GOLDEN_UPDATE=1")

    def test_no_peer_address_in_the_card(self):
        entry = {"address": "a@node-1", "name": "a", "provider": "claude",
                 "peers": ["x@node-2", "y@node-3"]}
        card = resident_cards.card_text(entry, fake_personas())
        self.assertNotIn("x@node-2", card)
        self.assertNotIn("y@node-3", card)

    def test_first_line_names_the_version(self):
        card = resident_cards.card_text({"address": "a@node-1", "name": "a", "provider": "claude"})
        self.assertEqual(card.split("\n", 1)[0],
                         f"[resident card v{resident_cards.RESIDENT_CARD_VERSION}]")

    def test_a_failing_persona_manager_still_writes_the_card(self):
        entry = {"address": "a@node-1", "name": "a", "provider": "claude", "character": "c1"}
        card = resident_cards.card_text(entry, RaisingPersonas())
        self.assertEqual(card, resident_cards.card_text({"address": "a@node-1", "name": "a",
                                                         "provider": "claude"}))

    def test_the_seven_sections_appear_in_order(self):
        card = resident_cards.card_text({"address": "a@node-1", "name": "a", "provider": "claude",
                                         "character": "c1"}, fake_personas())
        positions = []
        for heading in resident_cards.SECTION_HEADINGS:
            self.assertIn(heading, card)
            positions.append(card.index(heading))
        self.assertEqual(positions, sorted(positions), "the seven sections are out of order")

    def test_an_unknown_character_id_omits_the_character_part(self):
        plain = resident_cards.card_text({"address": "claude@node-2", "name": "claude",
                                          "provider": "claude"})
        unknown = resident_cards.card_text({"address": "claude@node-2", "name": "claude",
                                            "provider": "claude", "character": "c9"}, fake_personas())
        self.assertEqual(unknown, plain)
        self.assertNotIn("캐릭터:", unknown)

    def test_the_default_persona_manager_is_only_a_lookup(self):
        # no personas passed: the lazily built PersonaManager finds no such character, so no character part.
        card = resident_cards.card_text({"address": "a@node-1", "name": "a", "provider": "claude",
                                         "character": "no-such-character-zz"})
        self.assertEqual(card, resident_cards.card_text({"address": "a@node-1", "name": "a",
                                                         "provider": "claude"}))

    def test_an_empty_body_falls_back_to_the_tagline(self):
        card = resident_cards.card_text({"address": "a@node-1", "name": "a", "provider": "claude",
                                         "character": "c3"}, fake_personas())
        self.assertIn("한 줄 소개만 남은 캐릭터", card)

    def test_the_card_without_a_character_stays_short(self):
        card = resident_cards.card_text({"address": "a@node-1", "name": "a", "provider": "claude"})
        self.assertLess(len(card), 2000)


class ResidentOpenCardTests(unittest.TestCase):
    """The card of a declaration with tools: "open" (resident-open-tools.md O2): the closed card
    plus one plain-ASCII section about the sandbox, and its own version line."""

    def setUp(self):
        testenv.isolate_data_dir()
        self.addCleanup(testenv.restore_data_dir)

    def test_dump_matches_golden(self):
        text = open_dump()
        if os.environ.get("GOLDEN_UPDATE") == "1":
            OPEN_GOLDEN.parent.mkdir(exist_ok=True)
            OPEN_GOLDEN.write_text(text, encoding="utf-8")
        self.assertTrue(OPEN_GOLDEN.exists(), "run once with GOLDEN_UPDATE=1 to record the dump")
        self.assertEqual(text, OPEN_GOLDEN.read_text(encoding="utf-8"),
                         "open resident cards changed; diff golden/resident_cards_open.txt "
                         "after GOLDEN_UPDATE=1")

    def test_the_open_section_names_the_agents_own_sandbox_home(self):
        card = resident_cards.card_text(dict(OPEN_CASES[0][1]), fake_personas())
        self.assertIn("/home/gsa-claude/work", card)
        other = resident_cards.card_text(dict(OPEN_CASES[1][1]), fake_personas())
        self.assertIn("/home/gsa-agy/work", other)
        self.assertNotIn("gsa-claude", other)

    def test_the_open_section_is_plain_ascii(self):
        card = resident_cards.card_text(dict(OPEN_CASES[0][1]), fake_personas())
        section = card.split(resident_cards.OPEN_SECTION_HEADING, 1)[1]
        self.assertEqual(section, section.encode("ascii").decode("ascii"))

    def test_the_open_section_tells_the_worktree_flow(self):
        # O3: call task_worktree first, work only there, task_update collects the patch, a refusal
        # leaves the task open.
        card = resident_cards.card_text(dict(OPEN_CASES[0][1]), fake_personas())
        for needle in ("task_worktree", "task_update status=completed", "bad_patch",
                       "patch_too_large"):
            self.assertIn(needle, card)

    def test_no_line_of_either_card_starts_with_bang_or_slash(self):
        for dump_text in (dump(), open_dump()):
            for line in dump_text.split("\n"):
                self.assertFalse(line.startswith(("!", "/")), line)

    def test_the_open_card_carries_its_own_version_and_the_closed_its_own(self):
        open_card = resident_cards.card_text({"address": "a@node-1", "name": "a",
                                              "provider": "claude", "tools": "open"})
        self.assertEqual(open_card.split("\n", 1)[0],
                         f"[resident card v{resident_cards.RESIDENT_OPEN_CARD_VERSION}]")
        closed_card = resident_cards.card_text({"address": "a@node-1", "name": "a",
                                                "provider": "claude"})
        self.assertEqual(closed_card.split("\n", 1)[0],
                         f"[resident card v{resident_cards.RESIDENT_CARD_VERSION}]")

    def test_a_closed_card_never_names_a_sandbox_home(self):
        # the same declaration without `tools`: the open additions must all be gone
        entry = {"address": "claude@node-1", "name": "claude", "provider": "claude",
                 "character": "c1"}
        closed = resident_cards.card_text(dict(entry), fake_personas())
        self.assertNotIn(resident_cards.OPEN_SECTION_HEADING, closed)
        self.assertNotIn("gsa-", closed)


if __name__ == "__main__":
    unittest.main()

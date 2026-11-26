"""Role cards: the seat rules the engine adds to a persona (docs/design/agent-characters.md, C1).

A persona says who an agent is; how to behave in a seat (cite what you read, grade objections, approve only what is
confirmed) is the same for every persona of that seat, so it is written once here instead of in every persona. Only
personas that opt in with `"role_card": "<seat>"` get a card: their `system_prompt` no longer carries the seat rules.
Every other persona (custom ones on the hub, the ops, design and general builtins) is sent exactly as before.

The value of `role_card` names the persona's home seat: the seat its text was written for. The card always follows
the home seat (operator's decision of 2026-10-31): a persona keeps its seat and its role, and a team that seats it
elsewhere gets a warning from `team_manager.validate_lineup`, not another card.

Cards are keyed by seat and card kind. The card kind is the persona's own, `"role_card_kind"` (absent: the code
cards), not the kind of the project it runs in, so a persona and its cards can be reused in any project (operator's
decision of 2026-10-31). The ops cards (chapter 5 of the design document) hold the sentences every ops persona of
that seat ended with, plus the generic seat rules the operator approved on 2026-10-31 (decisions C-G of the C1
implementation notes); a persona-specific variant of such a rule stays in the persona's own text.
"""

from typing import Any, Dict, Mapping, Optional, Tuple

import character

# Seats a persona may name as its home seat.
CARD_SEATS = ("expert", "critic", "judge")
# Card kinds a persona may name besides the code cards (role_card_kind absent).
CARD_KINDS = ("ops",)

# (seat, card kind) -> card. The sentences are the builtin personas' own (agent-characters.md, C1 implementation notes).
CARDS: Dict[Tuple[str, Optional[str]], str] = {
    ("expert", None): (
        "주장은 직접 읽은 파일과 줄만 근거로 삼고 파일:줄로 인용하세요. 읽지 않은 코드는 추측하지 말고 먼저 요청하세요. "
        "제안에는 바꿀 파일과 변경 내용, 추가하거나 고칠 테스트, 확인할 명령, 되돌리는 방법을 포함하세요. "
        "과제가 질문이면 코드로 확인한 답을 쓰세요."),
    ("critic", None): (
        "지적마다 해당 코드를 파일:줄로 인용하고, 어떤 입력에서 무엇이 잘못되는지 구체적인 시나리오와 "
        "심각도(높음/중간/낮음)를 적으세요. 인용할 수 없는 추측은 지적하지 말고 필요한 코드를 먼저 요청하세요. "
        "제안에 문제가 없으면 없다고 분명히 쓰세요."),
    ("judge", None): (
        "근거가 확인된 항목만 승인하고, 인용이 없거나 확인되지 않은 주장, 지어낸 파일·줄이나 점검 결과에 기댄 항목은 "
        "승인하지 마세요. 높음 심각도 지적이 해소되지 않았으면 합의로 보지 마세요. 변경이 필요하면 아래 규칙에 적힌 "
        "도구 조치(파일 수정, 테스트 실행 등)로 제안하세요. 당신이 도구를 직접 실행하지는 않으며, 어떤 조치가 사람의 "
        "승인 없이 실행되는지는 아래 규칙에 적혀 있습니다. 승인하는 변경은 작고 되돌릴 수 있게 하고, 주문이 요구하지 "
        "않은 변경은 넣지 마세요. 판정에는 승인한 항목, 보류한 항목과 그 이유를 분명히 적으세요."),
    ("expert", "ops"): (
        "추측하지 말고 먼저 ops 블록으로 대상 서버를 점검해 사실을 확인하세요. 주장마다 [#번호 조회 호스트]로 근거를 "
        "인용하세요. 주문이 모호하면 점검 결과로 현황을 보고하고, 가장 합리적인 해석으로 구체적인 안을 가정과 함께 "
        "내놓으세요."),
    ("critic", "ops"): "이의는 [차단]과 [권고] 둘로 나눠 적으세요. 모든 주장에 [#번호 조회 호스트]로 근거를 인용하세요.",
    ("judge", "ops"): (
        "비판의 [권고]는 경고나 확인 사항(checks)으로 적고, [차단]만 조치를 바꾸거나 빼게 하세요. 사용자가 시키지 않은 "
        "금지 조건('~하지 않는다')을 새로 만들지 마세요. 확인되지 않은 수치나 지어낸 인용에 근거한 조치는 승인하지 "
        "마세요. 사용자가 정해야 할 값은 하나만 묻고 권장 기본값을 함께 쓰세요."),
}


def card(seat: str, kind: Optional[str] = None) -> str:
    """The card of a seat of a card kind ('' for a seat without one)."""
    return CARDS.get((seat, kind)) or CARDS.get((seat, None)) or ""


def home_seat(persona: Mapping[str, Any]) -> Optional[str]:
    """The seat a persona opted into role cards with, or None (no card: the persona is sent as written)."""
    seat = persona.get("role_card")
    return seat if seat in CARD_SEATS else None


def card_kind(persona: Mapping[str, Any]) -> Optional[str]:
    """The kind of cards a persona gets: its role_card_kind, or None (the code cards)."""
    kind = persona.get("role_card_kind")
    return kind if kind in CARD_KINDS else None


def persona_text(persona: Mapping[str, Any]) -> str:
    """What reaches the model for a persona: its own text (`character.body_text`), then the card of its home seat and
    card kind when it opted in. The seat a team gives it does not change the card."""
    text = character.body_text(persona)
    home = home_seat(persona)
    if home is None:
        return text
    extra = card(home, card_kind(persona))
    if not text:
        return extra
    if not extra:
        return text
    # a character's block ends in list lines, so its card starts on its own line; format 1 keeps the single space
    joiner = "\n" if character.is_character(persona) else " "
    return f"{text}{joiner}{extra}"

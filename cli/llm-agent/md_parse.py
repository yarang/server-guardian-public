"""
Markdown parsing shared by the Office exports (docs/design/office-export.md): a line scanner that turns any text into
blocks, and an inline scanner that hands text pieces and their emphasis to a renderer. `office_docx` and `office_pptx`
render the same blocks, so a document reads the same in both.

The parser accepts any text: whatever it cannot place is a paragraph, so malformed Markdown never raises. The scanners
avoid backtracking patterns and bound every look-ahead (or remember a failed search), because the text is
agent-written and the converters run on the hub.

Blocks are tuples: ("heading", level, text), ("para", text), ("item", level, marker, text) with the marker already
literal ("\u2022", "1." or a task mark), ("quote", text), ("code", [lines]), ("rule",) and
("table", header, aligns, rows).
"""

import re
from typing import Callable, Dict, List, Optional, Tuple

MAX_INLINE_DEPTH = 6
MAX_LIST_LEVEL = 8
LABEL_WINDOW = 500  # longest link label / URL looked for

_HEADING_OPEN = re.compile(r"#{1,6}[ \t]+")
_FENCE = re.compile(r"[ ]{0,3}(`{3,}|~{3,})(.*)")
_RULE = re.compile(r"[ ]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
_BULLET = re.compile(r"([ \t]*)([-*+])[ \t]+(.*)")
_NUMBERED = re.compile(r"([ \t]*)(\d{1,9})([.)])[ \t]+(.*)")
_QUOTE = re.compile(r"[ ]{0,3}>[ ]?(.*)")
_DELIMITER_CELL = re.compile(r":?-+:?")
_TASK = re.compile(r"\[([ xX])\][ \t]+(.*)")

Format = Tuple[bool, bool, bool, bool]  # bold, italic, strike, code
PLAIN: Format = (False, False, False, False)
Block = Tuple


# --- inline --------------------------------------------------------------------------------------

def _find_close(text: str, start: int, marker: str) -> int:
    """Index of the closing `marker` for an emphasis that opened before `start`, or -1."""
    position = start
    while True:
        position = text.find(marker, position)
        if position < 0:
            return -1
        if text[position - 1:position] == "\\":
            position += 1
            continue
        # A longer run of the same character is a different marker (`**` while looking for `*`).
        if len(marker) == 1 and text[position + 1:position + 2] == marker:
            position += 2
            continue
        if position > start and not text[position - 1].isspace():
            return position
        position += len(marker)


def _link_target(text: str, start: int) -> Optional[Tuple[str, int]]:
    """For `(` at `start`: the URL and the index after its `)` (one level of parentheses allowed), else None."""
    window = start + LABEL_WINDOW
    end = text.find(")", start, window)
    if end < 0:
        return None
    if "(" in text[start + 1:end]:  # an address with parentheses: take the next closer
        end = text.find(")", end + 1, window)
        if end < 0:
            return None
    url = text[start + 1:end]
    if "\n" in url:
        return None
    return url, end + 1



def inline(text: str, emit: Callable[[str, Format], str], fmt: Format = PLAIN, depth: int = 0) -> str:
    """Inline Markdown as the concatenation of `emit(text, format)` pieces (the converter decides what a run is)."""
    out: List[str] = []
    buffer: List[str] = []
    dead: Dict[str, int] = {}  # marker -> a start from which no closer exists (a later start cannot find one either)

    def flush() -> None:
        if buffer:
            out.append(emit("".join(buffer), fmt))
            buffer.clear()

    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n and not text[i + 1].isalnum() and not text[i + 1].isspace():
            buffer.append(text[i + 1])
            i += 2
            continue
        if char == "`":
            ticks = n - i - len(text[i:].lstrip("`"))
            run = "`" * ticks
            end = -1 if dead.get(run, n + 1) <= i else text.find(run, i + ticks)
            while end >= 0 and text[end + ticks:end + ticks + 1] == "`":
                end = text.find(run, end + ticks + 1)
            if end > i + ticks:
                flush()
                out.append(emit(text[i + ticks:end].strip(), (fmt[0], fmt[1], fmt[2], True)))
                i = end + ticks
                continue
            dead[run] = i
            buffer.append(run)
            i += ticks
            continue
        if char in "![" and depth < MAX_INLINE_DEPTH:
            is_image = char == "!"
            open_at = i + 1 if is_image else i
            if text[open_at:open_at + 1] == "[":
                close = text.find("]", open_at + 1, open_at + LABEL_WINDOW)
                target = _link_target(text, close + 1) if close > 0 and text[close + 1:close + 2] == "(" else None
                if target and "[" not in text[open_at + 1:close]:
                    label = text[open_at + 1:close]
                    url = target[0].split(None, 1)[0] if target[0].strip() else ""
                    flush()
                    if is_image:
                        out.append(emit(label or "[image]", (fmt[0], True, fmt[2], fmt[3])))
                    else:
                        out.append(inline(label, emit, fmt, depth + 1))
                        if url and url != label:
                            out.append(emit(f" ({url})", fmt))
                    i = target[1]
                    continue
        if char in "*_~" and depth < MAX_INLINE_DEPTH:
            marker = next((m for m in ("***", "___", "**", "__", "~~", "*", "_") if text.startswith(m, i)), "")
            after = text[i + len(marker):i + len(marker) + 1]
            # An underscore inside a word (snake_case) is not emphasis.
            intraword = marker[:1] == "_" and i > 0 and text[i - 1].isalnum()
            if marker and after and not after.isspace() and not intraword and dead.get(marker, n + 1) > i:
                close = _find_close(text, i + len(marker), marker)
                if close > 0:
                    flush()
                    bold, italic, strike, code = fmt
                    if marker in ("***", "___"):
                        bold, italic = True, True
                    elif marker in ("**", "__"):
                        bold = True
                    elif marker == "~~":
                        strike = True
                    else:
                        italic = True
                    out.append(inline(text[i + len(marker):close], emit, (bold, italic, strike, code), depth + 1))
                    i = close + len(marker)
                    continue
                dead[marker] = i
            buffer.append(marker or char)
            i += len(marker) or 1
            continue
        buffer.append(char)
        i += 1
    flush()
    return "".join(out)


# --- blocks --------------------------------------------------------------------------------------

def _heading(line: str) -> Optional[Tuple[int, str]]:
    """(level, text) of an ATX heading line, else None. A closing run of # is dropped."""
    if len(line) - len(line.lstrip()) >= 4:
        return None
    body = line.lstrip()
    opener = _HEADING_OPEN.match(body)
    if not opener:
        return None
    text = body[opener.end():].rstrip()
    cut = len(text)
    while cut > 0 and text[cut - 1] == "#":
        cut -= 1
    if 0 < cut < len(text) and text[cut - 1] in " \t":
        text = text[:cut].rstrip()
    return opener.group(0).count("#"), text


def _split_row(line: str) -> List[str]:
    """Cells of a table row; `\\|` is a literal pipe, the outer pipes are optional."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells, current, i = [], [], 0
    while i < len(text):
        if text[i] == "\\" and text[i + 1:i + 2] == "|":
            current.append("|")
            i += 2
            continue
        if text[i] == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(text[i])
        i += 1
    cells.append("".join(current).strip())
    return cells


def _delimiter(line: str) -> Optional[List[str]]:
    """Alignments (left / center / right) when `line` is a table delimiter row, else None."""
    if "|" not in line:
        return None
    cells = _split_row(line)
    if not cells or not all(_DELIMITER_CELL.fullmatch(cell) for cell in cells):
        return None
    return ["center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left"
            for c in cells]



def parse_blocks(lines: List[str]) -> List[Block]:
    """The blocks of a text given as lines (no line ends)."""
    out: List[Block] = []
    paragraph: List[str] = []
    stack: List[int] = []  # indentation of the open list levels
    item: Optional[List] = None  # [level, marker, [text lines]] of the list item still collecting continuation lines
    quote: List[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(("para", " ".join(part.strip() for part in paragraph)))
            paragraph.clear()

    def flush_item() -> None:
        nonlocal item
        if item:
            marker, text = item[1], " ".join(item[2])
            task = _TASK.fullmatch(text)
            if task:
                marker, text = ("☑" if task.group(1) in "xX" else "☐"), task.group(2)
            out.append(("item", item[0], marker, text))
        item = None

    def flush_quote() -> None:
        if quote:
            out.append(("quote", " ".join(quote)))
            quote.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_item()
        flush_quote()

    i, total = 0, len(lines)
    while i < total:
        line = lines[i].expandtabs(4)
        stripped = line.strip()
        fence = _FENCE.fullmatch(line)
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            flush_all()
            stack.clear()
            marker = fence.group(1)
            body: List[str] = []
            i += 1
            while i < total:
                closing = lines[i].strip()
                if closing.startswith(marker[0] * len(marker)) and closing.strip(marker[0]) == "":
                    break
                body.append(lines[i].expandtabs(4))
                i += 1
            i += 1  # the closing fence (an unclosed fence runs to the end of the text)
            out.append(("code", body or [""]))
            continue
        if not stripped:
            flush_paragraph()
            flush_quote()
            flush_item()
            if not (i + 1 < total and (_BULLET.fullmatch(lines[i + 1]) or _NUMBERED.fullmatch(lines[i + 1]))):
                stack.clear()
            i += 1
            continue
        heading = _heading(line)
        if heading:
            flush_all()
            stack.clear()
            out.append(("heading", heading[0], heading[1]))
            i += 1
            continue
        if _RULE.fullmatch(line):
            flush_all()
            stack.clear()
            out.append(("rule",))
            i += 1
            continue
        if "|" in line and i + 1 < total:
            aligns = _delimiter(lines[i + 1])
            header = _split_row(line)
            if aligns is not None and len(aligns) == len(header):
                flush_all()
                stack.clear()
                rows = []
                i += 2
                while i < total and lines[i].strip() and "|" in lines[i]:
                    rows.append(_split_row(lines[i]))
                    i += 1
                out.append(("table", header, aligns, rows))
                continue
        quoted = _QUOTE.fullmatch(line)
        if quoted:
            flush_paragraph()
            flush_item()
            quote.append(quoted.group(1).strip())
            i += 1
            continue
        bullet, numbered = _BULLET.fullmatch(line), _NUMBERED.fullmatch(line)
        if bullet or numbered:
            flush_paragraph()
            flush_quote()
            flush_item()
            indent = len(line) - len(line.lstrip())
            while stack and stack[-1] > indent:
                stack.pop()
            if not stack or stack[-1] < indent:
                stack.append(indent)
            level = min(len(stack) - 1, MAX_LIST_LEVEL)
            if bullet:
                item = [level, "•", [bullet.group(3).strip()]]
            elif numbered:
                item = [level, numbered.group(2) + numbered.group(3), [numbered.group(4).strip()]]
            i += 1
            continue
        if item and line.startswith(" "):
            item[2].append(stripped)
            i += 1
            continue
        flush_item()
        flush_quote()
        stack.clear()
        paragraph.append(line)
        i += 1
    flush_all()
    return out

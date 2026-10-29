"""
Markdown to PPTX with the standard library (docs/design/office-export.md, D3).

`markdown_to_pptx(text)` returns the bytes of a minimal PresentationML package: a slide master with two layouts (Title
Slide, Title and Content), a theme and one part per slide. The text is parsed by `md_parse` (shared with the DOCX
converter) and laid out by these rules:

- A first-level heading that opens the document is the deck title: a title slide whose subtitle is the first short
  paragraph. Every other first- and second-level heading starts a slide; deeper headings stay inside their slide as bold
  lines. Text before the first heading goes on an untitled slide.
- A slide never overflows silently: text is measured in line units (a Korean or other wide glyph counts two) and a
  section that does not fit continues on further slides titled `<title> (2)`, `<title> (3)`. Nothing is cut; a long
  paragraph is split at whitespace, a code block between lines, a table between rows (the header row repeats).
- Tables get slides of their own. Images become their alt text and links text with the URL, as in the DOCX export.

The package has no macros, no hyperlink, picture or other external relationship, and the same text always gives the
same bytes. A document that needs more than MAX_SLIDES slides is refused, never truncated. `check_package` runs over
the result with this format's part limit.
"""

import math
import unicodedata
from functools import partial
from typing import List, NamedTuple, Optional, Tuple

from md_parse import PLAIN, Format, inline, parse_blocks
from office_ooxml import (NS_CONTENT_TYPES, NS_RELS, REL_OFFICE_DOCUMENT, OfficeError, build_package, check_package,
                          clean_text, escape, xml_part)

MAIN_PART = "ppt/presentation.xml"
MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MAX_SLIDES = 60
FIXED_PARTS = 11  # content types, root rels, presentation + rels, master + rels, two layouts + rels, theme
MAX_PARTS = FIXED_PARTS + 2 * MAX_SLIDES  # every slide adds its part and its rels
MAX_TABLE_COLUMNS = 20
TABLE_ROWS = 8  # data rows on one table slide
BODY_LINES = 11  # line budget of the body placeholder (20 pt text)
LINE_UNITS = 70  # width units of one body line; a wide glyph is two units
CODE_UNITS = 90  # width units of one 14 pt monospace line
CODE_LINE_COST = 0.75  # a code line is smaller than a body line
TITLE_MAX = 100
SUBTITLE_MAX = 160
_MAX_UNITS_PER_SLIDE = int(BODY_LINES / CODE_LINE_COST)  # the most lines any slide can hold

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS = f'xmlns:a="{_A}" xmlns:r="{_R}" xmlns:p="{_P}"'
_CT = "application/vnd.openxmlformats-officedocument."
_STEP = 285750  # indent of one list level, EMU
_TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"
_ROW_HEIGHT = 370840
_CONTENT_WIDTH = 10972800
_CODE: Format = (False, False, False, True)
_BOLD: Format = (True, False, False, False)
_ITALIC: Format = (False, True, False, False)


class _Slide(NamedTuple):
    layout: int  # 1 title slide, 2 title and content
    title: str
    paras: List[str]  # a:p elements of the body (the subtitle on a title slide)
    table: str  # a graphicFrame, or ""


_Unit = Tuple[float, str]  # (line cost, a:p)


# --- measuring and splitting -----------------------------------------------------------------------

def _char_width(char: str) -> int:
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _lines(text: str, units: int) -> int:
    return max(1, math.ceil(_width(text) / units))


def _split_text(text: str, budget: int) -> List[str]:
    """Pieces of at most `budget` width units. A cut falls at whitespace when one lies in the second half of the
    window, else inside the word; only the blank at a cut is dropped. One pass: the cost is linear in the text."""
    pieces: List[str] = []
    start, size = 0, len(text)
    while start < size:
        if text[start].isspace():
            start += 1
            continue
        used, index, space = 0, start, -1
        while index < size:
            used += _char_width(text[index])
            if used > budget:
                break
            index += 1
            if text[index - 1].isspace():
                space = index - 1
        else:
            pieces.append(text[start:].rstrip())
            break
        end = space if space > start + (index - start) // 2 else index
        pieces.append(text[start:end].rstrip())
        start = end
    return pieces


def _plain(text: str) -> str:
    return inline(text, lambda piece, _fmt: piece).strip()


def _title_text(text: str) -> str:
    plain = _plain(text)
    return plain if len(plain) <= TITLE_MAX else plain[:TITLE_MAX - 1].rstrip() + "…"


def _numbered(title: str, number: int) -> str:
    return f"{title} ({number})" if title and number > 1 else title


# --- text ------------------------------------------------------------------------------------------

def _run(text: str, fmt: Format = PLAIN, size: int = 0) -> str:
    text = text.replace("\n", " ")
    if not text:
        return ""
    bold, italic, strike, code = fmt
    attrs = (f' sz="{size}"' if size else "") + (' b="1"' if bold else "") + (' i="1"' if italic else "") \
        + (' strike="sngStrike"' if strike else "")
    if code:
        font = '<a:latin typeface="Courier New"/><a:cs typeface="Courier New"/>'
        return f"<a:r><a:rPr{attrs}>{font}</a:rPr><a:t>{escape(text)}</a:t></a:r>"
    return f"<a:r><a:rPr{attrs}/><a:t>{escape(text)}</a:t></a:r>"


def _ppr(mode: str, level: int = 0) -> str:
    """Paragraph properties. `bullet` inherits the master bullet; `hang` has none and starts where the bullet would (for
    a literal marker); `cont` continues an item's text; `flat` is plain; `code` is flat without space above."""
    if mode == "bullet":
        return f'<a:pPr lvl="{level}"/>'
    if mode == "hang":
        return f'<a:pPr lvl="{level}"><a:buNone/></a:pPr>'
    if mode == "cont":
        return f'<a:pPr marL="{_STEP * (level + 1)}" indent="0" lvl="{level}"><a:buNone/></a:pPr>'
    spacing = '<a:spcBef><a:spcPts val="0"/></a:spcBef>' if mode == "code" else ""
    return f'<a:pPr marL="0" indent="0">{spacing}<a:buNone/></a:pPr>'


def _para(text: str, mode: str, level: int = 0, fmt: Format = PLAIN) -> str:
    return f"<a:p>{_ppr(mode, level)}{inline(text, _run, fmt)}</a:p>"


def _text_units(text: str, level: int, first: str, rest: str, fmt: Format = PLAIN) -> List[_Unit]:
    """The paragraphs of one text. Text longer than a slide is split; only the first piece keeps the `first` style."""
    width = max(LINE_UNITS - 4 * level, 30)
    pieces = _split_text(text, (BODY_LINES - 1) * width) or [""]
    return [(float(_lines(piece, width)), _para(piece, rest if index else first, level, fmt))
            for index, piece in enumerate(pieces)]


def _code_pieces(line: str) -> List[str]:
    budget = (BODY_LINES - 1) * CODE_UNITS
    if _width(line) <= budget:
        return [line]
    indent = line[:len(line) - len(line.lstrip())]
    pieces = _split_text(line, budget) or [""]
    pieces[0] = indent + pieces[0]
    return pieces


def _code_units(lines: List[str]) -> List[_Unit]:
    out: List[_Unit] = []
    for line in lines:
        for piece in _code_pieces(line):
            runs = _run(piece, _CODE, size=1400)
            out.append((CODE_LINE_COST * _lines(piece, CODE_UNITS), f"<a:p>{_ppr('code')}{runs}</a:p>"))
    return out


def _units(block: tuple) -> List[_Unit]:
    kind = block[0]
    if kind == "heading":
        return _text_units(block[2], 0, "flat", "flat", _BOLD) if block[2].strip() else []
    if kind == "para":
        return _text_units(block[1], 0, "flat", "flat")
    if kind == "quote":
        return _text_units(block[1], 0, "flat", "flat", _ITALIC)
    if kind == "item":
        _, level, marker, text = block
        if marker == "•":
            return _text_units(text, level, "bullet", "cont")
        return _text_units(f"{marker} {text}", level, "hang", "cont")
    if kind == "code":
        return _code_units(block[1])
    return []


# --- tables ----------------------------------------------------------------------------------------

def _border(side: str) -> str:
    return (f'<a:ln{side} w="12700" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:srgbClr val="999999"/>'
            f'</a:solidFill><a:prstDash val="solid"/></a:ln{side}>')


def _cell(text: str, align: str, head: bool) -> str:
    algn = {"center": "ctr", "right": "r"}.get(align, "l")
    runs = inline(text, partial(_run, size=1400), (head, False, False, False))
    paragraph = f'<a:p><a:pPr algn="{algn}"/>{runs or "<a:endParaRPr sz=" + chr(34) + "1400" + chr(34) + "/>"}</a:p>'
    fill = '<a:solidFill><a:srgbClr val="E7E6E6"/></a:solidFill>' if head else "<a:noFill/>"
    borders = "".join(_border(side) for side in ("L", "R", "T", "B"))
    return (f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>{paragraph}</a:txBody><a:tcPr marL="91440" marR="91440" '
            f'marT="45720" marB="45720" anchor="ctr">{borders}{fill}</a:tcPr></a:tc>')


def _table_frame(header: List[str], aligns: List[str], rows: List[List[str]], columns: int) -> str:
    width = _CONTENT_WIDTH // columns
    body = []
    for index, row in enumerate([header] + rows):
        cells = "".join(_cell(row[c] if c < len(row) else "", aligns[c] if c < len(aligns) else "left", index == 0)
                        for c in range(columns))
        body.append(f'<a:tr h="{_ROW_HEIGHT}">{cells}</a:tr>')
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="Table 2"/><p:cNvGraphicFramePr>'
            '<a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
            f'<p:xfrm><a:off x="609600" y="1463040"/><a:ext cx="{width * columns}" cy="{_ROW_HEIGHT * len(body)}"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{_TABLE_URI}"><a:tbl><a:tblPr firstRow="1"/><a:tblGrid>'
            + f'<a:gridCol w="{width}"/>' * columns + f'</a:tblGrid>{"".join(body)}</a:tbl></a:graphicData></a:graphic>'
            '</p:graphicFrame>')


def _table_frames(header: List[str], aligns: List[str], rows: List[List[str]]) -> List[str]:
    columns = max([len(header)] + [len(row) for row in rows])
    if columns > MAX_TABLE_COLUMNS:
        raise OfficeError(f"a table with {columns} columns does not fit a slide (limit {MAX_TABLE_COLUMNS})")
    chunks = [rows[i:i + TABLE_ROWS] for i in range(0, len(rows), TABLE_ROWS)] or [[]]
    return [_table_frame(header, aligns, chunk, columns) for chunk in chunks]


# --- slides ----------------------------------------------------------------------------------------

def _too_many() -> OfficeError:
    return OfficeError(f"the document needs more than {MAX_SLIDES} slides; split it into several documents")


def _pack(units: List[_Unit]) -> List[List[str]]:
    """Consecutive paragraphs per slide, each slide within the line budget (one oversized paragraph stands alone)."""
    groups: List[List[str]] = []
    current: List[str] = []
    used = 0.0
    for cost, xml in units:
        if current and used + cost > BODY_LINES:
            groups.append(current)
            current, used = [], 0.0
        current.append(xml)
        used += cost
    if current:
        groups.append(current)
    return groups


def _section_slides(title: str, blocks: List[tuple], number: int = 1) -> List[_Slide]:
    """The slides of one section; `number` is the position of its first slide (2 when the title slide came first)."""
    slides: List[_Slide] = []
    pending: List[_Unit] = []

    def flush() -> None:
        nonlocal number
        for group in _pack(pending):
            slides.append(_Slide(2, _numbered(title, number), group, ""))
            number += 1
        pending.clear()

    for block in blocks:
        if block[0] != "table":
            pending.extend(_units(block))
            if len(pending) > MAX_SLIDES * _MAX_UNITS_PER_SLIDE:
                raise _too_many()
            continue
        flush()
        for frame in _table_frames(block[1], block[2], block[3]):
            slides.append(_Slide(2, _numbered(title, number), [], frame))
            number += 1
    flush()
    if not slides and number == 1 and title:
        slides.append(_Slide(2, title, [], ""))  # a heading with nothing under it is still a slide
    if len(slides) > MAX_SLIDES:
        raise _too_many()
    return slides


def _subtitle(intro: List[tuple]) -> Tuple[List[str], List[tuple]]:
    """The first intro paragraph as the subtitle when it is short, and the intro that is left."""
    if intro and intro[0][0] == "para" and _width(_plain(intro[0][1])) <= SUBTITLE_MAX:
        return [_para(intro[0][1], "flat")], intro[1:]
    return [], intro


def _deck(blocks: List[tuple]) -> List[_Slide]:
    blocks = [block for block in blocks if block[0] != "rule"]
    head = ""
    if blocks and blocks[0][:2] == ("heading", 1):
        head, blocks = _title_text(blocks[0][2]), blocks[1:]
        if not head:  # a blank first heading is no deck title: its text is a section like any other
            blocks = [("heading", 1, "")] + blocks
    intro: List[tuple] = []
    sections: List[Tuple[str, List[tuple]]] = []
    for block in blocks:
        if block[0] == "heading" and block[1] <= 2:
            sections.append((_title_text(block[2]), []))
        elif sections:
            sections[-1][1].append(block)
        else:
            intro.append(block)
    slides: List[_Slide] = []
    if head:
        subtitle, intro = _subtitle(intro)
        slides.append(_Slide(1, head, subtitle, ""))
        slides.extend(_section_slides(head, intro, 2))
    else:
        slides.extend(_section_slides("", intro))
    for title, body in sections:
        slides.extend(_section_slides(title, body))
        if len(slides) > MAX_SLIDES:
            raise _too_many()
    return slides


def _title_shape(title: str, kind: str) -> str:
    return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>'
            f'<p:ph type="{kind}"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r>'
            f'<a:t>{escape(title)}</a:t></a:r></a:p></p:txBody></p:sp>')


def _body_shape(paras: List[str], subtitle: bool) -> str:
    holder = '<p:ph type="subTitle" idx="1"/>' if subtitle else '<p:ph idx="1"/>'
    return ('<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>'
            f'{holder}</p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/>'
            f'{"".join(paras)}</p:txBody></p:sp>')


def _slide_xml(slide: _Slide) -> bytes:
    shapes = _title_shape(slide.title, "ctrTitle" if slide.layout == 1 else "title") if slide.title else ""
    if slide.table:
        shapes += slide.table
    elif slide.paras:
        shapes += _body_shape(slide.paras, slide.layout == 1)
    return xml_part(f'<p:sld {_NS}><p:cSld><p:spTree>{_TREE_HEAD}{shapes}</p:spTree></p:cSld>'
                    '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


# --- master, layouts, theme ------------------------------------------------------------------------

_TREE_HEAD = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
              '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def _placeholder(ident: int, name: str, holder: str, text: str, frame: Optional[Tuple[int, int, int, int]] = None,
                 body_pr: str = "<a:bodyPr/>", styles: str = "<a:lstStyle/>") -> str:
    geometry = ""
    if frame:
        x, y, cx, cy = frame
        geometry = (f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>')
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{ident}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr>{holder}</p:nvPr></p:nvSpPr><p:spPr>{geometry}</p:spPr><p:txBody>{body_pr}{styles}'
            f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')


def _levels(tag: str, sizes: List[int], extra: str, bullet: bool) -> str:
    out = []
    for n, size in enumerate(sizes, start=1):
        spacing = '<a:lnSpc><a:spcPct val="100000"/></a:lnSpc><a:spcBef><a:spcPts val="600"/></a:spcBef>' if bullet else ""
        indent = f'marL="{_STEP * n}" indent="-{_STEP}"' if bullet else 'marL="0"'
        out.append(f'<a:lvl{n}pPr {indent} algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1">'
                   f'{spacing}{extra}<a:defRPr sz="{size}" kern="1200"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'
                   '<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr>'
                   f'</a:lvl{n}pPr>')
    return f"<p:{tag}>{''.join(out)}</p:{tag}>"


def _master() -> bytes:
    title_style = ('<p:titleStyle><a:lvl1pPr algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" '
                   'hangingPunct="1"><a:lnSpc><a:spcPct val="90000"/></a:lnSpc><a:spcBef><a:spcPct val="0"/></a:spcBef>'
                   '<a:buNone/><a:defRPr sz="2800" b="1" kern="1200"><a:solidFill><a:schemeClr val="tx2"/></a:solidFill>'
                   '<a:latin typeface="+mj-lt"/><a:ea typeface="+mj-ea"/><a:cs typeface="+mj-cs"/></a:defRPr>'
                   '</a:lvl1pPr></p:titleStyle>')
    body_style = _levels("bodyStyle", [2000, 1800] + [1600] * 7, '<a:buFont typeface="Arial"/><a:buChar char="•"/>', True)
    other_style = _levels("otherStyle", [1800] * 9, "", False)
    shapes = (_placeholder(2, "Title Placeholder 1", '<p:ph type="title"/>', "Click to edit Master title style",
                           (609600, 365760, _CONTENT_WIDTH, 1005840), '<a:bodyPr anchor="ctr"><a:normAutofit/></a:bodyPr>')
              + _placeholder(3, "Text Placeholder 2", '<p:ph type="body" idx="1"/>', "Click to edit Master text styles",
                             (609600, 1463040, _CONTENT_WIDTH, 4754880), "<a:bodyPr><a:normAutofit/></a:bodyPr>"))
    colors = " ".join(f'{n}="{n}"' for n in ("accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"))
    return xml_part(
        f'<p:sldMaster {_NS}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/>'
        f'</p:bgPr></p:bg><p:spTree>{_TREE_HEAD}{shapes}</p:spTree></p:cSld>'
        f'<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" {colors}/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/><p:sldLayoutId id="2147483650" r:id="rId2"/>'
        f'</p:sldLayoutIdLst><p:txStyles>{title_style}{body_style}{other_style}</p:txStyles></p:sldMaster>')


def _layout(kind: str, name: str, shapes: str) -> bytes:
    return xml_part(f'<p:sldLayout {_NS} type="{kind}" preserve="1"><p:cSld name="{name}"><p:spTree>{_TREE_HEAD}{shapes}'
                    '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')


def _layouts() -> Tuple[bytes, bytes]:
    title = _placeholder(
        2, "Title 1", '<p:ph type="ctrTitle"/>', "Click to edit Master title style", (914400, 2130425, 10363200, 1470025),
        '<a:bodyPr anchor="b"><a:normAutofit/></a:bodyPr>', '<a:lstStyle><a:lvl1pPr algn="ctr"><a:defRPr sz="4000"/></a:lvl1pPr></a:lstStyle>')
    subtitle = _placeholder(
        3, "Subtitle 2", '<p:ph type="subTitle" idx="1"/>', "Click to edit Master subtitle style",
        (914400, 3708400, 10363200, 1752600), "<a:bodyPr><a:normAutofit/></a:bodyPr>",
        '<a:lstStyle><a:lvl1pPr marL="0" indent="0" algn="ctr"><a:buNone/><a:defRPr sz="2400"/></a:lvl1pPr></a:lstStyle>')
    plain_title = _placeholder(2, "Title 1", '<p:ph type="title"/>', "Click to edit Master title style")
    content = _placeholder(3, "Content Placeholder 2", '<p:ph idx="1"/>', "Click to edit Master text styles")
    return _layout("title", "Title Slide", title + subtitle), _layout("obj", "Title and Content", plain_title + content)


def _theme() -> bytes:
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    line = ('<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>')
    colors = {"dk1": "000000", "lt1": "FFFFFF", "dk2": "44546A", "lt2": "E7E6E6", "accent1": "4472C4", "accent2": "ED7D31",
              "accent3": "A5A5A5", "accent4": "FFC000", "accent5": "5B9BD5", "accent6": "70AD47", "hlink": "0563C1",
              "folHlink": "954F72"}
    scheme = "".join(f'<a:{name}><a:srgbClr val="{value}"/></a:{name}>' for name, value in colors.items())
    fonts = '<a:latin typeface="Calibri"/><a:ea typeface="Malgun Gothic"/><a:cs typeface=""/>'
    return xml_part(
        f'<a:theme xmlns:a="{_A}" name="Guardian"><a:themeElements><a:clrScheme name="Guardian">{scheme}</a:clrScheme>'
        f'<a:fontScheme name="Guardian"><a:majorFont>{fonts}</a:majorFont><a:minorFont>{fonts}</a:minorFont></a:fontScheme>'
        f'<a:fmtScheme name="Guardian"><a:fillStyleLst>{fill * 3}</a:fillStyleLst><a:lnStyleLst>'
        + "".join(line.format(w=w) for w in (6350, 12700, 19050)) + '</a:lnStyleLst><a:effectStyleLst>'
        + "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3 + f'</a:effectStyleLst><a:bgFillStyleLst>{fill * 3}'
        '</a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


# --- package ---------------------------------------------------------------------------------------

def _rels(*items: Tuple[str, str, str]) -> bytes:
    body = "".join(f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>' for rid, kind, target in items)
    return xml_part(f'<Relationships xmlns="{NS_RELS}">{body}</Relationships>')


def _content_types(count: int) -> bytes:
    def override(part: str, kind: str) -> str:
        return f'<Override PartName="/{part}" ContentType="{_CT}{kind}"/>'

    slides = "".join(override(f"ppt/slides/slide{n}.xml", "presentationml.slide+xml") for n in range(1, count + 1))
    return xml_part(
        f'<Types xmlns="{NS_CONTENT_TYPES}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + override("ppt/presentation.xml", "presentationml.presentation.main+xml")
        + override("ppt/slideMasters/slideMaster1.xml", "presentationml.slideMaster+xml")
        + override("ppt/slideLayouts/slideLayout1.xml", "presentationml.slideLayout+xml")
        + override("ppt/slideLayouts/slideLayout2.xml", "presentationml.slideLayout+xml")
        + override("ppt/theme/theme1.xml", "theme+xml") + slides + "</Types>")


def _presentation(count: int) -> bytes:
    ids = "".join(f'<p:sldId id="{256 + n}" r:id="rId{3 + n}"/>' for n in range(count))
    return xml_part(
        f'<p:presentation {_NS}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'<p:sldIdLst>{ids}</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
        '</p:presentation>')


def _parts(slides: List[_Slide]) -> List[Tuple[str, bytes]]:
    layout_one, layout_two = _layouts()
    parts = [
        ("[Content_Types].xml", _content_types(len(slides))),
        ("_rels/.rels", _rels(("rId1", REL_OFFICE_DOCUMENT, MAIN_PART))),
        (MAIN_PART, _presentation(len(slides))),
        ("ppt/_rels/presentation.xml.rels", _rels(
            ("rId1", f"{_R}/slideMaster", "slideMasters/slideMaster1.xml"), ("rId2", f"{_R}/theme", "theme/theme1.xml"),
            *[(f"rId{3 + n}", f"{_R}/slide", f"slides/slide{n + 1}.xml") for n in range(len(slides))])),
        ("ppt/slideMasters/slideMaster1.xml", _master()),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", _rels(
            ("rId1", f"{_R}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", f"{_R}/slideLayout", "../slideLayouts/slideLayout2.xml"), ("rId3", f"{_R}/theme", "../theme/theme1.xml"))),
        ("ppt/slideLayouts/slideLayout1.xml", layout_one),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _rels(("rId1", f"{_R}/slideMaster", "../slideMasters/slideMaster1.xml"))),
        ("ppt/slideLayouts/slideLayout2.xml", layout_two),
        ("ppt/slideLayouts/_rels/slideLayout2.xml.rels", _rels(("rId1", f"{_R}/slideMaster", "../slideMasters/slideMaster1.xml"))),
        ("ppt/theme/theme1.xml", _theme()),
    ]
    for number, slide in enumerate(slides, start=1):
        parts.append((f"ppt/slides/slide{number}.xml", _slide_xml(slide)))
        parts.append((f"ppt/slides/_rels/slide{number}.xml.rels",
                      _rels(("rId1", f"{_R}/slideLayout", f"../slideLayouts/slideLayout{slide.layout}.xml"))))
    return parts


def markdown_to_pptx(text: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    """The PPTX package for a Markdown text. Raises OfficeError when there is no text for a slide, when it needs more than
    MAX_SLIDES slides or a table wider than MAX_TABLE_COLUMNS, when the result is above max_bytes or fails the check."""
    slides = _deck(parse_blocks(clean_text(text).split("\n")))
    if not slides:
        raise OfficeError("the document has no text for a slide")
    result = build_package(_parts(slides), max_bytes)
    problems = check_package(result, MAIN_PART, MAX_PARTS)
    if problems:
        raise OfficeError("the converted file failed its structure check: " + "; ".join(problems[:3]))
    return result

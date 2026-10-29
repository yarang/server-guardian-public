"""
Markdown to DOCX with the standard library (docs/design/office-export.md, D2).

`markdown_to_docx(text)` returns the bytes of a minimal WordprocessingML package: headings, paragraphs, bullet and
numbered lists (a literal marker and a hanging indent, no numbering part), block quotes, fenced code, tables and
bold / italic / strike / code spans. It writes no hyperlink and no image relationship: a link stays text with its
target shown, an image becomes its alt text. The package has no macros and no external relationship, and the same text
always produces the same bytes.

The text is parsed by `md_parse` (shared with the PPTX converter), which accepts any text: malformed Markdown yields
a valid document or an OfficeError, never a broken package. `check_package` runs over the result.
"""

from typing import List

from md_parse import PLAIN, Format, inline, parse_blocks
from office_ooxml import (NS_CONTENT_TYPES, NS_RELS, REL_OFFICE_DOCUMENT, OfficeError, build_package, check_package,
                          clean_text, escape, xml_part)

MAIN_PART = "word/document.xml"
MAX_PARTS = 64  # the format is a handful of parts; the shared default is plenty
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_TABLE_COLUMNS = 60  # Word's own limit is 63
CONTENT_WIDTH = 9026  # twips between the margins of an A4 page with 2.54 cm margins

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# --- inline --------------------------------------------------------------------------------------

def _run(text: str, fmt: Format = PLAIN) -> str:
    """One w:r. Tabs and line breaks become their elements; the text keeps its spaces."""
    bold, italic, strike, code = fmt
    rpr = ""
    if code:
        rpr += '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:eastAsia="Courier New" w:cs="Courier New"/>'
    if bold:
        rpr += "<w:b/><w:bCs/>"
    if italic:
        rpr += "<w:i/><w:iCs/>"
    if strike:
        rpr += "<w:strike/>"
    if code:
        rpr += '<w:sz w:val="20"/><w:szCs w:val="20"/><w:shd w:val="clear" w:color="auto" w:fill="EDEDED"/>'
    pieces = []
    for index, line in enumerate(clean_text(text).split("\n")):
        if index:
            pieces.append("<w:br/>")
        for position, chunk in enumerate(line.split("\t")):
            if position:
                pieces.append("<w:tab/>")
            if chunk:
                pieces.append(f'<w:t xml:space="preserve">{escape(chunk)}</w:t>')
    if not pieces:
        return ""
    return f"<w:r>{'<w:rPr>' + rpr + '</w:rPr>' if rpr else ''}{''.join(pieces)}</w:r>"

def _inline(text: str, fmt: Format = PLAIN) -> str:
    return inline(text, _run, fmt)


# --- blocks --------------------------------------------------------------------------------------

def _paragraph(content: str, style: str = "", ppr_extra: str = "", ppr_late: str = "") -> str:
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + ppr_extra + ppr_late
    return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{content}</w:p>"


def _table(header: List[str], aligns: List[str], rows: List[List[str]]) -> str:
    columns = min(max([len(header)] + [len(row) for row in rows]), MAX_TABLE_COLUMNS)
    width = CONTENT_WIDTH // columns
    borders = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
                      for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="{width * columns}" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders>'
           '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="100" w:type="dxa"/>'
           '<w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>'
           + f'<w:gridCol w:w="{width}"/>' * columns + "</w:tblGrid>"]
    for index, row in enumerate([header] + rows):
        head = index == 0
        out.append("<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if head else ""))
        for column in range(columns):  # ragged rows are padded to the widest one
            text = row[column] if column < len(row) else ""
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if head else ""
            align = aligns[column] if column < len(aligns) else "left"
            jc = f'<w:jc w:val="{align}"/>' if align != "left" else ""
            content = _inline(text, (head, False, False, False))
            # A cell always holds a paragraph, even an empty one: Word refuses a w:tc without it.
            out.append(f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}</w:tcPr>'
                       + _paragraph(content, "TableText", ppr_late=jc) + "</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    # Two tables in a row would merge in Word, and a body may not end in a table: a paragraph always follows.
    return "".join(out) + _paragraph("", "TableText")


def _list_item(level: int, marker: str, text: str) -> str:
    ind = f'<w:ind w:left="{360 * (level + 2)}" w:hanging="360"/>'
    return _paragraph(_run(marker) + "<w:r><w:tab/></w:r>" + _inline(text), "ListParagraph", ppr_late=ind)


def _render(block: tuple) -> List[str]:
    kind = block[0]
    if kind == "heading":
        return [_paragraph(_inline(block[2]), f"Heading{block[1]}")]
    if kind == "para":
        return [_paragraph(_inline(block[1]))]
    if kind == "item":
        return [_list_item(block[1], block[2], block[3])]
    if kind == "quote":
        return [_paragraph(_inline(block[1]), "Quote")]
    if kind == "code":
        return [_paragraph(_run(line) if line else "", "CodeBlock") for line in block[1]]
    if kind == "rule":
        return [_paragraph("", "", '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/></w:pBdr>')]
    return [_table(block[1], block[2], block[3])]


def _blocks(lines: List[str]) -> List[str]:
    return [xml for block in parse_blocks(lines) for xml in _render(block)]



# --- package -------------------------------------------------------------------------------------

def _styles() -> str:
    def heading(level: int, size: int) -> str:
        return (f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/>'
                '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/>'
                f'<w:spacing w:before="{360 if level < 3 else 240}" w:after="120"/><w:outlineLvl w:val="{level - 1}"/></w:pPr>'
                f'<w:rPr><w:b/><w:bCs/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>')

    sizes = {1: 40, 2: 32, 3: 28, 4: 24, 5: 22, 6: 22}
    return (
        f'<w:styles xmlns:w="{_W}"><w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Malgun Gothic" w:cs="Calibri"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US" w:eastAsia="ko-KR" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
        '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/>'
        '<w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
        '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/>'
        '<w:semiHidden/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
        '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
        + "".join(heading(level, size) for level, size in sizes.items())
        + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
        '<w:qFormat/><w:pPr><w:spacing w:after="60"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="BBBBBB"/></w:pBdr><w:ind w:left="360"/></w:pPr>'
        '<w:rPr><w:i/><w:iCs/><w:color w:val="555555"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/><w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
        '<w:ind w:left="120"/></w:pPr><w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:eastAsia="Courier New" '
        'w:cs="Courier New"/><w:sz w:val="19"/><w:szCs w:val="19"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        '<w:pPr><w:spacing w:before="40" w:after="40" w:line="240" w:lineRule="auto"/></w:pPr></w:style></w:styles>')


def markdown_to_docx(text: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    """The DOCX package for a Markdown text. Raises OfficeError when the result is above max_bytes or fails the check."""
    blocks = _blocks(clean_text(text).split("\n")) or [_paragraph("")]
    section = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
               'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')
    document = f'<w:document xmlns:w="{_W}"><w:body>{"".join(blocks)}{section}</w:body></w:document>'
    parts = [
        ("[Content_Types].xml", xml_part(
            f'<Types xmlns="{NS_CONTENT_TYPES}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')),
        ("_rels/.rels", xml_part(
            f'<Relationships xmlns="{NS_RELS}"><Relationship Id="rId1" Type="{REL_OFFICE_DOCUMENT}" Target="word/document.xml"/></Relationships>')),
        ("word/document.xml", xml_part(document)),
        ("word/_rels/document.xml.rels", xml_part(
            f'<Relationships xmlns="{NS_RELS}"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')),
        ("word/styles.xml", xml_part(_styles())),
    ]
    result = build_package(parts, max_bytes)
    problems = check_package(result, MAIN_PART)
    if problems:
        raise OfficeError("the converted file failed its structure check: " + "; ".join(problems[:3]))
    return result

"""
Artifacts of design projects: validators and the immutable version store (docs/design/design-project.md, P2).

A design debate ends in text (Markdown, Mermaid, SVG). The judge proposes `jev_mcp:write_artifact`; a person approves it;
the approved text becomes a new version of the thread's artifacts under
<GUARDIAN_DATA_DIR>/design-artifacts/<project>/<thread>/vN/. A version is a full snapshot (the files of the previous
version plus the written one), is never changed once stored, and is described by manifest.json.

Validation is deterministic and standard-library only: L1 (structure and security) and L2 (references and accessibility)
errors block the write, warnings are shown on the approval screen. Decision 11: no quota, a 2 MiB safety limit per
text file, and versions that are not the latest of their thread expire after artifacts.retention_days (prune()),
leaving their manifest as a tombstone.
"""

import calendar
import contextlib
import hashlib
import html
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

TOOL = "jev_mcp:write_artifact"
MANIFEST_VERSION = 1
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,80}")  # always used with fullmatch
EXTENSIONS = {".md": "markdown", ".mmd": "mermaid", ".svg": "svg"}
MIME = {"markdown": "text/markdown; charset=utf-8", "mermaid": "text/plain; charset=utf-8", "svg": "image/svg+xml"}
# Derived files (docs/design/office-export.md): made by convert() from a stored Markdown file, never written through
# write_artifact, so they are not in EXTENSIONS and never pass the text validators.
DERIVED_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
CONVERT_TARGETS = tuple(DERIVED_MIME)  # office_<target>.py provides markdown_to_<target>, MAIN_PART and MAX_PARTS
DEFAULT_MAX_BYTES = 262144
DEFAULT_SAFETY_BYTES = 2 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
MAX_TITLE = 200
_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_VERSION_DIR = re.compile(r"v([1-9][0-9]{0,8})")
_BASE_VERSION = re.compile(r"(?:artifact@)?v([1-9][0-9]{0,8})")


class ArtifactError(ValueError):
    """An artifact write that is refused; the message is shown on the approval screen."""


class StaleBase(ArtifactError):
    """base_version is not the thread's latest version."""


# --- paths ---------------------------------------------------------------------------------------

def data_root() -> Path:
    return Path(os.environ.get("GUARDIAN_DATA_DIR", "~/.guardian/data")).expanduser()


def artifacts_root() -> Path:
    """<GUARDIAN_DATA_DIR>/design-artifacts: user content, part of the nightly backup. Creates nothing."""
    return data_root() / "design-artifacts"


def safe_id(value: Any) -> str:
    cleaned = _ID_CHARS.sub("_", str(value or "")).strip(".")
    return cleaned[:80]


def safety_limit() -> int:
    """GUARDIAN_ARTIFACT_MAX_FILE_BYTES: the L1 size limit of one text file (default 2 MiB)."""
    try:
        value = int(os.environ.get("GUARDIAN_ARTIFACT_MAX_FILE_BYTES", ""))
    except ValueError:
        return DEFAULT_SAFETY_BYTES
    return value if value > 0 else DEFAULT_SAFETY_BYTES


def artifact_kind(name: str) -> Optional[str]:
    """markdown, mermaid or svg by the file name's extension; None for any other."""
    return EXTENSIONS.get(os.path.splitext(name)[1].lower())


def format_version(number: int) -> str:
    return f"artifact@v{number}"


def parse_base_version(raw: Any) -> Optional[int]:
    """`artifact@v3` (or `v3`) -> 3; empty or None -> None (the thread has no version yet)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    match = _BASE_VERSION.fullmatch(str(raw).strip())
    if not match:
        raise ArtifactError(f"base_version must look like artifact@v3, got {str(raw)[:40]!r}")
    return int(match.group(1))


# --- validation ----------------------------------------------------------------------------------

@dataclass
class Report:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, layer: str, message: str) -> None:
        self.errors.append(f"{layer}: {message}")

    def warn(self, layer: str, message: str) -> None:
        self.warnings.append(f"{layer}: {message}")

    def as_dict(self) -> Dict[str, List[str]]:
        return {"errors": list(self.errors), "warnings": list(self.warnings)}


# Credential shapes that must never be stored in an artifact a dashboard user can download.
_SECRET_SHAPES = (
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("an API secret key", re.compile(r"\b(?:sk|rk)[-_](?:live[-_]|test[-_])?[A-Za-z0-9_-]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Tailscale key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{10,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)


def find_secret(text: str) -> Optional[str]:
    """What kind of credential the text looks like, or None. Entity-encoded text (&#65;KIA...) is checked decoded too."""
    candidates = (text, html.unescape(text)) if "&" in text else (text,)
    for candidate in candidates:
        for label, pattern in _SECRET_SHAPES:
            if pattern.search(candidate):
                return label
    return None


# C0/C1 controls (except tab, newline, carriage return) and bidirectional overrides: they can rewrite what the
# approval screen and an editor show.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")  # CommonMark: at most three spaces, tabs do not open a fence
_HEADING = re.compile(r"^(#{1,6})\s+\S")
_IMAGE = re.compile(r"!\[([^\]\n]{0,500})\]\(([^)\s]{0,2000})")
_LINK = re.compile(r"(?<!!)\[[^\]\n]{0,500}\]\(([^)\s]{1,2000})")
_SCAN_LINE = 4000  # the image and link checks look at the first characters of a line only, which bounds their cost
_MD_SCRIPT = re.compile(r"<\s*/?\s*(script|iframe|object|embed|foreignobject|style|meta|form|link|base)\b", re.I)
_BACKTICKS = re.compile(r"`+")
_HTML_TAG = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9:-]*)(?:\s[^<>\n]*|/)?>")
_ATTRIBUTELESS = re.compile(r"</?\s*[a-zA-Z0-9-]+\s*/?>")
# Raw HTML in Markdown and Mermaid labels is refused except these inert tags written without attributes.
_INERT_TAGS = {"br", "hr", "sup", "sub", "kbd", "details", "summary", "b", "i", "em", "strong", "code", "p", "u", "s",
               "del", "ins", "mark", "small"}
_SCHEME_BAD = ("javascript:", "vbscript:", "data:text/html", "data:application", "data:image/svg")


def _fence_scan(text: str) -> Tuple[List[Tuple[int, str, bool]], bool]:
    """([(line number, line, inside)], unterminated) of a Markdown text.

    inside is True for fence lines and fenced content. Fence rules follow CommonMark: the opening line has at most three
    spaces of indent and, for backticks, no backtick in its info string; the closing line uses the same character, at
    least as many of them, and nothing else. A fence that is still open at the end runs to the end of the text."""
    marker = ""
    lines: List[Tuple[int, str, bool]] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _FENCE.fullmatch(line)
        if not marker:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                marker = match.group(1)
                lines.append((number, line, True))
            else:
                lines.append((number, line, False))
        else:
            lines.append((number, line, True))
            if match and match.group(1)[0] == marker[0] and len(match.group(1)) >= len(marker) and not match.group(2).strip():
                marker = ""
    return lines, bool(marker)


def printable(text: str) -> str:
    """The text with control and bidirectional-override characters replaced by U+FFFD, for the approval screen."""
    return _CONTROL_CHARS.sub("\ufffd", text)


def _strip_code_spans(line: str) -> str:
    """The line without its inline code spans: a run of N backticks closes at the next run of exactly N (linear)."""
    runs = [(m.start(), m.end()) for m in _BACKTICKS.finditer(line)]
    if len(runs) < 2:
        return line
    next_same = [-1] * len(runs)
    last: Dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        length = runs[index][1] - runs[index][0]
        next_same[index] = last.get(length, -1)
        last[length] = index
    out: List[str] = []
    cursor = 0
    index = 0
    while index < len(runs):
        close = next_same[index]
        if close == -1:
            index += 1
            continue
        out.append(line[cursor:runs[index][0]])
        cursor = runs[close][1]
        index = close + 1
    out.append(line[cursor:])
    return "".join(out)


def _scheme_text(text: str) -> str:
    """Text as a browser reads a URL scheme: entities decoded, whitespace and control characters dropped."""
    return re.sub(r"[\x00-\x20\x7f-\x9f]", "", html.unescape(text)).lower()


def _html_problem(text: str) -> Optional[str]:
    """Why a line of Markdown or a Mermaid label holds markup that is not inert, or None."""
    decoded = html.unescape(text)
    if _MD_SCRIPT.search(decoded):
        return "script-like tags (script, iframe, style, meta, form, ...)"
    squeezed = _scheme_text(decoded)
    if any(bad in squeezed for bad in _SCHEME_BAD):
        return "a script or data URL"
    for tag in _HTML_TAG.finditer(decoded):
        if tag.group(1).lower() not in _INERT_TAGS or not _ATTRIBUTELESS.fullmatch(tag.group(0)):
            return f"raw HTML <{tag.group(1)}> (use Markdown; only simple tags such as <br> are allowed)"
    return None


_EXTERNAL_TARGET = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#|/|\.\.?/)", re.I)


def _unknown_target(target: str, known: Optional[Sequence[str]]) -> Optional[str]:
    """The artifact name a relative link or image target points at when the thread has no such artifact."""
    if known is None or _EXTERNAL_TARGET.match(target):
        return None
    path = target.split("#", 1)[0]
    return path if path and path not in known else None


def _heading_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().rstrip("#").strip()).casefold()


def _check_markdown(text: str, report: Report, known: Optional[Sequence[str]],
                    required: Optional[Sequence[str]] = None) -> None:
    lines, unterminated = _fence_scan(text)
    present = set()
    if unterminated:
        report.error("L1", "a code fence is never closed")
    previous = 0
    reported = False
    for number, line, inside in lines:
        if inside:
            continue
        problem = _html_problem(_strip_code_spans(line))
        if problem and not reported:
            report.error("L1", f"line {number} contains {problem}")
            reported = True
        heading = _HEADING.match(line)
        if heading:
            present.add(_heading_key(line[len(heading.group(1)):]))
            level = len(heading.group(1))
            if previous and level > previous + 1:
                report.warn("L2", f"line {number}: heading level jumps from {previous} to {level}")
            previous = level
        head = line[:_SCAN_LINE]
        for alt, target in _IMAGE.findall(head):
            if not alt.strip():
                report.error("L2", f"line {number}: image {target[:60]!r} has no alt text")
            missing = _unknown_target(target, known)
            if missing:
                report.warn("L2", f"line {number}: image {missing!r}, which is not an artifact of this thread")
        for target in _LINK.findall(head):
            missing = _unknown_target(target, known)
            if missing:
                report.warn("L2", f"line {number}: link to {missing!r}, which is not an artifact of this thread")
    for section in required or ():
        if _heading_key(section) not in present:
            report.error("L2", f"the required section {section!r} is missing (artifacts.required_sections)")


_MERMAID_STARTS = (
    "flowchart", "graph", "sequencediagram", "classdiagram", "statediagram", "erdiagram", "journey", "gantt", "pie",
    "quadrantchart", "requirementdiagram", "gitgraph", "mindmap", "timeline", "c4context", "c4container",
    "c4component", "c4dynamic", "c4deployment", "sankey-beta", "xychart-beta", "block-beta", "architecture-beta",
)
# Interaction directives start a statement (after a newline or a semicolon); init directives, front matter config and
# the securityLevel key can loosen Mermaid's own sandbox, so they are refused wherever they appear.
_MERMAID_DIRECTIVE = re.compile(r"(?:^|;)[ \t]*(click|callback|link|links)\b", re.I | re.M)
_MERMAID_CONFIG = re.compile(r"%%\s*\{|securityLevel|^[ \t]*config[ \t]*:|^[ \t]*(?:theme|flowchart)Config[ \t]*:|"
                             r"\bon[a-z]+[ \t]*=", re.I | re.M)
_PAIRS = {")": "(", "]": "[", "}": "{"}


def _mermaid_body(text: str) -> List[str]:
    """The statements of a Mermaid text without a leading front matter block and without %% comment lines."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        lines = lines[end + 1:] if end is not None else lines
    return [line for line in lines if line.strip() and not line.strip().startswith("%%")]


def _brackets_balanced(line: str) -> bool:
    stack: List[str] = []
    quote = ""
    for char in line:
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in _PAIRS:
            if not stack or stack.pop() != _PAIRS[char]:
                return False
    return not stack and not quote


def _check_mermaid(text: str, report: Report) -> None:
    body = _mermaid_body(text)
    if not body:
        report.error("L1", "the diagram is empty")
        return
    first = body[0].strip().lower()
    if not any(first == start or first.startswith(start) and not first[len(start):len(start) + 1].isalnum()
               for start in _MERMAID_STARTS):
        report.error("L1", f"the first statement {body[0].strip()[:40]!r} is not a Mermaid diagram type")
    if _MERMAID_DIRECTIVE.search(text):
        report.error("L1", "click/callback/link directives are not allowed")
    if _MERMAID_CONFIG.search(text):
        report.error("L1", "init directives, config blocks, securityLevel and event handlers are not allowed")
    problem = next((p for p in (_html_problem(line) for line in body) if p), None)
    if problem:
        report.error("L1", f"the diagram contains {problem}")
    for number, line in enumerate(body, 1):
        if not _brackets_balanced(line):
            report.error("L1", f"statement {number} has unbalanced brackets or quotes: {line.strip()[:60]!r}")
            break


_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
# Allow list of SVG elements; animation elements (animate, set, ...) can rewrite href and are left out on purpose.
_SVG_ELEMENTS = {"svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
                 "text", "tspan", "textpath", "use", "symbol", "marker", "lineargradient", "radialgradient", "stop",
                 "clippath", "mask", "pattern", "filter", "style", "a", "switch", "image", "metadata"}
_SVG_DOCTYPE = re.compile(r"<!\s*(DOCTYPE|ENTITY)|<\?xml-stylesheet", re.I)
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n]?|(.))", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.S)
_CSS_URL = re.compile(r"url\(\s*['\"]?([^)'\"]*)")
_CSS_BAD = ("@import", "image-set(", "-moz-binding", "behavior:", "expression(", "javascript:", "vbscript:", "data:text/html",
            "cross-fade(", "element(", "src(", "@font-face", "@namespace")


def _css_text(value: str) -> str:
    """CSS as the browser reads it: escapes decoded, comments and whitespace squeezed, lowercase."""
    def unescape(match):
        if match.group(1):
            try:
                return chr(int(match.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return match.group(2)
    value = _CSS_ESCAPE.sub(unescape, value)
    return re.sub(r"\s+", "", _CSS_COMMENT.sub("", value)).lower()


def _css_problems(value: str, refs: List[str]) -> Optional[str]:
    """A reason the CSS (a style element or attribute) is refused, or None; #id url() references go into refs."""
    css = _css_text(value)
    bad = next((b for b in _CSS_BAD if b in css), None)
    if bad:
        return f"{bad!r}"
    for target in _CSS_URL.findall(css):
        if target.startswith("#"):
            refs.append(target[1:])
        elif target:
            return "an external url()"
    return None


def _check_svg(text: str, report: Report) -> None:
    if _SVG_DOCTYPE.search(text):
        report.error("L1", "DOCTYPE, ENTITY and xml-stylesheet are not allowed")
        return
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError) as exc:
        report.error("L1", f"not well-formed XML: {exc}")
        return
    if root.tag != f"{{{_SVG_NS}}}svg":
        report.error("L1", f"the root element must be <svg xmlns=\"{_SVG_NS}\">")
        return
    ids: Dict[str, int] = {}
    refs: List[str] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue  # comments and processing instructions are never rendered
        namespace, _, name = element.tag[1:].partition("}") if element.tag.startswith("{") else ("", "", element.tag)
        name = name.lower()
        if namespace != _SVG_NS:
            report.error("L1", f"<{name}> is outside the SVG namespace; only SVG elements are allowed")
            continue
        if name not in _SVG_ELEMENTS and not name.startswith("fe"):
            report.error("L1", f"<{name}> is not an allowed SVG element")
        if name == "style":
            problem = _css_problems("".join(element.itertext()), refs)
            if problem:
                report.error("L1", f"<style> contains {problem}")
        for key, value in element.attrib.items():
            namespace, _, attr = key[1:].partition("}") if key.startswith("{") else ("", "", key)
            attr = attr.lower()
            if namespace not in ("", _XLINK_NS, _XML_NS):
                report.error("L1", f"attribute {key[:60]!r} is in a foreign namespace")
                continue
            if attr.startswith("on") or attr in ("srcdoc", "xml:base", "base") or namespace == _XML_NS and attr == "base":
                report.error("L1", f"attribute {attr!r} is not allowed")
            squeezed = _scheme_text(value)
            if any(bad in squeezed for bad in _SCHEME_BAD):
                report.error("L1", f"attribute {attr!r} holds a script or data URL")
            if attr == "href":
                if value.startswith("#"):
                    refs.append(value[1:])
                else:
                    report.error("L1", f"external reference {value[:60]!r}: only #id references are allowed")
            if attr in ("style", "fill", "stroke", "filter", "clip-path", "mask", "marker-start", "marker-mid", "marker-end",
                        "cursor") or "url(" in value.lower() or "\\" in value:
                problem = _css_problems(value, refs)
                if problem:
                    report.error("L1", f"attribute {attr!r} contains {problem}")
            if attr == "id":
                ids[value] = ids.get(value, 0) + 1
    for ident, count in ids.items():
        if count > 1:
            report.error("L2", f"id {ident!r} is used {count} times")
    for ref in dict.fromkeys(refs):
        if ref not in ids:
            report.error("L2", f"reference to #{ref}, which no element defines")
    if not any(isinstance(c.tag, str) and c.tag == f"{{{_SVG_NS}}}title" for c in root):
        report.warn("L2", "the SVG has no <title>; add one for screen readers")


def validate(name: str, content: str, types: Sequence[str], max_bytes: int = DEFAULT_MAX_BYTES,
             known: Optional[Sequence[str]] = None, required_sections: Optional[Sequence[str]] = None) -> Report:
    """L1 and L2 checks of one artifact text.

    known: the artifact names of the thread, to check relative links; None (the thread is not known, e.g. at
    proposal time) skips the link and image-target checks. required_sections: headings this Markdown file must
    contain at any level (a missing one is an L2 error); the project's artifacts.required_sections names them per file."""
    report = Report()
    if not NAME_PATTERN.fullmatch(name or ""):
        report.error("L1", "name must match ^[a-z0-9][a-z0-9._-]{0,80}$ (lowercase, no directories)")
        return report
    kind = artifact_kind(name)
    if kind is None:
        report.error("L1", f"name must end in one of {', '.join(sorted(EXTENSIONS))}")
        return report
    if kind not in types:
        report.error("L1", f"this project works on {', '.join(types)}, not {kind}")
    size = len(content.encode("utf-8"))
    if size == 0:
        report.error("L1", "the content is empty")
    limit = min(max_bytes, safety_limit())
    if size > max_bytes:
        report.error("L1", f"{size} bytes exceed the project's artifacts.max_bytes ({max_bytes})")
    if size > safety_limit():
        report.error("L1", f"{size} bytes exceed the safety limit of {safety_limit()} bytes per file")
    if size > limit or report.errors and kind not in types:
        return report  # nothing is parsed or scanned that is already refused for its size or type
    if _CONTROL_CHARS.search(content):
        report.error("L1", "the content contains control or bidirectional-override characters")
    secret = find_secret(content)
    if secret:
        report.error("L1", f"the content looks like it contains {secret}; artifacts can be downloaded, so it is refused")
    if kind == "markdown":
        _check_markdown(content, report, known, required_sections)
    elif kind == "mermaid":
        _check_mermaid(content, report)
    else:
        _check_svg(content, report)
    return report


def check_arguments(arguments: Dict[str, Any], project: Any = None, known: Optional[Sequence[str]] = None) -> Report:
    """Checks the arguments of a write_artifact action against the project's artifact settings.

    Raises ArtifactError for a malformed base_version or title; the content's own findings are in the Report."""
    settings = getattr(project, "artifacts", None) or {}
    types = settings.get("types") or list(EXTENSIONS.values())
    max_bytes = int(settings.get("max_bytes") or DEFAULT_MAX_BYTES)
    parse_base_version(arguments.get("base_version"))
    title = arguments.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE or not title.isprintable()):
        raise ArtifactError(f"title must be one printable line of at most {MAX_TITLE} characters")
    if title and find_secret(title):
        raise ArtifactError("the title looks like it contains a credential")
    name = str(arguments.get("name", ""))
    required = (settings.get("required_sections") or {}).get(name)
    return validate(name, str(arguments.get("content", "")), types, max_bytes, known, required)


# --- storage -------------------------------------------------------------------------------------

def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(stamp: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return time.time()  # an unreadable stamp never makes a version look old


def _private_dir(path: Path) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)


def _convert_markdown(name: str, data: bytes, target: str) -> bytes:
    """The Office package for a stored Markdown file, checked as a package. Raises ArtifactError."""
    import importlib

    import office_ooxml

    module = importlib.import_module(f"office_{target}")
    try:
        result = getattr(module, f"markdown_to_{target}")(data.decode("utf-8"), max_bytes=safety_limit())
    except (UnicodeDecodeError, office_ooxml.OfficeError) as exc:
        raise ArtifactError(f"{name} cannot be converted: {exc}") from None
    problems = office_ooxml.check_package(result, module.MAIN_PART, module.MAX_PARTS)
    if problems:
        raise ArtifactError(f"{name} produced an invalid package: {'; '.join(problems[:3])}")
    return result


def _atomic_json(path: Path, data: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def project_lock(project_id: str, root: Optional[Path] = None) -> Iterator[None]:
    """artifacts/<project>/.lock, held while versions of the project are written or pruned (blocking)."""
    base = (root or artifacts_root()) / safe_id(project_id)
    _private_dir(base)
    fd = os.open(base / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


class ArtifactStore:
    """The versions of one thread of one design project."""

    def __init__(self, project_id: str, thread_id: str, root: Optional[Path] = None) -> None:
        self.project_id = safe_id(project_id)
        self.thread_id = safe_id(thread_id)
        if not self.project_id or not self.thread_id:
            raise ArtifactError("the project and the thread need usable ids")
        self.root = root or artifacts_root()
        self.directory = self.root / self.project_id / self.thread_id

    def versions(self) -> List[int]:
        """Numbers of the complete versions (a directory with a manifest), ascending."""
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return []
        found = []
        for entry in entries:
            match = _VERSION_DIR.fullmatch(entry)
            if match and (self.directory / entry / "manifest.json").is_file():
                found.append(int(match.group(1)))
        return sorted(found)

    def latest(self) -> Optional[int]:
        found = self.versions()
        return found[-1] if found else None

    def manifest(self, version: int) -> Optional[Dict[str, Any]]:
        try:
            with open(self.directory / f"v{version}" / "manifest.json", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # A manifest is data on disk; a path in it is trusted only when it is a plain artifact file name.
        files = data.get("files")
        data["files"] = [f for f in files if isinstance(f, dict) and isinstance(f.get("path"), str)
                         and NAME_PATTERN.fullmatch(f["path"])] if isinstance(files, list) else []
        return data

    def names(self, version: Optional[int] = None) -> List[str]:
        """File names of a version (default: the latest) that still have their content."""
        number = self.latest() if version is None else version
        manifest = self.manifest(number) if number else None
        return [f["path"] for f in (manifest or {}).get("files", []) if not f.get("expired")]

    def read(self, version: int, name: str) -> Optional[str]:
        """The text of one file of a version; None when the version or file is unknown or has expired."""
        if not NAME_PATTERN.fullmatch(name or "") or artifact_kind(name) is None:
            return None  # a derived binary file is not text
        manifest = self.manifest(version)
        if not manifest or name not in {f["path"] for f in manifest.get("files", []) if not f.get("expired")}:
            return None
        try:
            return (self.directory / f"v{version}" / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def check_base(self, base_version: Optional[str]) -> Optional[int]:
        """Returns the latest version number after checking that base_version names it. Raises StaleBase."""
        base = parse_base_version(base_version)
        latest = self.latest()
        if latest is None:
            if base is not None:
                raise StaleBase(f"base_version {format_version(base)} does not exist: this thread has no version yet")
        elif base != latest:
            shown = format_version(base) if base is not None else "no base_version"
            raise StaleBase(f"{shown} is not the latest version of this thread; it is now {format_version(latest)}. "
                            "Debate again on the current text")
        return latest

    def write(self, name: str, content: str, *, project: Any = None, base_version: Optional[str] = None,
              title: str = "", agent: str = "", stage: str = "final") -> Dict[str, Any]:
        """Stores content as `name` in a new version (the previous version's files plus this one). Returns the manifest.

        Under the project lock: the base_version check, the validation, the temporary directory and the rename that
        makes the version appear all happen while no other write of the project can run. Existing versions are
        never touched. Raises ArtifactError (StaleBase for a moved base)."""
        with project_lock(self.project_id, self.root):
            latest = self.check_base(base_version)
            known = [n for n in self.names(latest)] if latest else []
            report = check_arguments({"name": name, "content": content, "base_version": base_version, "title": title},
                                     project, known)
            if not report.ok:
                raise ArtifactError("; ".join(report.errors))
            number = (latest or 0) + 1
            files = []
            if latest:
                files = [dict(f) for f in (self.manifest(latest) or {}).get("files", [])
                         if f["path"] != name and f.get("source") != name and not f.get("expired")]
            data = content.encode("utf-8")
            kind = artifact_kind(name) or ""
            entry = {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "mime": MIME[kind],
                     "kind": kind, "stage": stage, "agent": agent[:120], "title": title}
            files.append(entry)
            files.sort(key=lambda f: f["path"])
            manifest = {"manifest_version": MANIFEST_VERSION, "version": number, "project": self.project_id,
                        "thread": self.thread_id, "created_at": utc_now(), "parent": latest, "written": name,
                        "files": files}
            _private_dir(self.directory)
            staging = Path(tempfile.mkdtemp(dir=self.directory, prefix=".tmp-v"))
            try:
                for item in files:
                    if item["path"] == name:
                        (staging / name).write_bytes(data)
                    else:
                        (staging / item["path"]).write_bytes((self.directory / f"v{latest}" / item["path"]).read_bytes())
                    os.chmod(staging / item["path"], 0o600)
                _atomic_json(staging / "manifest.json", manifest)
                os.chmod(staging, 0o700)
                os.rename(staging, self.directory / f"v{number}")
            except BaseException:
                _remove_tree(staging)
                raise
            return {**manifest, "warnings": report.warnings}

    def convert(self, version: int, name: str, target: str = "docx") -> Dict[str, Any]:
        """Adds a derived Office file to a stored version: `name` (Markdown) -> `<stem>.<target>`. Returns the new entry.

        The source is re-read and checked against the manifest (sha256 and size) before it is used; the result is
        checked as a package, written beside its source in the same version directory and listed in the manifest with
        the source's name and sha256. The version is otherwise unchanged and an existing file is never overwritten.
        Raises ArtifactError. Runs under the project lock."""
        if target not in CONVERT_TARGETS:
            raise ArtifactError(f"cannot convert to {target!r}; the supported formats are {', '.join(CONVERT_TARGETS)}")
        if not NAME_PATTERN.fullmatch(name or "") or artifact_kind(name) != "markdown":
            raise ArtifactError("only a Markdown (.md) file can be converted")
        derived = f"{name[:-len('.md')]}.{target}"
        if not NAME_PATTERN.fullmatch(derived):
            raise ArtifactError("the converted file name would be too long")
        with project_lock(self.project_id, self.root):
            manifest = self.manifest(version)
            if not manifest:
                raise ArtifactError(f"{format_version(version)} does not exist")
            files = manifest.get("files", [])
            source = next((f for f in files if f["path"] == name), None)
            if source is None or source.get("expired"):
                raise ArtifactError(f"{name} is not available in {format_version(version)}")
            if any(f["path"] == derived for f in files):
                raise ArtifactError(f"{derived} already exists in {format_version(version)}")
            folder = self.directory / f"v{version}"
            data, digest = self._read_source(folder, name, source)
            result = _convert_markdown(name, data, target)
            entry = {"path": derived, "sha256": hashlib.sha256(result).hexdigest(), "bytes": len(result),
                     "mime": DERIVED_MIME[target], "kind": target, "stage": source.get("stage", "final"),
                     "agent": source.get("agent", ""), "title": source.get("title", ""),
                     "source": name, "source_sha256": digest, "converted_at": utc_now()}
            self._store_derived(folder, entry, result, format_version(version))
            return entry

    @staticmethod
    def _read_source(folder: Path, name: str, source: Dict[str, Any]) -> Tuple[bytes, str]:
        """The bytes of a stored source file and their sha256, once they match the manifest entry."""
        try:
            data = (folder / name).read_bytes()
        except OSError:
            raise ArtifactError(f"{name} cannot be read") from None
        digest = hashlib.sha256(data).hexdigest()
        if digest != source.get("sha256") or len(data) != source.get("bytes"):
            raise ArtifactError(f"{name} does not match its manifest entry; refusing to convert it")
        if len(data) > DEFAULT_MAX_BYTES:
            raise ArtifactError(f"{name} is {len(data)} bytes, above the {DEFAULT_MAX_BYTES} byte conversion limit")
        return data, digest

    @staticmethod
    def _store_derived(folder: Path, entry: Dict[str, Any], result: bytes, label: str) -> None:
        """Writes a derived file next to its source (never over an existing name) and lists it in the manifest; the file
        is removed again when the manifest cannot be written."""
        derived = entry["path"]
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix="." + entry["kind"])
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(result)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.link(tmp, folder / derived)  # fails when the name appeared meanwhile: never an overwrite
            os.unlink(tmp)
        except FileExistsError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise ArtifactError(f"{derived} already exists in {label}") from None
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        try:
            raw = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            raw["files"] = sorted([*raw.get("files", []), entry], key=lambda f: str(f.get("path")))
            _atomic_json(folder / "manifest.json", raw)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(folder / derived)
            raise

    def prune(self, retention_days: int, now: Optional[float] = None, dry_run: bool = False) -> List[int]:
        """Expires the versions older than retention_days that are not the latest; returns their numbers.
        dry_run only reports them.

        An expired version keeps its manifest as a tombstone (sha256, size, expiry date per file) and loses its
        files. 0 days keeps everything. Call it under project_lock."""
        if retention_days <= 0:
            return []
        moment = time.time() if now is None else now
        found = self.versions()
        expired = []
        for number in found[:-1]:  # the latest version is never pruned
            manifest = self.manifest(number)
            if not manifest or manifest.get("expired_at"):
                continue
            if moment - _epoch(str(manifest.get("created_at"))) < retention_days * 86400:
                continue
            if dry_run:
                expired.append(number)
                continue
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
            folder = self.directory / f"v{number}"
            for item in manifest.get("files", []):
                with contextlib.suppress(OSError):
                    os.unlink(folder / item["path"])
                item["expired"] = True
            manifest["expired_at"] = stamp
            _atomic_json(folder / "manifest.json", manifest)
            expired.append(number)
        return expired


def _remove_tree(path: Path) -> None:
    with contextlib.suppress(OSError):
        for child in path.iterdir():
            with contextlib.suppress(OSError):
                child.unlink()
        path.rmdir()


def prune_all(retention_for: Any, now: Optional[float] = None, root: Optional[Path] = None,
              dry_run: bool = False) -> Dict[str, List[str]]:
    """Prunes every thread of every project directory. retention_for(project_id) -> days (None: skip the project).

    Returns {project: ["thread:vN", ...]} for what expired (dry_run: for what would expire, nothing is changed).
    Leftover .tmp-v* directories of a crashed write are removed."""
    base = root or artifacts_root()
    result: Dict[str, List[str]] = {}
    try:
        projects = sorted(p for p in os.listdir(base) if (base / p).is_dir() and not (base / p).is_symlink())
    except OSError:
        return result
    for project_id in projects:
        days = retention_for(project_id)
        if days is None:
            continue
        with project_lock(project_id, base):
            for thread in sorted(os.listdir(base / project_id)):
                directory = base / project_id / thread
                if directory.is_symlink() or not directory.is_dir():
                    continue
                if not dry_run:
                    for leftover in directory.glob(".tmp-v*"):
                        if time.time() - leftover.stat().st_mtime > 3600:
                            _remove_tree(leftover)
                for number in ArtifactStore(project_id, thread, base).prune(int(days), now, dry_run):
                    result.setdefault(project_id, []).append(f"{thread}:v{number}")
    return result

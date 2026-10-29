"""
Shared parts of the Office exports (docs/design/office-export.md): XML text hygiene, a deterministic zip writer and a
structural check of a finished package. Standard library only.

An Office file written here is a derived file: it is built from a stored Markdown version, is macro-free, carries no
external relationship and is never read back. `check_package` is the gate every converter result passes before the
store keeps it, so a malformed source can at worst be refused, never stored as a broken package.
"""

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, List, Sequence, Tuple

NS_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"

# XML 1.0 forbids these code points in any text, escaped or not; lone surrogates cannot be encoded at all.
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_FIXED_STAMP = (1980, 1, 1, 0, 0, 0)  # the same source always yields the same bytes, so the same sha256

MAX_PARTS = 64
MAX_UNCOMPRESSED = 64 * 1024 * 1024


class OfficeError(ValueError):
    """A conversion that is refused; the message is shown to the person who pressed the button."""


def clean_text(text: str) -> str:
    """Text for an XML node: line ends normalised, characters XML 1.0 cannot carry removed."""
    return _ILLEGAL_XML.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def escape(text: str) -> str:
    return clean_text(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_part(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body).encode("utf-8")


def build_package(parts: Sequence[Tuple[str, bytes]], max_bytes: int) -> bytes:
    """A zip with the parts in the given order ([Content_Types].xml first), fixed timestamps, deflate."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time=_FIXED_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(info, data)
    result = buffer.getvalue()
    if len(result) > max_bytes:
        raise OfficeError(f"the converted file would be {len(result)} bytes, above the {max_bytes} byte limit")
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def check_package(data: bytes, main_part: str, max_parts: int = MAX_PARTS) -> List[str]:
    """Structural problems of an OOXML package; an empty list means it passed.

    Checked: the zip is intact with plain unique member names; no VBA or embedded objects; [Content_Types].xml covers
    every part; every part that is XML parses; _rels/.rels points at `main_part`; every relationship is internal and
    its target exists. A format with many fixed parts (a slide is two) passes its own `max_parts`. It does not check the
    schema of the parts."""
    problems: List[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return [f"not a zip file: {exc}"]
    with archive:
        bad = archive.testzip()
        if bad:
            problems.append(f"corrupt member {bad}")
        names = [info.filename for info in archive.infolist()]
        if len(names) > max_parts:
            problems.append(f"{len(names)} parts, more than {max_parts}")
        if len(set(names)) != len(names):
            problems.append("duplicate member names")
        if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED:
            problems.append("uncompressed size too large")
        for name in names:
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or "\\" in name or "" in parts[:-1]:
                problems.append(f"unsafe member name {name!r}")
            low = name.lower()
            if "vbaproject" in low or "/embeddings/" in low or low.endswith((".bin", ".exe", ".dll")):
                problems.append(f"forbidden part {name}")
        if problems:
            return problems
        if "[Content_Types].xml" not in names:
            return ["missing [Content_Types].xml"]
        trees: Dict[str, ET.Element] = {}
        for name in names:
            if name.endswith((".xml", ".rels")):
                try:
                    trees[name] = ET.fromstring(archive.read(name))
                except ET.ParseError as exc:
                    problems.append(f"{name} is not well-formed XML: {exc}")
        if problems:
            return problems
        defaults, overrides = set(), set()
        for node in trees["[Content_Types].xml"]:
            if _local(node.tag) == "Default":
                defaults.add(node.get("Extension", "").lower())
            elif _local(node.tag) == "Override":
                overrides.add(node.get("PartName", "").lstrip("/"))
        for name in names:
            if name == "[Content_Types].xml":
                continue
            ext = posixpath.basename(name).rpartition(".")[2].lower()  # ".rels" has the extension "rels"
            if name not in overrides and ext not in defaults:
                problems.append(f"{name} has no content type")
        for override in overrides:
            if override not in names:
                problems.append(f"content type override for missing part {override}")
        if "_rels/.rels" not in trees:
            return problems + ["missing _rels/.rels"]
        roots = [node.get("Target", "") for node in trees["_rels/.rels"]
                 if _local(node.tag) == "Relationship" and node.get("Type") == REL_OFFICE_DOCUMENT]
        if [_resolve("", target) for target in roots] != [main_part]:
            problems.append(f"_rels/.rels must point at {main_part} only")
        for name, tree in trees.items():
            if not name.endswith(".rels"):
                continue
            owner = posixpath.join(posixpath.dirname(posixpath.dirname(name)), posixpath.basename(name)[:-5])
            seen = set()
            for node in tree:
                if _local(node.tag) != "Relationship":
                    continue
                rid, target = node.get("Id", ""), node.get("Target", "")
                if rid in seen:
                    problems.append(f"{name}: duplicate relationship id {rid}")
                seen.add(rid)
                if node.get("TargetMode", "Internal") != "Internal":
                    problems.append(f"{name}: external relationship {rid}")
                elif _resolve(owner, target) not in names:
                    problems.append(f"{name}: target {target} does not exist")
    return problems

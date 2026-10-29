"""Markdown to PPTX (docs/design/office-export.md, D3): the converter never yields an invalid package, the slide rules
hold (title slide, one slide per second-level heading, lossless continuation slides) and the store adds the file to its
version next to a DOCX of the same source."""
import contextlib
import hashlib
import io
import json
import re
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import artifact_ops
import office_ooxml
import office_pptx
from artifact_ops import ArtifactError, ArtifactStore
from project_registry import ProjectManager
from test_office_docx import CORPUS, GOOD, STORED
from testenv import isolate_data_dir, restore_data_dir

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def structure(data: bytes):
    return office_ooxml.check_package(data, office_pptx.MAIN_PART, max_parts=office_pptx.MAX_PARTS)


def slides(data: bytes):
    """Title, body paragraphs and tables of every slide, in slide order."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                   key=lambda n: int(re.findall(r"\d+", n)[0]))
    out = []
    for name in names:
        root = ET.fromstring(archive.read(name))
        title, body, tables = "", [], []
        for shape in root.iter(f"{{{P}}}sp"):
            holder = shape.find(f".//{{{P}}}ph")
            texts = ["".join(t.text or "" for t in para.iter(f"{{{A}}}t")) for para in shape.iter(f"{{{A}}}p")]
            if holder is not None and holder.get("type") in ("title", "ctrTitle"):
                title = " ".join(texts)
            else:
                body.extend(texts)
        for table in root.iter(f"{{{A}}}tbl"):
            tables.append([["".join(t.text or "" for t in cell.iter(f"{{{A}}}t")) for cell in row.iter(f"{{{A}}}tc")]
                           for row in table.iter(f"{{{A}}}tr")])
        out.append(SimpleNamespace(title=title, body=body, tables=tables))
    return out


def convert(text: str):
    return slides(office_pptx.markdown_to_pptx(text))


class SlideRuleTests(unittest.TestCase):
    def test_a_realistic_document_is_a_valid_package(self) -> None:
        data = office_pptx.markdown_to_pptx(GOOD)
        self.assertEqual(structure(data), [])
        deck = slides(data)
        self.assertEqual(deck[0].title, "Title")
        self.assertIn("bold", " ".join(deck[0].body))
        flat = " ".join(" ".join(s.body) + " ".join(c for t in s.tables for r in t for c in r) for s in deck)
        for expected in ("one", "nested", "☑ done", "☐ open", "1. first", "quoted", "extra", "println", "diagram"):
            self.assertIn(expected, flat)
        self.assertNotIn("<script", flat)

    def test_one_slide_per_second_level_heading_after_the_title_slide(self) -> None:
        deck = convert("# Deck\n\nintro\n\n## A\n\n- a1\n\n## B\n\n- b1\n")
        self.assertEqual([s.title for s in deck], ["Deck", "A", "B"])
        self.assertEqual(deck[0].body, ["intro"])
        self.assertEqual(deck[1].body, ["a1"])

    def test_deeper_headings_stay_inside_their_slide(self) -> None:
        deck = convert("## A\n\n### Sub\n\ntext\n\n#### Deeper\n\nmore")
        self.assertEqual(len(deck), 1)
        self.assertEqual(deck[0].body, ["Sub", "text", "Deeper", "more"])

    def test_without_a_first_level_heading_there_is_no_title_slide(self) -> None:
        self.assertEqual([s.title for s in convert("## A\n\nx\n\n## B\n\ny")], ["A", "B"])
        deck = convert("loose text\n\n## A\n\nx")
        self.assertEqual([s.title for s in deck], ["", "A"])
        self.assertEqual(deck[0].body, ["loose text"])

    def test_only_the_first_first_level_heading_is_the_deck_title(self) -> None:
        self.assertEqual([s.title for s in convert("# Deck\n\n# Second\n\ntext")], ["Deck", "Second"])

    def test_the_rest_of_the_intro_continues_the_title(self) -> None:
        deck = convert("# T\n\nfirst paragraph\n\n- more\n\n## A\n\nx")
        self.assertEqual([s.title for s in deck], ["T", "T (2)", "A"])
        self.assertEqual(deck[0].body, ["first paragraph"])
        self.assertEqual(deck[1].body, ["more"])

    def test_a_heading_without_content_is_a_slide_of_its_own(self) -> None:
        deck = convert("## Lonely\n\n## Next\n\nx")
        self.assertEqual([(s.title, s.body) for s in deck], [("Lonely", []), ("Next", ["x"])])

    def test_blank_headings_and_rules_make_no_slide(self) -> None:
        self.assertEqual([s.title for s in convert("## \n\n---\n\n## Real\n\nx")], ["Real"])

    def test_a_document_without_text_is_refused(self) -> None:
        for text in ("", "\n\n  \n", "---\n", "## \n"):
            with self.subTest(text=text), self.assertRaisesRegex(office_ooxml.OfficeError, "no text"):
                office_pptx.markdown_to_pptx(text)

    def test_a_long_section_continues_on_more_slides_without_losing_items(self) -> None:
        deck = convert("## Long\n\n" + "\n".join(f"- item {i}" for i in range(40)))
        self.assertEqual([s.title for s in deck][:3], ["Long", "Long (2)", "Long (3)"])
        self.assertEqual([line for s in deck for line in s.body], [f"item {i}" for i in range(40)])
        for slide in deck:
            self.assertLessEqual(len(slide.body), office_pptx.BODY_LINES)

    def test_a_long_paragraph_is_split_not_cut(self) -> None:
        deck = convert("## P\n\n" + "word " * 1500)
        self.assertGreater(len(deck), 1)
        self.assertEqual(" ".join(line for s in deck for line in s.body).split().count("word"), 1500)

    def test_a_long_code_block_continues_line_by_line(self) -> None:
        code = [f"line {i}" for i in range(100)]
        deck = convert("## C\n\n```\n" + "\n".join(code) + "\n```\n")
        self.assertGreater(len(deck), 1)
        self.assertEqual([line for s in deck for line in s.body], code)

    def test_korean_text_counts_double_width(self) -> None:
        wide = convert("## 한글\n\n" + "\n".join("가나다라마바사아자차" * 8 for _ in range(10)))
        narrow = convert("## en\n\n" + "\n".join("abcdefghij" * 8 for _ in range(10)))
        self.assertGreater(len(wide), len(narrow), "an 80-glyph Korean line wraps where 80 Latin letters fit")

    def test_tables_get_their_own_slides_and_repeat_the_header(self) -> None:
        rows = "".join(f"| r{i} | v{i} |\n" for i in range(30))
        deck = convert("## T\n\nbefore\n\n| a | b |\n|---|---|\n" + rows + "\nafter\n")
        tables = [t for s in deck for t in s.tables]
        self.assertGreater(len(tables), 1)
        for table in tables:
            self.assertEqual(table[0], ["a", "b"])
        self.assertEqual([row[0] for t in tables for row in t[1:]], [f"r{i}" for i in range(30)])
        with_table = [s for s in deck if s.tables]
        for slide in with_table:
            self.assertEqual(slide.body, [], "a table slide holds the table alone")
        self.assertEqual([s.body for s in deck if not s.tables], [["before"], ["after"]])

    def test_a_table_too_wide_for_a_slide_is_refused(self) -> None:
        columns = office_pptx.MAX_TABLE_COLUMNS + 1
        text = "|" + "a|" * columns + "\n|" + "-|" * columns + "\n"
        with self.assertRaisesRegex(office_ooxml.OfficeError, "columns"):
            office_pptx.markdown_to_pptx(text)

    def test_table_cells_keep_their_alignment_and_padding(self) -> None:
        deck = convert("## T\n\n|a|b|c|\n|:-|-:|:-:|\n|1|\n")
        self.assertEqual(deck[0].tables[0], [["a", "b", "c"], ["1", "", ""]])

    def test_a_very_long_heading_is_shortened_with_an_ellipsis(self) -> None:
        deck = convert("## " + "x" * 500 + "\n\ntext")
        self.assertLess(len(deck[0].title), 250)
        self.assertTrue(deck[0].title.endswith("…"))

    def test_more_slides_than_the_limit_is_refused_and_the_limit_itself_passes(self) -> None:
        def document(count):
            return "\n\n".join(f"## S{i}\n\nx" for i in range(count))

        with self.assertRaisesRegex(office_ooxml.OfficeError, "slides"):
            office_pptx.markdown_to_pptx(document(office_pptx.MAX_SLIDES + 1))
        data = office_pptx.markdown_to_pptx(document(office_pptx.MAX_SLIDES))
        self.assertEqual(structure(data), [])
        self.assertEqual(len(slides(data)), office_pptx.MAX_SLIDES)


class PackageTests(unittest.TestCase):
    def test_the_package_has_what_powerpoint_needs_to_open_it_without_repair(self) -> None:
        archive = zipfile.ZipFile(io.BytesIO(office_pptx.markdown_to_pptx("# T\n\nhello\n\n## A\n\n- x\n")))
        names = set(archive.namelist())
        for required in ("[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels",
                         "ppt/slideMasters/slideMaster1.xml", "ppt/slideMasters/_rels/slideMaster1.xml.rels",
                         "ppt/slideLayouts/slideLayout1.xml", "ppt/slideLayouts/slideLayout2.xml",
                         "ppt/slideLayouts/_rels/slideLayout1.xml.rels", "ppt/slideLayouts/_rels/slideLayout2.xml.rels",
                         "ppt/theme/theme1.xml", "ppt/slides/slide1.xml", "ppt/slides/_rels/slide1.xml.rels"):
            self.assertIn(required, names)
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        ids = [int(node.get("id")) for node in presentation.iter(f"{{{P}}}sldId")]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(i >= 256 for i in ids), "slide ids start at 256")
        self.assertIsNotNone(presentation.find(f"{{{P}}}sldSz"))
        self.assertIsNotNone(presentation.find(f"{{{P}}}notesSz"))
        master = ET.fromstring(archive.read("ppt/slideMasters/slideMaster1.xml"))
        layout_ids = [int(node.get("id")) for node in master.iter(f"{{{P}}}sldLayoutId")]
        self.assertEqual(len(layout_ids), 2)
        self.assertTrue(all(i >= 2147483649 for i in layout_ids))
        for slide in (n for n in names if re.fullmatch(r"ppt/slides/_rels/slide\d+\.xml\.rels", n)):
            self.assertIn(b"slideLayout", archive.read(slide))

    def test_no_relationship_leaves_the_package(self) -> None:
        data = office_pptx.markdown_to_pptx(GOOD + "\n[a](https://evil.example/x)\n![i](http://evil.example/i.png)\n")
        archive = zipfile.ZipFile(io.BytesIO(data))
        for name in archive.namelist():
            if name.endswith(".rels"):
                self.assertNotIn("TargetMode", archive.read(name).decode(), name)
        everything = b"".join(archive.read(n) for n in archive.namelist())
        for forbidden in (b"hlinkClick", b"<p:pic", b"vbaProject", b"oleObj", b"r:embed", b"r:link"):
            self.assertNotIn(forbidden, everything)

    def test_same_source_same_bytes(self) -> None:
        first, second = office_pptx.markdown_to_pptx(GOOD), office_pptx.markdown_to_pptx(GOOD)
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())

    def test_a_result_above_the_limit_is_refused(self) -> None:
        with self.assertRaises(office_ooxml.OfficeError):
            office_pptx.markdown_to_pptx(GOOD, max_bytes=500)

    def test_the_corpus_never_yields_an_invalid_package(self) -> None:
        for label, text in CORPUS.items():
            with self.subTest(case=label):
                started = time.monotonic()
                try:
                    data = office_pptx.markdown_to_pptx(text)
                except office_ooxml.OfficeError:
                    continue  # a refusal is allowed, a broken file is not
                self.assertEqual(structure(data), [])
                self.assertLess(time.monotonic() - started, 10.0, "conversion must stay fast on adversarial input")

    def test_a_document_of_the_largest_allowed_source_is_refused_or_valid_and_fast(self) -> None:
        text = ("## Section\n\n" + "- a bullet with some words in it\n" * 40 + "\n") * 30
        started = time.monotonic()
        try:
            self.assertEqual(structure(office_pptx.markdown_to_pptx(text[:262144])), [])
        except office_ooxml.OfficeError:
            pass
        self.assertLess(time.monotonic() - started, 10.0)

    def test_the_checker_catches_broken_presentations(self) -> None:
        good = office_pptx.markdown_to_pptx(GOOD)

        def rebuilt(edit):
            source = zipfile.ZipFile(io.BytesIO(good))
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as target:
                for name in source.namelist():
                    data = edit(name, source.read(name))
                    if data is not None:
                        target.writestr(name, data)
            return out.getvalue()

        self.assertTrue(structure(rebuilt(lambda n, d: None if n == "ppt/slideMasters/slideMaster1.xml" else d)))
        self.assertTrue(structure(rebuilt(lambda n, d: None if n == "ppt/theme/theme1.xml" else d)))
        self.assertTrue(structure(rebuilt(lambda n, d: d + b"<" if n == "ppt/slides/slide1.xml" else d)))
        self.assertTrue(structure(rebuilt(lambda n, d: None if n == "[Content_Types].xml" else d)))

    def test_the_part_limit_is_per_format(self) -> None:
        data = office_pptx.markdown_to_pptx("\n\n".join(f"## S{i}\n\nx" for i in range(office_pptx.MAX_SLIDES)))
        self.assertGreater(len(zipfile.ZipFile(io.BytesIO(data)).namelist()), office_ooxml.MAX_PARTS)
        self.assertTrue(office_ooxml.check_package(data, office_pptx.MAIN_PART), "the default limit still applies elsewhere")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ArtifactStore("arch-docs", "t1", Path(self.tmp.name))
        self.store.write("overview.md", STORED, title="Overview")
        self.folder = Path(self.tmp.name) / "arch-docs" / "t1"

    def derived(self, version: int = 1, ext: str = ".pptx"):
        return [f for f in self.store.manifest(version)["files"] if f["path"].endswith(ext)]

    def test_convert_adds_a_listed_pptx_to_the_same_version(self) -> None:
        entry = self.store.convert(1, "overview.md", "pptx")
        self.assertEqual(entry["path"], "overview.pptx")
        self.assertEqual(self.store.versions(), [1])
        listed = self.derived()[0]
        self.assertEqual(listed["kind"], "pptx")
        self.assertEqual(listed["mime"], office_pptx.MIME)
        self.assertEqual(listed["source"], "overview.md")
        source = (self.folder / "v1" / "overview.md").read_bytes()
        self.assertEqual(listed["source_sha256"], hashlib.sha256(source).hexdigest())
        data = (self.folder / "v1" / "overview.pptx").read_bytes()
        self.assertEqual(listed["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(structure(data), [])
        self.assertEqual(((self.folder / "v1" / "overview.pptx").stat().st_mode & 0o777), 0o600)

    def test_docx_and_pptx_of_one_source_live_side_by_side(self) -> None:
        self.store.convert(1, "overview.md", "docx")
        self.store.convert(1, "overview.md", "pptx")
        self.assertEqual([f["path"] for f in self.store.manifest(1)["files"]], ["overview.docx", "overview.md", "overview.pptx"])
        with self.assertRaisesRegex(ArtifactError, "already exists"):
            self.store.convert(1, "overview.md", "pptx")
        self.assertEqual(len(self.derived(1, ".docx")), 1)

    def test_convert_refuses_other_formats_and_non_markdown_sources(self) -> None:
        for fmt in ("xlsx", "odp", "PPTX", "", "pptx "):
            with self.subTest(fmt=fmt), self.assertRaises(ArtifactError):
                self.store.convert(1, "overview.md", fmt)
        self.store.convert(1, "overview.md", "pptx")
        with self.assertRaises(ArtifactError):
            self.store.convert(1, "overview.pptx", "pptx")

    def test_an_empty_document_leaves_no_file(self) -> None:
        self.store.write("blank.md", "---\n", base_version="artifact@v1")
        with self.assertRaisesRegex(ArtifactError, "no text"):
            self.store.convert(2, "blank.md", "pptx")
        self.assertFalse((self.folder / "v2" / "blank.pptx").exists())
        self.assertEqual(self.derived(2), [])

    def test_a_failed_manifest_write_leaves_no_file(self) -> None:
        original = artifact_ops._atomic_json

        def boom(path, data):
            raise OSError("disk full")

        artifact_ops._atomic_json = boom
        try:
            with self.assertRaises(OSError):
                self.store.convert(1, "overview.md", "pptx")
        finally:
            artifact_ops._atomic_json = original
        self.assertFalse((self.folder / "v1" / "overview.pptx").exists())

    def test_a_rewritten_source_drops_both_derived_files(self) -> None:
        self.store.convert(1, "overview.md", "docx")
        self.store.convert(1, "overview.md", "pptx")
        self.store.write("other.md", "# Other\n", base_version="artifact@v1")
        self.assertEqual([f["path"] for f in self.derived(2, ".pptx")], ["overview.pptx"])
        self.assertEqual([f["path"] for f in self.derived(2, ".docx")], ["overview.docx"])
        self.store.write("overview.md", "# Rewritten\n", base_version="artifact@v2")
        self.assertEqual(self.derived(3, ".pptx") + self.derived(3, ".docx"), [])
        self.assertFalse((self.folder / "v3" / "overview.pptx").exists())
        self.assertFalse((self.folder / "v3" / "overview.docx").exists())

    def test_the_derived_file_is_not_text_and_cannot_be_written(self) -> None:
        self.store.convert(1, "overview.md", "pptx")
        self.assertIsNone(self.store.read(1, "overview.pptx"))
        self.assertIn("overview.pptx", self.store.names(1))
        self.assertFalse(artifact_ops.validate("overview.pptx", "x", ["markdown", "mermaid", "svg"]).ok)

    def test_pruning_removes_the_pptx_with_its_version(self) -> None:
        self.store.convert(1, "overview.md", "pptx")
        self.store.write("other.md", "# Other\n", base_version="artifact@v1")
        self.store.prune(1, now=time.time() + 5 * 86400)
        self.assertFalse((self.folder / "v1" / "overview.pptx").exists())


class ConvertCommandTests(unittest.TestCase):
    """guardian.py --artifact-convert --artifact-format pptx: the same JSON answer and exit codes as for docx."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "builtin").mkdir()
        (root / "custom").mkdir()
        (root / "custom" / "docs.json").write_text(json.dumps({"id": "docs", "kind": "design", "name": "docs"}))
        self.manager = ProjectManager(directory=root / "custom", builtin_dir=root / "builtin")
        self.store = ArtifactStore("docs", "t1")
        self.store.write("overview.md", STORED, title="Overview")
        self.addCleanup(self.cleanup_store)

    def cleanup_store(self) -> None:
        self._tmp.cleanup()
        for path in sorted(artifact_ops.artifacts_root().rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()

    def run_command(self, name="overview.md", fmt="pptx", version="artifact@v1"):
        import guardian

        args = SimpleNamespace(artifact_convert=name, project="docs", thread="t1", artifact_version=version, artifact_format=fmt)
        out = io.StringIO()
        code = 0
        with patch("project_registry.ProjectManager", return_value=self.manager), contextlib.redirect_stdout(out):
            try:
                self.assertTrue(guardian.handle_artifact_convert(args))
            except SystemExit as exit_:
                code = exit_.code
        return code, json.loads(out.getvalue())

    def test_success_answers_with_both_hashes(self) -> None:
        code, answer = self.run_command()
        self.assertEqual(code, 0)
        self.assertEqual(answer["file"]["path"], "overview.pptx")
        self.assertEqual(answer["file"]["kind"], "pptx")
        self.assertEqual(answer["result_sha256"], answer["file"]["sha256"])
        self.assertEqual(len(answer["source_sha256"]), 64)

    def test_exit_codes(self) -> None:
        self.run_command()
        for label, kwargs, want in (("again", {}, 4), ("not markdown", dict(name="overview.pptx"), 2),
                                    ("bad format", dict(fmt="xlsx"), 2), ("no such file", dict(name="missing.md"), 3)):
            with self.subTest(case=label):
                code, answer = self.run_command(**kwargs)
                self.assertEqual(code, want, answer)
                self.assertIn("error", answer)


def setUpModule() -> None:
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


if __name__ == "__main__":
    unittest.main()

"""Markdown to DOCX (docs/design/office-export.md, D2): the converter never yields an invalid package, and the store
adds the file to its version under the rules of the design (source checked, no overwrite, derived file follows its source)."""
import contextlib
import hashlib
import io
import json
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import artifact_ops
import office_docx
import office_ooxml
from artifact_ops import ArtifactError, ArtifactStore
from project_registry import ProjectManager
from testenv import isolate_data_dir, restore_data_dir

GOOD = """# Title

Intro with **bold**, *italic*, `code`, ~~gone~~ and a [link](https://example.com/x?a=1&b=2) & <tags>.

## List

- one
  - nested
- [x] done
- [ ] open

1. first
2. second

> quoted
> text

| a | b | c |
|:--|--:|:-:|
| 1 | 2 |
| x | | z | extra |

```go
func main() {\tprintln("hi") }
```

---

![diagram](img/a.png)
"""

# Inputs that have broken naive converters: the result must be a valid package or an OfficeError, never a bad file.
# GOOD as the store accepts it: the Markdown validator refuses raw HTML and image targets it cannot find.
STORED = GOOD.replace(" & <tags>.", " & more.").replace("![diagram](img/a.png)\n", "diagram\n")

CORPUS = {
    "empty": "",
    "only blank": "\n\n   \n",
    "control chars": "# a\x00b\x01c\x0b\x0c\x1f\n\ntext \ud800 ￾ ￿ end\n",
    "carriage returns": "a\r\nb\rc\r\n\r\n- x\r- y\r",
    "unclosed fence": "```python\nprint(1)\n\nmore\n",
    "unclosed tilde fence": "~~~\nabc",
    "fence inside list": "- a\n  ```\n  code\n- b\n",
    "unmatched markers": "**bold *it `code ~~del [link](http://x\n\n__ _ * ** ` `` ``` [ ] ( ) ![",
    "nested markers": "***a **b *c ~~d `e` f~~ g* h** i***",
    "link edge cases": "[](x) [a]() [a](<b c>) [a](b (c)) [[a]](b) ![a]() ![](x) [a](javascript:alert(1))",
    "huge link": "[" + "a" * 5000 + "](" + "b" * 5000 + ")",
    "ragged table": "|a|b|\n|-|-|\n|1|\n|1|2|3|4|\n||\n",
    "table no body": "| a | b |\n|---|---|\n",
    "table delimiter only": "|---|---|\n|---|---|\n",
    "pipe escapes": "| a \\| b | `c | d` |\n|---|---|\n| 1 | 2 |\n",
    "table then text": "| a |\n|---|\ntext right after\n",
    "heading levels": "####### seven\n\n#nospace\n\n# \n\n#\n",
    "deep lists": "\n".join("  " * i + "- item" for i in range(40)),
    "deep quote": ">" * 60 + " deep\n",
    "mixed list kinds": "- a\n1. b\n- c\n2. d\n",
    "html block": "<div>\n<script>alert(1)</script>\n</div>\n<!-- c -->",
    "entities": "&amp; &lt;b&gt; &#0; &#x1F; &nbsp;",
    "unicode": "# 제목\n\n한글 **굵게** 😀 ‮ rtl ​ zero\n\n| 열 | 값 |\n|---|---|\n| 가 | 1 |\n",
    "tabs": "\ttabbed code\n\n-\ttab list\n",
    "setext": "Title\n=====\n\nSub\n---\n",
    "long line": "word " * 50000,
    "many paragraphs": "\n\n".join(f"p{i}" for i in range(3000)),
    "many rows": "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 2000,
    "many markers": "*a " * 20000,
    "backtick storm": "`" * 5000 + "\n" + "``a`` " * 2000,
    "bracket storm": "[" * 5000 + "](" * 2000,
}


def structure(data: bytes):
    return office_ooxml.check_package(data, office_docx.MAIN_PART)


class ConverterTests(unittest.TestCase):
    def test_a_realistic_document_is_a_valid_package(self) -> None:
        data = office_docx.markdown_to_docx(GOOD)
        self.assertEqual(structure(data), [])
        body = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
        for expected in ("Title", "bold", "nested", "quoted", "diagram"):
            self.assertIn(expected, body)
        self.assertNotIn("<script", body)

    def test_the_corpus_never_yields_an_invalid_package(self) -> None:
        for label, text in CORPUS.items():
            with self.subTest(case=label):
                started = time.monotonic()
                try:
                    data = office_docx.markdown_to_docx(text)
                except office_ooxml.OfficeError:
                    continue  # a refusal is allowed, a broken file is not
                self.assertEqual(structure(data), [])
                self.assertLess(time.monotonic() - started, 10.0, "conversion must stay fast on adversarial input")

    def test_no_relationship_leaves_the_package(self) -> None:
        data = office_docx.markdown_to_docx(GOOD + "\n[a](https://evil.example/x)\n![i](http://evil.example/i.png)\n")
        archive = zipfile.ZipFile(io.BytesIO(data))
        for name in archive.namelist():
            if name.endswith(".rels"):
                self.assertNotIn("TargetMode", archive.read(name).decode(), name)
        everything = b"".join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b"hyperlink", everything.lower())
        self.assertNotIn(b"<w:drawing", everything)
        self.assertNotIn(b"vbaProject", everything)

    def test_same_source_same_bytes(self) -> None:
        first, second = office_docx.markdown_to_docx(GOOD), office_docx.markdown_to_docx(GOOD)
        self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())

    def test_a_result_above_the_limit_is_refused(self) -> None:
        with self.assertRaises(office_ooxml.OfficeError):
            office_docx.markdown_to_docx(GOOD * 20, max_bytes=500)

    def test_the_checker_catches_broken_packages(self) -> None:
        good = office_docx.markdown_to_docx(GOOD)
        self.assertTrue(structure(b"not a zip"))

        def rebuilt(edit):
            source = zipfile.ZipFile(io.BytesIO(good))
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as target:
                for name in source.namelist():
                    data = edit(name, source.read(name))
                    if data is not None:
                        target.writestr(name, data)
            return out.getvalue()

        self.assertTrue(structure(rebuilt(lambda n, d: None if n == "word/document.xml" else d)))
        self.assertTrue(structure(rebuilt(lambda n, d: d + b"<" if n == "word/document.xml" else d)))
        self.assertTrue(structure(rebuilt(lambda n, d: None if n == "[Content_Types].xml" else d)))
        external = b'<Relationship Id="rId9" Type="x" Target="http://e.example" TargetMode="External"/></Relationships>'
        self.assertTrue(structure(rebuilt(
            lambda n, d: d.replace(b"</Relationships>", external) if n == "_rels/.rels" else d)))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ArtifactStore("arch-docs", "t1", Path(self.tmp.name))
        self.store.write("overview.md", STORED, title="Overview")

    def derived(self, version: int = 1):
        return [f for f in self.store.manifest(version)["files"] if f["path"].endswith(".docx")]

    def test_convert_adds_a_listed_file_to_the_same_version(self) -> None:
        entry = self.store.convert(1, "overview.md")
        self.assertEqual(entry["path"], "overview.docx")
        self.assertEqual(self.store.versions(), [1])
        listed = self.derived()[0]
        self.assertEqual(listed["source"], "overview.md")
        source = (Path(self.tmp.name) / "arch-docs" / "t1" / "v1" / "overview.md").read_bytes()
        self.assertEqual(listed["source_sha256"], hashlib.sha256(source).hexdigest())
        path = Path(self.tmp.name) / "arch-docs" / "t1" / "v1" / "overview.docx"
        data = path.read_bytes()
        self.assertEqual(listed["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(listed["bytes"], len(data))
        self.assertEqual(structure(data), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.startswith(".tmp")], [])

    def test_convert_refuses_to_overwrite(self) -> None:
        self.store.convert(1, "overview.md")
        before = self.derived()
        with self.assertRaisesRegex(ArtifactError, "already exists"):
            self.store.convert(1, "overview.md")
        self.assertEqual(self.derived(), before)

    def test_convert_refuses_what_is_not_a_stored_markdown_file(self) -> None:
        for name in ("missing.md", "../x.md", "overview.docx", "A.md", ""):
            with self.subTest(name=name), self.assertRaises(ArtifactError):
                self.store.convert(1, name)
        with self.assertRaises(ArtifactError):
            self.store.convert(9, "overview.md")
        with self.assertRaises(ArtifactError):
            self.store.convert(1, "overview.md", "xlsx")

    def test_convert_refuses_a_source_that_no_longer_matches_its_manifest(self) -> None:
        (Path(self.tmp.name) / "arch-docs" / "t1" / "v1" / "overview.md").write_text("# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactError, "does not match"):
            self.store.convert(1, "overview.md")
        self.assertEqual(self.derived(), [])

    def test_a_failed_manifest_write_leaves_no_file(self) -> None:
        original = artifact_ops._atomic_json

        def boom(path, data):
            raise OSError("disk full")

        artifact_ops._atomic_json = boom
        try:
            with self.assertRaises(OSError):
                self.store.convert(1, "overview.md")
        finally:
            artifact_ops._atomic_json = original
        self.assertFalse((Path(self.tmp.name) / "arch-docs" / "t1" / "v1" / "overview.docx").exists())
        self.assertEqual(self.derived(), [])

    def test_a_new_version_keeps_the_docx_of_an_unchanged_source_and_drops_it_for_a_rewritten_one(self) -> None:
        self.store.convert(1, "overview.md")
        self.store.write("other.md", "# Other\n", base_version="artifact@v1")
        carried = self.derived(2)
        self.assertEqual([f["path"] for f in carried], ["overview.docx"])
        self.assertTrue((Path(self.tmp.name) / "arch-docs" / "t1" / "v2" / "overview.docx").is_file())
        self.store.write("overview.md", "# Rewritten\n", base_version="artifact@v2")
        self.assertEqual(self.derived(3), [])
        self.assertFalse((Path(self.tmp.name) / "arch-docs" / "t1" / "v3" / "overview.docx").exists())
        self.assertEqual(len(self.derived(1)), 1, "an older version keeps its own derived file")

    def test_the_derived_file_is_not_text(self) -> None:
        self.store.convert(1, "overview.md")
        self.assertIsNone(self.store.read(1, "overview.docx"))
        self.assertIn("overview.docx", self.store.names(1))
        report = artifact_ops.validate("overview.docx", "x", ["markdown", "mermaid", "svg"])
        self.assertFalse(report.ok, "a docx can never be written through write_artifact")

    def test_pruning_removes_the_derived_file_with_its_version(self) -> None:
        self.store.convert(1, "overview.md")
        self.store.write("other.md", "# Other\n", base_version="artifact@v1")
        self.store.prune(1, now=time.time() + 5 * 86400)
        folder = Path(self.tmp.name) / "arch-docs" / "t1" / "v1"
        self.assertFalse((folder / "overview.docx").exists())
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(all(f.get("expired") for f in manifest["files"]))
        with self.assertRaisesRegex(ArtifactError, "not available"):
            self.store.convert(1, "overview.md")


class ConvertCommandTests(unittest.TestCase):
    """guardian.py --artifact-convert: JSON answer and the exit codes the hub API maps to 400/404/409."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "builtin").mkdir()
        (root / "custom").mkdir()
        (root / "custom" / "docs.json").write_text(json.dumps({"id": "docs", "kind": "design", "name": "docs"}))
        (root / "custom" / "code.json").write_text(json.dumps({"id": "code", "workspace": "~/ws/code"}))
        self.manager = ProjectManager(directory=root / "custom", builtin_dir=root / "builtin")
        self.store = ArtifactStore("docs", "t1")
        self.store.write("overview.md", STORED, title="Overview")
        self.addCleanup(self.cleanup_store)

    def cleanup_store(self) -> None:
        self._tmp.cleanup()
        for path in sorted(artifact_ops.artifacts_root().rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()

    def run_command(self, name="overview.md", project="docs", thread="t1", version="artifact@v1", fmt=None):
        import guardian

        args = SimpleNamespace(artifact_convert=name, project=project, thread=thread, artifact_version=version, artifact_format=fmt)
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
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["file"]["path"], "overview.docx")
        self.assertEqual(answer["source"], "overview.md")
        self.assertEqual(len(answer["source_sha256"]), 64)
        self.assertEqual(answer["result_sha256"], answer["file"]["sha256"])

    def test_exit_codes(self) -> None:
        self.run_command()
        cases = {
            "again": (dict(), 4),
            "no such file": (dict(name="missing.md"), 3),
            "no such version": (dict(version="artifact@v9"), 3),
            "no such project": (dict(project="nope"), 3),
            "code project without a kept report": (dict(project="code"), 3),
            "not markdown": (dict(name="overview.docx"), 2),
            "bad format": (dict(fmt="xlsx"), 2),
            "bad version": (dict(version="latest"), 2),
            "no thread": (dict(thread=""), 2),
        }
        for label, (kwargs, want) in cases.items():
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

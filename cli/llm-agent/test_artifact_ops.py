"""Design artifacts: validators, the immutable version store, retention and the approval flow (design-project.md, P2)."""
import json
import os
import stat
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import action_ops
import artifact_ops
from artifact_ops import ArtifactError, ArtifactStore, StaleBase, TOOL
from db_manager import DecisionDBManager
from debate_engine import design_base_rule
from mcp_client import ActionValidationError, MCPClient
from project_registry import ProjectManager, parse_project
from testenv import isolate_data_dir, restore_data_dir

ALL = ["markdown", "mermaid", "svg"]
DESIGN = {"id": "arch-docs", "kind": "design", "name": "Architecture docs"}
SVG_OK = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>Box</title>'
          '<rect id="r" width="5" height="5"/></svg>')


def setUpModule() -> None:
    os.environ["GUARDIAN_JEV_GATE"] = "off"
    isolate_data_dir()


def tearDownModule() -> None:
    restore_data_dir()


def check(name: str, content: str, **kwargs: Any) -> artifact_ops.Report:
    return artifact_ops.validate(name, content, ALL, **kwargs)


class NameAndLimitTests(unittest.TestCase):
    def test_names_are_flat_lowercase_files_with_a_known_extension(self) -> None:
        self.assertTrue(check("overview.md", "# A\n").ok)
        for bad in ("../x.md", "a/b.md", "A.md", ".hidden.md", "x.txt", "", "x" * 90 + ".md"):
            with self.subTest(name=bad):
                self.assertFalse(check(bad, "# A\n").ok)

    def test_types_of_the_project_are_enforced(self) -> None:
        report = artifact_ops.validate("a.svg", SVG_OK, ["markdown"])
        self.assertIn("works on markdown", report.errors[0])

    def test_empty_and_nul_content_are_refused(self) -> None:
        self.assertFalse(check("a.md", "").ok)
        self.assertFalse(check("a.md", "# A\x00\n").ok)

    def test_the_project_limit_and_the_safety_limit(self) -> None:
        self.assertIn("artifacts.max_bytes", check("a.md", "x" * 101, max_bytes=100).errors[0])
        with patch.dict(os.environ, {"GUARDIAN_ARTIFACT_MAX_FILE_BYTES": "50"}):
            report = check("a.md", "x" * 60)
        self.assertTrue(any("safety limit of 50" in e for e in report.errors))

    def test_credential_shapes_are_refused(self) -> None:
        samples = [
            "-----BEGIN RSA PRIVATE KEY-----",
            "key AKIAABCDEFGHIJKLMNOP here",
            "ghp_" + "a" * 36,
            "sk-" + "b" * 30,
            "xoxb-1234567890-abcdef",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:20]):
                self.assertFalse(check("a.md", f"# A\n\n{sample}\n").ok)
        self.assertTrue(check("a.md", "# A\n\nThe sk-learn library.\n").ok)


class MarkdownTests(unittest.TestCase):
    def test_plain_markdown_passes(self) -> None:
        report = check("a.md", "# Title\n\n## Part\n\ntext [link](https://example.com) and [top](#title)\n")
        self.assertEqual((report.errors, report.warnings), ([], []))

    def test_an_unclosed_fence_is_an_error(self) -> None:
        self.assertTrue(any("never closed" in e for e in check("a.md", "# A\n\n```go\ncode\n").errors))

    def test_script_like_content_is_an_error_but_not_inside_a_fence(self) -> None:
        for line in ("<script>x()</script>", "<iframe src=x>", "[x](javascript:alert(1))", '<a onclick="x()">'):
            with self.subTest(line=line):
                self.assertFalse(check("a.md", f"# A\n\n{line}\n").ok)
        self.assertTrue(check("a.md", "# A\n\n```html\n<script>x()</script>\n```\n").ok)

    def test_an_image_without_alt_text_is_an_error(self) -> None:
        self.assertFalse(check("a.md", "# A\n\n![](https://example.com/x.png)\n").ok)
        self.assertTrue(check("a.md", "# A\n\n![diagram](https://example.com/x.png)\n").ok)

    def test_a_heading_jump_is_only_a_warning(self) -> None:
        report = check("a.md", "# A\n\n### C\n")
        self.assertTrue(report.ok)
        self.assertTrue(any("jumps from 1 to 3" in w for w in report.warnings))

    def test_a_link_to_an_unknown_artifact_warns_only_when_the_thread_is_known(self) -> None:
        text = "# A\n\nsee [other](other.md#x)\n"
        self.assertEqual(check("a.md", text).warnings, [])
        self.assertEqual(check("a.md", text, known=["other.md"]).warnings, [])
        self.assertTrue(any("other.md" in w for w in check("a.md", text, known=["b.md"]).warnings))

    def test_an_image_target_that_is_not_an_artifact_warns_like_a_link(self) -> None:
        text = "# A\n\n![legend](legend.svg)\n![web](https://example.com/x.png)\n![up](../x.png)\n![dir](figures/legend.svg)\n"
        self.assertEqual(check("a.md", text).warnings, [])
        warnings = check("a.md", text, known=["legend.svg"]).warnings
        self.assertEqual(len(warnings), 1)
        self.assertIn("image 'figures/legend.svg'", warnings[0])

    def test_required_sections_are_an_error_when_missing_at_any_heading_level(self) -> None:
        text = "# Record\n\n## Context\n\nx\n\n### decision ##\n\ny\n"
        self.assertTrue(check("a.md", text, required_sections=["Context", "Decision"]).ok)
        report = check("a.md", text, required_sections=["Context", "Consequences"])
        self.assertFalse(report.ok)
        self.assertTrue(any("'Consequences' is missing" in e for e in report.errors))
        self.assertTrue(check("a.md", text).ok)  # opt-in: nothing is required by default

    def test_a_heading_inside_a_code_fence_does_not_count_as_a_section(self) -> None:
        text = "# Record\n\n```\n## Decision\n```\n"
        self.assertFalse(check("a.md", text, required_sections=["Decision"]).ok)

    def test_check_arguments_requires_the_sections_of_that_file_only(self) -> None:
        project = SimpleNamespace(artifacts={"types": ["markdown"], "required_sections": {"decisions.md": ["Decision"]}})
        args = {"name": "decisions.md", "content": "# D\n"}
        self.assertFalse(artifact_ops.check_arguments(args, project).ok)
        self.assertTrue(artifact_ops.check_arguments({**args, "name": "notes.md"}, project).ok)


class MermaidTests(unittest.TestCase):
    def test_a_diagram_passes(self) -> None:
        self.assertTrue(check("a.mmd", "%% note\nflowchart TD\n  A[Start] --> B(End)\n").ok)
        self.assertTrue(check("a.mmd", "---\ntitle: T\n---\nsequenceDiagram\n  A->>B: hi\n").ok)

    def test_the_first_statement_must_be_a_diagram_type(self) -> None:
        self.assertFalse(check("a.mmd", "hello world\n").ok)
        self.assertFalse(check("a.mmd", "flowchartX TD\n").ok)
        self.assertFalse(check("a.mmd", "%% only a comment\n").ok)

    def test_interaction_directives_and_scripts_are_refused(self) -> None:
        for extra in ("click A callback", "click A href \"x\"", "A --> B: javascript:x", "A[<script>x</script>]"):
            with self.subTest(extra=extra):
                self.assertFalse(check("a.mmd", f"flowchart TD\n  {extra}\n").ok)

    def test_unbalanced_brackets_are_an_error(self) -> None:
        self.assertFalse(check("a.mmd", "flowchart TD\n  A[Start --> B\n").ok)
        self.assertFalse(check("a.mmd", 'flowchart TD\n  A["open --> B\n').ok)
        self.assertTrue(check("a.mmd", 'flowchart TD\n  A["a (b"] --> B\n').ok)


class SvgTests(unittest.TestCase):
    def test_a_clean_svg_passes(self) -> None:
        self.assertEqual(check("a.svg", SVG_OK).errors, [])

    def test_a_missing_title_is_a_warning(self) -> None:
        report = check("a.svg", '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
        self.assertTrue(report.ok)
        self.assertTrue(any("<title>" in w for w in report.warnings))

    def test_doctype_and_entities_are_refused_before_parsing(self) -> None:
        bomb = '<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><svg xmlns="http://www.w3.org/2000/svg">&b;</svg>'
        report = check("a.svg", bomb)
        self.assertTrue(any("DOCTYPE" in e for e in report.errors))
        self.assertFalse(check("a.svg", '<?xml-stylesheet href="x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>').ok)

    def test_malformed_xml_and_a_wrong_root_are_refused(self) -> None:
        self.assertFalse(check("a.svg", "<svg xmlns='http://www.w3.org/2000/svg'><g></svg>").ok)
        self.assertFalse(check("a.svg", "<html/>").ok)

    def test_active_content_is_refused(self) -> None:
        cases = [
            '<script>alert(1)</script>',
            '<foreignObject><div/></foreignObject>',
            '<rect onclick="x()"/>',
            '<a href="https://example.com"><rect/></a>',
            '<a xlink:href="javascript:x()" xmlns:xlink="http://www.w3.org/1999/xlink"><rect/></a>',
            '<rect fill="url(https://example.com/x)"/>',
            '<style>@import url(x.css);</style>',
            '<image href="data:text/html,x"/>',
        ]
        for inner in cases:
            with self.subTest(inner=inner):
                svg = f'<svg xmlns="http://www.w3.org/2000/svg"><title>T</title>{inner}</svg>'
                self.assertFalse(check("a.svg", svg).ok)

    def test_ids_and_references(self) -> None:
        dup = '<svg xmlns="http://www.w3.org/2000/svg"><title>T</title><g id="a"/><g id="a"/></svg>'
        self.assertTrue(any("used 2 times" in e for e in check("a.svg", dup).errors))
        dangling = '<svg xmlns="http://www.w3.org/2000/svg"><title>T</title><use href="#nope"/></svg>'
        self.assertTrue(any("#nope" in e for e in check("a.svg", dangling).errors))
        fine = ('<svg xmlns="http://www.w3.org/2000/svg"><title>T</title><defs><linearGradient id="g"/></defs>'
                '<rect fill="url(#g)"/><use href="#g"/></svg>')
        self.assertEqual(check("a.svg", fine).errors, [])


def svg(inner: str, root_extra: str = "") -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg"{root_extra}><title>t</title>{inner}</svg>'


class HardeningTests(unittest.TestCase):
    """Bypasses found by the security review of P2; each input below was accepted before the fix."""

    def refused(self, name: str, content: str) -> None:
        report = check(name, content)
        self.assertFalse(report.ok, f"accepted: {content[:80]!r}")

    def test_svg_style_text_after_a_child_element_is_scanned(self) -> None:
        self.refused("a.svg", svg("<style><g/>@import url(https://evil.example/x.css);</style>"))

    def test_svg_css_function_case_escapes_and_image_set(self) -> None:
        for css in ("a{fill:URL(https://evil.example/x)}", "a{background:u\\72l(https://evil.example/x)}",
                    "a{background:image-set('https://evil.example/x' 1x)}", "a{x:/**/url(//evil.example/x)}"):
            with self.subTest(css=css):
                self.refused("a.svg", svg(f"<style>{css}</style>"))
        self.refused("a.svg", svg('<rect style="fill:url(https://evil.example/x)" width="1" height="1"/>'))

    def test_svg_animation_and_foreign_namespaces_are_refused(self) -> None:
        self.refused("a.svg", svg('<a id="l" href="#l"><animate attributeName="href" values="javascript:alert(1)"/></a>'))
        self.refused("a.svg", svg('<set attributeName="href" to="javascript:alert(1)"/>'))
        self.refused("a.svg", svg('<h:img xmlns:h="http://www.w3.org/1999/xhtml" src="x" onerror="alert(1)"/>'))
        self.refused("a.svg", svg('<h:form xmlns:h="http://www.w3.org/1999/xhtml"/>'))
        self.refused("a.svg", '<svg><title>t</title></svg>')  # no namespace: a browser would not render it as SVG

    def test_svg_script_urls_split_by_whitespace_or_entities(self) -> None:
        self.refused("a.svg", svg('<a href="java&#9;script:alert(1)"><text>x</text></a>'))
        self.refused("a.svg", svg('<a href="&#106;avascript:alert(1)"><text>x</text></a>'))
        self.refused("a.svg", svg('<a xml:base="https://evil.example/" href="#a"><text>x</text></a>'))

    def test_svg_stays_usable_for_normal_diagrams(self) -> None:
        good = svg('<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
                   '<rect fill="url(#g)" width="4" height="4"/><text x="1" y="1">Server</text>'
                   '<style>.a{fill:#123;stroke:url(#g)}</style>')
        self.assertTrue(check("a.svg", good).ok, check("a.svg", good).errors)

    def test_markdown_raw_html_is_refused_but_simple_tags_pass(self) -> None:
        for bad in ("<img src=x onerror=alert(1)>", "<a href='x' onclick='y'>t</a>", "<svg onload=alert(1)>",
                    "<details open ontoggle=alert(1)>", "[x](javascript&#58;alert(1))", "[x](java&#9;script:alert(1))",
                    "[x](&#106;avascript:alert(1))", "[x](data:text/html,<b>)"):
            with self.subTest(markdown=bad):
                self.refused("a.md", f"# A\n\n{bad}\n")
        self.assertTrue(check("a.md", "# A\n\nline<br>break and <kbd>Ctrl</kbd>\n").ok)
        self.assertTrue(check("a.md", "# A\n\n`<img src=x>` stays inert in a code span\n").ok)

    def test_markdown_fences_follow_commonmark(self) -> None:
        # a tab-indented or four-space-indented line does not open a fence, so what follows is live text
        self.refused("a.md", "# A\n\n\t```\n<script>alert(1)</script>\n")
        self.refused("a.md", "# A\n\n    ```\n<script>alert(1)</script>\n")
        # a backtick fence whose info string holds a backtick is not a fence either
        self.refused("a.md", "# A\n\n``` `x`\n<script>alert(1)</script>\n```\n")
        # a real fence still protects its content
        self.assertTrue(check("a.md", "# A\n\n```html\n<script>x</script>\n```\n").ok)
        self.assertTrue(check("a.md", "# A\n\n~~~\n<script>x</script>\n~~~\n").ok)

    def test_mermaid_directives_after_a_semicolon_and_init_blocks(self) -> None:
        self.refused("a.mmd", 'flowchart TD\n  A --> B; click A callback "x"\n')
        self.refused("a.mmd", 'flowchart TD\n  A --> B;click A href "https://evil.example"\n')
        self.refused("a.mmd", '%%{init: {"securityLevel": "loose"}}%%\nflowchart TD\n  A --> B\n')
        self.refused("a.mmd", '---\nconfig:\n  securityLevel: loose\n---\nflowchart TD\n  A --> B\n')
        self.refused("a.mmd", 'flowchart TD\n  A["<img src=x onerror=alert(1)>"] --> B\n')
        self.assertTrue(check("a.mmd", 'flowchart TD\n  A["Click here<br>to start"] --> B\n').ok)

    def test_more_credential_shapes_and_encodings_are_refused(self) -> None:
        for secret in ("github_pat_" + "a" * 40, "AIza" + "B" * 35, "tskey-auth-" + "k" * 20,
                       "sk_live_" + "a" * 24, "&#65;KIA" + "A" * 16, "-----BEGIN PRIVATE KEY BLOCK-----"):
            with self.subTest(secret=secret[:20]):
                self.refused("a.md", f"# A\n\n{secret}\n")
        with self.assertRaises(ArtifactError):
            artifact_ops.check_arguments({"name": "a.md", "content": "# A\n", "title": "sk-" + "a" * 30})

    def test_control_and_bidirectional_characters_are_refused(self) -> None:
        for char in ("\x1b", "\x07", "\x9b", "\u202e", "\u2066", "\u200f"):
            with self.subTest(char=hex(ord(char))):
                self.refused("a.md", f"# A\n\nsafe {char} text\n")
        self.assertTrue(check("a.md", "# A\n\ntab\tand 한국어 and 👨\u200d👩\n").ok)
        self.assertNotIn("\x1b", artifact_ops.printable("a\x1bb"))

    def test_hostile_inputs_are_cheap(self) -> None:
        for content in ("![" * 32000, "`" * 60000, "<a " * 30000, "[" * 60000, "(" * 60000):
            started = time.monotonic()
            check("a.md", content)
            self.assertLess(time.monotonic() - started, 2.0, content[:6])

    def test_oversized_input_is_refused_before_it_is_scanned(self) -> None:
        started = time.monotonic()
        report = check("a.md", "![" * 400000, max_bytes=1000)
        self.assertFalse(report.ok)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_names_and_versions_reject_a_trailing_newline(self) -> None:
        self.refused("a.md\n", "# A\n")
        with self.assertRaises(ArtifactError):
            artifact_ops.parse_base_version("artifact@v1\n2")

    def test_a_tampered_manifest_cannot_name_a_path_outside_the_version(self) -> None:
        with TemporaryDirectory() as tmp:
            store = ArtifactStore("p", "t", Path(tmp))
            store.write("a.md", "# A\n", base_version=None)
            store.write("b.md", "# B\n", base_version="artifact@v1")
            victim = Path(tmp) / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            manifest_path = Path(tmp) / "p" / "t" / "v1" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"].append({"path": "../../../victim.txt", "sha256": "0", "bytes": 4})
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(store.names(1), ["a.md"])
            self.assertIsNone(store.read(1, "../../../victim.txt"))
            old = time.time() + 90 * 86400
            store.prune(30, now=old)
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")


class VersionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = parse_project(DESIGN)
        self.store = ArtifactStore("arch-docs", "t1", self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str, base: Any = "auto", **kwargs: Any) -> Dict[str, Any]:
        if base == "auto":
            latest = self.store.latest()
            base = artifact_ops.format_version(latest) if latest else None
        return self.store.write(name, content, project=self.project, base_version=base, **kwargs)

    def test_the_first_write_creates_v1_with_a_manifest(self) -> None:
        manifest = self.write("overview.md", "# One\n", title="Overview", agent="Judge")
        self.assertEqual((manifest["version"], manifest["parent"], manifest["written"]), (1, None, "overview.md"))
        self.assertEqual(self.store.versions(), [1])
        self.assertEqual(self.store.read(1, "overview.md"), "# One\n")
        entry = manifest["files"][0]
        self.assertEqual((entry["kind"], entry["title"], entry["bytes"]), ("markdown", "Overview", 6))
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(json.loads((self.root / "arch-docs" / "t1" / "v1" / "manifest.json").read_text())["version"], 1)

    def test_each_write_is_a_full_snapshot_and_old_versions_do_not_change(self) -> None:
        self.write("overview.md", "# One\n")
        self.write("flow.mmd", "flowchart TD\n  A --> B\n")
        self.write("overview.md", "# Two\n")
        self.assertEqual(self.store.versions(), [1, 2, 3])
        self.assertEqual(self.store.names(1), ["overview.md"])
        self.assertEqual(self.store.names(2), ["flow.mmd", "overview.md"])
        self.assertEqual(self.store.names(), ["flow.mmd", "overview.md"])
        self.assertEqual(self.store.read(1, "overview.md"), "# One\n")
        self.assertEqual(self.store.read(2, "overview.md"), "# One\n")
        self.assertEqual(self.store.read(3, "overview.md"), "# Two\n")
        self.assertEqual(self.store.read(3, "flow.mmd"), "flowchart TD\n  A --> B\n")
        self.assertEqual(self.store.manifest(3)["parent"], 2)

    def test_threads_and_projects_are_separate(self) -> None:
        self.write("a.md", "# A\n")
        other = ArtifactStore("arch-docs", "t2", self.root)
        self.assertIsNone(other.latest())
        other.write("a.md", "# Other\n", project=self.project, base_version=None)
        self.assertEqual(self.store.read(1, "a.md"), "# A\n")

    def test_a_stale_or_missing_base_is_refused(self) -> None:
        self.write("a.md", "# A\n")
        with self.assertRaises(StaleBase):
            self.write("a.md", "# B\n", base=None)
        with self.assertRaises(StaleBase):
            self.write("a.md", "# B\n", base="artifact@v7")
        with self.assertRaises(StaleBase):
            ArtifactStore("arch-docs", "fresh", self.root).write("a.md", "# A\n", project=self.project,
                                                                  base_version="artifact@v1")
        self.assertEqual(self.store.versions(), [1])

    def test_a_malformed_base_version_is_an_artifact_error(self) -> None:
        for raw in ("latest", "artifact@v0", "v-1", "artifact@3x"):
            with self.subTest(raw=raw), self.assertRaises(ArtifactError):
                artifact_ops.parse_base_version(raw)
        self.assertEqual(artifact_ops.parse_base_version("v3"), 3)
        self.assertIsNone(artifact_ops.parse_base_version("  "))

    def test_invalid_content_writes_nothing(self) -> None:
        with self.assertRaises(ArtifactError):
            self.write("a.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", base=None)
        self.assertEqual(self.store.versions(), [])
        leftovers = [p.name for p in (self.root / "arch-docs" / "t1").glob("*")] if (self.root / "arch-docs" / "t1").exists() else []
        self.assertEqual(leftovers, [])

    def test_project_limits_apply(self) -> None:
        small = parse_project({**DESIGN, "artifacts": {"max_bytes": 20}})
        with self.assertRaisesRegex(ArtifactError, "max_bytes"):
            self.store.write("a.md", "# " + "x" * 40 + "\n", project=small, base_version=None)
        only_md = parse_project({**DESIGN, "artifacts": {"types": ["markdown"]}})
        with self.assertRaisesRegex(ArtifactError, "works on markdown"):
            self.store.write("a.mmd", "flowchart TD\n  A --> B\n", project=only_md, base_version=None)

    def test_a_crashed_write_leaves_no_version(self) -> None:
        self.write("a.md", "# A\n")
        with patch.object(artifact_ops.os, "rename", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.write("a.md", "# B\n")
        self.assertEqual(self.store.versions(), [1])
        self.assertEqual([p.name for p in (self.root / "arch-docs" / "t1").glob(".tmp-v*")], [])
        self.write("a.md", "# B\n")  # and the next write works
        self.assertEqual(self.store.versions(), [1, 2])

    def test_files_are_private(self) -> None:
        self.write("a.md", "# A\n")
        version = self.root / "arch-docs" / "t1" / "v1"
        self.assertEqual(stat.S_IMODE((version / "a.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((version / "manifest.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(version.stat().st_mode), 0o700)

    def test_read_refuses_names_outside_the_manifest(self) -> None:
        self.write("a.md", "# A\n")
        (self.root / "arch-docs" / "t1" / "secret.md").write_text("no")
        for name in ("../secret.md", "secret.md", "manifest.json", "../../x", ""):
            with self.subTest(name=name):
                self.assertIsNone(self.store.read(1, name))
        self.assertIsNone(self.store.read(9, "a.md"))

    def test_ids_are_sanitized_into_one_directory_level(self) -> None:
        store = ArtifactStore("../../etc", "t/../x", self.root)
        self.assertEqual(store.directory.parent.parent, self.root)
        with self.assertRaises(ArtifactError):
            ArtifactStore("..", "t1", self.root)

    def test_concurrent_writes_make_exactly_one_new_version(self) -> None:
        self.write("a.md", "# A\n")
        outcomes: List[str] = []

        def attempt(text: str) -> None:
            try:
                self.store.write("a.md", text, project=self.project, base_version="artifact@v1")
                outcomes.append("ok")
            except StaleBase:
                outcomes.append("stale")

        threads = [threading.Thread(target=attempt, args=(f"# {n}\n",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["ok", "stale", "stale", "stale"])
        self.assertEqual(self.store.versions(), [1, 2])


class RetentionTests(unittest.TestCase):
    DAY = 86400

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = parse_project(DESIGN)
        self.store = ArtifactStore("arch-docs", "t1", self.root)
        self.start = time.time()
        for number in range(1, 4):
            latest = self.store.latest()
            self.store.write("a.md", f"# v{number}\n", project=self.project,
                             base_version=artifact_ops.format_version(latest) if latest else None)
        self._age(1, 40)
        self._age(2, 10)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _age(self, number: int, days: int) -> None:
        path = self.root / "arch-docs" / "t1" / f"v{number}" / "manifest.json"
        data = json.loads(path.read_text())
        data["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.start - days * self.DAY))
        path.write_text(json.dumps(data))

    def test_old_versions_expire_but_leave_a_tombstone(self) -> None:
        self.assertEqual(self.store.prune(30, self.start), [1])
        self.assertIsNone(self.store.read(1, "a.md"))
        self.assertEqual(self.store.names(1), [])
        tomb = self.store.manifest(1)
        self.assertTrue(tomb["expired_at"])
        self.assertTrue(tomb["files"][0]["expired"])
        self.assertEqual(len(tomb["files"][0]["sha256"]), 64)
        self.assertFalse((self.root / "arch-docs" / "t1" / "v1" / "a.md").exists())
        self.assertEqual(self.store.read(2, "a.md"), "# v2\n")
        self.assertEqual(self.store.versions(), [1, 2, 3])

    def test_pruning_twice_changes_nothing(self) -> None:
        self.store.prune(30, self.start)
        self.assertEqual(self.store.prune(30, self.start), [])

    def test_the_latest_version_is_never_pruned(self) -> None:
        self._age(3, 900)
        self.store.prune(1, self.start)
        self.assertEqual(self.store.read(3, "a.md"), "# v3\n")
        self.assertIsNone(self.store.read(2, "a.md"))

    def test_zero_days_keeps_everything(self) -> None:
        self.assertEqual(self.store.prune(0, self.start), [])
        self.assertEqual(self.store.read(1, "a.md"), "# v1\n")

    def test_an_expired_file_is_not_carried_into_the_next_version(self) -> None:
        self.store.write("b.md", "# b\n", project=self.project, base_version="artifact@v3")
        self._age(4, 0)
        self.store.prune(30, self.start)  # v3 is now old but v4 is the latest
        self._age(3, 40)
        self.assertEqual(self.store.prune(30, self.start), [3])
        self.assertEqual(self.store.names(), ["a.md", "b.md"])

    def test_prune_all_uses_each_projects_setting_and_cleans_stale_staging(self) -> None:
        other = ArtifactStore("other", "t1", self.root)
        for _ in range(2):
            latest = other.latest()
            other.write("a.md", "# o\n", project=self.project,
                        base_version=artifact_ops.format_version(latest) if latest else None)
        old = self.root / "arch-docs" / "t1" / ".tmp-vcrashed"
        old.mkdir()
        (old / "a.md").write_text("half")
        os.utime(old, (self.start - 7200, self.start - 7200))
        fresh = self.root / "arch-docs" / "t1" / ".tmp-vfresh"
        fresh.mkdir()
        days = {"arch-docs": 30, "other": 0}
        result = artifact_ops.prune_all(lambda pid: days.get(pid), self.start, self.root)
        self.assertEqual(result, {"arch-docs": ["t1:v1"]})
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(other.read(1, "a.md"), "# o\n")

    def test_a_dry_run_reports_and_changes_nothing(self) -> None:
        self.assertEqual(self.store.prune(30, self.start, dry_run=True), [1])
        self.assertEqual(self.store.read(1, "a.md"), "# v1\n")
        old = self.root / "arch-docs" / "t1" / ".tmp-vcrashed"
        old.mkdir()
        os.utime(old, (self.start - 7200, self.start - 7200))
        result = artifact_ops.prune_all(lambda _p: 30, self.start, self.root, dry_run=True)
        self.assertEqual(result, {"arch-docs": ["t1:v1"]})
        self.assertTrue(old.exists())

    def test_prune_all_on_a_missing_root_is_empty(self) -> None:
        self.assertEqual(artifact_ops.prune_all(lambda _p: 30, self.start, self.root / "nope"), {})


class ApprovalFlowTests(unittest.TestCase):
    """judge proposal -> validation -> person approves -> new version, through the real action_ops."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db = DecisionDBManager(db_path=root / "d.db")
        self.db.save_debate_session("arch-docs", "t1", "task", [], {"approved": True})
        self.client = MCPClient(project=parse_project(DESIGN))
        self.store = ArtifactStore("arch-docs", "t1")

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for path in sorted(artifact_ops.artifacts_root().rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()

    def propose(self, **arguments: Any) -> int:
        return self.db.add_action("t1", 1, TOOL, arguments, "why", "PROPOSED", project_id="arch-docs")

    def test_an_approved_proposal_becomes_the_next_version(self) -> None:
        first = self.propose(name="overview.md", content="# One\n", title="Overview")
        result = action_ops.run_action(self.db, self.client, first, lambda _a: True, actor="alice")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual((result["version"], result["artifact"]), ("artifact@v1", "overview.md"))
        self.assertEqual(self.db.get_action(first)["status"], "EXECUTED")
        self.assertEqual(self.db.get_action(first)["decided_by"], "alice")
        self.assertEqual(self.store.read(1, "overview.md"), "# One\n")

        second = self.propose(name="overview.md", content="# Two\n", base_version="artifact@v1")
        action_ops.run_action(self.db, self.client, second, lambda _a: True, actor="alice")
        self.assertEqual(self.store.latest(), 2)
        self.assertEqual(self.store.read(1, "overview.md"), "# One\n")

    def test_declining_stores_nothing(self) -> None:
        action_id = self.propose(name="overview.md", content="# One\n")
        self.assertEqual(action_ops.run_action(self.db, self.client, action_id, lambda _a: False)["status"], "SKIPPED")
        self.assertIsNone(self.store.latest())
        self.assertEqual(self.db.get_action(action_id)["status"], "PROPOSED")

    def test_a_proposal_whose_base_moved_is_stale_at_approval(self) -> None:
        first = self.propose(name="overview.md", content="# One\n")
        stale = self.propose(name="overview.md", content="# Other\n")
        action_ops.run_action(self.db, self.client, first, lambda _a: True)
        with self.assertRaisesRegex(RuntimeError, "not the latest version"):
            action_ops.run_action(self.db, self.client, stale, lambda _a: True)
        self.assertEqual(self.store.latest(), 1)

    def test_invalid_content_is_caught_before_approval(self) -> None:
        with self.assertRaises(ActionValidationError):
            self.client.validate(TOOL, {"name": "a.svg", "content": "<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"})
        with self.assertRaises(ActionValidationError):
            self.client.validate(TOOL, {"name": "../a.md", "content": "# A\n"})
        with self.assertRaises(ActionValidationError):
            self.client.validate(TOOL, {"name": "a.md", "content": "# A\n", "base_version": "latest"})
        self.client.validate(TOOL, {"name": "a.md", "content": "# A\n"})

    def test_proposal_checks_reject_a_wrong_base_so_the_judge_can_repair_it(self) -> None:
        self.propose_and_run("a.md", "# A\n")
        proposal = {"tool": TOOL, "arguments": {"name": "a.md", "content": "# B\n", "base_version": "artifact@v5"},
                    "reason": "r"}
        problems = action_ops.invalid_proposals(self.client, [proposal], None, None, None, thread_id="t1")
        self.assertEqual(len(problems), 1)
        self.assertIn("latest", problems[0]["error"])
        self.assertTrue(problems[0]["repairable"])
        good = {**proposal, "arguments": {**proposal["arguments"], "base_version": "artifact@v1"}}
        self.assertEqual(action_ops.invalid_proposals(self.client, [good], None, None, None, thread_id="t1"), [])

    def propose_and_run(self, name: str, content: str) -> None:
        latest = self.store.latest()
        args = {"name": name, "content": content}
        if latest:
            args["base_version"] = artifact_ops.format_version(latest)
        action_ops.run_action(self.db, self.client, self.propose(**args), lambda _a: True)

    def test_the_approval_text_shows_the_content_the_target_and_the_findings(self) -> None:
        action_id = self.propose(name="deck.md", content="# A\n\n### C\n\nbody\n", title="Deck")
        text = action_ops.describe_action(self.db.get_action(action_id), self.client)
        self.assertIn("deck.md", text)
        self.assertIn("artifact@v1", text)
        self.assertIn("### C", text)
        self.assertIn("jumps from 1 to 3", text)

    def test_the_judge_prompt_names_the_current_base_version(self) -> None:
        self.assertIn("no stored version yet", design_base_rule("arch-docs", "t1"))
        self.propose_and_run("a.md", "# A\n")
        self.assertIn("artifact@v1", design_base_rule("arch-docs", "t1"))

    def test_the_judge_prompt_does_not_list_a_converted_docx(self) -> None:
        self.propose_and_run("a.md", "# A\n\ntext\n")
        self.store.convert(1, "a.md")
        self.assertIn("a.docx", self.store.names(1))
        rule = design_base_rule("arch-docs", "t1")
        self.assertIn("files: a.md)", rule)
        self.assertNotIn("docx", rule)

    def test_a_design_project_never_auto_applies_a_write_artifact_action(self) -> None:
        action_id = self.propose(name="a.md", content="# A\n")
        with patch.object(action_ops, "run_action") as run:
            self.assertEqual(action_ops.auto_run_actions(self.db, self.client, [action_id]), [])
        run.assert_not_called()
        self.assertEqual(self.db.get_action(action_id)["status"], "PROPOSED")
        self.assertIsNone(self.store.latest())


class PruneCommandTests(unittest.TestCase):
    """guardian.py --artifacts-prune takes the retention from each design project's file."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "builtin").mkdir()
        (root / "custom").mkdir()
        for pid, extra in (("kept", {"artifacts": {"retention_days": 0}}), ("short", {"artifacts": {"retention_days": 5}})):
            (root / "custom" / f"{pid}.json").write_text(json.dumps({"id": pid, "kind": "design", "name": pid, **extra}))
        (root / "custom" / "code.json").write_text(json.dumps({"id": "code", "workspace": "~/ws/code"}))
        self.manager = ProjectManager(directory=root / "custom", builtin_dir=root / "builtin")
        self.stores = {pid: ArtifactStore(pid, "t1") for pid in ("kept", "short", "code", "gone")}
        for pid, store in self.stores.items():
            for _ in range(2):
                latest = store.latest()
                store.write("a.md", "# A\n", project=parse_project(DESIGN),
                            base_version=artifact_ops.format_version(latest) if latest else None)
                old = store.directory / "v1" / "manifest.json"
                data = json.loads(old.read_text())
                data["created_at"] = "2020-01-01T00:00:00Z"
                old.write_text(json.dumps(data))

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for path in sorted(artifact_ops.artifacts_root().rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()

    def run_command(self, *flags: str) -> Dict[str, Any]:
        import contextlib
        import io

        import guardian

        args = SimpleNamespace(artifacts_prune=True, json=True, dry_run="--dry-run" in flags)
        out = io.StringIO()
        with patch("project_registry.ProjectManager", return_value=self.manager), contextlib.redirect_stdout(out):
            self.assertTrue(guardian.handle_artifacts_prune(args))
        return json.loads(out.getvalue())

    def test_design_retention_decides_and_other_projects_use_the_default(self) -> None:
        answer = self.run_command("--dry-run")
        self.assertEqual(answer, {"ok": True, "dry_run": True, "expired": {"code": ["t1:v1"], "short": ["t1:v1"]}})
        self.assertEqual(self.stores["short"].read(1, "a.md"), "# A\n")
        answer = self.run_command()
        self.assertEqual(answer["expired"], {"code": ["t1:v1"], "short": ["t1:v1"]})
        self.assertIsNone(self.stores["short"].read(1, "a.md"))
        self.assertIsNone(self.stores["code"].read(1, "a.md"))  # a report snapshot of a code project: 30 days
        self.assertEqual(self.stores["code"].read(2, "a.md"), "# A\n")  # the latest version is never pruned
        for pid in ("kept", "gone"):  # retention 0, no project file
            self.assertEqual(self.stores[pid].read(1, "a.md"), "# A\n", pid)


if __name__ == "__main__":
    unittest.main()

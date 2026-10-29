"""Tests for the standalone debate report (Markdown, Mermaid flow, SVG charts, writer)."""

import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import debate_report as dr
from debate_engine import JUDGE_DIAGRAM_HINT, DebateEngine, judge_missing_parts
from mcp_client import parse_actions, parse_decision

HOSTILE = 'Ev"il; #x <b>&</b>\nline2 %%comment {a}'


def step(round_num, agent, role, provider="claude", content="text", **extra):
    return {"round": round_num, "agent": agent, "role": role, "provider": provider, "content": content, **extra}


def sample(retrieval=True):
    rec = [{"step": 1, "op": "read", "target": "a.py", "bytes": 2048}] if retrieval else []
    return [
        step(1, "Alice", "Domain Expert", content="proposal", **({"retrievals": rec} if rec else {})),
        step(1, "Bob", "Critic Agent", content="critique"),
        step(1, "Judge", "JEV-MCP Judge", content="verdict\n```mermaid\nflowchart TD\n A-->B\n```", consensus=True),
        step(2, "Alice", "Domain Expert", content="again", fallbacks=["codex: timeout"]),
    ]


def build(transcript=None, **kw):
    args = dict(project_id="proj", thread_id="t1", task="Fix the bug\nsecond line", transcript=transcript or sample(),
                decision={"approved": True, "final_round": 1, "decision": "Do X.\nThen Y.", "checks": ["c1"],
                          "actions": [{"id": 3, "tool": "jev_mcp:apply_patch", "status": "pending", "error": None}],
                          "jev_gate": "PASSED", "jev": {"gate": "PASSED", "reasons": ["fine"]}},
                status="APPROVED", team={"id": "default"}, context={"sha": "a" * 40, "files": ["a", "b"]},
                updated="2026-10-13T10:00:00")
    args.update(kw)
    return dr.build_report(**args)


class MermaidTests(unittest.TestCase):
    def test_hostile_names_are_neutralised(self) -> None:
        transcript = [step(1, HOSTILE, 'Domain "Expert"; #', provider=HOSTILE), step(1, "Bob", HOSTILE + "Critic"),
                      step(1, "J", "Judge")]
        text = dr.mermaid_flow(transcript, status=HOSTILE)
        self.assertTrue(text.startswith("sequenceDiagram\n"))
        for line in text.splitlines()[1:]:
            self.assertNotIn(";", line)
            self.assertNotIn("#", line)
            self.assertNotIn("<", line)
            self.assertNotIn("%%", line)
            if line.strip().startswith("participant"):
                self.assertNotIn('"', line)  # labels are unquoted: mermaid would show the quotes

    def test_participants_use_alias_ids(self) -> None:
        text = dr.mermaid_flow(sample(), "APPROVED")
        self.assertIn('participant A1 as Alice (Domain Expert)', text)
        self.assertIn('participant A3 as Judge (JEV-MCP Judge)', text)
        self.assertIn("Note over A1,A3: Round 1", text)
        self.assertIn("A1->>A2: proposal (claude / 1 reads)", text)
        self.assertIn("A2->>A3: critique", text)
        self.assertIn("A3->>A1: verdict approved", text)
        self.assertTrue(text.rstrip().endswith("Decision APPROVED"))

    def test_deterministic_and_empty(self) -> None:
        self.assertEqual(dr.mermaid_flow(sample(), "X"), dr.mermaid_flow(sample(), "X"))
        empty = dr.mermaid_flow([], "REVISION_REQUIRED")
        self.assertIn("participant T", empty)

    def test_round_cap(self) -> None:
        transcript = [step(n, "A", "Domain Expert") for n in range(1, 31)]
        text = dr.mermaid_flow(transcript)
        self.assertIn("+10 more rounds", text)
        self.assertNotIn("Round 21", text)


class SvgTests(unittest.TestCase):
    def check_safe(self, svg: str) -> ET.Element:
        root = ET.fromstring(svg)
        low = svg.lower()
        for bad in ("<script", "foreignobject", "href=", "javascript:", "<image", "<style"):
            self.assertNotIn(bad, low)
        for element in root.iter():
            for attr in element.attrib:
                self.assertFalse(attr.lower().startswith("on"), attr)
        self.assertLess(len(svg.encode("utf-8")), 200 * 1024)
        self.assertIn("viewBox", root.attrib)
        self.assertIn("width", root.attrib)
        self.assertIn("height", root.attrib)
        first = root.findall("{http://www.w3.org/2000/svg}rect")[0]
        self.assertEqual(first.attrib["fill"], "#ffffff")
        return root

    def test_summary_is_safe_with_hostile_text(self) -> None:
        evil = '</text><script>alert(1)</script>" onload="x'
        transcript = [step(1, evil, evil, provider=evil, retrievals=[{"bytes": 5}])]
        svg = dr.summary_svg(transcript, status=evil, thread_id=evil)
        root = self.check_safe(svg)
        texts = " ".join("".join(t.itertext()) for t in root.iter("{http://www.w3.org/2000/svg}text"))
        self.assertIn("<script>", texts)  # present as text, never as markup

    def test_summary_shows_provider_size_and_retrievals(self) -> None:
        svg = dr.summary_svg(sample(), "APPROVED", "t1")
        self.check_safe(svg)
        self.assertIn("claude", svg)
        self.assertIn("1 retrievals", svg)
        self.assertIn("Round 2", svg)
        self.assertIn("APPROVED", svg)
        self.assertEqual(svg, dr.summary_svg(sample(), "APPROVED", "t1"))

    def test_summary_caps_rows_and_columns(self) -> None:
        transcript = [step(r, f"Agent{a}", "Critic Agent") for r in range(1, 26) for a in range(15)]
        svg = dr.summary_svg(transcript, "APPROVED", "t1")
        self.check_safe(svg)
        self.assertIn("+3 more agents", svg)
        self.assertIn("+5 more rounds", svg)
        self.assertNotIn("Round 21", svg)

    def test_retrieval_svg(self) -> None:
        self.assertEqual(dr.retrieval_svg(sample(retrieval=False)), "")
        svg = dr.retrieval_svg(sample(), "t1")
        self.check_safe(svg)
        self.assertIn("2.0 KB", svg)
        self.assertIn("Alice", svg)


class ReportTests(unittest.TestCase):
    def test_sections_and_links(self) -> None:
        md, assets = build()
        headings = [ln for ln in md.splitlines() if ln.startswith("#") and not ln.startswith("###")]
        self.assertEqual(headings, ["# 토론 결과: Fix the bug", "## 요약", "## 진행 흐름", "## 라운드 요약",
                                    "## 검색 사용량", "## 토론 기록"])
        self.assertIn("| 프로젝트 | `proj` |", md)
        self.assertIn("`aaaaaaaaaaaa` · 2 files", md)
        self.assertIn("```mermaid\nsequenceDiagram", md)
        self.assertIn("![라운드 요약](t1.assets/summary.svg)", md)
        self.assertIn("![에이전트별 검색 바이트](t1.assets/retrieval.svg)", md)
        self.assertIn("### Round 1 · Alice (Domain Expert)", md)
        self.assertIn("> skipped codex: timeout", md)
        self.assertIn("flowchart TD\n A-->B", md)  # agent mermaid block is verbatim
        self.assertIn("### 판결\n\nDo X.\nThen Y.", md)
        self.assertNotIn("\n> Do X.", md)  # the verdict is body text, not a greyed-out quote
        self.assertIn("jev_mcp:apply_patch", md)
        self.assertEqual(sorted(assets), ["retrieval.svg", "summary.svg"])
        for name in assets:
            self.assertRegex(name, dr.ASSET_NAME_RE)

    def test_links_match_the_dashboard_form(self) -> None:
        md, assets = build()
        links = re.findall(r"!\[[^\]]*\]\(([^)]*)\)", md)
        self.assertEqual(sorted(links), sorted(f"t1.assets/{n}" for n in assets))

    def test_no_retrieval_omits_section_and_asset(self) -> None:
        md, assets = build(sample(retrieval=False))
        self.assertNotIn("## 검색 사용량", md)
        self.assertNotIn("retrieval.svg", md)
        self.assertEqual(list(assets), ["summary.svg"])

    def test_no_decision_and_no_team(self) -> None:
        md, _ = build(decision=None, status="REVISION_REQUIRED", team=None, context=None)
        self.assertIn("최종 결정이 기록되지 않았습니다", md)
        self.assertIn("| 팀 | - |", md)

    def test_deterministic(self) -> None:
        self.assertEqual(build(), build())

    def test_agent_headings_stay_below_the_round_heading(self) -> None:
        content = "# 제안\n\n## 배경\ntext\n```bash\n# comment\n```\n### 세부"
        md, _ = build([step(1, "Alice", "Domain Expert", content=content)])
        self.assertIn("#### 제안\n\n##### 배경\ntext\n```bash\n# comment\n```\n###### 세부", md)
        self.assertNotIn("\n# 제안", md)
        self.assertNotIn("\n## 배경", md)

    def test_rounds_are_separated(self) -> None:
        md, _ = build()
        record = md.split("## 토론 기록", 1)[1]
        self.assertEqual(record.count("\n---\n"), len(sample()) - 1)

    def test_verdict_items_are_separate_paragraphs(self) -> None:
        decision = {"decision": "intro\n[R1] first: a\n[R2] second: b\n[추가] more", "rationale": "## 근거\n(1) one\n(2) two\n## 대안\nx",
                    "final_round": 1}
        md, _ = build(decision=decision)
        self.assertIn("### 판결\n\nintro\n\n**[R1]** first: a\n\n**[R2]** second: b\n\n**[추가]** more", md)
        self.assertIn("### 근거\n\n(1) one\n\n(2) two\n#### 대안\nx", md)  # no quote, no repeated heading
        self.assertIn("| R1 | first | 0 | 0 | 변경 없음 |", md)  # the trace still reads the raw markers

    def test_demote_headings_helper(self) -> None:
        self.assertEqual(dr.demote_headings("no headings\n#hashtag"), "no headings\n#hashtag")
        self.assertEqual(dr.demote_headings("## a\n#### b"), "#### a\n###### b")
        self.assertEqual(dr.demote_headings("~~~\n# x\n~~~\n# y"), "~~~\n# x\n~~~\n#### y")


class WriterTests(unittest.TestCase):
    def test_write_and_stale_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            md, assets = build()
            path = dr.write_report(root, "t1", md, assets)
            self.assertEqual(path, root / "t1.md")
            self.assertEqual(path.read_text(encoding="utf-8"), md)
            adir = root / "t1.assets"
            self.assertEqual(sorted(p.name for p in adir.iterdir()), ["retrieval.svg", "summary.svg"])
            (adir / "old_chart.svg").write_text("<svg/>")
            (adir / "notes.txt").write_text("keep")
            (adir / "Bad Name.svg").write_text("keep")
            md2, assets2 = build(sample(retrieval=False))
            dr.write_report(root, "t1", md2, assets2)
            self.assertEqual(sorted(p.name for p in adir.iterdir()), ["Bad Name.svg", "notes.txt", "summary.svg"])
            self.assertEqual([p.name for p in root.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".")], [])

    def test_symlinks_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            secret = outside / "secret.svg"
            secret.write_text("secret")
            adir = root / "t1.assets"
            adir.mkdir()
            (adir / "stale.svg").symlink_to(secret)
            md, assets = build(sample(retrieval=False))
            dr.write_report(root, "t1", md, assets)
            self.assertEqual(secret.read_text(), "secret")
            # A symlinked assets directory is left alone and nothing is written through it.
            other = root / "t2.assets"
            other.symlink_to(outside, target_is_directory=True)
            dr.write_report(root, "t2", md, assets)
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["secret.svg"])
            self.assertTrue((root / "t2.md").exists())

    def test_rejects_bad_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                dr.write_report(Path(tmp), "t1", "x", {"../evil.svg": "<svg/>"})
            with self.assertRaises(ValueError):
                dr.write_report(Path(tmp), "../t1", "x", {})

    def test_overwrite_is_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dr.write_report(root, "t1", "old", {})
            with patch("debate_report.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    dr.write_report(root, "t1", "new", {})
            self.assertEqual((root / "t1.md").read_text(), "old")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["t1.md"])


class EngineIntegrationTests(unittest.TestCase):
    def test_save_debate_log_writes_report_json_and_latest(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GUARDIAN_DATA_DIR": tmp, "HOME": tmp}):
                root = Path(tmp) / "decisions"
                engine = DebateEngine.__new__(DebateEngine)
                engine.db = type("DB", (), {"save_debate_session": lambda self, **k: None})()
                with patch("debate_engine.decisions_dir", return_value=root):
                    path = engine._save_debate_log("proj", "t1", "task", sample(), {"approved": True}, None, None)
                self.assertEqual(path, root / "projects" / "proj" / "t1.md")
                md = path.read_text(encoding="utf-8")
                self.assertTrue(md.startswith("# 토론 결과: task"))
                self.assertEqual((root / "latest_debate_transcript.md").read_text(encoding="utf-8"), md)
                self.assertTrue((root / "projects" / "proj" / "t1.assets" / "summary.svg").exists())
                index = json.loads((root / "projects" / "proj" / "threads_index.json").read_text())
                self.assertEqual(index["t1"]["status"], "APPROVED")
                self.assertTrue((root / "projects" / "proj" / "t1.json").exists())


class JudgeDiagramTests(unittest.TestCase):
    ANSWER = (
        "Rejected alternatives are described below.\n```mermaid\nflowchart TD\n  A{VERDICT: REJECTED} --> B\n```\n"
        '```json\n{"decision": "Use X", "rationale": "r", "constraints": ["c"], "checks": [], '
        '"actions": [{"tool": "t", "arguments": {}, "reason": "why"}]}\n```\n'
        "VERDICT: APPROVED"
    )

    def test_mermaid_block_does_not_disturb_parsing(self) -> None:
        self.assertTrue(DebateEngine._parse_verdict(self.ANSWER))
        self.assertEqual(judge_missing_parts(self.ANSWER), [])
        self.assertEqual(parse_decision(self.ANSWER), "Use X")
        self.assertEqual(len(parse_actions(self.ANSWER)), 1)

    def test_hint_is_optional_and_single(self) -> None:
        self.assertIn("MAY", JUDGE_DIAGRAM_HINT)
        self.assertIn("at most one", JUDGE_DIAGRAM_HINT)
        self.assertIn("mermaid", JUDGE_DIAGRAM_HINT)


class DraftRoundTests(unittest.TestCase):
    def test_report_handles_two_expert_entries_in_one_round(self) -> None:
        transcript = [
            step(1, "Alice", "Domain Expert", content="draft one", draft="A"),
            step(1, "Dan", "Domain Expert", provider="codex", content="draft two", draft="B"),
            step(1, "Bob", "Critic Agent", content="critique"),
            step(1, "Judge", "JEV-MCP Judge", content="verdict", consensus=True),
        ]
        markdown, assets = build(transcript)
        self.assertIn("draft one", markdown)
        self.assertIn("draft two", markdown)
        for name, svg in assets.items():
            ET.fromstring(svg)
        flow = dr.mermaid_flow(transcript, "APPROVED")
        self.assertIn("Dan", flow)


if __name__ == "__main__":
    unittest.main()


class DecisionParagraphTests(unittest.TestCase):
    def test_paragraphs_and_list_lines_are_kept(self) -> None:
        block = {"decision": "Approve with changes.\n\n\n\n[1]  Security:   safe_load.\n[2] Clock:\tsim time.\n   \n- keep   lists\n", "actions": []}
        answer = "Judgment.\n```json\n" + json.dumps(block) + "\n```\nVERDICT: APPROVED"
        self.assertEqual(parse_decision(answer),
                         "Approve with changes.\n\n[1] Security: safe_load.\n[2] Clock: sim time.\n\n- keep lists")

    def test_single_line_decision_is_unchanged(self) -> None:
        answer = 'x\n```json\n{"decision": "  Use   the wrapper. ", "actions": []}\n```\nVERDICT: APPROVED'
        self.assertEqual(parse_decision(answer), "Use the wrapper.")

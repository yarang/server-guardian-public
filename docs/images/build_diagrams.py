#!/usr/bin/env python3
"""
Draws the system diagrams of agent.md in English (name.svg) and Korean (name.ko.svg) from one description, so the two
languages always show the same picture. Standard library only; run it after changing a label or the system:

    python3 docs/images/build_diagrams.py

Every label is a pair {"en": ..., "ko": ...}; text that is the same in both languages (paths, routes, tool names) is a
plain string. Long labels are wrapped by an estimate of their width (Hangul is about twice as wide as Latin text).
White background, so the pictures read the same in light and dark themes.
"""

from pathlib import Path
from typing import Dict, List, Sequence, Tuple, Union
from xml.sax.saxutils import escape

Label = Union[str, Dict[str, str]]
OUT = Path(__file__).resolve().parent
FONT = "system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Noto Sans KR', sans-serif"
MONO = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
INK, MUTED, LINE = "#1f2937", "#4b5563", "#94a3b8"
TONES = {  # fill, stroke, title colour
    "hub": ("#eef2ff", "#6366f1", "#3730a3"),
    "node": ("#f0fdf4", "#22c55e", "#166534"),
    "admin": ("#f8fafc", "#64748b", "#334155"),
    "git": ("#fff7ed", "#f97316", "#9a3412"),
    "low": ("#f0fdf4", "#22c55e", "#166534"),
    "medium": ("#fffbeb", "#f59e0b", "#92400e"),
    "high": ("#fef2f2", "#ef4444", "#991b1b"),
    "neutral": ("#ffffff", "#cbd5e1", "#334155"),
}


def say(label: Label, lang: str) -> str:
    return label if isinstance(label, str) else label[lang]


def width_of(text: str, size: float) -> float:
    return sum(size * (1.0 if "가" <= ch <= "힣" else 0.56) for ch in text)


def wrap(text: str, size: float, max_width: float) -> List[str]:
    """Greedy word wrap by estimated width; a single long word stays on its own line."""
    lines: List[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}".strip()
            if line and width_of(candidate, size) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines


class Svg:
    def __init__(self, width: int, height: int, lang: str):
        self.width, self.height, self.lang, self.parts = width, height, lang, []

    def rect(self, x, y, w, h, tone="neutral", r=10, dash=False):
        fill, stroke, _ = TONES[tone]
        extra = ' stroke-dasharray="6 4"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}"'
                          f' stroke-width="1.5"{extra}/>')

    def text(self, x, y, label: Label, size=13, weight=400, fill=INK, anchor="start", mono=False, max_width=None,
             line_height=1.35) -> float:
        """Writes (wrapped) text with its first baseline at y; returns the y below the last line."""
        content = say(label, self.lang)
        lines = wrap(content, size, max_width) if max_width else content.split("\n")
        family = MONO if mono else FONT
        for i, line in enumerate(lines):
            self.parts.append(f'<text x="{x}" y="{y + i * size * line_height:.1f}" font-family="{family}" '
                              f'font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">'
                              f'{escape(line)}</text>')
        return y + len(lines) * size * line_height

    def arrow(self, x1, y1, x2, y2, label: Label = "", dash=False, label_dy=-6, head=True):
        extra = (' stroke-dasharray="5 4"' if dash else "") + (' marker-end="url(#arrow)"' if head else "")
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{LINE}" stroke-width="1.8"'
                          f'{extra}/>')
        if label:
            if x1 == x2:  # vertical: the label goes beside the line, not on it
                self.text(x1 + 8, (y1 + y2) / 2 + 4, label, size=11, fill=MUTED)
            else:
                self.text((x1 + x2) / 2, (y1 + y2) / 2 + label_dy, label, size=11, fill=MUTED, anchor="middle")

    def card(self, x, y, w, h, title: Label, lines: Sequence[Label] = (), tone="neutral", mono_lines=False,
             size=12, dash=False):
        self.rect(x, y, w, h, tone, dash=dash)
        _, _, title_colour = TONES[tone]
        ty = self.text(x + 14, y + 24, title, size=14, weight=700, fill=title_colour, max_width=w - 28)
        for line in lines:
            ty = self.text(x + 14, ty + 4, line, size=size, fill=INK, mono=mono_lines, max_width=w - 28)

    def header(self, title: Label, subtitle: Label):
        self.text(self.width / 2, 34, title, size=20, weight=700, anchor="middle")
        self.text(self.width / 2, 58, subtitle, size=13, fill=MUTED, anchor="middle", max_width=self.width - 80)

    def save(self, name: str) -> Path:
        path = OUT / (f"{name}.svg" if self.lang == "en" else f"{name}.ko.svg")
        body = "\n".join(self.parts)
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" width="{self.width}" '
            f'height="{self.height}" role="img" lang="{self.lang}">\n'
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{LINE}"/></marker></defs>\n'
            f'<rect width="100%" height="100%" fill="#ffffff"/>\n{body}\n</svg>\n', encoding="utf-8")
        return path


def L(en: str, ko: str) -> Dict[str, str]:
    return {"en": en, "ko": ko}


# --- 1. system architecture --------------------------------------------------------------------------------------

def system_architecture(lang: str) -> Path:
    s = Svg(1000, 640, lang)
    s.header(L("Server Guardian: one hub, a fleet reached over SSH", "Server Guardian: 허브 하나, SSH로 다루는 플릿"),
             L("Only the hub runs the agent. Other servers run no agent; LLM CLIs on Tier-1 servers get Guardian skills.",
               "에이전트는 허브에서만 돈다. 다른 서버에는 에이전트가 없고, Tier-1 서버의 LLM CLI에는 Guardian skill이 있다."))
    s.card(24, 96, 230, 300, L("Admin Mac", "관리용 Mac"), [
        L("scripts/ci.sh: the CI gate", "scripts/ci.sh: CI 관문"),
        L("install-agent-service.sh: deploy the hub", "install-agent-service.sh: 허브 배포"),
        L("deploy-llm-env.sh, deploy-llm-skills.sh", "deploy-llm-env.sh, deploy-llm-skills.sh"),
        L("export-public.sh: masked public copy", "export-public.sh: 가린 공개 사본"),
        L("Dashboard in a browser or the Tauri app", "대시보드: 브라우저 또는 Tauri 앱"),
    ], tone="admin")
    s.rect(300, 80, 400, 420, "hub", r=14)
    s.text(500, 106, L("Hub (site-a-arm1)", "허브 (site-a-arm1)"), size=16, weight=700, fill=TONES["hub"][2],
           anchor="middle")
    s.card(318, 120, 364, 92, L("Go agent :9870", "Go agent :9870"), [
        L("Dashboard, REST and SSE API, audit log", "대시보드, REST·SSE API, audit 로그"),
        L("Bearer AGENT_TOKEN or session cookie", "Bearer AGENT_TOKEN 또는 세션 쿠키"),
    ])
    s.card(318, 222, 364, 112, L("Debate engine (Python CLI)", "토론 엔진 (Python CLI)"), [
        L("Expert, critics, judge; jev gate", "전문가, 비판자, 판정자; jev 관문"),
        L("Reads code (repo blocks, MCP) or checks servers (ops blocks)", "코드 읽기(repo 블록, MCP) 또는 서버 점검(ops 블록)"),
        L("Actions: measure, run, verify", "조치: 측정, 실행, 검증"),
    ])
    s.card(318, 344, 176, 142, L("~/.guardian/data", "~/.guardian/data"), [
        L("Decision DB, projects, teams, debate jobs, code index, wiki", "결정 DB, 프로젝트, 팀, 토론 작업, 코드 인덱스, 위키"),
    ], size=11)
    s.card(506, 344, 176, 142, L("systemd timers", "systemd 타이머"), [
        L("02:30 UTC ops watch", "02:30 UTC 야간 점검"),
        L("03:30 UTC DB backup", "03:30 UTC DB 백업"),
        L("LLM CLIs: claude, codex, agy", "LLM CLI: claude, codex, agy"),
    ], size=11)
    s.card(748, 96, 228, 150, L("Tier-1 servers", "Tier-1 서버"), [
        L("LLM CLIs with Guardian skills (host check, host cleanup)", "Guardian skill이 있는 LLM CLI (호스트 점검·정리)"),
        L("No agent; one keeps the backup copy", "에이전트 없음; 한 대가 백업 사본 보관"),
    ], tone="node")
    s.card(748, 270, 228, 120, L("Tier-2 servers", "Tier-2 서버"), [
        L("SSH targets only: checks, commands, approved actions", "SSH 대상만: 점검, 명령, 승인된 조치"),
    ], tone="node")
    s.card(24, 528, 300, 88, L("Gitea (source of truth)", "Gitea (원본)"), [
        L("git push also updates the private GitHub mirror", "git push가 비공개 GitHub 미러도 갱신"),
    ], tone="git")
    s.card(348, 528, 300, 88, L("Release and checkouts", "릴리스와 체크아웃"), [
        L("~/.guardian/current (the running release), ~/server-guardian and ~/guardian-workspace (debates)", "~/.guardian/current (실행 중인 릴리스), ~/server-guardian과 ~/guardian-workspace (토론용)"),
    ], tone="git")
    s.card(672, 528, 304, 88, L("Public copy on GitHub", "GitHub 공개 사본"), [
        L("export-public.sh on the admin Mac rewrites the whole history with private names masked",
          "관리용 Mac의 export-public.sh가 사적 이름을 가려 전체 이력을 다시 씀"),
    ], tone="git", dash=True)
    s.arrow(254, 200, 300, 200, L("Tailscale", "Tailscale"))
    s.arrow(700, 170, 748, 170, L("fleet SSH", "플릿 SSH"))
    s.arrow(700, 330, 748, 330, L("fleet SSH", "플릿 SSH"))
    s.arrow(140, 396, 140, 528, L("git push", "git push"), label_dy=0)
    s.arrow(324, 572, 348, 572)
    s.arrow(500, 528, 500, 500, L("release on deploy", "배포 때 릴리스"), label_dy=0)
    return s.save("system_architecture")


# --- 2. hub API -----------------------------------------------------------------------------------------------------

API_GROUPS: List[Tuple[Label, List[str]]] = [
    (L("Status and metrics", "상태와 지표"), ["GET  /api/status", "GET  /api/metrics", "GET  /api/stream/metrics (SSE)"]),
    (L("sshd of the hub", "허브의 sshd"), ["GET  /api/ssh/config", "GET  /api/ssh/security", "GET  /api/ssh/keys"]),
    (L("Fleet", "플릿"), ["GET  /api/fleet/diagnose", "GET  /api/fleet/ssh", "POST /api/fleet/exec (/stream)"]),
    (L("Commands and audit", "명령과 audit"), ["POST /api/exec, /api/exec/remote", "GET  /api/audit"]),
    (L("Nodes", "노드"), ["GET /api/nodes, /api/nodes/registration", "POST /api/nodes/check", "POST /api/nodes, /api/nodes/{id}/probe",
                          "PUT, DELETE /api/nodes/{id}; revoke-key", "GET, POST /api/mesh/join, /api/mesh/nodes(/{id}/remove)"]),
    (L("Groups", "그룹"), ["GET, POST /api/groups", "POST /api/groups/preview", "DELETE /api/groups/{id}"]),
    (L("Debates", "토론"), ["POST, GET /api/debates", "GET  /api/debates/{id}(/events, /prompts/{seq})",
                            "DELETE /api/debates/{id} (cancel)"]),
    (L("Actions and threads", "조치와 스레드"), ["GET  /api/actions(/{id})", "POST /api/actions/{id}/run, reject, fail-stale",
                                           "POST /api/threads/{t}/publish, close, jev-recheck", "GET  /api/threads/{t}/report"]),
    (L("Projects, teams, personas, code", "프로젝트, 팀, 페르소나, 코드"), [
        "GET, POST, DELETE /api/projects | teams | personas", "POST /api/projects/{id}/sync, workspace-reset",
        "GET  /api/projects/{id}/files, wiki"]),
    (L("Sign-in", "로그인"), ["POST /api/auth/login", "POST /api/auth/logout"]),
]


def hub_api(lang: str) -> Path:
    s = Svg(1000, 784, lang)
    s.header(L("Hub API (port 9870)", "허브 API (포트 9870)"),
             L("Listens on 127.0.0.1 and the hub's Tailscale IP only. Every /api/ route passes withSecurity(withAuth(mux)): "
               "Bearer AGENT_TOKEN, or the session cookie the dashboard gets at sign-in.",
               "127.0.0.1과 허브의 Tailscale IP에서만 받는다. 모든 /api/ 경로는 withSecurity(withAuth(mux))를 거친다: "
               "Bearer AGENT_TOKEN, 또는 대시보드가 로그인 때 받는 세션 쿠키."))
    col_w, row_h, top = 466, 118, 96
    for i, (title, routes) in enumerate(API_GROUPS):
        x = 24 + (i % 2) * (col_w + 20)
        y = top + (i // 2) * (row_h + 12)
        s.card(x, y, col_w, row_h, title, routes, mono_lines=True, size=11)
    s.text(500, 762, L("Debates, actions, threads, groups, projects, teams, personas, files and the wiki run guardian.py; "
                       "commands on /api/exec* are written to the audit log.",
                       "토론, 조치, 스레드, 그룹, 프로젝트, 팀, 페르소나, 파일, 위키 경로는 guardian.py를 실행한다; "
                       "/api/exec* 명령은 audit 로그에 남는다."),
           size=11, fill=MUTED, anchor="middle", max_width=940)
    return s.save("hub_api")


# --- 3. debate pipeline --------------------------------------------------------------------------------------------

def debate_pipeline(lang: str) -> Path:
    s = Svg(1000, 600, lang)
    s.header(L("A debate: from a task to verified actions", "토론: 과제에서 검증된 조치까지"),
             L("Code projects read a repository; ops projects check servers. Nothing runs before it is approved, by a person "
               "or by a policy.",
               "코드 프로젝트는 저장소를 읽고, ops 프로젝트는 서버를 점검한다. 사람이나 정책이 승인하기 전에는 아무것도 실행되지 않는다."))
    boxes = [
        (L("Task", "과제"), [L("Dashboard, CLI or the nightly ops watch", "대시보드, CLI 또는 야간 점검")]),
        (L("Expert", "전문가"), [L("Proposal", "제안")]),
        (L("Critics", "비판자"), [L("Critiques, in parallel", "병렬 비판")]),
        (L("Judge", "판정자"), [L("Verdict and actions", "판정과 조치")]),
        (L("jev gate", "jev 관문"), [L("Independent check of the decision", "결정에 대한 독립 검토")]),
    ]
    w, gap, y = 172, 22, 92
    for i, (title, lines) in enumerate(boxes):
        x = 24 + i * (w + gap)
        s.card(x, y, w, 96, title, lines, tone="hub" if 1 <= i <= 3 else "neutral")
        if i:
            s.arrow(x - gap, y + 48, x, y + 48)
    s.rect(218, 202, 660, 88, "admin", dash=True)
    s.text(232, 228, L("Every agent can look before it answers:", "모든 에이전트는 답하기 전에 직접 볼 수 있다:"),
           size=12, weight=700, fill=TONES["admin"][2])
    ny = s.text(232, 248, L("```repo blocks or MCP tools (code projects) · ```ops checks (ops projects) · citations are checked",
                       "```repo 블록 또는 MCP 도구 (코드 프로젝트) · ```ops 점검 (ops 프로젝트) · 인용 검사"),
           size=12, max_width=630)
    s.text(232, ny + 2, L("Rounds repeat until the gate approves or the team's round limit is reached.",
                       "승인되거나 팀의 라운드 한도에 이를 때까지 라운드를 반복한다."),
           size=11, fill=MUTED, max_width=630)
    y2 = 312
    s.card(24, y2, 210, 136, L("Recorded actions", "기록된 조치"), [
        L("PROPOSED, with a measured preview", "PROPOSED, 측정한 미리보기와 함께"),
        L("HELD while jev has not passed", "jev 통과 전에는 HELD"),
        L("INVALID: outside the project or policy", "INVALID: 프로젝트·정책 밖"),
    ], size=11)
    s.card(256, y2, 240, 136, L("Approval", "승인"), [
        L("A person, in the dashboard or the CLI", "사람: 대시보드 또는 CLI"),
        L("Code edits after jev PASSED (auto:engine)", "jev 통과 후 코드 수정 (auto:engine)"),
        L("Low-risk ops actions in policy.auto_run (auto:<project>)", "policy.auto_run의 low ops 조치 (auto:<project>)"),
    ], size=11)
    s.card(518, y2, 230, 136, L("Run", "실행"), [
        L("One action per host at a time", "호스트당 한 번에 하나"),
        L("Measured again: changed target → STALE", "다시 측정: 대상이 바뀌면 STALE"),
        L("Through the hub API, in the audit log", "허브 API를 거쳐 audit에 기록"),
    ], size=11)
    s.card(770, y2, 206, 136, L("Result", "결과"), [
        L("EXECUTED or FAILED", "EXECUTED 또는 FAILED"),
        L("Verify: before, after, freed", "검증: 실행 전, 후, 확보량"),
        L("Code: publish a branch after tests", "코드: 테스트 후 브랜치 게시"),
    ], size=11)
    s.arrow(900, 188, 900, 300, head=False)
    s.text(908, 246, L("approved", "승인됨"), size=11, fill=MUTED)
    s.arrow(900, 300, 129, 300, dash=True, head=False)
    s.arrow(129, 300, 129, y2)
    for x1, x2 in ((234, 256), (496, 518), (748, 770)):
        s.arrow(x1, y2 + 64, x2, y2 + 64)
    s.rect(24, 470, 952, 102, "neutral")
    s.text(40, 496, L("Where the rules live", "규칙이 있는 곳"), size=13, weight=700)
    s.text(40, 518, L("Teams and personas: who argues and which tools the judge may propose (configs/teams, configs/personas). "
                      "Projects: what the agents read or which servers they may touch, and the ops policy (configs/projects).",
                      "팀과 페르소나: 누가 토론하고 판정자가 어떤 도구를 제안할 수 있는지 (configs/teams, configs/personas). "
                      "프로젝트: 에이전트가 읽는 대상 또는 다룰 수 있는 서버, 그리고 ops 정책 (configs/projects)."),
           size=12, max_width=920)
    return s.save("debate_pipeline")


# --- 4. action risk policy -----------------------------------------------------------------------------------------

def action_risk_policy(lang: str) -> Path:
    s = Svg(1000, 680, lang)
    s.header(L("Actions: risk, approval and guards", "조치: 위험도, 승인, 안전장치"),
             L("Risk is computed per action (docker_prune depends on its scope). Every server command goes through the hub "
               "API and the audit log.",
               "위험도는 조치마다 계산한다 (docker_prune은 범위에 따라 다르다). 모든 서버 명령은 허브 API와 audit 로그를 거친다."))
    cols = [(24, 110), (134, 300), (434, 170), (604, 186), (790, 186)]
    heads = [L("Risk", "위험도"), L("Tools", "도구"), L("Approval", "승인"), L("Runs by itself", "자동 실행"),
             L("Guards", "안전장치")]
    top = 84
    for (x, w), head in zip(cols, heads):
        s.text(x + 8, top + 18, head, size=13, weight=700, fill=MUTED)
    rows = [
        ("low", "LOW", ["ops:apt_clean", "ops:journal_vacuum", "ops:docker_prune",
                        L("  (dangling images, build cache)", "  (dangling 이미지, build cache)")],
         L("A person ticks a confirmation box", "사람이 확인란을 체크"),
         L("Only if listed in the project's policy.auto_run, after a real jev PASSED, under a daily cap",
           "프로젝트 policy.auto_run에 있을 때만, 실제 jev 통과 후, 하루 한도 안에서"),
         L("Measured preview; drift → STALE; verify after; one action per host",
           "측정한 미리보기; 바뀌면 STALE; 실행 후 검증; 호스트당 하나")),
        ("medium", "MEDIUM", ["ops:remove_paths", "ops:truncate_logs", "ops:remove_old_kernels", "ops:restart_service",
                              "ops:docker_prune", L("  (stopped containers, unused images)", "  (중지된 컨테이너, 미사용 이미지)"),
                              "jev_mcp:apply_patch", "jev_mcp:execute_code_action"],
         L("A person ticks a confirmation box", "사람이 확인란을 체크"),
         L("Code edits only: applied after jev PASSED (not with run_test). Ops: never",
           "코드 수정만: jev 통과 후 적용 (run_test 제외). ops: 안 됨"),
         L("Protected paths and units; code edits only on code the judge read; base-commit and workspace locks",
           "보호 경로·유닛; 판정자가 읽은 코드만 수정; 기준 커밋·워크스페이스 잠금")),
        ("high", "HIGH", ["jev_mcp:server_remediation", "jev_mcp:run_tests"],
         L("A person types the action number", "사람이 조치 번호를 직접 입력"),
         L("Never", "안 됨"),
         L("Only target hosts of the project, the hub only as local; allow-listed test commands",
           "프로젝트의 대상 서버만, 허브는 local로만; 허용 목록의 테스트 명령만")),
    ]
    y = top + 30
    heights = [140, 210, 110]
    for (tone, badge, tools, approval, auto, guards), h in zip(rows, heights):
        s.rect(24, y, 952, h, tone, r=8)
        s.text(cols[0][0] + 10, y + 26, badge, size=15, weight=700, fill=TONES[tone][2])
        ty = y + 24
        for tool in tools:
            ty = s.text(cols[1][0] + 8, ty, tool, size=12, mono=True) + 1
        s.text(cols[2][0] + 8, y + 24, approval, size=12, max_width=cols[2][1] - 16)
        s.text(cols[3][0] + 8, y + 24, auto, size=12, max_width=cols[3][1] - 16)
        s.text(cols[4][0] + 8, y + 24, guards, size=12, max_width=cols[4][1] - 16)
        y += h + 10
    s.text(500, y + 22, L("Every proposal is validated first: an unknown tool, a host outside the project or a protected path "
                          "is recorded INVALID and never runs. Off switches: GUARDIAN_AUTO_APPLY=off, GUARDIAN_UI_ACTIONS=off.",
                          "모든 제안은 먼저 검증한다: 모르는 도구, 프로젝트 밖의 서버, 보호 경로는 INVALID로 기록되고 실행되지 않는다. "
                          "끄는 스위치: GUARDIAN_AUTO_APPLY=off, GUARDIAN_UI_ACTIONS=off."),
           size=11, fill=MUTED, anchor="middle", max_width=940)
    return s.save("action_risk_policy")


if __name__ == "__main__":
    for draw in (system_architecture, hub_api, debate_pipeline, action_risk_policy):
        for language in ("en", "ko"):
            print(draw(language).relative_to(OUT.parent.parent))

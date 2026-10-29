# 대시보드 React 전환과 디자인 시스템 적용 (설계)

[English](dashboard-react-migration.md) | 한국어

> 2026-10-18: 이 전환은 끝났다. 이후 화면 구성은 [ui-flow-redesign.ko.md](ui-flow-redesign.ko.md)대로 다시 짜였고(아래 "구조" 표의 메모), 문구는 언어별 카탈로그로 옮겨졌다([dashboard-i18n.ko.md](dashboard-i18n.ko.md)). 아래 본문은 전환 당시의 설계다.

## 목표

`apps/web-desktop`(Vanilla TypeScript, 약 4,100줄, innerHTML 문자열 렌더링)을 React 18 앱으로 다시 만들고,
모든 화면을 디자인 시스템 `@server-guardian/ui`(`packages/guardian-ui`) 컴포넌트로 구성한다.

- 화면 일관성: 같은 토큰(`--sg-*`), 글꼴(Inter와 한글용 IBM Plex Sans KR, JetBrains Mono. [dashboard-notion-theme.ko.md](dashboard-notion-theme.ko.md) 참고), 상태 색 규칙(`ok/warn/crit/info/idle`)
- 보안: React가 텍스트를 기본 이스케이프하므로 innerHTML 기반 XSS 표면이 사라진다. `dangerouslySetInnerHTML`은 쓰지 않는다
  (2026-10-18: Markdown 문서(보고서, 위키)를 그리는 `MarkdownDoc.tsx`만 marked로 만든 HTML과 Mermaid SVG를 DOMPurify(`lib/report.ts`)로 정리한 뒤 `innerHTML`에 넣는다. `dangerouslySetInnerHTML`은 여전히 쓰지 않는다.)
- 정직성: 2026-10-04 핫픽스의 원칙을 유지한다. API 실패는 오류로 보여 주고 가짜 데이터로 대체하지 않는다

## 구조

| 항목 | 결정 |
|---|---|
| 의존성 | `"@server-guardian/ui": "file:../../packages/guardian-ui"`, `react`/`react-dom` 18. 앱 빌드 전에 디자인 시스템을 빌드한다(`prebuild`). Vite `resolve.dedupe: ['react','react-dom']`로 React 중복 로드를 막는다 |
| 재사용 | `src/api.ts`(허브 API, 인증), `src/types.ts`는 그대로 쓴다 |
| 진입점 | `src/main.tsx` → `<GuardianRoot>` 안에 `<App>` |
| 탐색 | 데스크톱 `Sidebar`, 760px 미만 `TabBar` (디자인 시스템이 전환을 처리). URL 해시(`#/fleet` 등)로 탭 상태를 유지 |
| 화면 | `src/pages/`의 `FleetPage`, `SshPage`, `AssistantPage`, `DebatePage`(팀·페르소나·토론·액션), `BatchPage`, `TerminalPage` (2026-10-18: 지금은 라우트가 넷이다: `#/fleet`(탭: 요약, 노드, SSH, 명령, 그룹), `#/debates`, `#/approvals`, `#/settings`(허브 연결, 프로젝트, 페르소나, 팀). `AssistantPage`, `BatchPage`, `TerminalPage`, `DebatePage`는 없어졌고 `OverviewPage`, `NodesPage`, `RunPage`, `DebatesPage`, `ApprovalsPage`, `SettingsPage` 등이 그 역할을 맡는다. `src/router.ts`, [ui-flow-redesign.ko.md](ui-flow-redesign.ko.md)) |
| 데이터 | `src/hooks/`의 작은 훅(`useHubData`, 폴링). 실패는 `{error}` 상태로 화면에 표시 (2026-10-18: 여러 화면이 같이 쓰는 데이터는 `src/state/`의 스토어가 가진다) |
| 인증 | 기존 흐름 유지(같은 출처면 HttpOnly 쿠키, Tauri면 토큰). 로그인 창과 에이전트 주소 설정은 디자인 시스템 `TextField`, `Button`, `Panel`로 만든다 |
| 글꼴 | 디자인 시스템이 woff2를 직접 제공하므로 Google Fonts를 제거하고, CSP(`apps/agent/ui.go`, `tauri.conf.json`)에서 googleapis/gstatic을 뺀다 |
| 테스트 | Vitest + Testing Library(jsdom)로 화면별 렌더링과 오류 표시, 액션 승인 확인 단계 테스트 |
| 제거 | `main.ts`, `debate.ts`, `actions.ts`, `icons.ts`, `index.css`, `escape.ts`와 그 테스트(React 이스케이프로 대체) |

## AI Multi-Agent Center → Assistant

지금 탭은 진단 수치만 실제이고, "AI 종합 의견"과 제안 액션는 코드에 고정된 문장이다. 전환하면서:

- 실제 데이터만 보여 준다: `fleet/diagnose` 결과(그룹별 온라인 수, 소요 시간), SSH 보안 점수와 규칙
- 고정 문구와 하드코딩된 액션는 삭제한다
- "이 내용으로 토론하기" 버튼이 Debate 화면의 작업 입력을 미리 채운다. 분석과 액션는 실제 LLM 토론과 jev 관문, 승인 흐름을 거친다

(2026-10-18: Assistant 탭은 이후 UI 흐름 재설계에서 없어졌다. 토론 미리 채우기는 플릿·노드·그룹 화면에서 `setDebatePrefill`(`src/state/debatePrefill.ts`)로 한다.)

## 단계

1. 앱 골격(의존성, 진입점, 탐색, 인증, 공용 훅)과 Fleet, SSH, Assistant, Batch, Terminal 화면
2. Debate 화면(페르소나·팀 편집, 토론 실행·조회, 코드 묶음, 결과, 액션 승인·diff)
3. 기존 Vanilla 파일 제거, CSP 정리, 설치 스크립트가 디자인 시스템을 먼저 빌드하도록 확인, 로컬 브라우저 검증, 배포

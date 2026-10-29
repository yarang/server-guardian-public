# 대시보드 디자인 언어: 웜 페이퍼 라이트 테마

[English](dashboard-notion-theme.md) | 한국어

상태: 결정됨 (2026-10-19). 범위: `packages/guardian-ui`의 토큰과 컴포넌트 스타일, 그리고 이를 통해
`apps/web-desktop`. 참고 자료: [reference/DESIGN-notion.md](reference/DESIGN-notion.md). Notion 마케팅 사이트의
디자인 언어를 분석한 외부 문서로, 아래 토큰의 출처이며 원문 그대로(영어만) 둔다.

## 1. 결정

대시보드를 다크 그래파이트 운영 콘솔에서 참고 자료의 밝은 디자인 언어로 바꾼다. 따뜻한 오프화이트 바탕, 헤어라인으로
구분하는 흰 카드, 제목의 자간을 좁힌 거의 검은색 Inter 글꼴, 구조용 파란색 하나가 핵심이다. 변경은 디자인 토큰
(`src/styles/tokens.css`)과 일부 컴포넌트 규칙에서 한다. `apps/web-desktop`의 화면 코드는 모든 색을 `--sg-*` 토큰에서
읽으므로 고칠 필요가 없다.

## 2. 토큰 대응

| 참고 자료 | 토큰 | 값 |
|---|---|---|
| `canvas-soft` | `--sg-bg` | `#f6f5f4` |
| `surface` / `canvas` | `--sg-surface`, `--sg-surface-raised` | `#ffffff` |
| (사이드바, 표 머리글) | `--sg-surface-sunken` | `#f1f0ee` |
| (코드, 터미널) | `--sg-code-bg` | `#f7f6f3` |
| `hairline` | `--sg-border` | `#e6e6e6` |
| 입력창 테두리 | `--sg-border-strong` | `#dddddd` |
| `ink` (~95%) | `--sg-text` | `rgba(0,0,0,0.95)` |
| `ink-secondary` | `--sg-text-secondary` | `#31302e` |
| `ink-muted` | `--sg-text-muted` | `#615d59` |
| `primary` / `primary-active` | `--sg-accent` / `--sg-accent-hover` | `#0075de` / `#005bab` |
| 모서리 `xs/sm/md/lg/xl/full` | `--sg-r-tag/sm/md/card(panel)/dialog/pill` | 4 / 5 / 8 / 12 / 16 / 9999 px |
| Level 1 / Level 2 그림자 | `--sg-shadow-1` / `--sg-shadow-2` | 참고 자료의 겹 그림자 |
| 제목 자간 | `--sg-track-title/h3/h2/h1` | -0.125 / -0.25 / -0.625 / -1 px |

컴포넌트: 버튼은 굵기 500, 모서리 8px이다(참고 자료의 utility 버튼. 대시보드에는 마케팅 CTA가 없으므로 알약형 버튼은
쓰지 않는다). 보조 버튼은 헤어라인이 있는 흰 버튼이다. 입력창은 흰 바탕에 모서리 4px이다. 상태 칩은 eyebrow 알약
(산세리프 12px/600, 대문자 변환 없음)이다. 사이드바의 활성 항목에는 2px 파란 표시가 붙는다. 대화상자와 명령 팔레트는
Level 2 그림자, 선택된 세그먼트와 마우스를 올린 통계 링크는 Level 1 그림자를 쓴다. 모달 배경은 `--sg-overlay`다.

## 3. 참고 자료와 다른 점

- **의미 기반 상태 색.** 참고 자료에는 오류/성공 색 체계가 없고 스티커 색은 장식용이다. 운영 콘솔에는 상태 표시가
  필요하므로 `ok/warn/crit/info/idle`을 별도의 의미 색으로 두고, 흰 바탕에서 4.5:1을 넘도록 어둡게 했다: ok
  `#16833a`, warn `#b25000`, crit `#d1242f`, info는 accent, idle은 muted. 점, 칩, 미터, diff 줄, 경고에만 쓰고, 위험
  버튼을 빼면 CTA 채우기에는 쓰지 않는다.
- **스티커 팔레트는 쓰지 않는다.** 대시보드에는 일러스트가 없으므로 장식 색은 토큰으로 두지 않는다.
- **어두운 히어로 띠 없음.** 인디고 `secondary` "night" 띠는 마케팅 장치라 대시보드에는 없다.
- **기본 크기 14px.** 참고 자료의 촘촘한 본문은 15px이지만, 표와 노드 행의 밀도를 지키려고 14px(화면 코드의 12/13px
  인라인 크기 포함)를 유지한다. 제목 굵기와 자간은 참고 자료를 따른다.
- **글꼴.** `NotionInter`는 독점 글꼴이므로 `@fontsource/inter`의 Inter(라틴 subset, 400-700)를 번들하고, 한글은
  IBM Plex Sans KR, 코드는 JetBrains Mono를 쓴다.
- **코드와 터미널도 밝게.** `--sg-code-bg`를 따뜻한 종이 색으로 두어 문서 블록처럼 읽히게 한다. 어두운 영역은 남기지
  않는다.
- **다크 테마 없음.** 이전 그래파이트 팔레트는 git 기록(이 변경 이전의 `tokens.css`)에 있다. 설정의 테마 전환은 별도
  변경으로 한다(두 메시지 카탈로그, `prefers-color-scheme`).

## 4. 새 화면의 규칙

- 색은 `--sg-*` 토큰에서만 읽는다. 화면 코드에 hex 값을 쓰지 않는다.
- 파란색(`--sg-accent`)은 주 동작, 링크, 포커스, 선택에만 쓴다. 장식에는 쓰지 않는다.
- 그림자보다 여백과 헤어라인으로 묶는다. Level 2 그림자는 떠 있는 레이어에만 쓴다.
- 제목은 굵기 600-700에 맞는 `--sg-track-*`를 쓰고, 본문은 굵기 400을 유지한다.

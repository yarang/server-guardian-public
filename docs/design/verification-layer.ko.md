# 검증 계층: 다른 도구가 만든 작업을 Guardian이 검증한다 (설계)

[English](verification-layer.md) | 한국어

상태: 2026-11-23 제안. DP1(포지셔닝), DP2(V1 빠른 리뷰 먼저), DP5(운영자 세션 S3-S5 보류)를 운영자가 2026-11-23에
승인했고 나머지 결정은 열려 있다. V0은 2026-11-23 완료, V1a(엔진과 CLI), V1b(허브 API), V1c(대시보드)는 2026-11-23 구현, 2026-11-24 `e9a939a`로 배포(허브에서 `GET /api/reviews` 응답 확인, 배포된 탭은 브라우저에서 열지 않음). 허브에서 첫 실제 리뷰 2026-11-24(claude, 57초, 평가 도구의 실제 결함 두 개). V1e는 2026-11-24 1회 반복으로 실행했고 리뷰가 결정 규칙을 충족했다(재현율 0.94 대 1.00, 시간은 토론의 0.19, 엄격 정밀도 1.00은 에이전트 예비 채점이며 운영자 확인 대기). V3는 DP7을 기다린다. DP3는 2026-11-24 결정: V2 입구 전부를 한 단계로(아래 V2 구현 계획). V2(git ref `--ref`, 상주 패치 `--action`, 세션 MCP 도구 `review_submit`/`review_result`, 대시보드 브랜치 모드와 `apply_patch` 액션의 리뷰 섹션)는 2026-11-24 구현, 같은 날 `8131c23`으로 배포(로컬 허브에서 실제 브라우저로 확인; 실제 ref·액션 리뷰는 아직 돌리지 않음; 워크스테이션 세션 파일은 다시 설치하지 않음).
출발점: Orca(orca.dev, "agent development environment")와의 비교. Orca는 여러 코딩 에이전트를 나란히 돌리는
데스크톱 앱으로, 작업마다 git worktree 하나를 주고, 기본값으로 모든 에이전트를 완전 자율 플래그로 실행하며, 리뷰는
diff를 읽는 사람에게 맡긴다. 비교 결론은 두 도구의 자리가 다르다는 것이다. Orca는 처리량을, Guardian은 신뢰를 높인다.
에이전트 작업량이 많아지면 diff를 리뷰하는 사람이 병목이 되고, 그 자리를 Guardian의 검증(인용, 비평자, jev, verdict
source)이 채울 수 있다. 이 문서는 그 결론을 개발 방향으로 옮긴다.

## 포지셔닝

Guardian은 **검증·통제 계층**이자 **fleet 운영 도구**다. 에이전트 개발 환경(ADE)이 아니다.

- 사람이 에이전트와 함께 코드를 쓰는 장소가 되려 하지 않는다(worktree 사이드바, 내장 터미널, 브라우저 탭, 데스크톱
  셸 없음).
- 작업이 어디서 만들어졌든(아무 도구에서 push한 브랜치, 상주 에이전트의 patch, pull request) 받아서 답한다: 이것이 맞는가,
  무엇이 틀렸는가, 어떤 줄이 근거인가.
- 권한 원칙은 유지한다: 기본 읽기 전용, 타입이 있는 action, 쓰기는 수동 승인, 열린 에이전트는 샌드박스. Orca의 기본값
  (모든 에이전트를 완전 자율 플래그로 실행, "the worktree itself is the sandbox")은 이 계층이 존재하는 이유를 보여주는
  대조 사례이며, 문서에 그렇게 적는다.

## 계정을 쓰는 방식

두 도구 모두 사용자 자신의 계정으로 돈다. 차이는 계정 하나가 무엇을 사 오는가이다.

| | 처리량 도구 (Orca) | Guardian |
|---|---|---|
| 계정 하나를 더하면 | 손이 하나 더 (병렬성) | 시각이 하나 더 (교차 검증) |
| 자리 | 같은 provider를 여러 번 | 자리마다 다른 provider |
| 병목 | 사람의 리뷰, 구독 한도 | 리뷰 한 번의 길이, 슬롯 |

이 계층의 원칙: **자리는 손이 아니라 시각이다.** 리뷰 자리는 가능하면 작업을 만든 provider와 다른 provider를 쓴다
(작성자를 알 때: 상주 에이전트, 토론의 lead). 쿼터는 반복이 아니라 이견에 쓴다.

## 무엇이 바뀌나

### V1 빠른 리뷰 (새 실행 종류)

빈자리: 지금 diff는 solo 답(빠르지만 인용 외 검증 없음, `solo-agent.md`)이나 토론(검증되지만 수십 분) 중 하나를 받는다.
빠른 리뷰는 그 사이에 선다.

- **입력.** 코드 프로젝트와 변경: 프로젝트 workspace의 `base..head` ref, 또는 patch 파일. CLI
  `guardian.py --review --project ID --range BASE..HEAD | --patch FILE [--provider NAME] [--role PERSONA]`.
- **실행.** solo 실행기를 바탕으로 한 리뷰어 세션 하나(기본 persona: 프로젝트의 critic, judge 전용 persona는 거부):
  읽기 전용 저장소 도구, action 프로토콜 없음, 자체 슬롯 `GUARDIAN_MAX_REVIEWS`(기본 2). 프롬프트에 diff가 들어가고,
  주변 코드는 평소의 repo 도구로 읽는다.
- **출력.** severity(`high`/`medium`/`low`), 시나리오, 인용을 갖춘 지적 목록. 라벨은 `REVIEW`
  (`PASSED`/`APPROVED`는 절대 아님, 그것은 공용 마감의 말이다).
- **결정론적 검사.** `evidence.py`의 line-span 인용 검사. 지적은 바뀐 줄이나, 변경의 영향을 받는다고 보여준 줄을
  인용해야 하며, 아니면 `notes`로 옮긴다(audit의 정밀도 규칙). 등록된 verdict source(`verdict-sources.md`)는 `alarm`
  역할로만 돌 수 있다. 예: 프로젝트의 테스트 명령. 신호를 더할 뿐 결정하지 않는다.
- **승격.** Quick ask처럼 "토론으로 실행"이 diff와 지적을 담아 토론을 미리 채워, 공용 마감이 시험하게 한다. 빠른 리뷰는
  action을 만들지 않는다.
- **목표.** 바뀐 줄 약 500줄까지의 diff를 허브에서 5분 안에.

### V2 다른 곳에서 만든 작업의 입구

- **Git ref.** 아무 도구(ADE, 사람, CI)가 프로젝트 remote에 push한 브랜치를 workspace로 fetch해 범위로 리뷰한다. 특정
  ADE 연동 코드는 없다. 인터페이스는 git이다.
- **상주 patch.** 열린 에이전트의 PROPOSED `jev_mcp:apply_patch`(`resident-open-tools.md`, O3)에 Approvals의 "리뷰"
  버튼을 단다. 리뷰는 승인 항목에 붙어, 사람이 결정 전에 지적을 본다.
- **API.** `/api/solo`처럼 전역 인증 체인 뒤에 `POST /api/reviews`, `GET /api/reviews/{id}`, `GET /api/reviews`.
- **세션 MCP.** `session_mcp.py`에 읽기 전용 도구 쌍(`review_submit`, `review_result`)을 두어, 워크스테이션의 운영자
  세션이 push한 브랜치의 리뷰를 허브에 요청할 수 있게 한다. 메일은 여전히 아무 권한도 주지 않고, 도구는 읽기 전용
  실행을 시작할 뿐이다.

### V3 후보 비교

"한 작업에 에이전트 여럿을 경쟁시키고 승자를 고른다"는 처리량 패턴은 고르는 일을 diff N개를 읽는 사람에게 남긴다.
Guardian이 비교를 할 수 있다.

- 입력: 한 작업에 대한 후보 변경 2~4개(브랜치 또는 상주 patch).
- 후보마다 V1 리뷰를 받고, 비평자들이 중립 라벨을 돌려 가며(`multi-perspective.md`의 공정성 규칙, `team-flows.md`의
  `versus` 순서 규칙) 비교하고, 이유와 인용으로 순위를 매긴다.
- 사람이 하나를 승인하고 나머지는 버린다. 자동 병합은 없다.
- 새 엔진 경로가 아니라 flow 카탈로그의 step 종류로 만든다(`flow-templates.md`, `flow-graph.md`). V1 측정 결과가 나온
  뒤에 시작한다.

### V4 task board: 시도 기록과 운영자 대기

Orca 오케스트레이션 용어 중 두 가지가 상주 task board(`resident-agents.md`)에 적은 비용으로 맞는다.

- **시도.** 에이전트가 task를 claim할 때마다 별도 기록(에이전트, epoch, 시작, 끝, 결과)을 남겨, 재시도된 task가 마지막
  상태만이 아니라 이력을 보여준다.
- **운영자 대기.** 에이전트가 task 아래에서 운영자에게 질문하면 `waiting_operator` 상태가 되고, 운영자가 답하면 풀린다.
  모니터 탭은 이것을 맨 위에 보여준다.

기존 안전장치(hop 제한, epoch fencing, 메일은 권한을 주지 않음)는 그대로다.

## 범위 조정

운영자가 하나씩 결정할 보류와 제한 제안(결정 표 참조):

- **운영자 세션.** S2 수용 시험(WSL 호스트)을 마친 뒤 S3-S5를 보류한다. Guardian 고유의 부분(세션이 이름으로 메일
  저장소에 참여, 메일은 권한을 주지 않음, 키는 허브가 보관)은 남기고, 대화형 세션의 원격 제어 UX는 외부 도구에 있으며
  이 계층의 일이 아니다.
- **Provider.** 지금의 묶음이 줄 수 없는 시각이 필요한 자리가 아니면 새 provider는 없다. 추가 모델은 새 wrapper 대신
  가능하면 `opencode`로.
- **ADE 기능 없음.** 대시보드에 worktree 관리, 내장 터미널, 브라우저 창, 데스크톱 셸을 넣지 않는다.

## 결정

| # | 질문 | 권고 | 결정 |
|---|---|---|---|
| DP1 | 포지셔닝(검증·통제 계층 + fleet 운영, ADE 아님)을 채택하나? | 예 | 2026-11-23 승인 |
| DP2 | V1 빠른 리뷰를 solo 실행기 위의 새 실행 종류로 만드나? | 예, 가장 먼저 | 2026-11-23 승인 |
| DP3 | V2 입구: git ref, 상주 patch 버튼, API, 세션 MCP 도구. 전부인가 일부인가? | git ref와 상주 patch 먼저, API는 함께, 세션 MCP는 S2 수용 뒤 | 넷 모두: git ref, 상주 patch 버튼, 세션 MCP 도구를 한 단계에서 함께 구현(API는 V1b부터 있음). 운영자가 S2 수용(WSL 미완)을 기다리지 않기로 함. 2026-11-24 결정 |
| DP4 | 작성자 provider를 알면 기본 리뷰어 provider를 다르게 하나? | 예, 다른 것이 없으면 아무거나 | 미정 |
| DP5 | S2 수용 시험 뒤 운영자 세션 S3-S5를 보류하나? | 예 | 2026-11-23 승인 |
| DP6 | 새 provider를 동결하고 추가 모델은 `opencode`로? | 예 | 미정 |
| DP7 | V3 후보 비교: 지금 계획하나, V1 측정 뒤인가? | V1 측정 뒤 | 미정 |
| DP8 | task board에 시도 기록과 `waiting_operator`를 넣나? | 예, 작게, 다음 상주 변경과 함께 | 미정 |

## 단계

- **V0** 포지셔닝을 `README.md`, `docs/README.md`, 용어집에 적는다(DP1 뒤).
- **V1** 빠른 리뷰: CLI, 실행기, 검사, `reviews/` 기록(ops-watch prune으로 30일 만료), API, Quick ask 옆 대시보드 입구,
  두 카탈로그.
- **V1e** 평가(아래). 결과가 V3를 결정한다.
- **V2** git ref fetch, 상주 patch "리뷰" 버튼, 세션 MCP 도구.
- **V3** 후보 비교 step 종류(V1e 통과 시에만).
- **V4** task board 시도 기록과 `waiting_operator`.

## V1 구현 계획

각 단계는 건드린 부분의 `scripts/ci.sh`로 끝난다. 배포는 V1c 뒤, 깨끗한 트리에서, 운영자 승인을 받아 한다(이
저장소의 게이트).

- **V1a 엔진 (`cli/llm-agent/review_agent.py`).**
  - `ReviewRunner`는 solo 실행기의 준비 과정(프로젝트, persona, provider, 임시 결정 DB, 이벤트)을 재사용하고, 코드
    프로젝트만 받는다. ops·design 프로젝트와 general 모드는 거부한다.
  - 변경 입력: `--range BASE..HEAD`는 workspace에서 `git rev-parse`로 해석하고, `--patch FILE`은 임시 worktree에서 head에
    대해 `git apply --check`로 확인한다. diff는 바뀐 줄 `GUARDIAN_REVIEW_MAX_LINES`(기본 1500)까지이며, 더 크면 나누라는
    안내와 함께 거부한다. 몰래 자르지 않는다.
  - 저장소 도구는 head 커밋을 읽는다(확인할 것: retrieval 준비가 context ref로 준 sha에 고정되는지).
  - 프롬프트: 리뷰 규칙, diff, audit의 findings 블록(severity, claim, scenario, 인용). `REVIEW_PROMPT_VERSION` 1. 블록이
    없거나 깨지면 audit 버전 4처럼 한 번 다시 묻는다.
  - 검사: line-span 인용 검사, 변경 규칙(지적은 `GUARDIAN_CITATION_SLACK` 안의 바뀐 줄이나, 바뀐 심볼 호출부의 보여진 줄을
    인용해야 하며, 아니면 `notes`로 간다).
  - 기록 `reviews/<id>/result.json` + `events.jsonl`, 슬롯 `GUARDIAN_MAX_REVIEWS`(2), ops-watch 유닛의 `--solo-prune`
    옆에 `--review-prune`(30일).
  - 테스트 `test_review_agent.py`(`testenv`로 데이터 디렉터리 격리, 가짜 LLM, 임시 git 저장소): range와 patch 입력, 크기
    거부, 변경 규칙, notes, 재질문, 라벨이 판정이 아님, 위조 블록에서 action 행이 생기지 않음.
- **V1b 허브 (`apps/agent/review_handler.go`).** `solo_handler.go`를 본뜬 `POST /api/reviews`, `GET /api/reviews/{id}`,
  `GET /api/reviews`, 전역 인증 체인 뒤. Go 테스트.
- **V1c 대시보드.** Quick ask 옆 "빠른 리뷰" 탭(`#/debates/review`): 프로젝트, range 또는 붙여 넣은 patch, severity별 지적과
  인용 칩, 접힌 notes, `REVIEW` 칩과 "판정자 없음" 문구, "토론으로 실행". 두 카탈로그. 실제 브라우저에서 데스크톱 폭과
  390 px 확인.
- **V1e 평가**는 배포 뒤, 아래와 같이.

## V2 구현 계획

모든 입구는 기존 `ReviewRunner`로 끝난다. CLI에 변경 입력 두 개를 더하고, 각 화면과 도구는 그것을 부르는 얇은 호출자다.
검증기 하나, 슬롯 하나(`GUARDIAN_MAX_REVIEWS`), 기록 형식 하나. 리뷰는 여전히 조치를 만들거나 바꾸거나 결정하지 않는다.

- **V2a git ref (`review_agent.py`, CLI).** `--ref BRANCH [--base REF] [--expect-sha SHA]`.
  - 프로젝트에 설정된 `origin`에서만 가져온다. URL은 요청에서 받지 않는다. 브랜치는 `validate_ref`와
    `git check-ref-format --branch`를 통과해야 하고 `-`로 시작할 수 없다.
  - 명시적 refspec으로 한 번만 fetch해 전용 이름공간에 둔다: `+refs/heads/<branch>:refs/guardian/review/<id>/head`,
    프로젝트 브랜치는 `refs/guardian/review/<id>/base-tip`. `--no-tags`, `--no-write-fetch-head`(허브 git에 없으면
    대체), `GIT_TERMINAL_PROMPT=0`, 시간 제한. `refs/remotes/origin/*`, 로컬 브랜치, HEAD, index, 작업 트리는 건드리지
    않으므로 publish 잠금이 필요 없다.
  - 기준: `--base`가 있으면 그것, 없으면 head와 `base-tip`의 merge-base. `--expect-sha`는 가져온 head가 다르면 거부한다
    (push 뒤 브랜치가 움직이는 경쟁을 막는다). 두 sha와 브랜치는 `result.json`의 `source`에 남는다.
  - `refs/guardian/review/<id>/*`는 기록과 수명이 같다. `--review-prune`이 기록과 함께 지우므로 "토론으로 실행"이 head
    커밋을 계속 찾을 수 있다.
- **V2b 상주 patch (`review_agent.py`, CLI).** `--action ID`.
  - 결정 DB는 읽기 전용(`mode=ro`)으로 연다. 코드 프로젝트와 `base_sha`가 있는 PROPOSED `jev_mcp:apply_patch`만 받는다.
    리뷰 대상은 작업의 원본 patch가 아니라 조치의 `edits`(승인하면 적용될 내용)다. head 커밋은 `base_sha`의 임시
    index 위에서 edits로 만든다(hash-object, update-index, write-tree, commit-tree). 작업 공간은 건드리지 않는다.
  - 기록에는 `source: {kind: "action", action_id}`가 남는다. 결정 DB 스키마와 조치 상태는 바뀌지 않는다.
- **V2c 허브 (`review_handler.go`).** `reviewRequest`에 `ref`, `base`, `expect_sha`, `action`, 선택 항목
  `started_by`(형태 `session:<workstation>` 또는 빈 값)를 더한다. range, patch, ref, action 중 정확히 하나. Go는 형태만,
  의미는 CLI가 검사한다. `GET /api/reviews?action=ID`는 한 조치의 리뷰를 나열한다.
- **V2d 세션 MCP (`session_mcp.py` -> `session_local.py` -> `session_daemon.py` -> `session_uplink.py` ->
  `session_link.py`).** 도구 `review_submit`(project, ref, base, expect_sha, provider. 링크로 patch는 보내지 않음)과
  `review_result`(review id, `wait_sec`는 `resident_query_get`처럼 상한). 모든 단계가 두 op를 나열한다(`TOOL_OPS`,
  `_check_*`가 있는 `MCP_OPS`, daemon 처리기, `LinkClient.OPS`, link 처리기). 워크스테이션은 신뢰하지 않으므로 허브
  쪽이 모두 다시 검증한다. link 진입점은 리뷰를 직접 띄우지 않고 허브 자신의 `POST /api/reviews`(`127.0.0.1:9870`)를
  허브의 `AGENT_TOKEN`(허브에서 읽고 워크스테이션으로 보내지 않음)으로 부른다. `started_by: session:<workstation>`은
  클라이언트가 아니라 link 줄에서 정한다. `review_result`는 그 워크스테이션이 시작한 리뷰에만 답한다. 메일은 여전히
  아무 권한도 주지 않는다.
- **V2e 대시보드.** 빠른 리뷰에 "브랜치" 모드(브랜치, 선택 기준)를 더한다. 승인 화면의 PROPOSED `apply_patch` 카드에
  "리뷰" 버튼을 두고 연결된 리뷰(상태 칩, 발견 수, 리뷰 링크)를 보여 준다. 승인과 거절 흐름은 그대로다. 두 카탈로그
  모두. 로컬 허브에서 실제 브라우저로 데스크톱 폭과 390 px를 확인한다.
- **테스트.** 데이터 디렉터리는 `testenv`로 격리하고 임시 bare 저장소를 `origin`으로 쓴다. ref: 전용 이름공간에만
  fetch, 잘못된 이름과 옵션처럼 보이는 이름 거부, `--expect-sha` 불일치 거부, prune이 ref 삭제. action: 수정 파일 하나와
  추가 파일 하나가 edits를 적용한 것과 같은 트리가 되는지, PROPOSED가 아니거나 apply_patch가 아니면 거부, 실행 뒤 조치
  행이 그대로인지. 세션: 각 단계가 두 op를 받고 추가 필드를 거부, `started_by`는 link에서, 다른 워크스테이션의 리뷰
  id는 거부.
- **배포.** 허브 먼저(새 워크스테이션 daemon이 옛 허브와 말하면 모르는 op로 실패한다). 워크스테이션 쪽(`session_*.py`)은
  운영자의 OK가 있을 때만 다시 설치하고, 새 도구의 권한 허용 규칙은 묻지 않고 더하지 않는다.

## 평가 (V1e)

기존 결함 심기 자료(`proposer_eval.py` 과제, A4 audit 씨앗)를 diff로 바꿔 쓴다. 같은 diff에 대해 빠른 리뷰와 `default`
팀 토론을 비교한다. 먼저 1회 반복, Codex 쿼터는 운영자 승인 뒤. 결정 규칙: 재현율이 토론의 0.8 이상이고 시간이 4분의 1
이하이며 엄격 정밀도가 토론보다 낮지 않으면, V1을 기본 1차 검사로 둔다.

## 비목표

- 대량으로 코드를 만들기 위해 에이전트를 돌리는 일(만드는 도구의 일이다).
- 특정 ADE, 그 클라우드 중계, 그 API에 대한 의존.
- 공용 마감, jev 게이트, 승인 규칙의 변경. 빠른 리뷰는 solo 실행처럼 자문일 뿐이다.
- 빠른 리뷰나 비교가 권한 것을 자동 병합하거나 자동 적용하는 일.

## 위험

- **라벨을 판정으로 읽음.** high 지적이 없는 `REVIEW`를 승인으로 여길 수 있다. `SOLO`처럼 라벨, "Judge 없음" 문구,
  토론으로 실행 버튼을 모든 화면에 둔다.
- **diff만 보는 맹점.** diff만 읽는 리뷰어는 다른 곳의 영향을 놓친다. 프롬프트가 바뀐 심볼의 호출부를 repo 도구로
  읽도록 요구하고, V1e가 이를 측정한다.
- **쿼터.** push되는 모든 브랜치를 리뷰하면 공유 쿼터를 먹는다. 리뷰는 요청이 있을 때만(버튼, CLI, API, 세션 도구)
  시작한다. 이 문서에 push 시 자동 리뷰는 없다.

## 참고

- `docs/design/solo-agent.md`(실행기, 라벨, 검사), `docs/design/verdict-sources.md`(alarm 역할),
  `docs/design/resident-open-tools.md`(O3 patch), `docs/design/resident-agents.md`(task board),
  `docs/design/operator-sessions.md`, `docs/design/team-flows.md`, `docs/design/flow-graph.md`,
  `docs/design/multi-perspective.md`.
- Orca 문서, 2026-11-23 열람: 개요, worktree, 에이전트와 세션, 지원 에이전트, 실행 방식, SSH, 오케스트레이션, 봇 보안,
  GitHub 리뷰, 텔레메트리(orca.dev/docs).

## 구현 메모

- V0 (2026-11-23): `README.md`/`README.ko.md`에 포지셔닝 문단, 용어집에 "빠른 리뷰"와 "검증 계층" 행, 운영자 세션에
  S3-S5 보류(DP5) 표시, 문서 목록 갱신.
- V1a (2026-11-23): `cli/llm-agent/review_agent.py`, `guardian.py --review --project ID --range BASE..HEAD |
  --patch FILE|- [--patch-base REF] [--role] [--provider] [--language] [--review-id] [--result-file] [--json]`,
  `--review-prune`(ops-watch 유닛의 ExecStartPre), `test_review_agent.py` 테스트 21개. 계획과 다른 점:
  - head 커밋의 바뀐 파일을 diff 옆에 context pack으로 넣는다(코드 pack의 자체 한도 적용). 이것이 없으면 diff만으로는 "받은
    코드"로 치지 않아, 지적이 바뀐 파일을 인용할 때마다 엔진의 인용 검사가 다시 물었다. diff hunk도 line-span 규칙의 보여진 줄로 센다.
  - patch는 임시 index에서 base에 적용하고, 어떤 ref도 가리키지 않는 커밋 객체로 만든다(workspace 객체 저장소에 남고 `git gc`가
    지운다). workspace의 index, 작업 트리, ref는 건드리지 않고, 그 뒤 리뷰는 range와 똑같이 돈다.
  - 변경 규칙의 "바뀐 심볼 호출부의 보여진 줄"은 결정론적으로 근사한다: 바뀐 줄 밖의 지적도 검증된 인용문이 변경이 선언하거나
    건드린 심볼(hunk 머리의 문맥, 바뀐 줄의 선언)을 담으면 지적으로 남는다. 나머지는 이유와 함께 `notes`로 간다.
  - 기본 리뷰어는 기본 팀의 첫 비평가(`critic_redteam`)다. 캐릭터는 critic 또는 solo 자리가 있어야 한다. severity 하한과 audit의
    재확인 세션은 없다: 세션 하나, 블록이 없거나 깨지면 한 번 다시 묻기. 블록이 끝내 읽히지 않으면 `state: "bad_format"`과 마스킹한
    본문으로 저장한다.
  - 슬롯은 solo 실행기의 flock 도우미를 자체 디렉터리(`reviews/.slot-N`)에서 재사용한다.
  - 실제 provider로는 아직 돌려 보지 않았다(가짜 실행기만). V1b/V1c와 V1e가 남았다.
- V1b (2026-11-23): `apps/agent/review_handler.go`: `POST /api/reviews`(`{project, range | patch + patch_base?, role?,
  provider?, language?}` -> 202 `{id, status, label, note}`. 시작 유예 시간 안에 CLI가 2/4로 끝나면 400/429, 기록 삭제),
  `GET /api/reviews/{id}`, `GET /api/reviews[?project=ID]`(`state`, `range`/`patch`, 파일 수, severity별 `counts`와 notes 수).
  받은 patch는 실행 자체의 기록 디렉터리 안에 `change.diff`(0600)로 쓰고 `--patch=<path>`로 넘긴다. 본문 한도는 CLI의 patch
  한도와 같은 2 MiB + 16 KiB. solo 자식 실행기를 `launchRecordRun`으로 바꿔 둘이 함께 쓴다. 슬롯 `GUARDIAN_MAX_REVIEWS`(기본 2).
  Go 테스트 7개.
- V1c (2026-11-23): 작업 흐름 탭 "빠른 리뷰"(`#/debates/review[/<id>]`, `pages/review/ReviewTab.tsx`, `lib/review.ts`,
  `ko`·`en` 카탈로그 `review`). code 프로젝트의 첫 탭이고 ops·design 프로젝트에는 없다. 범위 또는 붙여 넣은 patch(+ 기준),
  리뷰어와 프로바이더 선택, 프로젝트의 최근 리뷰. 결과 화면은 `REVIEW` 칩, "판정이 아님" 문구, severity별로 묶은 지적(인용과
  시나리오), 접힌 notes, `bad_format`일 때 답 원문, "토론으로 넘기기"(변경과 지적으로 토론을 미리 채움). 현황의 "최근 토론"
  카드는 "최근 검증"이 되어 토론 위에 프로젝트의 최근 빠른 리뷰 세 건을 보여 준다. severity 개수는 끝난 리뷰(`state: "done"`)에만
  표시한다. 이 트리로 만든 로컬 허브에서 `GUARDIAN_LLM_PROVIDERS=none`(모델 호출 없이 리뷰는 대체 응답으로 끝남)으로 실제
  브라우저(Playwright) 확인: 시작, 목록, 상세, 현황, 설정 -> 흐름, 시작 폼을 1280 px와 390 px에서 보았고 가로 넘침은 없다. 이 확인에서
  하나를 찾았다: 390 px에서 긴 선택지 문구가 `SelectField`를 카드 밖으로 넓혔다. 공용 컴포넌트가 이제 select를 줄일 수 있게 해,
  모든 폼에 적용된다. `#/debates` 기본 화면은 그대로 토론 목록이다. 빠른 리뷰는 첫 탭이지 시작 화면은 아니다.
- V1e 평가 도구(2026-11-24, 같은 날 운영자가 실행 승인): `cli/llm-agent/review_eval.py`와 `test_review_eval.py`. 계획과 다른
  점: 재료는 `proposer_eval.py` 과제가 아니라 audit A4b에 심었던 코드 결함 16개(`docs/analysis/2026-10-19-audit-a4b/ground-truth.json`,
  기준 `a3f5783`)다. `proposer_eval.py` 과제는 문서 결함이라 코드 리뷰에 맞지 않는다. `--prepare`는 전체 히스토리가 있는
  체크아웃을 평가 디렉터리에 복제하고, 결함 네 개(종류마다 하나: AUTH, IGNOREDERR, BOUNDS, DEADBRANCH, 파일 네 개)씩 묶은 케이스
  네 개를 커밋하고, 평가 데이터 디렉터리에 평가 프로젝트 파일을, 그리고 케이스마다 빠른 리뷰 한 번과 `default` 팀 토론 한 번(1라운드,
  과제에 diff, head 커밋의 바뀐 파일을 참조 코드로)을 돌리는 `run.sh`를 쓴다. 실행 스크립트는 `~/.guardian/data` 아래 데이터
  디렉터리를 거부하고 `GUARDIAN_AUTO_APPLY=off`를 건다. `--score`는 심은 줄에서 5줄 안의 리뷰 지적을 적중으로 세고(notes에 들어간
  적중은 따로 센다), 토론은 결정과 비평에서 파일 이름과 가까운 줄 번호 또는 함수 이름으로 예비 채점하며, `score.json`과
  `grading.json`을 쓴다. 결정 규칙의 정밀도 조건은 채점표를 채울 때까지 보류다.
- 첫 실제 리뷰(2026-11-24, 허브, 범위 `adabfd2..c2b5bd0`): claude로 57.1초에 끝났고 medium 지적 두 개는 모두 V1e 평가 도구의
  실제 결함이었다. 실행 스크립트의 재실행 판단이 파일 존재만 보았는데 셸 리다이렉트는 리뷰가 실패해도 파일을 만든다. 그리고
  요약은 끝나지 않은 리뷰를 세면서 결정은 그것을 무시했다. V1e 뒤 둘 다 고쳤다: 출력은 임시 파일에 쓰고 종료 코드 0일 때만
  제자리로 옮기며, 리뷰나 토론이 끝나지 않았으면 결정은 `incomplete`다.
- V1e 결과(2026-11-24, 1회 반복, 샌드박스 `~/guardian-eval/data-review`, 자동 적용 끔; `score.json`과 `grading.json`은
  `docs/analysis/2026-11-24-review-eval/`): 리뷰는 심은 결함 16개 중 15개(재현율 0.94), 평균 40.1초, 지적 15개 모두 심은 결함에
  해당, notes에 들어간 것 없음, 리뷰는 모두 끝남. 놓친 것은 IGNOREDERR-3(케이스 3, `go_facts.py`: goindex 오류 때 남은 facts
  파일이 built로 캐시됨). 토론은 16개 중 16개(느슨한 자동 예비 채점으로 재현율 1.00), 평균 206.1초, 대체 실행 없음. 케이스 2는
  DISPUTED, 나머지는 APPROVED. 재현율 비 0.94(규칙 0.8)와 시간 비 0.19(규칙 0.25) 통과. 엄격 정밀도: 에이전트가 리뷰 주장 15개를
  정답과 대조해 읽었고 모두 심은 결함을 짚었으므로 리뷰 정밀도는 1.00이고 정밀도 조건이 성립한다. 이는 운영자가 확인할 예비
  채점이지 사람 채점이 아니다. 한계: 1회 반복; diff가 작아(케이스당 결함 4개, 토론 3-4분) 리뷰가 겨냥한 약 500줄 크기에서 시간
  비를 재지 못했다; 토론 재현율은 느슨한 예비 채점에서 왔다; 심은 결함이 모두 바뀐 줄에 있어 diff만 읽는 맹점(위 위험)은 재지
  않았다. 토론이 남긴 PROPOSED 액션은 샌드박스 결정 DB에 승인 없이 남아 있다. 결론: V1은 기본 첫 검사로 남고, V3를 지금
  계획할지는 DP7이다.
- V2(2026-11-24, 하나의 서면 계약을 바탕으로 z.ai 에이전트가 V2a-V2e를 병렬로 만들고 claude가 검토·보완; 2026-11-24에
  `8131c23`로 배포). 계획과 다른 점과 메모:
  - 허브는 시작할 때 기록 옆 `request.json`(0600)에 소스를 남기므로, 실행 중인 리뷰도 무엇을 보는지 바로 보인다.
    `result.json.source`는 `{kind: "ref", ref, base, head, base_ref}` 또는 `{kind: "action", action_id, base, head}`.
    목록 행에 `source_kind`, `ref`, `action_id`, `started_by`가 더해졌다.
  - ref 가져오기는 `--refmap=`도 넘겨, 설정된 `remote.origin.fetch`가 명시 refspec 옆에 추적 ref를 더하지 못하게
    한다. `--review-prune`은 기록과 함께 `refs/guardian/review/<id>/*`를 지우고, 모든 코드 프로젝트 작업 공간에서
    기록이 없어진 리뷰 ref를 쓸어낸다.
  - MCP 도구 `review_result` 뒤의 링크 op 이름은 `review_get`이다. 두 링크 처리기 모두 먼저 호출한 세션이 그
    스트림의 워크스테이션과 lease 아래 묶여 살아 있는지 확인한다(`mail_sessions.py`의 새 `session_live`, 저장소
    기반 처리기와 같은 방식). 다른 워크스테이션이나 허브가 시작한 리뷰는 모르는 id와 똑같이 `not_found`로 답한다.
    허브 응답 중 `id`, `status`, `label`, `note`만 돌아간다.
  - 대시보드의 리뷰 섹션은 승인 화면이 여는 액션 대화상자(`ActionDialog.tsx`, 새 `pages/review/ActionReviews.tsx`)에
    있다. 모든 `jev_mcp:apply_patch`에 보이고("리뷰하기" 버튼은 PROPOSED일 때만), 리뷰가 실행 중이면 5초마다 조회하며
    페이지가 숨겨지면 멈춘다. 승인·거절을 잠그지 않는다. 브랜치 검사는 `pages/review/source.ts`에 있고 CLI의
    `validate_ref`를 따른다.
  - 이 트리로 만든 로컬 임시 허브(`GUARDIAN_LLM_PROVIDERS=none`)에서 실제 브라우저로 데스크톱 폭과 390 px를 확인했다:
    브랜치 모드, 목록·상세 이름, 액션 섹션; V2로 인한 넘침 없음. V2와 무관한 것: 375 px에서 `.sg-seg`/`sg-tabbar`가
    10 px 넘친다; 한 상세 화면의 첫 로드가 "signal timed out"이었으나 API는 30 ms에 답했다(다시 시도하면 됨, 임시
    허브에서만 봄). Go 허브가 시작한 리뷰 id는 UTC, CLI에서 시작한 것은 현지 시간을 쓴다.
  - 실제 `--ref`·`--action` 리뷰는 아직 돌리지 않았다(쿼터; 운영자 OK 먼저). Mac의 세션 파일(`session_*.py`)은
    다시 설치하지 않았으므로 두 MCP 도구는 아직 워크스테이션에서 쓸 수 없다.

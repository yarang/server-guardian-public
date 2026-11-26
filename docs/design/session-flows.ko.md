# 운영자 세션에서 만드는 흐름 (설계)

[English](session-flows.md) | 한국어

상태: 2026-11-25 제안. 같은 날 운영자가 결정: SD1 초안 후 승격, SD2 실행 허용, SD3 kind와 자리만.
SD4-SD6은 2026-11-26에 권장안대로 결정했습니다(7절). SF1은 2026-11-26에 구현했고 배포하지 않았습니다. SF2-SF4는
구현하지 않았습니다.
출발점: "워크플로우를 만들 수 있도록 mcp를 개편할 수 있을까?" 워크스테이션의 운영자 세션(Claude Code, agy)은
이미 세션 링크(`docs/design/operator-sessions.ko.md`)로 허브에 닿고 빠른 리뷰를 시작할 수 있습니다
(`review_submit`, `docs/design/verification-layer.ko.md`의 V2d). 그러나 흐름(`docs/design/flow-graph.ko.md`)을
나열하거나 초안을 만들거나 시작할 수는 없습니다. 지금은 대시보드의 흐름 편집기와 시작 폼이 유일한 길입니다.

## 1. 목표와 비목표

목표: 대시보드 없이 운영자 세션에서

- 어떤 흐름이 있고 흐름에 무엇을 넣을 수 있는지(일, kind, 캐릭터, 한도) 봅니다.
- 짧은 형식(kind와 자리에 앉힐 캐릭터)으로 흐름 초안을 쓰고, 허브가 검사하고, 고치고, 다시 검사합니다.
- 초안을 허브에 두고, 사람이 대시보드에서 운영자 흐름으로 승격합니다.
- 기존 흐름(저장된 흐름, 템플릿, 팀)을 자리 교체와 매개변수로 실행하고 결과를 읽습니다.

비목표:

- 세션에서 완전한 흐름 그래프를 만들지 않습니다(SD3). 자유 자리, 묶음, 연결, 입력 방식, 마감 설정은 없습니다.
  그런 것은 캔버스 편집기에서만 그립니다.
- 운영자 흐름 저장소에 직접 쓰지 않고, 기본 흐름이나 운영자 흐름을 덮어쓰지 않습니다(SD1).
- 초안은 실행하지 않습니다. 사람이 승격한 뒤에만 실행됩니다.
- 새 권한이 없습니다. 세션이 시작한 실행도 다른 실행처럼 액션을 제안할 뿐이고, 사람 없이는 아무것도 하지
  않습니다(SD4).
- 세션에서 스레드를 이어 가지 않습니다. 흐름 시작은 `POST /api/debates`가 이미 요구하듯 새 스레드를 엽니다.
- 상주 에이전트는 대상이 아닙니다. 메일은 아무 권한도 주지 않고, 상주 에이전트는 흐름 도구를 받지 않습니다.

## 2. 제약

1. 워크스테이션은 신뢰하지 않습니다(`operator-sessions.ko.md`, 아키텍처 규칙 2). 허브가 모든 프레임을 다시
   검사하고, 모르는 필드는 거부하며, `started_by`는 클라이언트 값이 아니라 링크 자신의 워크스테이션입니다.
2. 세션 링크는 아무것도 띄우지 않습니다. 리뷰 op처럼 링크 입구가 허브의 `AGENT_TOKEN`으로 loopback의 허브
   HTTP API를 부릅니다. 토큰은 허브에서 읽고 스트림을 건너지 않습니다.
3. 새 op마다 세션 체인의 모든 층이 바뀝니다: `session_mcp.py`의 도구, 데몬의 `MCP_OPS`(`session_local.py`),
   링크 클라이언트의 `OPS`(`session_uplink.py`), `session_link.py`의 검사기와 처리기. 이미 떠 있는 MCP 프로세스는
   CLI 세션을 다시 시작할 때까지 옛 코드를 씁니다.
4. 흐름은 도구를 넓히지 않습니다. 도구는 흐름이 아니라 프로젝트의 `policy`와 캐릭터의 자리 규칙에서 옵니다.
   초안에는 `allowed_tools`, `host`, `code_tools`가 없습니다.
5. 허브 데이터는 운영 상태입니다. 초안은 `flows/`가 아니라 자기 디렉터리에 둡니다.
6. 실행은 공유 LLM 할당량을 쓰고 토론 슬롯을 차지합니다. 기존 슬롯 한도가 그대로 적용됩니다(꽉 차면 HTTP 429).

## 3. 초안 형식 (SD3)

초안은 provider, host, 도구가 빠진 팀 모양의 라인업입니다.

```json
{
  "id": "api-review",
  "name": "API review",
  "kind": "debate",
  "lead": "backend_expert",
  "critics": ["security_critic", "api_critic"],
  "proposers": [],
  "close": "default_judge",
  "max_rounds": 2,
  "category": "development",
  "projects": ["code"]
}
```

| kind | 초안이 채우는 자리 |
|---|---|
| `debate` | `lead`, `critics`(1-4), `proposers`(0-2), `close` |
| `panel` | `lead`, `proposers`(1-2), `close`; critic 없음 |
| `verify` | `lead`, `critics`(1-4, 체크리스트 방식), `close` |
| `pipeline` | `stages`(2-4, 순서대로 캐릭터), `lead`, `critics`(0-4), `close` |
| `versus` | `sides`(두 개의 `{character, stance}`), `exchanges`(1-2), `close` |

자리마다 캐릭터 id 하나를 둡니다. 허브는 초안을 팀 dict(`expert`, `critic`, `judge`, `proposers`,
`flow: {kind, stages | sides, exchanges}`)로 바꾸고, 각 멤버의 provider는 캐릭터에서 가져옵니다(SD5). 그다음
`team_to_flow`가 이미 쓰는 길인 `parse_lineup`과 `lineup_flow`를 거치고, `validate_flow`와 `compile_flow`로
결과를 검사합니다. 저장된 초안은 짧은 형식과 변환된 흐름을 함께 가지므로, 대시보드는 승격하면 저장될 그래프를
보여 줄 수 있습니다.

`projects`는 다른 흐름과 마찬가지로 프로젝트 id가 아니라 프로젝트 종류(`code`, `ops`, `design`)를 적습니다.

한도: 워크스테이션당 초안 20개(SD6). id는 `TEAM_ID_PATTERN`을 따르고 워크스테이션 안에서만 유일합니다.
`name`은 최대 `MAX_NAME_CHARS`자입니다.

## 4. 세션 도구

| 도구 | 허브 경로 | 쓰기 |
|---|---|---|
| `flow_catalog` | kind, kind마다 받는 자리, 캐릭터와 홈 자리, 한도, 분류 | 없음 |
| `flow_list` | `GET /api/flows`를 줄여서: id, name, source, kind, runnable, projects, params | 없음 |
| `flow_get` | 흐름 하나의 자리와 매개변수, 거꾸로 변환되면 짧은 형식 | 없음 |
| `flow_check` | 초안을 변환하고 검사; 오류, 경고, 라운드당 호출 수, 그래프를 답함 | 없음 |
| `flow_draft_save` | 이 워크스테이션 아래에 초안 저장(먼저 검사, 틀리면 거부) | 초안 저장소 |
| `flow_draft_list` / `flow_draft_delete` | 이 워크스테이션의 초안만 | 초안 저장소 |
| `flow_run` | `project`, `flow`, `seats`, `params`, `task?`, `started_by`로 `POST /api/debates` | 실행 시작 |
| `flow_run_get` | `GET /api/debates/{id}`, `started_by`가 이 워크스테이션일 때만 답함 | 없음 |

`flow_check`는 모델이 일하는 고리입니다: 초안, 검사, 수정, 재검사. 그동안 허브 상태는 바뀌지 않습니다.
`flow_run_get`은 다른 워크스테이션(또는 대시보드)이 시작한 실행을 `review_get`처럼 모르는 id와 똑같이 답합니다.
두 get 도구는 `wait_sec` 0-15를 받습니다.

## 5. 승격

대시보드의 흐름 종류 탭에 "세션 초안" 목록이 생깁니다: 워크스테이션, 초안 id, kind, 수정 시각, 검사 상태,
그리고 읽기 보기의 변환된 그래프. "승격"은 운영자가 확인한 id(기본값: 초안 id)로 `FlowStore.save`를 거쳐 변환된
흐름을 저장합니다. 이미 있는 id는 운영자가 덮어쓰기를 명시적으로 체크하지 않으면 거부하고, 기본 흐름 id는 늘
거부합니다. 승격된 흐름은 `promoted_from: {workstation, draft, at}`을 기록하고 초안은 지워집니다. "삭제"는 초안을
지웁니다. 둘 다 CLI 플래그 위의 Go 엔드포인트(`GET /api/flow-drafts`,
`POST /api/flow-drafts/{ws}/{id}/promote`, `DELETE /api/flow-drafts/{ws}/{id}`)이고, 흐름이 쓰는
`configGet`/`configSave` 방식과 같습니다.

## 6. 실행

순서는 [session-flows-sequence.mmd](session-flows-sequence.mmd)에 있습니다.

- `flow_run`은 대시보드의 흐름 시작과 같은 필드를 받습니다. Go의 `validateFlow`와 Python의 `resolve_run`이
  검사하고, 자리 교체는 그 자리에 앉을 수 없는 캐릭터를 거부하는 `swap_characters`를 거칩니다.
- `POST /api/debates`가 `started_by`를 배웁니다(`session:<workstation>`만, 리뷰 처리기처럼 검사). 작업 요약에
  저장하고 토론 목록에 보입니다.
- 세션이 시작한 실행은 스스로 아무것도 하지 않습니다(SD4): jev 게이트 뒤의 `auto:engine` 적용, ops
  `auto_run`, batch 자동 해제가 모두 꺼집니다. 그 액션은 승인 탭에서 사람을 기다립니다.

## 7. 결정

| # | 질문 | 결정 |
|---|---|---|
| SD1 | 세션의 흐름을 어디에 저장하나 | 2026-11-25 결정: 워크스테이션별 초안 저장소; 사람이 대시보드에서 승격. 세션은 기본 흐름이나 운영자 흐름을 덮어쓰지 않음. |
| SD2 | 세션이 실행을 시작해도 되나 | 2026-11-25 결정: 예, 기존 흐름만, `started_by: session:<ws>` 기록, 기존 슬롯 한도 안에서. |
| SD3 | 세션이 보내는 흐름 형식 | 2026-11-25 결정: kind와 자리에 앉힐 캐릭터(3절); 그래프는 허브가 만듦. |
| SD4 | 세션이 시작한 실행의 자동 경로 | 2026-11-26 결정: 모두 끔(`auto:engine`, ops `auto_run`, batch 자동); 모든 액션을 사람이 승인. |
| SD5 | 초안 자리의 provider | 2026-11-26 결정: 캐릭터(format 2 `providers`)에서, 없으면 허브 기본 순서; 초안은 provider를 적지 않음. |
| SD6 | 초안 한도 | 2026-11-26 결정: 워크스테이션당 20개, 만료 없음; 워크스테이션을 지우면 초안도 함께 지움. |

## 8. 단계

| 단계 | 내용 | 확인 |
|---|---|---|
| SF1 | `flow_drafts.py`: 짧은 형식 → 팀 dict → 흐름, `flow-drafts/<ws>/` 아래 초안 저장소, CLI `--flow-draft-check`, `--flow-draft-save --workstation`, `--flow-drafts`, `--flow-draft-promote`, `--flow-draft-delete` | kind마다 단위 테스트, 거부, 승격 충돌; `testenv.isolate_data_dir()` |
| SF2 | 초안용 Go 엔드포인트; 네 층 모두에 세션 op `flow_catalog`, `flow_list`, `flow_get`, `flow_check`, `flow_draft_*` | Go 테스트; 링크 검사기 테스트; `test_session_e2e.py` 종단 간 |
| SF3 | `POST /api/debates`와 작업 요약의 `started_by`; SD4 스위치; `flow_run`, `flow_run_get` | Go 테스트; 가짜 실행 e2e; 운영자 OK 후 허브 샌드박스에서 실제 실행 한 번 |
| SF4 | 대시보드: 승격과 삭제가 있는 세션 초안 목록; 토론 목록의 `started_by`; `ko`와 `en` 문자열 | 웹 테스트; 로컬 허브에서 실제 브라우저로 데스크톱 폭과 390 px |

## 9. 구현 메모

- SF1 (2026-11-26): `cli/llm-agent/flow_drafts.py`(짧은 형식, 흐름으로의 변환, `flow-drafts/<ws>/` 아래 초안
  저장소), CLI 플래그 `--flow-draft-check`, `--flow-draft-save --workstation`, `--flow-drafts`,
  `--flow-draft-promote [--promote-as NEWID] [--overwrite]`, `--flow-draft-delete`(세션 그룹의 기존
  `--workstation` 플래그를 재사용), SF2 엔드포인트용 `config_ops` 래퍼, `flow_graph.FLOW_KEYS`에 추가된
  `promoted_from`과 `validate_flow`의 형태 검사, 워크스테이션 삭제 시 초안 함께 삭제(SD6,
  `session_registry.remove_workstation`); `test_flow_drafts.py`에 테스트 29개. 계획에서 달라진 점:
  - 없는 초안은 승격과 삭제에서 NOT_FOUND(종료 코드 3, HTTP 404)로 답합니다. 다른 삭제가 쓰는 코드이고, 검증
    거부는 계속 종료 코드 2입니다.
  - `promote`는 기본 흐름 디렉터리의 파일로 기본 흐름 id를 거부합니다. overwrite를 주어도, 사용자 흐름이 그 id를
    가리고 있어도 거부하므로 초안은 기본 id를 가질 수 없습니다(claude 리뷰; 첫 구현은 저장된 출처를 보고 가려진
    id를 통과시켰습니다).
  - SD5: 형식 2 캐릭터 파일에는 공급자가 없으므로 변환된 초안의 모든 자리는 공급자 목록이 비어 허브의 기본 순서로
    실행합니다. 초안은 공급자를 아예 적을 수 없고, 자리에 사전을 넣으면 거부합니다.
  - 짧은 형식의 자리 수 오류는 필수 목록이 없으면 0개로 셉니다("critics: 1-4 characters, got 0"). 메시지가
    없음이 아니라 범위를 말합니다.

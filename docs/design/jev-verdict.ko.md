# jev 판정: 마감은 jev로 판정하고 LLM은 결정문을 쓴다 (설계)

[English](jev-verdict.md) | 한국어

> [verdict-sources.md](verdict-sources.ko.md)가 일반화했다(2026-11-01): jev 판정은 첫 번째 판정기이고, 결정과 단계는 그 층
> 안에서 그대로 유효하다.

상태: 2026-11-01 제안. 결정 V1-V7은 같은 날 운영자와 정했다. 아무것도 만들지 않았다. J1(그림자 모드)이 먼저이고, 판정은
J2의 측정을 거친 뒤에만 jev로 바꿀 수 있다.

## 질문

판정자 자리(흐름의 마감, [flow-graph.md](flow-graph.ko.md))는 LLM CLI(`claude`, `codex`, ...)만 쓴다. 허브가 이미 비용을
내고 jev 게이트에서 부르는 typesafe.ai의 jev API를 왜 쓸 수 없는가?

## 확인한 것

- **막는 결정은 없고, 구조가 막는다.** 자리 실행기(`llm_runner.py`)는 CLI 세션을 돌리고, 판정자에게는 세션이 필요하다.
  코드를 읽고(repo 블록이나 MCP), 여러 턴에 걸쳐 답하고, 잘못된 조치를 다시 쓴다. jev에는 그런 인터페이스가 없다.
- **jev는 판단하고, 쓰지 않는다.** 도구(`jev_check`, `jev_classify`, `jev_score`, `jev_ask`, `jev_triage`)는 호출하는 쪽이
  준 질문과 선택지에 대한 확률을 돌려주고, 자유 문장을 만드는 도구는 없다. 허브 키로 쓸 수 있는 모델: `jev-latest`
  (2026-10-22), `jev-preview`. 판정자 자리는 두 가지 일을 한다: **판정**(승인 여부)은 jev의 판단 그대로이고, **작성**(결정문,
  근거, 제약, 조치)은 jev가 할 수 없다.
- **jev는 이미 마감에 게이트로 들어가 있다.** LLM 판정자가 승인하면 `jev_gate.py`가 쓰인 결정문을 제출하고
  (`jev_submit_decision`), `PASSED`면 조치가 풀리고 `DISPUTED`면 묶인다. 허브에서 2026-10-03~11-01에 게이트까지 간 승인은
  62건: `PASSED` 50, **`DISPUTED` 12 (19 %)**(근거 약함 9, 제약 불확실 6, 되돌리기 어려움 2, 한 건에 여러 개). 12건 중 나중에
  통과한 것은 없다: 10건은 사람이 스레드를 닫았고 2건은 `DISPUTED`로 남아 있다. 이 불일치는 판정이 아니라 쓰인 결정문의
  품질에 관한 것이다. jev에게 승인 여부를 물은 적은 없으므로, LLM 판정자와 얼마나 일치할지에 대한 데이터는 아직 없다.
- **jev 키는 엔진에만 있다.** `TYPESAFE_*`는 어떤 에이전트 프로세스에도 넘기지 않는다(`env_policy.py`, `NEVER_PREFIXES`).
  jev는 엔진만 부른다. 이 설계는 그것을 지킨다.

## 결정 (2026-11-01)

| # | 결정 |
|---|---|
| V1 | **마감의 일을 둘로 나눈다:** 판정과 작성. 흐름의 마감에 `verdict: "llm"`(기본, 지금 동작) 또는 `"jev"`가 생긴다. |
| V2 | **`verdict: "jev"`면 jev가 판정하고 마감의 캐릭터가 쓴다.** 작성자는 판정자 페르소나, 제공자, 도구를 그대로 쓰고, 그 차례는 이번 라운드가 승인됐다고 알리며 지금 형식의 결정문, 근거, 제약, 조치를 요청한다. |
| V3 | **jev는 확신할 때만 정한다.** 승인 임계값 이상이면 jev가 승인하고, 거부 임계값 이하면 jev가 이유와 함께 거부한다. 그 사이면 지금과 똑같이 LLM 판정자가 정한다. jev 오류나 시간 초과도 LLM 판정자로 돌아간다. |
| V4 | **작성자는 이의를 낼 수 있다.** 결정문 대신 `OBJECTION:`과 이유로 답할 수 있다. 그러면 이번 라운드는 **승인하지 않고**, 이의는 다음 라운드가 읽는 거부 사유가 되며, `verdict_disagreement` 이벤트가 기록한다. (사람에게 넘기기, jev 판정 우선보다 이것을 골랐다.) |
| V5 | **jev 게이트는 바뀌지 않는다.** 쓰인 결정문은 여전히 검사하고, `DISPUTED`면 여전히 조치를 묶어 사람에게 넘긴다. |
| V6 | **그림자 모드가 먼저다.** 어떤 흐름이든 `verdict: "jev"`를 쓰기 전에, LLM 판정자가 판정할 때마다 같은 자료로 jev에게도 같은 판정 질문을 하고 둘 다 기록하되 결과는 바꾸지 않는다. J2의 측정이 기준을 넘은 뒤에만 흐름별로 바꿀 수 있다. |
| V7 | **키는 엔진에 둔다.** jev는 엔진이 `jev-mcp`로 부르고 에이전트는 부르지 않는다. `TYPESAFE_*`는 어떤 자식 환경에도 들어가지 않는다. |

## 판정 질문

jev는 LLM 판정자가 보는 것을 본다: 과제와 이번 라운드의 판정 자료(제안이나 라벨 붙은 초안들과 비평, 정리자가 있으면
요약, [flow-graph.md](flow-graph.ko.md) G3 참고). `jev_ask` 한 번이므로 추가 질문은 비용이 거의 없다:

| id | 종류 | 질문 (전체를 보냄) |
|---|---|---|
| `approve` | check | 판정자가 이 제안과 그 비평을 읽고 비평의 타당한 점을 받아들여 최종 결정을 직접 쓴다. 지금 이 자료로 그 결정을 쓸 수 있는가? 예: 제안이 좋은 바탕이고 비평이 제기한 것은 결정 안에서 정리할 수 있다(수정, 제약, 확인할 것, 범위 축소). 아니오: 비평이 제안의 근본이 틀렸거나 과제를 놓쳤거나 뒷받침 없는 주장에 기댄다는 것을 보여 주므로 먼저 다시 써야 한다. |
| `fundamental` | check | 비평이 결정 안에서는 고칠 수 없고 제안을 다시 써야 하는 결함을 보여 주는가? |
| `evidence` | score | 제안의 주장이 자료에 인용된 근거에 얼마나 기대는가? (없음, 일부, 대부분, 전부) |

`approve`만 판정하고, `fundamental`과 `evidence`는 불일치를 설명하려고 기록한다. 이것은 질문 2판이다(기록에
`questions: 2`). 1판은 제안이 비평의 막는 점에 모두 답했는지 물었는데, 한 라운드에서 비평은 제안 뒤에 오므로 제안은 결코
답할 수 없고, jev는 과거 60건 중 56건에 아니라고 했다([재시험](../analysis/2026-11-01-jev-verdict-replay/results.ko.md)). 임계값은 `yes_at_or_above` 0.8과
`no_at_or_below` 0.2에서 시작해 그림자 데이터로 맞춘다(J2).

## `verdict: "jev"`의 라운드

```
가운데(변경 없음) ──► jev 판정 ──┬─ 승인 (p ≥ 0.8) ─► 작성자(마감 캐릭터) ─┬─ 결정문 ─► jev 게이트 ─► 조치 / 묶음
                                 │                                        └─ OBJECTION ─► 미승인, 다음 라운드
                                 ├─ 거부 (p ≤ 0.2) ─► 미승인, jev의 이유를 다음 라운드로 (LLM 호출 없음)
                                 └─ 그 사이 / jev 오류 ─► 지금처럼 LLM 판정자
```

- 거부되는 라운드는 LLM 판정자 차례 대신 jev 호출 한 번이다. 승인되는 라운드는 jev 호출 한 번과 작성자 차례(지금 판정자와
  같은 LLM 차례)다.
- 마지막 라운드는 지금 규칙 그대로: 승인이 없으면 스레드는 `REVISION_REQUIRED`로 끝난다.
- `UNVERIFIED`는 그대로다(자리나 작성자의 대체 답).
- transcript: jev 판정은 역할 `JEV Verdict`, `seat: "close"`, 확률과 세 답을 가진 항목이다. 작성자 항목은 판정자 역할을
  유지하므로 리포트, jev 게이트, 감사가 지금처럼 읽는다.

## 그림자 모드 (J1)

`GUARDIAN_JEV_VERDICT=shadow`(기본 `off`, ops와 design 프로젝트 포함): LLM 판정자 차례가 끝날 때마다 엔진이 같은 자료로
판정 질문을 하고 `jev_verdict` 이벤트(`{p_approve, jev, llm, agree, blocking, evidence}`)를 남긴다. 그 밖에는 아무것도
바뀌지 않는다. jev 실패는 기록하고 건너뛴다. `guardian.py --jev-verdict-report`가 기록된 이벤트를 프로젝트 종류별로
요약한다: 일치율, 혼동표, 불일치와 그 스레드 id.

`verdict: "jev"`를 허용하는 기준(J2, 운영자가 확인할 제안 숫자): 그림자 판정 30건 이상. 그중 jev가 확신한(구간 밖) 경우
LLM 판정자와 일치율 90 % 이상. jev가 확신해 승인했는데 LLM 판정자가 거부한 모든 경우를 사람이 읽고 해가 없다고 확인.

## 흐름 파일

```json
"close": {"character": "jev_mcp_decision", "verdict": "jev", "allowed_tools": ["jev_mcp:run_tests"]}
```

`verdict`는 모든 흐름에서 유효하지만, 운영자가 허브에서 켜기 전(`GUARDIAN_JEV_VERDICT=on`)에는 `"jev"`를 저장할 때 거부한다.
그래서 J2 전에는 어떤 흐름도 쓰지 않는다. 편집기의 마감 패널은 이 안내와 함께 선택지를 보여 준다.

## 이벤트 약속 (J3)

화면은 이벤트로 실행 상태를 정하므로, jev가 판정한 라운드도 화면에는 다른 라운드와 똑같아 보여야 한다.

- jev가 정했든 LLM 판정자가 정했든, 작성자가 이의를 냈든, 모든 라운드는 지금 모양(`approved`, `unverified`)의 `verdict`
  이벤트를 하나 낸다(이의는 `approved: false`). 목록, 현황, 실행 상태(`lib/debate.ts`)는 이 이벤트와 jev 게이트만 읽으므로
  바꾸지 않아도 된다.
- 새 이벤트는 더해질 뿐 대신하지 않는다: `jev_verdict`(`p_approve`, `verdict` approve | reject | uncertain, 임계값,
  `blocking`, `evidence`, `decided_by` jev | llm)와 `verdict_disagreement`(작성자의 이의, 또는 그림자 모드에서 LLM 판정자와
  다른 jev 판정). 둘 다 `seat: "close"`를 갖는다.
- `final_decision`에 `verdict_by`(`llm` 또는 `jev`)와, jev일 때 `jev_verdict`(확률과 답)가 붙어, 결과 화면과 리포트가
  이벤트 없이도 누가 정했는지 안다.

## UI 영향 (J4)

2026-11-01 대시보드 코드로 확인했다. 지금 화면은 판정이 LLM 판정자 한 차례라고 가정한다.

| 화면 | 지금 | jev 판정일 때 | 바꿀 것 |
|---|---|---|---|
| 결과: 판정 흐름 칩(`ResultView.tsx`, `verdictSteps`) | "LLM 판정자 -> jev-mcp -> 게이트" 고정 | jev가 승인해도 "LLM 판정자: 승인", LLM이 판정하지 않은 라운드에 "LLM 판정자: 수정 필요"로 보임 | 첫 단계가 `verdict_by`를 따른다: "jev 판정 (p 0.86)" 다음 "작성자". 이의는 따로 한 단계 |
| Agents 모니터(`lib/agentMonitor.ts`, `Timeline.tsx`, `LaneCards.tsx`) | 모르는 이벤트 종류는 무시 | `jev_verdict`, `verdict_disagreement`가 안 보이고, jev가 정한 라운드에서 판정자 레인이 "대기"로 남아 멈춘 것처럼 보임 | 두 이벤트의 타임라인 항목. 그런 라운드에서 판정자 레인은 "jev가 판정", 작성할 때는 "작성자"로 표시 |
| 대화 기록(결과 화면) | 역할을 원문 칩으로 표시 | `JEV Verdict` 항목이 영어 원문과 숫자로 보이고, `OBJECTION`이 보통 판정자 발언처럼 보임 | 역할 라벨과 읽을 수 있는 한 줄(확률, 판정, 이유), 이의 표시 |
| 실행 상태(목록, 현황) | `verdict` 이벤트와 게이트로 | jev 라운드가 `verdict`를 내지 않을 때만 틀림 | 위 이벤트 약속이 지켜지면 없음 |
| 리포트, Word/PowerPoint 내보내기(`debate_report.py`, Go 리포트) | 라운드마다 판정자 글이 있음 | jev가 거부한 라운드에는 판정자 글이 없음 | 그런 라운드에 jev 판정 행(확률, 판정, 이유) |
| 흐름 에디터(마감 패널, 캔버스, 갤러리) | - | - | 판정 방식 선택과 임계값, 캐릭터 라벨 "작성자", 마감 노드와 갤러리 행에 "jev 판정". 허브가 켜기 전에는 흐리게 하고 안내. 선택 옆에 흐름의 그림자 일치율(J1 데이터)을 보여 줄 수 있음 |
| 승인, 자동 적용, 빠른 질문, 설정, 노드 | - | 그대로: 조치는 여전히 작성자가 제안하고, 자동 적용은 여전히 jev 게이트 `PASSED`가 필요 | 없음 |

같은 빈틈이 지금 [flow-graph.md](flow-graph.ko.md) G3의 정리자에도 있다: 대화 기록에 원문 역할 `Summariser`가 보이고,
모니터 레인이 역할 순서에 자리가 없다. J4가 같은 방식으로 둘 다 고친다. 그 전에 따로 고칠 수도 있다.

## 단계

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| J1 | 그림자 모드: 판정 질문, `jev_verdict` 이벤트, `--jev-verdict-report`, 가짜 jev로 테스트 | 허브에 `GUARDIAN_JEV_VERDICT=shadow`로 배포 |
| J2 | 운영 토론에서 측정 | 위 기준을 넘는지 확인, `docs/analysis/`에 기록 |
| J3 | `close.verdict: "jev"`: 컴파일, 엔진 분기, 작성자 차례와 `OBJECTION`, 대체 경로, 이벤트 | `llm`의 golden dump 변화 없음, 분기마다 seeded 테스트, 모든 분기에서 위 이벤트 약속 유지 |
| J4 | 대시보드: "UI 영향"의 모든 행, 정리자 라벨 포함 | 대시보드 테스트, 끝까지 실행된 jev 판정 실행과 정리자 실행을 브라우저로 확인 |

## 하지 않는 것

- jev가 글을 쓰거나 코드를 읽는 것.
- jev 게이트를 없애거나 약하게 하는 것.
- 다른 자리(비평가, 정리자)의 jev.

## 열린 점

- 임계값과 J2 기준(위에 제안).
- 독립성: `verdict: "jev"`면 같은 모델이 판정하고 쓰인 결정문도 검사한다. 보는 대상이 다르다(라운드 대 결정문). J2에서 오류가
  함께 움직이면 둘 중 하나에 `jev-preview`를 쓸 수 있다.

## 구현 메모

### J1 그림자 모드 (2026-11-01 구현)

`cli/llm-agent/jev_verdict.py`(질문, TypeSafe System One API로 묻는 `JevVerdict.ask`, `parse_answer`, `compare`, `report`),
LLM 판정자의 `verdict` 이벤트 바로 뒤의 `DebateEngine._shadow_verdict`, `guardian.py --jev-verdict-report`, 테스트
`test_jev_verdict.py`(실제 엔진 실행에 가짜 jev). 세부:

- **jev-mcp가 아니라 API 직접 호출.** 허브의 jev-mcp(`owner/jev-mcp` 0.1.0, 게이트의 서버)에는 `jev_ask`가 없어서 첫
  배포(`998e7af`)는 오류만 기록했다. 이제 엔진이 `POST <TYPESAFE_BASE_URL>/v1/systemone`을 직접 보낸다(표준 라이브러리).
  설정은 jev-mcp가 읽는 것과 같다: `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`(기본 `https://api.typesafe.ai`),
  `TYPESAFE_DEFAULT_MODEL`(기본 `jev-latest`), 환경이나 llm.env에서. 오류는 HTTP 상태를 말하고 키는 담지 않는다. 허브에서 확인
  (2026-11-01): 호출당 약 0.2초. 답하지 않은 `[차단]`이 있는 비평은 `p_approve` 0.03(거부), 깨끗한 비평은 0.41(불확실)이라
  임계값은 J2 데이터로 맞춰야 한다.
- **답의 모양.** System One은 check에 `answers.<id>.noul`(예일 확률), score에 확률과 함께 `score`를 돌려준다(2026-11-01
  모델 `jev-1.14.0`, 짧은 입력에 약 0.3초). `approve` 확률이 없으면 오류다.
- **jev가 읽는 것.** `Task: <에이전트용 과제>`와 판정 자료(마감이 요약을 받으면 요약), 최대 24 000자. 더 길면 앞과 뒤를
  남기고 잘린 곳을 표시한다(기록의 `clipped`).
- **이벤트.** `jev_verdict`(`seat: "close"`, `mode: "shadow"`, `p_approve`, `verdict`, `p_blocking`, `evidence`, 임계값,
  `model`, `latency_ms`, `clipped`, `llm`, `agree`, `decided_by: "llm"`), jev 실패 때는 `{mode, error}`. jev가 확신하고
  다르면 `verdict_disagreement`(`kind: "shadow"`). `verdict` 이벤트는 그대로다. 대체 답(UNVERIFIED)의 판정은 비교하지 않는다.
- **설정.** `GUARDIAN_JEV_VERDICT=shadow`로 켠다(그 밖은 끔, `on`은 J3용으로 남김). `GUARDIAN_JEV_VERDICT_YES` / `_NO`
  (기본 0.8 / 0.2, 엇갈리면 기본값). 허브에서는 설치 스크립트가 보존하는 `~/.guardian/agent.env`의 운영자 키다.
- **보고.** `<data>/debate-jobs/*/events.jsonl`을 읽는다: 판정 수, jev가 확신한 수, 일치율, 혼동표(jev 승인/거부/불확실 대
  LLM 승인/거부), 프로젝트 종류별, 사람이 읽을 jev 승인-LLM 거부 사례, J2 기준(30건, 일치율 90 %).
- **배포 (2026-11-01).** 허브 `agent.env`에 `GUARDIAN_JEV_VERDICT=shadow`를 더하고(설치 스크립트가 보존) `998e7af`, 이어서
  API 직접 호출의 `14d8276`을 배포했다(그 사이 돈 토론이 없어 오류 이벤트는 기록되지 않았다). 배포된 릴리스에서 판정 호출은
  약 0.2초에 답했다(`jev-1.14.0`). 이제 J2가 시작된다: 허브의 숫자는 `guardian.py --jev-verdict-report`로 본다(서비스 환경으로
  실행하지 않으면 모드가 `off`로 보인다).
- **과거 데이터 재시험과 질문 2판 (2026-11-01).** 허브의 과거 LLM 판정 60건을 다시 돌렸다
  (`docs/analysis/2026-11-01-jev-verdict-replay/`). 1판: 확신 거부 56건, 일치율 14 %. 2판(0.8 / 0.2): 확신 승인 13건 모두 LLM과
  일치, LLM이 승인한 곳에서 확신 거부 3건, LLM 거부 8건은 모두 불확실. 그림자 모드를 2판으로 바꿨고 `p_blocking`은
  `p_fundamental`이 됐으며, 보고서는 지금 판의 기록만 센다. J3는 새 그림자 기록의 J2를 기다린다.


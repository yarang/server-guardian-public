# 팀 흐름: 제안 → 비판 → 판정이 아닌 구조 (설계)

[English](team-flows.md) | 한국어

상태: 2026-10-22 제안. 같은 날 운영자가 범위를 정했다. 검증된 흐름 템플릿의 닫힌 목록이며, 첫 묶음은 병렬 패널, 순차 파이프라인,
찬반 대립, 단독 작성 + 검증이다. 2026-10-23 승인. F0, F1, F2, F3는 구현했다(끝의 구현 메모). F4, F5는 아직이다.
출발점: "팀 구조도 제안 → 비판 → 판정이 아닌 다른 구조도 만들 수 있게 하고 싶다."

## 문제

팀 파일은 세 자리(`expert`, `critic`, `judge`와 `docs/design/multi-perspective.md`의 선택 항목 `proposers`)만 채운다.
토론의 모양은 데이터가 아니라 `DebateEngine._run_debate`(`debate_engine.py`)의 라운드 루프다. Expert(와 proposers)가 쓰고,
Critic 패널이 그 제안을 읽고, Judge가 판정하며, 반려되면 비판을 들고 다음 라운드가 시작된다. "한 제안을 비평가들이 공격한다"가
아닌 일(여러 독립 의견 수집, 여러 손을 거친 문서 작성, 이름 붙은 두 선택지 사이의 결정, 한 저자의 결과물 점검)은 이 모양에
억지로 끼워 맞춰야 한다.

## 아이디어: 흐름은 중간을 바꾸고, 마무리는 공유한다

라운드 중간 이후는 모든 구조에서 같고, 이 프로젝트의 보장을 담고 있다.

```
 중간 (흐름마다 다름)              마무리 (공유, 변경 없음)
 ------------------------          -----------------------------------------------------------
 `proposal`과 `critique` 글,       Judge가 decision / rationale / constraints / actions 작성
 transcript 항목을 만든다          -> jev 게이트 -> 제안 액션 (INVALID 검사, STALE 드리프트)
                                   -> 자동 적용 / 계획 / 배치 투표 -> 토론 저장 + 이벤트
```

흐름은 (과제, 이력, 멤버)를 (`proposal`, `critique`, transcript 항목)으로 바꾸는 함수다. Judge, jev 게이트, 인용 검사, 대체 응답이
섞이면 `UNVERIFIED`, 액션 목록, "되돌릴 수 없는 일은 사람이 한다"는 흐름마다 다시 만들지 않으므로 새 구조가 이 보장을 약하게 만들 수
없다. 의도적으로 유지하는 결과는 다음과 같다.

- **모든 흐름에 Judge가 필수다.** Judge 없는 흐름은 없고, 보고서 전용 팀은 `allowed_tools: []`로 만든다.
- **transcript의 역할 이름은 그대로** `Domain Expert`, `Critic Agent`, `JEV-MCP Judge`다. 흐름은 항목과 이벤트에 선택 필드
  `stage`(와 `side`)만 더한다. 대시보드, `jev`, 감사, 기존 소비자는 그대로 동작한다.
- **흐름이 없으면 변화도 없다.** `flow`가 없는 팀은 `debate`이고 프롬프트와 스냅샷이 바이트 단위로 같다(`fleet-provisioning.md`의
  `fleet-ops` 선례). 기존 팀은 프롬프트 버전을 올리지 않는다.

## 흐름 (첫 묶음)

모든 흐름에서 `max_rounds`는 Judge 라운드 수다. 2라운드부터는 `multi-perspective.md`처럼 리드(`expert`) 혼자 정리하므로 반려가
전체 팬아웃을 되풀이하지 않는다. 새 흐름의 기본 `max_rounds`는 1이다.

| `flow.kind` | 중간 | 쓰는 자리 | 어울리는 일 |
|---|---|---|---|
| `debate` (기본) | Expert가 쓰고 Critic 패널이 공격 | `expert`, `critic`, `proposers` | 변경 검토, 설계 결정 |
| `panel` | 패널 2-3명이 서로 모르게 병렬 응답, 비판 없음. Judge는 중립 라벨(라운드마다 회전)로 응답을 보고 하나의 결정을 씀 | `expert` + `proposers`(1-2명, 기존 한도) | 의견 수집, 탐색, 이름 짓기, 선택지 나열 |
| `pipeline` | 단계가 순서대로 실행되며 각 단계가 앞 결과를 읽고 고치거나 늘림(초안, 보강, 편집). 마지막 결과가 proposal | `flow.stages`(멤버 2-4명), `critic`은 선택 | 보고서, 문서, 여러 손을 거치는 설계 |
| `versus` | 두 변호인이 반대 입장을 변호하고 서로에게 한 번씩 답함(`exchanges`, 기본 1). Judge가 둘 사이를 결정 | `flow.sides`(정확히 2개, 멤버 + `stance` 한 문장) | A/B 기술 선택, 트레이드오프 |
| `verify` | Expert가 혼자 쓰고 검증자가 체크리스트로 결과를 확인해 실패 항목만 증거와 함께 보고(대안 제시 없음) | `expert`, 검증자로서의 `critic` | 공격이 낭비인 가볍고 위험이 낮은 일 |

`verify`는 `debate`와 검증자 프롬프트(따지지 말고 확인할 것)와 한 라운드라는 점이 다르고, critic 자리를 재사용한다.

팀 파일에 더해지는 항목(모두 선택, 없으면 `debate`)은 영문 문서의 JSON 예시와 같다. 흐름의 멤버는 멤버 필드(`providers`, `host`,
`code_tools`)를 그대로 가지므로 단계나 편이 워커 노드에서 생각할 수 있다(`remote-agents.md`). `expert`는 계속 필수다(2라운드부터
정리하는 리드).

## 결정

1. **그래프가 아니라 닫힌 목록.** 종류는 엔진에서 구현하고 시험하며 팀은 하나를 고른다. 일반 단계 목록이나 노드 그래프는 위 보장을
   단계마다 일반화해야 하므로 지금은 하지 않는다.
2. **마무리는 공유한다**(위 참조). Judge가 필수인 이유이기도 하다.
3. **`team_manager.py`에서 검증한다.** 모르는 `kind`, 맞지 않는 멤버 수, 없거나 400자를 넘는 `stance`, 다른 종류 팀의 흐름 필드는
   `TeamValidationError`다. `debate`일 때 스냅샷은 `flow`를 생략한다.
4. **ops·design 프로젝트**는 `debate`와 `verify`만 실행한다(지금 proposers와 같음). 다른 흐름은 조용히 무시하지 않고 시작할 때
   분명한 메시지로 거절한다.
5. **이벤트**: `debate_started.flow`(종류), `agent_started`/`agent_finished`에 `stage`(0부터)와 `versus`의 `side`(`A`/`B`).
   필드만 추가하므로 `debate-agent-retrieval.md`의 이벤트 계약은 그대로 유효하다.
6. **`multi-perspective.md`의 공정성 규칙을 따른다.** 서로 모르는 응답은 라운드마다 회전하는 중립 라벨을 쓰고, `versus`의 입장은
   모델이 아니라 팀 파일이 정하며, Judge는 위치가 결과를 좌우하지 않도록 회전된 A/B 순서로 양쪽을 본다.

## 단계

- **F0 리팩터링, 동작 변화 없음.** 라운드 루프의 중간을 `_flow_debate()`(proposal, critique, 제공자, transcript 항목 반환)로 잘라내고
  `_run_debate`가 `lineup.flow_kind`로 분기한다. 기존 테스트와 모든 내장 팀의 프롬프트 바이트 비교가 관문이다.
- **F1 스키마와 가벼운 흐름.** `team_manager.py` 파싱, 검증, 스냅샷, `verify`와 `panel`(둘 다 기존 초안·critic 경로 재사용).
  `test_debate_*`가 쓰는 가짜 LLM으로 시험한다.
- **F2 `pipeline`.** 순차 단계, 프롬프트에 단계 사슬, 단계별 대체 응답 처리.
- **F3 `versus`.** 입장, 한 번의 교환, Judge에게 회전된 A/B 순서.
- **F4 대시보드.** 팀 편집기에 흐름 선택과 종류별 입력, 한 줄 설명, 구성 미리보기. 토론 결과 transcript에 단계·편 배지.
  `--teams`가 흐름을 나열한다. 문자열은 `ko`와 `en` 모두.
- **F5 평가.** 흐름별 심어 둔 결함·결정 과제를 `debate`와 비교한다(`proposer_eval.py` 도구). 공유 Codex 쿼터를 쓰므로 운영자의
  승인이 필요하다. 내장 예시 팀은 그 뒤에만 추가한다.

## 하지 않는 것

- 임의 그래프, Judge 판정 외의 모델 출력에 따라 달라지는 반복, 지금의 검색 이상으로 단계 안에서 도구 쓰기.
- Judge 없는 흐름, jev 게이트 없는 자동 승인.
- 페르소나나 제공자를 고르는 방식 변경.

## 열린 점

- `panel` 크기: 4명(1 + proposers 3)도 가능하지만 `multi-perspective.md`에서 잰 메모리 비용(초안 3개에서 1.5-2.5배)을 보면 기존
  한도(패널 3명)로 시작하고 F5 뒤에 올리는 편이 낫다.
- `versus`의 `exchanges`는 고정(1, 최대 2)이며 Judge가 세 번째 교환을 요구할 수 없다.
- 팀 편집기에 흐름별 라운드당 호출 수를 보여 주면 좋다. 숫자는 F5에서 나온다.

## 구현 메모

- **F0** (`bdbed56`): 순수 리팩터링. 라운드 중간이 `FlowContext`, `Middle`과 함께 `DebateEngine._flow_debate()`가 되었다. 관문은
  결정적 가짜 LLM으로 모든 내장 팀을 다섯 시나리오(기본, proposers, 이어서 실행, 대체 응답 두 가지)로 돌려 모든 프롬프트, transcript
  항목, 세션, 이벤트를 기록한 골든 덤프이며, 전후가 바이트 단위로 같다.
- **F1**: `team_manager.py`가 `flow`(`debate`, `panel`, `verify`)를 파싱한다. 모르는 종류나 그 종류가 받지 않는 필드는
  `TeamValidationError`다. 스냅샷은 `debate`가 아닐 때만 `flow`를 싣는다. 설계와 다른 점:
  - 패널은 Judge 라운드 1회로 제한한다(`max_rounds` 2 이상은 거절). 2라운드부터는 리드가 자기 답을 정리하는 것뿐이라 비판 없는
    토론이 되기 때문이다. `verify`는 여러 라운드를 허용하고, 둘 다 기본값은 1이다.
  - 패널에는 critic 자리가 없다(`critic`은 선택이고 주면 거절). 저장된 라인업을 `--critic`으로 덮어쓰면 경고하고 critic을 버린다.
  - `verify`는 proposers를 받지 않는다. 검증자 프롬프트(`VERIFY_TASK`)는 실패 항목과 증거만 요구하며, 깨끗한 응답은 "All items
    pass."이다.
  - ops·design 프로젝트는 `debate`와 `verify`를 실행하고, 패널은 LLM 호출 전에 거절한다.
  - `RETRIEVAL_PROMPT_VERSION`은 `2026-10-23.p18`(F2부터 p19). 기존 팀은 이 버전 표시 외에는 변하지 않는다(버전 문자열을 되돌리면 골든 덤프가
    같다).
  - `guardian.py --teams`가 흐름을 표시하고 빈 critic 줄은 생략한다. Go 핸들러는 팀 JSON을 그대로 넘긴다. 대시보드 편집기
    (`lib/debate.ts`)는 팀을 저장할 때 아직 `flow`를 버린다: F4.
- **F2** (`pipeline`): `flow.stages`(멤버 2-4명, 이 종류만 받는다)가 `Lineup.stages`로 파싱되고, 스냅샷은
  `flow: {"kind": "pipeline", "stages": [...]}`를 싣는다. 단계는 페르소나·제공자·`host`·`code_tools` 검사와 `--teams` 목록
  (`stages : a -> b -> c`)에 포함된다. 엔진: `_flow_pipeline()`. 비평가 쪽 절반은 `_flow_critique()`로 분리해 `debate`와
  공유한다(골든 덤프 동일). 결정과 설계와 다른 점:
  - 단계는 바로 앞 단계의 결과만 읽고("Work of stage N") diff가 아니라 전체 결과를 돌려준다. 마지막 결과가 proposal이다.
    transcript 항목은 역할 `Domain Expert`를 유지하고 `stage`(0부터)를 갖는다. `agent_started`/`agent_finished`도 `stage`를
    싣는다(`EventWriter.emit`의 최상위 선택 필드).
  - 세션 키는 위치 기준(`Domain Expert:stage{i}`)이라 같은 페르소나가 두 단계에 있어도 세션이 둘이다.
  - `critic` 자리는 선택이다. 없으면 한 라운드만 돈다(`max_rounds` 2 이상은 거절, `--max-rounds`로 덮어쓰면 경고하고
    1로 줄인다). Judge는 proposal만 읽는다. `expert`는 계속 필수다. 2라운드부터 다시 쓰는 리드이므로, critic 없는
    파이프라인에서는 호출되지 않는다.
  - 2라운드부터(critic이 있을 때) 단계는 다시 돌지 않는다. 리드(`expert`)가 이전 proposal("Proposal of the earlier
    round", `draft_bundle`로 전달)을 비판에 맞춰 고쳐 쓴다. 이 항목에는 `stage`가 없다. 이어서 실행한 스레드는 새 실행의
    첫 라운드에서 마지막 비판을 프롬프트에 담아 단계를 다시 돌린다.
  - 대체 응답을 낸 단계는 사슬을 끝내고 critic을 건너뛴다. Judge의 판정은 다른 곳과 같이 `UNVERIFIED`다.
  - `proposers`가 있는 파이프라인 팀은 거절한다(단계가 곧 초안 작성). 인접한 두 단계가 같은 제공자를 선호하면 경고한다.
  - ops·design 프로젝트는 패널과 같이 LLM 호출 전에 파이프라인을 거절한다.
  - `RETRIEVAL_PROMPT_VERSION`은 `2026-10-23.p19`. 버전을 되돌리면 골든 덤프가 같다. 대시보드 편집기는 저장할 때 아직
    `flow`와 `stages`를 버린다: F4.
- **F3** (`versus`): `flow.sides`(정확히 2명, 각자 400자 이하의 `stance`)와 `flow.exchanges`(1-2, 기본 1)를 파싱해
  `Lineup.sides` / `Lineup.exchanges`에 담는다. `Member.stance`는 대결 측에서만 스냅샷에 들어가고, `flow`의 `sides`와
  `exchanges`는 이 종류에만 나온다. 측은 페르소나·제공자·`host`·`code_tools` 검사와 `--teams` 목록에 포함된다
  (`side A: persona(providers)  stance`). 엔진: `_flow_versus()`. 결정과 설계 대비 차이:
  - **호출**: 두 측이 서로 모른 채 병렬로 먼저 입장을 낸다. 교환도 동시에 이루어진다(둘 다 상대의 가장 최근 주장에
    답하고, 이미 본 반박에 다시 답하지 않는다). 호출 수 = 2 + 2 x `exchanges` + Judge. 입장은 매 프롬프트에 반복되며
    모델에서 가져오지 않는다.
  - **한 라운드.** `max_rounds` 2 이상은 거절한다(반려되면 같은 두 주장을 되풀이할 뿐이다. 더 긴 교환은
    `flow.exchanges`로 한다). `--max-rounds`로 덮어쓰면 경고하고 1로 줄인다. 그래서 "이후 라운드" 분기가 없다. `expert`
    자리는 스키마상 필수지만 호출되지 않고, 이어서 실행한 스레드는 앞선 Judge의 사유를 프롬프트에 싣지 않고 교환 전체를
    다시 돈다(Judge는 자기 세션을 통해 이력을 읽는다).
  - **critic과 proposers 없음**: 검증이 둘 다 거절한다. 저장된 팀에 `--critic`으로 덮어쓰면 경고하고 무시한다.
  - **Judge가 보는 것**: 이름 없는 두 입장(`### Position A`, `### Position B`, 각각 입장·첫 주장·답변)이다. 어느 측이
    A인지는 `versus_order(thread_id, round)`가 정한다. 스레드의 해시를 라운드 번호로 뒤집은 값이라 재실행해도 같고, 팀
    파일의 첫 측을 늘 앞세우지도 않는다. 대화 기록 항목에는 `side`(`A`/`B`, 팀 파일 순서), `exchange`(0 = 첫 주장),
    `position`(Judge가 본 라벨)이 있어서 대시보드가 두 가지 라벨링을 구분할 수 있다.
  - **세션**은 위치 기준(`Domain Expert:side{i}`)이라 같은 페르소나가 양쪽에 있어도 되고 세션은 둘이다. 두 측이 같은
    제공자를 선호하면 경고한다.
  - **대체 응답**: 대체 제공자로 답한 측이 있으면 교환을 일찍 끝내고, Judge의 판정은 다른 곳과 같이 `UNVERIFIED`다.
  - **이벤트**: `agent_started`/`agent_finished`에 최상위 `side`가 붙는다(`EventWriter.emit(side=)`). `debate_started`는
    `sides`(입장, 에이전트 키, 제공자)를 싣는다.
  - ops·design 프로젝트는 패널·파이프라인과 같이 LLM 호출 전에 versus를 거절한다.
  - `RETRIEVAL_PROMPT_VERSION`은 `2026-10-23.p20`. 버전을 되돌리면 모든 빌트인 팀의 골든 덤프가 같다. 대시보드 편집기는
    저장할 때 아직 `flow`, `stages`, `sides`를 버린다: F4.

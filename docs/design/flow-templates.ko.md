# 흐름 템플릿: 저장해 두고 매개변수로 시작하는 업무 흐름 (설계)

[English](flow-templates.md) | 한국어

상태: 2026-10-29 제안. "결정" 절의 항목은 같은 날 운영자가 정했다. 아직 만들지 않았다 (T1-T4 단계).
출발: "flow로 진행한다면 flow의 템플릿을 만들어서 운영할 수 있도록 설계하자." 대시보드 메뉴 이름은 "업무 흐름"이고,
`debate`(토론)는 여러 흐름 종류 가운데 하나가 된다 (`docs/design/team-flows.ko.md`).

## 문제

지금 흐름은 다섯 종류(`debate`, `panel`, `verify`, `pipeline`, `versus`) 중 하나이며, 팀 파일에 적히고
`debate_engine.py`에 종류마다 함수 하나로 구현돼 있다. 한계는 세 가지다.

- 일을 시작하는 사람이 종류를 고르고 그 종류가 요구하는 것(`versus`의 두 입장, `verify`의 질문)을 채울 수 없다.
  시작 양식은 팀만 고르고, 입장 문장은 팀 파일에 들어 있다.
- 운영자가 흐름을 한 덩어리로 보관·복제·검토할 수 없다. 팀 파일에 누가 어디에 앉는지(구성), 일이 어떤 모양인지(흐름),
  무엇을 묻는지(과제)가 섞여 있다.
- 새 모양이 필요하면 엔진 코드를 새로 써야 한다. 대부분의 모양은 같은 조각의 순서만 다르다.

## 모델

```
단계 종류(코드, 닫힌 목록)  ->  템플릿(데이터)  ->  팀(자리를 채움)  ->  시작(매개변수 값)
```

- **단계 종류**는 엔진이 구현하고 시험한 조각이다. 템플릿은 코드를 담지 않고 조각의 순서만 정한다.
- **템플릿**은 라운드 가운데 부분의 모양, 시작 양식, 과제 문안, 실행할 수 있는 프로젝트 종류를 정의한다.
  기본 팀을 하나 이름으로 가리킨다.
- **팀**은 지금처럼 자리(`expert`, `proposers`, `critic`, `judge`, `flow.stages`, `flow.sides`)를 채운다.
  템플릿은 팀을 id로 참조할 뿐 구성을 복사하지 않는다.
- 템플릿 없는 팀도 그대로 동작한다. 팀의 `flow` 값이 매개변수 없는 암묵적 템플릿이고, `flow`가 없으면 `debate`다.
  골든 덤프와 이미 시작된 스레드의 스냅샷은 바뀌지 않는다.

라운드의 마감은 공유되며 템플릿의 일부가 아니다: 판정자(필수), jev 게이트, 액션 검증, 자동 적용, 계획과 투표,
대체 응답이면 `UNVERIFIED` (`team-flows.ko.md` "문제").

## 단계 종류

| 단계 | 하는 일 | 공정성 규칙 |
|---|---|---|
| `draft` | 한 자리가 초안을 쓴다 | - |
| `blind_draft` | 여러 자리가 서로 보지 않고 쓴다 | 판정자와 이후 단계는 라운드마다 돌린 중립 라벨로 본다 |
| `critique` | 비평가가 반대 의견을 낸다(`mode: object`) 또는 실패한 점검 항목을 나열한다(`mode: checklist`) | `checklist`에서는 "All items pass."가 정상 응답이다 |
| `rewrite` | 앞 결과를 다음 자리가 고쳐 쓴다 | 앞 결과의 사슬이 프롬프트에 그대로 들어간다 |
| `exchange` | 두 측이 동시에 답을 교환한다, 1-2회 | 입장은 사람이 정하고 모델이 정하지 않는다. 판정자는 해시로 돌린 A/B 순서로 본다 |
| `consolidate` | 리드가 하나의 제안으로 합친다 | 2라운드 이후 기본 동작과 같다 |

기존 다섯 종류는 이 조각의 조합이다: `verify` = `draft` + `critique(checklist)`, `panel` = `blind_draft`,
`pipeline` = `draft` + `rewrite` x N, `versus` = `blind_draft`(두 측) + `exchange`, `debate` = 선택적 `blind_draft` +
`critique(object)` + `consolidate`.

넣지 않는 것은 `team-flows.ko.md`의 비목표이며 그대로 유지한다: 모델 출력에 따른 분기, 판정자 평결 외의 반복, 지금의
검색을 넘어서는 단계 안의 도구 사용, 판정자 없는 흐름, jev 게이트 없는 승인. "새 흐름 종류"는 이 조각의 순차·병렬
조합이며 임의의 그래프가 아니다.

## 템플릿 파일

내장: `configs/flow-templates/*.json` (저장소의 데이터, `scripts/ci.sh config`가 검사). 운영자 것:
`<GUARDIAN_DATA_DIR>/flow-templates/*.json`. 내장 id와 같은 custom 파일은 내장을 가리고, custom 파일을 지우면 내장이
다시 보인다 (`TeamManager`의 규칙). `created`와 `modified`는 서버가 찍는다.

```json
{
  "id": "design-decision-versus",
  "name": "Design decision (two positions)",
  "category": "development",
  "projects": ["code"],
  "team": "guardian-dev",
  "steps": [
    {"kind": "blind_draft", "seats": ["side:A", "side:B"]},
    {"kind": "exchange", "seats": ["side:A", "side:B"], "count": 1}
  ],
  "params": {
    "TOPIC":    {"type": "text",   "label": "Question",   "max": 2000},
    "STANCE_A": {"type": "string", "label": "Position A", "max": 400},
    "STANCE_B": {"type": "string", "label": "Position B", "max": 400}
  },
  "task": "Decide: ${TOPIC}",
  "stances": {"side:A": "${STANCE_A}", "side:B": "${STANCE_B}"},
  "max_rounds": 1,
  "reviewed": "2026-10-29"
}
```

- `id`는 파일 이름과 같고 `[a-z0-9-]{1,40}`. `category`는 팀 분류 중 하나다.
- 자리 이름은 팀의 슬롯을 가리킨다: `expert`, `proposer:1-2`, `critic:1-4`, `stage:1-4`, `side:A|B`. 판정자는
  단계 자리가 아니며 마감에서 항상 실행된다.
- `team`은 기본 팀이다. 시작할 때 다른 팀을 고를 수 있고, 그때 자리 검사를 다시 한다.
- `params`는 `command_sets.py`의 모양(`type`, `max`, `values`, `default`)에 `label`을 더한다. 종류는 `string`,
  `text`, `int`, `enum`. 값은 과제 문안과 입장에만 들어가고 셸에 닿지 않는다. 자리표시자는 대문자 `${NAME}`.
- 입장은 사람이 채우는 매개변수이거나 템플릿에 고정된 문장이며, 채운 뒤 400자 이하다. 모델이 쓴 글은 입장이 될 수 없다.
- `reviewed`가 180일을 넘으면 명령 세트처럼 CI가 경고한다.

## 검증

단일 출처는 Python(`flow_templates.py`, 신설)이며 `team_manager.validate_lineup`을 재사용한다. Go는 중계만 하고
검증하지 않는다 (`debate-teams-api.md`의 규칙).

- 단계 1-6개, 한 단계 최대 4자리, 전체 최대 6자리, 라운드당 모델 호출 수 상한. 넘으면 실행이 아니라 저장에서 거부한다.
- 단계가 쓰는 모든 자리가 선택한 팀에 있어야 한다. 어느 단계도 쓰지 않는 구성원은 경고다. 세션 키가 요구하는 곳에서
  페르소나는 중복되지 않는다.
- 제안을 만드는 단계(`draft`, `blind_draft`, `rewrite`, `consolidate`, 리드가 읽는 `exchange`)가 하나 이상 있어야
  하고, 그 마지막 단계 뒤에는 `critique`만 올 수 있다. 아니면 거부. (처음 문구인 "마지막 단계는 제안을 만들어야
  한다"는 마지막 단계가 비평인 모든 토론을 막게 된다.)
- `projects`에 없는 프로젝트 종류는 시작할 때 분명한 메시지로 거부하고 조용히 무시하지 않는다. ops·design 프로젝트는
  그 조합이 시험을 통과한 뒤에만 `projects`에 적는다. 지금은 `debate`와 `verify`만 돌린다.
- 스레드가 `threads.team_snapshot`에 저장하는 것: 템플릿 id, 풀어낸 단계, 매개변수 값, 템플릿 본문의 해시. 템플릿을
  나중에 고쳐도 이미 시작한 스레드는 바뀌지 않는다.

## 엔진

- `_flow_steps(ctx, ...)`는 다른 `_flow_*` 함수처럼 `Middle`을 돌려주고 단계를 해석한다. 단계 종류마다 작은 함수
  (`_step_draft` 등)가 `_flow_critique`, `_flow_pipeline`, `_flow_versus`의 조각을 넘겨받는다.
- 이벤트는 필드만 더한다: `debate_started.template`, `agent_started`/`agent_finished`의 `step`(0부터). `stage`와
  `side`는 그대로. `debate-agent-retrieval.md`의 이벤트 계약은 유효하다.
- 프롬프트가 바뀌면 `RETRIEVAL_PROMPT_VERSION`을 올린다. 기존 종류를 단계로 다시 표현할 때는 골든 덤프가 바이트 단위로
  같아야 한다 (F0의 관문: 내장 팀 전부 x 다섯 시나리오, 가짜 LLM).
- 새 조합은 심어 둔 결함 과제와 결정 과제에서 `debate`와 비교해 측정한 뒤에야 검증됐다고 부른다
  (`proposer_eval.py`의 하네스, Codex 한도를 운영과 나눠 쓰므로 운영자의 OK가 필요하다).

## API

팀과 같은 방식으로 Python CLI와 Go 허브가 맡는다.

- `GET /api/flow-templates`는 `guardian.py --flow-templates --json`을 실행한다 ->
  `{templates: [{id, name, source, data, resolved, errors, warnings}], catalog: {step_kinds, limits, categories, project_kinds}}`
- `POST /api/flow-templates` (`--flow-template-save`, 표준 입력의 JSON) -> `{ok, id, warnings}` 또는 400 `{error}`.
  `DELETE /api/flow-templates/{id}` (`--flow-template-delete`): 내장은 400, 없는 id는 404.
- `POST /api/debates`에 `template`과 `params`가 늘고, 작업 요약에 `flow`와 `template`이 늘어난다. CLI에는
  `--flow-template ID`와 `--param KEY=VALUE`가 늘어난다.

## 대시보드

메뉴 "업무 흐름", 탭 "흐름 종류" (`#/debates/templates`부터 시작, 이전 해시는 `ALIASES`로 유지).

- **갤러리**: 분류별 카드 (`TeamsSection.tsx`의 `TeamRow`/`TeamList`, `Grid`, `Card`, `StatusChip` 재사용). 카드에는
  한 줄 설명, 단계 아이콘 줄, 프로젝트 종류 칩, 라운드당 모델 호출 수를 보인다.
- **편집기**: `TeamDialog.tsx` 패턴의 모달. 단계 목록(추가·삭제·순서), 단계별 자리, 매개변수 표, 선택한 팀으로 자리를
  채운 미리보기와 호출 수. 상태와 검증은 `lib/flowTemplates.ts`로 옮긴다.
- **시작**: `StartForm.tsx`는 템플릿 -> 매개변수 양식 -> 팀(미리 채움) -> 라운드 순서가 된다. "다시 시작"과
  `debatePrefill`은 템플릿과 값을 함께 옮긴다.
- 팀 편집기(설정 -> 흐름 구성)에는 `team-flows.ko.md`의 F4가 비워 둔 `flow` 선택기가 생긴다.
- 문자열은 `ko/debate.ts`와 `en/debate.ts`에 함께 넣는다 (새 그룹 `flowTemplates`). `flow.kind`와 단계 종류는 상태·URL·
  페이로드에서 원시값으로 두고 표시할 때만 라벨로 바꾼다 (`dashboard-i18n.md`).

## 용어

용어집(`docs/glossary.md`)에 넣기 전에 운영자가 확인할 새 낱말: 업무 흐름(workflow), 흐름 종류(flow kind), 흐름 템플릿
(flow template), 단계(step)와 여섯 단계 이름. 기존 낱말(팀, 프로젝트, 스레드, 판정자)은 바꾸지 않는다.

## 단계 계획

- **T1 템플릿 층.** 저장소, 검증, CLI, API, 시작 양식. 단계는 기존 다섯 종류의 별칭으로만 존재하고 엔진은 바뀌지 않는다.
- **T2 단계 엔진 (구현).** `_flow_steps`와 여섯 조각. 다섯 종류를 모두 다시 표현했고 골든 덤프는 같다. 프롬프트가
  바뀌지 않아 버전 상승은 없다.
- **T3a 사용자 정의 단계 (백엔드 구현).** `draft`, `blind_draft`, `critique`의 짧은 목록을 받아 엔진의 단계 목록으로
  옮기고 스레드 스냅샷에 보관한다. **T3b** 갤러리와 편집기 화면(2026-10-30 구현, 아래 참조), **T3a2** 아래 미룬 조각.
- **T4 평가.** 조합별 시험 과제. 결과가 나오기 전에는 내장 예시 템플릿을 추가하지 않는다.
  T4a(기존 다섯 종류, 허브 실행 2026-10-30)는 측정을 마쳤다: `docs/analysis/2026-10-30-flow-eval/results.ko.md`. 예시
  템플릿은 아직 없다(사람 채점과 반복이 먼저). T4b는 T3a 조합을 측정한다.

## 결정

1. **템플릿은 팀을 참조한다 (2026-10-29).** 내장하지 않고 팀을 대체하지도 않는다. 팀 파일, 스레드 스냅샷, 프로젝트의
   `"team"` 키, API 계약이 그대로 유지된다.
2. **범위는 새 흐름 종류까지 넓힌다 (2026-10-29).** `team-flows.ko.md` 결정 1("닫힌 목록, 그래프 아님")을 개정한다:
   닫힌 목록은 이제 단계 종류의 목록이다. 위의 비목표는 유지한다.
3. **마감은 공유되며 템플릿 밖에 있다.** 템플릿은 판정자, 게이트, `UNVERIFIED`를 없앨 수 없다.
4. **입장은 사람에게서 온다.** 매개변수 또는 고정 문장이며 모델 출력이 아니다.

## 열린 점

- 입장을 시작할 때마다 묻는지 템플릿에 고정하는지는 템플릿마다 고르며, 둘 다 지원한다.
- ~~`critique(object)`와 "2라운드 이후 리드 단독 합치기"를 단계로 보여 줄지~~ T2에서 정했다: 둘 다 단계다(`critique`, `consolidate`).
- 비용 라벨은 T4가 측정값을 줄 때까지 호출 수만 보인다.
- ops·design 프로젝트가 돌릴 조합은 시험 뒤 템플릿마다 정한다.

## 구현 메모

### T1 템플릿 층 (2026-10-29 구현, `f33aee9`로 배포)

설계대로 만들었고, 어긋난 점과 한계는 다음과 같다.

- **단계는 하나뿐.** `steps`는 `{"kind": <흐름 종류>}` 하나만 담는 목록이며 기존 다섯 종류의 별칭이다. 엔진은 팀의
  흐름을 그대로 돌리고, 템플릿은 팀을 고르고 과제를 채우고 자기 자신을 기록할 뿐이다. 단계가 둘 이상이거나 다섯
  종류 밖이면 T3 전까지 거부한다.
- **파일.** `cli/llm-agent/flow_templates.py`(저장소, 검증, 채우기; 내장 폴더 `configs/flow-templates/`는 없거나
  비어 있어도 되고, 운영자 폴더는 `<GUARDIAN_DATA_DIR>/flow-templates/`; custom 파일이 내장 파일을 가린다). CLI:
  `--flow-templates --json`, `--flow-template-save`, `--flow-template-delete`, `--debate - --flow-template ID
  --param KEY=VALUE`. API: `GET/POST /api/flow-templates`, `DELETE /api/flow-templates/{id}`, `POST /api/debates`의
  `template`, `params`.
- **매개변수 값의 검증은 Python 단독.** Go는 모양(id 형식, 매개변수 개수와 크기)만 보고 정렬된
  `--param=NAME=VALUE` 인자로 넘긴다. 값은 셸에 닿지 않는다.
- **자리표시 과제.** Go는 과제로 `template: <id>`를 보내고, 채워진 과제는 `result.json`에서 읽어 작업 요약에 대신
  넣는다. 스레드 스냅샷에 `template`(id, 내용 해시, 종류, 매개변수, 입장)을 담고, `lineup.template`과
  `debate_started.template`이 같은 값을 되풀이한다.
- **잘못된 매개변수 값은 실패한 작업**(종료 코드 2, "validation failed")이지 HTTP 400이 아니다. 명세는 Python만
  알기 때문이다. 시작 양식이 같은 검사를 따라 하므로(`lib/flowTemplates.ts`) 보통은 사람이 먼저 문제를 본다.
- **항상 새 스레드.** 템플릿에서 시작한 작업의 "다시 시작"과 "이 스레드에서 이어가기"는 채워진 과제 문장을 자유
  과제로 시작하며 매개변수 양식을 다시 열지 않는다.
- **옛 허브.** `GET /api/flow-templates`가 없으면 템플릿 선택기는 숨고 자유 과제 양식은 그대로다.
- **CI.** `scripts/ci.sh config`가 빈 데이터 폴더로 `--flow-templates --json`을 돌린다. 오류는 빌드를 실패시키고,
  `reviewed`가 180일(`GUARDIAN_FLOW_TEMPLATE_MAX_AGE_DAYS`)을 넘으면 경고를 낸다.
- **내장 템플릿은 넣지 않았다.** 어떤 예시가 자리를 얻을지는 T4가 정한다.

### T2 단계 엔진 (2026-10-29 구현, `8e2a591`로 배포)

다섯 흐름 종류가 모두 `_flow_steps`로 돈다. `_flow_debate`, `_flow_critique`, `_flow_pipeline`, `_flow_versus`는 없어졌다.
설계와 다른 점과 한계:

- **단계 목록은 아직 팀에서 만든다.** `_steps_of(ctx, round)`가 팀의 흐름 종류로 한 라운드의 목록을 쓴다
  (`debate`/`verify`: `draft` 또는 `blind_draft` 또는 `consolidate`, 이어서 `critique`; `panel`: `blind_draft`;
  `pipeline`: 단계마다 `draft` + `rewrite`, 이후 라운드는 리드의 `rewrite`, 선택적 `critique`; `versus`: `sides` 자리의
  `blind_draft`, 그다음 교환 횟수만큼 `exchange`). T3는 이 함수의 입력을 템플릿의 `steps`로 바꾸며 해석기는 그대로다.
- **열린 점 결정:** `critique(object)`와 "2라운드 이후 리드 단독 합치기"는 단계다(`critique`, `consolidate`).
  제안자가 없으면 이후 라운드의 리드는 평범한 `draft`다.
- **조각.** `_step_draft`(draft, blind_draft, consolidate, 파이프라인의 첫 단), `_step_critique`(`mode` `object` 또는
  `checklist`), `_step_rewrite`(한 단, 또는 거부 뒤의 리드), `_step_exchange`(측의 첫 주장과 각 답변 차례).
  상태는 `FlowRun`에 있다. 단이나 측이 대체 응답을 내면 `stopped`가 켜져 뒤 단계는 돌지 않고 라운드는 이전처럼
  `UNVERIFIED`가 된다.
- **프롬프트는 바뀌지 않아** `RETRIEVAL_PROMPT_VERSION`을 올리지 않았다(위 계획은 상승을 예상했지만 바이트 동일 관문이
  있어 필요 없었다).
- **관문.** `cli/llm-agent/test_flow_golden.py`와 `golden/flows.json`: 37개 사례(여섯 모양, 두 라운드 verify,
  비평가가 있는 파이프라인, plain/resumed/canned/canned-critic/canned-mid, ops와 design 종류, 내장 `guardian-dev`와
  `fleet-ops`)를 가짜 LLM과 고정 git 저장소로 돌린다. 첫 판은 이후 라운드의 프롬프트를 놓쳤다(바꾼 프롬프트가 통과했다).
  사례를 늘린 뒤에는 프롬프트 변이 13개가 모두 관문을 실패시킨다. CI의 `GUARDIAN_LLM_PROVIDERS` 값이 시스템 프롬프트
  전달 방식을 바꾸므로 하네스가 이 값을 고정한다.
- **이벤트.** `agent_started`/`agent_finished`(와 같은 에이전트의 검색 이벤트)에 라운드 단계 목록의 순번 `step`이 붙는다.
  판정자에는 없다. 골든 덤프는 이전과 정확히 이 키만 다르다.
- **만들지 않은 것:** 사용자 정의 `steps`(T3), 편집기(T3), 측정(T4).

### T3a 사용자 정의 단계 (2026-10-30 구현, 2026-10-31 `bab58a0`로 배포)

`steps`는 이제 흐름 종류 하나의 별칭 대신 사용자 정의 목록일 수 있다. 새 프롬프트 문장 없이 새 조합을 만드는 가장 좁은
문법이라서 골든 사례는 하나도 움직이지 않았고 `RETRIEVAL_PROMPT_VERSION`도 그대로다. 위 설계와 다른 점과 남은 것:

- **문법.** 첫 단계는 `draft` 또는 `blind_draft`, 선택적인 둘째 단계는 `critique`이고 `mode`는 `object`(기본) 또는
  `checklist`다. 자리는 암묵적이며 묶음 전체만 가리킨다(리드, 제안자 전체, 비평가 전체). 그래서 단계에 `seats` 키가
  없고 모르는 키는 거부한다. 최대 2단계. 흐름 종류 `kind` 하나만 가진 한 단계는 T1 별칭 그대로다.
- **보고되는 종류는 `"steps"`.** 팀의 `flow_kind`는 바뀌지 않는다. 사용자 정의 템플릿은 `debate` 또는 `verify` 팀(리드,
  선택적 제안자, 비평가)이 필요하다. `blind_draft`는 제안자가, `critique`는 비평가가 있어야 하며 저장, 시작, 저장된
  스레드를 이어갈 때 다시 확인한다. 코드 프로젝트 전용이고 `stances`는 거부한다(측이 없다).
- **라운드.** `blind_draft`는 1라운드의 독립 초안이고 이후 라운드는 리드가 합친다. `draft`는 리드의 초안 그대로다.
  `critique` 단계가 없으면 한 라운드만 돌고(`max_rounds`가 1보다 크면 경고와 함께 1로 줄인다) 판정자에게는 패널처럼
  독립 답변으로 보인다.
- **동등성.** `engine_steps`는 흐름 종류가 `_steps_of`에서 만드는 것과 같은 단계 목록을 쓴다. `draft` + `critique`는 제안자
  없는 토론, `draft` + `critique(checklist)`는 `verify`, `blind_draft` + `critique`는 제안자 있는 토론, `blind_draft`
  하나는 패널이다. `test_flow_custom_steps.py`가 이를 고정하므로 그 뒤의 조각은 골든 덤프가 이미 지키는 것이다.
  실제로 새로운 것은 `draft` 하나(한 라운드, 비평가 없음), `blind_draft` + `critique(checklist)`, 어느 팀에서든
  `draft` + 두 비평 방식이다.
- **이어가기.** `Lineup.snapshot()`이 단계를 담고, 템플릿 실행 없이 이어가는 스레드는 단계를 복원한다
  (`flow_templates.restore`). 요청이 팀을 지정해도 같고, 자리를 잃은 팀은 "cannot continue"로 거부한다.
- **미룸(T3a2).** 라운드 안의 `rewrite`와 `consolidate`, `exchange`/versus의 측, 단 자리, 3-6단계 목록. 자기 프롬프트
  문장이 필요해서 프롬프트 버전 상승과 T4b 측정이 따라온다.

### T3b 갤러리와 편집기 (2026-10-30 구현, 2026-10-31 `bab58a0`로 배포)

대시보드(`apps/web-desktop`)만 바뀌었다. API와 검증기는 T1/T3a 그대로이고 검증의 단일 출처는 Python이다. 대화상자는
같은 검사를 따라 해서 왕복 전에 첫 문제를 알려 줄 뿐이다. 위 대시보드 절과 다른 점:

- **탭.** `#/debates/templates`의 "흐름 종류": 분류별 갤러리(편집, 복사, 삭제)와 새 템플릿/편집 대화상자
  (`pages/templates/`, 폼 로직은 `lib/flowTemplateEditor.ts`). 문구는 `debate.ts`가 아니라 자체 카탈로그
  `i18n/{ko,en}/flowTemplates.ts`(그룹 `flowTemplates`)에 둔다.
- **모양.** 대화상자는 흐름 종류 하나의 별칭(`versus`는 입장 포함)과 T3a 사용자 정의 단계(첫 단계 `draft` 또는
  `blind_draft`, 선택적 `critique`의 `object`/`checklist`)를 모두 편집한다. 사용자 정의 단계는 코드 프로젝트 전용이라
  프로젝트 목록이 고정된다.
- **미리보기.** 사용자 정의 단계는 팀을 고르면 라운드당 호출 수(자리 + 판정자)와 빠진 자리를 보여 준다. 별칭은 T4 전까지
  측정값이 없어서 호출 수를 보여 주지 않는다(위 열린 점).
- **저장 규칙.** 저장할 때마다 `reviewed`는 오늘 날짜가 된다. 내장 템플릿은 제자리에서 고치지 않고 같은 id의 사용자
  덮어쓰기로 열린다(id 읽기 전용, 버튼 "복사본 저장"). 삭제는 사용자 템플릿만 된다. 저장 응답의 경고는 배너에 보인다.
- **하지 않은 것.** 시작 양식은 그대로(T1 선택기가 이미 있다)이고 팀 편집기의 `flow` 선택기도 없다. 둘 다 뒤에 붙일 수
  있다.
- **실제 브라우저 확인(2026-10-31, 허브 대시보드를 Chrome에서, 읽기만, 저장 없음).** 탭과 빈 갤러리(허브에 템플릿이 아직
  없음), 새 템플릿 대화상자의 두 모양: 사용자 정의 단계는 `guardian-dev`를 고르면 미리보기("첫 라운드 호출 5회(판정자
  포함)")가 나오고, 별칭 모양은 종류 선택과 프로젝트 체크박스만 있고 호출 수는 없다. 취소하면 남는 것이 없었다. 시작
  양식의 T1 선택기는 시작할 수 있는 템플릿이 없으면 숨는 것이 설계대로라 아직 보지 못했다. 보려면 허브에 템플릿 하나를
  저장해야 하는데, 이는 운영 데이터에 쓰는 일이다.
- **허브의 첫 템플릿과 T1 선택기(2026-11-01, 운영자 요청).** 별칭 템플릿 하나를 `POST /api/flow-templates`로 허브에
  저장했다(사용자 정의, `~/.guardian/data/flow-templates/code-review.json`): `code-review`("코드 검토"), 분류
  `development`, 프로젝트 `code`, 팀 `default`, `debate` 단계 하나, `max_rounds` 2, 매개변수 `TARGET`(string, 필수, 최대
  200), `FOCUS`(enum: 정확성 버그, 보안, 동시성과 자원 정리, 오류 처리, 기본값 정확성 버그), `CONTEXT`(text, 최대 1000,
  기본값 "없음"). 과제는 발견마다 파일:줄 근거, 틀어지는 입력, 심각도를 적고 근거 없는 의심은 메모로 나누며 패치는 내지
  말라고 한다. 코드 프로젝트의 코드 변경 동작은 jev 관문을 넘으면 자동 적용되기 때문이다. 먼저 scratch
  `GUARDIAN_DATA_DIR`에서 검증했다(허브에서도 같은 내용 해시 `3e1573f94c2a3c99`, 오류와 경고 없음). Chrome에서: 갤러리는
  "개발" 아래에 보여 주고, 코드 프로젝트 `server-guardian`에서 시작 양식에 선택기 "시작 방식"("직접 작성" / "템플릿: 코드 검토")가
  나오며, 고르면 세 칸(빈 대상, 기본값의 enum, "없음"이 채워진 text)과 팀 선택 "템플릿 기본 (default)"이 보인다. ops
  프로젝트 `fleet`와 설계 프로젝트 `dashboard-ui`에서는 선택기가 숨는다(프로젝트 `code`만). 토론은 시작하지 않았다.
- **템플릿으로 시작한 첫 운영 토론(2026-11-01, 운영자 승인).** 코드 프로젝트 `server-guardian`에서 `code-review`를
  `TARGET` `cli/llm-agent/flow_templates.py`, `FOCUS` 정확성 버그, `CONTEXT` "없음"으로 시작했다. T1과 T2 모두의 첫 운영
  실행이다: 작업 메타에 `template: "code-review"`와 매개변수가 남고, `debate_started`에 템플릿, 팀 `default`, `max_rounds`
  2, 프롬프트 `p25`가 나오며, 전문가와 비평가 이벤트에 최상위 `step`(0과 1, 판정자는 구현대로 없음)이 붙는다. 1라운드에서
  APPROVED, jev 관문 PASSED, 약 5.5분, 조회 19회, 인용 불일치 없음. `apply_patch`나 `execute_code_action`은 제안되지
  않아 과제의 "패치 금지" 줄이 지켜졌다. 판정자는 `flow_templates.py`의 발견 여덟 개를 승인했다(medium 둘: 정수 `min`
  기본값이 검증과 길이 추정(0)과 변환(-10⁹)에서 다르고, 공백뿐인 `stance`가 통과해 팀 관점을 덮어쓴다. low 여섯: 무한대
  기본값이 `OverflowError`를 내고, 실수가 정수 매개변수로 잘리고, 해시할 수 없는 `projects` 항목이 `TypeError`를 내고,
  `$`가 id 패턴에 끝 줄바꿈을 통과시키고(`command_sets.py`도), 일부 형식에서 `min`/`max`가 무시되고, 깨진 사용자 정의
  파일이 내장으로 대체되는데 `is_custom`은 참이다). 여덟 개 모두 고쳤다(2026-11-01, 같은 날 `51b44b9`로 배포, 허브의
  `code-review`는 새 검증을 통과, 시험은 `test_flow_templates.py`):
  - `min`이 없는 정수 매개변수는 검증, 변환, 길이 추정에서 똑같이 `INT_MIN`(-10⁹)까지 내려가고, 대시보드 편집기
    (`flowTemplateEditor.ts` `maxLen`, `boundsOrder`)도 같이 따른다. 범위 없는 정수는 이제 10자가 아니라 11자로 세므로,
    길이 한도에 한 글자 차로 걸친 템플릿은 이제 거절될 수 있다.
  - 빈 관점은 검증에서 거절되고, `resolve`는 팀 관점처럼 관점의 앞뒤 공백을 잘라낸다.
  - 정수 매개변수는 `int` 또는 부호를 붙일 수 있는 ASCII 숫자 열 자리 이하의 문자열(`WHOLE_NUMBER`)만 받는다.
    실수(`inf`, `nan` 포함), `"１"`, `"1_0"`은 잘리거나 예외를 내지 않고 거절된다. 열 자리 제한은 `int()`의
    4300자리 한도도 막는다. 그 `ValueError`는 호출자가 잡지 않는다.
  - `projects` 항목은 해시하기 전에 문자열인지 확인한다.
  - id, 매개변수 이름, `NAME=value` 인자는 `fullmatch`를 쓴다. `command_sets.py`는 이미 그렇게 하고 있어 발견의 그
    부분은 재현되지 않았다. `team_manager.py`(`TEAM_ID_PATTERN.match`)에도 같은 결함이 있으나 이번 변경 밖이라
    그대로 둔다.
  - 정수가 아닌 매개변수의 `min`과 열거형 매개변수의 `max`는 거절된다. 대시보드는 이것을 쓰지 않는다. 이것을 담은
    손으로 쓴 파일은 이제 검증에 실패한다(허브의 `code-review`는 해당 없음).
  - 읽을 수 없는 사용자 정의 파일은 같은 id의 내장을 가린다: `get`은 None을 돌려주고, 목록은 내장을 보이는 대신 그
    id를 뺀다.
  - 여덟 개 밖으로: 빈 열거형 값도 거절한다.

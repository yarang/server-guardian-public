# 판정기: 마감에 꽂는 판정 부품 (설계)

[English](verdict-sources.md) | 한국어

상태: 2026-11-01 제안. 방향(마감에 판정기 층을 두고, LLM 작성자는 유지하며, 판정기는 허브에 등록하고 흐름은 고르기만
한다)은 같은 날 운영자와 합의했고, 아래 세부는 검토를 기다린다. S1(등록과 그림자 실행)은 2026-11-02에 만들어 `c4a1ef6`으로 배포했다.
S2~S5는 만들지 않았다. [jev-verdict.md](jev-verdict.ko.md)가 이 층의 첫 번째 판정기다.

## 문제

흐름의 마감([flow-graph.md](flow-graph.ko.md))에는 판정자가 하나 있다. 판정을 내리고 결정문을 쓰는 LLM 차례이고, 그 뒤에
고정된 jev 게이트가 온다. 두 가지 질문이 나왔다.

- 판정을 LLM이 아닌 것에서 받을 수 있는가? jev(TypeSafe)는 판정은 하지만 글은 쓰지 않는다([jev-verdict.md](jev-verdict.ko.md)).
  다른 서비스와 검사(MCP 서버 뒤의 보안 스캐너, 프로젝트 테스트)도 판정할 수 있다.
- 대시보드 권한이 있으면 누구나 고칠 수 있는 흐름 파일이 임의 명령을 실행하거나 임의 서버에 닿지 않게 하면서, 흐름이 그런
  서비스를 어떻게 쓸 것인가?

이것들을 LLM 제공자로 꽂는 방법은 통하지 않는다. 제공자는 판정자의 답 전체(판정, 결정문, 근거, 제약, 조치)를 써야 하고,
"모른다"고 말할 수 없으며, 제공자 칸은 모든 자리에 있다.

## 착안

```
마감 = [판정기 1, 판정기 2, ...]  (판정기마다 순서와 역할)
     + 작성자 (마감의 캐릭터, LLM)
     + 공통 검사 (조치 검증, jev 게이트)
```

**판정기**는 LLM 판정자가 읽는 것과 같은 자료로 라운드에 대한 질문 하나에 답하고, 그 밖에는 아무것도 하지 않는다. 결정문은
여전히 LLM이 쓴다.

### 판정기의 약속

- **입력:** 과제, 판정 자료(제안이나 초안들, 비평, 있으면 요약), 프로젝트 id와 종류, 코드를 읽는 판정기를 위한 커밋과 작업
  공간 경로.
- **출력:** `approve`, `reject`, `uncertain`과 선택적으로 확률, 이유(짧은 글), 메타데이터(모델, 시간, 비용).
- **규칙:** 읽기 전용. 시간 제한. 실패나 시간 초과는 `uncertain`으로 치고 기록한다. 판정기는 자기 키만 받는다(`env_policy`
  규칙: `TYPESAFE_*` 같은 것은 에이전트에 가지 않는다).

### 역할

| 역할 | 뜻 | 예 |
|---|---|---|
| `decide` (판정) | 확신하면 그 판정기가 라운드를 정한다. 모르면 다음 `decide` 판정기에 묻고, 아무도 확신하지 못하면 지금처럼 LLM 판정자가 정한다 | jev 판정 (jev-verdict.md V3) |
| `alarm` (경보) | 판정을 바꾸지 않는다. 승인된 라운드에서 울리면 자동 적용을 멈추고 사람이 정한다 | jev "근본 결함" 점수 |
| `gate` (관문) | 승인된 라운드는 이 판정기가 승인해야 통과한다. 아니면 승인하지 않고 이유를 다음 라운드로 넘긴다 | 프로젝트 테스트, 보안 스캔. jev 게이트는 쓰인 결정문에 대한 이 역할이다 |

`alarm`과 `gate` 판정기는 승인 뒤에, `decide` 판정기는 작성자 앞에 돈다. `decide`가 승인해도 작성자는 이의를 낼 수 있고
(jev-verdict.md V4), 그러면 라운드는 승인되지 않는다.

## 판정기 종류

| 종류 | 하는 일 | 바탕 |
|---|---|---|
| `llm` | 지금의 LLM 판정자(기본값이자 마지막 `decide` 단계) | 지금 엔진 그대로 |
| `jev` | 판정 질문으로 System One 요청 한 번 | `jev_verdict.py` (J1) |
| `mcp` | 허브에 등록된 MCP 서버의 도구 하나를 부르고 답을 읽는다 | `mcp_stdio.py`, `configs/mcp_servers.json`의 서버(jev 게이트도 하나를 씀) |
| `command` | 허용된 명령(프로젝트의 테스트 명령)을 작업 공간의 임시 사본에서 실행, 종료 코드 0이면 승인 | 프로젝트의 `test_commands`와 `jev_mcp:run_tests`의 실행 정책 |
| `http` (나중) | 외부 판정 서비스 호출 | – |

## 판정기 등록 (운영자, 허브에서)

흐름은 판정기를 id로 고르고, 판정기가 무엇을 실행하는지는 명령 세트처럼 운영자가 정한다.

- **서버.** MCP 서버는 `configs/mcp_servers.json`에 더한다(코드와 함께 검토하고 배포). 키는 그 서버의 환경(llm.env)에만 두고
  흐름에는 넣지 않는다.
- **판정기 파일.** `configs/verdicts/*.json`(기본 제공)과 `<GUARDIAN_DATA_DIR>/verdicts/*.json`(운영자). 저장할 때 검증하고
  명령 세트처럼 `reviewed` 날짜(180일)를 둔다.

```json
{
  "id": "security-scan", "name": "Security scan", "kind": "mcp", "reviewed": "2026-11-01",
  "server": "secscan", "tool": "scan_change",
  "arguments": {"diff": "${proposal}", "repo": "${workspace}"},
  "answer": {"probability": "result.risk_free", "approve_at": 0.8, "reject_at": 0.2, "reasons": "result.findings"},
  "timeout_s": 60, "projects": ["code"], "roles": ["gate", "alarm"]
}
```

- **인자**에는 정해진 자리표시자만 쓴다: `${task}`, `${proposal}`, `${critique}`, `${material}`, `${workspace}`, `${commit}`,
  `${project}`. 셸도 자유 식도 없다.
- **답**은 정해진 형식으로 읽는다: 확률까지의 점 경로와 두 임계값, 또는 불리언까지의 경로, 그리고 선택적으로 이유까지의 경로.
  판정기 파일에 코드는 없다.
- **읽기 전용.** 도구는 읽기 전용이어야 한다. 파일에 그렇게 적고, 허브가 쓰기 도구로 표시한 것은 검증이 거부한다. 변경은
  여전히 조치와 그 승인으로만 일어난다.
- **허용 역할.** 판정기는 맡을 수 있는 역할을 적는다(예: 검증되지 않은 스캐너는 `alarm`만).
- `jev`와 `command` 판정기는 자기만의 작은 형식이 있다(jev 질문 판과 임계값, 프로젝트 테스트 명령 이름).

## 흐름에서 쓰기

마감에 `verdict_sources`가 생긴다. `{source, role}`의 순서 있는 목록이다.

```json
"close": {"character": "jev_mcp_decision", "providers": ["claude"],
          "verdict_sources": [{"source": "jev", "role": "decide"},
                              {"source": "project-tests", "role": "gate"},
                              {"source": "jev-fundamental", "role": "alarm"}]}
```

비어 있거나 없으면 지금의 마감이다. 모르는 판정기, 판정기가 허용하지 않는 역할, 흐름의 프로젝트 종류를 빼는 판정기, 아직
그림자 상태인 판정기("그림자가 먼저" 참고)는 검증이 거부한다.

### 편집기

마감 패널은 허브의 판정기를 이름, 종류, 역할, 그림자 일치율과 함께 나열한다. 운영자가 하나를 더하고 역할을 고르고 순서를
정한다. 그래서 운영자에게 jev 고르기는 제공자를 고르는 것처럼 보이고, 흐름에는 역할을 가진 판정기로 저장된다. 캔버스의 마감
노드는 판정기를 순서와 역할대로 보여 주고, 보기 화면은 결과별로 칠한다.

## 그림자가 먼저

새 판정기는 모두 그림자로 시작한다. 지금의 LLM 판정자 옆에서 돌고 `verdict_source` 이벤트(`source`, `role: shadow`, 결과,
확률, 이유, 시간, 오류)를 남기며 아무것도 바꾸지 않는다. LLM 판정자와의 일치율, 그리고 `gate`와 `alarm`이라면 적중과 헛경보를
보고서로 본다(J1 보고서를 일반화). 운영자는 판정기 파일에 적힌 기준을 넘으면 그 판정기의 그림자를 푼다. jev가 그림자 상태의
첫 판정기다(J1, 2026-11-01부터 실행 중).

## 이벤트와 결과

- 판정기마다 라운드마다 `verdict_source` 이벤트 하나(역할, 결과, 시간)와 지금 모양의 `verdict` 이벤트(jev-verdict.md의 이벤트
  약속: 화면은 계속 이것을 읽는다).
- `final_decision.verdict_by`가 정한 판정기(`llm` 또는 판정기 id)를 적고, `final_decision.sources`가 모든 판정기의 결과를 담아
  결과 화면과 리포트는 이벤트 없이 읽는다.
- 울린 `alarm`은 `DISPUTED` jev 게이트처럼 자동 적용을 멈추고 어느 판정기가 울렸는지 말한다.

## 지금 있는 것과의 관계

| 지금 | 이 설계에서 |
|---|---|
| LLM 판정자 | `llm` 판정기, `decide` 단계의 마지막 |
| jev 판정, J1 그림자 | 그림자 상태의 `jev` 판정기. jev-verdict.md의 J3는 이것에 `decide` 역할을 주는 것 |
| jev 게이트(쓰인 결정문에 `jev_submit_decision`) | 그대로. 같은 사슬에 결정문에 대한 `gate`로 보여 준다. 이 층으로 옮기는 것은 선택이고 나중 |
| 판정자 도구(`jev_mcp:run_tests`, 패치) | 그대로: 판정이 아니라 작성자가 제안하는 조치 |
| 판정자의 저장소 MCP(`code_tools: "mcp"`) | 그대로: LLM이 코드를 읽게 한다. 판정기는 자기 종류로 작업 공간을 읽을 수 있다 |

## 단계

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| S1 | 판정기 파일, 등록, 검증, `--verdict-sources` 보고와 API. 첫 등록 판정기 `jev`. 등록된 모든 판정기의 그림자 이벤트 | J1 그림자가 `jev` 판정기로 돈다. 가짜 판정기로 테스트 |
| S2 | 엔진의 역할: LLM 대체가 있는 `decide` 사슬, `gate`, `alarm`. `verdict_by`, `sources`. 작성자 차례와 이의(jev-verdict.md J3) | 판정기 없으면 golden dump 변화 없음. 역할별 seeded 테스트 |
| S3 | `mcp` 종류(인자 자리표시자, 답 경로, 읽기 전용 검사) | 테스트의 가짜 MCP 서버, sandbox의 실제 읽기 전용 서버 하나 |
| S4 | `command` 종류(임시 사본에서 프로젝트 테스트 명령) | `run_tests`의 실행 정책 재사용, 테스트 |
| S5 | 대시보드: 마감 패널 목록, 캔버스, 보기 화면, 판정 흐름 칩, 모니터(jev-verdict.md의 UI 영향 표) | 대시보드 테스트, 역할마다 끝난 실행을 브라우저로 확인 |

jev-verdict.md의 J2(측정)는 S1 동안 계속된다. jev 판정기는 그 뒤에야 역할을 받는다.

## 하지 않는 것

- 결정문을 쓰거나 무엇이든 바꾸는 판정기.
- 흐름 파일에 적힌 임의 명령, 서버, 코드.
- LLM 작성자나 jev 게이트를 없애는 것.

## 열린 점 (검토용)

- jev 게이트를 나중에 이 층으로 옮길지, 옆에 둘지.
- 판정기별 비용 표시(호출, 초, 서비스가 알려 주면 금액).
- `gate` 실패가 이유를 다음 라운드로 넘기는 대신 스레드를 바로 끝내야 하는지.
- 그림자를 푸는 기준: 모든 판정기에 하나의 규칙인지, 역할별인지.

## 구현 메모

### S1 (2026-11-02, `c4a1ef6`으로 배포)

- 2026-11-02 배포. 허브의 `--verdict-sources`는 그림자 상태의 `jev`를 보여 주고(기존 `GUARDIAN_JEV_VERDICT=shadow`로
  켜짐), `GET /api/verdict-sources`는 200을 돌려준다. J2 집계에는 첫 기록이 있다(그날 아침 토론의 `jev_verdict` 이벤트, 일치).

- `cli/llm-agent/verdict_sources.py`: 검증, 두 저장소(`configs/verdicts/`, `<GUARDIAN_DATA_DIR>/verdicts/`; 기본 제공 경로는
  `GUARDIAN_VERDICT_SOURCES_DIR`로 바꿀 수 있음), `JevSource`, `run_shadow`, `collect`/`summarize`/`report`.
  판정기는 `configs/verdicts/jev.json` 하나뿐이다: 역할 `decide`, `alarm`, 세 프로젝트 종류 모두, 질문 2판, 0.8 / 0.2,
  기준 30건과 일치율 0.9(jev-verdict.md의 J2 기준).
- 설계에 없던 파일 키: `timeout_s`(1~600, 기본 60)와 `shadow_bar`(`min_verdicts`, `min_agreement`). S1은 기준을 넘었는지
  보고만 하고, 그림자를 푸는 것은 S2다. "모든 판정기에 하나의 기준인지, 역할별인지"라는 열린 점은 우선 판정기 파일마다 기준을
  두는 것으로 답했다.
- `mcp`와 `command` 파일은 실행할 수 없는 채로 저장하지 않고 거부한다("not built yet (phase S3/S4)").
- 스위치: `GUARDIAN_VERDICT_SOURCES=shadow`. 허브에 있는 J1 스위치 `GUARDIAN_JEV_VERDICT=shadow`도 같은 그림자 실행을 켜므로,
  배포할 때 `agent.env`를 바꿀 필요가 없다. `GUARDIAN_JEV_VERDICT_YES` / `_NO`는 여전히 jev 파일의 임계값보다 앞선다.
- 이벤트: 판정기마다 라운드마다 `verdict_source`(`source`, `kind`, `role: "shadow"`, `verdict`, `probability`, `reasons`,
  `latency_ms`, `error`, `llm`, `agree`와 종류별 필드, 자리 `close`). jev 판정기는 J1의 `jev_verdict` 이벤트도 그대로 쓰고,
  `verdict_disagreement`에는 `source`가 더해졌다(jev는 `jev`와 `p_approve` 필드를 유지). 보고서는 jev의 `verdict_source`가
  없는 작업에서만 `jev_verdict`를 세므로, J2 집계는 끊기지도 두 번 세지도 않는다. `timeout_s`를 넘긴 판정기는 uncertain으로
  기록한다. jev의 요청 시간 제한이 `timeout_s`이고, 따로 감시하는 스레드는 없다.
- 엔진은 판정기에 `{project, kind, commit, workspace}`를 맥락으로 넘긴다. jev는 쓰지 않는다.
- `guardian.py --verdict-sources`(파일이 틀리면 종료 코드 2), 읽기 전용 `GET /api/verdict-sources`, `scripts/ci.sh config`
  검사(`reviewed`가 오래되면 경고). `--jev-verdict-report`는 그대로다.
- 흐름 필드는 아직 없다: `close.verdict_sources`는 S2에서 들어온다. 모든 판정기가 그림자 상태라 검증이 어차피 거부한다.
  프롬프트 변화 없음, golden dump 변화 없음.
- 테스트: `test_verdict_sources.py`(검증, 가짜 판정기, 판정기를 주입한 엔진 실행, 두 이벤트 모양에 대한 보고서).
  `test_jev_verdict.py`는 가짜의 `limits` 인자 외에는 그대로 돈다.

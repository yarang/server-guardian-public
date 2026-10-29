# 설계·문서·이미지 산출물 프로젝트 (`kind: "design"`) 검토

[English](design-project.md) | 한국어

상태: 검토 결과(2026-10-17). 결정 1~7은 권장안으로 확정되었고 P1이 구현되었다(배포 전, 아래 "구현 메모 (P1)"). 결정 8, 10, 11, 12는 2026-10-18에 확정되었고, 결정 9는 전제가 틀려서 같은 날 다시 열렸다("결정 기록"). P2와 결정 12의 0~1단계가 구현되었다(배포 전, "구현 메모 (P2)"). P3도 구현되었다(배포 전, "구현 메모 (P3)").
방법: 검토 팀 5명(엔진 적합성, 검증·품질, 저장·열람·다운로드, 보안·반대 논거, 능력 조사)이 각자 코드와 문서를 읽고 보고서를 냈다.
이 문서는 그 종합이다. 보고서의 출처가 웹 문서인 항목은 "미확인"으로 표시했고, 코드 사실 일부(`mcp_client.py:148`, `--tools ""`,
`ops_mcp.py` 부재, CSP 적용 범위)는 종합자가 직접 다시 확인했다.

## 질문과 결론

질문: Claude의 최신 프롬프트 기법으로 이 CLI를 코딩 전용이 아니라 설계, 문서, 이미지 같은 산출물을 만드는 프로젝트로 쓸 수 있는가?
그 결과물을 사용자가 어떻게 확인하고 내려받는가?

**결론: 조건부로 가능하다. 범위를 나눠서 답한다.**

| 산출물 | 판정 | 조건 |
|---|---|---|
| Markdown 문서, 설계서 | 가능 | 지금도 `code` 프로젝트의 `apply_patch`로 `docs/`에 쓸 수 있다. 새 kind의 이득은 저장소 없는 프로젝트와 버전 관리다 |
| Mermaid, SVG 다이어그램 | 가능 | 응답 텍스트에서 엔진이 추출한다. 대시보드에 렌더 경로가 이미 있다(`MarkdownDoc.tsx`) |
| 단일 HTML/CSS 시안 | 조건부 | 보안 격리(아래 "보안")가 선행 조건. 서버 렌더 없이 사용자 브라우저에서 본다 |
| PNG, PDF | 조건부, 후순위 | 서버 렌더러 없이 **사용자 브라우저**에서 변환(SVG→canvas→PNG, 인쇄→PDF)하는 안은 **종합자 제안이며 검토 팀이 확인하지 않았다(미검증)**. 서버 렌더는 결정 필요 |
| DOCX, PPTX, XLSX | 조건부, 후순위 | 표준 라이브러리만으로는 직접 OOXML을 써야 한다. 또는 외부 라이브러리·Skill이 필요하다(결정 필요) |
| 래스터 이미지 직접 생성 | **Claude로는 불가** | Claude는 이미지를 이해할 뿐 생성하지 못한다(공식 FAQ, 능력 조사). codex의 `image_generation`은 2차 자료뿐이라 미확인이고 현재 저장소는 꺼 둔다. 이미지는 "코드로 그린 SVG" 또는 외부 이미지 모델이어야 한다 |

"프롬프트 기법만으로" 되는 것은 텍스트 계열(Markdown, Mermaid, SVG, HTML)까지다. 그 너머는 렌더러, 라이브러리, 외부 모델 같은 **실행 환경 결정**이
필요하고, 그 결정은 CLAUDE.md의 확정 결정(표준 라이브러리 전용, 큐·워커 없음, 허브 하나)과 맞닿아 있다.

## 현재 구조에서 재사용되는 것과 막히는 것

- 재사용: Expert, Critic 패널, Judge 구조, 이벤트(`events.jsonl`), 세션, 팀·페르소나(`configs/personas/*.json`), jev 게이트
  (`code_context=None`이면 코드 줄을 생략). 토론 엔진 본체는 코드에 결합되어 있지 않다.
- 막힘 1, git 전제: workspace 잠금, base SHA, publish, sync, wiki, reset. `kind: "ops"`가 이미 `repo`와 `workspace` 없는 프로젝트를 만들었으므로
  `_parse_ops`(`project_registry.py:183-225`)가 틀이 된다.
- 막힘 2, 선례의 한계: ops 분기는 `is_ops` 불리언이다. `mcp_client.py:148`의 `(name in SERVER_TOOLS) == (self.kind == "ops")`는 kind가 둘일 때만
  맞는 식이어서, `design`을 더하면 코드 도구(`apply_patch` 등)가 그대로 열린다. `kind → 도구 집합` 표(`KIND_TOOLS`)로 바꿔야 한다.
  또 `Project.is_ops`가 "워크스페이스 없음"의 뜻으로도 쓰이므로 `has_workspace`로 분리한다.
- 막힘 3, ops의 나머지는 미구현: `ops_mcp.py`와 `OPS_TOOLS`는 설계서에만 있다(코드에는 `SERVER_TOOLS` 하나). "ops 선례를 따른다"는 말은 P1(kind 검증)까지다.
- 막힘 4, 에이전트는 파일을 만들지 못한다: claude는 `--tools ""`로 실행된다(`llm_runner.py:357`, `:545`). 에이전트는 렌더 결과를 볼 수도 없다.
  산출물은 응답 텍스트로 받고 엔진이 쓴다. 이 구조는 보안상 장점이므로 1차에서 바꾸지 않는다.
- 막힘 5, 검증: `evidence.py`의 인용 검증은 "이 줄에 이 인용문이 있는가"만 증명한다. 코드에 대한 사실 주장에는 그대로 쓸 수 있지만, 설계가 좋은지는
  증명하지 못한다. jev 게이트도 제안·비평·액션 요약만 4,500자 이내로 본다. 평가 1에서 같은 12문항의 jev PASSED가 7에서 10으로 바뀌었으므로 품질 지표로 쓰지 않는다.

## 결정 (사용자 확인 필요, 권장 기본안)

| # | 항목 | 권장 기본안 | 다른 선택지 |
|---|---|---|---|
| 1 | 범위(1차) | **Markdown, Mermaid, SVG**. 기존 렌더 경로를 재사용한다 | HTML/PDF/PNG까지(격리 설계 선행), DOCX/PPTX까지 |
| 2 | kind 구조 | `kind: "design"` 하나, 종류는 `artifacts.types` 허용 목록 | 종류별 kind. `offers`류 분기가 곱절이 된다 |
| 3 | 렌더 위치 | **사용자 브라우저**. 허브는 렌더하지 않는다 | 허브에 rsvg/Chromium 설치(자원·SSRF 위험, `systemd-run` 한도 필요), 별도 호스트("허브 하나" 결정 변경) |
| 4 | 이미지 | 1차는 **코드로 그린 SVG**만. PNG는 브라우저 변환을 P4에서 시험한다(종합자 제안, 미검증: 변환 가능 여부와 아래 PDF 제약을 P4에서 확인) | 외부 이미지 모델 연결(자격 증명, 비용, 약관), codex `image_generation` 켜기 |
| 5 | 쓰기와 승인 | 새 도구 `write_artifact`, **기본 수동 승인**. `FILE_TOOLS`·`WRITE_TOOLS`에 넣지 않는다 | jev 통과 시 자동 적용(`GUARDIAN_AUTO_APPLY_ARTIFACTS`, 기본 끔, 평가 뒤 재검토) |
| 6 | 저장 | `<DATA>/design-artifacts/<project>/<thread>/vN/`, 불변 버전, `manifest.json`, **백업 포함** | 작업 클론(git)에 저장하고 publish. 이미지가 저장소를 부풀린다 |
| 7 | 에이전트 쓰기 권한 | **주지 않는다**(`--tools ""` 유지). 엔진이 응답에서 추출해 쓴다 | 산출물 모드로 임시 디렉터리 쓰기 허용(`acceptEdits`), 확장자·크기·경로 검사 필요 |
| 8 | 러너 | 산출물 생성은 **claude 전용** | codex/agy 허용. 둘은 `agent.env`·`llm.env`를 읽을 수 있는 기존 구멍이 있다(`env_policy.py`가 명시) |
| 9 | 시작 시점 | **평가 2 재실행 후** (claude CLI 실패 원인을 이해한 다음) | 바로 P1 시작. P1은 retrieval을 쓰지 않으므로 평가 2와 독립이지만, 폴백 신뢰성은 공통이다 |
| 10 | 위치 | server-guardian에 `kind`로 추가 | **0. 아무것도 만들지 않기**: 문서·Mermaid는 지금도 `code` 프로젝트의 `apply_patch`로 `docs/`에 쓸 수 있다(`JUDGE_DIAGRAM_HINT`가 mermaid를 허용). 새 kind의 이득은 저장소 없는 프로젝트, 버전 관리, 다운로드 화면이다. 그 이득이 필요 없으면 가장 싼 선택이다. 그 밖에 별도 프로젝트로 분리, 또는 Claude 앱 Artifacts/Docs 사용(격리·렌더가 이미 해결됨) |
| 11 | 버전 보관 | 스레드의 최신 버전만 기한 없이 보관하고, 그보다 오래된 버전은 `artifacts.retention_days`(기본 30, 프로젝트별 설정, 0 = 보관) 뒤 삭제한다. 총량 한도 없음, 파일당 안전장치 하나 | 모든 버전 영구 보관(텍스트는 싸지만 P4의 바이너리는 다르다), 승인된 버전 영구 보관 |
| 12 | 스레드 토큰 정리 | 이전 라운드 정리 단계. 프로그램(결정론적)으로 먼저 하고, 그래도 크면 경량 LLM 요약을 선택 기능으로 | LLM 요약을 먼저(사용량 소모, 캐시 안정성 저하), 정리 없음 |

### 결정 기록 (2026-10-18)

결정 8~10을 상황 요약과 함께 JEV(`jev_ask`, 모델 jev-1.13.0)에 물었다. JEV는 작성자가 쓴 틀을 점수화하므로 사실 검증이 아니라 참고 신호다.

| # | 결과 | 확률 | 등급 | 상태 |
|---|---|---|---|---|
| 8 | claude 전용 (감사 모드도 같은 `agent.env`/`llm.env` 이유로 Expert를 제한했다) | 1.00 | act | **확정** |
| 9 | ~~평가 2를 재실행한 뒤 P2 시작~~ (틀린 전제로 질문함, 아래 정정 참조) | 0.95 | act | **재개방** |
| 10 | server-guardian의 `kind: "design"`으로 계속 (대안: P1에서 멈추고 `code` 프로젝트 사용 0.09, Claude 앱 Artifacts 0.08) | 0.81 | review | **확정**: review 등급 결과를 보고 사용자가 A안을 확인했다 |

결정 11과 12는 2026-10-18에 사용자가 확정했으며 JEV에 묻지 않았다.

| # | 결과 | 상태 |
|---|---|---|
| 11 | 스레드의 최신이 아닌 버전은 30일 뒤, 또는 프로젝트의 `artifacts.retention_days` 뒤 삭제한다. 최신 버전은 삭제하지 않는다. 삭제된 버전은 `manifest.json` 항목(sha256, 크기, 삭제일)을 묘비로 남겨서, 이전의 `[artifact:이름@v3 §제목]` 인용이 "찾을 수 없음"이 아니라 "만료됨"으로 읽히게 한다. 크기 총량 한도는 없다. L1에 안전장치 하나만 둔다: 2 MiB를 넘는 텍스트 파일은 검증 실패이며 사유가 승인 화면에 보인다. 정리는 새 타이머가 아니라 기존 야간 작업에서 실행한다 | **확정**(사용자). 결정 9의 보관 정책 조건이 충족되었다 |
| 12 | 스레드에 토큰 낭비를 막는 정리 단계를 둔다. 순서: (0) 측정, (1) 결정론적 압축, (2) 선택적 경량 LLM 요약(기본 꺼짐) | **확정**(사용자). 디자인 전용이 아니라 엔진 전반이며 별도 작업으로 구현한다 |

결정 12 상세(2026-10-18에 확인한 코드 사실):

- `_format_history`(`debate_engine.py:730`)는 이전 라운드를 전문 그대로 잇는다. 이것은 새 CLI 세션을 시작하는 에이전트에게만 전송된다(`_compose`의 `new_session`). 즉 스레드를 이어갈 때 세션을 잃었거나 폴백으로 공급자가 바뀐 경우다. 재개된 세션은 이미 내용을 갖고 있어서, 정리는 이런 경우에만 이득이 있다.
- 검색 결과는 토론당 약 90~105 KB(평가 2 `metrics.jsonl`, 약 20 steps)이고 세션 안에 남는다. 정리 단계는 이를 건드리지 않는다.
- 지금은 토큰을 측정하지 않는다. `metrics.jsonl`에는 바이트만 있고 첫 턴의 `prompt_bytes`가 이벤트로 나간다. 0단계에서 턴별 `history_bytes`를 추가하고 임계값은 그 데이터로 정한다. 기록의 비중이 작으면 1단계는 만들 가치가 없을 수 있다.
- 1단계는 표준 라이브러리 모듈(`thread_digest.py`, `DIGEST_VERSION`, 프롬프트 버전 올림)이다: 최신 1라운드는 전문, 이전 라운드는 Judge 판정, 결정 항목, `findings`, Critic 차단 사유, 제안의 첫 제목이나 문단만 남기고, 긴 코드 펜스는 `[코드 생략: path:lines]`로 바꾸며, 제안의 반복 인용은 제거한다. 같은 입력은 같은 출력을 낸다(프롬프트 캐시 앞부분 안정, 정답 기록 테스트, 사용 한도 영향 없음). 압축본은 탐색용일 뿐 증거가 아니다. 그 줄이 세션에 보인 경우에만 인용이 통과한다.
- 2단계는 1단계 뒤에도 기록이 임계값(예: 24 KiB)을 넘을 때만 한 번 요약해 스레드에 저장한다. 새 세션이 3개 이상(Expert, Critic 패널, Judge)일 때만 이득이다. `llm_runner.py`가 더 가벼운 모델을 고를 수 있는지는 확인하지 못했으며 먼저 확인해야 한다.

**정정 (2026-10-18).** 결정 9에서 JEV에 준 전제가 틀렸다. "135회 중 107회가 내장 답으로 대체됨"은 평가 2의 첫 실행(2026-10-12)에만 해당하며, 그 평가의 `plan.md`가 이미 무효로 선언했다. 2026-10-16~17 재실행은 폴백 0건으로 135회를 채웠고(6회는 폐기하고 30분 뒤 반복, 원인은 claude 사용 한도) 결과는 2026-10-17에 커밋되었다(`0b8588c`). 즉 "평가 2가 다시 유효하다"는 조건은 이 문서를 쓸 때 이미 충족되어 있었고 다시 돌릴 것이 없다. 정정한 사실로 JEV에 다시 묻자 "보관 정책과 한도를 정한 뒤 P2 시작"(0.83, review 등급)을 골랐고 재실행은 불필요하다고 보았다(yes 0.13). 남는 신뢰성 문제는 사용 한도로 인한 간헐적 폴백(약 135회 중 6회)이며, P2의 "폴백과 속도 제한은 반복으로 세지 않고 `deferred`로 멈춘다" 규칙이 이를 다룬다. 새 결정 9는 사용자가 확인해야 한다.

JEV는 허브에서 실제 모델로 돌려 보기 전에 P1을 감사 A1/A2와 함께 배포하는 것도 맞지 않을 가능성이 높다고 보았다(yes 0.23).

## 설계 요약

### 데이터 모델

```json
{
  "id": "arch-docs", "kind": "design", "name": "...", "description": "...",
  "artifacts": {"types": ["markdown", "mermaid", "svg"], "max_bytes": 262144, "reference_repo": null},
  "review": {"rubric": ["일관성", "누락된 요구", "다이어그램 정확성"]},
  "policy": {"allowed_actions": ["jev_mcp:write_artifact"]}
}
```

- `kind`는 `code | ops | design`. `repo`, `workspace`, `tests`, `context`, `wiki`, `targets`는 design에서 거부하고, code는 `artifacts`를 거부한다.
- `artifacts.types`는 비어 있으면 안 되고 `ARTIFACT_TYPES`의 부분집합이다. `png`, `jpeg`, `html`은 결정 3·4와 보안 설계가 끝나기 전(P4 이후)에는 거부한다.
- `required_sections`(선택, 기본은 비어 있음)는 Markdown 산출물 이름마다 들어 있어야 할 제목을 적는다. 예: `{"decisions.md": ["Context", "Decision", "Consequences"]}`. 빠진 제목은 L2 오류다(심어 둔 결함 말뭉치 참고).
- `reference_repo`는 지금 `null`만 허용하는 예약 필드다(ops의 `runbook`과 같은 방식). 판정자 도구 = (팀 ∩ 페르소나) ∩ kind ∩ `policy.allowed_actions`.

### 도구와 팀

- `TOOLS`에 `jev_mcp:write_artifact`(risk medium)를 추가한다. 렌더 도구는 결정 3이 정해진 뒤에 추가한다.
- 구현은 신규 `artifact_ops.py`(버전 저장소, 종류별 검증기, 잠금 `artifacts/<project>/.lock`). git 개념은 `base_version`(`artifact@v3`)으로 대체한다.
- 페르소나가 코드 편향("수석 개발자", "시니어 코드 리뷰어")이므로 `design_expert`, `design_critic`(rubric 기반), `design_judge`와 팀 `design-review`를 새로 만든다.
  Expert 프롬프트는 거의 일반적이고, 바꿀 곳은 Judge 규칙(`JUDGE_PATCH_RULES`의 file:line, "checks in the code")이다. judge profile로 분리하고
  `RETRIEVAL_PROMPT_VERSION`을 올린다. code 프롬프트는 바이트 동일하게 유지하는 테스트를 둔다.
- 프롬프트 기법의 대응(능력 조사): 명세 우선(Expert가 목적·독자·구성·제약을 쓴 뒤 산출, Critic은 그 명세를 기준으로 검토), 구조화 출력(심판의 JSON 계약,
  `max_tokens`로 끝난 응답은 실패 처리), 디자인 비평·판결의 effort 상향(캐시 무효화 비용 있음), 렌더-비평 루프(아래 검증 L4, 후순위).

### 최신 프롬프트 기법의 자리 (능력 조사 보고, 원문 재확인 전)

| 기법 | 꽂히는 곳 | 비고 |
|---|---|---|
| 명세 우선 | Expert가 목적·독자·구성·제약을 쓴 뒤 산출. Critic은 산출물이 아니라 이 명세를 기준으로 검토 | 긴 입력은 앞, 질문은 끝. 현재의 정적 앞/동적 뒤 배치와 맞는다 |
| 구조화 출력 | Judge의 JSON 계약 | 사고가 줄어 정확도가 떨어질 수 있어 "답하기 전에 충분히 생각하라" 문장 또는 effort 상향. `max_tokens`로 끝난 응답은 JSON이 유효해도 실패로 보고 재시도 |
| effort 상향 | 디자인 비평·판결 | 현재 `--effort medium`. 바꾸면 프롬프트 캐시가 무효화된다. `xhigh` 이상은 "끝나면 멈추고 보고하라" 필요 |
| 문서·디자인 Skill | 팀 지침 | 커스텀 Skill은 `.claude/skills/`에서 `-p`로도 로드된다. 허브의 문서 Skill 동기화는 미확인이라 기대지 않는다 |
| 역할 분리 | 기존 Expert/Critic/Judge가 곧 이 구조 | `--agents` 동적 서브에이전트는 불필요 |
| 렌더-비평 루프(비전) | Critic (후순위, P5) | 저장소에 없는 새 기능. 이미지 전달 경로와 렌더러가 선행 조건 |

### 검증 층 (표준 라이브러리 범위)

| 층 | 검사 | 가능 여부 |
|---|---|---|
| L1 구조·보안 | SVG `xml.etree` 파싱(DOCTYPE/ENTITY 사전 거부), script, foreignObject, `on*`, 외부 href 금지, 크기 상한, JSON, 마크다운 펜스 | 가능 |
| L2 참조·접근성 | 상대 링크, id 중복, `alt`, 제목 순서, mermaid 노드 정의/참조, 16진수 색 대비(WCAG 근사), 필수 섹션, 용어 | 가능. 대비·용어는 경고만 |
| L3 렌더 성공·mermaid 문법 | 실제 파서·렌더러 | 불가. 외부 바이너리 필요(결정 3) |
| L4 시각 비평 | Critic이 렌더 이미지를 본다 | 불가. 이미지 전달 경로가 없다(러너는 텍스트 stdin). MCP 이미지 결과를 claude CLI가 받는지 미끼 실험 필요 |

원칙: 결정론적 검사가 막고, Critic은 조언하며, 사람이 마지막에 승인한다. L1 실패와 보안 실패는 엔진이 비승인으로 강제하며 Judge가 면제할 수 없다.
코드에 대한 사실 주장은 기존 `findings`와 `check_findings`로 검증하고, 설계 의도는 "검증 불가"로 기록한다. 증거 표기는 `[artifact:<name>@v<n> §제목]`이고
"보여준 버전만 인정" 원칙을 따른다.

개선 루프: 최초 생성 1회 + 수정 최대 2회. 조기 종료는 L1·L2 통과와 Critic 차단 0, 두 번 연속 개선 없음, 예산 소진.
폴백 응답과 속도 제한은 반복으로 세지 않고 `deferred`로 멈춘다. 반복당 2~5분은 코드 토론의 실측(약 283초)에서 **추정**한 값이다.

### 자동 적용과의 관계

`is_auto_approvable`은 도구 이름만 본다. 산출물 kind가 `apply_patch`나 `execute_code_action`을 재사용하면 `auto:engine`이 사람 없이 쓴다.
그래서 (1) `write_artifact`는 `FILE_TOOLS` 밖에 둔다, (2) `is_auto_approvable`에 kind 검사를 넣는다, (3) 버전 디렉터리에만 쓰고 덮어쓰지 않는다,
(4) 테스트로 고정한다: jev PASSED와 `GUARDIAN_AUTO_APPLY`가 켜져도 design에서 `auto_run_actions`는 빈 목록을 돌려준다(감사 모드의 "액션 행 없음" 테스트와 같은 형태).

## 확인과 다운로드

### 저장

`<DATA>/design-artifacts/<project>/<thread>/vN/` 에 임시 디렉터리에서 완성한 뒤 rename한다(`code_wiki.py` 패턴). `manifest.json`은 `os.replace`.
파일 항목은 `{path, sha256, bytes, mime, kind, stage, agent, title, preview_of}`. 파일명은 `^[a-z0-9][a-z0-9._-]{0,80}$`, 하위 디렉터리 없음,
`manifest.files`에 있는 파일만 서빙한다. MIME은 매니페스트가 아니라 Go의 확장자 허용 목록으로 정한다.
한도(결정 11): 총량 한도는 없다. 검사는 L1의 텍스트 파일당 2 MiB 안전 한도(`GUARDIAN_ARTIFACT_MAX_FILE_BYTES`) 하나뿐이고 검증 실패로 보고한다. 바이너리 한도는 P4와 함께 정한다. 스레드의 최신이 아닌 버전은 `artifacts.retention_days`(기본 30, 0 = 보관) 뒤 정리하고, 최신 버전은 정리하지 않는다.
백업은 포함한다(`db_backup.py`의 `_is_excluded`는 새 디렉터리를 자동 포함한다). 파생 미리보기는 `.cache/`에 두고 제외 규칙을 추가한다.

### 열람

| 형식 | 방식 |
|---|---|
| md, mermaid(.mmd), svg | 기존 `MarkdownDoc`, `sanitizeSvg` 재사용. **LLM이 직접 쓴 SVG는 인라인으로 넣지 않고 `<img>`로만** 표시한다 |
| png (P4 이후) | `<img>` (같은 출처 URL, 쿠키 인증, CSP `'self'`로 충분) |
| pdf (P4 이후) | 같은 출처 `<iframe>`. 아래 보안 절의 `CSP: sandbox` 헤더를 모든 산출물에 걸면 Chrome 내장 PDF 뷰어가 샌드박스 문서를 거부하는 것으로 알려져 있다(종합자 메모, 미검증). 헤더를 html/svg에만 걸거나 P4에서 "sandbox 헤더 아래 PDF 미리보기"를 확인 항목으로 둔다 |
| html | 기본은 정제 뷰. 원본은 `sandbox=""` iframe(스크립트 없음). 결정 1에서 HTML을 제외하면 P4 |
| pptx, docx | 미리보기 없음. `preview_of`로 연결된 PDF/PNG가 있으면 그것을 보여 주고, 없으면 파일 카드와 다운로드만 |

UI는 위키 탭과 같은 수준의 "산출물" 탭과 토론 결과의 `ReportButton` 옆 진입점. 목록 → 버전 선택 → 파일 카드(형식 칩, 크기, 단계) → 상세.
한국어 문구는 `ko/artifacts.ts`로 분리하고, 모바일은 `useNarrow`로 1열(iframe 100% 폭 70vh, 다운로드 버튼 44px).

### 다운로드와 인증

- 허브 대시보드(같은 출처)는 HttpOnly 세션 쿠키(`guardian_session`, SameSite=Strict)로 인증된다. 쿠키는 `Authorization` 없이도 통과하므로
  (`auth.go` `authorized`) `<a href="…/files/<name>?download=1">`이 헤더 없이 동작한다. blob 변환이 필요 없고 큰 파일도 스트리밍된다.
  종합자가 코드로 확인한 부분: `withSecurity`의 `crossSiteWithoutOrigin`(`security.go:160`)은 `Sec-Fetch-Site`가 `cross-site`·`same-site`일 때만 거부한다.
  대시보드 안의 링크 클릭(`same-origin`)과 주소창·새 탭 직접 열기(`none`)는 통과하고, 쿠키가 `SameSite=Strict`라서 다른 사이트에서 건 링크에는 쿠키가 붙지 않아 401이 된다.
  브라우저에서 실제로 내려받아 보는 시험은 아직 하지 않았다(P3 끝 조건).
- `?download=1`로 attachment를 강제하면 새 탭에서 직접 열어도 실행되지 않고 저장만 된다. 이것이 HTML 산출물의 기본 안전 경로다.
- Tauri와 교차 출처만 `authFetch`(Bearer)로 받아 저장한다. 기존 `save_download`는 문자열 전용에 `.md`만 허용하고 16 MiB 상한이라
  바이너리용 `save_download_bytes`가 필요하다(Rust 변경, 결정 필요).
- 응답 헤더: `Content-Disposition`은 `mime.FormatMediaType("attachment", …)`로 UTF-8 파일명 처리, `ETag`는 sha256, `nosniff`, `Cache-Control: private, no-cache`.
  `http.ServeContent`가 Range와 If-None-Match를 처리한다. 전체 번들은 `archive/zip` 스트림에 `manifest.json` 포함, 이미 압축된 형식은 Store.
- `?access_token=` 쿼리 경로(`streamPaths`)에는 산출물 라우트를 넣지 않는다(URL에 토큰이 남는다). 조회·다운로드는 `recordAudit`에 이름만 기록한다.

| 경로(GET) | 응답 |
|---|---|
| `/api/projects/{id}/artifacts` | 스레드별 최신 버전 요약 |
| `/api/threads/{thread}/artifacts` | 버전 목록 |
| `/api/threads/{thread}/artifacts/v{n}` | 매니페스트 |
| `/api/threads/{thread}/artifacts/v{n}/files/{name}` | 파일 바이트, `?download=1`이면 attachment |
| `/api/threads/{thread}/artifacts/v{n}/bundle.zip` | zip 스트림 |

CLI를 거치지 않고 디스크를 직접 읽는 읽기 전용 경로다(`debate_report.go`와 같은 `openRegular` O_NOFOLLOW + 크기 상한). 이벤트는 메타데이터만 낸다:
`artifact_created {version, path, kind, mime, bytes, sha256, stage}`, `artifact_version_finished`, `artifact_skipped {path, reason}`, `artifact_iteration`.

## 보안 (차단 항목)

LLM이 만든 HTML/SVG를 대시보드와 같은 출처에서 스크립트가 실행되는 상태로 서빙하면 저장형 XSS다. 스크립트가 `fetch('/api/exec', {credentials:'include'})`를
보내면 `withSecurity`(same-origin)와 `withAuth`(쿠키)를 모두 통과해 허브 사용자 권한의 셸이 된다. `dashboardCSP`는 `ui.go`에서 UI 응답에만 붙고
(`setUISecurityHeaders`), `/api` 아래에는 `nosniff`만 붙으므로 산출물 라우트에는 CSP가 없다. 최소 안전 설계:

1. 산출물 응답에 고정 헤더: `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'`,
   `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. Content-Type은 확장자 표로 고정한다.
2. 화면에서는 SVG를 `<img>`로, HTML을 `<iframe sandbox="">`로 보인다. `allow-scripts`는 주지 않고 `allow-same-origin`은 절대 주지 않는다.
   헤더의 `sandbox`는 URL을 새 탭에서 직접 열 때를 막는다. 둘을 함께 쓴다.
3. 서버 측 정화는 보안 경계로 쓰지 않는다. 별도 포트는 쿠키가 포트로 구분되지 않아 이득이 없다.
4. `blob:` iframe과 `srcdoc`은 sandbox 없이 쓰면 대시보드 출처를 상속한다. CSP에 `blob:`을 추가하지 않고 쿠키 기반 같은 출처 URL을 쓴다.
5. `sanitizeSvg`는 `ADD_TAGS: ['foreignObject']`를 켠다. mermaid가 만든 SVG에만 허용하고 LLM이 직접 쓴 SVG에는 쓰지 않는다.
6. 저장 전에 알려진 토큰 패턴(AGENT_TOKEN 값 등)을 스캔한다. `is_secret`은 경로 이름만 본다. 압축 파일은 받지도 풀지도 않아 zip slip이 없다.

허브 렌더(결정 3의 C안)는 권하지 않는다. 헤드리스 Chromium은 한 번에 300~800 MB(추정)를 쓰고 2 OCPU에서 Gitea와 경쟁하며, 렌더 대상 HTML이
`file://~/.guardian/agent.env`나 OCI 메타데이터, `127.0.0.1:9870`에 닿을 수 있다. 큐가 없어 동시 실행 제한도 마땅치 않다.

## 단계

| 단계 | 내용 | 끝 조건 |
|---|---|---|
| D0 | 위 결정 확정, 허브 claude 버전 확인(아래) | 사용자 기록 |
| P1 | `design` kind 검증, `has_workspace`, `KIND_TOOLS`, judge profile, 페르소나·팀, git 기능(sync, publish, wiki, reset) 거부, 산출물 텍스트를 task에 붙인 읽기 전용 비평 | design 토론이 기록되고 code context가 붙지 않는다. code 프롬프트 바이트 동일. `ci.sh python` 통과 |
| P2 | `artifact_ops.py`, `write_artifact`, L1·L2 검사기, 버전 저장소, `base_version` stale 검사, 수동 승인 화면 | 제안→승인→새 버전 기록. 자동 적용 0건 테스트. 씨앗 결함 재현율이 D0에서 정한 목표 이상 |
| P3 | Go 읽기 엔드포인트(목록, 파일, zip), 보안 헤더, 감사, 백업 규칙, 대시보드 "산출물" 탭(md, svg, mermaid), 이벤트 | 쿠키 없이 401, Bearer로 200과 Range 206, 경로 이탈·심볼릭 링크 404, 내려받은 파일의 sha256이 매니페스트와 일치, `ci.sh web`·`go` 통과 |
| P4 (조건부) | HTML 격리 뷰, png/pdf 형식 허용과 미리보기, 브라우저 PNG/PDF 변환(미검증 제안), Tauri `save_download_bytes`, 텍스트 개선 루프 | 보안 헤더 테스트, sandbox 헤더 아래 PDF 미리보기와 `print()` 가능 여부 확인(`sandbox=""` iframe 안의 내용은 부모가 인쇄할 수 없으므로 인쇄 PDF는 대시보드가 직접 렌더하는 Markdown·mermaid에만 해당할 수 있다), 평가 A에서 `single` 대비 이득 |
| P5 (조건부) | 서버 렌더, 시각 비평, DOCX/PPTX, 이미지 모델 | 결정 3·4가 바뀐 뒤에만 |

평가: 결정론 층은 정상 산출물 10개에 결함 30개를 심고(깨진 SVG, 낮은 대비, 끊긴 링크, 필수 섹션 누락, 코드에 대한 거짓 주장) 재현율·정밀도를 잰다.
품질 층은 `single`, `loop-det`, `loop-text`, `loop-vision`과 같은 예산의 best-of-N 대조군을 두고, 질문 15개 × 3회, 라벨을 가린 채점(사람 재채점 20%)을 한다.
평가 2의 규칙(폴백이 있는 실행은 `.fallback-<k>`로 옮겨 다시 돌림, 분모는 유효한 실행만)을 그대로 쓴다.

### 구현 메모 (P1)

2026-10-17 구현. 사용자가 결정 1~7을 권장안으로 확정하고 P1 시작을 지시했다. 설계와 다른 점은 다음과 같다.

- **시작 시점**: 결정 9의 "평가 2 재실행 뒤"를 따르지 않고 사용자 지시로 먼저 시작했다. 평가 2는 실제로는 2026-10-16~17에 이미 유효하게 재실행되었다(결정 기록의 정정 참조).
- **`write_artifact`는 P1에 없다**: `TOOLS`에 넣지 않았다. 그래서 `policy.allowed_actions`는 비어 있어야 하며, 위 예시처럼 `["jev_mcp:write_artifact"]`를 주면 거부한다(P2에서 도구와 함께 허용). `KIND_TOOLS["design"]`(`DESIGN_TOOLS`)는 빈 집합이다.
- **`KIND_TOOLS`**: `mcp_client.py`의 표가 도구 목록을 kind별로 나눈다(ops = `SERVER_TOOLS`, design = `DESIGN_TOOLS`, code = 나머지 전부). 프로젝트가 없으면(kind `None`) 전부 제공하는 기존 동작을 유지한다. 판정자 도구가 비어 있으므로 design 토론의 액션 목록은 항상 비고, 에이전트가 액션을 내놓아도 INVALID로 기록된다(`validate` 메시지는 바꾸지 않아 "belongs to code projects" 문구가 나온다).
- **`has_workspace`**: `is_ops`와 따로 두었다. `NO_WORKSPACE_KINDS`(ops, design)에는 `clone_path`가 `NoWorkspace`를 일으키고, sync·publish·wiki·`sync_projects("all")`는 이 속성으로 거부하거나 건너뛴다. ops 대상 요약(`config_ops`)은 `is_ops` 그대로다.
- **자동 적용 허용 목록**: `action_ops.AUTO_APPLY_KINDS = (None, "code", "ops")`. 검사는 액션이 속한 프로젝트의 클라이언트(`_client(mcp, action)`) 기준이라 `Workspaces` 리졸버를 쓰는 재검사 경로(`--jev-recheck`)에서도 design은 건너뛴다(보안 검토 지적: 리졸버에는 `kind`가 없어 처음 구현은 이 경로에서 통과했다). 토론 엔진도 `project.is_design`이면 `_auto_apply`를 부르지 않는다. 금지 목록이 아니라 허용 목록이라 이후에 kind가 늘어도 기본이 꺼짐이다.
- **산출물 형식**: `artifacts.types`는 `markdown`, `mermaid`, `svg`만 받는다. `html`, `png`, `jpeg`, `pdf`는 "later phase" 오류로 거부한다. `artifacts.max_bytes`는 1..1 MiB로 검증만 하고 P1에서는 강제하지 않는다(강제는 `write_artifact`와 함께 P2). 지키지 않는 한도를 에이전트에게 알리지 않도록 프롬프트에서는 뺐다. 실제 상한은 Go 핸들러의 task 길이 제한이다.
- **검토 기준(`review.rubric`)**: 기본값은 영어 세 항목(`consistency`, `missing requirements`, `diagram accuracy`)이고 최대 10개, 항목은 한 줄(출력 가능한 문자)만 받는다. 프롬프트는 `design_prompt_text`가 task 끝에 붙인다.
- **토론 경로**: design 프로젝트는 `_setup_retrieval(None, None, ..., repository=False)`로 검색을 끄고, 팀의 코드 컨텍스트 설정은 경고와 함께 무시한다. `context_paths`는 거부한다. 팀을 주지 않고 스레드에도 저장된 팀이 없으면 `design-review`(페르소나 `design_expert`, `design_critic`, `design_judge`, `allowed_tools: []`)를 쓴다. 판정자 프롬프트는 도구 대신 "받은 산출물의 구절"을 근거로 하게 하고 액션 규칙을 design용으로 바꿨다.
- **프롬프트 버전**: `RETRIEVAL_PROMPT_VERSION`을 `2026-10-16.p6`에서 `2026-10-17.p7`로 올렸다. 이 값은 컨텍스트 스냅샷에만 기록되므로 캐시 무효화는 없다. 버전을 고정해 둔 기존 테스트 네 개(`test_evidence`, `test_system_prompt`, `test_go_facts`, `test_code_wiki`)를 같이 고쳤다.
- **code·ops 프롬프트 동일성**: 토론 호출 9건(에이전트·판정자의 모든 필드)을 변경 전후로 떠서 비교했고 차이가 없었다(스레드 id는 정규화).
- **대시보드**: Go 에이전트는 프로젝트 `kind`를 보지 않고 CLI가 거부한다. 웹은 `lib/project.ts`에 `hasNoRepository`(ops 또는 design)를 더해 토론 시작 폼의 참조 코드 필드와 토론 페이지의 위키 탭을 숨기고 보내지 않는다. 코드 리뷰에서 나온 문제 때문이다: 처음에는 폼이 팀의 기본 컨텍스트 경로를 `context_paths`로 보내 design 토론이 "drop the context paths"로 거부됐다. `types.ts`의 `kind`에도 `'design'`을 추가했다. 그 밖의 화면(프로젝트 목록 카드, 파일 보기, 동기화 버튼)은 손대지 않았고 design 프로젝트에서의 동작을 화면에서 확인하지 않았다. 전용 표시는 P3에서 다룬다.

### 구현 메모 (P2)

2026-10-18에 구현했고 배포하지 않았다. 사용자가 결정 11과 12 이후 P2와 토큰 정리를 시작하라고 했다. 설계와 다른 점:

- **저장소 구조**: `<GUARDIAN_DATA_DIR>/design-artifacts/<project>/<thread>/vN/`. 버전마다 전체 스냅샷과 `manifest.json`(`manifest_version`, `version`, `parent`, `written`, `created_at`, 파일별 `path`, `sha256`, `bytes`, `mime`, `kind`, `stage`, `agent`, `title`)을 둔다. 쓰기는 프로젝트별 `flock` 아래 `.tmp-v*` 디렉터리에 하고 `rename` 한 번으로 나타난다. manifest는 `os.replace`로 쓴다. 파일은 600, 디렉터리는 700이다. 백업 규칙은 필요 없다: 야간 tarball이 이 디렉터리를 포함하고 안에 캐시가 없다.
- **도구**: `jev_mcp:write_artifact(name, content, base_version?, title?)`, 위험도 medium, `FILE_TOOLS`와 `WRITE_TOOLS` 밖이라 자동 적용되지 않는다(`AUTO_APPLY_KINDS` 그대로, 엔진은 design에서 `_auto_apply`를 부르지 않으며, 자동 실행 0건을 테스트가 고정한다). `KIND_TOOLS["design"]`은 `{write_artifact}`이다.
- **도구 제한**: design 프로젝트의 `policy.allowed_actions` 기본값은 `["jev_mcp:write_artifact"]`이다. 명시적 `[]`은 도구를 끄고(판정자는 P1의 "도구 없음" 프롬프트를 받는다), 그 밖의 이름은 저장 시 거부한다. `MCPClient.project_tools`는 이제 ops와 design 모두 이 정책으로 좁힌다. `team_manager.judge_tools`는 빈 페르소나 목록을 "전부"로 보므로 실제 한계는 페르소나가 아니라 프로젝트의 kind와 정책이다.
- **`base_version`**: `artifact@vN`. 스레드에 버전이 생기면 필수, 첫 쓰기에서는 생략한다. 낡은 값은 고칠 수 있는 제안 오류이고(판정자가 현재 버전이 적힌 오류와 함께 수정 요청을 받고, 파일 목록은 프롬프트에 있다), 승인 때 다시(`stale_reason`), 쓰기 때 락 안에서 한 번 더(`STALE`, `_execute`가 FAILED로 기록) 검사한다. 스레드의 다른 파일에 대한 링크 검사는 쓰기 때만 한다(`known`). 제안 시점에는 파일 집합이 바뀔 수 있다.
- **검증기**는 L1·L2를 설계대로 구현했고 차이는 이렇다: 제목 순서는 경고, alt 없는 이미지는 오류, SVG는 `#id`가 아닌 모든 `href`를 금지, Mermaid는 파서가 아니라 다이어그램 종류·금지 지시어·괄호 균형으로 검사한다. 비밀 형태(개인 키 블록, AWS 키 id, GitHub, `sk-`, Slack, JWT)는 세 형식 모두에서 거부한다. 대비 검사와 용어 검사는 **미룬다**: 경고일 뿐인데 색 모델과 용어집 출처가 P2에 없다.
- **한도**: 파일당 2 MiB(`GUARDIAN_ARTIFACT_MAX_FILE_BYTES`)와 프로젝트의 `artifacts.max_bytes`를 이제 적용한다(P1은 적용하지 않았다). `artifacts.retention_days`는 0..3650으로 검증한다(기본 30, 0은 전부 보관).
- **보관**: `guardian.py --artifacts-prune`(`--dry-run`은 목록만)이 프로젝트의 일수보다 오래된 최신 아닌 버전을 만료시키고 manifest를 묘비로 남긴다(`expired`, `expired_at`). 한 시간이 지난 `.tmp-v*` 디렉터리도 지운다. 프로젝트가 없어졌거나 design이 아닌 디렉터리는 건드리지 않는다. 결정 11대로 새 타이머 없이 `server-guardian-ops-watch.service`(02:30 UTC)의 `ExecStartPre=-`로 돌며, 실패해도 감시를 건너뛰지 않는다. 만료된 버전의 파일은 다음 버전으로 이어지지 않는다.
- **승인 화면**: `describe_action`이 인자, 내용의 처음 60줄, 대상 `artifact@vN`, L1·L2 결과를 보여준다.
- **프롬프트 버전**: `RETRIEVAL_PROMPT_VERSION`은 `2026-10-18.p14`이다(판정자의 design 도구 규칙과 아래 스레드 요약).
- **하지 않은 것**: 반복 개선 루프, Critic이 저장된 버전을 쓰는 것, `[artifact:...]` 인용 검사, Go 엔드포인트와 대시보드 탭(P3). 심어 둔 결함 말뭉치는 그 뒤에 만들었다(아래 "심어 둔 결함 말뭉치"). P2 종료 조건 "재현율이 목표 이상"은 그 절에 적은 목표(2026-10-18 설정, 섹션 숫자는 확인 대기)에 달려 있다. 나머지 종료 조건은 `test_artifact_ops.py`가 충족한다.
- **보안 검토 후 보강** (같은 날, 첫 커밋 전). 검토가 첫 검증기의 우회 입력을 재현해 다음과 같이 고쳤고, judge에게 달라지는 점도 함께 적는다.
  - Markdown: 속성 없는 무해한 태그(`br`, `kbd`, `sup`, `details` 등)를 뺀 원시 HTML은 거부한다. 엔티티로 인코딩했거나 공백으로 쪼갠 `javascript:`/`data:` 대상은 해독한 뒤 검사한다. 펜스는 CommonMark를 따른다(들여쓰기 3칸 이하, 백틱 펜스의 정보 문자열에 백틱 금지). 그래서 들여쓴 펜스가 살아 있는 텍스트를 가리지 못한다. 인라인 코드 구간은 선형 스캐너로 건너뛴다.
  - Mermaid: `click`/`callback`/`link`는 줄 머리뿐 아니라 `;` 뒤에서도 거부한다. `%%{...}` init 지시문, front matter의 `config:`, `securityLevel`은 어디에 있든 거부한다. 라벨의 HTML은 Markdown의 태그 규칙을 따른다.
  - SVG: 요소 허용 목록(애니메이션 요소와 `foreignObject` 없음), 모든 요소와 속성은 SVG·xlink·xml 네임스페이스여야 하고 루트에 `xmlns`가 필요하다. `href`는 `#id`만 허용하고 `xml:base`는 거부한다. CSS(style 요소와 속성)는 이스케이프·주석·대소문자를 해독한 뒤 `@import`, `image-set(`, `expression(`, `#`가 아닌 `url()`을 찾고, style 요소는 전체 텍스트를 검사한다(자식 요소 뒤의 텍스트를 건너뛰던 문제).
  - 공통: 제어 문자와 양방향 덮어쓰기 문자는 거부한다(승인 화면에서도 U+FFFD로 표시). 자격 증명 형태를 더 잡는다(세분화 GitHub 토큰, Google·Tailscale 키, `sk_live_`). 엔티티를 해독한 뒤와 제목에도 적용한다. `artifacts.max_bytes`나 안전 한도를 넘는 텍스트는 검사·파싱 전에 거부한다. 이름과 버전은 `fullmatch`로 맞춘다(끝의 개행이 `$`를 통과하던 문제). 일반 산출물 이름이 아닌 manifest 경로는 manifest를 읽을 때 버리므로, 변조된 manifest로 `read`·`write`·`prune`이 버전 밖의 파일을 건드릴 수 없다. prune은 심볼릭 링크인 프로젝트·스레드 디렉터리를 건너뛴다.
  - 승인 화면은 쓰기와 같은 프로젝트별 유형과 크기 한도로 검증한다.
  - judge에게 달라지는 점: Markdown의 원시 HTML, SVG 애니메이션, 네임스페이스 없는 `<svg>`를 쓴 산출물은 이제 제안 단계에서 거부되고, judge는 다른 잘못된 제안처럼 고쳐서 다시 낸다.

### 구현 메모 (P3)

2026-10-18에 구현했고 배포 전이다. 설계와 달라진 점:

- **경로**는 위의 GET 5개이고 `apps/agent/artifact_handler.go`에서 `h.guard` 없이 등록한다(디스크를 직접 읽는다). Go 1.22 패턴은 와일드카드 앞에 문자를 붙일 수 없어서 버전은 `{version}` 한 세그먼트로 받아 `v[1-9][0-9]{0,8}`로 해석한다. URL은 같다.
- **층층이 읽기.** 모든 디렉터리 단계(데이터 루트, `design-artifacts`, 프로젝트, 스레드, 버전)는 실제 디렉터리여야 하고 파일은 `openRegular`(O_NOFOLLOW, 일반 파일만)로 연다. 어디든 심볼릭 링크면 404다. manifest에 있고 확장자 표(`.md`, `.mmd`, `.svg`)에 맞는 이름만 제공하며, MIME은 manifest가 아니라 그 표로 정한다. manifest는 경로의 버전과 같은 버전이라고 적혀 있어야 한다. 스레드 id는 80자까지(`safe_id`가 쓰는 길이)다.
- **내용은 읽을 때마다 manifest와 대조한다**: 크기 상한 2 MiB, 길이, sha256. 어긋나면 409(`artifact content changed`)이고, 번들은 스트리밍을 시작하기 전에 모든 파일을 검증한다. 그래서 변조된 저장소가 멀쩡해 보이는 manifest를 가진 불완전한 zip을 만들 수 없다. 보기마다 해시를 한 번 계산하는 비용이 들지만 파일이 작다.
- **고정 헤더**(모든 파일): `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: private, no-cache`, `ETag` = sha256. Range와 조건부 요청은 `http.ServeContent`가 처리한다. `?download=1`은 `Content-Disposition: attachment`를 보낸다. 이 경로는 `streamPaths`에 없으므로 `?access_token=`은 무시된다(쿠키나 Bearer가 없으면 401). 감사 줄에는 `artifact <name>@v<n>` 또는 `artifact bundle @v<n>`만 남고 내용은 남지 않는다.
- **만료된 버전**은 목록에 남고(요약에 `expired_at`, manifest의 파일은 `expired`), 파일 요청은 내용 대신 404로 답한다.
- **백업**: `db_backup._is_excluded`는 `design-artifacts/` 아래의 `.cache` 디렉터리(파생 미리보기)를 뺀다. 산출물 자체는 백업한다. `teams/.cache`는 영향이 없다.
- **대시보드**: `#/debates/artifacts[/<thread>[/v<n>[/<file>]]]`. 위키 탭이 없는 `kind: "design"` 프로젝트에서만 두 번째 탭(토론 | 산출물)으로 보인다. 목록 → 버전 칩(만료는 흐리게) → 파일 카드(종류 칩, 크기, 단계, 에이전트, sha256 앞부분) → 뷰어. Markdown은 `MarkdownDoc`, `.mmd`는 원본 안의 가장 긴 백틱 열보다 긴 펜스로 감싼 mermaid 블록이다. **SVG는 가져온 텍스트로 만든 `<img src="data:image/svg+xml;base64,…">`로만 보여 준다**(인라인 마크업 없음, `blob:` 없음, CSP 추가 없음). 문구는 `i18n/{ko,en}/artifacts.ts`와 `debate.page.tabArtifacts`에 있다.
- **내려받기**는 설계의 `<a href="…?download=1">` 경로를 쓰지 않는다. 대시보드가 `authFetch`(쿠키 또는 Bearer, 코드 경로 하나, URL에 토큰 없음)로 텍스트를 받아 Blob 링크로 저장한다. 파일이 2 MiB 이하라 스트리밍이 필요 없다. Tauri 셸에는 아직 `save_download`(Markdown)뿐이라, 다른 종류는 `save_download_bytes`가 생길 때(P4)까지 안내 문구를 보인다. `?download=1` 헤더 자체는 Go 테스트가 확인한다.
- **이벤트는 구현하지 않았다**(`artifact_created`, `artifact_version_finished`, `artifact_skipped`). `write_artifact`는 승인된 액션의 CLI 실행(`action_ops._execute`) 안에서 돌고 어떤 작업의 `events.jsonl`에도 속하지 않아서, 이벤트를 쓸 곳이 없다. 기록은 감사 줄과 manifest다. 이벤트를 넣으려면 작업과 무관한 싱크가 필요하다. 하지 않았다.
- **진입점**: `pages/debate/ArtifactsLink.tsx`가 `JobResult`와 `RunDetail`의 `ReportButton` 옆에 "산출물 보기" 링크(`#/debates/artifacts/<thread>`)를 둔다. 끝난 토론에만 보인다. 토론이 선택된 프로젝트의 것이고 그 프로젝트가 `kind: "design"`일 때만 보인다. 산출물 탭이 선택된 설계 프로젝트에만 있기 때문이다. 프로젝트가 없는 작업은 기본 코드 프로젝트로 본다. 스레드에 저장된 버전이 있는지는 확인하지 않는다. 비어 있으면 탭이 자기 빈 화면을 보여 준다.
- **하지 않은 것**: 실제 브라우저 내려받기 확인(대시보드에서 클릭해 저장한 파일의 sha256을 manifest와 비교). Go 테스트가 쿠키 없는 401, Bearer 200, Range 206, 경로 이탈·심볼릭 링크 404, 제공·zip 바이트의 sha256을 확인하고, `ci.sh web`과 `go`가 통과한다. 브라우저 확인은 P3의 종료 조건으로 **열려 있다**.

### 심어 둔 결함 말뭉치 (P2 평가)

2026-10-18에 `cli/llm-agent/artifact_corpus.py`로 만들었다(테스트는 `test_artifact_corpus.py`, 실행은 `python3 artifact_corpus.py [--json] [--min-recall 클래스=값]`). 모델이 필요 없다. 측정 대상이 결정적 계층이기 때문이고, 품질 계층(`single`, `loop-*`)은 별도 평가다.

- **말뭉치**: 정상 산출물 10개(Markdown 4, SVG 3, Mermaid 3이고 오류와 경고가 모두 없어야 한다)와 결함 30개이다. 5개 클래스에 6개씩이고, 각각 정상 산출물의 한 곳을 정확히 한 번 바꾼 것이다. 결함이 정상 원본에 **더한** 검사 결과만 센다. 원본이 이미 가진 경고 때문에 결함이 잡힌 것처럼 보이는 일이 없다.
- **지표**: 재현율은 더해진 결과 중에 심은 클래스를 가리키는 것(클래스별 패턴)이 있는 결함의 비율이다. 정밀도는 그 탐지 수를, 지적된 결함 수와 지적된 정상 산출물 수의 합으로 나눈 값이다. 맞는 결과 없이 지적만 된 것은 `misflagged`로 따로 적는다.
- **결과**(아래 두 탐지기를 넣은 뒤): 지적된 정상 산출물 0개, 재현율 18/30 = 0.60, 정밀도 1.00.

| 클래스 | 탐지기 | 재현율 | P2 목표 |
|---|---|---|---|
| 깨진 SVG | L1/L2 SVG 검사 | 6/6 | 1.0 |
| 끊어진 링크 | L2 상대 링크·이미지 대상 검사 | 6/6 | 1.0 |
| 필수 섹션 누락 | L2 `artifacts.required_sections`(선택 사용) | 6/6 | 1.0 |
| 낮은 대비 | 없음(미룸, P2 메모 참고) | 0/6 | 탐지기가 생길 때까지 없음 |
| 코드에 대한 거짓 주장 | 설계상 없음: 토론의 `findings`와 `check_findings`가 맡고 모델이 필요하다 | 0/6 | 결정적 목표가 아님 |

- **기준선**(말뭉치를 처음 돌린 결과, 탐지기 추가 전): 재현율 10/30 = 0.33. Markdown 이미지는 alt 텍스트가 없는지만 봐서 끊어진 링크가 4/6이었고, 프로젝트에 섹션 목록이 없어서 섹션 누락은 0/6이었다.
- **같은 날 추가한 탐지기 두 개**(사용자와 목표를 정한 뒤):
  - **이미지 대상**: 스레드의 산출물이 아닌 상대 경로의 Markdown 이미지 대상은 L2 경고다(`image 'x', which is not an artifact of this thread`). 링크와 같은 규칙이다(스킴, `#`, `/`, `./` 형태는 건너뛰고, 스레드를 모르면 통째로 건너뛴다). 산출물 이름은 평평하므로 디렉터리 안의 대상(`figures/a.svg`)은 없는 것으로 본다.
  - **필수 섹션**: 프로젝트 설정 `artifacts.required_sections`가 Markdown 산출물 이름마다 들어 있어야 할 제목을 적는다(제목 수준 무관, 대소문자와 공백 무시, 코드 펜스 안의 제목은 세지 않는다). 빠진 제목은 L2 **오류**라서 쓰기가 거부되고 judge가 다른 잘못된 제안처럼 고쳐서 다시 낸다. **기본값은 비어 있다.** 파일을 적기 전까지 어떤 프로젝트도 영향을 받지 않는다. `check_arguments`는 쓰는 파일의 목록만 적용한다. 검증은 `project_registry._parse_required_sections`에 있다(Markdown 이름만, 파일마다 출력 가능한 제목 1~32개).
- **목표**(`artifact_corpus.py`의 `TARGETS`, `python3 artifact_corpus.py --targets`와 `test_artifact_corpus.py`가 지킨다): 깨진 SVG, 끊어진 링크, 섹션 누락 모두 1.0. 섹션 목표는 탐지기와 함께 제안한 것이라 **사용자의 확인이 아직 필요하다**. 1.0에 닿는 것은 말뭉치가 필수 제목을 스스로 정하기 때문이다. 실제 프로젝트에서는 그 프로젝트의 목록이 무엇을 잡을지 정한다.
- **목표에 넣지 않은 것**: 낮은 대비는 색 조합 탐지기가 생기기 전까지 0/6이다(미룸, P2 메모 참고). 거짓 주장은 결정적 목표에서 뺀다. 이 6개는 토론의 `findings`를 거치는 별도 측정의 입력이며, 그 측정은 모델과 저장된 코드 참조가 필요해서 **돌리지 않았다**.
- **P2 종료 조건**: 앞의 두 가지는 충족했다. 세 번째(심어 둔 결함 재현율이 목표 이상)는 위 숫자를 사용자가 확인하면 결정적 세 클래스에서 충족된다. 낮은 대비와 거짓 주장은 우연이 아니라 목표를 그렇게 정했기 때문에 충족 대상이 아니다.

### 구현 메모 (결정 12, 0·1단계)

2026-10-18에 구현했고 배포하지 않았다. design 전용이 아니라 엔진 전체에 적용된다.

- **0단계**: `agent_started` 이벤트에 `history_bytes`가 실린다. 그 프롬프트에 다시 보낸 이전 라운드의 크기다(이어받은 세션은 0). 임계값을 조정하기 전에 `events.jsonl`에서 확인한다.
- **1단계**: `thread_digest.py`(`DIGEST_VERSION = 1`)가 새 세션용 `_format_history`를 대신한다. 16 KiB(`GUARDIAN_THREAD_DIGEST_MIN_BYTES`) 미만이면 원문 그대로 보낸다. 넘으면 최신 라운드는 통째로 두고 이전 항목은 머리만 남긴다(Expert 1200자, Critic 800자, Judge 800자, 열린 코드 펜스는 닫고, Judge는 마지막 `VERDICT:` 줄을 유지). 뒤에 `[N more characters condensed]`가 붙는다. 머리말이 이 글이 탐색용일 뿐임을 알린다. `GUARDIAN_THREAD_DIGEST=off`면 전체 재생으로 돌아간다. 결과가 원문보다 짧을 때만 쓴다.
- **차이**: 계획은 판정, 결정 항목, `findings`, Critic 차단 사유, 제안의 첫 제목을 남기고 긴 코드 펜스와 반복 인용을 없애는 것이었다. 구현은 항목마다 머리를 자르는 방식이다. 기록이 자유 형식 글이라 구조적 요약은 페르소나 형식에 의존하기 때문이다. 요청한 안전장치는 탐색용 머리말, `evidence.py`의 줄 범위 규칙(변경 없음: 인용은 그 줄이 세션에 보였을 때만 통과), 닫힌 펜스, 크기 검사, 끄는 스위치다. 구조적 요약은 `DIGEST_VERSION`을 올려 나중에 바꿀 수 있다.
- **2단계**(가벼운 LLM 요약)는 구현하지 않았다. `llm_runner.py`가 더 가벼운 모델을 고를 수 있는지는 여전히 미확인이다.

## 위험

| 등급 | 항목 | 대응 |
|---|---|---|
| CRITICAL | 같은 출처에서 LLM HTML 실행(XSS가 곧 `/api/exec`) | 보안 절의 헤더와 sandbox. P3의 끝 조건에 헤더 테스트 포함 |
| CRITICAL | 허브에서 LLM HTML 렌더(파일 읽기, SSRF) | 서버 렌더 금지(결정 3) |
| HIGH | design이 코드 도구와 자동 적용을 조용히 상속 | `KIND_TOOLS`, `is_auto_approvable` kind 검사, 테스트로 고정 |
| HIGH | codex/agy가 `agent.env`·`llm.env`를 읽어 산출물에 유출 | claude 전용(결정 8), 저장 전 토큰 스캔 |
| HIGH | 사용 한도 폴백이 에이전트 답을 대체(평가 2 첫 실행은 135회 중 107회 손실, 유효한 재실행은 135회에서 6회 폐기) | 폴백은 반복으로 세지 않고 `deferred`로 멈춘다. 폐기 후 반복하는 평가 규칙을 적용한다 |
| MEDIUM | 산출물 누적으로 백업 tarball·디스크 증가 | 최신이 아닌 버전의 보관 기간 정리(결정 11), L1 파일당 안전 한도 |
| MEDIUM | 외부 URL을 통한 유출 | CSP `img-src data:`, `default-src 'none'` |
| MEDIUM | 반복 개선 루프의 시간·구독 한도 소모 | 수정 2회 상한, 속도 제한 시 `deferred` |
| LOW | 경로 탈출, 심볼릭 링크, 다운로드 감사 누락 | 서버 생성 파일명, `openRegular`, `recordAudit` |

반대 논거도 남긴다. (a) 이 제품의 정체성은 서버 수호·운영 토론이고, ops P2~P6와 감사 모드가 로드맵에 먼저 있다. (b) 문서와 다이어그램은 Claude 앱의
Artifacts/Docs가 격리와 렌더를 이미 해결한다. 그래서 1차 범위를 Markdown, Mermaid, SVG로 좁히는 것을 권한다.

## 검토에서 서로 어긋난 부분과 미확인

- Go 변경: 엔진 담당은 "Go 변경 없음"(`newCmd`가 `--project=`를 그대로 통과), 저장 담당은 읽기 엔드포인트 5개가 필요하다고 했다. 둘 다 맞다. 토론 시작은 Go 변경이 없고,
  산출물 열람·다운로드는 Go 핸들러가 새로 필요하다. 또 엔진 담당은 server-ops 설계서 8장의 "Go에서 409"가 실제 구현(Python CLI에서 거부)과 다르다고 지적했다. 설계서 수정은 하지 않았다.
- 허브의 claude 버전: 옛 문서(`debate-code-context.md`)에는 2.1.138, 플래그 검증은 이 Mac의 2.1.289에서 했다. 허브의 현재 버전은 **미확인**이다.
  `--json-schema`·`acceptEdits` 같은 최신 플래그는 허브 버전을 본 뒤에만 쓴다(러너에 `parse_claude_version` 기능 감지가 있다).
- codex `image_generation`과 agy의 이미지 생성: 공식 문서로 확인하지 못했다(미확인).
- 허브에서 문서 Skill(pdf, xlsx)이 claude.ai 로그인으로 동기화되는지: 공식 문서끼리 어긋나고, 허브의 최소 환경 변수 구성에서는 미확인이다.
  자격 증명을 노드 간에 복사하지 않는 규칙이 있으므로 동기화에 기대지 않는다.
- 시간·메모리 수치(반복당 2~5분, Chromium 300~800 MB)는 측정이 아니라 추정이다.
- 능력 조사의 프롬프트 기법(Sonnet 5.5 가이드의 사고 문구, 크롭 도구 효과)은 에이전트가 읽은 공식 문서의 요약이며 종합자가 원문을 다시 열어 보지 않았다.

## 사용자 결정이 필요한 항목

1. 정정된 결정 9 확인: 보관 정책 조건은 충족되었으니(결정 11) 지금 P2를 시작할지, 다른 것(예: 감사 A4 비교와 배포)을 기다릴지.
2. Tauri에 `save_download_bytes`(Rust 변경)를 추가할지. P4부터 필요하다.
3. 백업에 바이너리를 어디까지 포함할지와 그 한도. P4부터 필요하다.

## 결정 기록 (2026-10-23): 문서 형식에 한해 표준 라이브러리 제한을 푼다

운영자가 "Python 엔진은 표준 라이브러리만 쓴다"는 제한(CLAUDE.md, 확정 결정)을 풀었다. 위 표의 DOCX 등 문서 형식(PPTX, XLSX)을
OOXML 직접 작성 대신 라이브러리로 만들기 위해서다. 좁게 읽었다: DOCX를 묻다가 내린 결정이므로 문서 산출물 형식에 한한다.
나머지 제외 항목(tree-sitter, 그래프 DB, 벡터 인덱스, 작업 큐, 플릿 워커)은 운영자가 따로 말하기 전까지 유지한다. 아직 만든 것은
없다. P4 전에 정할 것:

1. 어떤 라이브러리인가(예: `python-docx`), 지연 import로 두어 패키지가 없으면 DOCX 도구만 실패하게 할 것인가.
2. 의존성이 허브에 어떻게 닿는가: `scripts/install-agent-service.sh`가 만드는 릴리스 트리에는 지금 Python 환경이 없다.
3. DOCX 미리보기: `preview_of`로 PDF/PNG를 잇지 않으면 위 계획대로 파일 카드만 둔다.

# 토론 팀(라인업) 설계

[English](debate-teams.md) | 한국어

## 결정

토론의 실행 단위는 **개별 에이전트**이고, **팀은 역할 배치(라인업)를 정의하는 설정**이다.
팀 대 팀 토론(각 진영이 내부 토론 후 대표 의견을 내는 방식)은 채택하지 않는다.

| 기준 | 팀 대 팀 | 개별 에이전트 + 라인업 (채택) |
|---|---|---|
| 라운드당 LLM 호출 | 약 10~15회 | 3회, 비평가 패널 시 3+N회 |
| 지연 | 호출당 최대 300초가 직렬로 누적 | 패널은 병렬 실행 |
| 추적성 | 팀 내부 합의 과정이 흐려짐 | 발언마다 에이전트·provider 기록 |
| jev 관문 | 요약된 대표 의견을 판정 | 원문 발언을 판정 |

관점 다양성은 **역할별 provider 지정**과 **비평가 패널**로 얻는다.

## 구조

```
configs/teams/<id>.json ──TeamManager.load──▶ Lineup ──▶ DebateEngine.run_debate(team=<id>)
                                               │
                                               ├─ expert : Member(persona, providers)   단일
                                               ├─ critics: [Member, ...]               1~4명, 병렬
                                               └─ judge  : Member(persona, providers)   단일
```

- `cli/llm-agent/team_manager.py`: `Member`, `Lineup`, 파싱·검증, `TeamManager`(파일 로드/저장)
- 저장 위치는 두 계층: 저장소 `configs/teams`(builtin, 읽기 전용 기본값)와 `GUARDIAN_TEAMS_DIR`(기본 `<GUARDIAN_DATA_DIR>/teams`, 즉 `~/.guardian/data/teams`, custom). 같은 id면 custom이 우선하고, 저장은 항상 custom에 한다. 페르소나도 같은 방식(`configs/personas` / `GUARDIAN_PERSONAS_DIR`, 기본 `<GUARDIAN_DATA_DIR>/personas`)이다.
- `DebateEngine`: 라인업을 받아 역할별로 개별 에이전트를 호출한다. 엔진은 상대가 단일인지 패널인지 몰라도 되도록 `_ask_critics`가 결과 목록을 돌려준다.
- `LLMRunner.run_session_prompt(providers=[...])`: 멤버가 지정한 provider만 그 순서로 시도한다. 실행 호스트 기본 순서(`GUARDIAN_LLM_PROVIDERS`)에 없더라도 설치되어 있으면 사용한다. 하나도 설치되어 있지 않으면 built-in 폴백 → 라운드 `UNVERIFIED`.

### 기본 제공 팀 (2026-10-18)

| id | 라인업 | 라운드 | 허용 도구 |
|---|---|---|---|
| `default` | `domain_architect` / `critic_redteam` / `jev_mcp_decision`, 실행 호스트 기본 provider | 기본 3 | 생략(판정자 페르소나 설정) |
| `cross-review` | `domain_architect` / `critic_redteam` + `devops_specialist` 패널 / `jev_mcp_decision`, 역할별 provider | 3 | `jev_mcp:run_tests`, `jev_mcp:apply_patch`, `jev_mcp:execute_code_action` |
| `fleet-ops` | `ops_sre` / `ops_safety_critic` + `ops_capacity_critic` 패널 / `ops_judge`, 실행 호스트 기본 provider | 2 | 노드 도구 8개(`ops:*` 카탈로그 7개와 `jev_mcp:server_remediation`) |
| `fleet-provision` | `ops_provisioner` / `ops_provision_safety_critic` + `ops_dependency_critic` 패널 / `ops_provision_judge`, 실행 호스트 기본 provider | 2 | `ops:package_install`, `ops:package_remove`, `ops:restart_service`. 패키지 주문은 단일 액션이 아니라 계획으로 낸다([fleet-provisioning.ko.md](fleet-provisioning.ko.md) 참조) |
| `fleet-services` | `ops_service_engineer` / `ops_service_safety_critic` + `ops_service_cause_critic` 패널 / `ops_service_judge`, 실행 호스트 기본 provider | 2 | `ops:restart_service` |
| `fleet-mesh` | `ops_mesh_engineer` / `ops_mesh_risk_critic` / `ops_mesh_judge`, 실행 호스트 기본 provider | 2 | 없음(보고 전용, [server-ops-project.ko.md](server-ops-project.ko.md) P8 참조) |
| `guardian-dev` | `guardian_lead_dev`(claude) / `guardian_security_critic`(codex, agy) + `guardian_qa_critic`(agy, codex) + `guardian_release_critic`(agy, codex) 패널 / `guardian_dev_judge`(claude) | 3 | `jev_mcp:run_tests`, `jev_mcp:apply_patch`, `jev_mcp:execute_code_action` |

`guardian-dev`는 Server Guardian 자체를 개발하는 팀이다. Claude가 초안과 판정을 맡고 Codex와 Gemini(`agy`)가 검토한다. 어떤 프로젝트의 기본 팀도 아니며 `--team guardian-dev`로 고른다.

`fleet-ops`는 운영(ops) 프로젝트용 팀이다. 내장 ops 프로젝트 `fleet`의 기본 팀이며, 페르소나와 역할은 [server-ops-project.ko.md](server-ops-project.ko.md) 14장 "운영 팀과 LLM CLI skill"에 있다. `configs/teams/default.json`이 없어도 `default`는 코드의 기본 라인업(`default_lineup()`)으로 동작한다.

### 규칙

- **Expert와 Judge는 항상 단일 에이전트.** 제안과 승인에 각각 책임 주체가 하나씩 있어야 jev 관문과 액션 승인 흐름이 성립한다.
- **비평가 패널은 원문을 그대로 이어 붙인다** (`### Critique from <이름>`). LLM으로 요약하지 않는다. 근거가 흐려지고 호출이 늘어나기 때문이다.
- **패널 멤버 중 하나라도 built-in 폴백이면 라운드 전체가 `UNVERIFIED`.**
- **팀은 판정자 도구를 좁힐 수만 있다.** 실제 허용 도구 = `allowed_tools` ∩ 판정자 페르소나의 `mcp_capabilities`. `allowed_tools: []`이면 도구 제안 없음, 생략하면 페르소나 설정을 따른다.
  (2026-10-18: 그 결과를 프로젝트가 한 번 더 좁힌다. code 프로젝트는 코드 도구만, ops 프로젝트는 노드 도구 중 `policy.allowed_actions`에 있는 것만 남는다(`MCPClient.project_tools`). ops 프로젝트에서 남는 도구가 없으면 토론 시작 때 경고한다. 폐기된 도구(`jev_mcp:git_commit_push`)는 경고와 함께 무시된다.)
- 고위험 도구(`server_remediation`)를 허용하면 검증 시 경고한다. 실행은 여전히 사람 승인이 필요하다.

### 세션 키

`agent_sessions`의 기본 키는 `(thread_id, role)`이다. 마이그레이션 없이 패널을 지원하기 위해:

- 단일 멤버: `role` 그대로 (`Critic Agent`) — 기존 스레드의 세션이 그대로 이어진다.
- 패널 멤버: `role:persona` (`Critic Agent:devops_specialist`) — 그래서 패널 내 페르소나는 중복될 수 없다.
- `--attach <thread> --role "critic agent:dev"` 처럼 접두어로 선택한다.

### 재현성

- 스레드 시작(또는 `--team`으로 재실행) 시 라인업 스냅샷을 `threads.team_snapshot`(JSON)과 스레드 JSON/Markdown 내보내기에 저장한다.
- 기존 DB에는 시작 시 `ALTER TABLE threads ADD COLUMN team_snapshot`이 자동 적용된다.
- `--thread`로 이어갈 때 `--team`이 없으면 **저장된 라인업을 재사용**한다. `--team`을 주면 그 팀으로 교체되고 스냅샷도 갱신된다.

### 우선순위

1. `run_debate(team=...)` / `--team`
2. 스레드에 저장된 라인업
3. 프로젝트의 기본 팀 (2026-10-18 추가: 프로젝트 JSON의 `"team"`, 예: 내장 `fleet`은 `fleet-ops`)
4. 기본 라인업 (`domain_architect` / `critic_redteam` / `jev_mcp_decision`, 실행 호스트 기본 provider)

(`DebateEngine._resolve_lineup`.) 저장된 라인업이 더는 유효하지 않거나(그 사이 페르소나가 지워짐) 프로젝트의 팀을 불러올 수 없으면 기본 라인업으로 돌아가고 경고를 남긴다.
그 위에 `expert_id` / `critic_id` / `judge_id` 인자가 해당 역할의 페르소나만 덮어쓴다(실행 호스트 기본 provider로). 라운드 수는 `--rounds` > 팀 `max_rounds` > 3.

## 팀 파일 스키마

```json
{
  "id": "cross-review",
  "name": "Cross-provider review (critic panel)",
  "expert": {"persona": "domain_architect", "providers": ["claude", "codex"]},
  "critic": [
    {"persona": "critic_redteam", "providers": ["codex", "agy"]},
    {"persona": "devops_specialist", "providers": ["agy", "codex"]}
  ],
  "judge": {"persona": "jev_mcp_decision", "providers": ["claude", "codex"]},
  "max_rounds": 3,
  "allowed_tools": ["jev_mcp:run_tests", "jev_mcp:apply_patch", "jev_mcp:execute_code_action"]
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `id` | O | 파일명과 같아야 함. 소문자·숫자·`-`·`_`, 64자 이내 |
| `expert`, `judge` | O | `{"persona", "providers"}` 또는 페르소나 id 문자열 |
| `critic` | O | 멤버 하나 또는 1~4개 목록 |
| `proposers` | | 추가 초안 작성자 0~2명 목록(선택, 2026-10-18 추가, [multi-perspective.ko.md](multi-perspective.ko.md)). 실행의 첫 라운드에 전문가와 서로 보지 않고 초안을 쓴다. ops·design 프로젝트에서는 경고와 함께 무시된다. 페르소나는 전문가·다른 제안자와 달라야 한다 |
| `providers` | | 시도 순서. 비우면 실행 호스트 기본 순서(`claude, codex, agy, zai, opencode, ollama, llm`). 고를 수 있는 이름은 여기에 `exaone`을 더한 것 |
| `code_tools` | | 멤버별 선택 필드(2026-10-18 추가). `"mcp"`이면 claude·codex 멤버가 읽기 전용 저장소 도구(`repo_mcp.py`)를 쓴다. 실행 호스트에 `GUARDIAN_REPO_MCP=1`이 필요하다. 선택 사용(opt-in): 팀 편집 화면의 "저장소 도구 추가" 체크박스(기본 꺼짐, 꺼져 있으면 필드를 저장하지 않는다). code 프로젝트에서만 쓰이고, ops·design 프로젝트는 무시하며 토론 시작 때 경고한다(2026-10-19) |
| `max_rounds` | | 1~10 |
| `allowed_tools` | | 판정자 도구 상한. 생략 시 페르소나 설정 |
| `context` | | 기본 참고 코드 `{"paths": [glob...], "ref": "HEAD"}`(2026-10-18 추가, [debate-teams-api.ko.md](debate-teams-api.ko.md) 5절). ops 프로젝트에서는 무시되고 경고가 남는다 |
| `category` | | 설정 → 팀 목록용 팀 목표(2026-10-19 추가): `development`, `operations`, `design-docs`, `general` 중 하나. 다른 값은 저장이 거부되고, 이미 저장된 알 수 없는 값은 경고와 함께 "기타"로 표시된다. 라인업 스냅샷에는 들어가지 않는다 |
| `created`, `modified` | | ISO 8601 시각(2026-10-19 추가). 대시보드나 `--team-save`로 저장할 때마다 엔진이 찍는다. `modified`는 현재 시각이고, `created`는 저장된 팀(builtin을 편집한 경우 builtin 포함)의 값을 유지하며 첫 저장 때 정해진다. builtin 파일에는 첫 커밋 날짜가 들어 있다. 날짜가 없는 팀은 맨 뒤에 정렬된다 |

설정 → 팀 페이지는 팀을 행(이름, 출처, id, 한 줄 요약, 생성일)으로 보여 주고, 행을 열면 전체 라인업이 나온다. 기본은 목표별 묶음에 최근 생성순이고, 정렬은 최근 수정순·이름순으로, 묶기는 끌 수 있다.

비밀값(토큰)은 팀 파일에 넣지 않는다. 자격증명은 계속 `~/.guardian/llm.env`에만 둔다.

### 페르소나 페이지 (2026-10-22)

페르소나에는 전용 대시보드 페이지 `#/debates/personas`가 있다(토론 페이지의 "팀 관리" 옆 "페르소나" 링크와 설정 → 페르소나에서 들어간다). `#/debates/personas/<id>`는 한 페르소나를 편집하고 `#/debates/personas/new`는 빈 양식을 연다. 목록은 모든 페르소나를 기본/사용자 정의 칩과 함께 보여 주며, 이름·id·전문 분야로 검색하고 역할로 거를 수 있다. 편집기는 모달이 아니라 전체 페이지이고 허브가 받는 모든 필드를 다룬다: id(새로 만들 때만), 이름, 역할(기존 역할 이름을 제안으로 쓰는 자유 입력), 전문 분야, 시스템 프롬프트, `focus_areas`·`critique_focus`(한 줄에 하나), `mcp_capabilities`(팀 편집기와 같은 도구 목록의 체크박스이므로 새 엔드포인트는 없다). "다른 페르소나에서 시작"은 기존 페르소나를 id를 비운 채 복사하고, 시작 템플릿 3종(전문가·비평가·판정자, 문구는 i18n 카탈로그)은 뼈대를 채운다. "사용하는 팀" 영역은 전문가·제안자·비평가·판정자로 그 페르소나를 쓰는 팀을 보여 준다. 기본 제공 페르소나를 저장하면 사용자 정의 덮어쓰기가 만들어지고, 삭제는 사용자 정의 페르소나만 되며 확인을 거친다. 허브 에이전트의 검증 오류는 페이지에 그대로 남는다. 설정 → 페르소나의 기존 편집 대화상자는 빠른 수정용으로 남겨 두었다(온도 상한은 여전히 1).

## 사용

```bash
python3 cli/llm-agent/guardian.py --teams                        # 목록 + 검증 + 이 노드 설치 여부
python3 cli/llm-agent/guardian.py --debate "작업" --team cross-review
python3 cli/llm-agent/guardian.py --debate "후속 지시" --thread <ID>  # 저장된 라인업 재사용
```

## 모델에 전달되는 퍼소나 필드 (2026-10-30)

에이전트에게 전달되는 것은 `system_prompt`뿐이고, `mcp_capabilities`는 판정자의 도구를 좁힌다. 선택 필드 `profile`(`tagline`, `look`. 2026-10-30 "AI로 제안" 카드와 함께 추가, `agent-characters.md` 2.6절)은 보여 주기만 하고 보내지 않는다. `domain`, `focus_areas`, `critique_focus`는
저장되고 표시될 뿐 전달되지 않으며, 에이전트를 이끌어야 할 관점은 `system_prompt`에 적는다. `temperature`는 2026-10-23에 없앴다(LLM CLI는
온도를 받지 않는다. 예전 파일도 읽히고, 검증에서 이 필드를 버린다). 프롬프트 `p21`(GitHub 미러의 번호, 로컬 이력과 합친 뒤 `p23`)에서 일반 퍼소나를 새 퍼소나의 규칙에 맞춰 다시 썼다. `domain_architect`는 읽은
코드에서 가장 작은 변경을 제안하고 `파일:줄`로 인용한다. `critic_redteam`은 인용하고, 구체적인 시나리오와 함께 높음/중간/낮음으로 등급을
매기고, 문제가 없으면 없다고 쓴다. `devops_specialist`(`cross-review`의 비평가, `proposer_eval`의 제안자)는 두 역할의 규칙을 다 가진다.
`server_guardian`(CLI 채팅)은 정해진 수의 노드를 스스로 관리한다고 말하지 않고, 사람이 승인할 액션을 제안한다. 2026-10-19의
`proposer_eval` 구성은 예전 문구를 썼다.

## 다음 단계

1. **대시보드 연동**: [debate-teams-api.ko.md](debate-teams-api.ko.md) 참고 (페르소나·팀 편집, 토론 실행).
2. **대상 범위**: 팀에 `target_group`(노드 그룹)을 추가해 `server_remediation`의 `target_host`를 그 그룹으로 제한.
   (2026-10-18: 팀이 아니라 ops 프로젝트의 `targets`로 구현됐다. [server-ops-project.ko.md](server-ops-project.ko.md) 참고.)
3. **팀 대 팀**: 실제 토론 기록에서 패널로 부족하다는 근거가 나올 때만, 같은 `ask` 인터페이스를 갖는 `TeamParticipant`로 추가.

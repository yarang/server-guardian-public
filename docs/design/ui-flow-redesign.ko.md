# 웹 대시보드 UI 흐름 재설계

[English](ui-flow-redesign.md) | 한국어

상태: 승인(2026-10-07). 범위: `apps/web-desktop`. 1~4장은 승인된 계획이고, 5장은 어떻게 만들었는지와 그 뒤에 무엇이
바뀌었는지를 기록한다("이후 변경"이 현재 내비게이션을 설명한다).

## 1. 현재 상태 (3d00cd9 기준)

평평한 라우트 여섯 개, 각각 해시 조각 하나(`#/fleet`, `#/ssh`, `#/assistant`, `#/debate`, `#/batch`,
`#/terminal`). `App.tsx`는 이동할 때마다 페이지를 다시 마운트한다(`<div key={route}>`).

| 라우트 | 담고 있는 것 | 불러오는 데이터 |
|---|---|---|
| Fleet Overview | 허브 메트릭(5초 폴링), group-by/필터가 있는 노드 인벤토리, 노드 그룹 CRUD, fleet 진단(열면 자동 실행) | metrics, servers, groups, diagnoseFleet |
| SSH Security Audit | fleet sshd 감사(그룹 요약, 규칙×노드 히트맵, 선택한 노드), 접이식 영역의 허브 로컬 감사 | fleet ssh, groups, hub ssh security/config/keys |
| Assistant | fleet 진단 요약, 허브만의 SSH 점수, "Debate this" 넘기기 | diagnoseFleet, hub ssh security, groups |
| Persona & Debate | 프로젝트(프로젝트 선택), 페르소나, 팀, 토론 콘솔 + 작업 + 최근 목록, 제안된 액션, 액션 대화상자 | personas, teams, projects, debates, actions |
| Batch Execution | 대상(fleet / 그룹 / 노드), 프리셋, 스트리밍 실행, 노드별 표 | servers, groups |
| Remote Terminal | 대상(로컬 / 노드), 명령 기록 | servers |

### 문제

1. **흐름 없이 목록만 있다.** 작업 순환은 관찰 -> 분석(토론) -> 승인 -> 실행인데, 메뉴는 관찰, 실행, 설정을 한 단계에 섞어 둔다.
   지금 무엇에 주의해야 하는지(오프라인 노드, 실패한 SSH 규칙, 승인을 기다리는 액션)를 알려주는 곳이 없다.
2. **중복 작업.** fleet 진단은 Fleet을 열 때 *그리고* Assistant를 열 때 모든 노드를 프로브한다. servers와 groups는 네 페이지에서
   다시 가져온다. 페이지가 다시 마운트되므로 이동할 때마다 다시 돈다.
3. **Assistant는 얇은 다리다.** Fleet의 진단을 되풀이하고, fleet 숫자 옆에 허브만의 SSH 점수를 보인다(잘못 읽히지 않으려면 범위
   설명이 필요하다). 발견 사항에서 토론을 시작할 수 있는 유일한 곳이다.
4. **Persona & Debate가 과적재되어 있다.** 관심사 여섯 개가 세로로 쌓여 있다. 드물게 바뀌는 설정(프로젝트, 페르소나, 팀)이 주 작업
   (콘솔) 위에 있고, 가장 시간에 민감한 사람 단계인 승인 대기열은 맨 아래에 있다. 선택한 프로젝트는 페이지 지역 상태라 방문할
   때마다 `server-guardian`으로 돌아간다.
5. **엔터티 페이지가 없다.** 노드를 열 수 없다. 오프라인 노드나 SSH 실패 노드에서 그 노드의 감사, 그 노드의 셸, 그 노드에 대한
   토론으로 가는 길이 없다. 토론과 액션를 링크할 수 없다.
6. **이동하면 상태를 잃는다.** Batch를 떠나면 스트림 감시가 취소되고, Debate를 떠나면 지켜보던 작업이 사라지고, Terminal 기록도
   없어진다. 라우터는 첫 조각 뒤를 모두 무시하므로 아무것도 딥 링크할 수 없다.
7. **Terminal과 Batch가 겹친다.** Batch는 이미 노드 한 대에서도 돈다. Terminal은 로컬 agent 노드를 더할 뿐이다.
8. **모바일.** TabBar에 항목이 여섯 개다.

## 2. 목표 정보 구조

작업 순환으로 묶은 일곱 영역. 라벨은 영어로 둔다(UI 언어).
(2026-10-18: 대시보드 문구는 이제 기본이 한국어이고, Settings에서 영어를 고를 수 있다.
[dashboard-i18n.md](dashboard-i18n.ko.md) 참고.)

| 묶음 | 영역 | 라우트 | 대체하는 것 |
|---|---|---|---|
| Monitor | Overview | `#/overview` (기본) | Assistant, Fleet 상단 |
| Monitor | Nodes | `#/nodes`, `#/nodes/:id` | Fleet 인벤토리 + 진단 |
| Monitor | Security | `#/security` | SSH Security Audit |
| Respond | Debate | `#/debate`, `#/debate/:jobId` | Persona & Debate의 콘솔 + 최근 목록 |
| Respond | Approvals | `#/approvals`, `#/approvals/:id` | 제안된 액션 패널 + 액션 대화상자 |
| Respond | Run | `#/run?target=...` | Batch Execution + Remote Terminal |
| Configure | Settings | `#/settings/:section` | agent 엔드포인트 대화상자, 프로젝트, 페르소나, 팀, 노드 그룹 |

예전 해시는 계속 동작한다: `fleet` -> `nodes`, `assistant` -> `overview`, `ssh` -> `security`,
`batch` / `terminal` -> `run`.

### Overview (홈)

"지금 무엇에 주의해야 하는가"에 답한다. 카드마다 주된 다음 단계가 하나 있다:

- Fleet 도달성(온라인 x / y, 오프라인 이름) -> 필터된 Nodes 열기, 또는 *Debate this*.
- 허브 메트릭(간단한 CPU / 메모리 / 디스크 / 부하).
- Fleet SSH(평균 점수, 실패 노드) -> Security, 또는 *Debate this*.
- 기다리는 승인(개수, 가장 오래된 것) -> Approvals.
- 실행 중 / 최근 토론 -> Debate 작업.

*Debate this*는 기존의 미리 채우기 넘기기를 유지하되, 작업 범위를 그 카드로 좁힌다.

### Nodes

실시간 프로브(상태, 가동 시간, 부하, 메모리, 디스크)를 합친 인벤토리 표, 지금처럼 group-by와 그룹 필터. 행을 누르면 `#/nodes/:id`가
열린다: 프로브 결과, 그 노드의 SSH 감사 행과 규칙, 그리고 액션: *Open shell*(`#/run?target=<id>`), *Debate this node*. 그룹 CRUD는
Settings로 옮기고, Nodes는 그룹으로 필터만 한다.

### Security

지금의 fleet SSH 감사, 내용은 그대로. 노드 이름은 `#/nodes/:id`로 링크한다. 허브 로컬 보기는 접이식 영역에 남는다.

### Debate

페이지 머리에 프로젝트 전환기(브라우저마다 저장). 본문: 시작 폼, 그다음 지켜보는 작업(로그와 결과), 그다음 최근 토론.
`#/debate/:jobId`는 작업을 바로 연다. 페르소나, 팀, 프로젝트는 Settings에서 고치고, 시작 폼은 팀만 고른다.

### Approvals

제안된 액션 목록이 본문이며, 프로젝트와 스레드로 필터한다. `#/approvals/:id`는 기존 액션 대화상자 내용을 페이지 섹션으로 연다
(실행 / 거부 / 재확인은 그대로, 확인 절차도 그대로). 내비게이션 항목은 결정을 기다리는 액션 수를 보인다.

### Run

대상 선택기 하나: 로컬 agent 노드, 노드, 그룹, fleet 전체. 단일 대상은 터미널처럼 동작한다(기록 유지). 그룹 / fleet 실행은 확인
대화상자, 진행 막대, 노드별 표를 유지한다. 프리셋도 남는다. `?target=`은 미리 고른다(노드 페이지에서).

### Settings

섹션: Agent 엔드포인트(와 로그아웃), Projects, Personas, Teams, Node groups. 기존 대화상자를 그대로 다시 쓴다.

## 3. 공통 변경

- **매개변수가 있는 라우터.** `parseHash`는 `{ route, params, query }`를 돌려주고, `hrefFor(route, ...)`가 링크를 만든다.
  모르는 해시는 Overview로 간다.
- **공유 허브 데이터.** servers, groups, 최근 fleet 진단은 측정 시각과 함께 앱 수준 저장소에 둔다. Overview와 Nodes는 같은 프로브를
  읽고, 방문할 때마다 프로브하는 대신 *Re-run*을 제공한다.
- **오래 걸리는 작업은 이동해도 살아남는다.** batch 스트림과 지켜보는 토론 작업은 페이지 위에서 소유한다. 페이지로 돌아오면 실시간
  상태를 보인다. 라우트가 바뀌었다는 이유만으로 페이지를 다시 마운트하지 않는다.
- **내비게이션.** 사이드바는 세 묶음을 머리글과 함께 보이고, 승인 수는 배지다. 모바일 TabBar는 Overview, Nodes, Debate, Approvals,
  Run을 보이고, Security와 Settings는 머리 메뉴에서 간다.

## 4. 단계별 전달

각 단계는 앱이 동작하고 테스트가 통과하는 상태로 끝나며, 따로 커밋한다.

1. **기반.** 매개변수와 예전 별칭이 있는 라우터, 묶음 내비게이션, servers / groups / diagnosis 공유 저장소, 라우트가 바뀌어도
   페이지를 다시 마운트하지 않기. 그 밖의 페이지는 그대로.
2. **Overview와 Nodes.** 새 Overview(Assistant를 흡수), 프로브를 합친 Nodes와 노드 상세, Security에서 노드 링크.
3. **Run.** Terminal을 Batch에 합쳐 `?target=`이 있는 Run으로. 이동해도 실행을 유지.
4. **Debate, Approvals, Settings.** 토론 페이지를 나눔. 배지와 `#/approvals/:id`가 있는 Approvals 페이지. 설정 섹션이 있는
   Settings. 프로젝트 선택 저장. 이동해도 지켜보는 작업 유지.

## 5. 구현 메모

위 계획과 다르거나 빈 곳을 채운 선택.

1~3단계:

- `fleet`는 `nodes`의 별칭이다.
- Run의 기본 대상은 로컬 agent 노드다(확인이 필요 없다).
- 노드 페이지에는 `#/run?target=<id>`로 가는 "Run on this node" 링크 하나가 있다.
- Overview는 fleet SSH 감사를 직접 불러온다. 공유 허브 저장소에 들어 있지 않다.
- Security의 히트맵 행 라벨은 링크가 아니다.

4단계:

- **선택한 프로젝트**는 앱 수준 저장소(`state/projectStore.tsx`)이며 localStorage(`guardian.project`)에 저장된다. 저장된 id가
  불러온 프로젝트 목록에 없으면 저장값을 덮어쓰지 않고 `server-guardian`으로 대체한다(목록에 잠시 닿지 못해도 선택을 잊지 않는다).
  프로젝트 목록 자체도 이 저장소에 있으며 Settings와 공유한다.
- **Approvals 페이지는 별도 상세 페이지가 아니라 목록 위에 액션 대화상자를 쓴다.** `#/approvals/:id`는 목록 위에 `ActionDialog`를
  그리고, 닫으면 `#/approvals`로 이동한다. 그래서 실행 / 거부 확인과 실행 중 잠금은 바뀌지 않는다. Debate 결과의 액션 버튼은
  `#/approvals/<id>`로 이동한다(Debate에는 대화상자가 없다).
- **Approvals 정렬**: 언제나 PROPOSED, 그다음 HELD, 그다음 결정된 것. 그리고 지역 상태 필터(All / Awaiting decision / Held /
  Decided), 기본값 All.
- **배지와 Overview 카드는 한 번의 조회를 공유한다**(`state/approvalsStore.tsx`): 30초 폴링, 전역 Refresh, 프로젝트 전환, 그리고
  실행 / 거부 / 재확인 뒤의 `refresh()`. Approvals 페이지의 목록은 여전히 자기 (스레드로 필터된) 보기를 따로 가져오므로, 그 페이지는
  요청을 하나 더 한다.
- **지켜보는 토론 작업**은 `state/debateStore.tsx`가 (최근 토론 목록과 함께) 소유한다. 작업을 시작하거나 열면 URL이
  `#/debate/<id>`로 바뀐다. 작업을 여는 유일한 출처는 URL이다. 시작 폼 필드 자체는 페이지 지역 상태이며 Debate를 떠나면 초기화된다.
- **Settings** 섹션은 `#/settings/<section>` 위의 SegmentedControl이고, 각 섹션은 보일 때 자기 데이터를 가져온다. "Change endpoint
  or sign out"은 여전히 기존의 단일 대화상자다(별도 로그아웃 버튼 없음). Projects는 "Use" 버튼을 잃었다. 선택은 전환기로만 한다.
- **Nodes**는 그룹 편집기 대신 `#/settings/groups`로 가는 "Manage groups" 링크를 둔다.

### 5단계: 그룹 관리 흐름

그룹 CRUD는 Settings -> Node groups에 남고, Nodes와 노드 상세가 이제 그곳으로 이어진다. 모든 수정은 `POST /api/groups`를
거치고(허브가 검증하며 오류는 받은 그대로 보인다), 그다음 `hubStore`의 공유 groups를 다시 불러오므로 Nodes, Run, Security, 노드
상세가 같은 것을 본다.

- **Nodes 다중 선택**: 체크박스 열과 "Select all visible". 도구 막대에 "N selected"와 *Create group from selection*(새 그룹
  `GroupDialog`를 `initialInclude`로 시작, 이름은 비어 있음), *Add to group...*, *Clear selection*이 있다. 필터가 바뀌면 선택은 보이는
  행으로 줄어든다. *Add to group*은 바뀔 내용을 먼저 보인다: `include`에 추가되는 id(중복 제거)와 그 그룹의 `exclude`에서 빠지는 id.
- **노드 상세 멤버십**: "Group membership" 패널이 그룹마다 노드가 어떻게 들어 있는지 보인다. 출처는 허브의 그룹 목록에서 온다
  (`members`는 풀린 값): `include`에 id가 있으면 -> "added", 그 밖에 규칙으로 맞은 멤버면 -> "by rule". 추가된 멤버의 *Remove*는
  `include`에서 빼고, 규칙으로 맞은 경우는 `exclude`에 넣는다("excluded from rule match"). 둘 다 먼저 확인한다. 저장한 뒤에도 노드가
  여전히 멤버이면(골라 넣었고 규칙에도 맞는 경우) 결과가 그렇게 말한다.
- **Nodes의 그룹 요약**(`?group=<id>`): 이름, 설명, 멤버 수, 정의, 공유 진단 기준 도달성(온라인 x / y, 오프라인 이름, 프로브 나이.
  진단이 없으면 "not probed"), 그리고 멤버의 fleet SSH 요약(캐시된 `fetchFleetSSHAudit(memberIds, false)` 한 번). 액션: Run on this
  group(`#/run?target=group:<id>`), Debate this group(`buildGroupDebateTask` 미리 채우기), Edit group(`#/settings/groups?edit=<id>`,
  대화상자를 연다. 닫으면 쿼리를 지운다).
- **Settings 목록**: 그룹 이름은 `#/nodes?group=<id>`로 링크한다.

### 이후 변경 (2026-10-12부터)

5단계 뒤에 내비게이션을 한 번 더 합쳤다(커밋 "consolidate navigation into Fleet, Debates, Approvals and Settings"와
"move node groups from Settings to a Fleet Groups tab"). `apps/web-desktop/src/router.ts`, `App.tsx`, `pages/FleetPage.tsx`
기준의 현재 상태:

- **목적지 네 개**: Fleet(`#/fleet`, 기본), Debates(`#/debates`), Approvals(`#/approvals`), Settings(`#/settings`).
  사이드바와 모바일 탭 막대가 같은 네 개를 보인다. 묶음 머리글과 More 탭은 없다.
- **Fleet 탭**(첫 경로 조각, URL에 남는다): 요약(`#/fleet`, 예전 Overview), 노드(`#/fleet/nodes`, 노드 페이지
  `#/fleet/nodes/<id>`, 등록 `#/fleet/nodes/new`), SSH 감사(`#/fleet/ssh`, 예전 Security), 명령(`#/fleet/commands?target=...`,
  예전 Run), 그룹(`#/fleet/groups`, `?edit=<id>`는 그룹 하나를 연다).
- **Debates**는 토론 콘솔과 토론 에이전트 모니터를 합친다: 목록 하나와 그 옆의 선택한 토론(`#/debates/<id>`), 그리고 저장소가 있는
  프로젝트를 위한 위키 보기(`#/debates/wiki[/<page>]`).
- **Settings 섹션**: 허브 연결(`endpoint`, 언어 선택 포함), Projects, Personas, Teams. 노드 그룹은 Fleet의 그룹 탭으로 옮겼고,
  `#/settings/groups`는 쿼리를 유지한 채 그리로 넘어간다.
- **예전 해시**는 매개변수와 쿼리를 유지한 채 계속 동작한다: `overview`, `assistant`, `more` -> Fleet. `nodes` -> Fleet 노드.
  `security`, `ssh` -> Fleet SSH 감사. `run`, `batch`, `terminal` -> Fleet 명령. `agents`, `debate` -> Debates
  (`#/agents?run=<id>`는 그 토론을 선택한다). 모르는 해시는 Fleet 요약으로 간다.
- **배지**: Fleet은 그룹이나 fleet 실행이 진행 중이면 "Running", Approvals는 기다리는 액션 수(Execute 중에는 "Running"도),
  Debates는 Publish 중에 "Running"을 보인다.

### 목적지 다섯 개 (2026-10-29)

디자인 보드의 사이트맵(메뉴, 용어, 흐름)에 맞춰 메뉴를 다시 정리했다. 범위는 메뉴와 용어뿐이다. 흐름 종류(템플릿,
`flow-templates.ko.md`), 설정의 키 섹션(`agent-seats-keys.ko.md`), 승인 기록, 더 풍부한 현황 카드는 **만들지 않았고**
그 자리 표시용 빈 페이지도 없다. 구현은 z.ai 에이전트(`scripts/zai-agent.sh`)가 했고 독립 검토자가 확인했다. 2026-10-29에 `4f8380a`로
배포했고 운영자가 실제 브라우저에서 확인했다.

- **목적지 다섯 개**: 현황, 노드, 업무 흐름, 승인, 설정(사이드바와 탭 다섯 개의 모바일 막대). 라우트 id는 일부러
  `fleet`(노드), `debates`(업무 흐름), `approvals`, `settings` 그대로다(`src`의 호출 지점 89곳, 테스트의 `#/...` 문자열 약
  290개). 새 id는 `overview`뿐이고 기본 라우트다. 아이콘: `grid`(현황), `server`(노드), `flow`(업무 흐름), `check`(승인),
  `gear`(설정); 새 셋은 `packages/guardian-ui`의 `Icon.tsx`에 있다.
- **현황**(`#/overview`)은 예전 Fleet 요약 페이지다. Fleet 탭은 이제 노드, 그룹, SSH 감사, 명령, 명령 세트
  (`#/fleet/commandSets`, 설정에서 옮겨 왔다). 사이드바의 노드 항목은 `#/fleet/nodes`로 링크한다.
- **설정 묶음**: 허브 연결(`endpoint`), 흐름 구성(`flowConfig`: 프로젝트, 팀, 페르소나를 둘째 전환기로; `#/settings/flowConfig`는
  프로젝트를 연다), 실행 환경(`workers`, 예전 워커), 화면(`display`: 언어와 내보내기 폴더, 허브 연결에서 옮겨 왔다).
- **예전 해시**: 맨 `#/fleet`와 `#/fleet/summary` -> 현황(더 이상 노드 목록이 아니다). `#/overview`, `#/assistant`, `#/more` -> 현황.
  `#/settings/commandSets` -> Fleet 명령 세트. `#/settings/groups` -> Fleet 그룹(쿼리 유지). 앞 절의 다른 별칭은 그대로이고
  모르는 해시는 현황으로 간다.
- **용어**: 내비게이션 문구에서 Fleet -> 노드, Debates -> 업무 흐름. `glossary.ko.md`(내비게이션)를 본다.

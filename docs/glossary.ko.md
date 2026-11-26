# 용어집

[English](glossary.md) | 한국어

한 가지 대상에는 한 가지 낱말만 쓴다. 대시보드(두 카탈로그), API, CLI, 문서에 모두 해당한다. 2026-10-28에 운영자가
정했다. 새 화면·필드·문서에 여기 없는 낱말이 필요하면 먼저 이 문서에 추가한다.

대상이 아닌 것: 제품 이름 "Server Guardian", 인벤토리 파일 이름 `servers.json`과 `GUARDIAN_SERVERS_FILE`(개념이 아니라
파일 이름이다), 그리고 "headscale 서버", "MCP 서버", "SSH 서버"처럼 그 소프트웨어를 가리키는 "서버".

## 머신

| 한국어 | English | 뜻 | 쓰지 않는 말 |
|---|---|---|---|
| 노드 | node | 플릿 인벤토리에 있는 모든 머신. 허브도 포함한다. Guardian이 점검하고 조작하는 대상이다. API는 `/api/nodes`, `node_start`, `total_nodes`, 타입 `FleetNode`. | 서버, server, 머신, 장비 |
| 플릿 | fleet | 인벤토리의 모든 노드. 대시보드의 목적지 이름은 노드, 플릿 요약은 현황이다. | |
| 인벤토리 | inventory | 노드와 메타데이터의 목록(`servers.json`). | |
| 노드 그룹 | node group | 사용자가 정의한 노드 묶음(`docs/design/node-groups.ko.md`). | |
| 허브 | hub | 허브 에이전트, 대시보드, 데이터가 있는 단 하나의 노드. `guardian_role`은 `hub`. | |
| 허브 에이전트 | hub agent | 허브에서 도는 `apps/agent/`의 Go 프로그램. 다른 노드에는 에이전트가 없다. | 토론 에이전트를 뜻하는데 "에이전트"만 쓰기 |
| 워커 | worker | 참여를 선택한 노드에서 도는 상주형·무상태 모델 워커(`docs/design/remote-agents.ko.md`). | |
| 실행 호스트 | execution host | 토론 에이전트의 LLM CLI가 실행되는 머신. 허브이거나 워커 노드다. 점검 대상 노드가 아니다. | 노드와 같은 뜻의 "호스트" |
| 호스트 | host | "실행 호스트", SSH "호스트 키", "호스트 이름" 안에서만 쓴다. | 노드 |

노드의 `guardian_role`은 `hub` 또는 `node`다. 옛 값 `agent`는 인벤토리를 이전하기 전까지 `node`로 읽는다.

## 토론

| 한국어 | English | 뜻 | 쓰지 않는 말 |
|---|---|---|---|
| 토론 | debate | 팀이 하나의 과제를 한 번 수행하는 것. | 논의, 디베이트 |
| 토론 에이전트 | debate agent | 토론의 참가자. 역할은 아래와 같다. | "에이전트"만 쓰기 |
| 전문가 | expert | 초안을 쓰고 정리하는 토론 에이전트. | |
| 비평가 | critic | 초안을 검토하는 토론 에이전트. 한 팀에 한 명 이상의 패널이 있다. | 비판자, 크리틱 |
| 제안자 | proposer | 첫 라운드에서 초안을 더 쓰는 토론 에이전트(`proposers`). | |
| 판정자 | judge | 판정을 내리는 토론 에이전트. | 판사, 심판 |
| 투표 | vote | 계획에 대한 비평가의 명시적 찬성 또는 반대. | |
| 게이트 | gate | 계획이나 액션을 사람 없이 실행해도 되는지 가르는 검사(jev 게이트, 배치 게이트). | |
| 팀 | team | 페르소나와 흐름을 가진 토론 에이전트의 구성. | |
| 프로젝트 | project | 토론의 대상. `code`, `ops`, `design` 중 하나. | |
| 스레드 | thread | 한 과제에 대한 라운드의 연속. | 대화 |
| 흐름 | flow | 자리, 동시 묶음, 마감, 연결로 그린 일하는 방식(`docs/design/flow-graph.ko.md`). 팀과 템플릿도 흐름으로 보인다. | 워크플로, 플로우 |
| 자리 | seat | 흐름에서 캐릭터 하나가 맡은 일 하나를 하는 곳. | 노드(머신과 겹침) |
| 맡은 일 | job | 자리가 하는 일: 작성, 비평, 다시 쓰기, 반론, 정리. | |
| 동시 묶음 | bundle | 병렬로 도는 자리들. | 그룹(노드 그룹과 겹침) |
| 마감 | close | 흐름마다 하나 있는 공통 마감: 판정자, jev 게이트, 조치 검사. | 게이트(검사 이름과 겹침) |
| 연결 | link | 한 자리가 다른 자리의 결과를 읽는다는 표시. 이전 라운드를 읽으면 되돌아보기 연결. | 엣지 |
| 받는 방식 | input mode | 자리가 앞 결과를 받는 방식: 원문, 요약, 핵심만, 최신만. | |
| 정리자 | summariser | 받는 자리에 맞게 앞 결과를 줄이는 자리. | |
| 빠른 리뷰 | quick review | 리뷰어 하나가 읽기 전용 도구로 변경(`base..head` 또는 patch)을 검사한다. 라벨 `REVIEW`, 판정자 없음, 판정이 아니다(`docs/design/verification-layer.ko.md`). | 퀵 리뷰, quick check |
| 검증 계층 | verification layer | 코드 작업에서 Guardian의 역할: 작업을 만들지 않고 다른 곳에서 만든 작업을 검증한다. | ADE, 개발 환경 |

## 변경

| 한국어 | English | 뜻 | 쓰지 않는 말 |
|---|---|---|---|
| 액션 | action | 제안된, 타입이 있는 변경(`apply_patch`, `ops:docker_prune` 등). 판정자와 게이트를 거치고 필요하면 승인을 받는다. | 객체 이름으로서의 remediation |
| 승인 | approval | 액션에 대한 사람의 결정. | 허가, 결재 |
| 계획 | plan | 여러 노드에 걸친 액션의 순서 있는 목록(`plan_ops.py`). | |

한국어 문장에서는 명사 "조치" 대신 무슨 일이 일어나는지를 풀어 쓴다("서비스를 재시작해요", "패키지를 삭제해요").
객체의 이름은 "액션"이다.

## 내비게이션

대시보드의 다섯 목적지(2026-10-29 결정, `docs/design/ui-flow-redesign.ko.md`). 라우트 id는 새 `overview`를 빼고 그대로다.

| 한국어 | English | 뜻 | 라우트 |
|---|---|---|---|
| 현황 | Overview | 지금 주의가 필요한 것(예전 Fleet 요약). | `#/overview` |
| 노드 | Nodes | 인벤토리, 그룹, SSH 감사, 명령, 명령 세트. | `#/fleet` |
| 업무 흐름 | Workflows | 토론(팀의 한 번 실행)을 시작하고 읽는 곳. 한 번의 실행은 여전히 토론이다. | `#/debates` |
| 승인 | Approvals | 사람을 기다리는 액션. | `#/approvals` |
| 설정 | Settings | 허브 연결, 흐름 구성, 실행 환경, 화면. | `#/settings` |
| 흐름 구성 | flow setup | 프로젝트, 팀, 페르소나를 담은 설정 묶음. | `#/settings/flowConfig` |
| 실행 환경 | execution environments | 토론 에이전트를 돌리는 워커의 설정 묶음. | `#/settings/workers` |


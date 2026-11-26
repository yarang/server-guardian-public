# 사용 가이드

[English](README.md) | 한국어

Server Guardian을 배포하고 일상적으로 운영·사용하는 방법을 안내한다. 대시보드, CLI, 문서 전반에서 쓰는 용어는
[용어집](../glossary.ko.md)을 따른다. 전체 아키텍처와 상세 명세는 [README](../../README.ko.md)와
[agent.ko.md](../../agent.ko.md)에서 볼 수 있다.

## Server Guardian이 하는 일

Server Guardian은 **단 하나의 허브**(`guardian_role: hub`)에서 Linux 노드 플릿과 코드 저장소를 함께 관리한다.
다른 노드에는 기본적으로 에이전트를 두지 않으며, 허브가 전용 플릿 SSH 키로 접속해 점검과 작업을 수행한다.

- **플릿 관리**: 허브 메트릭 모니터링, 노드 도달성 점검, 동적 노드 그룹 관리, 노드별 SSH 보안 감사
  (`SEC-SSH-001`~`SEC-SSH-005`), 단발 명령 및 검증된 명령 세트 병렬 실행.
- **멀티 에이전트 토론과 흐름**: 설치된 LLM CLI(`claude`, `codex`, `agy`, `exaone`, `opencode`, `zai`)로
  **코드(`code`)**, **운영(`ops`)**, **설계(`design`)** 프로젝트에 대해 구조화된 토론(전문가, 비평가 패널, 판정자)이나
  단독 빠른 질문을 실행한다.
- **안전한 변경 통제**: 제안된 코드 수정, 노드 정리, 패키지 설치·삭제 계획, 일괄 스크립트는 모두 타입이 있는 액션
  검사, 게이트(`jev` 게이트 또는 비평가 투표 게이트), 그리고 명시적인 사람 승인을 거쳐 실행된다.
- **상주 에이전트와 운영자 세션**: Tier-1 노드에 이름을 가진 상주 에이전트를 두고, 내 워크스테이션의 Claude Code나
  Antigravity(`agy`) 대화형 세션을 MCP로 허브 메일망에 연결해 메시지를 주고받는다.

## 가이드 목차

| 문서 | 다루는 내용 | 추천 대상 |
|---|---|---|
| [설치와 배포](setup-and-deployment.ko.md) | 허브 배포와 롤백, 플릿 SSH 키 보호, LLM CLI 자격증명, Guardian CLI 스킬, 실행 환경(워커), 로컬 개발, CI 관문 | 허브를 처음 구축하거나 업그레이드하는 관리자 |
| [대시보드 사용법](dashboard.ko.md) | 로그인, 5개 주 메뉴(`현황`, `노드`, `업무 흐름`, `승인`, `설정`), 언어 전환, 오피스 내보내기(DOCX/PPTX/Mermaid) | 웹·데스크톱 대시보드 일상 사용자 |
| [플릿 관리와 운영 프로젝트](fleet-and-ops.ko.md) | 인벤토리, 노드 그룹, 원격 실행, SSH 감사, 운영(`ops`) 프로젝트, 정리 액션 카탈로그, 야간 감시, 프로비저닝 계획, 일괄 실행 | 인프라·노드 운영자 |
| [토론, 흐름, 프로젝트](debates-and-projects.ko.md) | `code`/`ops`/`design` 프로젝트, 토론과 흐름 실행, 빠른 질문, 팀·페르소나, 코드 위키, 저장소 전체 감사, 승인, 게시(Publish) | 개발자와 코드 리뷰어 |
| [상주 에이전트와 운영자 세션(MCP)](residents-and-sessions.ko.md) | Tier-1 상주 에이전트(닫힌 도구·샌드박스 열린 도구), 메일함과 작업 보드, 워크스테이션 세션 링크 데몬, Claude Code·Antigravity(`agy`) MCP와 훅 | 노드·워크스테이션 간 에이전트 협업 사용자 |

## 역할별 빠른 시작

### 처음 구축하는 관리자
1. [설치와 배포](setup-and-deployment.ko.md)를 따라 허브 릴리스를 배포하고 플릿 SSH 키와 LLM CLI 자격증명을
   구성한다.
2. `http://<허브 Tailscale IP>:9870/`으로 대시보드를 열고 `AGENT_TOKEN`으로 로그인한다([대시보드 사용법](dashboard.ko.md)).
3. **노드** 화면에서 플릿 도달성 점검과 SSH 감사를 실행한다([플릿 관리와 운영 프로젝트](fleet-and-ops.ko.md)).

### 저장소를 다루는 개발자
1. **설정 > 흐름 구성 > 프로젝트**에서 `code` 프로젝트를 선택하거나 새로 등록한다.
2. **업무 흐름** 화면(또는 `guardian.py --debate` / `--agent-run`)에서 토론이나 **빠른 질문**을 시작한다
   ([토론, 흐름, 프로젝트](debates-and-projects.ko.md)).
3. **승인** 화면에서 제안된 액션을 검토하고 테스트를 통과시킨 뒤 **게시(Publish)**로 새 브랜치에 푸시한다.

### 워크스테이션 CLI(Claude Code / Antigravity)를 연결하는 운영자
1. 허브에 워크스테이션을 등록(`guardian.py --session-add`)하고 `scripts/install-session-link.sh`로 링크 데몬을
   설치한다([상주 에이전트와 운영자 세션(MCP)](residents-and-sessions.ko.md)).
2. Claude Code 또는 Antigravity 설정에 `guardian-session` MCP 서버와 훅을 연결한다.
3. 세션에서 `session_link`로 `work@ws-mac-1` 같은 주소를 등록한 뒤 상주 에이전트, 다른 세션, `operator`와 메일을
   주고받는다.

## 관련 운영 런북

- [허브 백업과 복원](../operations/backup-restore.ko.md)
- [headscale 운영](../operations/headscale.ko.md)
- [Fleet sshd 하드닝 절차](../operations/2026-10-05-fleet-sshd-hardening.ko.md)

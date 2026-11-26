# 토론, 흐름, 프로젝트

[English](debates-and-projects.md) | 한국어

[사용 가이드](README.ko.md)로 돌아가기.

이 문서는 프로젝트 설정, 멀티 에이전트 토론과 흐름 실행, 빠른 질문, 코드 위키, 저장소 전체 감사, 액션 승인 및
브랜치 게시(Publish) 사용법을 정리한다.

## 1. 프로젝트 종류

모든 토론은 하나의 프로젝트에 속한다. 내장 정의는 `configs/projects/`에 있고, 대시보드나 CLI로 저장한 프로젝트는
허브의 `~/.guardian/data/projects/`에 저장되어 같은 ID의 내장 프로젝트를 덮어쓴다.

| 종류 | 대상 | 워크스페이스·위키 | 대표 액션 |
|---|---|---|---|
| `code` | Git 저장소(예: 내장 `server-guardian`, 사용자 서비스 저장소) | 허브 클론(`~/guardian-workspaces/<id>`), 심볼 인덱스, 코드 위키 | `apply_patch`, `execute_code_action`, `run_tests`, **게시(Publish)** |
| `ops` | 노드와 노드 그룹(예: 내장 `fleet`) | 저장소·위키 없음 | 정리 카탈로그(`ops:*`), 패키지 계획, 명령 세트, `server_remediation` |
| `design` | 설계 문서, 제안서, 다이어그램 | 저장소 없음, 버전 관리되는 Markdown **산출물** | 버전 문서, Mermaid 다이어그램, DOCX/PPTX 내보내기 |

프로젝트 관리 및 동기화(**설정 > 흐름 구성 > 프로젝트** 또는 CLI):

```bash
python3 cli/llm-agent/guardian.py --projects
python3 cli/llm-agent/guardian.py --project-sync all
python3 cli/llm-agent/guardian.py --project-sync my-service
```

비공개 Git 저장소는 허브에 Git 자격증명(예: 토큰 + `git config --global credential.helper store`)을 한 번 등록해
두고, 프로젝트 설정의 `repo` URL에는 자격증명을 넣지 않는다.

## 2. 토론과 흐름 시작하기

토론에서는 **전문가**가 초안을 쓰고 정리하며, **비평가** 패널(1~4명 병렬)이 초안을 검토하고, **판정자**가 최종
판정과 타입이 있는 액션을 제안한다.

### 내장 팀
- **코드**: `default`, `cross-review`, `guardian-dev`
- **운영**: `fleet-ops`, `fleet-provision`, `fleet-mesh`, `fleet-services`
- **설계**: `design-review`

### CLI에서 실행하기

```bash
# 코드 프로젝트에서 새 토론 시작
python3 cli/llm-agent/guardian.py --debate "session_daemon.py의 경쟁 조건을 찾아 수정해 줘" \
  --project server-guardian --team cross-review --context-path 'cli/llm-agent/session_*.py'

# 저장된 흐름 그래프를 사용하고 특정 자리의 캐릭터 교체
python3 cli/llm-agent/guardian.py --debate "API 인증 변경 사항 리뷰" \
  --project my-service --flow code-review --seat critic_1=security_critic

# 기존 스레드 이어가기
python3 cli/llm-agent/guardian.py --debate "두 번째 비평가 지적 사항을 반영해 줘" --thread <스레드ID>

# 대화형 REPL
python3 cli/llm-agent/guardian.py
```

### 흐름 편집기
**설정 > 흐름 구성 > 흐름**(`#/settings/flows`)의 캔버스에서 흐름을 시각적으로 설계·수정할 수 있다.
- **자리(seat)**: 캐릭터와 맡은 일(`작성`, `비평`, `다시 쓰기`, `반론`, `정리`) 지정.
- **동시 묶음(bundle)**: 여러 자리를 병렬로 실행.
- **연결(link)과 받는 방식**: 앞 단계 결과를 받는 방식(`원문`, `요약`, `핵심만`, `최신만`) 지정.
- **마감(close)**: 모든 흐름은 공통 마감(판정자, `jev` 게이트, 액션 검사)으로 끝난다.

CLI 조회:

```bash
python3 cli/llm-agent/guardian.py --teams
python3 cli/llm-agent/guardian.py --personas
python3 cli/llm-agent/guardian.py --flows
```

## 3. 빠른 질문 (단독 에이전트)

비평가 패널, 판정자, 액션 없이 단일 역할에게 빠르게 읽기 전용 답변을 얻고 싶을 때는 **빠른 질문**
(**업무 흐름 > 빠른 질문**, `#/debates/ask`)이나 `--agent-run`을 사용한다.

```bash
# 특정 프로젝트 맥락으로 읽기 전용 질문
python3 cli/llm-agent/guardian.py --agent-run "허브가 대시보드 요청을 어떻게 인증하나요?" \
  --project server-guardian --provider claude

# 일반 질문 (프로젝트 맥락 없음)
python3 cli/llm-agent/guardian.py --agent-run "systemd MemoryMax와 MemoryHigh 차이를 설명해 줘" \
  --general --provider claude
```

## 4. 증거, 코드 위키, 저장소 전체 감사

### 코드 읽기와 증거 검증
토론 에이전트는 ```` ```repo ```` 블록이나 읽기 전용 MCP 서버 `repo_mcp.py`로 저장소를 읽으며, 심볼 인덱스, 저장소
지도, Go 타입 사실(`tools/goindex`)을 길잡이로 활용한다. 지도와 위키는 길잡이일 뿐이며, 세션에 실제로 표시된 코드
줄만 인용 검증을 통과한다.

### 코드 위키
코드 위키는 대시보드에서 사람이 저장소를 탐색하기 위한 문서다(**업무 흐름 > 위키**, `#/debates/wiki`,
**읽기**·**파일**·**이력** 모드). 갱신은 수동으로 실행한다.

```bash
python3 cli/llm-agent/guardian.py --code-wiki-list --project server-guardian
python3 cli/llm-agent/guardian.py --code-wiki-build --project server-guardian --stale
python3 cli/llm-agent/guardian.py --code-wiki-build --project server-guardian --all
```

### 저장소 전체 감사 모드
저장소 전체를 샤드로 나누어 특정 속성 위반을 찾는 읽기 전용 감사 모드다(코드를 수정하지 않음).

```bash
python3 cli/llm-agent/guardian.py --repo-audit "인증 검사가 빠진 HTTP 핸들러를 모두 찾아 줘" \
  --project server-guardian --audit-scope 'apps/agent/*.go'
python3 cli/llm-agent/guardian.py --repo-audit-list --project server-guardian
python3 cli/llm-agent/guardian.py --repo-audit-run <감사ID> --project server-guardian
python3 cli/llm-agent/guardian.py --repo-audit-report <감사ID> --project server-guardian
```

## 5. 판정, `jev` 게이트, 액션, 게시(Publish)

1. **판정과 `jev` 게이트**: 판정자가 승인하면 토론 결과가 ADR로 `jev-mcp`에 제출되어 독립 심사를 받는다. `jev`
   게이트가 `APPROVED`를 통과해야 제안된 액션이 해제되며, 그렇지 않으면 `HELD` 상태로 남는다.
   ```bash
   python3 cli/llm-agent/guardian.py --jev-status [<스레드ID>]
   python3 cli/llm-agent/guardian.py --jev-recheck <스레드ID>
   ```
2. **코드 액션**:
   - 프로젝트 클론 안의 파일 수정(`apply_patch`, `execute_code_action`)은 `jev` 게이트 통과 후 자동 적용된다
     (`decided_by: auto:engine`, `GUARDIAN_AUTO_APPLY=off`로 끌 수 있음). 원격 저장소에는 절대 푸시하지 않는다.
   - `run_tests`와 `run_test`가 붙은 액션은 사람 승인을 기다린다.
   ```bash
   python3 cli/llm-agent/guardian.py --actions [<스레드ID>]
   python3 cli/llm-agent/guardian.py --show-action <액션ID>
   python3 cli/llm-agent/guardian.py --run-action <액션ID>
   python3 cli/llm-agent/guardian.py --reject-action <액션ID>
   ```
3. **게시(Publish, 유일한 지원 푸시 경로)**: 판정자 액션이 생성·수정한 파일만 골라 프로젝트의 `tests.default`를
   통과한 뒤 새 브랜치로 푸시한다(강제 푸시 없음).
   ```bash
   # 계획 확인 후 게시 ('yes' 입력 필요)
   python3 cli/llm-agent/guardian.py --publish-thread <스레드ID> [--branch fix/my-branch]

   # 게시 이력 조회 및 더티 워크스페이스 초기화
   python3 cli/llm-agent/guardian.py --publications <스레드ID>
   python3 cli/llm-agent/guardian.py --workspace-reset <프로젝트ID>
   ```
   게시된 브랜치를 메인 브랜치에 병합한 뒤 `--project-sync`로 허브 클론을 동기화한다.

4. **보고서 오피스 내보내기(CLI)**:
   ```bash
   # code/ops 토론 보고서를 산출물 버전으로 보관
   python3 cli/llm-agent/guardian.py --report-keep --thread <스레드ID>

   # 산출물 버전을 Word 또는 PowerPoint로 변환
   python3 cli/llm-agent/guardian.py --artifact-convert report.md --thread <스레드ID> \
     --artifact-format docx
   ```

## 6. 토론 CLI 세션 조회와 접속

토론 역할별 CLI 세션은 허브의 `agent_sessions` 테이블과 `~/.guardian/llm-sessions`에 보존되어 다음 라운드에서
이어진다.

```bash
# 기록된 세션 목록 조회
python3 cli/llm-agent/guardian.py --sessions [<스레드ID>]

# 특정 역할의 CLI 세션에 tmux로 접속
python3 cli/llm-agent/guardian.py --attach <스레드ID> --role critic

# 30일 넘은 세션 정리
python3 cli/llm-agent/guardian.py --prune-sessions 30
```

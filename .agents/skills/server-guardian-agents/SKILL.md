---
name: server-guardian-agents
description: >-
  Server Guardian 저장소를 위한 5개 전문 Antigravity 에이전트(go-agent, python-engine,
  web-dashboard, docs-sync, guardian-reviewer)를 정의하고 오케스트레이션하는 스킬.
  에이전트 설정, 멀티에이전트 작업 실행, Go/Python/React/문서 병렬 수정, 배포 전 코드 감사 요청 시 사용한다.
---

# Server Guardian 멀티에이전트 오케스트레이션 가이드

이 스킬은 **Server Guardian** 아키텍처에 맞춘 5개의 도메인 전문 에이전트(`.agents/agents/<이름>/agent.md`)를 조합해 작업을 수행하는 방법을 안내합니다.

| 에이전트 이름 | 역할 | 담당 영역 | 검증 명령 |
| :--- | :--- | :--- | :--- |
| `go-agent` | Go 허브 에이전트 전문가 | `apps/agent/`, `tools/goindex/` | `zsh -lic "scripts/ci.sh go"` |
| `python-engine` | Python 토론/CLI 엔진 전문가 | `cli/llm-agent/`, `scripts/` | `zsh -lic "scripts/ci.sh python config"` |
| `web-dashboard` | 웹 대시보드 & UI 전문가 | `apps/web-desktop/`, `packages/guardian-ui/` | `zsh -lic "scripts/ci.sh web"` |
| `docs-sync` | 한/영 문서 동기화 전문가 | `docs/`, `README*.md`, `agent*.md`, `CLAUDE.md` | `zsh -lic "scripts/ci.sh config"` |
| `guardian-reviewer` | 보안·규칙 감사 및 CI 게이트키퍼 | 저장소 전체 | `zsh -lic "scripts/ci.sh"` + `redact-private.py --check` |

## 1. 에이전트 정의 파일 위치

각 사용자 정의 에이전트는 `.agents/agents/<이름>/agent.md`에 한국어로 정의되어 있으며, 필요할 때 직접 열어 수정할 수 있습니다:
- [go-agent/agent.md](../../agents/go-agent/agent.md)
- [python-engine/agent.md](../../agents/python-engine/agent.md)
- [web-dashboard/agent.md](../../agents/web-dashboard/agent.md)
- [docs-sync/agent.md](../../agents/docs-sync/agent.md)
- [guardian-reviewer/agent.md](../../agents/guardian-reviewer/agent.md)
- 프롬프트 모음 레퍼런스: [references/subagent-prompts.md](./references/subagent-prompts.md)

## 2. 권장 멀티에이전트 워크플로우

### 풀스택 기능 구현 (예: `docs/design/`의 신규 단계 구현)
1. **백엔드 및 엔진 (병렬 또는 순차)**:
   - `python-engine`을 호출하여 `cli/llm-agent/`의 CLI 플래그, 엔진 로직, 단위 테스트(`testenv.isolate_data_dir()` 적용)를 구현합니다.
   - `go-agent`를 호출하여 `apps/agent/`의 `/api/` 엔드포인트를 `withSecurity(withAuth(mux))` 체인 뒤에 연결하고 핸들러 테스트를 추가합니다.
2. **프론트엔드 UI**:
   - `web-dashboard`를 호출하여 UI 화면/컴포넌트를 구현하고 `src/i18n/ko`와 `src/i18n/en` 카탈로그를 동시에 업데이트합니다.
3. **문서 및 구현 메모 동기화**:
   - `docs-sync`를 호출하여 `docs/design/<기능>.md`와 `<기능>.ko.md` 양쪽에 구현 메모를 기록하고 `CLAUDE.md`를 갱신합니다.
4. **최종 검증 게이트**:
   - `guardian-reviewer`를 호출하여 `git diff` 검토, `redact-private.py --check`, 전체 `scripts/ci.sh` 통과를 확인합니다.

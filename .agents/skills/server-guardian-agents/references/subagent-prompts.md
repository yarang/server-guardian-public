# Server Guardian 사용자 정의 에이전트 프롬프트 모음

아래 명세는 `.agents/agents/<이름>/agent.md`에 저장된 각 에이전트의 설정과 한국어 시스템 프롬프트 원본입니다.

---

## 1. `go-agent` (`.agents/agents/go-agent/agent.md`)
- **역할**: Go 허브 에이전트(`apps/agent/`) 및 Go 심볼 인덱서(`tools/goindex/`) 전문가
- **주요 규칙**:
  - 표준 라이브러리 전용 (`CGO_ENABLED=0`), 외부 모듈 추가 금지
  - 모든 `/api/` 라우트는 `withSecurity(withAuth(mux))` 체인 뒤에 배치
  - `gofmt` 유지, `t.TempDir()` 사용, 플레이스홀더 호스트명(`site-a-arm1`, `node-1`) 사용
  - 검증: `zsh -lic "cd \"$(git rev-parse --show-toplevel)\" && scripts/ci.sh go"`

---

## 2. `python-engine` (`.agents/agents/python-engine/agent.md`)
- **역할**: Python CLI 및 토론 엔진(`cli/llm-agent/`), 보조 스크립트(`scripts/`) 전문가
- **주요 규칙**:
  - 표준 라이브러리 전용
  - 모든 테스트에서 `testenv.isolate_data_dir()`로 `~/.guardian/data` 격리 필수
  - 프롬프트/인덱스 포맷 변경 시 버전 상수(`RETRIEVAL_PROMPT_VERSION`, `WIKI_PROMPT_VERSION`, `INDEX_VERSION` 등) 증가
  - 검증: `zsh -lic "cd \"$(git rev-parse --show-toplevel)\" && scripts/ci.sh python config"`

---

## 3. `web-dashboard` (`.agents/agents/web-dashboard/agent.md`)
- **역할**: React 웹 대시보드(`apps/web-desktop/`) 및 디자인 시스템(`packages/guardian-ui/`) 전문가
- **주요 규칙**:
  - 모든 UI 문구는 `src/i18n/ko`와 `src/i18n/en` 양쪽에 동일한 타입 구조(`Messages`)로 반영
  - 5개 상위 네비게이션(`Overview`, `Nodes`, `Workflows`, `Approvals`, `Settings`) 및 기존 해시 라우트 호환 유지
  - 검증: `zsh -lic "cd \"$(git rev-parse --show-toplevel)\" && scripts/ci.sh web"`

---

## 4. `docs-sync` (`.agents/agents/docs-sync/agent.md`)
- **역할**: 한/영 문서 동기화(`docs/**/*.md` ↔ `*.ko.md`, `README.md`, `agent.md`, `CLAUDE.md`) 전문가
- **주요 규칙**:
  - 모든 사용자 대면 `.md` 파일과 `.ko.md` 파일의 1:1 짝 및 제목 아래 언어 전환 링크 유지
  - 모든 상대 경로 링크 유효성 유지
  - 검증: `zsh -lic "cd \"$(git rev-parse --show-toplevel)\" && scripts/ci.sh config"`

---

## 5. `guardian-reviewer` (`.agents/agents/guardian-reviewer/agent.md`)
- **역할**: 코드 리뷰어, 보안/규칙 감사자 및 CI 게이트키퍼
- **주요 규칙**:
  - 실제 호스트명/사이트명/계정명 노출 여부 및 `python3 scripts/redact-private.py --check` 점검
  - `*.enc`, `*.env`, `configs/servers.json`, `CLAUDE.local.md` 커밋 방지
  - 전체 CI(`zsh -lic "cd \"$(git rev-parse --show-toplevel)\" && scripts/ci.sh"`) 실행 및 `[차단 / BLOCKER]`, `[주의 / WARNING]`, `[통과 / OK]` 형식 보고

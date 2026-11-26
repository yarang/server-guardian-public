# Server Guardian Antigravity 규칙

전체 프로젝트 컨텍스트, 아키텍처, 현재 단계(phase) 상태는 항상 저장소 루트의 `CLAUDE.md`와 `agent.ko.md`를 참고하세요.
사용자에게는 항상 **한국어**로 답변하고, 코드·코드 주석·커밋 메시지는 영어로 작성합니다.

## 절대 어기면 안 되는 핵심 규칙

1. **비밀정보 및 실제 호스트명 보호**:
   - `*.enc` 파일, `*.env`, `configs/servers.json`, `CLAUDE.local.md`는 절대 읽거나 이동·수정·커밋하지 마세요.
   - 코드, 테스트, 문서에 실제 호스트명, 사이트명, 계정명을 절대 적지 말고 중립적인 플레이스홀더(`site-a-arm1`, `node-1`, `hub-1`, `git.example.com`)만 사용하세요.
2. **운영 데이터 디렉터리 격리**:
   - 운영 상태 디렉터리인 `~/.guardian/data`에 테스트 데이터를 절대 쓰지 마세요.
   - Python 테스트는 반드시 `testenv.isolate_data_dir()`를 호출해야 하며, CLI 검증 시에는 임시 `GUARDIAN_DATA_DIR`와 `GUARDIAN_LLM_PROVIDERS=none`을 사용하세요.
3. **표준 라이브러리 전용**:
   - `apps/agent/`, `tools/goindex/` (Go) 및 `cli/llm-agent/` (Python)는 명시적 승인 없이 외부 패키지를 추가하지 마세요.
4. **한/영 양방향 동기화**:
   - 사용자 대면 문서(`README.md`, `agent.md`, `docs/**/*.md`)는 대응하는 `.ko.md` 문서와 항상 함께 갱신하고, 제목 바로 아래 상호 언어 링크를 유지하세요.
   - 대시보드 UI 문자열은 `apps/web-desktop/src/i18n/ko`와 `apps/web-desktop/src/i18n/en` 양쪽에 동일한 구조로 추가하세요.
5. **CI 검증**:
   - 수정한 영역에 맞춰 `zsh -lic "cd \"$(git rev-parse --show-toplevel)\" && scripts/ci.sh <섹션>"`(`go`, `python`, `web`, `shell`, `config`)을 실행하고, 커밋·배포 전에는 전체 `scripts/ci.sh`를 통과시키세요.

## 사용자 정의 전문 에이전트 (`.agents/agents/`)

여러 컴포넌트를 다루는 작업에서는 아래 5개의 전문 에이전트(`invoke_subagent` 또는 `agy --agent <이름>`)를 활용하세요:
- `go-agent`: Go 허브 에이전트 및 인덱서 (`apps/agent/`, `tools/goindex/`)
- `python-engine`: Python CLI 및 토론 엔진 (`cli/llm-agent/`, `scripts/`)
- `web-dashboard`: React 웹 대시보드 및 디자인 시스템 (`apps/web-desktop/`, `packages/guardian-ui/`)
- `docs-sync`: 한/영 문서 동기화 (`docs/**/*.md` ↔ `*.ko.md`, `README`, `agent.md`, `CLAUDE.md`)
- `guardian-reviewer`: 코드 리뷰, 호스트명 마스킹 검사(`scripts/redact-private.py --check`), 전체 `scripts/ci.sh` 게이트 검증

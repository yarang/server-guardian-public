# 상주 에이전트 다중 활성 세션 관리 (설계)

[English](resident-multi-session.md) | 한국어

상태: 2026-11-23 확정 (MD1–MD5 운영자 승인 완료; MS1–MS4 단계 구현 완료).

이 문서는 [resident-agents.ko.md](resident-agents.ko.md)와
[resident-session-query.ko.md](resident-session-query.ko.md)를 확장하여, **단일 상주 에이전트(`<name>@<node>`)
안에서 여러 개의 격리된 활성 프로바이더 세션**(예: 프로젝트별·주제별 세션)을 안전하게 유지하고 선택적으로
**읽기 전용 분기 질의(`mode: "fork"`)**를 실행하는 구조를 설계한다. 유닉스 샌드박스 계정이나 자격증명을
중복 생성하지 않으며, 워커 노드의 단일 상주 슬롯(D5)과 격리 불변식을 그대로 지킨다.

## 1. 문제와 배경

현재 상주 에이전트는 에포크(`epoch`)당 **단 하나의 활성 프로바이더 세션**(`agents.session_id`,
`incarnations.session_id`, 노드 `state.json`)만 유지한다.

1. **프로젝트 간 문맥 오염.** 같은 상주 에이전트(`codex@node-1`)에 `server-guardian` 질의와 `fleet`
   운영 질의를 번갈아 보내면 두 프로젝트의 대화가 하나의 스레드에 뒤섞여 컨텍스트 오염과 토큰 낭비가 생긴다.
2. **`--tools open` 에이전트 복제의 비효율.** 프로젝트마다 `codex-sg`, `codex-ops`, `codex-design`을
   별도 에이전트로 등록하면 `/home/gsa-<name>` 유닉스 계정, 프로바이더 자격증명, 시작 카드 턴, 바인딩
   감시가 에이전트 수만큼 선형으로 늘어난다.
3. **1회성 비파괴 검토 경로 부재.** [resident-session-query.ko.md](resident-session-query.ko.md)에
   `session_queries.mode` 컬럼만 예약되어 있고 `"in_place"`만 구현되어 있어, 모든 검토 질의가 본 활성
   세션 이력을 변경한다.

## 2. 프로바이더 CLI 실측 역량

Tier-1 워커 노드의 실제 CLI(`claude`, `agy`, `codex`)를 확인한 결과는 다음과 같다.

| 프로바이더 | 단일 홈 내 다중 독립 세션 유지 | 네이티브 읽기 전용 분기 (`mode: "fork"`) | 비고 |
|---|---|---|---|
| `claude` | **지원** (`--session-id <uuid>` / `--resume <uuid>`) | **지원** (`--resume <uuid> --fork-session --no-session-persistence`) | UUID 선할당; `--fork-session`으로 원본 세션 변경 없이 분기 실행 가능 |
| `agy` | **지원** (새 실행 시 새 `conversation_id` 발급, `--conversation <id>`로 임의 세션 재개) | **미지원** | CLI 분기 플래그 없음 → `fork_unsupported`로 fail-closed 거절 |
| `codex` | **지원** (`codex exec`로 새 `thread_id` 발급, `codex exec resume <tid>`로 임의 스레드 재개) | **미지원** | CLI 분기 플래그 없음 → `fork_unsupported`로 fail-closed 거절 |

**세 프로바이더 모두** 단일 에이전트 홈·샌드박스 안에서 여러 독립 세션 ID를 네이티브로 유지할 수 있으므로,
**스코프 기반 세션 풀(Scoped Session Pool)**은 `claude`·`agy`·`codex`(`closed`·`open` 모두)에서
동일하게 동작한다. `mode: "fork"`는 `configs/resident-providers.json`의 능력 플래그로 제어해 `claude`에서만
활성화하고 미지원 프로바이더는 침묵 폴백 없이 거절한다.

## 3. 아키텍처: 스코프 세션 풀 + 선택적 분기(`fork`)

### 3.1 세션 키 (`session_key`)

하나의 incarnation `(address, epoch)` 아래에서 각 활성 세션은 검증된 `session_key`로 구분한다.

- **`main`**: 기본 메일함·작업판 세션(기존 `incarnations.session_id`와 100% 하위 호환). 바인딩 시 시작 카드
  턴이 자동 생성한다.
- **`project:<project_id>`**: 허브에 등록된 특정 프로젝트에 고정된 지속형 세션(예: `project:server-guardian`,
  `project:fleet`).
- **`project:<project_id>:<label>`** (선택, MD2): 같은 프로젝트 안에서 주제별로 나눈 명명 세션(예:
  `project:server-guardian:review`, `project:server-guardian:debug`; `label`은 `^[a-z0-9][a-z0-9-]{0,23}$`).

검증 규칙(허브 fail-closed):
- `project:<id>` 또는 `project:<id>:<label>` 키는 반드시 질의의 `project` 인자와 일치해야 한다(불일치 시
  `session_key_project_mismatch` 거절).
- 메일함 깨움과 작업 턴은 항상 `session_key = "main"`에서 실행되어 기존 메일·작업 문맥 연속성을 지킨다.

### 3.2 수명주기: 지연 생성, 리셋, 아카이브, 에포크 펜싱

1. **첫 질의 시 지연 생성(Lazy Create).** `main`과 달리 스코프 세션은 미리 토큰을 쓰지 않고 첫
   `resident_query`가 들어올 때 생성한다. 첫 턴에서 드라이버가 시작 카드 + 프로젝트 질의 헤더로 새 프로바이더
   세션을 열고, 반환된 `provider_session_id`를 기록해 해당 `session_key`의 `session_ready = 1`을 세운다.
   - 사전 조건: `agents.state == "ready"`이고 `main` 바인딩이 완료(`incarnations.session_ready == 1`)되어
     에이전트·샌드박스 정상 동작이 확인된 상태에서만 스코프 세션 지연 생성을 허용한다.
2. **명시적 리셋 / 아카이브.** 운영자는 `main`이나 다른 프로젝트 세션을 건드리지 않고 특정 스코프 세션만
   `archived`로 닫고 노드의 로컬 매핑을 비워 새 문맥으로 다시 시작할 수 있다.
3. **개수 상한 및 유휴 자동 정리.**
   - `(address, epoch)`당 활성 세션 상한: **`MAX_SESSIONS_PER_AGENT = 8`**(`main` 포함). 상한 도달 시
     `session_limit_reached`로 거절하며, 불필요한 세션을 리셋/아카이브하면 새 세션을 열 수 있다.
   - **14일간 미사용**(MD4)된 스코프 세션은 허브 스윕에서 자동으로 `archived` 처리한다(`main`은 incarnation이
     열려 있는 동안 자동 아카이브하지 않는다).
4. **에포크 펜싱과 장애 격리.**
   - 에이전트가 새 `epoch`로 재바인딩(크래시·은퇴·`main` 유실)되면 이전 `epoch`의 모든 `agent_sessions` 행은
     `closed`(`epoch_changed`)로 마감되고 대기 질의도 `epoch_changed`로 종료한다.
   - 반대로 **특정 스코프 세션 하나만** `missing_session` 또는 `resume_new_session`을 겪으면, 그 스코프
     세션만 `lost` 처리하고 해당 질의를 `session_lost`로 끝낸다 — **`main`과 다른 스코프 세션은 유지한다**.

### 3.3 동시성·샌드박스 불변식 (변경 없음)

- **단일 상주 슬롯(D5) 유지.** 세션은 디스크에 여러 개 *보관*되지만, 워커 노드에서 **동시에 실행되는 턴은
  항상 1개**(에이전트당 단일 `turn_thread`)로 직렬화된다. CLI 세션 파일 동시 손상을 원천 차단한다.
- **샌드박스 계정 중복 없음.** `--tools open` 에이전트의 모든 스코프 세션은 기존 `gsa-<name>` 계정과
  `guardian-sandbox-run` 유닛을 공유하며, 기존 `_open_pre_turn` / `_pre_turn_checks`를 그대로 거친다.
- **예산은 에이전트 단위 유지.** 직통 질의 시간당 예산(20회/시간, QD4)과 대기열 상한(5건)은 세션 수가 늘어도
  **에이전트당 총량**으로 유지해 예산 우회를 막는다.

### 3.4 선택적 읽기 전용 분기 질의 (`mode: "fork"`)

호출자가 이미 `session_ready == 1`인 세션(`main` 또는 `project:<id>`)을 대상으로 `mode: "fork"`를 요청하면:

1. 허브가 `configs/resident-providers.json`의 `supports_fork`를 확인한다(`claude`만 `true`). 미지원 프로바이더는
   즉시 `fork_unsupported`(HTTP 400)로 거절한다.
2. 대상 `session_key`가 이미 생성되어 있어야 한다(`session_ready == 1`).
3. 노드 드라이버가 `claude --resume <source_session_id> --fork-session --no-session-persistence`로 실행한다.
4. 원본 `provider_session_id`를 변경하지 않고 분기 응답 텍스트만 `query_reply`로 반환한다.

## 4. 데이터 모델 (`mail.db` 스키마 7 → 8)

`cli/llm-agent/mail_store.py`에 추가형 마이그레이션으로 반영한다.

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
    address TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    session_key TEXT NOT NULL,               -- 'main' | 'project:<id>' | 'project:<id>:<label>'
    project TEXT,                            -- 'main'은 NULL, 스코프 세션은 프로젝트 ID
    label TEXT,                              -- 선택 하위 라벨
    provider_session_id TEXT,                -- 프로바이더 session/conversation/thread ID
    session_ready INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- active | archived | lost | closed
    turns INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_used TEXT,
    closed TEXT,
    PRIMARY KEY(address, epoch, session_key),
    FOREIGN KEY(address) REFERENCES agents(address)
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_active
    ON agent_sessions(address, epoch, status);

ALTER TABLE session_queries
    ADD COLUMN session_key TEXT NOT NULL DEFAULT 'main';
```

노드 로컬 상태(`<agent_dir>/state.json`):
- 업그레이드 호환을 위해 최상위 `session_id` / `first_turn`은 `sessions["main"]`과 동기화한다.
- `sessions: { "<session_key>": { "session_id": "...", "first_turn": false, "last_used": "..." } }` 맵을 추가한다.

## 5. 프로토콜과 인터페이스 확장

### 릴레이 프로토콜 (워커 프로토콜 3 하위 호환 확장)
- `hello`에 `"multi_session": true`(및 `claude` 지원 시 `"fork_query": true`)를 추가한다. 미지원 노드에
  `main` 외 `session_key`를 보내면 허브가 `node_without_multi_session`으로 거절한다.
- **허브 → 노드:**
  - `query`: `session_key`(기본 `"main"`), `create_if_missing`(bool), `mode`(`"in_place"` | `"fork"`) 추가.
  - `session_reset`: `{agent, epoch, session_key}` — 유휴 상태인 특정 스코프 세션의 로컬 매핑 제거(실행 중 턴은
    중단하지 않음).
- **노드 → 허브:**
  - `admit`: `kind == "query"` 시 `session_key` 포함.
  - `turn_end`: `session_key`와 갱신된 `session_id`를 함께 전달해 `agent_sessions`를 갱신(`session_key == "main"`일
    때만 `incarnations.session_id` 갱신).

### CLI · Go API · 대시보드 · 워크스테이션 MCP
- **허브 CLI (`guardian.py`)**:
  - `--resident-sessions <address> [--json]` — 에이전트의 활성/아카이브 세션 목록 조회
  - `--resident-session-reset <address> --session-key <key>` — 특정 스코프 세션 아카이브·초기화
  - `--resident-query <address> --project <id> --prompt ... [--session-key <key>] [--mode in_place|fork]`
- **허브 Go API (`apps/agent/debate_handler.go`)**:
  - `GET /api/residents/sessions?agent=<addr>&project=<id>` (`mode=ro`)
  - `POST /api/residents/sessions/reset` (`{address, session_key}`)
  - `POST /api/residents/queries`에 선택 필드 `session_key`, `mode` 추가
- **대시보드 (`SessionQueryPanel.tsx`)**:
  - 현재 선택한 허브 프로젝트 기준 세션 선택기(`project:<id>`, 라벨 세션, `main`), 턴 수/상태 표시, 스코프 세션
    리셋 버튼 추가
  - 채팅 대화를 `(agent, project, session_key)` 단위로 필터링
- **워크스테이션 MCP (`guardian-session`)**:
  - `resident_query`에 선택 인자 `session_key`, `mode` 추가(`session_local.py` 검증 → `session_uplink.py` →
    `session_link.py`)

## 6. 보안 불변식

| 위험 | 완화책 |
|---|---|
| 타 프로젝트 세션 가로채기 (`project=A`, `session_key=project:B`) | 허브가 `project`와 `session_key`의 프로젝트 접두사 일치를 fail-closed 검증 |
| 다중 세션을 통한 예산 우회 | 시간당 질의 예산(20회/시간)과 대기열 상한(5건)은 세션별이 아닌 **에이전트당 총량**으로 고정 |
| 워커 노드 세션 파일 무한 증가 | `MAX_SESSIONS_PER_AGENT = 8` 상한 + 14일 유휴 자동 아카이브 |
| 스코프 세션 손실이 `main` 메일 루프를 흔듦 | 스코프 세션 `missing_session` 시 해당 `session_key`만 `lost` 처리하고 `main`은 유지 |
| 미지원 프로바이더에서 `mode: "fork"`가 `in_place`로 조용히 폴백 | 허브·노드 이중 능력 검사, 미지원 시 `fork_unsupported`로 즉시 거절 |
| 동시 resume으로 인한 세션 저장소 손상 | D5 단일 상주 슬롯 및 에이전트당 단일 `turn_thread` 그대로 유지 |

## 7. 단계별 구현 계획

| 단계 | 범위 | 완료 기준 |
|---|---|---|
| **MS1 — 저장소·정책·CLI** | `mail.db` 스키마 8(`agent_sessions`, `session_queries.session_key`), 지연 생성 수락, 리셋·아카이브, 상한 8, 에포크 펜싱, `guardian.py --resident-sessions*` | 프로젝트 불일치 거절, 상한 8, 유휴 아카이브, 스코프 `session_lost` 격리, `mode=ro` 조회 단위 테스트 |
| **MS2 — 릴레이·노드 드라이버** | `mail_relay.py`, `resident_node.py`, `resident_driver.py` 다중 `session_key` 상태 맵, 지연 생성, `session_reset`, (MD3 승인 시) `claude` `mode: "fork"` | `main` + 2개 프로젝트 세션 전환, 스코프 유실 시 `main` 보존, `fork` 능력 게이트 socketpair E2E 테스트 |
| **MS3 — Go API·모니터·대시보드** | `debate_handler.go`, `resident_monitor.py`, `SessionQueryPanel.tsx` 세션 선택기·리셋·다국어(`ko`/`en`) | `go test`, `scripts/ci.sh web`, 390px 반응형 검증 |
| **MS4 — 워크스테이션 MCP·문서** | `session_local.py`, `session_daemon.py`, `session_mcp.py`, `session_link.py`, `mail_sessions.py`(`session_key`/`mode`) + 한/영 사용 가이드 | stdlib-only 가드 + 워크스테이션→상주 멀티 세션 실전 스모크 테스트 |

## 8. 운영자 결정 필요 항목 (MD1–MD5)

| # | 결정 항목 | 선택지 | 권장안 |
|---|---|---|---|
| **MD1** | `resident_query`에서 `session_key` 생략 시 기본값 | **(A)** `project:<id>` (프로젝트별 자동 분리) vs. **(B)** `main` (기존 메일함 세션 공유) | **(A) `project:<id>`** — 기본적으로 프로젝트 간 문맥 오염을 방지하고, 필요 시 명시적으로 `session_key="main"` 지정 |
| **MD2** | 프로젝트 내 명명 하위 세션(`project:<id>:<label>`) 허용 여부 | **(A)** 허용(`project:<id>` + 선택 `:label`) vs. **(B)** 프로젝트당 1개(`project:<id>`만) | **(A)** — 상한 8개 안에서 같은 프로젝트의 리뷰·디버깅 대화를 분리 가능 |
| **MD3** | `mode: "fork"` 포함 범위 | **(A)** MS2에 `claude` 전용 `--fork-session --no-session-persistence` 포함(`agy`/`codex`는 `fork_unsupported`) vs. **(B)** MS1–MS4 이후 별도 후속 | **(A)** — `claude` 구현 비용이 작고 비파괴 검토에 즉시 유용 |
| **MD4** | 세션 개수 상한 및 유휴 정리 기간 | 에이전트당 최대 활성 세션 수 / 유휴 아카이브 기준 | **최대 8개(`main` 포함) / 14일 미사용 시 자동 아카이브** |
| **MD5** | 직전 상주 에이전트 리뷰 개선안(채팅 스크롤 튐 방지, 탭 복귀 즉시 폴링, 취소 동기화, MCP `done` 플래그) 반영 시점 | **(A)** MS3/MS4에 함께 반영 vs. **(B)** MS1 전에 먼저 반영 | **(A)** 또는 **(B)** 운영자 선호에 따름 |

# 상주 에이전트 활성 세션 직접 질의 (설계)

[English](resident-session-query.md) | 한국어

상태: 2026-11-22 결정(QD1–QD5 운영자 확정; Q1–Q4 단계 구현 완료).

이 문서는 [resident-agents.ko.md](resident-agents.ko.md), [resident-open-tools.ko.md](resident-open-tools.ko.md),
[resident-codex-and-policy.ko.md](resident-codex-and-policy.ko.md)에 따라 동작 중인 상주 에이전트가 생성해
유지하고 있는 프로바이더 세션(`claude` `session_id`, `agy` `conversation_id`, `codex` `thread_id`)에 허브를
거쳐 안전하게 접근하고 직접 질의하는 경로를 설계한다.

## 1. 문제와 현재 구조의 빈 곳

Tier-1 워커 노드(`node-1` 등)의 상주 에이전트는 `HeadlessDriver`(`cli/llm-agent/resident_driver.py`) 아래에서
턴을 넘어 유지되는 프로바이더 세션(`agents.session_id`, `incarnations.session_id`, 노드 `state.json`)을 사용한다.
그러나 현재는 네 가지 이유로 이 활성 세션에 직접 접근해 질의할 수 없다.

1. **메일함 간접 경로만 존재한다.** 외부에서는 `--mail-send`나 `session_send`로 비동기 메일을 넣고, 에이전트가
   깨어나 `mail_inbox`를 읽은 뒤 `mail_send`로 답장하기를 기다려야 한다.
2. **모델 직출력이 버려진다.** `cli/llm-agent/resident_driver.py`의 `_claude_result`, `_agy_result`,
   `_codex_result`가 어시스턴트 응답 텍스트(`_text`)를 파싱하고도 그대로 폐기한다.
3. **노드 직접 `resume`은 동시성과 격리를 깨뜨린다.**
   - `HeadlessDriver`가 메일 깨움으로 같은 `session_id`를 실행하는 도중 외부에서 동시에 resume하면 CLI 세션
     저장소가 손상되거나 `resume_new_session` 충돌이 나며, 단일 상주 슬롯(D5)·턴 예산·`GUARDIAN_RESIDENT=off`를
     우회하게 된다.
   - `tools: "open"` 에이전트는 세션과 자격증명이 `0700` `/home/gsa-<name>`과 `guardian-sandbox-run` systemd
     유닛 안에 격리되어 있고, `tools: "closed"` 에이전트는 격리 홈(`hooks.json`)과 `--tools ""`·`--strict-mcp-config`
     봉쇄에 의존한다. 바깥에서 직접 실행하면 보안 경계가 무너진다.
   - 성형(star) 토폴로지 원칙상 워크스테이션과 대시보드는 Tier-1 노드에 직접 SSH하지 않는다.
4. **활성 세션 존재 여부 판별 함정.** `claude`는 `HeadlessDriver._bind()`에서 첫 턴이 실제로 디스크에 세션을
   만들기 전에 UUID `session_id`를 선할당(`first_turn = True`)한다. 따라서 `agents.session_id IS NOT NULL`만으로는
   "실제로 생성되어 사용 중인 세션"을 판별할 수 없다.

## 2. 채택 구조: 허브 중개 인플레이스(`in_place`) 세션 질의

세 가지 접근을 비교했다.

- **허브 중개 직통 `in_place` 질의 (채택, QD2):** 허브가 프로젝트 범위의 질의를 `mail.db`에 기록하고, 기존 워커
  프로토콜 3 SSH 릴레이 스트림(`mail_relay.py` ↔ `resident_node.py`)으로 노드에 전달한다. 노드의
  `HeadlessDriver`가 **기존 상주 슬롯(D5), 에포크 울타리, `closed` 봉쇄 또는 `open` 샌드박스 경로 그대로**
  활성 세션을 resume(`--resume` / `--conversation` / `codex exec resume`)해 질의를 실행하고, 모델의 직접 응답
  텍스트를 `turn_end`로 반환한다.
- **읽기 전용 분기 질의 (`mode: "fork"`, 보류):** 본 세션 문맥을 오염시키지 않고 분기 세션(예: Claude
  `--fork-session`)으로 1회 질의 후 폐기하는 방식. `agy`/`codex` 지원이 미검증이므로 스키마에 `mode` 컬럼만
  예약하고 이번 설계에서는 `"in_place"`만 구현한다.
- **대화형 터미널 직접 부착 (`tmux attach`, 비목표):** `HeadlessDriver` 일시정지 락이 필요하고 성형 구조에
  어긋나며, `tools: "open"` 샌드박스 내부로 TTY를 노출해 공격면을 넓힌다.

### 전체 실행 흐름 (20초 릴레이 계약에 맞춘 2단계 프로토콜)

릴레이 프레임은 `REQUEST_TIMEOUT = 20.0`초(`mail_relay.py`, `resident_node.py`)를 적용하는 반면, LLM 턴은
수십~수백 초가 걸린다. 따라서 단일 RPC로 답을 기다리지 않고 **접수/즉시 ACK(1초 미만)**와 **턴 실행/완료
(`turn_end` 결과 반환)**의 2단계로 분리한다.

```text
호출자 (대시보드 프로젝트 화면 / 허브 CLI --project <id>)
        |  POST /api/residents/queries  (또는 guardian.py --resident-query --project <id>)
   허브 (mail.db WAL: session_queries, status='queued', 등록 프로젝트 검증)
        |  202 Accepted {id, address, project, epoch, session_id, status}
   허브 mail_relay.py (2초 폴링, 기존 워커 프로토콜 3 SSH 스트림)
        |  허브 -> 노드: {"t": "query", "id": "hN", "agent": ..., "epoch": E,
        |                "query_id": "q-...", "expected_session_id": sid,
        |                "project": pid, "project_kind": kind, "task": tid?,
        |                "caller": "operator", "caller_kind": "operator",
        |                "mode": "in_place", "prompt": "...", "timeout_sec": 180}
        |  노드 -> 허브: {"t": "reply", "re": "hN", "ok": true, "result": "accepted"|"duplicate"}
   노드 ResidentManager -> HeadlessDriver
        |  1) admit(kind="query", query_id) + 노드 단일 상주 슬롯(D5) 점유
        |  2) _open_pre_turn / _pre_turn_checks (메일 턴과 동일, 대체 실행 없음)
        |  3) CliRunner.run(ctx, wrapped_prompt) -> TurnResult(ok, session_id, text)
        |  4) mask_secrets + 64 KiB 절단; 스트림 단절 시 pending_acks에 보관
        |  5) turn_end(end_reason, session_id, query_id, query_reply={status, answer, truncated, error})
   허브 mail_relay.py -> mail_store.py (BEGIN IMMEDIATE)
        |  이중 mask_secrets, session_queries(completed|failed) 갱신, turn 종료
   호출자 GET /api/residents/queries/{id} 폴링 (SQLite mode=ro)
```

## 3. 허브 프로젝트 기반 접근 모델 (QD1)

상주 세션 직접 질의는 **허브가 생성·등록한 프로젝트**(`cli/llm-agent/project_registry.py`의 `code`, `ops`,
`design`)를 기반으로만 진행한다.

1. **필수 `project` 지정.** 모든 질의는 `ProjectManager`에 등록된 유효한 `project` ID를 필수로 지정해야 한다.
   프로젝트가 없거나 미등록 ID면 허브 진입점에서 즉시 거절(`unknown_project`, HTTP 400)한다. 프로젝트 없는
   임의 전역 질의는 허용하지 않는다.
2. **프로젝트별 허용 에이전트 목록(선택 정책 키).** `project_registry.py`의 `POLICY_KEYS`에 선택 항목
   `resident_query_agents`(상주 에이전트 주소 목록)를 추가한다.
   - 생략(`None`): 해당 프로젝트에서 허브에 선언된 모든 상주 에이전트에 질의 가능.
   - 명시(`["claude@node-1", "coder@node-1"]`): 지정된 에이전트에만 질의 가능.
   - 빈 목록(`[]`): 해당 프로젝트에서는 상주 세션 직접 질의를 비활성화.
3. **허용 호출 주체.**
   - **Q1–Q3 단계:** 허브 프로젝트 컨텍스트의 운영자(`operator`, 대시보드 `AGENT_TOKEN` 또는 허브 CLI
     `--project <id>`).
   - **상주 에이전트 간 직접 질의 금지:** 에이전트끼리는 기존 메일함과 작업판만 사용한다(홉 제한 유지, 모델 간
     핑퐁 방지).
   - **워크스테이션 운영자 세션(선택 후속 단계 Q4):** 추후 `session_mcp.py`로 확장하더라도 등록된 허브
     `project` ID 지정, `peers` 및 `resident_query_agents` 통과, `caller_kind = "session"` 권한 미부여 경고
     헤더를 필수로 요구한다.
4. **선택 작업 참조(`task`).** `code` 프로젝트에서는 같은 프로젝트에 속한 작업 ID를 선택 인자로 연결해, 질의
   헤더에 작업 컨텍스트를 명시할 수 있다.

## 4. 활성 세션 존재 조건 (Fail-Closed)

대상 상주 에이전트가 현재 에포크에서 **실제로 세션을 생성해 사용 중일 때만** 질의를 수락한다.

- **허브 (`mail_store.py`):**
  - `incarnations` 테이블에 `session_ready INTEGER NOT NULL DEFAULT 0` 컬럼을 추가한다.
  - 열린 incarnation에서 `end_reason = 'normal'`인 턴이 끝나고 비어 있지 않은 `session_id`가 확정될 때만
    `session_ready = 1`로 올린다.
  - `agents.state == 'ready'` **AND** `agents.session_id IS NOT NULL` **AND** 현재 incarnation의
    `session_ready == 1`이 모두 참일 때만 질의를 생성한다(불충족 시 `no_active_session` 또는 `agent_not_ready`
    거절).
- **노드 (`resident_node.py`, `resident_driver.py`):**
  - `agent.ready`, `agent.epoch == frame.epoch`, `state.session_id == expected_session_id`, `not state.first_turn`,
    `not state.fresh`를 모두 확인한다.
  - 실행 중 `missing_session`이나 `resume_new_session`이 발생하면 기존 재바인딩(`force_fresh = True`)을 수행하고,
    빈 세션으로 답하는 대신 질의를 `failed(error="session_lost")`로 종료한다.

## 5. 데이터 모델 (`mail.db` 스키마 6 → 7)

`cli/llm-agent/mail_store.py`에 추가형 마이그레이션으로 반영한다. `resident_monitor.py`는 `PRAGMA table_info`
교집합으로 읽으므로 구버전·신버전 저장소와 모두 호환된다.

```sql
ALTER TABLE incarnations ADD COLUMN session_ready INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS session_queries (
    id TEXT PRIMARY KEY,                     -- q-YYYYMMDD-NNNN
    address TEXT NOT NULL,                   -- 예: claude@node-1
    project TEXT NOT NULL,                   -- 허브 프로젝트 ID (code|ops|design)
    project_kind TEXT NOT NULL,              -- 제출 시점 프로젝트 종류
    task TEXT,                               -- 선택: 동일 프로젝트의 작업 ID
    epoch INTEGER NOT NULL,                  -- 바인딩 에포크 펜싱
    session_id TEXT NOT NULL,                -- 대상 프로바이더 세션 ID
    caller TEXT NOT NULL,                    -- 'operator' (Q4에서 '<name>@<workstation>')
    caller_kind TEXT NOT NULL,               -- 'operator' | 'session'
    mode TEXT NOT NULL DEFAULT 'in_place',   -- 'in_place'
    prompt TEXT NOT NULL,                    -- 최대 32 KiB
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|dispatched|running|completed|failed|expired|cancelled
    turn_id TEXT,                            -- 연결된 turns.id
    answer TEXT,                             -- mask_secrets 적용, 최대 64 KiB
    answer_truncated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    timeout_sec INTEGER NOT NULL DEFAULT 180,
    created TEXT NOT NULL,
    started TEXT,
    completed TEXT,
    expires TEXT NOT NULL,                   -- 기본 created + 10분 (미실행 만료)
    FOREIGN KEY(address) REFERENCES agents(address),
    FOREIGN KEY(turn_id) REFERENCES turns(id)
);
CREATE INDEX IF NOT EXISTS idx_session_queries_dispatch
    ON session_queries(address, status, created);
```

- `TURN_KINDS`에 `"query"` 추가 (`turns.kind = 'query'`, `hop_in = 0`).
- **상태 전이 에포크 펜싱:** `bind_begin`으로 에포크가 바뀌거나 에이전트가 은퇴(`retiring`)하면 이전 에포크의
  `queued`/`dispatched` 질의는 `failed(error="epoch_changed")` 또는 `failed(error="agent_retired")`로 마감한다.
- **보관 정책(QD5):** 메일·작업(D7)과 동일하게 **30일 보관 + 야간 백업(`mailbox/mail.db`) 포함**.

## 6. 릴레이 프로토콜과 노드 드라이버 세부 규칙

### 릴레이 계약 (워커 프로토콜 3 하위 호환)
- `hello`에 `"session_query": true` 능력을 추가한다. 미지원 노드는 허브가 `node_without_session_query`로 거절한다.
- **허브 → 노드:**
  - `query`: 검증 후 `<agent_dir>/queries/<query_id>.json`(`0600`)에 저장하고 즉시 `accepted`/`duplicate` 또는
    오류(`no_active_session`, `epoch_mismatch`, `not_ready`, `queue_full`)를 회신한다.
  - `query_cancel`: 아직 실행 전인 `query_id`를 로컬 큐에서 제거한다(멱등).
- **노드 → 허브:**
  - `admit`: `kind == "query"`일 때 `query_id`를 함께 보낸다. 허브는 트랜잭션 안에서 질의 상태(`queued`/`dispatched`),
    에포크 일치, `GUARDIAN_RESIDENT != off`, 질의 시간당 예산을 확인하고 `status = 'running'`과 `turn_id`를 기록한다.
  - `turn_end`: 선택 필드 `query_id`와 `query_reply = {status, answer, truncated, error}`를 함께 보낸다.
  - **재연결 복구:** 질의 턴 도중 SSH 스트림이 끊겨도 기존 `pending_acks.json` 항목에 `query_id`·`query_reply`를
    함께 저장해 재연결 `sync` 핸드셰이크에서 답변이 유실되지 않게 한다.

### 드라이버 규칙 (`cli/llm-agent/resident_driver.py`)
1. **응답 텍스트 수집:** `TurnResult`에 `text: str = ""`를 추가하고, `_claude_result`·`_agy_result`·`_codex_result`에서
   이미 파싱한 어시스턴트 텍스트를 버리지 않고 채워 돌려준다.
2. **우선순위 스케줄링(QD3):** `_maybe_turn` 순서는 다음과 같다.
   1. `fresh`(시작 카드)
   2. `resume_pending`
   3. 대기 중인 직접 질의 `query`(FIFO) — **메일 깨움보다 우선**한다. 단, 연속 질의로 대기 메일이 굶지 않도록
      대기 메일이 있을 때는 연속 질의 2건마다 메일 턴 1회를 교대 실행한다.
   4. 메일 `wake` / `continuation` / `sweep`.
3. **`stalled` 오판 차단:** 직접 질의 턴은 `mail_inbox`를 읽는 턴이 아니므로, `kind == "query"` 완료 시 스풀
   미읽음 비교로 `state.stalled`를 절대 올리지 않는다.
4. **프롬프트 래핑(stdin 전용, argv/TUI 주입 금지):**
   ```text
   [guardian-session-query id=q-20261122-0001 project=server-guardian kind=code from=operator]
   Direct query from the hub under project "server-guardian" to your active resident session.
   Answer directly in your final assistant text response.
   Do not send mail or create tasks unless explicitly requested. Start-card rules still apply.
   ---
   <질의 본문>
   ```
   `agy`·`codex`는 기존처럼 `ctx.card`를 앞에 붙이고, `claude`는 `--append-system-prompt ctx.card`를 유지한다.
5. **비밀값 마스킹과 크기 제한:** 노드에서 64 KiB(UTF-8)로 절단(`truncated: true`) 및 1차 마스킹을 수행하고,
   허브 저장·조회 시 `audit_run.mask_secrets`를 이중 적용한다. `daemon.log`와 `mail_relay` 로그에는 질문·답변
   본문을 절대 남기지 않는다.
6. **타임아웃:** `min(query.timeout_sec, turn_timeout())`(기본 180초, 최대 600초)을 적용한다. `open` 에이전트는
   기존처럼 `guardian-sandbox-run --timeout`과 `--stop` 정리를 그대로 따른다.

## 7. 예산, 한도, 중지 스위치 (QD4)

| 항목 | 기본값 | 강제 지점 |
|---|---|---|
| 에이전트당 시간당 직접 질의 수 (기존 12턴 메일 예산과 분리, QD4) | 20회 | 허브 `admit`(제출 시 사전 검사 포함) |
| 에이전트당 대기(`queued`/`dispatched`/`running`) 질의 수 | 5건 | 허브 제출 + 노드 로컬 큐 |
| 질의 본문 크기 | 32 KiB | 허브 제출 + Go API |
| 응답 본문 크기 | 64 KiB(초과 절단) | 노드 출구 + 허브 저장소 |
| 미실행 질의 수명 | 10분(`expired`) | 허브 지연 정리 + 노드 폐기 |
| 단일 질의 실행 타임아웃 | 180초(최대 600초) | 노드 `CliRunner` / 샌드박스 래퍼 |
| 중지 스위치 | `GUARDIAN_RESIDENT=off` | 허브 제출 및 `admit` 즉시 거절 |
| 취소 범위 | `queued` / `dispatched`만 | 실행 중 강제 종료 금지(세션 손상 방지) |

## 8. 인터페이스 (CLI, Go API, 대시보드)

### 허브 CLI (`cli/llm-agent/guardian.py`)
```bash
# 허브 프로젝트 범위로 활성 상주 세션에 직접 질의하고 응답 대기 (기본 --wait)
python3 cli/llm-agent/guardian.py --resident-query claude@node-1 \
  --project server-guardian \
  --prompt "현재 세션에서 확인한 내용을 요약해 줘." \
  [--task <작업ID>] [--timeout 180] [--no-wait] [--json]

# 최근 질의 목록 조회 (메타데이터만, 프로젝트/에이전트 필터)
python3 cli/llm-agent/guardian.py --resident-queries [--project server-guardian] [claude@node-1] [--json]

# 단일 질의·응답 본문 조회 (mode=ro, mask_secrets 적용)
python3 cli/llm-agent/guardian.py --resident-query-get q-20261122-0001 [--json]

# 미실행 질의 취소
python3 cli/llm-agent/guardian.py --resident-query-cancel q-20261122-0001
```
`--wait` 모드는 `mode=ro` 연결로 0.5–1.0초 간격 폴링해 완료 시 응답을 출력한다.

### 허브 Go API (`apps/agent/debate_handler.go`)
모든 경로는 기존 전역 `withSecurity(withAuth(mux))`(`AGENT_TOKEN`) 뒤에 둔다.
- `POST /api/residents/queries` — 본문 `{address, project, prompt, task?, timeout_sec?}` → `202 Accepted`
  (`{id, address, project, epoch, session_id, status}`). `address`를 경로가 아닌 JSON 본문에 두어 `@` 인코딩
  문제를 피하고 `POST /api/debates`·`POST /api/solo` 패턴과 맞춘다.
- `GET /api/residents/queries?project=<id>&agent=<addr>&limit=<n>` — 읽기 전용(`mode=ro`) 메타데이터 목록(본문 제외).
- `GET /api/residents/queries/{id}` — `^q-[0-9]{8}-[0-9]{4,9}$` 검증 후 마스킹된 질의·응답 본문 반환(`mode=ro`).
- `DELETE /api/residents/queries/{id}` — `queued`/`dispatched` 질의 취소.
- `GET /api/residents/monitor` — 에이전트 카드에 `session_ready: bool`, `queries_pending: int`를 추가하고,
  스냅숏에 최근 질의 메타데이터(본문 제외)를 포함한다.

### 웹 대시보드 (`apps/web-desktop/src/pages/residents/ResidentAgentDetail.tsx`)
- **업무 흐름 > 상주 에이전트**(`#/debates/residents?agent=<address>`) 상세 화면에 **"세션 질의"** 패널을 추가한다.
- 상단에서 선택된 허브 프로젝트(`project_id`, 프로젝트 종류 배지)를 표시하고, 제출하는 모든 질의를 그 프로젝트에
  귀속한다.
- `agent.state === "ready" && agent.session_ready`일 때만 입력창을 활성화한다(그렇지 않으면 바인딩 중·활성 세션
  없음·오프라인 사유를 표시).
- 제출 시 `POST /api/residents/queries` 후 탭이 보이는 동안 2초 간격으로 `GET /api/residents/queries/{id}`를 폴링해
  답변과 턴 ID·에포크·경과 시간·절단 여부를 표시하고, 해당 프로젝트의 최근 질의 이력(클릭 시 본문 로드)을 제공한다.
- 모든 문자열은 `src/i18n/ko`와 `src/i18n/en`에 함께 추가한다.

## 9. 보안 불변 조건과 위험 대응

| 위험 | 대응 |
|---|---|
| 직접 질의가 `open` 샌드박스나 `closed` 도구 봉쇄를 우회 | 기존 `CliRunner` + `_open_pre_turn`/`_pre_turn_checks` 단일 경로만 사용, 약한 대체 실행 없음 |
| 메일 턴과 직접 질의가 같은 세션을 동시 resume해 파일 손상 | 노드 단일 상주 슬롯(D5)과 `HeadlessDriver` 단일 `turn_thread`로 완전 직렬화 |
| 재바인딩 직후 오래된 질의가 빈 새 세션에 들어감 | 허브·노드 양쪽 `(epoch, expected_session_id)` 펜싱; `missing_session`/`resume_new_session` 시 `session_lost` 실패 |
| 응답이나 로그에 비밀값 노출 | 노드 출구 1차 + 허브 저장·조회 2차 `mask_secrets`; `daemon.log`·`mail_relay`에 본문 기록 금지 |
| 직접 질의가 `stalled_turns` 종료를 오작동시킴 | `kind == "query"`를 스풀 미읽음 정체 카운트에서 제외 |
| 무소속 전역 호출이나 프로젝트 경계 이탈 | 등록된 허브 `project` ID 필수 + 선택 `policy.resident_query_agents` 허용 목록 + 에이전트 간 직접 질의 금지 |
| 대량 질의로 할당량 소진 | 에이전트당 시간당 20회 별도 예산, 대기 큐 5건 상한, 32 KiB/64 KiB 크기 제한, `GUARDIAN_RESIDENT=off` 중지 스위치 |

## 10. 단계별 구현 계획

| 단계 | 범위 | 완료 조건 |
|---|---|---|
| **Q1 — 저장소·프로젝트 정책·CLI** | `mail_store.py`(스키마 6: `incarnations.session_ready`, `session_queries`, `TURN_KINDS` `"query"`, 별도 예산·만료·취소·에포크 펜싱), `project_registry.py`(`resident_query_agents`), `guardian.py --resident-query*` | 활성 세션 게이트, 미등록 프로젝트·허용 목록 거절, 에포크 변경 실패, 예산·큐 상한·만료·취소·`mode=ro` 단위 테스트 통과 |
| **Q2 — 릴레이·노드 드라이버** | `mail_relay.py`(`query`/`query_cancel` 전달, `admit`/`turn_end` 반영), `resident_node.py`(로컬 큐, `pending_acks` 답변 복구), `resident_driver.py`(`TurnResult.text`, QD3 우선순위·기아 방지, `stalled` 제외, 프롬프트 래퍼, 비밀 마스킹) | 가짜 릴레이·가짜 CLI e2e: 메일 턴 직렬화, 스트림 단절 후 답변 복구, `closed`/`open` 경로, `missing_session` 실패 검증 |
| **Q3 — Go API·모니터·대시보드** | `debate_handler.go`(`/api/residents/queries*`), `resident_monitor.py`(`session_ready`, `queries_pending`, 질의 메타데이터), `ResidentAgentDetail.tsx` 세션 질의 패널 + `ko`/`en` 문자열 | `go test`, `scripts/ci.sh web`, 격리된 로컬 허브 실제 브라우저 확인(390 px 포함) |
| **Q4 — 세션 MCP (선택)** | `session_link.py`, `session_daemon.py`, `session_mcp.py`의 `resident_query(to, project, prompt)`(허브 등록 `project` 필수, `caller_kind="session"` 경고 헤더) | `lease` 펜싱 및 프로젝트 허용 목록 포함 워크스테이션→상주 에이전트 e2e 통과 |

## 11. 운영자 결정 (2026-11-22)

다섯 항목 모두 2026-11-22 운영자가 확정했다.

| # | 항목 | 결정 (2026-11-22) |
|---|---|---|
| **QD1** | 직접 질의 주체와 범위 | **허브가 만든 프로젝트 기반**: 허브에 등록된 `project_id`를 필수로 지정해 허브 대시보드·CLI(`operator`)에서 질의하며, 선택적 `policy.resident_query_agents`로 프로젝트별 대상 에이전트를 제한. 무소속 전역 질의와 에이전트 간 직접 질의 금지(후속 Q4 세션 MCP도 등록된 `project_id` 필수) |
| **QD2** | 질의 실행 모드 | **`in_place`** (모든 프로바이더 공통으로 활성 상주 세션을 직접 resume; `fork`는 보류) |
| **QD3** | 메일 깨움 대비 우선순위 | **직접 질의 우선** (대기 메일이 있을 때 연속 질의 2건마다 메일 1턴 교대로 기아 방지) |
| **QD4** | 시간당 예산 | **별도 예산(에이전트당 시간당 20회)**으로 기존 12턴 메일 예산과 분리하되 `GUARDIAN_RESIDENT=off` 중지 스위치에 종속 |
| **QD5** | 보관과 백업 | **30일 보관 + 야간 백업 포함**(메일·작업 D7과 동일), `mask_secrets` 적용 저장 |

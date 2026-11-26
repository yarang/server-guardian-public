# 상주 에이전트와 운영자 세션(MCP)

[English](residents-and-sessions.md) | 한국어

[사용 가이드](README.ko.md)로 돌아가기.

Server Guardian은 허브의 메일망(SQLite 메일 저장소와 작업 보드)에 두 가지 지속형 참가자를 연결한다.

1. **상주 에이전트**: 워커로 참여한 Tier-1 노드에서 실행되며 허브의 `guardian-mail-relay.service`가 관리하는
   이름 있는 `claude`·`agy` 및(`--tools open` 전용) `codex` 인스턴스.
2. **운영자 세션**: 워크스테이션(macOS / WSL)의 대화형 Claude Code 또는 Antigravity(`agy`) 세션이 로컬 세션
   링크 데몬과 `guardian-session` MCP 서버를 통해 허브 메일망에 참여하는 방식.

모든 통신은 허브를 중심으로 한 성형(star) 구조를 유지한다. 노드와 워크스테이션은 서로 직접 연결하지 않는다.

---

## 1부. 상주 에이전트 (Tier-1 노드)

### 닫힌 도구 vs 열린 도구(샌드박스)
- **`--tools closed` (기본값)**: 상주 에이전트가 내장 CLI 도구 없이 headless로 동작하며, 메일함 MCP와(프로젝트
  작업 수행 시) 읽기 전용 `resident_repo_mcp`만 호출할 수 있다.
- **`--tools open`**: 상주 에이전트(`claude`, `agy`, `codex`)가 전용 유닉스 사용자와 systemd 임시 유닛 샌드박스
  (`scripts/install-sandbox.sh`, `scripts/probe-sandbox.sh`, 에이전트별 자격증명은
  `scripts/sandbox-credential.sh --provider claude|codex`) 안에서 내장 파일 쓰기·셸 도구를 사용한다. 샌드박스
  안에서는 `$HOME/out/`과 `$HOME/.local/bin`(`PATH` 앞쪽 포함)이 턴 사이에도 유지되며, 산출물과 호스트 요청은
  허브가 중개하는 세 경로로만 밖으로 나간다.
  - 코드 작업 트리 변경 → 허브에 수동 승인 대기(`PROPOSED`) `jev_mcp:apply_patch` 1건.
  - `$HOME/out/`·`$HOME/work/`의 비-Git 산출물 → `artifact_publish`(비밀값 마스킹, 최대 512 KiB, 허브 보관 후
    CLI/대시보드에서 다운로드).
  - 노드 패키지 설치와 OS 업데이트 → `ops_propose_package`, `ops_propose_update`(`ops:system_update`)로 에이전트
    소속 노드에 고정된 `PROPOSED` 액션을 **승인** 대기열에 등록(자동 실행 없음).

### 상주 에이전트 등록과 관리

사전 조건: 대상 Tier-1 노드에 워커 프로토콜 3(`scripts/probe-worker.sh`, `scripts/install-worker.sh`)과 상주 프로브
(`scripts/probe-resident.sh`)가 통과되어 있어야 한다. `--tools open`을 쓰려면 샌드박스 설치·프로브를 먼저 마친다.

```bash
# 닫힌 도구 상주 에이전트 추가 (드라이버는 M0 표 기본값 headless 사용)
python3 cli/llm-agent/guardian.py --resident-add claude --node node-1 --character default_expert

# 샌드박스 열린 도구 상주 에이전트 추가
python3 cli/llm-agent/guardian.py --resident-add coder --node node-1 --character default_expert --tools open

# 등록된 상주 에이전트 상태와 epoch 조회
python3 cli/llm-agent/guardian.py --residents

# 상주 에이전트 제거 (은퇴 처리, 작업 해제, 미확인 메일 배달 불가 표시)
python3 cli/llm-agent/guardian.py --resident-remove claude@node-1
```

### 허브 CLI로 메일 보내기와 작업 할당

```bash
# 상주 에이전트(또는 연결된 운영자 세션)에 메일 보내기
python3 cli/llm-agent/guardian.py --mail-send claude@node-1 \
  --mail-kind question \
  --mail-subject "백업 타이머 점검" \
  --mail-body "최근 백업 실행 상태를 확인해 줘."

# 최근 메일 또는 특정 수신자의 대기 메일 목록 조회
python3 cli/llm-agent/guardian.py --mail-list
python3 cli/llm-agent/guardian.py --mail-list claude@node-1

# 코드 프로젝트 커밋에 고정된 작업 생성 (수락 시 읽기 전용 저장소 스냅숏 업로드)
python3 cli/llm-agent/guardian.py --task-create "session_daemon.py 락 순서 점검" \
  --task-detail "허브 네트워크 호출 중 락을 쥐는 구간이 없는지 확인해 줘." \
  --task-assignee claude@node-1 \
  --task-project server-guardian

# 작업 목록, 작업별 도구 호출 로그 조회 및 작업 취소
python3 cli/llm-agent/guardian.py --tasks
python3 cli/llm-agent/guardian.py --task-calls <작업ID>
python3 cli/llm-agent/guardian.py --task-cancel <작업ID> --task-reason "대체됨"
```

### 상주 에이전트 모니터링
대시보드의 **업무 흐름 > 상주 에이전트**(`#/debates/residents`)에서 에이전트 상태(`session_ready` 및 대기 질의 수
포함), 최근 턴, 비밀값이 마스킹된 메일 본문, 작업 보드, 반출된 산출물, 최근 질의 메타데이터, 저장소 이벤트를 읽기
전용으로 볼 수 있다. CLI에서도 조회할 수 있다.

```bash
python3 cli/llm-agent/guardian.py --resident-monitor
python3 cli/llm-agent/guardian.py --resident-mail-body <메일ID>
python3 cli/llm-agent/guardian.py --resident-artifacts [에이전트]
python3 cli/llm-agent/guardian.py --resident-artifact-get <산출물ID> [--resident-artifact-out 경로]
python3 cli/llm-agent/guardian.py --resident-artifact-pin <산출물ID>
```

### 활성 상주 세션 직접 질의
상주 에이전트가 `ready` 상태이고 현재 에폭에서 활성 프로바이더 세션이 생성된 뒤(`session_ready: true`)에는, 허브
운영자가 메일함을 거치지 않고 허브 릴레이를 통해 그 활성 세션에 직접 질의(`mode: "in_place"`)할 수 있다. 기존
상주 슬롯과 `closed`/`open` 샌드박스 격리를 그대로 사용한다.

- **허브 프로젝트 범위 필수**: 모든 질의는 허브에 등록된 프로젝트(`code`, `ops`, `design`)를 반드시 지정해야 한다.
  프로젝트 정책의 선택 키 `policy.resident_query_agents`로 대상 에이전트를 제한할 수 있다(생략 시 전체 상주
  에이전트 허용, `[]` 시 비활성화, `["name@node", ...]` 시 명시 허용 목록).
- **다중 활성 세션 격리 (MS1–MS4)**: 각 상주 에이전트는 에포크당 최대 **8개의 활성 세션**(`main` 메일함·작업용,
  프로젝트별 기본 격리 세션 `project:<id>`, 주제별 하위 세션 `project:<id>:<label>`)을 유지할 수 있으며, 첫 질의 시
  지연 생성되고 14일간 미사용 시 자동 아카이브된다. `claude` 에이전트에서는 `--query-mode fork`(또는 `mode: "fork"`)로
  원본 세션을 변경하지 않는 1회성 읽기 전용 분기 질의(`--fork-session --no-session-persistence`)를 실행할 수 있다
  (`agy`·`codex`는 `fork_unsupported`로 fail-closed 거절).
- **대시보드**: `#/debates/residents?agent=<주소>`에서 상단 프로젝트를 고른 뒤 채팅형 **활성 세션 직접 질의**
  콘솔을 사용한다(`project:<id>`·하위 라벨·`main` 세션 선택기, 스코프 세션 초기화, `claude` 분기 모드 전환, 최근
  10건 자동 로드, 스마트 스크롤 잠금, 경과/타임아웃 타이머, 2초 폴링 + 탭 복귀 즉시 갱신, `Cmd/Ctrl+Enter` 전송,
  `queued`/`dispatched` 취소 지원).
- **워크스테이션 MCP (`guardian-session`)**: 연결된 운영자 세션에서도 `resident_query`(선택 `session_key`, `mode`)와
  `resident_query_get` 도구로 활성 상주 세션에 직접 질의할 수 있다(2부 4단계 참조). 동일한 허브 프로젝트·`peers`·
  `resident_query_agents` 검사와 `caller_kind: "session"` 경고가 적용된다.
- **예산과 보존**: 에이전트당 시간당 20회 전용 예산(메일함 12회 예산과 분리), 대기열 최대 5건, 프롬프트 32 KiB·응답
  64 KiB 제한 및 이중 비밀값 마스킹, 미시작 10분 만료, `mail.db` 30일 보존.

```bash
# 허브 프로젝트 범위에서 활성 세션에 직접 질의하고 응답 대기 (기본값)
python3 cli/llm-agent/guardian.py --resident-query codex@node-1 \
  --project server-guardian \
  --prompt "현재 세션에서 확인한 내용을 요약해 줘." \
  [--query-task <작업ID>] [--query-timeout 180] [--no-wait] [--json]

# 최근 직접 질의 목록(메타데이터), 마스킹된 상세 조회, 미시작 질의 취소
python3 cli/llm-agent/guardian.py --resident-queries [--project server-guardian] [--query-session-key project:server-guardian] [codex@node-1]
python3 cli/llm-agent/guardian.py --resident-query-get <질의ID>
python3 cli/llm-agent/guardian.py --resident-query-cancel <질의ID>

# 스코프 활성 세션 목록 조회 및 개별 스코프 세션 초기화(main 유지)
python3 cli/llm-agent/guardian.py --resident-sessions [codex@node-1] [--project server-guardian]
python3 cli/llm-agent/guardian.py --resident-session-reset codex@node-1 --session-key project:server-guardian:review
```

---

## 2부. 워크스테이션 운영자 세션 (MCP)

운영자 세션을 연결하면 내 워크스테이션의 Claude Code나 Antigravity(`agy`) 세션이 `<이름>@<워크스테이션>`(예:
`work@ws-mac-1`) 주소로 허브 메일망에 참여한다.

### 연결 구조
- 워크스테이션은 `tag:workstation`으로 tailnet에 가입하고, 표준 라이브러리 기반 로컬 데몬(`session_daemon.py`)을
  실행한다.
- 데몬은 전용 키(`~/.guardian/session/link-key`)와 고정된 허브 호스트 키(`~/.guardian/session/known_hosts`)로
  허브의 강제 명령 입구(`guardian.py --session-link`)에 아웃바운드 `ssh -T` 스트림 하나를 연다.
- 허브에서는 Guardian이 단독 관리하는 `~/.ssh/authorized_keys2`(`from="<tailnet IP>"`, `command=...`,
  `restrict`)로 워크스테이션을 인증한다. 워크스테이션에는 **`AGENT_TOKEN`이 없고 SSH 서버도 열지 않는다**.
- 대화형 CLI는 `session_mcp.py`를 stdio 자식으로 실행하고, `session_mcp.py`는 이 세션 전용 유닉스 소켓으로만
  데몬과 통신한다. 데몬은 소켓에서 세션 신원을 찍으므로 도구 인자로 발신자를 위조할 수 없다.

### 1단계: 워크스테이션 링크 데몬 설치

macOS 또는 WSL/Linux의 `server-guardian` 체크아웃에서 실행한다.

```bash
# 변경 없이 사전 점검만 실행
scripts/install-session-link.sh \
  --workstation ws-mac-1 \
  --hub-address 100.64.0.1 \
  --hub-alias site-a-arm1 \
  --hub-user ubuntu \
  --hub-fingerprint SHA256:<허브-ed25519-지문> \
  --dry-run

# 모듈, 링크 키, known_hosts, 설정, LaunchAgent/systemd 사용자 유닛 설치
scripts/install-session-link.sh \
  --workstation ws-mac-1 \
  --hub-address 100.64.0.1 \
  --hub-alias site-a-arm1 \
  --hub-user ubuntu \
  --hub-fingerprint SHA256:<허브-ed25519-지문>
```

참고:
- macOS에서 홈 디렉터리가 `Owners: Disabled`인 외장 APFS 볼륨에 있으면 `launchd`가 LaunchAgent 등록을 거부한다
  (bootstrap error 5). 이때는 `--no-service`로 설치한 뒤 데몬을 직접 실행한다.
  ```bash
  nohup python3 ~/.guardian/session/lib/session_daemon.py --home "$HOME" \
    </dev/null >>~/.guardian/session/daemon.log 2>&1 &
  ```
- 상태 확인 및 즉시 중단(킬 스위치):
  ```bash
  scripts/install-session-link.sh --status
  scripts/install-session-link.sh --disable
  ```

### 2단계: 허브에 워크스테이션 등록

1단계 마지막에 출력된 공개키(`~/.guardian/session/link-key.pub`)를 허브에 등록한다.

```bash
# 허브에서 실행
python3 cli/llm-agent/guardian.py --session-add ws-mac-1 \
  --address 100.64.0.5 \
  --key "ssh-ed25519 AAAA..."

# 등록된 워크스테이션과 활성 세션 확인
python3 cli/llm-agent/guardian.py --session-list

# 워크스테이션 tailnet IP 변경 시 (authorized_keys2의 from= 갱신)
python3 cli/llm-agent/guardian.py --session-address ws-mac-1 --address 100.64.0.9

# 워크스테이션 제거
python3 cli/llm-agent/guardian.py --session-remove ws-mac-1
```

### 3단계: CLI에 MCP와 훅 연결

#### Claude Code
1. 사용자 범위 MCP 서버 등록(인스톨러가 출력하는 명령):
   ```bash
   claude mcp add --scope user guardian-session -- \
     python3 ~/.guardian/session/lib/session_mcp.py
   ```
2. 인스톨러가 출력한 4개 훅(`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, 각 `"timeout": 5`)과
   선택적 읽기 전용 `permissions.allow`(`session_inbox`, `session_list`, `session_status`)를
   `~/.claude/settings.json`에 병합한다.

#### Antigravity (`agy`)
1. `~/.gemini/config/mcp_config.json`(IDE 사용 시 `~/.gemini/antigravity-ide/mcp_config.json` 포함)에 `--client agy`
   옵션으로 `guardian-session`을 추가한다.
   ```json
   {
     "mcpServers": {
       "guardian-session": {
         "command": "/opt/homebrew/bin/python3",
         "args": [
           "/path/to/home/.guardian/session/lib/session_mcp.py",
           "--client",
           "agy"
         ]
       }
     }
   }
   ```
2. `~/.gemini/config/hooks.json`에 3개 훅(`agy-session-start`, `agy-pre-invocation`, `agy-stop`)을 추가한다.
   ```json
   {
     "guardian-session": {
       "SessionStart": [{
         "command": "exec \"/opt/homebrew/bin/python3\" \"/path/to/home/.guardian/session/lib/session_hooks.py\" agy-session-start",
         "timeout": 5
       }],
       "PreInvocation": [{
         "command": "exec \"/opt/homebrew/bin/python3\" \"/path/to/home/.guardian/session/lib/session_hooks.py\" agy-pre-invocation",
         "timeout": 5
       }],
       "Stop": [{
         "command": "exec \"/opt/homebrew/bin/python3\" \"/path/to/home/.guardian/session/lib/session_hooks.py\" agy-stop",
         "timeout": 5
       }]
     }
   }
   ```
   Antigravity는 프로세스당 하나의 MCP 자식을 공유(`agy-<ppid>`)하며 첫 호출 시 필요하면 지연 등록한다. 종료된
   프로세스는 데몬 틱이 자동으로 정리하고 허브 바인딩을 해제한다.

### 4단계: `guardian-session` MCP 도구 7종 사용법

| 도구 | 인자 | 기능 |
|---|---|---|
| `session_link` | `name`(선택), `away`(선택 bool) | 이 워크스테이션에서 고유한 주소 등록(예: `work` → `work@ws-mac-1`), 인자 없이 현재 주소 조회, `away=true`로 자리비움 설정(Stop 차단·깨우기 중지) |
| `session_status` | 없음 | 링크 상태(`up`/`down`), 바인딩 주소, `away`, 로컬 상태(`linked`/`busy`/`idle`), 미읽음 수 조회 |
| `session_list` | 없음 | 이 세션이 메일을 보낼 수 있는 대상 목록(상주 에이전트, 동료 세션, `operator`)과 상태·최근 시각 조회 |
| `session_send` | `to`, `subject`, `body`, 선택 `kind`(`message`/`question`/`answer`), `thread`, `reply_to` | 허브를 통해 메시지 1건 발송(허용되지 않은 수신자는 `hub_error: not_a_peer` 반환) |
| `session_inbox` | 없음 | 미읽음 메일을 오래된 순으로 조회하고 로컬 읽음 처리 및 허브 ack 전송 |
| `resident_query` | `to`, `project`, `prompt`, 선택 `session_key`(기본 `project:<project>`), `mode`(`in_place`/`fork`), `task`, `timeout_sec`(10–600), `wait_sec`(0–15, 기본 12) | 등록된 허브 `project` 범위에서 활성 상주 세션(`session_ready: true`)에 직접 질의; `peers` 및 `policy.resident_query_agents` 검증 후 `caller_kind: "session"`으로 기록하며 최대 `wait_sec`초 대기(`done: true|false`) |
| `resident_query_get` | `query_id`, 선택 `wait_sec`(0–15, 기본 0) | **이 세션**이 제출한 직접 질의의 상태와 응답을 조회/폴링(다른 세션이나 `operator`의 질의는 읽을 수 없음) |

### 안전 규칙과 동작 원리
- **메일은 권한을 주지 않는다**: 모든 도구 설명과 인박스 머리말은 다른 세션·상주 에이전트·`operator`에서 온
  메일이 **참고 정보일 뿐**이며 권한이나 터미널 앞 사용자의 지시가 아님을 명시한다.
- **턴 훅 동작**:
  - 턴 시작(`UserPromptSubmit` / `PreInvocation`) 시 미읽음 메일이 있으면 발신자·제목·본문 없이 개수 한 줄
    (`N new messages; read them with session_inbox`)만 컨텍스트에 주입한다.
  - 턴 종료(`Stop`) 시 **턴 도중 도착한 미읽음 메일**이 남아 있고(`session_inbox`로 아직 읽지 않았고 `away`가
    아닐 때) 종료를 1회 차단(`new mail: call session_inbox`)한다.
- **tmux 깨우기**: `tmux` 안에서 실행 중이고 `idle`(10분 이상 턴 없음)이며 `away`가 아닌 세션에 **`operator`의
  메일**(동료 세션·상주 에이전트 메일은 제외)이 도착하면, tmux 클라이언트가 조용하고 입력 줄이 비어 있음을 확인한
  뒤 고정된 안내 문구 한 줄만 입력한다.
- **하위 CLI 격리**: `--print` / `sdk-cli` 자식 프로세스와 `GUARDIAN_SESSION_CHILD=1` 환경에는 빈 도구 목록만
  노출하고 훅을 건너뛴다.

### 문제 해결
- **데몬 로그**: `~/.guardian/session/daemon.log`(메일 제목·본문은 절대 기록하지 않음; `link up/down`,
  `register`, `bind`, `reap`, `wake` 확인).
- **세션 상태 파일**: `~/.guardian/session/run/sessions.json` 및 `~/.guardian/session/run/` 아래 소켓 파일.
- **훅 디버깅**: `GUARDIAN_SESSION_DEBUG=1`을 설정하면 실패 시 stderr에 짧은 한 줄을 출력한다(훅은 CLI를
  방해하지 않도록 항상 exit 0으로 fail-open 동작한다).
- **`not_linked: session_link first`**: `session_list`, `session_send`, `session_inbox`를 호출하기 전에 먼저
  `session_link`로 소문자 이름(예: `name: "work"`)을 등록해야 한다.

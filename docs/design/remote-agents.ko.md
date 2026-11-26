# 워커 실행이 가능한 노드의 원격 토론 에이전트 (설계)

[English](remote-agents.md) | 한국어

상태: 2026-10-22 제안, 같은 날 운영자가 범위를 정함(아래 답변). W1(전송)과 W4a(능력 프로브와 설치 스크립트)는 구현해 허브에 배포했습니다(2026-10-22, `23dfc03`). 다만 워커가 있는 노드는 아직 없고 스크립트도 실제 노드에서 돈 적이 없습니다. 나머지는 구현하지 않았습니다.
배경: 허브가 토론 에이전트(Expert, Critic, Judge)를 실행 가능한 노드에서 돌릴 수 있게 하는 것이 운영자의 목표입니다.
이는 이전 결정 둘을 의도적으로 뒤집습니다. "다른 fleet 호스트는 agent를 돌리지 않는다"(CLAUDE.md, 토폴로지)와
"작업 큐나 fleet 워커 없음"(확정 결정)이며, `docs/design/multi-perspective.md`의 비목표 "물리적 분산"도 풉니다.
뒤집는 범위는 능력 프로브를 통과한 노드와 작은 워커 하나로 한정합니다("범위" 참고). 같은 날 운영자의 보완: Tier-1은 환경상의 예외였으므로 경계는 등급이 아니라 에이전트를 **실행할 수 있는** 노드와 **없는** 노드 사이에 둡니다.

## 결정 (운영자, 2026-10-22)

| 질문 | 결정 |
|---|---|
| 이유 | 허브 부하 분산(2 OCPU, 12 GB), 노드별 구독 한도 분산, 관점 다양화(다른 노드·아키텍처), 노드 안에서 직접 점검 |
| 실행 방식 | 실행 가능하고 켜 둔 노드마다 **상주 워커**(호출마다 SSH 1회 실행이 아님) |
| 코드 접근 | 턴 동안 노드로 **읽기 전용 스냅샷**을 보냄 |
| 배치 | **팀 JSON**이 역할별 실행 호스트를 지정. `host`가 없으면 지금처럼 허브 |

## 범위와 그대로 유지되는 것

- **등급이 아니라 능력.** 워커는 능력 프로브(아래)를 통과한 노드에 둘 수 있습니다. `tier`는 하드웨어 설명일 뿐 아무것도
  결정하지 않습니다. 지금의 Tier-1은 통과할 것으로 보이고, Tier-2는 조건을 실제로 충족할 때만 통과하며(1 GB 메모리는
  사실상 프로브에서 탈락), CLI가 동작하지 않는 Tier-1은 탈락합니다. 인벤토리에는 두 사실이 있습니다. `worker_capable`(마지막
  프로브 통과, 날짜와 사유 포함)과 `worker: true`(운영자가 워커를 설치해 켬). `worker: true`인 노드만 작업을 받습니다.

### 능력 프로브

`scripts/probe-worker.sh <host>`(읽기 전용, fleet 키 사용)가 조건별로 통과·실패와 사유를 보고하고 결과를 허브의 인벤토리
항목에 기록합니다. 조건: 설정된 `GUARDIAN_WORKER_MAX_JOBS`에 맞는 메모리(기본: 전체 4 GiB, 작업당 여유 1.5 GiB), 엔진의 최소
버전 이상 `python3`, 노드 자체 `llm.env`로 `deploy-llm-env.sh`의 PONG 시험에 답하는 LLM CLI 하나 이상, 스냅샷용 여유 디스크
(1 GiB), 허브에서 닿는 fleet 경로. 기준값은 한 곳(`configs/worker-requirements.json`)에 있어 운영자가 코드 없이 바꿀 수
있습니다. 프로브는 아무것도 설치하지 않고 자격증명도 건드리지 않습니다. 탈락한 노드는 대상이 아닐 뿐이며 사유는 설정에
표시됩니다.
- 결정 DB, jev 게이트, fleet 키, `AGENT_TOKEN`, 엔진은 허브에만 있습니다. 워커는 **모델 실행기**입니다. 프롬프트를 받아
  텍스트를 돌려줄 뿐 판단하지 않고, 토론 상태를 저장하지 않으며, 토론을 시작하거나 액션을 적용하거나 다른 노드에 닿지
  못합니다.
- 워커는 엔진처럼 Python 표준 라이브러리만 씁니다. 노드에 큐, 브로커, DB를 두지 않습니다.
- `scripts/install-agent-service.sh`는 허브 외 노드를 계속 거부합니다. 워커는 별도 설치기
  (`scripts/install-worker.sh`, W4a)를 씁니다.
- 증거·인용 검사, jev 게이트, 이벤트 계약은 바뀌지 않습니다. 워커가 돌려준 결과 위에서 허브가 그대로 수행합니다.

## 구조

```
허브(엔진)                                           워커 노드(상주 워커)
----------                                           ------------------------
debate_engine ──> LLMRunner.run_session_prompt
                    │ 역할에 host가 있는가?
                    │   없음 → 로컬 CLI (지금과 같음)
                    │   있음 → RemoteRunner ── ssh(fleet 키) ──> worker client ──유닉스 소켓──> worker daemon
                    │                                                                          │ claude/codex/agy를
                    │         <── 결과: 텍스트, 세션 id, provider, MCP 호출 로그 ──────────────┘ 노드 자체 llm.env로 실행
                    └ 실패 → 허브 provider로 폴백 (`failures`에 기록)
```

통합 지점은 하나입니다. `LLMRunner.run_session_prompt`(`llm_runner.py`)에 선택 인자 `host`를 추가하고, 엔진은
`_call_llm`(`debate_engine.py`, 유일한 호출자)에서 역할의 실행 호스트를 넘깁니다. 호출 이후(이벤트 발행, 세션 저장, 증거 검사)는
바뀌지 않습니다.

### 전송과 인증

워커는 TCP 포트가 아니라 **유닉스 소켓**(`~/.guardian/worker/worker.sock`, 0600)에서 듣습니다. 허브는 기존 fleet SSH 경로로
접근합니다. `ssh <host> python3 ~/.guardian/worker/worker.py client`가 stdin의 JSON 요청 하나를 소켓에 전달하고 응답을
stdout으로 돌려줍니다. 결과는 다음과 같습니다.

- "노드는 허브만 받는다"가 지금 그대로 유지됩니다. fleet 키의 `from=` 제한이 유일한 문이며 새 포트도, 새 토큰도, `AGENT_TOKEN`과
  혼동될 것도 없습니다. 워커는 `AGENT_TOKEN`, jev 키, 허브 전용 키를 받지 않습니다.
- 모든 호출이 SSH를 지나는데 상주 데몬을 두는 이유: 데몬이 작업 표를 가집니다. 작업에는 id가 있어 허브가 두 번째 SSH 호출로
  id를 취소할 수 있고(허브 작업을 끄면 원격 CLI 프로세스 트리도 끝남), SSH가 끊겨도 고아가 남지 않으며(타임아웃은 데몬이
  강제), 노드별 동시 실행 수가 한 곳에서 지켜집니다(`GUARDIAN_WORKER_MAX_JOBS`, 기본 2).
- 요청·응답은 프로토콜 버전이 있는 JSON 프레임입니다. `ping`이 프로토콜 버전과 워커 코드 버전을 돌려주고, 허브는 모르는
  프로토콜의 워커를 거부하고 보고합니다(조용한 다운그레이드 없음).

### 요청에 담기는 것

로컬 호출과 같은 인자입니다. 프롬프트, context, provider 순서(`providers`), `session_id`, `system`, `instructions`(상시 지침),
`plain_prompt`, 저장소 도구 명세(아래). 자격증명은 담지 않습니다. 응답은 `LLMResult`(provider, 텍스트, 세션 id, `resumed`,
`failures`)와 MCP 호출 로그입니다.

### 자격증명

워커는 노드 자체의 `~/.guardian/llm.env`를 읽습니다. `scripts/deploy-llm-env.sh`가 이미 LLM CLI를 쓰는 노드에 둡니다(0600, 허브 전용
키 제외). provider 환경은 같은 `provider_env` 규칙으로 만듭니다(claude 실행은 claude 변수만 봄).
`~/.claude/.credentials.json`과 `~/.codex/auth.json`은 복사하지 않습니다. 토큰이 필요한 노드는 `deploy-llm-env.sh`로
받습니다. 이 설계는 호출마다 허브에서 자격증명을 보내지 않습니다.

### 세션

CLI 세션은 실행 호스트의 디스크에 있으므로 세션은 **실행 호스트에 고정**됩니다.

- `agent_sessions`(스레드, 역할)에 `host` 열을 추가합니다(마이그레이션, 기본 `''` = 허브). 세션은 같은 실행 호스트에서만 재개합니다.
- 역할의 `host`가 바뀌었거나 실행 호스트에 닿지 않으면 실패하지 않고 **새** 세션을 시작합니다. 손실은 명시합니다.
  `failures`에 `"<host>: unreachable, session not resumed"`를 남기고, 이전 라운드는 다른 새 세션과 같이 스레드 요약
  (`thread_digest.py`)으로 전달합니다.
- `agent_started` 이벤트에 `host`가 붙습니다. 대시보드는 이미 `debate_started.host`를 보여 주며, 토론 에이전트별 실행 호스트는 각
  에이전트 옆에 표시합니다.

### 팀 JSON에서의 배치

역할(`expert`, 각 `critic`, `judge`, 각 `proposer`)은 `"host"`를 가질 수 있습니다. 실제 호스트 이름은 git에 들어가지 않으므로
(CLAUDE.md):

- `configs/teams/`의 builtin 팀은 **라벨**을 씁니다. `"host": "worker:any"`(`worker: true`인 아무 노드) 또는 `"worker:other"`
  (Expert가 쓰는 노드가 아닌 워커 노드, 관점 다양화용).
- 사용자 팀(허브 데이터, git 밖)은 **인벤토리 id**를 직접 쓸 수 있습니다.
- `any`/`other`는 스레드의 첫 턴에 한 번만 결정합니다. 인벤토리에 `"worker": true`가 있고 `ping`이 성공한 노드 중에서
  빈 작업 칸이 많은 순, 그다음 id 순으로 고릅니다. 선택은 `agent_sessions.host`에 저장되어 스레드 안에서 바뀌지 않습니다.
  쓸 수 있는 노드가 없는 라벨은 허브로 풀리고 기록됩니다.
- 라인업 검증(`team_manager.py`)은 지정한 노드가 존재하고 `worker: true`인지 확인합니다(능력은 있지만 켜지 않은 노드는 그 사유로 거부).

### 저장소 접근: 읽기 전용 스냅샷

저장소 도구(`RepoTools`)는 고정된 sha의 git 저장소 위에서 동작합니다(`git ls-tree`, `cat-file`, `grep`). `code_tools: "mcp"`인
원격 역할에는 허브가 **스냅샷**을 보내고 워커가 그 위에서 `repo_mcp.py`를 실행합니다.

1. 허브가 고정 sha에서 `git archive`로 스냅샷을 만들며 `is_secret`에 걸리는 경로(경로 구성요소마다 검사, 다른 곳과 동일),
   심볼릭 링크, 한도를 넘는 바이너리를 **뺍니다**. 필터는 허브에서 돌고 워커의 필터링을 신뢰하지 않습니다.
2. 워커는 이를 개인 임시 디렉터리(0700)에 풀고 `git init` 후 고정 작성자·날짜로 커밋 하나를 만든 뒤 `repo_mcp.py`가 그것을
   가리키게 합니다. 스냅샷 커밋 id는 허브의 sha와 다르며, 프롬프트(`_mcp_text`)에는 허브의 sha를 라벨로 유지하고 아래 로그에는
   sha가 없습니다.
3. 스냅샷은 내용 해시로 스레드 동안 워커에 캐시되고 스레드가 끝나거나 24시간 뒤 삭제됩니다. 워커 데이터 디렉터리 밖에는
   남지 않습니다.
4. **호출 로그가 허브로 돌아옵니다.** `repo_mcp.py`는 `calls.jsonl`(op, 경로, 줄 범위, 언급 경로, 내용은 없음)을 씁니다.
   "강한 인용은 세션에 보인 줄일 때만 통과"라는 규칙이 이것에 의존하므로, 워커는 매 응답에 새 로그 레코드를 실어 보내고 허브가
   그 턴의 `McpTurn` 로그에 붙입니다. 이것이 없으면 원격 에이전트의 강한 인용은 모두 검증에 실패합니다. 스냅샷은 비밀이 아닌
   모든 경로에서 허브 sha와 같은 blob을 가지므로 줄 범위는 양쪽에서 같은 줄을 뜻합니다.
5. 한도(`steps`, `step_bytes`, `turn_bytes`)는 로컬 턴과 같이 같은 로그로 셉니다.

심볼 인덱스(`repo_defs`, `repo_refs`, `repo_callers`, `repo_callees`)와 Go facts는 허브의 캐시와 Go 툴체인이 필요합니다. W2의
원격 역할은 `repo_list`, `repo_grep`, `repo_read`, `repo_outline`, `repo_map`을 받고, 인덱스 기반 도구는 W2b에서 인덱스를 스냅샷에
넣을 때까지 "이 호스트에서는 사용할 수 없음"을 돌려줍니다.

### 노드 안에서의 점검

"노드 안에서 점검"은 CLI에 셸을 주는 것이 **아닙니다**. claude의 `--tools ""`는 유지하고 codex와 agy도 지금 그대로입니다. 원격
역할은 지금과 같은 타입 지정·읽기 전용 ```ops 블록 카탈로그(`ops_checks.py`)를 쓰되, 워커가 명령을 SSH 대신 **로컬**에서 실행해
결과를 돌려줍니다. 감사 줄(`source=ops_check`) 기록과 인용 검증은 계속 허브가 하고, 토론 에이전트가 실행할 수 있는 것은 카탈로그뿐입니다.
엔진이 지금처럼 중개하며 실행기만 달라집니다(W3).

### 취소, 타임아웃, 실패

- 허브 작업 종료(`cancel`)는 워커에 `cancel <job_id>`를 보내고, 워커는 CLI 프로세스 그룹을 죽입니다.
- 타임아웃은 러너의 값(`GUARDIAN_LLM_TIMEOUT`)이며 데몬도 강제하므로 허브가 죽어도 실행이 남지 않습니다.
- 닿지 않는 노드, 거부된 프로토콜, 실패한 `ping`은 그 호출에 한해 허브 provider로 폴백합니다. 폴백은 provider 폴백과 같이
  `failures`와 `provider_failed` 이벤트로 남고, 폴백 답이 있는 라운드는 이미 `UNVERIFIED`입니다. 부작용은 분명히 적습니다.
  원격 세션의 연속성이 끊깁니다.
- 워커가 가득(`max_jobs`)이면 호출은 `GUARDIAN_WORKER_QUEUE_SECONDS`(기본 60) 동안 기다린 뒤 폴백합니다. 워커에 큐는 없습니다.

### 대시보드와 API

팀 편집기에 역할별 `host` 입력이 생기고(문구는 `ko`, `en` 둘 다), 설정에는 노드별 능력 결과와, 켜진 노드의 워커 상태(버전, 프로토콜, 실행 중
작업)가 허브의 `ping`으로 표시됩니다. 허브 에이전트는 읽기 전용으로 노출하며 워커와 직접 통신하지 않습니다(토론을 띄우는 Python
CLI가 통신합니다).

## 위험과 대응

| 위험 | 대응 |
|---|---|
| 비밀 코드가 노드로 감 | 허브 쪽 `is_secret` 스냅샷 필터, 스레드 종료 후 삭제, 프로브를 통과하고 켠 노드만 |
| 워커가 두 번째 제어면이 됨 | 상태·허브 자격증명·외부 접근이 없고, 문은 fleet 키 하나 |
| 원격 인용이 검증되지 않음 | MCP 호출 로그가 모든 응답과 함께 돌아옴(W2) |
| 다른 노드여도 같은 모델·같은 맹점 | 배치는 실행 호스트를 다양화할 뿐 모델은 아님. 라인업은 `providers`를 달리함(`multi-perspective.md`) |
| 노드 메모리 부족(운영자도 쓰는 노드에서 CLI 2개 병렬) | `GUARDIAN_WORKER_MAX_JOBS`, 대기 후 폴백 |
| 허브와 워커의 코드 불일치 | `ping`의 프로토콜 버전, `install-worker.sh`가 한 릴리스의 파일을 복사, 불일치는 거부 |
| 워커 환경에 허브 비밀이 섞임 | 고정된 최소 환경 + `llm.env`로 시작, 허브 전용 키는 `deploy-llm-env.sh`가 이미 제외 |

## 단계

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| W1 | `remote_agents.py`(허브 클라이언트)와 `worker.py`(데몬, 클라이언트 중계, 작업 표, 취소), `run_session_prompt`의 `host`, `agent_sessions.host`, `agent_started`의 `host`, 폴백. 저장소 접근이 **없는** 역할(ops Critic, 설계 검토) | 가짜 워커 테스트 통과, 허브 샌드박스 토론 1건에서 Critic 하나가 워커 노드에서 실행 |
| W2 | 스냅샷, 원격 `repo_mcp`, 호출 로그 반환, 인덱스 없는 도구 5개 | 심은 결함 과제에서 로컬·원격의 인용 결과가 같음 |
| W2b | 스냅샷 안의 심볼 인덱스 | 인덱스 도구가 원격에서 동작(W2에서 필요성이 확인될 때만) |
| W3 | 워커의 로컬 ops 점검 | ops 감사 줄과 인용이 SSH 경로와 같음 |
| W4a | `probe-worker.sh`(`configs/worker-requirements.json`, `worker_probe.py`), `install-worker.sh`(systemd 사용자 유닛, git 리비전의 파일, 허브 경로를 거치는 수락 ping, 인벤토리 `worker: true`, `--disable`, `--uninstall`) | 능력 있는 노드를 명령 하나로 켜고 다른 명령 하나로 되돌릴 수 있음. 점검에 실패하면 표시되지 않음 |
| W4b | 팀 편집기 `host`, 설정의 워커 상태, 읽기 전용 API, `ko`/`en` 문구 | 운영자가 JSON을 고치지 않고 역할을 배치하고 워커 상태를 볼 수 있음 |

각 단계는 배포 가능한 상태로 끝나며, 어디에도 `host`를 두지 않으면 동작은 지금과 정확히 같습니다.

## 테스트

- 가짜 워커(테스트 프로세스 안의 소켓 서버)로 프로토콜 불일치, 취소, 타임아웃, 가득 참, 닿지 않음, 세션 고정과 실행 호스트 이전을
  검증합니다. 모든 테스트가 `testenv.isolate_data_dir()`를 쓰며 실제 데이터 디렉터리는 건드리지 않습니다.
- 심은 결함 검사: 같은 과제를 로컬과 원격 Expert가 답해 인용 결과와 검색 이벤트가 같아야 합니다(W2).
- 시험 토론은 `~/guardian-eval`에서만 하고, Codex 한도를 운영과 공유하므로 먼저 운영자 동의가 필요합니다.
- CI: `scripts/ci.sh python`, `config`(팀 스키마), 편집기는 `web`. `redact-private.py --check`는 아무것도 찾지 않아야 합니다.

## 열린 문제

1. `worker:other`가 다른 **아키텍처**나 다른 provider 계정을 우선해야 하는지(지금 후보 노드는 모두 ARM64라 기계만 나뉩니다).
2. 원격 Judge가 필요한지. Judge는 jev 게이트 입력을 쥐고 있어 세션을 잃는 비용이 Critic보다 큽니다. 제안: 허용하되 builtin
   팀의 기본은 Critic과 proposer로 합니다.
3. 노드들이 한 토큰을 공유하면(`deploy-llm-env.sh`는 파일 하나를 보냄) 구독 한도는 노드별이 아니라 계정별입니다. 한도를
   나누려면 노드별 토큰이 필요하며, 이는 코드 밖의 운영자 결정입니다.

## 구현 메모

### W1 (전송과 세션 고정, 2026-10-22)

구현: `cli/llm-agent/remote_agents.py`(배치 문법, `RemoteRouter`, `SshTransport`), `worker.py`(`serve`, `client`, `exec`),
`Member.host`, `agent_sessions.host`, 엔진 배치(`DebateEngine._place`, `_call`의 host 라우팅).
테스트: `test_remote_agents.py`(스크립트된 전송, 가짜 작업 명령을 쓰는 유닉스 소켓 인프로세스 데몬, 허브 러너를 바꾼
엔진 배치). 설계와 달라졌거나 알아 둘 점:

- 허브가 `ssh ... worker.py client`를 직접 띄우고 요청을 stdin으로 보냅니다. `/api/exec/remote`를 거치지 않으므로
  프롬프트가 argv, 감사 로그, 명령 API에 닿지 않습니다.
- 작업은 자기 프로세스 그룹의 자식 프로세스입니다. 취소, 작업 타임아웃, 허브의 연결 끊김(요청 소켓의 `MSG_PEEK`으로
  확인)은 모두 `killpg`로 끝납니다. 두 폴링 사이에 취소가 들어오면 실패한 작업이 아니라 `cancelled`로 보고합니다
  (데몬 테스트에서 발견).
- 모든 `WorkerError`(연결 불가, 프로토콜 불일치, 바쁨, 타임아웃, 활성 워커 아님)는 허브에서 새 세션으로 호출을 실행하고,
  `failures`에 `<host>: <이유>; the call ran on the hub`가 남고 `LLMResult.host`는 `''`입니다. 연결 불가와
  불일치 노드는 60초, 바쁜 워커는 캐시하지 않습니다(ping은 30초 캐시).
- 이어 가던 워커 세션이 허브로 대체되면 새 허브 세션은 스레드를 본 적이 없습니다. 그래서 엔진이 히스토리를 붙인 전체 턴
  (언어 규칙은 `_call`과 같게 적용)을 `replay`로 라우터에 넘기고, `_fallback`이 그것을 써서 허브 호출은 두 번이 아니라
  한 번입니다. 워커가 스스로 새 세션을 시작한 경우(`resumed`가 거짓)에는 `_ask_turn`이 첫 턴처럼 히스토리를 붙여 그 턴을
  다시 보냅니다.
- SSH 호스트 키: 허브 에이전트는 고정되지 않은 노드에 `StrictHostKeyChecking=accept-new`를 붙이지만 `SshTransport`는
  붙이지 않습니다. 허브가 키를 본 적 없는 노드는 처음 접속을 신뢰하는 대신 허브로 폴백합니다. 워커로 쓰기 전에 한 번
  수동으로 접속하거나 키를 고정해 두십시오.
- 라벨은 스레드당 한 번 해석하고 이후 저장된 세션을 따라갑니다(세션이 허브로 옮겨 가도 마찬가지). 라벨이 스레드
  도중에 노드를 옮겨 다니지 않습니다. 인벤토리 id를 명시하면 그것이 요구 사항이라 새 세션으로 원격을 다시 시도합니다.
- `host`와 `code_tools: "mcp"`의 조합은 팀 검증에서 거부되고, `_ask`도 그런 멤버의 `host`를 무시합니다. 저장소
  도구는 W2까지 허브에 남습니다.
- 인벤토리 검증(`inventory_host_error`)은 `servers.json`이 읽힐 때만 동작합니다. 이 파일은 git에서 제외되어 CI에는 없습니다.
- W1 완료 조건 중 남은 것: 실제 워커 노드에서 Critic을 돌리는 시험 토론. W4의 설치 스크립트와 운영자의 승인(공유 Codex 쿼터)이 필요합니다.
  그 멤버의 `agent_started.host`가 설정되어 있고 **동시에** `failures`에 `the call ran on the hub` 항목이 없을 때만
  통과로 칩니다. 조용히 허브로 폴백한 토론은 그 밖에는 정상으로 보이기 때문입니다.
- 아직 없음: 설치 스크립트, 능력 점검, 노드 폼의 `worker` 플래그, 팀 편집기 필드와 설정 화면(W4), 스냅샷(W2), 로컬 ops
  점검(W3). 어떤 팀도 `host`를 지정하지 않으므로 지정하기 전까지 동작은 그대로입니다.
- 이 Mac에서는 `test_retrieval_loop`, `test_go_facts`, `test_team_manager` 한 건이 `GUARDIAN_LLM_PROVIDERS=none`(CI는
  설정함) 없이, 또는 Go가 있는 로그인 셸 밖에서 실행하면 실패합니다. `947046b`의 깨끗한 체크아웃에서도 똑같이 실패하므로
  W1 때문이 아닙니다.

### W4a (능력 프로브와 설치 스크립트, 2026-10-22)

구현: `configs/worker-requirements.json`, `cli/llm-agent/worker_probe.py`(판정 논리와 인벤토리 수정),
`scripts/probe-worker.sh`, `scripts/install-worker.sh`, `scripts/lib/{worker-facts,worker-common,worker-remote-install,llm-check}.sh`,
`deploy/worker/{files.txt,guardian-worker.service}`. 테스트: `test_worker_probe.py`(판정, 인벤토리 수정),
`test_worker_install.py`(파일 집합이 닫혀 있음), `scripts/test_worker_scripts.py`(가짜 fleet에서 두 스크립트를 처음부터 끝까지:
가짜 `ssh`, `systemctl --user`, `loginctl`, 데몬은 진짜 워커). 설계와 달라졌거나 알아 둘 점:

- **플래그 둘, 기록자 둘.** 프로브는 `worker_capable`(`passed`, `checked`, `max_jobs`, `clis`, `reasons`)을 쓰고, 실패한
  프로브는 `worker: false`도 씁니다. `worker: true`와 `worker_install`(`rev`, `installed`, `max_jobs`, `version`)은 설치
  스크립트만 쓰며, 프로브 통과 **그리고** 수락 점검 통과 뒤에만 씁니다. `--disable`은 `worker`를 끄고 `worker_install`을
  지웁니다. 둘 다 `~/.ssh/guardian_fleet.conf.lock`(허브 에이전트의 인벤토리 락)에 `flock`을 잡고 `.bak`을 남기며 허브는
  거부합니다.
- **수정은 허브에서 실행됩니다**(`worker_probe.py`를 ssh로 `python3 -`에 흘려 보냄). 노드를 켜는 데 허브 릴리스가 필요
  없습니다. `HUB_INVENTORY_PATH`로 두 스크립트를 다른 인벤토리(예: `~/guardian-eval`의 것)로 돌릴 수 있어, 시험이
  운영 노드에 표시를 남기지 않습니다.
- **사용자 유닛과 linger.** CLI 로그인은 계정 홈에 있으므로 워커는 systemd *사용자* 유닛이고 `loginctl enable-linger`가
  필요합니다. 설치 스크립트가 시도하며(안 되면 `sudo -n`), 안 되면 파일은 남지만 노드를 **표시하지 않고** 실행할 명령
  하나를 알려 줍니다. `deploy/hub/install.sh`는 유닛 목록을 명시해 복사하므로 `deploy/worker/`를 가져가지 않습니다.
- **작업 트리가 아니라 git 리비전에서 설치**(`--rev`, 기본 HEAD)하며, `~/.guardian/worker/`에 평평하게 풀고 `REVISION`
  파일을 둡니다. 이전 파일은 `previous/`에 복사해 두고 `restore`가 되돌립니다. 점검에 실패한 첫 설치는 멈추고
  (`systemctl --user disable --now`), 실패한 업데이트는 이전 버전을 복원하며 원래 가지고 있던 플래그를 유지합니다.
- **수락 점검 = 허브 자신의 경로**: `허브 -> ssh -> worker.py client -> 소켓`, 최대 8회 시도하며 `ok`, 프로토콜,
  스테이징한 파일의 `code_version`과 같은 `version`, 비어 있지 않은 `providers`를 요구합니다. 남는 빈틈: ping은 데몬이
  띄우는 작업의 환경(유닛의 PATH와 CLI가 스스로 필요로 하는 것)을 거치지 않으며, 첫 시험 토론이 그것을 확인합니다.
- **종단 테스트가 찾아낸 것:** `ping`이 요청마다 파일을 다시 해시해서, 재시작이 일어나지 않은 옛 프로세스가 *새* 버전을
  보고하고 점검을 통과했습니다. 이제 워커는 시작할 때 읽은 버전(`STARTED_VERSION`)을 보고합니다.
- **파일 집합은 닫혀 있습니다**(`deploy/worker/files.txt`: `worker.py`, `remote_agents.py`, `llm_runner.py`,
  `env_policy.py`). `group_manager`(허브 전용)와 `repo_mcp`(W2)는 지연 import라 노드에 없고, 목록에 없는 새 저장소
  import가 생기면 `test_worker_install.py`가 실패합니다. W2는 `repo_mcp.py`(와 그것이 import하는 것)를 목록에 넣어야 합니다.
- 프로브 기준: `min_total_memory_mib` 4096, `free_memory_mib_per_job` 1536, `default_max_jobs` 2, `python_min` 3.9,
  `min_free_disk_mib` 1024, `min_working_cli` 1. 노드가 보고하지 않은 사실은 그 조건을 실패시킵니다. 허브에서 노드로
  가는 경로가 막혔으면 `hub_path` 조건이 실패하고(판정), 노드에 닿지 못하면 프로브 실패(종료 코드 1, 판정 없음, 기록 없음)입니다.
- 먼저 가짜 fleet에서 검증했고, 2026-10-22에 처음 실제 노드에서 돌렸습니다(운영자 지시: Tier-1 호스트에 워커 설치).
  허브를 뺀 Tier-1 노드 6대가 모두 프로브를 통과했고(RAM 11.9GB, python 3.12 또는 3.14, claude·codex·agy가 PONG에 응답,
  여유 디스크 25-39GB) `max_jobs`는 2입니다. 설치는 리비전 `3aae65efebb8`에서 했고 수락 ping이 모두 첫 시도에 통과했습니다
  (버전 `de01a295ab28`, 여유 슬롯 2/2). ping으로는 알 수 없는 부분은 한 노드에서 직접 확인했습니다. 허브 → ssh →
  `worker.py client`로 보낸 실제 `run` 요청이 `claude-cli`의 `PONG`을 돌려줬습니다. 업로드 중 macOS `tar`의
  `LIBARCHIVE.xattr` 경고는 무해합니다. W1의 시험 토론은 아래에 있고,
  운영 팀에는 `host`가 없어 운영 토론은 아직 워커를 쓰지 않습니다.
- W4b는 버리지 않았습니다. 먼저 대시보드 팀 편집기가 모델에 없는 멤버 필드를 저장 때 되돌려 쓰는지(`host`가 지워지는지)
  확인해야 합니다. **2026-10-27 확인: 지워졌습니다.** `TeamManager.save`는 받은 payload로 파일을 통째로 교체하는데
  편집기는 자기가 아는 키만 보냈기 때문에, 대시보드에서 팀을 저장하면 `host`와 팀 수준의 `flow`, `sides`도 사라졌습니다.
  편집기를 고쳤습니다(`Member.extra`, `teamExtras`, `Draft.extra`): 모델에 없는 멤버·팀 키는 그대로 통과시켜 다시
  씁니다(`debate.lib.test.ts`, `settings.test.tsx`). `host` 입력란 자체는 W4b에서 편집기에 들어갔습니다(아래).

### W2 (저장소 스냅샷과 원격 `repo_mcp`, 2026-10-27)

- 구현: `repo_snapshot.py`, 프로토콜 2(`put-snapshot`, `snapshot_has`, `run`의 `repo` 필드, 응답의 `calls`),
  다섯 개의 `SNAPSHOT_TOOLS`만 제공하는 `repo_mcp.py --no-index`, 엔진의 원격용 문구, `test_remote_repo.py`(44개),
  `SeededDefectTests`(읽은 파일과 읽지 않은 파일의 인용에서 로컬·원격 `citation_check` 결과가 같음). `host`와
  `code_tools: "mcp"`를 함께 쓰면 거부하던 팀 검증은 제거했습니다.
- **위 설계 본문과 다른 점.**
  - 스냅샷은 `git archive`를 풀어 워커에서 커밋하는 방식이 아닙니다. 허브가 결정적인 `git fast-import` 스트림(zlib,
    ssh 모드 `put-snapshot KEY`)을 보내고, 워커가 `snapshots/KEY`의 bare 저장소로 가져옵니다. 키는 (모드, blob id,
    경로)의 해시라 파일 집합이 바뀌면 새 스냅샷입니다. 워커는 허브로부터 경로나 명령을 받지 않습니다. `repo` 필드는
    키만 지정하고 MCP 사양은 데몬이 만듭니다.
  - 2 MiB를 넘는 파일과 합계 64 MiB를 넘는 부분은 제외하므로 원격 `repo_list`가 허브보다 짧을 수 있습니다. 제외된
    파일은 증거로도 인정되지 않습니다. 라우터가 돌려받은 `paths`와 `spans`를 스냅샷에 있는 파일로만 걸러, 워커가
    갖고 있지 않은 파일을 "보여준 것"으로 만들 수 없습니다.
  - 정리는 "스레드가 끝날 때"가 아니라 TTL/LRU입니다: 사용 시점에 `GUARDIAN_WORKER_SNAPSHOT_TTL`(24시간)과 `_KEEP`(8).
  - 원격 지도에는 Go facts가 없고, 원격 역할에는 `repo_defs`, `repo_refs`, `repo_callers`, `repo_callees`, 위키 도구가
    아예 없습니다("사용할 수 없음"을 답하는 도구 대신 프롬프트가 그렇게 알려 줍니다). W2b는 만들지 않았습니다.
  - 호출 로그는 작업이 끝날 때만 돌아오므로(상한 8 MiB, 초과는 오류) 원격 토론 에이전트의 검색 이벤트는 실시간이 아니라
    턴이 끝날 때 나타납니다. 워커 실행이 실패하면 그때까지의 호출은 잃어버리며, 라우터가 허브로 폴백하면 새 세션이
    시작되므로 세지 않습니다.
  - 프로토콜 1 -> 2, 워커 파일 집합이 4개에서 16개로 늘었고(`deploy/worker/files.txt`), **git이 프로브 조건**이
    되었습니다(`git_version` 사실, 조건 `git`). 워커 설치 스크립트의 `CODE` 목록은 파일 집합과 같아야 합니다
    (`test_worker_install.py`가 확인합니다. 이 변경의 첫 버전은 이를 놓쳤고 설치 시험이 찾아냈습니다).
    프로토콜 1 노드의 롤백은 예전 4개 파일만 든 백업을 복원하므로, `restore`는 전체 집합이 아니라 백업에 있는 파일
    (최소 `worker.py`와 `remote_agents.py`)만 검증합니다. 남은 새 파일은 예전 코드에 해롭지 않습니다.
- **적용 순서.** 설치된 워커 6대는 프로토콜 1입니다. W2가 들어간 허브는 이들에 대한 모든 호출을 거부하고(프로토콜 불일치
  -> 허브로 폴백, 실패 기록 한 줄), 각 노드에서 `scripts/probe-worker.sh --record`로 새 `git` 조건을 확인한 뒤
  `scripts/install-worker.sh`를 다시 실행해야 합니다.
  **2026-10-27 완료**: Tier-1 워커 6대 모두 `git` 조건을 기록한 프로브, `HEAD` 설치를 마쳤고 `--workers` 리포트로 프로토콜 2와
  git을 확인했습니다. 허브는 2026-10-28에 `d84562c`로 배포해 프로토콜 2를 쓰지만, 프로토콜 2 워커 호출은 아직 관찰하지 못했습니다.

### W4b (팀 편집기 `host`와 워커 상태, 2026-10-27)

- 구현: `remote_agents.worker_report()` / `worker_choices()`, `guardian.py --workers`, Go 라우트 `GET /api/workers`
  (읽기 전용, `/api/teams`와 같은 `configGet` 통과 방식, CLI 제한 시간 30초), 팀 카탈로그의 `host_labels`·`workers`
  (켜진 워커 id, 네트워크 없이 인벤토리에서 읽음), 팀 편집기의 `host` 선택, 팀 목록의 `host` 표시, 설정 → 워커.
- 허브 에이전트는 워커와 직접 통신하지 않습니다. `--workers`가 켜진 노드를 병렬로(노드당 10초, 캐시 없이) 새로
  ping하고, 켜졌거나 프로브 기록이 있는 인벤토리 노드마다 한 행을 돌려줍니다. `state`는 `up`, `down`(오류 포함),
  `off`(ping하지 않음)이고, `up`이면 코드 버전과 허브가 지금 설치할 버전과 같은지, 프로토콜, 여유 슬롯, 공급자, git도
  담습니다. 인벤토리를 읽지 못하면 HTTP 오류가 아니라 응답의 `error`로 알립니다.
- `Member.host`는 이제 편집기의 정식 필드입니다(전에는 `extra`로 통과만 시켰음). 선택지는 허브(`host` 없음), 배치 라벨
  두 개, 켜진 워커입니다. 켜진 워커가 없고 멤버에 저장된 값도 없으면 필드를 숨깁니다(워커가 없으면 라벨도 의미가 없습니다). 허브가 더 이상 내주지
  않는 저장값은 경고 표시와 함께 선택 가능하게 남겨, 저장할 때 조용히 사라지지 않습니다.
- 설계와 다른 점: 설계에는 "읽기 전용 API"만 있었습니다. 배치 검증은 그대로입니다(저장 시 `host_syntax_error`, 실행 시
  `inventory_host_error`). 편집기는 이를 중복하지 않습니다.
- **W3는 만들지 않았습니다.** 감사 줄(`source=ops_check`)은 허브 에이전트가 exec의 부수 효과로 남깁니다. 워커가 로컬에서
  점검하면 이 줄이 사라지고, 되살리려면 기록 전용 감사 엔드포인트가 필요한데 토큰을 가진 누구나 감사 항목을 위조할 수
  있게 됩니다. 엔진은 이미 원격 역할의 ops 점검도 허브에서 실행하므로 W3의 "완료 조건"은 지금도 충족되고, 로컬 실행은
  SSH 왕복 한 번을 아낄 뿐입니다. 권고: 그 왕복이 문제가 되지 않는 한 W3는 버립니다. 결정은 운영자의 몫입니다.

### W1 시험 토론 (2026-10-22)

- 구성: 허브 샌드박스 `~/guardian-eval/data-w1`(자체 `GUARDIAN_DATA_DIR`, 운영 데이터 없음), 허브 릴리스 코드, 팀
  `guardian-dev-w1` = `guardian-dev`에서 `max_rounds`를 1로 하고 Critic 셋을 다르게 배치: 보안(제공자 codex, agy)은
  `host: "worker:any"`, QA(claude)는 `host: "worker:other"`, 릴리스(agy, codex)는 허브. 과제는 `TeamManager.save`와
  `validate_team_meta` 검토. 공유 Codex 쿼터 사용은 운영자가 승인했습니다.
- 결과: 1라운드 `APPROVED`, jev 게이트 `PASSED`, 액션 2건 제안, 전체 8분 22초. 보안 Critic은 워커 노드 A에서
  `codex-cli`로(63초), QA Critic은 워커 노드 B에서 `claude-cli`로(204초) 실행했고, 두 `agent_started`에는 `host`가
  있고 허브 쪽 토론 에이전트에는 없습니다. 릴리스 Critic은 허브(`agy-cli`, 221초), Expert와 Judge도 허브(`claude-cli`, 169초와
  111초)입니다. 워커 Critic 둘은 허브 Critic과 병렬로 돌았고, 어떤 라운드·토론 에이전트·이벤트에서도 허브나 대체 응답으로의
  폴백은 보고되지 않았습니다.
- 확인된 것: `host` 배치(`worker:any`, `worker:other`), 실제 모델 CLI로 허브 → ssh → 워커 경로, 결과가 공유 Judge 마무리로
  이어지는 흐름. 확인되지 않은 것: 실제 노드에서의 폴백 경로(가짜 플릿 시험만 있음), 워커가 바쁜 경우(`max_jobs` 2에
  닿은 적 없음), 허브와 워커 실행의 품질 차이(토론 한 번).

### 스냅샷 경로 검사 (2026-10-30)

`import_stream`은 트리 밖을 가리키는 경로를 `git fast-import`가 거부해 주리라 믿었다. git 2.43.0(Ubuntu 24.04의 기본값)은 `../escape.txt`를
그대로 저장하므로 그 환경에서 `test_remote_repo.HostileUploadTests`가 실패했고, 그 git을 쓰는 워커에는 이 검사가 없었다. 탐침은 git이
있는지만 보고 버전은 보지 않는다. 이제 워커가 가져온 트리를 나열하고(`git ls-tree -r -z --name-only`), 경로에 빈 구성 요소, `.`나 `..`
구성 요소, 대소문자와 상관없이 `.git` 구성 요소가 있으면 업로드를 거부하고 아무것도 남기지 않는다(`repo_snapshot.unsafe_path`). 예전에도
새어 나간 것은 없었다. 스냅샷은 체크아웃하지 않는 bare 저장소이고, `RepoTools`는 git 객체를 읽으며 요청한 경로의 `..`를 거부한다. 허브가
만드는 스냅샷에는 그런 경로가 없다. 워커는 다시 설치해야(`install-worker.sh`) 이 검사를 받는다. 스트림 형식과 `SNAPSHOT_FORMAT`은 그대로다.

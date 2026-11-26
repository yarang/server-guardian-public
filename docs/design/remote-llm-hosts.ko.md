# 원격 LLM 호스트: Tier-1 호스트에서 에이전트 턴 실행 (설계)

[English](remote-llm-hosts.md) | 한국어

상태: 2026-10-30 제안. 운영자가 결정 3건을 확정했다("결정" 참조). R1을 만들지는 R0(측정)이 정한다. R0 완료:
허브에서 7일(2026-10-30 ~ 2026-11-06) 측정한 판정이 `not-needed`라서 R1-R3은 만들지 않는다
(`../analysis/2026-11-08-r0-llm-load/results.ko.md`).
배경: 허브가 프로젝트 소스코드를 어디에 두는지에 대한 논의. 코드를 다른 서버로 옮기고 SSH로 읽는 방식은 기각했다(토론의
`git cat-file`이 모두 네트워크를 건너고, Go facts는 로컬 worktree와 툴체인이 필요하며, 조치와 테스트는 workspace에 쓴다).
남은 질문은 **에이전트**를 다른 곳에서 실행할 수 있느냐였다. 이 문서는 부하 분산 관점에서 그 답을 설계한다. 허브의 소스코드
저장 공간은 별도 주제다.

## 문제

모든 토론 에이전트(Expert, proposer, Critic, Judge)는 허브에 있는 엔진의 LLM CLI 하위 프로세스다(`llm_runner.py`,
`agent.ko.md` 1장). 허브는 2 OCPU, 12 GB ARM 호스트이고 Gitea, Go 에이전트, 대시보드, 타이머, fleet SSH도 함께 돌린다.
Tier-1 호스트(12 GB ARM, `"role": "llm_cli_host"`)에는 claude, codex, agy가 이미 설치되고 로그인되어 있지만 운영자만 쓴다.

에이전트 턴마다 허브가 실제로 돌리는 것은 대부분 모델 응답을 기다리는 CLI 프로세스(수백 MB의 Node 또는 Rust 프로세스)다.
엔진 자체의 일(`repo_tools`를 통한 조회, 코드 인덱스, 근거 검사, DB)은 작고, 코드 옆에 있어야 한다.

지금까지의 수치로는 허브가 포화 상태라는 근거가 없다. `multi-perspective.ko.md`의 P0 표본(최근 토론 10건: 에이전트 실행
중앙값 95초, 최대 374초)과 평가(가장 넓은 구성의 최대 메모리 1 GB 미만)가 그렇다. `multi-perspective.ko.md`("범위 밖")는
물리적 분산을 다시 검토할 조건을 적어 두었다. 허브가 포화되었을 때, 또는 별도 계정이나 쿼터가 필요할 때다. R0은 앞의
조건을 측정하고, 뒤의 조건은 나중을 위해 열어 둔다(결정 3).

## 결정 (2026-10-30, 운영자)

1. **목적: 허브 부하 분산.** 관점 다양성(그것은 `multi-perspective.ko.md`의 논리적 분리)도, 디스크 공간도 아니다.
2. **Standing decision "fleet workers 없음"(CLAUDE.md)을 LLM CLI 턴에 한해 완화한다.** 엔진, 조회, MCP 서버, 근거와 인용
   검사, jev, DB, 조치, 게시, ops 점검은 허브에 남는다.
3. **계정:** Tier-1 호스트는 지금 허브와 같은 계정으로 실행한다. 필요하면 호스트에 다른 계정을 써도 된다. 인증 정보는
   호스트 사이에 복사하지 않는다(CLAUDE.md). 각 호스트가 직접 로그인한다.

## 제안 (미결정)

- P-1 **코드는 허브를 떠나지 않는다.** 원격 턴은 프롬프트만 받는다. 코드는 repo 블록이나 MCP 브리지로만 모델에 닿고, 둘 다
  허브에서 서비스되므로 호출 로그와 line-span 규칙이 그대로 동작한다.
- P-2 **기본 꺼짐, 호스트별 옵트인.** 허브의 `GUARDIAN_LLM_REMOTE=on`과 호스트의 `llm_slots` > 0.
- P-3 **허브는 풀에 남고 대체 실행처가 된다.** 허브가 실행할 수 있는 턴은 원격 실패 때문에 실패하지 않는다.
- P-4 **세션은 만든 호스트에 머문다**(claude와 codex는 세션을 호스트별로 저장한다).
- P-5 **Tier-1 호스트에 데몬을 두지 않는다.** 허브가 SSH로 실행하는 표준 라이브러리 스크립트 하나만 배포하고, 그 스크립트는
  아무 포트도 듣지 않는다.

## 무엇이 어디서 도는가

| 부분 | 지금 | 원격 호스트 도입 후 |
|---|---|---|
| 엔진, 라운드 루프, 이벤트, DB | 허브 | 허브 |
| `repo_tools`, 코드 인덱스, 맵, Go facts, 위키 | 허브 | 허브 |
| ```repo 블록 (엔진이 읽고 다음 턴에 보여 줌) | 허브 | 허브 (변화 없음: CLI는 코드가 필요 없다) |
| `repo_mcp.py` (claude/codex MCP, `GUARDIAN_REPO_MCP=1`) | 허브에서 CLI의 자식 | 여전히 허브. SSH 소켓 포워딩으로 연결(R2) |
| LLM CLI 프로세스 (claude, codex, agy) | 허브 | Tier-1 호스트 또는 허브 |
| 세션 파일 (`~/.claude/projects`, `~/.codex/sessions`) | 허브 | 세션을 실행한 호스트 |
| 조치, 테스트, 게시, ops 점검, jev | 허브 | 허브 |

## 설계

### 호스트 풀

호스트는 허브 인벤토리(`~/.guardian/data/servers.json`)에서 온다. 조건은 `"role": "llm_cli_host"`이고 `cli_support`가
provider를 나열하며, 선택 필드 두 개를 새로 둔다.

- `llm_slots` (정수, 기본 0 = 쓰지 않음): 그 호스트에서 동시에 돌릴 수 있는 Guardian 턴 수. 12 GB 호스트는 2부터 시작하면
  적당하다.
- `llm_account` (자유 라벨, 예: `acct-a`, 이메일 주소는 금지): 같은 라벨의 호스트는 한 계정을 공유한다.

허브 자신의 몫은 `GUARDIAN_LLM_HUB_SLOTS`(기본: `GUARDIAN_MAX_PARALLEL_AGENTS`)다. 계정 단위 상한
`GUARDIAN_LLM_ACCOUNT_SLOTS`(기본 미설정)는 `llm_account`별 동시 턴 수를 묶는다. 호스트를 늘려도 같은 계정의 rate limit에
더 세게 부딪히기만 하는 일을 막기 위해서다.

### 배치

- 단위는 에이전트 턴 하나다. 세션을 이어 가는 턴은 그 세션의 호스트로 간다(P-4). 새 세션은 provider를 가지고 probe를 통과한
  호스트 중 빈 슬롯이 가장 많은 곳으로 간다. 동점이면 허브를 고른다(네트워크가 없으므로).
- 슬롯은 프로세스를 넘어 공유된다. 토론마다 `guardian.py` 프로세스가 따로 돌기 때문에 슬롯은
  `<GUARDIAN_DATA_DIR>/llm-hosts/<host>/slot-<n>.lock`의 `flock`이다(코드 위키, 감사 잠금과 같은 방식). 프로세스가 죽으면
  잠금도 풀린다.
- probe: `guardian-llm-exec probe`가 CLI 버전, load average, `MemAvailable`을 돌려준다. 5분 동안 캐시한다. probe나 접속에
  실패한 호스트는 10분 동안 제외한다. runner가 요구하는 버전(`CLAUDE_RECORDED_PROMPT_VERSION`)보다 오래된 claude가 있는
  호스트는 claude 턴에 쓰지 않는다.

### 원격 실행: `guardian-llm-exec`

각 Tier-1 호스트에 설치하는 표준 라이브러리 Python 스크립트다(`scripts/deploy-llm-skills.sh` 또는 그 옆의 스크립트로
배포). 허브는 이를 `ssh -o BatchMode=yes -o ServerAliveInterval=15 <host> python3 ~/.guardian-llm/guardian-llm-exec run`으로
실행한다. 스크립트는 stdin에서 JSON 봉투 하나를 읽는다.

```json
{"argv": ["claude", "--print", "..."], "env": {"API_TIMEOUT_MS": "..."}, "workdir": "~/.guardian/llm-sessions",
 "files": {"system.txt": "..."}, "stdin": "...", "outputs": ["last_message.txt"], "timeout": 600}
```

- `files`는 전용 임시 디렉터리(0700)에 쓰고, `argv`의 `{tmp}`가 그 디렉터리를 가리킨다. `outputs`는 실행 후 다시 읽는다.
  임시 디렉터리는 항상 지운다.
- `argv[0]`은 claude, codex, agy 중 하나여야 하고, `env` 키는 허용 목록에서만 받는다. 프롬프트 텍스트는 명령줄에 오르지
  않으므로 셸 인용 문제가 없다.
- CLI는 자기 프로세스 그룹에서 돈다. 스크립트의 stdin이 일찍 EOF에 닿거나(SSH 연결이 끊김, 허브가 취소함) 타임아웃이 지나면
  그룹 전체를 kill한다. 취소와 타임아웃은 이 방식으로 원격 호스트에 전달된다.
- 응답은 stdout의 JSON 객체 하나다. 종료 코드, stdout, stderr(끝부분), outputs가 들어 있다.

`llm_runner.py`에는 이음매 하나만 생긴다. CLI를 실행하는 모든 `subprocess.run`이 로컬 또는 원격 runner를 거친다. 무엇을
실행할지 정하는 부분(플래그, 잠금 설정, 대체 순서, 파싱)은 그대로다.

### 원격 호스트에서의 격리

허브와 같은 잠금 설정을 쓴다. claude는 내장 도구 없이 `--strict-mcp-config`로 실행하고, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`를
켠다(운영자의 `~/.claude/CLAUDE.md`와 skill이 토론 세션에 닿으면 안 된다). codex는 `--ignore-user-config`와
`--sandbox read-only`로 실행하고, 비어 있는 전용 workdir를 쓴다.

바뀌는 것은 저장소 텍스트에서 오는 prompt injection의 영향 범위다. 파일을 읽을 수 있는 CLI(codex의 read-only 샌드박스)는
그 사용자가 **그 호스트에서** 읽을 수 있는 것을 읽을 수 있고, 그 답은 허브의 DB에 남는다. Tier-1 호스트에는 fleet 키가
없으므로 그보다 멀리 가지는 못한다. R3은 호스트마다 전용 사용자(`guardian-llm`)를 두고, 그 사용자가 직접 로그인하며 홈에는
다른 것을 두지 않는 방안을 제안한다.

### 세션

- 저장된 에이전트 세션에 `host`를 추가한다(비어 있으면 허브, 기존 기록은 모두 이 값).
- resume은 그 호스트로 간다. 호스트를 쓸 수 없으면 다른 곳에서 새 세션으로 시작한다. 기존의 "resume 실패, 다음 provider"
  경로와 같고, thread digest가 이전 라운드를 넘겨준다.
- 세션이 원격이면 `guardian.py --attach`가 tmux 안에서 `ssh -t <host> <resume 명령>`을 연다.
- 디버깅할 때 transcript는 그 호스트의 `~/.claude/projects/...`에 있다. `agent_started`가 호스트를 기록한다.

### MCP 브리지 (R2)

`McpTurn`은 이미 턴마다 허브에 로그 하나를 둔다. 원격 claude나 codex 턴이면 허브가 그 턴의 전용 디렉터리에 Unix 소켓을
하나 더 열고, SSH를 `-R <원격 소켓>:<허브 소켓>`(stream-local 포워딩)으로 실행한다. 호스트의 CLI 설정에 들어가는 MCP 명령은
`guardian-llm-exec bridge <원격 소켓>`이고, stdio를 소켓으로 복사하는 일만 한다. 허브는 연결마다 지금과 같은 인자로
`repo_mcp.py`를 띄운다. 코드, 한도, 호출 로그는 허브에 남는다. 포워딩된 소켓은 그 턴의 SSH 연결이 열려 있는 동안만 있다.
R2 전까지 `code_tools: "mcp"` 멤버의 턴은 허브에서 돌리거나 repo 블록으로 대체한다(R1에서 결정, 허브가 더 안전한 기본값).

### 이벤트와 대시보드

`agent_started`와 `agent_finished`에 `host`를 싣는다(허브의 `debate_started.host`는 계속 엔진 호스트다). 호스트를 건너뛰면
새 경고 이벤트 `llm_host_unavailable`을 낸다. 대시보드는 에이전트 턴마다 호스트를 보여 준다. 설정 -> 서버에 `llm_slots`를
보여 주는 호스트 열을 둔다(R3).

## 단계

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| R0 | `scripts/llm_load_probe.py`로 측정("R0 절차" 참조): 실제 토론 중 허브 부하와 CLI 프로세스의 몫, 과거 턴의 동시 실행 수, Tier-1 호스트의 CLI 버전 | 아래 판정 규칙이 `go`이면 진행, 아니면 여기서 멈춤 |
| R1 | `guardian-llm-exec run/probe`, 인벤토리 필드, 슬롯, 배치, 허브 대체 실행, 이벤트의 `host`. 새 세션과 repo 블록 멤버만 | 같은 토론 과제가 로컬과 원격에서 같은 판정 종류를 내고, 허브가 돌릴 수 있는 턴이 실패하지 않음 |
| R2 | 세션의 호스트 고정, resume, `--attach`, MCP 브리지 | resume과 MCP 멤버가 있는 다라운드 토론이 원격에서 끝까지 돎 |
| R3 | 호스트별 전용 사용자, `llm_account` 상한, 대시보드 호스트 열과 설정 | 운영자가 대시보드에서 호스트를 켜고 끌 수 있음 |

## R0 절차

2026-10-30 결정: R1을 만들지는 R0이 정한다. 도구는 `scripts/llm_load_probe.py`다(표준 라이브러리, 허브에서 읽기만 하고
모델 쿼터를 쓰지 않음). 쓰는 곳은 `~/guardian-eval/r0`뿐이고 운영 데이터 디렉터리에는 쓰지 않는다.

```bash
# Mac의 checkout에서: 스크립트를 허브의 평가 디렉터리에 둔다
ssh <hub> 'mkdir -p ~/guardian-eval/r0' && scp scripts/llm_load_probe.py <hub>:guardian-eval/r0/
# 허브에서
cd ~/guardian-eval/r0
python3 llm_load_probe.py history --days 30          # 과거 턴: 걸린 시간, provider, 동시에 몇 개가 돌았는지
python3 llm_load_probe.py hosts                      # Tier-1 호스트: claude/codex/agy 버전, CPU, 메모리, 부하
nohup python3 llm_load_probe.py sample --duration 7d > sample.log 2>&1 &   # 평소처럼 토론이 도는 동안 5초 표본
python3 llm_load_probe.py report                     # 언제든 실행, 마지막 줄이 판정
```

`sample`은 모든 프로세스를 조상 관계에 따라 네 분류 중 하나로 나눈다. `llm`(엔진이 띄운 LLM CLI: 원격 호스트가 가져갈
몫), `retrieval`(`repo_mcp.py`), `engine`(`guardian.py`와 그 밖의 자식), `other`(Gitea, Go 에이전트, 운영자 자신의 CLI
세션)다. 두 표본 사이에 끝난 자식의 CPU는 부모의 `cutime`/`cstime`에서 두 번 세지 않고 가져온다. 구간 안에 엔진이 띄운
LLM CLI가 돌았으면 그 표본은 busy이고, 보고서는 busy 표본을 대상으로 한다.

판정 규칙(데이터를 보기 전에 고정, 스크립트의 `DECISION_RULE`):

| | 기준 | 임계값 |
|---|---|---|
| 데이터 충분 | busy 표본 수 | 360 이상 (5초 간격으로 토론 30분) |
| 압박 (하나라도) | 허브 CPU 사용률 95번째 백분위 | 70 % 이상 |
| | MemAvailable 최저값 | MemTotal의 15 % 미만 |
| | PSI cpu some avg10, p95 | 20 이상 |
| | PSI memory some avg10, p95 | 10 이상 |
| 몫 (하나라도) | `llm` CPU tick / 허브 busy tick | 50 % 이상 |
| | `llm` RSS / 사용 중 메모리, p95 | 50 % 이상 |

판정: `go`(압박과 몫이 모두 충족: R1을 만든다), `not-needed`(압박 없음: 허브가 감당한다, 기록하고 멈춘다), `elsewhere`(압박은
있지만 그 주된 몫이 CLI가 아님: 원격 호스트로는 해결되지 않는다, 엔진이나 Gitea를 본다), `insufficient`(busy 표본 부족: 계속
표본을 모은다). 결과는 호스트 이름을 placeholder로 바꿔 `docs/analysis/<날짜>-r0-llm-load/`에 남긴다. 같은 계정의 병렬
rate limit 시험은 R0에서 뺐다. 공유 쿼터를 쓰고, R1을 만든 뒤에야 필요하다(`GUARDIAN_LLM_ACCOUNT_SLOTS`).

## 효과 확인 방법

같은 과제 세트(`proposer_eval.py`의 결함 심기 과제)를 원격을 끈 상태와 켠 상태로 돌린다. 측정 항목은 실행 중 허브의 최대
RSS와 CPU, 걸린 시간, 대체 실행 비율, 점수다. 점수는 바뀌면 안 된다(같은 모델이 같은 근거를 읽으므로).

## 범위 밖

- 프로젝트 소스코드를 허브 밖으로 옮기거나 SSH로 읽는 것.
- 엔진, ops 점검, 조치, jev를 허브가 아닌 곳에서 돌리는 것. Tier-2 호스트.
- 관점 다양성(`multi-perspective.ko.md`).
- 호스트 사이의 인증 정보 복사.

## 위험과 열린 문제

- 같은 계정에서 병렬도만 늘어남: 턴을 분산하면 계정의 rate limit에 더 빨리 닿을 수 있다. `GUARDIAN_LLM_ACCOUNT_SLOTS`로
  묶고, R1을 만든 뒤에 측정한다(공유 쿼터를 쓰므로 R0에서는 하지 않음).
- 호스트 사이 CLI 버전 차이: probe가 오래된 claude는 거부하지만, codex나 agy의 출력 형식 변화는 호스트마다 다를 수 있다.
  `deploy-llm-skills.sh`가 버전을 보고하게 할 수 있다.
- 네트워크: 허브와 호스트 사이는 Tailscale이다. 연결이 끊기면 원격 턴은 의도대로 종료되고 허브가 로컬에서 다시 실행한다.
  그 턴의 시간만큼 손해를 본다.
- 이 설계는 엔진의 몫을 줄이지 않는다. R0에서 엔진(Go facts, 인덱스 빌드)이 주된 부하로 나오면 답은 여기가 아니라 다른
  곳(캐시, 스케줄링)에 있다.
- 팀이 자신을 허브에 고정할 수 있어야 하는가(`placement: "hub"`, 예: 평가용)? 열린 문제.

## 구현 메모

- R0 (2026-10-30): `scripts/llm_load_probe.py`와 `scripts/test_llm_load_probe.py`. 위 표와 달라진 점: 표본은 `ps` 대신
  `/proc`에서 읽는다(프로세스별 CPU tick과 reap된 자식). rate limit 시험은 R0에서 뺐다("R0 절차" 참조). `history`를
  추가했다(`events.jsonl`에서 과거 턴의 동시 실행 수를 보는 일은 비용이 없고, 분산할 턴이 얼마나 되는지 알려 준다). 엔진
  조상이 없는 LLM CLI(운영자 세션)는 `other`로 센다.
- R0 결과 (2026-11-08, `../analysis/2026-11-08-r0-llm-load/`): 판정 `not-needed`. 표본 120 958개, 바쁜 표본 2 238개.
  바쁜 동안 CPU p95 22 %, 최저 MemAvailable 86 %, PSI cpu some p95 3.0, memory 0. `llm` CPU 몫 51 %(몫은 넘었고
  압박은 없음). CPU 70 % 이상인 바쁜 표본 14개는 엔진의 몫이었다. 달라진 점: RSS에는 MemAvailable이 여유로 세는 파일
  매핑 페이지가 들어가서 `other` 메모리 몫이 100 %를 넘는다. 판정은 이 값을 쓰지 않는다. 다시 잴 때는 RSS를
  MemTotal과 비교하거나 `Pss`를 읽는다.

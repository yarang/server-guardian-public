# Server Guardian

[English](README.md) | 한국어

Server Guardian은 허브 한 대에서 Linux 노드 플릿을 관리한다. 메트릭을 수집하고, 모든 노드의 sshd를 감사하고,
SSH로 명령을 실행하며, LLM CLI(claude, codex, agy)로 멀티 에이전트 토론을 실행한다. 토론은 저장소의 코드를 고치거나
(code 프로젝트) 노드를 점검하고 정리한다(운영(ops) 프로젝트). 토론이 제안한 변경은 모두 승인을 기다리며, 정책이
스스로 실행하도록 허용한 작은 범위만 예외다.

상세 시스템 명세는 [agent.ko.md](agent.ko.md)에 있다.

---

## 아키텍처

**허브 하나, 다른 에이전트 없음.** 허브(인벤토리에서 `"guardian_role": "hub"`인 항목, 여기서는 `site-a-arm1`)만
Server Guardian 허브 에이전트, 대시보드, 토론 엔진, 결정 DB를 운영한다. 나머지 노드에는 에이전트가 없고, 허브가
자신의 fleet SSH 키로 접속한다.

| 구성 요소 | 경로 | 역할 |
|---|---|---|
| 허브 에이전트 | `apps/agent/` | Go, 표준 라이브러리만 사용. 메트릭, 허브와 플릿의 sshd 감사, fleet SSH 실행, 명령·토론 API, 감사 로그. 대시보드를 서빙하고 토론과 코드 위키를 위해 Python CLI를 실행한다. |
| CLI와 토론 엔진 | `cli/llm-agent/` | Python, 표준 라이브러리만 사용. `guardian.py`(CLI), `debate_engine.py`(전문가, 비평가 패널, 판정자), LLM CLI 실행기, 코드 인덱스, 저장소 지도, Go facts, 증거·인용 검사, 코드 위키, 노드 점검과 정리 액션. |
| 대시보드 | `apps/web-desktop/` | React + TypeScript + Vite, Tauri 2 데스크톱 앱으로도 빌드. 디자인 시스템은 `packages/guardian-ui/`. |
| 운영 | `scripts/`, `deploy/` | CI, 허브 설치, LLM CLI 자격 증명과 skill, fleet SSH 키, systemd 유닛, 공개 사본 내보내기. |

허브 데이터는 `~/.guardian/data`(`GUARDIAN_DATA_DIR`)에 있다: 결정 DB, 인벤토리, 팀, 페르소나, 프로젝트, 토론 작업,
코드 인덱스 캐시, 코드 위키 페이지, 감사 로그. production 상태이며 커밋하지 않는다.

### 허브 에이전트 (`apps/agent`)
- 시스템 메트릭(CPU, 메모리, 디스크, 부하 지수, 상위 프로세스)과 SSE 메트릭 스트림.
- 허브의 sshd: `sshd_config`를 파싱하고 `sshd -t`로 검증하며, `authorized_keys`를 보여 주고, CIS 스타일 규칙 5개
  (`SEC-SSH-001`~`SEC-SSH-005`)로 0~100점을 매긴다.
- 플릿: 병렬 진단, 병렬 명령 실행(스트림 포함), 노드별 SSH 감사, 허브 SSH 설정을 통한 노드 등록.
- 토론, 액션, 프로젝트, 팀, 페르소나, 그룹, 위키 경로. 이 경로들은 `guardian.py`를 실행한다(`debate_handler.go`의
  `newCmd`).
- `127.0.0.1`과 허브의 Tailscale IP의 9870 포트에서만 대기하며 공인 IP에는 열지 않는다. 모든 `/api/` 경로는
  `AGENT_TOKEN` 베어러 토큰이 필요하다(대시보드는 이를 HttpOnly 세션 쿠키로 바꾼다). `/api/exec`는 명령을 실행한다.
- 실행 경로(`/api/exec`, `/api/exec/remote`, `/api/fleet/exec`)로 보낸 명령은 감사 로그 `<data dir>/audit/exec.jsonl`에
  남는다(`GET /api/audit`로 읽음). 운영용 로그이며 위변조 방지는 없다.

### 토론 엔진 (`cli/llm-agent`)
- **LLM CLI.** 실행기는 설치된 CLI를 각자의 구독 로그인 그대로 서브프로세스로 호출하고, 실패하면 다음 CLI로 넘어간다.
  순서: `GUARDIAN_LLM_PROVIDERS`(예: `claude,codex,agy`). 호출당 제한 시간: `GUARDIAN_LLM_TIMEOUT`(초, 기본 300,
  허브는 600). claude 추론 수준: `GUARDIAN_CLAUDE_EFFORT`(`--effort`, 기본 medium, `default`면 CLI 자체 설정). 모든
  CLI가 실패하면 내장 휴리스틱 엔진이 답하고 `built-in-expert`로 표시되며, 토론은 `UNVERIFIED`로 기록한다.
- **자격 증명.** CLI는 `~/.guardian/llm.env`(권한 600, 경로 `GUARDIAN_LLM_ENV`)를 읽으며, ssh와 nohup 실행에서도
  적용된다. `~/.claude/.credentials.json`을 여러 노드에 복사하지 않는다: refresh token이 회전되어 한 노드가
  갱신하면 나머지가 401이 된다. `claude setup-token`으로 장기 토큰을 한 번 발급해 `CLAUDE_CODE_OAUTH_TOKEN`으로
  `scripts/deploy-llm-env.sh`로 배포한다(기본은 허브만, 다른 노드는 이름을 지정할 때만, CLI마다 PONG 검사,
  `--check-only`는 점검만). Anthropic 호환 엔드포인트는 `GUARDIAN_CLAUDE_CONFIG=<설정 파일>`일 때만 적용된다.
- **팀.** 팀(라인업)은 역할마다 페르소나와 provider를 정한다: 전문가 1명, 비평가 1~4명 패널(병렬 실행, 원문 그대로
  결합), 판정자 1명. 기본 팀은 `configs/teams/`(`default`, `cross-review`, `fleet-ops`, `guardian-dev`), 페르소나는
  `configs/personas/`에 있다. 대시보드에서 저장한 팀과 페르소나는 `~/.guardian/data/teams`와 `personas`에 저장되어
  같은 id를 덮어쓴다. 팀의 `allowed_tools`는 판정자 페르소나의 도구를 좁히기만 한다. 라인업은 스레드에 함께 저장되어
  스레드를 이어 갈 때 다시 쓰인다. 설계: [debate-teams.ko.md](docs/design/debate-teams.ko.md), API 계약:
  [debate-teams-api.ko.md](docs/design/debate-teams-api.ko.md).
- **코드 읽기.** 에이전트는 ```` ```repo ```` 블록이나 읽기 전용 MCP 서버 `repo_mcp.py`로 코드를 요청한다. 심볼
  인덱스, 저장소 지도, Go facts(`tools/goindex`), 코드 위키가 길을 찾는 데 도움을 주며, 세션에 실제로 보여 준 줄만
  증거로 인정된다. retrieval 예산은 무제한이다(단계당 256 KiB를 넘으면 `retrieval_warning`). 설계:
  [debate-agent-retrieval.ko.md](docs/design/debate-agent-retrieval.ko.md),
  [debate-code-index-wiki.ko.md](docs/design/debate-code-index-wiki.ko.md),
  [code-wiki-coverage.ko.md](docs/design/code-wiki-coverage.ko.md).
- **세션.** CLI 세션(claude, codex, agy)은 스레드·역할별로 SQLite 테이블 `agent_sessions`에 저장되고 다음
  라운드에서 이어서 쓰인다. CLI 프로세스는 호출마다 종료되고 대화는 디스크에 남는다. 세션 작업 디렉터리:
  `GUARDIAN_LLM_WORKDIR`(기본 `~/.guardian/llm-sessions`).
- **jev 관문** ([jev-mcp](https://git.example.com/owner/jev-mcp)). LLM 판정자가 승인하면 토론을 jev-mcp에 ADR로
  제출하고 TypeSafe jev가 독립적으로 평가한다. jev가 `adopt`를 충분한 신뢰도로 추천하고, 근거가 타당하며, 되돌리기
  쉬울 때만 판정이 `APPROVED`다. 아니면 `DISPUTED`, 평가 전이면 `PENDING_JEV`다. 판정이 `APPROVED`가 아니면 판정자의
  액션는 `HELD`로 보류되어 실행할 수 없다. 허브에서 jev-mcp는 `~/jev-mcp`(`uv sync`)에 있고, `configs/mcp_servers.json`의
  `jev`로 설정되며, ADR DB는 `~/.guardian/data/jev/`에 있다. API 키: Mac의 `~/.config/server-guardian/llm.env`에
  `TYPESAFE_API_KEY=...`를 추가하고 `scripts/deploy-llm-env.sh site-a-arm1`을 실행한다. `GUARDIAN_JEV_GATE=off`로 끌
  수 있다(테스트용).
- **판정자 액션.** 승인된 토론은 판정자가 제안한 도구 호출을 `PROPOSED`로 저장한다.
  - code 프로젝트: `apply_patch`와 `execute_code_action`(프로젝트 클론 안의 파일만)은 jev 관문을 통과하면 승인 없이
    적용된다(`decided_by`는 `auto:engine`, 클론에만 쓰고 push하지 않음). `run_tests`와 `run_test`를 요청한 파일 쓰기는
    사람을 기다린다. 코드가 저장소에 올라가는 길은 **Publish**(아래)뿐이다. `git_commit_push`는 폐지되었고, 예전에
    저장된 `PROPOSED` 액션는 `INVALID`가 된다.
  - ops 프로젝트: 정리 액션 카탈로그와 `server_remediation`(운영(ops) 프로젝트 참고).
  - `GUARDIAN_AUTO_APPLY=off` 또는 `GUARDIAN_UI_ACTIONS=off`면 모든 자동 실행이 꺼진다. `GUARDIAN_UI_ACTIONS=off`는
    대시보드의 모든 액션·스레드 결정(실행, 거절, fail-stale, jev 재검토, 스레드 닫기, Publish, 워크스페이스 초기화)도
    막는다. 읽기 전용 조회와 CLI는 그대로 동작하고, 403 메시지가 같은 일을 하는 CLI 명령을 알려 준다.
  - 대시보드는 실행 전에 다시 확인하고(고위험 액션는 액션 번호를 직접 입력), `decided_by`에 `dashboard@<IP>`를
    기록한다.

### 운영(ops) 프로젝트 (저장소 없는 노드 관리)
ops 프로젝트(`"kind": "ops"`)는 대상 노드(노드 그룹, 노드, 제외 목록), 정책, 팀, 자체 결정 이력을 묶는다. 저장소,
워크스페이스, 테스트, 위키가 없으며 sync, publish, 워크스페이스 초기화는 거부된다. 설계와 구현 메모:
[server-ops-project.ko.md](docs/design/server-ops-project.ko.md).
- **내장 프로젝트 `fleet`** (`configs/projects/fleet.json`): 허브를 뺀 모든 플릿 노드, 팀 `fleet-ops`, 정리 액션
  카탈로그 전체와 `server_remediation`, 자동 실행 없음, 야간 점검 켜짐. 플릿·노드·그룹 화면에서 시작한 토론은 그
  범위를 포함하는 ops 프로젝트로, 없으면 `fleet`으로 간다.
- **노드 점검.** 토론 에이전트는 ```` ```ops ```` 블록으로 읽기 전용 점검을 요청한다(`disk_usage`, `largest_dirs`,
  `largest_files`, `docker_usage`, `journal_usage`, `package_cache`, `service_status`, `top_processes`,
  `uptime_load` 등). 엔진은 고정된 명령 템플릿을 허브 에이전트를 거쳐 실행하므로 모든 점검이 감사 로그에 남는다
  (`source=ops_check`). 이들은 결과를 `[#<call> <check> <host>]` 형식으로 인용하고, 엔진은 모든 인용이 실제로
  결과를 돌려준 점검을 가리키는지 확인한다.
- **정리 액션 카탈로그.** 자유 셸 대신 타입이 있는 액션: `ops:docker_prune`, `ops:journal_vacuum`, `ops:apt_clean`,
  `ops:remove_paths`, `ops:truncate_logs`, `ops:remove_old_kernels`, `ops:restart_service`. 액션마다 제안할 때 측정하고
  (**preview**: 지우거나 재시작할 대상), 실행 직전에 다시 측정하며(**drift**: 양이 절반 넘게 **그리고** 256 MiB 넘게
  늘었거나 새 항목이 생기면 `STALE`이 되어 실행하지 않음), 실행 뒤에 측정한다(**verify**: before, after, freed, ok).
  고정 거부 목록(시스템 디렉터리, 홈 디렉터리, `.ssh` 등 보호 이름, 비밀처럼 보이는 경로)은 풀 수 없고,
  `policy.deny_paths`는 여기에 더한다. 노드마다 한 번에 액션 하나만 실행된다. `jev_mcp:server_remediation`(자유 명령,
  고위험)은 카탈로그가 다루지 않는 일을 위해 남아 있으며 항상 사람의 승인이 필요하다.
- **`auto_run`.** 기본은 꺼져 있다. 프로젝트는 low 등급 카탈로그 액션(low 범위의 `docker_prune`, `journal_vacuum`,
  `apt_clean`)를 `policy.auto_run`에 `max_auto_runs_per_day`(1~100)와 함께 넣을 수 있다. 이런 액션는 jev 판정이 실제로
  `PASSED`일 때만 승인 없이 실행되고, `decided_by`는 `auto:<project>`다.
- **야간 ops 점검.** `server-guardian-ops-watch.timer`가 매일 02:30 UTC에 `guardian.py --ops-watch`를 실행한다.
  `policy.watch`가 있는 ops 프로젝트마다 대상 노드의 디스크·inode 사용률(기본 85%)과 journal 크기(기본 `3G`)를
  점검하고, 새 발견 사항이 있으면 ops 토론을 하나 시작한다. 같은 발견 사항은 7일 동안 다시 토론을 시작하지 않는다.
  `guardian.py --ops-watch --dry-run`은 보고만 한다.
- **`fleet-ops` 팀.** 전문가 `ops_sre`, 비평가 `ops_safety_critic`과 `ops_capacity_critic`, 판정자 `ops_judge`,
  최대 2라운드, 노드 도구만 허용. 프로젝트는 기본 팀(`"team"`)을 지정할 수 있다.

### 대시보드 (`apps/web-desktop`)
- 허브 에이전트가 빌드된 정적 파일을 같은 포트로 서빙한다(`http://<Tailscale IP>:9870/`). 허브에서 Vite 개발 서버를
  띄우지 않는다. Tauri 데스크톱 앱도 같은 허브에 접속한다.
- 화면: **플릿**(요약, 노드 등록과 노드 상세를 포함한 노드, SSH 감사, 명령, 그룹), **토론**(토론 시작, 실시간 에이전트
  활동, 결과, 제안된 액션, Publish, 코드 위키 탭), **승인**, **설정**(허브 연결, 프로젝트, 페르소나, 팀).
- **언어.** 기본은 한국어이고, 설정 > 허브 연결 > 언어에서 English를 고를 수 있다. 선택은 브라우저마다 저장되며
  페이지를 다시 불러온다. 메시지 카탈로그: `src/i18n/ko`, `src/i18n/en`. 설계:
  [dashboard-i18n.ko.md](docs/design/dashboard-i18n.ko.md).

### LLM CLI용 Guardian skill
`deploy/llm-skills/`에는 claude, codex, agy가 함께 쓰는 `SKILL.md` skill이 있다. `scripts/deploy-llm-skills.sh`가 이를
허브와 모든 Tier-1 노드의 `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/config/skills`에 복사한다
(`manifest.json`: `all` 또는 `hub`).
- `guardian-host-check`(모든 대상): 이 노드의 읽기 전용 점검.
- `guardian-host-cleanup`(모든 대상): 카탈로그 액션와 거부 규칙만 쓰는 이 노드의 정리. 액션마다 운영자 확인과 전/후
  측정을 거친다.
- `guardian-fleet-ops`(허브): ops 점검 dry run, ops 토론 시작과 추적, 액션 보기와 운영자가 승인한 액션의 실행. 다른
  노드를 직접 바꾸지 않는다.
- `guardian-hub-status`(허브): 허브 에이전트, 타이머, 감사, 토론 상태.

플릿 단위 변경은 허브의 Guardian을 거치므로 `AGENT_TOKEN`은 허브 밖으로 나가지 않는다. `guardian-*` skill만 관리한다.
토론에는 영향이 없다: 엔진은 claude를 `--tools ""`로, agy를 `--disable-slash-commands`로 실행한다.

---

## 빠른 시작

### 1. 허브 에이전트 빌드와 실행 (로컬 개발)
```bash
cd apps/agent
go build -o agent .
AGENT_TOKEN=$(openssl rand -hex 32) ./agent
# AGENT_TOKEN is required (the agent does not start without it). The API listens on port 9870.
```

### 2. Python CLI 실행
```bash
# Resource and security diagnosis of the local node
python3 cli/llm-agent/guardian.py "Check the node's security and resources and plan the fixes"

# One remote node
python3 cli/llm-agent/guardian.py --target site-a-arm1 "Check the remote node's resources"

# Parallel diagnosis of the whole fleet (or one node or group with --target)
python3 cli/llm-agent/guardian.py --fleet

# Run a command on every node in parallel
python3 cli/llm-agent/guardian.py --exec "uname -m" --target all

# Per-node SSH audit: table per group, then failing rules per node
python3 cli/llm-agent/guardian.py --ssh-audit
python3 cli/llm-agent/guardian.py --ssh-audit --target site-b-cluster --json

# Debates
python3 cli/llm-agent/guardian.py --debate "task" [--team cross-review] [--project <id>]
python3 cli/llm-agent/guardian.py --debate "task" --thread <ID>     # continue a thread

# Interactive REPL
python3 cli/llm-agent/guardian.py
```

그 밖의 CLI 명령:
- 세션: `--sessions [<ID>]`, `--attach <ID> --role critic`(tmux 창에서 세션을 연다, `GUARDIAN_TMUX_SESSION`, 기본
  `guardian`), `--prune-sessions 30`.
- 액션: `--actions [<ID>]`, `--show-action N`, `--run-action N`(`yes` 입력을 받음), `--reject-action N`.
- jev: `--jev-status [<ID>]`, `--jev-recheck <ID>`(다시 평가하고, 통과하면 `HELD` 액션를 푼다).
- 팀과 페르소나: `--teams`, `--personas`, `--team-save`, `--persona-save`(stdin으로 JSON).
- ops 점검: `--ops-watch [PROJECT] [--dry-run]`.

### 3. 대시보드 (로컬 개발)
```bash
cd apps/web-desktop
npm install
npm run dev
# http://localhost:5173/ (local development only)
```

---

## 허브 배포

```bash
# The hub: agent, dashboard, workspace clone, Go toolchain, goindex, backup and ops watch timers
scripts/install-agent-service.sh site-a-arm1
# First install, or when the hub cannot be reached: inventory from the local snapshot (the hub's copy is never overwritten)
scripts/install-agent-service.sh site-a-arm1 --offline
# Open: http://<Tailscale IP>:9870/  (for example http://100.64.0.7:9870/)
```
- **허브 에이전트는 허브에만 설치한다.** 인벤토리 항목이 `guardian_role: hub`가 아닌 노드는 종료 코드 2로 거부되며, 노드별
  설치는 없다. 노드는 SSH로 관리한다(`scripts/setup-hub-ssh.sh`).
- 노드는 인벤토리 id여야 한다. 별칭이나 IP는 거부된다.
- 대시보드는 항상 빌드되어 서빙된다: `--no-ui`는 거부되고 `--with-ui`는 받아들이되 무시한다.
- **릴리스** ([install-tracks.ko.md](docs/design/install-tracks.ko.md), R1): 스크립트가 이 기기에서 릴리스 트리(agent,
  goindex, 대시보드, 테스트를 뺀 추적 중인 `cli/`, `configs/`, `deploy/` 파일, `VERSION`, `MANIFEST`)를 만들어
  `~/.guardian/releases/<version>-<time>/`에 올리고, 허브에서 그 안의 `deploy/hub/install.sh`를 실행한다. 그 스크립트가
  MANIFEST를 확인하고, `agent.env`를 쓰고, 유닛을 설치하고, `~/.guardian/current`를 새 릴리스로 바꾸고(이전 것은
  `~/.guardian/previous`가 된다), 허브 에이전트를 재시작해 확인한다. 시작하지 않는 릴리스는 스스로 되돌린다(exit 3). 릴리스는
  세 개를 남긴다. `--dry-run`은 무엇이 바뀔지만 알려 준다.
- 이전 릴리스로 되돌리기: `ssh <hub> bash ~/.guardian/current/deploy/hub/install.sh --rollback`(`--status`는 릴리스 목록).
- 허브 checkout `~/server-guardian`은 더 이상 아무것도 실행하지 않는다. 내장 `server-guardian` 프로젝트의 원본이다. 기본적으로
  fast-forward하고, 판정자 액션가 남긴 변경이 없으면 작업 클론 `~/guardian-workspace`를 같은 커밋으로 옮기며, 모든 프로젝트를
  sync한다. `--no-pull`은 이를 건너뛴다.
- 설치 스크립트는 `~/.guardian/agent.env`에 `GUARDIAN_WORKSPACE=~/guardian-workspace`, `GUARDIAN_LLM_TIMEOUT=600`,
  `GUARDIAN_DEBATE_TIMEOUT=7200`, `GUARDIAN_SERVERS_FILE=~/.guardian/data/servers.json`,
  `AGENT_UI_DIR=~/.guardian/current/ui`를 쓴다(자기가 관리하는 키만 다시 쓴다).
- 업로드는 임시 이름으로 보내고 sha256이 맞은 뒤에만 이름을 바꾼다. 업로드가 실패하면 허브의 Tailscale IPv4로 한 번 다시
  보낸다. `HUB_SSH_HOSTNAME=<주소>`로 처음부터 주소를 정할 수 있다.
- **허브 sshd 하드닝:** 설치 스크립트가 `deploy/ssh/00-guardian-hardening.conf`(`MaxAuthTries 4`, `PermitRootLogin no`)를
  허브의 `/etc/ssh/sshd_config.d/`에 넣는다. `sshd -t`와 `sshd -T`로 확인하고 실패하면 되돌리며, 내용이 같으면 건드리지
  않는다. `sshd_config`가 `sshd_config.d`를 include하지 않거나, root로 접속 중이거나, root에 강제 명령 없는 키가 있으면
  이 단계를 건너뛴다. 하드닝이 건너뛰어지거나 되돌려져도 설치는 끝난다. `--no-ssh-hardening`으로 끈다.
- **노드의 sshd 하드닝은 수동 단계다.** 설치 스크립트는 노드를 건드리지 않는다. 절차(허브에서 노드마다 SSH로
  `deploy/ssh/apply-ssh-hardening.sh` 실행)는
  [2026-10-05-fleet-sshd-hardening.ko.md](docs/operations/2026-10-05-fleet-sshd-hardening.ko.md)에 있다.

### 배포 관문
CI를 통과한 것만, 한 줄로 이어서 배포한다:
```bash
scripts/ci.sh && git push origin main && scripts/install-agent-service.sh site-a-arm1
```
- CI가 실패한 뒤에는 push하거나 배포하지 않는다.
- 배포 전에 production 토론이 실행 중이 아닌지 확인한다(`GET /api/debates` 또는 토론 화면).
- 설치 스크립트는 로컬 작업 트리를 빌드한다. 깨끗한 트리에서만 배포한다. 작업 중인 변경이 있으면 배포할 부분만 커밋하고
  그 커밋의 임시 `git worktree`에서 배포한다.

### 인벤토리: 허브 사본이 원본
노드 목록은 허브의 `~/.guardian/data/servers.json`(`GUARDIAN_SERVERS_FILE`)에 있고, 대시보드의 노드 등록·삭제가
여기에 기록된다. 노드 추가·삭제·수정은 허브나 대시보드에서 한다.
- `configs/servers.json`은 관리 스크립트의 `--offline` 모드를 위한 로컬 스냅샷일 뿐이다. git에서 무시되므로 실제
  노드가 저장소에 들어가지 않는다. 형식은 `configs/servers.example.json`에 있다.
- 노드를 고르는 Mac 스크립트(`install-agent-service.sh`, `setup-hub-ssh.sh`, `fleet-upgrade.sh`,
  `fleet-hostname.sh`, `deploy-llm-env.sh`, `deploy-llm-skills.sh`, `install-llm-clis.sh`)는 실행할 때마다
  `scripts/lib/inventory.sh`로 허브 사본을 읽기 전용으로(`ssh <허브> cat …`) 받는다. 허브 별칭은 `HUB_HOST`, 없으면
  스냅샷의 `guardian_role: hub` 항목이다.
- `--offline`(또는 `INVENTORY_OFFLINE=1`)은 첫 설치이거나 허브에 닿지 않을 때 스냅샷을 쓴다. 경고를 출력하며 허브
  사본을 덮어쓰지 않는다(없을 때만 초기값으로 심는다).
- `scripts/pull-inventory.sh`는 diff를 보여 준 뒤 허브 인벤토리를 로컬 스냅샷으로 복사한다(이전 파일은
  `configs/servers.json.bak`).
- `scripts/setup-hub-ssh.sh`는 허브 인벤토리에 더 이상 없는 id를 건너뛴다. `--revoke <id>`는 키를 회수한다.

### 허브 구성
- 결정 DB와 토론 기록: `~/.guardian/data/`(`GUARDIAN_DATA_DIR`, `GUARDIAN_DB_PATH`).
- **백업:** `server-guardian-db-backup.timer`가 매일 03:30 UTC에 일관된 DB 사본(`.db`)과 데이터 타르볼
  (`guardian_data-*.tar.gz`: `backups/`, `code-index/`, 로그를 뺀 `~/.guardian/data`와 `~/.ssh/guardian_fleet.conf`,
  `~/.ssh/guardian_known_hosts`)을 `~/.guardian/data/backups/`에 만들고 각각 14개를 보관하며, `backup_replica` 노드에도
  복사한다. 개인키와 비밀은 들어가지 않는다. 복원과 훈련: [backup-restore.ko.md](docs/operations/backup-restore.ko.md).
- **허브 → 노드 SSH:** 허브 전용 키를 각 노드의 `authorized_keys`에 `from="<허브 Tailscale IP>"` 제한으로 등록한다.
  - 개인키는 `/etc/guardian/fleet_key`(root, 0600)이고 `guardian-fleet-agent.service`의 ssh-agent
    (`/run/guardian-fleet/agent.sock`)가 보관한다. `~/.ssh/guardian_fleet.conf`(`~/.ssh/config`가 Include)의 호스트
    블록이 이 소켓을 `IdentityAgent`로 쓴다.
  - 허브 에이전트, LLM CLI, 백업은 `NoNewPrivileges`(프로세스 트리 전체에서 sudo 불가)와 `InaccessiblePaths`(키·비밀 파일
    차단) 샌드박스에서 실행된다. 키를 쓸 수는 있지만 읽거나 복사할 수는 없다.
  - 허브 sudo: 기본값은 허용(`--hub-sudo`)이다. 허브 에이전트, 판정자 액션, 대시보드 터미널이 허브에서 sudo를 쓸 수 있도록
    drop-in `hub-sudo.conf`가 샌드박스 일부를 푼다. 이 경우 root가 키를 읽을 수 있으므로 키 보호는 실수 방지 수준이다.
    `--no-hub-sudo`는 전체 샌드박스를 유지한다. 둘 다 없이 재배포하면 현재 설정을 유지한다.
  - 키 이전과 소켓 설정은 `deploy/hub/protect-fleet-key.sh`가 맡고, `install-agent-service.sh`와 `setup-hub-ssh.sh`가
    호출한다. 허브 설치 끝에 격리 점검(키 읽기·sudo 차단, fleet SSH 동작, `systemd-analyze security` 점수)이
    출력된다.
```bash
scripts/setup-hub-ssh.sh                         # issue, register and verify the hub key
scripts/setup-hub-ssh.sh --revoke [--key <public key|file|SHA256:..>] [--dry-run]   # remove only lines with that key (default: the hub's current key)
scripts/setup-hub-ssh.sh --rotate [--dry-run]    # add new key -> verify login with it -> switch the hub key -> remove the old key (keeps the old key on failure)
scripts/deploy-llm-env.sh                        # LLM CLI credentials to the hub
scripts/deploy-llm-skills.sh [--dry-run]         # Guardian skills to the hub and the Tier-1 nodes
```

---

## code 프로젝트 (다른 저장소)

토론 엔진은 등록한 다른 저장소도 읽고 고칠 수 있다. 프로젝트마다 허브에 작업 클론이 하나씩 있다.
- **정의:** 기본 프로젝트는 `configs/projects/*.json`에 있다. 대시보드(설정 > 프로젝트)에서 추가한 프로젝트는
  `~/.guardian/data/projects/`에 저장되고 같은 id의 기본 프로젝트를 덮어쓴다.
- **필드:**
  - `repo`: https 주소. 자격 증명은 넣지 않는다.
  - `branch`: 추적할 브랜치.
  - `workspace`: 클론 위치. 기본값 `~/guardian-workspaces/<id>`.
  - `tests.commands`: 판정자가 실행할 수 있는 테스트 명령 추가(예: `cargo test`). 셸, 인터프리터, 패키지 러너만 단독으로
    쓰는 명령은 거부한다.
  - `tests.default`: 판정자가 파일을 바꾼 뒤, 그리고 publish 전에 실행하는 테스트. 없으면 `run_test`와 publish가
    거부된다. 명령 하나, 또는 처음 실패할 때까지 차례로 실행하는 1~5개 명령의 목록(600초 한도를 나눠 씀). 모든 항목은
    같은 허용 목록 검사를 거치며 셸이나 `scripts/ci.sh` 같은 경로 스크립트는 쓸 수 없다. 이 저장소의 관문은
    `python3 -m unittest discover ...`, `go vet ./apps/agent/...`, `go test -race ./apps/agent/...`다(`-race`에는 허브의
    gcc가 필요하다).
  - `context.paths`: 기본 참조 코드.
  - `team`: 프로젝트의 기본 팀(선택).
- **클론과 sync:** 대시보드의 Clone/Sync 버튼이나 `--project-sync`. 허브 배포 때 모든 프로젝트를 sync한다. 수정된 파일이
  있는 클론은 건드리지 않는다.
- **비공개 저장소:** 허브에 git 자격 증명을 한 번 등록한다(예: Gitea access token과
  `git config --global credential.helper store`).
- **규칙:** 토론은 시작할 때 프로젝트를 고르고, 이어지는 토론은 그 프로젝트를 벗어날 수 없다. 판정자 액션는 그
  프로젝트의 클론에서만 실행된다. 코드 변경은 자동으로 적용되고 테스트·셸 액션는 사람 승인이 필요하다. 액션는 push하지
  않으며, push는 Publish만 한다.
```bash
python3 cli/llm-agent/guardian.py --projects
python3 cli/llm-agent/guardian.py --project-sync all
python3 cli/llm-agent/guardian.py --debate "three main defects and their fixes" --project my-service --context-path 'src/**/*.rs'
```

### Publish (스레드의 변경을 브랜치로 올리기)
Publish는 판정자 액션(`execute_code_action`, `apply_patch`)가 클론에 남긴 변경을 프로젝트 `repo`의 새 브랜치로 push한다.
지원하는 push 경로는 이것 하나다.
- `python3 cli/llm-agent/guardian.py --publish-thread <THREAD> [--branch <이름>]`(계획을 보여 주고 `yes`를 입력받음)
  또는 토론 결과의 **Publish** 버튼. `GUARDIAN_UI_ACTIONS=off`면 대시보드 쪽은 막힌다.
- 액션가 만든 파일만 추가하고, `tests.default`를 통과한 뒤에만 push한다. 강제 push는 없다.
- push 뒤: 브랜치를 병합하고, 허브 체크아웃을 갱신(재배포)한 다음 `--project-sync`를 실행해야 클론이 병합된 코드를 본다.
- 전제 조건, 실패와 재시도, 종료 코드: [publish-thread.ko.md](docs/design/publish-thread.ko.md).

---

## 노드 그룹

노드를 이름 있는 그룹으로 묶는다. 한 노드가 여러 그룹에 속할 수 있다. 그룹은 허브 결정 DB
(`~/.guardian/data/guardian_decisions.db`의 `node_groups`)에 저장된다. 설계: [node-groups.ko.md](docs/design/node-groups.ko.md).
- **멤버 = 직접 고른 노드 + 규칙에 맞는 노드 − 제외한 노드.** 규칙 안의 조건은 AND, 규칙끼리는 OR다. 규칙에 맞는 새
  노드는 자동으로 들어간다.
- **규칙 필드:** `tier`, `arch`, `guardian_role`, `role`(같음/다름), `tailscale_tags`, `cli_support`(포함/미포함),
  `name`(glob, 예: `site-b-*`).
- **처음 실행:** `servers.json`의 `group` 값마다 정적 그룹을 한 번 만든다. 그 뒤로는 사용자 그룹만 쓰며 `group` 필드는
  남아 있지만 쓰지 않는다.
- **대시보드:** 플릿 > 그룹(실시간 멤버 미리보기). Group by와 그룹 필터, 일괄 명령 대상, ops 프로젝트 대상이 이 그룹을
  쓴다.
- **CLI:** `--target`은 `local`/`all` → 노드 id·이름·호스트명 → 그룹 id·이름(대소문자 무시) 순으로 해석하고, 못 찾으면
  비슷한 이름을 알려 준다.
```bash
python3 cli/llm-agent/guardian.py --groups
echo '{"name":"Tier-1 ARM","rules":[{"conditions":[{"field":"tier","op":"eq","value":"Tier-1"},{"field":"arch","op":"eq","value":"aarch64"}]}]}' \
  | python3 cli/llm-agent/guardian.py --group-save --json
python3 cli/llm-agent/guardian.py --group-delete tier-1-arm --json
```

## 노드별 SSH 감사 (fleet)

허브가 fleet 키로 각 노드에 **고정된 읽기 전용 프로브**를 실행해 유효 sshd 설정과 로그인 사용자의 `authorized_keys`를
읽고, 허브 자체 감사(`/api/ssh/*`)와 같은 파서·규칙으로 평가한다. 설계: [fleet-ssh-audit.ko.md](docs/design/fleet-ssh-audit.ko.md).
- **API:** `GET /api/fleet/ssh?targets=a,b[&refresh=1]`(다른 fleet API와 같은 토큰). `targets`가 없으면 모든 노드,
  모르는 id는 SSH 전에 400.
- **상태:** `ok`(`sudo -n sshd -T` 또는 root의 `sshd -T`로 유효 설정), `partial`(root 권한이 없어
  `/etc/ssh/sshd_config`와 `sshd_config.d/`를 직접 읽음: 읽지 못한 Include와 기본값 차이가 빠질 수 있음, 또는 출력이
  잘림), `unreachable`(ssh 실패나 시간 초과), `error`(설정을 읽지 못함). 평가하지 못한 노드는 점수가 없고 평균에서도
  빠진다.
- **캐시:** 대상 집합별 60초 메모리 캐시, 동시 요청은 프로브 하나를 함께 쓴다. `refresh=1`(대시보드 감사 새로 고침, CLI
  `--refresh`)은 다시 프로브한다. 최대 8대 동시, 노드당 20초.
- **키:** 지문, 주석, 옵션만 담고 공개키 본문은 비운다.
- **CLI:** `--ssh-audit [--target 노드|그룹] [--refresh] [--json]`. 플릿, 그룹, 원격 노드 대상의 자연어 보안 감사도 이
  API를 쓴다.
- **대시보드:** 플릿 > SSH 감사.

## CI

`scripts/ci.sh`가 모든 검사를 한곳에서 실행한다. GitHub Actions(`.github/workflows/ci.yml`, 비공개 GitHub 미러)가 같은
스크립트를 섹션마다 job 하나로 실행한다. git.example.com의 Gitea는 Actions를 일부러 꺼 두었다.

```bash
scripts/ci.sh                 # default: go python web shell config (tauri only when named)
scripts/ci.sh go python       # selected sections
```

| 섹션 | 검사 |
|---|---|
| `go` | gofmt(`metrics.go`, `model.go` 제외), `go vet`, `go test -race`, `apps/agent`의 linux/arm64 빌드, `tools/goindex`도 같은 검사 |
| `python` | `cli/llm-agent` unittest 전체와 `scripts/test_*.py`, `GUARDIAN_LLM_PROVIDERS=none`(실제 LLM 호출 없음) |
| `web` | 디자인 시스템 typecheck·테스트·빌드, 대시보드 빌드·테스트 |
| `tauri` | 데스크톱 앱 release 빌드(설치 파일 번들 제외)와 Rust 테스트. 이름을 지정할 때만, CI에서는 별도 job |
| `shell` | `scripts/*.sh`, `scripts/lib/*.sh`, `deploy/*/*.sh`의 `bash -n`과 shellcheck(경고 이상) |
| `config` | `configs/` JSON 문법, 노드 id 중복 없음과 허브 1대, 기본 페르소나·팀, 이중 언어 문서(모든 `name.md`에 `name.ko.md`, 언어 줄, 상대 링크 확인) |

로컬에서는 없는 도구(예: shellcheck)를 경고 후 건너뛰고, CI(`CI=true`)에서는 실패로 처리한다.

## 공개 사본

저장소는 비공개 노드·사이트·계정 이름 대신 대체 이름(`site-a-arm1`, `site-b-*`, `owner` 등)을 쓴다. 대체 이름을 실제
이름에 대응시키는 규칙은 저장소 밖의 `~/.config/server-guardian/public-redactions.txt`(git-filter-repo `--replace-text`
형식)에 있다.
- `scripts/redact-private.py [--rules FILE] [--check] [path ...]`는 추적되는 텍스트 파일의 비공개 이름을 가린다.
  `--check`는 아무것도 바꾸지 않고, 남은 일치 항목이 있으면 1로 끝난다.
- `scripts/export-public.sh [--rules FILE] [--push REMOTE-URL] [--keep]`는 `main`의 임시 클론에서 공개 사본을 만든다:
  공개하면 안 되는 경로(인벤토리 스냅샷, 옛 결정 DB, `*.enc`, `CLAUDE.local.md`, `*.pyc`)를 지우고, 규칙으로 파일
  내용과 커밋 메시지를 다시 쓰며, 모든 blob과 메시지를 다시 검사하고(하나라도 일치하면 중단), `--push`가 있으면
  `main`을 공개 원격 저장소로 push한다. 이 체크아웃, 원격 저장소 설정, 허브는 바뀌지 않는다. git-filter-repo가 필요하다.

## 문서

설계 (`docs/design/`):
- [server-ops-project.ko.md](docs/design/server-ops-project.ko.md): ops 프로젝트, 노드 점검, 정리 액션 카탈로그, auto_run, ops 점검, fleet-ops 팀, LLM CLI skill
- [debate-teams.ko.md](docs/design/debate-teams.ko.md), [debate-teams-api.ko.md](docs/design/debate-teams-api.ko.md): 팀 라인업과 API
- [debate-agent-retrieval.ko.md](docs/design/debate-agent-retrieval.ko.md): 에이전트의 코드 읽기, 이벤트 계약, Agents 모니터
- [debate-code-index-wiki.ko.md](docs/design/debate-code-index-wiki.ko.md): 심볼 인덱스, 저장소 지도, Go facts, 코드 위키
- [code-wiki-coverage.ko.md](docs/design/code-wiki-coverage.ko.md): 위키 기능·그룹 페이지, 파일 보기
- [debate-code-context.ko.md](docs/design/debate-code-context.ko.md), [debate-patch-tool.ko.md](docs/design/debate-patch-tool.ko.md): 코드 컨텍스트와 패치 도구
- [publish-thread.ko.md](docs/design/publish-thread.ko.md): 스레드 변경 게시
- [node-groups.ko.md](docs/design/node-groups.ko.md), [node-registration.ko.md](docs/design/node-registration.ko.md), [fleet-ssh-audit.ko.md](docs/design/fleet-ssh-audit.ko.md): 플릿
- [dashboard-i18n.ko.md](docs/design/dashboard-i18n.ko.md), [dashboard-react-migration.ko.md](docs/design/dashboard-react-migration.ko.md), [ui-flow-redesign.ko.md](docs/design/ui-flow-redesign.ko.md): 대시보드

운영 (`docs/operations/`):
- [backup-restore.ko.md](docs/operations/backup-restore.ko.md): 허브 백업, 복원, 복원 훈련
- [2026-10-05-fleet-sshd-hardening.ko.md](docs/operations/2026-10-05-fleet-sshd-hardening.ko.md): 플릿 sshd 하드닝 기록과 노드 수동 절차

분석과 평가는 `docs/analysis/`에 있다.

## 라이선스

Copyright 2026 William Jung. [Apache License 2.0](LICENSE)으로 배포한다. [NOTICE](NOTICE) 참고.

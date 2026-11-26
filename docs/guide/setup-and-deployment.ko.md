# 설치와 배포

[English](setup-and-deployment.md) | 한국어

[사용 가이드](README.ko.md)로 돌아가기.

이 문서는 허브 설치·배포, 플릿 SSH 키 보호, LLM CLI 자격증명과 Guardian 스킬 배포, 실행 환경(워커) 구성, 로컬
개발 및 CI 실행 방법을 정리한다.

## 사전 요구 사항

- **허브 노드**: `systemd`, `python3`(3.9+), `git`, Tailscale(또는 headscale)이 연결된 Linux 노드(arm64 또는
  amd64). 인벤토리 항목에 `"guardian_role": "hub"`가 지정되어 있어야 한다.
- **관리자 워크스테이션**: `bash`, `ssh`, `go`(`apps/agent`, `tools/goindex` 빌드용), `node`/`npm`(대시보드
  빌드용), `python3`.
- **관리 대상 노드**: 허브에서 tailnet SSH로 접근 가능하면 된다. 별도 에이전트 설치는 필요 없다.

## 1. 허브 배포

허브는 `~/.guardian/releases/<버전>-<시각>/` 아래 불변 릴리스 트리로 동작한다. `~/.guardian/current`가 현재
릴리스를 가리키고, 직전 릴리스는 `~/.guardian/previous`로 유지된다(최근 3개 보관).

```bash
# 표준 배포 (허브 인벤토리 조회 -> agent/goindex/대시보드 빌드 -> 원자적 업로드 및 전환)
scripts/install-agent-service.sh site-a-arm1

# 허브를 변경하지 않고 변경 예정 항목만 미리 보기
scripts/install-agent-service.sh site-a-arm1 --dry-run

# 최초 설치 또는 허브 접속 불가 시 (로컬 스냅숏 configs/servers.json 사용)
scripts/install-agent-service.sh site-a-arm1 --offline
```

주요 동작:
- **허브 전용**: `guardian_role`이 `hub`가 아닌 노드를 넘기면 종료 코드 2로 거부된다. 별칭이나 IP가 아닌 인벤토리
  ID를 넘겨야 한다.
- **기동 실패 시 자동 롤백**: `deploy/hub/install.sh`가 `MANIFEST` 검증, systemd 유닛 설치, 허브 에이전트
  재시작 후 상태 검사를 수행하며, 실패 시 자동으로 `~/.guardian/previous`로 되돌린다(종료 코드 3).
- **수동 롤백 및 릴리스 상태 조회**:
  ```bash
  ssh site-a-arm1 bash ~/.guardian/current/deploy/hub/install.sh --status
  ssh site-a-arm1 bash ~/.guardian/current/deploy/hub/install.sh --rollback
  ```
- **허브 sshd 하드닝**: 기본 활성화(`deploy/ssh/00-guardian-hardening.conf`: `MaxAuthTries 4`,
  `PermitRootLogin no`). `sshd -t` 검증 실패 시 자동 복원되며, `--no-ssh-hardening`으로 건너뛸 수 있다.
- **허브 sudo 샌드박스**: 기본값(`--hub-sudo`)은 허브 에이전트와 대시보드 작업의 sudo 사용을 허용한다. 엄격한
  `NoNewPrivileges` 샌드박스를 유지하려면 `--no-hub-sudo`를 지정한다. 플래그 없이 재배포하면 현재 설정을
  유지한다.
- **워크스페이스 동기화**: 기본적으로 배포 시 `~/server-guardian`을 fast-forward하고 등록된 프로젝트 클론을
  동기화한다(판정자 액션으로 수정된 파일이 있으면 건드리지 않는다). `--no-pull`로 건너뛸 수 있다.

### 배포 관문 (권장 절차)

배포 전에는 실행 중인 운영 토론이 없는지 확인하고, CI 통과 후 한 번에 푸시·배포한다.

```bash
scripts/ci.sh && git push origin main && scripts/install-agent-service.sh site-a-arm1
```

인스톨러는 로컬 워킹 트리를 빌드하므로 반드시 깨끗한 트리(또는 배포할 커밋의 임시 `git worktree`)에서 배포한다.

## 2. 플릿 SSH 키 구성

허브는 `/etc/guardian/fleet_key`(root, `0600`)에 보관하고 `guardian-fleet-agent.service`
(`/run/guardian-fleet/agent.sock`)가 쥐고 있는 전용 키로 모든 관리 노드에 접속한다. 각 노드의 `authorized_keys`에는
`from="<허브 Tailscale IP>"` 제한이 걸린다.

```bash
# 플릿 SSH 키 발급, 배포, 보호 설정 및 접속 검증
scripts/setup-hub-ssh.sh

# 안전한 키 교체 (새 키 추가 -> 로그인 검증 -> 허브 키 전환 -> 이전 키 제거)
scripts/setup-hub-ssh.sh --rotate
scripts/setup-hub-ssh.sh --rotate --dry-run

# 플릿 노드에서 키 회수
scripts/setup-hub-ssh.sh --revoke [--key <공개키|파일|SHA256:...>] [--dry-run]
```

관리 노드의 `sshd` 하드닝은 허브 인스톨러가 자동으로 건드리지 않으며,
[Fleet sshd 하드닝](../operations/2026-10-05-fleet-sshd-hardening.ko.md) 절차에 따라 수동 적용한다.

## 3. LLM CLI 자격증명

토론 에이전트와 상주 에이전트는 설치된 LLM CLI(`claude`, `codex`, `agy`, `opencode`)나 HTTP API(`exaone`,
`zai`)를 호출한다. 허브(및 참여 워커 노드)에서는 `~/.guardian/llm.env`(`0600`)에서 자격증명을 읽는다.

**`~/.claude/.credentials.json`이나 `~/.codex/auth.json`을 노드 사이에 복사하지 말 것**: OAuth 리프레시 토큰이
갱신될 때 다른 노드가 `401` 오류를 겪는다. 대신 `claude setup-token`으로 장기 토큰을 한 번 발급해 로컬
`~/.config/server-guardian/llm.env`에 `CLAUDE_CODE_OAUTH_TOKEN`으로 저장한 뒤 배포한다.

```bash
# 허브(기본)에 배포하고 CLI별 PONG 검증 실행
scripts/deploy-llm-env.sh

# 지정 노드까지 함께 배포
scripts/deploy-llm-env.sh site-a-arm1 node-1

# 덮어쓰지 않고 현재 자격증명 상태만 확인
scripts/deploy-llm-env.sh --check-only
```

`~/.guardian/llm.env`의 주요 키:
- `CLAUDE_CODE_OAUTH_TOKEN`: `claude` CLI 장기 인증 토큰.
- `TYPESAFE_API_KEY`: `jev` 게이트(허브의 `~/jev-mcp`) 활성화.
- `EXAONE_API_KEY` / `OPENCODE_EXAONE_API_KEY`: `exaone`(HTTP) 및 `opencode`(CLI) 제공자 활성화.
- `ZAI_API_KEY`: `zai` 제공자 활성화.

허브 `~/.guardian/agent.env`의 주요 환경 변수:
- `AGENT_TOKEN`(필수): 허브 API 및 대시보드 로그인용 베어러 토큰.
- `GUARDIAN_LLM_PROVIDERS`: 제공자 대체 순서(예: `claude,codex,agy`).
- `GUARDIAN_LLM_TIMEOUT`: 호출당 제한 시간(초, 기본 `300`, 허브 `600`).
- `GUARDIAN_CLAUDE_EFFORT`: `--effort` 수준(기본 `medium`, `default`는 CLI 기본값 유지).
- `GUARDIAN_JEV_GATE=off`: `jev` 게이트 비활성화(테스트 전용).
- `GUARDIAN_AUTO_APPLY=off` / `GUARDIAN_UI_ACTIONS=off`: 자동 실행 비활성화 또는 대시보드 UI에서의 모든 쓰기·결정
  차단.

## 4. LLM CLI용 Guardian 스킬

`deploy/llm-skills/`에는 노드에서 LLM CLI와 대화할 때 Guardian 점검·운영 기능을 자연어로 바로 호출할 수 있는 공유
`SKILL.md` 스킬이 들어 있으며, `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/config/skills`에 배포된다.

| 스킬 | 설치 범위 | 역할 |
|---|---|---|
| `guardian-host-check` | `all`(허브 + Tier-1) | 이 노드의 상태·디스크·서비스 읽기 전용 점검 |
| `guardian-host-cleanup` | `all`(허브 + Tier-1) | 카탈로그 액션과 금지 규칙으로 이 노드 정리(운영자 확인 필수) |
| `guardian-fleet-ops` | `hub` | 야간 감시 드라이런, 운영 토론 시작·추적, 승인된 액션 실행 |
| `guardian-hub-status` | `hub` | 허브 에이전트, systemd 타이머, 감사 로그, 토론 상태 점검 |
| `guardian-ask` | `hub` | `guardian.py --agent-run`(빠른 질문)의 얇은 앞문 |

배포 및 미리 보기:

```bash
scripts/deploy-llm-skills.sh --dry-run
scripts/deploy-llm-skills.sh
```

`guardian-*` 접두사 스킬만 관리되며, 토론 실행 중에는 이 스킬이 로드되지 않는다. 스킬 디렉터리 구조와 `SKILL.md`
형식은 `deploy/llm-skills/`를 본다.

## 5. 실행 환경(워커)

선택 사항인 모델 워커를 설치하면 요건을 충족하는 Tier-1 노드에서 토론 에이전트 턴을 실행하거나(프로토콜 3) 상주
에이전트를 호스팅할 수 있다. 토론용 워커는 상태가 없으며 `AGENT_TOKEN`을 갖지 않는다.

```bash
# 후보 노드의 요건 점검 (configs/worker-requirements.json 기준)
scripts/probe-worker.sh node-1

# 조건을 통과한 노드에 워커 설치·업그레이드
scripts/install-worker.sh node-1
```

설치된 워커 상태는 대시보드의 **설정 > 실행 환경**(`#/settings/workers`)이나 CLI로 확인한다.

```bash
python3 cli/llm-agent/guardian.py --workers
```

## 6. 로컬 개발과 CI

```bash
# 1. Go 허브 에이전트 로컬 실행 (127.0.0.1:9870 수신)
cd apps/agent
go build -o agent .
AGENT_TOKEN=$(openssl rand -hex 32) ./agent

# 2. React 대시보드 개발 서버 실행 (http://localhost:5173/)
cd apps/web-desktop
npm install
npm run dev

# 3. CI 검사 실행 (섹션: go, python, web, shell, config; tauri는 명시할 때만)
scripts/ci.sh
scripts/ci.sh go python config
```

## 7. 백업과 정기 타이머

허브 배포 시 다음 두 systemd 타이머가 함께 활성화된다.
- `server-guardian-ops-watch.timer`(매일 02:30 UTC): `policy.watch`가 켜진 운영 프로젝트에 `guardian.py --ops-watch`를
  실행한다.
- `server-guardian-db-backup.timer`(매일 03:30 UTC): 일관된 DB 사본과 데이터 아카이브를 `~/.guardian/data/backups/`에
  생성하고(14개 보관, 개인키 제외) `backup_replica` 노드로 복제한다.

복구 절차와 훈련 방법은 [허브 백업과 복원](../operations/backup-restore.ko.md)을 참고한다.

# Server Guardian 시스템 분석 보고서 (2026-10-04)

[English](2026-10-04-system-analysis.md) | 한국어

> 2026-10-04 시점의 기록이며, 이후 코드가 바뀌었다.

> **2026-10-04 시점의 스냅샷입니다.** 이후 바뀐 것: CI가 추가되었습니다(`scripts/ci.sh`, GitHub Actions, 2026-10-05). 설치 스크립트는 점검 실패를 모아 비정상 종료하고 이전 바이너리를 `~/.guardian/agent.prev`로 남깁니다. 허브 sudo는 기본 허용입니다(`--hub-sudo`, 아래 6번 "sudo가 막힌다"는 당시 상태). `apply_patch`와 Publish가 생겼습니다. 현재 상태는 README와 `docs/design/`을 보세요. 본문은 당시 기록이라 고치지 않았습니다.

5명의 분석 에이전트가 영역을 나눠 읽기 전용으로 분석한 결과입니다. 파일 수정, 서버 접속, 실제 LLM 호출 없이 진행했습니다.

| 담당 | 범위 |
|---|---|
| agent-analyst | `apps/agent` (Go agent, 9870 API/SSE, 토론 작업 실행) |
| llm-analyst | `cli/llm-agent` (토론 엔진, provider 전환, 조치 승인, jev 관문) |
| frontend-analyst | `apps/web-desktop`, `packages/guardian-ui`, `.design-sync` |
| infra-analyst | `deploy/`, `scripts/`, `configs/` |
| security-reviewer | 영역 전체 보안 |

## 요약

토론 엔진과 조치 승인 절차는 견고합니다. 하지만 초기에 만든 실행 경로(`/api/exec`, 대시보드 `main.ts`, `run_tests` 도구)와 권한 구조 때문에 **공격 지점 한 곳만 뚫려도 서버 24대 전체를 root로 장악할 수 있습니다.**

## 1. 서버 전체 장악으로 이어지는 경로

1. 진입점 (셋 중 하나면 충분)
   - 대시보드 저장형 XSS: `apps/web-desktop/src/main.ts`의 innerHTML 18곳에 이스케이프가 없음. 예: 사용자 이름이 `<img onerror=...>`인 SSH 로그인 실패 기록을 운영자가 Exec 탭에서 `tail`로 조회.
   - LLM 프롬프트 인젝션: agy, opencode가 샌드박스 없이 실행되고 환경에 `AGENT_TOKEN`이 있음 (`cli/llm-agent/llm_runner.py:120,229-250`).
   - `run_tests` 우회: 위험도 low이지만 명령 앞부분만 검사하므로 `go test -exec /bin/sh`, `-toolexec`, `-c -o`, `pytest -p`가 통과 (`cli/llm-agent/mcp_client.py:47,178-188`).
2. `/api/exec`, `/api/fleet/exec` 호출. 샌드박스 없는 `sh -c`.
3. `ubuntu` 계정으로 실행되며, 이 계정은 비밀번호 없이 sudo 가능.
4. 허브의 같은 계정이 `command=` 제한 없는 플릿 SSH 키를 보유하므로 24대 전체를 root로 제어.

LLM 경로 두 개는 jev 평가와 운영자 승인을 우회하거나 약한 확인(체크박스)만 거칩니다.

## 2. 보안 우선순위

| 순위 | 문제 | 위치 | 조치 |
|---|---|---|---|
| 1 | 대시보드 XSS, CSP 없음 | `apps/web-desktop/src/main.ts`, `src-tauri/tauri.conf.json` (`csp: null`) | 공용 `escapeHtml` 적용, CSP 설정 |
| 2 | `run_tests` 인자 우회, 낮은 위험도 | `cli/llm-agent/mcp_client.py:47,178-188,305` | 정확한 플래그 허용 목록, 위험도 high, 하위 프로세스 환경에서 비밀 제거 |
| 3 | LLM CLI가 샌드박스 없이 비밀 보유 | `cli/llm-agent/llm_runner.py:120,229-250` | provider별 최소 환경, 도구 비활성화 |
| 4 | 토큰이 비면 인증 꺼짐, CORS `*`, Content-Type 미검사, GET으로 명령 실행 | `apps/agent/auth.go:60`, `main.go:18,126,218,323` | 토큰 없으면 시작 거부, Origin/Host/Content-Type 검사 |
| 5 | `target_host` ssh 옵션 주입 | `apps/agent/servers_handler.go:20` | `servers.json` 대조, 호스트 앞 `--` |
| 6 | agent 과권한 | `deploy/systemd/server-guardian-agent.service` | 전용 비sudo 계정, systemd 보안 옵션, 플릿 키 계정 분리 |

기타(Low): 세션 쿠키 30일·무효화 불가·Secure 없음, `access_token` 쿼리 전달, `openssl -pass pass:`로 비밀번호가 `ps`에 노출, AES-CBC에 MAC 없음, agy 프롬프트가 argv로 노출.

## 3. 실제 결과와 지어낸 결과를 구분할 수 없음

- 프론트엔드: 명령 실패 시 `exit_code 0`과 "Action applied successfully" 표시 (`api.ts:249-255`), 요청 실패 시 모든 서버 ONLINE "up 14 days" (`api.ts:385-401`), AI 탭은 하드코딩이며 속성 잘림으로 `echo `만 실행 (`api.ts:260-344`, `main.ts:606`).
- 백엔드 메트릭: uptime이 agent 가동 시간 (`metrics.go:49-57`), macOS 메모리 65% 고정, CPU%는 loadavg/코어 수, 실패 시 가짜 기본값, `/api/status` 항상 healthy.
- sshd 감사: 마지막 값 우선(sshd는 처음 값 우선, `sshd.go:74`), `Include`·`Match` 무시, 키 지문이 `ssh-keygen -lf`와 불일치 (`sshd.go:275`), 키 관리는 조회만 가능.

## 4. LLM 계층 정확성

- provider 전환으로 새 세션이 시작되면 이전 토론 내용과 코드를 보내지 않음 (`debate_engine.py:449-453`, `llm_runner.py:386`).
- agy 프롬프트를 argv 하나로 전달해 128KB 한도 초과 시 E2BIG로 조용히 전환 (`llm_runner.py:232,321`).
- 시간 예산이 허브 작업 제한(1시간)을 넘을 수 있고, 기록은 마지막에만 저장되어 강제 종료 시 전부 유실.
- jev 재확인 통과 시 같은 스레드의 이전 결정 HELD 조치까지 해제 (`action_ops.py:177`).
- 도구 예외 시 조치가 RUNNING에 영구 고정 (`mcp_client.py:218`).
- `execute_code_action`에 비밀 파일 검사 없음, `git_commit_push`가 `git add -A` 사용.
- 설계와 차이: 기본 팀이 `server_remediation`을 경고 없이 상속, `target_group` 미구현, 미커밋 변경 시 409 대신 FAILED, 심볼릭 링크 검사 없음.

## 5. 운영과 품질

- CI 없음 (shellcheck, 설정 JSON 검증, go/python 테스트 자동화 모두 없음).
- 설치 롤백 없음, 설치 후 상태 확인이 실패해도 성공 처리 (`scripts/install-agent-service.sh:216-238`).
- `agent.log` 로테이션 없음, 백업 실패 알림 없음, jev-mcp 설치 스크립트 없음.
- 공급망: agy curl|bash 설치, codex 버전 미고정, 미사용 `npx -y` filesystem-mcp 항목 (`configs/mcp_servers.json:13-17`).
- macOS 이식성: `wait -n`(bash 4.3+), `timeout`(coreutils) 필요.
- `servers.json`이 내부 IP와 키 경로를 담은 채 git에 추적됨. `.enc` 자격증명 번들은 gitignore로 제외되어 커밋된 적 없음.
- 테스트: Python 176개 통과·3개 건너뜀(Go 없음). Go 테스트 40개는 토론·인증에 집중되어 있고 메트릭·sshd·명령 실행·`main.ts`는 테스트 없음. 분석 환경에 Go·node가 없어 `go test`와 프론트엔드 빌드는 실행하지 못함.
- `packages/guardian-ui`: React 18 컴포넌트 24개 디자인 시스템이지만 앱 어디에도 연결되지 않음.

## 6. 잘 되어 있는 점

- 토론 작업은 셸 없이 argv로 실행, 프로세스 그룹 단위 종료.
- 조치 상태 전환이 원자적, 제안 시점과 실행 직전 이중 검증, 코드 변경 여부(base_sha) 확인.
- 위험도는 LLM이 아니라 카탈로그에서 결정, 고위험은 조치 번호 직접 입력.
- jev 관문 fail-closed, `GUARDIAN_UI_ACTIONS=off` 차단 스위치.
- 쓰기 도구는 별도 작업 클론으로 제한, 실행 중 저장소·보호 브랜치 거부, push 안 함.
- 256비트 토큰, 상수 시간 비교, HttpOnly + SameSite=Strict 쿠키, 기본 loopback 바인딩.
- `actions.ts`, `debate.ts`는 이스케이프 일관 적용.

## 7. 권장 진행 순서

1. 보안 핫픽스: 2절 표의 1~5번.
2. 가짜 데이터 제거: 실패 시 오류 상태 표시, AI 탭 제거 또는 실제 조치 승인 흐름에 연결.
3. CI 구축: gofmt, `go vet`, `go test -race`, pytest, shellcheck, JSON 스키마 검증.
4. LLM 계층 안정화: 전환 시 맥락 재전송, 프롬프트 크기 상한, 라운드별 기록 저장, 조치 안전장치 보강.
5. 권한 분리와 설치 롤백, sshd 감사·메트릭 재작성.

## 8. 1단계 핫픽스 진행 상태

1단계 작업을 시작했다가 중단했습니다. 부분 수정은 **리뷰와 테스트를 거치지 않아 커밋하지 않았고**, 로컬 stash에 보관했습니다.

- stash 이름: `wip: phase-1 security hotfix (partial, unreviewed, untested) 2026-10-04`
- 포함 파일: `apps/agent/main.go`, `apps/agent/security.go`(신규), `apps/web-desktop/{package.json, src-tauri/tauri.conf.json, src/actions.ts, src/debate.ts, src/main.ts, src/escape.ts(신규), tests/}`, `cli/llm-agent/env_policy.py`(신규)
- 이어서 하려면 `git stash list`로 확인 후 `git stash apply`.

재개 시 결정 사항:
- 토큰이 비면 시작 거부. 예외는 `GUARDIAN_INSECURE_NO_AUTH=1`이고 모든 바인딩이 loopback일 때.
- AI 탭 실행 버튼은 제거. 이스케이프만 고치면 하드코딩된 고위험 sshd 명령이 실제로 실행되기 때문.
- 서버 전체 명령 SSE의 POST 전환과 localStorage 토큰 정리는 2단계.
- 남는 위험: 환경 변수에서 토큰을 빼도 agy가 같은 `ubuntu` 계정이면 `~/.guardian/agent.env`를 직접 읽을 수 있음. 계정 분리(6번)로 해결.

**완료 (2026-10-04)**: stash의 부분 수정을 검토·완성·테스트해 커밋하고 허브에 배포했다. 2절 1~5번과 3절 프론트엔드 항목이 해당된다.
`d13adc1`(agent: 토큰 필수, Host/Origin/Content-Type 검사, CSP, `target_host` 검증), `637b446`(대시보드 이스케이프, CSP, 가짜 데이터 제거),
`80fc18e`(`run_tests` 플래그 허용 목록·위험도 high, 하위 프로세스·provider별 최소 환경), `ef44366`(`AGENT_ALLOWED_HOSTS`).
검토 중 stash에 없던 구멍 3개(Origin 없는 cross-site GET, 빈 본문 form POST, CORS `*`)도 막았다. 6번(계정 분리)과 SSE의 POST 전환은 남아 있다.
stash `wip: phase-1 security hotfix ...`는 이 커밋들로 대체되었다.

**6번 진행 (2026-10-05, `275c89b`)**: 결정에 따라 실행 계정은 `ubuntu`로 두고, LLM CLI도 agent와 같은 계정에서 돈다. 대신 systemd 격리와 fleet 키 보호를 적용했다.
- fleet 개인키는 `/etc/guardian/fleet_key`(root 0600)로 옮겼다. 키는 `guardian-fleet-agent.service`의 ssh-agent 소켓으로만 쓰인다. 허브의 `~/.ssh/guardian_fleet`는 삭제했다.
- `server-guardian-agent`와 `db-backup`에 `NoNewPrivileges`를 적용했다. 그래서 agent·LLM CLI 프로세스 트리 전체에서 sudo가 막힌다. `InaccessiblePaths`(fleet 키, `id_ed25519`, `backup.env`, headscale 설정), `ProtectSystem=full`, `PrivateTmp`, 커널 보호 옵션도 적용했다.
- `systemd-analyze security` 점수는 9.2 UNSAFE에서 4.0 OK가 됐다.
- 허브 검증 결과는 다음과 같다:
  - agent 네임스페이스 안에서 키 읽기와 sudo가 차단된다.
  - fleet SSH는 23대 모두 성공했다.
  - claude·codex·agy가 모두 PONG으로 응답했다.
  - 백업 서비스 수동 실행과 복제가 성공했다.
- 남는 위험: 결정에 따라 키를 쓰는 권한은 그대로다. 즉 ssh로 fleet 서버에 접속해 그 서버에서 sudo를 쓸 수 있다. 또 `~/.guardian/agent.env`와 `llm.env`는 agent 트리 안에서 여전히 읽힌다. `agent_client.py`와 `llm_runner.py`가 직접 읽기 때문이다. 이를 막으려면 LLM CLI를 별도 계정으로 분리해야 한다.
- 후속 (2026-10-05): 허브 sudo를 설치 옵션으로 만들었다. `--hub-sudo`가 기본값이고 `--no-hub-sudo`로 끈다. 켜면 보안 점수는 8.5이고 root가 키를 읽을 수 있다. 끄면 4.0이다.
  다만 허브 자신도 fleet SSH 대상이라 `ssh site-a-arm1 sudo …` 우회로가 남는다. 사용자 결정으로 이 우회로는 그대로 둔다.

**SSE POST 전환 (2026-10-05)**: `/api/fleet/exec/stream`은 POST 전용이 됐다. 본문은 JSON `{command, targets}`이고, GET은 405로 거부한다. 명령이 URL에 실리지 않으므로 접근 로그와 브라우저 기록에 남지 않는다. 요청은 다른 POST와 똑같이 CORS preflight와 Content-Type 검사를 거친다.
- 대시보드는 `EventSource` 대신 fetch로 SSE 본문을 읽는다. 토큰은 `Authorization` 헤더로 보낸다.
- 이 경로에서는 `?access_token=`을 받지 않는다. 쿼리 토큰은 읽기 전용인 `/api/stream/metrics`에만 남는다(현재 대시보드는 쓰지 않음).
- Python `stream_fleet_exec`도 POST로 바꿨다.
- 작업 중 `scripts/ci.sh`의 버그를 고쳤다. `( … ) || fail` 구문 안에서는 `set -e`가 꺼져서, `tsc`나 `go vet`이 실패해도 블록의 마지막 단계만 성공하면 통과했다.
- 남은 2단계 항목은 localStorage 토큰 정리다. 교차 출처 클라이언트(Tauri)만 토큰을 보관한다.

**localStorage 토큰 정리 (2026-10-05)**: 대시보드는 이제 `AGENT_TOKEN`을 localStorage에 저장하지 않는다.
- 허브 대시보드(같은 출처)는 지금처럼 HttpOnly 세션 쿠키만 쓴다.
- Tauri 앱은 토큰을 메모리에만 둔다. 영속 저장은 OS 자격 증명 저장소(macOS 키체인, Windows 자격 증명 관리자, Linux keyutils)를 쓴다. Rust 명령 `token_get`/`token_set`/`token_delete`(`src-tauri/src/token_store.rs`)가 agent origin별로 한 항목씩 관리한다. 그래서 agent 주소를 바꿔도 다른 agent의 토큰이 전송되지 않는다.
- 예전 버전이 남긴 `guardian_agent_token`은 첫 실행 때 처리한다. Tauri 앱에서는 키체인으로 옮기고, 그 밖의 환경에서는 지운다.
- 키체인을 쓸 수 없으면 앱이 켜져 있는 동안만 유지되는 세션으로 동작한다.
- Linux의 keyutils 저장소는 로그아웃하면 사라진다.
- 2단계 항목 중 남은 것은 기타(Low) 항목뿐이다: 세션 쿠키 수명·무효화·Secure, `openssl -pass`, AES-CBC MAC, agy argv.

**기타(Low) 항목 정리 (2026-10-05)**
- **세션 쿠키**:
  - 값이 `v2.<발급 시각>.<무작위 nonce>.<HMAC>`이 됐다. 7일 뒤 만료되고(이전 30일, 만료 없는 고정값), 로그인할 때마다 값이 달라진다.
  - 로그아웃은 POST 전용이다. 해당 nonce를 서버에서 폐기하므로 탈취된 쿠키도 로그아웃 뒤에는 쓸 수 없다.
  - HTTPS로 접속하면 `Secure`를 붙인다.
  - `AGENT_TOKEN`을 바꾸면 모든 세션이 무효가 된다.
  - 폐기 목록은 메모리에 있어 agent를 재시작하면 잊힌다. 그래도 7일 만료로 상한이 있다.
  - 기존 v1 쿠키는 거부되므로 배포 후 한 번 다시 로그인해야 한다.
- **자격 증명 번들**(`export-credentials.sh`/`import-credentials.sh`):
  - 암호화는 `scripts/creds_crypto.py`가 맡는다. 비밀번호는 openssl에 파이프(`-pass fd:N`)로 넘기므로 `ps`에 보이지 않는다.
  - 형식 v2는 AES-256-CBC 뒤에 HMAC-SHA256을 붙인다(encrypt-then-MAC). PBKDF2-SHA256은 600,000회이고, MAC 키는 별도로 유도한다. 변조되거나 잘린 파일은 복호화 전에 거부한다.
  - 예전 형식 파일은 `--legacy`를 붙여야만 연다.
  - export는 비밀번호 확인 입력을 받는다.
- **agy 프롬프트**: argv(`--print=<프롬프트>`) 대신 stdin의 stream-json(`{"event":"user","message":{"content":…}}`)으로 보낸다. 세션 재개(`--conversation`)도 같은 방식이다.

**3절 정확성 항목 일부 처리 (2026-10-05, `45dff85` 이후)**
- **Fleet 인벤토리:** 상태를 `servers.json`의 고정값 대신 페이지를 열 때 실행하는 진단 결과로 표시한다. 정적 `status`와 `tailscale_status` 필드는 삭제했다.
- **sshd 감사:**
  - root(허브 sudo)가 있으면 `sshd -T`로 실제 적용되는 설정을 읽는다. 허브는 `Include`된 파일에서 `PasswordAuthentication no`를 적용하고 있어, 점수가 50에서 75로 바로잡혔다.
  - root가 없으면 설정 파일을 직접 읽는다. 처음 값을 쓰고, Include는 따라가고, Match 블록은 건너뛴다. 이 경우 문법 검사는 "not checked"로 표시한다.
  - 실패 개수는 FAIL만 센다. WARN과 INFO는 따로 집계한다.
- **authorized_keys:** 지문은 `ssh-keygen -lf`와 같은 값으로 계산한다. `from=` 같은 옵션이 붙은 줄도 해석하고, 옵션을 화면에 표시한다.
- **macOS 메모리:** 고정값 65% 대신 vm_stat의 active+wired+compressed로 계산한다. 메모리를 읽지 못하면 추정치 대신 0을 보고한다.
- **남은 3절 항목:** uptime이 agent 가동 시간인 점, CPU% 계산 방식, `/api/status`가 항상 healthy인 점, `llm_runner.py`의 서버 수 기본값 21.
- **나머지 3절 항목 처리 (2026-10-05):**
  - uptime: 이제 호스트 부팅 이후 시간이다. Linux는 `/proc/uptime`, macOS는 `kern.boottime`을 읽는다. 이전에는 agent 가동 시간이었다.
  - CPU%: Linux는 `/proc/stat` 두 시점의 차이로 계산하고, macOS는 `top`의 1초 샘플을 쓴다. 이전에는 loadavg를 코어 수로 나눈 값이었다.
  - 디스크 기본값(100GB/40%): 읽지 못하면 0과 `issues`로 보고한다.
  - `/api/status`: 메트릭을 읽지 못하거나 디스크 ≥90%, 메모리 ≥95%이면 `degraded`와 `reasons`를 반환한다. 대시보드 배지는 online/degraded/offline 세 가지로 표시한다.
  - `llm_runner` 내장 엔진: 진단 결과가 없을 때 쓰던 서버 수 기본값 21을 없앴다. "매우 안정적" 같은 고정 문구와 보안 점수 기본값 50도 없앴다. 이제 측정값만 다시 서술한다.
  - 내장 엔진 고정 문구 제거 (2026-10-05):
    - 판정자·비평자·전문가 역할에서 지어낸 판정("APPROVED")과 비평·제안을 돌려주던 것을 "응답을 만들 수 없음" 안내로 바꿨다.
    - 페르소나의 시스템 프롬프트를 답처럼 되돌려주던 것을 없앴다.
    - 마지막 기본 응답 "정상 범위 내에 있습니다"를 없앴다.
    - 모든 내장 엔진 응답은 "⚠️ [LLM 응답 없음]" 배너와 실패 원인으로 시작한다.
    - 메트릭 요약을 만들고도 돌려주지 않던 버그를 고쳤다.
    - 대시보드에서 쓰이지 않던 `queryMultiAgent`를 삭제했다. 고정된 "AI 종합 의견"과 하드코딩된 조치가 들어 있었다.

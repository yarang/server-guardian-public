# 상주 Codex, 산출물 반출, 패키지·업데이트 정책 개정 (설계)

[English](resident-codex-and-policy.md) | 한국어

상태: 2026-11-22 결정(운영자가 PD1, PD2, PD3-a, PD4 확정; 1–3단계 구현 및 `2a3bfaf` 배포 완료, 샌드박스 프로브 `827bc3cc0a6b` 통과).

이 문서는 [resident-agents.ko.md](resident-agents.ko.md)(M0)와
[resident-open-tools.ko.md](resident-open-tools.ko.md)(OD1, OD3, OD4, OD6)에 대해 서로 연결된 세 가지 정책·설계
개정을 제안한다.

1. ChatGPT 구독 로그인(`auth.json`)을 쓰는 **`codex`**를 `tools: "open"` 샌드박스 상주 에이전트로 허용한다
   (`resident-open-tools.ko.md` 규칙 7 및 O4 단계 실현).
2. 개별 상주 에이전트가 만든 결과물이 `0700` 샌드박스 홈에 갇히거나 턴·에이전트 종료 시 사라지지 않도록 **지속
   출력 디렉터리와 허브 산출물 반출 경로**를 추가한다.
3. 동료 메일 프롬프트 주입으로부터 노드 자격증명과 플릿 SSH 키를 지키면서도, 상주 에이전트가 **프로그램 설치와
   시스템 업데이트**를 지원할 수 있도록 정책을 개정한다.

## 1. 현재 정책이 세 가지 목표를 막는 이유

| 목표 | 현재 코드 상태 | 정확한 차단 원인 |
|---|---|---|
| `codex` 상주 에이전트 추가 | `configs/resident-providers.json`에 `codex`가 `"resident": false`(`0.163.0`)로 고정. `resident_registry.py`, `resident_open.py`의 `OPEN_PROVIDERS = ("claude", "agy")`. `guardian-sandbox-launch.py`, `sandbox-remote-install.sh`도 `claude`·`agy`만 허용. | **닫힌 도구** 기준(M0 2층)에서 `apply_patch`와 협업 도구가 남아 실패했고, 규칙 7에서 `open` 허용을 예고했음에도 O2 구현은 `claude`·`agy`만 연결했다. |
| 비-Git 에이전트 산출물 보존·반출 | `/home/gsa-<name>` 자체는 디스크에 유지되지만(`BindPaths=/home/gsa-<name>`), `/tmp`는 `PrivateTmp=yes`라 매 턴 끝날 때 사라진다. 샌드박스 밖으로 나가는 통로는 `task_worktree`의 git `patch` 하나뿐이다(OD4). | 코드 작업의 git worktree 밖에서 만든 보고서, 스크립트, 다이어그램, 빌드 결과물, 진단 번들은 `0700` `/home/gsa-<name>` 안에 갇혀 허브·대시보드·다른 에이전트가 꺼낼 수 없고, `/tmp`에 쓰거나 에이전트를 제거하면 사라진다. |
| 프로그램 설치와 시스템 업데이트 지원 | `guardian-sandbox-run.sh`가 `ProtectSystem=strict`, `NoNewPrivileges=yes`, 빈 `CapabilityBoundingSet=`, `PATH=/usr/local/bin:/usr/bin:/bin`을 강제하고, OD6은 호스트·플릿 변경을 허브와 사람에게만 남긴다. | `$HOME/.local/bin`의 사용자 공간 도구도 `PATH`에 없고, OS 수준 `apt update/upgrade`나 `apt install`은 상주 에이전트가 요청하거나 수행할 경로가 전혀 없다. |

## 2. 축 1: `tools: "open"` 상주 에이전트로서의 `codex`

### 등록과 M0 표
- `tools: "closed"`에 대해서는 `codex` `"resident": false`를 유지한다(M0 닫힌 도구 기준 약화 없음).
- `configs/resident-providers.json`에 `"open_resident": true`(고정 버전 `"0.163.0"`)를 명시하고, 아래 경로의
  `OPEN_PROVIDERS`를 `("claude", "agy", "codex")`로 확장한다.
  - `cli/llm-agent/resident_registry.py`(`tools == "open"`일 때 `open_resident: true`와 노드 샌드박스 프로브
    통과를 검사),
  - `cli/llm-agent/resident_open.py`(`OPEN_PROVIDERS`, `CODEX_VERSION`, `expected_cli_versions()`),
  - `deploy/sandbox/guardian-sandbox-launch.py`(`PROVIDERS`, `build_env`, `codex_auth`),
  - `scripts/lib/sandbox-remote-install.sh`(`--cli codex=<버전>`, 노드 사용자의 `~/.local/bin/codex` 또는
    `/usr/bin/codex`를 root 소유 `/usr/local/lib/guardian-sandbox/cli/codex-<버전>/`으로 복사).

### 인증 (ChatGPT 구독 로그인 `auth.json`)
OD3(에이전트별 폐기 가능한 자격증명)와 상시 규칙(노드 간 `auth.json` 복사 금지 — 리프레시 토큰 회전 시 `401`
발생)을 지키기 위해 다음 방식을 따른다.
- `codex` 열린 에이전트마다 전용 `auth.json`을 `/home/gsa-<name>/.codex/auth.json`(`0600`, 소유자 `gsa-<name>`,
  `CODEX_HOME=/home/gsa-<name>/.codex`)에 둔다.
- `scripts/sandbox-credential.sh`에 `--provider codex`를 추가한다.
  - stdin으로 전용 `auth.json`을 설치하거나(로그·인자·환경 변수 미노출, 다른 노드 복사 금지), 대상 노드에서
    `gsa-<name>`으로 직접 `codex login --device-auth`를 실행해 에이전트 전용 ChatGPT 세션을 발급한다.
- `guardian-sandbox-launch.py`는 `$HOME/.codex/auth.json`의 존재, `gsa-<name>` 소유, `0600` 권한을 확인하고
  어긋나면 종료 코드 3(`launch: no_credential`)으로 거부한다.

### 턴 실행과 이어 가기
- `resident_driver.py`가 `guardian-sandbox-launch.py`를 통해 샌드박스 유닛 안에서 `codex`를 실행한다.
  - 첫 턴: `codex exec --json --skip-git-repo-check -s danger-full-access -a never -C <workdir>`(바깥 `systemd`
    유닛이 실제 OS 샌드박스이며, `RestrictNamespaces=yes` 아래에서는 내부 bwrap/Landlock이 실패한다).
    `-c mcp_servers.mailbox...`와 `-c mcp_servers.repo...`로 샌드박스 소켓의 MCP 사본을 연결한다.
  - 다음 턴: `thread.started`에서 읽은 `thread_id`로 `codex exec resume <thread_id> --json ...` 실행.
  - `CODEX_HOME=$HOME/.codex`로 격리해 노드 사용자의 스킬·설정이 새어 들어오지 않게 한다.

## 3. 축 2: 지속 출력과 허브 산출물 반출 (OD4 개정)

상주 에이전트가 만든 결과물이 샌드박스에 갇히거나 유실되지 않도록 출력·반출 경로를 정식화한다.

### `/home/gsa-<name>` 내부 지속 구조
- `/home/gsa-<name>/work/`: 작업 디렉터리(턴 사이 유지, 코드 작업 git worktree는 `work/tasks/`).
- `/home/gsa-<name>/out/`: 반출 대상 산출물 전용 디렉터리(턴 사이 유지, `/tmp` 사용 금지).
- `/home/gsa-<name>/.local/bin`: `guardian-sandbox-launch.py`의 `CLI_PATH`에 추가
  (`$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin`)해 에이전트가 설치한 사용자 공간 도구가 턴을 넘어 유지되게 한다.

### 허브 산출물 반출 (`artifact_publish`)
`task_update` 패치와 나란히 `mailbox_mcp.py`(열린 에이전트), `resident_node.py`, `mail_relay.py`, 허브 저장소에
타입이 있는 산출물 반출 경로를 추가한다.

1. **MCP 도구 `artifact_publish(path, title, task=None, summary="")`**:
   - `/home/gsa-<name>/out/`(또는 `work/`) 안의 일반 파일만 `O_NOFOLLOW`로 읽어 심볼릭 링크·경로 탈출을 막는다.
   - 크기 상한(기본 `10 MiB`, 설정 가능)을 적용하고, 텍스트 파일은 노드를 떠나기 전 `mask_secrets`로 비밀값을
     가린다(바이너리는 sha256 + 허용 확장자/MIME 검사).
2. **허브 영구 보관**:
   - 허브 `~/.guardian/data/resident-artifacts/<agent>/<artifact_id>/`에 저장하고 SQLite
     `resident_artifacts` 테이블(`id`, `agent`, `task_id`, `filename`, `sha256`, `size_bytes`, `created`,
     `expires`)에 메타데이터를 기록한다.
   - 야간 백업(`server-guardian-db-backup.timer`) 대상에 포함한다.
   - 기본 90일 보관(운영자가 고정 가능)하며, 상주 에이전트를 제거해도 이미 반출된 허브 산출물은 지우지 않는다.
3. **열람과 내려받기**:
   - 대시보드 **업무 흐름 > 상주 에이전트**(`#/debates/residents`)와 CLI
     (`guardian.py --resident-artifacts [AGENT]`, `--resident-artifact-get <ID>`)에서 미리 보기·다운로드를 제공한다.
   - `mail_send` / `task_update`에 산출물 ID를 첨부해 다른 에이전트나 운영자 세션이 바로 찾을 수 있게 한다.

등록된 Git 프로젝트의 코드 변경은 기존대로 `task_update` → `PROPOSED jev_mcp:apply_patch` 경로를 유지해 리뷰·diff·
**게시(Publish)** 흐름을 그대로 쓴다.

## 4. 축 3: 시스템 업데이트와 프로그램 설치 정책 개정 (OD1·OD6 개정)

프로그램 설치를 권한 계층에 따라 두 단계로 나눈다.

### 1단계: 사용자 공간 프로그램 설치 (`tools: "open"`에서 항상 허용)
- `/home/gsa-<name>`에 쓰기가 가능하고 `$HOME/.local/bin`이 `PATH`에 포함되므로, 모든 열린 상주 에이전트는
  `uv`, `pipx`, `python3 -m venv`, `npm --prefix ~/.local`, `cargo`, 단독 바이너리 등을 `sudo` 없이 턴 도중 즉시
  설치하고 다음 턴에서도 계속 사용할 수 있다. 호스트 격리 경계는 전혀 약화되지 않는다.

### 2단계: OS 수준 시스템 업데이트와 시스템 패키지 (`apt`, `systemctl`)
운영자 결정 **PD3**으로 세 가지 방안을 제시한다.

#### 방안 PD3-a (권장): 허브 중개 운영 액션 제안 (`ops_propose`)
- 상주 에이전트는 `gsa-<name>` 비root 상태를 유지한다.
- 에이전트에 `ops_propose_package`, `ops_propose_update` MCP 도구를 제공해, 허브의 기존 `ops_packages.py`,
  `package_roles.py` 보호 목록, preview/drift/verify, `exec.jsonl` 감사 로그를 그대로 타는 `PROPOSED` 액션·계획을
  제출하게 한다.
- 타입이 있는 `ops:system_update` 액션(`apt-get update` + `apt-get upgrade --with-new-pkgs` 또는 보안 업데이트
  범위; `apt list --upgradable` 미리 보기 및 사후 검증 포함)을 카탈로그에 추가한다.
- **승인 정책**:
  - 임의 `ops:package_install` / `ops:package_remove` / 전체 `apt upgrade`는 **승인** 화면(또는 CLI
    `--run-action`)에서 1클릭 확인 후 허브가 실행한다.
  - `policy.auto_run`을 켠 프로젝트에서는 `apt update`, 보안 업데이트, 검증된 번들 설치를 `jev` 통과 시 자동
    실행할 수 있다.
- **축 2와의 결합 효과**: 산출물 유실 우려는 축 2(`artifact_publish`)가 해소하고, 개발 도구는 1단계 사용자 공간
  설치로 즉시 해결하며, OS 전역 변경은 동료 메일 프롬프트 주입으로부터 안전하게 보호된다.

#### 방안 PD3-b: 범위 제한 root 패키지 래퍼 (`tools: "host-ops"` 모드)
- `closed`, `open` 외에 지정 노드용 세 번째 선택 모드 `tools: "host-ops"`를 도입한다.
  - `guardian-sandbox-run.sh`의 홈·SSH·`llm.env`·tailnet 격리는 유지하되, 허용 명령 목록(`apt-get update`,
    `apt-get upgrade`, `apt-get install <검증된 패키지명>`, `apt-get autoremove`)만 받는 고정 root 헬퍼
    `/usr/local/sbin/guardian-pkg-run` 호출을 허용한다.
  - **필수 가드**: `host-ops` 에이전트는 반드시 `peers: []`(`operator` 또는 명시적으로 신뢰한 로컬 운영자 세션
    전용 — 다른 상주 에이전트의 메일 차단)로 제한해 동료 메일이 root `postinst` 스크립트를 유발하지 못하게 한다.
  - 모든 호출을 허브 감사 로그에 기록한다.

#### 방안 PD3-c: 하이브리드 (모든 `open` 에이전트에 1단계 + PD3-a 적용, 필요 시 `operator` 전용 업데이트 에이전트 1개에만 PD3-b 선택 허용)
- 일반 열린 에이전트(`coder`, `codex`)는 사용자 공간 설치 + 허브 승인 OS 제안 + 산출물 반출을 쓰고, 턴 도중
  즉시 OS 패키지 설치가 꼭 필요한 경우에만 `operator` 전용 에이전트에 한해 PD3-b를 켠다.

## 5. 유지되는 보안 불변 조건

PD3에서 무엇을 고르든 아래 원칙은 그대로 유지한다.
- 노드 사용자의 `~/.ssh`, `~/.guardian/llm.env`, `/etc/guardian/fleet_key`, `worker.sock`은 상주 에이전트에서
  계속 가린다.
- 동료 메일은 권한을 주지 않는다(`INBOX_PREAMBLE` 및 시작 카드 규칙 유지).
- `git push`, 허브 배포, 다중 노드 플릿 변경은 허브와 사람에게 남는다(OD6).
- 기존 `claude@node-1`, `agy@node-1`은 명시적으로 바꾸기 전까지 `tools: "closed"`를 유지한다.

## 6. 단계별 구현 계획

1. **1단계 — `tools: "open"`에 `codex` 지원 + 지속 `$HOME/.local/bin` 및 `$HOME/out/`**:
   - `configs/resident-providers.json`, `resident_registry.py`, `resident_open.py`, `resident_driver.py`,
     `deploy/sandbox/guardian-sandbox-launch.py`, `scripts/lib/sandbox-remote-install.sh`,
     `scripts/sandbox-credential.sh` 수정 및 단위 테스트 추가.
   - `scripts/ci.sh` 통과 → 허브 배포 → `node-1` 워커·샌드박스 재설치(버전 갱신 및 `--record` 프로브) →
     `gsa-codex` ChatGPT 로그인 설정 → `codex@node-1` 등록.
2. **2단계 — 샌드박스 산출물 반출 (`artifact_publish`)**:
   - 허브 `resident_artifacts` 테이블·저장소, `artifact_publish` MCP 도구, 릴레이 전달, CLI
     (`--resident-artifacts` / `--resident-artifact-get`), 대시보드 **상주 에이전트** 다운로드, 야간 백업 포함.
3. **3단계 — 시스템 업데이트와 OS 패키지 설치 (PD3 결정에 따름)**:
   - `ops:system_update` + 상주 제안 도구(PD3-a), 또는 `guardian-pkg-run` + `tools: "host-ops"`(PD3-b), 또는
     하이브리드(PD3-c) 구현·테스트·배포.

## 7. 운영자 결정 (2026-11-22)

네 항목 모두 2026-11-22 운영자가 확정했다.

| # | 항목 | 결정 (2026-11-22) |
|---|---|---|
| **PD1** | `codex` 상주 모드 | **`tools: "open"` 허용**(`closed`는 계속 거부; `resident-open-tools.ko.md` 규칙 7·O4와 일치) |
| **PD2** | `codex` ChatGPT 인증 | **`gsa-codex` 전용 `/home/gsa-codex/.codex/auth.json`**(`codex login --device-auth`, 노드 사용자와 토큰 회전 충돌 방지) |
| **PD3** | 시스템 업데이트·OS 패키지 설치 정책 | **PD3-a**: 허브 중개 운영 액션 제안(`ops_propose_package` / `ops_propose_update`) + 타입이 있는 `ops:system_update`(승인 큐, `package_roles.py` 보호, 감사 로그, 저위험 업데이트 `policy.auto_run` 선택 재사용) |
| **PD4** | 샌드박스 산출물 반출 | **채택**: 지속 `$HOME/out/`·`$HOME/.local/bin` + `artifact_publish`로 허브 영구 저장(`~/.guardian/data/resident-artifacts/`, 90일 보관, 야간 백업 포함) |
